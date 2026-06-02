"""SQLAlchemy engine / session plumbing."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.tz import TZ_NAME

_settings = get_settings()

# connect_timeout keeps a downed/unreachable DB from hanging each request for the
# driver default (~tens of seconds) before failing — fail fast, surface a 503.
# `options=-c timezone=...` pins every connection to Central Time (the canonical
# wall-clock — see app/tz.py), so timestamptz columns render in Central, now() is
# Central, and any naive timestamp binds as Central. Migration 0008 sets the same
# default on the database itself; this guarantees it per-session regardless.
engine = create_engine(
    _settings.sqlalchemy_url,
    pool_pre_ping=True,
    future=True,
    connect_args={
        "connect_timeout": _settings.db_connect_timeout,
        "options": f"-c timezone={TZ_NAME}",
    },
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: yields a session and always closes it."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
