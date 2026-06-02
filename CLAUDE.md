# Port of Port Arthur — Berth & Dredging Data Layer

You are building the data layer for a unified berth + dredging scheduling system
for the Port of Port Arthur. This is the single source of truth that replaces
several hand-maintained spreadsheets and three uncoordinated intake channels
(online form, dock-operator entry, phone calls).

## Core model — read this first, it drives everything

The wharf is referenced **linearly**, not by discrete berths. Vessels and
dredging jobs are positioned by *station* along the wharf face.

- Canonical position measure = **POPA stationing**, in feet, stored on a PostGIS
  measured (`M`-valued) linestring representing the wharf centerline. "Berths"
  are just named station ranges — convenience labels, not the allocation unit.
- Three external stationing systems must reconcile to the canonical one. They
  are **affine functions** of POPA station (verified against the port's
  stationing crosswalk):
  - **Corps/USACE** = POPA + 12,040.65 ft (constant offset, same direction)
  - **Dock No.** ≈ 3365 − POPA station (reversed)
  - Stored as per-segment affine params (`scale`, `offset`) so the math
    generalizes; do not hard-code one transform inline.
- **Every reservation is a rectangle in (time) × (station) space.** A vessel
  occupies a station interval `[stern_sta, bow_sta]` over `[ETB, ETD]`. A
  dredging op occupies a station interval over a time window. **A conflict —
  vessel-vs-vessel OR vessel-vs-dredge — is: time ranges overlap AND station
  ranges overlap.** One primitive covers both problems.

## Data source — bootstrap from AIS, not from requests

Do **not** start from berth requests or the legacy spreadsheets. Seed the data
layer from **observed vessel data via AIS**, so the system has real occupancy
from day one with no manual intake.

- Source: **aisstream.io** (free websocket, `wss://stream.aisstream.io/v0/stream`).
  Subscribe with a `BoundingBox` around the POPA wharf on the Sabine-Neches
  waterway; send the subscription within 3s of connecting or it drops. Consume
  `PositionReport` (lat/lon, SOG, COG, heading) and `ShipStaticData` (name, IMO,
  callsign, dimensions A/B/C/D → LOA/beam, draught, destination, ETA).
- For historical backfill later, USCG/NOAA **Marine Cadastre** AIS is free; keep
  the ingestion source-agnostic so either feeds the same pipeline.
- **Derive occupancy, don't ask for it.** When a vessel's position sits within
  the wharf polygon (or within N meters of the quay line) at SOG ≈ 0 for a
  sustained window, emit a derived `reservation` of type `vessel`, status
  `observed`: project bow/stern onto the canonical wharf line to get the station
  range, use COG/heading vs channel axis for direction, and use the
  berthed→unberthed interval as the time range.
- **Geo → station for free:** the wharf centerline is a measured (`M` = POPA
  station) PostGIS line. `ST_LineLocatePoint` / `ST_InterpolatePoint` on an AIS
  lat/lon → interpolated `M` → canonical station.
- Berth **requests/intake** (form, operator, phone) are a **later layer** that
  gets reconciled against observed AIS — not part of the initial build.

## Stack (decided)

- Postgres + PostGIS
- Python, FastAPI, SQLAlchemy + GeoAlchemy2, Alembic for migrations
- pytest for tests
- Front end was meant for later, but a **read-only Leaflet UI now exists**
  (`app/static/index.html`, served by `app/main.py`) plus one write surface — the
  manual berth-request form. Both were added ahead of the build order at the
  user's request. Keep new UI thin and over the API; the data layer stays the
  product.

## Schema (target)

- `wharf_segment` — name, canonical measured geometry (`M` = POPA station),
  affine params per external stationing system (corps_scale/offset,
  dockno_scale/offset), and a nullable `apron` polygon (digitized water-side
  berthing zone; the occupancy "alongside" test, migration 0005)
- `vessel` — IMO/MMSI as canonical key (names are non-unique and misspelled),
  name, LOA, beam, draft
- `reservation` — vessel_id (nullable for dredging), type
  (`vessel|dredge|layberth`), `station_range numrange`, `time_range tstzrange`,
  direction (`upstream|downstream`), status
  (`observed|requested|tentative|confirmed|cancelled|completed`), source
  (`ais|form|phone|operator|email`), priority, cargo, notes, created_at
- `intake_event` — raw inbound request exactly as received, before
  normalization (audit + reconciliation trail)
- `position_report` — landed raw AIS positions (source-agnostic)

Enforce no-overlap at the DB, not in app code:

```sql
CREATE EXTENSION IF NOT EXISTS btree_gist;
ALTER TABLE reservation ADD CONSTRAINT no_wharf_overlap
  EXCLUDE USING gist (time_range WITH &&, station_range WITH &&)
  WHERE (status = 'confirmed');
```

The constraint is `confirmed`-only **on purpose**: `observed` AIS rows are
ground truth and must be allowed to overlap planned reservations — that overlap
is the signal you want to surface (a vessel sitting where something else is
planned), not block. Draft must be validated against controlling depth for the
station/time window before a reservation can be confirmed.

## Conventions

- Migrations only via Alembic; never edit the DB by hand.
- All position I/O goes through one crosswalk module (`app/crosswalk.py`) — no
  scattered stationing math.
- Normalize on ingest (canonical units, enums, IMO/MMSI), keep the raw input in
  `intake_event` / `position_report.raw`.
- Backfill is parse → human review → commit; legacy spreadsheets are messy
  (merged cells, free text like `"Chem Orchard - 607'"`, ambiguous `"X or Y"`,
  inline `CANCELLED`/`TBA`/`?`). Do not assume clean auto-parse.
- Tests required for the crosswalk module and the conflict-detection logic.

## Out of scope for now

No scheduling optimizer / auto-assignment (OR-Tools comes much later). The
deliverable is a conflict-safe data layer populated from live AIS.

Note: two items the original plan deferred have been pulled forward at the user's
request and now exist — a **read-only UI** and berth-request intake **capture**
(online-form CSV + manual phone/email/operator entry; see Build order step 7).
What remains out of scope is the **request→AIS reconciliation engine** (assigning
a berth/station to a `requested` row and promoting it toward `confirmed`) and the
legacy-spreadsheet backfill *commit* (its parser exists, the review pipeline does
not).

## Build order

1. ✅ Schema + Alembic migrations + exclusion constraint.
2. ✅ Stationing crosswalk module (canonical ↔ POPA / Corps / Dock No.) with tests.
3. ✅ Wharf centerline: a measured (`M` = POPA station) PostGIS line, REAL —
   derived from the ArcGIS berth polygons by `data/gis/build_centerline.py`
   (anchor Berth 4 = 351 ft), verified by `tests/test_geo_station_real.py`. Coarse
   (one chord per berth — the most a rectangular berth source allows); a finer
   quay survey would densify via the same script.
4. ✅ AIS ingestion: aisstream.io websocket client, bounding box around the wharf,
   persist `PositionReport` + `ShipStaticData`, upsert `vessel` by MMSI/IMO.
5. ✅ Occupancy derivation: detect berthed vessels (near quay, SOG ≈ 0,
   sustained, with hysteresis), project bow/stern to station range, write
   idempotent `observed` reservations (`app/occupancy/*`). "Alongside" now prefers
   the digitized **apron polygon** (`ST_Contains`), falling back to a centerline
   buffer when a segment has none (predicate isolated in
   `app/occupancy/alongside.py`). The apron seed + predicate await a live PostGIS
   to exercise.
6. ⬜ Conflict-detection query/service (time × station overlap), surfacing
   observed-vs-planned and dredge collisions, with tests.
7. 🟡 Request intake — **capture built** ahead of order (`app/intake/*`):
   online-form CSV export and **manual phone/email/operator entry**
   (`POST /intake/berth-request` + a form on the map page) both land raw in
   `intake_event` (deduped); manual entry also creates a `requested` reservation
   with an **empty/unassigned `station_range`**. **Reconciliation** against
   observed AIS (assigning the berth, promoting toward `confirmed`) and the
   legacy-backfill *commit* are still TODO.

**Current state: steps 1–5 complete; step 6 (conflict detection) is the next
core primitive.** Step 7 intake *capture* landed early at the user's request
(not reconciliation). DB-integration tests need a live PostGIS (they auto-skip
without one); pure logic — crosswalk, detector/projection, intake parsing &
normalization — is unit-tested.

## Repo layout

```
app/
  config.py            # env-driven settings (DB, AIS key, bbox, occupancy thresholds)
  db.py                # SQLAlchemy engine / session
  models.py            # ORM models (mirror the migration; migration is truth)
  crosswalk.py         # THE stationing module — all position math lives here
  main.py              # FastAPI: read-only endpoints + map page + POST /intake/berth-request
  static/              # Leaflet UI (index.html, map + occupancy timeline) + port GeoJSON (gis/)
  seed/wharf_seed.py   # seeds wharf_segment: real centerline + apron polygon (from data/gis/)
  ais/
    messages.py        # normalized AISPosition/AISStatic + aisstream parser
    source.py          # AISSource ABC + AisStreamSource (websocket)
    ingest.py          # source-agnostic Ingestor (upsert vessel, land positions)
    run.py             # runnable: python -m app.ais.run
  occupancy/           # step 5: detect.py, project.py, alongside.py, derive.py, run.py
  intake/              # step 7 capture: records.py (CSV parse), ingest.py, source.py,
                       #   run.py (online-form CSV); manual.py (phone/email/operator entry)
data/gis/              # build_centerline.py / to_geojson.py: derive real centerline + apron
                       #   from the ArcGIS berth shapefiles -> static GeoJSON + seed JSON
alembic/               # migrations: 0001 schema · 0002 occupancy · 0003 intake dedupe ·
                       #   0004 'email' source · 0005 wharf_segment.apron
tests/                 # pure: crosswalk, geo→station(real), ais/intake parsers, occupancy math;
                       #   db-marked (auto-skip): geo→station, occupancy derive, intake, reservations
```

## Working agreements for future changes

- New stationing systems = new affine params on `wharf_segment`, never new inline
  math. Extend `crosswalk.py` and its tests together.
- New AIS providers (e.g. Marine Cadastre) = a new `AISSource` that yields the
  same normalized `AISPosition`/`AISStatic`; the ingestor must not change.
- Any schema change goes through a new Alembic revision and a matching update to
  `app/models.py`. Keep enum value tuples in `models.py` and the migration in sync.
- The exclusion constraint stays `confirmed`-only. If you think you need to block
  `observed` overlaps, re-read the Core model section first.
- New intake channels = a new `IntakeSource` (CSV path) or a thin call into
  `app/intake/`, all landing raw in `intake_event` (deduped by content-hash
  `dedupe_key`) before any normalization — never skip the raw landing. A
  `requested` reservation projected from intake carries an **empty
  `station_range`** until reconciliation assigns the berth; an empty range never
  conflicts, which is intentional, so don't "fix" it with a placeholder span.
