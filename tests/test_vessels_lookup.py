"""DB-marked tests for GET /vessels/lookup — the IMO auto-fill lookup.

Exercises the endpoint through TestClient against a live PostGIS; auto-skips (via
the ``db_session`` fixture) when no migrated DB is around. This endpoint only reads,
so seeding a ``vessel`` row through the session is fine.

Note: the endpoint proposes, it never places — it only reads vessel particulars for
the intake form to pre-fill. The server-side AIS-dims drop on submit (an AIS-tracked
vessel's typed dims are dropped/NULL-filled) is covered by the intake tests; auto-fill
adds no new bypass, it just surfaces ``ais_tracked`` so the form shows dims read-only.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import get_session
from app.main import app

# Structurally valid IMOs (7 digits + correct check digit); see app.intake.valid_imo.
IMO_AIS = 9074729       # seeded as an AIS-tracked vessel (has MMSI)
IMO_MANUAL = 8814275    # seeded manual-only (no MMSI)
IMO_UNKNOWN = 9319466   # valid but never inserted -> a clean miss
IMO_INVALID = 1234560   # bad check digit -> 422


@pytest.fixture
def client(db_session):
    app.dependency_overrides[get_session] = lambda: db_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_session, None)


def _add_vessel(session, *, imo, mmsi=None, name="LOOKUPSHIP", loa=None, beam=None, draft=None):
    return session.execute(
        text(
            """
            INSERT INTO vessel (mmsi, imo, name, loa, beam, draft)
            VALUES (:m, :i, :n, :loa, :beam, :draft)
            RETURNING id
            """
        ),
        {"m": mmsi, "i": imo, "n": name, "loa": loa, "beam": beam, "draft": draft},
    ).scalar_one()


@pytest.mark.db
def test_lookup_onfile_ais_tracked(db_session, client):
    # 100 m x 16 m x 6 m -> feet via FEET_PER_M (3.280839895).
    _add_vessel(db_session, imo=IMO_AIS, mmsi=366123456, name="AIS ONE",
                loa=100, beam=16, draft=6)
    r = client.get(f"/vessels/lookup?imo={IMO_AIS}")
    assert r.status_code == 200
    d = r.json()
    assert d["found"] is True
    assert d["source"] == "onfile"
    assert d["ais_tracked"] is True
    assert d["dims_locked"] is False
    assert d["name"] == "AIS ONE"
    assert d["loa_ft"] == pytest.approx(328.1, abs=0.1)
    assert d["beam_ft"] == pytest.approx(52.5, abs=0.1)
    assert d["draft_ft"] == pytest.approx(19.7, abs=0.1)


@pytest.mark.db
def test_lookup_onfile_manual_only(db_session, client):
    _add_vessel(db_session, imo=IMO_MANUAL, mmsi=None, name="MANUAL ONE", loa=80)
    r = client.get(f"/vessels/lookup?imo={IMO_MANUAL}")
    assert r.status_code == 200
    d = r.json()
    assert d["found"] is True
    assert d["source"] == "onfile"
    assert d["ais_tracked"] is False          # no MMSI -> operator owns the dims
    assert d["loa_ft"] == pytest.approx(262.5, abs=0.1)


@pytest.mark.db
def test_lookup_not_found_is_200(db_session, client):
    # A valid but unknown IMO is a normal miss, not a 404 — the form prompts for
    # manual entry (and one day falls through to a Tier-2 external provider).
    r = client.get(f"/vessels/lookup?imo={IMO_UNKNOWN}")
    assert r.status_code == 200
    d = r.json()
    assert d["found"] is False
    assert d["source"] == "none"


@pytest.mark.db
def test_lookup_invalid_imo_422(db_session, client):
    r = client.get(f"/vessels/lookup?imo={IMO_INVALID}")
    assert r.status_code == 422


@pytest.mark.db
def test_lookup_most_recent_wins(db_session, client):
    # IMO is indexed but not unique; the most-recently-updated row answers.
    _add_vessel(db_session, imo=IMO_AIS, mmsi=None, name="OLD NAME", loa=50)
    _add_vessel(db_session, imo=IMO_AIS, mmsi=366999999, name="NEW NAME", loa=120)
    r = client.get(f"/vessels/lookup?imo={IMO_AIS}")
    d = r.json()
    assert d["found"] is True
    assert d["name"] == "NEW NAME"
    assert d["ais_tracked"] is True
