"""Depth data layer + draft gate tests (app/depth/* + the confirm path).

Pure tests (always run): the ``.XYZ`` parser, the filename-date parser, and the
``depth_shortfall`` comparison.

DB-marked tests (auto-skip without a migrated PostGIS): the PostGIS reduction
(``import_survey`` project -> clip -> bin -> min), the ``controlling_depth_over``
lookup, and the confirm endpoint's block (422) / override / no-survey-warning
behaviour. They build a SYNTHETIC wharf segment far from any seeded data and feed
soundings in EPSG:4326 (so ``ST_Transform`` is the identity and the test controls
the geometry directly), then commit the setup so the endpoint's own
rollback-on-422 doesn't discard it (same savepoint pattern as test_edit).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import get_session
from app.depth.gate import controlling_depth_over, depth_shortfall
from app.depth.ingest import import_survey
from app.depth.parse import parse_survey_date, parse_xyz
from app.main import app


# --- pure helpers (always run) ---------------------------------------------
def test_parse_xyz_basic():
    lines = [
        "# a comment",
        "",
        "3570274.00 13892746.00 24.75",
        "3570276.00 13892746.00 25.91 extra_col_ignored",
        "  3570278.0\t13892746.0\t26.82  ",
    ]
    rows = list(parse_xyz(lines))
    assert rows == [
        (3570274.00, 13892746.00, 24.75),
        (3570276.00, 13892746.00, 25.91),
        (3570278.0, 13892746.0, 26.82),
    ]


def test_parse_xyz_skips_unparseable():
    assert list(parse_xyz(["x y z", "1 2", "1 2 three", "4 5 6"])) == [(4.0, 5.0, 6.0)]


def test_parse_survey_date():
    assert parse_survey_date("07092023CND_POPA_B1-6_2x2.XYZ").isoformat() == "2023-07-09"
    assert parse_survey_date("no_date_here.xyz") is None
    assert parse_survey_date("99999999weird.xyz") is None  # invalid MMDDYYYY


def test_depth_shortfall_clears_and_blocks():
    # controlling 30 ft, draft 4 m (13.12 ft) + 2 ft clearance = 15.12 -> clears.
    assert depth_shortfall(30.0, 4.0, 2.0) is None
    # controlling 12 ft, same draft -> required 15.12 ft, short ~3.12 ft.
    short = depth_shortfall(12.0, 4.0, 2.0)
    assert short == pytest.approx(3.1234, abs=0.01)


def test_depth_shortfall_none_inputs():
    assert depth_shortfall(None, 4.0, 2.0) is None  # no depth data
    assert depth_shortfall(12.0, None, 2.0) is None  # unknown draft


# --- DB-marked tests (auto-skip without PostGIS) ---------------------------
@pytest.fixture
def client(db_session):
    app.dependency_overrides[get_session] = lambda: db_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_session, None)


def _synthetic_segment(session) -> None:
    """A measured wharf segment far from any real data (latitude 10°), carrying
    POPA station M 0..2000 over a short E-W line. Dock No. params match the
    published default (-1 / 3365) so Dock<->POPA in the endpoint is deterministic.
    """
    session.execute(
        text(
            """
            INSERT INTO wharf_segment
                (name, geom, popa_sta_start, popa_sta_end,
                 corps_scale, corps_offset, dockno_scale, dockno_offset)
            VALUES
                (:name,
                 ST_GeomFromEWKT('SRID=4326;LINESTRING M (0 10 0, 0.02 10 2000)'),
                 0, 2000, 1, 12040.65, -1, 3365)
            """
        ),
        {"name": "Synthetic Depth Test Wharf"},
    )


# Soundings (lon, lat, depth-ft) near the synthetic line. lat 10.0002 ~= 73 ft off
# the face (inside the [8,150] ft band); the on-line point (offset ~0) and the
# far point (~365 ft) are clipped out, so they must NOT set any bin's min.
_SOUNDINGS = [
    (0.005, 10.0002, 30.0),   # M~500  -> bin [0,1000)
    (0.005, 10.0002, 40.0),
    (0.015, 10.0002, 12.0),   # M~1500 -> bin [1000,2000)
    (0.015, 10.0002, 20.0),
    (0.010, 10.0,    2.0),    # on the face (toe) -> clipped
    (0.010, 10.001,  99.0),   # ~365 ft off -> clipped
]


def _ingest(session, soundings=_SOUNDINGS, bin_ft=1000.0) -> dict:
    import datetime as dt

    # Far-future date so this synthetic survey always wins the "latest active"
    # selection over any real survey already loaded into the dev DB.
    return import_survey(
        session, list(soundings), source_file="test.XYZ",
        surveyed_at=dt.date(2099, 1, 1), srid=4326, bin_ft=bin_ft,
    )


def test_import_reduces_to_controlling_min(db_session):
    _synthetic_segment(db_session)
    summary = _ingest(db_session)
    # Two bins survive the clip; the on-line/far soundings are excluded.
    assert summary["segment_count"] == 2
    assert summary["binned_points"] == 4
    assert summary["station_min"] == pytest.approx(0.0)
    assert summary["station_max"] == pytest.approx(2000.0)
    assert summary["min_depth_ft"] == pytest.approx(12.0)


def test_import_empty_when_off_wharf_raises(db_session):
    _synthetic_segment(db_session)
    # Soundings far off the face (all clipped) -> no segment -> 422-style refusal.
    with pytest.raises(ValueError):
        _ingest(db_session, soundings=[(0.010, 10.01, 5.0)])


def test_controlling_depth_over_ranges(db_session):
    _synthetic_segment(db_session)
    _ingest(db_session)
    assert controlling_depth_over(db_session, 200, 400)[0] == pytest.approx(30.0)
    assert controlling_depth_over(db_session, 1100, 1400)[0] == pytest.approx(12.0)
    # Spanning both bins takes the shallower (controlling) one.
    assert controlling_depth_over(db_session, 100, 1500)[0] == pytest.approx(12.0)
    # No survey coverage out here.
    assert controlling_depth_over(db_session, 5000, 6000) == (None, None)


def _confirm_at_popa_1200_1400(client, vid, *, override=False):
    # POPA [1200,1400] -> Dock stern 2165 (lo), bow 1965 (hi), via -1/3365.
    return client.post("/reservations", json={
        "vessel_id": vid, "etb": "2027-03-01T00:00:00Z",
        "etd": "2027-03-03T00:00:00Z",
        "station_lo": 2165, "station_hi": 1965, "status": "confirmed",
        "depth_override": override,
    })


def _add_vessel_with_draft(session, *, mmsi, draft_m):
    return session.execute(
        text("INSERT INTO vessel (mmsi, name, draft) VALUES (:m, 'DEEP', :d) RETURNING id"),
        {"m": mmsi, "d": draft_m},
    ).scalar_one()


def test_confirm_blocks_when_too_deep(client, db_session):
    _synthetic_segment(db_session)
    _ingest(db_session)
    vid = _add_vessel_with_draft(db_session, mmsi=636010001, draft_m=4.0)  # 13.1 ft
    db_session.commit()  # persist setup past the endpoint's rollback-on-422
    r = _confirm_at_popa_1200_1400(client, vid)
    assert r.status_code == 422
    assert "controlling depth" in r.json()["detail"]


def test_confirm_override_allows_with_warning(client, db_session):
    _synthetic_segment(db_session)
    _ingest(db_session)
    vid = _add_vessel_with_draft(db_session, mmsi=636010002, draft_m=4.0)
    db_session.commit()
    r = _confirm_at_popa_1200_1400(client, vid, override=True)
    assert r.status_code == 201
    assert any("depth override" in w for w in r.json()["warnings"])


def test_confirm_clears_when_shallow_enough(client, db_session):
    _synthetic_segment(db_session)
    _ingest(db_session)
    vid = _add_vessel_with_draft(db_session, mmsi=636010003, draft_m=2.0)  # 6.6 ft
    db_session.commit()
    r = _confirm_at_popa_1200_1400(client, vid)
    assert r.status_code == 201
    assert r.json()["warnings"] == []  # cleared, no warning


def test_confirm_warns_when_no_survey_covers(client, db_session):
    # No survey covers this station, so the gate can't evaluate -> it warns rather
    # than blocks. POPA ~5000 is well beyond the real wharf (max ~3375), so no
    # loaded survey overlaps it. POPA [5000,5200] -> Dock -1635 (lo) / -1835 (hi).
    vid = _add_vessel_with_draft(db_session, mmsi=636010004, draft_m=4.0)
    db_session.commit()
    r = client.post("/reservations", json={
        "vessel_id": vid, "etb": "2027-03-01T00:00:00Z",
        "etd": "2027-03-03T00:00:00Z",
        "station_lo": -1635, "station_hi": -1835, "status": "confirmed",
    })
    assert r.status_code == 201
    assert any("no controlling-depth survey" in w for w in r.json()["warnings"])
