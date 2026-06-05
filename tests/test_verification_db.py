"""DB-marked tests for the AIS verification service (GET /verification).

Exercises the endpoint through TestClient against a live PostGIS, so the real
LATERAL join + Postgres range operators are under test. Auto-skips (via the
``db_session`` fixture) when no migrated DB is around; seeds inside the fixture's
rolled-back transaction so nothing leaks.

States are judged against the DB clock (``now()``), so "past"/"future" windows use
far-off years (2020 / 2030) to stay deterministic regardless of the wall clock.
"""
from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import get_session
from app.main import app

UTC = dt.timezone.utc


@pytest.fixture
def client(db_session):
    app.dependency_overrides[get_session] = lambda: db_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_session, None)


def _add_vessel(session, *, mmsi=None, imo=None, name="TESTSHIP"):
    """A vessel row (needs at least one of mmsi/imo per the CHECK constraint)."""
    return session.execute(
        text("INSERT INTO vessel (mmsi, imo, name) VALUES (:m, :i, :n) RETURNING id"),
        {"m": mmsi, "i": imo, "n": name},
    ).scalar_one()


def _add_res(session, *, vessel_id, lo, hi, t_start, t_end, status, source="phone"):
    """Insert a reservation; empty station range when lo/hi are None, open-ended
    time when t_end is None."""
    station = "'empty'::numrange" if lo is None else "numrange(:lo, :hi, '[]')"
    return session.execute(
        text(
            f"""
            INSERT INTO reservation
                (vessel_id, type, station_range, time_range, status, source, created_at)
            VALUES (:vessel_id, 'vessel', {station},
                    tstzrange(:t_start, :t_end, '[)'),
                    CAST(:status AS reservation_status),
                    CAST(:source AS reservation_source), now())
            RETURNING id
            """
        ),
        {"vessel_id": vessel_id, "lo": lo, "hi": hi, "t_start": t_start,
         "t_end": t_end, "status": status, "source": source},
    ).scalar_one()


def _t(year, day, hour=0):
    return dt.datetime(year, 7, day, hour, tzinfo=UTC)


def _planned(payload, rid):
    return next((p for p in payload["planned"] if p["id"] == rid), None)


def _unplanned(payload, rid):
    return next((u for u in payload["unplanned"] if u["id"] == rid), None)


# --- the three planned states ----------------------------------------------
def test_arrived_when_observed_overlaps_same_vessel(client, db_session):
    v = _add_vessel(db_session, mmsi=636000010, imo=9000010)
    # Unplaced request (empty station) over a window an observed berthing covers.
    req = _add_res(db_session, vessel_id=v, lo=None, hi=None,
                   t_start=_t(2026, 10), t_end=_t(2026, 14), status="requested")
    _add_res(db_session, vessel_id=v, lo=400, hi=900,
             t_start=_t(2026, 11), t_end=_t(2026, 13), status="observed", source="ais")
    p = _planned(client.get("/verification").json(), req)
    assert p is not None
    assert p["state"] == "arrived"
    assert p["observed"] is not None
    assert p["where_planned"] is None          # request was unplaced -> can't tell


def test_no_show_when_past_window_unseen(client, db_session):
    v = _add_vessel(db_session, mmsi=636000011, imo=9000011)
    req = _add_res(db_session, vessel_id=v, lo=None, hi=None,
                   t_start=_t(2020, 10), t_end=_t(2020, 14), status="confirmed")
    p = _planned(client.get("/verification").json(), req)
    assert p is not None and p["state"] == "no_show"
    assert p["observed"] is None


def test_awaiting_when_future_window_unseen(client, db_session):
    v = _add_vessel(db_session, mmsi=636000012, imo=9000012)
    req = _add_res(db_session, vessel_id=v, lo=None, hi=None,
                   t_start=_t(2030, 10), t_end=_t(2030, 14), status="tentative")
    p = _planned(client.get("/verification").json(), req)
    assert p is not None and p["state"] == "awaiting"


# --- where_planned (the inline observed-vs-planned signal) -----------------
def test_where_planned_true_when_berthed_where_planned(client, db_session):
    v = _add_vessel(db_session, mmsi=636000013, imo=9000013)
    plan = _add_res(db_session, vessel_id=v, lo=400, hi=900,
                    t_start=_t(2026, 10), t_end=_t(2026, 14), status="confirmed")
    _add_res(db_session, vessel_id=v, lo=800, hi=1200,
             t_start=_t(2026, 11), t_end=_t(2026, 13), status="observed", source="ais")
    p = _planned(client.get("/verification").json(), plan)
    assert p["state"] == "arrived"
    assert p["where_planned"] is True          # 400-900 overlaps observed 800-1200


def test_where_planned_false_when_berthed_elsewhere(client, db_session):
    v = _add_vessel(db_session, mmsi=636000014, imo=9000014)
    plan = _add_res(db_session, vessel_id=v, lo=2000, hi=2400,
                    t_start=_t(2026, 10), t_end=_t(2026, 14), status="confirmed")
    _add_res(db_session, vessel_id=v, lo=400, hi=900,
             t_start=_t(2026, 11), t_end=_t(2026, 13), status="observed", source="ais")
    p = _planned(client.get("/verification").json(), plan)
    assert p["state"] == "arrived"
    assert p["where_planned"] is False         # planned 2000-2400, seen 400-900


# --- unplanned occupancy ----------------------------------------------------
def test_unplanned_observed_has_no_plan(client, db_session):
    v = _add_vessel(db_session, mmsi=636000015, imo=9000015)
    obs = _add_res(db_session, vessel_id=v, lo=400, hi=900,
                   t_start=_t(2026, 11), t_end=None, status="observed", source="ais")
    payload = client.get("/verification").json()
    u = _unplanned(payload, obs)
    assert u is not None
    assert u["ongoing"] is True                 # open-ended berthing
    # The observed row is not itself a "planned" entry.
    assert _planned(payload, obs) is None


def test_observed_with_matching_plan_is_not_unplanned(client, db_session):
    v = _add_vessel(db_session, mmsi=636000016, imo=9000016)
    _add_res(db_session, vessel_id=v, lo=None, hi=None,
             t_start=_t(2026, 10), t_end=_t(2026, 14), status="requested")
    obs = _add_res(db_session, vessel_id=v, lo=400, hi=900,
                   t_start=_t(2026, 11), t_end=_t(2026, 13), status="observed", source="ais")
    assert _unplanned(client.get("/verification").json(), obs) is None


# --- exclusions / filters ---------------------------------------------------
def test_vesselless_planned_row_excluded(client, db_session):
    # A name-only request (no vessel_id) can't be matched -> not in planned.
    rid = _add_res(db_session, vessel_id=None, lo=None, hi=None,
                   t_start=_t(2026, 10), t_end=_t(2026, 14), status="requested")
    assert _planned(client.get("/verification").json(), rid) is None


def test_window_narrows_planned_rows(client, db_session):
    v = _add_vessel(db_session, mmsi=636000017, imo=9000017)
    req = _add_res(db_session, vessel_id=v, lo=None, hi=None,
                   t_start=_t(2026, 10), t_end=_t(2026, 14), status="confirmed")
    out = client.get("/verification", params={
        "from": "2026-09-01T00:00:00Z", "to": "2026-09-05T00:00:00Z"}).json()
    assert _planned(out, req) is None
    inside = client.get("/verification", params={
        "from": "2026-07-09T00:00:00Z", "to": "2026-07-20T00:00:00Z"}).json()
    assert _planned(inside, req) is not None


# --- POST /verification/sweep (auto-archive stale planned rows) -------------
def _status_of(session, rid):
    return session.execute(
        text("SELECT status::text FROM reservation WHERE id = :id"), {"id": rid}
    ).scalar_one()


def _expired_ids(payload):
    return {e["id"] for e in payload.get("expired", [])}


def test_sweep_archives_no_show_as_cancelled(client, db_session):
    # A confirmed booking whose window is long past with no observed berthing.
    v = _add_vessel(db_session, mmsi=636000020, imo=9000020)
    rid = _add_res(db_session, vessel_id=v, lo=None, hi=None,
                   t_start=_t(2020, 10), t_end=_t(2020, 14), status="confirmed")
    # Read-only GET must NOT mutate — still surfaces it as a no_show.
    assert _planned(client.get("/verification").json(), rid)["state"] == "no_show"
    assert _status_of(db_session, rid) == "confirmed"
    # The sweep archives it: status -> cancelled, gone from planned, in `expired`.
    out = client.post("/verification/sweep").json()
    assert rid in _expired_ids(out)
    assert _planned(out, rid) is None
    assert _status_of(db_session, rid) == "cancelled"


def test_sweep_completes_arrived(client, db_session):
    v = _add_vessel(db_session, mmsi=636000021, imo=9000021)
    plan = _add_res(db_session, vessel_id=v, lo=400, hi=900,
                    t_start=_t(2020, 10), t_end=_t(2020, 14), status="confirmed")
    _add_res(db_session, vessel_id=v, lo=400, hi=900,
             t_start=_t(2020, 11), t_end=_t(2020, 13), status="observed", source="ais")
    out = client.post("/verification/sweep").json()
    assert plan in _expired_ids(out)
    assert _status_of(db_session, plan) == "completed"


def test_sweep_leaves_future_and_within_grace(client, db_session):
    # Future window: never expires.
    vf = _add_vessel(db_session, mmsi=636000022, imo=9000022)
    future = _add_res(db_session, vessel_id=vf, lo=None, hi=None,
                      t_start=_t(2030, 10), t_end=_t(2030, 14), status="confirmed")
    # Window ended ~10 min ago — inside the default 60-min grace, so it lingers.
    vr = _add_vessel(db_session, mmsi=636000023, imo=9000023)
    now = dt.datetime.now(UTC)
    recent = _add_res(db_session, vessel_id=vr, lo=None, hi=None,
                      t_start=now - dt.timedelta(hours=2),
                      t_end=now - dt.timedelta(minutes=10), status="confirmed")
    out = client.post("/verification/sweep").json()
    assert future not in _expired_ids(out)
    assert recent not in _expired_ids(out)
    assert _status_of(db_session, future) == "confirmed"
    assert _status_of(db_session, recent) == "confirmed"
    # Both still appear as live planned rows.
    assert _planned(out, future)["state"] == "awaiting"
    assert _planned(out, recent)["state"] == "no_show"


def test_sweep_appends_audit_note(client, db_session):
    v = _add_vessel(db_session, mmsi=636000024, imo=9000024, name="GHOST")
    rid = _add_res(db_session, vessel_id=v, lo=None, hi=None,
                   t_start=_t(2020, 10), t_end=_t(2020, 14), status="requested")
    client.post("/verification/sweep")
    note = db_session.execute(
        text("SELECT notes FROM reservation WHERE id = :id"), {"id": rid}
    ).scalar_one()
    assert note is not None and "no-show" in note


# --- GET /vessels/{id} detail ----------------------------------------------
def test_vessel_detail_aggregates_record_and_reservations(client, db_session):
    v = _add_vessel(db_session, mmsi=636000030, imo=9000030, name="DETAILSHIP")
    db_session.execute(
        text("UPDATE vessel SET loa = 180, beam = 32, draft = 11.5, "
             "callsign = 'ABCD', ship_type = 80 WHERE id = :id"),
        {"id": v},
    )
    rid = _add_res(db_session, vessel_id=v, lo=400, hi=900,
                   t_start=_t(2026, 10), t_end=_t(2026, 14), status="confirmed")
    body = client.get(f"/vessels/{v}").json()
    assert body["name"] == "DETAILSHIP"
    assert body["imo"] == 9000030
    assert body["loa"] == 180.0
    assert body["callsign"] == "ABCD"
    ids = {r["id"] for r in body["reservations"]}
    assert rid in ids
    assert body["latest_position"] is None        # no AIS fix landed


def test_vessel_detail_includes_latest_position(client, db_session):
    v = _add_vessel(db_session, mmsi=636000031, imo=9000031)
    db_session.execute(
        text(
            """
            INSERT INTO position_report (vessel_id, mmsi, lat, lon, geom, sog, msg_ts, raw, source)
            VALUES (:vid, :mmsi, 29.86, -93.94,
                    ST_SetSRID(ST_MakePoint(-93.94, 29.86), 4326),
                    0.2, now(), '{}'::jsonb, 'ais')
            """
        ),
        {"vid": v, "mmsi": 636000031},
    )
    body = client.get(f"/vessels/{v}").json()
    assert body["latest_position"] is not None
    assert body["latest_position"]["sog"] == 0.2


def test_vessel_detail_imo_only_vessel(client, db_session):
    # A vessel with no MMSI (IMO-only) must not trip the latest-position query's
    # NULL-typed bind param — regression for the AmbiguousParameter 500.
    v = _add_vessel(db_session, mmsi=None, imo=9000032, name="NOMMSI")
    r = client.get(f"/vessels/{v}")
    assert r.status_code == 200
    assert r.json()["latest_position"] is None


def test_vessel_detail_404_when_missing(client, db_session):
    assert client.get("/vessels/99999999").status_code == 404
