"""Depth-survey surface — upload a hydrographic ``.XYZ`` and read the profile.

The operator-facing "easy update" path: depths change constantly, so an operator
uploads each new condition survey from the map UI and it becomes the active
controlling-depth picture the draft gate reads. Ingestion (project -> clip -> bin
-> min, all in PostGIS) lives in ``app/depth/ingest.py``; this is the thin HTTP
layer over it.

The upload takes the ``.XYZ`` as the raw request body (text), with survey
metadata in the query string — so it needs no multipart/form dependency. Like
every mutating endpoint it routes through ``do_write`` (422 on a bad/empty
survey) and lands an ``audit_log`` row. Reads are plain.
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.crosswalk import segment_dockno_params
from app.db import get_session
from app.depth.ingest import import_survey
from app.depth.parse import parse_survey_date, parse_xyz
from app.routers.common import actor, do_write

router = APIRouter(prefix="/depth")


@router.post("/surveys", status_code=201)
async def upload_survey(
    request: Request,
    surveyed_at: str | None = Query(None, description="YYYY-MM-DD; else from filename"),
    source_file: str | None = Query(None, description="original filename"),
    datum: str | None = Query(None, description="vertical datum, e.g. MLLW"),
    srid: int = Query(2278, description="planar CRS of the soundings (EPSG)"),
    session: Session = Depends(get_session),
) -> dict:
    """Ingest an uploaded ``.XYZ`` sounding file (raw text body) as a new active
    depth survey. The survey date is taken from ``surveyed_at``, else parsed from
    the filename's leading ``MMDDYYYY``, else today. 422 if no sounding falls in
    the berthing zone (wrong CRS / off the wharf / empty)."""
    body = await request.body()
    t_blob = body.decode("utf-8", errors="ignore")

    if surveyed_at:
        try:
            survey_date = dt.date.fromisoformat(surveyed_at)
        except ValueError as exc:
            raise HTTPException(422, "surveyed_at must be YYYY-MM-DD") from exc
    else:
        survey_date = parse_survey_date(source_file or "") or dt.date.today()

    def _do() -> dict:
        return import_survey(
            session,
            parse_xyz(t_blob.splitlines()),
            source_file=source_file,
            surveyed_at=survey_date,
            datum=datum,
            srid=srid,
        )

    def _audit(result: dict) -> dict:
        return dict(
            actor=actor(request), action="import", entity="depth_survey",
            entity_id=result.get("id"),
            detail={
                "source_file": source_file,
                "surveyed_at": result.get("surveyed_at"),
                "parsed_points": result.get("parsed_points"),
                "segment_count": result.get("segment_count"),
                "min_depth_ft": result.get("min_depth_ft"),
                "station_min": result.get("station_min"),
                "station_max": result.get("station_max"),
            },
        )

    return do_write(session, _do, audit=_audit)


@router.get("/surveys")
def list_surveys(session: Session = Depends(get_session)) -> list[dict]:
    """All depth surveys, newest first. ``active`` ones feed the draft gate."""
    rows = session.execute(
        text(
            """
            SELECT id, surveyed_at, source_file, datum, srid, active,
                   point_count, station_min, station_max, min_depth_ft, bin_ft,
                   created_at
            FROM depth_survey
            ORDER BY surveyed_at DESC, id DESC
            """
        )
    ).all()
    return [
        {
            "id": r.id,
            "surveyed_at": r.surveyed_at.isoformat() if r.surveyed_at else None,
            "source_file": r.source_file,
            "datum": r.datum,
            "srid": r.srid,
            "active": r.active,
            "point_count": r.point_count,
            "station_min": float(r.station_min) if r.station_min is not None else None,
            "station_max": float(r.station_max) if r.station_max is not None else None,
            "min_depth_ft": float(r.min_depth_ft) if r.min_depth_ft is not None else None,
            "bin_ft": float(r.bin_ft) if r.bin_ft is not None else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@router.get("/profile")
def survey_profile(
    survey_id: int | None = Query(None, description="default: latest active survey"),
    session: Session = Depends(get_session),
) -> dict:
    """Per-station controlling-depth profile for one survey (default: the latest
    active one). Each bin carries POPA and Dock No. bounds (Dock No. is what the
    operator reads off the quay) plus the controlling depth. Empty when no survey
    exists."""
    if survey_id is None:
        sid = session.execute(
            text(
                "SELECT id FROM depth_survey WHERE active "
                "ORDER BY surveyed_at DESC, id DESC LIMIT 1"
            )
        ).scalar_one_or_none()
    else:
        sid = session.execute(
            text("SELECT id FROM depth_survey WHERE id = :id"), {"id": survey_id}
        ).scalar_one_or_none()
    if sid is None:
        return {"survey_id": None, "bins": []}

    dockno = segment_dockno_params(session)
    rows = session.execute(
        text(
            """
            SELECT lower(popa_range) AS lo, upper(popa_range) AS hi,
                   controlling_depth_ft AS depth, point_count AS n
            FROM depth_segment
            WHERE survey_id = :sid
            ORDER BY lower(popa_range)
            """
        ),
        {"sid": sid},
    ).all()
    bins = [
        {
            "popa_lo": float(r.lo),
            "popa_hi": float(r.hi),
            "dock_lo": dockno.from_popa(float(r.lo)),
            "dock_hi": dockno.from_popa(float(r.hi)),
            "controlling_depth_ft": float(r.depth),
            "point_count": r.n,
        }
        for r in rows
    ]

    # 2-D cross-section grid (migration 0013): the same soundings binned by
    # station AND perpendicular offset-from-quay (feet), so the map can draw how
    # the bottom shoals out into the channel. Older surveys (pre-0013) have no
    # cells, so this is simply empty and the overlay falls back to flat bands.
    cell_rows = session.execute(
        text(
            """
            SELECT lower(popa_range) AS lo, upper(popa_range) AS hi,
                   lower(offset_range) AS off_lo, upper(offset_range) AS off_hi,
                   controlling_depth_ft AS depth, point_count AS n
            FROM depth_cell
            WHERE survey_id = :sid
            ORDER BY lower(popa_range), lower(offset_range)
            """
        ),
        {"sid": sid},
    ).all()
    cells = [
        {
            "popa_lo": float(r.lo),
            "popa_hi": float(r.hi),
            "off_lo_ft": float(r.off_lo),
            "off_hi_ft": float(r.off_hi),
            "controlling_depth_ft": float(r.depth),
            "point_count": r.n,
        }
        for r in cell_rows
    ]
    return {"survey_id": int(sid), "bins": bins, "cells": cells}


@router.delete("/surveys/{survey_id}", status_code=204)
def delete_survey(
    survey_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> Response:
    """Delete a depth survey (cascades to its segments). 404 if it doesn't
    exist. Use to drop a bad import; ``active=false`` is the non-destructive
    alternative (a future edit endpoint)."""
    def _do() -> bool:
        res = session.execute(
            text("DELETE FROM depth_survey WHERE id = :id"), {"id": survey_id}
        )
        return res.rowcount > 0

    def _audit(deleted: bool) -> dict | None:
        if not deleted:
            return None
        return dict(
            actor=actor(request), action="delete", entity="depth_survey",
            entity_id=survey_id, detail=None,
        )

    deleted = do_write(session, _do, audit=_audit)
    if not deleted:
        raise HTTPException(status_code=404, detail="no such depth survey")
    return Response(status_code=204)
