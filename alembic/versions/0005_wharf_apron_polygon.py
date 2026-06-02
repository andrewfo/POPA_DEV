"""wharf_segment.apron: digitized berthing-zone polygon

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-02

The occupancy detector's "alongside" test was a symmetric ``ST_DWithin`` buffer
of the centerline (``app/occupancy/alongside.py``). This adds a real, digitized
**apron polygon** — a strip on the WATER side of the quay face (a berthed
vessel's AIS antenna floats off the quay, not on the landward berth rectangle).
Built by ``data/gis/build_centerline.py`` from the ArcGIS berth polygons and the
quay-face water normal; seeded by ``app/seed/wharf_seed.py`` from
``data/gis/apron_polygon.json``.

Nullable: a segment with no digitized apron falls back to the centerline buffer,
so existing data keeps working until re-seeded.
"""
from typing import Sequence, Union

import geoalchemy2
import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "wharf_segment",
        sa.Column(
            "apron",
            # spatial_index=False: the GIST index is created explicitly below,
            # mirroring how geom was handled in 0001.
            geoalchemy2.Geometry(
                geometry_type="POLYGON", srid=4326, spatial_index=False
            ),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_wharf_segment_apron", "wharf_segment", ["apron"], postgresql_using="gist"
    )


def downgrade() -> None:
    op.drop_index("ix_wharf_segment_apron", table_name="wharf_segment")
    op.drop_column("wharf_segment", "apron")
