"""Bow/stern geographic projection — pure math, no database.

AIS gives a single antenna position plus the ship's reference offsets A (antenna
to bow) and B (antenna to stern) and a heading. To turn that into a station
range we reconstruct the bow and stern points on the earth's surface, then hand
each to ``crosswalk.geo_to_station`` (done in ``derive.py``).

  bow   = antenna projected FORWARD  by A along heading
  stern = antenna projected AFT (heading + 180) by B

All distances are metres; bearings are degrees clockwise from true north. The
forward calculation uses the spherical earth model — at vessel scale (tens of
metres) the spherical/ellipsoidal difference is far below AIS position noise.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# Mean earth radius (metres), matches PostGIS spherical default closely enough
# for vessel-scale offsets.
_EARTH_R_M = 6371008.8

# Feet per metre — station ``M`` is in feet, AIS dimensions in metres. Only used
# for the no-heading fallback where we synthesize an LOA-wide range directly in
# station units; the normal path projects geographically and lets geo_to_station
# read feet straight off the measured line.
FEET_PER_M = 3.280839895


@dataclass(frozen=True)
class BowStern:
    """Reconstructed hull endpoints (WGS84) and how confident we are."""

    bow_lat: float
    bow_lon: float
    stern_lat: float
    stern_lon: float
    # False when heading/COG was missing and we had to assume the antenna is
    # amidships — the caller should flag the derived reservation low-confidence.
    confident: bool = True


def destination_point(
    lat: float, lon: float, bearing_deg: float, distance_m: float
) -> tuple[float, float]:
    """Point reached from (lat, lon) travelling ``distance_m`` along ``bearing_deg``.

    Standard spherical forward (direct) geodesic formula. Returns (lat, lon) in
    decimal degrees.
    """
    ang = distance_m / _EARTH_R_M
    brg = math.radians(bearing_deg)
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)

    sin_lat2 = math.sin(lat1) * math.cos(ang) + math.cos(lat1) * math.sin(ang) * math.cos(brg)
    lat2 = math.asin(max(-1.0, min(1.0, sin_lat2)))
    lon2 = lon1 + math.atan2(
        math.sin(brg) * math.sin(ang) * math.cos(lat1),
        math.cos(ang) - math.sin(lat1) * math.sin(lat2),
    )
    # Normalise longitude to [-180, 180).
    lon2_deg = (math.degrees(lon2) + 540.0) % 360.0 - 180.0
    return math.degrees(lat2), lon2_deg


def project_bow_stern(
    lat: float,
    lon: float,
    *,
    heading_deg: float | None,
    dim_a_m: float | None,
    dim_b_m: float | None,
    loa_m: float | None,
) -> BowStern:
    """Reconstruct bow & stern points from an antenna fix.

    ``dim_a_m`` / ``dim_b_m`` are the antenna->bow / antenna->stern offsets. When
    one is missing we fall back to a symmetric split of ``loa_m`` about the
    antenna. When ``heading_deg`` is missing we cannot orient the hull at all, so
    we return the antenna point for both ends and mark the result not confident
    (the caller synthesizes an LOA-wide station range instead).
    """
    if heading_deg is None:
        return BowStern(lat, lon, lat, lon, confident=False)

    # Resolve fore/aft offsets, falling back to a symmetric LOA split.
    half = (loa_m / 2.0) if loa_m else None
    a = dim_a_m if dim_a_m is not None else half
    b = dim_b_m if dim_b_m is not None else half
    if a is None or b is None:
        # No length info either — degenerate to the antenna point.
        return BowStern(lat, lon, lat, lon, confident=False)

    bow_lat, bow_lon = destination_point(lat, lon, heading_deg, a)
    stern_lat, stern_lon = destination_point(lat, lon, (heading_deg + 180.0) % 360.0, b)
    return BowStern(bow_lat, bow_lon, stern_lat, stern_lon, confident=True)
