"""depth cells — 2-D (station × cross-channel offset) depth field for visualization

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-01

Migration 0012 reduced each survey to a per-station *controlling* (shallowest)
depth — one number per station bin, collapsing the cross-channel dimension. That
is exactly what the draft gate needs (the governing depth over a footprint), but
it throws away how the bottom shoals *out into the channel*: a station's soundings
run from ~8 ft to ~150 ft off the quay face at a range of depths, and only their
minimum survives.

``depth_cell`` keeps that second dimension for the map's cross-section overlay: a
2-D grid binned by station AND by perpendicular offset-from-quay, each cell
carrying its own shallowest sounding (feet below datum). It is purely a
visualization layer — the gate still reads ``depth_segment`` unchanged (this table
is populated in the *same* ingest pass, so the two never disagree: a station's
``depth_segment`` controlling depth is the min over that station's ``depth_cell``
row). The raw soundings are still not stored; this is a coarser reduction, not the
point cloud.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import NUMRANGE

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "depth_cell",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("survey_id", sa.Integer(), nullable=False),
        # Half-open POPA station bin [lo, hi) in feet (same binning as
        # depth_segment — a station's cells tile that station's segment).
        sa.Column("popa_range", NUMRANGE(), nullable=False),
        # Half-open perpendicular offset bin [lo, hi) in FEET off the quay-face
        # centerline — the cross-channel axis. Cells at increasing offset march
        # out into the channel, where the bottom generally deepens.
        sa.Column("offset_range", NUMRANGE(), nullable=False),
        # Shallowest sounding in this 2-D cell, feet below datum.
        sa.Column("controlling_depth_ft", sa.Numeric(6, 2), nullable=False),
        # Soundings that fell in this cell (confidence / debugging).
        sa.Column("point_count", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["survey_id"], ["depth_survey.id"],
            name="fk_depth_cell_survey_id", ondelete="CASCADE",
        ),
    )
    op.create_index("ix_depth_cell_survey_id", "depth_cell", ["survey_id"])


def downgrade() -> None:
    op.drop_index("ix_depth_cell_survey_id", table_name="depth_cell")
    op.drop_table("depth_cell")
