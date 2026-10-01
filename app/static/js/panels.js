// Sidebar panels: status / stats, conflicts, and the merged "alongside now"
// (who's at the wharf + AIS verification + future planned arrivals).
import {
  api, esc, fmtCentral, fmtSta, PAL,
  shipTypeCategory, SHIP_CATEGORIES, isServiceCraftType,
} from "./api.js";
import { map, stationToLatLon, locateVesselOnMap } from "./map.js";
import { showShipHover, hideShipHover } from "./history.js";

// --- Status / stats --------------------------------------------------------
export function setStatus(ok, text) {
  const dot = document.getElementById("statusDot");
  if (dot) dot.className = "dot " + (ok ? "ok" : "down");
  const txt = document.getElementById("statusText");
  if (txt) txt.textContent = (text || "").toUpperCase();
  const badge = document.getElementById("statusBadge");
  if (badge) badge.className = "badge " + (ok ? "ok" : "down");
  const fs = document.getElementById("footSys");
  if (fs) fs.textContent = ok ? "DATA LAYER NOMINAL" : "DATA LAYER OFFLINE";
}

// Drive the full-width CONFLICTS alert strip: green/neutral at 0, red on any.
function setConflictAlert(n) {
  const strip = document.getElementById("conflictAlert");
  const msg = document.getElementById("conflictMsg");
  if (!strip) return;
  strip.className = "alert-strip" + (n > 0 ? " alert" : "");
  if (msg) msg.textContent = n > 0
    ? `${n} time × station overlap${n === 1 ? "" : "s"} — review`
    : "no time × station overlaps";
}

// Stat tile id -> /stats key. Driven from one map so the markup, the fetch fill,
// and the offline reset never drift apart.
const STAT_FIELDS = {
  statVessels: "vessels",
  statMoored: "moored",
  statReservations: "reservations",
  statRequests: "berth_requests",
  statConfirmed: "confirmed",
  statArrivals: "arrivals_24h",
};
export async function loadStats() {
  const fa = document.getElementById("footApi");
  try {
    const t = performance.now();
    const s = await api("/stats");
    if (fa) fa.textContent = Math.round(performance.now() - t) + "MS";
    for (const [id, key] of Object.entries(STAT_FIELDS)) {
      document.getElementById(id).textContent = s[key] ?? 0;
    }
    setStatus(true, "data layer online");
  } catch (e) {
    if (fa) fa.textContent = "—";
    setStatus(false, "database offline");
    for (const id of Object.keys(STAT_FIELDS)) {
      document.getElementById(id).textContent = "0";
    }
  }
}

// --- Worker liveness (footer chips) ----------------------------------------
// One chip per background worker (AIS ingest / occupancy + sweep / AI intake),
// coloured by the health the server derives from each worker's last heartbeat
// (GET /workers). Abbreviated name on the chip; full label + age + last-batch
// detail in the tooltip.
const WK_ABBR = { ais: "AIS", occupancy: "OCC", "intake-dataverse": "INT" };
function fmtAge(s) {
  if (s == null) return "no heartbeat";
  if (s < 90) return `${Math.round(s)}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}
export async function loadWorkers() {
  const el = document.getElementById("workerChips");
  if (!el) return;
  try {
    const { workers } = await api("/workers");
    el.innerHTML = workers.map((w) => {
      const ab = WK_ABBR[w.name] || w.name.slice(0, 3).toUpperCase();
      const det = w.detail
        ? " · " + Object.entries(w.detail).map(([k, v]) => `${k}=${v}`).join(" ")
        : "";
      const title = `${w.label}: ${w.health.toUpperCase()} (${fmtAge(w.age_seconds)})${det}`;
      return `<span class="wk ${esc(w.health)}" title="${esc(title)}">${esc(ab)}</span>`;
    }).join("");
  } catch (e) {
    el.textContent = "—";
  }
}

// --- Conflicts (time × station overlaps) -----------------------------------
// A dedicated alert layer for the contested stretch of wharf (red = the
// stylesheet's reserved alert colour). CONFLICTS holds the last fetch so a card
// click can map back to its overlap rectangle.
const conflictLayer = L.layerGroup().addTo(map);
let CONFLICTS = [];

// The live Overview panels hide harbor craft (tugs/towboats/pilots) by default;
// one global toggle (#svcCraftToggle, at the top of the Overview tab) reveals
// them. Conflicts and the verification half of "alongside" re-fetch with
// service_craft=1; the moored feed (unfiltered server-side) is filtered
// client-side in loadAlongside. State lives here because every loader reads it.
let SHOW_SERVICE_CRAFT = false;
const svcQS = () => (SHOW_SERVICE_CRAFT ? "?service_craft=1" : "");
(function () {
  const t = document.getElementById("svcCraftToggle");
  if (t) t.addEventListener("change", () => {
    SHOW_SERVICE_CRAFT = t.checked;
    loadConflicts();
    loadAlongside();
  });
})();

const CONFLICT_CAT = {
  "observed-vs-planned": "observed vs planned",
  "dredge-vs-vessel": "dredge vs vessel",
  "planned-vs-planned": "planned vs planned",
};

function conflictName(side) {
  return esc(side.vessel_name || (side.type === "dredge" ? "Dredging op" : "(unnamed)"));
}

function conflictCard(c, i) {
  const dlo = Math.round(Math.min(c.overlap.station_lo_dock, c.overlap.station_hi_dock));
  const dhi = Math.round(Math.max(c.overlap.station_lo_dock, c.overlap.station_hi_dock));
  const win = `${fmtCentral(c.overlap.t_start)} → ${fmtCentral(c.overlap.t_end)}`;
  return `
    <div class="card conflict-card" data-conflict="${i}" style="cursor:pointer;border-left-color:${PAL.red}">
      <div class="name">${conflictName(c.a)} ⇄ ${conflictName(c.b)}
        <span class="status-badge" style="color:${PAL.red}">conflict</span></div>
      <div class="meta">${esc(CONFLICT_CAT[c.category] || c.category)} · Dock <b>${dlo}–${dhi}</b></div>
      <div class="meta">overlap <b>${win}</b></div>
    </div>`;
}

// Draw the overlap station sub-range as a red line on the quay, sampling the
// centerline across the span so it follows the stepped bulkhead. Auto-clears.
function highlightConflict(c) {
  conflictLayer.clearLayers();
  const lo = Math.min(c.overlap.station_lo, c.overlap.station_hi);
  const hi = Math.max(c.overlap.station_lo, c.overlap.station_hi);
  if (lo == null || hi == null || !(hi >= lo)) return;
  const pts = [];
  const N = 12;
  for (let i = 0; i <= N; i++) {
    const ll = stationToLatLon(lo + (hi - lo) * (i / N));
    if (ll) pts.push([ll.lat, ll.lon]);
  }
  if (pts.length < 2) return;
  L.polyline(pts, { color: PAL.red, weight: 9, opacity: 0.75, lineCap: "round" }).addTo(conflictLayer);
  map.fitBounds(L.latLngBounds(pts).pad(0.6), { maxZoom: 16, animate: true });
  setTimeout(() => conflictLayer.clearLayers(), 6000);
}

function wireConflictCards(el) {
  el.querySelectorAll(".conflict-card").forEach((card) => {
    card.addEventListener("click", () => {
      const c = CONFLICTS[Number(card.dataset.conflict)];
      if (c) highlightConflict(c);
    });
  });
}

export async function loadConflicts() {
  const el = document.getElementById("conflicts");
  const stat = document.getElementById("statConflicts");
  try {
    const cs = await api("/conflicts" + svcQS());
    CONFLICTS = cs;
    if (stat) stat.textContent = cs.length;
    setConflictAlert(cs.length);
    if (!el) return;
    el.innerHTML = cs.length
      ? cs.map(conflictCard).join("")
      : '<div class="empty">no conflicts</div>';
    wireConflictCards(el);
  } catch (e) {
    CONFLICTS = [];
    if (stat) stat.textContent = "0";
    setConflictAlert(0);
    if (el) el.innerHTML = '<div class="empty">unavailable (DB offline)</div>';
  }
}

// --- Alongside now + AIS verification + future planned ---------------------
// One merged panel. "Alongside now" is who's physically at the wharf right now
// (live AIS: /occupancy/moored — the per-vessel form of the "Moored now" stat,
// each projected to a berth server-side; populates without the occupancy worker).
// Each alongside ship carries its AIS-verification badge — how the operator's
// booking lines up with observed reality (arrived / berthed elsewhere /
// unplanned) — joined by vessel_id to the read-only GET /verification payload,
// plus a craft status bar (tug/tow/pilot vs cargo/tanker/…). "Future planned"
// below lists bookings not yet alongside (awaiting; a lapsed one shows no-show).
// Read-only throughout: the sweep that archives stale rows is the occupancy
// worker's job (app/occupancy/run.py), never this panel's.
const VERIFY_STATE = {
  arrived:  { label: "arrived",  color: PAL.green },
  no_show:  { label: "no-show",  color: PAL.red },
  awaiting: { label: "awaiting", color: PAL.muted },
};

// The verification badge for a moored ship, from its verification entry (joined
// by vessel_id). No entry (contact not upserted / worker cold / plan absent) ->
// the plain "moored" it showed before this merge.
function verifyBadge(vinfo) {
  if (!vinfo) return { label: "moored", color: PAL.green };
  if (vinfo.kind === "unplanned") return { label: "unplanned", color: PAL.amber };
  // planned: it's physically here, so the only discrepancy is wrong station.
  if (vinfo.where_planned === false) return { label: "berthed elsewhere", color: PAL.amber };
  return VERIFY_STATE.arrived;
}

function alongsideCard(r, vinfo) {
  const name = esc(r.vessel_name || "(unnamed)");
  const where = r.berth_name ? esc(r.berth_name)
    : (r.popa_station != null ? "POPA " + fmtSta(r.popa_station) : "berth —");
  const clickable = r.mmsi != null;
  const hoverable = r.vessel_id != null;
  const vb = verifyBadge(vinfo);
  const craft = SHIP_CATEGORIES[shipTypeCategory(r.ship_type)];
  return `
    <div class="card${clickable ? " along-card" : ""}${hoverable ? " hist-card" : ""}" data-along-mmsi="${r.mmsi ?? ""}" data-vessel-id="${r.vessel_id ?? ""}" style="border-left-color:${vb.color}${clickable ? ";cursor:pointer" : ""}"${hoverable ? ' title="Hover for the ship dossier"' : ""}>
      <div class="name">${name}
        <span class="status-badge" style="color:${vb.color}">${vb.label}</span>
        <span class="status-badge craft-badge" style="color:${craft.color}">${esc(craft.label)}</span></div>
      <div class="meta">${where} · since <b>${fmtCentral(r.since ?? r.msg_ts)}</b></div>
    </div>`;
}

// A booking not yet alongside — "awaiting" (upcoming), or "no-show" (window
// lapsed, never arrived; flagged red until the worker archives it to History).
function futurePlannedCard(p) {
  const st = VERIFY_STATE[p.state] || { label: p.state, color: PAL.muted };
  const name = esc(p.vessel_name || "(unnamed)");
  const win = `${fmtCentral(p.t_start)} → ${fmtCentral(p.t_end)}`;
  return `
    <div class="card" style="border-left-color:${st.color}">
      <div class="name">${name}
        <span class="status-badge" style="color:${st.color}">${st.label}</span></div>
      <div class="meta">${esc(p.status)}${p.berth_name ? " · " + esc(p.berth_name) : ""} · plan <b>${win}</b></div>
    </div>`;
}

export async function loadAlongside() {
  const el = document.getElementById("alongside");
  const fel = document.getElementById("futurePlanned");
  const fhdr = document.getElementById("futurePlannedHdr");
  if (!el) return;
  try {
    // Who's alongside now (live positions, unfiltered server-side) + the
    // read-only verification payload. Pass svcQS() so a revealed tug can still
    // match an unplanned entry; the mutating POST /verification/sweep is NOT
    // called here — the occupancy worker owns stale-row archiving.
    const moored = await api("/occupancy/moored");
    let v = { planned: [], unplanned: [] };
    try { v = await api("/verification" + svcQS()); } catch (_) { /* degrade to moored-only */ }

    // vessel_id -> verification info; a planned entry wins over an unplanned one.
    const vmap = new Map();
    for (const p of (v.planned || [])) {
      if (p.vessel_id != null && !vmap.has(p.vessel_id)) {
        vmap.set(p.vessel_id, { kind: "planned", state: p.state, where_planned: p.where_planned });
      }
    }
    for (const u of (v.unplanned || [])) {
      if (u.vessel_id != null && !vmap.has(u.vessel_id)) {
        vmap.set(u.vessel_id, { kind: "unplanned" });
      }
    }

    // The global "show harbor craft" toggle governs this unfiltered feed too:
    // hide tug/tow/pilot client-side (by ship_type) unless the toggle is on.
    const rows = SHOW_SERVICE_CRAFT
      ? moored
      : moored.filter((r) => !isServiceCraftType(r.ship_type));

    el.innerHTML = rows.length
      ? rows.map((r) => alongsideCard(r, vmap.get(r.vessel_id))).join("")
      : '<div class="empty">nothing alongside</div>';
    el.querySelectorAll(".along-card").forEach((card) => {
      card.addEventListener("click", () => {
        const mmsi = Number(card.dataset.alongMmsi);
        if (mmsi) locateVesselOnMap(mmsi);
      });
    });
    // Hover a moored card -> the same floating ship dossier History uses
    // (vessel record + dimensions + latest AIS fix + booking log).
    el.querySelectorAll(".hist-card").forEach((card) => {
      const vid = Number(card.dataset.vesselId);
      if (!vid) return;
      card.addEventListener("mouseenter", () => showShipHover(card, vid));
      card.addEventListener("mouseleave", hideShipHover);
    });

    // Future planned: bookings not yet alongside (awaiting / no-show), minus any
    // vessel already in the moored list; no-shows (flagged) sort first.
    const mooredIds = new Set(rows.map((r) => r.vessel_id).filter((x) => x != null));
    const future = (v.planned || [])
      .filter((p) => (p.state === "awaiting" || p.state === "no_show") && !mooredIds.has(p.vessel_id))
      .sort((a, b) => (b.state === "no_show") - (a.state === "no_show"));
    if (future.length) {
      if (fhdr) fhdr.style.display = "";
      if (fel) fel.innerHTML = future.map(futurePlannedCard).join("");
    } else {
      if (fhdr) fhdr.style.display = "none";
      if (fel) fel.innerHTML = "";
    }
  } catch (e) {
    el.innerHTML = '<div class="empty">unavailable (DB offline)</div>';
    if (fhdr) fhdr.style.display = "none";
    if (fel) fel.innerHTML = "";
  }
}
