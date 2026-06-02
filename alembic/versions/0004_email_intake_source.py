"""intake channel: add 'email' to reservation_source / intake_source enums

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-02

Some agents don't submit the online Adobe Sign form or phone in — they email the
berth request. Manual entry (``app/intake/manual.py``) captures those by hand, so
the source enums need an ``email`` value alongside the existing
``ais|form|phone|operator``.

``ALTER TYPE ... ADD VALUE`` cannot run inside a transaction block that later
*uses* the new label; we don't use it here, but we still wrap it in an
``autocommit_block`` so it's robust across Postgres versions. Removing an enum
label is not supported by Postgres without recreating the type, so ``downgrade``
is a documented no-op.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE reservation_source ADD VALUE IF NOT EXISTS 'email'")
        op.execute("ALTER TYPE intake_source ADD VALUE IF NOT EXISTS 'email'")


def downgrade() -> None:
    # Postgres can't drop a single enum label without recreating the type (and
    # rewriting every dependent column). Not worth it for an additive change;
    # leave the label in place on downgrade.
    pass
