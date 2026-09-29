# Reference documentation

Per-section reference for the POPA wharf data layer: **what each part of the
codebase is and does**, cross-referenced to the files. This is the descriptive
"how it works today" layer.

- For **future plans / open issues**, see [`../PLAN.md`](../PLAN.md).
- For the **design contract** (rules that override everything), see
  [`../../CLAUDE.md`](../../CLAUDE.md).
- For **setup/run**, see [`../../README.md`](../../README.md).

When a reference doc and `CLAUDE.md` disagree, `CLAUDE.md` wins — these docs
describe, they don't govern. Each doc is kept in sync by the `update-context`
skill whenever the code it covers changes.

## Sections

| Doc | Covers | Code |
| --- | --- | --- |
| [core.md](./core.md) | Cross-cutting app-root modules: settings, DB/session, ORM, time, app assembly, auth, audit, worker liveness | `app/{config,db,models,tz,main,auth,audit,workers}.py` |
| [crosswalk.md](./crosswalk.md) | Stationing crosswalk (POPA ↔ Corps ↔ Dock No.) + wharf centerline/apron geometry | `app/crosswalk.py`, `app/seed/wharf_seed.py`, `data/gis/` |
| [migrations.md](./migrations.md) | Alembic migration history + the schema each revision builds | `alembic/versions/0001…0015` |
| [ais.md](./ais.md) | AIS ingestion (step 4): normalized messages, pluggable sources, source-agnostic ingestor | `app/ais/` |
| [occupancy.md](./occupancy.md) | Occupancy derivation (step 5): detect → project → alongside → idempotent `observed` rows | `app/occupancy/` |
| [depth.md](./depth.md) | Controlling-depth data layer + draft gate (step 6): `.XYZ` reduction, versioned surveys, confirm-time gate | `app/depth/` |
| [scheduling.md](./scheduling.md) | The scheduling brain: conflict primitive, feasibility oracle, AIS verification, manual edit surface, ship types | `app/{conflicts,feasibility,verification,edit,shiptypes}.py` |
| [intake.md](./intake.md) | Berth-request intake (step 7 capture): manual entry, LLM normalizer, Dataverse pull worker | `app/intake/` |
| [routers.md](./routers.md) | The HTTP surface split by concern + the `do_write`/audit write-wrapper | `app/routers/` |
| [frontend.md](./frontend.md) | The Leaflet UI: map, occupancy timeline, panels, edit/intake forms, ES modules | `app/static/` |
| [deployment.md](./deployment.md) | Packaging, dev/prod compose, CI, auth's ops role, the schedule importer | `Dockerfile`, `docker-compose*.yml`, `scripts/`, `.github/`, `docs/DEPLOY.md` |
| [tests.md](./tests.md) | The test suite: the `db_session` fixture + every test file by area (pure vs db-marked) | `tests/` |
