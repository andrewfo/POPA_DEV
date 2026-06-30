// Sidebar panels: status / stats, conflicts, AIS verification, and "alongside now".
import { api, apiWrite, esc, fmtCentral, fmtSta, PAL } from "./api.js";
import { map, stationToLatLon, locateVesselOnMap } from "./map.js";
import { showShipHover, hideShipHover } from "./history.js";

// --- Status / stats --------------------------------------------------------
export function setStatus(ok, text) {
  const dot = document.getElementById("statusDot");
  dot.className = "dot " + (ok ? "ok" : "down");
  document.getElementById("statusText").textContent = (text || "").toUpperCase();
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
const WK_ABBR = { ais: "AIS", occupancy: "OCC", "intake-dataverse": "AI" };
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

// Both live panels hide harbor craft (tugs/towboats/pilots) server-side by
// default; one toggle (#svcCraftToggle, in the verification section) re-fetches
// both with service_craft=1. State lives here because both loaders read it.
let SHOW_SERVICE_CRAFT = false;
const svcQS = () => (SHOW_SERVICE_CRAFT ? "?service_craft=1" : "");
(function () {
  const t = document.getElementById("svcCraftToggle");
  if (t) t.addEventListener("change", () => {
    SHOW_SERVICE_CRAFT = t.checked;
    loadConflicts();
    loadVerification();
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

// --- AIS verification (plan vs observed reality) ---------------------------
// Read-only: surfaces how operator placements line up with observed AIS. Never
// mutates status (and AIS can't place — see /verification). Reuses the conflict
// highlight layer to draw a "berthed elsewhere" observed range on click.
const VERIFY_STATE = {
  arrived:  { label: "arrived",  color: PAL.green },
  no_show:  { label: "no-show",  color: PAL.red },
  awaiting: { label: "awaiting", color: PAL.muted },
};

function verifyPlannedCard(p, i) {
  const st = VERIFY_STATE[p.state] || { label: p.state, color: PAL.muted };
  const name = esc(p.vessel_name || "(unnamed)");
  const win = `${fmtCentral(p.t_start)} → ${fmtCentral(p.t_end)}`;
  // "berthed elsewhere" only when we have both a planned and an observed range.
  const elsewhere = p.where_planned === false
    ? ` <span class="status-badge" style="color:${PAL.amber}">berthed elsewhere</span>` : "";
  const clickable = p.observed && p.where_planned === false;
  return `
    <div class="card${clickable ? " verify-card" : ""}" data-verify="${i}" style="border-left-color:${st.color}${clickable ? ";cursor:pointer" : ""}">
      <div class="name">${name}
        <span class="status-badge" style="color:${st.color}">${st.label}</span>${elsewhere}</div>
      <div class="meta">${esc(p.status)}${p.berth_name ? " · " + esc(p.berth_name) : ""} · plan <b>${win}</b></div>
    </div>`;
}

function verifyUnplannedCard(u) {
  const name = esc(u.vessel_name || "(unnamed)");
  const since = fmtCentral(u.t_start);
  return `
    <div class="card" style="border-left-color:${PAL.amber}">
      <div class="name">${name}
        <span class="status-badge" style="color:${PAL.amber}">unplanned</span></div>
      <div class="meta">observed${u.berth_name ? " · " + esc(u.berth_name) : ""} · since ${since}${u.ongoing ? " · ongoing" : ""}</div>
    </div>`;
}

let VERIFY_PLANNED = [];

export async function loadVerification() {
  const el = document.getElementById("verification");
  if (!el) return;
  try {
    // POST /verification/sweep first auto-archives stale planned rows (a no-show
    // or arrived past its window + grace period -> cancelled/completed), so they
    // drop out of this panel into History, then returns the fresh payload. The
    // read-only GET /verification is still there for clients that must not mutate.
    const w = await apiWrite("POST", "/verification/sweep" + svcQS());
    if (!w.ok) throw new Error("sweep failed");
    const v = w.data || {};
    VERIFY_PLANNED = v.planned || [];
    // Surface the actionable rows first: discrepancies (no-show / berthed
    // elsewhere) and unplanned arrivals; quietly arrived/awaiting rows follow.
    const flagged = VERIFY_PLANNED
      .map((p, i) => ({ p, i }))
      .filter(({ p }) => p.state === "no_show" || p.where_planned === false);
    const ok = VERIFY_PLANNED
      .map((p, i) => ({ p, i }))
      .filter(({ p }) => !(p.state === "no_show" || p.where_planned === false));
    const unplanned = v.unplanned || [];
    const parts = [];
    for (const { p, i } of flagged) parts.push(verifyPlannedCard(p, i));
    for (const u of unplanned) parts.push(verifyUnplannedCard(u));
    for (const { p, i } of ok) parts.push(verifyPlannedCard(p, i));
    el.innerHTML = parts.length ? parts.join("") : '<div class="empty">nothing to verify</div>';
    // A "berthed elsewhere" card maps to its observed range via the conflict layer.
    el.querySelectorAll(".verify-card").forEach((card) => {
      card.addEventListener("click", () => {
        const p = VERIFY_PLANNED[Number(card.dataset.verify)];
        if (p && p.observed) highlightConflict({ overlap: p.observed });
      });
    });
  } catch (e) {
    VERIFY_PLANNED = [];
    el.innerHTML = '<div class="empty">unavailable (DB offline)</div>';
  }
}

// --- Alongside now (currently berthed) -------------------------------------
// Who is physically at the wharf right now, from live AIS: /occupancy/moored is
// the per-vessel form of the "Moored now" stat (latest fix alongside + slow),
// each projected to a berth server-side. Reads live positions, so it populates
// without the occupancy-derivation worker. Click a row to locate its AIS contact;
// hover for the full ship dossier (the same floating panel History uses — vessel
// record + dimensions + latest AIS fix + booking log), shown when the contact has
// been upserted into the vessel table (vessel_id present).
function alongsideCard(r) {
  const name = esc(r.vessel_name || "(unnamed)");
  const where = r.berth_name ? esc(r.berth_name)
    : (r.popa_station != null ? "POPA " + fmtSta(r.popa_station) : "berth —");
  const clickable = r.mmsi != null;
  const hoverable = r.vessel_id != null;
  return `
    <div class="card${clickable ? " along-card" : ""}${hoverable ? " hist-card" : ""}" data-along-mmsi="${r.mmsi ?? ""}" data-vessel-id="${r.vessel_id ?? ""}" style="border-left-color:${PAL.green}${clickable ? ";cursor:pointer" : ""}"${hoverable ? ' title="Hover for the ship dossier"' : ""}>
      <div class="name">${name}
        <span class="status-badge" style="color:${PAL.green}">moored</span></div>
      <div class="meta">${where} · since <b>${fmtCentral(r.since ?? r.msg_ts)}</b></div>
    </div>`;
}

export async function loadAlongside() {
  const el = document.getElementById("alongside");
  if (!el) return;
  try {
    const rs = await api("/occupancy/moored");
    el.innerHTML = rs.length
      ? rs.map(alongsideCard).join("")
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
  } catch (e) {
    el.innerHTML = '<div class="empty">unavailable (DB offline)</div>';
  }
}
