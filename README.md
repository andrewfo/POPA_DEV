# POPA Wharf Data Layer

Conflict-safe berth + dredging scheduling data layer for the **Port of Port
Arthur**, bootstrapped from live AIS. The wharf is referenced *linearly* by POPA
station (feet) along a measured PostGIS centerline; every reservation is a
rectangle in (time × station) space and a conflict is "time overlaps **and**
station overlaps". See [`CLAUDE.md`](./CLAUDE.md) for the full design.

> **Build steps 1–6 complete** — schema, stationing crosswalk, wharf centerline,
> AIS ingestion, occupancy derivation, and the **conflict-detection service**. A
> **read-only Leaflet UI**, a **manual edit surface**, and berth-request intake
> **capture** (manual phone/email/operator entry — the online-form feed was
> retired) were added ahead of the build order. Step 7's **AIS verification
> layer** now exists too (`GET /verification`): after-arrival checks of operator
> placements against observed AIS (arrived / no-show / awaiting / where-planned /
> unplanned), read-only — *not* an auto-placer — plus auto status-mutation of
> stale rows (`POST /verification/sweep`). An **optional AI-assisted intake
> channel** also exists: a worker pulls the Power Pages / Dataverse berth-request
> table and a cheap LLM (OpenRouter / Gemini Flash) normalizes each row before it
> lands as a `requested` row — it *proposes, never places*. Still to come: the
> legacy-spreadsheet backfill commit. See [`PLAN.md`](./PLAN.md) for status.

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
# One-command dev stack (DB + migrate + seed + API + AIS ingestor):
./scripts/dev.sh                  # macOS / Linux
.\scripts\dev.ps1                 # Windows (PowerShell)
# Add --occupancy / -Occupancy for the occupancy derivation loop.
# Use --down / -Down to stop the DB container.

# Or run each piece individually:

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
#   GET  /conflicts[?status=&from=&to=&current=&service_craft=]  -> pairs overlapping in time AND
#                            station (+ overlap rect). Live-scoped by default: current/future only,
#                            harbor craft (tugs/towboats/pilots) hidden; widen via the params
#   GET  /verification[?current=&service_craft=]  -> plan vs observed AIS (unplanned list is
#                            ongoing-only + no harbor craft by default)
#   GET  /berths          -> named berth catalog (POPA station ranges) to assign from
#   POST /intake/berth-request  -> manual berth request (phone/email); also a form on /
#   PATCH /reservations/{id}    -> edit; assign a berth via {"berth_id": N} (fills station_range)

# AIS ingestion (live aisstream.io websocket -> DB). Long-running; reconnects.
python -m app.ais.run

# OPTIONAL AI-assisted intake worker: poll the Power Pages / Dataverse berth-
# request table, LLM-normalize each new row (OpenRouter), record it as a
# `requested` reservation, then mark the row triaged. Long-running; outbound only.
# Needs OPENROUTER_API_KEY + the DATAVERSE_* vars (see .env.example); self-exits
# if they're unset. Manual entry stays available via the form on the map page.
python -m app.intake.dataverse_run
#   --once       run one batch then exit (instead of looping)
#   --dry-run    fetch + parse + PRINT each row, write nothing (Dataverse + key, no DB)
#   --input F    parse sample rows from a local JSON file; needs ONLY OpenRouter —
#                no Dataverse, no DB. Eyeball the model before going live:
python -m app.intake.dataverse_run --input docs/sample_berth_requests.json
```

## Deploy (production)

`scripts/dev.*` and `docker-compose.yml` are for **local dev only** (DB
container + bare `uvicorn --reload`). For a real deployment use the production
image + stack — see [`DEPLOY.md`](./DEPLOY.md) for the full host + deployment
playbook (reverse proxy + TLS over a sanctioned network). In brief:

```bash
# 1. Secrets: copy and fill in (gitignored). Set a strong DB password, the
#    operator login, and your AIS key.
cp .env.example .env

# 2. Build the image and bring up the full stack:
#    db + a one-shot migrate/seed + api (gunicorn) + ais ingestor + occupancy.
docker compose -f docker-compose.prod.yml up -d --build
```

The `migrate` service runs `alembic upgrade head` + the wharf/berth seed **once**
to completion, and `api`/`ais`/`occupancy` wait for it (and a healthy DB) before
starting. One image (`Dockerfile`) runs all four roles. Tail logs / tear down:
`docker compose -f docker-compose.prod.yml logs -f`,
`... down` (add `-v` to also drop the data volume).

**Auth.** HTTP Basic gates the **whole app** (map + reads + writes). It is active
**only when `OPERATOR_USER` *and* `OPERATOR_PASSWORD` are set** — leave either
blank and the app runs open (fine for a private dev box, never for a reachable
deployment). `/health` stays open so the container healthcheck can probe. The
browser caches the login and replays it on the map's API calls, so no UI changes
are needed.

**TLS is required.** Basic auth only base64-encodes credentials, so terminate
TLS in a reverse proxy / load balancer (nginx, Caddy, cloud LB) in front of
`api`; do not expose port 8000 to the internet over plain HTTP. For stronger
secret isolation than an env file, switch the compose `env_file` to file-backed
Docker `secrets:`.

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
| SW     | 29.823   | −93.9586  |
| NE     | 29.866   | −93.930   |

The SW corner reaches out into the navigation channel (the water side where
vessels transit/berth); the NE corner stays snug to the quay, off the city.

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
(`app/occupancy/alongside.py`). A berthing whose vessel has gone silent past
`BERTH_STALE_CLOSE_MIN` (default 180) — while the feed kept landing other
traffic — is closed at its last fix instead of reading "ongoing" forever.
See `app/occupancy/*` and `PLAN.md` §2.

The AIS verification panel needs this worker running (it is what turns raw
positions into `observed` rows) — in dev, start the stack with
`.\scripts\dev.ps1 -Occupancy` / `./scripts/dev.sh --occupancy`.

## Project layout

See the "Repo layout" section in [`CLAUDE.md`](./CLAUDE.md).
