# HTTP Routers

*Reference for `app/routers/common.py`, `app/routers/read_only.py`, `app/routers/intake.py`, `app/routers/edit.py`, `app/routers/analysis.py`, `app/routers/depth.py`. POPA wharf data layer — roadmap → [`../PLAN.md`](../PLAN.md); design contract → [`../../CLAUDE.md`](../../CLAUDE.md).*

## Purpose

The `app/routers/` package is the HTTP surface of the POPA data layer. It splits the original monolithic `app/main.py` into concern-grouped `APIRouter` modules; `app/main.py` mounts them all onto the FastAPI app. The routers are thin: they validate HTTP shapes, delegate to the logic modules (`conflicts.py`, `edit.py`, `intake/manual.py`, etc.), and wire the audit trail via `common.do_write`. No business logic lives in the routers themselves.

## Files

| File | What it does |
| --- | --- |
| `app/routers/common.py` | Shared write plumbing: `actor(request)` (derives the authenticated operator from `request.state.operator` set by the auth middleware) and `do_write(session, fn, audit=)` (runs the write function, calls the optional audit hook, commits, and translates `ValueError`→422, `IntegrityError`→409, `LookupError`→404). |
| `app/routers/read_only.py` | Read-only data endpoints: positions, reservations, vessels, stats, history, berth catalog, wharf segments, worker heartbeats, bbox config, DB health. Never mutates rows. |
| `app/routers/intake.py` | Manual berth-request intake: create / edit / delete a request (`intake_event` + projected `requested` reservation), plus the intake audit list. Write paths use `do_write`. |
| `app/routers/edit.py` | Manual edit surface: vessel patch and reservation create / edit / delete (including berth assignment and confirm promotion). Write paths use `do_write`. |
| `app/routers/analysis.py` | Read-only analysis surfaces (conflicts, verification, feasibility) and the write sweep (`POST /verification/sweep`). |
| `app/routers/depth.py` | Depth-survey upload and read. `POST /depth/surveys` ingests a `.XYZ` sounding file; the GETs return the survey catalog and the per-station controlling-depth profile + 2-D cross-section grid. |

## Notable endpoints

### Read-only (`read_only.py`)

| Method + Path | Description |
| --- | --- |
| `GET /health` | Auth-exempt liveness probe (the only exempt path). |
| `GET /health/db` | PostGIS connectivity check. |
| `GET /config/bbox` | AIS bounding box + `moored_sog_kn` threshold for the map. |
| `GET /workers` | Per-worker liveness derived from `worker_heartbeat` rows (ais, occupancy, intake-dataverse). |
| `GET /wharf-segments` | Wharf segment records (name, POPA range, affine params). |
| `GET /wharf-segments/geojson` | Centerline GeoJSON for the map. |
| `GET /positions/recent` | Most recent AIS positions (current only: within `berth_stale_close_min` of feed clock). Each fix carries `alongside` flag and `ship_type`. |
| `GET /occupancy/moored` | Vessels alongside right now — per-vessel detail with POPA station and berth name. Same staleness gate as `/positions/recent`. |
| `GET /reservations` | Reservation list with optional `status`/`from`/`to` filters. Returns POPA + Dock No. station bounds. |
| `GET /history` | Reservation log (all statuses), newest-arrival-first. Accepts `name` / `imo` / `status` / `from` / `to` filters. |
| `GET /berths` | Named berth catalog ordered by station. |
| `GET /vessels` | Vessel list with latest AIS fix per vessel (LATERAL join). |
| `GET /vessels/lookup?imo=` | IMO auto-fill for intake: resolve a vessel's name + LOA/beam/draft (**feet**) from its IMO. Rejects an invalid IMO with 422; a clean miss returns `{found:false}` (not 404). Reads on-file AIS data (Tier 1) via `app/vessel_lookup.py`, falling through to an external provider (Tier 2, stubbed). Registered **before** `/vessels/{id}` so it isn't shadowed. |
| `GET /vessels/search?q=&limit=` | Vessel-name type-ahead for intake (phone callers give a name, not an IMO): on-file ships whose name contains `q` (or whose IMO starts with an all-digit `q`), one row per **structurally valid** IMO (most-recently-updated wins, same as lookup), name-prefix matches first then most recently seen. Returns `{results: [{imo, mmsi, name, loa_ft, beam_ft, ship_type, ais_tracked, last_seen}]}`; `q` under 2 chars → empty list (200). `limit` 1–20, default 8. Via `app/vessel_lookup.search_onfile`; also before `/vessels/{id}`. |
| `GET /vessels/{id}` | Full vessel detail: record + reservation log + latest AIS position. |
| `GET /stats` | Sidebar headline counts (vessels present, arrivals in 24h, berth requests, reservations by status, moored now, confirmed). |
| `GET /geo-to-station` | Project lat/lon → POPA station + Corps + Dock No. equivalents. |

### Intake (`intake.py`)

| Method + Path | Description |
| --- | --- |
| `POST /intake/berth-request` | Create a manual berth request. Idempotent on duplicate content. 422 if IMO already belongs to a different ship; 409 on confirmed overlap. Returns `ais_overrides` when AIS-tracked vessel dims were dropped. |
| `PATCH /intake/berth-requests/{id}` | Edit a manual berth request in place (the one sanctioned raw mutation). Editable-channel rows only (`phone/email/operator/ai`). |
| `DELETE /intake/berth-requests/{id}` | Soft-delete a manual berth request (keeps audit row, drops reservation). Editable-channel rows only. |
| `GET /intake/berth-requests` | Raw `intake_event` list, newest first. Each row includes `vessel` dims in feet + `ais_tracked` flag for the edit-form prefill, last-writer `updated_at`/`last_actor` (migration 0017), and `possible_duplicate` — a same-IMO/overlapping-window twin request, derived live (mirrors `_find_duplicate`), null when none. `include_deleted=true` shows the full audit trail. |

### Edit (`edit.py`)

| Method + Path | Description |
| --- | --- |
| `PATCH /vessels/{id}` | Correct a vessel record (authoritative: provided fields overwrite). For AIS-tracked vessels, `loa`/`beam`/`draft` are dropped unless `dims_locked=true` is also set. |
| `POST /reservations` | Create a reservation. Station bounds in Dock No. feet (omit for unassigned). `confirmed` engages the draft gate and the exclusion constraint. |
| `PATCH /reservations/{id}` | Edit a reservation: time window, berth (by `berth_id` or explicit `station_lo/hi` Dock No. feet, or `bow_dock` + `direction` for promote-from-request), status, type, cargo, notes. Cancelling clears the station range. |
| `DELETE /reservations/{id}` | Hard-delete a reservation. |

### Analysis (`analysis.py`)

| Method + Path | Description |
| --- | --- |
| `GET /conflicts` | Conflict pairs (time AND station overlap), each with the intersection rectangle. Live-alert defaults: `current=true`, `service_craft=false`. Accepts `status`, `from`, `to`, `limit`. |
| `GET /verification` | AIS verification of operator placements: `planned` list (`arrived`/`no_show`/`awaiting` + `where_planned`) and `unplanned` observed berthings. Live-alert defaults: `current=true`, `service_craft=false` (for the unplanned list only). Read-only. |
| `GET /feasibility` | Free station bands + discrete candidate berths for a planned reservation. 404 if reservation missing; 422 if vessel/LOA/window unknown. |
| `POST /verification/sweep` | Auto-archive stale planned rows (on evidence), then return the `GET /verification` payload plus an `expired` list. The write companion to `GET /verification`. |

### Depth (`depth.py`, prefix `/depth`)

| Method + Path | Description |
| --- | --- |
| `POST /depth/surveys` | Upload a `.XYZ` sounding file (raw text body, metadata in query string). Ingests via `import_survey` → `depth_survey` + `depth_segment` + `depth_cell` rows. 422 if no sounding falls in the berthing zone. |
| `GET /depth/surveys` | All depth surveys, newest first. |
| `GET /depth/profile` | Per-station controlling-depth profile for one survey (default: latest active). Returns `bins` (1-D station segments) and `cells` (2-D station × offset grid for the cross-section overlay). |
| `DELETE /depth/surveys/{id}` | Delete a depth survey (cascades to its segments). |

## Key concepts & invariants

**`do_write` is the sole commit path for all mutations.** Every write endpoint delegates to `do_write(session, fn, audit=callback)` in `common.py`. It:
1. Calls `fn()` (the session-touching logic function — does NOT commit).
2. Calls `audit(result)` if provided; if it returns a non-`None` dict, calls `record_audit(session, **spec)` to land one `audit_log` row **in the same transaction**.
3. Calls `session.commit()`.
4. Translates `ValueError` → 422, `LookupError` → 404, `IntegrityError` → 409 (with human-readable detail for `no_wharf_overlap`, `uq_intake_event_dedupe_key`, MMSI unique, and `vessel_requires_mmsi_or_imo`).

The audit callback returns `None` to skip logging a no-op (deduped re-submission, 404, empty sweep). Only actual changes are logged.

**Every mutating endpoint writes one `audit_log` row in the same transaction.** The actor is `request.state.operator` (set by `app/auth.BasicAuthMiddleware` from the HTTP Basic credentials; `None` when auth is open). With a single shared credential this records the configured `OPERATOR_USER`. The `detail` JSONB carries context: changed-field list, pre-edit raw, depth-override warnings, or swept reservation IDs.

**Auth is whole-app middleware, not per-route.** `app/auth.BasicAuthMiddleware` (in `app/auth.py`) gates every path except `/health`. Routers have no per-route auth dependencies. This is the only approach that also covers the mounted static map (`/`, `/static/*`).

**Routers are thin; the logic modules own the work.** A router function: validates HTTP input, calls the logic function, wires the audit hook, returns the result. No SQL, no business logic, no stationing math in routers. Stationing conversions happen in `app/crosswalk.py` (called from the logic modules, never from routers directly).

**Live-alert defaults on `GET /conflicts` and `GET /verification`.** Both endpoints default to `current=true` (drop pairs/rows whose overlap ended before now) and `service_craft=false` (hide harbor tug/towboat/pilot pairs). An explicit `from`/`to` window turns `current_only` off (a historical query means the past on purpose). These are filtering conveniences at the query layer; the underlying rows are never suppressed at ingest or derivation.

**`POST /verification/sweep` is the write companion to `GET /verification`.** The **occupancy worker** calls the sweep to self-heal (auto-archive stale planned rows on evidence); the UI's "Alongside now" panel reads the plain `GET /verification` (stale archiving is already covered by the worker, so the panel stays read-only). The sweep records a single `audit_log` row only when it actually moved rows; no-op sweeps are not logged.

`GET /verification` includes `vessel_id` on both the `planned` and `unplanned` entries so the "Alongside now" panel can join its moored feed to a plan-vs-observed badge by vessel. `GET /occupancy/moored` likewise carries `imo` and `ship_type` for that panel's per-ship craft status bar, and `GET /reservations` carries read-only display extras (`created_at`, last-writer `updated_at`/`last_actor`, the linked vessel's effective dims via `_vessel_dims`, and the linked `intake_event.raw`) so the reservation cards reuse the berth-request card layout. All additive and null-safe. Every mutating write threads the authenticated `actor(request)` into the write function so `last_actor`/`updated_at` are stamped in the write's own transaction (NULL actor = agent/AIS/uncredentialed).

**Station bounds in responses are always dual: POPA + Dock No.** `station_lo`/`station_hi` are canonical POPA feet; `station_lo_dock`/`station_hi_dock` are Dock No. feet, converted server-side through the wharf segment's affine params. The UI never does stationing math.

## Connections

- **Upstream**: `app/main.py` mounts all six routers and adds middleware (auth, CORS-like, OperationalError handler). `app/db.py` provides `get_session` (FastAPI dependency). `app/auth.py` provides the BasicAuthMiddleware that sets `request.state.operator`.
- **Logic modules**: `app/conflicts.py`, `app/feasibility.py`, `app/verification.py` (wired by `analysis.py`); `app/edit.py` (wired by `edit.py`); `app/intake/manual.py` (wired by `intake.py`); `app/depth/ingest.py` + `app/depth/parse.py` (wired by `depth.py`).
- **Sibling reference docs**: `docs/reference/scheduling.md` (conflict/feasibility/verification logic), `docs/reference/intake.md` (intake pipeline logic), `docs/reference/ais.md` (AIS ingestion and occupancy logic).
