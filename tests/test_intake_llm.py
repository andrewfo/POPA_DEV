"""LLM-assisted intake parsing tests.

All pure (no DB, no network): the LLM call is injected as a fake ``complete``
callable, so these exercise the prompt assembly, the tolerant JSON parsing, and
the extraction -> ``BerthRequestForm`` mapping. The live OpenRouter call
(``openrouter_complete``) and the Dataverse worker are not exercised here.
"""
from __future__ import annotations

import datetime as dt
import json

from app.intake.llm import (
    LlmExtraction,
    build_messages,
    extraction_from_json,
    parse_request,
    to_form,
)


def _fake(text: str):
    """A ``complete`` that ignores the messages and returns canned model text."""
    return lambda messages: text


# --- build_messages --------------------------------------------------------
def test_build_messages_includes_system_and_payload():
    msgs = build_messages({"Vessel Name": "SAGA ADVENTURE", "IMO": "9317406"})
    assert msgs[0]["role"] == "system"
    # the messy worked-example shapes the model; the real payload is last
    assert msgs[-1]["role"] == "user"
    assert "SAGA ADVENTURE" in msgs[-1]["content"]


def test_build_messages_accepts_raw_string():
    msgs = build_messages("free text request, abt 600 ft")
    assert "free text request" in msgs[-1]["content"]


# --- extraction_from_json (tolerant) ---------------------------------------
def test_extraction_parses_clean_json():
    ext, notes = extraction_from_json('{"vessel": "X", "imo": 9317406, "confidence": 0.9}')
    assert ext.vessel == "X"
    assert ext.imo == 9317406
    assert notes == []


def test_extraction_strips_code_fences():
    ext, notes = extraction_from_json('```json\n{"vessel": "Y"}\n```')
    assert ext.vessel == "Y"


def test_extraction_ignores_unknown_keys():
    ext, _ = extraction_from_json('{"vessel": "Z", "totally_made_up": 1}')
    assert ext.vessel == "Z"


def test_extraction_junk_degrades_to_empty():
    ext, notes = extraction_from_json("the vessel is the SAGA ADVENTURE")
    assert ext.vessel is None
    assert notes and "valid JSON" in notes[0]


def test_extraction_non_object_degrades_to_empty():
    ext, notes = extraction_from_json("[1, 2, 3]")
    assert ext.vessel is None
    assert notes


# --- to_form mapping -------------------------------------------------------
def test_to_form_passes_feet_through_and_parses_dates():
    ext = LlmExtraction(
        vessel="SAGA ADVENTURE",
        imo=9317406,
        length_ft=585.0,
        beam_ft=91.5,
        etb="2026-06-16T08:00",
        etd="2026-06-19",
        inbound_cargo="steel coils",
    )
    form, notes = to_form(ext, source="email")
    assert form.vessel == "SAGA ADVENTURE"
    assert form.imo == 9317406
    assert form.length_ft == 585.0
    assert form.etb == dt.datetime(2026, 6, 16, 8, 0)
    # a bare date lands at midnight
    assert form.etd == dt.datetime(2026, 6, 19, 0, 0)
    assert notes == []


def test_to_form_drops_bad_imo_with_note():
    # 9317407 fails the IMO checksum (valid is ...406)
    ext = LlmExtraction(vessel="X", imo=9317407)
    form, notes = to_form(ext, source="email")
    assert form.imo is None
    assert any("checksum" in n for n in notes)


def test_to_form_drops_unparseable_date_with_note():
    ext = LlmExtraction(vessel="X", etb="sometime next week")
    form, notes = to_form(ext, source="email")
    assert form.etb is None
    assert any("ETB" in n for n in notes)


def test_to_form_surfaces_model_parse_notes():
    ext = LlmExtraction(vessel="X", parse_notes="ETD was TBA -> null")
    _, notes = to_form(ext, source="email")
    assert any("TBA" in n for n in notes)


def test_to_form_keeps_utc_instant_aware():
    ext = LlmExtraction(vessel="X", etb="2026-06-16T13:00:00Z")
    form, _ = to_form(ext, source="email")
    assert form.etb is not None and form.etb.tzinfo is not None


# --- parse_request end-to-end (fake LLM) -----------------------------------
def test_parse_request_end_to_end_with_fake_llm():
    canned = json.dumps(
        {
            "vessel": "Chem Orchard",
            "imo": None,
            "length_ft": 607.0,
            "etb": "2026-06-16T08:00",
            "inbound_cargo": "sulphur",
            "confidence": 0.7,
            "parse_notes": "no IMO present",
        }
    )
    result = parse_request(
        {"Vessel Name": "Chem Orchard", "Notes": "abt 607' loa"},
        complete=_fake(canned),
        source="email",
    )
    assert result.form.vessel == "Chem Orchard"
    assert result.form.length_ft == 607.0
    assert result.form.source == "email"
    assert result.confidence == 0.7
    assert any("no IMO present" in n for n in result.notes)


def test_parse_request_survives_a_bad_llm_response():
    result = parse_request(
        {"Vessel Name": "X"}, complete=_fake("sorry, I can't help"), source="email"
    )
    # nothing extracted, but a form still exists and the failure is noted
    assert result.form.vessel is None
    assert result.notes
