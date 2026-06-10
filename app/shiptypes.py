"""AIS ship-type classification — who is *traffic* vs who is *infrastructure*.

The occupancy detector is deliberately type-blind: a tug holding station at the
quay IS alongside, and that ground truth lands as an ``observed`` reservation
like any other (History keeps it, the conflict primitive can see it). But the
*live scheduling surfaces* — the verification panel's unplanned list and the
conflicts feed — answer "what does the berth plan need to react to?", and a
harbor tug working a ship move is not an arrival anyone files a berth request
for. Surfacing each pause as UNPLANNED (or as an observed-vs-planned conflict
against the very ship it is assisting) buries the real signal.

So service craft are filtered at the *query* layer (``app/verification.py``,
``app/conflicts.py``), never at ingest/derivation — the rows still exist, and
both endpoints take an ``include_service_craft`` escape hatch.

The set mirrors the UI's ``shipTypeCategory`` (``app/static/js/api.js``): the
tug/towing bucket (31/32 towing, 52 tug, plus 56/57 — the ITU-R M.1371 "spare —
local vessel" slots the Sabine-Neches towboat fleet broadcasts en masse,
verified against the live vessel list) and 50 (pilot). Keep the two definitions
in sync. Dredgers (33) are deliberately NOT here — dredging occupancy is this
system's other half. An unknown/NULL type is NOT a service craft: when in
doubt, show the vessel.
"""
from __future__ import annotations

SERVICE_CRAFT_TYPES = frozenset({31, 32, 50, 52, 56, 57})

# Inlined into raw SQL by verification/conflicts (a static module constant, not
# user input). One rendering so the two queries can't drift.
SERVICE_CRAFT_SQL = ", ".join(str(t) for t in sorted(SERVICE_CRAFT_TYPES))


def is_service_craft(ship_type: int | None) -> bool:
    """Is this AIS ship-type code a harbor service craft (tug/towing/pilot)?

    ``None``/unknown is **not** a service craft — an untyped vessel could be a
    real arrival, so it stays visible on the live panels."""
    return ship_type is not None and ship_type in SERVICE_CRAFT_TYPES
