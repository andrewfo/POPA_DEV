"""intake idempotency: intake_event.dedupe_key

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-02

Berth-request intake (Adobe Sign forms harvested into a SharePoint list, exported
to CSV by Power Automate) lands raw rows in ``intake_event``. Power Automate
re-exports the whole list periodically, so re-ingesting the same export must not
duplicate. ``dedupe_key`` is a stable content hash of the raw row; an
``ON CONFLICT DO NOTHING`` insert keyed on it makes re-imports idempotent.

Unique only when non-null (a phone/operator intake may have no stable payload),
so a partial unique index is used — same pattern as ``reservation.derived_key``.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("intake_event", sa.Column("dedupe_key", sa.Text()))
    op.create_index(
        "uq_intake_event_dedupe_key",
        "intake_event",
        ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text("dedupe_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_intake_event_dedupe_key", table_name="intake_event")
    op.drop_column("intake_event", "dedupe_key")
