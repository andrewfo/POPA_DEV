// Depth surveys: upload a hydrographic .XYZ + list the surveys that back the
// draft-vs-controlling-depth confirm gate. The latest ACTIVE survey is what the
// gate reads; uploading a new one supersedes it (depths change constantly).
import { api, apiWrite, esc, fmtSta } from "./api.js";
import { depthLayer, loadDepthOverlay, map } from "./map.js";

// Refresh the map's controlling-depth overlay if it's currently shown (an
// upload/delete changes the active profile under it).
function refreshDepthOverlay() {
  if (map.hasLayer(depthLayer)) loadDepthOverlay();
}

const fmtDepth = (d) => (d == null ? "—" : `${Number(d).toFixed(1)} ft`);
const fmtDate = (s) => (s ? s : "—");

export async function loadDepthSurveys() {
  const status = document.getElementById("depthStatus");
  const list = document.getElementById("depthList");
  if (!status || !list) return;
  try {
    const rows = await api("/depth/surveys");
    const active = rows.find((r) => r.active);
    status.innerHTML = active
      ? `Active survey <b>${esc(fmtDate(active.surveyed_at))}</b> · controlling min `
        + `<b>${fmtDepth(active.min_depth_ft)}</b> · POPA `
        + `${fmtSta(active.station_min)}–${fmtSta(active.station_max)}`
      : "No depth survey loaded — the draft gate warns rather than blocks.";
    list.innerHTML = rows.length
      ? rows.map(surveyRow).join("")
      : '<div class="empty">none</div>';
    wireDepthRows(list);
  } catch (e) {
    status.textContent = "unavailable (DB offline)";
    list.innerHTML = '<div class="empty">unavailable</div>';
  }
}

function surveyRow(r) {
  const badge = r.active
    ? '<span class="status-badge" style="color:var(--st-confirmed)">active</span>'
    : '<span class="status-badge" style="color:var(--muted)">inactive</span>';
  return `
    <div class="card" data-survey="${r.id}">
      <div class="name">${esc(fmtDate(r.surveyed_at))} ${badge}</div>
      <div class="meta">${esc(r.source_file || "—")}${r.datum ? " · " + esc(r.datum) : ""}
        · ${r.point_count ?? "—"} pts · controlling min <b>${fmtDepth(r.min_depth_ft)}</b></div>
      <div class="meta">POPA ${fmtSta(r.station_min)}–${fmtSta(r.station_max)} · ${Number(r.bin_ft || 0)} ft bins</div>
      <div class="row-actions">
        <button class="btn-sm" data-act="del-survey">Delete</button>
      </div>
    </div>`;
}

function wireDepthRows(container) {
  container.querySelectorAll("[data-survey]").forEach((card) => {
    const id = Number(card.dataset.survey);
    const del = card.querySelector('[data-act="del-survey"]');
    if (del) del.addEventListener("click", async () => {
      if (!confirm("Delete this depth survey? The next-latest active survey (if any) takes over the draft gate.")) return;
      const w = await apiWrite("DELETE", `/depth/surveys/${id}`);
      if (w.ok) { loadDepthSurveys(); refreshDepthOverlay(); }
      else alert("Delete failed: HTTP " + w.status);
    });
  });
}

// Upload: the .XYZ goes as the raw request body; metadata rides in the query
// string (so no multipart dependency). The server parses + reduces in PostGIS.
(function () {
  const form = document.getElementById("depthUploadForm");
  if (!form) return;
  const fileEl = document.getElementById("depthFile");
  const dateEl = document.getElementById("depthDate");
  const datumEl = document.getElementById("depthDatum");
  const sridEl = document.getElementById("depthSrid");
  const btn = document.getElementById("depthUploadBtn");
  const result = document.getElementById("depthResult");

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const file = fileEl.files[0];
    if (!file) return;
    result.className = "result";
    result.textContent = "Uploading & reducing… (large surveys take a few seconds)";
    btn.disabled = true; btn.textContent = "Working…";

    const params = new URLSearchParams({ source_file: file.name });
    if (dateEl.value) params.set("surveyed_at", dateEl.value);
    if (datumEl.value) params.set("datum", datumEl.value);
    if (sridEl.value) params.set("srid", sridEl.value);

    try {
      const r = await fetch(`/depth/surveys?${params}`, { method: "POST", body: file });
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail ? JSON.stringify(data.detail) : "HTTP " + r.status);
      result.className = "result ok";
      result.innerHTML = `Imported survey #${data.id} (${esc(data.surveyed_at)}): `
        + `${data.parsed_points} soundings → ${data.segment_count} station bins, `
        + `shallowest ${fmtDepth(data.min_depth_ft)}.`;
      form.reset();
      sridEl.value = 2278;
      loadDepthSurveys();
      refreshDepthOverlay();
    } catch (err) {
      result.className = "result err";
      result.textContent = "Failed: " + err.message;
    } finally {
      btn.disabled = false; btn.textContent = "Upload survey";
    }
  });
})();
