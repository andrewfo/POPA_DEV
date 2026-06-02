# POPA Wharf Data Layer

Conflict-safe berth + dredging scheduling data layer for the **Port of Port
Arthur**, bootstrapped from live AIS. The wharf is referenced *linearly* by POPA
station (feet) along a measured PostGIS centerline; every reservation is a
rectangle in (time × station) space and a conflict is "time overlaps **and**
station overlaps". See [`CLAUDE.md`](./CLAUDE.md) for the full design.

> **Build steps 1–5 complete** — schema, stationing crosswalk, wharf centerline,
> AIS ingestion, and occupancy derivation. A **read-only Leaflet UI** and
> berth-request intake **capture** (online-form CSV + manual phone/email entry)
> were added ahead of the build order. Still to come: the **conflict-detection
> service** (step 6) and request→AIS **reconciliation** (step 7). See
> [`PLAN.md`](./PLAN.md) for status.

## Prerequisites

- **Python 3.11+**
- **Docker** (for the local Postgres + PostGIS), or your own PostGIS 3.x server
- A free **aisstream.io** API key (only needed to actually run ingestion)

## Setup

```bash
# 1. Configure environment
cp .env.example .env
#   edit .env: set AISSTREAM_API_KEY, adjust the bounding box if needed

# 2. Python environment
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\Activate.ps1
pip install -e .[dev]                # or: pip install -r requirements.txt

# 3. Start Postgres + PostGIS
docker compose up -d

# 4. Apply migrations (creates tables, enums, btree_gist exclusion constraint)
alembic upgrade head

# 5. Seed the wharf segment (real centerline + apron) and the named berth
#    catalog (canonical POPA station range per berth) — all from data/gis/
python -m app.seed.wharf_seed
#   geometry is pre-built; regenerate from the ArcGIS shapefiles with:
#   python data/gis/build_centerline.py   (needs pyshp, pyproj)
```

## Run

```bash
# API + read-only Leaflet map (served at /)
uvicorn app.main:app --reload
#   GET  /                -> map UI + berth occupancy timeline (Gantt drawer)
#   GET  /health          -> liveness
#   GET  /health/db       -> DB + PostGIS reachable
#   GET  /wharf-segments  -> seeded segments + affine params
#   GET  /vessels         -> vessels seen via AIS
#   GET  /stats           -> row counts
#   GET  /geo-to-station?lat=..&lon=..  -> project a point to POPA station
#   GET  /reservations[?status=&from=&to=]  -> reservations (status + time-window filter)
#   GET  /berths          -> named berth catalog (POPA station ranges) to assign from
#   POST /intake/berth-request  -> manual berth request (phone/email); also a form on /
#   PATCH /reservations/{id}    -> edit; assign a berth via {"berth_id": N} (fills station_range)

# AIS ingestion (live aisstream.io websocket -> DB). Long-running; reconnects.
python -m app.ais.run

# Berth-request intake (online-form CSV export -> intake_event; idempotent)
python -m app.intake.run path/to/BerthRequests.csv [--dry-run]
```

## Tests

```bash
pytest                       # pure tests (crosswalk + AIS parser) always run
pytest -m "not db"           # explicitly skip DB tests
pytest -m db                 # DB tests (geo->station); needs migrated PostGIS
```

The `db`-marked tests auto-skip if no database is reachable, so `pytest` is green
even with nothing running. To run them, have the DB up and migrated (steps 3–4),
and either set `DATABASE_URL` or rely on the `.env` defaults.

## AIS bounding box

The subscription box (in `.env`, defaults in `app/config.py`) covers the POPA
public wharf on the Sabine-Neches waterway:

| Corner | Latitude | Longitude |
|--------|----------|-----------|
| SW     | 29.855   | −93.945   |
| NE     | 29.885   | −93.920   |

aisstream wants `BoundingBoxes` as `[[[sw_lat, sw_lon], [ne_lat, ne_lon]]]`; the
config exposes exactly that via `Settings.ais_bounding_box`. Tighten or widen by
editing the `AIS_BBOX_*` vars — no code change needed.

## Wharf geometry (real)

The centerline and the apron/berthing-zone polygon are **derived from the port's
ArcGIS berth shapefiles** by `data/gis/build_centerline.py` (anchor Berth 4 =
station 351 ft) — not placeholders. `app/seed/wharf_seed.py` seeds both into
`wharf_segment`, and `tests/test_geo_station_real.py` verifies geo→station
end-to-end with no database. Caveat: the centerline is a coarse 7-vertex line
(one chord per berth — the most rectangular berth polygons allow); a finer quay
survey would densify it via the same script.

## Occupancy derivation (step 5) — built

`python -m app.occupancy.run` reads `position_report`, detects berthed vessels
(SOG ≈ 0 inside the "alongside" zone, with hysteresis), projects bow/stern to a
`[stern_sta, bow_sta]` POPA station range, and writes idempotent `observed`
reservations. "Alongside" prefers the digitized **apron polygon**
(`ST_Contains`), falling back to a centerline buffer when a segment has none
(`app/occupancy/alongside.py`). See `app/occupancy/*` and `PLAN.md` §2.

## Project layout

See the "Repo layout" section in [`CLAUDE.md`](./CLAUDE.md).
