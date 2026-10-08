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
    live.innerHTML = `<span class="tour-dim">looking up that spot…</span>`;
    try {
      const r = await api(`/geo-to-station?lat=${ll.lat}&lon=${ll.lng}`);
      live.innerHTML = r.popa_station == null
        ? `<span class="tour-dim">That spot isn't on the wharf — try clicking closer to the gold line.</span>`
        : `<div class="tour-readout">
             <div><span>Dock No.</span><b>${Math.round(r.dockno)}′</b></div>
             <div><span>POPA</span><b>${fmtSta(r.popa_station)}</b></div>
             <div><span>Corps</span><b>${fmtSta(r.corps)}</b></div>
           </div>
           <div class="tour-dim">The dock number is the one painted on the wharf. The other two are the
             same spot in the port's and the Corps' numbering. Click again to try another spot.</div>`;
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
    id: "map", title: "Reading the map", blurb: "The wharf, the ships, and the map buttons",
    steps: [
      { target: "#map", place: "inside", title: "The map",
        body: `The map is turned so the wharf runs <b>left to right</b>. The <b>gold line</b> is the face of the wharf.
               The <b>yellow marks</b> are the dock numbers painted on the wharf. Zoom in to see more of them.` },
      { target: "#map", place: "inside", title: "Where is that?",
        body: `Every spot on the wharf has a <b>dock number</b> in feet — the same numbers painted on the dock.
               Click the map and the console tells you the dock number for that spot.`,
        try: { hint: "Click anywhere near the gold wharf line", bind: mapClickProbe } },
      { target: () => shipLegend(), before: () => ensureLegend(true), title: "Ship dots",
        body: `Each dot is a ship, placed by its live tracking signal (AIS). The <b>colour</b> tells you the kind of ship —
               this key explains them. A <b>solid</b> dot is tied up at our wharf, a <b>hollow</b> dot is stopped somewhere
               else, and a <b>pulsing</b> dot is moving.` },
      { target: ".view-toggle", title: "Now vs. planned",
        body: `These buttons switch the ship shapes on the map. <b>Current</b> shows the ships tied up right now.
               <b>Planned</b> shows the confirmed bookings for the time picked on the schedule below the map.`,
        try: { hint: "Click Current, then Planned", bind: onClick(".view-toggle button") } },
      { target: ".leaflet-control-layers", title: "Turning things on and off",
        body: `Point at this button to choose what the map shows — ship dots, ship shapes, berth names, water depth,
               and the dock-number marks.` },
    ],
  },
  {
    id: "status", title: "The top of the screen", blurb: "Counts, the clash warning, the clock",
    steps: [
      { target: ".stats", before: ensureSidebar, title: "Quick counts",
        body: `How many ships came by today, how many are tied up now, and how many bookings and requests there are.
               The screen updates itself every 15 seconds — no need to refresh.` },
      { target: "#conflictAlert", title: "Clash warning",
        body: `This bar turns <b style="color:var(--amber)">yellow</b> or <b style="color:var(--red)">red</b> when two ships
               are booked into the <b>same spot at the same time</b>. If it's quiet, there are no clashes.` },
      { target: () => $("#svcCraftToggle")?.closest("label"), before: () => showTab("overview"),
        title: "Tugs and pilot boats",
        body: `Tugs, tows and pilot boats are hidden from the lists so they don't crowd things out. Tick this box to show them.` },
      { target: "#clock", title: "Local time",
        body: `All times in the console are <b>Port Arthur local time</b> (Central). The second line is UTC, in case you
               need to match a ship's report.` },
      { target: "#workerChips", title: "Is the data live?",
        body: `These lights show whether the live feeds are working. <b style="color:var(--green)">Green</b> means all good.
               <b style="color:var(--amber)">Yellow</b> or <b style="color:var(--red)">red</b> means the information may be
               out of date — let the system admin know.`,
        empty: "These lights appear once the live feeds have started." },
    ],
  },
  {
    id: "overview", title: "What's happening now", blurb: "Clashes, ships at the wharf, depth",
    steps: [
      { target: () => $("#conflicts")?.closest("details"), before: () => { ensureSidebar(); showTab("overview"); },
        title: "Clashes",
        body: `Each card here is two bookings that want the same spot at the same time. <b>Click one</b> and the map
               shows you where.` },
      { target: () => $("#alongside")?.closest("details"), title: "Ships at the wharf now",
        body: `Every ship tied up right now, and whether it matches its booking: <b>arrived</b> as planned, tied up
               <b>somewhere else</b>, or <b>no booking</b> at all. Ships that are booked but haven't arrived yet are listed
               underneath.`,
        try: { hint: "Click a ship to find it on the map", bind: onClick(".along-card") },
        empty: "No ships are tied up right now." },
      { target: "#depthPanel", before: () => { const d = $("#depthPanel"); if (d) d.open = true; },
        title: "Water depth",
        body: `Upload a new depth survey here when one comes in. Before a booking is confirmed, the console checks the
               ship's <b>draft</b> against the latest depths to make sure it won't touch bottom.` },
    ],
  },
  {
    id: "intake", title: "Taking a berth request", blurb: "Entering a request from a phone call or email",
    steps: [
      { target: '#sidebarTabs .tab[data-tab="requests"]', before: ensureSidebar, title: "Berth requests",
        body: `Every request for a berth ends up here, whether it came in by phone, email, or the online form.`,
        try: { hint: "Click the Berth requests tab", bind: onClick('.tab[data-tab="requests"]') } },
      { target: "#intakePanel", before: () => { showTab("requests"); $("#intakePanel").open = true; },
        title: "New request",
        body: `Use this form when someone calls or emails. <b>Nothing is saved during the tour</b>, so go ahead and try the
               boxes.` },
      { target: () => $('#berthRequestForm [name="imo"]')?.closest("fieldset"), title: "The ship",
        body: `Start with the ship's <b>IMO number</b>, or type part of its <b>name</b> and pick it from the list. If we've
               seen the ship before, its name and size fill in for you.`,
        try: { hint: "Type a few letters of a ship's name", bind: onEvent("input", '#berthRequestForm [name="vessel"]') } },
      { target: () => $('#berthRequestForm [name="etb"]')?.closest("fieldset"), title: "When",
        body: `Enter the <b>arrival time</b> (required) and the departure time if you know it. Use local time.` },
      { target: () => $("#berthRequests")?.closest("details"), title: "After you save",
        body: `The request shows up in this list. It doesn't have a spot on the wharf yet — that happens when you
               place it on the Reservations tab. If the same ship is requested twice for the same dates, both cards
               are marked <b>possible duplicate</b> so you can decide which one to keep.` },
    ],
  },
  {
    id: "place", title: "Giving a ship a spot", blurb: "Picking a berth and confirming the booking",
    steps: [
      { target: '#sidebarTabs .tab[data-tab="reservations"]', before: ensureSidebar, title: "Reservations",
        body: `All bookings, from first request to finished visit.`,
        try: { hint: "Click the Reservations tab", bind: onClick('.tab[data-tab="reservations"]') } },
      { target: "#resFilter", before: () => showTab("reservations"), title: "Show only some",
        body: `Choose which bookings to see: <b>requested</b> (no spot yet), <b>confirmed</b>, <b>completed</b> or
               <b>cancelled</b>.`,
        try: { hint: "Pick a different option", bind: onEvent("change", "#resFilter") } },
      { target: () => $('#requests [data-act="find-berth"]')?.closest(".card"), title: "Find a spot",
        body: `Click <b>Find berth</b> and the console shows the open spots that fit this ship for its dates, with room
               to spare and enough water. Pick the one you want. To set the spot yourself, use <b>⋯ → Edit placement</b>
               and type the <b>dock number at the bow</b> and which way the ship faces.`,
        empty: "There are no bookings to place right now. Set the filter to Requested to see the Find berth button." },
      { target: "#requests", title: "Confirming",
        body: `When you mark a booking <b>Confirmed</b>, the console double-checks two things first:
               <br>• the ship isn't too deep for the water there, and
               <br>• it doesn't overlap another confirmed ship (ships need at least <b>75 ft</b> between them).
               <br>If either check fails, you'll get a message and nothing is changed.` },
    ],
  },
  {
    id: "ships", title: "Ship details", blurb: "Looking up ships and fixing wrong sizes",
    steps: [
      { target: "#shipSearch", before: () => { ensureSidebar(); showTab("ships"); }, title: "Saved ships",
        body: `Every ship the console knows about.`,
        try: { hint: "Search for a ship by name or IMO", bind: onEvent("input", "#shipSearch") } },
      { target: "#vessels", title: "When a ship's size is wrong",
        body: `Ship sizes normally come straight from the ship's own tracking signal. If that's wrong, edit the length,
               beam or draft and say yes when asked to <b>override</b>. The ship then shows an <b>Edited</b> tag and keeps
               your number. <b>Revert to AIS</b> switches back to the ship's own numbers.` },
    ],
  },
  {
    id: "timeline", title: "The schedule", blurb: "Who's at which berth, and when",
    steps: [
      { target: "#timelineDrawer", before: () => ensureTimeline(true), title: "Berth schedule",
        body: `One row per berth, one bar per booking, coloured by status. Drag the top edge to make it bigger; the arrow
               on the left hides it.` },
      { target: () => $("#timelineDrawer .tl-seg"), title: "Which days",
        body: `Choose how many days to show, or use the arrows to go back and forward. <b>Today</b> jumps back to now.`,
        try: { hint: "Pick one of the day buttons", bind: onClick("#timelineDrawer [data-preset], #timelineDrawer [data-pan]") } },
      { target: "#tlSvg", title: "Now, and a time you pick",
        body: `The <b style="color:var(--red)">red NOW</b> line is the current time. Click anywhere on the schedule to pick
               a different time — with the map on <b>Planned</b>, it shows the wharf as booked at that moment.`,
        try: { hint: "Click somewhere on the schedule", bind: onEvent("mousedown", "#tlSvg") } },
      { target: "#tlPlay", title: "Play",
        body: `Moves through time on its own. Set the map to <b>Planned</b> and watch ships come and go.`,
        try: { hint: "Press Play (press again to stop)", bind: onClick("#tlPlay") } },
    ],
  },
  {
    id: "history", title: "Looking back", blurb: "Past visits and ship details",
    steps: [
      { target: '#sidebarTabs .tab[data-tab="history"]', before: () => { ensureSidebar(); closeHistFilters(); },
        title: "History", body: `Every past visit and booking, newest first.`,
        try: { hint: "Click the History tab", bind: onClick('.tab[data-tab="history"]') } },
      { target: "#histFilterBtn", before: () => { showTab("history"); closeHistFilters(); }, title: "Search",
        body: `Look up visits by ship name, IMO, status or dates.`,
        try: { hint: "Click Filters", bind: onClick("#histFilterBtn") } },
      { target: "#histFilterModal .sheet",
        before: () => { if (!$("#histFilterModal").classList.contains("open")) $("#histFilterBtn")?.click(); },
        title: "Narrow it down", body: `Fill in what you know and click <b>Search</b>. <b>Clear</b> starts over.` },
      { target: "#historyList", before: closeHistFilters, title: "Ship details",
        body: `Point at any row to see that ship's details, its past visits, and where it was last seen.`,
        try: { hint: "Point at a row", bind: onEvent("mouseover", "#historyList .hist-card") } },
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
        <button type="button" class="tour-x" data-tour="close" title="Close the tour (Esc)">×</button>
      </div>
      <div class="tour-prog"><i style="width:${pct}%"></i></div>
      <h4>${s.title}</h4>
      <div class="tour-text">${s.body}</div>
      <div class="tour-empty" hidden>${s.empty ? esc(s.empty) : "There's nothing here right now — this fills in as ships come and go."}</div>
      ${s.try ? `<div class="tour-await"><span class="tour-pulse"></span><span>Your turn: ${esc(s.try.hint)}</span></div>` : ""}
      <div class="tour-live"></div>
      <div class="tour-foot">
        <button type="button" class="btn-sm" data-tour="back" ${i === 0 ? "disabled" : ""}>‹ Back</button>
        <span class="tour-keys">or use ← →</span>
        ${s.try ? `<button type="button" class="tour-skip" data-tour="skip">skip</button>` : ""}
        <button type="button" class="btn-sm primary" data-tour="next" ${s.try ? "disabled" : ""}>${last ? "Finish ✓" : "Next ›"}</button>
      </div>`;
    if (s.try) {
      cleanup = s.try.bind(() => {
        const a = card.querySelector(".tour-await");
        if (a && !a.classList.contains("done")) {
          a.classList.add("done");
          a.querySelector("span:last-child").textContent = "✓ That's it.";
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
    { name: "Ship", status: "confirmed" },
    { name: "Dredging", status: "confirmed" },
  ];
  const STATUS_TXT = { observed: "seen at the wharf", requested: "requested", confirmed: "confirmed" };
  const setPreset = (k) => PRESETS[k].forEach((p, j) => Object.assign(boxes[j], p));
  setPreset("tooClose");

  root.innerHTML = `
    <div class="demo-bar">
      <span class="demo-lbl">Try</span>
      <button type="button" class="btn-sm" data-preset="overlap">Same spot, same time</button>
      <button type="button" class="btn-sm" data-preset="space">Same time, different spots</button>
      <button type="button" class="btn-sm" data-preset="time">Same spot, one after the other</button>
      <button type="button" class="btn-sm" data-preset="tooClose">Too close together</button>
    </div>
    <svg class="demo-svg" viewBox="0 0 ${VW} ${VH}" role="img" aria-label="Practice area: wharf spot by time"></svg>
    <div class="demo-bar">
      ${boxes.map((b, j) => `<label class="demo-lbl">${b.name}
        <select data-box="${j}">${Object.entries(STATUS_TXT).map(([v, t]) =>
          `<option value="${v}" ${v === b.status ? "selected" : ""}>${t}</option>`).join("")}</select></label>`).join("")}
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
    if (!tOv) msg = "<b>No clash.</b> They're never at the wharf at the same time.";
    else if (!sGap) msg = `<b>No clash.</b> Same time, but ${clear} ft apart — more than the 75 ft needed.`;
    else if (bothObs) { cls = "dim"; msg = "<b>No clash shown.</b> Both are ships actually sitting at the wharf, so there's nothing to plan."; }
    else if (bothConf) { cls = "bad"; msg = sRaw
        ? "<b>Not allowed.</b> Two confirmed bookings can't use the same spot at the same time — the console won't save the second one."
        : `<b>Not allowed.</b> Only ${clear} ft apart — confirmed ships need at least 75 ft between them.`; }
    else if (sRaw) { cls = "warn"; msg = "<b>Shows as a clash.</b> It appears on the clash list for you to sort out. Only two <i>confirmed</i> bookings are stopped outright."; }
    else { cls = "warn"; msg = `<b>OK for now.</b> But ${clear} ft apart is less than 75 ft, so you couldn't confirm both.`; }
    verdict.className = "demo-verdict " + cls;
    verdict.innerHTML = `<div>${chip(tOv, "same time")}${chip(sRaw, "same spot")}${
      tOv && !sRaw ? chip(sGap, `closer than 75 ft (${clear} ft)`) : ""}</div><div>${msg}</div>`;
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
    svg.append(el("text", { x: 10, y: TOP + ph / 2, class: "demo-ax", transform: `rotate(-90 10 ${TOP + ph / 2})`, "text-anchor": "middle" }, "along the wharf"));
    boxes.forEach((b, j) => {
      const g = el("g", { "data-j": j, class: "demo-box" });
      g.append(el("rect", { x: X(b.t0), y: Y(b.s0 - GAP / 2), width: X(b.t1) - X(b.t0),
        height: Y(b.s1 + GAP / 2) - Y(b.s0 - GAP / 2), class: "demo-halo" }));
      g.append(el("rect", { x: X(b.t0), y: Y(b.s0), width: X(b.t1) - X(b.t0), height: Y(b.s1) - Y(b.s0),
        class: "demo-rect", style: `fill:var(--st-${b.status});stroke:var(--st-${b.status})`, "data-act": "move" }));
      g.append(el("text", { x: X(b.t0) + 6, y: Y(b.s0) + 14, class: "demo-name" }, `${b.name} · ${STATUS_TXT[b.status]}`));
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
