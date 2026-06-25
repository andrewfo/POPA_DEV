# POPA Wharf Data Layer — Roadmap & Build Plan

This document outlines what's built, what's next, and the longer-term shape of
the system. It is the planning companion to [`CLAUDE.md`](./CLAUDE.md) (design
contract) and [`README.md`](./README.md) (setup/run). When the two disagree,
`CLAUDE.md` wins — this file is intent, not authority.

---

## 1. Where we are today

**Steps 1–6 are complete (including step 6's draft-vs-controlling-depth gate),
and step 7's AIS verification layer now exists.** The
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
| 6 | Conflict-detection service (+ draft-vs-depth gate) | ✅ | `app/conflicts.py`, `GET /conflicts` (`app/routers/analysis.py`), `tests/test_conflicts*.py`; **draft gate** `app/depth/*` + `app/edit._depth_gate` + `app/routers/depth.py` (migration `0012`), `tests/test_depth.py` |
| 7 | Request intake + AIS verification; legacy backfill | 🟡 capture + AI-assisted intake + AIS verification + auto status-mutation built; legacy backfill TODO | `app/intake/*` (incl. `llm.py` + `dataverse_run.py`), `app/verification.py`, `GET /verification`, `POST /verification/sweep`, `tests/test_verification*.py`/`test_intake_*`, migrations `0003`/`0004` |

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
  - **AI-assisted intake channel (2026-06):** an optional pull worker
    (`app/intake/dataverse_run.py`) polls the Power Pages / Dataverse "Berth
    Request" table outbound, normalizes each messy row with a cheap LLM
    (`app/intake/llm.py`, via **OpenRouter**, default Gemini Flash), and records
    it through the *same* `record_manual_request` pipeline (raw preserved,
    `requested`/empty-range row). The model **proposes**; placement/confirm stay
    the operator's. Tagged `source='ai'` (its own provenance value, migration
    `0009`); editability is the separate `EDITABLE_SOURCES` axis, which includes
    `ai`.
  - What's still TODO for step 7: committing the legacy-spreadsheet backfill
    (parse exists; review→commit not).

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
- **Intake detail lives only in `intake_event.raw`.** Fields with no normalized
  column (flag, S/S line, destinations, deadweight, bunkering detail — type /
  metric tons / acknowledgement, cargo weights, agency, requestor name/email/
  phone, signature) are kept raw + summarized into `reservation.notes`. Promote
  to columns if/when they're queried. The `BerthRequestForm` now mirrors the
  Power Pages **online form** (`docs/power-pages-berth-intake.md`) field-for-field
  so a submission normalizes cleanly — **in FEET / net tons** (the app converts
  feet→metres on ingest; the Dataverse form must not pre-convert), bunker fuel in
  metric tons, `bunker_type` from the shared `BUNKER_TYPES` list.
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

**Stale-open closure (2026-06):** a vessel that departs *while coverage is down*
(or leaves the receiver's range) just stops sampling, so its trailing berthing
used to read "ongoing" forever — the source of ghost ships in the live panels.
`detect_berthings` now takes `as_of`/`stale_after`: a trailing segment silent
longer than `berth_stale_close_min` (config, default 180 min) is emitted CLOSED
at its last fix. The clock is the **feed clock** (newest `position_report`
anywhere in the bbox), never the wall clock, so a dead feed closes nothing —
the same "no data is not departure evidence" principle as the sweep's
`feed_alive`. Self-healing because derivation is idempotent: a vessel that
reappears alongside re-extends and reopens the same `derived_key` event.

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
- **At least one side must be a planned row** (2026-06): an
  observed-vs-observed overlap is AIS noise (a rafted tug, projection slop, a
  stale derivation artifact), never a scheduling decision — excluded in the SQL
  unconditionally.
- **Live-alert defaults** (2026-06): `GET /conflicts` defaults to
  `current=true` (the overlap rectangle must reach the present/future; an
  explicit `from`/`to` window disables it) and `service_craft=false` (pairs
  where an *observed* side is a harbor tug/towboat/pilot boat —
  `app/shiptypes.py`, mirroring the UI's type buckets — are hidden; a *planned*
  row for a tug always shows). Filtering is query-layer only; the observed rows
  themselves are untouched.

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

### 3.3 Draft vs controlling depth — ✅ BUILT (2026-06)
- `CLAUDE.md` requires: **draft must be validated against controlling depth for
  the station/time window before a reservation can be confirmed.** Now enforced.
  The **controlling-depth source** is a versioned hydrographic survey
  (`depth_survey` + `depth_segment`, migration `0012`): an operator uploads a
  port condition-survey `.XYZ` (Texas South Central State Plane ftUS, EPSG:2278)
  via `POST /depth/surveys` (or `python -m app.depth.ingest`), which PostGIS
  reduces — project each sounding onto the centerline → POPA station, clip to the
  berthing zone, bin, keep the **shallowest** = controlling depth (`app/depth/`).
- Confirm path: `app/edit._depth_gate` (→ `app/depth/gate.controlling_depth_over`
  + `depth_shortfall`) checks `vessel.draft + DEPTH_CLEARANCE_FT` against the
  shallowest controlling depth over the reservation's station range, per the
  **latest active** survey. Too deep → **422 block**, unless `depth_override`
  downgrades it to a warning (logged in the audit detail). No covering survey /
  unknown draft / unassigned berth → warns rather than blocks (never invents
  depth). Tide is **not** modelled — clearance is a flat margin.
- UI: a "Depth surveys" panel (upload + list + active status,
  `app/static/js/depth.js`) and an "Override depth check" checkbox on the
  placement form. `GET /depth/surveys` / `GET /depth/profile` read it back.
- **Not** wired as a `GET /conflicts` category — the depth check is a confirm-time
  gate (block before write), not an after-the-fact overlap surface.

### 3.4 Tests ✅
- Overlap matrix (time-only / station-only / both / neither → correct
  classification), the half-open `[a,b)` time-adjacency edge case (touching ⇒ no
  overlap), the closed-station shared-endpoint case, open-ended time, empty
  station never conflicts, the intersection rectangle, and `classify` — pure in
  `test_conflicts.py`; the same matrix through `GET /conflicts` against real
  PostGIS in `test_conflicts_db.py` (incl. dredge-vs-vessel using the identical
  path). Depth gate is tested separately in `tests/test_depth.py` (see §3.3):
  pure (`.XYZ` parse, `depth_shortfall`) + db-marked (PostGIS reduction,
  `controlling_depth_over`, confirm 422 / override / clears / no-survey-warning).

---

## 4. Step 7 — Intake + AIS verification (🟡 capture built early)

Pulled forward at the user's request. **Capture** and the **AIS verification
layer** now both exist. (This was originally framed as request→AIS
*reconciliation* that would auto-*place* requests; reframed this session to a
*verification* layer — see the "Built — AIS verification layer" block below for
why AIS can't place. The auto status-mutation off those findings is now built
too; an AI-assisted intake channel was since added. Only the legacy-spreadsheet
backfill commit remains on step 7.)

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
`GET /verification` itself is **read-only**. The **unplanned list is live-scoped
by default** (2026-06): `current=true` keeps only *ongoing* berthings (a closed
visit means the vessel left — that's History; an explicit `from`/`to` disables
it) and `service_craft=false` hides harbor tugs/towboats/pilot boats
(`app/shiptypes.py`) — nobody files a berth request for a tug working a ship
move, so each pause was becoming a permanent "unplanned" card. The planned list
is operator rows and is never filtered this way; a UI toggle ("show harbor
craft") re-fetches both this panel and `/conflicts` with `service_craft=1`.

**Built — auto status-mutation** (the once-deferred half, now shipped):
`expire_stale` + `POST /verification/sweep` (and the occupancy worker runs it each
batch) auto-archive a planned row whose window has been fully past for longer than
`verification_grace_minutes` (default 720 = 12 h) to a **terminal** status — `completed`
if AIS observed the vessel berth, else `cancelled` (no-show) — with an audit line
appended to `notes`. Status-only; never places, and only ever moves a row
*outside* the confirmed-only exclusion constraint, so it can't raise a 409.

**Built — AI-assisted intake channel** (2026-06): an optional pull worker
(`app/intake/dataverse_run.py`) polls the Power Pages / Dataverse "Berth Request"
table outbound (Azure AD client-creds — no inbound exposure, no DLP ask), a cheap
LLM (`app/intake/llm.py`, OpenRouter / Gemini Flash) normalizes each messy row
into a `BerthRequestForm`, and it records through the same `record_manual_request`
pipeline (raw preserved via `source_raw`, confidence/caveats onto `notes`,
`requested`/empty-range row). **Proposes, never places.** Tests in
`tests/test_intake_llm.py` + `tests/test_intake_dataverse.py` (faked LLM/client).
The worker has a CLI to validate before going live — `--once`, `--dry-run`
(fetch+parse+print, no writes), and `--input FILE` (parse local sample rows;
OpenRouter only, no Dataverse/DB — sample in `docs/sample_berth_requests.json`).
Needs an Entra app-registration + Dataverse application user + an OpenRouter key
to run live; IT-side setup in `docs/dataverse-entra-setup.md`,
`docs/berth-intake-handoff.md`.

**Caveat — model reliability.** Live sampling showed the cheapest model
(`gemini-2.0-flash-lite`) occasionally returns prose-wrapped or truncated JSON and
silently drops an otherwise-clean row. `extraction_from_json` is hardened against
this (string-aware brace extraction + `max_tokens` + raw-text logging on failure),
which rescued the failing sample; still, for production prefer a non-lite model
(`--model google/gemini-2.0-flash-001`) and watch the worker logs for the
"did not return valid JSON" warning.

**Still TODO:**
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
- ~~**Per-worker liveness in the console.**~~ **Done (2026-06-22)** — the single
  "data layer" dot only proved the API/DB answered, saying nothing about the
  background workers (they run as separate processes/containers). Each worker now
  upserts a `worker_heartbeat` row every cycle (migration `0011`, `app/workers.py`
  `beat()`); `GET /workers` reads them back and derives each worker's health
  (`ok`/`stale`/`error`/`offline`) from how stale its last beat is against its
  nominal cadence (`classify`, unit-tested in `tests/test_workers.py`). The footer
  renders a dot per worker (AIS ingest / occupancy + sweep / AI intake). Liveness
  is from a heartbeat, **not** inferred from data freshness — the low-volume
  workers (occupancy, AI intake) legitimately idle for long stretches, so absence
  of new rows isn't death. This partly covers the "stale feed = silent failure"
  bullet below for the AIS worker (its beat ties to a real commit), though a
  dedicated last-message-age healthcheck is still worth adding.
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
- ✅ `worker_heartbeat` (migration `0011`) — per-worker liveness telemetry (one
  upserted row per worker; never grows). Not a domain table; see §5.4.
- ✅ `depth_survey` + `depth_segment` (migration `0012`) — the controlling-depth
  data layer for the draft gate; versioned hydrographic surveys reduced to a
  per-station controlling-depth profile (§3.3).
- Wharf apron polygon / `wharf_area` (§2.1).
- `reservation.derived_key` + unique index for idempotent observed rows (§2.5).
- Keep `app/models.py` enum tuples and the migration in lockstep. The exclusion
  constraint stays **`confirmed`-only** — do not extend it to block `observed`.

### 5.7 Code organization
- ✅ **HTTP surface split into `app/routers/`** — `main.py` was a 1,000-line
  module mixing every endpoint with app assembly. The routes now live in
  `app/routers/{read_only,intake,edit,analysis}.py` (+ `common.py` for the
  `do_write` write-wrapper and the `actor` audit helper); `main.py` keeps only
  app assembly (middleware, the `OperationalError` handler, the static mount,
  `/` + `/health`) and includes the routers. Pure mechanical move — `from
  app.main import app` is unchanged, so the test suite is untouched.
- 🟡 **Frontend modularization (planned, deferred)** — `app/static/index.html`
  is where most commits land; its ~2,250-line inline `<script>` should split
  into plain ES modules (`static/js/{api,state,map,timeline,panels,forms,history,app}.js`,
  no framework / build step) loaded via `<script type="module">`. Full breakdown
  in **[`FRONTEND_SPLIT_PLAN.md`](../FRONTEND_SPLIT_PLAN.md)**. Deferred because it
  needs browser smoke-testing and the file was under active concurrent edit; do
  it when quiescent, before it hits 5,000 lines.

---

## 6. Out of scope (still)

- **Scheduling optimizer / auto-assignment** (OR-Tools) — much later. This, not
  AIS, is what would ever *place* ships automatically (from requests + berth
  availability).
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
   map; plus auto status-mutation of stale rows (`expire_stale` /
   `POST /verification/sweep`).
4. ~~**AI-assisted intake**: pull the Power Pages / Dataverse berth-request table
   and LLM-normalize each row into a `requested` reservation.~~ **Done** —
   `app/intake/llm.py` + `dataverse_run.py` (OpenRouter / Gemini Flash, pull /
   outbound, proposes-never-places); needs IT app-registration + an OpenRouter key
   to run live.
5. ~~**Controlling-depth** table + draft validation in the confirm path (the
   deferred half of step 6, §3.3).~~ **Done (2026-06)** — `depth_survey`/
   `depth_segment` (migration `0012`), `.XYZ` upload + PostGIS reduction
   (`app/depth/*`), and the confirm gate (`app/edit._depth_gate`, blocks 422 with
   override). See §3.3.
6. **CI** with a PostGIS service container; AIS reconnect/metrics hardening.
   (Production deployment packaging is **done** — see §5.4; the prod image makes a
   CI build/integration job straightforward.)
7. *(Optional)* wire the legacy-spreadsheet backfill review→commit pipeline.
8. **Frontend modularization** — split `index.html`'s inline script into ES
   modules per [`FRONTEND_SPLIT_PLAN.md`](../FRONTEND_SPLIT_PLAN.md) (no build
   step); needs a browser smoke-test pass. See §5.7.
