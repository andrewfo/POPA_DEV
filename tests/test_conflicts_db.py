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


def _add_reservation(session, *, lo, hi, t_start, t_end, status="requested", rtype="vessel"):
    """Insert a reservation directly; returns its id. Empty station range when
    lo/hi are None (mirrors an unassigned manual request)."""
    station = "'empty'::numrange" if lo is None else "numrange(:lo, :hi, '[]')"
    rid = session.execute(
        text(
            f"""
            INSERT INTO reservation
                (type, station_range, time_range, status, source, created_at)
            VALUES (CAST(:rtype AS reservation_type), {station},
                    tstzrange(:t_start, :t_end, '[)'),
                    CAST(:status AS reservation_status), 'phone', now())
            RETURNING id
            """
        ),
        {"lo": lo, "hi": hi, "t_start": t_start, "t_end": t_end,
         "status": status, "rtype": rtype},
    ).scalar_one()
    return rid


def _t(day, hour=0):
    return dt.datetime(2026, 7, day, hour, tzinfo=UTC)


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


# --- time window -----------------------------------------------------------
def test_window_narrows_to_overlapping_pairs(client, db_session):
    a = _add_reservation(db_session, lo=400, hi=900, t_start=_t(10), t_end=_t(14),
                         status="observed")
    b = _add_reservation(db_session, lo=800, hi=1200, t_start=_t(12), t_end=_t(16),
                         status="confirmed")
    # A September window excludes the July pair.
    out = client.get("/conflicts", params={
        "from": "2026-09-01T00:00:00Z", "to": "2026-09-05T00:00:00Z"}).json()
    assert _pair(out, a, b) is None
    # A July window includes it.
    inside = client.get("/conflicts", params={
        "from": "2026-07-09T00:00:00Z", "to": "2026-07-20T00:00:00Z"}).json()
    assert _pair(inside, a, b) is not None
