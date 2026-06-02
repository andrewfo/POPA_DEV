"""End-to-end geo->station check on the REAL derived wharf geometry — pure, no DB.

Validates that the digitized centerline (``data/gis/centerline_vertices.json``,
produced by ``data/gis/build_centerline.py`` from the ArcGIS berth polygons) is
internally consistent and projects lat/lon back to the expected POPA stations,
using the pure ``project_to_station`` oracle. This is the offline counterpart of
the PostGIS ``geo_to_station`` path and closes the "no end-to-end geo->station
fixture" gap without needing a database.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.crosswalk import project_to_station

_REPO = Path(__file__).resolve().parents[1]
_VERTICES = _REPO / "data" / "gis" / "centerline_vertices.json"
_BERTH_STA = _REPO / "app" / "static" / "gis" / "berth_stations.json"


@pytest.fixture(scope="module")
def vertices() -> list[tuple[float, float, float]]:
    if not _VERTICES.exists():
        pytest.skip("centerline_vertices.json not built (run data/gis/build_centerline.py)")
    return [tuple(v) for v in json.loads(_VERTICES.read_text())]


def test_each_vertex_projects_to_its_own_station(vertices):
    # A point sitting exactly on a centerline vertex must read that vertex's M.
    for lon, lat, m in vertices:
        got = project_to_station(vertices, lat, lon)
        assert got == pytest.approx(m, abs=0.5), f"vertex @ {m} ft -> {got}"


def test_stations_are_monotonic_sw_to_ne(vertices):
    stations = [m for *_, m in vertices]
    assert stations == sorted(stations), "POPA station must increase SW -> NE"


def test_anchor_berth4_sw_is_station_351(vertices):
    # build_centerline anchors the Berth 5/4 junction (Berth 4 SW corner) at 351.
    assert any(m == pytest.approx(351.0, abs=0.5) for *_, m in vertices)


def test_midpoint_between_vertices_interpolates(vertices):
    # The lon/lat midpoint of two adjacent vertices should read ~the mean station
    # (small slack for the face's gentle curvature between corners).
    (lo1, la1, m1), (lo2, la2, m2) = vertices[2], vertices[3]  # Berth 4 reach
    mid = project_to_station(vertices, (la1 + la2) / 2, (lo1 + lo2) / 2)
    assert mid == pytest.approx((m1 + m2) / 2, abs=8.0)


def test_offset_perpendicular_keeps_station(vertices):
    # A point pushed PERPENDICULAR to the quay (into the water) still projects to
    # the same station — the whole point of measuring along the line. Build the
    # offset from the local segment normal so it's truly perpendicular.
    import math

    (lo1, la1, m1), (lo2, la2, m2) = vertices[3], vertices[4]
    lat0 = sum(v[1] for v in vertices) / len(vertices)
    k = math.cos(math.radians(lat0))
    dx, dy = (lo2 - lo1) * k, (la2 - la1)
    length = math.hypot(dx, dy)
    nx, ny = -dy / length, dx / length            # unit perpendicular (scaled frame)
    mlon, mlat = (lo1 + lo2) / 2, (la1 + la2) / 2
    d = 0.0006                                     # offset in scaled degrees (~60 m)
    got = project_to_station(vertices, mlat + ny * d, mlon + (nx * d) / k)
    assert got == pytest.approx((m1 + m2) / 2, abs=6.0)


def test_centerline_endpoints_match_berth_station_extents(vertices):
    if not _BERTH_STA.exists():
        pytest.skip("berth_stations.json not built")
    berth = json.loads(_BERTH_STA.read_text())
    flat = [v for pair in berth.values() for v in pair]
    stations = [m for *_, m in vertices]
    # The face line should span exactly the berths' station extent (same source).
    assert min(stations) == pytest.approx(min(flat), abs=0.5)
    assert max(stations) == pytest.approx(max(flat), abs=0.5)
