"""Manual berth-request entry tests.

``normalize_form`` is pure (units, cargo, notes, warnings) and always runs.
``record_manual_request`` and the endpoint are DB-marked and auto-skip without
a migrated PostGIS (via the ``db_session`` fixture).
"""
from __future__ import annotations

import datetime as dt

from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

import pytest

from app.db import get_session
from app.intake.manual import (
    FEET_PER_M,
    BerthRequestForm,
    delete_manual_request,
    normalize_form,
    record_manual_request,
    update_manual_request,
)
from app.main import app
from app.models import IntakeEvent, Reservation, Vessel
from app.tz import CENTRAL


def _form(**over) -> BerthRequestForm:
    base = dict(
        source="phone",
        vessel="SAGA ADVENTURE",
        imo=9317406,
        length_ft=585.0,
        beam_ft=91.5,
        draft_ft=31.1,
        etb=dt.date(2026, 6, 16),
        etd=dt.date(2026, 6, 19),
        inbound_cargo="steel coils",
        inbound_tons=12000.0,
        agency="ISS BEAUMONT",
    )
    base.update(over)
    return BerthRequestForm(**base)


# --- pure normalization ----------------------------------------------------
def test_feet_to_metres_conversion():
    req = normalize_form(_form())
    assert req.loa_m == round(585.0 / FEET_PER_M, 2)
    assert req.beam_m == round(91.5 / FEET_PER_M, 2)
    assert req.draft_m == round(31.1 / FEET_PER_M, 2)


def test_dates_become_midnight_central_timestamps():
    req = normalize_form(_form())
    assert req.etb == dt.datetime(2026, 6, 16, tzinfo=CENTRAL)
    assert req.etd == dt.datetime(2026, 6, 19, tzinfo=CENTRAL)


def test_etb_etd_keep_time_of_day():
    # The form carries an arrival/departure time of day; a naive datetime is
    # read as Central Time (datetime-local inputs send no zone).
    req = normalize_form(
        _form(
            etb=dt.datetime(2026, 6, 16, 14, 30),
            etd=dt.datetime(2026, 6, 19, 6, 0),
        )
    )
    assert req.etb == dt.datetime(2026, 6, 16, 14, 30, tzinfo=CENTRAL)
    assert req.etd == dt.datetime(2026, 6, 19, 6, 0, tzinfo=CENTRAL)


def test_etb_aware_input_keeps_its_instant():
    # An explicitly-zoned value (e.g. 'Z') is not re-stamped — its instant is
    # preserved, equal to the same moment expressed in Central.
    req = normalize_form(_form(etb="2026-06-16T14:30:00Z"))
    assert req.etb == dt.datetime(2026, 6, 16, 14, 30, tzinfo=dt.timezone.utc)
    assert req.etb.utcoffset() == dt.timedelta(0)  # kept UTC, not shifted


def test_cargo_summary_combines_in_and_out():
    req = normalize_form(_form(outbound_cargo="bagged rice", outbound_tons=8000))
    assert "IN: steel coils (12000 t)" in req.cargo
    assert "OUT: bagged rice (8000 t)" in req.cargo


def test_no_cargo_is_none():
    req = normalize_form(_form(inbound_cargo=None, inbound_tons=None))
    assert req.cargo is None


def test_raw_preserves_feet_and_extra_fields():
    req = normalize_form(_form(flag="PANAMA", deadweight_lbs=45000))
    assert req.raw["length_ft"] == 585.0          # verbatim feet, not metres
    assert req.raw["flag"] == "PANAMA"
    assert req.raw["deadweight_lbs"] == 45000


def test_missing_imo_and_etb_warn():
    req = normalize_form(_form(imo=None, etb=None))
    assert any("IMO" in w for w in req.warnings)
    assert any("arrival date" in w for w in req.warnings)


def test_unknown_source_coerced_with_warning():
    req = normalize_form(_form(source="carrier-pigeon"))
    assert req.source == "phone"
    assert any("unknown source" in w for w in req.warnings)


def test_notes_flag_unassigned_berth_and_bunkers():
    req = normalize_form(_form(bunkers=True))
    assert "berth UNASSIGNED" in req.notes
    assert "bunkers: yes" in req.notes


# --- DB: end-to-end landing ------------------------------------------------
def _counts(session):
    return (
        session.execute(select(func.count()).select_from(IntakeEvent)).scalar_one(),
        session.execute(select(func.count()).select_from(Reservation)).scalar_one(),
    )


def test_records_intake_reservation_and_vessel(db_session):
    ev0, res0 = _counts(db_session)
    out = record_manual_request(db_session, _form(imo=9111222))

    assert out["duplicate"] is False
    assert out["reservation_id"] is not None
    assert out["vessel_id"] is not None
    ev1, res1 = _counts(db_session)
    assert (ev1, res1) == (ev0 + 1, res0 + 1)

    res = db_session.execute(
        select(Reservation).where(Reservation.id == out["reservation_id"])
    ).scalar_one()
    assert res.status == "requested"
    assert res.source == "phone"
    assert res.type == "vessel"
    # Station range is empty (unassigned berth) so it can never false-conflict.
    assert db_session.execute(
        text("SELECT isempty(station_range) FROM reservation WHERE id = :i"),
        {"i": res.id},
    ).scalar_one() is True

    # Vessel was created keyed on IMO, dimensions stored in metres.
    v = db_session.execute(
        select(Vessel).where(Vessel.imo == 9111222)
    ).scalar_one()
    assert v.name == "SAGA ADVENTURE"
    assert float(v.loa) == round(585.0 / FEET_PER_M, 2)

    # Intake event is linked + processed, raw kept verbatim.
    ev = db_session.execute(
        select(IntakeEvent).where(IntakeEvent.id == out["intake_event_id"])
    ).scalar_one()
    assert ev.processed is True
    assert ev.reservation_id == res.id
    assert ev.source == "phone"
    assert ev.raw["length_ft"] == 585.0


def test_duplicate_submission_is_idempotent(db_session):
    f = _form(imo=9333444)
    first = record_manual_request(db_session, f)
    ev1, res1 = _counts(db_session)

    second = record_manual_request(db_session, _form(imo=9333444))
    ev2, res2 = _counts(db_session)

    assert second["duplicate"] is True
    assert second["reservation_id"] == first["reservation_id"]
    assert (ev2, res2) == (ev1, res1)  # nothing new landed


def test_missing_etb_lands_intake_without_reservation(db_session):
    ev0, res0 = _counts(db_session)
    out = record_manual_request(db_session, _form(imo=9555666, etb=None))

    assert out["duplicate"] is False
    assert out["reservation_id"] is None
    ev1, res1 = _counts(db_session)
    assert (ev1, res1) == (ev0 + 1, res0)  # intake landed, no reservation

    ev = db_session.execute(
        select(IntakeEvent).where(IntakeEvent.id == out["intake_event_id"])
    ).scalar_one()
    assert ev.processed is False  # left for human follow-up


# --- DB: in-place edit of a manual request ---------------------------------
def test_edit_overwrites_raw_and_reprojects_reservation(db_session):
    out = record_manual_request(db_session, _form(imo=9777888, draft_ft=31.1))
    rid = out["reservation_id"]
    ev0, _ = _counts(db_session)

    edited = update_manual_request(
        db_session,
        out["intake_event_id"],
        _form(imo=9777888, draft_ft=33.0, inbound_cargo="grain"),
    )
    # Same rows reused — an edit, not a new request (no extra intake_event).
    assert edited["intake_event_id"] == out["intake_event_id"]
    assert edited["reservation_id"] == rid
    assert _counts(db_session)[0] == ev0

    ev = db_session.execute(
        select(IntakeEvent).where(IntakeEvent.id == out["intake_event_id"])
    ).scalar_one()
    assert ev.raw["draft_ft"] == 33.0  # raw mutated in place
    res = db_session.execute(
        select(Reservation).where(Reservation.id == rid)
    ).scalar_one()
    assert res.cargo == "IN: grain (12000 t)"  # reservation re-projected
    # An explicit edit is authoritative: the corrected draft overwrites the
    # vessel row (unlike the create path, which only fills NULLs).
    v = db_session.execute(select(Vessel).where(Vessel.imo == 9777888)).scalar_one()
    assert v.name == "SAGA ADVENTURE"
    assert float(v.draft) == round(33.0 / FEET_PER_M, 2)


def test_edit_propagates_vessel_name_and_loa(db_session):
    # Bug #2: changing the name / LOA on a berth request must reach the vessel
    # row (and therefore the reservations view), even though a vessel already
    # exists for this IMO.
    out = record_manual_request(db_session, _form(imo=9881199, vessel="OLD NAME"))

    update_manual_request(
        db_session,
        out["intake_event_id"],
        _form(imo=9881199, vessel="NEW NAME", length_ft=620.0),
    )

    v = db_session.execute(select(Vessel).where(Vessel.imo == 9881199)).scalar_one()
    assert v.name == "NEW NAME"
    assert float(v.loa) == round(620.0 / FEET_PER_M, 2)


def test_edit_blank_field_keeps_existing_vessel_dim(db_session):
    # Authoritative-but-not-destructive: a value the operator leaves blank does
    # NOT wipe the stored dimension (the request form isn't a vessel eraser).
    out = record_manual_request(db_session, _form(imo=9882200, beam_ft=91.5))

    update_manual_request(
        db_session,
        out["intake_event_id"],
        _form(imo=9882200, beam_ft=None),  # beam cleared on the form
    )

    v = db_session.execute(select(Vessel).where(Vessel.imo == 9882200)).scalar_one()
    assert float(v.beam) == round(91.5 / FEET_PER_M, 2)  # preserved


def test_empty_submission_records_nothing(db_session):
    # Bug #1 safety net: a content-empty POST (no vessel / imo / etb) must not
    # land a blank berth-request card.
    ev0, res0 = _counts(db_session)
    out = record_manual_request(
        db_session, BerthRequestForm(source="phone")
    )
    assert out["skipped"] is True
    assert out["intake_event_id"] is None
    assert _counts(db_session) == (ev0, res0)


def test_edit_creates_reservation_when_arrival_date_added(db_session):
    # Originally no ETB -> intake landed, no reservation.
    out = record_manual_request(db_session, _form(imo=9001122, etb=None))
    assert out["reservation_id"] is None

    edited = update_manual_request(
        db_session,
        out["intake_event_id"],
        _form(imo=9001122, etb=dt.date(2026, 7, 1), etd=dt.date(2026, 7, 5)),
    )
    assert edited["reservation_id"] is not None
    ev = db_session.execute(
        select(IntakeEvent).where(IntakeEvent.id == out["intake_event_id"])
    ).scalar_one()
    assert ev.processed is True
    assert ev.reservation_id == edited["reservation_id"]


def test_edit_rejects_online_form_rows(db_session):
    # An online-form row (source='form') uses SharePoint keys and is immutable.
    ev_id = db_session.execute(
        text(
            "INSERT INTO intake_event (source, raw, dedupe_key, processed) "
            "VALUES ('form', '{}'::jsonb, 'k-form-1', false) RETURNING id"
        )
    ).scalar_one()
    with pytest.raises(ValueError):
        update_manual_request(db_session, ev_id, _form(imo=9223344))


def test_edit_missing_event_raises_lookup(db_session):
    with pytest.raises(LookupError):
        update_manual_request(db_session, 999999, _form(imo=9334455))


def test_delete_removes_intake_event_and_reservation(db_session):
    ev0, res0 = _counts(db_session)
    out = record_manual_request(db_session, _form(imo=9445566))
    rid = out["reservation_id"]
    assert rid is not None

    deleted = delete_manual_request(db_session, out["intake_event_id"])
    assert deleted["deleted"] is True
    assert deleted["reservation_id"] == rid
    # Back to the starting counts — both the audit row and its projection gone.
    assert _counts(db_session) == (ev0, res0)
    assert db_session.execute(
        select(IntakeEvent).where(IntakeEvent.id == out["intake_event_id"])
    ).scalar_one_or_none() is None
    assert db_session.execute(
        select(Reservation).where(Reservation.id == rid)
    ).scalar_one_or_none() is None


def test_delete_without_reservation(db_session):
    # No ETB -> intake landed but no reservation; delete still drops the event.
    out = record_manual_request(db_session, _form(imo=9667788, etb=None))
    assert out["reservation_id"] is None

    deleted = delete_manual_request(db_session, out["intake_event_id"])
    assert deleted["reservation_id"] is None
    assert db_session.execute(
        select(IntakeEvent).where(IntakeEvent.id == out["intake_event_id"])
    ).scalar_one_or_none() is None


def test_delete_rejects_online_form_rows(db_session):
    ev_id = db_session.execute(
        text(
            "INSERT INTO intake_event (source, raw, dedupe_key, processed) "
            "VALUES ('form', '{}'::jsonb, 'k-form-del', false) RETURNING id"
        )
    ).scalar_one()
    with pytest.raises(ValueError):
        delete_manual_request(db_session, ev_id)


def test_delete_missing_event_raises_lookup(db_session):
    with pytest.raises(LookupError):
        delete_manual_request(db_session, 999999)


# --- DB: end-to-end through the HTTP endpoints -----------------------------
@pytest.fixture
def client(db_session):
    app.dependency_overrides[get_session] = lambda: db_session
    # Whole-app HTTP Basic is active iff OPERATOR_USER+PASSWORD are set (blank in
    # CI -> open). Attach credentials when configured so this runs either way.
    from app.config import get_settings

    s = get_settings()
    c = TestClient(app)
    if s.operator_user and s.operator_password:
        import base64

        token = base64.b64encode(
            f"{s.operator_user}:{s.operator_password}".encode()
        ).decode()
        c.headers["Authorization"] = f"Basic {token}"
    try:
        yield c
    finally:
        app.dependency_overrides.pop(get_session, None)


def test_edit_request_endpoint_updates_reservation_view(client, db_session):
    # The user's scenario: record a request, edit the name through PATCH, and
    # confirm the Reservations view reflects it — with no extra berth-request row.
    created = client.post(
        "/intake/berth-request",
        json={"source": "phone", "vessel": "OLD NAME", "imo": 9090909,
              "draft_ft": 31.1, "etb": "2026-07-10T08:00"},
    )
    assert created.status_code == 201
    body = created.json()
    intake_id, res_id = body["intake_event_id"], body["reservation_id"]
    ev_before = len(client.get("/intake/berth-requests", params={"limit": 500}).json())

    patched = client.patch(
        f"/intake/berth-requests/{intake_id}",
        json={"source": "phone", "vessel": "NEW NAME", "imo": 9090909,
              "draft_ft": 31.1, "etb": "2026-07-10T08:00"},
    )
    assert patched.status_code == 200

    # Reservations view shows the corrected name (bug #2)...
    rows = client.get("/reservations", params={"limit": 500}).json()
    res = next(r for r in rows if r["id"] == res_id)
    assert res["vessel_name"] == "NEW NAME"
    # ...and no phantom berth-request card was created (bug #1).
    ev_after = len(client.get("/intake/berth-requests", params={"limit": 500}).json())
    assert ev_after == ev_before
