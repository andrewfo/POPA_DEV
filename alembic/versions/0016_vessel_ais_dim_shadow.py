"""shadow the last AIS-reported dimensions so Revert-to-AIS is immediate

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-29

The ``dims_locked`` override (migration 0015) overwrites ``loa``/``beam``/``draft``
in place with the operator's pinned value, so the original AIS figures are lost
from the row — a later "revert to AIS" could only clear the lock and wait for the
next ``ShipStaticData`` to refill them.

These shadow columns hold the **last dimensions AIS reported**, maintained by the
ingestor on *every* ``ShipStaticData`` even while the row is locked
(``app/ais/ingest._upsert_vessel_static`` writes ``ais_loa/ais_beam/ais_draft``
un-gated by the lock, unlike the live dimension columns). Reverting an override
then restores ``loa``/``beam``/``draft`` from the shadow **immediately**
(``app/edit.update_vessel``) instead of waiting for a broadcast.

Backfilled from the current live dims for **unlocked** AIS-tracked rows (where the
live dimension already *is* the AIS value today). Locked rows are left NULL — the
AIS value was already overwritten and can't be recovered, so a revert on such a
pre-existing lock falls back to unlock-only (the feed refills on the next
broadcast). Non-dimension fields are unaffected.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("vessel", sa.Column("ais_loa", sa.Numeric(8, 2), nullable=True))
    op.add_column("vessel", sa.Column("ais_beam", sa.Numeric(8, 2), nullable=True))
    op.add_column("vessel", sa.Column("ais_draft", sa.Numeric(6, 2), nullable=True))
    # Seed the shadow for rows whose live dims ARE the AIS value today (AIS-tracked
    # and not overridden). Locked rows can't be recovered — leave them NULL.
    op.execute(
        """
        UPDATE vessel
           SET ais_loa = loa, ais_beam = beam, ais_draft = draft
         WHERE mmsi IS NOT NULL AND dims_locked = false
        """
    )


def downgrade() -> None:
    op.drop_column("vessel", "ais_draft")
    op.drop_column("vessel", "ais_beam")
    op.drop_column("vessel", "ais_loa")
