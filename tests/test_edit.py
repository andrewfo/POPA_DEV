"""Manual edit surface tests — ship data + scheduling (app/edit.py + endpoints).

Pure range/validation helpers (``_station_range``, ``_time_range``, the
partial-update field selection) always run. The
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
    _station_from_bow,
    _station_range,
    _station_range_for,
    _time_range,
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


def test_station_from_bow_upstream_stern_below():
    # Upstream: bow sits at the high (POPA) end, the stern LOA feet below it.
    sr = _station_from_bow(900.0, "upstream", 600.0)
    assert (sr.empty, sr.lo, sr.hi) == (False, 300.0, 900.0)


def test_station_from_bow_downstream_stern_above():
    # Downstream: bow at the low end, the stern LOA feet above it.
    sr = _station_from_bow(300.0, "downstream", 600.0)
    assert (sr.empty, sr.lo, sr.hi) == (False, 300.0, 900.0)


def test_station_from_bow_requires_direction():
    with pytest.raises(ValueError):
        _station_from_bow(900.0, None, 600.0)
    with pytest.raises(ValueError):
        _station_from_bow(900.0, "", 600.0)


def test_station_from_bow_requires_loa():
    with pytest.raises(ValueError):
        _station_from_bow(900.0, "upstream", 0.0)


def test_time_range_open_ended_ok():
    tr = _time_range(dt.datetime(2026, 7, 1, tzinfo=UTC), None)
    assert tr.upper is None and tr.lower is not None


def test_time_range_etd_before_etb_rejected():
    with pytest.raises(ValueError):
        _time_range(dt.datetime(2026, 7, 5, tzinfo=UTC), dt.datetime(2026, 7, 1, tzinfo=UTC))


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
    # A manual-only vessel (no MMSI) owns its dimensions, so loa overwrites.
    vid = db_session.execute(
        text("INSERT INTO vessel (imo, name) VALUES (9111215, 'MISPELT') RETURNING id")
    ).scalar_one()
    r = client.patch(f"/vessels/{vid}", json={"name": "AGIOS NIKOLAOS", "loa": 183.5})
    assert r.status_code == 200
    row = db_session.execute(
        text("SELECT name, loa FROM vessel WHERE id = :id"), {"id": vid}
    ).one()
    assert row.name == "AGIOS NIKOLAOS"
    assert float(row.loa) == 183.5


def test_patch_vessel_404(client):
    assert client.patch("/vessels/99999999", json={"name": "x"}).status_code == 404


def test_patch_ais_vessel_dimension_is_not_applied(client, db_session):
    # AIS is authoritative for an AIS-tracked (MMSI) vessel's dimensions: a draft
    # edit here is DROPPED, not applied (so it can't corrupt the AIS value until a
    # far-off next ShipStaticData — the ACER ARROW bug), and the response says so.
    vid = db_session.execute(
        text("INSERT INTO vessel (mmsi, name, draft) "
             "VALUES (636000123, 'AIS SHIP', 9.1) RETURNING id")
    ).scalar_one()
    r = client.patch(f"/vessels/{vid}", json={"draft": 6.1})
    assert r.status_code == 200
    warns = " ".join(r.json().get("warnings", []))
    assert "AIS-tracked" in warns and "not applied" in warns
    # The stored draft is unchanged — the AIS value stands.
    draft = db_session.execute(
        text("SELECT draft FROM vessel WHERE id = :id"), {"id": vid}
    ).scalar_one()
    assert float(draft) == 9.1


def test_patch_ais_vessel_dimension_override_applies_and_locks(client, db_session):
    # The escape hatch: when AIS itself is wrong, ticking the override pins the
    # manual dims AND sets dims_locked so the ingestor stops reverting them.
    vid = db_session.execute(
        text("INSERT INTO vessel (mmsi, name, draft) "
             "VALUES (636000223, 'AIS SHIP', 9.1) RETURNING id")
    ).scalar_one()
    r = client.patch(f"/vessels/{vid}", json={"draft": 6.1, "dims_locked": True})
    assert r.status_code == 200
    warns = " ".join(r.json().get("warnings", []))
    assert "override in force" in warns
    row = db_session.execute(
        text("SELECT draft, dims_locked FROM vessel WHERE id = :id"), {"id": vid}
    ).one()
    assert float(row.draft) == 6.1
    assert row.dims_locked is True


def test_manual_override_cancels_ais_override_note(client, db_session):
    # Pinning the entered dims cancels the now-stale [AIS override] note on the
    # vessel's reservations (the entered value governs, AIS no longer "kept").
    vid = db_session.execute(
        text("INSERT INTO vessel (mmsi, name, draft) "
             "VALUES (636000227, 'AIS SHIP', 9.1) RETURNING id")
    ).scalar_one()
    rid = db_session.execute(
        text(
            """
            INSERT INTO reservation
                (vessel_id, type, station_range, time_range, status, source,
                 notes, created_at)
            VALUES (:vid, 'vessel', 'empty'::numrange,
                    tstzrange(now(), now() + interval '1 day', '[)'),
                    'requested', 'phone',
                    '[AIS override] entered draft 20.0→29.9 ft; AIS values kept (authoritative).',
                    now())
            RETURNING id
            """
        ),
        {"vid": vid},
    ).scalar_one()
    r = client.patch(f"/vessels/{vid}", json={"draft": 6.1, "dims_locked": True})
    assert r.status_code == 200
    assert r.json().get("override_cancelled") == 1
    notes = db_session.execute(
        text("SELECT notes FROM reservation WHERE id = :id"), {"id": rid}
    ).scalar_one()
    assert "[AIS override cancelled]" in notes
    assert "[AIS override]" not in notes
    assert "entered value now applied (manual override)." in notes
    assert "AIS values kept (authoritative)." not in notes


def test_override_note_flip_is_idempotent_and_scoped(client, db_session):
    # An unrelated (non-override) note is untouched, and re-running the override
    # doesn't double-mangle an already-cancelled note.
    vid = db_session.execute(
        text("INSERT INTO vessel (mmsi, name, draft, dims_locked) "
             "VALUES (636000228, 'AIS SHIP', 9.1, TRUE) RETURNING id")
    ).scalar_one()
    rid = db_session.execute(
        text(
            """
            INSERT INTO reservation
                (vessel_id, type, station_range, time_range, status, source,
                 notes, created_at)
            VALUES (:vid, 'vessel', 'empty'::numrange,
                    tstzrange(now(), now() + interval '1 day', '[)'),
                    'requested', 'phone', 'plain note, no marker', now())
            RETURNING id
            """
        ),
        {"vid": vid},
    ).scalar_one()
    r = client.patch(f"/vessels/{vid}", json={"draft": 6.0, "dims_locked": True})
    assert r.status_code == 200
    assert r.json().get("override_cancelled") == 0  # nothing to flip
    notes = db_session.execute(
        text("SELECT notes FROM reservation WHERE id = :id"), {"id": rid}
    ).scalar_one()
    assert notes == "plain note, no marker"


def test_patch_ais_vessel_clearing_override_hands_dims_back(client, db_session):
    # Unticking the override drops a fresh dim edit (AIS is authoritative again)
    # and clears the lock so the feed resumes updating.
    vid = db_session.execute(
        text("INSERT INTO vessel (mmsi, name, draft, dims_locked) "
             "VALUES (636000224, 'AIS SHIP', 6.1, TRUE) RETURNING id")
    ).scalar_one()
    r = client.patch(f"/vessels/{vid}", json={"draft": 5.0, "dims_locked": False})
    assert r.status_code == 200
    warns = " ".join(r.json().get("warnings", []))
    assert "AIS-tracked" in warns and "not applied" in warns
    row = db_session.execute(
        text("SELECT draft, dims_locked FROM vessel WHERE id = :id"), {"id": vid}
    ).one()
    assert float(row.draft) == 6.1  # dim edit dropped; prior pinned value stands
    assert row.dims_locked is False


def test_ingestor_does_not_revert_locked_dims(db_session):
    # The other half of the escape hatch: a dims_locked vessel keeps its pinned
    # value even when a fresh ShipStaticData carries a different one — otherwise
    # the override would revert on the next feed message.
    from app.ais.ingest import Ingestor
    from app.ais.messages import AISStatic

    vid = db_session.execute(
        text("INSERT INTO vessel (mmsi, name, draft, dims_locked) "
             "VALUES (636000225, 'PINNED', 6.1, TRUE) RETURNING id")
    ).scalar_one()
    ing = Ingestor(db_session)
    ing._upsert_vessel_static(
        AISStatic(mmsi=636000225, name="PINNED", draft=9.4, loa=200.0)
    )
    row = db_session.execute(
        text("SELECT draft, loa FROM vessel WHERE id = :id"), {"id": vid}
    ).one()
    assert float(row.draft) == 6.1   # locked — feed value ignored
    assert row.loa is None           # locked — feed loa ignored


def test_ingestor_updates_unlocked_dims(db_session):
    # Sanity: without the lock the feed still owns the dimensions (default).
    from app.ais.ingest import Ingestor
    from app.ais.messages import AISStatic

    vid = db_session.execute(
        text("INSERT INTO vessel (mmsi, name, draft) "
             "VALUES (636000226, 'OPEN', 6.1) RETURNING id")
    ).scalar_one()
    ing = Ingestor(db_session)
    ing._upsert_vessel_static(AISStatic(mmsi=636000226, name="OPEN", draft=9.4))
    draft = db_session.execute(
        text("SELECT draft FROM vessel WHERE id = :id"), {"id": vid}
    ).scalar_one()
    assert float(draft) == 9.4       # feed value applied


def test_patch_manual_vessel_dimension_does_not_warn(client, db_session):
    # A manual-only vessel (no MMSI) owns its dimensions — no warning.
    vid = db_session.execute(
        text("INSERT INTO vessel (imo, name) VALUES (9111228, 'MANUAL ONLY') RETURNING id")
    ).scalar_one()
    r = client.patch(f"/vessels/{vid}", json={"draft": 6.1})
    assert r.status_code == 200
    assert not r.json().get("warnings")


def test_patch_ais_vessel_nondimension_edit_does_not_warn(client, db_session):
    # A non-dimension edit (name) on an AIS vessel never warns.
    vid = _add_vessel(db_session, mmsi=636000124, name="TYPO")
    r = client.patch(f"/vessels/{vid}", json={"name": "FIXED"})
    assert r.status_code == 200
    assert not r.json().get("warnings")


def test_create_edit_reservation(client, db_session):
    vid = _add_vessel(db_session, mmsi=636000002)
    # Station bounds are entered in Dock No. (stern first, the larger Dock No.);
    # stored canonically as POPA = 3365 - Dock.
    created = client.post("/reservations", json={
        "vessel_id": vid, "type": "vessel", "status": "tentative",
        "etb": "2026-07-10T00:00:00Z", "etd": "2026-07-12T00:00:00Z",
        "station_lo": 900, "station_hi": 400, "cargo": "steel",
    })
    assert created.status_code == 201
    rid = created.json()["id"]

    got = _get_reservation(client, rid)
    assert got["status"] == "tentative"
    # Dock round-trips; canonical POPA is the reversed transform.
    assert got["station_lo_dock"] == 900 and got["station_hi_dock"] == 400
    assert got["station_lo"] == 2465 and got["station_hi"] == 2965

    # Edit only the ETD + status; station bounds must be preserved. Zone-less
    # input is read as Central (the canonical wall-clock), so it round-trips to
    # the same date — see app/tz.py.
    edited = client.patch(f"/reservations/{rid}", json={
        "etd": "2026-07-15T00:00:00", "status": "requested",
    })
    assert edited.status_code == 200
    got = _get_reservation(client, rid)
    assert got["status"] == "requested"
    assert got["station_lo_dock"] == 900 and got["station_hi_dock"] == 400
    assert got["t_end"].startswith("2026-07-15")


def test_promote_request_from_bow(client, db_session):
    # The promote-from-request path: a requested row already carries its time
    # window; the operator supplies only the bow (Dock No.) + heading and the
    # stern follows from the vessel LOA (metres). 182.88 m ~= 600 ft.
    vid = _add_vessel(db_session, mmsi=636000099, name="LONGSHIP")
    db_session.execute(
        text("UPDATE vessel SET loa = 182.88 WHERE id = :id"), {"id": vid}
    )
    # Zone-less ETB is read as Central (canonical wall-clock — app/tz.py).
    rid = client.post("/reservations", json={
        "vessel_id": vid, "etb": "2026-08-01T00:00:00", "status": "requested",
    }).json()["id"]
    assert _get_reservation(client, rid)["station_unassigned"] is True

    # bow Dock 400 -> bow POPA 2965; upstream -> bow at the high end, stern below.
    r = client.patch(f"/reservations/{rid}", json={
        "bow_dock": 400, "direction": "upstream", "status": "confirmed",
    })
    assert r.status_code == 200
    got = _get_reservation(client, rid)
    assert got["station_unassigned"] is False
    assert got["station_hi"] == pytest.approx(2965.0)          # bow is exact
    assert got["station_hi"] - got["station_lo"] == pytest.approx(600.0, abs=0.1)
    assert got["station_hi_dock"] == pytest.approx(400.0)      # bow Dock round-trips
    # Schedule preserved from the request (no ETB/ETD entered on promotion).
    assert got["t_start"].startswith("2026-08-01")
    assert got["status"] == "confirmed"


def test_promote_from_bow_needs_direction_and_loa(client, db_session):
    # No LOA on the vessel -> 422 (can't derive the stern from the bow).
    vid = _add_vessel(db_session, mmsi=636000098)
    rid = client.post("/reservations", json={
        "vessel_id": vid, "etb": "2026-08-05T00:00:00Z", "status": "requested",
    }).json()["id"]
    assert client.patch(
        f"/reservations/{rid}", json={"bow_dock": 400, "direction": "upstream"}
    ).status_code == 422

    # LOA present but no heading -> 422 (an un-oriented hull can't be placed).
    db_session.execute(
        text("UPDATE vessel SET loa = 100 WHERE id = :id"), {"id": vid}
    )
    assert client.patch(
        f"/reservations/{rid}", json={"bow_dock": 400}
    ).status_code == 422


def test_assign_then_unassign_station(client, db_session):
    rid = client.post("/reservations", json={
        "etb": "2026-08-01T00:00:00Z", "status": "requested",
    }).json()["id"]
    assert _get_reservation(client, rid)["station_unassigned"] is True

    # Dock No. bounds (stern 300 > bow 100); assigning them fills the range.
    client.patch(f"/reservations/{rid}", json={"station_lo": 300, "station_hi": 100})
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
    # Explicit Dock No. bounds (stern 2200 > bow 1500) override the berth's full
    # range (a shorter vessel). Stored as POPA = 3365 - Dock -> [1165, 1865].
    rid = client.post("/reservations", json={
        "etb": "2026-08-20T00:00:00Z", "status": "tentative",
        "berth_id": bid, "station_lo": 2200, "station_hi": 1500,
    }).json()["id"]
    got = _get_reservation(client, rid)
    assert got["station_lo"] == 1165 and got["station_hi"] == 1865
    assert got["station_lo_dock"] == 2200 and got["station_hi_dock"] == 1500
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


def test_cancel_clears_placement(client, db_session):
    bid = _add_berth(db_session, name="Test Berth Cancel", lo=351.0, hi=1107.3)
    rid = client.post("/reservations", json={
        "etb": "2026-09-05T00:00:00Z", "status": "tentative", "berth_id": bid,
    }).json()["id"]
    placed = _get_reservation(client, rid)
    assert placed["station_unassigned"] is False and placed["berth_id"] == bid

    # Cancelling frees the berth and the station range (the visit is no longer
    # alongside); the time window is kept as the historical record.
    client.patch(f"/reservations/{rid}", json={"status": "cancelled"})
    got = _get_reservation(client, rid)
    assert got["status"] == "cancelled"
    assert got["station_unassigned"] is True and got["berth_id"] is None
    assert got["t_start"] is not None


def test_cancel_with_concurrent_berth_assignment_still_unplaces(client, db_session):
    bid = _add_berth(db_session, name="Test Berth Cancel2", lo=351.0, hi=1107.3)
    rid = client.post("/reservations", json={
        "etb": "2026-09-06T00:00:00Z", "status": "tentative",
    }).json()["id"]

    # A single edit that both assigns a berth and cancels still ends unplaced.
    client.patch(f"/reservations/{rid}", json={"berth_id": bid, "status": "cancelled"})
    got = _get_reservation(client, rid)
    assert got["station_unassigned"] is True and got["berth_id"] is None


def test_reset_to_requested_clears_placement_but_keeps_row(client, db_session):
    # The "Cancel" button reverts a placed row to a blank pending request: it
    # sends status=requested + unassigned=true, which frees the berth and empties
    # the station range while keeping the row (status 'requested', so it stays in
    # the active list and can be re-placed) and its arrival/departure window.
    bid = _add_berth(db_session, name="Test Berth Reset", lo=351.0, hi=1107.3)
    rid = client.post("/reservations", json={
        "etb": "2026-09-10T00:00:00Z", "etd": "2026-09-12T00:00:00Z",
        "status": "tentative", "berth_id": bid,
    }).json()["id"]
    placed = _get_reservation(client, rid)
    assert placed["station_unassigned"] is False and placed["berth_id"] == bid

    r = client.patch(f"/reservations/{rid}", json={
        "status": "requested", "unassigned": True,
    })
    assert r.status_code == 200
    got = _get_reservation(client, rid)
    assert got["status"] == "requested"
    assert got["station_unassigned"] is True and got["berth_id"] is None
    assert got["t_start"] is not None and got["t_end"] is not None


def test_cancel_ignores_resent_bow_placement_without_loa(client, db_session):
    # Regression: the "Edit placement" form re-sends the row's bow_dock + heading.
    # On a vessel with no LOA, bow placement can't resolve a stern — it must NOT
    # 422 and block the cancel. status=cancelled short-circuits placement, so the
    # stale bow_dock/direction are ignored and the row ends unplaced.
    vid = _add_vessel(db_session, mmsi=636000077, name="NO LOA")  # loa is NULL
    rid = client.post("/reservations", json={
        "etb": "2026-09-07T00:00:00Z", "status": "tentative", "vessel_id": vid,
    }).json()["id"]

    r = client.patch(f"/reservations/{rid}", json={
        "status": "cancelled", "bow_dock": 1500.0, "direction": "upstream",
    })
    assert r.status_code == 200
    got = _get_reservation(client, rid)
    assert got["status"] == "cancelled"
    assert got["station_unassigned"] is True and got["berth_id"] is None


def test_confirm_returns_depth_warning(client):
    created = client.post("/reservations", json={
        "etb": "2026-10-01T00:00:00Z", "etd": "2026-10-03T00:00:00Z",
        "station_lo": 1900, "station_hi": 1500, "status": "confirmed",
    })
    assert created.status_code == 201
    # No vessel/draft and no depth survey -> the gate can't evaluate, so it warns
    # (rather than blocks). The blocking path is covered in tests/test_depth.py.
    assert created.json()["warnings"]


def test_confirmed_overlap_rejected_409(client):
    # Dock No. bounds; POPA = 3365 - Dock. base -> POPA [400, 900].
    base = {
        "etb": "2026-11-01T00:00:00Z", "etd": "2026-11-05T00:00:00Z",
        "station_lo": 2965, "station_hi": 2465, "status": "confirmed",
    }
    first = client.post("/reservations", json=base)
    assert first.status_code == 201

    # Overlapping in BOTH time and station (POPA [600, 1000]), also confirmed
    # -> exclusion constraint.
    overlap = client.post("/reservations", json={
        "etb": "2026-11-03T00:00:00Z", "etd": "2026-11-08T00:00:00Z",
        "station_lo": 2765, "station_hi": 2365, "status": "confirmed",
    })
    assert overlap.status_code == 409

    # Non-overlapping station (POPA [1000, 1400]) at the same time is fine.
    ok = client.post("/reservations", json={
        "etb": "2026-11-03T00:00:00Z", "etd": "2026-11-08T00:00:00Z",
        "station_lo": 2365, "station_hi": 1965, "status": "confirmed",
    })
    assert ok.status_code == 201


def test_confirmed_within_min_gap_rejected_409(client):
    """Two confirmed vessels that don't overlap but sit closer than the 75 ft
    mooring gap must still collide (migration 0007 buffers the station range)."""
    # POPA [400, 900] confirmed (Dock No. = 3365 - POPA).
    first = client.post("/reservations", json={
        "etb": "2026-12-01T00:00:00Z", "etd": "2026-12-05T00:00:00Z",
        "station_lo": 2965, "station_hi": 2465, "status": "confirmed",
    })
    assert first.status_code == 201

    # POPA [950, 1200]: a real gap of only 50 ft above the first hull -> conflict.
    too_close = client.post("/reservations", json={
        "etb": "2026-12-02T00:00:00Z", "etd": "2026-12-06T00:00:00Z",
        "station_lo": 2415, "station_hi": 2165, "status": "confirmed",
    })
    assert too_close.status_code == 409

    # POPA [980, 1200]: an 80 ft gap clears the 75 ft minimum -> allowed.
    clear = client.post("/reservations", json={
        "etb": "2026-12-02T00:00:00Z", "etd": "2026-12-06T00:00:00Z",
        "station_lo": 2385, "station_hi": 2165, "status": "confirmed",
    })
    assert clear.status_code == 201


# --- PATCH /intake/berth-requests/{id} (edit a berth request in place) ------
def test_edit_berth_request_endpoint(client):
    created = client.post("/intake/berth-request", json={
        "source": "phone", "vessel": "EDIT ME", "imo": 9600009,
        "etb": "2027-01-10T00:00:00Z", "etd": "2027-01-12T00:00:00Z",
        "inbound_cargo": "coal",
    }).json()
    intake_id, rid = created["intake_event_id"], created["reservation_id"]

    r = client.patch(f"/intake/berth-requests/{intake_id}", json={
        "source": "phone", "vessel": "EDIT ME", "imo": 9600009,
        "etb": "2027-01-10T00:00:00Z", "etd": "2027-01-12T00:00:00Z",
        "inbound_cargo": "petcoke",
    })
    assert r.status_code == 200
    assert r.json()["reservation_id"] == rid  # same reservation, re-projected
    assert _get_reservation(client, rid)["cargo"] == "IN: petcoke"


def test_edit_berth_request_missing_404(client):
    assert client.patch(
        "/intake/berth-requests/99999999", json={"vessel": "X", "imo": 9600009}
    ).status_code == 404
