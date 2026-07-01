"""Manual berth-request intake — the phone/email/operator channel.

Create / edit / delete a manual berth request (lands raw in ``intake_event`` and
projects a ``requested`` reservation; see ``app/intake/manual.py``), plus the
raw intake audit list. Write paths route through ``common.do_write``.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_session
from app.intake.manual import (
    FEET_PER_M,
    BerthRequestForm,
    delete_manual_request,
    record_manual_request,
    update_manual_request,
)
from app.routers.common import actor, do_write

router = APIRouter()


@router.post("/intake/berth-request", status_code=201)
def create_berth_request(
    form: BerthRequestForm,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    """Manual berth-request entry (phone / email / walk-in) — the sole intake
    channel. Lands the raw request in ``intake_event`` and creates a
    ``status='requested'`` reservation (berth left unassigned). Idempotent on an
    identical re-submission. 422 if the IMO already belongs to a different ship
    (two ships can't share an IMO); 409 on a confirmed time x station overlap."""
    def _audit(result: dict) -> dict | None:
        # Don't log a deduped or content-empty no-op — only an actual new landing.
        if result.get("duplicate") or result.get("skipped"):
            return None
        return dict(
            actor=actor(request), action="create", entity="intake_event",
            entity_id=result.get("intake_event_id"),
            detail={
                "reservation_id": result.get("reservation_id"),
                "vessel_id": result.get("vessel_id"),
                "source": form.source,
            },
        )

    return do_write(session, lambda: record_manual_request(session, form), audit=_audit)


@router.patch("/intake/berth-requests/{intake_id}")
def edit_berth_request(
    intake_id: int,
    form: BerthRequestForm,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    """Correct a manual berth request **in place**: overwrites the raw
    ``intake_event`` payload and re-projects its ``requested`` reservation. Only
    editable-channel rows (phone/email/operator/ai) are editable — the
    online-form CSV export is left immutable. 404 if the event is missing or was
    already deleted, 422 if it isn't editable, 409 if the edit collides
    (duplicate content, or a confirmed time x station overlap)."""
    def _audit(result: dict) -> dict:
        return dict(
            actor=actor(request), action="edit", entity="intake_event",
            entity_id=intake_id,
            detail={
                "reservation_id": result.get("reservation_id"),
                "vessel_id": result.get("vessel_id"),
                # The edit overwrites raw in place; keep the pre-edit payload.
                "prior_raw": result.get("prior_raw"),
            },
        )

    return do_write(
        session, lambda: update_manual_request(session, intake_id, form), audit=_audit
    )


@router.delete("/intake/berth-requests/{intake_id}", status_code=204)
def remove_berth_request(
    intake_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> Response:
    """Withdraw a manual berth request: soft-deletes the raw ``intake_event`` row
    (kept for audit) and drops the ``requested`` reservation it projected. Only
    editable-channel rows (phone/email/operator/ai) can be deleted — the
    online-form CSV export is immutable. 404 if the event is missing or already
    deleted, 422 if it isn't an editable-channel row."""
    def _audit(result: dict) -> dict:
        return dict(
            actor=actor(request), action="delete", entity="intake_event",
            entity_id=intake_id,
            detail={
                "reservation_id": result.get("reservation_id"),
                "prior_raw": result.get("prior_raw"),
            },
        )

    do_write(
        session, lambda: delete_manual_request(session, intake_id), audit=_audit
    )
    return Response(status_code=204)


@router.get("/intake/berth-requests")
def list_berth_requests(
    limit: int = 100,
    include_deleted: bool = False,
    session: Session = Depends(get_session),
) -> list[dict]:
    """Raw inbound berth requests exactly as received — the ``intake_event``
    audit trail, newest first. Each row is one submission (online form, phone,
    email, operator, or AI-parsed entry) with its verbatim payload in ``raw`` and
    a link to the ``requested`` reservation it produced, if any.

    Soft-deleted (withdrawn) rows are hidden by default — the live request panel
    shows only active requests. Pass ``include_deleted=true`` to see the full
    trail (each carries ``deleted_at``); this is the request audit/reconciliation
    view, distinct from ``/reservations`` (the scheduling rectangles)."""
    rows = session.execute(
        text(
            """
            SELECT e.id, e.source, e.received_at, e.processed,
                   e.reservation_id, e.raw, e.deleted_at,
                   r.status AS reservation_status,
                   v.id AS vessel_id, v.mmsi AS vessel_mmsi,
                   v.loa AS vessel_loa, v.beam AS vessel_beam, v.draft AS vessel_draft
            FROM intake_event e
            LEFT JOIN reservation r ON r.id = e.reservation_id
            LEFT JOIN vessel v ON v.id = r.vessel_id
            WHERE (:include_deleted OR e.deleted_at IS NULL)
            ORDER BY e.received_at DESC
            LIMIT :limit
            """
        ),
        {"limit": limit, "include_deleted": include_deleted},
    ).all()
    return [
        {
            "id": r.id,
            "source": r.source,
            "received_at": r.received_at.isoformat() if r.received_at else None,
            "processed": r.processed,
            "reservation_id": r.reservation_id,
            "reservation_status": r.reservation_status,
            "raw": r.raw,
            "deleted_at": r.deleted_at.isoformat() if r.deleted_at else None,
            # The linked vessel's *effective* (stored) dimensions, in feet. For an
            # AIS-tracked ship (has MMSI) these are the AIS-authoritative values —
            # what actually governs, not the operator's original typed entry that
            # AIS overrode — so the edit form can prefill reality rather than the
            # dropped entry. NULL when the request never projected a vessel.
            "vessel": _vessel_dims(r),
        }
        for r in rows
    ]


def _vessel_dims(r) -> dict | None:
    """The linked vessel's stored dimensions as feet + whether it's AIS-tracked,
    for the berth-request edit prefill. Store is metres; convert to feet here so
    the UI (which works in feet) needs no conversion."""
    if r.vessel_id is None:
        return None

    def to_ft(m) -> float | None:
        return round(float(m) * FEET_PER_M, 1) if m is not None else None

    return {
        "ais_tracked": r.vessel_mmsi is not None,
        "loa_ft": to_ft(r.vessel_loa),
        "beam_ft": to_ft(r.vessel_beam),
        "draft_ft": to_ft(r.vessel_draft),
    }
