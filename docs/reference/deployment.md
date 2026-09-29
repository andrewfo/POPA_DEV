# Deployment Reference

*Reference for `Dockerfile`, `docker-compose.prod.yml`, `docker-compose.yml`, `scripts/dev.sh`, `scripts/dev.ps1`, `scripts/import_berthing_schedule.py`, `.env.example`, `.github/workflows/ci.yml`, `docs/DEPLOY.md`, `app/auth.py`. POPA wharf data layer — roadmap → [`../PLAN.md`](../PLAN.md); design contract → [`../../CLAUDE.md`](../../CLAUDE.md).*

## Purpose

The production runtime is one Docker image (`popa-wharf`) that runs four roles
(API server, one-shot migrate/seed, AIS ingestor, occupancy worker) selected by
overriding the container command. The production compose (`docker-compose.prod.yml`)
orchestrates the stack over a persistent PostGIS volume with health/completion
gates between services.

The dev stack uses a separate lightweight compose (`docker-compose.yml`) that only
runs PostGIS; the API, workers, and optional AI-intake worker are started by the
dev script (`scripts/dev.sh` / `scripts/dev.ps1`), which also runs Alembic and the
wharf seed. CI runs on every push/PR via GitHub Actions (`.github/workflows/ci.yml`).

## Files

| File | What it does |
| --- | --- |
| `Dockerfile` | Builds `popa-wharf` from `python:3.11-slim`. Two-layer install: dependencies first (cached unless `pyproject.toml` changes), then source. Copies `alembic.ini`, `alembic/`, and `data/` at runtime (needed by migrate/seed). Runs as non-root `appuser` (UID 10001). Default `CMD`: gunicorn + uvicorn workers on `0.0.0.0:8000`; worker count from `WEB_CONCURRENCY` (default 4). |
| `docker-compose.prod.yml` | Production stack. Five services: `db` (PostGIS 16-3.4, named volume `popa_wharf_pgdata_prod`, healthcheck via `pg_isready`); `migrate` (one-shot: `alembic upgrade head && python -m app.seed.wharf_seed`, `restart: "no"`, waits for `db` healthy); `api` (gunicorn, depends on both `db` healthy and `migrate` completed, published **`127.0.0.1:8000` only**, healthcheck via `/health`); `ais` (`python -m app.ais.run`); `occupancy` (shell loop: `python -m app.occupancy.run; sleep $OCC_EVERY`). Optional fifth service `intake-dataverse` (`python -m app.intake.dataverse_run`, `restart: "no"` — self-exits if env vars absent). All app services share `env_file: .env` + `POSTGRES_HOST: db`. |
| `docker-compose.yml` | Dev-only PostGIS container. Exposes port 5432; dev defaults `popa/popa/popa_wharf`. Does **not** run the API or workers — those are started separately by `scripts/dev.sh`. |
| `scripts/dev.sh` | One-command local dev stack (macOS/Linux). Starts PostGIS (`docker compose up -d`), waits up to 90 s for healthy, runs `alembic upgrade head` + `app.seed.wharf_seed`, then backgrounds `uvicorn` (with `--reload`), the AIS ingestor (if `AISSTREAM_API_KEY` is set), an occupancy loop, and the AI-intake worker (if OpenRouter + Dataverse creds are set). Flags: `--no-api`, `--no-ais`, `--no-occupancy`, `--no-intake`, `--occ-every <s>`, `--port <n>`, `--down`. Traps `EXIT/INT/TERM` to kill all background PIDs. |
| `scripts/dev.ps1` | Same as `dev.sh` for Windows PowerShell. |
| `scripts/import_berthing_schedule.py` | One-time CSV importer. Loads a columnar berthing schedule (`berth,ship,start_date,end_date,cargo,loa_ft,imo,notes`) into the API via two HTTP calls per row: `POST /intake/berth-request` (creates vessel + `requested` reservation) then `PATCH /reservations/{id}` (assigns the berth + tries `confirmed`, falls back to `tentative` on 409). Dry-run by default; `--commit` to write. Idempotent (intake dedupes by content hash). Accepts `--base`, `--user`, `--password` or reads from `BASE_URL`/`OPERATOR_USER`/`OPERATOR_PASSWORD` env vars. |
| `.env.example` | Canonical config and secrets template. Copy to `.env` (gitignored). Key groups below. |
| `.github/workflows/ci.yml` | Four-job CI pipeline; see the CI section below. |
| `docs/DEPLOY.md` | Host + reverse-proxy + TLS playbook (Ubuntu + Docker + Caddy/nginx); operator-rotation, log, backup, update steps. |
| `app/auth.py` | HTTP Basic middleware. Gates the whole app (including the static mount at `/`). Active only when both `OPERATOR_USER` and `OPERATOR_PASSWORD` are set; blank = open (dev and TestClient run without credentials). Sets `request.state.operator` (the authenticated username, or `None`) for the `audit_log`. `/health` is the sole auth-exempt path (container probe). |

### `.env.example` key groups

| Group | Variables |
| --- | --- |
| Database | `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `POSTGRES_HOST`, `POSTGRES_PORT`, `DATABASE_URL` |
| Operator auth | `OPERATOR_USER`, `OPERATOR_PASSWORD` — setting **both** enables HTTP Basic; leaving either blank = open |
| AIS ingestion | `AISSTREAM_API_KEY`, `AIS_BBOX_SW_LAT/LON`, `AIS_BBOX_NE_LAT/LON` |
| Production workers | `WEB_CONCURRENCY` (gunicorn workers), `OCC_EVERY` (occupancy cadence seconds) |
| AI-assisted intake | `OPENROUTER_API_KEY`, `INTAKE_LLM_MODEL`, `INTAKE_LLM_SOURCE`, `DATAVERSE_URL`, `DATAVERSE_TENANT_ID`, `DATAVERSE_CLIENT_ID`, `DATAVERSE_CLIENT_SECRET`, `DATAVERSE_TABLE`, `DATAVERSE_ID_FIELD`, `DATAVERSE_STATUS_FIELD`, `DATAVERSE_STATUS_NEW`, `DATAVERSE_STATUS_TRIAGED`, `DATAVERSE_POLL_SECONDS`, `DATAVERSE_BATCH_LIMIT` |
| Draft gate | `DEPTH_CLEARANCE_FT`, `DEPTH_BIN_FT`, `DEPTH_TOE_OFFSET_FT`, `DEPTH_MAX_OFFSET_FT` |

## CI pipeline (`.github/workflows/ci.yml`)

Runs on every push to `main` and every PR. Concurrent runs for the same ref are cancelled.

| Job | Runner | What it does |
| --- | --- | --- |
| `pure-tests` | ubuntu-latest | Installs `.[dev]` (Python 3.12, pip-cached). Runs `pytest -v -ra` with no database. DB tests auto-skip via the `db_session` fixture's connect probe. Proves the no-DB path stays green. |
| `db-tests` | ubuntu-latest + PostGIS 16-3.4 service container | Spins a real `postgis/postgis:16-3.4` container with `pg_isready` healthcheck. Sets `DATABASE_URL`. Runs `alembic upgrade head`, `python -m app.seed.wharf_seed`, then `pytest -v -ra` against the migrated+seeded DB. DB tests run (not skip); pure tests also run — full coverage both paths. |
| `lint` | ubuntu-latest | Installs `ruff`, runs `ruff check app tests`. Separate job so style nits don't mask test results. |
| `typecheck` | ubuntu-latest | Installs `mypy`, runs `mypy app`. `continue-on-error: true` — advisory until the tree is clean. |

## Key concepts & invariants

- **One image, four roles.** The `Dockerfile` `CMD` runs the API; compose overrides the command for migrate, ais, and occupancy. This means a single `docker build` covers the whole stack.
- **HTTP Basic must sit behind upstream TLS.** Basic auth only base64-encodes credentials. The API port is published as `127.0.0.1:8000` in `docker-compose.prod.yml` — never `0.0.0.0` without TLS in front. `DEPLOY.md` documents Caddy/nginx as the TLS-terminating reverse proxy.
- **Auth is active only when both env vars are set.** `OPERATOR_USER=` + `OPERATOR_PASSWORD=` (or blank) → app runs open. This keeps dev and the TestClient suite credential-free with no code change.
- **`/health` is the lone auth-exempt path.** The container probe calls it; every other path (including the static map at `/`) is gated.
- **Migrations run once, as a one-shot service.** The `migrate` service (`restart: "no"`) runs `alembic upgrade head` + wharf seed before `api` and workers start (`depends_on: migrate: condition: service_completed_successfully`). `alembic upgrade head` is idempotent — it applies new migrations and skips already-applied ones.
- **Central Time is the DB default (migration 0008).** The DB session timezone is pinned to `America/Chicago`; `timestamptz` columns store absolute instants but are read/written in Central wall-clock by the app (`app/tz.py`). This is baked in at the schema level, not a runtime option.
- **Dev compose exposes port 5432; prod compose does not.** The production `db` service has no `ports:` binding — it is only reachable from within the compose network by the app services.
- **The `intake-dataverse` worker self-exits if unconfigured.** Leave the Dataverse/OpenRouter env vars blank to keep intake manual-only. `restart: "no"` prevents a crash-loop.

## Connections

- Schema migrations: `alembic/` (revisions 0001–0015). Schema changes require a new Alembic revision + matching `app/models.py` update — never edited by hand.
- Auth middleware: `app/auth.py` — sets `request.state.operator` for the audit log; the single shared credential records "the operator did it"; the middleware is the one place to add per-user identity if multiple credentials are added.
- Config: `app/config.py` reads `.env` via Pydantic settings.
- Frontend: see [`frontend.md`](frontend.md).
- Test suite: see [`tests.md`](tests.md).
