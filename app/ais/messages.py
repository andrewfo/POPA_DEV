"""Normalized AIS message types + parsers.

The ingestion pipeline only ever sees these normalized objects, never a
provider's raw wire format. Each source (aisstream now, Marine Cadastre later)
is responsible for parsing its own format into ``AISPosition`` / ``AISStatic``;
that keeps the persistence path entirely source-agnostic.

Dimensions: AIS reports A/B/C/D offsets of the position reference from the
ship's extremities. LOA = A + B (bow + stern), beam = C + D (port + starboard),
all in metres.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

# AIS sentinel values that mean "not available".
_HEADING_NA = 511
_COG_NA = 360.0
_SOG_NA = 102.3


@dataclass
class AISPosition:
    mmsi: int
    lat: float
    lon: float
    sog: float | None = None
    cog: float | None = None
    heading: float | None = None
    nav_status: int | None = None
    msg_ts: dt.datetime | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class AISStatic:
    mmsi: int
    imo: int | None = None
    name: str | None = None
    callsign: str | None = None
    ship_type: int | None = None
    loa: float | None = None
    beam: float | None = None
    draft: float | None = None
    destination: str | None = None
    raw: dict = field(default_factory=dict)


def _clean_str(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip().rstrip("@").strip()  # AIS pads with '@'
    return s or None


def _parse_ts(value) -> dt.datetime | None:
    """aisstream MetaData.time_utc looks like
    '2024-01-19 10:30:00.123456789 +0000 UTC'. Parse leniently; None on failure.
    """
    if not value:
        return None
    s = str(value).strip()
    s = s.removesuffix(" UTC").strip()
    # Trim fractional seconds to 6 digits (Python max) if present.
    if "." in s:
        head, _, tail = s.partition(".")
        digits = ""
        for ch in tail:
            if ch.isdigit():
                digits += ch
            else:
                tail_rest = tail[len(digits):]
                break
        else:
            tail_rest = ""
        s = f"{head}.{digits[:6]}{tail_rest}"
    s = s.replace(" +0000", "+00:00").replace(" +00:00", "+00:00")
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError:
        return None


def parse_aisstream(envelope: dict) -> AISPosition | AISStatic | None:
    """Parse one aisstream.io message envelope into a normalized object.

    Returns None for message types we don't ingest.
    """
    msg_type = envelope.get("MessageType")
    meta = envelope.get("MetaData", {}) or {}
    body = envelope.get("Message", {}) or {}
    mmsi = meta.get("MMSI") or meta.get("MMSI_String")
    if mmsi is None:
        return None
    mmsi = int(mmsi)

    if msg_type == "PositionReport":
        pr = body.get("PositionReport", {}) or {}
        lat = pr.get("Latitude", meta.get("latitude"))
        lon = pr.get("Longitude", meta.get("longitude"))
        if lat is None or lon is None:
            return None
        sog = pr.get("Sog")
        cog = pr.get("Cog")
        heading = pr.get("TrueHeading")
        return AISPosition(
            mmsi=mmsi,
            lat=float(lat),
            lon=float(lon),
            sog=None if sog in (None, _SOG_NA) else float(sog),
            cog=None if cog in (None, _COG_NA) else float(cog),
            heading=None if heading in (None, _HEADING_NA) else float(heading),
            nav_status=pr.get("NavigationalStatus"),
            msg_ts=_parse_ts(meta.get("time_utc")),
            raw=envelope,
        )

    if msg_type == "ShipStaticData":
        sd = body.get("ShipStaticData", {}) or {}
        dim = sd.get("Dimension", {}) or {}
        a, b = dim.get("A"), dim.get("B")
        c, d = dim.get("C"), dim.get("D")
        loa = (a + b) if a is not None and b is not None else None
        beam = (c + d) if c is not None and d is not None else None
        imo = sd.get("ImoNumber")
        return AISStatic(
            mmsi=mmsi,
            imo=int(imo) if imo else None,
            name=_clean_str(sd.get("Name") or meta.get("ShipName")),
            callsign=_clean_str(sd.get("CallSign")),
            ship_type=sd.get("Type"),
            loa=float(loa) if loa else None,
            beam=float(beam) if beam else None,
            draft=(
                float(sd["MaximumStaticDraught"])
                if sd.get("MaximumStaticDraught")
                else None
            ),
            destination=_clean_str(sd.get("Destination")),
            raw=envelope,
        )

    return None
