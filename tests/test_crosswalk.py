"""Tests for the stationing crosswalk (pure math — no database required)."""
import math

import pytest

from app.crosswalk import (
    AffineParams,
    corps_to_popa,
    dockno_to_popa,
    format_station,
    parse_station,
    popa_to_corps,
    popa_to_dockno,
)


# --- Stationing notation ---------------------------------------------------
@pytest.mark.parametrize(
    "text,feet",
    [
        ("12+02", 1202.0),
        ("-12+02", -1202.0),
        ("108+38.65", 10838.65),
        ("33+66", 3366.0),
        ("0+00", 0.0),
        ("1202", 1202.0),  # plain numeric also accepted
    ],
)
def test_parse_station(text, feet):
    assert parse_station(text) == pytest.approx(feet)


@pytest.mark.parametrize(
    "feet,text",
    [
        (1202.0, "12+02"),
        (-1202.0, "-12+02"),
        (10838.65, "108+38.65"),
        (3366.0, "33+66"),
        (0.0, "0+00"),
    ],
)
def test_format_station(feet, text):
    assert format_station(feet) == text


def test_parse_format_round_trip():
    for s in ("12+02", "-12+02", "108+38.65", "0+50", "33+66"):
        assert format_station(parse_station(s)) == s


# --- Known crosswalk reference points --------------------------------------
def test_popa_to_corps_known_point():
    # Port crosswalk: POPA -12+02 reconciles to Corps 108+38.65.
    popa = parse_station("-12+02")          # -1202 ft
    corps = popa_to_corps(popa)             # -1202 + 12040.65
    assert corps == pytest.approx(parse_station("108+38.65"))  # 10838.65
    assert format_station(corps) == "108+38.65"


def test_corps_to_popa_known_point():
    popa = corps_to_popa(parse_station("108+38.65"))
    assert popa == pytest.approx(-1202.0)
    assert format_station(popa) == "-12+02"


def test_popa_to_dockno_known_point():
    # Dock No. is reversed: dockno = 3365 - POPA station.
    popa = parse_station("33+66")           # 3366 ft
    dockno = popa_to_dockno(popa)           # 3365 - 3366 = -1
    assert dockno == pytest.approx(-1.0)
    # Round-trips back to the original POPA station.
    assert dockno_to_popa(dockno) == pytest.approx(popa)


# --- Round-trip identity across a range ------------------------------------
@pytest.mark.parametrize("popa", [-1500.0, -1202.0, 0.0, 500.25, 1202.0, 3366.0, 5000.0])
def test_corps_round_trip_identity(popa):
    assert corps_to_popa(popa_to_corps(popa)) == pytest.approx(popa)


@pytest.mark.parametrize("popa", [-1500.0, -1202.0, 0.0, 500.25, 1202.0, 3366.0, 5000.0])
def test_dockno_round_trip_identity(popa):
    assert dockno_to_popa(popa_to_dockno(popa)) == pytest.approx(popa)


# --- Affine generality -----------------------------------------------------
def test_affine_params_inverse_arbitrary():
    p = AffineParams(scale=2.5, offset=-17.0)
    for x in (-10.0, 0.0, 3.3, 1000.0):
        assert p.to_popa(p.from_popa(x)) == pytest.approx(x)


def test_affine_zero_scale_rejected():
    with pytest.raises(ValueError):
        AffineParams(scale=0.0, offset=5.0)


def test_corps_offset_value():
    # Guard the published constant: a 1-ft POPA step is a 1-ft Corps step.
    assert popa_to_corps(1.0) - popa_to_corps(0.0) == pytest.approx(1.0)
    assert popa_to_corps(0.0) == pytest.approx(12040.65)


def test_dockno_is_reversed():
    # A positive POPA step decreases Dock No. (reversed direction).
    assert popa_to_dockno(1.0) < popa_to_dockno(0.0)
    assert math.isclose(popa_to_dockno(0.0), 3365.0)
