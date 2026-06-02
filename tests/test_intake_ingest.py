"""DB-marked tests for intake landing + idempotency.

Auto-skips (via the ``db_session`` fixture) when no migrated PostGIS is around.
"""
from sqlalchemy import func, select

from app.intake.ingest import IntakeIngestor, dedupe_key
from app.intake.records import parse_row
from app.models import IntakeEvent


def _count(session) -> int:
    return session.execute(select(func.count()).select_from(IntakeEvent)).scalar_one()


def test_dedupe_key_is_order_independent():
    assert dedupe_key({"a": 1, "b": 2}) == dedupe_key({"b": 2, "a": 1})
    assert dedupe_key({"a": 1}) != dedupe_key({"a": 2})


def test_lands_raw_and_is_idempotent(db_session):
    rows = [
        {"Vessel": "SAGA ADVENTURE", "IMO Number": "9317406", "Assigned Berth": "Berth 2"},
        {"Vessel": "PRIORITY", "IMO Number": "9282558", "Assigned Berth": "Berth 1"},
    ]
    before = _count(db_session)

    ing = IntakeIngestor(db_session)
    for r in rows:
        ing.handle(parse_row(r))
    ing.flush()
    assert ing.inserted == 2 and ing.skipped == 0
    assert _count(db_session) == before + 2

    # Re-ingesting the same export lands nothing new.
    ing2 = IntakeIngestor(db_session)
    for r in rows:
        ing2.handle(parse_row(r))
    ing2.flush()
    assert ing2.inserted == 0 and ing2.skipped == 2
    assert _count(db_session) == before + 2

    # Raw row is stored verbatim, source is 'form', not yet processed.
    ev = db_session.execute(
        select(IntakeEvent).where(IntakeEvent.raw["Vessel"].astext == "SAGA ADVENTURE")
    ).scalars().first()
    assert ev is not None
    assert ev.source == "form"
    assert ev.processed is False
    assert ev.raw["IMO Number"] == "9317406"
