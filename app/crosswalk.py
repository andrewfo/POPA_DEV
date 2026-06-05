"""Stationing crosswalk — the ONE module all position I/O goes through.

Canonical position = POPA station, in feet, along the measured wharf
centerline (``M`` = POPA station). Three external stationing systems are affine
functions of POPA station:

    external_value = scale * popa_station + offset

Verified against the port's stationing crosswalk:
  * Corps/USACE : scale = 1,  offset = 12040.65   (constant offset, same dir)
  * Dock No.    : scale = -1, offset = 3365        (reversed)

The affine parameters are stored per ``wharf_segment`` (not hard-coded here) so
the math generalizes if a segment uses different params. ``AffineParams`` is the
in-memory carrier; ``DEFAULT_*`` are convenience defaults matching the port's
published crosswalk and the values seeded into the first segment.

Stationing notation note: "12+02" means 12*100 + 02 = 1202 ft; a leading sign
applies to the whole value, so "-12+02" = -1202 ft. Helpers ``parse_station``
and ``format_station`` convert to/from that notation.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

# Published POPA crosswalk defaults.
DEFAULT_CORPS_SCALE = 1.0
DEFAULT_CORPS_OFFSET = 12040.65
DEFAULT_DOCKNO_SCALE = -1.0
DEFAULT_DOCKNO_OFFSET = 3365.0


@dataclass(frozen=True)
class AffineParams:
    """external = scale * popa + offset (and the exact inverse)."""

    scale: float
    offset: float

    def __post_init__(self) -> None:
        if self.scale == 0:
            raise ValueError("affine scale must be non-zero (transform not invertible)")

    def from_popa(self, popa_station: float) -> float:
        """POPA station -> external system value."""
        return self.scale * popa_station + self.offset

    def to_popa(self, external_value: float) -> float:
        """External system value -> POPA station (exact inverse)."""
        return (external_value - self.offset) / self.scale


DEFAULT_CORPS = AffineParams(DEFAULT_CORPS_SCALE, DEFAULT_CORPS_OFFSET)
DEFAULT_DOCKNO = AffineParams(DEFAULT_DOCKNO_SCALE, DEFAULT_DOCKNO_OFFSET)


# --- Convenience wrappers against the published defaults -------------------
def popa_to_corps(popa_station: float, params: AffineParams = DEFAULT_CORPS) -> float:
    return params.from_popa(popa_station)


def corps_to_popa(corps_value: float, params: AffineParams = DEFAULT_CORPS) -> float:
    return params.to_popa(corps_value)


def popa_to_dockno(popa_station: float, params: AffineParams = DEFAULT_DOCKNO) -> float:
    return params.from_popa(popa_station)


def dockno_to_popa(dockno_value: float, params: AffineParams = DEFAULT_DOCKNO) -> float:
    return params.to_popa(dockno_value)


def _segment_affine_row(session, segment_id: int | None = None):
    """Load a wharf segment's affine columns (corps + Dock No.) in one query.

    Returns the row (with ``corps_scale/offset`` and ``dockno_scale/offset``) or
    ``None`` when no segment is seeded (e.g. a bare test DB), so callers fall
    back to the published defaults.
    """
    from sqlalchemy import text

    where = "WHERE id = :sid" if segment_id is not None else ""
    sql = (
        "SELECT corps_scale, corps_offset, dockno_scale, dockno_offset "
        f"FROM wharf_segment {where} ORDER BY id LIMIT 1"
    )
    params = {"sid": segment_id} if segment_id is not None else {}
    return session.execute(text(sql), params).first()


def segment_dockno_params(session, segment_id: int | None = None) -> AffineParams:
    """Load a wharf segment's Dock No. affine params (POPA <-> Dock No.) from the
    database, so callers convert through the per-segment crosswalk rather than
    hard-coding the published transform. Falls back to ``DEFAULT_DOCKNO`` when no
    segment is seeded (e.g. a bare test DB).

    Args:
        session: a SQLAlchemy Session bound to the database.
        segment_id: restrict to one ``wharf_segment``; otherwise the first
            segment (lowest id) is used — there is a single wharf face today.
    """
    row = _segment_affine_row(session, segment_id)
    if row is None:
        return DEFAULT_DOCKNO
    return AffineParams(float(row.dockno_scale), float(row.dockno_offset))


def segment_corps_params(session, segment_id: int | None = None) -> AffineParams:
    """Load a wharf segment's Corps/USACE affine params (POPA <-> Corps) from the
    database. Falls back to ``DEFAULT_CORPS`` when no segment is seeded. Mirror of
    ``segment_dockno_params`` — see it for the args."""
    row = _segment_affine_row(session, segment_id)
    if row is None:
        return DEFAULT_CORPS
    return AffineParams(float(row.corps_scale), float(row.corps_offset))


# --- Stationing notation (NN+NN feet) --------------------------------------
def parse_station(text: str) -> float:
    """Parse stationing notation like '12+02', '-12+02', '108+38.65' to feet.

    Accepts a plain numeric string too (e.g. '1202' -> 1202.0).
    """
    s = text.strip()
    if "+" not in s:
        return float(s)
    sign = 1.0
    if s[0] in "+-":
        if s[0] == "-":
            sign = -1.0
        s = s[1:]
    whole, _, frac = s.partition("+")
    return sign * (float(whole) * 100.0 + float(frac))


def format_station(feet: float) -> str:
    """Format feet as stationing notation, e.g. 1202.0 -> '12+02',
    -1202.0 -> '-12+02', 10838.65 -> '108+38.65'.

    The fractional "+NN" part is always at least two integer digits; decimals
    are shown only when present (trailing zeros trimmed).
    """
    sign = "-" if feet < 0 else ""
    mag = abs(feet)
    stations = int(mag // 100)
    # Round to hundredths of a foot (stationing precision) so float noise from
    # crosswalk arithmetic doesn't leak into the formatted string.
    remainder = round(mag - stations * 100, 2)
    if remainder >= 100.0:  # rounding pushed us into the next station
        stations += 1
        remainder -= 100.0
    if remainder == int(remainder):
        rem_str = f"{int(remainder):02d}"
    else:
        rem_str = f"{remainder:.2f}".zfill(5)  # 2 int digits, e.g. "38.65", "02.50"
    return f"{sign}{stations}+{rem_str}"


# --- Geo -> station ---------------------------------------------------------
def geo_to_station(session, lat: float, lon: float, segment_id: int | None = None):
    """Project a lat/lon onto the measured wharf centerline and return the
    interpolated POPA station (the ``M`` value at the closest point).

    Uses ``ST_InterpolatePoint(line_with_M, point)`` which returns the measure
    of the location on the line closest to the point — i.e. geo -> station for
    free, no extra math. Returns ``None`` if no matching segment exists.

    Args:
        session: a SQLAlchemy Session bound to a PostGIS database.
        lat, lon: WGS84 decimal degrees.
        segment_id: restrict to one ``wharf_segment``; otherwise the nearest
            segment's line is used.
    """
    from sqlalchemy import text

    where = "WHERE ws.id = :segment_id" if segment_id is not None else ""
    sql = f"""
        SELECT ST_InterpolatePoint(
                   ws.geom,
                   ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)
               ) AS station
        FROM wharf_segment ws
        {where}
        ORDER BY ws.geom <-> ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)
        LIMIT 1
    """
    params = {"lat": lat, "lon": lon}
    if segment_id is not None:
        params["segment_id"] = segment_id
    row = session.execute(text(sql), params).first()
    return None if row is None else float(row.station)


# --- Geo -> station, pure (no database) ------------------------------------
def project_to_station(
    vertices: Sequence[tuple[float, float, float]], lat: float, lon: float
) -> float:
    """Project a lat/lon onto a measured polyline and return the POPA station
    (interpolated ``M``) of the closest point — the pure-Python counterpart of
    ``geo_to_station``.

    ``vertices`` is ``[(lon, lat, M), ...]`` — the same measured centerline that
    is seeded into ``wharf_segment.geom`` (e.g.
    ``data/gis/centerline_vertices.json``). Uses a local equirectangular
    projection about the polyline's mean latitude, which is accurate to far below
    a foot at wharf scale (a few thousand feet).

    This exists for **verification and offline use** (tests, tooling, a
    DB-less map) — PostGIS ``ST_InterpolatePoint`` via ``geo_to_station`` remains
    authoritative for anything that touches the database.
    """
    if len(vertices) < 2:
        raise ValueError("need at least two vertices to project onto a line")

    lat0 = sum(v[1] for v in vertices) / len(vertices)
    k = math.cos(math.radians(lat0))  # longitude foreshortening at this latitude

    px, py = lon * k, lat
    best_d2: float | None = None
    best_m = vertices[0][2]
    for (lo1, la1, m1), (lo2, la2, m2) in zip(vertices[:-1], vertices[1:]):
        ax, ay = lo1 * k, la1
        bx, by = lo2 * k, la2
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        t = 0.0 if seg2 == 0 else ((px - ax) * dx + (py - ay) * dy) / seg2
        t = max(0.0, min(1.0, t))
        cx, cy = ax + t * dx, ay + t * dy
        d2 = (px - cx) ** 2 + (py - cy) ** 2
        if best_d2 is None or d2 < best_d2:
            best_d2 = d2
            best_m = m1 + t * (m2 - m1)
    return best_m
