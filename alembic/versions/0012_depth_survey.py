"""depth surveys — controlling-depth data layer for the draft gate

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-25

The build order's step-6 draft-vs-controlling-depth gate was deferred for one
reason: "no depth data layer yet". A vessel may only be CONFIRMED at a berth if
its draft clears the controlling depth over the station range it occupies — but
nothing held depth. This adds that layer, fed by the port's periodic
hydrographic condition surveys (an ``.XYZ`` point cloud of soundings in Texas
South Central State Plane ftUS, EPSG:2278).

Two tables:

* ``depth_survey`` — one row per uploaded survey. Surveys are VERSIONED, not
  overwritten: depths change constantly (shoaling, dredging), so each upload is
  a new dated row and the gate reads the *latest active* survey covering a
  station range. ``active`` lets an operator disable a bad import without losing
  the audit trail.
* ``depth_segment`` — the survey reduced to a per-station controlling depth.
  Each row is a station bin (a half-open POPA ``numrange``) carrying the
  *shallowest* sounding in that bin (``controlling_depth_ft`` — controlling =
  governing = shallowest) within the berthing zone. A GiST index on the range
  drives the gate's ``&&`` overlap lookup against a reservation's
  ``station_range``.

The raw soundings are NOT stored row-by-row (a survey is ~400k points); only the
reduced profile lives in the DB. The reduction (project each sounding onto the
measured centerline -> POPA station, clip to the berthing zone, bin, take the
min) runs in PostGIS — see ``app/depth/ingest.py``.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import NUMRANGE

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "depth_survey",
        sa.Column("id", sa.Integer(), primary_key=True),
        # Date the survey was taken (parsed from the filename, operator-editable).
        sa.Column("surveyed_at", sa.Date(), nullable=False),
        # Original filename, for provenance / the audit trail.
        sa.Column("source_file", sa.Text(), nullable=True),
        # Planar CRS the uploaded soundings were in (PostGIS transforms it to the
        # 4326 measured centerline). POPA's condition surveys are Texas South
        # Central State Plane ftUS = EPSG:2278.
        sa.Column("srid", sa.Integer(), nullable=False, server_default="2278"),
        # Vertical datum the depths reference (free text, e.g. "MLLW"). Not
        # modelled further — the gate compares draft to depth in the same datum.
        sa.Column("datum", sa.Text(), nullable=True),
        # Number of soundings actually binned (after the berthing-zone clip).
        sa.Column("point_count", sa.Integer(), nullable=True),
        # POPA station extent the survey covers (feet) — drives the gate's
        # "does any survey cover this range?" test.
        sa.Column("station_min", sa.Numeric(12, 4), nullable=True),
        sa.Column("station_max", sa.Numeric(12, 4), nullable=True),
        # Shallowest controlling depth anywhere in the survey (feet) — a quick
        # headline for the UI.
        sa.Column("min_depth_ft", sa.Numeric(6, 2), nullable=True),
        # Station bin width used for the reduction (feet), recorded so a later
        # re-run with a different resolution is self-describing.
        sa.Column("bin_ft", sa.Numeric(8, 2), nullable=True),
        # An operator can disable a survey (e.g. a bad import) without deleting
        # it; the gate only reads active surveys.
        sa.Column(
            "active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
    )

    op.create_table(
        "depth_segment",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("survey_id", sa.Integer(), nullable=False),
        # Half-open POPA station bin [lo, hi) in feet — tiles the survey without
        # double-counting boundary soundings.
        sa.Column("popa_range", NUMRANGE(), nullable=False),
        # Controlling (shallowest) sounding in this bin, feet below datum.
        sa.Column("controlling_depth_ft", sa.Numeric(6, 2), nullable=False),
        # How many soundings fell in this bin (confidence / debugging).
        sa.Column("point_count", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["survey_id"], ["depth_survey.id"],
            name="fk_depth_segment_survey_id", ondelete="CASCADE",
        ),
    )
    # GiST over the range drives the gate's && overlap against station_range.
    # (Range types have a built-in GiST opclass — no btree_gist needed here.)
    op.create_index(
        "ix_depth_segment_popa_range", "depth_segment", ["popa_range"],
        postgresql_using="gist",
    )
    op.create_index("ix_depth_segment_survey_id", "depth_segment", ["survey_id"])
    # Latest-active-survey lookup.
    op.create_index(
        "ix_depth_survey_active", "depth_survey", ["active", "surveyed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_depth_survey_active", table_name="depth_survey")
    op.drop_index("ix_depth_segment_survey_id", table_name="depth_segment")
    op.drop_index("ix_depth_segment_popa_range", table_name="depth_segment")
    op.drop_table("depth_segment")
    op.drop_table("depth_survey")
