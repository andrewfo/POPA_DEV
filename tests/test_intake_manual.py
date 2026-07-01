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
from pydantic import ValidationError

from app.intake.manual import (
    FEET_PER_M,
    BerthRequestForm,
    delete_manual_request,
    normalize_form,
    record_manual_request,
    update_manual_request,
    valid_imo,
)
from app.main import app
from app.models import AuditLog, IntakeEvent, Reservation, Vessel
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


def test_ai_source_is_accepted_not_downgraded():
    # 'ai' is the AI normalizer's own provenance tag — a real accepted source,
    # not downgraded to 'phone' the way an unknown value is.
    req = normalize_form(_form(source="ai"))
    assert req.source == "ai"
    assert not any("unknown source" in w for w in req.warnings)


def test_unknown_source_still_downgrades_with_warning():
    req = normalize_form(_form(source="carrier-pigeon"))
    assert req.source == "phone"
    assert any("unknown source" in w for w in req.warnings)


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


# --- IMO validation --------------------------------------------------------
def test_valid_imo_accepts_real_check_digits():
    # Known-good IMO numbers (check digit agrees with the first six).
    assert valid_imo(9317406)   # the suite's SAGA ADVENTURE
    assert valid_imo(9074729)   # classic worked example
    assert valid_imo(1234567)   # 1*7+2*6+3*5+4*4+5*3+6*2 = 77 -> 7


def test_valid_imo_rejects_bad_length_and_check_digit():
    assert not valid_imo(75)         # too short — the headline reject case
    assert not valid_imo(931740)     # 6 digits
    assert not valid_imo(93174060)   # 8 digits
    assert not valid_imo(9317400)    # right length, wrong check digit


def test_form_rejects_malformed_imo():
    # The transport-layer field validator turns a bad IMO into a 422 rather than
    # letting "75" become a vessel key.
    with pytest.raises(ValidationError):
        _form(imo=75)


def test_form_accepts_missing_imo():
    # IMO is required by the UI, but the model leaves it optional so an edit /
    # partial submission validates; only a *present, invalid* value is rejected.
    assert BerthRequestForm(vessel="NO IMO YET").imo is None


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


def test_notes_are_one_labelled_item_per_line():
    # Readability: notes are a newline-separated list, not a semicolon run-on, so
    # each captured field shows on its own line (the UI renders pre-line).
    req = normalize_form(
        _form(agency="ISS BEAUMONT", flag="LR", due_from="Beaumont", sail_for="Houston")
    )
    lines = req.notes.split("\n")
    assert lines[0] == "manual entry (phone)"
    assert "agent: ISS BEAUMONT" in lines
    assert "flag: LR" in lines
    assert "route: Beaumont → Houston" in lines
    assert "berth UNASSIGNED (port to assign)" in lines
    # no semicolon-joined run-on between distinct fields
    assert "; agent:" not in req.notes


# --- DB: end-to-end landing ------------------------------------------------
def _counts(session):
    return (
        session.execute(select(func.count()).select_from(IntakeEvent)).scalar_one(),
        session.execute(select(func.count()).select_from(Reservation)).scalar_one(),
    )


def test_records_intake_reservation_and_vessel(db_session):
    ev0, res0 = _counts(db_session)
    out = record_manual_request(db_session, _form(imo=9111228))

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
        select(Vessel).where(Vessel.imo == 9111228)
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
    f = _form(imo=9333448)
    first = record_manual_request(db_session, f)
    ev1, res1 = _counts(db_session)

    second = record_manual_request(db_session, _form(imo=9333448))
    ev2, res2 = _counts(db_session)

    assert second["duplicate"] is True
    assert second["reservation_id"] == first["reservation_id"]
    assert (ev2, res2) == (ev1, res1)  # nothing new landed


def test_missing_etb_lands_intake_without_reservation(db_session):
    ev0, res0 = _counts(db_session)
    out = record_manual_request(db_session, _form(imo=9555668, etb=None))

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


def test_ais_tracked_vessel_warns_that_entered_draft_is_overridden(db_session):
    # An AIS-tracked ship (has MMSI) owns its dimensions: a request keeps AIS's
    # draft, so an operator's entered draft is dropped — and we say so. Seed the
    # AIS ship first (MMSI + a real 9.1 m draft), then request it with 20 ft.
    db_session.execute(text(
        "INSERT INTO vessel (mmsi, imo, name, draft) "
        "VALUES (565440111, 9990002, 'AIS SHIP', 9.1)"
    ))
    out = record_manual_request(
        db_session,
        _form(vessel="AIS SHIP", imo=9990002, draft_ft=20.0, beam_ft=None),
    )
    warns = " ".join(out["warnings"])
    assert "not applied" in warns and "AIS-tracked" in warns
    assert "20.0 ft" in warns and "29.9 ft" in warns   # entered vs authoritative
    # And the stored draft is unchanged (NULL-fill kept the AIS value).
    v = db_session.execute(select(Vessel).where(Vessel.imo == 9990002)).scalar_one()
    assert float(v.draft) == 9.1


def test_manual_only_vessel_does_not_warn_about_override(db_session):
    # A ship with no MMSI is the operator's to define — the entered draft applies,
    # so no override warning.
    out = record_manual_request(
        db_session, _form(imo=9990014, draft_ft=20.0),
    )
    assert not any("not applied" in w for w in out["warnings"])


def test_edit_propagates_vessel_name_and_loa(db_session):
    # Bug #2: changing the name / LOA on a berth request must reach the vessel
    # row (and therefore the reservations view), even though a vessel already
    # exists for this IMO.
    out = record_manual_request(db_session, _form(imo=9881196, vessel="OLD NAME"))

    update_manual_request(
        db_session,
        out["intake_event_id"],
        _form(imo=9881196, vessel="NEW NAME", length_ft=620.0),
    )

    v = db_session.execute(select(Vessel).where(Vessel.imo == 9881196)).scalar_one()
    assert v.name == "NEW NAME"
    assert float(v.loa) == round(620.0 / FEET_PER_M, 2)


def test_edit_blank_field_keeps_existing_vessel_dim(db_session):
    # Authoritative-but-not-destructive: a value the operator leaves blank does
    # NOT wipe the stored dimension (the request form isn't a vessel eraser).
    out = record_manual_request(db_session, _form(imo=9882205, beam_ft=91.5))

    update_manual_request(
        db_session,
        out["intake_event_id"],
        _form(imo=9882205, beam_ft=None),  # beam cleared on the form
    )

    v = db_session.execute(select(Vessel).where(Vessel.imo == 9882205)).scalar_one()
    assert float(v.beam) == round(91.5 / FEET_PER_M, 2)  # preserved


def test_create_overwrites_manual_only_vessel_loa(db_session):
    # A vessel that only ever came from manual entry (no MMSI) is the operator's
    # to correct: a re-submitted request with a different LOA overwrites it,
    # instead of silently keeping the first value (the "always 1000 ft" bug).
    record_manual_request(db_session, _form(imo=9112234, length_ft=585.0))
    # Re-submit (a distinct payload, so it isn't deduped) with a corrected LOA.
    record_manual_request(
        db_session,
        _form(imo=9112234, length_ft=400.0,
              etb=dt.date(2026, 7, 2), etd=dt.date(2026, 7, 5)),
    )

    v = db_session.execute(select(Vessel).where(Vessel.imo == 9112234)).scalar_one()
    assert float(v.loa) == round(400.0 / FEET_PER_M, 2)  # overwritten, not stuck


def test_create_preserves_ais_vessel_loa(db_session):
    # The mirror invariant: an AIS-tracked vessel (has an MMSI) keeps its
    # AIS-sourced dimensions authoritative — a manual request for the SAME ship
    # only NULL-fills, so it can't clobber the live measurement.
    db_session.execute(
        text(
            "INSERT INTO vessel (imo, mmsi, name, loa) "
            "VALUES (9114452, 366114455, 'AIS BOAT', 300)"
        )
    )
    record_manual_request(
        db_session, _form(imo=9114452, vessel="AIS BOAT", length_ft=100.0)
    )

    v = db_session.execute(select(Vessel).where(Vessel.imo == 9114452)).scalar_one()
    assert float(v.loa) == 300.0  # AIS value untouched by the manual request


def test_create_rejects_imo_belonging_to_another_ship(db_session):
    # The operator's rule: two ships can't share an IMO. A new request whose IMO
    # is already on file under a *different* ship name is refused (the operator
    # likely mistyped it) rather than silently merging + renaming the other ship.
    record_manual_request(db_session, _form(imo=9118898, vessel="FIRST SHIP"))

    with pytest.raises(ValueError, match="already on file"):
        record_manual_request(db_session, _form(imo=9118898, vessel="SECOND SHIP"))

    # The first ship's record is untouched — no rename leaked through.
    v = db_session.execute(select(Vessel).where(Vessel.imo == 9118898)).scalar_one()
    assert v.name == "FIRST SHIP"


def test_create_same_ship_same_imo_is_allowed(db_session):
    # The legitimate case: the SAME ship phoned in twice (same IMO, same name,
    # case/whitespace aside) is not a collision — it's a second visit.
    record_manual_request(db_session, _form(imo=9119907, vessel="REPEAT CALLER"))
    out = record_manual_request(
        db_session,
        _form(imo=9119907, vessel="  repeat   caller ",  # sloppy re-typing
              etb=dt.date(2026, 8, 1), etd=dt.date(2026, 8, 3)),
    )
    assert out["duplicate"] is False
    assert out["reservation_id"] is not None  # a second reservation, one vessel
    assert (
        db_session.execute(
            select(func.count()).select_from(Vessel).where(Vessel.imo == 9119907)
        ).scalar_one()
        == 1
    )


def test_corrected_loa_reprojects_placed_footprint(db_session):
    # End-to-end of the reported bug: a bow-placed reservation's footprint must
    # follow a corrected LOA, holding the bow fixed — not stay at the length
    # captured when it was placed.
    out = record_manual_request(db_session, _form(imo=9116670, length_ft=585.0))
    vid = out["vessel_id"]
    loa_ft = 585.0  # the LOA at placement, in feet (the station-range span)

    # Place it from the bow: upstream keeps the bow at the upper bound (POPA 2000).
    db_session.execute(
        text(
            """
            UPDATE reservation
               SET status = 'confirmed', direction = 'upstream',
                   station_range = numrange(
                       CAST(:lo AS numeric), CAST(:hi AS numeric), '[]')
             WHERE vessel_id = :v
            """
        ),
        {"lo": 2000 - loa_ft, "hi": 2000, "v": vid},
    )

    # Operator re-submits with a corrected, shorter LOA.
    record_manual_request(
        db_session,
        _form(imo=9116670, length_ft=250.0,
              etb=dt.date(2026, 7, 2), etd=dt.date(2026, 7, 5)),
    )

    lo, hi = db_session.execute(
        text(
            "SELECT lower(station_range), upper(station_range) "
            "FROM reservation WHERE vessel_id = :v AND status = 'confirmed'"
        ),
        {"v": vid},
    ).one()
    assert float(hi) == 2000.0                       # bow held fixed
    assert round(float(hi) - float(lo), 1) == 250.0  # span re-derived from LOA


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
    out = record_manual_request(db_session, _form(imo=9001124, etb=None))
    assert out["reservation_id"] is None

    edited = update_manual_request(
        db_session,
        out["intake_event_id"],
        _form(imo=9001124, etb=dt.date(2026, 7, 1), etd=dt.date(2026, 7, 5)),
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
        update_manual_request(db_session, 999999, _form(imo=9334454))


def test_delete_soft_deletes_event_and_drops_reservation(db_session):
    ev0, res0 = _counts(db_session)
    out = record_manual_request(db_session, _form(imo=9445564))
    rid = out["reservation_id"]
    assert rid is not None

    deleted = delete_manual_request(db_session, out["intake_event_id"])
    assert deleted["deleted"] is True
    assert deleted["reservation_id"] == rid
    # The audit row SURVIVES (soft-delete — "the evidence a request arrived"), so
    # the intake count is unchanged; only the projected reservation is dropped.
    assert _counts(db_session) == (ev0 + 1, res0)
    row = db_session.execute(
        select(IntakeEvent).where(IntakeEvent.id == out["intake_event_id"])
    ).scalar_one()
    assert row.deleted_at is not None  # marked deleted, not erased
    assert db_session.execute(
        select(Reservation).where(Reservation.id == rid)
    ).scalar_one_or_none() is None


def test_delete_without_reservation(db_session):
    # No ETB -> intake landed but no reservation; delete still soft-deletes it.
    out = record_manual_request(db_session, _form(imo=9667784, etb=None))
    assert out["reservation_id"] is None

    deleted = delete_manual_request(db_session, out["intake_event_id"])
    assert deleted["reservation_id"] is None
    row = db_session.execute(
        select(IntakeEvent).where(IntakeEvent.id == out["intake_event_id"])
    ).scalar_one()
    assert row.deleted_at is not None


def test_deleted_row_can_be_resubmitted_and_is_not_re_editable(db_session):
    # A soft-deleted row no longer blocks an identical re-submission (the dedupe
    # index is partial on deleted_at IS NULL), and is itself off-limits to a
    # second delete/edit (it's invisible to the live API).
    out = record_manual_request(db_session, _form(imo=9445564))
    delete_manual_request(db_session, out["intake_event_id"])

    again = record_manual_request(db_session, _form(imo=9445564))
    assert again["duplicate"] is False
    assert again["intake_event_id"] != out["intake_event_id"]

    with pytest.raises(LookupError):
        delete_manual_request(db_session, out["intake_event_id"])
    with pytest.raises(LookupError):
        update_manual_request(db_session, out["intake_event_id"], _form(imo=9445564))


def test_ai_channel_is_editable_and_deletable(db_session):
    # Provenance ('ai') is decoupled from permission: an AI-tagged card carries a
    # distinct source but stays operator-correctable.
    out = record_manual_request(db_session, _form(imo=9317406, source="ai"))
    assert out["intake_event_id"] is not None
    src = db_session.execute(
        select(IntakeEvent.source).where(IntakeEvent.id == out["intake_event_id"])
    ).scalar_one()
    assert src == "ai"
    # Editable...
    edited = update_manual_request(
        db_session, out["intake_event_id"], _form(imo=9317406, vessel="NEW", source="ai")
    )
    assert edited["intake_event_id"] == out["intake_event_id"]
    # ...and deletable.
    deleted = delete_manual_request(db_session, out["intake_event_id"])
    assert deleted["deleted"] is True


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


def test_create_endpoint_rejects_duplicate_imo_cleanly(client, db_session):
    # The reported bug, through HTTP: a second ship under an existing IMO must
    # fail with a clean 4xx (not a 500) and leave no orphan berth-request card.
    first = client.post(
        "/intake/berth-request",
        json={"source": "phone", "vessel": "ALPHA", "imo": 9095955,
              "draft_ft": 10.0, "etb": "2026-07-10T08:00"},
    )
    assert first.status_code == 201
    before = len(client.get("/intake/berth-requests", params={"limit": 500}).json())

    clash = client.post(
        "/intake/berth-request",
        json={"source": "phone", "vessel": "BRAVO", "imo": 9095955,
              "draft_ft": 10.0, "etb": "2026-07-11T08:00"},
    )
    assert clash.status_code == 422
    assert "IMO" in clash.json()["detail"]
    # Rolled back: no second audit row, first ship's name intact.
    after = len(client.get("/intake/berth-requests", params={"limit": 500}).json())
    assert after == before
    rows = client.get("/reservations", params={"limit": 500}).json()
    assert all(r["vessel_name"] != "BRAVO" for r in rows)


def test_edit_request_endpoint_updates_reservation_view(client, db_session):
    # The user's scenario: record a request, edit the name through PATCH, and
    # confirm the Reservations view reflects it — with no extra berth-request row.
    created = client.post(
        "/intake/berth-request",
        json={"source": "phone", "vessel": "OLD NAME", "imo": 9090905,
              "draft_ft": 31.1, "etb": "2026-07-10T08:00"},
    )
    assert created.status_code == 201
    body = created.json()
    intake_id, res_id = body["intake_event_id"], body["reservation_id"]
    ev_before = len(client.get("/intake/berth-requests", params={"limit": 500}).json())

    patched = client.patch(
        f"/intake/berth-requests/{intake_id}",
        json={"source": "phone", "vessel": "NEW NAME", "imo": 9090905,
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


def test_writes_leave_an_audit_trail(client, db_session):
    # Every mutating endpoint lands an audit_log row in the same transaction, so a
    # create + edit + delete each leave a trace of what was touched.
    def _audit_rows():
        return db_session.execute(
            select(AuditLog).where(AuditLog.entity == "intake_event")
        ).scalars().all()

    before = len(_audit_rows())
    created = client.post(
        "/intake/berth-request",
        json={"source": "phone", "vessel": "AUDIT ME", "imo": 9445564,
              "draft_ft": 10.0, "etb": "2026-07-10T08:00"},
    )
    assert created.status_code == 201
    intake_id = created.json()["intake_event_id"]

    client.patch(
        f"/intake/berth-requests/{intake_id}",
        json={"source": "phone", "vessel": "AUDIT ME 2", "imo": 9445564,
              "draft_ft": 10.0, "etb": "2026-07-10T08:00"},
    )
    client.delete(f"/intake/berth-requests/{intake_id}")

    rows = _audit_rows()
    assert len(rows) == before + 3
    actions = [r.action for r in rows if r.entity_id == intake_id]
    assert actions == ["create", "edit", "delete"]
    # The delete's audit detail preserves the pre-delete raw — the trail outlives
    # even the (soft-deleted) row's payload.
    delete_row = next(
        r for r in rows if r.entity_id == intake_id and r.action == "delete"
    )
    assert delete_row.detail["prior_raw"]["vessel"] == "AUDIT ME 2"
