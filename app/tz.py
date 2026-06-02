"""Central Time is the canonical wall-clock for the whole system.

The Port of Port Arthur runs on US Central Time, so the data layer is Central
end to end. Operator-entered wall-clock times arrive **without a zone** (HTML
``datetime-local`` / ``date`` inputs send none); we interpret them as Central
here rather than UTC. The DB session also runs at ``America/Chicago`` (set in
``app/db.py`` per connection, and as the database default in migration 0008), so
``timestamptz`` columns render in Central and ``now()`` is Central.

The columns still store absolute instants — Central is just how we read and
write them. This module is the single place the zone is defined; nothing else
should hard-code an offset or a zone name.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

# IANA zone (handles the CST/CDT daylight-saving switch, so it is correct across
# the year — never a fixed -6/-5 offset). The literal is mirrored into the DB
# session timezone (app/db.py) and migration 0008; keep the three in sync.
TZ_NAME = "America/Chicago"
CENTRAL = ZoneInfo(TZ_NAME)


def assume_central(value: dt.datetime | None) -> dt.datetime | None:
    """Stamp a naive datetime as Central Time; pass an already-aware one through
    unchanged (its absolute instant is preserved). ``None`` passes through.

    This is the boundary where zone-less operator input becomes a real instant —
    call it on anything coming off a form before it reaches a ``timestamptz``."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=CENTRAL)
