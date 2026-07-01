"""repair AIS-tracked vessels whose dimensions were corrupted by a manual edit

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-01

For an AIS-tracked vessel (``mmsi`` present), the dimensions are AIS's to own:
``loa = dim_a + dim_b`` (bow + stern offsets) and ``draft`` come straight from
``ShipStaticData``. Before this release the manual **vessel Edit** surface
applied a dimension edit even for such a ship (it only *warned* the feed would
revert it), and the revert lands only on the next ShipStaticData — so a berthed /
gone-quiet vessel kept the wrong manual value indefinitely, and its stored ``loa``
drifted away from ``dim_a + dim_b``. The reported symptom was ACER ARROW showing
LOA 500 ft (152.40 m) with dim_a+dim_b = 200 m, and a 20 ft (6.10 m) manual draft
over its real AIS draft.

The code fix (``app/edit._strip_ais_dims``) stops the corruption going forward.
This migration heals rows already corrupted, detected by the tell-tale signature
``loa <> dim_a + dim_b`` on an MMSI-keyed vessel:

- **loa** is recomputed from ``dim_a + dim_b`` — fully recoverable from AIS data
  still on the row.
- **draft** on those same rows is set to NULL (unknown): the AIS draft was
  overwritten and can't be recomputed, and a *wrong* draft is worse than an
  unknown one (a too-shallow manual draft could let a too-deep ship confirm past
  the depth gate; NULL just warns "not validated"). The live feed refills it from
  the next ShipStaticData.

Idempotent and safe on a clean DB: with no drifted rows the UPDATE matches
nothing. Purely a data repair — no schema change. Not reversible (the corrupted
values are exactly what we're discarding), so downgrade is a no-op.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE vessel
           SET loa = dim_a + dim_b,
               draft = NULL,
               updated_at = now()
         WHERE mmsi IS NOT NULL
           AND dim_a IS NOT NULL
           AND dim_b IS NOT NULL
           AND (loa IS NULL OR abs(loa - (dim_a + dim_b)) > 0.5)
        """
    )


def downgrade() -> None:
    # The corrupted values we discarded are not recoverable; nothing to restore.
    pass
