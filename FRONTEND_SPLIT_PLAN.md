# Frontend split plan — `app/static/index.html` → ES modules

**Executed.** The inline `<script>` is now `app/static/js/{api,state,map,timeline,
panels,history,forms,app}.js`, loaded by a single `<script type="module"
src="/static/js/app.js">`. The line numbers below reference the pre-split
`index.html` and are kept as a record of where each piece came from.

No framework, no build step. Plain ES modules loaded with
`<script type="module">`. Target Leaflet stays a classic CDN `<script>` (sets the
`L` global), which executes before the deferred module graph — modules read `L`
as a global, unchanged.

## What we're splitting

`index.html` today: `<style>` (20–703), markup (704–1048), Leaflet/leaflet-rotate
CDN tags (1049–1054), and **one ~2,250-line inline `<script>` (1055–3305)**. Only
the script moves; `<style>` and markup stay in `index.html`.

## Why it's coupled today (the three mechanisms to replace)

1. **Function hoisting in one scope.** e.g. the timeline IIFE (~2820) calls
   `centralParts` / `CENTRAL_TZ` defined ~1850, and the boot block (3293) calls
   `loadTimeline` defined above it. One script scope makes order-independence
   free; modules must make these explicit imports.
2. **Shared top-level mutable state.** A handful of `let`/`const` bindings are
   written by one block and read by others (see inventory below).
3. **`window.*` bridges — only 3, and none are needed by inline HTML.** A grep
   confirms zero `onclick=`/`onchange=` in generated markup, so every bridge can
   become a normal `import`:
   - `window.onTimeCursor` (timeline → vessel outlines), set at 2812
   - `window.stationToLatLon` (vessel outlines → conflict highlighter), set 2730
   - `window.editBerthRequest` (called from the request-card wiring), set 2109

## Shared mutable state inventory (the part to get right)

ES module `export let` gives importers a **live read** binding, but an importer
**cannot reassign** it — only the owning module can. Several of these are
reassigned wholesale on fetch. Use the **state-object pattern**: export a `const`
object and mutate its fields, so any module can both read and update.

| State | Defined | Written by | Read by | Home |
|---|---|---|---|---|
| `map` (const) | 1151 | — | map, outlines, timeline, chrome | `map.js` (export const) |
| `CENTERLINE` (let) | 1163 | `loadReferenceGeo` | outlines, timeline | `state.js` object field |
| `BERTH_STA` (let) | 1159 | `loadReferenceGeo` | timeline `lanes()` | `state.js` object field |
| `shipLayer` + base layers | map region | map setup | outlines `render`, layer toggle | `map.js` |
| `MOORED_SOG` (let) | 1080 | `loadBbox` | `loadPositions` colouring | `state.js` object field |
| `VESSELS` (let) | 1129 | `loadVessels` | edit + new-reservation picker | `state.js` object field |

Recommendation: a tiny `state.js` exporting `export const state = { centerline:
null, berthSta: {}, mooredSog: 0.5, vessels: [] }`. Mutators do `state.centerline
= …`; readers read `state.centerline`. `map` and `shipLayer` live in `map.js`
(never reassigned, only mutated via Leaflet) and are exported directly.

## Target module set

The review named four (`api.js`, `map.js`, `timeline.js`, `forms.js`). The file
has ~7 natural seams; forcing four makes `forms.js` enormous. Recommended set
(the four, plus `panels.js`, `history.js`, `app.js`):

### `api.js` — shared kernel (no DOM, no Leaflet)
Pure helpers + constants every other module imports. From current lines:
- `api` (1056), `apiWrite` (1131), `formPayload` (1140)
- `esc` (1125) — **dedupe**: re-defined locally at 2692 and 2878; export one
- `fmtSta` (1116), `FT_PER_M` (1076)
- `PAL` (1063), `STATUS_COLORS`/`RES_STATUS_COLOR` (1069/1128), `cssVar` (1062)
- `SHIP_CATEGORIES` (1084), `shipTypeCategory` (1103), `isTugType` (1115),
  `shipTypeLabel` (2330)
- date helpers — **move up from the forms region**: `CENTRAL_TZ`, `centralParts`
  (1850), `isoToLocalInput` (1858), `localInputToIso` (1865), `fmtCentral`
  (1413). These are imported by both `timeline.js` and `forms.js`, so they must
  live in the kernel, resolving the hoisting dependency in (1).

### `state.js` — shared mutable state
The `state` object above. Imported by `map.js`, `timeline.js`, `forms.js`,
`panels.js`.

### `map.js` — map, layers, AIS dots, vessel outlines
- `map` init + `shipLayer`/base layers + layer-toggle box (1151–~1350)
- `syncLabelCounter` (1169), `applyWharfBearing` (1175), `aisIcon` (1278),
  `locateVesselOnMap` (1292), `shipLegend` (1332)
- loaders: `loadReferenceGeo` (1615), `loadWharfGeometry` (1650),
  `loadPositions` (1781), `loadBbox` (1605)
- **vessel outlines block** (2674–2813): `stationToLatLon`, `hullFor`, `render`.
  Export `renderOutlines` (replaces `window.onTimeCursor`) and `stationToLatLon`
  (replaces that bridge; `panels.js` imports it for the conflict highlighter).
- map coord/scale readout chrome (3271–3290)
- Exports: `map`, `shipLayer`, `loadReferenceGeo`, `loadWharfGeometry`,
  `loadPositions`, `loadBbox`, `locateVesselOnMap`, `renderOutlines`,
  `stationToLatLon`.

### `timeline.js` — occupancy Gantt drawer
The whole `loadTimeline` IIFE (2815–3213). Convert to `export function
loadTimeline()` (or keep the closure and `export` the returned `load`). Replace
`window.onTimeCursor(tSel, rows)` → imported `renderOutlines(tSel, rows)`.
Imports: `api`, `fmtSta`, date helpers, `PAL`, `STATUS_COLORS` from `api.js`;
`state` (for `berthSta`); `map` (for `invalidateSize`); `renderOutlines` from
`map.js`. Exports: `loadTimeline`.

### `panels.js` — sidebar status / stats / conflicts / verification / alongside
- `setStatus` (1352), `setConflictAlert` (1363), `loadStats` (1383)
- conflicts: `conflictName` (1423), `conflictCard` (1427), `highlightConflict`
  (1442, imports `stationToLatLon` + `map`), `wireConflictCards` (1460),
  `loadConflicts` (1469)
- verification: `verifyPlannedCard` (1500), `verifyUnplannedCard` (1516),
  `loadVerification` (1529)
- alongside: `alongsideCard` (1573), `loadAlongside` (1586)
- Exports: `loadStats`, `loadConflicts`, `loadVerification`, `loadAlongside`,
  `setStatus`.

### `forms.js` — reservations, requests, vessel edit, tabs
- `loadVessels` (1658) + `openVesselEditor` (1718) [writes `state.vessels`]
- `writeError` (1772), `warnHtml` (1777)
- `loadRequests` (1821), `resCard` (1869), `wireResCards` (1954),
  `isValidImo` (2066)
- new berth-request form IIFE (2079) — defines `editBerthRequest`; **export it**
  and import where the request-card wiring calls it (replaces `window.editBerthRequest`)
- request filter IIFE (2195), tab-switching IIFE (2203),
  `loadBerthRequests` (2227), request-actions IIFE (2245), `rawField` (2270),
  `reqCard` (2279), filter IIFE (2319)
- Exports: `loadVessels`, `loadRequests`, `loadBerthRequests`, `editBerthRequest`.

### `history.js` — History tab + ship-detail hover
`kvRow` (2366), `mFt` (2372), `compassSvg` (2379), `resTimeline` (2394),
`renderResNotes` (2427), `shipResRow` (2462), `renderShipDetail` (2486),
`positionShipHover` (2537), `showShipHover` (2551), `hideShipHover` (2577),
`updateHistFilterSummary` (2585), `histCard` (2336), `loadHistory` (2601),
history-modal IIFE (2637). Exports: `loadHistory`.

### `app.js` — entry point (the only `<script type="module">`)
- chrome IIFEs that are pure wiring: sidebar resize (3218), header clock (3242),
  sidebar toggle (3262)
- the **boot block** (3292–3304), as explicit imported calls:
  ```js
  import { loadBbox, loadReferenceGeo, loadWharfGeometry, loadPositions } from "./map.js";
  import { loadTimeline } from "./timeline.js";
  import { loadStats, loadConflicts, loadVerification, loadAlongside } from "./panels.js";
  import { loadVessels, loadRequests, loadBerthRequests } from "./forms.js";

  loadBbox();
  loadReferenceGeo().then(loadTimeline);   // lanes need BERTH_STA first
  loadStats(); loadWharfGeometry(); loadVessels();
  loadRequests(); loadBerthRequests(); loadPositions();
  loadConflicts(); loadVerification(); loadAlongside();
  setInterval(() => { loadStats(); loadPositions(); loadTimeline();
                      loadConflicts(); loadVerification(); loadAlongside(); }, 15000);
  ```

## index.html change

Replace lines 1055–3305 (the inline `<script>`) with, after the existing CDN tags:
```html
<script type="module" src="/static/js/app.js"></script>
```
Keep the Leaflet + leaflet-rotate classic `<script>` tags as-is and before it.

## Gotchas / pre-flight

- **`L` global**: modules use the CDN-provided `L` (and `map.getBearing` etc. from
  leaflet-rotate). Classic scripts run before deferred modules, so `L` is ready.
  Don't `import` Leaflet.
- **`esc` is defined 3×** (1125, 2692, 2878) — collapse to the one in `api.js`.
- **Order dependency `loadReferenceGeo → loadTimeline`** (BERTH_STA before lanes)
  is preserved by the `.then()` chain in `app.js`; don't parallelize those two.
- **`localStorage` keys** (`tlCursor`, `tlCollapsed`, `tlHeight`, `sidebarW`) are
  unchanged — pure string keys, no scope concern.
- **No inline HTML handlers** to preserve (confirmed by grep), so no module needs
  to leak anything onto `window`.

## Verification (no build step / no FE tests today)

1. Serve and open `/`; confirm zero console errors and that the module graph
   resolves (Network tab: `api.js`, `state.js`, `map.js`, … all 200).
2. Smoke each surface: map dots + outlines, timeline drag/play, request
   create/edit/delete, reservation edit/confirm, History modal + ship hover,
   conflicts/verification panels, the 15s poll.
3. Optional follow-on (ties to the deferred CI item): a one-page Playwright smoke
   that loads `/` against the test stack and asserts no console errors — turns
   this from "click everything by hand" into a guard.
