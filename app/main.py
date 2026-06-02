"""FastAPI app. Read-only endpoints over the data layer + a thin Leaflet map,
plus write paths: manual berth-request entry (the phone/email channel — see
``app/intake/manual.py``) and the manual edit surface for ship data and
scheduling (create/edit/cancel/delete vessels & reservations — see
``app/edit.py``). No automatic conflict service yet; confirmed-vs-confirmed
overlaps are caught by the DB exclusion constraint and surfaced as a 409.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app import __version__
from app.config import get_settings
from app.crosswalk import geo_to_station, segment_dockno_params
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
from app.intake.manual import BerthRequestForm, record_manual_request
from app.models import PositionReport, Vessel, WharfSegment

app = FastAPI(title="POPA Wharf Data Layer", version=__version__)


@app.exception_handler(OperationalError)
def _db_unavailable(request: Request, exc: OperationalError) -> JSONResponse:
    """The database is down/unreachable (e.g. Postgres not started). Return a
    clean 503 the frontend can show as a banner, instead of dumping a multi-page
    connection-timeout traceback on every poll of the read-only endpoints."""
    return JSONResponse(
        status_code=503,
        content={
            "detail": "database unavailable",
            "hint": "start Postgres/PostGIS (see docker-compose.yml) and retry",
        },
    )


def _do_write(session: Session, fn):
    """Run a write function and commit, translating DB-layer failures into clean
    HTTP errors instead of 500s.

    A bad enum / range raises ``ValueError`` -> 422. The exclusion constraint
    ``no_wharf_overlap`` (confirmed-only, time x station) and the unique-MMSI /
    "mmsi or imo required" checks raise ``IntegrityError`` -> 409. The raw
    INSERT/UPDATE executes inside ``fn`` (before commit), so both the call and
    the commit are wrapped, and the poisoned transaction is rolled back."""
    try:
        result = fn()
        session.commit()
        return result
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        session.rollback()
        text_ = str(getattr(exc, "orig", exc))
        if "no_wharf_overlap" in text_:
            detail = (
                "confirmed reservation overlaps another confirmed booking "
                "(time x station). Adjust the window, the berth, or keep it "
                "tentative."
            )
        elif "mmsi" in text_ and "key" in text_.lower():
            detail = "another vessel already uses that MMSI"
        elif "vessel_requires_mmsi_or_imo" in text_:
            detail = "a vessel needs at least one of MMSI or IMO"
        else:
            detail = "write violates a database constraint"
        raise HTTPException(status_code=409, detail=detail) from exc


_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """Serve the read-only Leaflet map over the data layer."""
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/config/bbox")
def config_bbox() -> dict:
    """The AIS bounding box, so the map can frame the wharf without hard-coding it."""
    s = get_settings()
    return {
        "sw": {"lat": s.ais_bbox_sw_lat, "lon": s.ais_bbox_sw_lon},
        "ne": {"lat": s.ais_bbox_ne_lat, "lon": s.ais_bbox_ne_lon},
    }


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}


@app.get("/health/db")
def health_db(session: Session = Depends(get_session)) -> dict:
    session.execute(text("SELECT 1"))
    postgis = session.execute(text("SELECT PostGIS_Lib_Version()")).scalar_one()
    return {"status": "ok", "postgis": postgis}


@app.get("/wharf-segments")
def list_segments(session: Session = Depends(get_session)) -> list[dict]:
    rows = session.execute(select(WharfSegment)).scalars().all()
    return [
        {
            "id": s.id,
            "name": s.name,
            "popa_sta_start": float(s.popa_sta_start),
            "popa_sta_end": float(s.popa_sta_end),
            "corps": {"scale": float(s.corps_scale), "offset": float(s.corps_offset)},
            "dockno": {"scale": float(s.dockno_scale), "offset": float(s.dockno_offset)},
        }
        for s in rows
    ]


@app.get("/wharf-segments/geojson")
def segments_geojson(session: Session = Depends(get_session)) -> dict:
    """Wharf centerlines as a GeoJSON FeatureCollection (M dropped) for the map."""
    rows = session.execute(
        select(
            WharfSegment.id,
            WharfSegment.name,
            WharfSegment.popa_sta_start,
            WharfSegment.popa_sta_end,
            func.ST_AsGeoJSON(func.ST_Force2D(WharfSegment.geom)).label("gj"),
        )
    ).all()
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": json.loads(r.gj),
                "properties": {
                    "id": r.id,
                    "name": r.name,
                    "popa_sta_start": float(r.popa_sta_start),
                    "popa_sta_end": float(r.popa_sta_end),
                },
            }
            for r in rows
        ],
    }


@app.get("/positions/recent")
def positions_recent(
    limit: int = 500, session: Session = Depends(get_session)
) -> list[dict]:
    """Most recent landed AIS positions, joined to vessel name when known."""
    rows = session.execute(
        select(
            PositionReport.id,
            PositionReport.mmsi,
            PositionReport.lat,
            PositionReport.lon,
            PositionReport.sog,
            PositionReport.cog,
            PositionReport.heading,
            PositionReport.msg_ts,
            Vessel.name.label("vessel_name"),
        )
        .join(Vessel, Vessel.id == PositionReport.vessel_id, isouter=True)
        .order_by(PositionReport.msg_ts.desc().nullslast(), PositionReport.id.desc())
        .limit(limit)
    ).all()
    return [
        {
            "id": r.id,
            "mmsi": r.mmsi,
            "lat": r.lat,
            "lon": r.lon,
            "sog": r.sog,
            "cog": r.cog,
            "heading": r.heading,
            "msg_ts": r.msg_ts.isoformat() if r.msg_ts else None,
            "vessel_name": r.vessel_name,
        }
        for r in rows
    ]


@app.get("/vessels")
def list_vessels(
    limit: int = 100, session: Session = Depends(get_session)
) -> list[dict]:
    rows = (
        session.execute(select(Vessel).order_by(Vessel.updated_at.desc()).limit(limit))
        .scalars()
        .all()
    )
    return [
        {
            "id": v.id,
            "mmsi": v.mmsi,
            "imo": v.imo,
            "name": v.name,
            "callsign": v.callsign,
            "ship_type": v.ship_type,
            "destination": v.destination,
            "loa": float(v.loa) if v.loa is not None else None,
            "beam": float(v.beam) if v.beam is not None else None,
            "draft": float(v.draft) if v.draft is not None else None,
        }
        for v in rows
    ]


@app.get("/stats")
def stats(session: Session = Depends(get_session)) -> dict:
    return {
        "vessels": session.execute(select(func.count(Vessel.id))).scalar_one(),
        "position_reports": session.execute(
            select(func.count(PositionReport.id))
        ).scalar_one(),
    }


@app.get("/geo-to-station")
def geo_to_station_endpoint(
    lat: float, lon: float, segment_id: int | None = None,
    session: Session = Depends(get_session),
) -> dict:
    """Project a lat/lon onto the wharf centerline -> canonical POPA station."""
    station = geo_to_station(session, lat, lon, segment_id)
    return {"lat": lat, "lon": lon, "popa_station": station}


@app.post("/intake/berth-request", status_code=201)
def create_berth_request(
    form: BerthRequestForm, session: Session = Depends(get_session)
) -> dict:
    """Manual berth-request entry for vessels that call/email instead of using
    the online form. Lands the raw request in ``intake_event`` and creates a
    ``status='requested'`` reservation (berth left unassigned). Idempotent on an
    identical re-submission."""
    result = record_manual_request(session, form)
    session.commit()
    return result


@app.get("/reservations")
def list_reservations(
    status: str | None = None,
    limit: int = 100,
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
    session: Session = Depends(get_session),
) -> list[dict]:
    """Reservations, newest first, optionally filtered by status and by a time
    window (``from``/``to``, ISO datetimes — FastAPI rejects malformed values
    with a 422). The window is an overlap test, so a reservation counts if any
    part of its ``time_range`` falls inside it. Omitting both ``from`` and ``to``
    returns everything, *including* rows with an empty ``time_range`` (e.g. a
    same-day request where ETB == ETD), which an overlap test alone would drop.
    Station/time ranges are returned as bounds; an empty station range
    (unassigned berth) reports ``station_unassigned: true``."""
    # Inverted bounds are a no-op window, not a 500: normalize so tstzrange()
    # never sees lower > upper.
    if from_ is not None and to is not None and from_ > to:
        from_, to = to, from_
    rows = session.execute(
        text(
            """
            SELECT r.id, r.type, r.status, r.source, r.direction, r.cargo, r.notes,
                   r.priority,
                   lower(r.time_range)    AS t_start,
                   upper(r.time_range)    AS t_end,
                   isempty(r.station_range) AS sta_unassigned,
                   lower(r.station_range) AS sta_lo,
                   upper(r.station_range) AS sta_hi,
                   r.created_at,
                   v.name AS vessel_name, v.imo AS vessel_imo,
                   r.berth_id, b.name AS berth_name
            FROM reservation r
            LEFT JOIN vessel v ON v.id = r.vessel_id
            LEFT JOIN berth b ON b.id = r.berth_id
            WHERE (CAST(:status AS text) IS NULL OR r.status::text = CAST(:status AS text))
              AND ((CAST(:t_from AS timestamptz) IS NULL AND CAST(:t_to AS timestamptz) IS NULL)
                   OR r.time_range && tstzrange(:t_from, :t_to, '[]'))
            ORDER BY r.created_at DESC
            LIMIT :limit
            """
        ),
        {"status": status, "limit": limit, "t_from": from_, "t_to": to},
    ).all()
    # Dock No. bounds alongside POPA: the map/timeline render from POPA, but the
    # edit forms read/write Dock No. (the painted stationing). Convert through the
    # wharf segment's affine params so the UI never does stationing math itself.
    dock = segment_dockno_params(session)
    return [
        {
            "id": r.id,
            "type": r.type,
            "status": r.status,
            "source": r.source,
            "direction": r.direction,
            "priority": r.priority,
            "cargo": r.cargo,
            "notes": r.notes,
            "t_start": r.t_start.isoformat() if r.t_start else None,
            "t_end": r.t_end.isoformat() if r.t_end else None,
            "station_unassigned": r.sta_unassigned,
            "station_lo": float(r.sta_lo) if r.sta_lo is not None else None,
            "station_hi": float(r.sta_hi) if r.sta_hi is not None else None,
            # Dock No. equivalents (stern = larger Dock No. = the POPA lower bound).
            "station_lo_dock": float(dock.from_popa(float(r.sta_lo))) if r.sta_lo is not None else None,
            "station_hi_dock": float(dock.from_popa(float(r.sta_hi))) if r.sta_hi is not None else None,
            "vessel_name": r.vessel_name,
            "vessel_imo": r.vessel_imo,
            "berth_id": r.berth_id,
            "berth_name": r.berth_name,
        }
        for r in rows
    ]


@app.get("/berths")
def list_berths(session: Session = Depends(get_session)) -> list[dict]:
    """The named berth catalog — each a canonical POPA station range. An operator
    assigns one to a ``requested`` reservation (``PATCH /reservations/{id}`` with
    ``berth_id``), which fills the reservation's ``station_range`` from the berth.
    Ordered by station so the list reads SW -> NE along the wharf."""
    rows = session.execute(
        text(
            """
            SELECT id, name, popa_sta_start, popa_sta_end
            FROM berth ORDER BY popa_sta_start
            """
        )
    ).all()
    return [
        {
            "id": r.id,
            "name": r.name,
            "station_lo": float(r.popa_sta_start),
            "station_hi": float(r.popa_sta_end),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Manual edit surface — ship data + scheduling (see app/edit.py)
# ---------------------------------------------------------------------------
@app.patch("/vessels/{vessel_id}")
def edit_vessel(
    vessel_id: int, upd: VesselUpdate, session: Session = Depends(get_session)
) -> dict:
    """Correct a vessel record. Only the fields present in the body overwrite
    (a manual edit is authoritative — unlike intake, which only fills NULLs).
    Dimensions are metres. 404 if the vessel does not exist."""
    result = _do_write(session, lambda: update_vessel(session, vessel_id, upd))
    if result is None:
        raise HTTPException(status_code=404, detail="no such vessel")
    return result


@app.post("/reservations", status_code=201)
def post_reservation(
    req: ReservationCreate, session: Session = Depends(get_session)
) -> dict:
    """Create a reservation (vessel / dredge / layberth). Station bounds are
    canonical POPA feet (omit for an unassigned berth). Promoting to
    ``confirmed`` engages the no-overlap exclusion constraint (-> 409 on
    collision)."""
    return _do_write(session, lambda: create_reservation(session, req))


@app.patch("/reservations/{res_id}")
def edit_reservation(
    res_id: int, upd: ReservationUpdate, session: Session = Depends(get_session)
) -> dict:
    """Edit a reservation: time window, berth (station range), status (incl.
    ``confirmed``), type, direction, priority, cargo, notes. Cancelling is a
    ``status='cancelled'`` edit. 404 if it does not exist; 409 if a confirmed
    edit overlaps another confirmed booking."""
    result = _do_write(session, lambda: update_reservation(session, res_id, upd))
    if result is None:
        raise HTTPException(status_code=404, detail="no such reservation")
    return result


@app.delete("/reservations/{res_id}", status_code=204)
def remove_reservation(
    res_id: int, session: Session = Depends(get_session)
) -> Response:
    """Hard-delete a reservation. 404 if it does not exist."""
    deleted = _do_write(session, lambda: delete_reservation(session, res_id))
    if not deleted:
        raise HTTPException(status_code=404, detail="no such reservation")
    return Response(status_code=204)
