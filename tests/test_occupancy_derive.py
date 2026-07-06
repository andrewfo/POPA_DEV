"""Occupancy derivation against a live PostGIS DB (marked `db`).

Inserts a controlled measured segment + a synthetic berthing track, then checks
that derivation writes exactly one observed reservation and that re-running is
idempotent (updates the same row, never duplicates).
"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text

from app.occupancy.derive import derive_observed

pytestmark = pytest.mark.db

T0 = dt.datetime(2026, 6, 1, 12, 0, tzinfo=dt.UTC)


def _insert_segment(session) -> int:
    # Measured line lon=0, lat 0->1, M 0->1000. A point at lat=0.5 -> station 500.
    row = session.execute(
        text(
            """
            INSERT INTO wharf_segment
                (name, geom, popa_sta_start, popa_sta_end,
                 corps_scale, corps_offset, dockno_scale, dockno_offset)
            VALUES
                (:name,
                 ST_GeomFromEWKT('SRID=4326;LINESTRING M (0 0 0, 0 1 1000)'),
                 0, 1000, 1, 12040.65, -1, 3365)
            RETURNING id
            """
        ),
        {"name": "TEST_SEGMENT_occupancy"},
    ).first()
    return int(row.id)


def _insert_vessel(session) -> int:
    row = session.execute(
        text(
            """
            INSERT INTO vessel (mmsi, name, loa, beam, dim_a, dim_b, draft)
            VALUES (:mmsi, 'TEST BERTHED', 100, 18, 40, 60, 6.5)
            RETURNING id
            """
        ),
        {"mmsi": 999000777},
    ).first()
    return int(row.id)


def _insert_position(session, vessel_id, minute, *, lat, sog, heading, t0=T0):
    session.execute(
        text(
            """
            INSERT INTO position_report
                (vessel_id, mmsi, lat, lon, geom, sog, heading, nav_status,
                 source, msg_ts, raw)
            VALUES
                (:vid, 999000777, :lat, 0.0,
                 ST_SetSRID(ST_MakePoint(0.0, :lat), 4326),
                 :sog, :heading, 5, 'ais', :ts, '{}'::jsonb)
            """
        ),
        {
            "vid": vessel_id,
            "lat": lat,
            "sog": sog,
            "heading": heading,
            "ts": t0 + dt.timedelta(minutes=minute),
        },
    )


def _seed_berthing_track(session, vessel_id, t0=T0):
    # 0..70 min alongside (lat 0.5, on the line), still, heading north (along line).
    # Still berthed at the last sample -> an OPEN-ENDED event.
    for i in range(8):
        _insert_position(session, vessel_id, i * 10, lat=0.5, sog=0.1, heading=0.0, t0=t0)


def _seed_berth_then_depart_track(session, vessel_id):
    # Alongside + still through 70 min, then underway (SOG above the depart
    # threshold) sustained past the depart-gap -> a CLOSED event ending at 70 min.
    for i in range(8):
        _insert_position(session, vessel_id, i * 10, lat=0.5, sog=0.1, heading=0.0)
    for minute in (80, 90, 100, 110):
        _insert_position(session, vessel_id, minute, lat=0.5, sog=5.0, heading=0.0)


def test_derive_writes_one_reservation_and_is_idempotent(db_session):
    seg_id = _insert_segment(db_session)
    vid = _insert_vessel(db_session)
    _seed_berthing_track(db_session, vid)

    first = derive_observed(db_session, segment_id=seg_id, since=T0 - dt.timedelta(days=1))
    mine = [r for r in first if r.vessel_id == vid]
    assert len(mine) == 1
    res = mine[0]
    assert res.inserted is True
    # Station range centred on ~500 (lat 0.5 on the 0..1000 measured line).
    assert res.station_lo < 500.0 < res.station_hi
    assert res.t_start == T0
    assert res.t_end == T0 + dt.timedelta(minutes=70)
    assert res.direction in ("upstream", "downstream")

    count = db_session.execute(
        text("SELECT count(*) FROM reservation WHERE vessel_id = :v"), {"v": vid}
    ).scalar_one()
    assert count == 1

    # Re-run over the same data: must UPDATE, not duplicate.
    second = derive_observed(db_session, segment_id=seg_id, since=T0 - dt.timedelta(days=1))
    mine2 = [r for r in second if r.vessel_id == vid]
    assert len(mine2) == 1
    assert mine2[0].reservation_id == res.reservation_id
    assert mine2[0].inserted is False

    count2 = db_session.execute(
        text("SELECT count(*) FROM reservation WHERE vessel_id = :v"), {"v": vid}
    ).scalar_one()
    assert count2 == 1


def test_observed_reservation_has_expected_status_and_source(db_session):
    seg_id = _insert_segment(db_session)
    vid = _insert_vessel(db_session)
    _seed_berthing_track(db_session, vid)

    derive_observed(db_session, segment_id=seg_id, since=T0 - dt.timedelta(days=1))
    row = db_session.execute(
        text(
            "SELECT status, source, type FROM reservation WHERE vessel_id = :v"
        ),
        {"v": vid},
    ).one()
    assert (row.status, row.source, row.type) == ("observed", "ais", "vessel")


def test_open_ended_berthing_has_unbounded_upper_and_contains_now(db_session):
    # A still-alongside (open-ended) berthing must stay open above so it contains
    # `now()` while the vessel sits there — not capped at the last AIS fix. The
    # track must be FRESH against the feed clock (a trailing segment silent past
    # berth_stale_close_min is closed as departed-during-a-gap), so seed it
    # ending near now — on a dev DB with live traffic the feed clock IS now.
    t0 = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=70)
    seg_id = _insert_segment(db_session)
    vid = _insert_vessel(db_session)
    _seed_berthing_track(db_session, vid, t0=t0)

    res = derive_observed(db_session, segment_id=seg_id, since=t0 - dt.timedelta(days=1))
    assert [r for r in res if r.vessel_id == vid][0].open_ended is True

    row = db_session.execute(
        text(
            "SELECT upper(time_range) AS hi, (time_range @> now()) AS contains_now "
            "FROM reservation WHERE vessel_id = :v"
        ),
        {"v": vid},
    ).one()
    assert row.hi is None            # unbounded above
    assert row.contains_now is True


def test_stale_open_berthing_is_closed_at_last_fix(db_session):
    # The vessel never sent a departure — it left while coverage was down — but
    # the FEED kept landing fixes (another vessel reports now). The trailing
    # segment is stale against the feed clock and must be CLOSED at its last
    # fix instead of reading "ongoing" forever.
    seg_id = _insert_segment(db_session)
    vid = _insert_vessel(db_session)
    _seed_berthing_track(db_session, vid)          # ends T0+70, long ago
    other = db_session.execute(
        text("INSERT INTO vessel (mmsi, name) VALUES (999000778, 'FEED PROOF') "
             "RETURNING id")
    ).scalar_one()
    db_session.execute(
        text(
            """
            INSERT INTO position_report
                (vessel_id, mmsi, lat, lon, geom, sog, source, msg_ts, raw)
            VALUES (:vid, 999000778, 0.9, 0.0,
                    ST_SetSRID(ST_MakePoint(0.0, 0.9), 4326),
                    7.0, 'ais', now(), '{}'::jsonb)
            """
        ),
        {"vid": other},
    )

    res = derive_observed(db_session, segment_id=seg_id, since=T0 - dt.timedelta(days=1))
    mine = [r for r in res if r.vessel_id == vid]
    assert len(mine) == 1
    assert mine[0].open_ended is False

    row = db_session.execute(
        text("SELECT upper(time_range) AS hi FROM reservation WHERE vessel_id = :v"),
        {"v": vid},
    ).one()
    assert row.hi == T0 + dt.timedelta(minutes=70)   # closed at the last fix


def test_departed_berthing_has_bounded_upper(db_session):
    # Once the vessel leaves, the event is closed: the stored range gets a real
    # upper bound (the last still fix), and no longer contains `now()`.
    seg_id = _insert_segment(db_session)
    vid = _insert_vessel(db_session)
    _seed_berth_then_depart_track(db_session, vid)

    res = derive_observed(db_session, segment_id=seg_id, since=T0 - dt.timedelta(days=1))
    mine = [r for r in res if r.vessel_id == vid]
    assert len(mine) == 1
    assert mine[0].open_ended is False

    row = db_session.execute(
        text(
            "SELECT upper(time_range) AS hi, (time_range @> now()) AS contains_now "
            "FROM reservation WHERE vessel_id = :v"
        ),
        {"v": vid},
    ).one()
    assert row.hi == T0 + dt.timedelta(minutes=70)
    assert row.contains_now is False
