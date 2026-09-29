# Frontend Reference

*Reference for `app/static/index.html`, `app/static/js/`, `app/static/gis/`. POPA wharf data layer — roadmap → [`../PLAN.md`](../PLAN.md); design contract → [`../../CLAUDE.md`](../../CLAUDE.md).*

## Purpose

A read-only and write Leaflet UI served by `app/main.py` at `/` as a static mount.
The page combines an aerial-imagery map (vessel dots, to-scale hull outlines,
berth polygons, station ticks, controlling-depth overlay, feasibility bands) with
a resizable sidebar (stat tiles, conflicts, AIS verification, "alongside now", the
berth occupancy Gantt timeline, vessel list, berth-request forms, depth-survey
management, and history). All data comes from the FastAPI backend; the UI never
does stationing math and never writes directly to the database.

The page started as a single large inline `<script>` in `index.html` and was
refactored into plain ES modules loaded via a single `<script type="module">`
entry point (`app.js`). No build step: modules are delivered straight to the
browser alongside Leaflet (CDN `<script>` tags run before the deferred module,
so `L` is available globally when any module evaluates).

## Files

### Entry point and HTML

| File | What it does |
| --- | --- |
| `app/static/index.html` | Single-page application. Declares the CSS custom-property design-token palette, the sidebar/map/timeline layout, all panel `<div>` targets, and the two `<script type="module">` entry line (`app.js`). Leaflet and the rotate plugin are loaded via CDN `<script>` tags before the module. Contains the tutorial modal markup. |

### ES modules (`app/static/js/`)

| Module | Responsibilities |
| --- | --- |
| `app.js` | Entry point only. Wires sidebar resize/collapse (persisted in `localStorage`), the header dual-clock (Central / UTC), the tutorial modal, and runs the boot sequence. Calls all `load*` functions once at startup then polls every 15 s: stats, positions, timeline, conflicts, verification, alongside, workers. |
| `api.js` | Shared kernel — no DOM, no Leaflet. `api()` GET helper (surfaces FastAPI's `detail` on errors); `apiWrite()` JSON write helper; `formPayload()` form extractor; `errorDetail()` body flattener. Design-token palette (`PAL`, `STATUS_COLORS`, `BADGE_COLORS`) read once from CSS custom properties. Central-time helpers: `centralParts`, `isoToLocalInput`, `localInputToIso`, `fmtCentral`, `CENTRAL_TZ`. AIS type mapping: `shipTypeCategory`, `SHIP_CATEGORIES`, `shipTypeLabel`, `isTugType`, `NAV_STATUS`. Unit conversion: `FT_PER_M`. Station formatter: `fmtSta` (plus+decimal STA notation). HTML escaper: `esc`. |
| `state.js` | Shared mutable state object. Four fields: `centerline` (coords + POPA station array from `centerline.geojson`, drives `stationToLatLon`); `berthSta` (berth name → `[lo, hi]` POPA ft, shared by map and timeline); `mooredSog` (knot threshold from `/config/bbox`); `vessels` (last vessel list, for the edit form). |
| `map.js` | Leaflet map, all map layers, and the controlling-depth and feasibility overlays. Initialises the map with `leaflet-rotate` so the wharf face runs left-to-right; auto-derives and applies the wharf bearing from the centerline endpoints. Layers: basemap (ArcGIS World Imagery), wharf segment, berths, warehouses, centerline (faint), POPA station ticks (`feet_markers.geojson`), Dock No. ticks (`yellow_markers.geojson`), AIS contact dots (`vesselLayer`), to-scale vessel outlines (`shipLayer`), controlling-depth band (`depthLayer`), feasibility slots (`feasibilityLayer`). Exports: `stationToLatLon` (POPA ft → `{lat, lon}`, interpolated along the centerline); `renderOutlines` (called by the timeline cursor to draw hull polygons); `loadDepthOverlay` (called by `depth.js` after upload/delete); `renderFeasibility` / `clearFeasibility` / `highlightFeasSlot` / `clearFeasHighlight` (feasibility oracle picker). `loadPositions` fetches `/positions/recent` — the server's recency gate already drops stale contacts, so no second cutoff here. `locateVesselOnMap` jumps the chart to a vessel and flashes a halo. |
| `timeline.js` | Berth occupancy Gantt (SVG). Fetches `/reservations` for the active time window; draws berth lanes (built from `state.berthSta`) + reservation bars with greedy sub-row packing. Axis ticks snap to Central midnight/hour boundaries (not UTC). Features: draggable time cursor (persisted in `localStorage`) that calls `renderOutlines` to preview the schedule; play/scrub at configurable speed; ±3d / this week / 7d / 30d presets + custom pan; hide-cancelled toggle; collapsible + resizable drawer. Observed (AIS) rows are excluded — the timeline shows only request-originated reservations. |
| `panels.js` | Sidebar panels. `loadStats` — six stat tiles from `/stats` + system-status dot/footer. `loadWorkers` — per-worker health chips from `/workers` (abbreviated name, age, detail). `loadConflicts` — conflict cards from `/conflicts`; clicking a card draws a red polyline on the quay over the overlapping station range and zooms to it; drives the full-width alert strip. `loadVerification` — calls `POST /verification/sweep` (which auto-archives stale rows and returns the fresh payload); renders planned rows (arrived / no-show / awaiting, flagged discrepancies first) + unplanned arrivals; "berthed elsewhere" cards are clickable to highlight the observed range. `loadAlongside` — moored vessels from `/occupancy/moored`; click locates on map; hover shows the ship-dossier panel from `history.js`. Service-craft toggle (one checkbox, re-fetches both conflicts and verification). |
| `forms.js` | Write surfaces. `loadVessels` — vessel table from `/vessels`, rendered in its own **Saved ships** sidebar tab (`data-panel="ships"`; moved out of Overview) and refreshed on tab activation; click a row locates on map; Edit button opens `openVesselEditor` (inline `PATCH /vessels/{id}` form; dims in metres). When a vessel's dims are pinned (`dims_locked`), the editor also shows a **Revert to AIS** button — a `PATCH /vessels/{id}` with `{dims_locked: false}`; the server restores the live loa/beam/draft from the `ais_*` shadow it kept current while pinned (migration 0016), so the real AIS value is back at once (a warning reports whether it restored the shadow or, for a pre-shadow lock, is waiting on the next broadcast). `loadRequests` — reservation list + inline create/edit/confirm/cancel/delete forms (`POST /reservations`, `PATCH`, `DELETE`); "Find berth" button calls `GET /feasibility` and renders the picker with hover-highlight via `map.js`; hover picker row confirms on select. `loadBerthRequests` — berth-request cards from `/intake/berth-requests`; Edit opens the request edit form; Delete soft-deletes the intake row. The berth-request create/edit form (`editBerthRequest`, exported) handles AIS-override warnings and the **easy manual-override** flow: an AIS-tracked vessel's LOA/beam/draft show **editable and tinted** (`.ais-dim`, flipping to `.ais-dim-edited` when changed), and overtyping one arms a single **confirm-to-pin** on save (`offerOverrideConfirm` → `applyManualOverride` → `PATCH /vessels/{id}` with `dims_locked: true`); declining (or a prior override riding through an unrelated edit) keeps the quiet red "Manual override" button (`renderManualOverride`). The vessel editor (`openVesselEditor`) mirrors this — changing a dim with the override box unticked prompts one confirm and auto-pins, so the operator needn't know to tick first. **IMO auto-fill (create mode only):** a debounced listener on the IMO input fires `GET /vessels/lookup?imo=` once the value passes the client `isValidImo` check, then pre-fills name + LOA/beam/draft — blanks only, except an AIS-tracked vessel's dims, which fill + flag as `.ais-dim` (editable, confirm-to-pin on change); a miss shows "not on file — enter manually". Station ranges are entered in Dock No. feet (the server converts to POPA). |
| `history.js` | History tab. Fetches `/history`; renders a booking log (all statuses, including observed berthings). Hovering a card with a `vessel_id` triggers `showShipHover` — floats a dossier panel beside the card from `GET /vessels/{id}` (full vessel record + reservation mini-timeline + latest AIS fix + compass SVG for heading/COG). Cache per vessel so repeated hovers don't re-fetch. |
| `depth.js` | Depth-survey management panel. Lists surveys from `/depth/surveys` (active badge, source file, point count, controlling min). Upload form: POSTs an `.XYZ` file to `/depth/surveys`; after success calls `refreshDepthOverlay` so the map updates if the layer is currently shown. Delete: `DELETE /depth/surveys/{id}`. Survey dates and `created_at` are rendered in Central. |

### Static GeoJSON (`app/static/gis/`)

| File | Content |
| --- | --- |
| `centerline.geojson` | Derived wharf centerline LineString. Vertex coordinates carry a `stations` array (POPA ft per vertex) used by `stationToLatLon`. Drives the map bearing. |
| `berths.geojson` | Berth polygons (berthing-water rectangles from the ArcGIS export). Rendered as dashed polygons with berth-name tooltips. |
| `berth_stations.json` | Berth name → `[sw_station, ne_station]` POPA ft. Loaded into `state.berthSta` and used by both the map popups and the timeline lanes. |
| `feet_markers.geojson` | POPA station ticks (perpendicular to the quay, into the channel, every 100 ft). Toggled off by default in the layer control. |
| `yellow_markers.geojson` | Dock No. ticks (on the apron, landward direction, every 50 ft; majors labelled). On by default. Mirrors the port's "Wharf Stationing with Aerial" exhibit. |
| `warehouses.geojson` | Warehouse footprints rendered as dark semi-transparent polygons with labels. |
| `apron.geojson` | Apron polygon (water-side berthing zone). Not rendered directly by the UI but present for reference. |

## Key concepts & invariants

- **UI is thin over the API.** The data layer is the product. All stationing conversions happen server-side (`app/crosswalk.py`); the UI sends and receives Dock No. feet for station ranges but never runs the affine math itself.
- **One "still here" recency gate on the server.** `/positions/recent` applies the `berth_stale_close_min` cutoff against the feed clock before responding; the client draws whatever it receives without a second staleness filter. This matches the occupancy worker's stale-close logic — one definition, not two.
- **Station ranges shown and entered in Dock No. feet.** Reservation forms use `station_lo` / `station_hi` as Dock No. ft; the server converts to canonical POPA on store. `/reservations` returns both `station_lo/hi` (POPA) and `station_lo/hi_dock` for display. The map outlines are computed from POPA.
- **Vessel dimensions in metres.** The canonical store is metres; feet equivalents are display-only (`FT_PER_M = 3.280839895`). The vessel edit form takes metres; the vessel table shows feet.
- **Central Time everywhere in the UI.** `isoToLocalInput` and `localInputToIso` round-trip through Central without attaching a zone to `datetime-local` values (the server stamps them Central via `app/tz.py`). The timeline axis ticks snap to Central day/hour boundaries, not UTC, to avoid the "gridline on yesterday" bug.
- **No build step.** Plain ES modules + Leaflet CDN. `app.js` is the only `<script type="module">` in `index.html`. The classic Leaflet `<script>` tags run first so `L` is available globally.
- **`shipTypeCategory` in `api.js` mirrors `app/shiptypes.py`.** The UI buckets (tug/towing/pilot, 33=dredger excluded from service-craft set) must stay in sync with the server-side set; `test_shiptypes.py` pins the membership.
- **Timeline excludes observed rows.** The Gantt shows only request-originated reservations; observed (AIS-derived) occupancy is live AIS ground truth, not a booking, and is shown on the map as hull outlines (`outlineMode = "current"`) instead.
- **Feasibility oracle: proposes, never places.** The picker calls `GET /feasibility`, shows candidates on the map, and confirms on click — it does not assign a station range itself.

## Connections

- Served by `app/main.py` as a static mount; auth middleware (`app/auth.py`) covers the static mount — the `/` path is gated like every other route.
- All data from the FastAPI routers: `/stats`, `/positions/recent`, `/vessels`, `/reservations`, `/conflicts`, `/verification`, `/occupancy/moored`, `/intake/berth-requests`, `/depth/surveys`, `/depth/profile`, `/history`, `/workers`, `/feasibility`, `/wharf-segments/geojson`, `/config/bbox`, `/berths`.
- `stationToLatLon` is the client-side inverse of `app/crosswalk.geo_to_station`; it uses the `stations` array embedded in `centerline.geojson`.
- Deployment: see [`deployment.md`](deployment.md).
- Test suite: the UI has no automated tests; the API it calls is tested in `tests/`. See [`tests.md`](tests.md).
