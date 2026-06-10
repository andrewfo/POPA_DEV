"""Conflict detection — the core primitive the data layer exists to serve.

A conflict between two reservations is, per the core model (CLAUDE.md):

    their time ranges overlap  AND  their station ranges overlap

One primitive covers vessel-vs-vessel, vessel-vs-dredge, and the headline
signal *observed-vs-planned* (an AIS-observed vessel sitting where something is
planned). Unlike the ``no_wharf_overlap`` exclusion constraint — which *blocks*
``confirmed``-vs-``confirmed`` collisions at write time — this module *surfaces*
overlaps as a query: ``observed`` rows are allowed to overlap planned rows on
purpose, and that overlap is exactly the signal we want to report.

Two layers live here:

* **Pure predicates** (``time_overlaps`` / ``station_overlaps`` /
  ``reservations_conflict`` / ``overlap_interval`` / ``classify``) — unit-tested,
  no database. They model the *exact* inclusivity of the stored ranges:
    - ``time_range`` is half-open ``[lo, hi)`` (so windows that merely touch at an
      instant do NOT conflict), with a ``None`` upper bound meaning open-ended.
    - ``station_range`` is closed ``[lo, hi]`` (so a shared station endpoint DOES
      count as overlap); an empty/unassigned range (``None`` bound) never
      overlaps — an unassigned ``requested`` row raises no false conflict.
* **The DB query** (``find_conflicts``) — a self-join using Postgres' own range
  operators (``&&`` to detect, ``*`` to compute the overlap rectangle), which is
  the authoritative source of truth. It mirrors the ``GET /reservations`` SQL
  (vessel-name fallback, Dock No. conversion) so the two stay consistent.

The conflict primitive uses the **raw** ranges, NOT the ±37.5 ft mooring-gap
buffer from migration 0007 — that buffer is a write-time safety margin for
``confirmed`` bookings; the conflict *query* reports true overlaps.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.crosswalk import segment_dockno_params
from app.shiptypes import SERVICE_CRAFT_SQL

# Statuses that are off the board — a cancelled or completed reservation no
# longer occupies the wharf, so it never participates in a conflict.
INACTIVE_STATUSES = ("cancelled", "completed")


# ---------------------------------------------------------------------------
# Pure predicates — model the stored ranges exactly; no database
# ---------------------------------------------------------------------------
def time_overlaps(
    a_lo: Any, a_hi: Any, b_lo: Any, b_hi: Any
) -> bool:
    """Do two **half-open** time intervals ``[a_lo, a_hi)`` and ``[b_lo, b_hi)``
    overlap? ``None`` lower = -infinity, ``None`` upper = +infinity (open-ended
    berthing). Touching endpoints do NOT overlap: ``[1, 2)`` and ``[2, 3)`` are
    adjacent, not overlapping — matching ``tstzrange(..., '[)')``.
    """
    # Overlap iff a_lo < b_hi and b_lo < a_hi. A None bound short-circuits the
    # comparison it would make trivially true (±infinity).
    left = a_lo is None or b_hi is None or a_lo < b_hi
    right = b_lo is None or a_hi is None or b_lo < a_hi
    return left and right


def station_overlaps(
    a_lo: float | None,
    a_hi: float | None,
    b_lo: float | None,
    b_hi: float | None,
) -> bool:
    """Do two **closed** station intervals ``[a_lo, a_hi]`` and ``[b_lo, b_hi]``
    overlap? A shared endpoint DOES overlap (``[400, 900]`` and ``[900, 1200]``
    share station 900) — matching ``numrange(..., '[]')``. An empty/unassigned
    range (any ``None`` bound) never overlaps, so an unassigned ``requested`` row
    raises no false conflict.
    """
    if a_lo is None or a_hi is None or b_lo is None or b_hi is None:
        return False
    return a_lo <= b_hi and b_lo <= a_hi


@dataclass(frozen=True)
class ResInterval:
    """A reservation reduced to its conflict-relevant rectangle: a time interval
    and a station interval. ``t_hi`` may be ``None`` (open-ended); ``s_lo``/``s_hi``
    are ``None`` for an empty/unassigned station range."""

    t_lo: datetime | None
    t_hi: datetime | None
    s_lo: float | None
    s_hi: float | None


def reservations_conflict(a: ResInterval, b: ResInterval) -> bool:
    """True iff ``a`` and ``b`` overlap in BOTH time and station — the conflict
    primitive."""
    return time_overlaps(a.t_lo, a.t_hi, b.t_lo, b.t_hi) and station_overlaps(
        a.s_lo, a.s_hi, b.s_lo, b.s_hi
    )


def _max_lower(x: Any, y: Any) -> Any:
    """Larger of two lower bounds, treating ``None`` as -infinity."""
    if x is None:
        return y
    if y is None:
        return x
    return max(x, y)


def _min_upper(x: Any, y: Any) -> Any:
    """Smaller of two upper bounds, treating ``None`` as +infinity."""
    if x is None:
        return y
    if y is None:
        return x
    return min(x, y)


def overlap_interval(
    a_lo: Any, a_hi: Any, b_lo: Any, b_hi: Any
) -> tuple[Any, Any]:
    """The intersection ``[max(lows), min(highs)]`` of two intervals — the values
    of the overlap rectangle's bounds (``None`` = unbounded). Mirrors what the SQL
    ``range_a * range_b`` intersection returns; callers should only trust it when
    the intervals actually overlap."""
    return _max_lower(a_lo, b_lo), _min_upper(a_hi, b_hi)


def classify(a_type: str, a_status: str, b_type: str, b_status: str) -> str:
    """Label a conflict pair by category — the same primitive, three lenses:

    * ``dredge-vs-vessel`` — either side is a dredging op (no special-casing in
      the query; the type just changes the label).
    * ``observed-vs-planned`` — exactly one side is AIS-``observed``; the other is
      planned. The headline signal: a vessel sitting where something is planned.
    * ``planned-vs-planned`` — neither (or both) observed; a sanity check even
      though the DB blocks ``confirmed`` overlaps.
    """
    if a_type == "dredge" or b_type == "dredge":
        return "dredge-vs-vessel"
    if (a_status == "observed") != (b_status == "observed"):
        return "observed-vs-planned"
    return "planned-vs-planned"


# ---------------------------------------------------------------------------
# DB query — Postgres range operators are the source of truth
# ---------------------------------------------------------------------------
# Per-side vessel name: fall back to the linked intake_event.raw name when a
# reservation has no vessel row (an IMO-less barge/tug the vessel_requires_
# mmsi_or_imo CHECK won't let us key) — same source GET /reservations uses.
_NAME_FALLBACK = (
    "COALESCE({v}.name, (SELECT COALESCE(e.raw->>'vessel', e.raw->>'Vessel') "
    "FROM intake_event e WHERE e.reservation_id = {r}.id ORDER BY e.id LIMIT 1))"
)

# Two live-panel filters (both gated by params so historical queries can widen):
#   * :current_only — keep only pairs whose overlap rectangle reaches the present
#     or future (open-ended, or its time intersection ends after now()). A
#     collision that fully resolved in the past is History, not an alert.
#   * :include_service_craft — an OBSERVED harbor tug/towboat/pilot boat (see
#     app/shiptypes.py) holding station against a planned ship is almost always
#     the tug *working that ship's move*, not a berth collision; drop pairs where
#     an observed side's vessel is a service craft. A *planned* row for a tug is
#     operator-entered and always kept; NULL ship_type is kept (when in doubt,
#     show it).
_CONFLICTS_SQL = text(
    f"""
    SELECT
        r1.id AS a_id, r1.type AS a_type, r1.status AS a_status,
        lower(r1.time_range)    AS a_t_start, upper(r1.time_range)    AS a_t_end,
        lower(r1.station_range) AS a_sta_lo,  upper(r1.station_range) AS a_sta_hi,
        r1.berth_id AS a_berth_id, b1.name AS a_berth_name,
        {_NAME_FALLBACK.format(v="v1", r="r1")} AS a_vessel_name,
        v1.imo AS a_vessel_imo,
        r2.id AS b_id, r2.type AS b_type, r2.status AS b_status,
        lower(r2.time_range)    AS b_t_start, upper(r2.time_range)    AS b_t_end,
        lower(r2.station_range) AS b_sta_lo,  upper(r2.station_range) AS b_sta_hi,
        r2.berth_id AS b_berth_id, b2.name AS b_berth_name,
        {_NAME_FALLBACK.format(v="v2", r="r2")} AS b_vessel_name,
        v2.imo AS b_vessel_imo,
        -- The overlap rectangle, straight from Postgres range intersection.
        lower(r1.time_range * r2.time_range)       AS ov_t_start,
        upper(r1.time_range * r2.time_range)       AS ov_t_end,
        lower(r1.station_range * r2.station_range) AS ov_sta_lo,
        upper(r1.station_range * r2.station_range) AS ov_sta_hi
    FROM reservation r1
    JOIN reservation r2
      ON r1.id < r2.id                              -- dedupe pairs, no self-pair
     AND r1.time_range && r2.time_range             -- time overlap
     AND r1.station_range && r2.station_range       -- station overlap (empty ranges
                                                     -- never match -> unassigned
                                                     -- requests drop out for free)
    LEFT JOIN vessel v1 ON v1.id = r1.vessel_id
    LEFT JOIN vessel v2 ON v2.id = r2.vessel_id
    LEFT JOIN berth  b1 ON b1.id = r1.berth_id
    LEFT JOIN berth  b2 ON b2.id = r2.berth_id
    WHERE r1.status::text NOT IN ('cancelled', 'completed')
      AND r2.status::text NOT IN ('cancelled', 'completed')
      -- AIS can't conflict with itself: an observed-vs-observed overlap is a
      -- rafted tug, projection slop, or a stale derivation artifact — never a
      -- scheduling decision. Conflicts protect the PLAN, so at least one side
      -- must be a planned row (classify() has no both-observed category either).
      AND NOT (r1.status::text = 'observed' AND r2.status::text = 'observed')
      AND (CAST(:status AS text) IS NULL
           OR r1.status::text = CAST(:status AS text)
           OR r2.status::text = CAST(:status AS text))
      AND ((CAST(:t_from AS timestamptz) IS NULL AND CAST(:t_to AS timestamptz) IS NULL)
           OR (r1.time_range && tstzrange(:t_from, :t_to, '[]')
               AND r2.time_range && tstzrange(:t_from, :t_to, '[]')))
      AND (NOT CAST(:current_only AS boolean)
           OR upper(r1.time_range * r2.time_range) IS NULL
           OR upper(r1.time_range * r2.time_range) > now())
      AND (CAST(:include_service_craft AS boolean)
           OR NOT (r1.status::text = 'observed'
                   AND COALESCE(v1.ship_type, -1) IN ({SERVICE_CRAFT_SQL})))
      AND (CAST(:include_service_craft AS boolean)
           OR NOT (r2.status::text = 'observed'
                   AND COALESCE(v2.ship_type, -1) IN ({SERVICE_CRAFT_SQL})))
    ORDER BY ov_t_start
    LIMIT :limit
    """
)


def find_conflicts(
    session: Session,
    *,
    t_from: datetime | None = None,
    t_to: datetime | None = None,
    status: str | None = None,
    limit: int = 200,
    current_only: bool = False,
    include_service_craft: bool = True,
) -> list[dict]:
    """All current conflict pairs (time AND station overlap), each with the
    overlapping sub-rectangle so the UI can highlight the exact collision.

    Cancelled/completed rows never participate, and at least one side must be a
    planned row — two ``observed`` rows overlapping is AIS noise (rafted tug,
    projection slop, stale artifact), not a scheduling conflict. ``status``
    (optional) restricts to pairs where at least one side has that status — e.g.
    ``observed`` yields the observed-vs-planned signal. ``t_from``/``t_to``
    (optional) keep only pairs whose windows both overlap that span, mirroring
    ``GET /reservations``.

    ``current_only`` drops pairs whose overlap rectangle ended before now (a
    fully-resolved past collision is History, not an alert);
    ``include_service_craft=False`` drops pairs where an **observed** side is a
    harbor tug/towboat/pilot boat (``app/shiptypes.py``) — almost always the tug
    working the very move the planned row describes. Function defaults preserve
    the unfiltered behaviour; the endpoint flips them for the live panel.
    """
    rows = session.execute(
        _CONFLICTS_SQL,
        {
            "status": status,
            "t_from": t_from,
            "t_to": t_to,
            "limit": limit,
            "current_only": current_only,
            "include_service_craft": include_service_craft,
        },
    ).all()

    # Dock No. alongside POPA, converted server-side through the wharf segment's
    # affine params (the UI never does stationing math itself).
    dock = segment_dockno_params(session)

    def dk(v: Any) -> float | None:
        return float(dock.from_popa(float(v))) if v is not None else None

    def side(r: Any, p: str) -> dict:
        g = lambda n: getattr(r, f"{p}_{n}")  # noqa: E731
        t_start, t_end = g("t_start"), g("t_end")
        sta_lo, sta_hi = g("sta_lo"), g("sta_hi")
        return {
            "id": g("id"),
            "type": g("type"),
            "status": g("status"),
            "vessel_name": g("vessel_name"),
            "vessel_imo": g("vessel_imo"),
            "berth_id": g("berth_id"),
            "berth_name": g("berth_name"),
            "t_start": t_start.isoformat() if t_start else None,
            "t_end": t_end.isoformat() if t_end else None,
            "station_lo": float(sta_lo) if sta_lo is not None else None,
            "station_hi": float(sta_hi) if sta_hi is not None else None,
            "station_lo_dock": dk(sta_lo),
            "station_hi_dock": dk(sta_hi),
        }

    return [
        {
            "a": side(r, "a"),
            "b": side(r, "b"),
            "overlap": {
                "t_start": r.ov_t_start.isoformat() if r.ov_t_start else None,
                "t_end": r.ov_t_end.isoformat() if r.ov_t_end else None,
                "station_lo": float(r.ov_sta_lo) if r.ov_sta_lo is not None else None,
                "station_hi": float(r.ov_sta_hi) if r.ov_sta_hi is not None else None,
                "station_lo_dock": dk(r.ov_sta_lo),
                "station_hi_dock": dk(r.ov_sta_hi),
            },
            "category": classify(r.a_type, r.a_status, r.b_type, r.b_status),
        }
        for r in rows
    ]
