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

from pydantic import BaseModel, field_validator
from sqlalchemy import and_, func, insert, select, text, update
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

# Provenance values a *new* intake request may legitimately carry: the human
# channels PLUS 'ai' (the AI-assisted normalizer's own tag, migration 0009 —
# distinct from the human 'email' channel it used to borrow). An unrecognized
# source is downgraded to ``_DEFAULT_SOURCE`` with a warning (see normalize_form).
ACCEPTED_SOURCES = MANUAL_SOURCES + ("ai",)

# Sources whose raw payload uses *this form's* lowercase keys, so an operator can
# re-edit / delete them through the same form. DELIBERATELY decoupled from
# provenance (that was the whole point of the 'ai' tag): it INCLUDES the AI
# channel because AI cards are operator-correctable, while the legacy online-form
# export ('form', different column names) stays read-only history. "Provenance"
# answers *how it was parsed*; this set answers *may an operator edit it* — two
# separate questions that used to share one tuple.
EDITABLE_SOURCES = MANUAL_SOURCES + ("ai",)

# Bunker fuel grades offered on the online request form (code -> full label).
# Mirrors the Power Pages "Bunkering Type" choice; stored by code, rendered with
# the label in notes. Kept here so the form, normalization, and the Dataverse
# spec (docs/power-pages-berth-intake.md) share one list.
BUNKER_TYPES = {
    "BIO": "Biofuels/Alternative Fuels (BIO)",
    "HFO": "Heavy Fuel Oil (HFO)",
    "LNG": "Liquified Natural Gas (LNG)",
    "MGO": "Marine Gas Oil (MGO)",
    "VLSFO": "Very Low-Sulfur Fuel Oil (VLSFO)",
}


def dedupe_key(raw: dict) -> str:
    """Stable content hash of a raw intake row (order-independent), so an
    identical re-submission is deduped at the ``intake_event`` level.

    ``source_raw`` (the verbatim upstream payload — e.g. a full Dataverse row) is
    **excluded** from the hash: such rows carry volatile system columns
    (``modifiedon``, ``@odata.etag``, formatted-value annotations) that drift
    between polls, so hashing them would defeat idempotency and re-land the same
    logical request on a re-poll. The normalized form fields are the stable
    identity; the verbatim payload is still preserved in ``intake_event.raw``."""
    stable = {k: v for k, v in raw.items() if k != "source_raw"}
    blob = json.dumps(stable, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def valid_imo(imo: int) -> bool:
    """True if ``imo`` is a structurally valid IMO ship-identification number.

    An IMO number is exactly **7 digits**; the leading 6 identify the ship and
    the 7th is a check digit = (sum of digit_i * (7-i) for i in 0..5) mod 10.
    This rejects free-typed garbage like ``75`` (too short) or a transposed
    number whose check digit no longer agrees — catching most fat-finger errors
    at intake. (MMSI, not validated here, is a separate 9-digit identifier.)"""
    if imo < 1_000_000 or imo > 9_999_999:  # not 7 digits
        return False
    digits = [int(c) for c in str(imo)]
    checksum = sum(d * (7 - i) for i, d in enumerate(digits[:6])) % 10
    return checksum == digits[6]


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

    # Bunkering detail (only meaningful when ``bunkers`` is set). ``bunker_type``
    # is one of BUNKER_TYPES (stored by code); ``bunker_qty_mt`` is the
    # approximate fuel quantity in **metric tons** (not feet/lbs — bunkers are
    # quoted in MT); ``bunkering_acknowledged`` is the requestor ticking the
    # port's bunkering acknowledgement on the online form.
    bunkering_acknowledged: bool = False
    bunker_type: str | None = None
    bunker_qty_mt: float | None = None

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

    # Who filed the request (the online form's requestor block). Captured into
    # notes + raw with no dedicated columns — same treatment as agency/flag.
    # ``signature`` is the online form's typed/drawn acceptance; kept in raw for
    # audit and not surfaced on the operator entry form.
    requestor_name: str | None = None
    requestor_email: str | None = None
    requestor_phone: str | None = None
    signature: str | None = None

    # Free-text note carried onto the reservation. The hand-entry form leaves
    # this blank; the AI-assisted path (app/intake/llm.py) uses it to surface the
    # model's confidence/caveats ("LLM: ETD 'TBA' -> null") so an operator knows
    # which auto-parsed cards to eyeball. Appended verbatim to reservation.notes.
    notes: str | None = None

    # The verbatim upstream payload when this request was derived from another
    # system (the AI path sets it to the original Dataverse row). It rides into
    # ``intake_event.raw`` via ``model_dump`` so the pre-normalization input is
    # preserved — "never skip the raw landing" — and folds into the dedupe hash,
    # so the same source row is idempotent. The hand-entry form leaves it None.
    source_raw: dict | None = None

    @field_validator("imo")
    @classmethod
    def _check_imo(cls, v: int | None) -> int | None:
        # Reject a structurally impossible IMO (wrong length or bad check digit)
        # at the transport layer so a typo never lands as a real vessel key.
        if v is not None and not valid_imo(v):
            raise ValueError(
                f"{v} is not a valid IMO number (must be 7 digits with a "
                "correct check digit)"
            )
        return v

    @field_validator("bunker_type")
    @classmethod
    def _norm_bunker_type(cls, v: str | None) -> str | None:
        # Normalize the choice to its uppercase code (e.g. "mgo" -> "MGO"); an
        # unrecognized value is kept verbatim and warned about in normalize_form,
        # not rejected (a bad fuel grade is harmless, unlike a bad IMO key).
        if v is None:
            return None
        return v.strip().upper() or None


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


def _bunker_summary(form: BerthRequestForm) -> str:
    """One-line bunkering description for notes: ``bunkers: no`` when not taking
    bunkers; otherwise ``bunkers: yes`` plus type (full label), approximate
    metric tons, and whether the acknowledgement was given."""
    if not form.bunkers:
        return "bunkers: no"
    detail: list[str] = []
    if form.bunker_type:
        detail.append(BUNKER_TYPES.get(form.bunker_type, form.bunker_type))
    if form.bunker_qty_mt is not None:
        detail.append(f"~{form.bunker_qty_mt:g} MT")
    if form.bunkering_acknowledged:
        detail.append("acknowledged")
    return "bunkers: yes" + (f" ({', '.join(detail)})" if detail else "")


def _notes(form: BerthRequestForm, source: str, warnings: list[str]) -> str:
    """Assemble human-readable notes from the fields without dedicated columns
    (agent, requestor, flag, line, DWT, bunkers, origin/destination, berth
    status). One labelled item per line — newline-separated, not a semicolon
    run-on — so it reads as a list. The UI renders reservation notes with
    ``white-space:pre-line``, so the line breaks show through."""
    lines = [f"manual entry ({source})"]
    if form.agency:
        lines.append(f"agent: {form.agency}")
    requestor = ", ".join(
        b for b in (form.requestor_name, form.requestor_email, form.requestor_phone) if b
    )
    if requestor:
        lines.append(f"requestor: {requestor}")
    if form.ss_line:
        lines.append(f"line: {form.ss_line}")
    if form.flag:
        lines.append(f"flag: {form.flag}")
    if form.due_from or form.sail_for:
        lines.append(f"route: {form.due_from or '?'} → {form.sail_for or '?'}")
    if form.destinations:
        lines.append(f"destinations: {form.destinations}")
    if form.deadweight_lbs is not None:
        lines.append(f"DWT: {form.deadweight_lbs:g} lbs")
    lines.append(_bunker_summary(form))
    lines.append("berth UNASSIGNED (port to assign)")
    if form.notes:
        lines.append(form.notes.strip())
    if warnings:
        lines.append("warnings: " + "; ".join(warnings))
    return "\n".join(lines)


def normalize_form(form: BerthRequestForm) -> NormalizedRequest:
    """Pure normalization: structured form -> what the DB stores, plus warnings.
    No database access — unit-testable on its own."""
    warnings: list[str] = []

    source = form.source if form.source in ACCEPTED_SOURCES else _DEFAULT_SOURCE
    if form.source not in ACCEPTED_SOURCES:
        warnings.append(f"unknown source {form.source!r}; recorded as {source!r}")

    if not form.vessel:
        warnings.append("missing vessel name")
    if form.imo is None:
        warnings.append("missing IMO — vessel not keyed; reconcile by hand")
    if form.etb is None:
        warnings.append("missing arrival date (ETB) — no time window")
    if form.bunker_type and form.bunker_type not in BUNKER_TYPES:
        warnings.append(
            f"unknown bunker type {form.bunker_type!r}; expected one of "
            f"{', '.join(BUNKER_TYPES)}"
        )

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
def _upsert_vessel(
    session: Session, req: NormalizedRequest, *, overwrite: bool = False
) -> int | None:
    """Find-or-create a vessel by IMO (IMO isn't a unique key in this schema —
    AIS keys on MMSI — so we look it up explicitly).

    ``overwrite`` chooses how an *existing* vessel's columns are merged:

    - ``False`` (default, the NULL-fill path): never clobber existing detail —
      only fill columns that are currently NULL (``COALESCE(existing, new)``),
      leaving AIS-sourced (and any prior) dimensions in place.
    - ``True`` (the authoritative path): a *provided* value wins
      (``COALESCE(new, existing)``). A field the operator left blank (``new`` is
      NULL) still keeps the existing value — a request never wipes a stored
      dimension.

    The caller decides which applies (see ``record_manual_request`` /
    ``update_manual_request``): an explicit edit, or a create that resolves to
    the *same* manual ship, is authoritative; an AIS-tracked vessel is left
    NULL-fill so its live dimensions stay authoritative."""
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
        merged = (
            # overwrite: new wins when provided, else keep existing.
            {k: func.coalesce(v, getattr(Vessel, k)) for k, v in fields.items()}
            if overwrite
            # default: keep what we already know, only fill NULLs.
            else {k: func.coalesce(getattr(Vessel, k), v) for k, v in fields.items()}
        )
        session.execute(
            update(Vessel)
            .where(Vessel.id == existing)
            .values(**merged, updated_at=func.now())
        )
        return existing

    return int(
        session.execute(
            insert(Vessel).values(imo=req.imo, **fields).returning(Vessel.id)
        ).scalar_one()
    )


def _reproject_placements(session: Session, vessel_id: int | None) -> None:
    """Re-derive any **bow-placed planned** reservation's station range from the
    vessel's (now possibly corrected) LOA, holding the bow fixed.

    A reservation placed from the bow stores ``[stern, bow]`` where the LOA set
    the span (``app/edit.py:_station_from_bow``): upstream keeps the bow at the
    upper bound and the stern below, downstream keeps the bow at the lower bound
    and the stern above. When an authoritative request overwrites the vessel's
    LOA, the footprint must follow — otherwise the map keeps drawing the length
    captured at placement time (the "always 1000 ft" bug).

    Only planned, bow-placed rows are touched. A berth-assigned row (``berth_id``
    set) takes its span from the berth; an unplaced row has an empty range; an
    un-oriented row (no ``direction``) can't be re-derived; an ``observed`` AIS
    row is real measured occupancy — none are LOA-driven. The LOA store is metres
    (``vessel.loa``); the range is feet, so we scale by ``FEET_PER_M`` in SQL.

    A longer footprint that now collides with a ``confirmed`` row surfaces as a
    409 at commit (the no-overlap exclusion constraint), which is the intended
    signal — never pre-empted here. Does NOT commit."""
    if vessel_id is None:
        return
    session.execute(
        text(
            """
            UPDATE reservation
               SET station_range = CASE reservation.direction
                   WHEN 'upstream' THEN numrange(
                       upper(reservation.station_range)
                           - CAST(v.loa * :ftpm AS numeric),
                       upper(reservation.station_range), '[]')
                   WHEN 'downstream' THEN numrange(
                       lower(reservation.station_range),
                       lower(reservation.station_range)
                           + CAST(v.loa * :ftpm AS numeric), '[]')
                   END
              FROM vessel v
             WHERE reservation.vessel_id = v.id
               AND v.id = :vid
               AND v.loa IS NOT NULL
               AND reservation.status IN ('tentative', 'confirmed')
               AND reservation.berth_id IS NULL
               AND reservation.direction IS NOT NULL
               AND NOT isempty(reservation.station_range)
            """
        ),
        {"vid": vessel_id, "ftpm": FEET_PER_M},
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


def _norm_name(name: str | None) -> str | None:
    """Normalize a vessel name for *identity* comparison only (case-fold +
    collapse whitespace). Storage keeps the operator's original casing; this is
    just to tell "same ship" from "different ship" when an IMO is reused."""
    if name is None:
        return None
    collapsed = " ".join(name.split())
    return collapsed.casefold() or None


def _resolve_imo_vessel(session: Session, req: NormalizedRequest) -> bool:
    """Decide how a create should treat the vessel an IMO resolves to, enforcing
    that an IMO identifies **exactly one ship**.

    Returns ``overwrite`` for ``_upsert_vessel``:

    - No IMO, or IMO not yet on file → ``False`` (a fresh insert; nothing to
      overwrite).
    - IMO already on file for the **same ship** (the stored name matches, or
      either side is unnamed) → ``True`` for a manual-only vessel (no MMSI), so a
      re-submitted request with a corrected LOA/dims takes effect (and the
      footprint re-projects); ``False`` for an AIS-tracked vessel (has MMSI), to
      keep its live dimensions authoritative.
    - IMO already on file for a **different ship** (a different stored name) →
      raise ``ValueError`` → the endpoint returns it as a 4xx. This is the guard
      the operator asked for: two ships may not share an IMO. The fix is to use
      the right IMO (or correct the existing record via the request's Edit)."""
    if req.imo is None:
        return False
    existing = session.execute(
        select(Vessel.name, Vessel.mmsi).where(Vessel.imo == req.imo).limit(1)
    ).first()
    if existing is None:
        return False  # new IMO → plain insert

    cur_name, new_name = _norm_name(existing.name), _norm_name(req.vessel_name)
    if cur_name and new_name and cur_name != new_name:
        raise ValueError(
            f"IMO {req.imo} is already on file as {existing.name!r}; two ships "
            f"can't share an IMO. Use this ship's IMO, give the new ship its own "
            f"IMO, or correct the existing record via its Edit button."
        )
    # Same ship: a manual-only record is the operator's to correct; an
    # AIS-tracked vessel keeps its dimensions authoritative (NULL-fill only).
    return existing.mmsi is None


def _ais_overrides(
    session: Session, req: NormalizedRequest
) -> tuple[str | None, list[tuple[str, float, float]]]:
    """Dimensions an **AIS-tracked** vessel (has MMSI) overrides on this request.

    Returns ``(vessel_name, [(label, entered_m, ais_m), ...])`` — one entry per
    draft/LOA/beam the operator provided that differs from the stored AIS value.
    Empty when the IMO is new, the ship is manual-only (no MMSI), or nothing
    differs. AIS is authoritative for such a ship: the create only NULL-fills
    (``_resolve_imo_vessel`` -> ``overwrite=False``) and the ingestor overwrites by
    MMSI on every ``ShipStaticData`` — so these entered values are dropped. The
    caller both **warns** and **records** the discrepancy (below) so the override is
    apparent, not silent (the "I set 20 ft but it shows 30 ft" case)."""
    if req.imo is None:
        return None, []
    row = session.execute(
        select(Vessel.name, Vessel.mmsi, Vessel.loa, Vessel.beam, Vessel.draft)
        .where(Vessel.imo == req.imo)
        .limit(1)
    ).first()
    if row is None or row.mmsi is None:
        return None, []  # a new IMO or a manual-only ship — the entered value applies
    name = row.name or f"IMO {req.imo}"
    diffs: list[tuple[str, float, float]] = []
    for label, provided_m, stored in (
        ("Draft", req.draft_m, row.draft),
        ("LOA", req.loa_m, row.loa),
        ("Beam", req.beam_m, row.beam),
    ):
        if provided_m is None or stored is None:
            continue
        if abs(float(provided_m) - float(stored)) < 0.05:
            continue  # they entered ~the AIS value; nothing was overridden
        diffs.append((label, float(provided_m), float(stored)))
    return name, diffs


def _override_warnings(name: str, diffs: list[tuple[str, float, float]]) -> list[str]:
    """Verbose per-dimension operator warnings (feet) — shown once at submit."""
    return [
        f"{label} {ent * FEET_PER_M:.1f} ft not applied — {name} is AIS-tracked, so "
        f"AIS stays authoritative for its dimensions ({ais * FEET_PER_M:.1f} ft). "
        f"Correct it at the AIS source, not here — the live feed overwrites manual "
        f"dimensions."
        for label, ent, ais in diffs
    ]


def _override_note(diffs: list[tuple[str, float, float]]) -> str:
    """Compact, durable note (feet) stored on the reservation, so the override
    stays apparent on the request card and in History — not just a submit-time
    warning that scrolls away."""
    parts = ", ".join(
        f"{label.lower()} {ent * FEET_PER_M:.1f}→{ais * FEET_PER_M:.1f} ft"
        for label, ent, ais in diffs
    )
    return f"[AIS override] entered {parts}; AIS values kept (authoritative)."


def record_manual_request(session: Session, form: BerthRequestForm) -> dict:
    """Land a manual berth request: ``intake_event`` (raw, deduped) + a
    ``requested`` reservation (+ vessel upsert), all in one transaction. Does
    NOT commit — the caller (the endpoint) owns the transaction boundary.

    Idempotent: an identical re-submission (same raw payload) is deduped at the
    ``intake_event`` level and creates no second reservation.

    Returns a summary dict for the API response.
    """
    req = normalize_form(form)

    # 0. Refuse a content-empty submission. With nothing to key, schedule, or
    #    even name a vessel by, landing it would only create a blank
    #    berth-request card (and a stray audit row) — never what an operator
    #    wants. The form marks vessel/imo/draft required, so this only catches a
    #    stray/empty POST; a real request always carries at least one of these.
    if req.vessel_name is None and req.imo is None and req.etb is None:
        return {
            "skipped": True,
            "duplicate": False,
            "intake_event_id": None,
            "reservation_id": None,
            "vessel_id": None,
            "warnings": req.warnings + ["empty request — nothing recorded"],
        }

    # 0b. Enforce one ship per IMO. If this IMO is already on file under a
    #    different ship's name, refuse before landing anything (the operator
    #    likely mistyped the IMO); otherwise learn whether the matched ship is
    #    ours to overwrite. Raised here, pre-landing, so a rejected request
    #    leaves no orphan audit row.
    overwrite_vessel = _resolve_imo_vessel(session, req)
    # An AIS-tracked ship's dimensions are authoritative, so an entered draft/LOA/
    # beam that differs is dropped. Make that apparent, not silent: warn the
    # operator now AND stamp the discrepancy onto the reservation's notes, so it
    # shows on the request card and in History (the entered value also stays
    # verbatim in intake_event.raw).
    _ov_name, _ov_diffs = _ais_overrides(session, req)
    if _ov_diffs:
        req.warnings.extend(_override_warnings(_ov_name, _ov_diffs))
        _ov_note = _override_note(_ov_diffs)
        req.notes = f"{req.notes}\n{_ov_note}" if req.notes else _ov_note

    # 1. Land the raw request. ON CONFLICT DO NOTHING on the content hash makes a
    #    duplicate submission a no-op; if nothing lands, don't create a second
    #    reservation either.
    key = dedupe_key(req.raw)
    intake_id = session.execute(
        pg_insert(IntakeEvent)
        .values(source=req.source, raw=req.raw, dedupe_key=key, processed=False)
        .on_conflict_do_nothing(
            # Must match the partial unique index predicate exactly (migration
            # 0010): a *live* row's dedupe_key is unique, but a soft-deleted row
            # (deleted_at set) is excluded — so re-submitting content that was
            # deleted lands a fresh request rather than silently no-op'ing.
            index_elements=["dedupe_key"],
            index_where=and_(
                IntakeEvent.dedupe_key.isnot(None),
                IntakeEvent.deleted_at.is_(None),
            ),
        )
        .returning(IntakeEvent.id)
    ).scalar_one_or_none()

    if intake_id is None:
        # A conflict means a *live* row already holds this content; surface it
        # (not a soft-deleted one, which the partial index ignores).
        existing = session.execute(
            select(IntakeEvent.id, IntakeEvent.reservation_id).where(
                IntakeEvent.dedupe_key == key,
                IntakeEvent.deleted_at.is_(None),
            )
        ).first()
        return {
            "duplicate": True,
            "intake_event_id": existing.id if existing else None,
            "reservation_id": existing.reservation_id if existing else None,
            "vessel_id": None,
            "warnings": req.warnings,
        }

    # 2/3. Upsert the vessel and create the requested reservation. ``overwrite``
    #    is True only when the IMO resolved to the *same* manual ship (so a
    #    corrected LOA/dims takes), never to a different ship (that was refused
    #    above) nor an AIS-tracked one (its dimensions stay authoritative).
    vessel_id = _upsert_vessel(session, req, overwrite=overwrite_vessel)
    # A corrected LOA must reach any already-placed reservation's footprint, not
    # just the vessel row.
    _reproject_placements(session, vessel_id)
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
    """Withdraw a manual berth request: **soft-delete** the raw ``intake_event``
    row (stamp ``deleted_at``) and drop the ``requested`` reservation it
    projected. An operator removing a phoned/emailed request that was mistaken or
    withdrawn.

    The raw row is kept, not erased: ``intake_event`` is "the audit +
    reconciliation trail", so the evidence that a request ever arrived must
    survive a delete (migration 0010). It just stops appearing in the live API
    (``GET /intake/berth-requests`` filters ``deleted_at IS NULL``) and no longer
    blocks a re-submission of the same content (the dedupe index is partial on
    ``deleted_at IS NULL``). The projected reservation IS hard-dropped — a
    withdrawn request is no longer scheduled — and its id is returned (-> the
    audit_log) so the trail still names what was removed.

    Restricted to editable channels (``phone|email|operator|ai``); the immutable
    online-form export cannot be deleted here. Does NOT commit — the endpoint owns
    the transaction boundary. Raises ``LookupError`` if the event doesn't exist or
    was already deleted (-> 404) and ``ValueError`` if it isn't editable (-> 422).
    """
    existing = session.execute(
        select(
            IntakeEvent.source,
            IntakeEvent.reservation_id,
            IntakeEvent.deleted_at,
            IntakeEvent.raw,
        ).where(IntakeEvent.id == intake_id)
    ).first()
    if existing is None:
        raise LookupError(f"intake_event {intake_id} not found")
    if existing.deleted_at is not None:
        raise LookupError(f"intake_event {intake_id} was already deleted")
    if existing.source not in EDITABLE_SOURCES:
        raise ValueError(
            f"only editable-channel requests ({', '.join(EDITABLE_SOURCES)}) "
            f"can be deleted, not {existing.source!r}"
        )

    # Soft-delete the raw audit row (keep it + its verbatim payload). Then drop
    # the projected reservation; the intake_event -> reservation FK is ON DELETE
    # SET NULL, so dropping it clears the link on the now-archived row — the id is
    # preserved in the return (-> audit detail) so the trail still names it.
    session.execute(
        update(IntakeEvent)
        .where(IntakeEvent.id == intake_id)
        .values(deleted_at=func.now())
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
        # Pre-delete raw, preserved for the audit_log even though the soft-deleted
        # row still holds it (keeps the audit entry self-contained).
        "prior_raw": existing.raw,
    }


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
            IntakeEvent.id,
            IntakeEvent.source,
            IntakeEvent.reservation_id,
            IntakeEvent.raw,
            IntakeEvent.deleted_at,
        ).where(IntakeEvent.id == intake_id)
    ).first()
    if existing is None:
        raise LookupError(f"intake_event {intake_id} not found")
    if existing.deleted_at is not None:
        raise LookupError(f"intake_event {intake_id} was already deleted")
    if existing.source not in EDITABLE_SOURCES:
        raise ValueError(
            f"only editable-channel requests ({', '.join(EDITABLE_SOURCES)}) "
            f"can be edited, not {existing.source!r}"
        )

    # The operator edit form has no inputs for the provenance-only fields
    # (``source_raw`` — the verbatim upstream row — ``signature``, and the AI
    # ``notes`` caveat), so a PATCH omits them. Carry them forward from the stored
    # raw rather than wiping an AI card's verbatim Dataverse payload and
    # confidence note on the first sanctioned edit.
    prior = existing.raw or {}
    if form.source_raw is None:
        form.source_raw = prior.get("source_raw")
    if form.signature is None:
        form.signature = prior.get("signature")
    if form.notes is None:
        form.notes = prior.get("notes")

    req = normalize_form(form)
    key = dedupe_key(req.raw)

    # Replace the raw payload + its content hash in place. A collision with
    # another row's hash trips the unique index -> IntegrityError -> 409.
    session.execute(
        update(IntakeEvent)
        .where(IntakeEvent.id == intake_id)
        .values(source=req.source, raw=req.raw, dedupe_key=key)
    )

    # An explicit operator edit is authoritative: a value the operator supplies
    # (e.g. a corrected name or LOA) overwrites the vessel row, so the change
    # propagates to the reservation view — unlike the NULL-fill-only create path.
    vessel_id = _upsert_vessel(session, req, overwrite=True)
    # An authoritative LOA edit must re-derive any already-placed footprint, so
    # the map stops drawing the length captured at placement time.
    _reproject_placements(session, vessel_id)
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
        # Pre-edit raw, for the audit_log: the edit overwrites raw in place (the one
        # sanctioned mutation), so the prior payload only survives if captured here.
        "prior_raw": prior,
    }
