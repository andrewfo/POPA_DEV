"""Read-only analysis surfaces: conflicts + AIS verification.

``GET /conflicts`` (``app/conflicts.py``) and ``GET /verification``
(``app/verification.py``) surface findings without mutating rows.
``POST /verification/sweep`` is the explicit write companion that auto-archives
stale planned rows before returning the verification payload.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.config import get_settings
from app.conflicts import find_conflicts
from app.db import get_session
from app.routers.common import actor
from app.verification import expire_stale, verify

router = APIRouter()


@router.get("/conflicts")
def list_conflicts(
    status: str | None = None,
    limit: int = 200,
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
    current: bool = True,
    service_craft: bool = False,
    session: Session = Depends(get_session),
) -> list[dict]:
    """Conflict pairs — reservations that overlap in BOTH time and station — each
    with the overlapping sub-rectangle (POPA + Dock No.) so the UI can highlight
    the exact collision. This *surfaces* overlaps (the headline being an
    AIS-``observed`` vessel sitting where something is planned); it does not block
    them — that is the ``confirmed``-only DB exclusion constraint's job.

    ``status`` keeps only pairs where at least one side has that status (e.g.
    ``observed`` -> observed-vs-planned). ``from``/``to`` (ISO datetimes, FastAPI
    rejects malformed with 422) narrow to pairs whose windows both overlap that
    span. Cancelled/completed rows never appear; an unassigned ``requested`` row
    (empty station range) never conflicts.

    This is a *live alert* feed by default: ``current=true`` keeps only pairs
    whose overlap reaches the present/future (asking for an explicit ``from``/
    ``to`` window turns that off — a historical query means the past on purpose),
    and ``service_craft=false`` hides pairs where an observed side is a harbor
    tug/towboat/pilot boat (``app/shiptypes.py``)."""
    # Inverted bounds are a no-op window, not a 500 (mirrors /reservations).
    if from_ is not None and to is not None and from_ > to:
        from_, to = to, from_
    return find_conflicts(
        session, t_from=from_, t_to=to, status=status, limit=limit,
        current_only=current and from_ is None and to is None,
        include_service_craft=service_craft,
    )


@router.get("/verification")
def get_verification(
    limit: int = 200,
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
    current: bool = True,
    service_craft: bool = False,
    session: Session = Depends(get_session),
) -> dict:
    """AIS *verification* of operator placements (NOT placement — AIS can't position
    a not-yet-arrived ship, and its ranges are approximate). For each planned
    reservation (``requested``/``tentative``/``confirmed`` with a vessel + window),
    report whether an ``observed`` AIS berthing for that vessel overlaps its window:
    ``arrived`` / ``no_show`` / ``awaiting``, plus a ``where_planned`` flag (the
    inline form of step 6's observed-vs-planned signal). Also lists ``unplanned``
    observed berthings — a vessel alongside that no plan covers. Matches on vessel
    identity + TIME overlap (an empty ``requested`` station range can't match the
    conflict join). Read-only: surfaces findings, never mutates status.

    ``from``/``to`` (ISO datetimes; FastAPI 422s on malformed) narrow to rows whose
    window overlaps that span.

    The **unplanned** list is live-scoped by default: ``current=true`` keeps only
    ongoing berthings (a closed visit means the vessel left — that's History; an
    explicit ``from``/``to`` window turns this off), and ``service_craft=false``
    hides harbor tugs/towboats/pilot boats (``app/shiptypes.py``) — nobody files
    a berth request for a tug working a ship move. The planned list is operator
    rows and is never filtered this way."""
    # Inverted bounds are a no-op window, not a 500 (mirrors /reservations).
    if from_ is not None and to is not None and from_ > to:
        from_, to = to, from_
    return verify(
        session, t_from=from_, t_to=to, limit=limit,
        current_only=current and from_ is None and to is None,
        include_service_craft=service_craft,
    )


@router.post("/verification/sweep")
def sweep_verification(
    request: Request,
    limit: int = 200,
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
    current: bool = True,
    service_craft: bool = False,
    session: Session = Depends(get_session),
) -> dict:
    """Auto-archive stale planned rows, then return the fresh verification payload.

    This is the *write* companion to ``GET /verification``: a planned reservation
    whose window has been fully past for longer than the grace period
    (``config.verification_grace_minutes``) is swept to a terminal status —
    ``completed`` if AIS observed the vessel berth, else ``cancelled`` (no-show) —
    so it stops lingering in the live panel and shows in History with that status.
    The UI calls this instead of the GET so the panel self-heals on each refresh;
    the prod occupancy worker runs the same sweep on its periodic batch.

    Returns the ``GET /verification`` shape plus an ``expired`` list naming what was
    just archived. Read clients that must not mutate keep using the GET."""
    if from_ is not None and to is not None and from_ > to:
        from_, to = to, from_
    grace = get_settings().verification_grace_minutes
    expired = expire_stale(session, grace_minutes=grace)
    # Auto-archiving is a status mutation; record it in the trail when it actually
    # moved rows (the UI/worker calls this every refresh, so most sweeps are no-ops
    # — don't log those). The terminal status per row is in the expired payload.
    if expired:
        record_audit(
            session, actor=actor(request), action="sweep", entity="reservation",
            entity_id=None, detail={"expired": expired},
        )
    session.commit()
    payload = verify(
        session, t_from=from_, t_to=to, limit=limit,
        current_only=current and from_ is None and to is None,
        include_service_craft=service_craft,
    )
    payload["expired"] = expired
    return payload
