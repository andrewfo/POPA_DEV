"""Land normalized berth-request records into ``intake_event``.

Source-agnostic: it consumes ``BerthRequest`` records from any ``IntakeSource``
and appends one ``intake_event`` row per request, *raw exactly as received*
(``processed=False`` — request→reservation reconciliation is a later layer).

Idempotent: each row gets a ``dedupe_key`` (a content hash of the raw payload),
inserted ``ON CONFLICT DO NOTHING``, so re-ingesting the same export — Power
Automate re-exports the whole list — adds nothing. A genuinely edited row hashes
differently and lands as a new event, which is the audit trail we want.
"""
from __future__ import annotations

import hashlib
import json
import logging

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.intake.records import BerthRequest
from app.intake.source import IntakeSource
from app.models import IntakeEvent

logger = logging.getLogger(__name__)


def dedupe_key(raw: dict) -> str:
    """Stable content hash of a raw intake row (order-independent)."""
    blob = json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class IntakeIngestor:
    def __init__(
        self, session: Session, source: str = "form", commit_every: int = 200
    ) -> None:
        self.session = session
        self.source = source
        self.commit_every = commit_every
        self._pending = 0
        self.seen = 0
        self.inserted = 0
        self.skipped = 0  # duplicates (already landed)
        self.warnings = 0

    def handle(self, rec: BerthRequest) -> None:
        self.seen += 1
        if rec.warnings:
            self.warnings += 1
        stmt = (
            pg_insert(IntakeEvent)
            .values(
                source=self.source,
                raw=rec.raw,
                dedupe_key=dedupe_key(rec.raw),
                processed=False,
            )
            .on_conflict_do_nothing(index_elements=["dedupe_key"])
            .returning(IntakeEvent.id)
        )
        landed = self.session.execute(stmt).scalar_one_or_none()
        if landed is None:
            self.skipped += 1
        else:
            self.inserted += 1
        self._pending += 1
        if self._pending >= self.commit_every:
            self.flush()

    def flush(self) -> None:
        if self._pending:
            self.session.commit()
            self._pending = 0

    def run(self, source: IntakeSource) -> None:
        try:
            for rec in source.stream():
                self.handle(rec)
        finally:
            self.flush()
        logger.info(
            "intake done: seen=%d inserted=%d skipped=%d rows_with_warnings=%d",
            self.seen, self.inserted, self.skipped, self.warnings,
        )
