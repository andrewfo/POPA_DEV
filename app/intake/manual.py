"""Manual berth-request entry — the sole intake channel.

Berth requests are entered **by hand** by an operator from a phone call, an
email, or a walk-in (there is no automated online-form feed: the Adobe Sign ->
SharePoint pipeline was retired). This module captures the "Berth Request and
Assignment Record" data and lands it in the ``intake_event`` table (raw,
deduped, idempotent), then projects the request into a ``status='requested'``
reservation so it is immediately visible to scheduling / conflict detection.

The berth (station range) is left **UNASSIGNED**: a phoned/emailed request has
no berth yet — the port assigns it later (the form's "Port of Port Arthur"
section). We store an *empty* ``numrange`` until then, which never overlaps
anything, so an unassigned request raises no false conflict. Time, vessel, and
cargo are captured now; the station range is filled when the berth is assigned.

Units: the form is in **feet** (LOA, beam, draft); ``vessel`` stores **metres**
(to match AIS-derived dimensions), so we convert on the way in and keep the
verbatim feet values in ``intake_event.raw``.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field

from pydantic import BaseModel
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import IntakeEvent, Vessel
from app.tz import CENTRAL, assume_central

# Station "M" is feet; AIS/vessel dimensions are metres. Same constant as
# app/occupancy/project.FEET_PER_M — duplicated to avoid coupling intake to the
# occupancy package.
FEET_PER_M = 3.280839895

# The human channels a manual request can arrive on. (The legacy 'form' source
# — the retired online Adobe Sign feed — is no longer an entry channel; existing
# 'form' rows stay valid history but new entries can't claim it.) 'email' is
# added in migration 0004.
MANUAL_SOURCES = ("phone", "email", "operator")
_DEFAULT_SOURCE = "phone"


def dedupe_key(raw: dict) -> str:
    """Stable content hash of a raw intake row (order-independent), so an
    identical re-submission is deduped at the ``intake_event`` level."""
    blob = json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class BerthRequestForm(BaseModel):
    """The "Berth Request and Assignment Record" as a structured submission.

    Everything is optional at the transport layer; ``normalize_form`` decides
    what is required to build a vessel / reservation and records a warning for
    anything missing. Date-only fields arrive as ``YYYY-MM-DD`` (HTML date
    inputs); ETB/ETD arrive as ``YYYY-MM-DDTHH:MM`` (``datetime-local`` inputs —
    the arrival/departure timestamp) and a bare date is still accepted.
    """

    # how the request reached us (not on the paper form, but operational metadata)
    source: str = _DEFAULT_SOURCE

    request_date: dt.date | None = None
    vessel: str | None = None
    ss_line: str | None = None
    flag: str | None = None
    destinations: str | None = None
    length_ft: float | None = None
    deadweight_lbs: float | None = None
    beam_ft: float | None = None
    imo: int | None = None
    draft_ft: float | None = None
    bunkers: bool = False

    # "Vessel is due from <origin> on <etb>" / "To Sail For <dest> on <etd>".
    # ETB/ETD carry a time of day (arrival/departure timestamp), interpreted as
    # Central Time; a bare date is still accepted and lands at midnight Central.
    due_from: str | None = None
    etb: dt.datetime | None = None
    sail_for: str | None = None
    etd: dt.datetime | None = None

    inbound_cargo: str | None = None
    inbound_tons: float | None = None
    outbound_cargo: str | None = None
    outbound_tons: float | None = None
    outbound_cargo_start: dt.date | None = None

    agency: str | None = None


@dataclass
class NormalizedRequest:
    """A manual request reduced to what the data layer stores."""

    source: str
    imo: int | None
    vessel_name: str | None
    loa_m: float | None
    beam_m: float | None
    draft_m: float | None
    etb: dt.datetime | None
    etd: dt.datetime | None
    cargo: str | None
    notes: str
    warnings: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


def _ft_to_m(value: float | None) -> float | None:
    return None if value is None else round(value / FEET_PER_M, 2)


def _to_dt(value: dt.datetime | dt.date | None) -> dt.datetime | None:
    """Coerce an ETB/ETD input to a Central-Time timestamp for the
    ``timestamptz`` ``time_range``. A ``datetime`` (the arrival/departure time of
    day captured by the form) keeps its instant, assuming Central when naive
    (form inputs send no zone); a bare ``date`` lands at midnight Central (a
    date-only request). All zone handling goes through ``app/tz.py``."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return assume_central(value)
    return dt.datetime.combine(value, dt.time.min, tzinfo=CENTRAL)


def _cargo_summary(form: BerthRequestForm) -> str | None:
    """One-line cargo string for ``reservation.cargo`` (<=200 chars). Full
    detail (weights, dates) is preserved in ``intake_event.raw`` and notes."""
    parts: list[str] = []
    if form.inbound_cargo:
        bit = f"IN: {form.inbound_cargo}"
        if form.inbound_tons is not None:
            bit += f" ({form.inbound_tons:g} t)"
        parts.append(bit)
    if form.outbound_cargo:
        bit = f"OUT: {form.outbound_cargo}"
        if form.outbound_tons is not None:
            bit += f" ({form.outbound_tons:g} t)"
        parts.append(bit)
    if not parts:
        return None
    return "; ".join(parts)[:200]


def _notes(form: BerthRequestForm, source: str, warnings: list[str]) -> str:
    """Assemble human-readable notes from the fields without dedicated columns
    (agent, flag, line, DWT, bunkers, origin/destination, berth status)."""
    bits = [f"manual entry ({source})"]
    if form.agency:
        bits.append(f"agent: {form.agency}")
    if form.ss_line:
        bits.append(f"line: {form.ss_line}")
    if form.flag:
        bits.append(f"flag: {form.flag}")
    if form.due_from or form.sail_for:
        bits.append(f"from {form.due_from or '?'} → {form.sail_for or '?'}")
    if form.destinations:
        bits.append(f"destinations: {form.destinations}")
    if form.deadweight_lbs is not None:
        bits.append(f"DWT {form.deadweight_lbs:g} lbs")
    bits.append(f"bunkers: {'yes' if form.bunkers else 'no'}")
    bits.append("berth UNASSIGNED (port to assign)")
    if warnings:
        bits.append("warnings: " + "; ".join(warnings))
    return "; ".join(bits)


def normalize_form(form: BerthRequestForm) -> NormalizedRequest:
    """Pure normalization: structured form -> what the DB stores, plus warnings.
    No database access — unit-testable on its own."""
    warnings: list[str] = []

    source = form.source if form.source in MANUAL_SOURCES else _DEFAULT_SOURCE
    if form.source not in MANUAL_SOURCES:
        warnings.append(f"unknown source {form.source!r}; recorded as {source!r}")

    if not form.vessel:
        warnings.append("missing vessel name")
    if form.imo is None:
        warnings.append("missing IMO — vessel not keyed; reconcile by hand")
    if form.etb is None:
        warnings.append("missing arrival date (ETB) — no time window")

    return NormalizedRequest(
        source=source,
        imo=form.imo,
        vessel_name=form.vessel.strip() if form.vessel else None,
        loa_m=_ft_to_m(form.length_ft),
        beam_m=_ft_to_m(form.beam_ft),
        draft_m=_ft_to_m(form.draft_ft),
        etb=_to_dt(form.etb),
        etd=_to_dt(form.etd),
        cargo=_cargo_summary(form),
        notes=_notes(form, source, warnings),
        warnings=warnings,
        raw=form.model_dump(mode="json"),
    )


# --- persistence -----------------------------------------------------------
def _upsert_vessel(session: Session, req: NormalizedRequest) -> int | None:
    """Find-or-create a vessel by IMO (IMO isn't a unique key in this schema —
    AIS keys on MMSI — so we look it up explicitly). Existing detail is never
    clobbered: we only fill columns that are currently NULL, leaving
    AIS-sourced dimensions authoritative."""
    if req.imo is None:
        return None  # nothing to key on; reservation carries the name in notes

    existing = session.execute(
        select(Vessel.id).where(Vessel.imo == req.imo).limit(1)
    ).scalar_one_or_none()

    fields = {
        "name": req.vessel_name,
        "loa": req.loa_m,
        "beam": req.beam_m,
        "draft": req.draft_m,
    }
    if existing is not None:
        session.execute(
            update(Vessel)
            .where(Vessel.id == existing)
            .values(
                # COALESCE(existing, new): keep what we already know.
                **{k: func.coalesce(getattr(Vessel, k), v) for k, v in fields.items()},
                updated_at=func.now(),
            )
        )
        return existing

    return int(
        session.execute(
            insert(Vessel).values(imo=req.imo, **fields).returning(Vessel.id)
        ).scalar_one()
    )


def _insert_reservation(session: Session, req: NormalizedRequest, vessel_id: int | None) -> int:
    """Create the status='requested' reservation. Station range is empty
    (unassigned); time range spans ETB..ETD (open-ended if ETD is missing)."""
    row = session.execute(
        text(
            """
            INSERT INTO reservation
                (vessel_id, type, station_range, time_range, direction,
                 status, source, cargo, notes, created_at)
            VALUES
                (:vessel_id, 'vessel',
                 'empty'::numrange,
                 tstzrange(:etb, :etd, '[)'),
                 NULL, 'requested', :source, :cargo, :notes, now())
            RETURNING id
            """
        ),
        {
            "vessel_id": vessel_id,
            "etb": req.etb,
            "etd": req.etd,
            "source": req.source,
            "cargo": req.cargo,
            "notes": req.notes,
        },
    ).one()
    return int(row.id)


def record_manual_request(session: Session, form: BerthRequestForm) -> dict:
    """Land a manual berth request: ``intake_event`` (raw, deduped) + a
    ``requested`` reservation (+ vessel upsert), all in one transaction. Does
    NOT commit — the caller (the endpoint) owns the transaction boundary.

    Idempotent: an identical re-submission (same raw payload) is deduped at the
    ``intake_event`` level and creates no second reservation.

    Returns a summary dict for the API response.
    """
    req = normalize_form(form)

    # 1. Land the raw request. ON CONFLICT DO NOTHING on the content hash makes a
    #    duplicate submission a no-op; if nothing lands, don't create a second
    #    reservation either.
    key = dedupe_key(req.raw)
    intake_id = session.execute(
        pg_insert(IntakeEvent)
        .values(source=req.source, raw=req.raw, dedupe_key=key, processed=False)
        .on_conflict_do_nothing(
            index_elements=["dedupe_key"],
            index_where=IntakeEvent.dedupe_key.isnot(None),
        )
        .returning(IntakeEvent.id)
    ).scalar_one_or_none()

    if intake_id is None:
        existing = session.execute(
            select(IntakeEvent.id, IntakeEvent.reservation_id).where(
                IntakeEvent.dedupe_key == key
            )
        ).first()
        return {
            "duplicate": True,
            "intake_event_id": existing.id if existing else None,
            "reservation_id": existing.reservation_id if existing else None,
            "vessel_id": None,
            "warnings": req.warnings,
        }

    # 2/3. Upsert the vessel and create the requested reservation.
    vessel_id = _upsert_vessel(session, req)
    reservation_id: int | None = None
    if req.etb is not None:
        reservation_id = _insert_reservation(session, req, vessel_id)
        # 4. Link the intake event to its reservation and mark it processed.
        session.execute(
            update(IntakeEvent)
            .where(IntakeEvent.id == intake_id)
            .values(processed=True, reservation_id=reservation_id)
        )
    else:
        # No arrival date -> no defensible time window; keep the request as an
        # unprocessed intake_event for human follow-up rather than inventing one.
        req.warnings.append("no reservation created (missing arrival date)")

    return {
        "duplicate": False,
        "intake_event_id": intake_id,
        "reservation_id": reservation_id,
        "vessel_id": vessel_id,
        "warnings": req.warnings,
    }


def delete_manual_request(session: Session, intake_id: int) -> dict:
    """Delete a manual berth request: drop the raw ``intake_event`` row and the
    ``requested`` reservation it projected. An operator removing a phoned/emailed
    request that was mistaken or withdrawn, rather than leaving a stale row + an
    orphan reservation behind.

    Like the in-place edit, this is restricted to manual channels
    (``phone|email|operator``); the immutable online-form CSV export cannot be
    deleted here. Does NOT commit — the endpoint owns the transaction boundary.
    Raises ``LookupError`` if the event doesn't exist (-> 404) and ``ValueError``
    if it isn't an editable manual-channel row (-> 422).
    """
    existing = session.execute(
        select(IntakeEvent.source, IntakeEvent.reservation_id).where(
            IntakeEvent.id == intake_id
        )
    ).first()
    if existing is None:
        raise LookupError(f"intake_event {intake_id} not found")
    if existing.source not in EDITABLE_SOURCES:
        raise ValueError(
            f"only manual-channel requests ({', '.join(EDITABLE_SOURCES)}) "
            f"can be deleted, not {existing.source!r}"
        )

    # The intake_event -> reservation FK is ON DELETE SET NULL, so deleting the
    # event first just clears the link; then drop the projected reservation.
    session.execute(
        text("DELETE FROM intake_event WHERE id = :id"), {"id": intake_id}
    )
    reservation_id = existing.reservation_id
    if reservation_id is not None:
        session.execute(
            text("DELETE FROM reservation WHERE id = :id"), {"id": reservation_id}
        )

    return {
        "deleted": True,
        "intake_event_id": intake_id,
        "reservation_id": reservation_id,
    }


# Channels whose raw payload uses this form's lowercase keys, so an operator can
# re-edit them through the same form. A legacy 'form' row (the retired online
# feed) carries the old export's column names, so it stays read-only here.
EDITABLE_SOURCES = ("phone", "email", "operator")


def update_manual_request(
    session: Session, intake_id: int, form: BerthRequestForm
) -> dict:
    """Overwrite an existing manual berth request **in place**: re-normalize the
    form, replace ``intake_event.raw`` (and its content-hash ``dedupe_key``), and
    re-project the linked ``requested`` reservation (creating one if the edit now
    supplies an arrival date).

    This deliberately *mutates* the raw audit row — the one place we do — to let
    an operator correct a phoned/emailed request one row at a time, rather than
    leaving a stale duplicate behind. The berth assignment (``station_range`` /
    ``berth_id``), direction, and reconciliation ``status`` are left untouched:
    the request governs vessel / time / cargo, not where or whether it berthed.

    Does NOT commit — the endpoint owns the transaction boundary. Raises
    ``LookupError`` if the event doesn't exist and ``ValueError`` if it isn't an
    editable manual-channel row.
    """
    existing = session.execute(
        select(
            IntakeEvent.id, IntakeEvent.source, IntakeEvent.reservation_id
        ).where(IntakeEvent.id == intake_id)
    ).first()
    if existing is None:
        raise LookupError(f"intake_event {intake_id} not found")
    if existing.source not in EDITABLE_SOURCES:
        raise ValueError(
            f"only manual-channel requests ({', '.join(EDITABLE_SOURCES)}) "
            f"can be edited, not {existing.source!r}"
        )

    req = normalize_form(form)
    key = dedupe_key(req.raw)

    # Replace the raw payload + its content hash in place. A collision with
    # another row's hash trips the unique index -> IntegrityError -> 409.
    session.execute(
        update(IntakeEvent)
        .where(IntakeEvent.id == intake_id)
        .values(source=req.source, raw=req.raw, dedupe_key=key)
    )

    vessel_id = _upsert_vessel(session, req)
    reservation_id = existing.reservation_id

    if reservation_id is not None:
        # Re-project onto the existing reservation. An empty time range when the
        # arrival date is cleared never conflicts (same rule as an unassigned
        # berth) and avoids an accidental unbounded range.
        session.execute(
            text(
                """
                UPDATE reservation
                   SET vessel_id  = :vessel_id,
                       time_range = CASE
                           WHEN :etb IS NULL THEN 'empty'::tstzrange
                           ELSE tstzrange(:etb, :etd, '[)')
                       END,
                       source = :source,
                       cargo  = :cargo,
                       notes  = :notes
                 WHERE id = :rid
                """
            ),
            {
                "vessel_id": vessel_id,
                "etb": req.etb,
                "etd": req.etd,
                "source": req.source,
                "cargo": req.cargo,
                "notes": req.notes,
                "rid": reservation_id,
            },
        )
        if req.etb is None:
            req.warnings.append("arrival date cleared — time window emptied")
    elif req.etb is not None:
        # No reservation existed (the original lacked an arrival date); the edit
        # now supplies one, so project it and link the event.
        reservation_id = _insert_reservation(session, req, vessel_id)
        session.execute(
            update(IntakeEvent)
            .where(IntakeEvent.id == intake_id)
            .values(processed=True, reservation_id=reservation_id)
        )
    else:
        req.warnings.append("no reservation created (missing arrival date)")

    return {
        "duplicate": False,
        "intake_event_id": intake_id,
        "reservation_id": reservation_id,
        "vessel_id": vessel_id,
        "warnings": req.warnings,
    }
