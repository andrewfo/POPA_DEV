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
  (`app/static/index.html`, served by `app/main.py`) plus write surfaces — the
  manual berth-request form and a **manual edit surface** for ship data and
  scheduling (create/edit/cancel/delete vessels & reservations; see
  `app/edit.py`). All were added ahead of the build order at the user's request.
  Keep new UI thin and over the API; the data layer stays the
  product.
- **Deployment** is a production Docker image (`Dockerfile`, one image for all
  four roles) + `docker-compose.prod.yml` (db + one-shot migrate/seed + api +
  ais + occupancy), distinct from the dev-only `docker-compose.yml` + `scripts/dev.*`.
  The whole app sits behind **HTTP Basic** (`app/auth.py`).

## Schema (target)

- `wharf_segment` — name, canonical measured geometry (`M` = POPA station),
  affine params per external stationing system (corps_scale/offset,
  dockno_scale/offset), and a nullable `apron` polygon (digitized water-side
  berthing zone; the occupancy "alongside" test, migration 0005)
- `vessel` — IMO/MMSI as canonical key (names are non-unique and misspelled),
  name, LOA, beam, draft
- `berth` — named canonical POPA station range (`popa_sta_start/end`, migration
  0006). "Berths are named station ranges": a berth is the operator's *handle*,
  not the allocation unit. Seeded from the port's berth shapefile via
  `data/gis` (`berth_stations.json`). Assigning one to a reservation copies its
  range onto `station_range`, so conflict detection still runs on the canonical
  range — never on `berth_id`.
- `reservation` — vessel_id (nullable for dredging), nullable `berth_id`
  (assigned berth, `ON DELETE SET NULL`), type
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
- **Central Time is the canonical wall-clock.** The whole system runs on US
  Central (`America/Chicago`): the DB session is pinned to it (`app/db.py`) and
  it's the database default (migration 0008); `timestamptz` columns still store
  absolute instants, Central is just how they're read/written. Operator input
  arrives zone-less (`datetime-local`/`date` send no zone) and is stamped Central
  via the one time module (`app/tz.py` — `assume_central`); never hard-code a
  `-6`/`-5` offset or attach UTC to form input. AIS `time_utc` is genuinely UTC
  and stays so (it's a real instant; it just renders in Central).
- Normalize on ingest (canonical units, enums, IMO/MMSI), keep the raw input in
  `intake_event` / `position_report.raw`.
- Backfill is parse → human review → commit; legacy spreadsheets are messy
  (merged cells, free text like `"Chem Orchard - 607'"`, ambiguous `"X or Y"`,
  inline `CANCELLED`/`TBA`/`?`). Do not assume clean auto-parse.
- Tests required for the crosswalk module and the conflict-detection logic.

## Out of scope for now

No scheduling optimizer / auto-assignment (OR-Tools comes much later). The
deliverable is a conflict-safe data layer populated from live AIS.

Note: items the original plan deferred have been pulled forward at the user's
request and now exist — a **read-only UI**, berth-request intake **capture**
(manual phone/email/operator entry; see Build order step 7),
and a **manual edit surface** (`app/edit.py`): create/edit/cancel/delete vessels
and reservations, including **manually assigning a berth (from the named `berth`
catalog, which fills `station_range`) and promoting a row to `confirmed`** —
i.e. the *manual* form of reconciliation, done by an operator one row at a time. What remains unbuilt is the
**AIS verification layer** — and it is a *verification*, not a *placement*, tool.
AIS only knows where a vessel **is now / was**, never where a not-yet-arrived
ship **will** berth, and its derived station ranges are **approximate** (a
`confident` flag, `observed` never blocks `confirmed`), not survey-grade — so it
cannot position a reservation. Its job is to check operator placements against
reality once a vessel arrives: did the planned vessel actually show up (advance
status / flag a no-show), berth where planned, and depart (auto-complete); did an
**unrequested** vessel appear (unplanned occupancy). The headline half of that —
*is the vessel where you planned it* — **already ships** as step 6's
`observed-vs-planned` conflict, so the net-new work is status-lifecycle
automation. **Placement stays the operator's job** (and a future scheduling
optimizer's, which works from requests + berth availability, **not** from AIS).
Also unbuilt: the legacy-spreadsheet backfill *commit*
(its parser exists, the review pipeline does not). Confirming still cannot run
the **draft-vs-controlling-depth** gate (no controlling-depth data layer yet);
the edit surface returns a warning saying the check was skipped rather than
blocking. The no-overlap (time × station) guarantee is enforced by the
`confirmed`-only DB exclusion constraint — a confirmed edit that collides
surfaces as a 409.

## Build order

1. ✅ Schema + Alembic migrations + exclusion constraint.
2. ✅ Stationing crosswalk module (canonical ↔ POPA / Corps / Dock No.) with tests.
3. ✅ Wharf centerline: a measured (`M` = POPA station) PostGIS line, REAL —
   built by `data/gis/build_centerline.py` (anchor Berth 4 = 351 ft), verified by
   `tests/test_geo_station_real.py`. The ArcGIS berth polygons are berthing-WATER
   rectangles (their water-side edge is ~250 ft out in the channel), so they give
   **stationing** but not clean quay-face **geometry**: the script chains the
   berth edges as a stationing reference, then projects that onto the surveyed
   bulkhead line (`data/gis/quayface.*`) to place the centerline ON the real quay
   carrying canonical POPA. The NE end clips to the survey's extent (~Dock No. 0 /
   POPA ~3365). A finer quay survey densifies via the same script.
4. ✅ AIS ingestion: aisstream.io websocket client, bounding box around the wharf,
   persist `PositionReport` + `ShipStaticData`, upsert `vessel` by MMSI/IMO.
5. ✅ Occupancy derivation: detect berthed vessels (near quay, SOG ≈ 0,
   sustained, with hysteresis), project bow/stern to station range, write
   idempotent `observed` reservations (`app/occupancy/*`). "Alongside" now prefers
   the digitized **apron polygon** (`ST_Contains`), falling back to a centerline
   buffer when a segment has none (predicate isolated in
   `app/occupancy/alongside.py`). The apron seed + predicate await a live PostGIS
   to exercise.
6. ✅ Conflict-detection query/service (`app/conflicts.py`, `GET /conflicts`):
   the one primitive — time ranges overlap AND station ranges overlap — surfaced
   as a query, covering vessel-vs-vessel, vessel-vs-dredge, and the headline
   observed-vs-planned. Pure overlap predicates (time half-open `[)`, station
   closed `[]`) + `classify` are unit-tested; `find_conflicts` runs the self-join
   with Postgres `&&`/`*` (raw ranges, not the mooring buffer), returning each
   pair's overlap rectangle (POPA + Dock). A thin map surface lists conflicts and
   highlights the contested stretch. The **draft-vs-controlling-depth** half is
   deferred (no depth data layer yet; confirm stays warned-not-enforced).
7. 🟡 Request intake — **capture built** ahead of order (`app/intake/*`):
   **manual phone/email/operator entry** is the primary intake channel
   (`POST /intake/berth-request` + a form on the map page); the automated
   Adobe-Sign online-form feed was retired, but an **optional AI-assisted pull
   channel** was then added (`app/intake/llm.py` + `dataverse_run.py`): a worker
   polls the Power Pages / Dataverse "Berth Request" table **outbound**
   (Azure AD client-creds — no inbound exposure, no HTTP-connector DLP ask),
   hands each messy row to a cheap LLM via **OpenRouter** (default Gemini Flash)
   that normalizes it into a `BerthRequestForm`, and records it through the
   **same `record_manual_request` pipeline**. The model **proposes** (and never
   places — placement/confirm stay the operator's, like AIS verification); its
   confidence/caveats ride onto `reservation.notes`, the verbatim source row is
   preserved in `intake_event.raw` (`BerthRequestForm.source_raw`), and cards are
   tagged `source='email'` so they stay editable. Each entry (manual or AI) lands
   raw in `intake_event` (deduped) and creates a `requested` reservation with an
   **empty/unassigned `station_range`**. A **manual edit surface**
   (`app/edit.py`, sidebar forms over `PATCH /vessels/{id}`,
   `POST /reservations`, `PATCH`/`DELETE /reservations/{id}`) now lets an
   operator correct vessel records and create/edit/cancel/delete reservations,
   including manually assigning a berth from the named `berth` catalog
   (`GET /berths`; assignment fills `station_range`) and promoting to
   `confirmed` (the manual form of reconciliation; a confirmed overlap → 409 via
   the exclusion constraint). An operator can also **edit or delete a manual
   berth request** (Edit / Delete buttons on each request card → `PATCH` /
   `DELETE /intake/berth-requests/{id}`): edit re-projects the linked reservation
   in place (the one sanctioned mutation of an `intake_event.raw` row), delete
   drops the raw row and its projected reservation. Both are manual channels only
   (`phone|email|operator`); any legacy online-form row stays immutable. The
   **AIS verification layer** is now built (`app/verification.py`,
   `GET /verification`): a **read-only** check of operator placements against
   observed AIS — each planned row (vessel + window) is `arrived` / `no_show` /
   `awaiting` with a `where_planned` flag, plus `unplanned` observed berthings no
   request covered. It matches on `vessel_id` + **time** overlap (an empty
   `requested` station range can't match step 6's station-`&&` join) and **never
   places** — AIS can't position a not-yet-arrived ship, and observed ranges are
   approximate, so *placement* stays the operator's job. The **auto
   status-mutation** half is now also built (`expire_stale`,
   `POST /verification/sweep`): a planned row whose window has been fully past for
   longer than the grace period (`config.verification_grace_minutes`, default 60)
   is auto-archived to a **terminal** status — `completed` if AIS observed the
   vessel berth, else `cancelled` (no-show) — with an audit line appended to
   `notes`, so it stops lingering in the live panel and shows in History instead.
   This is a *status-lifecycle* mutation only; it still never **places** a row.
   `GET /verification` stays read-only (the sweep is the explicit write companion;
   the UI calls the sweep, the occupancy worker runs it each batch). Archiving only
   ever moves a row to a status *outside* the confirmed-only exclusion constraint,
   so it can never raise a 409. Still TODO on step 7: the legacy-backfill *commit*.

**Current state: steps 1–6 complete; step 7's AIS verification layer is now built**
(`app/verification.py`, `GET /verification`) — a read-only check of operator
placements against observed AIS (`arrived`/`no_show`/`awaiting` + `where_planned`
+ `unplanned`), matching on `vessel_id` + time overlap, that **never places** a
row (AIS can't position a not-yet-arrived ship; observed ranges are approximate —
placement stays operator-driven, and a future optimizer's). The deferred **auto
status-mutation** half is now built too (`expire_stale` /
`POST /verification/sweep` + the occupancy worker): stale planned rows past their
window + grace auto-archive to `completed`/`cancelled`. An **optional AI-assisted
intake channel** is also now built (`app/intake/llm.py` + `dataverse_run.py`): a
worker pulls the Power Pages / Dataverse "Berth Request" table outbound and a
cheap LLM (OpenRouter / Gemini Flash) normalizes each row into a
`BerthRequestForm`, recorded through the same `record_manual_request` pipeline —
it **proposes, never places**. What remains on step 7: the legacy-spreadsheet
backfill *commit*. Step 7 intake *capture* plus a manual *edit* surface landed
early at the user's request; step 6's draft-vs-controlling-depth gate is deferred
(no depth data layer yet).
DB-integration tests need a live PostGIS (they auto-skip without one); pure
logic — crosswalk, detector/projection, conflict overlap predicates, intake
parsing & normalization, edit range/validation helpers — is unit-tested.

## Repo layout

```
app/
  config.py            # env-driven settings (DB, AIS key, bbox, occupancy thresholds)
  db.py                # SQLAlchemy engine / session
  models.py            # ORM models (mirror the migration; migration is truth)
  crosswalk.py         # THE stationing module — all position math lives here
  tz.py                # THE time module — Central Time canon (assume_central); no inline offsets
  main.py              # FastAPI: read-only endpoints + map page + intake + edit + conflicts
  edit.py              # manual edit surface: vessel patch + reservation create/edit/delete
  conflicts.py         # step 6: time×station overlap primitive + find_conflicts (GET /conflicts)
  verification.py      # step 7: AIS verification of operator placements (GET /verification) —
                       #   arrived/no-show/awaiting + where-planned + unplanned; never PLACES.
                       #   expire_stale (POST /verification/sweep + occupancy worker) auto-archives
                       #   stale planned rows past window+grace -> completed/cancelled (status only)
  auth.py              # HTTP Basic gate (whole-app middleware); active only when OPERATOR_USER+PASSWORD set
  static/              # Leaflet UI (index.html, map + occupancy timeline + edit forms) + GeoJSON (gis/)
  seed/wharf_seed.py   # seeds wharf_segment (real centerline + apron) + berth catalog (from data/gis/)
  ais/
    messages.py        # normalized AISPosition/AISStatic + aisstream parser
    source.py          # AISSource ABC + AisStreamSource (websocket)
    ingest.py          # source-agnostic Ingestor (upsert vessel, land positions)
    run.py             # runnable: python -m app.ais.run
  occupancy/           # step 5: detect.py, project.py, alongside.py, derive.py, run.py
  intake/              # step 7 capture: manual.py — manual phone/email/operator
                       #   entry + dedupe_key + in-place request edit/delete
                       #   (record/update/delete_manual_request). The Adobe-Sign
                       #   online-form feed was retired; an OPTIONAL AI-assisted
                       #   PULL channel was then added (proposes, never places):
                       #   llm.py            # OpenRouter (Gemini Flash) normalizer:
                       #                     #   messy row -> BerthRequestForm; tolerant,
                       #                     #   unit-tested with a faked LLM
                       #   dataverse_run.py  # worker: poll Power Pages/Dataverse table
                       #                     #   (outbound, client-creds) -> llm -> the
                       #                     #   same record_manual_request pipeline
data/gis/              # build_centerline.py / to_geojson.py: real centerline + apron from
                       #   berth shapefiles (stationing) + quayface.* survey (quay geometry)
                       #   -> static GeoJSON + seed JSON
alembic/               # migrations: 0001 schema · 0002 occupancy · 0003 intake dedupe ·
                       #   0004 'email' source · 0005 wharf_segment.apron ·
                       #   0006 berth catalog + reservation.berth_id ·
                       #   0007 min mooring-gap buffer on the overlap constraint ·
                       #   0008 database default TimeZone = America/Chicago
tests/                 # pure: crosswalk, geo→station(real), ais/intake parsers, occupancy math,
                       #   edit range/validation, conflict overlap predicates; db-marked (auto-skip):
                       #   geo→station, occupancy derive, intake, reservations, edit (vessel patch /
                       #   reservation CRUD / 409), conflicts (GET /conflicts overlap matrix)
scripts/
  dev.sh               # one-command local dev stack (macOS/Linux): DB + migrate + seed + API + AIS
  dev.ps1              # same, for Windows (PowerShell)
Dockerfile             # production app image (one image runs all four roles); CMD = gunicorn API
docker-compose.prod.yml# prod stack: db + one-shot migrate/seed + api + ais + occupancy (NOT the dev compose)
.env.example           # single config/secrets template (DB, operator creds, AIS key, prod worker knobs) -> .env
DEPLOY.md              # host + deployment playbook (reverse proxy + TLS over a sanctioned net; Azure/Entra option)
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
- **Auth is whole-app HTTP Basic via a middleware** (`app/auth.py`), not per-route
  dependencies — the middleware is the only thing that also covers the mounted
  static map (`/`, `/static/*`). It is **active only when both `OPERATOR_USER`
  and `OPERATOR_PASSWORD` are set**; blank ⇒ open, on purpose, so dev and the
  TestClient suite run without credentials and a deployment turns it on by env
  alone. Keep `/health` the lone auth-exempt path (the container probe); gate any
  new endpoint by default. Basic only base64-encodes credentials, so it MUST run
  behind TLS terminated upstream (the prod compose does not terminate TLS); the
  api port therefore binds to **`127.0.0.1` only** in `docker-compose.prod.yml`
  (a TLS-terminating reverse proxy on a sanctioned network is the sole front
  door — see `DEPLOY.md`). Don't widen that bind to `0.0.0.0` without putting
  TLS in front.
- Intake is operator-driven (`app/intake/manual.py`); the Adobe-Sign online-form
  feed was retired. There is also an **optional AI-assisted pull channel**
  (`app/intake/llm.py` + `dataverse_run.py`) — see the rules for it below. A new
  intake channel = a thin call into `app/intake/`, landing raw in `intake_event`
  (deduped by content-hash `dedupe_key`) before any normalization — never skip the
  raw landing. A
  `requested` reservation projected from intake carries an **empty
  `station_range`** until reconciliation assigns the berth; an empty range never
  conflicts, which is intentional, so don't "fix" it with a placeholder span.
  **One sanctioned exception to "never mutate raw":** the manual berth-request
  *edit* (`update_manual_request` / `PATCH /intake/berth-requests/{id}`)
  overwrites an existing `intake_event.raw` row in place (re-computing its
  `dedupe_key`) and re-projects the linked reservation — an operator correcting a
  phoned/emailed request one row at a time, rather than leaving a stale
  duplicate. It is restricted to manual channels (`phone|email|operator`); any
  legacy online-form row stays immutable, and the edit leaves the reservation's
  berth/`station_range`, direction, and `status` alone (the request governs
  vessel/time/cargo only, with one exception below). Unlike the **create** path
  for an **AIS-tracked vessel** (one with an MMSI — there the create only
  NULL-fills, keeping AIS dims authoritative), an **edit is authoritative for the
  vessel record**: a provided name/LOA/beam/draft overwrites
  (`_upsert_vessel(..., overwrite=True)`, `COALESCE(new, existing)`) so a
  correction reaches the reservations view — but a field left blank never wipes a
  stored dimension. **One IMO = one ship is enforced on the create path**
  (`_resolve_imo_vessel`): a new request whose IMO is already on file under a
  *different* ship name is **refused** (`ValueError` → 422, pre-landing so no
  orphan audit row) rather than silently merging and renaming the existing ship
  — the operator must use the right IMO or correct the existing record via Edit.
  When the IMO resolves to the **same** ship (stored name matches, or either
  side is unnamed), a **manual-only vessel** (no MMSI) is overwritten so a
  re-submitted corrected LOA/dims takes effect, while an **AIS-tracked vessel**
  (has MMSI) still only NULL-fills (its dimensions stay authoritative). Whenever
  a request's LOA reaches the vessel (create or edit), `_reproject_placements`
  re-derives any **bow-placed, planned** reservation's `station_range` from the
  new LOA holding the bow fixed (berth-assigned, unplaced, un-oriented, and
  `observed` rows are left alone), so the to-scale map footprint follows the
  corrected length instead of staying at the value captured at placement; a
  re-derived `confirmed` row that now collides surfaces as a 409. A content-empty submission (no vessel/imo/etb) is refused
  outright (`record_manual_request` returns `skipped`, lands no row) so a stray
  POST can't create a blank request card. `delete_manual_request` / `DELETE
  /intake/berth-requests/{id}` likewise removes a manual row outright (raw event
  + its projected reservation), same channel restriction. New *channels* still
  never skip the raw landing.
- **The AI-assisted intake channel** (`app/intake/llm.py` + `dataverse_run.py`)
  is a *normalizer*, not a placer — it follows the same "AI proposes, the
  deterministic layer + operator dispose" rule as AIS verification. The LLM (via
  **OpenRouter**, model is a config string defaulting to the cheapest Gemini
  Flash; `httpx`, no SDK dep) only turns a messy row into a `BerthRequestForm`;
  it then flows through the **same `record_manual_request`** path, so it lands raw
  + dedupes + creates the empty-range `requested` row exactly like a hand entry.
  Keep the parse **tolerant** — a bad IMO / unparseable date / ambiguous `"X or
  Y"` becomes null + a note, never a hard failure (the network call is isolated
  behind a `complete` callable so the mapping stays unit-testable with a faked
  LLM). It **never places** (no `station_range`, no confirm) and never auto-writes
  status — placement/confirm stay the operator's. The worker pulls **outbound**
  from Dataverse (Azure AD client-creds); do not replace it with an inbound
  webhook into the api (that reopens the 127.0.0.1-bind + HTTP-connector DLP
  problem the pull was chosen to avoid). AI cards are tagged `source='email'` (a
  manual channel) so an operator can still edit/delete them; the verbatim source
  row is preserved via `BerthRequestForm.source_raw`, and the model's confidence/
  caveats via `BerthRequestForm.notes` → `reservation.notes`.
- The manual **edit** surface (`app/edit.py`) is **authoritative**: a vessel
  edit overwrites the fields it sets (unlike intake *create*, which only fills
  NULLs except when an IMO resolves to the same manual-only ship — see above —
  and refuses an IMO already held by a different ship). Vessel dims are edited in **metres** (the
  canonical store), not feet. Station ranges are entered in **Dock No. feet** —
  the stationing painted on the wharf (the yellow dock markers on the map), what
  an operator actually reads off the quay — and converted to canonical POPA on
  store via the wharf segment's affine params
  (`crosswalk.segment_dockno_params` → `AffineParams.to_popa`). All stationing
  math stays in `app/crosswalk.py` (server-side; the UI never converts), and the
  canonical store stays POPA. Because Dock No. is reversed, the **stern** end (the
  larger Dock No.) maps to the lower POPA bound: enter stern in `station_lo`, bow
  in `station_hi` (`/reservations` returns both `station_lo/hi` POPA and
  `station_lo/hi_dock`; the map outline renders from POPA, the edit forms from
  Dock). Range/validation logic lives in pure helpers (`_station_range`,
  `_time_range`) with unit tests; the session functions don't commit (the
  endpoint does). Let a
  confirmed-overlap IntegrityError surface as a 409 — never pre-empt it by
  blocking `observed`/`tentative` overlaps or by skipping the constraint.
- DB-marked tests use the `db_session` fixture, which now nests the session in a
  SAVEPOINT (`join_transaction_mode="create_savepoint"`) so endpoint
  commits/rollbacks under `TestClient` stay inside the rolled-back transaction.
  Test write endpoints through `TestClient`, not by committing real rows.
