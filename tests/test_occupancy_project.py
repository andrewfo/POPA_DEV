"""Bow/stern projection tests — pure geodesic math, no database."""
from __future__ import annotations

import math

import pytest

from app.occupancy.project import destination_point, project_bow_stern

# ~metres per degree of latitude (constant); longitude scales by cos(lat).
_M_PER_DEG_LAT = math.pi / 180.0 * 6371008.8


def test_destination_due_north_moves_latitude_only():
    lat, lon = destination_point(29.857, -93.94, bearing_deg=0.0, distance_m=100.0)
    assert lon == pytest.approx(-93.94, abs=1e-9)
    assert (lat - 29.857) * _M_PER_DEG_LAT == pytest.approx(100.0, abs=0.5)


def test_destination_due_east_moves_longitude_only():
    lat0 = 29.857
    lat, lon = destination_point(lat0, -93.94, bearing_deg=90.0, distance_m=100.0)
    assert lat == pytest.approx(lat0, abs=1e-6)
    east_m = (lon + 93.94) * _M_PER_DEG_LAT * math.cos(math.radians(lat0))
    assert east_m == pytest.approx(100.0, abs=0.5)


def test_bow_stern_split_along_heading():
    # Heading north; bow 30 m ahead (north), stern 70 m astern (south).
    bs = project_bow_stern(
        29.857, -93.94, heading_deg=0.0, dim_a_m=30.0, dim_b_m=70.0, loa_m=100.0
    )
    assert bs.confident is True
    assert bs.bow_lat > 29.857 > bs.stern_lat  # bow north of antenna, stern south
    bow_m = (bs.bow_lat - 29.857) * _M_PER_DEG_LAT
    stern_m = (29.857 - bs.stern_lat) * _M_PER_DEG_LAT
    assert bow_m == pytest.approx(30.0, abs=0.5)
    assert stern_m == pytest.approx(70.0, abs=0.5)


def test_missing_dims_fall_back_to_symmetric_loa_split():
    bs = project_bow_stern(
        29.857, -93.94, heading_deg=0.0, dim_a_m=None, dim_b_m=None, loa_m=100.0
    )
    assert bs.confident is True
    bow_m = (bs.bow_lat - 29.857) * _M_PER_DEG_LAT
    stern_m = (29.857 - bs.stern_lat) * _M_PER_DEG_LAT
    assert bow_m == pytest.approx(50.0, abs=0.5)
    assert stern_m == pytest.approx(50.0, abs=0.5)


def test_missing_heading_is_not_confident():
    bs = project_bow_stern(
        29.857, -93.94, heading_deg=None, dim_a_m=30.0, dim_b_m=70.0, loa_m=100.0
    )
    assert bs.confident is False
    assert (bs.bow_lat, bs.bow_lon) == (29.857, -93.94)
    assert (bs.stern_lat, bs.stern_lon) == (29.857, -93.94)


def test_no_dims_and_no_loa_is_not_confident():
    bs = project_bow_stern(
        29.857, -93.94, heading_deg=0.0, dim_a_m=None, dim_b_m=None, loa_m=None
    )
    assert bs.confident is False
