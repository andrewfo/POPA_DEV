"""DB-marked tests for the GET /reservations time-window filter.

Exercises the endpoint through FastAPI's TestClient against a live PostGIS,
auto-skipping (via the ``db_session`` fixture) when no migrated DB is around.
Each test seeds reservations inside the fixture's rolled-back transaction and
points the app's session dependency at that same connection, so nothing leaks.
"""
from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import get_session
from app.main import app

UTC = dt.timezone.utc


@pytest.fixture
def client(db_session):
    """A TestClient whose DB session is the test's transactional one."""
    app.dependency_overrides[get_session] = lambda: db_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_session, None)


def _add_reservation(session, *, lo, hi, t_start, t_end, status="requested"):
    """Insert a reservation directly; returns its id. Empty station range when
    lo/hi are None (mirrors an unassigned manual request)."""
    station = "'empty'::numrange" if lo is None else "numrange(:lo, :hi, '[]')"
    rid = session.execute(
        text(
            f"""
            INSERT INTO reservation
                (type, station_range, time_range, status, source, created_at)
            VALUES ('vessel', {station}, tstzrange(:t_start, :t_end, '[)'),
                    CAST(:status AS reservation_status), 'phone', now())
            RETURNING id
            """
        ),
        {"lo": lo, "hi": hi, "t_start": t_start, "t_end": t_end, "status": status},
    ).scalar_one()
    return rid


def _ids(payload):
    return {r["id"] for r in payload}


def test_window_includes_overlapping_excludes_outside(client, db_session):
    inside = _add_reservation(
        db_session, lo=400, hi=900,
        t_start=dt.datetime(2026, 7, 10, tzinfo=UTC),
        t_end=dt.datetime(2026, 7, 12, tzinfo=UTC),
    )
    outside = _add_reservation(
        db_session, lo=400, hi=900,
        t_start=dt.datetime(2026, 9, 1, tzinfo=UTC),
        t_end=dt.datetime(2026, 9, 3, tzinfo=UTC),
    )

    r = client.get("/reservations", params={
        "from": "2026-07-09T00:00:00Z", "to": "2026-07-11T00:00:00Z", "limit": 500,
    })
    assert r.status_code == 200
    got = _ids(r.json())
    assert inside in got
    assert outside not in got


def test_open_ended_reservation_is_included(client, db_session):
    # Still-alongside observed row: open upper bound -> overlaps any later window.
    open_ended = _add_reservation(
        db_session, lo=400, hi=900,
        t_start=dt.datetime(2026, 7, 1, tzinfo=UTC), t_end=None,
        status="observed",
    )
    r = client.get("/reservations", params={
        "from": "2026-08-01T00:00:00Z", "to": "2026-08-05T00:00:00Z", "limit": 500,
    })
    assert r.status_code == 200
    assert open_ended in _ids(r.json())


def test_no_window_returns_unfiltered(client, db_session):
    far = _add_reservation(
        db_session, lo=None, hi=None,
        t_start=dt.datetime(2030, 1, 1, tzinfo=UTC),
        t_end=dt.datetime(2030, 1, 2, tzinfo=UTC),
    )
    r = client.get("/reservations", params={"limit": 500})
    assert r.status_code == 200
    assert far in _ids(r.json())
