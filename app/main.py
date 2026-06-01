"""FastAPI app. Phase-1 scope: health + a couple of read-only endpoints to
confirm the data layer is alive. No conflict service, intake, or UI yet.
"""
from __future__ import annotations

from fastapi import Depends, FastAPI
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app import __version__
from app.crosswalk import geo_to_station
from app.db import get_session
from app.models import PositionReport, Vessel, WharfSegment

app = FastAPI(title="POPA Wharf Data Layer", version=__version__)


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
