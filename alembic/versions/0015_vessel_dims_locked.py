"""operator override lock for an AIS-tracked vessel's dimensions

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-01

Normally an AIS-tracked vessel (``mmsi`` present) owns its dimensions: ``loa =
dim_a + dim_b``, ``beam`` and ``draft`` come straight from ``ShipStaticData`` and
the manual Edit surface refuses to overwrite them (``app/edit._strip_ais_dims``),
because the feed is authoritative and would otherwise drift (migration 0014).

But AIS itself is sometimes wrong (a mis-encoded LOA, a stale draft), and an
operator needs an escape hatch to pin a corrected value. ``dims_locked`` is that
override: when set, the operator's manual loa/beam/draft **wins** and the AIS
ingestor stops overwriting the dimension columns for that vessel (see
``app/ais/ingest._upsert_vessel_static`` — the COALESCE-merge keeps the existing
value for a locked row). It is off by default, so nothing changes until an
operator explicitly overrides; clearing it hands the dimensions back to AIS,
which refills them on the next ``ShipStaticData``.

Non-dimension fields (name, callsign, ship_type, destination, imo) keep merging
from AIS regardless of the lock — only the dimensions are pinned.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "vessel",
        sa.Column(
            "dims_locked",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("vessel", "dims_locked")
