// Interactive tutorial. The header "? Tutorial" button opens a chapter menu
// (#tutorialModal); each chapter is a spotlight tour over the REAL console: it
// dims the page, cuts a hole over the element being explained, opens the right
// tab / drawer / panel itself, and on "try it" steps waits for the user to do
// the real action before Next unlocks. Chapter 0 is an in-modal sandbox of the
// core model (every booking is a time × station rectangle).
//
// Read-only by design: the tour never submits a form or calls a write endpoint.
// The one live lookup (click-the-chart) is GET /geo-to-station, so stationing
// stays server-side. UI state the tour changes (tab, timeline drawer, legend,
// intake panel, sidebar) is snapshotted on start and restored on exit.
import { api, fmtSta, esc } from "./api.js";
import { map } from "./map.js";

const $ = (s) => document.querySelector(s);
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};

// --- UI drivers (click the real controls so app state stays consistent) ---
function showTab(name) {
  const b = $(`#sidebarTabs .tab[data-tab="${name}"]`);
  if (b && !b.classList.contains("active")) b.click();
}
function ensureSidebar() {
  if (document.body.classList.contains("sidebar-collapsed")) $("#sidebarToggle")?.click();
}
function ensureTimeline(open = true) {
  const d = $("#timelineDrawer");
  if (d && d.classList.contains("tl-collapsed") === open) $("#tlToggle")?.click();
}
const shipLegend = () => $(".map-legend:not(.depth-legend)");
function ensureLegend(open = true) {
  const l = shipLegend();
  if (l && l.classList.contains("collapsed") === open) l.querySelector(".lg-title")?.click();
}
function closeHistFilters() {
  if ($("#histFilterModal")?.classList.contains("open")) $("#histFilterClose")?.click();
}

// --- "try it" binders: each returns a cleanup fn and calls done() once ------
function onEvent(type, sel, test) {
  return (done) => {
    const h = (e) => {
      const t = e.target instanceof Element && e.target.closest(sel);
      if (t && (!test || test(t, e))) done();
    };
    document.addEventListener(type, h, true);
    return () => document.removeEventListener(type, h, true);
  };
}
const onClick = (sel, test) => onEvent("click", sel, test);

// Click the chart -> project the point onto the quay server-side.
function mapClickProbe(done, card) {
  const live = card.querySelector(".tour-live");
  // Listen on the container (capture), not map "click": berth polygons and
  // markers swallow Leaflet's map click, and the quay is exactly where they sit.
  const h = async (e) => {
    if (!(e.target instanceof Element) || e.target.closest(".leaflet-control, .leaflet-popup")) return;
    if (map.dragging?.moved?.()) return;   // the tail of a pan, not a pick
    const ll = map.mouseEventToLatLng(e);
    live.innerHTML = `<span class="tour-dim">projecting ${ll.lat.toFixed(5)}, ${ll.lng.toFixed(5)}…</span>`;
    try {
      const r = await api(`/geo-to-station?lat=${ll.lat}&lon=${ll.lng}`);
      live.innerHTML = r.popa_station == null
        ? `<span class="tour-dim">That point is off the measured wharf — try closer to the gold quay line.</span>`
        : `<div class="tour-readout">
             <div><span>POPA</span><b>${fmtSta(r.popa_station)}</b></div>
             <div><span>Corps</span><b>${fmtSta(r.corps)}</b></div>
             <div><span>Dock No.</span><b>${Math.round(r.dockno)}′</b></div>
           </div>
           <div class="tour-dim">Same spot, three rulers — converted server-side. Click again to compare.</div>`;
    } catch (err) {
      live.innerHTML = `<span class="tour-dim">lookup failed: ${esc(err.message)}</span>`;
    }
    done();
  };
  const box = map.getContainer();
  box.addEventListener("click", h, true);
  return () => box.removeEventListener("click", h, true);
}

// --- Chapters ---------------------------------------------------------------
// Step: { target, title, body, before?, try?: {hint, bind}, empty?, place? }
// target: selector or fn -> Element. Missing/zero-size target -> centered card
// with the `empty` note, so the tour never dead-ends on an empty dataset.
const CHAPTERS = [
  {
    id: "map", title: "Reading the map", blurb: "Rotated chart, quay rulers, ship dots, outlines, layers",
    steps: [
      { target: "#map", place: "inside", title: "The chart",
        body: `The chart is <b>rotated</b> so the wharf runs left↔right. The <b>gold line</b> is the measured
               quay centerline — the ruler everything is positioned on. <b>Yellow ticks</b> are the Dock No.
               feet painted on the quay; <b>red ticks</b> are canonical POPA stationing. Zoom in and the ticks get denser.` },
      { target: "#map", place: "inside", title: "One spot, three rulers",
        body: `POPA, Corps/USACE and Dock No. are three numbering systems for the same quay. Operators type
               <b>Dock No.</b> (what you read off the wharf); the server converts.`,
        try: { hint: "Click anywhere near the quay line on the chart", bind: mapClickProbe } },
      { target: () => shipLegend(), before: () => ensureLegend(true), title: "AIS ship dots",
        body: `Live AIS contacts, <b>coloured by ship type</b> (this key). State is shown by <b>motion</b>, not colour:
               <b>solid</b> = moored at our wharf, <b>hollow</b> = stopped off-berth, <b>pulsing ring</b> = underway.
               Contacts that have gone silent are hidden.` },
      { target: ".view-toggle", title: "Current vs Planned outlines",
        body: `To-scale hull outlines. <b>Current</b> draws what AIS says is alongside right now.
               <b>Planned</b> draws the confirmed bookings at the timeline cursor's moment — scrub time, watch the wharf fill.`,
        try: { hint: "Flip between Current and Planned", bind: onClick(".view-toggle button") } },
      { target: ".leaflet-control-layers", title: "Layers",
        body: `Hover this icon for the layer list: ship dots, outlines, berths, the <b>controlling-depth</b> overlay
               (shallow→deep ramp with bed-elevation labels), feasible-berth hints and the marker sets.` },
    ],
  },
  {
    id: "status", title: "Live status at a glance", blurb: "Counters, conflict strip, clock, worker health",
    steps: [
      { target: ".stats", before: ensureSidebar, title: "Live counters",
        body: `Vessels seen in 24 h, moored now, reservations, open requests, confirmed bookings, arrivals.
               Everything on screen refreshes every <b>15 s</b>.` },
      { target: "#conflictAlert", title: "The conflict strip",
        body: `The headline alert. It turns <b style="color:var(--amber)">amber</b>/<b style="color:var(--red)">red</b>
               the moment two bookings overlap in <i>both</i> time and station. Current &amp; upcoming only —
               resolved past overlaps live in History.` },
      { target: () => $("#svcCraftToggle")?.closest("label"), before: () => showTab("overview"),
        title: "Harbor craft filter",
        body: `Tugs, tows and pilots are hidden from the live panels by default so they stay a planning surface,
               not a log. Tick this to reveal them.` },
      { target: "#clock", title: "Central Time is canonical",
        body: `The whole system reads and writes <b>America/Chicago</b>. UTC is shown underneath for AIS cross-checks.
               Times you type without a zone are taken as Central.` },
      { target: "#workerChips", title: "Background workers",
        body: `One chip per worker (AIS ingest, occupancy, intake…): <b style="color:var(--green)">green</b> healthy,
               <b style="color:var(--amber)">amber</b> stale, <b style="color:var(--red)">red</b> error. Hover for detail.
               If AIS goes stale, "moored now" freezes rather than emptying — a dead feed is not a departure.`,
        empty: "Worker chips appear once the workers have reported in." },
    ],
  },
  {
    id: "overview", title: "Overview: what's happening now", blurb: "Conflicts, Alongside now, depth surveys",
    steps: [
      { target: () => $("#conflicts")?.closest("details"), before: () => { ensureSidebar(); showTab("overview"); },
        title: "Conflicts",
        body: `Each card is a pair of rectangles that collide. <b>Click one</b> to highlight the contested stretch on the
               chart. AIS-observed vs AIS-observed is never a conflict — AIS can't conflict with itself.` },
      { target: () => $("#alongside")?.closest("details"), title: "Alongside now",
        body: `Vessels physically at the wharf right now, each checked against its booking:
               <b>arrived</b>, <b>berthed elsewhere</b>, or <b>unplanned</b>. Hover a row for the ship dossier;
               bookings not yet arrived are listed under <b>Future planned</b>.`,
        try: { hint: "Click a vessel to locate it on the chart (cyan ping)", bind: onClick(".along-card") },
        empty: "Nobody is alongside right now — this fills from live AIS." },
      { target: "#depthPanel", before: () => { const d = $("#depthPanel"); if (d) d.open = true; },
        title: "Depth surveys",
        body: `Upload hydrographic <code>.XYZ</code> condition surveys here. The latest active survey is what the
               <b>draft gate</b> checks before any booking can be confirmed.` },
    ],
  },
  {
    id: "intake", title: "Capturing a berth request", blurb: "Phone / email / operator entry — walk the form",
    steps: [
      { target: '#sidebarTabs .tab[data-tab="requests"]', before: ensureSidebar, title: "Berth requests tab",
        body: `Every inbound request — phone, email, operator entry, and the AI-read mailbox — lands here.`,
        try: { hint: "Open the Berth requests tab", bind: onClick('.tab[data-tab="requests"]') } },
      { target: "#intakePanel", before: () => { showTab("requests"); $("#intakePanel").open = true; },
        title: "New berth request",
        body: `Type what the caller gave you. <b>The tour won't submit anything</b> — feel free to poke at the fields.` },
      { target: () => $('#berthRequestForm [name="imo"]')?.closest("fieldset"), title: "Vessel: IMO first",
        body: `<b>IMO</b> is the canonical key (names are non-unique and misspelled). Enter it — or start typing a
               <b>name</b> to pick a ship on file — and name + dimensions auto-fill. For AIS-tracked ships the
               feed owns LOA/beam/draft.`,
        try: { hint: "Type a few letters of a vessel name", bind: onEvent("input", '#berthRequestForm [name="vessel"]') } },
      { target: () => $('#berthRequestForm [name="etb"]')?.closest("fieldset"), title: "Schedule",
        body: `<b>ETB</b> is required, ETD optional. Times are Central.` },
      { target: () => $("#berthRequests")?.closest("details"), title: "What happens on submit",
        body: `The raw request is kept for audit, and a <code>requested</code> reservation is created with an
               <b>empty station range</b> — it blocks nothing until an operator places it. If the same IMO is already
               requested over an overlapping window, both cards get a <b>possible duplicate</b> flag; you reconcile
               (nothing is auto-merged).` },
    ],
  },
  {
    id: "place", title: "Place & confirm a booking", blurb: "Find berth, placement, the depth and overlap gates",
    steps: [
      { target: '#sidebarTabs .tab[data-tab="reservations"]', before: ensureSidebar, title: "Reservations tab",
        body: `Every booking, whatever its source.`,
        try: { hint: "Open the Reservations tab", bind: onClick('.tab[data-tab="reservations"]') } },
      { target: "#resFilter", before: () => showTab("reservations"), title: "Filter by status",
        body: `<b>requested</b> → <b>tentative</b> → <b>confirmed</b> → <b>completed</b> (or <b>cancelled</b>).`,
        try: { hint: "Pick a different status", bind: onEvent("change", "#resFilter") } },
      { target: () => $('#requests [data-act="find-berth"]')?.closest(".card"), title: "Find berth",
        body: `<b>Find berth</b> asks the feasibility oracle where this vessel fits in its window — free wharf minus
               everything occupying it, padded by the 75 ft mooring gap, depth-checked — and paints candidates on the
               chart. It <b>proposes</b>; you pick. Or use <b>⋯ → Edit placement</b>: type the bow in Dock No. feet and
               the heading, and the stern follows from LOA.`,
        empty: "No placeable bookings in this filter — set it to Requested or Active to see the Find berth button." },
      { target: "#requests", title: "Confirming: two gates",
        body: `Promoting to <b>Confirmed</b> runs two checks. <b>Draft vs controlling depth</b> (422 unless an operator
               overrides to a warning), and the database's <b>no-overlap constraint</b> — a confirmed booking that
               collides with another (inside the 75 ft gap) is refused with a <b>409</b>. Only confirmed rows are
               hard-guarded; see chapter 0 to play with it.` },
    ],
  },
  {
    id: "ships", title: "Ship particulars & the AIS override", blurb: "Saved ships, pinning dims, revert to AIS",
    steps: [
      { target: "#shipSearch", before: () => { ensureSidebar(); showTab("ships"); }, title: "Saved ships",
        body: `Every vessel on file, learned from AIS static data and manual entry.`,
        try: { hint: "Search by name or IMO", bind: onEvent("input", "#shipSearch") } },
      { target: "#vessels", title: "When AIS is wrong",
        body: `For an AIS-tracked ship the feed is <b>authoritative</b> on LOA/beam/draft — a plain edit is dropped.
               If AIS itself is wrong, edit the dimension and confirm the prompt to <b>pin</b> it: the ship shows an
               <b>Edited</b> badge and the ingestor stops reverting it. <b>Revert to AIS</b> restores the live values
               immediately. Ships with no MMSI own their dimensions outright.` },
    ],
  },
  {
    id: "timeline", title: "The occupancy timeline", blurb: "Gantt lanes, time cursor, playback",
    steps: [
      { target: "#timelineDrawer", before: () => ensureTimeline(true), title: "Berth occupancy",
        body: `A Gantt chart under the map: one lane per berth, bars coloured by status. Drag the drawer's top edge to
               resize; the caret collapses it.` },
      { target: () => $("#timelineDrawer .tl-seg"), title: "Window",
        body: `Presets and pan arrows move the time window; <b>Today</b> recenters on now.`,
        try: { hint: "Pick a window preset", bind: onClick("#timelineDrawer [data-preset], #timelineDrawer [data-pan]") } },
      { target: "#tlSvg", title: "NOW line & time cursor",
        body: `The <b style="color:var(--red)">red NOW</b> line is the current instant. The cursor is a moment you
               choose — it drives the map's <b>Planned</b> outlines.`,
        try: { hint: "Click or drag on the chart to move the cursor", bind: onEvent("mousedown", "#tlSvg") } },
      { target: "#tlPlay", title: "Play",
        body: `Sweeps the cursor forward (speed alongside). Switch the map to <b>Planned</b> to watch berths fill and empty.`,
        try: { hint: "Press Play (press again to pause)", bind: onClick("#tlPlay") } },
    ],
  },
  {
    id: "history", title: "History & the ship dossier", blurb: "Every booking ever, filters, hover dossier",
    steps: [
      { target: '#sidebarTabs .tab[data-tab="history"]', before: () => { ensureSidebar(); closeHistFilters(); },
        title: "History", body: `Every booking and every AIS-observed berthing, newest arrival first.`,
        try: { hint: "Open the History tab", bind: onClick('.tab[data-tab="history"]') } },
      { target: "#histFilterBtn", before: () => { showTab("history"); closeHistFilters(); }, title: "Filters",
        body: `Search by vessel name, IMO, status or date range.`,
        try: { hint: "Open Filters", bind: onClick("#histFilterBtn") } },
      { target: "#histFilterModal .sheet",
        before: () => { if (!$("#histFilterModal").classList.contains("open")) $("#histFilterBtn")?.click(); },
        title: "Narrow it down", body: `Fill any combination and hit <b>Search</b>; <b>Clear</b> resets.` },
      { target: "#historyList", before: closeHistFilters, title: "Hover for the dossier",
        body: `Hover any row for the full ship dossier — the vessel record, its reservation log and the latest AIS fix.`,
        try: { hint: "Hover a history row", bind: onEvent("mouseover", "#historyList .hist-card") },
        empty: "No history yet — it fills as AIS observes berthings and bookings are made." },
    ],
  },
];

// --- Spotlight engine -------------------------------------------------------
const engine = (function () {
  let layer, spot, card, steps = [], i = 0, cleanup = null, raf = 0, snap = null, onFinish = null;
  let lastRect = "";

  function build() {
    layer = document.createElement("div");
    layer.id = "tourLayer";
    layer.innerHTML = `<div class="tour-spot"></div><div class="tour-card" role="dialog" aria-live="polite"></div>`;
    document.body.appendChild(layer);
    spot = layer.querySelector(".tour-spot");
    card = layer.querySelector(".tour-card");
    card.addEventListener("click", (e) => {
      const a = e.target.closest("[data-tour]");
      if (!a) return;
      ({ next, back: () => go(i - 1), skip: next, close: () => stop(false) })[a.dataset.tour]();
    });
  }

  function snapshot() {
    return {
      tab: $("#sidebarTabs .tab.active")?.dataset.tab,
      tlCollapsed: $("#timelineDrawer")?.classList.contains("tl-collapsed"),
      legendCollapsed: shipLegend()?.classList.contains("collapsed"),
      intakeOpen: $("#intakePanel")?.open,
      depthOpen: $("#depthPanel")?.open,
      sidebarCollapsed: document.body.classList.contains("sidebar-collapsed"),
    };
  }
  function restore(s) {
    if (!s) return;
    closeHistFilters();
    if (s.tab) showTab(s.tab);
    if (s.tlCollapsed != null) ensureTimeline(!s.tlCollapsed);
    if (s.legendCollapsed != null) ensureLegend(!s.legendCollapsed);
    if ($("#intakePanel")) $("#intakePanel").open = !!s.intakeOpen;
    if ($("#depthPanel")) $("#depthPanel").open = !!s.depthOpen;
    if (s.sidebarCollapsed !== document.body.classList.contains("sidebar-collapsed")) $("#sidebarToggle")?.click();
  }

  const resolve = (t) => (typeof t === "function" ? t() : t ? $(t) : null);
  const visible = (el) => { if (!el) return null; const r = el.getBoundingClientRect(); return r.width > 2 && r.height > 2 ? r : null; };

  function start(stepList, finish) {
    if (!layer) build();
    if (!snap) snap = snapshot();
    steps = stepList; onFinish = finish; i = 0;
    layer.classList.add("on");
    addEventListener("keydown", onKey, true);
    go(0);
    cancelAnimationFrame(raf); raf = requestAnimationFrame(track);
  }

  function stop(completed) {
    if (cleanup) { cleanup(); cleanup = null; }
    cancelAnimationFrame(raf);
    removeEventListener("keydown", onKey, true);
    layer?.classList.remove("on");
    restore(snap); snap = null;
    const f = onFinish; onFinish = null;
    if (f) f(completed);
  }

  function next() { if (i >= steps.length - 1) stop(true); else go(i + 1); }

  function go(n) {
    if (n < 0 || n >= steps.length) return;
    if (cleanup) { cleanup(); cleanup = null; }
    const prevChap = steps[i]?.chapter, forward = n > i;
    i = n;
    const s = steps[i];
    if (forward && prevChap && prevChap !== s.chapter) markDone(prevChap);
    try { s.before?.(); } catch { /* a missing control shouldn't break the tour */ }
    // Let tab switches / drawer expansion lay out, then bring the target on screen.
    setTimeout(() => {
      const el = resolve(s.target);
      if (el && visible(el)) el.scrollIntoView({ block: "nearest", inline: "nearest" });
    }, 30);
    renderCard(s);
    lastRect = "";
  }

  function renderCard(s) {
    const pct = Math.round(((i + 1) / steps.length) * 100);
    const last = i === steps.length - 1;
    card.innerHTML = `
      <div class="tour-head">
        <span class="tour-chap">${esc(s.chapterTitle)}</span>
        <span class="tour-count">${i + 1} / ${steps.length}</span>
        <button type="button" class="tour-x" data-tour="close" title="End tour (Esc)">×</button>
      </div>
      <div class="tour-prog"><i style="width:${pct}%"></i></div>
      <h4>${s.title}</h4>
      <div class="tour-text">${s.body}</div>
      <div class="tour-empty" hidden>${s.empty ? esc(s.empty) : "Nothing to point at right now — this area fills in with live data."}</div>
      ${s.try ? `<div class="tour-await"><span class="tour-pulse"></span><span>Your turn: ${esc(s.try.hint)}</span></div>` : ""}
      <div class="tour-live"></div>
      <div class="tour-foot">
        <button type="button" class="btn-sm" data-tour="back" ${i === 0 ? "disabled" : ""}>‹ Back</button>
        <span class="tour-keys">← → keys</span>
        ${s.try ? `<button type="button" class="tour-skip" data-tour="skip">skip</button>` : ""}
        <button type="button" class="btn-sm primary" data-tour="next" ${s.try ? "disabled" : ""}>${last ? "Finish ✓" : "Next ›"}</button>
      </div>`;
    if (s.try) {
      cleanup = s.try.bind(() => {
        const a = card.querySelector(".tour-await");
        if (a && !a.classList.contains("done")) {
          a.classList.add("done");
          a.querySelector("span:last-child").textContent = "✓ Nice — that's it.";
        }
        const nb = card.querySelector('[data-tour="next"]');
        if (nb) nb.disabled = false;
      }, card);
    }
  }

  // Re-measure every frame while active: the target can move (sidebar resize,
  // drawer animation, list refresh on the 15s poll). Cheap: one rect read.
  function track() {
    raf = requestAnimationFrame(track);
    const s = steps[i];
    if (!s) return;
    const r = visible(resolve(s.target));
    const key = r ? `${r.left|0},${r.top|0},${r.width|0},${r.height|0}` : "none";
    const cw = card.offsetWidth, ch = card.offsetHeight;
    const k2 = `${key}|${cw}|${ch}|${innerWidth}|${innerHeight}`;
    if (k2 === lastRect) return;
    lastRect = k2;
    card.querySelector(".tour-empty")?.toggleAttribute("hidden", !!r);
    place(r, s.place);
  }

  function place(r, mode) {
    const PAD = 6, GAP = 14, M = 12, W = innerWidth, H = innerHeight;
    const cw = card.offsetWidth, ch = card.offsetHeight;
    if (!r) {
      Object.assign(spot.style, { left: W / 2 + "px", top: H / 2 + "px", width: "0px", height: "0px" });
      spot.classList.add("none");
      Object.assign(card.style, { left: (W - cw) / 2 + "px", top: (H - ch) / 2 + "px" });
      return;
    }
    spot.classList.remove("none");
    Object.assign(spot.style, {
      left: r.left - PAD + "px", top: r.top - PAD + "px",
      width: r.width + PAD * 2 + "px", height: r.height + PAD * 2 + "px",
    });
    const clampX = (x) => Math.max(M, Math.min(W - cw - M, x));
    const clampY = (y) => Math.max(M, Math.min(H - ch - M, y));
    let x, y;
    if (mode !== "inside" && W - r.right - GAP >= cw + M) { x = r.right + GAP; y = clampY(r.top); }
    else if (mode !== "inside" && r.left - GAP >= cw + M) { x = r.left - GAP - cw; y = clampY(r.top); }
    else if (mode !== "inside" && H - r.bottom - GAP >= ch + M) { x = clampX(r.left); y = r.bottom + GAP; }
    else if (mode !== "inside" && r.top - GAP >= ch + M) { x = clampX(r.left); y = r.top - GAP - ch; }
    else { x = clampX(r.right - cw - 20); y = clampY(r.bottom - ch - 20); }   // big target: sit inside its corner
    Object.assign(card.style, { left: x + "px", top: y + "px" });
  }

  function onKey(e) {
    const typing = e.target instanceof Element && e.target.closest("input, select, textarea");
    if (e.key === "Escape") { e.stopPropagation(); stop(false); return; }
    if (typing) return;
    if (e.key === "ArrowRight") {
      const nb = card.querySelector('[data-tour="next"]');
      if (nb && !nb.disabled) { e.preventDefault(); next(); }
    } else if (e.key === "ArrowLeft") { e.preventDefault(); go(i - 1); }
  }

  return { start, active: () => !!layer?.classList.contains("on") };
})();

// --- Progress ---------------------------------------------------------------
function doneSet() {
  try { return new Set(JSON.parse(store.get("tourDone") || "[]")); } catch { return new Set(); }
}
function markDone(id) {
  const d = doneSet(); d.add(id);
  store.set("tourDone", JSON.stringify([...d]));
}
const stepsFor = (chs) => chs.flatMap((c) => c.steps.map((s) => ({ ...s, chapter: c.id,
  chapterTitle: `Ch ${CHAPTERS.indexOf(c) + 1} · ${c.title}` })));

// --- Chapter 0: time × station sandbox ---------------------------------------
// Abstract axes (hours × feet) — an illustration of the conflict primitive, not
// a stationing conversion. Mirrors the server's semantics: /conflicts compares
// RAW ranges; the DB exclusion constraint (confirmed-only) pads each range by
// half of migration 0007's 75 ft mooring gap; observed-vs-observed never conflicts.
function initDemo(root) {
  const GAP = 75, T = 72, S = 1200;                     // 72 h × 1200 ft canvas
  const VW = 600, VH = 250, L = 46, R = 10, TOP = 10, B = 26;
  const pw = VW - L - R, ph = VH - TOP - B;
  const X = (h) => L + (h / T) * pw, Y = (f) => TOP + (f / S) * ph;
  const PRESETS = {
    overlap:  [{ t0: 8, t1: 40, s0: 200, s1: 700 }, { t0: 28, t1: 60, s0: 500, s1: 950 }],
    space:    [{ t0: 8, t1: 40, s0: 150, s1: 550 }, { t0: 20, t1: 56, s0: 700, s1: 1100 }],
    time:     [{ t0: 4, t1: 30, s0: 300, s1: 800 }, { t0: 36, t1: 66, s0: 400, s1: 900 }],
    tooClose: [{ t0: 8, t1: 44, s0: 150, s1: 600 }, { t0: 20, t1: 60, s0: 640, s1: 1050 }],
  };
  const boxes = [
    { name: "Vessel A", status: "confirmed" },
    { name: "Dredge op", status: "confirmed" },
  ];
  const setPreset = (k) => PRESETS[k].forEach((p, j) => Object.assign(boxes[j], p));
  setPreset("tooClose");

  root.innerHTML = `
    <div class="demo-bar">
      <span class="demo-lbl">Scenario</span>
      <button type="button" class="btn-sm" data-preset="overlap">Overlap</button>
      <button type="button" class="btn-sm" data-preset="space">Same time, apart</button>
      <button type="button" class="btn-sm" data-preset="time">Same berth, back-to-back</button>
      <button type="button" class="btn-sm" data-preset="tooClose">Too close</button>
    </div>
    <svg class="demo-svg" viewBox="0 0 ${VW} ${VH}" role="img" aria-label="Time by station sandbox"></svg>
    <div class="demo-bar">
      ${boxes.map((b, j) => `<label class="demo-lbl">${b.name}
        <select data-box="${j}">${["observed", "requested", "confirmed"].map((s) =>
          `<option ${s === b.status ? "selected" : ""}>${s}</option>`).join("")}</select></label>`).join("")}
    </div>
    <div class="demo-verdict"></div>`;
  const svg = root.querySelector("svg"), verdict = root.querySelector(".demo-verdict");
  const NS = "http://www.w3.org/2000/svg";
  const el = (n, a, txt) => { const e = document.createElementNS(NS, n); for (const k in a) e.setAttribute(k, a[k]); if (txt != null) e.textContent = txt; return e; };

  function evaluate() {
    const [a, b] = boxes;
    const tOv = a.t0 < b.t1 && b.t0 < a.t1;
    const sRaw = a.s0 < b.s1 && b.s0 < a.s1;
    const clear = Math.max(b.s0 - a.s1, a.s0 - b.s1);   // hull-to-hull feet (negative = overlap)
    const sGap = clear < GAP;
    const bothConf = a.status === "confirmed" && b.status === "confirmed";
    const bothObs = a.status === "observed" && b.status === "observed";
    const chip = (ok, txt) => `<span class="demo-chip ${ok ? "hit" : ""}">${ok ? "✓" : "✗"} ${txt}</span>`;
    let cls = "ok", msg;
    if (!tOv) msg = "<b>No conflict.</b> They never share the wharf at the same time.";
    else if (!sGap) msg = `<b>No conflict.</b> Same time, but ${clear} ft apart — clear of the 75 ft gap.`;
    else if (bothObs) { cls = "dim"; msg = "<b>Not a conflict.</b> Observed vs observed — AIS can't conflict with itself."; }
    else if (bothConf) { cls = "bad"; msg = sRaw
        ? "<b>Refused (409).</b> The database will not hold two confirmed bookings on the same wharf at the same time."
        : `<b>Refused (409).</b> Hulls only ${clear} ft apart — inside the 75 ft mooring gap the constraint enforces.`; }
    else if (sRaw) { cls = "warn"; msg = "<b>Flagged on Conflicts — not blocked.</b> Only confirmed-vs-confirmed is hard-blocked; this overlap is a signal for the operator."; }
    else { cls = "warn"; msg = `<b>Allowed for now.</b> ${clear} ft apart is under the 75 ft gap — you couldn't confirm both.`; }
    verdict.className = "demo-verdict " + cls;
    verdict.innerHTML = `<div>${chip(tOv, "time overlap")}${chip(sRaw, "station overlap")}${
      tOv && !sRaw ? chip(sGap, `within 75 ft gap (${clear} ft)`) : ""}</div><div>${msg}</div>`;
    return tOv && sGap;
  }

  function draw() {
    const hit = evaluate();
    svg.replaceChildren();
    for (let h = 0; h <= T; h += 12) {
      svg.append(el("line", { x1: X(h), x2: X(h), y1: TOP, y2: TOP + ph, class: "demo-grid" }));
      svg.append(el("text", { x: X(h), y: VH - 10, class: "demo-ax", "text-anchor": "middle" }, `${h}h`));
    }
    for (let f = 0; f <= S; f += 300) {
      svg.append(el("line", { x1: L, x2: L + pw, y1: Y(f), y2: Y(f), class: "demo-grid" }));
      svg.append(el("text", { x: L - 6, y: Y(f) + 3, class: "demo-ax", "text-anchor": "end" }, `${f}′`));
    }
    svg.append(el("text", { x: 10, y: TOP + ph / 2, class: "demo-ax", transform: `rotate(-90 10 ${TOP + ph / 2})`, "text-anchor": "middle" }, "station"));
    boxes.forEach((b, j) => {
      const g = el("g", { "data-j": j, class: "demo-box" });
      g.append(el("rect", { x: X(b.t0), y: Y(b.s0 - GAP / 2), width: X(b.t1) - X(b.t0),
        height: Y(b.s1 + GAP / 2) - Y(b.s0 - GAP / 2), class: "demo-halo" }));
      g.append(el("rect", { x: X(b.t0), y: Y(b.s0), width: X(b.t1) - X(b.t0), height: Y(b.s1) - Y(b.s0),
        class: "demo-rect", style: `fill:var(--st-${b.status});stroke:var(--st-${b.status})`, "data-act": "move" }));
      g.append(el("text", { x: X(b.t0) + 6, y: Y(b.s0) + 14, class: "demo-name" }, `${b.name} · ${b.status}`));
      g.append(el("rect", { x: X(b.t1) - 9, y: Y(b.s1) - 9, width: 9, height: 9, class: "demo-handle", "data-act": "size" }));
      svg.append(g);
    });
    if (hit) {
      const [a, b] = boxes;
      const t0 = Math.max(a.t0, b.t0), t1 = Math.min(a.t1, b.t1);
      const s0 = Math.max(a.s0, b.s0) - (a.s0 < b.s1 && b.s0 < a.s1 ? 0 : GAP / 2);
      const s1 = Math.min(a.s1, b.s1) + (a.s0 < b.s1 && b.s0 < a.s1 ? 0 : GAP / 2);
      svg.append(el("rect", { x: X(t0), y: Y(Math.min(s0, s1)), width: X(t1) - X(t0),
        height: Math.abs(Y(s1) - Y(s0)), class: "demo-hit" }));
    }
  }

  // Drag: body moves, corner handle resizes. Snap 1 h / 25 ft.
  let drag = null;
  const toData = (e) => {
    const p = new DOMPoint(e.clientX, e.clientY).matrixTransform(svg.getScreenCTM().inverse());
    return { h: ((p.x - L) / pw) * T, f: ((p.y - TOP) / ph) * S };
  };
  svg.addEventListener("pointerdown", (e) => {
    const g = e.target.closest(".demo-box"), act = e.target.dataset.act;
    if (!g || !act) return;
    const b = boxes[+g.dataset.j];
    drag = { b, act, start: toData(e), orig: { ...b } };
    svg.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  svg.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const p = toData(e), { b, orig, start } = drag;
    const dh = Math.round(p.h - start.h), df = Math.round((p.f - start.f) / 25) * 25;
    if (drag.act === "move") {
      const w = orig.t1 - orig.t0, hgt = orig.s1 - orig.s0;
      b.t0 = Math.max(0, Math.min(T - w, orig.t0 + dh)); b.t1 = b.t0 + w;
      b.s0 = Math.max(0, Math.min(S - hgt, orig.s0 + df)); b.s1 = b.s0 + hgt;
    } else {
      b.t1 = Math.max(b.t0 + 4, Math.min(T, orig.t1 + dh));
      b.s1 = Math.max(b.s0 + 100, Math.min(S, orig.s1 + df));
    }
    draw();
  });
  const end = () => { drag = null; };
  svg.addEventListener("pointerup", end);
  svg.addEventListener("pointercancel", end);
  root.addEventListener("click", (e) => {
    const p = e.target.closest("[data-preset]");
    if (p) { setPreset(p.dataset.preset); draw(); }
  });
  root.addEventListener("change", (e) => {
    const s = e.target.closest("select[data-box]");
    if (s) { boxes[+s.dataset.box].status = s.value; draw(); }
  });
  draw();
}

// --- Menu modal + first-visit nudge -----------------------------------------
export function initTutorial() {
  const modal = $("#tutorialModal"), openBtn = $("#tutorialBtn"), closeBtn = $("#tutorialClose");
  const list = $("#tutChapters");
  if (!modal || !openBtn || !list) return;

  const renderMenu = () => {
    const done = doneSet();
    list.innerHTML = CHAPTERS.map((c, n) => `
      <button type="button" class="tut-ch${done.has(c.id) ? " done" : ""}" data-ch="${c.id}">
        <span class="tut-num">${done.has(c.id) ? "✓" : n + 1}</span>
        <span class="tut-ch-t"><b>${esc(c.title)}</b><span>${esc(c.blurb)}</span></span>
        <span class="tut-ch-n">${c.steps.length} steps ›</span>
      </button>`).join("");
  };
  const open = () => { renderMenu(); modal.classList.add("open"); dismissNudge(); };
  const close = () => modal.classList.remove("open");

  const run = (chs) => {
    close();
    engine.start(stepsFor(chs), (completed) => {
      if (!completed) return;
      markDone(chs[chs.length - 1].id);
      open();   // back to the menu so the next chapter is one click away
    });
  };

  openBtn.addEventListener("click", open);
  closeBtn?.addEventListener("click", close);
  modal.addEventListener("click", (e) => { if (e.target === modal) close(); });
  addEventListener("keydown", (e) => {
    if (e.key === "Escape" && modal.classList.contains("open") && !engine.active()) close();
  });
  list.addEventListener("click", (e) => {
    const b = e.target.closest("[data-ch]");
    if (b) run([CHAPTERS.find((c) => c.id === b.dataset.ch)]);
  });
  $("#tutStartAll")?.addEventListener("click", () => run(CHAPTERS));
  const demo = $("#tutDemo");
  if (demo) initDemo(demo);

  // One-time nudge beside the button; never auto-launches the overlay.
  let nudge = null;
  function dismissNudge() { nudge?.remove(); nudge = null; store.set("tourNudged", "1"); }
  if (!store.get("tourNudged")) {
    nudge = document.createElement("div");
    nudge.className = "tour-nudge";
    nudge.innerHTML = `New here? <button type="button" class="btn-sm primary">Take the tour</button>
                       <button type="button" class="tour-x" title="Dismiss">×</button>`;
    document.body.appendChild(nudge);
    const r = openBtn.getBoundingClientRect();
    Object.assign(nudge.style, { left: r.left + "px", top: r.bottom + 8 + "px" });
    nudge.querySelector(".primary").addEventListener("click", open);
    nudge.querySelector(".tour-x").addEventListener("click", dismissNudge);
  }
}
