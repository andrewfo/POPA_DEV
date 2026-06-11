// History tab + ship-detail hover dossier. /history returns the booking log
// (every status, observed berthings included), newest arrival first.
import {
  api, esc, fmtCentral, FT_PER_M, RES_STATUS_COLOR, BADGE_COLORS, shipTypeLabel, NAV_STATUS,
} from "./api.js";

function histCard(h) {
  const color = BADGE_COLORS[h.status] || "#8a99a6";
  const imo = h.vessel_imo ? ` <span class="imo">IMO ${esc(h.vessel_imo)}</span>` : "";
  // Pared down to identity + status + the booking window. Everything else
  // (position, cargo, direction, notes, dimensions, AIS) lives in the dossier
  // that floats on hover (showShipHover -> renderShipDetail). Hover only fires
  // when there's a vessel row to show (an IMO-less intake has no vessel_id).
  const hoverable = h.vessel_id != null;
  const dates = `ETB <b>${fmtCentral(h.t_start)}</b>${h.t_end ? ` → ETD <b>${fmtCentral(h.t_end)}</b>` : ""}`;
  return `
    <div class="card${hoverable ? " hist-card" : ""}" data-vessel-id="${h.vessel_id ?? ""}" style="border-left-color:${color}"${hoverable ? ' title="Hover for the ship dossier"' : ""}>
      <div class="name">${esc(h.vessel_name || "(unnamed)")}${imo}
        <span class="status-badge" style="color:${color}">${esc(h.status)}</span></div>
      <div class="meta">${dates}</div>
    </div>`;
}

// --- Ship detail hover panel -----------------------------------------------
// Hovering a History entry floats a box beside it: GET /vessels/{id} returns the
// full vessel record + its whole reservation log + latest AIS fix, cached per
// vessel. A read-only preview over that one call — the data layer is the product.

// One key/value row; skips itself when the value is empty so optional fields
// don't render blank. `v` must already be HTML-safe (caller escapes free text).
function kvRow(k, v) {
  if (v == null || v === "") return "";
  return `<div class="k">${esc(k)}</div><div class="v">${v}</div>`;
}
// Metres (canonical store) shown with its feet equivalent — the dual readout the
// rest of the UI uses. null in -> null out (row skipped by kvRow).
function mFt(m) {
  if (m == null) return null;
  return `${Number(m).toFixed(1)} m · ${Math.round(Number(m) * FT_PER_M)} ft`;
}

// A small compass: a ringed dial with an arrow at `deg` (0 = N, clockwise). Used
// for the AIS heading/COG — an orientation read at a glance, not a number.
function compassSvg(deg) {
  if (deg == null) return "";
  return `
    <svg class="compass" viewBox="0 0 40 40" width="40" height="40" aria-hidden="true">
      <circle class="cmp-ring" cx="20" cy="20" r="18"/>
      <text class="cmp-n" x="20" y="9" text-anchor="middle">N</text>
      <g transform="rotate(${Math.round(deg)} 20 20)">
        <path class="cmp-arrow" d="M20,6 L24,23 L20,19 L16,23 Z"/>
      </g>
    </svg>`;
}

// A horizontal strip placing every reservation as a status-coloured bar on one
// shared time axis — the ship's whole booking life at a glance (a compact
// timeline, distinct from the occupancy Gantt which is per-berth).
function resTimeline(res) {
  const items = res.filter((r) => r.t_start);
  if (!items.length) return "";
  const times = [];
  items.forEach((r) => {
    times.push(+new Date(r.t_start));
    if (r.t_end) times.push(+new Date(r.t_end));
  });
  let min = Math.min(...times), max = Math.max(...times);
  if (max <= min) max = min + 86400000;                    // 1-day span fallback
  const W = 320, H = 30, padX = 6, top = 4, lane = 12;
  const x = (t) => padX + (W - padX * 2) * ((t - min) / (max - min));
  const bars = items.map((r) => {
    const a = x(+new Date(r.t_start));
    const b = r.t_end ? x(+new Date(r.t_end)) : a + 3;
    const w = Math.max(3, b - a);
    const c = RES_STATUS_COLOR[r.status] || "#888";
    return `<rect x="${a.toFixed(1)}" y="${top}" width="${w.toFixed(1)}" height="${lane}" rx="0" fill="${c}"/>`;
  }).join("");
  const lab =
    `<text class="tl-label" x="${padX}" y="${H - 3}" text-anchor="start">${fmtCentral(new Date(min).toISOString())}</text>` +
    `<text class="tl-label" x="${W - padX}" y="${H - 3}" text-anchor="end">${fmtCentral(new Date(max).toISOString())}</text>`;
  return `<svg class="restimeline" viewBox="0 0 ${W} ${H}" width="100%">${bars}${lab}</svg>`;
}

// Turn a reservation's notes (our newline "key: value" + flag-line format, see
// app/intake/manual._notes) into a styled list instead of a flat grey blob:
//  - flag lines with no "key:" (e.g. "manual entry (phone)", "berth UNASSIGNED")
//    become pills (an unassigned/warning one tinted amber);
//  - "key: value" lines become a definition grid (accent key, ink value);
//  - the AI provenance line becomes a chip + muted caveat;
//  - warnings get an amber line.
// Operator-typed free-form notes (no recognised shape) fall through as a tag.
function renderResNotes(notes) {
  if (!notes) return "";
  const tags = [], kv = [], ai = [], warn = [];
  notes.split("\n").map((s) => s.trim()).filter(Boolean).forEach((line) => {
    if (/^AI-parsed/i.test(line)) { ai.push(line); return; }
    if (/^warnings?:/i.test(line)) { warn.push(line.replace(/^warnings?:\s*/i, "")); return; }
    const m = line.match(/^([^:]{1,22}):\s*(.+)$/);
    if (m) { kv.push([m[1], m[2]]); return; }
    tags.push(line);
  });
  let html = "";
  if (tags.length) {
    html += `<div class="note-tags">` + tags.map((t) =>
      `<span class="note-tag${/unassigned/i.test(t) ? " warn" : ""}">${esc(t)}</span>`
    ).join("") + `</div>`;
  }
  if (kv.length) {
    html += `<div class="note-kv">` + kv.map(([k, v]) =>
      `<span class="nk">${esc(k)}</span><span class="nv">${esc(v)}</span>`
    ).join("") + `</div>`;
  }
  if (ai.length) {
    html += ai.map((line) => {
      const parts = line.split(/\s[—–-]\s/);          // chip text — caveat detail
      const head = parts.shift();
      const detail = parts.join(" — ");
      return `<div class="note-ai"><span class="ai-chip">${esc(head)}</span>${detail ? `<span class="ai-detail">${esc(detail)}</span>` : ""}</div>`;
    }).join("");
  }
  if (warn.length) html += `<div class="note-warn">▲ ${esc(warn.join("; "))}</div>`;
  return `<div class="note-block">${html}</div>`;
}

// One reservation, compact: a status dot + the window + position + its notes
// (the notes that used to sit on the History card now live here).
function shipResRow(r) {
  const color = RES_STATUS_COLOR[r.status] || "#888";
  const sta = r.station_unassigned
    ? "berth unassigned"
    : `Dock ${Math.round(Math.min(r.station_lo_dock, r.station_hi_dock))}–${Math.round(Math.max(r.station_lo_dock, r.station_hi_dock))}`;
  const bits = [esc(sta)];
  if (r.berth_name) bits.push(esc(r.berth_name));
  if (r.direction) bits.push(esc(r.direction));
  if (r.cargo) bits.push(esc(r.cargo));
  return `
    <div class="dossier-res">
      <span class="dot" style="background:${color}"></span>
      <div class="rbody">
        <div class="rhead"><b>${esc(r.status)}</b> · ${esc(r.type)} · via ${esc(r.source)}</div>
        <div class="rmeta">ETB ${fmtCentral(r.t_start)}${r.t_end ? " → ETD " + fmtCentral(r.t_end) : ""}</div>
        <div class="rmeta">${bits.join(" · ")}</div>
        ${renderResNotes(r.notes)}
      </div>
    </div>`;
}

// The vessel dossier: a header band, dimensions (LOA/beam/draft), identity, an
// AIS fix with a compass, and the booking timeline + reservation rows (with
// notes). Pure -> paints identically from cache or a fresh fetch.
function renderShipDetail(v) {
  const typeLabel = shipTypeLabel(v.ship_type);
  const sub = [
    v.imo ? "IMO " + esc(v.imo) : null,
    v.mmsi ? "MMSI " + esc(v.mmsi) : null,
    v.callsign ? esc(v.callsign) : null,
    typeLabel ? esc(typeLabel) : null,
  ].filter(Boolean).join("  ·  ");
  const ident = [
    kvRow("Destination", esc(v.destination)),
    kvRow("First seen", v.created_at ? fmtCentral(v.created_at) : null),
    kvRow("Last update", v.updated_at ? fmtCentral(v.updated_at) : null),
  ].join("");
  const dims = [
    kvRow("LOA", mFt(v.loa)),
    kvRow("Beam", mFt(v.beam)),
    kvRow("Draft", mFt(v.draft)),
  ].join("");
  const p = v.latest_position;
  // Always show the AIS section so its absence reads as "no contact" rather than
  // a missing panel — a manual / non-AIS-tracked ship simply has no fix on record.
  const aisSection = `
    <h3>Latest AIS fix</h3>
    ${p ? `<div class="ais-row">
      ${compassSvg(p.heading != null ? p.heading : p.cog)}
      <div class="ais-facts">
        <div><span class="k">speed</span> ${p.sog != null ? p.sog.toFixed(1) + " kn" : "—"}${p.cog != null ? ` · <span class="k">cog</span> ${Math.round(p.cog)}°` : ""}${p.heading != null ? ` · <span class="k">hdg</span> ${Math.round(p.heading)}°` : ""}</div>
        <div><span class="k">${esc(NAV_STATUS[p.nav_status] || (p.nav_status != null ? "code " + p.nav_status : "status n/a"))}</span></div>
        <div>${p.lat.toFixed(5)}, ${p.lon.toFixed(5)} · ${fmtCentral(p.msg_ts)}</div>
      </div>
    </div>` : `<div class="dossier-none">no AIS fix on record (not AIS-tracked)</div>`}`;
  const res = v.reservations || [];
  return `
    <div class="dossier-head">
      <h2 class="dossier-title">${v.name ? esc(v.name) : ("Ship #" + v.id)}</h2>
      ${sub ? `<div class="dossier-sub">${sub}</div>` : ""}
    </div>
    <div class="dossier-body">
      ${dims ? `<h3>Dimensions</h3><div class="kv">${dims}</div>` : ""}
      ${ident ? `<h3>Identity</h3><div class="kv">${ident}</div>` : ""}
      ${aisSection}
      <h3>Reservations (${res.length})</h3>
      ${res.length ? resTimeline(res) + res.map(shipResRow).join("") : '<div class="empty">none on record</div>'}
    </div>`;
}

const SHIP_DETAIL_CACHE = new Map();   // vessel_id -> detail payload (per session)
let shipHoverTimer = null;

// Float the panel beside the hovered card: to its right, flipping to the left if
// it would run off-screen, and clamped vertically into the viewport.
function positionShipHover(card) {
  const box = document.getElementById("shipHover");
  if (!box) return;
  const r = card.getBoundingClientRect();
  const gap = 12, m = 8;
  let left = r.right + gap;
  if (left + box.offsetWidth > window.innerWidth - m) left = r.left - gap - box.offsetWidth;
  box.style.left = Math.max(m, left) + "px";
  let top = r.top;
  if (top + box.offsetHeight > window.innerHeight - m)
    top = Math.max(m, window.innerHeight - m - box.offsetHeight);
  box.style.top = top + "px";
}

async function showShipHover(card, vesselId) {
  if (!vesselId) return;
  clearTimeout(shipHoverTimer);
  const box = document.getElementById("shipHover");
  if (!box) return;
  box.dataset.vid = String(vesselId);
  const cached = SHIP_DETAIL_CACHE.get(vesselId);
  box.innerHTML = cached ? renderShipDetail(cached) : '<div class="empty">loading…</div>';
  box.classList.add("open");
  positionShipHover(card);
  if (cached) return;
  try {
    const v = await api("/vessels/" + vesselId);
    SHIP_DETAIL_CACHE.set(vesselId, v);
    // Only paint if the pointer is still on this same card.
    if (box.classList.contains("open") && box.dataset.vid === String(vesselId)) {
      box.innerHTML = renderShipDetail(v);
      positionShipHover(card);
    }
  } catch (e) {
    if (box.dataset.vid === String(vesselId))
      box.innerHTML = '<div class="empty">couldn’t load ship details</div>';
  }
}

// Small delay on hide so skating across rows doesn't flicker the panel.
function hideShipHover() {
  const box = document.getElementById("shipHover");
  if (!box) return;
  shipHoverTimer = setTimeout(() => box.classList.remove("open"), 80);
}

// One-line digest of the active filters, shown beside the Filters button so the
// operator sees what's applied without opening the modal.
function updateHistFilterSummary() {
  const el = document.getElementById("histFilterSummary");
  if (!el) return;
  const name = document.getElementById("histName").value.trim();
  const imo = document.getElementById("histImo").value.trim();
  const status = document.getElementById("histStatus").value;
  const from = document.getElementById("histFrom").value;
  const to = document.getElementById("histTo").value;
  const parts = [];
  if (name) parts.push("name: " + name);
  if (imo) parts.push("IMO: " + imo);
  if (status) parts.push(status);
  if (from || to) parts.push((from || "…") + " → " + (to || "…"));
  el.textContent = parts.length ? parts.join(" · ") : "no filters, showing all";
}

export async function loadHistory() {
  const el = document.getElementById("historyList");
  if (!el) return;
  const name = document.getElementById("histName").value.trim();
  const imo = document.getElementById("histImo").value.trim();
  const status = document.getElementById("histStatus").value;
  const from = document.getElementById("histFrom").value;   // YYYY-MM-DD (Central)
  const to = document.getElementById("histTo").value;
  updateHistFilterSummary();
  const qs = new URLSearchParams();
  if (name) qs.set("name", name);
  if (imo) qs.set("imo", imo);
  if (status) qs.set("status", status);
  // Date inputs are zone-less; send the day's Central bounds and let the server's
  // pinned zone interpret them (mirrors how the edit forms send wall-clock time).
  if (from) qs.set("from", from + "T00:00:00");
  if (to) qs.set("to", to + "T23:59:59");
  qs.set("limit", "200");
  el.innerHTML = '<div class="empty">loading…</div>';
  try {
    const rows = await api("/history?" + qs.toString());
    el.innerHTML = rows.length
      ? rows.map(histCard).join("")
      : '<div class="empty">no reservations match</div>';
    // Hover a ship's card -> floating detail panel (vessel record + its whole
    // reservation log + latest AIS fix).
    el.querySelectorAll(".hist-card").forEach((card) => {
      const vid = Number(card.dataset.vesselId);
      card.addEventListener("mouseenter", () => showShipHover(card, vid));
      card.addEventListener("mouseleave", hideShipHover);
    });
  } catch (e) {
    el.innerHTML = '<div class="empty">unavailable (DB offline)</div>';
  }
}

// History filter modal wiring.
(function () {
  const name = document.getElementById("histName");
  const imo = document.getElementById("histImo");
  const status = document.getElementById("histStatus");
  const from = document.getElementById("histFrom");
  const to = document.getElementById("histTo");
  const search = document.getElementById("histSearch");
  const clear = document.getElementById("histClear");
  const modal = document.getElementById("histFilterModal");
  const openBtn = document.getElementById("histFilterBtn");
  const closeBtn = document.getElementById("histFilterClose");
  const openModal = () => { if (modal) { modal.classList.add("open"); name.focus(); } };
  const closeModal = () => { if (modal) modal.classList.remove("open"); };
  const runAndClose = () => { loadHistory(); closeModal(); };
  if (openBtn) openBtn.addEventListener("click", openModal);
  if (closeBtn) closeBtn.addEventListener("click", closeModal);
  // Click the dimmed backdrop (not the sheet) closes; Esc closes.
  if (modal) modal.addEventListener("click", (e) => { if (e.target === modal) closeModal(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && modal && modal.classList.contains("open")) closeModal();
  });
  // Search (button or Enter in a text field) applies + closes the modal.
  if (search) search.addEventListener("click", runAndClose);
  const onEnter = (e) => { if (e.key === "Enter") runAndClose(); };
  if (name) name.addEventListener("keydown", onEnter);
  if (imo) imo.addEventListener("keydown", onEnter);
  // Selecting a status / date applies live, but leaves the modal open to tweak.
  if (status) status.addEventListener("change", loadHistory);
  if (from) from.addEventListener("change", loadHistory);
  if (to) to.addEventListener("change", loadHistory);
  if (clear) clear.addEventListener("click", () => {
    name.value = ""; imo.value = ""; status.value = ""; from.value = ""; to.value = "";
    loadHistory();
  });
})();
