"""SQLAlchemy engine / session plumbing."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

_settings = get_settings()

# connect_timeout keeps a downed/unreachable DB from hanging each request for the
# driver default (~tens of seconds) before failing — fail fast, surface a 503.
engine = create_engine(
    _settings.sqlalchemy_url,
    pool_pre_ping=True,
    future=True,
    connect_args={"connect_timeout": _settings.db_connect_timeout},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: yields a session and always closes it."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
