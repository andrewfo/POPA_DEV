"""DB-marked tests for GET /vessels/search — the intake form's vessel-name type-ahead.

Read-only endpoint, so seeding ``vessel`` / ``position_report`` rows through the
session is fine (same pattern as test_vessels_lookup.py). Names carry a nonsense
token (``ZQXQ``) so rows already in the dev DB never match.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import get_session
from app.main import app

IMO_A = 9074729
IMO_B = 8814275
IMO_C = 9319466


@pytest.fixture
def client(db_session):
    app.dependency_overrides[get_session] = lambda: db_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_session, None)


def _add_vessel(session, *, imo, name, mmsi=None, loa=None, beam=None):
    return session.execute(
        text(
            "INSERT INTO vessel (mmsi, imo, name, loa, beam) "
            "VALUES (:m, :i, :n, :loa, :beam) RETURNING id"
        ),
        {"m": mmsi, "i": imo, "n": name, "loa": loa, "beam": beam},
    ).scalar_one()


def _add_fix(session, *, mmsi, ts):
    session.execute(
        text(
            """
            INSERT INTO position_report (mmsi, lat, lon, geom, msg_ts, raw, source)
            VALUES (:m, 29.87, -93.93, ST_SetSRID(ST_MakePoint(-93.93, 29.87), 4326),
                    :ts, '{}'::jsonb, 'ais')
            """
        ),
        {"m": mmsi, "ts": ts},
    )


def _search(client, q, **params):
    r = client.get("/vessels/search", params={"q": q, **params})
    assert r.status_code == 200
    return r.json()["results"]


@pytest.mark.db
def test_substring_case_insensitive_dims_in_feet(db_session, client):
    _add_vessel(db_session, imo=IMO_A, name="CHEM ZQXQ ORCHARD", loa=100, beam=16)
    res = _search(client, "zqxq orch")
    assert [r["imo"] for r in res] == [IMO_A]
    assert res[0]["name"] == "CHEM ZQXQ ORCHARD"
    assert res[0]["loa_ft"] == pytest.approx(328.1, abs=0.1)
    assert res[0]["beam_ft"] == pytest.approx(52.5, abs=0.1)


@pytest.mark.db
def test_prefix_ranks_above_substring(db_session, client):
    _add_vessel(db_session, imo=IMO_A, name="THE ZQXQ BETA")
    _add_vessel(db_session, imo=IMO_B, name="ZQXQ ALPHA")
    res = _search(client, "zqxq")
    assert [r["imo"] for r in res] == [IMO_B, IMO_A]


@pytest.mark.db
def test_one_row_per_imo_most_recent_wins(db_session, client):
    _add_vessel(db_session, imo=IMO_A, name="ZQXQ OLD NAME", loa=50)
    _add_vessel(db_session, imo=IMO_A, name="ZQXQ NEW NAME", mmsi=366999991, loa=120)
    res = _search(client, "zqxq")
    assert len(res) == 1
    assert res[0]["name"] == "ZQXQ NEW NAME"
    assert res[0]["ais_tracked"] is True


@pytest.mark.db
def test_rows_without_valid_imo_excluded(db_session, client):
    _add_vessel(db_session, imo=None, mmsi=366999992, name="ZQXQ NO IMO")
    _add_vessel(db_session, imo=101212306, name="ZQXQ NINE DIGITS")  # AIS garbage
    _add_vessel(db_session, imo=1234560, name="ZQXQ BAD CHECK")      # bad check digit
    assert _search(client, "zqxq") == []


@pytest.mark.db
def test_digits_match_imo_prefix(db_session, client):
    _add_vessel(db_session, imo=IMO_C, name="ZQXQ DIGITS")
    res = _search(client, str(IMO_C)[:5])
    assert IMO_C in [r["imo"] for r in res]


@pytest.mark.db
def test_short_query_empty_and_limit_caps(db_session, client):
    for i, imo in enumerate((IMO_A, IMO_B, IMO_C)):
        _add_vessel(db_session, imo=imo, name=f"ZQXQ SHIP {i}")
    assert _search(client, "z") == []
    assert _search(client, "  ") == []
    assert len(_search(client, "zqxq", limit=2)) == 2
    assert client.get("/vessels/search", params={"q": "zqxq", "limit": 0}).status_code == 422


@pytest.mark.db
def test_like_metacharacters_are_literal(db_session, client):
    _add_vessel(db_session, imo=IMO_A, name="ZQXQ 100% PURE")
    _add_vessel(db_session, imo=IMO_B, name="ZQXQ 100X PURE")
    assert [r["imo"] for r in _search(client, "zqxq 100%")] == [IMO_A]
    assert [r["imo"] for r in _search(client, "zqxq 100_")] == []


@pytest.mark.db
def test_last_seen_from_ais_fix(db_session, client):
    _add_vessel(db_session, imo=IMO_A, name="ZQXQ AIS SHIP", mmsi=366999993)
    _add_vessel(db_session, imo=IMO_B, name="ZQXQ MANUAL SHIP")
    _add_fix(db_session, mmsi=366999993, ts="2026-10-01T12:00:00+00:00")
    _add_fix(db_session, mmsi=366999993, ts="2026-10-03T12:00:00+00:00")
    by_imo = {r["imo"]: r for r in _search(client, "zqxq")}
    assert by_imo[IMO_A]["last_seen"].startswith("2026-10-03")  # Central: still Oct 3
    assert by_imo[IMO_B]["last_seen"] is None
    # Recently-seen AIS ship ranks ahead of the never-seen manual one (both prefix).
    assert [r["imo"] for r in _search(client, "zqxq")] == [IMO_A, IMO_B]
