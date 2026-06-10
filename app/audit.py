"""Write-audit trail — who changed what.

The whole app sits behind a single shared HTTP-Basic credential (``app/auth.py``),
so until now there was no record of which writes happened or by whom: a stray
edit or delete left no trace. Every mutating endpoint now lands one ``audit_log``
row (migration 0010) inside the same transaction as the write it records, so the
log commits atomically with the change (and rolls back if the change does).

``actor`` is the Basic username (``None`` when auth is disabled — dev and the
TestClient suite run open). ``detail`` is free-form JSONB context: the
pre-edit/pre-delete ``raw`` payload, the changed-field list, the swept
reservation ids — whatever makes the entry self-explanatory later.

This is the *write* helper; reading the trail is a future surface (no endpoint
exposes it yet). It does NOT commit — the endpoint owns the transaction boundary,
exactly like the intake / edit session functions.
"""
from __future__ import annotations

from sqlalchemy import insert
from sqlalchemy.orm import Session

from app.models import AuditLog


def record_audit(
    session: Session,
    *,
    actor: str | None,
    action: str,
    entity: str,
    entity_id: int | None = None,
    detail: dict | None = None,
) -> None:
    """Append one audit row. ``action`` is the write kind
    (``create``/``edit``/``delete``/``sweep``), ``entity`` the table it touched
    (``intake_event``/``reservation``/``vessel``). Append-only and constraint-free,
    so this never blocks the write it records."""
    session.execute(
        insert(AuditLog).values(
            actor=actor,
            action=action,
            entity=entity,
            entity_id=entity_id,
            detail=detail,
        )
    )
