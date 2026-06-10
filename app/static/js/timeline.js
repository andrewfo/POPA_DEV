// Berth occupancy timeline (Gantt drawer). Renders reservations from
// /reservations as bars in discrete berth lanes over a time axis, with a
// draggable time cursor that drives the map's vessel outlines.
import { api, fmtSta, esc, PAL, STATUS_COLORS, centralParts, CENTRAL_TZ } from "./api.js";
import { state } from "./state.js";
import { map, renderOutlines } from "./map.js";

export const loadTimeline = (function () {
  const SVGNS = "http://www.w3.org/2000/svg";
  const LABEL_W = 96, ROW_H = 20, LANE_PAD = 4, HEADER_H = 24, RIGHT_PAD = 14;
  const H = 3600e3, DAY = 24 * H;

  // status -> colour; dredge overrides by type. Mirrors the reservation enums.
  const STATUS = STATUS_COLORS;
  const DREDGE = PAL.dredge;
  // Observed (AIS) is intentionally absent: the timeline shows only reservations
  // that came in through a berth request, never derived AIS occupancy.
  const LEGEND = [
    ["requested", "Requested"], ["tentative", "Tentative"],
    ["confirmed", "Confirmed"], ["completed", "Completed"],
    ["__dredge", "Dredging"],
  ];

  const drawer = document.getElementById("timelineDrawer");
  const stage = document.getElementById("stage");
  const svg = document.getElementById("tlSvg");
  const scroll = svg.parentElement;
  const tip = document.getElementById("tlTip");
  const emptyEl = document.getElementById("tlEmpty");
  const winLabel = document.getElementById("tlWindowLabel");
  const modeLabel = document.getElementById("tlModeLabel");
  const updatedEl = document.getElementById("tlUpdated");

  // Which preset window is active, shown in the header and used to highlight the
  // matching preset button. Panning away from a named preset -> "Custom range".
  const MODE_NAMES = {
    "3d": "±3 days", week: "This week", "7d": "Next 7 days",
    "30d": "Next 30 days", today: "Default view", custom: "Custom range",
  };
  function setMode(mode) {
    if (modeLabel) modeLabel.textContent = MODE_NAMES[mode] || mode;
    drawer.querySelectorAll("[data-preset]").forEach((b) =>
      b.classList.toggle("active", b.dataset.preset === mode));
  }

  // window state (ms). Default: now-2d .. now+7d.
  let t0 = Date.now() - 2 * DAY, t1 = Date.now() + 7 * DAY;
  let rows = [];                       // last fetched reservations
  let hideCancelled = true;
  // Time cursor: a single selected instant that drives the map's vessel outlines.
  let tSel = Number(localStorage.getItem("tlCursor")) || Date.now();
  let plotW = 1;                       // current plot width (set in render)
  let cursorLine = null, cursorHandle = null;   // live SVG refs for cheap drag

  const el = (tag, attrs) => {
    const n = document.createElementNS(SVGNS, tag);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    return n;
  };
  // Axis + tooltip times in Central (the canonical zone).
  const fmtD = (ms) => new Date(ms).toLocaleDateString(undefined, { timeZone: CENTRAL_TZ, month: "short", day: "numeric" });
  const fmtDT = (s) => s ? new Date(s).toLocaleString(undefined, { timeZone: CENTRAL_TZ, month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";

  // Build berth lanes from state.berthSta (high station on top), plus an
  // Unassigned lane at the top for requests with no berth yet. state.berthSta is
  // immutable after load (reassigned wholesale on fetch), so memoize on its
  // object identity instead of rebuilding the lane list on every frame.
  let _lanesCache = null, _lanesKey = null;
  function lanes() {
    if (_lanesCache && _lanesKey === state.berthSta) return _lanesCache;
    const berths = Object.entries(state.berthSta || {})
      .map(([name, r]) => ({ key: name, label: name, lo: Math.min(r[0], r[1]), hi: Math.max(r[0], r[1]), unassigned: false }))
      .sort((a, b) => b.hi - a.hi);
    _lanesKey = state.berthSta;
    _lanesCache = [{ key: "__un", label: "Unassigned", lo: null, hi: null, unassigned: true }, ...berths];
    return _lanesCache;
  }

  // Assign a reservation to one lane by greatest station overlap (fallback:
  // nearest by midpoint). Unassigned-station rows go to the Unassigned lane.
  function assignLane(r, ls) {
    if (r.station_unassigned || r.station_lo == null) return ls[0];
    const lo = Math.min(r.station_lo, r.station_hi), hi = Math.max(r.station_lo, r.station_hi);
    const mid = (lo + hi) / 2;
    let best = null, bestOv = -1, bestDist = Infinity;
    for (const L of ls) {
      if (L.unassigned) continue;
      const ov = Math.max(0, Math.min(hi, L.hi) - Math.max(lo, L.lo));
      const dist = Math.max(L.lo - mid, mid - L.hi, 0);
      if (ov > bestOv || (ov === bestOv && dist < bestDist)) { best = L; bestOv = ov; bestDist = dist; }
    }
    return best || ls[1] || ls[0];
  }

  // Greedy sub-row packing: items that overlap in time stack into separate rows.
  function pack(items) {
    items.sort((a, b) => a.s - b.s);
    const rowEnds = [];
    for (const it of items) {
      let placed = false;
      for (let i = 0; i < rowEnds.length; i++) {
        if (it.s >= rowEnds[i]) { it.row = i; rowEnds[i] = it.e; placed = true; break; }
      }
      if (!placed) { it.row = rowEnds.length; rowEnds.push(it.e); }
    }
    return Math.max(1, rowEnds.length);
  }

  function colorFor(r) { return r.type === "dredge" ? DREDGE : (STATUS[r.status] || "#888"); }

  // Central-Time zone offset (ms) at an instant, and the instant of the Central
  // calendar-day start containing it. Axis ticks snap to these so a gridline
  // lands on a Central midnight/hour — labelling a UTC-aligned tick in Central
  // reads a day early (the "now line stuck on yesterday" bug).
  function centralOffsetMs(ms) {
    const p = centralParts(ms);
    return Date.UTC(+p.y, +p.m - 1, +p.d, +p.hh, +p.mm) - ms;
  }
  function centralDayStart(ms) {
    const p = centralParts(ms);
    return Date.UTC(+p.y, +p.m - 1, +p.d, 0, 0) - centralOffsetMs(ms);
  }

  function render() {
    if (drawer.classList.contains("tl-collapsed")) return;   // body hidden -> nothing to draw
    const W = scroll.clientWidth || drawer.clientWidth || 800;
    if (W < 60) { requestAnimationFrame(render); return; }
    winLabel.textContent = `${fmtD(t0)} – ${fmtD(t1)}`;
    while (svg.firstChild) svg.removeChild(svg.firstChild);

    const ls = lanes();
    plotW = W - LABEL_W - RIGHT_PAD;
    const x = (ms) => LABEL_W + (Math.max(t0, Math.min(t1, ms)) - t0) / (t1 - t0) * plotW;

    // Bucket reservations into lanes, clamp times to the window, drop non-overlapping.
    // Observed (AIS) rows are derived occupancy, not bookings — the timeline shows
    // only reservations that came in through a berth request, so they're excluded.
    const visible = rows.filter((r) =>
      r.status !== "observed" && !(hideCancelled && r.status === "cancelled"));
    const byLane = new Map(ls.map((L) => [L.key, []]));
    for (const r of visible) {
      const s = r.t_start ? Date.parse(r.t_start) : t0;
      const e = r.t_end ? Date.parse(r.t_end) : t1;       // open-ended -> window edge
      if (e <= t0 || s >= t1) continue;                    // outside window
      byLane.get(assignLane(r, ls).key).push({ r, s, e });
    }

    // Layout: each lane height grows with its packed sub-row count.
    let y = HEADER_H, totalRows = 0;
    const laid = ls.map((L, i) => {
      const items = byLane.get(L.key);
      const nrows = pack(items);
      totalRows += items.length;
      const h = nrows * ROW_H + 2 * LANE_PAD;
      const lane = { L, i, y, h, items };
      y += h;
      return lane;
    });
    const totalH = y;
    svg.setAttribute("width", W);
    svg.setAttribute("height", totalH);
    emptyEl.style.display = totalRows ? "none" : "block";

    // Lane bands + labels.
    for (const lane of laid) {
      svg.appendChild(el("rect", { x: 0, y: lane.y, width: W, height: lane.h, class: "tl-lane-band" + (lane.i % 2 ? " alt" : "") }));
      const lbl = el("text", { x: 8, y: lane.y + lane.h / 2 + 4, class: "tl-lane-label" + (lane.L.unassigned ? " unassigned" : "") });
      lbl.textContent = lane.L.label;
      svg.appendChild(lbl);
    }

    // X gridlines + tick labels. Aim for ~one tick per 64px, snapped to a "nice"
    // step; ticks fall on Central day/hour boundaries (not UTC) so the date a
    // gridline carries matches the day it sits on. Sub-day steps print the time
    // of day; each Central midnight prints the date instead, so a multi-day view
    // shows both timestamps and the date rollovers.
    const span = t1 - t0;
    const nticks = Math.max(3, Math.round(plotW / 64));
    const NICE = [H, 2 * H, 3 * H, 6 * H, 12 * H, DAY, 2 * DAY, 7 * DAY, 14 * DAY, 30 * DAY];
    const step = NICE.find((s) => span / s <= nticks) || Math.ceil(span / nticks / DAY) * DAY;
    const subDay = step < DAY;
    for (let t = centralDayStart(t0); t <= t1; t += step) {
      if (t < t0) continue;                              // skip the pre-window midnight we step from
      const px = x(t);
      svg.appendChild(el("line", { x1: px, y1: HEADER_H, x2: px, y2: totalH, class: "tl-grid" }));
      const tk = el("text", { x: px + 3, y: 15, class: "tl-axis-label" });
      const cp = centralParts(t);
      const midnight = cp.hh === "00" && cp.mm === "00";
      tk.textContent = (!subDay || midnight)
        ? fmtD(t)
        : new Date(t).toLocaleTimeString(undefined, { timeZone: CENTRAL_TZ, hour: "numeric" });
      svg.appendChild(tk);
    }

    // "Now" marker — a bright solid red hairline with a labeled tick at the top,
    // re-read from the clock on every render/poll so it tracks the real time. The
    // red distinguishes it from the cyan time cursor.
    const now = Date.now();
    if (now >= t0 && now <= t1) {
      const nx = x(now);
      svg.appendChild(el("line", { x1: nx, y1: HEADER_H, x2: nx, y2: totalH, class: "tl-now" }));
      // Top tick + "NOW" label (a small notch riding the header band).
      svg.appendChild(el("rect", { x: nx - 2, y: HEADER_H - 4, width: 4, height: 4, class: "tl-now-tick" }));
      const lbl = el("text", { x: nx + 4, y: HEADER_H - 4, class: "tl-now-label" });
      lbl.textContent = "NOW";
      svg.appendChild(lbl);
    }

    // Clamp the time cursor into the (possibly changed) window; it's drawn last
    // so it overlays the bars.
    tSel = Math.max(t0, Math.min(t1, tSel));

    // Bars.
    for (const lane of laid) {
      for (const it of lane.items) {
        const bx = x(it.s), bw = Math.max(2, x(it.e) - bx);
        const by = lane.y + LANE_PAD + it.row * ROW_H + 1;
        const fill = colorFor(it.r);
        const rect = el("rect", { x: bx, y: by, width: bw, height: ROW_H - 3, rx: 0, class: "tl-bar", fill, stroke: "rgba(0,0,0,.5)" });
        if (it.r.t_end == null) rect.setAttribute("opacity", "0.85");   // open-ended hint
        rect.addEventListener("mousemove", (ev) => showTip(ev, it.r));
        rect.addEventListener("mouseleave", hideTip);
        svg.appendChild(rect);
        if (bw > 42) {
          const t = el("text", { x: bx + 5, y: by + ROW_H - 7, class: "tl-bar-label" });
          t.textContent = it.r.vessel_name || `#${it.r.id}`;
          svg.appendChild(t);
        }
      }
    }

    // Time cursor on top of everything; keep live refs so dragging only moves
    // these two nodes (no full SVG rebuild per mouse-move).
    const cx = x(tSel);
    cursorLine = el("line", { x1: cx, y1: HEADER_H - 6, x2: cx, y2: totalH, class: "tl-cursor" });
    cursorHandle = el("rect", { x: cx - 5, y: HEADER_H - 11, width: 10, height: 10, rx: 0, class: "tl-cursor-handle" });
    svg.appendChild(cursorLine);
    svg.appendChild(cursorHandle);
    updateCursorLabel();
    syncMap();
  }

  function showTip(ev, r) {
    const sta = r.station_unassigned ? "unassigned" : `POPA ${fmtSta(r.station_lo)}–${fmtSta(r.station_hi)}`;
    tip.innerHTML =
      `<b>${esc(r.vessel_name || "(unnamed)")}</b>${r.vessel_imo ? ` <span class="k">IMO ${esc(r.vessel_imo)}</span>` : ""}<br>` +
      `<span class="k">${esc(r.type)} · ${esc(r.status)} · via ${esc(r.source)}</span><br>` +
      `${fmtDT(r.t_start)} → ${r.t_end ? fmtDT(r.t_end) : "open"}<br>` +
      `<span class="k">${esc(sta)}</span>` +
      (r.cargo ? `<br>${esc(r.cargo)}` : "") + (r.notes ? `<br><span class="k" style="white-space:pre-line">${esc(r.notes)}</span>` : "");
    tip.style.display = "block";
    const pad = 14, w = tip.offsetWidth, h = tip.offsetHeight;
    let lx = ev.clientX + pad, ly = ev.clientY + pad;
    if (lx + w > innerWidth) lx = ev.clientX - w - pad;
    if (ly + h > innerHeight) ly = ev.clientY - h - pad;
    tip.style.left = lx + "px"; tip.style.top = ly + "px";
  }
  function hideTip() { tip.style.display = "none"; }

  function renderLegend() {
    document.getElementById("tlLegend").innerHTML = LEGEND.map(
      ([k, lbl]) => `<span><i style="background:${k === "__dredge" ? DREDGE : STATUS[k]}"></i>${lbl}</span>`
    ).join("");
  }

  async function load() {
    // Collapsed: body is hidden and render() no-ops, so skip the /reservations
    // round-trip entirely (the 15s poll calls this too). A fresh load fires on
    // expand via setCollapsed().
    if (drawer.classList.contains("tl-collapsed")) return;
    emptyEl.textContent = "no reservations in this window";   // clear any prior DB-offline text
    try {
      const from = new Date(t0).toISOString(), to = new Date(t1).toISOString();
      rows = await api(`/reservations?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}&limit=500`);
      render();
      if (updatedEl) updatedEl.textContent = "updated " + new Date().toLocaleTimeString();
    } catch (e) {
      rows = []; render();
      emptyEl.textContent = "unavailable (DB offline)"; emptyEl.style.display = "block";
    }
  }

  // --- window controls ---
  function setWindow(a, b) { t0 = a; t1 = b; load(); }
  function startOfWeek(d) { const x = new Date(d); x.setHours(0, 0, 0, 0); x.setDate(x.getDate() - ((x.getDay() + 6) % 7)); return x.getTime(); }
  drawer.querySelectorAll("[data-preset]").forEach((b) => b.addEventListener("click", () => {
    const now = Date.now(), p = b.dataset.preset;
    if (p === "3d") setWindow(now - 3 * DAY, now + 3 * DAY);
    else if (p === "week") setWindow(startOfWeek(now), startOfWeek(now) + 7 * DAY);
    else if (p === "7d") setWindow(now, now + 7 * DAY);
    else if (p === "30d") setWindow(now, now + 30 * DAY);
    setMode(p);
  }));
  drawer.querySelectorAll("[data-pan]").forEach((b) => b.addEventListener("click", () => {
    const d = (t1 - t0) / 2 * Number(b.dataset.pan); setWindow(t0 + d, t1 + d);
    setMode("custom");
  }));
  document.getElementById("tlToday").addEventListener("click", () => { setWindow(Date.now() - 2 * DAY, Date.now() + 7 * DAY); setMode("today"); });
  document.getElementById("tlHideCancelled").addEventListener("change", (e) => { hideCancelled = e.target.checked; render(); });

  // --- time cursor + playback ---
  const cursorLabel = document.getElementById("tlCursorLabel");
  const playBtn = document.getElementById("tlPlay");
  const speedSel = document.getElementById("tlSpeed");

  function updateCursorLabel() { if (cursorLabel) cursorLabel.textContent = fmtDT(new Date(tSel).toISOString()); }
  function positionCursor() {
    if (!cursorLine) return;
    const cx = LABEL_W + (Math.max(t0, Math.min(t1, tSel)) - t0) / (t1 - t0) * plotW;
    cursorLine.setAttribute("x1", cx); cursorLine.setAttribute("x2", cx);
    cursorHandle.setAttribute("x", cx - 5);
  }
  function syncMap() { renderOutlines(tSel, rows); }
  function setCursor(ms) {
    tSel = Math.max(t0, Math.min(t1, ms));
    localStorage.setItem("tlCursor", String(Math.round(tSel)));
    positionCursor(); updateCursorLabel(); syncMap();
  }
  function timeAt(clientX) {
    const r = svg.getBoundingClientRect();
    const frac = (clientX - r.left - LABEL_W) / plotW;
    return t0 + Math.max(0, Math.min(1, frac)) * (t1 - t0);
  }

  // Drag the cursor anywhere on the plot (a mousedown also lands on bars — fine,
  // it just sets the time there). Move/up are global so the drag survives leaving
  // the svg. Cheap: only the two cursor nodes move, no SVG rebuild.
  let cursoring = false;
  svg.addEventListener("mousedown", (e) => { stopPlay(); cursoring = true; setCursor(timeAt(e.clientX)); e.preventDefault(); });
  addEventListener("mousemove", (e) => { if (cursoring) setCursor(timeAt(e.clientX)); });
  addEventListener("mouseup", () => { cursoring = false; });

  // Play: sweep the cursor at <speed> sim-hours per real second; stop at t1.
  let playing = false, playRAF = 0, lastFrame = 0;
  function frame(ts) {
    if (!playing) return;
    if (lastFrame) {
      setCursor(tSel + ((ts - lastFrame) / 1000) * Number(speedSel.value) * H);
      if (tSel >= t1) { stopPlay(); return; }
    }
    lastFrame = ts;
    playRAF = requestAnimationFrame(frame);
  }
  function startPlay() {
    if (playing || drawer.classList.contains("tl-collapsed")) return;
    if (tSel >= t1) setCursor(t0);                 // rewind if parked at the end
    playing = true; lastFrame = 0; playBtn.textContent = "⏸";
    playRAF = requestAnimationFrame(frame);
  }
  function stopPlay() {
    if (!playing) return;
    playing = false; cancelAnimationFrame(playRAF); playBtn.textContent = "▶";
  }
  playBtn.addEventListener("click", () => (playing ? stopPlay() : startPlay()));

  // --- collapse / resize ---
  function invalidateMap() { if (map.invalidateSize) setTimeout(() => map.invalidateSize(), 0); }
  function setCollapsed(c) {
    if (c) stopPlay();                              // don't sweep a hidden timeline
    drawer.classList.toggle("tl-collapsed", c);
    stage.classList.toggle("drawer-collapsed", c);
    document.getElementById("tlToggle").textContent = c ? "▸" : "▾";
    localStorage.setItem("tlCollapsed", c ? "1" : "0");
    invalidateMap();
    if (!c) load();   // expanding: fetch fresh data the poll skipped while collapsed
  }
  document.getElementById("tlToggle").addEventListener("click", () => setCollapsed(!drawer.classList.contains("tl-collapsed")));

  (function resizable() {
    const handle = document.getElementById("tlResize");
    let dragging = false;
    const apply = (h) => { stage.style.setProperty("--drawerH", h + "px"); };
    const saved = Number(localStorage.getItem("tlHeight"));
    apply(saved && saved > 120 ? saved : 300);
    handle.addEventListener("mousedown", (e) => { dragging = true; e.preventDefault(); document.body.style.cursor = "row-resize"; });
    addEventListener("mousemove", (e) => {
      if (!dragging) return;
      // Live-resize the container only; a full SVG rebuild per mouse-move is
      // wasteful (up to 500 bars × every pixel). Re-render once on drop.
      const h = Math.max(120, Math.min(innerHeight * 0.7, innerHeight - e.clientY));
      apply(h);
    });
    addEventListener("mouseup", () => {
      if (!dragging) return; dragging = false; document.body.style.cursor = "";
      localStorage.setItem("tlHeight", parseInt(stage.style.getPropertyValue("--drawerH")) || 300);
      invalidateMap(); render();
    });
  })();

  let rz; addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(render, 150); });

  renderLegend();
  setMode("today");   // default window matches the Today preset
  setCollapsed(localStorage.getItem("tlCollapsed") === "1");
  return load;
})();
