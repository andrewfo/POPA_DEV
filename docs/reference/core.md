# Core Cross-Cutting Modules

*Reference for `app/config.py`, `app/db.py`, `app/models.py`, `app/tz.py`, `app/main.py`, `app/auth.py`, `app/audit.py`, `app/workers.py`. POPA wharf data layer — roadmap → [`../PLAN.md`](../PLAN.md); design contract → [`../../CLAUDE.md`](../../CLAUDE.md).*

## Purpose

These modules form the foundation every other part of the app depends on. They own the configuration surface, database connectivity, ORM definitions, time-zone canon, HTTP app assembly, authentication, and two cross-cutting operational concerns: the append-only audit trail and the per-worker liveness heartbeat system.

None of these modules should contain domain logic (stationing math, conflict detection, occupancy derivation). They are infrastructure: any module in `app/` imports from here; nothing here imports from domain modules.

## Files

| File | What it does |
| --- | --- |
| `app/config.py` | Pydantic `Settings` (loaded from `.env`); single `get_settings()` cached call. Owns all env-driven knobs: DB connection, operator credentials, AIS key/bbox, LLM/Dataverse keys, occupancy thresholds, verification grace, depth gate params. **Does not** hold the mooring-gap constant (see Invariants). |
| `app/db.py` | Creates the SQLAlchemy `engine` (with `connect_timeout` and `options=-c timezone=America/Chicago` per connection) and `SessionLocal`. Exposes `get_session()` as a FastAPI dependency. |
| `app/tz.py` | Defines `TZ_NAME = "America/Chicago"`, `CENTRAL = ZoneInfo(TZ_NAME)`, and `assume_central(value)` — the single entry point for attaching a time zone to naive operator input. |
| `app/models.py` | SQLAlchemy ORM declarations for every table: `WharfSegment`, `Berth`, `Vessel`, `Reservation`, `IntakeEvent`, `PositionReport`, `AuditLog`, `WorkerHeartbeat`, `DepthSurvey`, `DepthSegment`, `DepthCell`. Also defines enum value tuples (`RESERVATION_TYPES`, `RESERVATION_STATUSES`, `RESERVATION_SOURCES`, `DIRECTIONS`, `INTAKE_SOURCES`) that must stay in lockstep with the Alembic migrations. Migrations are the schema source of truth; this file mirrors them. |
| `app/main.py` | Assembles the `FastAPI` app: adds `BasicAuthMiddleware`, registers the `OperationalError → 503` handler, mounts `/static`, serves `GET /` (Leaflet map) and `GET /health`, includes all five routers (`read_only`, `intake`, `edit`, `analysis`, `depth`). |
| `app/auth.py` | `BasicAuthMiddleware` — a Starlette middleware (not a route dependency, so it also gates the static mount). Active only when both `OPERATOR_USER` and `OPERATOR_PASSWORD` are set; otherwise passes all requests through with `request.state.operator = None`. `/health` is always exempt. Sets `request.state.operator` to the authenticated username for downstream audit use. Uses `secrets.compare_digest` on both fields to avoid timing leaks. |
| `app/audit.py` | `record_audit(session, *, actor, action, entity, entity_id, detail)` — inserts one row into `audit_log`. Append-only; no constraints that could block the caller's transaction. Does **not** commit (the endpoint owns the transaction). Called via the `audit=` hook in `app/routers/common.do_write`. |
| `app/workers.py` | `KNOWN_WORKERS` registry (`name → (label, cadence_seconds)` for `ais`, `occupancy`, `intake-dataverse`). `beat(name, status, detail)` — self-contained upsert in its own session, commits and swallows errors so a heartbeat never crashes its worker. `record_heartbeat` — session-scoped form for callers that already own a transaction. `classify(status, age_seconds, cadence_seconds)` — pure function returning `offline / error / stale / ok`. Read by `GET /workers` (in `app/routers/`). |

## Key concepts & invariants

**Central Time is the canonical wall-clock.** The whole system operates on `America/Chicago`. Three places enforce this together and must stay in sync:
1. `app/tz.py` — defines `TZ_NAME`; `assume_central` stamps naive form input as Central before it reaches a `timestamptz` column.
2. `app/db.py` — passes `options=-c timezone=America/Chicago` on every connection, so `now()`, `timestamptz` renders, and naive binds are all Central regardless of system TZ.
3. Migration `0008` — sets `ALTER DATABASE ... SET TimeZone TO 'America/Chicago'` so direct `psql` sessions and reporting tools also see Central.

`timestamptz` columns still store absolute instants. Central is only how they are read and written. AIS `msg_ts` is a genuine UTC instant and is left as-is; it simply renders in Central. Never hard-code `-6`/`-5` offsets; never attach UTC to zone-less form input.

**Auth is whole-app middleware, not per-route.** `BasicAuthMiddleware` is registered on the `FastAPI` app before any router, so it covers the static mount at `/` and `/static/*` — mounted sub-apps bypass FastAPI route dependencies. Auth is disabled (open) when either credential env var is blank; this is intentional so local dev and the `TestClient` suite run without credentials. The operator credential set `request.state.operator` is the sole identity source for audit rows; with one shared credential it is always `OPERATOR_USER`.

**Audit rows are in-transaction with the write.** `record_audit` inserts into `audit_log` inside the same `Session` transaction as the mutation it records. If the mutation rolls back (e.g. a 409 conflict), the audit row rolls back too — only real, committed changes are logged. The table has no foreign keys (a deleted reservation must not orphan its audit row) and no constraints that could block the write.

**`app/models.py` mirrors migrations; migrations are truth.** When enum value tuples change (e.g. adding `'ai'` in migration 0009), both the migration and the tuple in `models.py` must be updated together. Migrations run `ALTER TYPE ... ADD VALUE` via `autocommit_block`; the ORM tuple is purely informational / for type-checking.

**The mooring-gap constant does not live in `config.py`.** Migration `0007`'s `GAP_FT = 75.0` is baked directly into the `no_wharf_overlap` exclusion constraint expression. There is no `Settings.min_vessel_gap_ft`; a former one was removed because it looked tunable but the constraint ignored it. The single Python-side mirror is `conflicts.MOORING_GAP_FT` in `app/conflicts.py` — an explicitly advisory copy for the feasibility oracle. Changing the gap requires a new migration.

**`/health` is the only auth-exempt path.** It is the container/orchestrator liveness probe and must be reachable without credentials at all times.

**Worker heartbeat is liveness telemetry, not an audit trail.** Each worker has exactly one row in `worker_heartbeat` (keyed on `name`), upserted on every cycle. `classify` judges health from how stale `beat_at` is relative to the worker's nominal cadence: `> 3 × cadence` (floor 45 s) is `stale`. A worker that has never beaten shows `offline`. The table never grows beyond `len(KNOWN_WORKERS)` rows.

## Connections

- `app/config.py` is imported by virtually every module; `get_settings()` is the single config access point.
- `app/db.py` supplies `get_session` to all FastAPI route handlers and `SessionLocal` to background workers and scripts.
- `app/tz.py` is imported by `app/db.py` (for `TZ_NAME`), `app/edit.py`, and `app/intake/manual.py` (to stamp form datetimes).
- `app/models.py` is imported by every module that issues ORM queries.
- `app/auth.py` is wired onto the app in `app/main.py`; it feeds `request.state.operator` to all write endpoints.
- `app/audit.py` is called from `app/routers/common.py` (`do_write`), which is used by every mutating router.
- `app/workers.py`'s `beat()` is called by `app/ais/run.py`, `app/occupancy/run.py`, and `app/intake/dataverse_run.py`; `GET /workers` (in `app/routers/analysis.py`) reads the heartbeat table back.
- See also: [`crosswalk.md`](crosswalk.md) (stationing/geometry layer), [`migrations.md`](migrations.md) (full schema history).
