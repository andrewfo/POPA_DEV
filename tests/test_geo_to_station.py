"""Geo -> station projection test. Requires a live PostGIS DB (marked `db`).

Inserts a controlled measured line so the expected station is exact, instead of
depending on the seeded placeholder geometry.
"""
import pytest
from sqlalchemy import text

from app.crosswalk import geo_to_station

pytestmark = pytest.mark.db


def _insert_test_segment(session) -> int:
    # A measured line from (lon=0, lat=0) M=0 to (lon=0, lat=1) M=1000.
    # SRID 4326. A point on the line projects to its M value directly.
    row = session.execute(
        text(
            """
            INSERT INTO wharf_segment
                (name, geom, popa_sta_start, popa_sta_end,
                 corps_scale, corps_offset, dockno_scale, dockno_offset)
            VALUES
                (:name,
                 ST_GeomFromEWKT('SRID=4326;LINESTRING M (0 0 0, 0 1 1000)'),
                 0, 1000, 1, 12040.65, -1, 3365)
            RETURNING id
            """
        ),
        {"name": "TEST_SEGMENT_geo_to_station"},
    ).first()
    return int(row.id)


def test_geo_to_station_midpoint(db_session):
    seg_id = _insert_test_segment(db_session)
    # Midpoint of the line: lat=0.5 on lon=0 -> M = 500.
    station = geo_to_station(db_session, lat=0.5, lon=0.0, segment_id=seg_id)
    assert station == pytest.approx(500.0, abs=1.0)


def test_geo_to_station_endpoints(db_session):
    seg_id = _insert_test_segment(db_session)
    assert geo_to_station(db_session, lat=0.0, lon=0.0, segment_id=seg_id) == pytest.approx(
        0.0, abs=1.0
    )
    assert geo_to_station(db_session, lat=1.0, lon=0.0, segment_id=seg_id) == pytest.approx(
        1000.0, abs=1.0
    )


def test_geo_to_station_quarter_point(db_session):
    seg_id = _insert_test_segment(db_session)
    station = geo_to_station(db_session, lat=0.25, lon=0.0, segment_id=seg_id)
    assert station == pytest.approx(250.0, abs=1.0)
