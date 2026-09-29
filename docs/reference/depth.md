# Controlling Depth & Draft Gate

*Reference for `app/depth/`. POPA wharf data layer — roadmap → [`../PLAN.md`](../PLAN.md); design contract → [`../../CLAUDE.md`](../../CLAUDE.md).*

## Purpose

The depth subsystem provides the draft-vs-controlling-depth gate that guards the confirm path: a reservation may only be moved to `confirmed` when the vessel's draft (plus a configurable under-keel clearance margin) does not exceed the shallowest recorded depth over the reservation's station range.

The data layer reduces periodic hydrographic condition surveys (`.XYZ` point clouds of soundings in Texas South Central State Plane ftUS, EPSG:2278) down to a lean per-station controlling-depth profile stored in the DB. Surveys are versioned — each upload is a new dated row; the gate always reads the latest active one. Raw soundings are not kept.

## Files

| File | What it does |
| --- | --- |
| `app/depth/parse.py` | Tolerant `.XYZ` reader. `parse_xyz()` yields `(x, y, z)` float triples, skipping blank lines, `#`/`;` comments, and any line whose first three tokens are not numeric. Extra columns past the third are ignored. `parse_survey_date()` extracts a date from POPA's `MMDDYYYY`-prefixed filenames (e.g. `07092023CND_POPA_B1-6_2x2.XYZ` → 2023-07-09); returns `None` when the pattern is absent. |
| `app/depth/ingest.py` | PostGIS reduction pipeline. `import_survey()` runs in three SQL passes inside a caller-owned transaction: (1) `COPY` soundings into a `TEMP TABLE`; (2) transform each sounding from the source CRS to WGS84, project onto the nearest `wharf_segment` centerline via `ST_InterpolatePoint` to get its POPA station, clip to the berthing zone (apron polygon or `[toe_offset_ft, max_offset_ft]` perpendicular band), and keep only surviving soundings in `tmp_proj`; (3a) bin `tmp_proj` by POPA station and take `min(z)` per bin → `depth_segment`; (3b) bin the same set by station AND perpendicular offset → `depth_cell` (visualization only). Raises `ValueError` (→ 422) if no sounding survives the clip. Also runnable as `python -m app.depth.ingest <file.XYZ>`. |
| `app/depth/gate.py` | Two-function gate. `depth_shortfall(controlling_ft, draft_m, clearance_ft)` — pure: returns the positive deficit in feet when `draft_m × 3.28 + clearance_ft > controlling_ft`, else `None`. `None` inputs (unknown depth or draft) also return `None` — the caller treats "can't evaluate" as a warning, not a block. `controlling_depth_over(session, lo, hi)` — DB lookup: finds the latest active `depth_survey` whose station extent overlaps `[lo, hi]`, then returns `(min(controlling_depth_ft), surveyed_at)` over all `depth_segment` rows in that survey that overlap the range. Returns `(None, None)` when no active survey covers the range. |

## Schema (migrations 0012 & 0013)

| Table | Purpose |
| --- | --- |
| `depth_survey` | One row per upload. Key columns: `surveyed_at` (date), `source_file`, `srid` (default 2278), `datum` (free text, e.g. `MLLW`), `active` (bool — operator can disable a bad import), `station_min`/`station_max` (POPA feet, backfilled from segments), `min_depth_ft` (headline shallowest), `bin_ft` (station bin width used). |
| `depth_segment` | Per-station controlling depth. Each row: `survey_id`, `popa_range numrange` (half-open `[lo, hi)`), `controlling_depth_ft` (shallowest sounding in the bin), `point_count`. GiST index on `popa_range` for `&&` overlap queries. |
| `depth_cell` | 2-D cross-section grid (migration 0013). Adds `offset_range numrange` (perpendicular feet off quay face) to the same binning. **Visualization only** — the map cross-section overlay shows how the bed shoals into the channel. The gate reads `depth_segment`, not this table. A station's `depth_segment` controlling depth equals `min(controlling_depth_ft)` over its `depth_cell` rows — they can never disagree because both are built from the same `tmp_proj` in the same ingest pass. |

## Key concepts & invariants

**Surveys are versioned, never overwritten.** Depths change constantly (shoaling, dredging). Each upload inserts a new `depth_survey` row. The `active` flag lets an operator disable a bad import without losing the audit trail. `controlling_depth_over` reads the latest `active` survey by `surveyed_at DESC, id DESC`.

**Raw soundings are not stored.** A condition survey is ~400k points. Only the reduced profile (`depth_segment` / `depth_cell`) lives in the DB. The `TEMP TABLE` used during ingest is `ON COMMIT DROP`.

**The gate blocks (422), warns, or passes — never invents a depth.**
- Block (422): `depth_shortfall > 0` and no operator override flag.
- Warn (pass with warning): `controlling_depth_over` returns `(None, None)` — no active survey covers the station range, or the vessel's draft is unknown, or the reservation has no assigned station range. A warn never prevents confirmation; it records the caveat in `notes`.
- Pass silently: depth clears.

Used by `app/edit._depth_gate` on every `PATCH /reservations/{id}` that transitions `status → confirmed`.

**Tide is not modelled.** The gate compares draft + clearance directly to the stored sounding depth. The clearance margin (`Settings.depth_clearance_ft`, default configurable) must absorb tidal uncertainty. The datum is recorded as free text (`MLLW`, etc.) but not modelled further.

**`depth_cell` is visualization-only; `depth_segment` is the gate's source of truth.** Because both tables are populated from the same `tmp_proj` in a single ingest pass, `min(depth_cell.controlling_depth_ft)` over a station always equals `depth_segment.controlling_depth_ft` for that station. Do not change either table's binning independently.

**CRS mismatch detection.** If the soundings' SRID is wrong or the file doesn't cover the wharf, no rows survive the berthing-zone clip and `import_survey` raises `ValueError`. A survey that passes this check is guaranteed to cover at least some of the wharf.

## Connections

- **Upstream (data):** `.XYZ` files uploaded via `POST /depth/surveys` (browser) or `python -m app.depth.ingest` (CLI). Source CRS must be EPSG:2278 by default (operator can override `--srid`).
- **Upstream (geometry):** `wharf_segment` table (centerline for `ST_InterpolatePoint`, apron for berthing-zone clip).
- **Downstream:** `app/edit._depth_gate` calls `controlling_depth_over` + `depth_shortfall` on every confirm transition. `app/feasibility.py` calls the same gate per candidate berth to label each `ok`/`shallow`/`unknown`.
- **API surface:** `POST /depth/surveys` (upload), `GET /depth/surveys` (list), `DELETE /depth/surveys/{id}` — wired in `app/routers/depth.py`.
- **Config:** `Settings.depth_bin_ft`, `Settings.depth_toe_offset_ft`, `Settings.depth_max_offset_ft`, `Settings.depth_offset_bin_ft`, `Settings.depth_clearance_ft`.
- **Tests:** `tests/test_depth.py` — pure `parse_xyz` + `depth_shortfall`; DB-marked tests for PostGIS reduction + gate 422/override path.
