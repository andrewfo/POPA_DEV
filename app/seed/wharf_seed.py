"""Seed the first ``wharf_segment`` from the port's stationing data.

Affine params come from ``app.crosswalk`` so there is exactly one source of
truth for the published crosswalk constants.

TODO(real-geometry): the vertex lat/lon below are a PLACEHOLDER straight-ish
line inside the AIS bounding box. Replace with vertices digitized from the
aerial / port GIS along the actual quay face. The ``M`` value on each vertex is
the canonical POPA station (feet) and MUST stay monotonic and correct — that is
what ``ST_InterpolatePoint`` reads to turn a lat/lon into a station. Only the
lat/lon need replacing; keep the M values tied to true stationing.

Run:  python -m app.seed.wharf_seed
"""
from __future__ import annotations

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

# (lon, lat, M=POPA station feet). PLACEHOLDER lat/lon — see module docstring.
# Spans POPA station -12+02 .. 33+66 (the crosswalk test reference points).
WHARF_VERTICES: list[tuple[float, float, float]] = [
    (-93.93800, 29.86000, -1202.0),
    (-93.93432, 29.86526, 0.0),
    (-93.93125, 29.86964, 1000.0),
    (-93.92819, 29.87402, 2000.0),
    (-93.92400, 29.88000, 3366.0),
]


def _wharf_ewkt(vertices: list[tuple[float, float, float]]) -> str:
    coords = ", ".join(f"{lon} {lat} {m}" for lon, lat, m in vertices)
    return f"SRID=4326;LINESTRING M ({coords})"


def seed_wharf(session: Session) -> int:
    """Insert (or update) the POPA Public Wharf segment. Returns its id."""
    ewkt = _wharf_ewkt(WHARF_VERTICES)
    sta_start = min(v[2] for v in WHARF_VERTICES)
    sta_end = max(v[2] for v in WHARF_VERTICES)

    row = session.execute(
        text(
            """
            INSERT INTO wharf_segment
                (name, geom, popa_sta_start, popa_sta_end,
                 corps_scale, corps_offset, dockno_scale, dockno_offset)
            VALUES
                (:name, ST_GeomFromEWKT(:ewkt), :sta_start, :sta_end,
                 :corps_scale, :corps_offset, :dockno_scale, :dockno_offset)
            ON CONFLICT (name) DO UPDATE SET
                geom = EXCLUDED.geom,
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
