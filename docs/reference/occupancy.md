# Occupancy Derivation

*Reference for `app/occupancy/`. POPA wharf data layer — roadmap → [`../PLAN.md`](../PLAN.md); design contract → [`../../CLAUDE.md`](../../CLAUDE.md).*

## Purpose

Build step 5 turns raw AIS position fixes into `observed` reservations — concrete station ranges and time windows that represent real vessels alongside the wharf. The output is the system's ground truth: the AIS-derived occupancy record that populates the map, the timeline, and the conflict surface before any operator has entered a single booking.

Each derived reservation is an idempotent upsert keyed on a stable `derived_key` (`v{vessel_id}:{t_start}`), so re-running the derivation over the same data updates existing rows rather than duplicating them. Derived rows are always `status='observed'` / `source='ais'` and are **intentionally allowed to overlap planned reservations** — that overlap is the signal step 6 surfaces, not an error condition.

## Files

| File | What it does |
| --- | --- |
| `app/occupancy/alongside.py` | The "is this position at the quay?" predicate and sample-loading query. `alongside_sql()` returns a SQL `CASE` expression: if the nearest `wharf_segment` has a digitized apron polygon (`apron IS NOT NULL`), uses `ST_Contains(apron, point)`; otherwise falls back to `ST_DWithin(centerline, point, buffer_m)`. `nearest_segment_lateral()` builds the `LEFT JOIN LATERAL` that finds the nearest segment. `load_samples()` runs the full query: pulls all `position_report` rows (optionally filtered by segment and `since`), classifies each as `alongside`, and returns them grouped by `vessel_id`. |
| `app/occupancy/detect.py` | Pure berthing-state detector (no DB). `detect_berthings()` reduces a single vessel's time-ordered `Sample` list to a list of `BerthingEvent` objects using two-threshold hysteresis: enter when `sog <= enter_sog` sustained for `dwell`; exit only after `sog > depart_sog` or not-alongside sustained for `depart_gap`. AIS `nav_status` (5 = moored, 1 = at anchor) relaxes the speed bar slightly but is never the sole signal. Trailing open-ended segments are closed at the last fix when stale against the feed clock (`as_of` / `stale_after`). |
| `app/occupancy/project.py` | Pure geometry: `project_bow_stern()` reconstructs hull endpoints from the antenna fix. Bow = antenna + `dim_a` metres along `heading`; stern = antenna + `dim_b` metres along `heading + 180°`. Uses the spherical earth formula (adequate at vessel scale). Falls back to a symmetric `loa/2` split when individual dim offsets are missing, and marks the result `confident=False` when heading is unavailable. |
| `app/occupancy/derive.py` | Orchestrates the full pipeline: load samples → detect berthing events → project bow/stern → convert to POPA station via `crosswalk.geo_to_station` → upsert `reservation`. Also computes the feed clock (`max(msg_ts)` over all `position_report`), passes it to `detect_berthings` as `as_of`, and applies the `stale_after` threshold so departed-during-outage vessels get their events closed. Does not commit — the caller owns the transaction. |
| `app/occupancy/run.py` | Runnable entry point (`python -m app.occupancy.run`). Calls `derive_observed()`, then `expire_stale()` (the step-7 auto status-mutation sweep), commits once, and beats `app/workers.beat("occupancy", …)`. Accepts `--since` and `--segment-id` flags. |

## Key concepts & invariants

**Feed clock, not wall clock.** Stale-berthing closure uses `max(msg_ts) over position_report` as the reference instant, never `datetime.now()`. If the AIS feed goes down, no new fixes land anywhere, the feed clock stops advancing, and no trailing segments are closed — silence of the whole feed is an outage, not departure evidence. This is the same principle as the verification sweep's `feed_alive` gate.

**One shared "still here" recency threshold.** The map's live-position dots (`GET /positions/recent`), the moored-vessel stat (`GET /stats`), and the "Alongside now" panel (`GET /occupancy/moored`) all filter on `msg_ts >= feed_clock - berth_stale_close_min`. The same threshold drives stale-segment closure in `detect_berthings`. A live surface must not define its own staleness rule.

**`observed` rows are allowed to overlap planned reservations.** The DB exclusion constraint is `WHERE (status = 'confirmed')` — `observed` rows are intentionally exempt. An observed-vs-planned overlap is the conflict service's signal (step 6), not something to prevent at ingest/derivation. Observed-vs-observed pairs are also never reported as conflicts (AIS cannot conflict with itself: rafted tugs, projection slop).

**Open-ended time range for still-berthed vessels.** A vessel that is alongside at the last sample gets an upper-unbounded `tstzrange` (`[t_start, NULL)`). Capping it at the last fix would make the berthing read as ended. When the vessel departs, the next derivation run re-detects the event as closed and the upsert overwrites with a bounded range.

**`derived_key` idempotency.** The key is `v{vessel_id}:{t_start.isoformat()}`. The `ON CONFLICT` upsert updates `station_range`, `time_range`, `direction`, and `notes` in place. Planned reservations never set `derived_key` (the partial unique index is `WHERE derived_key IS NOT NULL`).

**Alongside predicate: apron polygon preferred.** Migration 0005 added `wharf_segment.apron` (a digitized water-side berthing-zone polygon). When seeded, `ST_Contains(apron, point)` is tighter and more accurate than a symmetric centerline buffer. The buffer fallback is used for any segment without a seeded apron, so the code is backward-compatible.

**`confident` flag.** A `BowStern.confident=False` result (missing heading and COG) causes the projector to centre an LOA-wide interval on the antenna instead. The derived reservation notes "low confidence (no heading/dims)". The station range is approximate and the `confirmed`-only exclusion constraint does not apply to `observed` rows.

**Direction convention.** `UPSTREAM_TOWARD_INCREASING_STATION = True` in `derive.py`: a bow toward higher POPA station is `upstream`. This is a single flag; flip it if port convention turns out to be reversed.

**The occupancy worker also runs the verification sweep.** `run.py` calls `expire_stale()` in the same batch and transaction, so stale planned rows are auto-archived at the same cadence that new observed rows land.

## Connections

- **Upstream (data):** `position_report` table, populated by the AIS ingestor — see [`ais.md`](ais.md). `vessel` table (for `dim_a`, `dim_b`, `loa` lookups).
- **Upstream (geometry):** `wharf_segment` table (centerline + apron). `app/crosswalk.geo_to_station` converts lat/lon to POPA station via `ST_InterpolatePoint`.
- **Downstream:** `reservation` table (`status='observed'`, `source='ais'`). Conflict detection (`app/conflicts.py`, `GET /conflicts`) reads these rows. Verification (`app/verification.py`, `GET /verification`) matches them against planned rows.
- **Also calls:** `app/verification.expire_stale()` in the same batch run.
- **Config:** `Settings.berth_buffer_m`, `Settings.berth_enter_sog_kn`, `Settings.berth_depart_sog_kn`, `Settings.berth_dwell_min`, `Settings.berth_depart_gap_min`, `Settings.berth_stale_close_min`.
- **Migration:** 0002 adds `vessel.dim_a`/`dim_b` and `reservation.derived_key`.
