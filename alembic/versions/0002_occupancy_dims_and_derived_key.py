"""occupancy support: vessel A/B dims + reservation.derived_key

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-02

Step 5 (occupancy derivation) needs two things the initial schema lacked:

* ``vessel.dim_a`` / ``vessel.dim_b`` — the AIS position-reference offsets to
  bow (A) and stern (B), in metres. We already parse A/B/C/D off ShipStaticData
  but previously only kept their sums (LOA = A+B, beam = C+D). Bow/stern
  projection needs A and B individually to place the antenna correctly within
  the hull; without them we fall back to a symmetric LOA/2 split (low
  confidence).
* ``reservation.derived_key`` — a stable identity for a derived ``observed``
  reservation so re-deriving over the same window UPDATEs instead of
  duplicating. Unique only when non-null (planned reservations never set it),
  so a partial unique index is used.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("vessel", sa.Column("dim_a", sa.Numeric(8, 2)))
    op.add_column("vessel", sa.Column("dim_b", sa.Numeric(8, 2)))

    op.add_column("reservation", sa.Column("derived_key", sa.Text()))
    # Unique only among derived rows; planned reservations leave it NULL.
    op.create_index(
        "uq_reservation_derived_key",
        "reservation",
        ["derived_key"],
        unique=True,
        postgresql_where=sa.text("derived_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_reservation_derived_key", table_name="reservation")
    op.drop_column("reservation", "derived_key")
    op.drop_column("vessel", "dim_b")
    op.drop_column("vessel", "dim_a")
