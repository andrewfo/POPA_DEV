"""75 ft minimum separation in the no-overlap exclusion constraint

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-02

Until now ``no_wharf_overlap`` blocked two CONFIRMED reservations only when their
station ranges strictly **overlapped**. The port requires a clear mooring gap
between adjacent vessels, so two boats that merely sit close (but don't touch)
must also be rejected: there must be **at least 75 ft between all boats**.

We keep the single (time x station) primitive and the confirmed-only rule; we
just make the station side of the ``&&`` test operate on a **buffered** range.
Each non-empty station range is padded by half the required gap (37.5 ft) on
each side; two padded ranges then overlap exactly when the real gap between the
hulls is **less than** ``GAP_FT``. Padding both sides by ``GAP_FT/2`` means the
total required clearance between any two boats is ``GAP_FT``.

Boundary: padded ranges are built with the default ``[)`` bounds, so a real gap
of *exactly* 75 ft leaves the upper-exclusive and lower-inclusive edges touching
without sharing a point — i.e. exactly 75 ft is allowed; anything tighter
conflicts.

Empty ranges (an unassigned ``requested`` row) stay empty and never participate,
exactly as before — ``numrange`` on the bounds of an empty range would become
unbounded and collide with everything, so the ``CASE`` guards it explicitly.

The expression is built from immutable functions (``isempty``/``lower``/
``upper``/``numrange`` + numeric arithmetic), which an exclusion constraint
requires. ``GAP_FT`` is also mirrored in ``app.config.Settings.min_vessel_gap_ft``
for app-side use; the constraint bakes in the literal here (it cannot read app
config), so the two must be changed together via a new migration.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Required clear separation between two confirmed vessels (feet) and the
# per-side pad (half the gap) applied to each station range before the && test.
GAP_FT = 75.0
HALF_GAP_FT = GAP_FT / 2.0

# Station range padded by HALF_GAP_FT each side, leaving an empty range empty.
_BUFFERED_STATION = (
    "(CASE WHEN isempty(station_range) THEN station_range "
    f"ELSE numrange(lower(station_range) - {HALF_GAP_FT}, "
    f"upper(station_range) + {HALF_GAP_FT}) END)"
)


def upgrade() -> None:
    # Recreate the exclusion constraint with the buffered station range so the
    # && test fires when two confirmed hulls come within GAP_FT of each other.
    op.execute("ALTER TABLE reservation DROP CONSTRAINT no_wharf_overlap")
    op.execute(
        f"""
        ALTER TABLE reservation
        ADD CONSTRAINT no_wharf_overlap
        EXCLUDE USING gist (time_range WITH &&, {_BUFFERED_STATION} WITH &&)
        WHERE (status = 'confirmed')
        """
    )


def downgrade() -> None:
    # Restore the plain (strict-overlap) station range test.
    op.execute("ALTER TABLE reservation DROP CONSTRAINT no_wharf_overlap")
    op.execute(
        """
        ALTER TABLE reservation
        ADD CONSTRAINT no_wharf_overlap
        EXCLUDE USING gist (time_range WITH &&, station_range WITH &&)
        WHERE (status = 'confirmed')
        """
    )
