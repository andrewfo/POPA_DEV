"""Normalized berth-request records + a conservative CSV-row parser.

The SharePoint export is hand-driven and messy, exactly as CLAUDE.md warns:
barge/tow strings crammed into one ``Vessel`` field (``tug FANNIN/barge
HTCO3091``), junk IMO values (``N/A``, ``Test``, a callsign, ``MMSI: 367...``),
typo'd years, blank or ``Cancelled`` berths. Per the intake convention we keep
parsing **conservative**: the full row is preserved verbatim for
``intake_event.raw``, and any value we cannot read unambiguously becomes
``None`` plus a warning for the later reconciliation / human-review layer —
never a guess. Vessel-name de-tangling and request→reservation projection are
that later layer's job, not this parser's.

Source-agnostic in spirit: ``parse_row`` normalizes a plain ``dict`` keyed by
the SharePoint column names, so a future export with a different shape only has
to map its columns onto the same keys.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

# --- column names in the SharePoint export ---------------------------------
COL_VESSEL = "Vessel"
COL_ARRIVAL = "Port Arrival Date"
COL_DEPARTURE = "Port Departure Date"
COL_BERTH = "Assigned Berth"
COL_AGENCY = "Agency/Owner"
COL_IMO = "IMO Number"
COL_LENGTH = "Length (feet)"
COL_BEAM = "Beam Width"
COL_DRAFT = "Draft (feet)"
COL_ORIGIN = "Vessel Due From"
COL_DEST_SAIL = "Vessel To Sail For"
COL_DESTINATION = "Destination"
COL_INBOUND = "Inbound Cargo"
COL_OUTBOUND = "Outbound Cargo"
COL_STATUS = "AgreementStatus"
COL_SENDER = "SenderInfo"

# An IMO is exactly 7 digits; match one only when it stands alone (so a 9-digit
# MMSI or a longer official number never masquerades as an IMO).
_IMO_RE = re.compile(r"(?<!\d)(\d{7})(?!\d)")
_BERTH_RE = re.compile(r"berth\s*(\d+)\s*$", re.IGNORECASE)


@dataclass
class BerthRequest:
    """One normalized intake row. ``raw`` is the verbatim source row;
    ``warnings`` records every value we declined to interpret."""

    vessel_name: str | None = None
    imo: int | None = None
    assigned_berth: str | None = None
    agency: str | None = None
    arrival_date: dt.date | None = None
    departure_date: dt.date | None = None
    length_ft: float | None = None
    beam_ft: float | None = None
    draft_ft: float | None = None
    origin: str | None = None
    destination: str | None = None
    inbound_cargo: str | None = None
    outbound_cargo: str | None = None
    agreement_status: str | None = None  # 'signed' | 'cancelled' | None
    sender: str | None = None
    warnings: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


def _clean_str(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _parse_date(value, warnings: list[str], label: str) -> dt.date | None:
    """Parse the export's ``M/D/YYYY`` dates. Returns None (with a warning) on
    anything we can't read; obvious year typos are left for human review, not
    'corrected' here."""
    s = _clean_str(value)
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    warnings.append(f"unparseable {label}: {s!r}")
    return None


def _parse_imo(value, warnings: list[str]) -> int | None:
    s = _clean_str(value)
    if not s:
        return None
    m = _IMO_RE.search(s)
    if not m:
        warnings.append(f"no IMO in {s!r}")
        return None
    if not _IMO_RE.fullmatch(s):
        # e.g. "9415777 / PHILADELPHIA O.N. 1215132" — took the first 7-digit run.
        warnings.append(f"IMO extracted from noisy value {s!r}")
    return int(m.group(1))


def _parse_float(value, warnings: list[str], label: str) -> float | None:
    s = _clean_str(value)
    if not s:
        return None
    cleaned = re.sub(r"[,'\"]|ft\.?|feet", "", s, flags=re.IGNORECASE).strip()
    try:
        return float(cleaned)
    except ValueError:
        warnings.append(f"unparseable {label}: {s!r}")
        return None


def _normalize_berth(value, warnings: list[str]) -> str | None:
    """Canonicalize 'Berth N'. Blank → None. 'Cancelled' is a status leaking
    into the berth column → None + warning. Anything else ambiguous (e.g.
    'Berth 1 & 2') is kept verbatim with a warning for human review."""
    s = _clean_str(value)
    if not s:
        return None
    if s.lower() == "cancelled":
        warnings.append("berth column holds 'Cancelled' (status, not a berth)")
        return None
    m = _BERTH_RE.match(s)
    if m:
        return f"Berth {int(m.group(1))}"
    warnings.append(f"non-canonical berth {s!r}")
    return s


def _normalize_status(value) -> str | None:
    s = _clean_str(value)
    if not s:
        return None
    return s.strip().lower()


def parse_row(raw: dict) -> BerthRequest:
    """Normalize one export row into a ``BerthRequest`` (keeping ``raw`` intact)."""
    w: list[str] = []
    rec = BerthRequest(
        vessel_name=_clean_str(raw.get(COL_VESSEL)),
        imo=_parse_imo(raw.get(COL_IMO), w),
        assigned_berth=_normalize_berth(raw.get(COL_BERTH), w),
        agency=_clean_str(raw.get(COL_AGENCY)),
        arrival_date=_parse_date(raw.get(COL_ARRIVAL), w, "arrival date"),
        departure_date=_parse_date(raw.get(COL_DEPARTURE), w, "departure date"),
        length_ft=_parse_float(raw.get(COL_LENGTH), w, "length"),
        beam_ft=_parse_float(raw.get(COL_BEAM), w, "beam"),
        draft_ft=_parse_float(raw.get(COL_DRAFT), w, "draft"),
        origin=_clean_str(raw.get(COL_ORIGIN)),
        destination=_clean_str(raw.get(COL_DEST_SAIL) or raw.get(COL_DESTINATION)),
        inbound_cargo=_clean_str(raw.get(COL_INBOUND)),
        outbound_cargo=_clean_str(raw.get(COL_OUTBOUND)),
        agreement_status=_normalize_status(raw.get(COL_STATUS)),
        sender=_clean_str(raw.get(COL_SENDER)),
        raw=dict(raw),
    )
    if not rec.vessel_name:
        w.append("missing vessel name")
    rec.warnings = w
    return rec
