"""The single "is this point at the quay?" predicate, plus sample loading.

THIS is the Option-3 swap point. Today "alongside" means *within a buffer of the
wharf centerline* (``ST_DWithin`` on geography, metres). When a real apron
polygon is digitized, replace the one expression in ``_alongside_sql`` with a
point-in-polygon test (``ST_Contains(apron, point)``) and nothing else in the
occupancy package has to change — the detector consumes a pre-classified
``alongside`` boolean and never sees geometry.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class PosRow:
    """One position fix, classified, with everything derive.py needs."""

    vessel_id: int
    ts: dt.datetime
    lat: float
    lon: float
    sog: float | None
    cog: float | None
    heading: float | None
    nav_status: int | None
    alongside: bool


# The swappable predicate. ``seg.g`` is the (2D) wharf geometry chosen per row;
# ``pt`` is the position. Both are cast to geography so the buffer is in metres.
def _alongside_sql(point_sql: str) -> str:
    return f"ST_DWithin(seg.g::geography, ({point_sql})::geography, :buffer_m)"


def load_samples(
    session: Session,
    *,
    buffer_m: float,
    segment_id: int | None = None,
    since: dt.datetime | None = None,
) -> dict[int, list[PosRow]]:
    """Load position reports grouped by vessel, each tagged ``alongside``.

    Each report is classified against its nearest ``wharf_segment`` (or the
    given ``segment_id``). Rows lacking ``msg_ts`` fall back to ``created_at`` as
    their timestamp. Returns ``{vessel_id: [PosRow, ...]}`` ordered by time.
    """
    point = "ST_SetSRID(ST_MakePoint(pr.lon, pr.lat), 4326)"
    seg_where = "WHERE ws.id = :segment_id" if segment_id is not None else ""
    since_where = "AND COALESCE(pr.msg_ts, pr.created_at) >= :since" if since else ""

    sql = f"""
        SELECT
            pr.vessel_id AS vessel_id,
            COALESCE(pr.msg_ts, pr.created_at) AS ts,
            pr.lat AS lat,
            pr.lon AS lon,
            pr.sog AS sog,
            pr.cog AS cog,
            pr.heading AS heading,
            pr.nav_status AS nav_status,
            {_alongside_sql(point)} AS alongside
        FROM position_report pr
        CROSS JOIN LATERAL (
            SELECT ST_Force2D(ws.geom) AS g
            FROM wharf_segment ws
            {seg_where}
            ORDER BY ws.geom <-> {point}
            LIMIT 1
        ) seg
        WHERE pr.vessel_id IS NOT NULL
        {since_where}
        ORDER BY pr.vessel_id, ts
    """

    params: dict[str, object] = {"buffer_m": buffer_m}
    if segment_id is not None:
        params["segment_id"] = segment_id
    if since:
        params["since"] = since

    grouped: dict[int, list[PosRow]] = {}
    for r in session.execute(text(sql), params):
        grouped.setdefault(r.vessel_id, []).append(
            PosRow(
                vessel_id=r.vessel_id,
                ts=r.ts,
                lat=float(r.lat),
                lon=float(r.lon),
                sog=None if r.sog is None else float(r.sog),
                cog=None if r.cog is None else float(r.cog),
                heading=None if r.heading is None else float(r.heading),
                nav_status=None if r.nav_status is None else int(r.nav_status),
                alongside=bool(r.alongside),
            )
        )
    return grouped
