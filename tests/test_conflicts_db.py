"""DB-marked tests for the conflict service (GET /conflicts).

Exercises the endpoint through FastAPI's TestClient against a live PostGIS — so
the REAL Postgres ``&&`` / ``*`` range operators are the thing under test, not a
Python re-implementation. Auto-skips (via the ``db_session`` fixture) when no
migrated DB is around. Seeds reservations inside the fixture's rolled-back
transaction; nothing leaks.
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
    """A TestClient whose DB session is the test's transactional one."""
    app.dependency_overrides[get_session] = lambda: db_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_session, None)


def _add_reservation(session, *, lo, hi, t_start, t_end, status="requested",
                     rtype="vessel", vessel_id=None):
    """Insert a reservation directly; returns its id. Empty station range when
    lo/hi are None (mirrors an unassigned manual request)."""
    station = "'empty'::numrange" if lo is None else "numrange(:lo, :hi, '[]')"
    rid = session.execute(
        text(
            f"""
            INSERT INTO reservation
                (vessel_id, type, station_range, time_range, status, source, created_at)
            VALUES (:vessel_id, CAST(:rtype AS reservation_type), {station},
                    tstzrange(:t_start, :t_end, '[)'),
                    CAST(:status AS reservation_status), 'phone', now())
            RETURNING id
            """
        ),
        {"vessel_id": vessel_id, "lo": lo, "hi": hi, "t_start": t_start,
         "t_end": t_end, "status": status, "rtype": rtype},
    ).scalar_one()
    return rid


def _add_vessel(session, *, mmsi, name="TESTCRAFT", ship_type=None):
    return session.execute(
        text("INSERT INTO vessel (mmsi, name, ship_type) VALUES (:m, :n, :t) RETURNING id"),
        {"m": mmsi, "n": name, "t": ship_type},
    ).scalar_one()


def _t(day, hour=0):
    # Far future on purpose: GET /conflicts now defaults to current/future pairs
    # (current=true), so fixture windows must stay ahead of the wall clock to
    # remain visible — and "past" cases use 2020 (see the current-scope tests).
    return dt.datetime(2030, 7, day, hour, tzinfo=UTC)


def _pair(payload, id_a, id_b):
    """The conflict dict for the {id_a, id_b} pair, or None if not present."""
    want = {id_a, id_b}
    for c in payload:
        if {c["a"]["id"], c["b"]["id"]} == want:
            return c
    return None


# --- the overlap matrix in real Postgres -----------------------------------
def test_overlapping_pair_appears_with_overlap_rectangle(client, db_session):
    a = _add_reservation(db_session, lo=400, hi=900, t_start=_t(10), t_end=_t(14),
                         status="observed")
    b = _add_reservation(db_session, lo=800, hi=1200, t_start=_t(12), t_end=_t(16),
                         status="confirmed")
    r = client.get("/conflicts")
    assert r.status_code == 200
    c = _pair(r.json(), a, b)
    assert c is not None
    # Station intersection [800, 900] (closed); time intersection [12, 14).
    assert c["overlap"]["station_lo"] == 800.0
    assert c["overlap"]["station_hi"] == 900.0
    assert dt.datetime.fromisoformat(c["overlap"]["t_start"]) == _t(12)
    assert dt.datetime.fromisoformat(c["overlap"]["t_end"]) == _t(14)
    # Dock No. equivalents are present (server-side conversion).
    assert c["overlap"]["station_lo_dock"] is not None
    assert c["overlap"]["station_hi_dock"] is not None
    # Exactly one side observed -> the headline signal.
    assert c["category"] == "observed-vs-planned"


def test_time_only_overlap_does_not_conflict(client, db_session):
    a = _add_reservation(db_session, lo=400, hi=900, t_start=_t(10), t_end=_t(14))
    b = _add_reservation(db_session, lo=1000, hi=1200, t_start=_t(12), t_end=_t(16))
    assert _pair(client.get("/conflicts").json(), a, b) is None


def test_station_only_overlap_does_not_conflict(client, db_session):
    a = _add_reservation(db_session, lo=400, hi=900, t_start=_t(10), t_end=_t(14))
    b = _add_reservation(db_session, lo=800, hi=1200, t_start=_t(20), t_end=_t(22))
    assert _pair(client.get("/conflicts").json(), a, b) is None


def test_time_adjacent_halfopen_does_not_conflict(client, db_session):
    # [10, 12) then [12, 14): touching at 12 is adjacency, not overlap.
    a = _add_reservation(db_session, lo=400, hi=900, t_start=_t(10), t_end=_t(12))
    b = _add_reservation(db_session, lo=400, hi=900, t_start=_t(12), t_end=_t(14))
    assert _pair(client.get("/conflicts").json(), a, b) is None


def test_unassigned_request_never_conflicts(client, db_session):
    # An empty (unassigned) station range never participates, even over the same
    # window and an otherwise-overlapping berth — the intended invariant.
    occupied = _add_reservation(db_session, lo=400, hi=900, t_start=_t(10), t_end=_t(14),
                                status="observed")
    request = _add_reservation(db_session, lo=None, hi=None, t_start=_t(10), t_end=_t(14),
                               status="requested")
    assert _pair(client.get("/conflicts").json(), occupied, request) is None


# --- dredge uses the identical path ----------------------------------------
def test_dredge_vs_vessel_uses_same_path(client, db_session):
    vessel = _add_reservation(db_session, lo=400, hi=900, t_start=_t(10), t_end=_t(14),
                              status="observed")
    dredge = _add_reservation(db_session, lo=800, hi=1200, t_start=_t(12), t_end=_t(16),
                              status="confirmed", rtype="dredge")
    c = _pair(client.get("/conflicts").json(), vessel, dredge)
    assert c is not None
    assert c["category"] == "dredge-vs-vessel"


# --- status filter ---------------------------------------------------------
def test_status_filter_keeps_only_matching_side(client, db_session):
    obs = _add_reservation(db_session, lo=400, hi=900, t_start=_t(10), t_end=_t(14),
                           status="observed")
    conf = _add_reservation(db_session, lo=800, hi=1200, t_start=_t(12), t_end=_t(16),
                            status="confirmed")
    # A planned-vs-planned pair with no observed side (tentative + confirmed; not
    # blocked by the confirmed-only constraint).
    tent = _add_reservation(db_session, lo=2000, hi=2400, t_start=_t(10), t_end=_t(14),
                            status="tentative")
    conf2 = _add_reservation(db_session, lo=2200, hi=2600, t_start=_t(12), t_end=_t(16),
                             status="confirmed")
    payload = client.get("/conflicts", params={"status": "observed"}).json()
    assert _pair(payload, obs, conf) is not None       # has an observed side
    assert _pair(payload, tent, conf2) is None          # no observed side -> filtered


# --- live scoping: current/future only by default ---------------------------
def _t_past(day, hour=0):
    return dt.datetime(2020, 7, day, hour, tzinfo=UTC)


def test_past_pair_hidden_by_default(client, db_session):
    # A collision whose overlap fully resolved in the past is History, not an
    # alert: the live default (current=true) hides it; current=false shows it.
    a = _add_reservation(db_session, lo=400, hi=900, t_start=_t_past(10),
                         t_end=_t_past(14), status="observed")
    b = _add_reservation(db_session, lo=800, hi=1200, t_start=_t_past(12),
                         t_end=_t_past(16), status="tentative")
    assert _pair(client.get("/conflicts").json(), a, b) is None
    out = client.get("/conflicts", params={"current": "false"}).json()
    assert _pair(out, a, b) is not None


def test_ongoing_pair_survives_current_default(client, db_session):
    # An open-ended observed berthing against a window reaching the future is a
    # live alert — the current default must keep it.
    a = _add_reservation(db_session, lo=400, hi=900, t_start=_t_past(10),
                         t_end=None, status="observed")
    b = _add_reservation(db_session, lo=800, hi=1200, t_start=_t(12),
                         t_end=_t(16), status="confirmed")
    assert _pair(client.get("/conflicts").json(), a, b) is not None


# --- harbor service craft hidden from the live feed --------------------------
def test_observed_service_craft_pair_hidden_by_default(client, db_session):
    # An OBSERVED tug (AIS type 52) overlapping a planned ship is almost always
    # the tug working that very move — hidden unless service_craft=1 asks.
    tug = _add_vessel(db_session, mmsi=367000901, name="TESTTUG", ship_type=52)
    obs = _add_reservation(db_session, lo=400, hi=900, t_start=_t(10),
                           t_end=None, status="observed", vessel_id=tug)
    plan = _add_reservation(db_session, lo=800, hi=1200, t_start=_t(12),
                            t_end=_t(16), status="confirmed")
    assert _pair(client.get("/conflicts").json(), obs, plan) is None
    out = client.get("/conflicts", params={"service_craft": "true"}).json()
    assert _pair(out, obs, plan) is not None


def test_planned_service_craft_pair_is_kept(client, db_session):
    # The filter is observed-side only: a PLANNED row for a tug is operator-
    # entered on purpose and must keep conflicting like any other booking.
    tug = _add_vessel(db_session, mmsi=367000902, name="TESTTUG2", ship_type=52)
    plan_tug = _add_reservation(db_session, lo=400, hi=900, t_start=_t(10),
                                t_end=_t(14), status="tentative", vessel_id=tug)
    plan = _add_reservation(db_session, lo=800, hi=1200, t_start=_t(12),
                            t_end=_t(16), status="confirmed")
    assert _pair(client.get("/conflicts").json(), plan_tug, plan) is not None


def test_observed_vs_observed_pair_never_conflicts(client, db_session):
    # AIS can't conflict with itself: two observed rows overlapping (rafted tug,
    # projection slop, stale artifact) is not a scheduling conflict — at least
    # one side must be a planned row. Not even current=false/service_craft=true
    # widens this back in.
    a = _add_reservation(db_session, lo=400, hi=900, t_start=_t(10),
                         t_end=None, status="observed")
    b = _add_reservation(db_session, lo=800, hi=1200, t_start=_t(12),
                         t_end=None, status="observed")
    assert _pair(client.get("/conflicts").json(), a, b) is None
    out = client.get("/conflicts", params={
        "current": "false", "service_craft": "true"}).json()
    assert _pair(out, a, b) is None


def test_same_vessel_pair_never_conflicts(client, db_session):
    # A ship can't conflict with itself: its AIS-observed berthing overlapping
    # its own planned reservation is the arrived/where-planned verification
    # signal, not two things competing for one stretch of wharf. Same vessel_id
    # on both sides -> dropped, even with the filters widened.
    v = _add_vessel(db_session, mmsi=367000904, name="STOLT ENDURANCE")
    obs = _add_reservation(db_session, lo=400, hi=900, t_start=_t(10),
                           t_end=None, status="observed", vessel_id=v)
    plan = _add_reservation(db_session, lo=800, hi=1200, t_start=_t(12),
                            t_end=_t(16), status="confirmed", vessel_id=v)
    assert _pair(client.get("/conflicts").json(), obs, plan) is None
    out = client.get("/conflicts", params={
        "current": "false", "service_craft": "true"}).json()
    assert _pair(out, obs, plan) is None


def test_untyped_observed_vessel_is_kept(client, db_session):
    # NULL ship_type could be a real arrival — when in doubt, show it.
    v = _add_vessel(db_session, mmsi=367000903, name="NOTYPE", ship_type=None)
    obs = _add_reservation(db_session, lo=400, hi=900, t_start=_t(10),
                           t_end=None, status="observed", vessel_id=v)
    plan = _add_reservation(db_session, lo=800, hi=1200, t_start=_t(12),
                            t_end=_t(16), status="confirmed")
    assert _pair(client.get("/conflicts").json(), obs, plan) is not None


# --- time window -----------------------------------------------------------
def test_window_narrows_to_overlapping_pairs(client, db_session):
    a = _add_reservation(db_session, lo=400, hi=900, t_start=_t(10), t_end=_t(14),
                         status="observed")
    b = _add_reservation(db_session, lo=800, hi=1200, t_start=_t(12), t_end=_t(16),
                         status="confirmed")
    # A September window excludes the July pair.
    out = client.get("/conflicts", params={
        "from": "2030-09-01T00:00:00Z", "to": "2030-09-05T00:00:00Z"}).json()
    assert _pair(out, a, b) is None
    # A July window includes it.
    inside = client.get("/conflicts", params={
        "from": "2030-07-09T00:00:00Z", "to": "2030-07-20T00:00:00Z"}).json()
    assert _pair(inside, a, b) is not None
