---
name: dev-db
description: Bring up, migrate, seed, reset, or inspect the local PostGIS database for the POPA wharf data layer. Use when the user wants to start the DB, run/redo migrations, seed the wharf segment, reset to a clean schema, or run the db-marked tests (anything involving docker compose, alembic, app.seed.wharf_seed, or "the database won't connect").
---

# Local PostGIS dev database

The data layer needs Postgres + PostGIS 3.x. `docker-compose.yml` provides a
local `postgis/postgis:16-3.4` instance (`popa_wharf_db`). All schema changes go
through Alembic — **never** edit the DB by hand (see [new-migration](../new-migration/SKILL.md)).

## Bring it up from scratch

```powershell
docker compose up -d                 # starts popa_wharf_db on $POSTGRES_PORT (default 5432)
docker compose ps                    # confirm healthy (healthcheck = pg_isready)
alembic upgrade head                 # creates enums, tables, btree_gist exclusion constraint
python -m app.seed.wharf_seed        # seeds the first wharf_segment (PLACEHOLDER geometry)
```

Connection settings come from `.env` (copy `.env.example` first). Defaults:
user `popa`, password `popa`, db `popa_wharf`. `DATABASE_URL` overrides the
pieces if set. Config lives in `app/config.py`.

## Verify it's reachable

```powershell
curl http://localhost:8000/health/db   # if the API is running (uvicorn app.main:app)
# or directly:
docker compose exec db psql -U popa -d popa_wharf -c "\dt"
docker compose exec db psql -U popa -d popa_wharf -c "SELECT postgis_version();"
```

## Reset to a clean schema

Two options, least destructive first:

```powershell
# A. Roll migrations back down, then back up (keeps the container/volume):
alembic downgrade base
alembic upgrade head
python -m app.seed.wharf_seed

# B. Nuke the data volume entirely (full wipe — only when A is not enough):
docker compose down -v
docker compose up -d
alembic upgrade head
python -m app.seed.wharf_seed
```

## Running the DB-marked tests

`db`-marked tests (e.g. `tests/test_geo_to_station.py`) need a migrated PostGIS.
They auto-skip when no database is reachable, so plain `pytest` stays green.

```powershell
pytest -m db          # requires DB up + migrated + seeded
pytest -m "not db"    # pure tests only (crosswalk, AIS parser)
```

## Gotchas

- The `no_wharf_overlap` exclusion constraint requires the `btree_gist`
  extension; the migration creates it. If a constraint error mentions
  `btree_gist`, the migration didn't fully apply — re-run `alembic upgrade head`.
- The exclusion constraint is **`confirmed`-only by design.** `observed` rows
  are allowed to overlap. Do not "fix" overlap errors by widening the
  constraint.
- The seeded centerline uses **placeholder** lat/lon vertices. `geo_to_station`
  returns geometrically-correct-but-not-real stations until the real quay
  geometry is digitized (see README "Important TODO").
