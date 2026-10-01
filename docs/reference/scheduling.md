# Scheduling & Analysis Logic

*Reference for `app/conflicts.py`, `app/feasibility.py`, `app/verification.py`, `app/edit.py`, `app/shiptypes.py`. POPA wharf data layer — roadmap → [`../PLAN.md`](../PLAN.md); design contract → [`../../CLAUDE.md`](../../CLAUDE.md).*

## Purpose

These modules implement the core scheduling intelligence of the POPA wharf data layer: detecting conflicts between reservations, advising on feasible berth placements, verifying operator plans against observed AIS reality, and providing the manual edit surface through which operators create, correct, and confirm reservations.

The fundamental abstraction is a **rectangle in (time) × (station) space**. A vessel occupies a station interval `[stern_sta, bow_sta]` over `[ETB, ETD]`; a dredging op likewise. A conflict is any two such rectangles that overlap in both dimensions simultaneously. This one primitive covers vessel-vs-vessel, vessel-vs-dredge, and observed-vs-planned collisions.

## Files

| File | What it does |
| --- | --- |
| `app/conflicts.py` | The core overlap primitive. Pure predicates (`time_overlaps`, `station_overlaps`, `reservations_conflict`, `overlap_interval`, `classify`) plus `find_conflicts` — the DB self-join that surfaces all current overlap pairs with their intersection rectangle (POPA + Dock No.). Also defines `MOORING_GAP_FT = 75.0`, the advisory Python mirror of migration 0007's `GAP_FT`. |
| `app/feasibility.py` | Read-only feasibility oracle for `GET /feasibility`. Given a planned reservation (vessel + window), computes maximal free station bands (wharf extent minus all obstacles padded by `MOORING_GAP_FT`), snaps them to named berths, depth-checks each candidate slot, and returns discrete "Find berth" options. Never places a reservation. Pure interval math (`subtract_intervals`, `feasible_bands`, `placement_range`, `candidate_slots`) plus the `compute_feasibility` orchestrator. |
| `app/verification.py` | AIS verification of operator placements for `GET /verification`. Matches planned rows (requested/tentative/confirmed) to observed AIS berthings by vessel identity + time overlap; classifies each as `arrived`/`no_show`/`awaiting` and flags `where_planned`. Also surfaces `unplanned` observed berthings. The `expire_stale` function (used by `POST /verification/sweep` and the occupancy worker) auto-archives stale planned rows to `completed`/`cancelled` on evidence. |
| `app/edit.py` | Manual edit surface. Pydantic models `VesselUpdate`, `ReservationCreate`, `ReservationUpdate`. Pure helpers `_station_range`, `_time_range`, `_station_from_bow` (unit-tested). DB functions `update_vessel`, `create_reservation`, `update_reservation`, `delete_reservation` (do not commit — the router owns the transaction). Handles Dock-No.→POPA conversion, AIS-dims authority, `dims_locked` override, `_depth_gate` on confirm. |
| `app/shiptypes.py` | AIS service-craft set shared by `conflicts.py` and `verification.py`. `SERVICE_CRAFT_TYPES = {31, 32, 50, 52, 56, 57}` (tug/towing/pilot); `SERVICE_CRAFT_SQL` is the inlined SQL fragment. Dredgers (33) are deliberately excluded. NULL ship type is never treated as a service craft. |

## Key concepts & invariants

**The single overlap primitive** (`conflicts.py`):
- `time_range` is **half-open `[lo, hi)`** — touching endpoints are NOT a conflict (windows that share only an instant are adjacent, not overlapping).
- `station_range` is **closed `[lo, hi]`** — a shared station endpoint IS a conflict.
- An empty/unassigned station range (`None` bound) **never overlaps** — an unplaced `requested` row raises no false conflict. This is enforced structurally: Postgres's `&&` on an empty `numrange` returns false, and the Python `station_overlaps` predicate short-circuits on any `None` bound.
- Observed-vs-observed pairs are **never** conflicts — a rafted tug or AIS projection slop is not a scheduling decision. The SQL filters `NOT (r1.status = 'observed' AND r2.status = 'observed')`.
- Same-vessel pairs are never conflicts — a vessel sitting in its own planned berth is the verification signal, not a collision.
- **An open-ended `observed` row has an UNKNOWN departure, so it can't conflict with a purely-future plan.** Occupancy derivation leaves the upper time bound NULL while a vessel is still alongside; left as +∞ that open end would overlap every future planned window at the same station (the "test vs Mary Jane Moran" false alarm). The query clamps an open-ended observed row's **effective** upper bound to `now()` (CTE `res.eff_range`, observed-only — a *planned* open-ended row genuinely holds the berth forward and is left unclamped). It therefore conflicts with plans reaching the present but never with a wholly-future one. Because the clamp makes a present conflict's overlap rectangle close exactly at `now()`, the `current_only` filter compares `upper(...) >= now()` (not `>`) so that still-current alert survives. Per-side displayed times stay raw (an ongoing berthing still shows a NULL end); only the join/overlap/`current`/window logic uses `eff_range`.

**The exclusion constraint vs. the query**:
- `no_wharf_overlap` in the DB **blocks** `confirmed`-vs-`confirmed` overlaps at write time (migration 0007, padded by ±37.5 ft half-gap). It is `confirmed`-only on purpose: `observed` rows must be allowed to overlap planned rows — that overlap is the signal to surface, not suppress.
- `find_conflicts` uses the **raw** ranges (no gap padding) and **surfaces** overlaps for the live panel. The gap padding is only relevant at confirm time and in the feasibility oracle.

**`MOORING_GAP_FT = 75.0`** (in `conflicts.py`): an **advisory mirror** of migration 0007's `GAP_FT`. The DB constraint is the single source of truth; this Python constant lets the feasibility oracle pre-check the same gap so an offered berth slot doesn't then 409. It is NOT in `app/config` (where it would look tunable but be ignored by the constraint). If migration 0007's `GAP_FT` ever changes, update this to match.

**Feasibility oracle** (`feasibility.py`):
- Proposes, never places. Returns `candidates` (discrete vessel-sized berth options, up to `MAX_CANDIDATES = 20`) and `bands` (raw free intervals for the map overlay).
- Obstacles include ALL non-cancelled reservations with a non-empty station range overlapping the window, including `observed` AIS rows. The oracle is deliberately conservative: it is better to decline a spot that conflicts with a currently-berthed vessel than to offer one that does.
- Depth is checked via `controlling_depth_over` over each slot's exact footprint, mirroring the confirm gate.
- Bow direction: `upstream` → bow at the high-station end; `downstream` → bow at the low-station end. Mirrors `_station_from_bow` in `edit.py`.

**Verification** (`verification.py`):
- Matches on **vessel identity + time overlap**, NOT station (`&&`). An empty `requested` station range can't match the conflict join, but it can still be verified (the vessel either arrived or didn't).
- `where_planned` (`True`/`False`/`None`): does the observed station range overlap the planned one? `None` when the planned row is unplaced.
- Both the `planned` and `unplanned` payload entries carry `vessel_id` so the "Alongside now" UI panel can join its moored feed to a per-vessel plan-vs-observed badge (additive; the classification logic is unchanged).
- `expire_stale` acts **only on evidence**: `completed` if AIS observed the berthing; `cancelled` for a no-show of a `requested`/`tentative` row when `feed_alive` (any `position_report` landed during the window — proving the feed was up); `flagged` (note only, status stays `confirmed`) for a no-show of a confirmed booking. **No data ≠ no-show**: if `feed_alive` is false, the row is left untouched. A confirmed no-show is flagged exactly once (guarded by `[no-show flag]` in notes).
- Grace period (`config.verification_grace_minutes`, default 720 min / 12h): a row whose window just closed still shows until the grace expires, giving operators time to react before auto-archiving.

**Manual edit surface** (`edit.py`):
- Station bounds are entered in **Dock No. feet** (the painted quay markers) and converted to canonical **POPA feet** via `segment_dockno_params(session).to_popa()`. All stationing math stays in `app/crosswalk.py`.
- Dock No. is reversed relative to POPA: the stern (larger Dock No.) maps to the lower POPA bound. Enter stern in `station_lo`, bow in `station_hi`. An inverted pair raises `ValueError` → 422.
- Vessel dimensions are stored and edited in **metres** (canonical AIS store), not feet.
- For an **AIS-tracked vessel** (has MMSI), `loa`/`beam`/`draft` edits are dropped with a warning (`_strip_ais_dims`) — AIS is authoritative. The **`dims_locked` override** (migration 0015) is the escape hatch: with the lock in force, the manual dims are applied instead and the AIS ingestor stops reverting them. Unlocking hands dimensions back to AIS.
- `_depth_gate` runs only when promoting to `confirmed`: it blocks (422) when draft + `depth_clearance_ft` exceeds the controlling depth over the station range per the latest active survey. `depth_override=True` downgrades the block to a warning.
- `_station_from_bow`: derives the station range from bow position + direction + LOA (used by the promote-from-request path). `_station_range` and `_time_range` are pure helpers with unit tests.
- Session functions (`update_vessel`, `create_reservation`, etc.) do NOT commit — the router's `do_write` owns the transaction boundary.

**Service craft filtering** (`shiptypes.py`):
- Filtered at the **query layer** (`GET /conflicts`, `GET /verification` unplanned list), never at ingest or derivation. The rows still exist; both endpoints accept `include_service_craft=True` to widen the view.
- AIS types 31/32 (towing), 52 (tug), 50 (pilot), 56/57 (ITU-R M.1371 "spare" used by Sabine-Neches towboat fleet). Dredgers (33) are NOT service craft — dredging occupancy is this system's other half.
- `SERVICE_CRAFT_SQL` is a module constant inlined directly into SQL (not user input), so the two queries can't drift.

## Connections

- **Upstream**: `app/crosswalk.py` supplies all stationing conversions (POPA ↔ Dock No.) used by every module here. `app/depth/gate.py` supplies the `controlling_depth_over` / `depth_shortfall` functions used by `feasibility.py` and `edit.py`'s `_depth_gate`. `app/tz.py` supplies `assume_central` used by `edit._time_range`.
- **Downstream**: `app/routers/analysis.py` is the HTTP surface for `conflicts.py`, `feasibility.py`, and `verification.py`. `app/routers/edit.py` is the HTTP surface for `edit.py`. The occupancy worker (`app/occupancy/run.py`) calls `expire_stale` each batch cycle.
- **Sibling reference docs**: `docs/reference/ais.md` (AIS ingestion and occupancy derivation that produces the `observed` rows these modules query), `docs/reference/intake.md` (berth-request intake that produces the `requested` rows these modules verify/conflict-check), `docs/reference/routers.md` (HTTP wiring for these modules).
