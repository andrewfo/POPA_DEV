"""FastAPI app. Read-only endpoints over the data layer + a thin Leaflet map,
plus one write path: manual berth-request entry (the phone/email channel — see
``app/intake/manual.py``). No conflict service yet.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app import __version__
from app.config import get_settings
from app.crosswalk import geo_to_station
from app.db import get_session
from app.intake.manual import BerthRequestForm, record_manual_request
from app.models import PositionReport, Vessel, WharfSegment

app = FastAPI(title="POPA Wharf Data Layer", version=__version__)

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
    status: str | None = None, limit: int = 100,
    session: Session = Depends(get_session),
) -> list[dict]:
    """Reservations, newest first, optionally filtered by status. Station/time
    ranges are returned as bounds; an empty station range (unassigned berth)
    reports ``station_unassigned: true``."""
    rows = session.execute(
        text(
            """
            SELECT r.id, r.type, r.status, r.source, r.direction, r.cargo, r.notes,
                   lower(r.time_range)    AS t_start,
                   upper(r.time_range)    AS t_end,
                   isempty(r.station_range) AS sta_unassigned,
                   lower(r.station_range) AS sta_lo,
                   upper(r.station_range) AS sta_hi,
                   r.created_at,
                   v.name AS vessel_name, v.imo AS vessel_imo
            FROM reservation r
            LEFT JOIN vessel v ON v.id = r.vessel_id
            WHERE (:status IS NULL OR r.status::text = :status)
            ORDER BY r.created_at DESC
            LIMIT :limit
            """
        ),
        {"status": status, "limit": limit},
    ).all()
    return [
        {
            "id": r.id,
            "type": r.type,
            "status": r.status,
            "source": r.source,
            "direction": r.direction,
            "cargo": r.cargo,
            "notes": r.notes,
            "t_start": r.t_start.isoformat() if r.t_start else None,
            "t_end": r.t_end.isoformat() if r.t_end else None,
            "station_unassigned": r.sta_unassigned,
            "station_lo": float(r.sta_lo) if r.sta_lo is not None else None,
            "station_hi": float(r.sta_hi) if r.sta_hi is not None else None,
            "vessel_name": r.vessel_name,
            "vessel_imo": r.vessel_imo,
        }
        for r in rows
    ]
