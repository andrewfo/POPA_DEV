// Entry point — the only <script type="module"> index.html loads. Wires the page
// chrome (sidebar resize/collapse, header clock) and runs the boot sequence +
// 15s poll. The classic Leaflet CDN <script> tags run before this deferred
// module, so the `L` global is ready by the time any module evaluates.
import { CENTRAL_TZ } from "./api.js";
import { map, loadBbox, loadReferenceGeo, loadWharfGeometry, loadPositions } from "./map.js";
import { loadTimeline } from "./timeline.js";
import { loadStats, loadConflicts, loadVerification, loadAlongside, loadWorkers } from "./panels.js";
import { loadVessels, loadRequests, loadBerthRequests } from "./forms.js";

// --- Resizable left sidebar ------------------------------------------------
// Drag the divider between the sidebar and the map. Width (--sidebarW on <main>)
// persists in localStorage; the map is invalidated on drop so Leaflet re-fits.
(function () {
  const handle = document.getElementById("sidebarResize");
  const mainEl = document.querySelector("main");
  if (!handle || !mainEl) return;
  const MINW = 240;
  const apply = (w) => mainEl.style.setProperty("--sidebarW", w + "px");
  const saved = Number(localStorage.getItem("sidebarW"));
  if (saved && saved >= MINW) apply(saved);
  let dragging = false;
  handle.addEventListener("mousedown", (e) => { dragging = true; e.preventDefault(); document.body.style.cursor = "col-resize"; });
  addEventListener("mousemove", (e) => {
    // aside starts at viewport x=0, so clientX is the desired sidebar width.
    if (dragging) apply(Math.max(MINW, Math.min(innerWidth - 360, e.clientX)));
  });
  addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false; document.body.style.cursor = "";
    localStorage.setItem("sidebarW", parseInt(mainEl.style.getPropertyValue("--sidebarW")) || 340);
    if (map.invalidateSize) setTimeout(() => map.invalidateSize(), 0);
  });
})();

// --- Header clock (Central) + sidebar collapse -----------------------------
// Session boot, for the status-indicator uptime readout.
const BOOT_MS = Date.now();
(function () {
  const c = document.getElementById("clock");
  if (!c) return;
  const up = document.getElementById("statusUptime");
  const hms = (tz) => new Date().toLocaleTimeString("en-GB", { timeZone: tz, hour12: false });
  const dCT = () => new Date().toLocaleDateString("en-US", {
    timeZone: CENTRAL_TZ, weekday: "short", day: "2-digit", month: "short" }).toUpperCase();
  const tick = () => {
    // Dual readout: Central wall-clock (canonical) over UTC. Mono, tabular.
    c.innerHTML = `<span class="z">CT </span>${dCT()} ${hms(CENTRAL_TZ)}\n` +
                  `<span class="z">UTC</span> ${hms("UTC")}Z`;
    if (up) {
      const s = Math.floor((Date.now() - BOOT_MS) / 1000);
      const p = (n) => String(n).padStart(2, "0");
      up.textContent = `UP ${p(Math.floor(s/3600))}:${p(Math.floor((s%3600)/60))}:${p(s%60)}`;
    }
  };
  tick(); setInterval(tick, 1000);
})();
// Section-header info glyphs carry their explainer in the title tooltip;
// clicking one must not toggle the <details> it sits in.
document.querySelectorAll("summary .info").forEach((i) =>
  i.addEventListener("click", (e) => e.preventDefault()));
(function () {
  const btn = document.getElementById("sidebarToggle");
  if (!btn) return;
  btn.addEventListener("click", () => {
    const collapsed = document.body.classList.toggle("sidebar-collapsed");
    btn.textContent = collapsed ? "»" : "«";
    if (map.invalidateSize) setTimeout(() => map.invalidateSize(), 0);
  });
})();

// --- Boot ------------------------------------------------------------------
loadBbox();
loadReferenceGeo().then(loadTimeline);   // lanes need BERTH_STA loaded first
loadStats();
loadWharfGeometry();
loadVessels();
loadRequests();
loadBerthRequests();
loadPositions();
loadConflicts();
loadVerification();
loadAlongside();
loadWorkers();
setInterval(() => { loadStats(); loadPositions(); loadTimeline(); loadConflicts(); loadVerification(); loadAlongside(); loadWorkers(); }, 15000);
