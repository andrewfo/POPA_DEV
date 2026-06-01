"""Shared pytest fixtures.

``db_session`` yields a transactional session against a live Postgres+PostGIS,
and SKIPS (rather than fails) when no database is reachable — so the pure-math
crosswalk tests always run, even with no DB around.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import get_settings


@pytest.fixture
def db_session():
    url = os.environ.get("DATABASE_URL") or get_settings().sqlalchemy_url
    engine = create_engine(url, future=True)
    try:
        conn = engine.connect()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no database available ({exc})")

    # Run inside a transaction we roll back, so tests never leave state behind.
    trans = conn.begin()
    Session = sessionmaker(bind=conn, future=True)
    session = Session()

    # Require the schema to exist (migrations applied) and PostGIS present.
    try:
        session.execute(text("SELECT PostGIS_Lib_Version()"))
        session.execute(text("SELECT 1 FROM wharf_segment LIMIT 1"))
    except Exception as exc:  # noqa: BLE001
        session.close()
        trans.rollback()
        conn.close()
        pytest.skip(f"schema not migrated / PostGIS missing ({exc})")

    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        conn.close()
