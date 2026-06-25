"""Manual edit surface — ship data + scheduling (see ``app/edit.py``).

Correct a vessel record and create / edit / cancel / delete reservations
(including assigning a berth and promoting to ``confirmed``). The session
functions in ``app/edit.py`` don't commit; ``common.do_write`` runs them, commits,
and translates DB-layer failures (a confirmed overlap -> 409, a bad range -> 422).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.db import get_session
from app.edit import (
    ReservationCreate,
    ReservationUpdate,
    VesselUpdate,
    create_reservation,
    delete_reservation,
    update_reservation,
    update_vessel,
)
from app.routers.common import actor, do_write

router = APIRouter()


@router.patch("/vessels/{vessel_id}")
def edit_vessel(
    vessel_id: int,
    upd: VesselUpdate,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    """Correct a vessel record. Only the fields present in the body overwrite
    (a manual edit is authoritative — unlike intake, which only fills NULLs).
    Dimensions are metres. 404 if the vessel does not exist."""
    def _audit(result: dict | None) -> dict | None:
        if result is None:  # no such vessel -> 404, nothing changed
            return None
        return dict(
            actor=actor(request), action="edit", entity="vessel",
            entity_id=vessel_id,
            detail={"updated_fields": result.get("updated_fields")},
        )

    result = do_write(
        session, lambda: update_vessel(session, vessel_id, upd), audit=_audit
    )
    if result is None:
        raise HTTPException(status_code=404, detail="no such vessel")
    return result


@router.post("/reservations", status_code=201)
def post_reservation(
    req: ReservationCreate,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    """Create a reservation (vessel / dredge / layberth). Station bounds are
    canonical POPA feet (omit for an unassigned berth). Promoting to
    ``confirmed`` engages the no-overlap exclusion constraint (-> 409 on
    collision)."""
    def _audit(result: dict) -> dict:
        detail = {"type": req.type, "status": req.status, "source": req.source}
        if req.depth_override and result.get("warnings"):
            detail["depth_override"] = result["warnings"]
        return dict(
            actor=actor(request), action="create", entity="reservation",
            entity_id=result.get("id"), detail=detail,
        )

    return do_write(
        session, lambda: create_reservation(session, req), audit=_audit
    )


@router.patch("/reservations/{res_id}")
def edit_reservation(
    res_id: int,
    upd: ReservationUpdate,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    """Edit a reservation: time window, berth (station range), status (incl.
    ``confirmed``), type, direction, priority, cargo, notes. Cancelling is a
    ``status='cancelled'`` edit. 404 if it does not exist; 409 if a confirmed
    edit overlaps another confirmed booking."""
    def _audit(result: dict | None) -> dict | None:
        if result is None:  # no such reservation -> 404
            return None
        detail = {"changed": sorted(upd.model_dump(exclude_unset=True))}
        if upd.depth_override and result.get("warnings"):
            detail["depth_override"] = result["warnings"]
        return dict(
            actor=actor(request), action="edit", entity="reservation",
            entity_id=res_id, detail=detail,
        )

    result = do_write(
        session, lambda: update_reservation(session, res_id, upd), audit=_audit
    )
    if result is None:
        raise HTTPException(status_code=404, detail="no such reservation")
    return result


@router.delete("/reservations/{res_id}", status_code=204)
def remove_reservation(
    res_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> Response:
    """Hard-delete a reservation. 404 if it does not exist."""
    def _audit(deleted: bool) -> dict | None:
        if not deleted:  # nothing existed -> 404
            return None
        return dict(
            actor=actor(request), action="delete", entity="reservation",
            entity_id=res_id, detail=None,
        )

    deleted = do_write(
        session, lambda: delete_reservation(session, res_id), audit=_audit
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="no such reservation")
    return Response(status_code=204)
