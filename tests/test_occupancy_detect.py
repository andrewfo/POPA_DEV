"""Berthing detector tests — pure, no database."""
from __future__ import annotations

import datetime as dt

from app.occupancy.detect import Sample, detect_berthings

T0 = dt.datetime(2026, 6, 1, 12, 0, tzinfo=dt.timezone.utc)


def _track(specs: list[tuple[int, bool, float]]) -> list[Sample]:
    """specs: (minute_offset, alongside, sog)."""
    return [Sample(T0 + dt.timedelta(minutes=m), a, s) for m, a, s in specs]


def test_arrive_dwell_depart_yields_one_event():
    # Approach (moving, not alongside), then 60 min berthed, then leave.
    track = _track(
        [(0, False, 8.0), (5, True, 3.0)]  # approaching
        + [(10 + i * 5, True, 0.1) for i in range(13)]  # 10..70 min, still
        + [(75, True, 2.5), (85, False, 6.0)]  # departing, sustained > gap
    )
    events = detect_berthings(track)
    assert len(events) == 1
    ev = events[0]
    assert ev.t_start == T0 + dt.timedelta(minutes=10)
    assert ev.t_end == T0 + dt.timedelta(minutes=70)
    assert ev.open_ended is False


def test_short_stop_below_dwell_is_ignored():
    # Only 10 minutes alongside & still — under the 20 min dwell.
    track = _track([(0, True, 0.1), (5, True, 0.2), (10, True, 0.1), (20, False, 5.0)])
    assert detect_berthings(track) == []


def test_brief_blip_does_not_split_event():
    # One spurious fast/away sample mid-berthing (shorter than depart_gap) must
    # not break the single event in two.
    track = _track(
        [(i * 5, True, 0.1) for i in range(6)]  # 0..25 min still
        + [(30, False, 4.0)]  # blip at 30 (gap from last-still=5min < 10min)
        + [(35, True, 0.1), (40, True, 0.2), (60, True, 0.1)]  # back, still
    )
    events = detect_berthings(track)
    assert len(events) == 1
    assert events[0].t_start == T0
    assert events[0].t_end == T0 + dt.timedelta(minutes=60)


def test_two_separate_berthings():
    track = _track(
        [(i * 5, True, 0.1) for i in range(7)]  # 0..30 still  (event 1)
        + [(35 + i * 5, False, 6.0) for i in range(5)]  # 35..55 away (> gap)
        + [(60 + i * 5, True, 0.1) for i in range(7)]  # 60..90 still (event 2)
    )
    events = detect_berthings(track)
    assert len(events) == 2
    assert events[0].t_start == T0
    assert events[0].t_end == T0 + dt.timedelta(minutes=30)
    assert events[1].t_start == T0 + dt.timedelta(minutes=60)


def test_open_ended_when_still_berthed_at_end():
    track = _track([(i * 5, True, 0.1) for i in range(7)])  # 0..30, never leaves
    events = detect_berthings(track)
    assert len(events) == 1
    assert events[0].open_ended is True


def test_speed_between_thresholds_keeps_berthed():
    # SOG drifts between enter (0.5) and depart (1.0) — neither re-enters nor
    # departs; the vessel stays berthed across the whole window.
    track = _track(
        [(0, True, 0.1)]
        + [(5 + i * 5, True, 0.8) for i in range(13)]  # 5..65 min, neutral band
    )
    events = detect_berthings(track)
    assert len(events) == 1
    assert events[0].t_end == T0 + dt.timedelta(minutes=65)


def test_moored_nav_status_relaxes_entry():
    # SOG just above enter threshold but moored (nav 5) and alongside -> berthed.
    track = [
        Sample(T0 + dt.timedelta(minutes=m), True, 0.9, nav_status=5)
        for m in range(0, 35, 5)
    ]
    events = detect_berthings(track)
    assert len(events) == 1
