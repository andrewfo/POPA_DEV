"""intake provenance: add 'ai' to reservation_source / intake_source enums

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-10

The AI-assisted intake channel (``app/intake/llm.py`` + ``dataverse_run.py``) was
until now tagged ``source='email'`` — "the honest closest fit", because
editability was keyed off the *manual-channel* set and 'email' was the closest
human channel. That conflated **provenance** (how the request was parsed) with
**permission** (whether an operator may edit it), and made "how many requests
came through the AI channel?" unanswerable from the data.

This adds a real ``'ai'`` provenance value. Editability is decoupled separately
(an explicit ``EDITABLE_SOURCES`` set in ``app/intake/manual.py`` that *includes*
'ai'), so an AI card stays operator-editable without borrowing a human channel's
tag.

``ALTER TYPE ... ADD VALUE`` is wrapped in an ``autocommit_block`` (same pattern
as migration 0004). Removing an enum label needs the type recreated, so
``downgrade`` is a documented no-op.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE reservation_source ADD VALUE IF NOT EXISTS 'ai'")
        op.execute("ALTER TYPE intake_source ADD VALUE IF NOT EXISTS 'ai'")


def downgrade() -> None:
    # Postgres can't drop a single enum label without recreating the type (and
    # rewriting every dependent column). Not worth it for an additive change;
    # leave the label in place on downgrade. Existing 'ai' rows would need
    # re-tagging before the type could be narrowed.
    pass
