# POPA Wharf Data Layer — Roadmap & Build Plan

This document outlines what's built, what's next, and the longer-term shape of
the system. It is the planning companion to [`CLAUDE.md`](./CLAUDE.md) (design
contract) and [`README.md`](./README.md) (setup/run). When the two disagree,
`CLAUDE.md` wins — this file is intent, not authority.

---

## 1. Where we are today

**Steps 1–6 are complete, and step 7's AIS verification layer now exists.** The
repo is a conflict-safe data layer seeded from live AIS, with a
**conflict-detection service** (the time × station overlap primitive surfaced as
a query/API + a thin map surface), an **AIS verification service**
(`GET /verification` — operator placements checked against observed AIS:
arrived / no-show / awaiting / where-planned / unplanned; read-only), a read-only
Leaflet UI, and — pulled forward ahead of the build order, at the user's request —
**berth-request intake _capture_**. It is also now **deployable**: a production
Docker image + `docker-compose.prod.yml` (db + one-shot migrate/seed + api + ais
+ occupancy) behind whole-app HTTP Basic auth (see §5.4).

| # | Step | Status | Lands in |
|---|------|--------|----------|
| 1 | Schema + Alembic + exclusion constraint | ✅ | `alembic/versions/0001_initial_schema.py`, `app/models.py` |
| 2 | Stationing crosswalk (POPA ↔ Corps ↔ Dock No.) | ✅ | `app/crosswalk.py`, `tests/test_crosswalk.py` |
| 3 | Measured wharf centerline (real, derived from ArcGIS berths) | ✅ | `data/gis/build_centerline.py` → `app/seed/wharf_seed.py` |
| 4 | AIS ingestion (aisstream.io → DB) | ✅ | `app/ais/*` |
| 5 | Occupancy derivation | ✅ | `app/occupancy/*`, migration `0002`, `tests/test_occupancy_*` |
| 6 | Conflict-detection service | ✅ | `app/conflicts.py`, `GET /conflicts` in `app/main.py`, `tests/test_conflicts*.py`, conflicts panel in `app/static/index.html` |
| 7 | Request intake + AIS verification; legacy backfill | 🟡 capture + AIS verification built; auto status-mutation + legacy backfill TODO | `app/intake/*`, `app/verification.py`, `GET /verification`, `tests/test_verification*.py`, migrations `0003`/`0004` |

**Also built ahead of the plan** (deviations from "no UI in phase 1" / "intake
is a later layer", both user-requested):
- **Read-only Leaflet frontend** — `app/static/index.html`, served from
  `app/main.py`; real port geometry as static GeoJSON in `app/static/gis/`.
- **Berth occupancy timeline** — a Gantt drawer under the map (discrete berth
  lanes × time, bars colored by status, sub-row packing surfaces contention);
  reads `GET /reservations?from=&to=`. A data-viewing view, not the conflict
  service — see step 6.
- **Berth-request intake capture (step 7, partial):**
  - **manual phone/email/operator entry is now the sole channel** — the automated
    online-form feed (Adobe Sign → SharePoint CSV/Graph) was retired;
  - `POST /intake/berth-request` +
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
  copies the berth's POPA range onto `station_range`. Placement is the operator's
  job by design — AIS can't position a not-yet-arrived ship — so there is **no**
  automated placer. The not-yet-built layer is the **AIS verification** layer
  (§4), which checks these operator placements against reality after arrival.
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
- **Auth is all-or-nothing HTTP Basic** — one shared operator credential gates
  the whole app, with no per-user accounts, roles, or audit of *who* edited. Fine
  for a small operator team; revisit (OIDC/SSO against the portpa.com tenant) if
  that's needed. TLS is **not** terminated by the stack — a reverse proxy / LB
  must do it in front of `api` (Basic only base64-encodes credentials).

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

## 3. Step 6 — Conflict-detection service ✅ (done)

**Goal met:** the one primitive — *time ranges overlap AND station ranges
overlap* — is surfaced as a query/service, covering vessel-vs-vessel,
vessel-vs-dredge, and observed-vs-planned alike. Built in `app/conflicts.py`
(pure overlap helpers + `find_conflicts`), exposed as `GET /conflicts`, tested in
`tests/test_conflicts.py` (pure) + `tests/test_conflicts_db.py` (endpoint/DB), and
given a thin map surface in `app/static/index.html`.

### 3.1 Core query ✅
- A conflict between reservations `a` and `b` is
  `a.time_range && b.time_range AND a.station_range && b.station_range`, run as a
  self-join in `find_conflicts` using Postgres' own range operators (`&&` to
  detect, `*` to compute the overlap rectangle) — the source of truth. Uses the
  **raw** ranges, not the ±37.5 ft mooring buffer (that's a write-time margin).
- Pure predicates (`time_overlaps` half-open `[)`, `station_overlaps` closed
  `[]`, `reservations_conflict`, `overlap_interval`, `classify`) mirror the stored
  inclusivity exactly and are unit-tested without a DB.
- `status` filter (at least one side matches) gives the three lenses;
  `classify` labels each pair `observed-vs-planned` / `dredge-vs-vessel` /
  `planned-vs-planned`. Empty (unassigned) station ranges never match `&&`, so
  `requested` rows drop out for free — the intended "no false conflict".

### 3.2 Service / API surface ✅
- `GET /conflicts?from=..&to=..&status=..&limit=..` → list of conflict pairs,
  each with the overlapping time + station sub-rectangle (POPA **and** Dock No.)
  so the UI highlights the exact stretch. Mirrors `/reservations` for param
  parsing, vessel-name fallback, and Dock conversion.
- **UI:** a "Conflicts" stat tile + an Overview-tab panel listing each pair
  (names, category, overlap window + Dock range); clicking a card draws the
  contested station sub-range in red on the chart (reusing the vessel-outline
  `stationToLatLon` projection). Refreshed on the 15 s poll and after
  schedule/status writes.

### 3.3 Draft vs controlling depth — DEFERRED (carried to §5.6 / a later step)
- `CLAUDE.md` requires: **draft must be validated against controlling depth for
  the station/time window before a reservation can be confirmed.** Not built this
  step — it needs a **controlling-depth source** (a `controlling_depth` table
  keyed by station range + effective time window; depths change with dredging and
  shoaling) seeded from a hydrographic / Corps condition survey, which we don't
  have. Confirm path keeps today's warned-not-enforced behavior
  (`app/edit.confirm_warnings`). When the data lands: add the check in the confirm
  path and a `GET /conflicts` "depth" category
  (`vessel.draft > controlling_depth(station_range, time_range)` → block/flag).

### 3.4 Tests ✅
- Overlap matrix (time-only / station-only / both / neither → correct
  classification), the half-open `[a,b)` time-adjacency edge case (touching ⇒ no
  overlap), the closed-station shared-endpoint case, open-ended time, empty
  station never conflicts, the intersection rectangle, and `classify` — pure in
  `test_conflicts.py`; the same matrix through `GET /conflicts` against real
  PostGIS in `test_conflicts_db.py` (incl. dredge-vs-vessel using the identical
  path). Depth tests wait on §3.3's data source.

---

## 4. Step 7 — Intake + AIS verification (🟡 capture built early)

Pulled forward at the user's request. **Capture** and the **AIS verification
layer** now both exist. (This was originally framed as request→AIS
*reconciliation* that would auto-*place* requests; reframed this session to a
*verification* layer — see the "Built — AIS verification layer" block below for
why AIS can't place. Only auto status-mutation off the findings remains.)

**Built:**
- **Raw landing** — all channels land verbatim in `intake_event` *before*
  normalization, deduped by a content-hash `dedupe_key` (migration `0003`) so
  re-imports/re-submits are idempotent.
- **Online-form path (retired)** — the Adobe Sign → SharePoint CSV/Graph feed and
  its parser (`records.py`/`source.py`/`ingest.py`/`run.py`) were removed; intake
  is now manual-only. Any `intake_event` rows it left (`source='form'`) stay valid
  history and remain immutable.
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
  operator-driven placement is the system's *only* placement path (AIS can't
  place — see "Still TODO"); the after-arrival AIS **verification** layer (below)
  is still TODO.
- **In-place berth-request edit + delete** — Edit / Delete buttons on each
  manual-channel request card. `PATCH /intake/berth-requests/{id}`
  (`update_manual_request`) overwrites that `intake_event.raw` row and re-projects
  its reservation (leaving berth/status/direction alone) — the one sanctioned
  mutation of a raw intake row; `DELETE /intake/berth-requests/{id}`
  (`delete_manual_request`) drops the raw row and its projected reservation. Both
  are manual channels only (any legacy online-form row stays immutable). The
  form's Draft (ft) field is now mandatory.

**Built — AIS verification layer** (reframed; *not* an auto-placer). AIS only
knows where a vessel **is now**, never where a not-yet-arrived ship **will**
berth, and observed station ranges are approximate (not survey-grade), so it
cannot place a reservation — **placement stays the operator's job** (and a future
optimizer's). What it *does*, in `app/verification.py` + `GET /verification`
(`tests/test_verification*.py`, panel in `app/static/index.html`), is verify
operator placements against reality, matching on `vessel_id` + **time** overlap
(a `requested` row's empty station range can't match step 6's station-`&&` join,
so this is identity + time, not the conflict query). For each planned row
(vessel + non-empty window):
- **arrived** — an `observed` AIS berthing for that vessel overlaps the window;
- **no_show** — the window fully elapsed unseen;
- **awaiting** — current/future, nothing seen yet;
- plus a **where_planned** flag (observed range overlaps the planned one — the
  inline form of step 6's `observed-vs-planned`), and an **unplanned** list of
  observed berthings no request covered.
It is **read-only** — it surfaces findings, never mutates status (there's also no
`arrived` status to advance into).

**Still TODO:**
- **Auto status-mutation from verification** — e.g. auto-`completed` on observed
  departure, or flagging no-shows for bulk cancel. Deliberately deferred:
  surfacing for operator action came first (AIS is approximate; don't auto-write
  off it without review).
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
- ~~A `docker-compose` profile that runs API + ingestion + occupancy worker
  together for a realistic local stack.~~ **Done** — `scripts/dev.sh` (macOS/Linux)
  and `scripts/dev.ps1` (Windows) bring up the full stack in one command.
- ~~**Production deployment packaging.**~~ **Done** — a production `Dockerfile`
  (one slim, non-root image runs all four roles; gunicorn + uvicorn workers for
  the API) and `docker-compose.prod.yml`: `db` → a one-shot `migrate` (`alembic
  upgrade head` + seed, run separately so it happens once regardless of API
  worker count) → `api` / `ais` / `occupancy`, wired with health + completion
  gates and `restart: unless-stopped`. **Whole-app HTTP Basic** auth (`app/auth.py`,
  active iff `OPERATOR_USER`+`OPERATOR_PASSWORD` set; `/health` exempt). Secrets
  via gitignored `.env` (`.env.example` template). Smoke-tested end to
  end: migrate ran 0001→0008 + seeded, api healthy, auth 401/200 correct, AIS
  ingested. The host + network playbook is **[`DEPLOY.md`](./DEPLOY.md)**:
  run on a 24/7 Docker host reachable only over a network IT sanctions
  (corporate VPN/LAN, or Azure behind Entra ID SSO — reusing the `portpa.com`
  tenant), fronted by a reverse proxy that terminates TLS. So the api port binds
  to **`127.0.0.1` only** (the proxy is the sole front door; flip to `0.0.0.0` +
  your own TLS for direct exposure). Remaining: Docker `secrets:` over the env
  file; backups of the DB volume.
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

- **Scheduling optimizer / auto-assignment** (OR-Tools) — much later. This, not
  AIS, is what would ever *place* ships automatically (from requests + berth
  availability).
- **Auto status-mutation from AIS verification** — the verification *layer* is
  built (§4: arrival / no-show / awaiting / where-planned / unplanned, read-only),
  but auto-*writing* status off those findings (e.g. auto-`completed` on departure)
  is intentionally not. AIS is approximate; surface for operator action first.
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
2. ~~**Step 6**: conflict query/service + API, with the overlap test matrix.~~
   **Done** — `app/conflicts.py`, `GET /conflicts`, `tests/test_conflicts*.py`,
   and a conflicts panel on the map.
3. ~~**AIS verification layer (step 7 sequel)**: match planned rows to `observed`
   AIS on `vessel_id` + time overlap; report arrival / no-show / awaiting /
   where-planned + unplanned occupancy.~~ **Done** — `app/verification.py`,
   `GET /verification`, `tests/test_verification*.py`, verification panel on the
   map. Read-only (no auto status-mutation yet — §4 "Still TODO").
4. **Controlling-depth** table + draft validation in the confirm path (the
   deferred half of step 6, §3.3).
5. **CI** with a PostGIS service container; AIS reconnect/metrics hardening.
   (Production deployment packaging is **done** — see §5.4; the prod image makes a
   CI build/integration job straightforward.)
6. *(Optional)* wire the legacy-spreadsheet backfill review→commit pipeline.
