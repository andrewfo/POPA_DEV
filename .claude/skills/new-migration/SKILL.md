---
name: new-migration
description: Author a new Alembic migration for the POPA wharf schema and keep app/models.py in sync. Use whenever the schema changes — adding/altering a table or column, adding an enum value, changing a constraint or index, or any DDL. The migration is the source of truth; the DB is never edited by hand.
---

# Adding a schema change

**Rule from CLAUDE.md: migrations only via Alembic; the migration is the source
of truth for the live schema; `app/models.py` mirrors it.** Any schema change is
a paired edit — a new revision **and** a matching `models.py` update in the same
change.

## Workflow

1. **Edit `app/models.py`** to reflect the desired end state (new column, table,
   constraint, index, enum tuple).
2. **Generate the revision skeleton**, then hand-write the body — do not trust
   autogenerate blindly (it misses PostGIS/GIST/exclusion/enum details):
   ```powershell
   alembic revision -m "short description"   # creates alembic/versions/000N_*.py
   ```
   Set a readable revision id matching the existing `000N` convention and wire
   `down_revision` to the current head (`alembic heads` shows it).
3. **Write `upgrade()` and a real `downgrade()`.** Downgrade must actually
   reverse the change — `tests`/`alembic downgrade base` rely on it.
4. **Apply and round-trip test:**
   ```powershell
   alembic upgrade head
   alembic downgrade -1
   alembic upgrade head
   pytest -m db
   ```

## Project-specific patterns (mirror migration 0001)

- **Enums:** keep the value tuple in `models.py` (e.g. `RESERVATION_STATUSES`)
  and the migration's `postgresql.ENUM(...)` in sync. In the migration, declare
  enums with `create_type=False` and create/drop them explicitly so
  `create_table` doesn't re-emit `CREATE TYPE`. Adding a value to an existing
  enum needs `ALTER TYPE ... ADD VALUE` (note: not reversible inside a
  transaction — handle in the migration accordingly).
- **PostGIS geometry:** use `geoalchemy2` types; geometry columns carry an SRID
  (4326) and may be measured (`LINESTRINGM`, `M` = POPA station). Spatial
  indexes are created via the column definition.
- **Ranges:** `station_range` is `NUMRANGE`, `time_range` is `TSTZRANGE`.
- **The exclusion constraint stays `confirmed`-only.** It needs the
  `btree_gist` extension and is:
  ```sql
  EXCLUDE USING gist (time_range WITH &&, station_range WITH &&)
    WHERE (status = 'confirmed')
  ```
  `observed` AIS rows must be allowed to overlap planned reservations — that
  overlap is the signal to surface, not block. Re-read CLAUDE.md "Core model"
  before changing this.

## Checklist before done

- [ ] `models.py` and the migration describe the same schema (columns, enums,
      constraints, indexes).
- [ ] Enum value tuples match between the two.
- [ ] `downgrade()` cleanly reverses `upgrade()` (verified by the round-trip).
- [ ] `alembic upgrade head` then `pytest -m db` is green against a live PostGIS
      (see [dev-db](../dev-db/SKILL.md)).
