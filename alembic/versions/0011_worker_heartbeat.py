"""worker heartbeat table — per-worker liveness for the ops console

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-22

The console carried a single "data layer" status dot, which only reflected
whether the API/DB answered ``/stats`` — it said nothing about whether the
background workers (the live AIS ingestor, the occupancy + verification-sweep
batch, the optional AI/Dataverse intake poller) are actually alive. They run as
separate processes/containers, so the only shared place to record "I ran at T"
is the database.

``worker_heartbeat`` is one row per worker (keyed on ``name``), upserted each
cycle with a status (``ok``/``error``) and a small JSONB ``detail`` (the last
batch's counts, or an error string). ``GET /workers`` reads it back, derives
each worker's health from how stale its last beat is against the worker's
nominal cadence, and the footer renders a dot per worker. It is liveness
telemetry, not an audit trail — a worker overwrites its own single row, so the
table never grows.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "worker_heartbeat",
        # The worker's stable name ('ais' | 'occupancy' | 'intake-dataverse').
        # One row per worker; each cycle upserts it (never appends), so the table
        # stays at most a handful of rows.
        sa.Column("name", sa.String(length=64), primary_key=True),
        # 'ok' (a cycle completed) | 'error' (the cycle raised).
        sa.Column("status", sa.String(length=16), nullable=False),
        # Free-form context: last batch counts, last error message, etc.
        sa.Column("detail", JSONB(), nullable=True),
        sa.Column(
            "beat_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("worker_heartbeat")
