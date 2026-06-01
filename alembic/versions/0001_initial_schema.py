"""initial schema: wharf_segment, vessel, reservation, intake_event, position_report

Revision ID: 0001
Revises:
Create Date: 2026-06-01

Creates the conflict-safe wharf data layer. The headline piece is the
``no_wharf_overlap`` exclusion constraint on ``reservation``: it forbids two
CONFIRMED reservations from overlapping in BOTH time and station. It is
confirmed-only on purpose — ``observed`` AIS rows are ground truth and must be
allowed to overlap planned reservations, because that overlap is exactly the
signal we want to surface, not block.
"""
from typing import Sequence, Union

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Enum types. create_type=False because we create/drop them explicitly below;
# otherwise create_table would try to re-emit CREATE TYPE and fail.
reservation_type = postgresql.ENUM(
    "vessel", "dredge", "layberth", name="reservation_type", create_type=False
)
reservation_status = postgresql.ENUM(
    "observed", "requested", "tentative", "confirmed", "cancelled", "completed",
    name="reservation_status", create_type=False,
)
reservation_source = postgresql.ENUM(
    "ais", "form", "phone", "operator", name="reservation_source", create_type=False
)
direction = postgresql.ENUM(
    "upstream", "downstream", name="direction", create_type=False
)
intake_source = postgresql.ENUM(
    "ais", "form", "phone", "operator", name="intake_source", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()

    # Extensions: PostGIS for geometry, btree_gist so scalar/enum predicates can
    # participate in the GIST exclusion constraint alongside the range &&.
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    for enum in (
        reservation_type, reservation_status, reservation_source,
        direction, intake_source,
    ):
        enum.create(bind, checkfirst=True)

    # --- wharf_segment ---
    op.create_table(
        "wharf_segment",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column(
            "geom",
            geoalchemy2.Geometry(
                geometry_type="LINESTRINGM", srid=4326, spatial_index=False
            ),
            nullable=False,
        ),
        sa.Column("popa_sta_start", sa.Numeric(12, 4), nullable=False),
        sa.Column("popa_sta_end", sa.Numeric(12, 4), nullable=False),
        sa.Column("corps_scale", sa.Numeric(12, 6), nullable=False, server_default="1"),
        sa.Column("corps_offset", sa.Numeric(12, 4), nullable=False, server_default="0"),
        sa.Column("dockno_scale", sa.Numeric(12, 6), nullable=False, server_default="-1"),
        sa.Column("dockno_offset", sa.Numeric(12, 4), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.UniqueConstraint("name", name="uq_wharf_segment_name"),
    )
    op.create_index(
        "ix_wharf_segment_geom", "wharf_segment", ["geom"], postgresql_using="gist"
    )

    # --- vessel ---
    op.create_table(
        "vessel",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mmsi", sa.BigInteger()),
        sa.Column("imo", sa.BigInteger()),
        sa.Column("name", sa.String(length=120)),
        sa.Column("callsign", sa.String(length=32)),
        sa.Column("ship_type", sa.Integer()),
        sa.Column("loa", sa.Numeric(8, 2)),
        sa.Column("beam", sa.Numeric(8, 2)),
        sa.Column("draft", sa.Numeric(6, 2)),
        sa.Column("destination", sa.String(length=120)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.CheckConstraint(
            "mmsi IS NOT NULL OR imo IS NOT NULL",
            name="vessel_requires_mmsi_or_imo",
        ),
        sa.UniqueConstraint("mmsi", name="uq_vessel_mmsi"),
    )
    op.create_index("ix_vessel_mmsi", "vessel", ["mmsi"])
    op.create_index("ix_vessel_imo", "vessel", ["imo"])

    # --- reservation ---
    op.create_table(
        "reservation",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("vessel_id", sa.Integer()),
        sa.Column("type", reservation_type, nullable=False),
        sa.Column("station_range", postgresql.NUMRANGE(), nullable=False),
        sa.Column("time_range", postgresql.TSTZRANGE(), nullable=False),
        sa.Column("direction", direction),
        sa.Column("status", reservation_status, nullable=False),
        sa.Column("source", reservation_source, nullable=False),
        sa.Column("priority", sa.Integer()),
        sa.Column("cargo", sa.String(length=200)),
        sa.Column("notes", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["vessel_id"], ["vessel.id"], ondelete="SET NULL"
        ),
    )
    op.create_index("ix_reservation_status", "reservation", ["status"])
    op.create_index("ix_reservation_vessel_id", "reservation", ["vessel_id"])

    # No two CONFIRMED reservations may overlap in BOTH time and station.
    op.execute(
        """
        ALTER TABLE reservation
        ADD CONSTRAINT no_wharf_overlap
        EXCLUDE USING gist (time_range WITH &&, station_range WITH &&)
        WHERE (status = 'confirmed')
        """
    )

    # --- intake_event ---
    op.create_table(
        "intake_event",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", intake_source, nullable=False),
        sa.Column("raw", postgresql.JSONB(), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column("processed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reservation_id", sa.Integer()),
        sa.ForeignKeyConstraint(
            ["reservation_id"], ["reservation.id"], ondelete="SET NULL"
        ),
    )

    # --- position_report ---
    op.create_table(
        "position_report",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("vessel_id", sa.Integer()),
        sa.Column("mmsi", sa.BigInteger()),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lon", sa.Float(), nullable=False),
        sa.Column(
            "geom",
            geoalchemy2.Geometry(
                geometry_type="POINT", srid=4326, spatial_index=False
            ),
            nullable=False,
        ),
        sa.Column("sog", sa.Float()),
        sa.Column("cog", sa.Float()),
        sa.Column("heading", sa.Float()),
        sa.Column("nav_status", sa.Integer()),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="ais"),
        sa.Column("msg_ts", sa.DateTime(timezone=True)),
        sa.Column("raw", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["vessel_id"], ["vessel.id"], ondelete="SET NULL"
        ),
    )
    op.create_index("ix_position_report_vessel_id", "position_report", ["vessel_id"])
    op.create_index("ix_position_report_mmsi", "position_report", ["mmsi"])
    op.create_index(
        "ix_position_report_mmsi_ts", "position_report", ["mmsi", "msg_ts"]
    )
    op.create_index(
        "ix_position_report_geom", "position_report", ["geom"], postgresql_using="gist"
    )


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_index("ix_position_report_geom", table_name="position_report")
    op.drop_index("ix_position_report_mmsi_ts", table_name="position_report")
    op.drop_index("ix_position_report_mmsi", table_name="position_report")
    op.drop_index("ix_position_report_vessel_id", table_name="position_report")
    op.drop_table("position_report")

    op.drop_table("intake_event")

    op.execute("ALTER TABLE reservation DROP CONSTRAINT IF EXISTS no_wharf_overlap")
    op.drop_index("ix_reservation_vessel_id", table_name="reservation")
    op.drop_index("ix_reservation_status", table_name="reservation")
    op.drop_table("reservation")

    op.drop_index("ix_vessel_imo", table_name="vessel")
    op.drop_index("ix_vessel_mmsi", table_name="vessel")
    op.drop_table("vessel")

    op.drop_index("ix_wharf_segment_geom", table_name="wharf_segment")
    op.drop_table("wharf_segment")

    for enum in (
        intake_source, direction, reservation_source,
        reservation_status, reservation_type,
    ):
        enum.drop(bind, checkfirst=True)
