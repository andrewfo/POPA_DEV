"""Worker liveness heartbeats.

The background workers each run as their own process/container (the live AIS
ingestor, the occupancy + verification-sweep batch, the optional AI/Dataverse
intake poller). The console's single "data layer" dot only ever reflected
whether the API/DB answered — it said nothing about whether those workers are
alive. Each worker now stamps a ``worker_heartbeat`` row (migration 0011) every
cycle, and ``GET /workers`` reads it back so the footer can show a dot per
worker.

``beat`` is the worker-side call: it opens its OWN short-lived session, upserts,
commits, and swallows any error — a heartbeat must never crash or stall the
worker it tracks, and it must commit independently of the worker's own
transaction (so an ``error`` beat lands even when the cycle's transaction rolled
back). ``record_heartbeat`` is the session-scoped form (no commit) for callers
that already own a transaction. ``classify`` is the pure liveness rule the
endpoint applies.
"""
from __future__ import annotations

import logging

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import WorkerHeartbeat

logger = logging.getLogger(__name__)

# Canonical worker registry: name -> (human label, nominal cadence seconds). The
# console lists exactly these — even one that has never beat (shown "offline") —
# so a disabled/never-started worker is visible rather than silently missing.
# The cadence is how often the worker is expected to beat; health is judged
# against a multiple of it (see classify), so it need only be the right ballpark.
KNOWN_WORKERS: dict[str, tuple[str, int]] = {
    # AIS ingestor beats on each commit; a busy waterway commits every few sec,
    # bounded by the ingestor's commit_interval_s (~10s).
    "ais": ("AIS ingest", 15),
    # Occupancy batch loops on OCC_EVERY (compose default 60s).
    "occupancy": ("Occupancy + sweep", 60),
    # AI intake poller loops on DATAVERSE_POLL_SECONDS (default 300s); stays
    # offline when the channel isn't configured (it self-exits, never beating).
    "intake-dataverse": ("AI intake", 300),
}


def record_heartbeat(
    session: Session, *, name: str, status: str, detail: dict | None = None
) -> None:
    """Upsert this worker's single heartbeat row. Does NOT commit — the caller
    owns the transaction (mirrors record_audit / the intake/edit helpers)."""
    stmt = (
        pg_insert(WorkerHeartbeat)
        .values(name=name, status=status, detail=detail, beat_at=func.now())
        .on_conflict_do_update(
            index_elements=["name"],
            set_={"status": status, "detail": detail, "beat_at": func.now()},
        )
    )
    session.execute(stmt)


def beat(name: str, status: str, detail: dict | None = None) -> None:
    """Self-contained heartbeat for a worker process: own short session, commit,
    and swallow any failure. A heartbeat must never take down the worker it
    tracks, and it commits independently so an ``error`` beat survives a
    rolled-back cycle transaction."""
    try:
        session = SessionLocal()
        try:
            record_heartbeat(session, name=name, status=status, detail=detail)
            session.commit()
        finally:
            session.close()
    except Exception:  # noqa: BLE001 - liveness telemetry is best-effort
        logger.warning("heartbeat write failed (%s=%s)", name, status, exc_info=True)


def classify(status: str | None, age_seconds: float | None, cadence_seconds: int) -> str:
    """Derive a worker's health for the console.

    ``offline`` — never beat (no row). ``error`` — last cycle raised. ``stale`` —
    beat too long ago (more than three missed cadences, with a floor so a fast
    worker isn't flagged on one slow tick). ``ok`` otherwise.
    """
    if status is None or age_seconds is None:
        return "offline"
    if status == "error":
        return "error"
    if age_seconds > max(cadence_seconds * 3, 45):
        return "stale"
    return "ok"
