"""Manual berth-request entry tests.

``normalize_form`` is pure (units, cargo, notes, warnings) and always runs.
``record_manual_request`` and the endpoint are DB-marked and auto-skip without
a migrated PostGIS (via the ``db_session`` fixture).
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select, text

from app.intake.manual import (
    FEET_PER_M,
    BerthRequestForm,
    normalize_form,
    record_manual_request,
)
from app.models import IntakeEvent, Reservation, Vessel


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


def test_dates_become_midnight_utc_timestamps():
    req = normalize_form(_form())
    assert req.etb == dt.datetime(2026, 6, 16, tzinfo=dt.timezone.utc)
    assert req.etd == dt.datetime(2026, 6, 19, tzinfo=dt.timezone.utc)


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
