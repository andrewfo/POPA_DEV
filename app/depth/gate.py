"""The draft-vs-controlling-depth check.

Two pieces, kept here so the confirm path in ``app/edit.py`` stays lean:

* ``depth_shortfall`` — pure comparison (draft + clearance vs controlling
  depth), unit-tested without a DB.
* ``controlling_depth_over`` — the DB lookup: the shallowest controlling depth
  over a POPA station range, per the *latest active* depth survey covering it.

Per the core model, AIS/observed data and the depth gate stay separate concerns;
this only reads ``depth_survey`` / ``depth_segment`` (migration 0012). It does
not place or mutate anything.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import text
from sqlalchemy.orm import Session

# Station "M" / depths are feet; vessel draft is stored in metres (AIS canon).
# Same constant as app/edit.FEET_PER_M and app/occupancy/project.FEET_PER_M.
FEET_PER_M = 3.280839895


def depth_shortfall(
    controlling_ft: float | None, draft_m: float | None, clearance_ft: float
) -> float | None:
    """How many feet short the berth is for this draft, or ``None`` if it clears.

    The vessel clears when ``controlling_depth >= draft + clearance`` (all feet;
    draft converted from metres). Returns the positive deficit
    (``required - controlling``) when too deep, else ``None``. ``None`` inputs
    (unknown depth or draft) also return ``None`` — the caller treats "can't
    evaluate" as a warning, not a block.
    """
    if controlling_ft is None or draft_m is None:
        return None
    required = draft_m * FEET_PER_M + clearance_ft
    deficit = required - controlling_ft
    return deficit if deficit > 0 else None


def controlling_depth_over(
    session: Session, lo: float, hi: float
) -> tuple[float | None, dt.date | None]:
    """Shallowest controlling depth (feet) over POPA range ``[lo, hi]``, taken
    from the latest active depth survey that covers any of the range.

    Returns ``(controlling_ft, surveyed_at)``, or ``(None, None)`` when no active
    survey covers the range (the gate then warns instead of blocking). The survey
    is chosen first (latest ``surveyed_at`` that overlaps), then its shallowest
    bin over the range — so a newer survey supersedes an older one wherever it
    reaches.
    """
    row = session.execute(
        text(
            """
            SELECT s.surveyed_at AS surveyed_at,
                   (SELECT min(ds.controlling_depth_ft)
                      FROM depth_segment ds
                     WHERE ds.survey_id = s.id
                       AND ds.popa_range
                           && numrange(CAST(:lo AS numeric), CAST(:hi AS numeric), '[]')
                   ) AS ctrl
              FROM depth_survey s
             WHERE s.active
               AND numrange(s.station_min, s.station_max, '[]')
                   && numrange(CAST(:lo AS numeric), CAST(:hi AS numeric), '[]')
             ORDER BY s.surveyed_at DESC, s.id DESC
             LIMIT 1
            """
        ),
        {"lo": lo, "hi": hi},
    ).first()
    if row is None or row.ctrl is None:
        return (None, None)
    return (float(row.ctrl), row.surveyed_at)
