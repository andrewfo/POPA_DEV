"""DB-marked tests for the feasibility oracle (GET /feasibility).

Exercises the endpoint through FastAPI's TestClient against a live PostGIS, so the
real range operators + the depth lookup are under test. Auto-skips (via the
``db_session`` fixture) when no migrated DB is around. Each test pins a
deterministic wharf extent [0, 4000] inside the rolled-back transaction, so it
doesn't depend on whatever the dev DB was seeded with; nothing leaks.
"""
from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import get_session
from app.main import app

UTC = dt.timezone.utc
FEET_PER_M = 3.280839895


@pytest.fixture
def client(db_session):
    app.dependency_overrides[get_session] = lambda: db_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_session, None)


def _t(day, hour=0):
    return dt.datetime(2030, 7, day, hour, tzinfo=UTC)


def _seed_wharf(session) -> tuple[float, float]:
    """Pin a single wharf segment spanning POPA [0, 4000] (Dock No. = 3365 - POPA,
    the canonical default). Returns the extent. Also neutralizes any dev-seeded
    depth survey so depth state is whatever the test seeds (or none) — all inside
    the rolled-back transaction."""
    session.execute(text("DELETE FROM wharf_segment"))
    session.execute(text("UPDATE depth_survey SET active = false"))
    session.execute(
        text(
            """
            INSERT INTO wharf_segment
                (name, geom, popa_sta_start, popa_sta_end,
                 corps_scale, corps_offset, dockno_scale, dockno_offset)
            VALUES ('TEST WHARF',
                ST_GeomFromText('LINESTRINGM(-93.95 29.86 0, -93.90 29.90 4000)', 4326),
                0, 4000, 1, 12040.65, -1, 3365)
            """
        )
    )
    return (0.0, 4000.0)


def _vessel(session, *, loa_m, draft_m=5.0, imo=9000001, name="TESTSHIP"):
    return session.execute(
        text(
            "INSERT INTO vessel (imo, name, loa, draft) "
            "VALUES (:imo, :n, :loa, :draft) RETURNING id"
        ),
        {"imo": imo, "n": name, "loa": loa_m, "draft": draft_m},
    ).scalar_one()


def _reservation(session, *, lo, hi, t_start, t_end, status, vessel_id=None,
                 rtype="vessel"):
    station = "'empty'::numrange" if lo is None else "numrange(:lo, :hi, '[]')"
    return session.execute(
        text(
            f"""
            INSERT INTO reservation
                (vessel_id, type, station_range, time_range, status, source, created_at)
            VALUES (:vessel_id, CAST(:rtype AS reservation_type), {station},
                    tstzrange(:t_start, :t_end, '[)'),
                    CAST(:status AS reservation_status), 'phone', now())
            RETURNING id
            """
        ),
        {"vessel_id": vessel_id, "lo": lo, "hi": hi, "t_start": t_start,
         "t_end": t_end, "status": status, "rtype": rtype},
    ).scalar_one()


def _subject(session, *, loa_m=30.0, draft_m=5.0):
    """A requested (unplaced) reservation for a fresh vessel, over [t10, t14]."""
    v = _vessel(session, loa_m=loa_m, draft_m=draft_m, imo=9100000 + int(loa_m))
    rid = _reservation(session, lo=None, hi=None, t_start=_t(10), t_end=_t(14),
                       status="requested", vessel_id=v)
    return rid


def _depth(session, segments, *, station_min=0, station_max=4000):
    """One active survey with the given [(lo, hi, controlling_ft), ...] segments."""
    sid = session.execute(
        text(
            "INSERT INTO depth_survey (surveyed_at, active, station_min, station_max, srid) "
            "VALUES ('2026-06-01', true, :mn, :mx, 2278) RETURNING id"
        ),
        {"mn": station_min, "mx": station_max},
    ).scalar_one()
    for lo, hi, ft in segments:
        session.execute(
            text(
                "INSERT INTO depth_segment (survey_id, popa_range, controlling_depth_ft) "
                "VALUES (:sid, numrange(:lo, :hi, '[)'), :ft)"
            ),
            {"sid": sid, "lo": lo, "hi": hi, "ft": ft},
        )


def _overlaps(band, lo, hi):
    return band["popa_lo"] < hi and lo < band["popa_hi"]


def _covers(payload, popa):
    return any(b["popa_lo"] <= popa <= b["popa_hi"] for b in payload["bands"])


# --- blockers remove space -------------------------------------------------
def test_confirmed_obstacle_removes_its_span_plus_gap(client, db_session):
    _seed_wharf(db_session)
    rid = _subject(db_session)
    _reservation(db_session, lo=1000, hi=1200, t_start=_t(10), t_end=_t(14),
                 status="confirmed")
    payload = client.get(f"/feasibility?reservation_id={rid}").json()
    # Nothing may overlap the obstacle padded by the 75 ft gap [925, 1275].
    assert all(not _overlaps(b, 925, 1275) for b in payload["bands"])
    # But the wharf on either side is still offered.
    assert _covers(payload, 500)
    assert _covers(payload, 2000)


def test_tentative_obstacle_also_blocks(client, db_session):
    _seed_wharf(db_session)
    rid = _subject(db_session)
    _reservation(db_session, lo=1000, hi=1200, t_start=_t(10), t_end=_t(14),
                 status="tentative")
    payload = client.get(f"/feasibility?reservation_id={rid}").json()
    assert all(not _overlaps(b, 925, 1275) for b in payload["bands"])


def test_observed_is_advisory_not_blocking(client, db_session):
    _seed_wharf(db_session)
    rid = _subject(db_session)
    obs = _reservation(db_session, lo=1000, hi=1200, t_start=_t(10), t_end=_t(14),
                       status="observed")
    payload = client.get(f"/feasibility?reservation_id={rid}").json()
    # The observed span is NOT removed from the feasible set...
    assert _covers(payload, 1100)
    # ...it rides along as an advisory overlay instead.
    assert any(o["reservation_id"] == obs for o in payload["observed"])


def test_requested_placement_is_ignored(client, db_session):
    # A (rare) requested row carrying a station range is not a firm plan; it must
    # not remove space.
    _seed_wharf(db_session)
    rid = _subject(db_session)
    _reservation(db_session, lo=1000, hi=1200, t_start=_t(10), t_end=_t(14),
                 status="requested")
    payload = client.get(f"/feasibility?reservation_id={rid}").json()
    assert _covers(payload, 1100)


def test_out_of_window_obstacle_does_not_block(client, db_session):
    _seed_wharf(db_session)
    rid = _subject(db_session)
    # A confirmed booking in a different (later) window leaves the space free.
    _reservation(db_session, lo=1000, hi=1200, t_start=_t(20), t_end=_t(24),
                 status="confirmed")
    payload = client.get(f"/feasibility?reservation_id={rid}").json()
    assert _covers(payload, 1100)


# --- fit threshold ---------------------------------------------------------
def test_gap_shorter_than_loa_is_excluded(client, db_session):
    _seed_wharf(db_session)
    rid = _subject(db_session, loa_m=60.0)   # ~196.8 ft
    # Two confirmed bookings leaving a 150 ft clear middle — too short for the
    # vessel (needs LOA, and it's flanked by gaps on both sides).
    _reservation(db_session, lo=800, hi=1000, t_start=_t(10), t_end=_t(14),
                 status="confirmed")
    _reservation(db_session, lo=1300, hi=1500, t_start=_t(10), t_end=_t(14),
                 status="confirmed")
    payload = client.get(f"/feasibility?reservation_id={rid}").json()
    assert not _covers(payload, 1150)     # the short middle is not offered
    assert _covers(payload, 200)          # the open ends still are
    assert _covers(payload, 3000)


# --- depth annotation ------------------------------------------------------
def test_depth_flags_shallow_band_and_clears_deep_band(client, db_session):
    _seed_wharf(db_session)
    rid = _subject(db_session, draft_m=10.0)   # needs ~32.8 ft + clearance
    _reservation(db_session, lo=1000, hi=1200, t_start=_t(10), t_end=_t(14),
                 status="confirmed")
    # Deep below the obstacle, shallow above it.
    _depth(db_session, [(0, 3000, 60.0), (3000, 4000, 5.0)])
    payload = client.get(f"/feasibility?reservation_id={rid}").json()
    low = next(b for b in payload["bands"] if b["popa_hi"] <= 925)
    high = next(b for b in payload["bands"] if b["popa_lo"] >= 1275)
    assert low["depth"]["status"] == "ok"
    assert high["depth"]["status"] == "shallow"
    assert high["depth"]["shortfall_ft"] > 0


def test_no_survey_reports_unknown_depth(client, db_session):
    _seed_wharf(db_session)
    rid = _subject(db_session)
    payload = client.get(f"/feasibility?reservation_id={rid}").json()
    assert payload["bands"]
    assert all(b["depth"]["status"] == "unknown" for b in payload["bands"])


# --- berth labels ----------------------------------------------------------
def test_bands_are_annotated_with_covering_berths(client, db_session):
    _seed_wharf(db_session)
    db_session.execute(
        text("INSERT INTO berth (name, popa_sta_start, popa_sta_end) "
             "VALUES ('Berth Test', 0, 900)")
    )
    rid = _subject(db_session)
    _reservation(db_session, lo=1000, hi=1200, t_start=_t(10), t_end=_t(14),
                 status="confirmed")
    payload = client.get(f"/feasibility?reservation_id={rid}").json()
    low = next(b for b in payload["bands"] if b["popa_hi"] <= 925)
    assert "Berth Test" in low["berths"]


# --- error paths -----------------------------------------------------------
def test_missing_reservation_is_404(client, db_session):
    _seed_wharf(db_session)
    assert client.get("/feasibility?reservation_id=99999999").status_code == 404


def test_reservation_without_vessel_is_422(client, db_session):
    _seed_wharf(db_session)
    rid = _reservation(db_session, lo=None, hi=None, t_start=_t(10), t_end=_t(14),
                       status="requested", vessel_id=None, rtype="dredge")
    assert client.get(f"/feasibility?reservation_id={rid}").status_code == 422


def test_vessel_without_loa_is_422(client, db_session):
    _seed_wharf(db_session)
    v = _vessel(db_session, loa_m=None, imo=9200002)
    rid = _reservation(db_session, lo=None, hi=None, t_start=_t(10), t_end=_t(14),
                       status="requested", vessel_id=v)
    assert client.get(f"/feasibility?reservation_id={rid}").status_code == 422


# --- payload shape ---------------------------------------------------------
def test_bands_carry_dock_and_placement_fields(client, db_session):
    _seed_wharf(db_session)
    rid = _subject(db_session)
    payload = client.get(f"/feasibility?reservation_id={rid}").json()
    assert payload["wharf"] == {"popa_lo": 0.0, "popa_hi": 4000.0}
    band = payload["bands"][0]
    for key in ("popa_lo", "popa_hi", "dock_lo", "dock_hi", "length_ft",
                "bow_dock", "direction", "stern_popa_lo", "stern_popa_hi"):
        assert key in band
    # Dock No. is the reversed default: POPA 0 -> Dock 3365.
    assert band["dock_lo"] == pytest.approx(3365.0)
