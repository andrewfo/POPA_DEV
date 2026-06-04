"""FastAPI app. Read-only endpoints over the data layer + a thin Leaflet map,
plus write paths: manual berth-request entry (the phone/email channel — see
``app/intake/manual.py``) and the manual edit surface for ship data and
scheduling (create/edit/cancel/delete vessels & reservations — see
``app/edit.py``). Read-only analysis surfaces: the conflict service
(``GET /conflicts``, ``app/conflicts.py``) and AIS verification
(``GET /verification``, ``app/verification.py``); both surface findings without
mutating rows. Confirmed-vs-confirmed overlaps are blocked by the DB exclusion
constraint and surfaced as a 409.
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
from app.auth import BasicAuthMiddleware
from app.config import get_settings
from app.conflicts import find_conflicts
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
from app.intake.manual import (
    BerthRequestForm,
    delete_manual_request,
    record_manual_request,
    update_manual_request,
)
from app.models import Vessel, WharfSegment
from app.occupancy.alongside import alongside_sql, nearest_segment_lateral
from app.verification import verify

app = FastAPI(title="POPA Wharf Data Layer", version=__version__)

# Gate the whole app (map + reads + writes) behind HTTP Basic. No-op unless
# OPERATOR_USER/OPERATOR_PASSWORD are set, so dev and tests run open; /health
# stays exempt for container probes. See app/auth.py.
app.add_middleware(BasicAuthMiddleware)


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
    except LookupError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
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
        elif "uq_intake_event_dedupe_key" in text_:
            detail = "those exact details already exist on another berth request"
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
        # The SOG below which a contact reads "stopped". Shared with the client so
        # the map's green/red colouring uses the same threshold the server does
        # for "moored now" — one source of truth, not a hard-coded 0.5 in the JS.
        "moored_sog_kn": s.berth_enter_sog_kn,
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
    """Most recent landed AIS positions, joined to vessel name when known.

    Each fix carries an ``alongside`` flag — whether it sits in the berthing
    zone (the digitized apron polygon, else a centerline buffer; the one
    predicate in ``app/occupancy/alongside.py``). The map uses ``alongside`` plus
    a low SOG to colour a contact moored (green) vs underway (red), so a vessel
    stopped mid-channel reads underway, not moored."""
    point = "ST_SetSRID(ST_MakePoint(pr.lon, pr.lat), 4326)"
    rows = session.execute(
        text(
            f"""
            SELECT pr.id, pr.mmsi, pr.lat, pr.lon, pr.sog, pr.cog, pr.heading,
                   pr.msg_ts, v.name AS vessel_name, v.ship_type,
                   COALESCE({alongside_sql(point)}, false) AS alongside
            FROM position_report pr
            LEFT JOIN vessel v ON v.id = pr.vessel_id
            {nearest_segment_lateral(point)}
            ORDER BY pr.msg_ts DESC NULLS LAST, pr.id DESC
            LIMIT :limit
            """
        ),
        {"limit": limit, "buffer_m": get_settings().berth_buffer_m},
    ).all()
    return [
        {
            "id": r.id,
            "mmsi": r.mmsi,
            "lat": r.lat,
            "lon": r.lon,
            "sog": float(r.sog) if r.sog is not None else None,
            "cog": float(r.cog) if r.cog is not None else None,
            "heading": float(r.heading) if r.heading is not None else None,
            "msg_ts": r.msg_ts.isoformat() if r.msg_ts else None,
            "vessel_name": r.vessel_name,
            # AIS numeric ship type (e.g. 52 = tug, 31/32 = towing); lets the map
            # tint tugs distinctly from cargo/tanker traffic. NULL until the
            # vessel's ShipStaticData lands.
            "ship_type": r.ship_type,
            "alongside": bool(r.alongside),
        }
        for r in rows
    ]


@app.get("/history")
def reservation_history(
    name: str | None = None,
    status: str | None = None,
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
    limit: int = 200,
    session: Session = Depends(get_session),
) -> list[dict]:
    """Reservation history for the History tab — the log of bookings (every
    status, AIS-``observed`` berthings included), newest arrival first.

    This is the same row shape as ``/reservations`` but ordered chronologically
    by arrival (``lower(time_range)``) and with a vessel-``name`` substring
    filter, so an operator can answer "what has this ship / this week done at the
    wharf". The vessel name falls back to the linked ``intake_event`` payload for
    an IMO-less request (a barge/tug with no vessel row), the same way the
    reservations view does, so name search still finds them.

    Filters (all optional, AND-combined): ``name`` (case-insensitive substring),
    ``status`` (exact), and a ``from``/``to`` date window (ISO datetimes — FastAPI
    rejects malformed with 422 — an overlap test on ``time_range``)."""
    # Inverted bounds are a no-op window, not a 500 (mirrors /reservations).
    if from_ is not None and to is not None and from_ > to:
        from_, to = to, from_
    rows = session.execute(
        text(
            """
            WITH res AS (
                SELECT r.id, r.type, r.status, r.source, r.direction, r.cargo,
                       r.notes, r.priority,
                       lower(r.time_range)      AS t_start,
                       upper(r.time_range)      AS t_end,
                       isempty(r.station_range) AS sta_unassigned,
                       lower(r.station_range)   AS sta_lo,
                       upper(r.station_range)   AS sta_hi,
                       r.created_at, r.berth_id, b.name AS berth_name,
                       v.imo AS vessel_imo, v.ship_type,
                       -- Same name fallback as /reservations: an IMO-less request
                       -- has no vessel row, so read the name off its intake_event.
                       COALESCE(v.name, (
                           SELECT COALESCE(e.raw->>'vessel', e.raw->>'Vessel')
                           FROM intake_event e
                           WHERE e.reservation_id = r.id
                           ORDER BY e.id
                           LIMIT 1
                       )) AS vessel_name
                FROM reservation r
                LEFT JOIN vessel v ON v.id = r.vessel_id
                LEFT JOIN berth b ON b.id = r.berth_id
                WHERE (CAST(:status AS text) IS NULL OR r.status::text = CAST(:status AS text))
                  AND ((CAST(:t_from AS timestamptz) IS NULL AND CAST(:t_to AS timestamptz) IS NULL)
                       OR r.time_range && tstzrange(:t_from, :t_to, '[]'))
            )
            SELECT * FROM res
            WHERE (CAST(:name AS text) IS NULL
                   OR vessel_name ILIKE '%' || CAST(:name AS text) || '%')
            ORDER BY t_start DESC NULLS LAST, created_at DESC
            LIMIT :limit
            """
        ),
        {"status": status, "name": name, "t_from": from_, "t_to": to, "limit": limit},
    ).all()
    # Dock No. bounds alongside POPA (same conversion the reservations view uses).
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
            "station_lo_dock": float(dock.from_popa(float(r.sta_lo))) if r.sta_lo is not None else None,
            "station_hi_dock": float(dock.from_popa(float(r.sta_hi))) if r.sta_hi is not None else None,
            "vessel_name": r.vessel_name,
            "vessel_imo": r.vessel_imo,
            "ship_type": r.ship_type,
            "berth_id": r.berth_id,
            "berth_name": r.berth_name,
            "created_at": r.created_at.isoformat() if r.created_at else None,
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
    """Headline counts for the sidebar. Beyond present-vessel/request totals this
    surfaces the request + reservation picture: how many berth requests have
    landed, how many reservations exist by the statuses operators watch, and how
    many vessels are moored *right now* (latest AIS fix alongside the wharf and
    effectively stopped — the same test the map's green dots use). "Vessels" is
    vessels *present* (an AIS fix within the present-window), not every vessel
    row ever ingested — a vessel row is never removed when a ship leaves."""
    # Reservation breakdown by status in one grouped scan (counts every status,
    # so new ones show up without another query).
    res_rows = session.execute(
        text("SELECT status::text AS status, count(*) AS n FROM reservation GROUP BY status")
    ).all()
    by_status = {r.status: r.n for r in res_rows}
    # Moored *now* = contacts whose latest AIS fix is alongside (in the berthing
    # zone) AND effectively stopped. This must agree with the map's green dots, so
    # it reproduces their definition exactly: dedupe per MMSI (not vessel_id, which
    # is NULL until ShipStaticData lands — the map keys on MMSI), pick the latest
    # fix with the same ordering /positions/recent uses (msg_ts DESC NULLS LAST,
    # id DESC), and apply the same alongside predicate + the shared
    # berth_enter_sog_kn threshold (also handed to the client via /config/bbox).
    settings = get_settings()
    point = "ST_SetSRID(ST_MakePoint(l.lon, l.lat), 4326)"
    moored = session.execute(
        text(
            f"""
            WITH latest AS (
                SELECT DISTINCT ON (pr.mmsi)
                       pr.mmsi, pr.lat, pr.lon, pr.sog
                FROM position_report pr
                WHERE pr.mmsi IS NOT NULL
                ORDER BY pr.mmsi, pr.msg_ts DESC NULLS LAST, pr.id DESC
            )
            SELECT count(*)
            FROM latest l
            {nearest_segment_lateral(point)}
            WHERE l.sog IS NOT NULL AND l.sog < :enter_sog
              AND COALESCE({alongside_sql(point)}, false)
            """
        ),
        {
            "enter_sog": settings.berth_enter_sog_kn,
            "buffer_m": settings.berth_buffer_m,
        },
    ).scalar_one()
    # Arrivals in the next 24h = non-cancelled reservations whose ETB (the lower
    # bound of time_range) falls within [now, now+24h]. The forward-looking view
    # an operator preps berths against — counterpart to "Moored now" (right now).
    arrivals_24h = session.execute(
        text(
            """
            SELECT count(*)
            FROM reservation
            WHERE status <> 'cancelled'
              AND lower(time_range) >= now()
              AND lower(time_range) < now() + interval '24 hours'
            """
        )
    ).scalar_one()
    # "Vessels" = vessels *present*, not the all-time total. A vessel row is
    # upserted per MMSI/IMO and never removed when a ship leaves, so counting
    # every row only grows and overstates how many are actually around. Instead
    # count distinct MMSI with a fix inside the present-window (a departed vessel
    # stops broadcasting in the bbox, so its latest fix ages out).
    vessels_present = session.execute(
        text(
            """
            SELECT count(DISTINCT pr.mmsi)
            FROM position_report pr
            WHERE pr.mmsi IS NOT NULL
              AND pr.msg_ts >= now() - (:window_h * interval '1 hour')
            """
        ),
        {"window_h": settings.vessel_present_window_h},
    ).scalar_one()
    return {
        "vessels": vessels_present,
        "arrivals_24h": arrivals_24h,
        "berth_requests": session.execute(
            text("SELECT count(*) FROM intake_event")
        ).scalar_one(),
        # Reservations excluding cancelled — the "live" book of work.
        "reservations": sum(n for s, n in by_status.items() if s != "cancelled"),
        "moored": moored,
        "confirmed": by_status.get("confirmed", 0),
        "requested": by_status.get("requested", 0),
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
    """Manual berth-request entry (phone / email / walk-in) — the sole intake
    channel. Lands the raw request in ``intake_event`` and creates a
    ``status='requested'`` reservation (berth left unassigned). Idempotent on an
    identical re-submission. 422 if the IMO already belongs to a different ship
    (two ships can't share an IMO); 409 on a confirmed time x station overlap."""
    return _do_write(session, lambda: record_manual_request(session, form))


@app.patch("/intake/berth-requests/{intake_id}")
def edit_berth_request(
    intake_id: int, form: BerthRequestForm, session: Session = Depends(get_session)
) -> dict:
    """Correct a manual berth request **in place**: overwrites the raw
    ``intake_event`` payload and re-projects its ``requested`` reservation. Only
    manual-channel rows (phone/email/operator) are editable — the online-form CSV
    export is left immutable. 404 if the event is missing, 422 if it isn't
    editable, 409 if the edit collides (duplicate content, or a confirmed
    time x station overlap)."""
    return _do_write(session, lambda: update_manual_request(session, intake_id, form))


@app.delete("/intake/berth-requests/{intake_id}", status_code=204)
def remove_berth_request(
    intake_id: int, session: Session = Depends(get_session)
) -> Response:
    """Delete a manual berth request and the ``requested`` reservation it
    projected. Only manual-channel rows (phone/email/operator) can be deleted —
    the online-form CSV export is immutable. 404 if the event is missing, 422 if
    it isn't an editable manual-channel row."""
    _do_write(session, lambda: delete_manual_request(session, intake_id))
    return Response(status_code=204)


@app.get("/intake/berth-requests")
def list_berth_requests(
    limit: int = 100,
    session: Session = Depends(get_session),
) -> list[dict]:
    """Raw inbound berth requests exactly as received — the ``intake_event``
    audit trail, newest first. Each row is one submission (online form, phone,
    email, or operator entry) with its verbatim payload in ``raw`` and a link to
    the ``requested`` reservation it produced, if any. This is the request
    audit/reconciliation view, distinct from ``/reservations`` (the scheduling
    rectangles)."""
    rows = session.execute(
        text(
            """
            SELECT e.id, e.source, e.received_at, e.processed,
                   e.reservation_id, e.raw,
                   r.status AS reservation_status
            FROM intake_event e
            LEFT JOIN reservation r ON r.id = e.reservation_id
            ORDER BY e.received_at DESC
            LIMIT :limit
            """
        ),
        {"limit": limit},
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
        }
        for r in rows
    ]


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
                   -- A reservation projected from an IMO-less request (a barge/
                   -- tug the vessel_requires_mmsi_or_imo CHECK won't let us key)
                   -- has no vessel row, so fall back to the name carried in its
                   -- linked intake_event.raw — the same source the berth-requests
                   -- tab shows — rather than rendering "(unnamed)". Manual rows
                   -- use the lowercase 'vessel' key, the online form 'Vessel'.
                   COALESCE(v.name, (
                       SELECT COALESCE(e.raw->>'vessel', e.raw->>'Vessel')
                       FROM intake_event e
                       WHERE e.reservation_id = r.id
                       ORDER BY e.id
                       LIMIT 1
                   )) AS vessel_name,
                   v.imo AS vessel_imo,
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


@app.get("/conflicts")
def list_conflicts(
    status: str | None = None,
    limit: int = 200,
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
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
    (empty station range) never conflicts."""
    # Inverted bounds are a no-op window, not a 500 (mirrors /reservations).
    if from_ is not None and to is not None and from_ > to:
        from_, to = to, from_
    return find_conflicts(session, t_from=from_, t_to=to, status=status, limit=limit)


@app.get("/verification")
def get_verification(
    limit: int = 200,
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
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
    window overlaps that span."""
    # Inverted bounds are a no-op window, not a 500 (mirrors /reservations).
    if from_ is not None and to is not None and from_ > to:
        from_, to = to, from_
    return verify(session, t_from=from_, t_to=to, limit=limit)


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
