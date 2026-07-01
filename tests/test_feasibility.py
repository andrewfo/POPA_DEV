"""Pure feasibility-oracle tests — no database.

The interval math the oracle rests on: subtracting occupied spans from the wharf
extent, padding obstacles by the mooring gap, the ``width >= LOA`` fit threshold,
the two-obstacle ``LOA + 2*GAP`` spacing, and the placement range. The DB
orchestration (depth annotation, Dock No., 404/422) is exercised in
``test_feasibility_db.py``; these lock the geometry the endpoint must match.
"""
from __future__ import annotations

from app.feasibility import (
    candidate_slots,
    feasible_bands,
    placement_range,
    subtract_intervals,
)


# --- subtract_intervals ----------------------------------------------------
def test_subtract_nothing_returns_whole_extent():
    assert subtract_intervals((0, 1000), []) == [(0, 1000)]


def test_subtract_one_span_splits_extent():
    assert subtract_intervals((0, 1000), [(400, 500)]) == [(0, 400), (500, 1000)]


def test_subtract_merges_overlapping_spans():
    assert subtract_intervals((0, 1000), [(400, 500), (450, 600)]) == [
        (0, 400),
        (600, 1000),
    ]


def test_subtract_span_covering_extent_leaves_nothing():
    assert subtract_intervals((0, 1000), [(-50, 1200)]) == []


def test_subtract_clips_spans_to_extent():
    # A span reaching past the extent is clipped, not extended.
    assert subtract_intervals((0, 1000), [(-100, 200)]) == [(200, 1000)]


def test_subtract_adjacent_spans_leave_no_zero_width_gap():
    assert subtract_intervals((0, 1000), [(300, 400), (400, 500)]) == [
        (0, 300),
        (500, 1000),
    ]


# --- feasible_bands: gap padding + fit threshold ---------------------------
def test_no_obstacles_yields_full_extent_without_end_gaps():
    # The wharf ends carry no neighbour, so no gap is subtracted there.
    assert feasible_bands((0, 1000), [], 200, 75) == [(0, 1000)]


def test_obstacle_padded_by_full_gap_each_side():
    # Obstacle [400,500] padded ±75 -> [325,575] removed.
    assert feasible_bands((0, 1000), [(400, 500)], 200, 75) == [
        (0, 325),
        (575, 1000),
    ]


def test_bands_narrower_than_loa_are_dropped():
    # With LOA 400: the low band (325 ft) can't hold it; the high band (425) can.
    assert feasible_bands((0, 1000), [(400, 500)], 400, 75) == [(575, 1000)]


def test_two_obstacles_need_loa_plus_two_gaps_between():
    # Clear gap between the obstacles is 700-300 = 400 ft. A vessel between them
    # needs LOA + 2*GAP; with GAP 75 that's LOA + 150.
    obstacles = [(200, 300), (700, 800)]
    assert (375, 625) in feasible_bands((0, 1000), obstacles, 250, 75)  # 250+150=400, fits
    middle = [b for b in feasible_bands((0, 1000), obstacles, 251, 75) if b[0] >= 375]
    assert middle == []  # 251+150=401 > 400, no longer fits


# --- placement_range -------------------------------------------------------
def test_placement_range_is_band_minus_loa():
    assert placement_range((100, 500), 200) == (100, 300)


def test_placement_range_collapses_to_a_point_when_band_equals_loa():
    assert placement_range((100, 300), 200) == (100, 100)


# --- candidate_slots: discrete, berth-snapped placements -------------------
def test_candidate_one_slot_per_overlapping_berth():
    # Bands [0,925] and [1275,4000]; three berths — each yields one vessel-sized
    # slot anchored at (or clamped near) its low end.
    bands = [(0.0, 925.0), (1275.0, 4000.0)]
    berths = [(0.0, 900.0, "B1"), (900.0, 1400.0, "B2"), (1400.0, 2000.0, "B3")]
    slots = candidate_slots(bands, berths, 200.0)
    assert [name for _, name in slots] == ["B1", "B2", "B3"]
    # B2 is clamped so its footprint stays inside the [0,925] band but still
    # touches the berth (which starts at 900).
    starts = dict((name, start) for start, name in slots)
    assert starts["B1"] == 0.0
    assert 700.0 <= starts["B2"] <= 725.0
    assert starts["B3"] == 1400.0


def test_candidate_fallback_slot_when_no_berths():
    # No catalog -> one low-end slot per feasible band.
    bands = [(0.0, 925.0), (1275.0, 4000.0)]
    assert candidate_slots(bands, [], 200.0) == [(0.0, None), (1275.0, None)]


def test_candidate_skips_berth_that_cannot_host_the_footprint():
    # A berth wholly inside the padded obstacle gap (no overlapping band) yields
    # no slot.
    bands = [(0.0, 900.0), (1300.0, 2000.0)]  # gap 900..1300 blocked
    berths = [(1000.0, 1200.0, "GAP")]        # sits entirely in the blocked gap
    assert candidate_slots(bands, berths, 150.0) == [(0.0, None), (1300.0, None)]


def test_candidate_dedupes_by_start():
    # Two berths that resolve to the same anchored start collapse to one slot.
    bands = [(0.0, 4000.0)]
    berths = [(0.0, 100.0, "A"), (0.0, 120.0, "B")]
    slots = candidate_slots(bands, berths, 200.0)
    assert len(slots) == 1
    assert slots[0][0] == 0.0
