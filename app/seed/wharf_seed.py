"""Seed the first ``wharf_segment`` from the port's stationing data.

Affine params come from ``app.crosswalk`` so there is exactly one source of
truth for the published crosswalk constants.

The wharf face geometry + POPA stationing are DERIVED from the port's ArcGIS
berth polygons by ``data/gis/build_centerline.py`` (the quay face is each
berth's water-side edge; station = running footage along it). That script
writes ``data/gis/centerline_vertices.json`` as ``[[lon, lat, M], ...]`` where
``M`` is the canonical POPA station (feet) — what ``ST_InterpolatePoint`` reads
to turn a lat/lon into a station. If that file is missing we fall back to a
placeholder line so the seed still runs.

Run:  python -m app.seed.wharf_seed
"""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.crosswalk import (
    DEFAULT_CORPS_OFFSET,
    DEFAULT_CORPS_SCALE,
    DEFAULT_DOCKNO_OFFSET,
    DEFAULT_DOCKNO_SCALE,
)
from app.db import SessionLocal

SEGMENT_NAME = "POPA Public Wharf"

_VERTICES_FILE = (
    Path(__file__).parents[2] / "data" / "gis" / "centerline_vertices.json"
)
# Digitized berthing-zone polygon ([[lon, lat], ...] closed ring), also from
# build_centerline.py. Optional — NULL apron just means the occupancy detector
# falls back to the centerline buffer.
_APRON_FILE = Path(__file__).parents[2] / "data" / "gis" / "apron_polygon.json"

# PLACEHOLDER fallback (lon, lat, M=POPA station feet) if the derived file is
# absent — a straight-ish line inside the AIS bounding box.
_PLACEHOLDER_VERTICES: list[tuple[float, float, float]] = [
    (-93.93800, 29.86000, -1202.0),
    (-93.93432, 29.86526, 0.0),
    (-93.93125, 29.86964, 1000.0),
    (-93.92819, 29.87402, 2000.0),
    (-93.92400, 29.88000, 3366.0),
]


def _load_vertices() -> list[tuple[float, float, float]]:
    if _VERTICES_FILE.exists():
        return [tuple(v) for v in json.loads(_VERTICES_FILE.read_text())]
    return _PLACEHOLDER_VERTICES


WHARF_VERTICES: list[tuple[float, float, float]] = _load_vertices()


def _wharf_ewkt(vertices: list[tuple[float, float, float]]) -> str:
    coords = ", ".join(f"{lon} {lat} {m}" for lon, lat, m in vertices)
    return f"SRID=4326;LINESTRING M ({coords})"


def _apron_ewkt() -> str | None:
    """EWKT for the apron polygon, or None if it hasn't been digitized yet."""
    if not _APRON_FILE.exists():
        return None
    ring = json.loads(_APRON_FILE.read_text())
    coords = ", ".join(f"{lon} {lat}" for lon, lat in ring)
    return f"SRID=4326;POLYGON(({coords}))"


def seed_wharf(session: Session) -> int:
    """Insert (or update) the POPA Public Wharf segment. Returns its id."""
    ewkt = _wharf_ewkt(WHARF_VERTICES)
    sta_start = min(v[2] for v in WHARF_VERTICES)
    sta_end = max(v[2] for v in WHARF_VERTICES)

    row = session.execute(
        text(
            """
            INSERT INTO wharf_segment
                (name, geom, apron, popa_sta_start, popa_sta_end,
                 corps_scale, corps_offset, dockno_scale, dockno_offset)
            VALUES
                (:name, ST_GeomFromEWKT(:ewkt), ST_GeomFromEWKT(:apron),
                 :sta_start, :sta_end,
                 :corps_scale, :corps_offset, :dockno_scale, :dockno_offset)
            ON CONFLICT (name) DO UPDATE SET
                geom = EXCLUDED.geom,
                apron = EXCLUDED.apron,
                popa_sta_start = EXCLUDED.popa_sta_start,
                popa_sta_end = EXCLUDED.popa_sta_end,
                corps_scale = EXCLUDED.corps_scale,
                corps_offset = EXCLUDED.corps_offset,
                dockno_scale = EXCLUDED.dockno_scale,
                dockno_offset = EXCLUDED.dockno_offset
            RETURNING id
            """
        ),
        {
            "name": SEGMENT_NAME,
            "ewkt": ewkt,
            "apron": _apron_ewkt(),
            "sta_start": sta_start,
            "sta_end": sta_end,
            "corps_scale": DEFAULT_CORPS_SCALE,
            "corps_offset": DEFAULT_CORPS_OFFSET,
            "dockno_scale": DEFAULT_DOCKNO_SCALE,
            "dockno_offset": DEFAULT_DOCKNO_OFFSET,
        },
    ).first()
    session.commit()
    return int(row.id)


def main() -> None:
    session = SessionLocal()
    try:
        seg_id = seed_wharf(session)
        print(f"Seeded wharf_segment id={seg_id} ({SEGMENT_NAME})")
    finally:
        session.close()


if __name__ == "__main__":
    main()
