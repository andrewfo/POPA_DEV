# POPA Wharf Data Layer — Roadmap & Build Plan

This document outlines what's built, what's next, and the longer-term shape of
the system. It is the planning companion to [`CLAUDE.md`](./CLAUDE.md) (design
contract) and [`README.md`](./README.md) (setup/run). When the two disagree,
`CLAUDE.md` wins — this file is intent, not authority.

---

## 1. Where we are today

**Steps 1–5 are complete** (1–4 pushed; step 5 + the items below are in the
working tree, not yet committed). The repo is a conflict-safe data layer seeded
from live AIS, with a read-only Leaflet UI and — pulled forward ahead of the
build order, at the user's request — **berth-request intake _capture_**.

| # | Step | Status | Lands in |
|---|------|--------|----------|
| 1 | Schema + Alembic + exclusion constraint | ✅ | `alembic/versions/0001_initial_schema.py`, `app/models.py` |
| 2 | Stationing crosswalk (POPA ↔ Corps ↔ Dock No.) | ✅ | `app/crosswalk.py`, `tests/test_crosswalk.py` |
| 3 | Measured wharf centerline (real, derived from ArcGIS berths) | ✅ | `data/gis/build_centerline.py` → `app/seed/wharf_seed.py` |
| 4 | AIS ingestion (aisstream.io → DB) | ✅ | `app/ais/*` |
| 5 | Occupancy derivation | ✅ | `app/occupancy/*`, migration `0002`, `tests/test_occupancy_*` |
| 6 | Conflict-detection service | ⬜ (next) | — |
| 7 | Request intake + reconciliation; legacy backfill | 🟡 capture built; reconciliation TODO | `app/intake/*`, migrations `0003`/`0004`, `tests/test_intake_*` |

**Also built ahead of the plan** (deviations from "no UI in phase 1" / "intake
is a later layer", both user-requested):
- **Read-only Leaflet frontend** — `app/static/index.html`, served from
  `app/main.py`; real port geometry as static GeoJSON in `app/static/gis/`.
- **Berth occupancy timeline** — a Gantt drawer under the map (discrete berth
  lanes × time, bars colored by status, sub-row packing surfaces contention);
  reads `GET /reservations?from=&to=`. A data-viewing view, not the conflict
  service — see step 6.
- **Berth-request intake capture (step 7, partial):**
  - online-form path — SharePoint/Adobe-Sign CSV export → `intake_event`
    (`app/intake/records.py` parser, `ingest.py`, `source.py`, `run.py`);
  - **manual phone/email/operator entry** — `POST /intake/berth-request` +
    a form on the map page (`app/intake/manual.py`). Lands raw in
    `intake_event` *and* creates a `status='requested'` reservation with an
    **empty (unassigned) `station_range`** — the port assigns the berth later.
  - What's still TODO for step 7: **reconciliation against observed AIS**, and
    committing the legacy-spreadsheet backfill (parse exists; review→commit not).

### Known gaps carried forward (do these regardless of feature work)

- **Centerline geometry is REAL** (this was previously mis-flagged as
  placeholder). `data/gis/build_centerline.py` derives it from the ArcGIS berth
  polygons (anchor Berth 4 = 351 ft) → `centerline_vertices.json`, which
  `wharf_seed.py` seeds; `tests/test_geo_station_real.py` verifies it end-to-end
  (pure, no DB). Residual caveat: it's a coarse 7-vertex line (one chord per
  berth — the max fidelity rectangular berth polygons allow); a finer quay survey
  would densify via the same script. The placeholder line remains only as a
  fallback when the derived file is absent.
- **Apron polygon built; DB path unexercised.** The "alongside" predicate now
  prefers a digitized **water-side apron polygon** (`wharf_segment.apron`,
  migration `0005`; built by `build_centerline.py` → `apron.geojson` /
  `apron_polygon.json`; `app/occupancy/alongside.py` uses `ST_Contains(apron)`
  with the `ST_DWithin` buffer as fallback). The polygon + map render are
  verified; the **seed + `ST_Contains` predicate are unexercised until PostGIS is
  up** (backward-compatible: NULL apron → old buffer behavior). Optional refinement:
  tune `APRON_WATER_FT`/`APRON_INLAND_FT` or swap in a surveyed apron.
- **Requested reservations start with no berth, but can now be assigned one.**
  Manual/online intake still creates `requested` rows with an **empty
  `station_range`** (never conflicts). A named **`berth` catalog** now exists
  (migration `0006`, seeded from `data/gis` `berth_stations.json`); an operator
  assigns a berth via `PATCH /reservations/{id}` `{berth_id}` (or `POST`), which
  copies the berth's POPA range onto `station_range`. That's the *manual* path;
  **automated** request→AIS reconciliation is still the not-yet-built layer.
  UI gap: the sidebar form still takes raw station feet — no berth `<select>`
  over `GET /berths` yet.
- **Intake detail lives only in `intake_event.raw`.** Manual-entry fields with
  no normalized column (flag, S/S line, deadweight, bunkers, cargo weights,
  agency) are kept raw + summarized into `reservation.notes`. Promote to columns
  if/when they're queried.
- **No automated reconnect/health proof for the AIS client.** `app/ais/run.py`
  is long-running but we have no soak test or metrics.
- **DB tests are opt-in** and auto-skip without a database; CI needs a real
  PostGIS service to exercise them. (Note: on Windows, `engine.connect()` to a
  dead host can hang — set `?connect_timeout=N` to fail fast.)

---

## 2. Step 5 — Occupancy derivation ✅ (done)

Built as designed — see `app/occupancy/{detect,project,alongside,derive,run}.py`,
migration `0002` (`vessel.dim_a/dim_b`, `reservation.derived_key`), and
`tests/test_occupancy_*`. Berthing detection uses a two-threshold hysteresis on
SOG + an "alongside" buffer; bow/stern are projected from the AIS antenna +
A/B dims + heading (COG fallback) and run through `geo_to_station`; writes are
idempotent via `derived_key`, always `observed`/`ais`, and may overlap planned
rows by design. The "alongside" test now prefers the digitized apron polygon
(buffer fallback). Remaining caveats live in §1's "known gaps" (coarse 7-vertex
centerline; apron DB-path unexercised until PostGIS is up).

---

## 3. Step 6 — Conflict-detection service (next up)

**Goal:** surface conflicts as a query/service. One primitive: *time ranges
overlap AND station ranges overlap*. Covers vessel-vs-vessel, vessel-vs-dredge,
and observed-vs-planned alike.

### 3.1 Core query
- A conflict between reservations `a` and `b` is
  `a.time_range && b.time_range AND a.station_range && b.station_range`.
- Implement once as a SQL function or SQLAlchemy query in `app/conflicts.py`.
  Parameterize by status filter so callers choose what counts:
  - **observed-vs-planned** (the headline signal: a vessel sitting where
    something is planned) — `observed` × (`tentative|confirmed`);
  - **planned-vs-planned** — sanity check even though the DB blocks confirmed
    overlaps;
  - **dredge-vs-vessel** — same primitive, no special-casing.

### 3.2 Service / API surface
- `GET /conflicts?from=..&to=..&status=..` → list of conflict pairs with the
  overlapping time and station sub-intervals (compute the intersection so the UI
  can highlight the exact rectangle).
- Optionally `GET /reservations` with time/station window filters (foundation
  for the later Leaflet view).

### 3.3 Draft vs controlling depth
- `CLAUDE.md` requires: **draft must be validated against controlling depth for
  the station/time window before a reservation can be confirmed.**
- Needs a **controlling-depth source**: a `controlling_depth` table keyed by
  station range + effective time window (depths change with dredging and
  shoaling). Seed from the latest hydrographic survey / Corps condition survey.
- Add a check in the confirm path (and a `GET /conflicts` "depth" category):
  `vessel.draft > controlling_depth(station_range, time_range)` → block/flag.

### 3.4 Tests (required)
- Overlap matrix: pairs that touch only in time, only in station, in both,
  or in neither → correct classification.
- Half-open range edge cases (`[a,b)` adjacency must NOT count as overlap).
- Dredge-vs-vessel uses the identical path.
- Depth: under/over controlling depth at a station/time window.

---

## 4. Step 7 — Intake + reconciliation (🟡 capture built early)

Pulled forward at the user's request. **Capture** now exists; **reconciliation**
does not.

**Built:**
- **Raw landing** — all channels land verbatim in `intake_event` *before*
  normalization, deduped by a content-hash `dedupe_key` (migration `0003`) so
  re-imports/re-submits are idempotent.
- **Online-form path** — SharePoint/Adobe-Sign CSV export → `intake_event`
  (`app/intake/records.py` conservative parser + `ingest.py` + `source.py` +
  `python -m app.intake.run`). Messy values become `None` + a warning, never a
  guess; the raw row is always preserved.
- **Manual phone/email/operator entry** — `POST /intake/berth-request` and a
  form on the map page (`app/intake/manual.py`). Normalizes units (feet→metres),
  upserts `vessel` by IMO, lands `intake_event`, **and** creates a candidate
  `reservation` (status `requested`, source `phone|email|operator`) with an
  **empty `station_range`** (berth unassigned). `email` was added to the source
  enums in migration `0004`. `GET /reservations` lists them (with `berth_name`).
- **Berth catalog + manual assignment** (migration `0006`) — a named `berth`
  table (canonical POPA range per berth, seeded from `data/gis`
  `berth_stations.json`) and `reservation.berth_id`. An operator assigns a berth
  through the edit surface (`berth_id` on `POST`/`PATCH /reservations`), which
  fills `station_range` from the catalog; `GET /berths` lists the catalog. This
  is the manual half of reconciliation — the automated half (below) is still TODO.

**Still TODO:**
- **Reconcile against observed AIS** — match a request to the `observed`
  reservation(s) for the same vessel/window; assign the real `station_range`;
  surface agreement vs discrepancy (a request for a berth the vessel isn't at,
  or vice versa). This is the natural sequel to step 6's overlap primitive.
- **Legacy spreadsheet backfill commit** — the parser is conservative and
  tested, but the parse → **human review** → commit pipeline isn't wired. Source
  is messy (`"Chem Orchard - 607'"`, ambiguous `"X or Y"`, inline
  `CANCELLED`/`TBA`/`?`). Parse-then-review, not parse-then-trust.
- **Promote raw-only fields to columns** if queried (flag, DWT, agency, weights).

---

## 5. Cross-cutting / infrastructure backlog

Not tied to a single step — pick up as the system matures.

### 5.1 Real geometry (highest leverage)
- Digitize the wharf centerline and apron polygon from the port aerial/GIS.
- Verify the crosswalk affine params against the port's published stationing
  crosswalk at several known points (Corps offset 12,040.65; Dock No. 3365−POPA).
- Add a fixture comparing a few surveyed (lat/lon → station) points end-to-end.

### 5.2 CI / test infrastructure
- GitHub Actions: lint (ruff), type-check (mypy), `pytest` (pure) on every push;
  a job that spins up a **PostGIS service container**, runs `alembic upgrade
  head`, and runs the `db`-marked tests.
- Add a migration round-trip test (`upgrade` → `downgrade` → `upgrade`).

### 5.3 AIS ingestion hardening
- Reconnect with backoff + jitter; re-send the subscription within 3 s of every
  (re)connect (aisstream drops the socket otherwise).
- Metrics: messages/sec, last-message age, distinct MMSI, commit lag. Expose via
  a `/metrics` endpoint or structured logs.
- Dead-letter / raw-capture for unparseable frames (we already keep `raw`).
- A second `AISSource` for **Marine Cadastre** historical backfill — must yield
  the same normalized `AISPosition`/`AISStatic`; the `Ingestor` must not change.
- Position-report **retention/partitioning** policy: this table grows fast.
  Consider monthly partitions by `msg_ts` and a downsampling/archival job.

### 5.4 Observability & ops
- Structured logging config, request IDs, basic Prometheus-style counters.
- A `docker-compose` profile that runs API + ingestion + occupancy worker
  together for a realistic local stack.
- Healthcheck that reports last-AIS-message age (stale feed = silent failure).

### 5.5 Data quality
- Vessel identity merge: when IMO appears later for an MMSI-only row, link them;
  handle MMSI reuse / mismatched IMO.
- Sanity bounds on AIS values (lat/lon in bbox, SOG/heading ranges) before
  trusting them in derivation.

### 5.6 Schema evolution (always via Alembic)
- ✅ `berth` catalog + `reservation.berth_id` (migration `0006`) — named POPA
  station ranges; assigning a berth fills `station_range` (the canonical range
  stays the conflict primitive, never `berth_id`).
- `controlling_depth` table (§3.3).
- Wharf apron polygon / `wharf_area` (§2.1).
- `reservation.derived_key` + unique index for idempotent observed rows (§2.5).
- Keep `app/models.py` enum tuples and the migration in lockstep. The exclusion
  constraint stays **`confirmed`-only** — do not extend it to block `observed`.

---

## 6. Out of scope (still)

- **Scheduling optimizer / auto-assignment** (OR-Tools) — much later.
- **Request→AIS reconciliation engine** — intake *capture* exists (§4), but
  matching requests to observed occupancy and promoting `requested` → `tentative`
  → `confirmed` is not built.
- Anything that requires blocking `observed` overlaps. Re-read the Core model
  section of `CLAUDE.md` before reaching for that.

(No longer out of scope, built ahead of plan at the user's request: a read-only
Leaflet **UI** and berth-request intake **capture**. See §1.)

---

## 7. Suggested near-term sequence

1. ~~Digitize real centerline + apron polygon, add a geo→station fixture.~~
   **Done** — centerline + apron are real and verified (`build_centerline.py`,
   `tests/test_geo_station_real.py`). What's left here: bring PostGIS up and
   `alembic upgrade head` → `app.seed.wharf_seed` → run the db-marked tests to
   exercise the apron seed + `ST_Contains` predicate.
2. **Step 6**: conflict query/service + API, with the overlap test matrix.
3. **Reconciliation (step 7 sequel)**: match `requested` intake rows to
   `observed` AIS; assign their `station_range`; promote toward `confirmed`.
4. **Controlling-depth** table + draft validation in the confirm path.
5. **CI** with a PostGIS service container; AIS reconnect/metrics hardening.
6. *(Optional)* wire the legacy-spreadsheet backfill review→commit pipeline.
