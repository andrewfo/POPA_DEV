# Tests Reference

*Reference for `tests/conftest.py` and all `tests/test_*.py` files. POPA wharf data layer — roadmap → [`../PLAN.md`](../PLAN.md); design contract → [`../../CLAUDE.md`](../../CLAUDE.md).*

## Purpose

The test suite covers two orthogonal axes: **pure logic** (crosswalk math,
conflict predicates, AIS/intake parsers, occupancy detection/projection, edit
range helpers, depth shortfall, shiptypes, worker classification, auth
credential check) and **DB-integrated behaviour** (geo→station via PostGIS
`ST_InterpolatePoint`, occupancy derivation, intake recording, reservation
CRUD, conflict endpoint, feasibility oracle, verification, depth gate, history
filters). Pure tests always run; DB tests auto-skip when no migrated PostGIS is
reachable, and run in CI against a real PostGIS service container.

CLAUDE.md mandates tests for the crosswalk module and conflict-detection logic;
these are covered in `test_crosswalk.py` and `test_conflicts.py` respectively.

## `tests/conftest.py` — shared fixtures

| Fixture | Scope | What it does |
| --- | --- | --- |
| `_db_engine` | session | Probes the DB once (2 s `connect_timeout`). On failure, calls `pytest.skip(...)` which is cached for the whole session — subsequent DB tests skip instantly instead of each timing out. Also requires PostGIS (`PostGIS_Lib_Version()`) and the migrated schema (`wharf_segment` exists). |
| `db_session` | function | Opens a connection, begins a transaction, creates a `sessionmaker` with `join_transaction_mode="create_savepoint"`. Yields the session; rolls back the outer transaction on teardown. The SAVEPOINT mode means endpoint `commit()` / `rollback()` calls inside `TestClient` act on a savepoint inside the outer transaction, so real rows never leak into the dev DB. |

**Pattern for DB endpoint tests:** override the app's `get_session` dependency to
return `db_session`, then call the endpoint through `TestClient`. The endpoint
commits/rolls back against the SAVEPOINT; the fixture's outer `trans.rollback()`
cleans everything up.

## Test files by area

| File | Area | Pure or DB-marked |
| --- | --- | --- |
| `test_crosswalk.py` | Stationing crosswalk — `AffineParams`, `corps_to_popa`, `popa_to_corps`, `dockno_to_popa`, `popa_to_dockno`, `format_station`, `parse_station`, `segment_corps_params`, `segment_dockno_params` | Pure |
| `test_geo_station_real.py` | End-to-end geo→station on the real derived centerline (`data/gis/centerline_vertices.json`). Uses pure `project_to_station` oracle; validates internal consistency and round-trip accuracy against expected POPA stations. No DB. | Pure |
| `test_geo_to_station.py` | PostGIS `geo_to_station` path. Inserts a controlled measured line (exact geometry) and projects a lat/lon to verify the expected station. | DB-marked |
| `test_ais_messages.py` | aisstream.io envelope parsing — `parse_aisstream` → `AISPosition` / `AISStatic`; field extraction (MMSI, lat/lon, SOG, COG, ship type, dimensions, destination, ETA); malformed/missing fields. | Pure |
| `test_intake_manual.py` | Manual berth-request intake. Pure: `normalize_form` (units, cargo, notes, AIS-override warnings). DB-marked: `record_manual_request` (deduplication, vessel upsert, reservation projection, `intake_event` landing); `POST /intake/berth-request` endpoint; edit/delete paths. | Pure + DB-marked |
| `test_intake_llm.py` | LLM-assisted intake (`app/intake/llm.py`). Injects a fake `complete` callable. Covers prompt assembly, tolerant JSON parsing, extraction → `BerthRequestForm` mapping, bad IMO / unparseable date / ambiguous values → null + note. No DB, no network. | Pure |
| `test_intake_dataverse.py` | Dataverse worker orchestration (`dataverse_run.process_batch`, `run_input_file`). Fakes the Dataverse client, LLM call, and `record_manual_request`. No DB or network. | Pure |
| `test_occupancy_detect.py` | Berthing detector (`app/occupancy/detect.py`). `detect_berthings` with synthetic sample tracks: hysteresis enter/exit, SOG threshold, alongside flag, sustained-window logic. | Pure |
| `test_occupancy_project.py` | Bow/stern projection (`app/occupancy/project.py`). `project_bow_stern` and `destination_point` geodesic math against known distances/bearings. | Pure |
| `test_occupancy_derive.py` | Full occupancy derivation against PostGIS. Inserts a controlled measured segment + synthetic berthing track; checks `derive_occupancy` writes exactly one `observed` reservation; re-run is idempotent (updates same row, no duplicates). | DB-marked |
| `test_conflicts.py` | Conflict-primitive pure logic. Overlap matrix: time-only / station-only / both / neither; half-open time adjacency; closed station endpoints; open-ended time; empty station ranges; `overlap_interval` intersection rectangle; `classify` category (observed-vs-planned, dredge-vs-vessel, planned-vs-planned). | Pure |
| `test_conflicts_db.py` | Conflict endpoint (`GET /conflicts`) against PostGIS. Seeds reservations via `TestClient` inside the rolled-back transaction; exercises the real Postgres `&&` / `*` range operators and the self-join. | DB-marked |
| `test_feasibility.py` | Feasibility oracle interval math (`app/feasibility.py`). `subtract_intervals`, `feasible_bands`, `candidate_slots`, `placement_range`: gap padding, LOA fit threshold, two-obstacle spacing, wharf-extent clipping. | Pure |
| `test_feasibility_db.py` | Feasibility oracle endpoint (`GET /feasibility`) against PostGIS. Pins a deterministic wharf extent `[0, 4000]` inside the rolled-back transaction; tests depth annotation, Dock No. conversion, 404/422 responses. | DB-marked |
| `test_edit.py` | Manual edit surface (`app/edit.py`). Pure: `_station_range`, `_time_range`, partial-update field selection. DB-marked: vessel PATCH, reservation create/edit/cancel/delete, confirmed-overlap 409 (exclusion constraint), `PATCH /vessels/{id}` with `dims_locked`. | Pure + DB-marked |
| `test_depth.py` | Depth data layer + draft gate. Pure: `.XYZ` parser, filename-date parser, `depth_shortfall` comparison. DB-marked: PostGIS reduction (`import_survey` → project → clip → bin → shallowest per bin), `controlling_depth_over` lookup, confirm-endpoint block (422) / override / no-survey-warning. Uses a synthetic wharf segment far from seeded data; commits setup inside the SAVEPOINT so the endpoint's own rollback-on-422 doesn't discard it. | Pure + DB-marked |
| `test_verification.py` | AIS verification classifiers — `classify_planned` (arrived / no_show / awaiting), `where_planned`, `expiry_action`. State machine exercised directly; DB query covered separately. | Pure |
| `test_verification_db.py` | Verification endpoint (`GET /verification`, `POST /verification/sweep`) against PostGIS. Seeds vessels + reservations with far-off years (2020 / 2030) for determinism. Exercises LATERAL join, stale-close archival, `feed_alive` gate. | DB-marked |
| `test_reservations_window.py` | `GET /reservations` time-window filter against PostGIS. Seeds reservations inside the rolled-back transaction; checks from/to boundary behaviour. | DB-marked |
| `test_history_db.py` | `GET /history` filter matrix against PostGIS. Exercises name search and IMO search added alongside name search. | DB-marked |
| `test_vessels_lookup.py` | IMO auto-fill (`GET /vessels/lookup`). On-file AIS-tracked vs manual-only hit (dims in feet, `ais_tracked`), clean miss → 200 `{found:false}`, invalid IMO → 422, most-recently-updated row wins. | DB-marked |
| `test_vessels_search.py` | Vessel-name type-ahead (`GET /vessels/search`). Case-insensitive substring match, prefix ranks first, one row per IMO (most recent wins), invalid/missing IMOs excluded, digits match an IMO prefix, short `q` → `[]`, `limit` cap, `%`/`_` literal, `last_seen` from the latest AIS fix. | DB-marked |
| `test_workers.py` | Worker-liveness classifier (`app/workers.classify`). Health rules: offline (no heartbeat row), error (last cycle raised), stale (beat age > 3× cadence), ok. No DB. | Pure |
| `test_auth.py` | HTTP Basic middleware (`app/auth.py`). Pure: `check_credentials`. `TestClient`: 401 with no/wrong credentials, 200/pass-through with correct credentials, open mode (no env vars set), `/health` auth-exempt. No DB needed — middleware decides before route handlers. | Pure |
| `test_shiptypes.py` | Service-craft classification (`app/shiptypes.py`). Pins `SERVICE_CRAFT_TYPES == frozenset({31, 32, 50, 52, 56, 57})` so drift from the UI's `shipTypeCategory` buckets in `api.js` shows as a test failure. | Pure |

## Key concepts & invariants

- **Pure logic is always tested, DB tests auto-skip.** The `db_session` fixture's session-scoped connect probe caches the skip decision — a clean run without PostGIS completes quickly (one ~2 s probe, then instant skips for all DB tests).
- **DB tests run in CI against a real PostGIS service container** (`.github/workflows/ci.yml` `db-tests` job: `postgis/postgis:16-3.4`, migrated + seeded before `pytest`). The `pure-tests` job also runs the full suite without a DB, so both paths are always green.
- **Write endpoints are tested through `TestClient`, never by committing real rows.** The `db_session` fixture's outer `trans.rollback()` cleans everything up. `join_transaction_mode="create_savepoint"` is essential: without it, an endpoint's `session.commit()` would commit to the real transaction and leave rows in the DB.
- **SAVEPOINT setup pattern for tests that need committed setup rows** (e.g. `test_depth.py`): insert setup data and call `session.commit()` (which commits to the SAVEPOINT), then call the endpoint under test. The endpoint's `session.rollback()` on a 422 rolls back only to the inner SAVEPOINT, not the fixture's outer transaction.
- **`pytestmark = pytest.mark.db`** is present on some test modules as documentation, but the real gate is the `db_session` fixture: if a test doesn't request `db_session`, it runs regardless of the marker. If it does request `db_session` and no DB is reachable, it skips via the fixture's `pytest.skip()`.
- **CLAUDE.md mandates tests for the crosswalk module and the conflict-detection logic.** `test_crosswalk.py` and `test_conflicts.py` fulfil this explicitly.

## Connections

- Test config: `DATABASE_URL` env var (or assembled from `POSTGRES_*`) is read by `app/config.get_settings()` and by `conftest._db_engine`. CI sets `DATABASE_URL` directly for the `db-tests` job.
- CI pipeline: see [`deployment.md`](deployment.md) for the full workflow description.
- The crosswalk module under test: `app/crosswalk.py`. See [`core.md`](core.md) for the stationing model.
- The conflict service under test: `app/conflicts.py`. See [`core.md`](core.md) for the overlap primitive.
