"""Manual edit surface for ship data and scheduling.

Two write paths layered over the existing data layer, kept out of ``main.py``
(mirroring ``app/intake/manual.py``):

1. **Edit a vessel** — correct the canonical ``vessel`` row. AIS names are
   misspelled and dimensions are sometimes missing/wrong; an operator fixes them
   here. Unlike intake (which only *fills* NULLs, leaving AIS authoritative), a
   manual edit is **authoritative**: the fields present in the request overwrite.

2. **Create / edit / cancel / delete a reservation** — the scheduling rectangle
   in (time) x (station) space. This includes **assigning a station range**
   (the manual form of berth reconciliation) and **promoting toward**
   ``confirmed``. Confirmed-vs-confirmed overlaps are rejected by the DB
   exclusion constraint (``no_wharf_overlap``); the endpoint surfaces that as a
   409 rather than blocking it here.

Units: vessel dimensions are stored and edited in **metres** (the canonical AIS
store), distinct from the feet used on the paper berth-request form. Station
ranges are entered directly in canonical **POPA feet** — no crosswalk transform
is involved (the user supplies canonical station), so this does not route
stationing math outside ``app/crosswalk.py``.

The range/validation helpers (``_station_range``, ``_time_range``,
``confirm_warnings``) are pure and unit-tested without a DB; the ``*_vessel`` /
``*_reservation`` functions touch the session and do NOT commit — the endpoint
owns the transaction boundary.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from pydantic import BaseModel
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from app.models import (
    DIRECTIONS,
    RESERVATION_SOURCES,
    RESERVATION_STATUSES,
    RESERVATION_TYPES,
    Vessel,
)

# Validation before a reservation may be confirmed (CLAUDE.md): draft must be
# checked against controlling depth for the station/time window. No
# controlling-depth data layer exists yet, so we cannot enforce it — surface the
# gap as a warning instead of silently skipping it or inventing depth data.
_DEPTH_GATE_WARNING = (
    "controlling-depth data layer not available — draft was NOT validated "
    "against controlling depth for this station/time window before confirming"
)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class VesselUpdate(BaseModel):
    """Partial vessel edit. Only fields explicitly present overwrite; omitted
    fields are left untouched (use ``model_dump(exclude_unset=True)``)."""

    name: str | None = None
    imo: int | None = None
    mmsi: int | None = None
    callsign: str | None = None
    ship_type: int | None = None
    loa: float | None = None   # metres
    beam: float | None = None  # metres
    draft: float | None = None  # metres
    destination: str | None = None


class ReservationCreate(BaseModel):
    """A new reservation. ``etb`` is required (a reservation must have a time
    range); everything else is optional. Station bounds are canonical POPA feet
    and may be omitted (berth unassigned -> empty range, never conflicts)."""

    type: str = "vessel"
    vessel_id: int | None = None
    etb: dt.datetime
    etd: dt.datetime | None = None
    # Assign a named berth: its catalog range fills station_range. Explicit
    # station_lo/hi (a sub-span for a vessel shorter than the berth) override it.
    berth_id: int | None = None
    station_lo: float | None = None
    station_hi: float | None = None
    direction: str | None = None
    status: str = "tentative"
    source: str = "operator"
    priority: int | None = None
    cargo: str | None = None
    notes: str | None = None


class ReservationUpdate(BaseModel):
    """Partial reservation edit. Fields absent from the request are left as-is;
    time and station ranges are recomputed from the merge of existing bounds and
    whatever the request supplies. Set ``unassigned=True`` to clear the berth
    (empty station range)."""

    type: str | None = None
    vessel_id: int | None = None
    etb: dt.datetime | None = None
    etd: dt.datetime | None = None
    # Reassign the berth (its range fills station_range unless explicit
    # station_lo/hi are also given). ``unassigned=True`` clears both the berth
    # and the station range.
    berth_id: int | None = None
    station_lo: float | None = None
    station_hi: float | None = None
    unassigned: bool | None = None
    direction: str | None = None
    status: str | None = None
    priority: int | None = None
    cargo: str | None = None
    notes: str | None = None


# ---------------------------------------------------------------------------
# Pure helpers (no DB) — unit-tested directly
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StationRange:
    """A resolved station range. ``empty`` -> unassigned berth (``'empty'``
    numrange); otherwise ``[lo, hi]`` inclusive in POPA feet."""

    empty: bool
    lo: float | None = None
    hi: float | None = None


@dataclass(frozen=True)
class TimeRange:
    """A resolved ``[lower, upper)`` time window. ``upper`` may be ``None``
    (open-ended — still alongside / no ETD)."""

    lower: dt.datetime | None
    upper: dt.datetime | None


def _station_range(lo: float | None, hi: float | None, unassigned: bool) -> StationRange:
    """Resolve a station range from user input. ``unassigned`` wins (empty
    range). Otherwise both bounds are required and must be ordered ``lo < hi``.
    Raises ``ValueError`` on a single bound or an inverted/zero-width range."""
    if unassigned:
        return StationRange(empty=True)
    if lo is None and hi is None:
        return StationRange(empty=True)
    if lo is None or hi is None:
        raise ValueError("station range needs both lo and hi (or mark unassigned)")
    if lo >= hi:
        raise ValueError(f"station lo ({lo}) must be less than hi ({hi})")
    return StationRange(empty=False, lo=float(lo), hi=float(hi))


def _station_range_for(
    berth_bounds: tuple[float, float] | None,
    lo: float | None,
    hi: float | None,
    unassigned: bool,
) -> StationRange:
    """Resolve the station range when a berth may be assigned.

    Precedence: ``unassigned`` clears the range; otherwise explicit ``lo/hi``
    win (a vessel shorter than the berth occupies a sub-span); otherwise an
    assigned berth supplies its full catalog range; otherwise fall back to the
    plain bound resolution (empty when both bounds are None — berth unassigned).
    """
    if unassigned:
        return StationRange(empty=True)
    if lo is None and hi is None and berth_bounds is not None:
        b_lo, b_hi = berth_bounds
        return StationRange(empty=False, lo=float(b_lo), hi=float(b_hi))
    return _station_range(lo, hi, unassigned=False)


def _time_range(etb: dt.datetime | None, etd: dt.datetime | None) -> TimeRange:
    """Resolve a ``[etb, etd)`` window. ``etd`` may be omitted (open-ended).
    Raises ``ValueError`` if ``etd`` precedes ``etb``."""
    if etb is not None and etd is not None and etd < etb:
        raise ValueError("ETD must not precede ETB")
    return TimeRange(lower=etb, upper=etd)


def _validate_enum(value: str | None, allowed: tuple[str, ...], label: str) -> None:
    if value is not None and value not in allowed:
        raise ValueError(f"invalid {label} {value!r}; expected one of {allowed}")


def confirm_warnings(status: str | None) -> list[str]:
    """Warnings to surface when moving a reservation to ``confirmed`` (the
    draft-vs-controlling-depth gate that cannot be enforced yet)."""
    return [_DEPTH_GATE_WARNING] if status == "confirmed" else []


# ---------------------------------------------------------------------------
# SQL fragments for the resolved ranges
# ---------------------------------------------------------------------------
def _station_sql(sr: StationRange, params: dict) -> str:
    if sr.empty:
        return "'empty'::numrange"
    # Cast to numeric explicitly: float params bind as float8 and numrange() has
    # no float8 overload (only numeric), so an uncast call fails to resolve.
    params["slo"], params["shi"] = sr.lo, sr.hi
    return "numrange(CAST(:slo AS numeric), CAST(:shi AS numeric), '[]')"


def _time_sql(tr: TimeRange, params: dict) -> str:
    params["etb"], params["etd"] = tr.lower, tr.upper
    return "tstzrange(:etb, :etd, '[)')"


# ---------------------------------------------------------------------------
# Session functions — no commit; the endpoint owns the transaction
# ---------------------------------------------------------------------------
def _berth_bounds(session: Session, berth_id: int) -> tuple[float, float]:
    """Look up a berth's canonical POPA station range. Raises ``ValueError``
    (-> 422) if the berth id is unknown."""
    row = session.execute(
        text("SELECT popa_sta_start, popa_sta_end FROM berth WHERE id = :id"),
        {"id": berth_id},
    ).first()
    if row is None:
        raise ValueError(f"no such berth {berth_id}")
    return float(row.popa_sta_start), float(row.popa_sta_end)



def update_vessel(session: Session, vessel_id: int, upd: VesselUpdate) -> dict | None:
    """Overwrite the provided columns of a vessel. Returns a summary dict, or
    ``None`` if no such vessel (-> 404). ``mmsi``/``imo`` collisions or the
    "mmsi or imo required" check surface as IntegrityError at flush -> 409."""
    exists = session.execute(
        select(Vessel.id).where(Vessel.id == vessel_id)
    ).scalar_one_or_none()
    if exists is None:
        return None

    changes = upd.model_dump(exclude_unset=True)
    if changes:
        session.execute(
            update(Vessel)
            .where(Vessel.id == vessel_id)
            .values(**changes, updated_at=func.now())
        )
    return {"id": vessel_id, "updated_fields": sorted(changes)}


def create_reservation(session: Session, req: ReservationCreate) -> dict:
    """Insert a reservation. Validates enums + ranges (ValueError -> 422)."""
    _validate_enum(req.type, RESERVATION_TYPES, "type")
    _validate_enum(req.status, RESERVATION_STATUSES, "status")
    _validate_enum(req.source, RESERVATION_SOURCES, "source")
    _validate_enum(req.direction, DIRECTIONS, "direction")

    berth_bounds = _berth_bounds(session, req.berth_id) if req.berth_id else None
    sr = _station_range_for(berth_bounds, req.station_lo, req.station_hi, unassigned=False)
    tr = _time_range(req.etb, req.etd)

    params: dict = {
        "vessel_id": req.vessel_id,
        "berth_id": req.berth_id,
        "type": req.type,
        "direction": req.direction,
        "status": req.status,
        "source": req.source,
        "priority": req.priority,
        "cargo": req.cargo,
        "notes": req.notes,
    }
    station_sql = _station_sql(sr, params)
    time_sql = _time_sql(tr, params)
    rid = session.execute(
        text(
            f"""
            INSERT INTO reservation
                (vessel_id, berth_id, type, station_range, time_range, direction,
                 status, source, priority, cargo, notes, created_at)
            VALUES
                (:vessel_id, :berth_id, CAST(:type AS reservation_type),
                 {station_sql}, {time_sql},
                 CAST(:direction AS direction),
                 CAST(:status AS reservation_status),
                 CAST(:source AS reservation_source),
                 :priority, :cargo, :notes, now())
            RETURNING id
            """
        ),
        params,
    ).scalar_one()
    return {"id": int(rid), "warnings": confirm_warnings(req.status)}


def update_reservation(
    session: Session, res_id: int, upd: ReservationUpdate
) -> dict | None:
    """Edit a reservation. Time and station ranges are recomputed from the merge
    of the existing row and the supplied fields, so a partial edit (e.g. moving
    only the ETD, or only assigning a berth) preserves the other bound. Returns a
    summary dict, or ``None`` if no such reservation (-> 404)."""
    cur = session.execute(
        text(
            """
            SELECT type, vessel_id, berth_id, direction, status, priority, cargo,
                   notes,
                   lower(time_range) AS t_lo, upper(time_range) AS t_hi,
                   isempty(station_range) AS sta_empty,
                   lower(station_range) AS s_lo, upper(station_range) AS s_hi
            FROM reservation WHERE id = :id
            """
        ),
        {"id": res_id},
    ).first()
    if cur is None:
        return None

    changes = upd.model_dump(exclude_unset=True)

    # Scalar columns: take the supplied value, else keep the current one.
    type_ = changes.get("type", cur.type)
    vessel_id = changes.get("vessel_id", cur.vessel_id)
    direction = changes.get("direction", cur.direction)
    status = changes.get("status", cur.status)
    priority = changes.get("priority", cur.priority)
    cargo = changes.get("cargo", cur.cargo)
    notes = changes.get("notes", cur.notes)
    _validate_enum(type_, RESERVATION_TYPES, "type")
    _validate_enum(status, RESERVATION_STATUSES, "status")
    _validate_enum(direction, DIRECTIONS, "direction")

    # Time range: merge bounds.
    etb = changes.get("etb", cur.t_lo)
    etd = changes.get("etd", cur.t_hi)
    tr = _time_range(etb, etd)

    # Berth + station range. Precedence:
    #   unassigned -> clear both berth_id and the range;
    #   else a newly-assigned berth (no explicit bounds) -> its catalog range;
    #   else explicit lo/hi merged over the current range (berth_id unchanged);
    #   else keep the current range (and berth_id).
    has_bounds = "station_lo" in changes or "station_hi" in changes
    if changes.get("unassigned"):
        berth_id = None
        sr = StationRange(empty=True)
    elif "berth_id" in changes and changes["berth_id"] is not None and not has_bounds:
        berth_id = changes["berth_id"]
        sr = _station_range_for(_berth_bounds(session, berth_id), None, None, False)
    elif has_bounds:
        berth_id = changes.get("berth_id", cur.berth_id)
        lo = changes.get("station_lo", None if cur.sta_empty else float(cur.s_lo))
        hi = changes.get("station_hi", None if cur.sta_empty else float(cur.s_hi))
        sr = _station_range(lo, hi, unassigned=False)
    else:
        berth_id = changes.get("berth_id", cur.berth_id)
        if cur.sta_empty:
            sr = StationRange(empty=True)
        else:
            sr = StationRange(empty=False, lo=float(cur.s_lo), hi=float(cur.s_hi))

    params: dict = {
        "id": res_id,
        "type": type_,
        "vessel_id": vessel_id,
        "berth_id": berth_id,
        "direction": direction,
        "status": status,
        "priority": priority,
        "cargo": cargo,
        "notes": notes,
    }
    station_sql = _station_sql(sr, params)
    time_sql = _time_sql(tr, params)
    session.execute(
        text(
            f"""
            UPDATE reservation SET
                type = CAST(:type AS reservation_type),
                vessel_id = :vessel_id,
                berth_id = :berth_id,
                station_range = {station_sql},
                time_range = {time_sql},
                direction = CAST(:direction AS direction),
                status = CAST(:status AS reservation_status),
                priority = :priority,
                cargo = :cargo,
                notes = :notes
            WHERE id = :id
            """
        ),
        params,
    )
    return {"id": res_id, "warnings": confirm_warnings(status)}


def delete_reservation(session: Session, res_id: int) -> bool:
    """Hard-delete a reservation. Returns False if it did not exist."""
    result = session.execute(
        text("DELETE FROM reservation WHERE id = :id"), {"id": res_id}
    )
    return result.rowcount > 0
