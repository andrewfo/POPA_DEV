// Map, layers, AIS dots, and the to-scale vessel outlines. Leaflet's `L` is the
// CDN-provided global (the classic <script> runs before this deferred module);
// don't import it.
import {
  api, fmtSta, esc, fmtCentral, PAL, STATUS_COLORS, FT_PER_M,
  SHIP_CATEGORIES, shipTypeCategory,
} from "./api.js";
import { state } from "./state.js";

// --- Map -------------------------------------------------------------------
export const map = L.map("map", {
  zoomControl: true,
  rotate: true,                                  // leaflet-rotate
  bearing: 0,
  touchRotate: true,
  rotateControl: { closeOnZeroBearing: false },  // compass to fine-tune/reset
}).setView([29.87, -93.93], 14);

// Rotate the map so the wharf face runs left->right (stations increase to the
// right). Bearing is derived from the centerline endpoints; labels are
// counter-rotated via the --counter CSS var so text stays upright at any bearing
// (including when the user spins the compass control).
function syncLabelCounter() {
  const b = (typeof map.getBearing === "function") ? map.getBearing() : 0;
  document.documentElement.style.setProperty("--counter", (-b) + "deg");
}
map.on("rotate", syncLabelCounter);

function applyWharfBearing(coords) {
  if (typeof map.setBearing !== "function") return;  // plugin failed to load
  const [lon1, lat1] = coords[0];
  const [lon2, lat2] = coords[coords.length - 1];
  const latm = ((lat1 + lat2) / 2) * Math.PI / 180;
  const east = (lon2 - lon1) * Math.cos(latm) * 111320;
  const north = (lat2 - lat1) * 110540;
  const brng = Math.atan2(east, north) * 180 / Math.PI; // NE heading, deg from N
  map.setBearing(90 - brng);                             // put NE end to the right
  syncLabelCounter();
}
// Aerial imagery, to match the port's "Wharf Stationing with Aerial" exhibit.
L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
  maxZoom: 19, className: "basemap-tinted",
  attribution: "Imagery &copy; Esri, Maxar, Earthstar Geographics",
}).addTo(map);

const wharfLayer = L.geoJSON(null, {
  style: { color: PAL.blue, weight: 4, opacity: 0.95 },
  onEachFeature: (f, layer) => {
    const p = f.properties;
    layer.bindPopup(`<b>${p.name}</b><br>POPA ${fmtSta(p.popa_sta_start)} → ${fmtSta(p.popa_sta_end)}`);
  },
}).addTo(map);
const berthLayer = L.geoJSON(null, {
  style: { color: PAL.blue, weight: 1, dashArray: "3 3", fillColor: PAL.blue, fillOpacity: 0.06 },
  onEachFeature: (f, layer) => {
    const p = f.properties;
    const rng = state.berthSta[p.name];
    const span = rng ? `POPA ${fmtSta(rng[0])} – ${fmtSta(rng[1])} (${(rng[1] - rng[0]).toFixed(0)} ft)` : `width ${p.width} ft`;
    layer.bindPopup(`<b>${p.name}</b><br>${span}`);
    if (p.name) {
      layer.bindTooltip(`<span class="lbl">${p.name}</span>`, { permanent: true, direction: "center", className: "berth-label" });
    }
  },
}).addTo(map);
const warehouseLayer = L.geoJSON(null, {
  style: { color: PAL.muted, weight: 1, fillColor: "#1b2935", fillOpacity: 0.4 },
  onEachFeature: (f, layer) => {
    const p = f.properties;
    layer.bindTooltip(`<span class="lbl">${p.label || p.name}</span>`, { permanent: true, direction: "center", className: "wh-label" });
  },
}).addTo(map);
// The quay-face centerline is kept faint — the exhibit shows no drawn line, the
// ticks read off the photo's quay edge. (Still loaded: it drives the bearing.)
const centerlineLayer = L.geoJSON(null, {
  style: { color: PAL.muted, weight: 1.5, opacity: 0.35 },
  pointToLayer: (f, latlng) => L.circleMarker(latlng, {
    radius: 2, color: PAL.muted, fillColor: PAL.ink, fillOpacity: 0.5, weight: 1, opacity: 0.4,
  }),
}).addTo(map);
// Station ticks, straight from feet_markers.geojson: each feature is a
// LineString perpendicular to the quay, running from a nub on the concrete out
// into the channel (built with the real water-side normal, so direction/
// position match the port's "Wharf Stationing with Aerial" exhibit). Drawn as
// thin uniform light lines every 100 ft, labelled in POPA stationing
// (STA "X+YY") at the water end — the same reference the exhibit uses.
const feetLayer = L.geoJSON(null, {
  style: () => ({ color: PAL.inkDim, weight: 1, opacity: 0.8 }),
  onEachFeature: (f, layer) => {
    if (!f.properties.label) return;
    const co = f.geometry.coordinates;
    const end = co[co.length - 1];                 // water end of the tick
    L.marker([end[1], end[0]], {
      icon: L.divIcon({
        className: "ft-label",
        html: `<span class="lbl">${f.properties.label}</span>`,
        iconSize: [0, 0],
      }),
      interactive: false,
    }).addTo(feetLayer);
  },
});  // off by default — toggle on via the "Dredge markers" overlay
// Yellow quay-face ticks, from yellow_markers.geojson — the exhibit's second
// tick series: the port's Dock No. stationing painted on the wharf deck,
// anchored at Dock No. 0 (the NE start) and increasing SW, one tick every 50 ft
// along the whole face. Each tick sits ON the wharf, running from the quay edge
// landward onto the apron (vs. the long POPA lines into the channel); the round
// hundreds carry a yellow Dock No. label on the landward (dock) end, the 50-ft
// minors are drawn thinner and unlabelled.
const yellowLayer = L.geoJSON(null, {
  style: (f) => ({ color: PAL.amber, weight: f.properties.major ? 2 : 1, opacity: 0.95 }),
  onEachFeature: (f, layer) => {
    if (!f.properties.label) return;
    const co = f.geometry.coordinates;
    const a = co[0], b = co[co.length - 1];        // tick endpoints
    const mid = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];   // label rides the middle of the tick
    L.marker([mid[1], mid[0]], {
      icon: L.divIcon({
        className: "yk-label",
        html: `<span class="lbl">${f.properties.label}</span>`,
        iconSize: [0, 0],
      }),
      interactive: false,
    }).addTo(yellowLayer);
  },
}).addTo(map);
// AIS contacts: one dot per vessel (most-recent position, deduped per MMSI),
// filled by ship-type category; underway contacts pulse, moored ones sit still.
// Toggleable as a whole via the "Ship dots (AIS)" overlay in the layer control below.
const vesselLayer = L.layerGroup().addTo(map);
// Fill colour = ship-type category (t-<category> class); the pulse ring is only
// emitted for underway contacts, so a moored dot sits still (see the legend).
// Staleness is NOT decided here: /positions/recent only returns contacts whose
// fix is within berth_stale_close_min of the feed clock (the same recency gate as
// "moored now"), so a vessel that went silent never reaches this layer to draw —
// one server-side definition of "still here", not a second cutoff in the client.
// mode = moored (stopped + alongside POPA: solid, still) | anchored (stopped but
// off-berth — anchored / holding in the channel: hollow, still) | underway
// (moving: solid + pulsing ring). Only "underway" emits the pulse.
const aisIcon = (category, mode) => L.divIcon({
  className: "ais-contact t-" + category + " m-" + mode,
  html: (mode === "underway" ? '<span class="ais-pulse"></span>' : "") + '<span class="ais-dot"></span>',
  iconSize: [14, 14], iconAnchor: [7, 7],
});

// MMSI -> the live AIS contact marker, rebuilt each loadPositions(). Lets a
// sidebar vessel row jump the map to its contact. Separate transient layer for
// the cyan "locate" halo so we can clear it without touching the contacts.
let POSITION_MARKERS = {};
const locateLayer = L.layerGroup().addTo(map);

// Centre the chart on a vessel's most-recent AIS contact and flash a halo over
// it. Returns false when the vessel has no recent position to locate.
export function locateVesselOnMap(mmsi) {
  const m = mmsi != null ? POSITION_MARKERS[mmsi] : null;
  if (!m) return false;
  const ll = m.getLatLng();
  map.setView(ll, Math.max(map.getZoom(), 15), { animate: true });
  m.openPopup();
  locateLayer.clearLayers();
  L.marker(ll, {
    icon: L.divIcon({ className: "locate-halo", html: "<span></span>",
      iconSize: [0, 0] }),
    interactive: false, zIndexOffset: -1000,
  }).addTo(locateLayer);
  setTimeout(() => locateLayer.clearLayers(), 3200);
  return true;
}

// The to-scale vessel-outline layer. Defined here (not inside the renderer block
// below) so it can join the layer-toggle box; the renderer populates it and
// honours its on/off state via map.hasLayer(shipLayer).
export const shipLayer = L.layerGroup().addTo(map);

// Show/hide toggles for the map layers. Each layer (and its child label markers)
// is added to the map above, so unchecking removes the ticks/labels/berths
// together. Collapsed to a small layers icon (top-right) so it doesn't cover the
// water — it expands on hover/click. The two ship representations lead the list:
// the AIS "Ship dots" and the to-scale "Vessel outlines", each shown/hidden
// independently.
L.control.layers(null, {
  "Ship dots (AIS)": vesselLayer,
  "Vessel outlines": shipLayer,
  "Berths": berthLayer,
  "Dock markers (yellow)": yellowLayer,
  "Dredge markers": feetLayer,
}, { collapsed: true, position: "topright" }).addTo(map);

// View-mode toggle (top-left): which to-scale outlines the map draws. "Current"
// = observed vessels moored right now (the live picture, ignores the scrubber);
// "Planned" = confirmed bookings at the timeline cursor (drag the cursor to
// preview the schedule). Master on/off stays the "Vessel outlines" layer above.
// Read by renderOutlines (defined further down); persisted across reloads.
let outlineMode = localStorage.getItem("outlineMode") === "planned" ? "planned" : "current";
const viewToggle = L.control({ position: "topleft" });
viewToggle.onAdd = function () {
  const div = L.DomUtil.create("div", "view-toggle");
  div.innerHTML =
    '<button type="button" data-mode="current" title="Vessels moored right now (AIS)">Current</button>' +
    '<button type="button" data-mode="planned" title="Confirmed bookings at the timeline cursor">Planned</button>';
  const sync = () => div.querySelectorAll("button").forEach((b) =>
    b.classList.toggle("active", b.dataset.mode === outlineMode));
  div.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b || b.dataset.mode === outlineMode) return;
    outlineMode = b.dataset.mode;
    localStorage.setItem("outlineMode", outlineMode);
    sync();
    if (!map.hasLayer(shipLayer)) shipLayer.addTo(map);   // choosing a view implies you want outlines on
    renderOutlines(lastTSel, null);
  });
  sync();
  L.DomEvent.disableClickPropagation(div);
  return div;
};
viewToggle.addTo(map);

// Ship-type colour key (bottom-left). Swatches are generated from
// SHIP_CATEGORIES so they can never drift from the actual dot fills. The title
// is a toggle: it collapses the body to just the chip so the key doesn't cover
// the water. Starts collapsed.
const shipLegend = L.control({ position: "bottomleft" });
shipLegend.onAdd = function () {
  const div = L.DomUtil.create("div", "map-legend collapsed");
  const rows = Object.values(SHIP_CATEGORIES).map((c) =>
    `<div class="lg-row"><span class="lg-dot" style="background:${c.color}"></span>${c.label}</div>`
  ).join("");
  div.innerHTML =
    `<button type="button" class="lg-title" aria-expanded="false">Ship type</button>` +
    `<div class="lg-body">${rows}` +
    `<div class="lg-note">pulsing = underway · solid = moored</div>` +
    `<div class="lg-note">hollow = stopped off-berth</div>` +
    `<div class="lg-note">silent contacts are hidden</div></div>`;
  const title = div.querySelector(".lg-title");
  title.addEventListener("click", () => {
    const open = div.classList.toggle("collapsed") === false;
    title.setAttribute("aria-expanded", String(open));
  });
  L.DomEvent.disableClickPropagation(div);
  return div;
};
shipLegend.addTo(map);

// --- Bounding box + reference geometry -------------------------------------
export async function loadBbox() {
  try {
    const b = await api("/config/bbox");
    if (typeof b.moored_sog_kn === "number") state.mooredSog = b.moored_sog_kn;
    const bounds = [[b.sw.lat, b.sw.lon], [b.ne.lat, b.ne.lon]];
    L.rectangle(bounds, { color: PAL.blue, weight: 1, dashArray: "5 5", fill: false }).addTo(map);
  } catch (e) { /* keep default view */ }
}

// Real port geometry from ArcGIS exports (static GeoJSON — no DB needed).
export async function loadReferenceGeo() {
  try { state.berthSta = await api("/static/gis/berth_stations.json"); } catch (e) { /* optional */ }
  try {
    const wh = await api("/static/gis/warehouses.geojson");
    warehouseLayer.addData(wh);
  } catch (e) { /* optional */ }
  try {
    const berths = await api("/static/gis/berths.geojson");
    berthLayer.addData(berths);
    if (berths.features.length) map.fitBounds(berthLayer.getBounds(), { padding: [30, 30] });
  } catch (e) { /* optional */ }
  try {
    const fm = await api("/static/gis/feet_markers.geojson");
    feetLayer.addData(fm);
  } catch (e) { /* optional */ }
  try {
    const ym = await api("/static/gis/yellow_markers.geojson");
    yellowLayer.addData(ym);
  } catch (e) { /* optional */ }
  try {
    const cl = await api("/static/gis/centerline.geojson");
    centerlineLayer.addData(cl);
    const line = cl.features.find((f) => f.geometry.type === "LineString");
    if (line) {
      applyWharfBearing(line.geometry.coordinates);
      // stations[] (POPA ft per vertex) rides alongside the coords; keep both
      // so station -> lat/lon interpolation can place vessel outlines.
      state.centerline = { coords: line.geometry.coordinates, stations: line.properties.stations };
    }
  } catch (e) { /* optional */ }
}

// Draw the wharf segment geometry onto the map. (The Overview no longer lists
// the segments as cards — the affine params were internal crosswalk math; this
// keeps only the map layer.)
export async function loadWharfGeometry() {
  try {
    const gj = await api("/wharf-segments/geojson");
    wharfLayer.addData(gj);
    if (gj.features.length) map.fitBounds(wharfLayer.getBounds(), { padding: [60, 60] });
  } catch (e) { /* no geometry */ }
}

export async function loadPositions() {
  try {
    const ps = await api("/positions/recent?limit=500");
    vesselLayer.clearLayers();
    POSITION_MARKERS = {};
    const seen = new Set();
    for (const p of ps) {
      if (p.mmsi && seen.has(p.mmsi)) continue; // most-recent per vessel
      if (p.mmsi) seen.add(p.mmsi);
      // (Stale contacts never arrive here — the server's recency gate drops them.)
      // Moored = stopped (SOG below the berth-enter threshold) AND alongside (in
      // the berthing zone, per the server's apron/buffer test). Stopped but NOT
      // alongside = anchored/holding off-berth (still, hollow). Moving = underway.
      const stopped = (p.sog ?? 99) < state.mooredSog;
      const moored = stopped && p.alongside;
      // Three visual states: moored (at POPA), anchored (stopped but off-berth —
      // sits still without pulsing, drawn hollow), underway (moving, pulses).
      const mode = moored ? "moored" : (stopped ? "anchored" : "underway");
      // Dot fill = ship-type category (also labelled in the popup + legend).
      const cat = shipTypeCategory(p.ship_type);
      const catInfo = SHIP_CATEGORIES[cat];
      const stateNote = moored ? " · <b>moored</b>"
        : (stopped ? " · stopped (off berth)" : " · underway");
      // One info block, shared by the click popup and the hover tooltip, so
      // hovering a dot shows exactly what clicking it (or a "Recent vessels"
      // row, which opens this popup) does.
      const info =
        `<b>${p.vessel_name || "(unnamed)"}</b><br>` +
        `<span style="color:${catInfo.color}">●</span> ${esc(catInfo.label)}<br>` +
        `MMSI ${p.mmsi ?? "—"}<br>` +
        `SOG ${p.sog ?? "—"} kn${stateNote}<br>` +
        `${p.msg_ts ? fmtCentral(p.msg_ts) + " CT" : ""}`;
      const marker = L.marker([p.lat, p.lon], { icon: aisIcon(cat, mode) })
        .bindPopup(info)
        .bindTooltip(info, { direction: "top", offset: [0, -10] })
        .addTo(vesselLayer);
      if (p.mmsi != null) POSITION_MARKERS[p.mmsi] = marker;
    }
  } catch (e) { /* no positions */ }
}

// ===== VESSEL OUTLINES (to-scale, time-cursor driven) ==========================
// Real reservations drawn as to-scale ship polygons, positioned by projecting
// their POPA station range onto the wharf centerline. WHICH vessels show is
// driven by the timeline's draggable cursor: the timeline imports renderOutlines
// and calls it with (tSel, rows); we render whoever is alongside at that instant.
// The station -> lat/lon step is the client-side inverse of crosswalk.geo_to_station.
// Two view modes (toggled by the segmented control above the map):
//   "current" — observed (AIS ground truth) vessels moored RIGHT NOW, drawn
//               against the wall clock, independent of the timeline scrubber.
//   "planned" — confirmed bookings at the timeline cursor instant, so dragging
//               the cursor previews where reserved ships will sit.
const CURRENT_STATUS = { observed: STATUS_COLORS.observed };
const PLANNED_STATUS = { confirmed: STATUS_COLORS.confirmed };
const OUTLINE_DREDGE = PAL.dredge;
let VESSEL_DIMS = {};                 // "i"+imo / "m"+mmsi -> { loa, beam } (m)
let lastTSel = null, lastRows = [];

// Beam (hull width) isn't on /reservations; pull it from /vessels by IMO/MMSI.
async function loadDims() {
  try {
    const vs = await api("/vessels?limit=500");
    const m = {};
    for (const v of vs) {
      const d = { loa: v.loa, beam: v.beam };
      if (v.imo != null) m["i" + v.imo] = d;
      if (v.mmsi != null) m["m" + v.mmsi] = d;
    }
    VESSEL_DIMS = m;
  } catch (e) { /* DB offline -> fall back to LOA/7 from the station span */ }
}

// POPA station (ft) -> { lat, lon } on the centerline. Linear interpolation
// over state.centerline.coords paired with .stations (non-decreasing; the
// zero-length step at the stepped bulkhead is skipped). Clamps to the ends.
// Exported so the conflict highlighter (panels.js) can draw on the quay too.
export function stationToLatLon(sta) {
  const C = state.centerline;
  if (!C || !C.coords || C.coords.length < 2 || !C.stations) return null;
  const co = C.coords, st = C.stations, n = co.length;
  let i;
  if (sta <= st[0]) i = 0;
  else if (sta >= st[n - 1]) i = n - 2;
  else { i = 0; while (i < n - 2 && !(sta >= st[i] && sta < st[i + 1])) i++; }
  const lo = st[i], hi = st[i + 1];
  let f = hi > lo ? (sta - lo) / (hi - lo) : 0;
  f = Math.max(0, Math.min(1, f));
  const a = co[i], b = co[i + 1];
  return { lat: a[1] + (b[1] - a[1]) * f, lon: a[0] + (b[0] - a[0]) * f };
}

// To-scale ship ring [[lat,lon],...] spanning [staLo, staHi] along the quay,
// pushed to the WATER side (water-side normal + a fender standoff, matching
// build_centerline's water normal). beamM = width (m); dirUp true => bow toward
// increasing station; dredge => box.
function hullFor(staLo, staHi, beamM, dirUp, isDredge) {
  const A = stationToLatLon(staLo), B = stationToLatLon(staHi);
  if (!A || !B) return null;
  const cLat = (A.lat + B.lat) / 2;
  const mPerLat = 111320, mPerLon = 111320 * Math.cos(cLat * Math.PI / 180);
  let fe = (B.lon - A.lon) * mPerLon, fn = (B.lat - A.lat) * mPerLat;   // lo->hi
  const L = Math.hypot(fe, fn);                         // hull length (m)
  if (L < 1) return null;
  fe /= L; fn /= L;
  const re = fn, rn = -fe;                              // water-side normal
  const stand = 6;                                      // fender standoff (m)
  const taper = isDredge ? 0 : 0.28 * L;
  // local (along from A, cross from line) corners; bow tapered at the +along end
  let pts = [
    [0, stand], [0, stand + beamM],
    [L - taper, stand + beamM], [L, stand + beamM / 2], [L - taper, stand],
  ];
  if (!dirUp) pts = pts.map(([al, cr]) => [L - al, cr]);   // bow at the lo end
  return pts.map(([al, cr]) => {
    const de = al * fe + cr * re, dn = al * fn + cr * rn;
    return [A.lat + dn / mPerLat, A.lon + de / mPerLon];
  });
}

function timeContains(r, tSel) {
  const s = r.t_start ? Date.parse(r.t_start) : -Infinity;
  const e = r.t_end ? Date.parse(r.t_end) : Infinity;     // open-ended -> ongoing
  return s <= tSel && tSel <= e;
}

// Station span [lo, hi] (POPA ft) for the outline. Normally the reservation's own
// stern..bow range — but a degenerate range (bow ≈ stern: projection slop, or a
// moored vessel reported with no length yet) collapses the hull and used to be
// dropped by the `spanFt < 1` guard, so a real moored ship drew nothing. Fall
// back to the vessel's LOA (last-ditch a nominal 100 ft) centred on the reported
// station, so every moored vessel stays drawable.
function outlineSpan(r) {
  const lo = Math.min(r.station_lo, r.station_hi), hi = Math.max(r.station_lo, r.station_hi);
  if (hi - lo >= 1) return [lo, hi];
  const d = r.vessel_imo != null ? VESSEL_DIMS["i" + r.vessel_imo] : null;
  const loaFt = (d && d.loa != null && Number(d.loa) > 0) ? Number(d.loa) * FT_PER_M : 100;
  const mid = (lo + hi) / 2;
  return [mid - loaFt / 2, mid + loaFt / 2];
}

function beamFor(r, spanFt) {
  const d = r.vessel_imo != null ? VESSEL_DIMS["i" + r.vessel_imo] : null;
  let beamM = d && d.beam != null ? Number(d.beam) : 0;   // Numeric may arrive as string
  if (!(beamM > 0)) {                                     // no static data yet
    const loaM = (d && d.loa != null ? Number(d.loa) : 0) || spanFt / FT_PER_M;
    beamM = Math.max(8, (loaM || 30) / 7);                // plausible aspect ratio
  }
  return beamM;
}

// Render whoever is alongside at the cursor instant `tSel`. The timeline imports
// and calls this (was window.onTimeCursor); it also runs on overlayadd below.
export function renderOutlines(tSel, rows) {
  lastTSel = tSel;
  if (rows) lastRows = rows;
  shipLayer.clearLayers();
  // Off in the layer-toggle box, or nothing to place yet -> draw nothing.
  if (!map.hasLayer(shipLayer) || !state.centerline) return;
  const planned = outlineMode === "planned";
  // Current view ignores the scrubber and reads "now" (what's actually moored);
  // planned view follows the timeline cursor so you can preview the schedule.
  const at = planned ? tSel : Date.now();
  if (at == null) return;
  const statuses = planned ? PLANNED_STATUS : CURRENT_STATUS;
  for (const r of lastRows) {
    if (!(r.status in statuses)) continue;
    if (r.station_unassigned || r.station_lo == null || r.station_hi == null) continue;
    if (!timeContains(r, at)) continue;
    const [lo, hi] = outlineSpan(r);
    const spanFt = hi - lo;
    if (spanFt < 1) continue;
    const isDredge = r.type === "dredge";
    const beamM = beamFor(r, spanFt);
    const ring = hullFor(lo, hi, beamM, r.direction !== "downstream", isDredge);
    if (!ring) continue;
    const color = isDredge ? OUTLINE_DREDGE : statuses[r.status];
    L.polygon(ring, {
      color, weight: 2, fillColor: color,
      fillOpacity: r.status === "observed" ? 0.5 : 0.35,
      dashArray: isDredge ? "5 4" : null,
    }).bindPopup(
      `<b>${esc(r.vessel_name || (isDredge ? "Dredging op" : "(unnamed)"))}</b>` +
      (r.vessel_imo ? `<br>IMO ${esc(r.vessel_imo)}` : "") +
      `<br>LOA ≈ ${Math.round(spanFt)} ft · beam ${beamM.toFixed(0)} m` +
      `<br>POPA ${fmtSta(lo)}–${fmtSta(hi)}` +
      `<br><i>${esc(r.type)} · ${esc(r.status)}</i>`
    ).addTo(shipLayer);
  }
}

// Re-draw at the current cursor when the outlines layer is switched back on
// from the layer-toggle box (it was emptied while hidden).
map.on("overlayadd", (e) => { if (e.layer === shipLayer) renderOutlines(lastTSel, null); });
loadDims();
// ===== /VESSEL OUTLINES ========================================================

// --- Map coordinate / scale read-out ---------------------------------------
(function () {
  const foot = document.getElementById("footCoord");
  map.on("mousemove", (e) => {
    const la = e.latlng.lat.toFixed(5), lo = e.latlng.lng.toFixed(5);
    if (foot) foot.textContent = `LAT ${la}  LON ${lo}`;
  });
  map.on("mouseout", () => { if (foot) foot.textContent = "LAT —  LON —"; });
  // Live zoom / scale readout in the classification strip.
  const scaleEl = document.getElementById("classScale");
  if (scaleEl) {
    const upd = () => {
      const z = map.getZoom();
      // metres-per-pixel at the current latitude (Web Mercator).
      const c = map.getCenter();
      const mpp = 156543.03392 * Math.cos(c.lat * Math.PI / 180) / Math.pow(2, z);
      scaleEl.textContent = `ZOOM ${z.toFixed(1)}  ·  ${mpp < 1 ? (mpp*100).toFixed(0)+' CM/PX' : mpp.toFixed(1)+' M/PX'}`;
    };
    map.on("zoomend moveend", upd); upd();
  }
})();
