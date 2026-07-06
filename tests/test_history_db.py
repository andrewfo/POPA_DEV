"""DB-marked tests for GET /history filters.

Exercises the endpoint through TestClient against a live PostGIS; auto-skips (via
the ``db_session`` fixture) when no migrated DB is around. Focus here is the
filter matrix — in particular the IMO search added alongside the name search.
"""
from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import get_session
from app.main import app

UTC = dt.UTC


@pytest.fixture
def client(db_session):
    app.dependency_overrides[get_session] = lambda: db_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_session, None)


def _add_vessel(session, *, mmsi=None, imo=None, name="HISTSHIP"):
    return session.execute(
        text("INSERT INTO vessel (mmsi, imo, name) VALUES (:m, :i, :n) RETURNING id"),
        {"m": mmsi, "i": imo, "n": name},
    ).scalar_one()


def _add_res(session, *, vessel_id, status="confirmed", source="phone"):
    return session.execute(
        text(
            """
            INSERT INTO reservation
                (vessel_id, type, station_range, time_range, status, source, created_at)
            VALUES (:vessel_id, 'vessel', 'empty'::numrange,
                    tstzrange(:t0, :t1, '[)'),
                    CAST(:status AS reservation_status),
                    CAST(:source AS reservation_source), now())
            RETURNING id
            """
        ),
        {"vessel_id": vessel_id, "status": status, "source": source,
         "t0": dt.datetime(2026, 7, 10, tzinfo=UTC),
         "t1": dt.datetime(2026, 7, 14, tzinfo=UTC)},
    ).scalar_one()


def _ids(rows):
    return {r["id"] for r in rows}


def test_history_imo_exact_match(client, db_session):
    v = _add_vessel(db_session, mmsi=636000100, imo=9123456, name="ALPHA")
    other = _add_vessel(db_session, mmsi=636000101, imo=9765432, name="BETA")
    rid = _add_res(db_session, vessel_id=v)
    orid = _add_res(db_session, vessel_id=other)
    rows = client.get("/history", params={"imo": "9123456"}).json()
    assert rid in _ids(rows)
    assert orid not in _ids(rows)


def test_history_imo_partial_substring(client, db_session):
    v = _add_vessel(db_session, mmsi=636000102, imo=9222333, name="GAMMA")
    rid = _add_res(db_session, vessel_id=v)
    # A digit substring of the IMO still finds it.
    rows = client.get("/history", params={"imo": "2223"}).json()
    assert rid in _ids(rows)


def test_history_imo_no_match_excludes(client, db_session):
    v = _add_vessel(db_session, mmsi=636000103, imo=9333444, name="DELTA")
    rid = _add_res(db_session, vessel_id=v)
    rows = client.get("/history", params={"imo": "0000000"}).json()
    assert rid not in _ids(rows)


def test_history_imo_excludes_imo_less_rows(client, db_session):
    # A vessel with only an MMSI (no IMO) must drop out of an IMO search.
    v = _add_vessel(db_session, mmsi=636000104, imo=None, name="EPSILON")
    rid = _add_res(db_session, vessel_id=v)
    rows = client.get("/history", params={"imo": "9"}).json()
    assert rid not in _ids(rows)
    # ...but it's still there with no IMO filter.
    assert rid in _ids(client.get("/history").json())
