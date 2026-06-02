"""Shared pytest fixtures.

``db_session`` yields a transactional session against a live Postgres+PostGIS,
and SKIPS (rather than fails) when no database is reachable — so the pure-math
crosswalk tests always run, even with no DB around.

Connectivity is probed ONCE per session (``_db_engine``) with a short
``connect_timeout``: without a live DB the probe fails in ~1 s and every
``db_session`` test then skips instantly off the cached result, instead of each
test blocking the full TCP connect timeout (~21 s on Windows) on its own engine.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import get_settings

# Fail a dead connection fast so the no-DB path skips quickly. libpq's minimum
# effective connect_timeout is 2 s; that bounds the whole session's DB probe.
_CONNECT_TIMEOUT_S = 2


@pytest.fixture(scope="session")
def _db_engine():
    """Probe the database once. Skips (cached for the whole session, so the
    probe runs a single time) when no migrated PostGIS is reachable."""
    url = os.environ.get("DATABASE_URL") or get_settings().sqlalchemy_url
    engine = create_engine(
        url, future=True, connect_args={"connect_timeout": _CONNECT_TIMEOUT_S}
    )
    try:
        conn = engine.connect()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no database available ({exc})")

    # Require the schema to exist (migrations applied) and PostGIS present.
    try:
        conn.execute(text("SELECT PostGIS_Lib_Version()"))
        conn.execute(text("SELECT 1 FROM wharf_segment LIMIT 1"))
    except Exception as exc:  # noqa: BLE001
        conn.close()
        pytest.skip(f"schema not migrated / PostGIS missing ({exc})")
    conn.close()
    return engine


@pytest.fixture
def db_session(_db_engine):
    # Run inside a transaction we roll back, so tests never leave state behind.
    conn = _db_engine.connect()
    trans = conn.begin()
    Session = sessionmaker(bind=conn, future=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        conn.close()
