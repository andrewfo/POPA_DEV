"""Tests for berth-request row parsing (pure — no database).

The export is hand-driven and messy; these pin the conservative behaviour:
clean values normalize, ambiguous values become None + a warning, and verbatim
input is always preserved.
"""
import datetime as dt

from app.intake.records import BerthRequest, parse_row


def _row(**over):
    base = {
        "Vessel": "SAGA ADVENTURE",
        "Port Arrival Date": "6/16/2022",
        "Port Departure Date": "6/19/2022",
        "Assigned Berth": "Berth 2",
        "Agency/Owner": "ISS BEAUMONT",
        "IMO Number": "9317406",
        "Length (feet)": "585",
        "Beam Width": "91.5",
        "Draft (feet)": "31.1",
        "Vessel Due From": "Tampa, Fl",
        "Vessel To Sail For": "Port Arthur, TX",
        "AgreementStatus": "SIGNED",
        "SenderInfo": "eric@example.com",
    }
    base.update(over)
    return base


def test_parse_clean_row():
    rec = parse_row(_row())
    assert isinstance(rec, BerthRequest)
    assert rec.vessel_name == "SAGA ADVENTURE"
    assert rec.imo == 9317406
    assert rec.assigned_berth == "Berth 2"
    assert rec.arrival_date == dt.date(2022, 6, 16)
    assert rec.departure_date == dt.date(2022, 6, 19)
    assert rec.length_ft == 585.0
    assert rec.beam_ft == 91.5
    assert rec.draft_ft == 31.1
    assert rec.agreement_status == "signed"
    assert rec.warnings == []
    assert rec.raw["Vessel"] == "SAGA ADVENTURE"  # verbatim preserved


def test_berth_canonicalized_with_spacing():
    assert parse_row(_row(**{"Assigned Berth": "berth3"})).assigned_berth == "Berth 3"


def test_blank_berth_is_none_no_warning():
    rec = parse_row(_row(**{"Assigned Berth": ""}))
    assert rec.assigned_berth is None
    assert rec.warnings == []


def test_cancelled_in_berth_column_flagged():
    rec = parse_row(_row(**{"Assigned Berth": "Cancelled"}))
    assert rec.assigned_berth is None
    assert any("Cancelled" in w for w in rec.warnings)


def test_ambiguous_berth_kept_with_warning():
    rec = parse_row(_row(**{"Assigned Berth": "Berth 1 & 2"}))
    assert rec.assigned_berth == "Berth 1 & 2"  # verbatim, not guessed
    assert any("non-canonical" in w for w in rec.warnings)


def test_junk_imo_becomes_none_with_warning():
    for junk in ("N/A", "NA", "n/a", "Test", "Barge", "ngs-104", "WDG4395"):
        rec = parse_row(_row(**{"IMO Number": junk}))
        assert rec.imo is None, junk
        assert any("no IMO" in w for w in rec.warnings), junk


def test_mmsi_not_mistaken_for_imo():
    # 9-digit MMSI must not be read as a 7-digit IMO.
    rec = parse_row(_row(**{"IMO Number": "MMSI: 367736640 (TUG) / N/A BARGE"}))
    assert rec.imo is None
    assert any("no IMO" in w for w in rec.warnings)


def test_imo_extracted_from_noisy_value():
    rec = parse_row(_row(**{"IMO Number": "9415777 / PHILADELPHIA O.N. 1215132"}))
    assert rec.imo == 9415777
    assert any("noisy" in w for w in rec.warnings)


def test_barge_tow_name_preserved_verbatim():
    # No naive '/' splitting — and 'M/V' prefix is not a separator.
    for name in ("tug FANNIN/barge HTCO3091", "M/V INDEPENDENCE", "Innovation 650/9"):
        rec = parse_row(_row(**{"Vessel": name}))
        assert rec.vessel_name == name


def test_missing_vessel_name_flagged():
    rec = parse_row(_row(**{"Vessel": ""}))
    assert rec.vessel_name is None
    assert any("missing vessel name" in w for w in rec.warnings)


def test_bad_date_is_none_with_warning():
    rec = parse_row(_row(**{"Port Arrival Date": "not a date"}))
    assert rec.arrival_date is None
    assert any("arrival date" in w for w in rec.warnings)


def test_typo_year_parsed_as_is_not_corrected():
    # '6/23/2029' is an obvious typo but we don't second-guess it here.
    rec = parse_row(_row(**{"Port Departure Date": "6/23/2029"}))
    assert rec.departure_date == dt.date(2029, 6, 23)
    assert rec.warnings == []


def test_length_with_units_and_quotes():
    assert parse_row(_row(**{"Length (feet)": "607'"})).length_ft == 607.0
    assert parse_row(_row(**{"Length (feet)": "1,200 ft"})).length_ft == 1200.0
