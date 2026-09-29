# Port of Port Arthur — Berth & Dredging Data Layer

The single source of truth for berth + dredging scheduling at the Port of Port
Arthur, replacing hand-maintained spreadsheets and three uncoordinated intake
channels (online form, dock-operator entry, phone).

**This file is the design contract — the rules that override default behavior.**
For how each part works today, see [`docs/reference/`](docs/reference/); for the
roadmap and open issues, see [`docs/PLAN.md`](docs/PLAN.md). When a reference doc
disagrees with this file, this file wins.

## Core model — read this first, it drives everything

The wharf is referenced **linearly**, not by discrete berths. Vessels and
dredging jobs are positioned by *station* along the wharf face.

- Canonical position = **POPA stationing**, in feet, stored on a PostGIS measured
  (`M`-valued) linestring (the wharf centerline). "Berths" are just named station
  ranges — convenience labels, not the allocation unit.
- External stationing systems reconcile to POPA as **affine functions** (stored as
  per-segment `scale`/`offset` params — never hard-code a transform inline):
  - **Corps/USACE** = POPA + 12,040.65 ft (constant offset, same direction)
  - **Dock No.** ≈ 3365 − POPA (reversed)
- **Every reservation is a rectangle in (time) × (station) space.** A vessel
  occupies `[stern_sta, bow_sta]` over `[ETB, ETD]`; a dredge op occupies a
  station interval over a time window. **A conflict — vessel-vs-vessel OR
  vessel-vs-dredge — is: time ranges overlap AND station ranges overlap.** One
  primitive covers both problems.

## Data source — bootstrap from AIS, not from requests

Seed from **observed vessel data via AIS**, so the system has real occupancy from
day one with no manual intake. Do not start from requests or legacy spreadsheets.

- Source: **aisstream.io** websocket (`wss://stream.aisstream.io/v0/stream`).
  Subscribe with a `BoundingBox` around the wharf; send the subscription within 3s
  of connecting or it drops. Consume `PositionReport` + `ShipStaticData`.
- Keep ingestion **source-agnostic** (USCG/NOAA Marine Cadastre is a later
  historical backfill through the same pipeline).
- **Derive occupancy, don't ask for it.** A vessel near the quay at SOG ≈ 0 for a
  sustained window → a derived `reservation` (type `vessel`, status `observed`):
  project bow/stern onto the centerline for the station range, heading vs channel
  axis for direction, berthed→unberthed for the time range.
- **Geo → station for free:** `ST_LineLocatePoint` / `ST_InterpolatePoint` on an
  AIS lat/lon against the measured centerline → interpolated `M` → POPA station.

## Stack (decided)

- Postgres + PostGIS; Python, FastAPI, SQLAlchemy + GeoAlchemy2, Alembic; pytest.
- A **Leaflet UI** (`app/static/`, served by `app/main.py`) exists with read views
  plus write surfaces (berth-request form + manual edit surface). Keep UI thin and
  over the API — the data layer is the product.
- **Deployment:** one production Docker image (all roles) + `docker-compose.prod.yml`,
  distinct from the dev `docker-compose.yml` + `scripts/dev.*`, behind whole-app
  HTTP Basic (`app/auth.py`).

## Schema

Tables (see [`docs/reference/migrations.md`](docs/reference/migrations.md) for the
per-migration detail and [`docs/reference/core.md`](docs/reference/core.md) for the
ORM):

- `wharf_segment` — measured geometry (`M` = POPA station), per-system affine
  params, nullable `apron` polygon (the occupancy "alongside" test).
- `vessel` — IMO/MMSI canonical key (names are non-unique/misspelled), name, LOA,
  beam, draft, `dims_locked` (operator lock pinning AIS dims when AIS is wrong).
- `berth` — named POPA range (`popa_sta_start/end`). Assigning one to a reservation
  copies its range onto `station_range`; conflict detection runs on the range,
  **never on `berth_id`**.
- `reservation` — `vessel_id` (nullable for dredging), nullable `berth_id`
  (`ON DELETE SET NULL`), `type` (`vessel|dredge|layberth`), `station_range
  numrange`, `time_range tstzrange`, `direction` (`upstream|downstream`), `status`
  (`observed|requested|tentative|confirmed|cancelled|completed`), `source`
  (`ais|form|phone|operator|email|ai`), priority, cargo, notes, created_at.
- `intake_event` — raw inbound request as received, before normalization (audit +
  reconciliation trail). **Soft-deleted, not erased** (`deleted_at`); the dedupe
  unique index is partial on `dedupe_key IS NOT NULL AND deleted_at IS NULL`.
- `audit_log` — append-only who/what (actor, action, entity, entity_id, JSONB
  `detail`, at). One row per mutating endpoint, in the write's transaction.
- `position_report` — landed raw AIS positions (source-agnostic).
- `worker_heartbeat` — per-worker liveness, **upserted** each cycle (stays tiny).
  Liveness is a heartbeat, never inferred from data freshness.
- `depth_survey` / `depth_segment` — versioned controlling-depth profile (the gate
  reads the latest `active`; raw soundings not kept). `depth_cell` is a 2-D
  station×offset grid for the map cross-section — **visualization only**; the gate
  reads `depth_segment`, and a station's controlling depth is the min over its
  cells so the two never disagree.

Enforce no-overlap at the DB, not in app code:

```sql
CREATE EXTENSION IF NOT EXISTS btree_gist;
ALTER TABLE reservation ADD CONSTRAINT no_wharf_overlap
  EXCLUDE USING gist (time_range WITH &&, station_range WITH &&)
  WHERE (status = 'confirmed');
```

The constraint is `confirmed`-only **on purpose**: `observed` AIS rows are ground
truth and must be allowed to overlap planned reservations — that overlap is the
signal to surface, not block. Draft must be validated against controlling depth
before a reservation can be confirmed.

## Conventions

- Migrations only via Alembic; never edit the DB by hand.
- All position I/O goes through the one crosswalk module (`app/crosswalk.py`) — no
  scattered stationing math, server-side only (the UI never converts).
- **Central Time is the canonical wall-clock.** The whole system runs on
  `America/Chicago` (DB session pinned in `app/db.py`, DB default in migration
  0008). `timestamptz` still stores absolute instants; Central is how they're
  read/written. Zone-less operator input is stamped Central via `app/tz.py`
  (`assume_central`) — never hard-code a `-6`/`-5` offset. AIS `time_utc` is
  genuinely UTC and stays so.
- Normalize on ingest (units, enums, IMO/MMSI); keep the raw input in
  `intake_event` / `position_report.raw`.
- Backfill is parse → human review → commit; legacy spreadsheets are messy
  (merged cells, `"Chem Orchard - 607'"`, ambiguous `"X or Y"`, inline
  `CANCELLED`/`TBA`/`?`). Never assume clean auto-parse.
- Tests required for the crosswalk module and the conflict-detection logic.

## Build order & status

Steps 1–6 complete; step 7 mostly complete. **The only unbuilt piece is the
legacy-spreadsheet backfill *commit*** (its parser exists; the review pipeline
does not). Full status in [`docs/PLAN.md`](docs/PLAN.md).

1. ✅ Schema + Alembic + exclusion constraint
2. ✅ Stationing crosswalk (POPA ↔ Corps ↔ Dock No.) + tests
3. ✅ Real measured wharf centerline
4. ✅ AIS ingestion (aisstream.io → DB)
5. ✅ Occupancy derivation (observed reservations)
6. ✅ Conflict-detection service + draft-vs-controlling-depth gate
7. 🟡 Request intake (capture + AI-assisted pull + manual edit surface) and AIS
   verification (read-only check + auto status-mutation) built; **legacy backfill
   commit is TODO**

Pulled forward at the user's request and now built: the UI, intake capture, the
manual edit surface, and the read-only feasibility oracle (`app/feasibility.py`,
`GET /feasibility`) — the scheduler's feasible-position primitive, which
**proposes, never places**.

## Working agreements for future changes

Each rule is a *must*. The reference docs explain the mechanisms; this is the
contract.

- **Stationing:** a new external system = new affine params on `wharf_segment`,
  never inline math. Extend `crosswalk.py` and its tests together.
- **AIS providers:** a new source = a new `AISSource` yielding the same normalized
  `AISPosition`/`AISStatic`; the `Ingestor` must not change.
- **Schema:** every change goes through a new Alembic revision + a matching
  `app/models.py` update. Keep the enum tuples and the migration in lockstep.
- **The exclusion constraint stays `confirmed`-only.** If you think you need to
  block `observed` overlaps, re-read the Core model. The **75 ft minimum mooring
  gap is baked into the constraint** (migration 0007's `GAP_FT`, padding each range
  by half the gap before `&&`) — the single source of truth, with **no `app.config`
  mirror** (that trap was removed). Change the gap via a new migration only. The
  one Python copy, `conflicts.MOORING_GAP_FT`, is an explicitly-advisory mirror the
  feasibility oracle pre-checks against; keep it matching migration 0007 — don't
  "dedupe" it into config.
- **Live panels are alert surfaces, filtered at the QUERY layer, never at
  derivation.** `observed` rows stay in the one reservation table (History keeps
  everything); `GET /conflicts` and `GET /verification`'s unplanned list default to
  `current=true` + `service_craft=false` (the tug/towing/pilot set is
  `app/shiptypes.py` — mirror the UI's `shipTypeCategory`; dredgers excluded; NULL
  ship_type always shown). **Observed-vs-observed pairs are never conflicts** (AIS
  can't conflict with itself). **One "still here" recency gate shared by every live
  surface** (map dots, `moored` stat, "Alongside now"): keep a fix only when
  `msg_ts >= max(msg_ts) − berth_stale_close_min`, against the **feed clock**, the
  same threshold as stale-berthing close. A dead feed is not departure evidence
  (same principle as the sweep's `feed_alive`). Don't give a live surface its own
  staleness rule; don't suppress panel noise at ingest/derivation.
  `vessel_present_window_h` (24h) is a different, broader metric — don't use it to
  gate the live surfaces.
- **Auth is whole-app HTTP Basic middleware** (`app/auth.py`), not per-route deps —
  it's the only thing covering the mounted static map. **Active only when both
  `OPERATOR_USER` and `OPERATOR_PASSWORD` are set** (blank ⇒ open, so dev/tests run
  uncredentialed). `/health` is the lone auth-exempt path; gate any new endpoint by
  default. Basic only base64-encodes, so it **must** sit behind upstream TLS — the
  api port binds `127.0.0.1` only in the prod compose; don't widen to `0.0.0.0`
  without TLS in front. The middleware sets `request.state.operator` for
  `audit_log` (the one place to derive per-user identity if more credentials come).
- **Every mutating endpoint writes one `audit_log` row** (`app/audit.py`, via the
  `do_write` `audit=` hook) in the **same transaction** as the write. The log is
  append-only and constraint-free so it never blocks the write; the hook returns
  `None` to skip a no-op (deduped re-submit, 404, empty sweep).
- **Intake:** a new channel = a thin call into `app/intake/`, **landing raw in
  `intake_event` (deduped by content-hash `dedupe_key`) before any normalization —
  never skip the raw landing.** A `requested` reservation from intake carries an
  **empty `station_range`** until reconciliation assigns a berth; an empty range
  never conflicts — intentional, don't "fix" with a placeholder span. A
  content-empty submission is refused (lands no row). **One sanctioned exception to
  "never mutate raw":** the manual berth-request edit
  (`PATCH /intake/berth-requests/{id}`) overwrites the `intake_event.raw` row in
  place and re-projects its reservation — manual channels only
  (`phone|email|operator`), online-form rows stay immutable. Delete **soft-deletes**
  the raw row (kept for audit) and drops the projected reservation.
- **AIS dimensions are AIS-authoritative** for an AIS-tracked vessel (has MMSI), on
  **both** the intake and edit paths: a typed loa/beam/draft is not applied (it's
  dropped/NULL-filled with a warning naming entered-vs-AIS), because the ingestor
  overwrites by MMSI on every `ShipStaticData`. **Escape hatch:** the
  `dims_locked` lock — an operator pins a corrected value; the edit then applies it
  AND the ingestor stops reverting it (loa/beam/draft/dim_a/dim_b freeze together so
  `loa = dim_a+dim_b` can't drift). Manual-only vessels (no MMSI) own their dims.
  **One IMO = one ship:** a new request whose IMO is on file under a *different*
  name is refused (422, pre-landing) — don't silently merge/rename.
- **AI-assisted intake** (`app/intake/llm.py` + `dataverse_run.py`) is a
  *normalizer, not a placer* — it flows through the same `record_manual_request`
  path (lands raw, dedupes, empty-range `requested` row) and **never places or sets
  status**. Keep the parse **tolerant** (bad IMO / unparseable date → null + note,
  never a hard failure; the network call is isolated behind a `complete` callable
  for testing). The worker pulls **outbound** from Dataverse (Azure AD
  client-creds) — do **not** replace it with an inbound webhook (that reopens the
  127.0.0.1-bind + DLP problem the pull avoids). AI cards are `source='ai'` (its own
  provenance); editability is the separate `EDITABLE_SOURCES` axis, which includes
  `ai`.
- **The manual edit surface** (`app/edit.py`) is otherwise **authoritative** (a
  vessel edit overwrites the fields it sets; the AIS-dims exception above still
  applies). Vessel dims are edited in **metres** (canonical store). **Station
  ranges are entered in Dock No. feet** (what an operator reads off the quay) and
  converted to POPA on store via the segment's affine params — stern (larger Dock
  No.) goes in `station_lo`, bow in `station_hi`. Range/time logic lives in pure,
  unit-tested helpers; session functions don't commit (the endpoint does). Let a
  confirmed-overlap `IntegrityError` **surface as a 409** — never pre-empt it by
  blocking `observed`/`tentative` overlaps or skipping the constraint.
- **Placement is the operator's job** (and a future optimizer's, from requests +
  availability — **not** from AIS). AIS verification and the feasibility oracle and
  the AI channel all *propose*; they never place. AIS only knows where a vessel is
  now/was and its ranges are approximate, so it cannot position a not-yet-arrived
  ship.
- **Tests:** DB-marked tests use the `db_session` fixture (SAVEPOINT-nested so
  endpoint commits/rollbacks under `TestClient` stay inside the rolled-back
  transaction). Test write endpoints through `TestClient`, not by committing rows.

## Repo layout (terse)

Detail per section in [`docs/reference/`](docs/reference/).

```
app/
  config·db·models·crosswalk·tz·main·auth·audit·workers.py   # core → reference/core.md, crosswalk.md
  edit·conflicts·feasibility·verification·shiptypes.py        # scheduling → reference/scheduling.md
  routers/   # HTTP surface (common·read_only·intake·edit·analysis·depth) → reference/routers.md
  ais/       # AIS ingestion → reference/ais.md
  occupancy/ # occupancy derivation → reference/occupancy.md
  intake/    # manual + llm + dataverse_run → reference/intake.md
  depth/     # controlling-depth + draft gate → reference/depth.md
  seed/·static/                                               # wharf seed; Leaflet UI → reference/frontend.md
data/gis/·data/surveys/   # geometry build + raw surveys → reference/crosswalk.md, depth.md
alembic/                  # migrations 0001–0015 → reference/migrations.md
tests/                    # → reference/tests.md
scripts/·Dockerfile·docker-compose*.yml·.env.example·.github/ # → reference/deployment.md
docs/                     # PLAN.md (roadmap) · DEPLOY.md · reference/ (per-section docs)
```
