"""Manual edit surface tests — ship data + scheduling (app/edit.py + endpoints).

Pure range/validation helpers (``_station_range``, ``_time_range``,
``confirm_warnings``, the partial-update field selection) always run. The
endpoint behaviour — vessel patch, reservation create/edit/cancel/delete, and
the confirmed-overlap 409 — is DB-marked and auto-skips without a migrated
PostGIS (via the ``db_session`` fixture), using the same rolled-back-transaction
TestClient pattern as ``tests/test_reservations_window.py``.
"""
from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import get_session
from app.edit import (
    ReservationUpdate,
    VesselUpdate,
    _station_range,
    _station_range_for,
    _time_range,
    confirm_warnings,
)
from app.main import app

UTC = dt.timezone.utc


# --- pure helpers (always run) ---------------------------------------------
def test_station_range_valid():
    sr = _station_range(400.0, 900.0, unassigned=False)
    assert (sr.empty, sr.lo, sr.hi) == (False, 400.0, 900.0)


def test_station_range_unassigned_and_empty():
    assert _station_range(400, 900, unassigned=True).empty
    assert _station_range(None, None, unassigned=False).empty


def test_station_range_inverted_rejected():
    with pytest.raises(ValueError):
        _station_range(900, 400, unassigned=False)
    with pytest.raises(ValueError):
        _station_range(500, 500, unassigned=False)  # zero width


def test_station_range_single_bound_rejected():
    with pytest.raises(ValueError):
        _station_range(400, None, unassigned=False)


def test_station_range_for_uses_berth_when_no_bounds():
    # Assigning a berth with no explicit bounds -> the berth's full range.
    sr = _station_range_for((351.0, 1107.3), None, None, unassigned=False)
    assert (sr.empty, sr.lo, sr.hi) == (False, 351.0, 1107.3)


def test_station_range_for_explicit_bounds_override_berth():
    # A vessel shorter than the berth occupies a sub-span: explicit wins.
    sr = _station_range_for((351.0, 1107.3), 400.0, 900.0, unassigned=False)
    assert (sr.lo, sr.hi) == (400.0, 900.0)


def test_station_range_for_unassigned_and_no_berth():
    assert _station_range_for((351.0, 1107.3), None, None, unassigned=True).empty
    # No berth, no bounds -> empty (unassigned), same as the plain resolver.
    assert _station_range_for(None, None, None, unassigned=False).empty


def test_time_range_open_ended_ok():
    tr = _time_range(dt.datetime(2026, 7, 1, tzinfo=UTC), None)
    assert tr.upper is None and tr.lower is not None


def test_time_range_etd_before_etb_rejected():
    with pytest.raises(ValueError):
        _time_range(dt.datetime(2026, 7, 5, tzinfo=UTC), dt.datetime(2026, 7, 1, tzinfo=UTC))


def test_confirm_warnings_only_on_confirmed():
    assert confirm_warnings("confirmed")  # non-empty (depth gate)
    assert confirm_warnings("tentative") == []
    assert confirm_warnings(None) == []


def test_vessel_update_selects_only_provided_fields():
    upd = VesselUpdate(name="CORRECTED NAME")
    changes = upd.model_dump(exclude_unset=True)
    assert changes == {"name": "CORRECTED NAME"}  # loa/draft/etc. untouched


def test_reservation_update_unset_vs_explicit_null():
    # Explicitly clearing the ETD (open-ended) is distinct from not touching it.
    upd = ReservationUpdate(etd=None)
    assert "etd" in upd.model_dump(exclude_unset=True)
    assert "etb" not in ReservationUpdate().model_dump(exclude_unset=True)


# --- DB-marked endpoint tests (auto-skip without PostGIS) ------------------
@pytest.fixture
def client(db_session):
    app.dependency_overrides[get_session] = lambda: db_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_session, None)


def _add_vessel(session, *, mmsi=636000001, name="OLD NAME"):
    return session.execute(
        text("INSERT INTO vessel (mmsi, name) VALUES (:m, :n) RETURNING id"),
        {"m": mmsi, "n": name},
    ).scalar_one()


def _get_reservation(client, res_id):
    rows = client.get("/reservations", params={"limit": 500}).json()
    return next((r for r in rows if r["id"] == res_id), None)


def test_patch_vessel_overwrites(client, db_session):
    vid = _add_vessel(db_session, name="MISPELT")
    r = client.patch(f"/vessels/{vid}", json={"name": "AGIOS NIKOLAOS", "loa": 183.5})
    assert r.status_code == 200
    row = db_session.execute(
        text("SELECT name, loa FROM vessel WHERE id = :id"), {"id": vid}
    ).one()
    assert row.name == "AGIOS NIKOLAOS"
    assert float(row.loa) == 183.5


def test_patch_vessel_404(client):
    assert client.patch("/vessels/99999999", json={"name": "x"}).status_code == 404


def test_create_edit_reservation(client, db_session):
    vid = _add_vessel(db_session, mmsi=636000002)
    created = client.post("/reservations", json={
        "vessel_id": vid, "type": "vessel", "status": "tentative",
        "etb": "2026-07-10T00:00:00Z", "etd": "2026-07-12T00:00:00Z",
        "station_lo": 400, "station_hi": 900, "cargo": "steel",
    })
    assert created.status_code == 201
    rid = created.json()["id"]

    got = _get_reservation(client, rid)
    assert got["status"] == "tentative"
    assert got["station_lo"] == 400 and got["station_hi"] == 900

    # Edit only the ETD + status; station bounds must be preserved.
    edited = client.patch(f"/reservations/{rid}", json={
        "etd": "2026-07-15T00:00:00Z", "status": "requested",
    })
    assert edited.status_code == 200
    got = _get_reservation(client, rid)
    assert got["status"] == "requested"
    assert got["station_lo"] == 400 and got["station_hi"] == 900
    assert got["t_end"].startswith("2026-07-15")


def test_assign_then_unassign_station(client, db_session):
    rid = client.post("/reservations", json={
        "etb": "2026-08-01T00:00:00Z", "status": "requested",
    }).json()["id"]
    assert _get_reservation(client, rid)["station_unassigned"] is True

    client.patch(f"/reservations/{rid}", json={"station_lo": 100, "station_hi": 300})
    assert _get_reservation(client, rid)["station_unassigned"] is False

    client.patch(f"/reservations/{rid}", json={"unassigned": True})
    assert _get_reservation(client, rid)["station_unassigned"] is True


# Test-only berth names so these don't collide with a seeded catalog (the
# 'berth' unique name constraint) when the suite runs against a populated DB.
def _add_berth(session, *, name="Test Berth A", lo=351.0, hi=1107.3):
    return session.execute(
        text(
            "INSERT INTO berth (name, popa_sta_start, popa_sta_end) "
            "VALUES (:n, :lo, :hi) RETURNING id"
        ),
        {"n": name, "lo": lo, "hi": hi},
    ).scalar_one()


def test_assign_berth_fills_station_range(client, db_session):
    bid = _add_berth(db_session)
    # A requested berth-request lands with an unassigned (empty) station range.
    rid = client.post("/reservations", json={
        "etb": "2026-08-10T00:00:00Z", "status": "requested",
    }).json()["id"]
    assert _get_reservation(client, rid)["station_unassigned"] is True

    # Operator assigns the berth -> station_range is filled from the catalog.
    r = client.patch(f"/reservations/{rid}", json={"berth_id": bid})
    assert r.status_code == 200
    got = _get_reservation(client, rid)
    assert got["station_unassigned"] is False
    assert got["station_lo"] == 351.0 and got["station_hi"] == 1107.3
    assert got["berth_id"] == bid and got["berth_name"] == "Test Berth A"

    # Unassigning clears both the berth and the range.
    client.patch(f"/reservations/{rid}", json={"unassigned": True})
    got = _get_reservation(client, rid)
    assert got["station_unassigned"] is True and got["berth_id"] is None


def test_create_with_berth_and_subspan(client, db_session):
    bid = _add_berth(db_session, name="Test Berth B", lo=1107.3, hi=1966.5)
    # Explicit bounds override the berth's full range (a shorter vessel).
    rid = client.post("/reservations", json={
        "etb": "2026-08-20T00:00:00Z", "status": "tentative",
        "berth_id": bid, "station_lo": 1200, "station_hi": 1500,
    }).json()["id"]
    got = _get_reservation(client, rid)
    assert got["station_lo"] == 1200 and got["station_hi"] == 1500
    assert got["berth_id"] == bid


def test_assign_unknown_berth_422(client):
    rid = client.post("/reservations", json={
        "etb": "2026-08-25T00:00:00Z", "status": "requested",
    }).json()["id"]
    assert client.patch(
        f"/reservations/{rid}", json={"berth_id": 99999999}
    ).status_code == 422


def test_list_berths_ordered(client, db_session):
    _add_berth(db_session, name="Test Berth North", lo=2707.9, hi=3451.1)
    _add_berth(db_session, name="Test Berth South", lo=-411.4, hi=351.0)
    names = [b["name"] for b in client.get("/berths").json()]
    # Ordered SW -> NE by station: the southern (smaller POPA) berth precedes
    # the northern one, regardless of any other seeded berths in between.
    assert names.index("Test Berth South") < names.index("Test Berth North")


def test_cancel_and_delete(client):
    rid = client.post("/reservations", json={
        "etb": "2026-09-01T00:00:00Z", "status": "tentative",
    }).json()["id"]

    client.patch(f"/reservations/{rid}", json={"status": "cancelled"})
    assert _get_reservation(client, rid)["status"] == "cancelled"

    assert client.delete(f"/reservations/{rid}").status_code == 204
    assert _get_reservation(client, rid) is None
    assert client.delete(f"/reservations/{rid}").status_code == 404


def test_confirm_returns_depth_warning(client):
    created = client.post("/reservations", json={
        "etb": "2026-10-01T00:00:00Z", "etd": "2026-10-03T00:00:00Z",
        "station_lo": 1500, "station_hi": 1900, "status": "confirmed",
    })
    assert created.status_code == 201
    assert created.json()["warnings"]  # depth-gate warning surfaced


def test_confirmed_overlap_rejected_409(client):
    base = {
        "etb": "2026-11-01T00:00:00Z", "etd": "2026-11-05T00:00:00Z",
        "station_lo": 400, "station_hi": 900, "status": "confirmed",
    }
    first = client.post("/reservations", json=base)
    assert first.status_code == 201

    # Overlapping in BOTH time and station, also confirmed -> exclusion constraint.
    overlap = client.post("/reservations", json={
        "etb": "2026-11-03T00:00:00Z", "etd": "2026-11-08T00:00:00Z",
        "station_lo": 600, "station_hi": 1000, "status": "confirmed",
    })
    assert overlap.status_code == 409

    # Non-overlapping station at the same time is fine.
    ok = client.post("/reservations", json={
        "etb": "2026-11-03T00:00:00Z", "etd": "2026-11-08T00:00:00Z",
        "station_lo": 1000, "station_hi": 1400, "status": "confirmed",
    })
    assert ok.status_code == 201
