"""The feasibility oracle — where along the wharf can a requested vessel berth?

A read-only *advisor*, not a placer. For one planned reservation (vessel +
window), it computes the maximal station bands where the vessel would fit,
mirroring exactly what the confirm path enforces so an offered spot doesn't then
409/422:

* it clears the mooring gap from every reservation occupying the wharf in the
  window — ``confirmed``/``tentative``/placed ``requested`` bookings AND
  ``observed`` AIS berthings (the same 75 ft the ``no_wharf_overlap`` exclusion
  constraint bakes in — see ``app.conflicts.MOORING_GAP_FT``), so a candidate can
  never conflict with a ship there **now** or a booking **later**;
* it fits within the wharf extent;
* it carries enough controlling depth for the vessel's draft (the same
  ``app/depth/gate.py`` check the confirm gate runs).

``observed`` AIS rows are approximate, so padding them by the full mooring gap is
deliberately conservative — the operator would rather never be offered a spot that
conflicts with a currently-berthed vessel than be offered one that does. They also
still ride along as an overlay so the map shows *why* the space near them is gone.
Placement stays the operator's job — the oracle proposes, the operator confirms
through the existing form. Two layers, like ``app/conflicts.py``:

* **Pure interval math** (``subtract_intervals`` / ``feasible_bands`` /
  ``placement_range``) — unit-tested, no database.
* **The orchestrator** (``compute_feasibility``) — loads the reservation, gathers
  obstacles, runs the pure math, and annotates each band with depth + Dock No. +
  covering berths.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.conflicts import MOORING_GAP_FT
from app.crosswalk import segment_dockno_params
from app.depth.gate import FEET_PER_M, controlling_depth_over, depth_shortfall

Interval = tuple[float, float]

# Cap the discrete berth options shown in the picker, so a wide-open wharf with a
# large berth catalog doesn't flood the menu.
MAX_CANDIDATES = 20


# ---------------------------------------------------------------------------
# Pure interval math — no database
# ---------------------------------------------------------------------------
def subtract_intervals(extent: Interval, blocked: list[Interval]) -> list[Interval]:
    """Free sub-intervals of ``extent`` after removing the union of ``blocked``.

    Bounds are treated as a continuous line (feet); blocked spans are clipped to
    the extent and merged. Returns the gaps left over, low-to-high. A blocked span
    that fully covers the extent yields ``[]``; an empty ``blocked`` yields
    ``[extent]``.
    """
    lo, hi = extent
    if hi <= lo:
        return []
    # Clip to the extent and drop spans that don't touch it, then sweep.
    segs = sorted(
        (max(lo, b0), min(hi, b1)) for b0, b1 in blocked if b1 > lo and b0 < hi
    )
    free: list[Interval] = []
    cursor = lo
    for b0, b1 in segs:
        if b0 > cursor:
            free.append((cursor, b0))
        if b1 > cursor:
            cursor = b1
    if cursor < hi:
        free.append((cursor, hi))
    return free


def feasible_bands(
    extent: Interval, obstacles: list[Interval], loa_ft: float, gap_ft: float
) -> list[Interval]:
    """Maximal free bands within ``extent`` that admit a vessel of ``loa_ft``.

    Each obstacle ``[lo, hi]`` is padded by the full mooring gap on each side, so
    a hull placed flush against a free band's edge still clears the neighbour by
    ``gap_ft`` (and two obstacles need ``loa_ft + 2*gap_ft`` between them to admit
    the vessel — exactly what the ±half-gap exclusion constraint enforces). The
    wharf ends carry no gap (there's no neighbour to clear). Only bands at least
    ``loa_ft`` wide are returned.
    """
    padded = [(o0 - gap_ft, o1 + gap_ft) for o0, o1 in obstacles]
    return [
        (f0, f1)
        for f0, f1 in subtract_intervals(extent, padded)
        if (f1 - f0) >= loa_ft
    ]


def placement_range(band: Interval, loa_ft: float) -> Interval:
    """POPA range the hull's LOW (low-station) end may occupy within ``band``.

    The hull spans ``loa_ft``; its low end can slide from the band's low edge up
    to ``band_hi - loa_ft`` while the hull stays inside the band.
    """
    f0, f1 = band
    return (f0, f1 - loa_ft)


def candidate_slots(
    bands: list[Interval],
    berths: list[tuple[float, float, str]],
    loa_ft: float,
) -> list[tuple[float, str | None]]:
    """Discrete, vessel-sized placements — the concrete "berths" a Find-berth pick
    offers, not the raw free bands.

    One slot per named berth the vessel can sit at (its footprint anchored at the
    berth's low-station end, clamped to stay inside a feasible band and to still
    overlap that berth), plus a low-end fallback slot for any feasible band no
    berth covered. Each slot is a low-station start; the footprint is
    ``[start, start + loa_ft]``. Returns ``[(start, berth_name | None), ...]``
    sorted low-to-high, de-duplicated by start.
    """
    slots: list[tuple[float, str | None]] = []
    seen: set[float] = set()
    covered: set[Interval] = set()

    def push(start: float, name: str | None) -> None:
        key = round(start, 1)
        if key in seen:
            return
        seen.add(key)
        slots.append((start, name))

    for blo, bhi, name in berths:
        for flo, fhi in bands:
            if fhi - flo < loa_ft:
                continue
            if not (blo < fhi and flo < bhi):  # berth must overlap this free band
                continue
            start = min(max(blo, flo), fhi - loa_ft)
            if start + loa_ft <= blo or start >= bhi:  # footprint must touch the berth
                continue
            push(start, name)
            covered.add((flo, fhi))
            break

    for flo, fhi in bands:
        if (flo, fhi) not in covered:
            push(flo, None)

    slots.sort(key=lambda s: s[0])
    return slots


# ---------------------------------------------------------------------------
# Orchestrator — loads the reservation, gathers obstacles, annotates bands
# ---------------------------------------------------------------------------
_RESERVATION_SQL = text(
    """
    SELECT r.vessel_id,
           lower(r.time_range) AS t_start, upper(r.time_range) AS t_end,
           v.name AS vessel_name, v.imo AS vessel_imo,
           v.loa AS loa_m, v.beam AS beam_m, v.draft AS draft_m
    FROM reservation r
    LEFT JOIN vessel v ON v.id = r.vessel_id
    WHERE r.id = :rid
    """
)

# Anything occupying the wharf in the window removes space, so a candidate can
# never be offered where it would create a conflict — with a ship there **now**
# (``observed`` AIS) or a **booking later** (confirmed/tentative, or a requested
# row that carries a placement). Any non-empty station range whose time overlaps
# the window and isn't cancelled/completed. ``observed`` is INCLUDED (the operator
# asked that Find-berth never offer a spot that conflicts with a currently-berthed
# vessel — approximate AIS ranges are padded by the mooring gap like every other
# obstacle, so a candidate stays clear of them). An unplaced request (empty range)
# has nothing to avoid. Exclude the subject row itself.
_OBSTACLES_SQL = text(
    """
    SELECT lower(r.station_range) AS lo, upper(r.station_range) AS hi
    FROM reservation r
    WHERE r.id <> :rid
      AND r.status::text NOT IN ('cancelled', 'completed')
      AND NOT isempty(r.station_range)
      AND r.time_range && tstzrange(:t_from, :t_to, '[)')
    """
)

# Observed AIS berthings that overlap the window — these remove space too (via
# _OBSTACLES_SQL above); this query returns them again for the map overlay, so the
# operator can see which currently-berthed ship blocked a stretch.
_OBSERVED_SQL = text(
    """
    SELECT r.id,
           lower(r.station_range) AS lo, upper(r.station_range) AS hi,
           COALESCE(v.name, (
               SELECT COALESCE(e.raw->>'vessel', e.raw->>'Vessel')
               FROM intake_event e WHERE e.reservation_id = r.id
               ORDER BY e.id LIMIT 1
           )) AS vessel_name
    FROM reservation r
    LEFT JOIN vessel v ON v.id = r.vessel_id
    WHERE r.status::text = 'observed'
      AND NOT isempty(r.station_range)
      AND r.time_range && tstzrange(:t_from, :t_to, '[)')
    """
)


def compute_feasibility(session: Session, reservation_id: int) -> dict | None:
    """Feasibility bands for the vessel on one planned reservation.

    Returns ``None`` if the reservation doesn't exist (endpoint -> 404). Raises
    ``ValueError`` (endpoint -> 422) when the row can't be evaluated: no linked
    vessel, no time window, or an unknown LOA (can't size the berth).
    """
    r = session.execute(_RESERVATION_SQL, {"rid": reservation_id}).first()
    if r is None:
        return None
    if r.vessel_id is None:
        raise ValueError("reservation has no vessel to size a berth for")
    if r.t_start is None or r.t_end is None:
        raise ValueError(
            "no arrival/departure window set — add an ETB and ETD "
            "(or pick a berth from the catalog), then retry"
        )
    if r.loa_m is None:
        raise ValueError(
            "vessel length (LOA) is unknown — set it on the vessel, "
            "or pick a berth from the catalog"
        )

    loa_ft = float(r.loa_m) * FEET_PER_M
    draft_m = float(r.draft_m) if r.draft_m is not None else None
    clearance_ft = get_settings().depth_clearance_ft
    # Draft + the required under-keel depth, in feet — surfaced so the UI can show
    # WHY a berth reads "shallow" (draft is stored in metres; a berth clears when
    # its controlling depth >= draft_ft + clearance). None when draft is unknown.
    draft_ft = round(draft_m * FEET_PER_M, 1) if draft_m is not None else None
    required_ft = round(draft_ft + clearance_ft, 1) if draft_ft is not None else None

    extent = _wharf_extent(session)
    if extent is None:
        raise ValueError("no wharf segment seeded; cannot compute feasibility")

    win = {"rid": reservation_id, "t_from": r.t_start, "t_to": r.t_end}
    obstacles = [
        (float(o.lo), float(o.hi))
        for o in session.execute(_OBSTACLES_SQL, win).all()
    ]
    bands = feasible_bands(extent, obstacles, loa_ft, MOORING_GAP_FT)

    dock = segment_dockno_params(session)
    berths = _berth_ranges(session)
    # Suggest a concrete heading so a one-click prefill places consistently; honour
    # the reservation's own direction when set, else default upstream. The operator
    # can still flip it in the form.
    direction = _effective_direction(session, reservation_id) or "upstream"

    def dk(popa: float) -> float:
        return float(dock.from_popa(popa))

    band_rows = []
    for lo, hi in bands:
        ctrl, surveyed = controlling_depth_over(session, lo, hi)
        shortfall = depth_shortfall(ctrl, draft_m, clearance_ft)
        status = "unknown" if ctrl is None else ("shallow" if shortfall else "ok")
        stern_lo, stern_hi = placement_range((lo, hi), loa_ft)
        band_rows.append(
            {
                "popa_lo": lo,
                "popa_hi": hi,
                "dock_lo": dk(lo),
                "dock_hi": dk(hi),
                "length_ft": hi - lo,
                # Snug placement at the low-station end of the band; the operator
                # can still slide/flip it in the form.
                "bow_dock": dk(_bow_popa(lo, loa_ft, direction)),
                "direction": direction,
                "stern_popa_lo": stern_lo,
                "stern_popa_hi": stern_hi,
                "berths": [b["name"] for b in berths if b["lo"] < hi and lo < b["hi"]],
                "depth": {
                    "controlling_ft": ctrl,
                    "surveyed_at": surveyed.isoformat() if surveyed else None,
                    "shortfall_ft": round(shortfall, 2) if shortfall else None,
                    "status": status,
                },
            }
        )

    # Discrete, vessel-sized placements — the concrete berths the "Find berth"
    # picker offers (each confirms directly), as opposed to the raw free bands
    # (kept for the map's free-space context). Depth is checked over each slot's
    # exact footprint, so it's more precise than the band-wide reading above.
    slot_defs = candidate_slots(
        bands, [(b["lo"], b["hi"], b["name"]) for b in berths], loa_ft
    )[:MAX_CANDIDATES]
    candidates = []
    for start, berth_name in slot_defs:
        lo, hi = start, start + loa_ft
        ctrl, surveyed = controlling_depth_over(session, lo, hi)
        shortfall = depth_shortfall(ctrl, draft_m, clearance_ft)
        status = "unknown" if ctrl is None else ("shallow" if shortfall else "ok")
        candidates.append(
            {
                "berth": berth_name,
                "popa_lo": lo,
                "popa_hi": hi,
                "dock_lo": dk(lo),
                "dock_hi": dk(hi),
                "length_ft": loa_ft,
                "bow_dock": dk(_bow_popa(lo, loa_ft, direction)),
                "direction": direction,
                "depth": {
                    "controlling_ft": ctrl,
                    "surveyed_at": surveyed.isoformat() if surveyed else None,
                    "shortfall_ft": round(shortfall, 2) if shortfall else None,
                    "status": status,
                },
            }
        )

    observed = [
        {
            "reservation_id": o.id,
            "vessel_name": o.vessel_name,
            "popa_lo": float(o.lo),
            "popa_hi": float(o.hi),
            "dock_lo": dk(float(o.lo)),
            "dock_hi": dk(float(o.hi)),
        }
        for o in session.execute(_OBSERVED_SQL, win).all()
    ]

    return {
        "reservation_id": reservation_id,
        "vessel": {
            "name": r.vessel_name,
            "imo": r.vessel_imo,
            "loa_ft": round(loa_ft, 1),
            "draft_m": draft_m,
            "draft_ft": draft_ft,
            "beam_m": float(r.beam_m) if r.beam_m is not None else None,
        },
        "required_ft": required_ft,
        "window": {
            "t_start": r.t_start.isoformat(),
            "t_end": r.t_end.isoformat(),
        },
        "wharf": {"popa_lo": extent[0], "popa_hi": extent[1]},
        "clearance_ft": clearance_ft,
        "candidates": candidates,
        "bands": band_rows,
        "observed": observed,
    }


def _bow_popa(band_lo: float, loa_ft: float, direction: str | None) -> float:
    """Bow POPA station for a hull snug at the band's low end.

    Mirrors ``app/edit._station_from_bow``: an ``upstream`` vessel's bow is at the
    high-station end of its hull, a ``downstream`` (or un-oriented, default) bow at
    the low-station end. Either way the hull occupies ``[band_lo, band_lo+loa]``.
    """
    return band_lo + loa_ft if direction == "upstream" else band_lo


def _effective_direction(session: Session, reservation_id: int) -> str | None:
    row = session.execute(
        text("SELECT direction FROM reservation WHERE id = :rid"),
        {"rid": reservation_id},
    ).first()
    return row.direction if row else None


def _wharf_extent(session: Session) -> Interval | None:
    """POPA span of the wharf, from the same (lowest-id) segment the crosswalk uses."""
    row = session.execute(
        text(
            "SELECT popa_sta_start, popa_sta_end FROM wharf_segment "
            "ORDER BY id LIMIT 1"
        )
    ).first()
    if row is None:
        return None
    return (float(row.popa_sta_start), float(row.popa_sta_end))


def _berth_ranges(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            "SELECT name, popa_sta_start, popa_sta_end FROM berth "
            "ORDER BY popa_sta_start"
        )
    ).all()
    return [
        {"name": r.name, "lo": float(r.popa_sta_start), "hi": float(r.popa_sta_end)}
        for r in rows
    ]
