"""Live-SharePoint (Microsoft Graph) intake tests.

All pure — they exercise the Graph→canonical-row remap and the parser, with no
network. The remap (``graph_item_to_row``) is what makes the live list reuse the
exact CSV pipeline: internal field names → display-name keys, native values →
CSV-shaped strings, then the unchanged ``parse_row``.
"""
from __future__ import annotations

import datetime as dt

from app.intake.records import _parse_date, parse_row
from app.intake.source import _stringify, graph_item_to_row


# A SharePoint list item's `fields` is keyed by *internal* column names, which
# differ from the display names parse_row expects; values arrive native-typed
# (numbers as numbers, dates as ISO 8601). This is a representative item.
GRAPH_FIELDS = {
    "Title": "SAGA ADVENTURE",
    "Vessel": "SAGA ADVENTURE",
    "IMO_x0020_Number": 9317406,
    "Port_x0020_Arrival_x0020_Date": "2026-06-16T07:00:00Z",
    "Port_x0020_Departure_x0020_Date": "2026-06-19T07:00:00Z",
    "Assigned_x0020_Berth": "Berth 4",
    "Length_x0020__x0028_feet_x0029_": 585.0,
    "Beam_x0020_Width": 91.5,
    "Draft_x0020__x0028_feet_x0029_": 31.1,
    "Agency_x002f_Owner": "ISS BEAUMONT",
    "AgreementStatus": "signed",
    "id": "42",  # Graph system field, no display-name mapping
}

COLUMN_MAP = {
    "Vessel": "Vessel",
    "IMO_x0020_Number": "IMO Number",
    "Port_x0020_Arrival_x0020_Date": "Port Arrival Date",
    "Port_x0020_Departure_x0020_Date": "Port Departure Date",
    "Assigned_x0020_Berth": "Assigned Berth",
    "Length_x0020__x0028_feet_x0029_": "Length (feet)",
    "Beam_x0020_Width": "Beam Width",
    "Draft_x0020__x0028_feet_x0029_": "Draft (feet)",
    "Agency_x002f_Owner": "Agency/Owner",
    "AgreementStatus": "AgreementStatus",
}


def test_stringify_drops_integral_float_dotzero():
    # A numeric IMO must not read as "9317406.0" (which parse_row flags noisy).
    assert _stringify(9317406.0) == "9317406"
    assert _stringify(91.5) == "91.5"
    assert _stringify(None) is None
    assert _stringify("  ") is None


def test_graph_item_remaps_internal_names_to_display_keys():
    row = graph_item_to_row(GRAPH_FIELDS, COLUMN_MAP)
    assert row["Vessel"] == "SAGA ADVENTURE"
    assert row["IMO Number"] == "9317406"
    assert row["Assigned Berth"] == "Berth 4"
    assert row["Length (feet)"] == "585"
    # Unmapped Graph system fields are preserved verbatim for the audit trail.
    assert row["id"] == "42"


def test_graph_row_feeds_parse_row_cleanly():
    rec = parse_row(graph_item_to_row(GRAPH_FIELDS, COLUMN_MAP))
    assert rec.vessel_name == "SAGA ADVENTURE"
    assert rec.imo == 9317406
    assert rec.assigned_berth == "Berth 4"
    assert rec.arrival_date == dt.date(2026, 6, 16)
    assert rec.departure_date == dt.date(2026, 6, 19)
    assert rec.length_ft == 585.0
    assert rec.beam_ft == 91.5
    assert rec.draft_ft == 31.1
    assert rec.agency == "ISS BEAUMONT"
    assert rec.agreement_status == "signed"
    # Clean Graph item → no parse warnings, and IMO read without the .0 noise.
    assert rec.warnings == []
    # raw is the canonical-keyed row (display names), so dedupe/audit are uniform.
    assert rec.raw["IMO Number"] == "9317406"


def test_parse_date_accepts_both_iso_and_us():
    w: list[str] = []
    assert _parse_date("2026-06-16T07:00:00Z", w, "arrival date") == dt.date(2026, 6, 16)
    assert _parse_date("2026-06-16", w, "arrival date") == dt.date(2026, 6, 16)
    assert _parse_date("6/16/2026", w, "arrival date") == dt.date(2026, 6, 16)
    assert w == []
    assert _parse_date("not a date", w, "arrival date") is None
    assert w and "unparseable arrival date" in w[0]
