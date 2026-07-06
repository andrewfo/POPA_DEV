"""Pure unit tests for the AIS verification classifiers — no database.

Mirrors ``tests/test_conflicts.py``: the state machine (``arrived``/``no_show``/
``awaiting``) and the ``where_planned`` predicate are exercised directly, with the
DB query covered separately in ``test_verification_db.py``.
"""
from __future__ import annotations

from datetime import UTC, datetime

from app.verification import classify_planned, expiry_action, where_planned

UTC = UTC


def _t(y, m, d):
    return datetime(y, m, d, tzinfo=UTC)


AS_OF = _t(2026, 6, 4)


# --- classify_planned ------------------------------------------------------
def test_arrived_when_observed_present_regardless_of_window():
    # A matching observed berthing wins whether the window is past or future.
    assert classify_planned(_t(2030, 1, 1), _t(2030, 1, 2), True, AS_OF) == "arrived"
    assert classify_planned(_t(2020, 1, 1), _t(2020, 1, 2), True, AS_OF) == "arrived"


def test_no_show_when_window_elapsed_and_unseen():
    assert classify_planned(_t(2020, 1, 1), _t(2020, 1, 2), False, AS_OF) == "no_show"


def test_awaiting_when_future_and_unseen():
    assert classify_planned(_t(2030, 1, 1), _t(2030, 1, 2), False, AS_OF) == "awaiting"


def test_open_ended_window_is_never_no_show():
    # t_end None = open-ended; it can't have "fully elapsed".
    assert classify_planned(_t(2020, 1, 1), None, False, AS_OF) == "awaiting"


def test_no_show_boundary_tend_equals_as_of():
    # Half-open [start, end): a window ending exactly at as_of has elapsed.
    assert classify_planned(_t(2026, 1, 1), AS_OF, False, AS_OF) == "no_show"


# --- where_planned ---------------------------------------------------------
def test_where_planned_none_when_unplaced():
    # An unplaced requested row (empty station range) -> can't tell.
    assert where_planned(None, None, 400.0, 900.0) is None


def test_where_planned_none_when_no_observed():
    assert where_planned(400.0, 900.0, None, None) is None


def test_where_planned_true_on_station_overlap():
    # Closed-interval overlap (shares 800..900).
    assert where_planned(400.0, 900.0, 800.0, 1200.0) is True


def test_where_planned_false_when_disjoint():
    assert where_planned(400.0, 900.0, 1000.0, 1200.0) is False


# --- expiry_action (what a stale planned row becomes) ----------------------
def test_expiry_action_arrived_completes_any_status():
    # An observed berthing is positive evidence regardless of feed/status.
    assert expiry_action(True, False, "confirmed") == "completed"
    assert expiry_action(True, True, "requested") == "completed"


def test_expiry_action_dead_feed_is_not_a_no_show():
    # No berthing AND no AIS traffic in the window -> can't infer a no-show; the
    # feed may simply have been down. Leave the row alone (None).
    assert expiry_action(False, False, "requested") is None
    assert expiry_action(False, False, "confirmed") is None


def test_expiry_action_no_show_cancels_requested_tentative():
    # Feed was live, vessel unseen -> a genuine no-show; cheap to re-create.
    assert expiry_action(False, True, "requested") == "cancelled"
    assert expiry_action(False, True, "tentative") == "cancelled"


def test_expiry_action_confirmed_no_show_is_flagged_not_cancelled():
    # A slipped ETA must not auto-destroy a confirmed commitment.
    assert expiry_action(False, True, "confirmed") == "flagged"
