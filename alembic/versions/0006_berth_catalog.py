"""berth catalog + reservation.berth_id

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-02

"Berths" were until now purely a concept — named station ranges with no table
behind them (the one ``wharf_segment`` row spans the whole quay). This adds a
``berth`` catalog: each berth is a NAMED canonical POPA station range
(``popa_sta_start`` .. ``popa_sta_end``), seeded from the port's berth shapefile
via ``data/gis`` (see ``app/seed/wharf_seed.py``).

``reservation`` gains a nullable ``berth_id`` FK. The berth is the operator's
*handle*; the canonical occupancy primitive is still ``station_range``. Assigning
a berth copies that berth's range onto ``station_range`` (see ``app/edit.py``),
so conflict detection and the ``no_wharf_overlap`` exclusion constraint keep
operating on the canonical range exactly as before. ``ondelete="SET NULL"``
mirrors ``vessel_id``: dropping a berth from the catalog must not delete bookings.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "berth",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        # Canonical POPA station span this berth covers (feet). lo < hi.
        sa.Column("popa_sta_start", sa.Numeric(12, 4), nullable=False),
        sa.Column("popa_sta_end", sa.Numeric(12, 4), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.CheckConstraint(
            "popa_sta_start < popa_sta_end", name="berth_station_ordered"
        ),
        sa.UniqueConstraint("name", name="uq_berth_name"),
    )

    op.add_column(
        "reservation",
        sa.Column("berth_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_reservation_berth_id", "reservation", "berth",
        ["berth_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("ix_reservation_berth_id", "reservation", ["berth_id"])


def downgrade() -> None:
    op.drop_index("ix_reservation_berth_id", table_name="reservation")
    op.drop_constraint("fk_reservation_berth_id", "reservation", type_="foreignkey")
    op.drop_column("reservation", "berth_id")
    op.drop_table("berth")
