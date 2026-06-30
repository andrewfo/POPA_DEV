"""LLM-assisted normalization of a messy berth-request row into a structured
``BerthRequestForm``.

This is the AI-assisted intake path. A free-text / partially-structured request
(today: a row from the Power Pages "Berth Request" Dataverse table) is handed to
a cheap LLM — via **OpenRouter** (default Google Gemini Flash) — which extracts
the canonical fields. The model only **proposes**: the resulting
``BerthRequestForm`` flows through the *same* ``record_manual_request()``
pipeline as a hand-typed request, so the raw payload still lands in
``intake_event`` (deduped) and the projected reservation is a low-stakes
``requested`` row with an **empty station range** — it never places a vessel and
never trips the confirmed-only exclusion constraint. An operator reconciles
(assigns a berth, promotes to ``confirmed``) exactly as before. AI proposes; the
deterministic layer + the operator dispose.

Design notes:

- The LLM emits a permissive :class:`LlmExtraction` (it is told the schema in the
  prompt and asked for a JSON object). It is deliberately **tolerant** — a bad
  IMO or an ambiguous ``"X or Y"`` / ``"TBA"`` becomes ``null`` + a parse note,
  never a hard failure that loses the request.
- :func:`to_form` maps the extraction onto ``BerthRequestForm``. The submission
  is always in **feet/lbs** (US units), so the prompt tells the model to copy
  measurements through unchanged — no unit conversion — which matches the form's
  feet contract; ``record_manual_request`` does the feet → metres store. A
  checksum-invalid IMO is dropped (with a note) rather than raising.
- The network call is isolated behind a ``complete`` callable, so the prompt and
  mapping logic are unit-testable with a fake LLM (no key, no network). The live
  caller is :func:`openrouter_complete`, a thin ``httpx`` POST (no new
  dependency — ``httpx`` is already in the stack).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field

import httpx
from pydantic import BaseModel, ConfigDict, model_validator

from app.intake.manual import BUNKER_TYPES, BerthRequestForm, valid_imo

logger = logging.getLogger("app.intake.llm")

# The fields we ask the model to extract. Kept to the canonical berth-request
# columns ``BerthRequestForm`` actually carries; everything optional so a sparse
# request still parses. ``extra="ignore"`` so a model that volunteers an extra
# key never breaks validation.
class LlmExtraction(BaseModel):
    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _drop_nulls(cls, data):
        # The system prompt tells the model to emit ``null`` for absent/ambiguous
        # values. Most fields are Optional and accept it, but the few
        # non-Optional ones (``bunkers``, ``bunkering_acknowledged``,
        # ``confidence``) would raise a ValidationError on an explicit ``null`` —
        # which ``extraction_from_json`` then swallows, discarding the *entire*
        # otherwise-good extraction. Strip null-valued keys up front so every
        # such field falls back to its declared default instead.
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if v is not None}
        return data

    vessel: str | None = None
    imo: int | None = None
    # Dimensions in FEET (the form is feet/lbs; the prompt forbids unit conversion).
    length_ft: float | None = None
    beam_ft: float | None = None
    draft_ft: float | None = None
    # ISO-8601 strings (date or datetime); parsed tolerantly in to_form.
    etb: str | None = None
    etd: str | None = None
    inbound_cargo: str | None = None
    inbound_tons: float | None = None
    outbound_cargo: str | None = None
    outbound_tons: float | None = None
    agency: str | None = None
    ss_line: str | None = None
    flag: str | None = None
    destinations: str | None = None
    deadweight_lbs: float | None = None
    due_from: str | None = None
    sail_for: str | None = None
    # Bunkering (the request form's bunker block). bunker_type is one of the
    # BUNKER_TYPES codes; bunker_qty_mt is in metric tons (NOT feet/net tons).
    bunkers: bool = False
    bunker_type: str | None = None
    bunker_qty_mt: float | None = None
    bunkering_acknowledged: bool = False
    # Who filed the request.
    requestor_name: str | None = None
    requestor_email: str | None = None
    requestor_phone: str | None = None
    # The model's self-assessed confidence (0..1) and any caveats it wants to
    # flag (ambiguity it resolved to null, a unit it converted, a guess it
    # refused to make). Surfaced to the operator, never acted on automatically.
    confidence: float = 0.0
    parse_notes: str | None = None


@dataclass
class ParseResult:
    """The outcome of one LLM parse: a ready-to-record form plus the metadata an
    operator/worker wants for triage."""

    form: BerthRequestForm
    extraction: LlmExtraction
    confidence: float
    notes: list[str] = field(default_factory=list)


# --- prompt ----------------------------------------------------------------
# The allowed bunker-grade codes, sourced from the one shared catalogue in
# app/intake/manual so the prompt, the form, and normalize_form's validation
# never drift. Computed once at import, so SYSTEM_PROMPT stays a static string
# the provider can cache across calls.
_BUNKER_CODES = ", ".join(BUNKER_TYPES)

# Static so the provider can cache it across calls. Describes the task, the
# canonical units/timezone rules, and the anti-hallucination guardrails that the
# messy legacy text demands (CLAUDE.md: "X or Y", TBA, inline CANCELLED/?).
SYSTEM_PROMPT = f"""\
You normalize berth-request submissions for the Port of Port Arthur into a \
single JSON object. A submission is often messy or partial (free text, missing \
fields, inconsistent units). Extract only what is actually present.

Return a JSON object with these keys (use null when a value is absent or \
ambiguous):
- vessel: ship name (string)
- imo: IMO number as an integer ONLY if a 7-digit IMO is explicitly present; \
never invent or guess one
- length_ft, beam_ft, draft_ft: vessel dimensions in FEET. The submission ALWAYS \
provides measurements in US units (feet and pounds), so copy the numeric values \
through unchanged — do NOT convert units, and do NOT mention any unit conversion \
in parse_notes. Ignore any unit hint in a field name (e.g. a trailing "m" as in \
"beamm"/"draftm"): those values are already feet, not metres
- etb: requested arrival as an ISO-8601 string (date "YYYY-MM-DD" or datetime \
"YYYY-MM-DDTHH:MM"); etd: requested departure, same format
- inbound_cargo, outbound_cargo: short cargo descriptions; inbound_tons, \
outbound_tons: numeric tonnages if given
- agency: the requesting agent/agency; ss_line: steamship line; flag: vessel \
flag state; destinations: listed destination(s); deadweight_lbs: deadweight in \
pounds if given; due_from: origin; sail_for: destination
- bunkers: true if the vessel is taking on bunker fuel, else false; bunker_type: \
one of {_BUNKER_CODES} (the fuel grade, by code) or null; \
bunker_qty_mt: approximate bunker fuel quantity in METRIC TONS; \
bunkering_acknowledged: true if a bunkering acknowledgement was given, else false
- requestor_name, requestor_email, requestor_phone: who filed the request
- confidence: your overall confidence 0.0-1.0 that the extraction is correct
- parse_notes: a short string noting anything ambiguous, converted, or dropped \
(e.g. "ETD was 'TBA' -> null", "length given in metres, converted")

Rules:
- Local time is US Central. If a value is explicitly UTC, return an ISO string \
WITH a 'Z' or offset; otherwise return the local wall-clock time with NO offset.
- For ambiguous values like "X or Y", "TBA", "?", "CANCELLED", or blanks, use \
null and explain in parse_notes. Do not guess.
- Output ONLY the JSON object, no prose, no markdown fences.
"""

_EXAMPLE_INPUT = {
    "Vessel Name": "Chem Orchard",
    "Notes": "abt 607' loa, due Beaumont 6/16 am, sail TBA, sulphur ~9000mt in",
    "IMO": "",
}
_EXAMPLE_OUTPUT = {
    "vessel": "Chem Orchard",
    "imo": None,
    "length_ft": 607.0,
    "beam_ft": None,
    "draft_ft": None,
    "etb": "2026-06-16T08:00",
    "etd": None,
    "inbound_cargo": "sulphur",
    "inbound_tons": 9000.0,
    "outbound_cargo": None,
    "outbound_tons": None,
    "agency": None,
    "ss_line": None,
    "flag": None,
    "destinations": None,
    "deadweight_lbs": None,
    "due_from": "Beaumont",
    "sail_for": None,
    "bunkers": False,
    "bunker_type": None,
    "bunker_qty_mt": None,
    "bunkering_acknowledged": False,
    "requestor_name": None,
    "requestor_email": None,
    "requestor_phone": None,
    "confidence": 0.7,
    "parse_notes": "ETD 'TBA' -> null; no IMO present; tonnage assumed inbound",
}


def build_messages(raw: dict | str) -> list[dict]:
    """Assemble the chat messages for one request. Pure — no network. Includes a
    single worked example (messy input -> JSON) so the model has the shape and
    the guardrails in front of it."""
    payload = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": "Submission:\n" + json.dumps(_EXAMPLE_INPUT, ensure_ascii=False),
        },
        {"role": "assistant", "content": json.dumps(_EXAMPLE_OUTPUT, ensure_ascii=False)},
        {"role": "user", "content": "Submission:\n" + payload},
    ]


# --- parsing ---------------------------------------------------------------
def _strip_fences(text: str) -> str:
    """Tolerate a model that wraps JSON in ```...``` despite instructions."""
    s = text.strip()
    if s.startswith("```"):
        s = s[3:]
        if s[:4].lower() == "json":
            s = s[4:]
        if s.endswith("```"):
            s = s[:-3]
    return s.strip()


def _find_json_object(s: str) -> str | None:
    """Return the first balanced ``{...}`` substring, or ``None``. String-aware
    (braces inside quoted strings don't count), so it rescues a JSON object the
    model wrapped in prose ("Here is the result: {…}") or followed with a trailing
    explanation — a real failure mode for weaker models that ignore the
    JSON-object response format."""
    start = s.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return None


def extraction_from_json(text: str) -> tuple[LlmExtraction, list[str]]:
    """Parse the model's text into an :class:`LlmExtraction`. Never raises — junk
    or a non-object yields an empty extraction plus a note, so a bad LLM response
    degrades to "nothing extracted" rather than dropping the request.

    Two parse attempts before giving up: the stripped text as-is, then the first
    balanced ``{...}`` block dug out of it (rescues prose-wrapped JSON). On total
    failure the raw text is logged (truncated) so a silent drop is diagnosable."""
    stripped = _strip_fences(text)
    data = None
    for candidate in (stripped, _find_json_object(stripped)):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            data = parsed
            break
    if data is None:
        logger.warning("LLM did not return a parseable JSON object; raw=%r", text[:500])
        return LlmExtraction(), ["LLM did not return valid JSON; nothing extracted"]
    try:
        return LlmExtraction.model_validate(data), []
    except Exception:  # pragma: no cover - pydantic is tolerant here, but be safe
        logger.warning("LLM JSON did not match the expected shape; raw=%r", text[:500])
        return LlmExtraction(), ["LLM JSON did not match the expected shape"]


def _parse_iso(value: str | None) -> dt.datetime | None:
    """Tolerant ISO-8601 -> datetime. Accepts a full datetime or a bare date
    (lands at midnight). A trailing ``Z`` is normalized to ``+00:00`` so an
    explicitly-UTC value stays an aware instant (``BerthRequestForm`` / the tz
    module then keep the instant; a naive value is assumed Central downstream).
    Returns ``None`` on anything unparseable."""
    if not value:
        return None
    s = value.strip().replace("Z", "+00:00").replace("z", "+00:00")
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError:
        try:
            return dt.datetime.combine(dt.date.fromisoformat(s[:10]), dt.time.min)
        except ValueError:
            return None


def _scrub_unit_claims(parse_notes: str | None) -> str | None:
    """Drop any parse-note clause that claims a metre↔feet unit conversion.

    The berth-request form ALWAYS submits measurements in feet (the prompt forbids
    conversion), so a note asserting the source was in metres is provably false. It
    is a stubborn narration tic of the cheap model: it keeps the numeric value as
    feet (correct) yet still says it "converted from metres" — and prompt-level
    suppression does not reliably stop it. Stripping the clause deterministically
    keeps the false claim out of the operator-facing ``reservation.notes``. Clauses
    are the model's ``;``-separated fragments; any fragment mentioning metres/meters
    is removed (the form has no metres, so such a fragment is always wrong)."""
    if not parse_notes:
        return parse_notes
    kept = [
        c.strip()
        for c in parse_notes.split(";")
        if "metre" not in c.lower() and "meter" not in c.lower()
    ]
    return "; ".join(p for p in kept if p) or None


def to_form(ext: LlmExtraction, *, source: str) -> tuple[BerthRequestForm, list[str]]:
    """Map an extraction onto a ``BerthRequestForm``. Pure. A checksum-invalid IMO
    is dropped (with a note) rather than raising — the operator can supply the
    right IMO on reconciliation. Dimensions pass through as feet (the form is
    feet/lbs; the model is told not to convert units), and any false
    metre-conversion claim the model still narrates is scrubbed from the notes."""
    notes: list[str] = []
    parse_notes = _scrub_unit_claims(ext.parse_notes)
    if parse_notes:
        notes.append(f"LLM: {parse_notes}")

    imo = ext.imo
    if imo is not None and not valid_imo(imo):
        notes.append(f"LLM proposed IMO {imo} failed the checksum; dropped")
        imo = None

    etb = _parse_iso(ext.etb)
    if ext.etb and etb is None:
        notes.append(f"could not parse ETB {ext.etb!r}; dropped")
    etd = _parse_iso(ext.etd)
    if ext.etd and etd is None:
        notes.append(f"could not parse ETD {ext.etd!r}; dropped")

    # LlmExtraction's fields are a same-named subset of BerthRequestForm, so copy
    # them in bulk and override only the ones that differ: imo (checksum-dropped
    # above), etb/etd (parsed str -> datetime), and the LLM-only confidence/
    # parse_notes (which aren't form fields). This keeps a new request column from
    # having to be threaded through a hand-written mapping as well.
    carried = ext.model_dump(exclude={"imo", "etb", "etd", "confidence", "parse_notes"})
    form = BerthRequestForm(source=source, imo=imo, etb=etb, etd=etd, **carried)
    return form, notes


def parse_request(
    raw: dict | str,
    *,
    complete,
    source: str = "ai",
) -> ParseResult:
    """Run one request through the LLM and return a ready-to-record form.

    ``complete`` is a callable ``(messages) -> str`` returning the model's text;
    inject a fake in tests, or :func:`openrouter_complete` live. The mapping is
    pure and tolerant — see :func:`extraction_from_json` / :func:`to_form`."""
    messages = build_messages(raw)
    text = complete(messages)
    ext, jnotes = extraction_from_json(text)
    form, mnotes = to_form(ext, source=source)
    notes = jnotes + mnotes
    # Stamp the AI provenance + confidence + caveats onto the form's notes so they
    # ride into reservation.notes and an operator sees which auto-parsed cards to
    # eyeball. The card already says "manual entry (ai)" (its source tag); this
    # rides alongside.
    summary = f"AI-parsed (confidence {ext.confidence:.0%})"
    if notes:
        summary += " — " + "; ".join(notes)
    form.notes = summary
    return ParseResult(form=form, extraction=ext, confidence=ext.confidence, notes=notes)


# --- live OpenRouter call --------------------------------------------------
def openrouter_complete(
    *,
    api_key: str,
    model: str,
    base_url: str = "https://openrouter.ai/api/v1",
    timeout: float = 30.0,
):
    """Build a ``complete`` callable that POSTs to OpenRouter's OpenAI-compatible
    chat-completions endpoint. ``temperature=0`` for stable extraction; asks for a
    JSON object response. Raises on HTTP error (the worker decides retry/skip).

    Holds a single pooled ``httpx.Client`` so a batch of rows reuses one
    keep-alive connection instead of a fresh TLS handshake per call. The client
    lives for the worker's lifetime (process exit closes it)."""
    client = httpx.Client(timeout=timeout)

    def complete(messages: list[dict]) -> str:
        resp = client.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": messages,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                # Bound the response so a chatty/looping model can't truncate the
                # JSON mid-object (a truncated object => unparseable). The extraction
                # object is ~25 short fields; 1024 is comfortable headroom.
                "max_tokens": 1024,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    return complete
