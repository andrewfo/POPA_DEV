# Alembic Migration History & Schema

*Reference for `alembic/versions/0001`–`0017` and the tables they create. POPA wharf data layer — roadmap → [`../PLAN.md`](../PLAN.md); design contract → [`../../CLAUDE.md`](../../CLAUDE.md).*

## Purpose

Alembic migrations are the **single source of truth** for the live schema. The ORM in `app/models.py` mirrors the migration-created schema but is never the authority — if the two diverge, the migration wins. The DB is never edited by hand; every schema change goes through a new migration revision.

Each migration file is a self-contained, documented change. Migrations are applied in sequence (`0001` → `0017`); the Alembic `revision` / `down_revision` chain enforces this ordering. The `upgrade` function is the canonical definition of what the schema contains.

## Tables

| Table | Created in | Purpose |
| --- | --- | --- |
| `wharf_segment` | 0001 | Measured (`M` = POPA station) centerline + affine crosswalk params + apron polygon |
| `vessel` | 0001 | Physical vessel: MMSI/IMO identity, name, dimensions (loa/beam/draft/dim_a/dim_b), `dims_locked`, `ais_*` dim shadow (0016) |
| `reservation` | 0001 | Time × station rectangle: every occupancy (vessel, dredge, layberth) — AIS-derived or planned; `updated_at`/`last_actor` last-writer provenance (0017) |
| `intake_event` | 0001 | Raw inbound berth request exactly as received; audit + reconciliation trail; soft-deleted; `updated_at`/`last_actor` last-writer provenance (0017) |
| `position_report` | 0001 | Landed AIS position fixes; source-agnostic |
| `berth` | 0006 | Named POPA station ranges — the operator's handle for a wharf stretch |
| `audit_log` | 0010 | Append-only write trail: who/what for every mutating endpoint |
| `worker_heartbeat` | 0011 | Per-worker liveness: one upserted row per background worker |
| `depth_survey` | 0012 | One hydrographic condition survey (versioned; active flag) |
| `depth_segment` | 0012 | Per-station controlling (shallowest) depth, reduced from a survey |
| `depth_cell` | 0013 | 2-D station × cross-channel offset depth grid (visualization only) |

## Migration table

| # | Title | Key changes |
| --- | --- | --- |
| 0001 | Initial schema | Creates `wharf_segment`, `vessel`, `reservation`, `intake_event`, `position_report`. Adds PostGIS + btree_gist extensions. Defines all five enum types (`reservation_type`, `reservation_status`, `reservation_source`, `direction`, `intake_source`) with initial values `ais\|form\|phone\|operator`. Adds `no_wharf_overlap` exclusion constraint (confirmed-only, bare station overlap). |
| 0002 | Occupancy dims & derived key | Adds `vessel.dim_a` / `dim_b` (AIS bow/stern offset metres). Adds `reservation.derived_key` (partial unique index `WHERE derived_key IS NOT NULL`) for idempotent AIS-derived row upserts. |
| 0003 | Intake dedupe key | Adds `intake_event.dedupe_key` (partial unique index `WHERE dedupe_key IS NOT NULL`) so re-importing the same raw row is idempotent. |
| 0004 | `email` intake source | Adds `'email'` to `reservation_source` and `intake_source` enums (manual phone/email channel). Uses `autocommit_block`; downgrade is a documented no-op (Postgres cannot drop a single enum label). |
| 0005 | Wharf apron polygon | Adds nullable `wharf_segment.apron` (POLYGON SRID 4326 + GiST index). Occupancy "alongside" test uses `ST_Contains` on this polygon when present, centerline buffer when NULL. |
| 0006 | Berth catalog | Creates `berth` table (name, `popa_sta_start/end`, CHECK `start < end`). Adds nullable `reservation.berth_id` FK (`ON DELETE SET NULL`). Berth assignment copies the range onto `reservation.station_range`; conflict detection continues on the range. |
| 0007 | 75 ft minimum mooring gap | Recreates `no_wharf_overlap` with a **buffered** station range: each non-empty range is padded ±37.5 ft before the `&&` test, so two confirmed vessels closer than 75 ft collide. `GAP_FT = 75.0` in the migration is the **single source of truth** — no app-config mirror. Empty ranges remain empty (CASE guard). |
| 0008 | Central Time DB default | `ALTER DATABASE ... SET TimeZone TO 'America/Chicago'`. Applies to new connections; `app/db.py` also pins the zone per-connection (belt-and-braces). |
| 0009 | `ai` intake source | Adds `'ai'` to both source enums — distinct provenance for the AI-assisted intake channel (previously borrowed `'email'`). `autocommit_block`; downgrade no-op. |
| 0010 | Soft-delete + audit log | Adds `intake_event.deleted_at` (NULL = live; set = soft-deleted). Repartitions dedupe index to `WHERE dedupe_key IS NOT NULL AND deleted_at IS NULL` so soft-deleted rows don't block re-submission. Creates `audit_log` (actor, action, entity, entity_id, JSONB detail, at) — append-only, no FKs. |
| 0011 | Worker heartbeat | Creates `worker_heartbeat` (PK on `name`; upserted, never appended). One row per background worker; `GET /workers` derives liveness from `beat_at` staleness. |
| 0012 | Depth surveys | Creates `depth_survey` (versioned, active flag, srid, datum, station extent, bin_ft) and `depth_segment` (per-station `popa_range NUMRANGE`, `controlling_depth_ft`; GiST index on range; CASCADE deletes from survey). Raw soundings not stored. |
| 0013 | Depth cells | Creates `depth_cell` (survey + `popa_range` + `offset_range` NUMRANGE + `controlling_depth_ft`; CASCADE). 2-D station × cross-channel grid for map visualization only; draft gate still reads `depth_segment`. |
| 0014 | Repair AIS LOA drift | Data-only repair: for MMSI vessels where `loa ≠ dim_a + dim_b`, recomputes `loa = dim_a + dim_b` and nulls `draft` (irrecoverable). Fixes corruption caused by a former bug in the manual edit surface. No schema change; downgrade is a no-op. |
| 0015 | `vessel.dims_locked` | Adds `vessel.dims_locked BOOLEAN NOT NULL DEFAULT false`. When set: the manual edit surface **applies** dimension edits for an AIS-tracked vessel, and the AIS ingestor stops overwriting those columns. Off by default; clearing it hands dimensions back to AIS. |
| 0016 | `vessel.ais_*` dim shadow | Adds `vessel.ais_loa`/`ais_beam`/`ais_draft` (NUMERIC, nullable) — the last dims AIS reported, maintained by the ingestor on **every** `ShipStaticData` even while `dims_locked` freezes the live columns (they are *not* in the ingestor's locked set). Lets **revert-to-AIS** (`app/edit.update_vessel`, clearing the lock) restore the live loa/beam/draft **immediately** instead of waiting for the next broadcast. Backfilled from live dims for unlocked AIS-tracked rows; locked rows left NULL (unrecoverable → revert falls back to unlock-only). |
| 0017 | last-writer provenance | Adds `updated_at TIMESTAMPTZ DEFAULT now()` + `last_actor TEXT` to **both** `intake_event` and `reservation`. `updated_at` is stamped on every write (create and edit), distinct from `created_at`/`received_at` (first arrival); `last_actor` is the Basic-auth username (`request.state.operator`) threaded through the write functions, **NULL** for agent/AIS/uncredentialed writes — the "no human touched this" signal. Backfilled: `updated_at` from `received_at` (intake) / `created_at` (reservation), `last_actor` NULL. Powers the card's "edited by … · …" line and the write-precedence rule (operator authoritative over the agent). |

## Key invariants

**Migrations are the schema source of truth.** `app/models.py` is a mirror — it must be kept in sync, but the DB schema is always what the latest applied migration says. Never edit the DB by hand; never rely on model declarations to drive schema changes.

**Keep enum value tuples in `app/models.py` in lockstep with migrations.** Enum additions use `ALTER TYPE ... ADD VALUE IF NOT EXISTS` inside an `autocommit_block` (Postgres cannot add an enum value inside a normal transaction that later uses it). Removing an enum label requires recreating the type and rewriting every dependent column — not done lightly; all downgrade functions for enum-addition migrations are documented no-ops.

**`no_wharf_overlap` exclusion constraint is `confirmed`-only on purpose.** `observed` AIS rows are ground truth and are intentionally allowed to overlap planned (`tentative`, `requested`) reservations — that overlap is the signal the conflict-detection surface exposes, not something to block. Archiving a row (to `completed` / `cancelled`) moves it outside the `confirmed` predicate, so the sweep can never raise a 409.

**75 ft mooring gap is baked into the constraint, not config.** Migration 0007's `GAP_FT = 75.0` is a literal in the constraint expression; the constraint cannot read `app.config`. The one Python-side copy is `app/conflicts.MOORING_GAP_FT` — an explicitly documented advisory mirror for the feasibility oracle. Changing the gap requires authoring a new migration that drops and recreates the constraint; editing config or `MOORING_GAP_FT` alone has no effect on the DB.

**`btree_gist` is required.** The exclusion constraint uses a GIST index on `time_range` (a range type) alongside `station_range` (also a range type). `btree_gist` is installed in migration 0001 (`CREATE EXTENSION IF NOT EXISTS btree_gist`); it must be present before the constraint can be created.

**Empty `station_range` never conflicts.** An intake-captured `requested` reservation has an empty `numrange()` (no berth assigned yet). Migration 0007's `CASE WHEN isempty(station_range) THEN station_range ...` guard keeps the empty range empty after buffering, so it never matches anything via `&&`. This is intentional — unplaced requests should not block confirmed bookings.

**Soft-delete, not hard-delete, for `intake_event`.** A berth request that is withdrawn still leaves its raw row (with `deleted_at` set) for the audit trail. The partial dedupe index (`deleted_at IS NULL`) means the soft-deleted row no longer blocks a re-submission of the same content.

**New migrations convention.** Use the `/new-migration` skill (or `alembic revision -m "<description>"`) to create a new revision file in `alembic/versions/`. Always: update `app/models.py` to mirror the schema change; test with the `dev-db` skill to apply on the local PostGIS instance; add or update any affected db-marked tests. Never hand-edit the live DB.

## Connections

- All schema objects are declared in `app/models.py` — see [`core.md`](core.md).
- The measured centerline seeded into `wharf_segment` is produced by `data/gis/build_centerline.py` and loaded by `app/seed/wharf_seed.py` — see [`crosswalk.md`](crosswalk.md).
- The `no_wharf_overlap` constraint's advisory Python mirror (`conflicts.MOORING_GAP_FT`) lives in `app/conflicts.py`, used by `app/feasibility.py`.
- Depth survey ingestion (projection of `.XYZ` soundings into `depth_segment` / `depth_cell`) lives in `app/depth/ingest.py`.
