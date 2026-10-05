"""last-writer provenance: updated_at + last_actor on intake_event and reservation

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-05

Answers "who last wrote this record, and when" so an operator can trust what they
see when a berth request exists from both the agent (form/AI) and a manual channel.

Both tables gain:

- ``updated_at timestamptz`` (server default ``now()``) — stamped on every write
  (create and edit), so a card can show how fresh the current values are, distinct
  from ``received_at`` / ``created_at`` (when the record first arrived).
- ``last_actor text`` — the Basic-auth username (``request.state.operator``) of the
  writer. NULL for agent / AIS / unauthenticated writes, which is exactly the
  signal "no human touched this".

Backfilled so existing rows read as "last written when they arrived, by an unknown
actor": ``updated_at`` from ``received_at`` (intake_event) / ``created_at``
(reservation); ``last_actor`` left NULL.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for table in ("intake_event", "reservation"):
        op.add_column(
            table,
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=True,
            ),
        )
        op.add_column(table, sa.Column("last_actor", sa.Text(), nullable=True))

    # Backfill: existing rows were last written when they arrived.
    op.execute("UPDATE intake_event SET updated_at = received_at")
    op.execute("UPDATE reservation SET updated_at = created_at")


def downgrade() -> None:
    for table in ("reservation", "intake_event"):
        op.drop_column(table, "last_actor")
        op.drop_column(table, "updated_at")
