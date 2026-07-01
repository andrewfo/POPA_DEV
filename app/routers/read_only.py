"""Read-only endpoints over the data layer — never mutate rows.

Segments, recent AIS positions, reservation history, vessels, headline stats,
geo->station projection, live occupancy, reservations, the berth catalog, plus
the bbox/db-health probes the map and ops UI poll.
"""
from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crosswalk import (
    geo_to_station,
    segment_corps_params,
    segment_dockno_params,
)
from app.db import get_session
from app.models import Vessel, WharfSegment
from app.occupancy.alongside import alongside_sql, nearest_segment_lateral
from app.workers import KNOWN_WORKERS, classify

router = APIRouter()


@router.get("/workers")
def workers(session: Session = Depends(get_session)) -> dict:
    """Per-worker liveness for the console footer. One entry per known worker
    (the live AIS ingestor, the occupancy + verification-sweep batch, the
    optional AI/Dataverse intake poller), even one that has never beat (shown
    ``offline``). Each worker upserts a ``worker_heartbeat`` row every cycle;
    here we read it back and derive ``health`` from how stale the last beat is
    against the worker's nominal cadence. Age is measured against the DB clock
    (now() - beat_at), not the request host's, so it's skew-free."""
    rows = {
        r.name: r
        for r in session.execute(
            text(
                "SELECT name, status, detail, beat_at, "
                "EXTRACT(EPOCH FROM (now() - beat_at)) AS age_s "
                "FROM worker_heartbeat"
            )
        ).all()
    }

    def entry(name: str, label: str, cadence: int) -> dict:
        r = rows.pop(name, None)
        if r is None:
            return {
                "name": name, "label": label, "status": None, "health": "offline",
                "beat_at": None, "age_seconds": None,
                "cadence_seconds": cadence, "detail": None,
            }
        age = float(r.age_s)
        return {
            "name": name,
            "label": label,
            "status": r.status,
            "health": classify(r.status, age, cadence),
            "beat_at": r.beat_at.isoformat(),
            "age_seconds": round(age),
            "cadence_seconds": cadence,
            "detail": r.detail,
        }

    out = [entry(n, label, cad) for n, (label, cad) in KNOWN_WORKERS.items()]
    # Any heartbeat from a worker not in the registry (forward-compat): list it
    # too, judged against a generic cadence so it's never silently dropped.
    for name, r in rows.items():
        age = float(r.age_s)
        out.append({
            "name": name, "label": name, "status": r.status,
            "health": classify(r.status, age, 60),
            "beat_at": r.beat_at.isoformat(), "age_seconds": round(age),
            "cadence_seconds": 60, "detail": r.detail,
        })
    return {"workers": out}


@router.get("/config/bbox")
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


@router.get("/health/db")
def health_db(session: Session = Depends(get_session)) -> dict:
    session.execute(text("SELECT 1"))
    postgis = session.execute(text("SELECT PostGIS_Lib_Version()")).scalar_one()
    return {"status": "ok", "postgis": postgis}


@router.get("/wharf-segments")
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


@router.get("/wharf-segments/geojson")
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


@router.get("/positions/recent")
def positions_recent(
    limit: int = 500, session: Session = Depends(get_session)
) -> list[dict]:
    """Most recent landed AIS positions, joined to vessel name when known.

    Each fix carries an ``alongside`` flag — whether it sits in the berthing
    zone (the digitized apron polygon, else a centerline buffer; the one
    predicate in ``app/occupancy/alongside.py``). The map colours a contact by
    ship-type category and uses ``alongside`` plus a low SOG to show state by
    motion — moored contacts sit still, underway ones pulse — so a vessel stopped
    mid-channel reads underway, not moored.

    Only contacts whose fix is **current** are returned: ``msg_ts`` must be within
    ``berth_stale_close_min`` of the feed clock (the newest fix landed anywhere),
    the *same* recency gate ``/occupancy/moored`` applies. A vessel that left or
    went AIS-dark stops broadcasting, so its last-known position ages out of the
    feed instead of lingering as a frozen dot — the map and the "moored now" panel
    therefore share one definition of "still here" (and a stalled ingestor doesn't
    blank every contact, since it's measured against the feed clock, not now())."""
    point = "ST_SetSRID(ST_MakePoint(pr.lon, pr.lat), 4326)"
    settings = get_settings()
    rows = session.execute(
        text(
            f"""
            SELECT pr.id, pr.mmsi, pr.lat, pr.lon, pr.sog, pr.cog, pr.heading,
                   pr.msg_ts, v.name AS vessel_name, v.ship_type,
                   COALESCE({alongside_sql(point)}, false) AS alongside
            FROM position_report pr
            LEFT JOIN vessel v ON v.id = pr.vessel_id
            {nearest_segment_lateral(point)}
            WHERE pr.msg_ts >= (SELECT max(msg_ts) FROM position_report)
                               - (:stale_min * interval '1 minute')
            ORDER BY pr.msg_ts DESC NULLS LAST, pr.id DESC
            LIMIT :limit
            """
        ),
        {
            "limit": limit,
            "buffer_m": settings.berth_buffer_m,
            "stale_min": settings.berth_stale_close_min,
        },
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
            # AIS numeric ship type (ITU-R M.1371: 80s tanker, 70s cargo, 52 tug,
            # 33 dredger, 50 pilot, ...); the map buckets it into a coloured
            # category (shipTypeCategory). NULL until the vessel's ShipStaticData
            # lands.
            "ship_type": r.ship_type,
            "alongside": bool(r.alongside),
        }
        for r in rows
    ]


@router.get("/history")
def reservation_history(
    name: str | None = None,
    imo: str | None = None,
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
    ``imo`` (digit substring of the vessel's IMO — IMO-less requests carry no IMO
    so they drop out of an IMO search), ``status`` (exact), and a ``from``/``to``
    date window (ISO datetimes — FastAPI rejects malformed with 422 — an overlap
    test on ``time_range``)."""
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
                       v.id AS vessel_id, v.mmsi AS vessel_mmsi,
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
                  AND (CAST(:imo AS text) IS NULL
                       OR CAST(v.imo AS text) LIKE '%' || CAST(:imo AS text) || '%')
            )
            SELECT * FROM res
            WHERE (CAST(:name AS text) IS NULL
                   OR vessel_name ILIKE '%' || CAST(:name AS text) || '%')
            ORDER BY t_start DESC NULLS LAST, created_at DESC
            LIMIT :limit
            """
        ),
        {"status": status, "name": name, "imo": imo, "t_from": from_,
         "t_to": to, "limit": limit},
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
            "vessel_id": r.vessel_id,
            "vessel_name": r.vessel_name,
            "vessel_mmsi": r.vessel_mmsi,
            "vessel_imo": r.vessel_imo,
            "ship_type": r.ship_type,
            "berth_id": r.berth_id,
            "berth_name": r.berth_name,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@router.get("/vessels")
def list_vessels(
    limit: int = 100, session: Session = Depends(get_session)
) -> list[dict]:
    """Recent vessels with their latest AIS fix folded in. Each static vessel
    record (identity + dimensions) joins, per row, to its most recent landed
    ``position_report`` via a LATERAL — so the ops list carries live movement
    state (SOG/COG/nav status), not just the registry."""
    rows = session.execute(
        text(
            """
            SELECT v.id, v.mmsi, v.imo, v.name, v.callsign, v.ship_type,
                   v.destination, v.loa, v.beam, v.draft, v.dims_locked,
                   p.sog, p.cog, p.nav_status, p.msg_ts
            FROM vessel v
            LEFT JOIN LATERAL (
                SELECT sog, cog, nav_status, msg_ts
                FROM position_report
                WHERE vessel_id = v.id
                   OR (v.mmsi IS NOT NULL AND mmsi = v.mmsi)
                ORDER BY msg_ts DESC NULLS LAST, id DESC
                LIMIT 1
            ) p ON true
            ORDER BY v.updated_at DESC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    ).all()
    return [
        {
            "id": r.id,
            "mmsi": r.mmsi,
            "imo": r.imo,
            "name": r.name,
            "callsign": r.callsign,
            "ship_type": r.ship_type,
            "destination": r.destination,
            "loa": float(r.loa) if r.loa is not None else None,
            "beam": float(r.beam) if r.beam is not None else None,
            "draft": float(r.draft) if r.draft is not None else None,
            "dims_locked": bool(r.dims_locked),
            "sog": r.sog,
            "cog": r.cog,
            "nav_status": r.nav_status,
            "position_ts": r.msg_ts.isoformat() if r.msg_ts else None,
        }
        for r in rows
    ]


@router.get("/vessels/{vessel_id}")
def vessel_detail(
    vessel_id: int, session: Session = Depends(get_session)
) -> dict:
    """Everything on file for one vessel — the ship detail view behind a History
    click. Aggregates three things in one round trip: the full ``vessel`` record
    (every column, dimensions in canonical metres), its whole reservation log
    (every status, newest arrival first, with POPA + Dock No. station bounds like
    ``/history``), and its latest landed AIS position (lat/lon, SOG/COG/heading,
    nav status, time). 404 if no such vessel."""
    v = session.get(Vessel, vessel_id)
    if v is None:
        raise HTTPException(status_code=404, detail="vessel not found")

    dock = segment_dockno_params(session)

    def dk(val: object) -> float | None:
        return float(dock.from_popa(float(val))) if val is not None else None

    def fl(val: object) -> float | None:
        return float(val) if val is not None else None

    res_rows = session.execute(
        text(
            """
            SELECT r.id, r.type, r.status, r.source, r.direction, r.cargo,
                   r.notes, r.priority,
                   lower(r.time_range)      AS t_start,
                   upper(r.time_range)      AS t_end,
                   isempty(r.station_range) AS sta_unassigned,
                   lower(r.station_range)   AS sta_lo,
                   upper(r.station_range)   AS sta_hi,
                   r.created_at, r.berth_id, b.name AS berth_name
            FROM reservation r
            LEFT JOIN berth b ON b.id = r.berth_id
            WHERE r.vessel_id = :vid
            ORDER BY lower(r.time_range) DESC NULLS LAST, r.created_at DESC
            """
        ),
        {"vid": vessel_id},
    ).all()
    reservations = [
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
            "station_lo": fl(r.sta_lo),
            "station_hi": fl(r.sta_hi),
            "station_lo_dock": dk(r.sta_lo),
            "station_hi_dock": dk(r.sta_hi),
            "berth_id": r.berth_id,
            "berth_name": r.berth_name,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in res_rows
    ]

    # Latest landed AIS fix for this vessel (by MMSI or vessel_id link), newest
    # first — the same ordering /positions/recent uses.
    pos = session.execute(
        text(
            """
            SELECT lat, lon, sog, cog, heading, nav_status, msg_ts
            FROM position_report
            WHERE vessel_id = :vid
               OR (CAST(:mmsi AS bigint) IS NOT NULL AND mmsi = CAST(:mmsi AS bigint))
            ORDER BY msg_ts DESC NULLS LAST, id DESC
            LIMIT 1
            """
        ),
        {"vid": vessel_id, "mmsi": v.mmsi},
    ).first()
    latest_position = (
        {
            "lat": pos.lat,
            "lon": pos.lon,
            "sog": pos.sog,
            "cog": pos.cog,
            "heading": pos.heading,
            "nav_status": pos.nav_status,
            "msg_ts": pos.msg_ts.isoformat() if pos.msg_ts else None,
        }
        if pos is not None
        else None
    )

    return {
        "id": v.id,
        "mmsi": v.mmsi,
        "imo": v.imo,
        "name": v.name,
        "callsign": v.callsign,
        "ship_type": v.ship_type,
        "destination": v.destination,
        "loa": fl(v.loa),
        "beam": fl(v.beam),
        "draft": fl(v.draft),
        "dim_a": fl(v.dim_a),
        "dim_b": fl(v.dim_b),
        "created_at": v.created_at.isoformat() if v.created_at else None,
        "updated_at": v.updated_at.isoformat() if v.updated_at else None,
        "reservations": reservations,
        "latest_position": latest_position,
    }


@router.get("/stats")
def stats(session: Session = Depends(get_session)) -> dict:
    """Headline counts for the sidebar. Beyond present-vessel/request totals this
    surfaces the request + reservation picture: how many berth requests have
    landed, how many reservations exist by the statuses operators watch, and how
    many vessels are moored *right now* (latest AIS fix alongside the wharf and
    effectively stopped — the same test the map's green dots use). "Vessels" is
    vessels *present* (an AIS fix within the present-window), not every vessel
    row ever ingested — a vessel row is never removed when a ship leaves."""
    # The sidebar tiles count the *live* book of operator-managed work:
    # requested/tentative/confirmed reservations that are current or upcoming.
    # Deliberately scoped two ways, matching the conflicts/verification panels:
    #   - status: only the planning statuses. `observed` rows are AIS ground
    #     truth (they belong to the occupancy/History surfaces; counting them let
    #     derived berthings dwarf the handful of real bookings), `completed` is
    #     done, `cancelled` is dropped.
    #   - time: only rows whose window hasn't fully elapsed. A confirmed booking
    #     whose ETD is in the past is history — e.g. a kept no-show the sweep
    #     flags but won't auto-cancel — not "confirmed right now", so it drops off
    #     the tile the same way it's absent from the Reservations tab's current
    #     view. (Empty/unbounded windows are kept — they have no past edge.)
    res_rows = session.execute(
        text(
            """
            SELECT status::text AS status, count(*) AS n
            FROM reservation
            WHERE status IN ('requested', 'tentative', 'confirmed')
              AND (upper(time_range) IS NULL OR upper(time_range) >= now())
            GROUP BY status
            """
        )
    ).all()
    by_status = {r.status: r.n for r in res_rows}
    # Moored *now* = contacts whose latest AIS fix is alongside (in the berthing
    # zone) AND effectively stopped. This must agree with the map's green dots, so
    # it reproduces their definition exactly: dedupe per MMSI (not vessel_id, which
    # is NULL until ShipStaticData lands — the map keys on MMSI), pick the latest
    # fix with the same ordering /positions/recent uses (msg_ts DESC NULLS LAST,
    # id DESC), and apply the same alongside predicate + the shared
    # berth_enter_sog_kn threshold (also handed to the client via /config/bbox).
    # The latest fix must also be *current*: a vessel that left stops broadcasting
    # in the bbox, so its last fix — even if it was alongside and stopped — must
    # not linger as "moored now". Gate it the way the occupancy worker closes a
    # berthing: silent longer than berth_stale_close_min, measured against the
    # FEED CLOCK (newest fix anywhere), not wall time — so a stalled ingestor
    # doesn't age out every contact, and this agrees with the observed
    # reservations the verification panel + map outlines are built on. (Tighter,
    # feed-relative gate than vessels_present's broad 24h wall-clock window.)
    settings = get_settings()
    point = "ST_SetSRID(ST_MakePoint(l.lon, l.lat), 4326)"
    moored = session.execute(
        text(
            f"""
            WITH latest AS (
                SELECT DISTINCT ON (pr.mmsi)
                       pr.mmsi, pr.lat, pr.lon, pr.sog, pr.msg_ts
                FROM position_report pr
                WHERE pr.mmsi IS NOT NULL
                ORDER BY pr.mmsi, pr.msg_ts DESC NULLS LAST, pr.id DESC
            )
            SELECT count(*)
            FROM latest l
            {nearest_segment_lateral(point)}
            WHERE l.sog IS NOT NULL AND l.sog < :enter_sog
              AND l.msg_ts >= (SELECT max(msg_ts) FROM position_report)
                              - (:stale_min * interval '1 minute')
              AND COALESCE({alongside_sql(point)}, false)
            """
        ),
        {
            "enter_sog": settings.berth_enter_sog_kn,
            "buffer_m": settings.berth_buffer_m,
            "stale_min": settings.berth_stale_close_min,
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
        # Live (not soft-deleted) requests only — a withdrawn request is kept for
        # audit but shouldn't inflate the operator's request count.
        "berth_requests": session.execute(
            text("SELECT count(*) FROM intake_event WHERE deleted_at IS NULL")
        ).scalar_one(),
        # The live book of work (see the scan above): requested/tentative/
        # confirmed, current or upcoming only.
        "reservations": sum(by_status.values()),
        "moored": moored,
        "confirmed": by_status.get("confirmed", 0),
        "requested": by_status.get("requested", 0),
    }


@router.get("/geo-to-station")
def geo_to_station_endpoint(
    lat: float, lon: float, segment_id: int | None = None,
    session: Session = Depends(get_session),
) -> dict:
    """Project a lat/lon onto the wharf centerline -> canonical POPA station,
    with the Corps/USACE and Dock No. equivalents converted server-side through
    the per-segment crosswalk (the UI never does stationing math)."""
    station = geo_to_station(session, lat, lon, segment_id)
    corps = dockno = None
    if station is not None:
        corps = segment_corps_params(session, segment_id).from_popa(station)
        dockno = segment_dockno_params(session, segment_id).from_popa(station)
    return {
        "lat": lat, "lon": lon, "popa_station": station,
        "corps": corps, "dockno": dockno,
    }


@router.get("/occupancy/moored")
def occupancy_moored(session: Session = Depends(get_session)) -> list[dict]:
    """Vessels alongside **right now** — the detailed, per-vessel counterpart of
    the ``moored`` count in ``/stats``: each vessel's latest AIS fix that sits in
    the berthing zone (apron polygon, else centerline buffer) below the mooring
    SOG threshold. Same predicate (``alongside_sql`` + ``nearest_segment_lateral``
    + ``berth_enter_sog_kn``) as the stat, so the list and the count can't drift.

    Each fix is projected onto the wharf centerline (POPA station, via the one
    crosswalk path ``geo_to_station``) and labelled with the berth whose station
    range contains it. This reads **live positions**, not derived ``observed``
    reservations, so "who's alongside" populates without the occupancy-derivation
    worker running. Newest fix first. The latest fix must be *current*: a departed
    vessel's last fix — even if it was alongside and stopped — must not linger
    here, so it's gated the way the occupancy worker closes a berthing: silent
    longer than ``berth_stale_close_min`` against the FEED CLOCK (newest fix
    anywhere), not wall time. This agrees with the observed reservations the
    verification panel and map outlines use (same threshold + clock), and with
    the ``moored`` stat.

    ``since`` is when the vessel went alongside — the start of its ongoing
    ``observed`` berthing (the same value the AIS-verification panel shows, so the
    two agree), NOT the latest-fix time (which is ~now and would misread "since").
    It falls back to the latest fix only when no observed berthing exists yet
    (worker hasn't run)."""
    settings = get_settings()
    point = "ST_SetSRID(ST_MakePoint(l.lon, l.lat), 4326)"
    rows = session.execute(
        text(
            f"""
            WITH latest AS (
                SELECT DISTINCT ON (pr.mmsi)
                       pr.mmsi, pr.lat, pr.lon, pr.sog, pr.msg_ts, pr.vessel_id
                FROM position_report pr
                WHERE pr.mmsi IS NOT NULL
                ORDER BY pr.mmsi, pr.msg_ts DESC NULLS LAST, pr.id DESC
            )
            SELECT l.mmsi, l.lat, l.lon, l.sog, l.msg_ts,
                   v.id AS vessel_id, v.name AS vessel_name,
                   COALESCE(obs.since, l.msg_ts) AS since
            FROM latest l
            LEFT JOIN vessel v ON v.id = l.vessel_id
                              OR (l.vessel_id IS NULL AND v.mmsi = l.mmsi)
            LEFT JOIN LATERAL (
                SELECT lower(r.time_range) AS since
                FROM reservation r
                WHERE r.vessel_id = l.vessel_id
                  AND r.status = 'observed'
                  AND r.time_range @> now()
                ORDER BY lower(r.time_range) DESC
                LIMIT 1
            ) obs ON true
            {nearest_segment_lateral(point)}
            WHERE l.sog IS NOT NULL AND l.sog < :enter_sog
              AND l.msg_ts >= (SELECT max(msg_ts) FROM position_report)
                              - (:stale_min * interval '1 minute')
              AND COALESCE({alongside_sql(point)}, false)
            ORDER BY l.msg_ts DESC NULLS LAST
            """
        ),
        {
            "enter_sog": settings.berth_enter_sog_kn,
            "buffer_m": settings.berth_buffer_m,
            "stale_min": settings.berth_stale_close_min,
        },
    ).all()
    # Berth catalog loaded once; matching a POPA station to a named berth is a
    # range-membership lookup, not stationing math (that stays in the crosswalk).
    berths = session.execute(
        text("SELECT name, popa_sta_start, popa_sta_end FROM berth ORDER BY popa_sta_start")
    ).all()

    def berth_for(sta: float | None) -> str | None:
        if sta is None:
            return None
        for b in berths:
            if float(b.popa_sta_start) <= sta <= float(b.popa_sta_end):
                return b.name
        return None

    out = []
    for r in rows:
        station = geo_to_station(session, r.lat, r.lon)
        out.append(
            {
                "mmsi": r.mmsi,
                # vessel_id drives the hover dossier (GET /vessels/{id}); null for
                # an AIS contact not yet upserted into the vessel table.
                "vessel_id": r.vessel_id,
                "vessel_name": r.vessel_name,
                "sog": float(r.sog) if r.sog is not None else None,
                "msg_ts": r.msg_ts.isoformat() if r.msg_ts else None,
                # When the vessel went alongside (observed-berthing start), else
                # the latest fix as a fallback — what the card labels "since".
                "since": r.since.isoformat() if r.since else None,
                "popa_station": station,
                "berth_name": berth_for(station),
            }
        )
    return out


@router.get("/reservations")
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


@router.get("/berths")
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
