"""audit trail: intake_event soft-delete + audit_log table

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-10

``intake_event`` calls itself "the audit + reconciliation trail", but deleting a
manual berth request hard-``DELETE``d the row — an operator (or a stray click)
could erase the evidence that a request ever arrived. Two changes preserve the
claim at near-zero cost:

1. **Soft-delete** ``intake_event``. A new nullable ``deleted_at`` marks a
   removed request; the row (and its verbatim ``raw`` payload) stays for audit.
   The dedupe unique index is repartitioned to ``WHERE dedupe_key IS NOT NULL
   AND deleted_at IS NULL`` so (a) a soft-deleted row no longer blocks a
   legitimate re-submission of the same content, and (b) the live-row uniqueness
   the ``ON CONFLICT`` idempotency relies on still holds (its ``index_where`` in
   ``app/intake/manual.py`` is updated to match this predicate exactly).

2. **``audit_log``** — who did what. With a single shared HTTP-Basic credential
   there was no record of which writes happened or by whom. Every mutating
   endpoint now lands one row here (actor = the Basic username, NULL when auth is
   disabled; action/entity/entity_id; a small JSONB ``detail`` carrying e.g. the
   pre-edit/pre-delete raw payload). It is append-only and has no constraints
   that a write could trip, so logging never blocks the write it records.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Soft-delete marker + repartitioned dedupe index.
    op.add_column(
        "intake_event",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_index("uq_intake_event_dedupe_key", table_name="intake_event")
    op.create_index(
        "uq_intake_event_dedupe_key",
        "intake_event",
        ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text("dedupe_key IS NOT NULL AND deleted_at IS NULL"),
    )

    # 2. Append-only write audit. No FKs (the referenced row may be soft-deleted
    #    or, for a reservation, hard-deleted — the log must outlive both).
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        # The HTTP-Basic principal; NULL when auth is disabled (dev/tests run open).
        sa.Column("actor", sa.String(length=120), nullable=True),
        # 'create' | 'edit' | 'delete' | 'sweep' (the write that occurred).
        sa.Column("action", sa.String(length=32), nullable=False),
        # 'intake_event' | 'reservation' | 'vessel' (what it touched).
        sa.Column("entity", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        # Free-form context: pre-edit/pre-delete raw, changed field list, etc.
        sa.Column("detail", JSONB(), nullable=True),
        sa.Column(
            "at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
    )
    op.create_index("ix_audit_log_at", "audit_log", ["at"])
    op.create_index("ix_audit_log_entity", "audit_log", ["entity", "entity_id"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_entity", table_name="audit_log")
    op.drop_index("ix_audit_log_at", table_name="audit_log")
    op.drop_table("audit_log")

    op.drop_index("uq_intake_event_dedupe_key", table_name="intake_event")
    op.create_index(
        "uq_intake_event_dedupe_key",
        "intake_event",
        ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text("dedupe_key IS NOT NULL"),
    )
    op.drop_column("intake_event", "deleted_at")
