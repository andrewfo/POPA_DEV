"""Pure conflict-primitive tests — no database.

The overlap matrix (time-only / station-only / both / neither), the half-open
time adjacency edge case, the closed station endpoint case, open-ended time,
empty station ranges, the intersection rectangle, and the category classifier.
The real Postgres ``&&`` is exercised separately in ``test_conflicts_db.py``;
these lock the semantics the SQL must match.
"""
from __future__ import annotations

import datetime as dt

from app.conflicts import (
    ResInterval,
    classify,
    overlap_interval,
    reservations_conflict,
    station_overlaps,
    time_overlaps,
)

UTC = dt.timezone.utc


def _t(day: int) -> dt.datetime:
    return dt.datetime(2026, 7, day, tzinfo=UTC)


# --- time_overlaps: half-open [lo, hi) -------------------------------------
def test_time_overlapping_windows_overlap():
    assert time_overlaps(_t(10), _t(14), _t(12), _t(16)) is True


def test_time_disjoint_windows_do_not_overlap():
    assert time_overlaps(_t(10), _t(12), _t(14), _t(16)) is False


def test_time_adjacent_halfopen_do_not_overlap():
    # [10, 12) then [12, 14): touching at the instant 12 is adjacency, NOT
    # overlap — the PLAN §3.4 requirement.
    assert time_overlaps(_t(10), _t(12), _t(12), _t(14)) is False


def test_time_nested_window_overlaps():
    assert time_overlaps(_t(10), _t(20), _t(12), _t(14)) is True


def test_time_open_ended_upper_overlaps_later_window():
    # None upper = +infinity (open-ended berthing): still alongside at _t(20).
    assert time_overlaps(_t(10), None, _t(20), _t(22)) is True


def test_time_open_ended_lower_overlaps_earlier_window():
    assert time_overlaps(None, _t(12), _t(10), _t(11)) is True


# --- station_overlaps: closed [lo, hi] -------------------------------------
def test_station_overlapping_ranges_overlap():
    assert station_overlaps(400, 900, 800, 1200) is True


def test_station_disjoint_ranges_do_not_overlap():
    assert station_overlaps(400, 900, 1000, 1200) is False


def test_station_shared_endpoint_overlaps():
    # Closed ranges: [400, 900] and [900, 1200] share station 900 -> overlap.
    assert station_overlaps(400, 900, 900, 1200) is True


def test_station_empty_never_overlaps():
    # An unassigned (empty) station range -> None bound -> never conflicts.
    assert station_overlaps(None, None, 400, 900) is False
    assert station_overlaps(400, 900, None, None) is False


# --- reservations_conflict: the AND of both axes ---------------------------
def _ri(t_lo, t_hi, s_lo, s_hi) -> ResInterval:
    return ResInterval(t_lo=t_lo, t_hi=t_hi, s_lo=s_lo, s_hi=s_hi)


def test_conflict_requires_both_time_and_station():
    a = _ri(_t(10), _t(14), 400, 900)
    both = _ri(_t(12), _t(16), 800, 1200)      # overlaps in time AND station
    time_only = _ri(_t(12), _t(16), 1000, 1200)  # time yes, station no
    station_only = _ri(_t(20), _t(22), 800, 1200)  # station yes, time no
    neither = _ri(_t(20), _t(22), 1000, 1200)

    assert reservations_conflict(a, both) is True
    assert reservations_conflict(a, time_only) is False
    assert reservations_conflict(a, station_only) is False
    assert reservations_conflict(a, neither) is False


def test_conflict_with_unassigned_station_never_conflicts():
    a = _ri(_t(10), _t(14), 400, 900)
    unassigned = _ri(_t(10), _t(14), None, None)  # same time, empty station
    assert reservations_conflict(a, unassigned) is False


# --- overlap_interval: the intersection rectangle bounds --------------------
def test_overlap_interval_time():
    assert overlap_interval(_t(10), _t(14), _t(12), _t(16)) == (_t(12), _t(14))


def test_overlap_interval_station():
    assert overlap_interval(400, 900, 800, 1200) == (800, 900)


def test_overlap_interval_open_ended_upper():
    # None upper = +infinity: the min of (None, _t(14)) is _t(14).
    assert overlap_interval(_t(10), None, _t(12), _t(14)) == (_t(12), _t(14))


# --- classify: three lenses on the one primitive ---------------------------
def test_classify_dredge_vs_vessel():
    assert classify("dredge", "confirmed", "vessel", "observed") == "dredge-vs-vessel"
    assert classify("vessel", "confirmed", "dredge", "tentative") == "dredge-vs-vessel"


def test_classify_observed_vs_planned():
    assert classify("vessel", "observed", "vessel", "confirmed") == "observed-vs-planned"
    assert classify("vessel", "tentative", "vessel", "observed") == "observed-vs-planned"


def test_classify_planned_vs_planned():
    assert classify("vessel", "confirmed", "vessel", "tentative") == "planned-vs-planned"
    # Both observed is not the headline "observed-vs-planned" signal.
    assert classify("vessel", "observed", "vessel", "observed") == "planned-vs-planned"
