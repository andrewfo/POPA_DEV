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
bounds are entered in **Dock No. feet** — the stationing painted on the wharf
(the yellow dock markers on the map), which is what an operator reads off the
quay — and converted to canonical **POPA feet** for storage via the wharf
segment's affine params (``crosswalk.segment_dockno_params`` ->
``AffineParams.to_popa``). All stationing math stays inside ``app/crosswalk.py``;
the canonical store remains POPA. Dock No. is reversed relative to POPA, so the
**stern** end (the larger Dock No.) maps to the lower POPA bound — enter stern in
``station_lo`` and bow in ``station_hi`` (an inverted pair is rejected).

The range/validation helpers (``_station_range``, ``_time_range``) are pure and
unit-tested without a DB. Confirming a reservation runs the
draft-vs-controlling-depth gate (``_depth_gate`` -> ``app/depth/gate.py``): it
blocks (422) when the vessel's draft + clearance exceeds the shallowest
controlling depth over the station range, unless ``depth_override`` downgrades it
to a warning. The ``*_vessel`` / ``*_reservation`` functions touch the session
and do NOT commit — the endpoint owns the transaction boundary.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from pydantic import BaseModel
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crosswalk import format_station, segment_dockno_params
from app.depth.gate import controlling_depth_over, depth_shortfall
from app.tz import assume_central
from app.models import (
    DIRECTIONS,
    RESERVATION_SOURCES,
    RESERVATION_STATUSES,
    RESERVATION_TYPES,
    Vessel,
)

# Station "M" is feet; vessel LOA is stored in metres (matching AIS dimensions).
# Used to turn a vessel length into a station span when placing a reservation
# from the bow alone. Same constant as app/occupancy/project.FEET_PER_M.
FEET_PER_M = 3.280839895


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
    range); everything else is optional. Station bounds are **Dock No. feet**
    (converted to canonical POPA on store) and may be omitted (berth unassigned
    -> empty range, never conflicts)."""

    type: str = "vessel"
    vessel_id: int | None = None
    etb: dt.datetime
    etd: dt.datetime | None = None
    # Assign a named berth: its catalog range fills station_range. Explicit
    # station_lo/hi (a sub-span for a vessel shorter than the berth) override it.
    # station_lo/hi are Dock No. feet (stern in lo, bow in hi); see module docs.
    berth_id: int | None = None
    station_lo: float | None = None
    station_hi: float | None = None
    direction: str | None = None
    status: str = "tentative"
    source: str = "operator"
    priority: int | None = None
    cargo: str | None = None
    notes: str | None = None
    # Override the draft-vs-controlling-depth gate when confirming: instead of
    # blocking (422), record the shortfall as a warning + audit note. Ignored
    # unless status == 'confirmed'.
    depth_override: bool = False


class ReservationUpdate(BaseModel):
    """Partial reservation edit. Fields absent from the request are left as-is;
    time and station ranges are recomputed from the merge of existing bounds and
    whatever the request supplies. Set ``unassigned=True`` to clear the berth
    (empty station range).

    The primary scheduling path is **promoting a berth request**: the request
    already carries the time window (``time_range``, from intake) and the vessel,
    so the operator supplies only where the **bow** sits (``bow_dock``, Dock No.
    feet) and the heading (``direction``). The stern follows from the vessel's
    LOA — see ``_station_from_bow`` — so neither ETB/ETD nor a stern bound is
    entered here. (``etb``/``etd`` and explicit ``station_lo/hi`` remain for
    direct corrections, but the request's window is preserved when they're
    omitted.)"""

    type: str | None = None
    vessel_id: int | None = None
    etb: dt.datetime | None = None
    etd: dt.datetime | None = None
    # Reassign the berth (its range fills station_range unless explicit
    # station_lo/hi are also given). ``unassigned=True`` clears both the berth
    # and the station range. station_lo/hi are Dock No. feet (see module docs).
    berth_id: int | None = None
    station_lo: float | None = None
    station_hi: float | None = None
    # Promote-from-request path: place the vessel by its bow (Dock No. feet) +
    # heading; the stern is derived from the vessel LOA. Takes precedence over
    # berth_id / station_lo/hi when supplied.
    bow_dock: float | None = None
    unassigned: bool | None = None
    direction: str | None = None
    status: str | None = None
    priority: int | None = None
    cargo: str | None = None
    notes: str | None = None
    # Override the draft-vs-controlling-depth gate when confirming (see
    # ReservationCreate.depth_override). Ignored unless status == 'confirmed'.
    depth_override: bool = False


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


def _station_from_bow(
    bow: float, direction: str | None, loa_ft: float
) -> StationRange:
    """Station range from the bow station + heading + vessel length (all POPA
    feet). Promoting a berth request supplies only where the bow sits and which
    way the vessel points; the stern follows from the LOA.

    Mirrors the map's hull rendering (``dirUp = direction != 'downstream'`` ->
    bow toward increasing station): **upstream** -> bow at ``hi``, stern below;
    **downstream** -> bow at ``lo``, stern above. ``direction`` must be set (an
    un-oriented hull can't be placed) and the vessel must have a known LOA."""
    if direction not in ("upstream", "downstream"):
        raise ValueError(
            "direction (upstream/downstream) is required to place from the bow"
        )
    if loa_ft <= 0:
        raise ValueError(
            "vessel LOA is required to place from the bow — set it on the vessel first"
        )
    if direction == "downstream":
        return StationRange(empty=False, lo=float(bow), hi=float(bow) + loa_ft)
    return StationRange(empty=False, lo=float(bow) - loa_ft, hi=float(bow))


def _time_range(etb: dt.datetime | None, etd: dt.datetime | None) -> TimeRange:
    """Resolve a ``[etb, etd)`` window. ``etd`` may be omitted (open-ended).
    Raises ``ValueError`` if ``etd`` precedes ``etb``.

    Naive bounds (a ``datetime-local`` input sends no zone) are read as Central
    Time — the canonical wall-clock (``app/tz.py``); an already-zoned bound keeps
    its instant. The store stays ``timestamptz`` (absolute instants)."""
    etb = assume_central(etb)
    etd = assume_central(etd)
    if etb is not None and etd is not None and etd < etb:
        raise ValueError("ETD must not precede ETB")
    return TimeRange(lower=etb, upper=etd)


def _validate_enum(value: str | None, allowed: tuple[str, ...], label: str) -> None:
    if value is not None and value not in allowed:
        raise ValueError(f"invalid {label} {value!r}; expected one of {allowed}")


def _depth_gate(
    session: Session,
    sr: StationRange,
    vessel_id: int | None,
    override: bool,
) -> list[str]:
    """The draft-vs-controlling-depth check, run when a reservation is moved to
    ``confirmed`` (CLAUDE.md: draft must clear controlling depth before confirm).

    Blocks by raising ``ValueError`` (-> 422) when the vessel's draft + the
    configured under-keel clearance exceeds the shallowest controlling depth over
    the station range, per the latest active depth survey. ``override=True``
    downgrades that block to a ``[depth override]`` warning (logged in the audit
    detail by the caller). When the check can't be evaluated — no berth assigned,
    unknown draft, or no survey covering the range — it warns rather than blocks
    (we never invent depth data). Returns the warnings to surface; an empty list
    means the berth cleared."""
    if sr.empty:
        return ["no berth assigned — draft not validated against controlling depth"]
    draft_m = _vessel_draft_m(session, vessel_id)
    if draft_m is None:
        return ["vessel draft unknown — not validated against controlling depth"]

    controlling_ft, surveyed_at = controlling_depth_over(session, sr.lo, sr.hi)
    if controlling_ft is None:
        return [
            "no controlling-depth survey covers this station range — "
            "draft not validated"
        ]

    clearance = get_settings().depth_clearance_ft
    deficit = depth_shortfall(controlling_ft, draft_m, clearance)
    if deficit is None:
        return []  # clears

    draft_ft = draft_m * FEET_PER_M
    msg = (
        f"draft {draft_ft:.1f} ft + {clearance:.1f} ft clearance exceeds "
        f"controlling depth {controlling_ft:.1f} ft over "
        f"{format_station(sr.lo)}..{format_station(sr.hi)} "
        f"(survey {surveyed_at:%Y-%m-%d}, short {deficit:.1f} ft)"
    )
    if not override:
        raise ValueError(msg)
    return [f"[depth override] {msg}"]


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
def _dock_to_popa(session: Session, value: float | None) -> float | None:
    """Convert an operator-entered Dock No. station (feet) to canonical POPA via
    the wharf segment's affine params. ``None`` passes through (unset bound).

    All stationing math goes through ``crosswalk`` — this is just the call site.
    Each bound is converted independently so the ``station_lo`` (stern) slot keeps
    its meaning across partial edits; since Dock No. is reversed, a stern Dock No.
    larger than the bow's lands as the lower POPA bound, which ``_station_range``
    then validates as ``lo < hi``.
    """
    if value is None:
        return None
    return segment_dockno_params(session).to_popa(value)


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


def _vessel_loa_ft(session: Session, vessel_id: int | None) -> float:
    """The vessel's LOA in **feet** (the store is metres). Returns 0.0 when the
    vessel or its LOA is unknown; the bow-placement helper turns that into a
    clean 422 telling the operator to set the length first."""
    if vessel_id is None:
        return 0.0
    loa_m = session.execute(
        select(Vessel.loa).where(Vessel.id == vessel_id)
    ).scalar_one_or_none()
    return float(loa_m) * FEET_PER_M if loa_m is not None else 0.0


def _vessel_draft_m(session: Session, vessel_id: int | None) -> float | None:
    """The vessel's draft in **metres** (the canonical store), or ``None`` when
    the vessel or its draft is unknown — the depth gate treats that as "can't
    evaluate" (a warning, not a block)."""
    if vessel_id is None:
        return None
    draft = session.execute(
        select(Vessel.draft).where(Vessel.id == vessel_id)
    ).scalar_one_or_none()
    return float(draft) if draft is not None else None



# Dimension columns AIS keeps authoritative for an MMSI-keyed vessel. Editing them
# here *applies* (this surface is authoritative, unlike intake's NULL-fill), but the
# AIS ingestor overwrites them by MMSI on the next ShipStaticData — so the edit is
# transient. Warn rather than let the operator think it stuck.
_AIS_EDIT_DIMS = {"loa": "LOA", "beam": "beam", "draft": "draft"}


def _ais_dim_edit_warnings(mmsi: int | None, changes: dict) -> list[str]:
    """Warn when this edit changes a dimension of an AIS-tracked vessel — the live
    feed will overwrite it on the next AIS message, so the change won't stick."""
    if mmsi is None:
        return []
    edited = [label for field, label in _AIS_EDIT_DIMS.items() if changes.get(field) is not None]
    if not edited:
        return []
    return [
        f"{', '.join(edited)} saved, but this vessel is AIS-tracked (MMSI {mmsi}); "
        f"the live AIS feed overwrites its dimensions on the next message, so this "
        f"edit won't stick. Correct it at the AIS source."
    ]


def update_vessel(session: Session, vessel_id: int, upd: VesselUpdate) -> dict | None:
    """Overwrite the provided columns of a vessel. Returns a summary dict, or
    ``None`` if no such vessel (-> 404). ``mmsi``/``imo`` collisions or the
    "mmsi or imo required" check surface as IntegrityError at flush -> 409."""
    row = session.execute(
        select(Vessel.mmsi).where(Vessel.id == vessel_id)
    ).first()
    if row is None:
        return None

    changes = upd.model_dump(exclude_unset=True)
    # Effective MMSI: an edit may be setting one now (linking the vessel to AIS).
    effective_mmsi = changes.get("mmsi", row.mmsi)
    warnings = _ais_dim_edit_warnings(effective_mmsi, changes)
    if changes:
        session.execute(
            update(Vessel)
            .where(Vessel.id == vessel_id)
            .values(**changes, updated_at=func.now())
        )
    return {"id": vessel_id, "updated_fields": sorted(changes), "warnings": warnings}


def create_reservation(session: Session, req: ReservationCreate) -> dict:
    """Insert a reservation. Validates enums + ranges (ValueError -> 422)."""
    _validate_enum(req.type, RESERVATION_TYPES, "type")
    _validate_enum(req.status, RESERVATION_STATUSES, "status")
    _validate_enum(req.source, RESERVATION_SOURCES, "source")
    _validate_enum(req.direction, DIRECTIONS, "direction")

    berth_bounds = _berth_bounds(session, req.berth_id) if req.berth_id else None
    # Operator-entered bounds are Dock No.; berth catalog bounds are already POPA.
    lo = _dock_to_popa(session, req.station_lo)
    hi = _dock_to_popa(session, req.station_hi)
    sr = _station_range_for(berth_bounds, lo, hi, unassigned=False)
    tr = _time_range(req.etb, req.etd)

    # Draft gate: only confirming a reservation validates draft vs controlling
    # depth (blocks via ValueError -> 422 unless overridden). Run before the
    # INSERT so a block short-circuits cleanly.
    warnings = (
        _depth_gate(session, sr, req.vessel_id, req.depth_override)
        if req.status == "confirmed"
        else []
    )

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
    return {"id": int(rid), "warnings": warnings}


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

    # Convert incoming Dock No. bounds to canonical POPA up front, so the merge
    # below (which mixes supplied bounds with the stored POPA range) is all-POPA.
    for k in ("station_lo", "station_hi"):
        if changes.get(k) is not None:
            changes[k] = _dock_to_popa(session, changes[k])

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
    #   cancelled -> drop the placement (free berth + empty range), short-circuit;
    #   else unassigned -> clear both berth_id and the range;
    #   else a bow placement (promote-from-request) -> stern from the vessel LOA;
    #   else a newly-assigned berth (no explicit bounds) -> its catalog range;
    #   else explicit lo/hi merged over the current range (berth_id unchanged);
    #   else keep the current range (and berth_id).
    has_bounds = "station_lo" in changes or "station_hi" in changes
    if status == "cancelled":
        # Cancelling drops the placement entirely: a cancelled visit is no longer
        # alongside, so it frees its berth and station range (an empty range never
        # conflicts). Resolved FIRST and short-circuiting the placement branches:
        # the edit form re-sends the row's existing bow_dock + heading, and a
        # stale bow placement must neither (a) re-place a cancelled row nor (b)
        # fail (e.g. unknown LOA -> 422) and block the cancel. The time range is
        # kept as the historical record of when the visit was meant to occur.
        berth_id = None
        sr = StationRange(empty=True)
    elif changes.get("unassigned"):
        berth_id = None
        sr = StationRange(empty=True)
    elif changes.get("bow_dock") is not None:
        berth_id = changes.get("berth_id", cur.berth_id)
        bow = _dock_to_popa(session, changes["bow_dock"])
        sr = _station_from_bow(bow, direction, _vessel_loa_ft(session, vessel_id))
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

    # Draft gate: confirming validates draft vs controlling depth over the
    # resolved range (blocks -> 422 unless overridden). The override flag rides on
    # the edit body and is ignored unless the row is being confirmed.
    warnings = (
        _depth_gate(session, sr, vessel_id, bool(changes.get("depth_override")))
        if status == "confirmed"
        else []
    )

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
    return {"id": res_id, "warnings": warnings}


def delete_reservation(session: Session, res_id: int) -> bool:
    """Hard-delete a reservation. Returns False if it did not exist."""
    result = session.execute(
        text("DELETE FROM reservation WHERE id = :id"), {"id": res_id}
    )
    return result.rowcount > 0
