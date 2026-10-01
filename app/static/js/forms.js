// Write surfaces: vessel list + inline editor, reservations (edit / confirm /
// cancel), the berth-request form (create + in-place edit), the raw berth-request
// audit list, and the sidebar tab switcher.
import {
  api, apiWrite, errorDetail, esc, fmtCentral, formPayload, isoToLocalInput, localInputToIso,
  centralParts, CENTRAL_TZ, NAV_STATUS, FT_PER_M, BADGE_COLORS, sourceLabel,
} from "./api.js";
import { state } from "./state.js";
import {
  locateVesselOnMap, loadPositions,
  renderFeasibility, clearFeasibility, highlightFeasSlot, clearFeasHighlight,
} from "./map.js";
import { loadTimeline } from "./timeline.js";
import { loadStats, loadConflicts, loadAlongside } from "./panels.js";
import { loadHistory } from "./history.js";

// Assigned by the berth-request form IIFE below (it closes over the form's local
// state). Exported as a live binding so reqCard's Edit button — and any other
// caller — can flip the form into edit mode. Replaces the old window.* bridge.
export let editBerthRequest;

// Current saved-ships IMO search term (substring match), set by the search bar.
let vesselImoFilter = "";

export async function loadVessels() {
  const el = document.getElementById("vessels");
  try {
    // Fetch a wide slice so the IMO search reaches beyond the most-recent few;
    // the render step trims to the current filter client-side.
    const vs = await api("/vessels?limit=500");
    state.vessels = vs;
    renderVessels();
  } catch (e) {
    el.innerHTML = '<div class="empty">unavailable (DB offline)</div>';
  }
}

// Render state.vessels into the table, applying the IMO search filter. Split from
// the fetch so the search bar can re-render without re-hitting the API.
function renderVessels() {
  const el = document.getElementById("vessels");
  if (!el) return;
  const all = state.vessels || [];
  if (!all.length) { el.innerHTML = '<div class="empty">no vessels yet, run AIS ingestion</div>'; return; }
  const q = vesselImoFilter.trim();
  const ql = q.toLowerCase();
  const vs = q ? all.filter((v) =>
    (v.imo != null && String(v.imo).includes(q)) ||
    (v.name && v.name.toLowerCase().includes(ql))) : all;
  if (!vs.length) { el.innerHTML = `<div class="empty">no saved ship matching “${esc(q)}”</div>`; return; }
  {
    el.innerHTML = `<table id="vesselsTbl"><thead><tr>` +
      `<th>Name</th><th>MMSI</th><th>IMO</th><th>Call</th>` +
      `<th title="Draught (ft)">Drft</th>` +
      `<th title="Length overall (ft)">LOA</th><th></th>` +
      `</tr></thead><tbody>${
      vs.map((v) => {
        const navLabel = v.nav_status != null ? (NAV_STATUS[v.nav_status] || ("code " + v.nav_status)) : null;
        // Row tooltip carries the fuller fix the dense columns can't: nav label,
        // destination, and when the fix landed.
        const tip = [];
        if (navLabel) tip.push("Nav: " + navLabel);
        if (v.destination) tip.push("Dest: " + v.destination);
        if (v.position_ts) tip.push("Fix: " + fmtCentral(v.position_ts) + " CT");
        const locatable = v.mmsi != null;
        if (locatable) tip.unshift("Click to locate on the chart");
        const loc = locatable ? ` class="locatable" data-locate-mmsi="${v.mmsi}"` : "";
        if (v.dims_locked) tip.push("AIS dimensions overridden (pinned)");
        const title = tip.length ? ` title="${esc(tip.join(" · "))}"` : "";
        const drft = v.draft != null ? Math.round(Number(v.draft) * FT_PER_M) : "—";
        // Flag a pinned (overridden) ship right in the list so the operator sees it
        // without opening the editor. Same amber "Edited" badge as the edit form.
        const edited = v.dims_locked
          ? ` <span class="status-badge" style="color:var(--amber)" title="AIS dimensions overridden — pinned">Edited</span>` : "";
        return `<tr${loc}${title}>` +
          `<td>${v.name ? esc(v.name) : "<i>unknown</i>"}${edited}</td>` +
          `<td class="num">${v.mmsi ?? "—"}</td>` +
          `<td class="num">${v.imo ?? "—"}</td>` +
          `<td>${v.callsign ? esc(v.callsign) : "—"}</td>` +
          `<td class="num">${drft}</td>` +
          `<td class="num">${v.loa != null ? Math.round(Number(v.loa) * FT_PER_M) : "—"}</td>` +
          `<td class="act"><button class="btn-sm" data-edit-vessel="${v.id}">Edit</button></td>` +
          `</tr>`;
      }).join("")
    }</tbody></table>`;
    el.querySelectorAll("[data-edit-vessel]").forEach((b) =>
      b.addEventListener("click", (e) => { e.stopPropagation(); openVesselEditor(Number(b.dataset.editVessel)); }));
    // Click a row (anywhere but the Edit button) to centre the chart on that
    // vessel's live AIS contact and flash a locate halo over it.
    el.querySelectorAll("tr.locatable").forEach((tr) =>
      tr.addEventListener("click", () => {
        if (!locateVesselOnMap(Number(tr.dataset.locateMmsi))) {
          tr.classList.remove("locate-miss"); void tr.offsetWidth; // restart anim
          tr.classList.add("locate-miss");
        }
      }));
  }
}

// Wire the saved-ships IMO search box: filter the loaded list on each keystroke.
(function () {
  const box = document.getElementById("shipSearch");
  if (!box) return;
  box.addEventListener("input", () => { vesselImoFilter = box.value; renderVessels(); });
})();

// Inline editor for one vessel (dimensions in metres — the canonical store).
function openVesselEditor(id) {
  const v = state.vessels.find((x) => x.id === id);
  const box = document.getElementById("vesselEditor");
  if (!v) { box.innerHTML = ""; return; }
  const val = (x) => (x == null ? "" : x);
  // Dimensions are stored in metres but the Saved-ships surface shows feet (what
  // an operator reads off the quay). Display rounded feet; convert back to metres
  // on save. `data-init-ft` stashes the shown value so an untouched field isn't
  // round-tripped (which would drift the precise stored metres).
  const ft = (m) => (m == null ? "" : Math.round(Number(m) * FT_PER_M));
  box.innerHTML = `
    <div class="card">
      <div class="name">Edit vessel #${v.id}</div>
      <form class="edit-form open" id="vesselEditForm">
        <label>Name</label><input name="name" value="${esc(val(v.name))}" />
        <div class="grid2">
          <div><label>IMO</label><input type="number" name="imo" value="${val(v.imo)}" /></div>
          <div><label>MMSI</label><input type="number" name="mmsi" value="${val(v.mmsi)}" /></div>
        </div>
        <div class="grid2">
          <div><label>LOA (ft)</label><input type="number" step="any" name="loa" value="${ft(v.loa)}" data-init-ft="${ft(v.loa)}" /></div>
          <div><label>Beam (ft)</label><input type="number" step="any" name="beam" value="${ft(v.beam)}" data-init-ft="${ft(v.beam)}" /></div>
        </div>
        <div class="grid2">
          <div><label>Draft (ft)</label><input type="number" step="any" name="draft" value="${ft(v.draft)}" data-init-ft="${ft(v.draft)}" /></div>
          <div><label>Callsign</label><input name="callsign" value="${esc(val(v.callsign))}" /></div>
        </div>
        ${v.mmsi != null ? (v.dims_locked ? `
        <div class="edited-note"><span class="status-badge" style="color:var(--amber)">Edited</span> AIS dimensions have been overridden — pinned to the values above; the live feed won't revert them.</div>
        <div class="hint" style="margin:-2px 0 6px">Use “Revert to AIS” to unpin and hand the dimensions back to the feed.</div>
        <div class="row-actions"><button type="button" class="btn-sm" id="revertAis">Revert to AIS</button></div>
        ` : `
        <div class="hint" style="margin:-2px 0 6px">This vessel is AIS-tracked, so LOA/beam/draft come from the live feed. Just edit a value and save — you'll be asked to confirm before it's pinned.</div>
        `) : ""}
        <label>Destination</label><input name="destination" value="${esc(val(v.destination))}" />
        <div class="row-actions">
          <button type="submit" class="btn-sm primary">Save</button>
          <button type="button" class="btn-sm" id="vesselEditCancel">Cancel</button>
        </div>
        <div class="mini-result" id="vesselEditResult"></div>
      </form>
    </div>`;
  const form = document.getElementById("vesselEditForm");
  const res = document.getElementById("vesselEditResult");
  document.getElementById("vesselEditCancel").addEventListener("click", () => { box.innerHTML = ""; });
  // Revert to AIS: undo a manual dimension override. Unlocks (dims_locked →
  // false) and the server restores LOA/beam/draft from the AIS shadow it kept
  // current while the row was pinned (migration 0016) — so the real AIS value is
  // back at once, not on the next broadcast. (If the shadow is unknown — a lock
  // predating the feature — the feed refills on the next ShipStaticData instead;
  // the returned warning says which happened.) Shown only when currently pinned.
  const revertBtn = document.getElementById("revertAis");
  if (revertBtn) revertBtn.addEventListener("click", async () => {
    if (!confirm(
      `Revert ${v.name || "this vessel"} to AIS dimensions?\n\n` +
      `This unpins the manual override and restores the last dimensions AIS ` +
      `reported; the live feed then keeps them updated.`
    )) return;
    const w = await apiWrite("PATCH", `/vessels/${id}`, { dims_locked: false });
    if (w.ok) {
      const warns = (w.data && w.data.warnings) || [];
      res.className = "mini-result ok";
      res.textContent = "Reverted to AIS. " + warns.join(" ");
      loadVessels(); loadPositions();
      setTimeout(() => { box.innerHTML = ""; }, 2200);
    } else {
      res.className = "mini-result err";
      res.textContent = "Failed: " + writeError(w);
    }
  });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    res.className = "mini-result";
    // Blank fields are skipped (not cleared) — a manual edit overwrites only what
    // it sets, mirroring the server's exclude_unset partial update.
    const payload = formPayload(form, ["imo", "mmsi", "loa", "beam", "draft"]);
    // loa/beam/draft are ENTERED in feet on this surface but stored in metres.
    // Convert only fields the operator actually changed from the shown value —
    // an untouched (rounded) field is dropped so it can't overwrite the precise
    // stored metres with a round-tripped one.
    for (const k of ["loa", "beam", "draft"]) {
      const el = form.elements[k];
      const raw = String(el.value || "").trim();
      if (raw === "" || raw === el.dataset.initFt) { delete payload[k]; continue; }
      payload[k] = Number(raw) / FT_PER_M;   // feet -> metres (canonical store)
    }
    // Easy-override: on an AIS-tracked vessel that isn't already pinned, a changed
    // dimension would be dropped server-side (AIS stays authoritative). Offer one
    // confirm to pin it — the sole way to override now that the checkbox is gone.
    // Declining leaves the server's drop-and-warn behavior intact. An already-
    // pinned vessel (dims_locked) just applies the edit, so no prompt is needed.
    if (v.mmsi != null && !v.dims_locked) {
      const dimChanged = ["loa", "beam", "draft"].some(
        (k) => k in payload && Number(payload[k]) !== Number(v[k])
      );
      if (dimChanged && confirm(
        `You changed AIS-reported dimensions on ${v.name || "this vessel"}.\n\n` +
        `Pin your values and stop the AIS feed from reverting them? ` +
        `Only do this if AIS itself is wrong.`
      )) {
        payload.dims_locked = true;
      }
    }
    const w = await apiWrite("PATCH", `/vessels/${id}`, payload);
    if (w.ok) {
      const warns = w.data && w.data.warnings;
      res.className = "mini-result ok";
      res.innerHTML = "Saved." + warnHtml(warns);
      loadVessels(); loadPositions();
      // Keep the editor open when there's a warning (e.g. an AIS-tracked vessel's
      // dimensions won't stick) so the operator actually reads it.
      if (!(warns && warns.length)) setTimeout(() => { box.innerHTML = ""; }, 1200);
    } else {
      res.className = "mini-result err";
      res.textContent = "Failed: " + writeError(w);
    }
  });
  box.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

// Human-readable error from an apiWrite() failure (detail may be a string or a
// FastAPI validation-error array).
function writeError(w) {
  return errorDetail(w.data) || ("HTTP " + w.status);
}
function warnHtml(warnings) {
  return (warnings && warnings.length) ? `<span class="warn">▲ ${esc(warnings.join("; "))}</span>` : "";
}

// --- AIS-authoritative dimension override (the easy manual-correction path) ---
// AIS is authoritative for an AIS-tracked vessel's LOA/beam/draft: a typed value
// is dropped server-side unless the operator pins it with `dims_locked` (migration
// 0015). To make correcting a wrong autofill easy, the intake + vessel forms show
// AIS dims as EDITABLE (tinted, not read-only); overtyping one arms a single
// confirm on save that pins the entered value via these helpers — instead of the
// value being silently dropped and the operator hunting for a button.

// Flag a dim input as carrying an AIS-authoritative value: tint it (`.ais-dim`)
// and stash the shown value (feet) on the element so an edit away from it is
// detectable. Left editable on purpose — the confirm-to-pin flow handles intent.
function markAisDim(el, ft) {
  if (!el) return;
  el.readOnly = false;
  el.dataset.aisFt = String(ft);
  el.classList.add("ais-dim");
  el.classList.remove("ais-dim-edited");
}
// Clear the AIS flag from a dim input (a fresh lookup, a miss, or a form reset).
function unmarkAisDim(el) {
  if (!el) return;
  el.readOnly = false;
  delete el.dataset.aisFt;
  el.classList.remove("ais-dim", "ais-dim-edited");
}
// Wire live tinting: an AIS dim flips amber -> green (`.ais-dim-edited`) the
// moment its value differs from the stashed AIS value, so a pending override is
// visible before save. No-op for a field that carries no AIS value.
function wireAisDimEdit(el) {
  if (!el) return;
  el.addEventListener("input", () => {
    if (!("aisFt" in el.dataset)) return;
    const changed = String(el.value || "").trim() !== String(el.dataset.aisFt);
    el.classList.toggle("ais-dim-edited", changed);
    el.classList.toggle("ais-dim", !changed);
  });
}

// Pin the operator's ENTERED dimensions onto an AIS-tracked vessel: PATCH it with
// `dims_locked` + the entered metres values (the migration-0015 escape hatch).
// Shared by the confirm-on-save flow and the fallback "Manual override" button.
async function applyManualOverride(vesselId, overrides) {
  const patch = { dims_locked: true };
  for (const o of overrides) patch[o.field] = o.entered_m;
  return apiWrite("PATCH", `/vessels/${vesselId}`, patch);
}
// Human summary of an ais_overrides list: "draft 20 ft (AIS 30 ft), ...".
function overrideSummary(overrides) {
  return overrides.map((o) => `${o.label.toLowerCase()} ${o.entered_ft} ft (AIS ${o.ais_ft} ft)`).join(", ");
}

// After an intake save DROPPED the operator's entered AIS-tracked dims, offer to
// pin them with a single confirm ("confirm after" — the easy override). On accept,
// PATCH the vessel (dims_locked + entered values) and report it in `container`. On
// decline, keep AIS authoritative but leave the red "Manual override" button so it
// can still be pinned later. `data` is the intake response (needs `vessel_id` +
// non-empty `ais_overrides`). Returns true iff the override was applied.
async function offerOverrideConfirm(container, data) {
  const ov = data && data.ais_overrides;
  if (!data || !data.vessel_id || !ov || !ov.length) return false;
  const entered = overrideSummary(ov);
  if (!confirm(
    `You changed AIS-reported dimensions: ${entered}.\n\n` +
    `Pin your entered value onto vessel #${data.vessel_id} and stop the AIS feed ` +
    `from reverting it? Only do this if AIS itself is wrong.`
  )) {
    renderManualOverride(container, data);   // declined — leave the escape button
    return false;
  }
  const w = await applyManualOverride(data.vessel_id, ov);
  if (w.ok) {
    const flipped = w.data && w.data.override_cancelled;
    container.className = "result ok";
    container.innerHTML =
      `Manual override applied — entered dimensions (${esc(entered)}) pinned on ` +
      `vessel #${data.vessel_id}; the AIS feed will no longer revert them ` +
      `(clear the lock on the vessel to hand them back to AIS).` +
      (flipped ? ` AIS override → cancelled on ${flipped} reservation${flipped === 1 ? "" : "s"}.` : "");
    loadVessels(); loadRequests(); loadBerthRequests(); loadPositions();
    return true;
  }
  container.className = "result err";
  container.innerHTML = `Override failed: ${esc(writeError(w))}`;
  renderManualOverride(container, data);   // let them retry via the button
  return false;
}

// Fallback red "Manual override" button in the result area — the decline path of
// offerOverrideConfirm (and any later correction). Clicking it pins the ENTERED
// value onto the vessel (`dims_locked`) via the same shared PATCH. `data` is the
// intake response; it must carry `vessel_id` and a non-empty `ais_overrides`.
function renderManualOverride(container, data) {
  const ov = data && data.ais_overrides;
  if (!data || !data.vessel_id || !ov || !ov.length) return;
  const entered = overrideSummary(ov);
  const wrap = document.createElement("div");
  wrap.style.marginTop = "6px";
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "btn-sm override";
  btn.textContent = "Manual override";
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    const w = await applyManualOverride(data.vessel_id, ov);
    if (w.ok) {
      const flipped = w.data && w.data.override_cancelled;
      container.className = "result ok";
      container.innerHTML =
        `Manual override applied — entered dimensions (${esc(entered)}) pinned on ` +
        `vessel #${data.vessel_id}; the AIS feed will no longer revert them ` +
        `(clear the lock on the vessel to hand them back to AIS).` +
        (flipped ? ` AIS override → cancelled on ${flipped} reservation${flipped === 1 ? "" : "s"}.` : "");
      loadVessels(); loadRequests(); loadBerthRequests(); loadPositions();
    } else {
      btn.disabled = false;
      container.insertAdjacentHTML(
        "beforeend", `<span class="warn">▲ override failed: ${esc(writeError(w))}</span>`
      );
    }
  });
  const hint = document.createElement("div");
  hint.className = "warn";
  hint.style.marginTop = "2px";
  hint.textContent =
    `AIS is authoritative for this vessel. Only override if AIS itself is wrong — ` +
    `this pins ${entered} and stops the feed updating the dimensions.`;
  wrap.appendChild(btn);
  wrap.appendChild(hint);
  container.appendChild(wrap);
}

// --- Reservations (read + inline edit / unconfirm) -------------------------
export async function loadRequests() {
  const el = document.getElementById("requests");
  const sel = document.getElementById("resFilter");
  const filter = sel ? sel.value : "active";
  try {
    // 'active' & 'all' fetch unfiltered then trim client-side; a specific
    // status uses the API's own filter.
    const q = (filter === "active" || filter === "all")
      ? "/reservations?limit=100" : `/reservations?status=${filter}&limit=100`;
    let rs = await api(q);
    // Reservations come from berth requests (and their promotions) only. AIS
    // 'observed' berthings are ground truth for the conflict/verification layers
    // and the History audit log — never the scheduling list here.
    rs = rs.filter((r) => r.status !== "observed");
    if (filter === "active") rs = rs.filter((r) => r.status !== "cancelled");
    if (!rs.length) { el.innerHTML = '<div class="empty">none</div>'; return; }
    el.innerHTML = rs.map(resCard).join("");
    wireResCards(el);
  } catch (e) {
    el.innerHTML = '<div class="empty">unavailable (DB offline)</div>';
  }
}

function resCard(r) {
  // Show the time of day when there is one; a date-only request (midnight
  // Central) reads as just the date. Central throughout — store, edit form, view.
  const fmtDate = (s) => {
    if (!s) return "—";
    const d = new Date(s);
    if (isNaN(d)) return esc(s);
    const p = centralParts(d);
    const opts = { timeZone: CENTRAL_TZ, year: "numeric", month: "short", day: "numeric" };
    if (p.hh !== "00" || p.mm !== "00") { opts.hour = "2-digit"; opts.minute = "2-digit"; opts.hour12 = false; }
    return d.toLocaleString(undefined, opts);
  };
  const color = BADGE_COLORS[r.status] || "#8a99a6";
  // Header: name + IMO subline, with status / channel / relative-age badges —
  // mirrors reqCard so the two tabs scan the same way.
  const idLine = r.vessel_imo ? `<span class="req-id">IMO ${esc(r.vessel_imo)}</span>` : "";
  const rel = relTime(r.created_at);
  const badges = [
    `<span class="status-badge" style="color:${color}">${esc(r.status)}</span>`,
    `<span class="status-badge" style="color:var(--muted)">${esc(channelBadge(r.source))}</span>`,
  ];
  if (rel) badges.push(`<span class="req-time" title="${esc(fmtCentral(r.created_at))} CT">${esc(rel)}</span>`);
  // Schedule strip: ETB -> ETD · duration from the reservation's own (normalized)
  // window, then the placed berth (or an amber "Unassigned" pill) on the next line.
  const schedTimes = (r.t_start || r.t_end)
    ? `<span class="t">${esc(fmtDate(r.t_start))}</span>` +
      (r.t_end ? ` <span class="arw">→</span> <span class="t">${esc(fmtDate(r.t_end))}</span>` : "") +
      (durLabel(r.t_start, r.t_end) ? ` <span class="dur">· ${durLabel(r.t_start, r.t_end)}</span>` : "")
    : `<span class="t" style="color:var(--muted)">Not scheduled</span>`;
  const berthLine = r.station_unassigned
    ? `<span class="status-badge" style="color:var(--amber)">Unassigned</span>`
    : `Dock <span class="t">${Math.round(Math.min(r.station_lo_dock, r.station_hi_dock))}–${Math.round(Math.max(r.station_lo_dock, r.station_hi_dock))}</span>`;
  const opt = (cur, v, lbl) => `<option value="${v}"${(cur || "") === v ? " selected" : ""}>${lbl}</option>`;
  const statusSel = ["requested", "confirmed", "completed", "cancelled"].map((s) => opt(r.status, s, s)).join("");
  const typeSel = ["vessel", "dredge", "layberth"].map((t) => opt(r.type, t, t)).join("");
  const dirSel = ["", "upstream", "downstream"].map((d) => opt(r.direction, d, d || "—")).join("");
  // The bow's Dock No.: upstream -> bow at the high POPA end (the smaller Dock
  // No.); downstream -> bow at the low POPA end (the larger Dock No.). Blank when
  // the berth is unassigned.
  const bowDock = r.station_unassigned ? ""
    : (r.direction === "downstream" ? r.station_lo_dock : r.station_hi_dock);
  // Card face stays minimal: show ONLY an "[AIS override]" note line (a dropped
  // manual dimension the operator must still see), not the rest of the notes.
  const overrideHtml = r.notes
    ? esc(r.notes).split("\n")
        .filter((ln) => ln.includes("[AIS override]"))
        .map((ln) => `<span class="note-ais">▲ ${ln}</span>`)
        .join("<br>")
    : "";
  return `
    <div class="card req-card" data-res="${r.id}" style="border-left-color:${color}">
      <div class="req-head">
        <div class="req-idwrap">
          <div class="req-name">${esc(r.vessel_name || "(unnamed)")}</div>
          ${idLine}
        </div>
        <div class="req-badges">${badges.join("")}</div>
      </div>
      <div class="req-sched">${schedTimes}<span class="ports">${berthLine}</span></div>
      ${dimStrip(r)}
      ${overrideHtml ? `<div class="meta">${overrideHtml}</div>` : ""}
      <div class="req-actions">
        ${r.status === "requested" ? '<button class="btn-sm primary" data-act="confirm">Place + Confirm</button>' : ""}
        ${["requested", "tentative", "confirmed"].includes(r.status) ? '<button class="btn-sm" data-act="find-berth">Find berth</button>' : ""}
        <span class="sp"></span>
        <button class="req-kebab" aria-label="More actions" aria-haspopup="true">⋮</button>
        <div class="req-menu">
          <button class="menu-item" data-act="edit-request">Edit request</button>
          <button class="menu-item" data-act="edit">Edit placement</button>
          ${r.status === "confirmed" ? '<button class="menu-item" data-act="unconfirm">Unconfirm</button>' : ""}
          ${r.status !== "cancelled" ? '<button class="menu-item danger" data-act="cancel">Cancel</button>' : ""}
        </div>
      </div>
      <details class="req-all">
        <summary class="req-more">All info</summary>
        ${resAllInfo(r)}
      </details>
      <form class="edit-form" data-edit-request>
        <fieldset>
          <legend>Berth request</legend>
          <div class="hint" style="margin:0 0 6px">Update the request as received: arrival/departure times and cargo. Placement (bow + heading) stays under “Edit placement”.</div>
          <div class="grid2">
            <div><label>Arrival (ETB)</label><input type="datetime-local" name="etb" value="${isoToLocalInput(r.t_start)}" /></div>
            <div><label>Departure (ETD)</label><input type="datetime-local" name="etd" value="${isoToLocalInput(r.t_end)}" /></div>
          </div>
          <label>Cargo</label><input name="cargo" value="${esc(r.cargo)}" />
          <label>Notes</label><textarea name="notes" rows="4">${esc(r.notes)}</textarea>
        </fieldset>
        <div class="row-actions">
          <button type="submit" class="btn-sm primary">Save request</button>
          <button type="button" class="btn-sm" data-act="close-request">Close</button>
        </div>
        <div class="mini-result"></div>
      </form>
      <form class="edit-form" data-edit>
        <div class="grid2">
          <div><label>Status</label><select name="status">${statusSel}</select></div>
          <div><label>Type</label><select name="type">${typeSel}</select></div>
        </div>
        <fieldset>
          <legend>Place vessel</legend>
          <div class="hint" style="margin:0 0 6px">Schedule comes from the berth request. Enter only the bow position (read off the yellow dock markers) and the heading; the stern follows from the vessel's LOA.</div>
          <div class="grid2">
            <div><label>Bow (Dock No.)</label><input type="number" step="any" name="bow_dock" value="${bowDock}" /></div>
            <div><label>Direction</label><select name="direction">${dirSel}</select></div>
          </div>
        </fieldset>
        <div class="check"><input type="checkbox" name="depth_override" /><label style="margin:0">Override depth check</label></div>
        <div class="hint" style="margin:-2px 0 6px">Confirming validates the vessel's draft against the latest depth survey. Tick to confirm anyway when too deep (logged as a warning).</div>
        <div class="grid2">
          <div><label>Priority</label><input type="number" name="priority" value="${r.priority ?? ""}" /></div>
          <div></div>
        </div>
        <label>Cargo</label><input name="cargo" value="${esc(r.cargo)}" />
        <label>Notes</label><textarea name="notes" rows="4">${esc(r.notes)}</textarea>
        <div class="row-actions">
          <button type="submit" class="btn-sm primary">Save changes</button>
          <button type="button" class="btn-sm" data-act="close">Close</button>
        </div>
        <div class="mini-result"></div>
      </form>
    </div>`;
}

// The Find-berth hover picker: a floating window of the concrete, vessel-sized
// candidate berths the oracle returned. One picker at a time (opening a new one
// closes the old). Hovering a row highlights that berth on the map; clicking it
// CONFIRMS the placement straight away.
let closeFeasPicker = null;

// Guard so the one document-level "click outside closes the kebab menu" listener
// is attached once, not re-added on every loadRequests() re-render.
let _resMenuWired = false;

function openFeasPicker(anchorBtn, resId, payload) {
  if (closeFeasPicker) closeFeasPicker();
  const cands = payload.candidates || [];
  const obsN = (payload.observed || []).length;
  const v = payload.vessel || {};
  const requiredFt = payload.required_ft;   // draft + under-keel clearance, ft
  const dockRange = (c) => `Dock ${Math.round(Math.min(c.dock_lo, c.dock_hi))}–${Math.round(Math.max(c.dock_lo, c.dock_hi))}`;
  // Chip shows the actual controlling depth (the shallowest reading under the
  // whole hull), so "shallow" is never a bare label — the operator sees the ft.
  const depthChip = (d) => {
    if (d.controlling_ft == null) return '<span class="feas-chip warn">no survey</span>';
    const ft = `${d.controlling_ft.toFixed(0)} ft`;
    return d.status === "ok"
      ? `<span class="feas-chip ok">${ft} deep</span>`
      : `<span class="feas-chip warn" title="shallowest under the hull">${ft} · too shallow</span>`;
  };

  const pop = document.createElement("div");
  pop.className = "feas-pop";
  const rows = cands.map((c, i) =>
    `<button type="button" class="feas-cand ${c.depth.status === "ok" ? "ok" : "warn"}" data-i="${i}">` +
    `<span class="feas-berth">${c.berth ? esc(c.berth) : "Open berth"}</span>` +
    `<span class="feas-where">${dockRange(c)}</span>` +
    depthChip(c.depth) + `</button>`
  ).join("");
  // Header spells out draft + the depth the vessel needs, so a "too shallow"
  // berth is self-explanatory (draft is metres-canonical; shown here in ft).
  const draftBit = v.draft_ft != null
    ? ` · draft ${Math.round(v.draft_ft)} ft${requiredFt != null ? ` <span class="feas-need">needs ${Math.round(requiredFt)} ft</span>` : ""}`
    : "";
  pop.innerHTML =
    `<div class="feas-pop-head"><b>${esc(v.name || "vessel")}</b> · LOA ${Math.round(v.loa_ft)} ft${draftBit}` +
    `<button type="button" class="feas-pop-x" title="Close">×</button></div>` +
    (cands.length
      ? `<div class="feas-pop-hint">${cands.length} berth${cands.length > 1 ? "s" : ""} clear of every other vessel and booking · hover to locate · click to confirm. Depth = shallowest under the full hull.${obsN ? ` ${obsN} observed alongside (avoided).` : ""}</div>` +
        `<div class="feas-cands">${rows}</div>`
      : `<div class="feas-pop-hint">No berth fits this vessel in its window — every spot is booked, occupied, or too short.${obsN ? ` ${obsN} observed alongside now.` : ""}</div>`) +
    `<div class="feas-pop-result"></div>`;
  document.body.appendChild(pop);

  // Anchor under the button, kept within the viewport.
  const r = anchorBtn.getBoundingClientRect();
  pop.style.top = `${Math.round(r.bottom + 6)}px`;
  pop.style.left = `${Math.round(Math.min(r.left, window.innerWidth - pop.offsetWidth - 10))}px`;

  const result = pop.querySelector(".feas-pop-result");
  let done = false;
  const close = () => {
    if (done) return;
    done = true;
    document.removeEventListener("mousedown", onOutside, true);
    document.removeEventListener("keydown", onKey, true);
    pop.remove();
    clearFeasibility();
    if (closeFeasPicker === close) closeFeasPicker = null;
  };
  closeFeasPicker = close;
  const onOutside = (e) => { if (!pop.contains(e.target) && e.target !== anchorBtn) close(); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  // Defer so the click that opened the picker doesn't immediately close it.
  setTimeout(() => document.addEventListener("mousedown", onOutside, true), 0);
  document.addEventListener("keydown", onKey, true);
  pop.querySelector(".feas-pop-x").addEventListener("click", close);

  pop.querySelectorAll(".feas-cand").forEach((btn) => {
    const c = cands[Number(btn.dataset.i)];
    btn.addEventListener("mouseenter", () => highlightFeasSlot(c.popa_lo, c.popa_hi));
    btn.addEventListener("mouseleave", () => clearFeasHighlight());
    btn.addEventListener("click", () => confirmCandidate(resId, c, requiredFt, result, close));
  });
}

// Confirm a chosen candidate berth directly: PATCH the reservation to placed +
// confirmed. A shallow / unsurveyed berth needs an explicit depth override
// (the oracle already flagged it), so ask before sending it. Refreshes on success.
async function confirmCandidate(resId, c, requiredFt, result, close) {
  result.className = "feas-pop-result";
  const base = { bow_dock: c.bow_dock, direction: c.direction, status: "confirmed", unassigned: false };
  let payload = base;
  if (c.depth.status !== "ok") {
    const need = requiredFt != null ? ` (needs ${Math.round(requiredFt)} ft under keel)` : "";
    const why = c.depth.status === "shallow"
      ? `Shallowest depth under the hull here is ${c.depth.controlling_ft.toFixed(1)} ft${need} — ${c.depth.shortfall_ft} ft short.`
      : "No depth survey covers this berth.";
    if (!confirm(`${why}\n\nConfirm the placement anyway? (logged as a depth override)`)) return;
    payload = { ...base, depth_override: true };
  }
  result.textContent = "Confirming…";
  let w = await apiWrite("PATCH", `/reservations/${resId}`, payload);
  // A depth block we didn't pre-empt (survey changed since the lookup) -> offer override.
  if (!w.ok && w.status === 422 && !payload.depth_override
      && confirm("Too shallow to confirm. Override and confirm anyway?")) {
    w = await apiWrite("PATCH", `/reservations/${resId}`, { ...base, depth_override: true });
  }
  if (w.ok) {
    close();
    loadRequests(); loadTimeline(); loadStats(); loadConflicts(); loadAlongside();
  } else {
    result.className = "feas-pop-result err";
    result.textContent = "Failed: " + writeError(w);
  }
}

function wireResCards(container) {
  // One document listener for "click outside closes any open kebab menu" — added
  // once (the per-card listeners below are fine to re-add since the nodes are fresh
  // each render, but a document listener would otherwise stack).
  if (!_resMenuWired) {
    _resMenuWired = true;
    document.addEventListener("click", (e) => {
      if (e.target.closest(".req-kebab") || e.target.closest(".req-menu")) return;
      container.querySelectorAll(".req-menu.open").forEach((m) => m.classList.remove("open"));
    });
  }
  container.querySelectorAll(".req-card[data-res]").forEach((card) => {
    const id = Number(card.dataset.res);
    const form = card.querySelector("[data-edit]");
    const res = form.querySelector(".mini-result");
    // Kebab overflow: toggle this card's menu (closing any other), and close it
    // after any item is chosen. The item handlers themselves are wired below by
    // their [data-act] — they resolve whether on the face or inside the menu.
    const kebab = card.querySelector(".req-kebab");
    const menu = card.querySelector(".req-menu");
    if (kebab && menu) {
      kebab.addEventListener("click", (e) => {
        e.stopPropagation();
        const open = menu.classList.contains("open");
        container.querySelectorAll(".req-menu.open").forEach((m) => m.classList.remove("open"));
        if (!open) menu.classList.add("open");
      });
      menu.querySelectorAll("button").forEach((b) =>
        b.addEventListener("click", () => menu.classList.remove("open")));
    }
    card.querySelector('[data-act="edit"]').addEventListener("click", () => form.classList.toggle("open"));
    form.querySelector('[data-act="close"]').addEventListener("click", () => form.classList.remove("open"));

    // Separate "Edit request" form: revise the berth-request info as received —
    // arrival/departure timestamps and cargo/notes — without touching the
    // placement (bow + heading), which stays on the "Edit placement" form.
    const reqForm = card.querySelector("[data-edit-request]");
    const reqRes = reqForm.querySelector(".mini-result");
    card.querySelector('[data-act="edit-request"]').addEventListener("click", () => reqForm.classList.toggle("open"));
    reqForm.querySelector('[data-act="close-request"]').addEventListener("click", () => reqForm.classList.remove("open"));
    reqForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      reqRes.className = "mini-result";
      const payload = { cargo: reqForm.elements.cargo.value, notes: reqForm.elements.notes.value };
      const etb = localInputToIso(reqForm.elements.etb.value);
      const etd = localInputToIso(reqForm.elements.etd.value);
      if (etb) payload.etb = etb;   // blank -> leave the stored arrival as-is
      if (etd) payload.etd = etd;   // blank -> leave the stored departure as-is
      const w = await apiWrite("PATCH", `/reservations/${id}`, payload);
      if (w.ok) {
        reqRes.className = "mini-result ok";
        reqRes.innerHTML = "Saved." + warnHtml(w.data && w.data.warnings);
        loadTimeline();
        loadStats();   // status change may move the confirmed / reservation counts
        loadConflicts();   // ...and may create or resolve a conflict
        loadAlongside();
        setTimeout(loadRequests, 1200);   // let the confirmation show first
      } else {
        reqRes.className = "mini-result err";
        reqRes.textContent = "Failed: " + writeError(w);
      }
    });

    // Promote a berth request to confirmed: open the edit form prefilled with
    // status=confirmed and drop the operator on the bow field — the one thing a
    // requested row is missing (its schedule is already set). Saving runs the
    // confirmed-only no-overlap check (a clash surfaces as a 409 in the
    // mini-result).
    const confirmBtn = card.querySelector('[data-act="confirm"]');
    if (confirmBtn) confirmBtn.addEventListener("click", () => {
      form.classList.add("open");
      form.elements.status.value = "confirmed";
      form.scrollIntoView({ behavior: "smooth", block: "nearest" });
      form.elements.bow_dock.focus();
    });

    // Unconfirm: back a confirmed reservation out to a pending `requested` row
    // rather than cancelling/deleting it. The placement (station range + time)
    // is preserved, the row stays in the active list, and the "Place + Confirm"
    // button reappears (it renders only for `requested`), so the operator can
    // re-place and re-confirm the very same request.
    const unconfirmBtn = card.querySelector('[data-act="unconfirm"]');
    if (unconfirmBtn) unconfirmBtn.addEventListener("click", async () => {
      if (!confirm("Unconfirm this reservation? It returns to a pending request — placement is kept and you can re-confirm it.")) return;
      const w = await apiWrite("PATCH", `/reservations/${id}`, { status: "requested" });
      if (w.ok) { loadRequests(); loadTimeline(); loadStats(); loadConflicts(); loadAlongside(); } else alert("Failed: " + writeError(w));
    });
    // Cancel: clear the placement and return the row to a blank, pending
    // request. Sends status=requested + unassigned=true (no bow/heading), so the
    // server frees the berth, empties the station range (gone from the timeline
    // bar), and drops the row back to 'requested' — WITHOUT re-running bow
    // placement (which could fail on a vessel with no LOA). The request stays in
    // the reservations tab (the "Place + Confirm" button reappears, since it
    // renders only for 'requested'), so it can be re-placed and re-confirmed; the
    // arrival/departure window is kept. A hard delete (row + reservation) lives
    // on the Berth requests tab.
    const cancelBtn = card.querySelector('[data-act="cancel"]');
    if (cancelBtn) cancelBtn.addEventListener("click", async () => {
      if (!confirm("Cancel this placement? It clears the berth, station range, and timeline bar and returns the row to a pending request (blank, unconfirmed) so you can re-place it. The arrival/departure stay.")) return;
      const w = await apiWrite("PATCH", `/reservations/${id}`, { status: "requested", unassigned: true });
      if (w.ok) { loadRequests(); loadTimeline(); loadStats(); loadConflicts(); loadAlongside(); } else alert("Failed: " + writeError(w));
    });

    // Find berth: ask the feasibility oracle where this vessel fits in its window
    // and open a hover picker of the concrete, vessel-sized candidate berths.
    // Hovering a row highlights that slot on the map; clicking it CONFIRMS the
    // placement directly (the depth gate + overlap constraint stay the backstop).
    const findBtn = card.querySelector('[data-act="find-berth"]');
    if (findBtn) findBtn.addEventListener("click", async () => {
      findBtn.disabled = true;
      const prev = findBtn.textContent;
      findBtn.textContent = "Finding…";
      try {
        const p = await api(`/feasibility?reservation_id=${id}`);
        renderFeasibility(p);
        openFeasPicker(findBtn, id, p);
      } catch (e) {
        clearFeasibility();
        alert("Couldn’t find berths: " + esc(String(e.message || e)));
      } finally {
        findBtn.disabled = false;
        findBtn.textContent = prev;
      }
    });

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      res.className = "mini-result";
      // Only bow + heading place the vessel; the schedule stays as set by the
      // berth request (no ETB/ETD here). The stern is derived server-side from
      // the bow, the heading, and the vessel's LOA.
      const payload = formPayload(form, ["bow_dock", "priority"]);
      payload.depth_override = form.elements.depth_override.checked;
      // Placing needs both bow + heading. If the heading isn't set, leave the
      // station range untouched (this is a plain status/cargo edit) rather than
      // sending a half-placement the server would reject.
      if (!form.elements.direction.value) delete payload.bow_dock;
      const w = await apiWrite("PATCH", `/reservations/${id}`, payload);
      if (w.ok) {
        res.className = "mini-result ok";
        res.innerHTML = "Saved." + warnHtml(w.data && w.data.warnings);
        clearFeasibility();   // the placement moved; any painted bands are now stale
        loadTimeline();
        loadStats();   // status change may move the confirmed / reservation counts
        loadConflicts();   // ...and may create or resolve a conflict
        loadAlongside();
        setTimeout(loadRequests, 1500);   // let the confirmation (and any warning) show first
      } else {
        res.className = "mini-result err";
        res.textContent = "Failed: " + writeError(w);
      }
    });
  });
}

// An IMO number is 7 digits + a check digit (the 7th): the trailing digit
// equals (sum of digit_i * (7-i) for the first six) mod 10. Mirrors
// app/intake/manual.valid_imo so the form rejects a typo like "75" before it
// reaches the server (which validates again).
function isValidImo(imo) {
  const s = String(imo);
  if (!/^[0-9]{7}$/.test(s)) return false;
  let sum = 0;
  for (let i = 0; i < 6; i++) sum += Number(s[i]) * (7 - i);
  return sum % 10 === Number(s[6]);
}

// --- Berth requests (write) ------------------------------------------------
// One form, two modes: record a new request (POST) or correct an existing
// manual-channel one in place (PATCH). editBerthRequest() flips it into edit
// mode (pre-filling from the raw payload); the cancel button / a successful
// save flips it back. Exported (via the module-level binding) so reqCard's Edit
// button can call it.
(function () {
  const form = document.getElementById("berthRequestForm");
  if (!form) return;
  const result = document.getElementById("brResult");
  const btn = document.getElementById("brSubmit");
  const cancelBtn = document.getElementById("brCancelEdit");

  let editingId = null;   // intake_event id being edited, or null in create mode

  function setMode(id) {
    editingId = id;
    btn.textContent = id ? "Save changes" : "Record berth request";
    cancelBtn.style.display = id ? "" : "none";
  }

  // Today's calendar date as YYYY-MM-DD for the <input type=date> default. Uses
  // the operator's local (Central, at the port) calendar day — request_date is
  // just "when we took the request", not an absolute instant.
  function todayLocalDate() {
    const d = new Date();
    return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  }

  // Default the "Date" (request_date) field to today when it's blank, so a new
  // request is stamped without the operator typing it. Skipped when already set
  // (e.g. an edit prefilled it from the raw payload).
  function setDefaultDate() {
    const el = form.elements.request_date;
    if (el && !el.value) el.value = todayLocalDate();
  }

  function resetForm() {
    form.reset();
    clearAutofill();
    setDefaultDate();
    setMode(null);
  }

  cancelBtn.addEventListener("click", () => {
    resetForm();
    result.className = "result";
    result.textContent = "";
  });

  // Pre-fill the form from a raw manual-request payload and switch to edit mode.
  // Raw keys mirror the form's field names (set by BerthRequestForm.model_dump),
  // so we can drive inputs directly; datetime-local wants "YYYY-MM-DDTHH:MM".
  // `vessel` (when present) carries the linked vessel's *effective* dimensions in
  // feet; for an AIS-tracked ship those override the typed entry (see below).
  editBerthRequest = function (id, raw, vessel) {
    resetForm();
    for (const el of form.elements) {
      if (!el.name || !(el.name in raw)) continue;
      const v = raw[el.name];
      if (el.type === "checkbox") el.checked = !!v;
      else if (el.type === "datetime-local") el.value = v ? String(v).slice(0, 16) : "";
      else el.value = v == null ? "" : v;
    }
    // For an AIS-tracked vessel, AIS dimensions are authoritative and persist
    // through the edit — the entry's typed dims were never applied. Show the
    // effective AIS value (only where AIS actually has one), not the original
    // entry, so the form reflects what's really in force. A dim AIS lacks keeps
    // the operator's typed value (that one does apply).
    let aisNote = "";
    if (vessel && vessel.ais_tracked) {
      const shown = [];
      for (const [name, ft] of [["length_ft", vessel.loa_ft], ["beam_ft", vessel.beam_ft], ["draft_ft", vessel.draft_ft]]) {
        const el = form.elements[name];
        if (ft == null || !el) continue;
        el.value = ft;
        // Tint the AIS value + arm confirm-to-pin on change (skip when already
        // operator-pinned: those are the operator's own values, not AIS's).
        if (!vessel.dims_locked) markAisDim(el, ft);
        shown.push(`${name.replace("_ft", "")} ${ft} ft`);
      }
      if (shown.length) {
        aisNote = vessel.dims_locked
          // Already pinned via Manual override: the shown dims are the operator's
          // own, managed on the vessel — editing here re-pins them on save.
          ? ` Dimensions are operator-pinned via manual override (${shown.join(", ")}); edit any and save to re-pin, or unlock them on the vessel.`
          : ` AIS-reported dimensions shown (${shown.join(", ")}); edit any to override — you'll confirm before it's pinned.`;
      }
    }
    setMode(id);
    document.getElementById("intakePanel").open = true;
    form.scrollIntoView({ behavior: "smooth", block: "nearest" });
    result.className = "result";
    result.textContent = `Editing berth request #${id}.` + aisNote;
  };

  // --- IMO auto-fill (Tier 1) ----------------------------------------------
  // In CREATE mode, a structurally-valid IMO looks the ship up (GET
  // /vessels/lookup) and pre-fills name + dims before submit — the biggest lever
  // for "input ships with minimal info". Fill only blank fields, EXCEPT an
  // AIS-tracked vessel's LOA/beam/draft, which are AIS-authoritative: overwrite
  // and lock them (a typed value wouldn't apply — the ingestor owns them; a dim
  // AIS lacks stays editable). A miss prompts manual entry; the same call will
  // one day answer from an external provider (Tier 2) with no change here.
  const imoInput = form.elements.imo;
  const dimInputs = ["length_ft", "beam_ft", "draft_ft"].map((n) => form.elements[n]);
  let lastLookup = null;   // IMO we last resolved, so re-blur doesn't re-query

  function clearAutofill() {
    // Drop the AIS flag/tint from the dim fields (undo a prior lookup); leaves any
    // typed values in place.
    dimInputs.forEach((el) => unmarkAisDim(el));
    lastLookup = null;
  }
  // Live tinting: an AIS dim flips to "edited" the moment its value differs from
  // the AIS value, so a pending confirm-to-pin override is visible before save.
  dimInputs.forEach((el) => wireAisDimEdit(el));

  async function autofillFromImo() {
    if (editingId) return;                              // create mode only
    const raw = String(imoInput.value || "").trim();
    if (!isValidImo(raw)) { clearAutofill(); return; } // wait for a real IMO
    if (raw === lastLookup) return;
    lastLookup = raw;
    let data;
    try {
      data = await api(`/vessels/lookup?imo=${encodeURIComponent(raw)}`);
    } catch { lastLookup = null; return; }             // stay silent; submit-time guard still applies
    // Drop a stale result if the operator moved on or opened an edit meanwhile.
    if (editingId || String(imoInput.value || "").trim() !== raw) return;

    if (!data || !data.found) {
      clearAutofill();
      lastLookup = raw;
      result.className = "result";
      result.innerHTML = `<span class="warn">▲ IMO ${esc(raw)} not on file — enter the vessel’s dimensions manually.</span>`;
      return;
    }

    const notes = [];
    // Name: fill if blank; never overwrite a typed name — flag a mismatch instead
    // (pre-empts the server's one-IMO-one-ship 422).
    const nameEl = form.elements.vessel;
    if (data.name) {
      const typed = String(nameEl.value || "").trim();
      if (!typed) nameEl.value = data.name;
      else if (typed.toLowerCase() !== data.name.toLowerCase())
        notes.push(`IMO is on file as “${esc(data.name)}” (kept your entry “${esc(typed)}”)`);
    }
    // Dims: AIS-authoritative ones fill + flag (editable — overtyping arms a
    // confirm-to-pin override on save); others fill only when blank.
    const shownLock = [];
    for (const [name, ft] of [["length_ft", data.loa_ft], ["beam_ft", data.beam_ft], ["draft_ft", data.draft_ft]]) {
      const el = form.elements[name];
      if (!el) continue;
      if (data.ais_tracked && ft != null) {
        el.value = ft;
        markAisDim(el, ft);
        shownLock.push(`${name.replace("_ft", "")} ${ft} ft`);
      } else {
        unmarkAisDim(el);
        if (ft != null && !String(el.value || "").trim()) el.value = ft;
      }
    }
    result.className = "result ok";
    let msg = `Filled from on-file data for IMO ${esc(raw)}.`;
    if (shownLock.length) {
      msg += data.dims_locked
        ? ` Dimensions are operator-pinned via manual override (${shownLock.join(", ")}); edit any and save to re-pin, or unlock them on the vessel.`
        : ` AIS-reported dimensions shown (${shownLock.join(", ")}); edit any to override — you’ll confirm before it’s pinned.`;
    }
    if (notes.length) msg += ` ${notes.join("; ")}.`;
    result.innerHTML = msg;
  }

  let imoTimer = null;
  let pendingLookup = null;                     // in-flight autofill, so submit can await it
  function scheduleLookup() { pendingLookup = autofillFromImo(); }
  imoInput.addEventListener("input", () => {
    clearTimeout(imoTimer);
    imoTimer = setTimeout(scheduleLookup, 350);
  });
  imoInput.addEventListener("blur", scheduleLookup);

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    result.className = "result";
    btn.disabled = true; btn.textContent = editingId ? "Saving…" : "Recording…";

    // Race guard: if the operator typed an IMO and hit Record before the debounced
    // lookup fired (or while it's still in flight), run/await it now so the
    // auto-filled name + dims are in the form before we build the payload.
    if (!editingId) {
      clearTimeout(imoTimer);
      if (isValidImo(String(imoInput.value || "").trim())) scheduleLookup();
      await pendingLookup;
    }

    // Build a typed JSON payload: skip blanks, coerce numbers, checkboxes->bool.
    const numFields = new Set(["imo", "length_ft", "beam_ft", "draft_ft", "deadweight_lbs", "inbound_tons", "outbound_tons", "bunker_qty_mt"]);
    const boolFields = new Set(["bunkers", "bunkering_acknowledged"]);
    const payload = {};
    for (const [k, v] of new FormData(form).entries()) {
      if (boolFields.has(k)) continue;          // handled below (unchecked boxes are absent from FormData)
      if (v === "" || v == null) continue;
      payload[k] = numFields.has(k) ? Number(v) : v;
    }
    payload.bunkers = form.elements.bunkers.checked;
    payload.bunkering_acknowledged = form.elements.bunkering_acknowledged.checked;

    const editing = editingId;
    // Guard a content-empty NEW request: the native `required` on IMO normally
    // blocks this, but never POST a blank form (which would land a stray empty
    // berth-request card). IMO is the key field now — a name/dims auto-fill from
    // it — so require at least an IMO or a vessel name. Editing is always allowed.
    if (!editing && !payload.imo && !payload.vessel) {
      result.className = "result err";
      result.textContent = "Enter at least an IMO before recording a request.";
      btn.disabled = false; btn.textContent = "Record berth request";
      return;
    }
    // Reject a malformed IMO (wrong length or bad check digit) before sending —
    // an IMO is a 7-digit ship key, so a "75" must not become a vessel record.
    if (payload.imo != null && !isValidImo(payload.imo)) {
      result.className = "result err";
      result.textContent = `“${payload.imo}” is not a valid IMO number — it must be 7 digits with a correct check digit.`;
      btn.disabled = false; btn.textContent = editing ? "Save changes" : "Record berth request";
      return;
    }
    const [method, url] = editing
      ? ["PATCH", `/intake/berth-requests/${editing}`]
      : ["POST", "/intake/berth-request"];

    try {
      const r = await fetch(url, {
        method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail ? JSON.stringify(data.detail) : ("HTTP " + r.status));
      result.className = data.skipped ? "result err" : "result ok";
      const msg = editing
        ? `Updated request #${editing}. Reservation #${data.reservation_id ?? "—"}, vessel #${data.vessel_id ?? "—"}.`
        : data.skipped
          ? "Nothing recorded — the request was empty."
          : data.duplicate
            ? "Already on file — duplicate request, nothing added."
            : `Recorded. Reservation #${data.reservation_id ?? "—"} (requested), vessel #${data.vessel_id ?? "—"}.`;
      const warns = (data.warnings && data.warnings.length)
        ? `<span class="warn">▲ ${data.warnings.join("; ")}</span>` : "";
      result.innerHTML = msg + warns;
      // Dropped an AIS-tracked vessel's entered dims (AIS is authoritative)? If the
      // operator actually changed a dim in THIS session (a live .ais-dim-edited
      // field), offer to pin it with a single confirm — the easy override for when
      // AIS itself is wrong. A persisted prior override that merely rode through an
      // unrelated edit (e.g. changing only the date) keeps the quiet red button
      // instead, so the dialog doesn't re-pop. Read the flag before resetForm().
      const dimEditedNow = !!form.querySelector(".ais-dim-edited");
      if (dimEditedNow) await offerOverrideConfirm(result, data);
      else renderManualOverride(result, data);
      if (editing || (!data.duplicate && !data.skipped)) resetForm();
      loadRequests(); loadBerthRequests(); loadVessels(); loadStats();
    } catch (err) {
      result.className = "result err";
      result.textContent = "Failed: " + err.message;
    } finally {
      btn.disabled = false;
      btn.textContent = editingId ? "Save changes" : "Record berth request";
    }
  });

  setDefaultDate();   // stamp today's date on first render (create mode)
})();

// Reservations status filter -> reload the list.
(function () {
  const sel = document.getElementById("resFilter");
  if (sel) sel.addEventListener("change", loadRequests);
})();

// --- Sidebar tabs ----------------------------------------------------------
// Toggle the .active panel; the last-selected tab persists in localStorage so a
// refresh keeps you on the same view.
(function () {
  const nav = document.getElementById("sidebarTabs");
  if (!nav) return;
  const panels = document.querySelectorAll(".tab-panel");
  function show(name) {
    nav.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    panels.forEach((p) => p.classList.toggle("active", p.dataset.panel === name));
    localStorage.setItem("sidebarTab", name);
    // History is a heavier (sessionizing) query, so load it lazily on first
    // activation rather than on boot / in the 15s poll.
    if (name === "history") loadHistory();
    // Refresh the saved-ships list on activation (it's cheap and not in the 15s
    // poll), so an override / new AIS vessel shows without a full reload.
    if (name === "ships") loadVessels();
  }
  nav.querySelectorAll(".tab").forEach((b) =>
    b.addEventListener("click", () => show(b.dataset.tab)));
  const saved = localStorage.getItem("sidebarTab");
  if (saved && nav.querySelector(`.tab[data-tab="${saved}"]`)) show(saved);
})();

// --- Berth requests (raw intake_event audit trail) -------------------------
// The actual inbound requests as received (phone / email / operator),
// distinct from the reservations they project into. Read-only.
let _berthRequestRows = [];   // last-loaded rows (unfiltered), keyed for the delegated handlers

// The channel a request reads as: phone/email/operator ride through as themselves,
// but the AI-normalized online form (source='ai') and the retired legacy online
// form (source='form') both read as one "form" channel — a worker never picks
// "form" as a received-via (Dataverse pulls it), so it is display/filter only.
function reqChannel(source) {
  if (source === "ai" || source === "form") return "form";
  if (source === "phone" || source === "email" || source === "operator") return source;
  return "operator";
}
// Short uppercase badge for the card header. sourceLabel already maps ai->form.
const channelBadge = (source) => sourceLabel(source).toUpperCase();

// The four filter segments. One is active at a time (there is no "all"); the
// selection persists so the tab reopens where the operator left it.
const REQ_CHANNELS = [
  { key: "phone", label: "Phone" },
  { key: "email", label: "Email" },
  { key: "operator", label: "Operator" },
  { key: "form", label: "Form" },
];
let _reqChannel = localStorage.getItem("reqChannel") || "form";

export async function loadBerthRequests() {
  const el = document.getElementById("berthRequests");
  if (!el) return;
  el.innerHTML = '<div class="empty">loading…</div>';
  let rows;
  try {
    rows = await api("/intake/berth-requests?limit=200");
  } catch (e) {
    el.innerHTML = '<div class="empty">unavailable (DB offline)</div>';
    const seg = document.getElementById("reqSourceSeg");
    if (seg) seg.innerHTML = "";
    return;
  }
  _berthRequestRows = rows;
  renderReqSeg(rows);
  const shown = rows.filter((r) => reqChannel(r.source) === _reqChannel);
  if (!shown.length) {
    const lbl = (REQ_CHANNELS.find((c) => c.key === _reqChannel) || {}).label || _reqChannel;
    el.innerHTML = `<div class="empty">no ${esc(lbl.toLowerCase())} requests on file</div>`;
    return;
  }
  el.innerHTML = shown.map(reqCard).join("");
}

// Segmented channel filter: a button per channel with its live count, the active
// one highlighted. Rebuilt on every load so counts track the data.
function renderReqSeg(rows) {
  const seg = document.getElementById("reqSourceSeg");
  if (!seg) return;
  const counts = {};
  for (const r of rows) { const c = reqChannel(r.source); counts[c] = (counts[c] || 0) + 1; }
  seg.innerHTML = REQ_CHANNELS.map((c) =>
    `<button type="button" data-seg="${c.key}" class="${c.key === _reqChannel ? "active" : ""}" ` +
    `role="tab" aria-selected="${c.key === _reqChannel}">${c.label}<span class="c">${counts[c.key] || 0}</span></button>`
  ).join("");
}

// Cards are re-rendered via innerHTML, so delegate the Edit / kebab / copy / Delete
// clicks to the container and look the row up by id to hand its raw payload to the form.
(function () {
  const el = document.getElementById("berthRequests");
  if (!el) return;
  el.addEventListener("click", async (e) => {
    const editBtn = e.target.closest("[data-edit-req]");
    if (editBtn) {
      const id = Number(editBtn.dataset.editReq);
      const row = _berthRequestRows.find((r) => r.id === id);
      if (row) editBerthRequest(id, row.raw || {}, row.vessel || null);
      return;
    }
    // Kebab toggles its own menu (Delete lives there); only one menu open at a time.
    const kebab = e.target.closest("[data-kebab]");
    if (kebab) {
      const menu = el.querySelector(`[data-menu="${kebab.dataset.kebab}"]`);
      const wasOpen = menu && menu.classList.contains("open");
      el.querySelectorAll(".req-menu.open").forEach((m) => m.classList.remove("open"));
      if (menu && !wasOpen) menu.classList.add("open");
      return;
    }
    // Copy-on-click for contact fields.
    const copyEl = e.target.closest("[data-copy]");
    if (copyEl) {
      try {
        await navigator.clipboard.writeText(copyEl.dataset.copy);
        copyEl.classList.add("copied");
        setTimeout(() => copyEl.classList.remove("copied"), 1200);
      } catch (_) { /* clipboard blocked — no-op */ }
      return;
    }
    const delBtn = e.target.closest("[data-delete-req]");
    if (delBtn) {
      const id = Number(delBtn.dataset.deleteReq);
      if (!confirm("Delete this berth request and its requested reservation?")) return;
      const w = await apiWrite("DELETE", `/intake/berth-requests/${id}`);
      if (w.ok) { loadBerthRequests(); loadRequests(); loadTimeline(); loadStats(); loadConflicts(); loadAlongside(); }
      else alert("Failed: " + writeError(w));
    }
  });
  // Dismiss any open kebab menu on an outside click.
  document.addEventListener("click", (e) => {
    if (e.target.closest("[data-kebab]") || e.target.closest(".req-menu")) return;
    el.querySelectorAll(".req-menu.open").forEach((m) => m.classList.remove("open"));
  });
})();

// Pull a display value from a raw intake payload across both shapes: the manual
// form (lowercase keys: vessel/imo/etb/etd/agency) and any legacy online-form
// row left in the table (capitalized keys: "Vessel"/"IMO Number"/...).
function rawField(raw, keys) {
  if (!raw) return null;
  for (const k of keys) {
    const v = raw[k];
    if (v !== undefined && v !== null && v !== "") return v;
  }
  return null;
}

// --- card render helpers ---------------------------------------------------

// Processing state -> {label, colour}. A reconciled reservation shows its own
// status in the matching palette; a processed request with no reservation says so;
// an unprocessed request (no arrival date yet) is flagged for action. These are the
// system's real statuses — not invented approve/reject states.
function statusInfo(r) {
  if (r.reservation_id) {
    const s = r.reservation_status || "reserved";
    return { label: s, color: BADGE_COLORS[s] || "var(--ink-dim)" };
  }
  if (r.processed) return { label: "no reservation", color: "var(--muted)" };
  return { label: "unprocessed", color: "var(--warn)" };
}

// Compact relative age ("30m ago", "3h ago", "2d ago") off the browser clock; the
// card carries the absolute Central instant in a title for hover.
function relTime(iso) {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (isNaN(t)) return "";
  const future = Date.now() < t;
  let s = Math.abs(Date.now() - t) / 1000;
  let out;
  if (s < 45) return "just now";
  if (s < 3600) out = `${Math.round(s / 60)}m`;
  else if (s < 86400) out = `${Math.round(s / 3600)}h`;
  else if (s < 86400 * 30) out = `${Math.round(s / 86400)}d`;
  else out = `${Math.round(s / (86400 * 30))}mo`;
  return future ? `in ${out}` : `${out} ago`;
}

// The AI channel stamps "AI-parsed (confidence 99%)" into notes; pull the percent
// back out for a discreet header pill. Null when absent (operator/phone/email).
function parseConfidence(notes) {
  if (!notes) return null;
  const m = String(notes).match(/confidence\s+(\d+(?:\.\d+)?)\s*%/i);
  return m ? Math.round(parseFloat(m[1])) : null;
}

const has = (v) => v !== undefined && v !== null && v !== "";
function fmtNum(n, dp) {
  return Number(n).toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: dp });
}
function rawNum(raw, keys) {
  const v = rawField(raw, keys);
  return has(v) && !isNaN(Number(v)) ? Number(v) : null;
}

// ETB -> ETD as a compact mono timeline with duration, plus from/to ports. Empty
// string when there's nothing to show (so the strip is omitted entirely).
function durLabel(etb, etd) {
  if (!etb || !etd) return null;
  const a = new Date(etb).getTime(), b = new Date(etd).getTime();
  if (isNaN(a) || isNaN(b)) return null;
  const h = Math.round((b - a) / 3600000);
  if (h < 48) return `${h}h`;
  const d = Math.floor(h / 24), rh = h % 24;
  return rh ? `${d}d ${rh}h` : `${d}d`;
}
function schedStrip(raw) {
  const etb = rawField(raw, ["etb", "Port Arrival Date"]);
  const etd = rawField(raw, ["etd", "Port Departure Date"]);
  const from = rawField(raw, ["due_from"]);
  const to = rawField(raw, ["sail_for"]);
  const dest = rawField(raw, ["destinations"]);
  const ports = (from || to)
    ? `${esc(from || "?")} <span class="arw">→</span> ${esc(to || "?")}`
    : (dest ? esc(String(dest)) : "");
  if (!etb && !etd && !ports) return "";
  let time = "";
  if (etb || etd) {
    const a = `<span class="t">${esc(etb ? fmtCentral(etb) : "—")}</span>`;
    const b = etd ? ` <span class="arw">→</span> <span class="t">${esc(fmtCentral(etd))}</span>` : "";
    const dur = durLabel(etb, etd);
    time = `${a}${b}${dur ? ` <span class="dur">· ${dur}</span>` : ""}`;
  }
  return `<div class="req-sched">${time}${ports ? `<span class="ports">${ports}</span>` : ""}</div>`;
}

// Four dimension tiles (LOA/BEAM/DRAFT/DWT). LOA/beam/draft prefer the linked
// vessel's effective feet (AIS-authoritative); DWT is the request's own figure.
function dimStrip(r) {
  const raw = r.raw || {}, v = r.vessel || {};
  const loa = v.loa_ft != null ? v.loa_ft : rawNum(raw, ["length_ft"]);
  const beam = v.beam_ft != null ? v.beam_ft : rawNum(raw, ["beam_ft"]);
  const draft = v.draft_ft != null ? v.draft_ft : rawNum(raw, ["draft_ft"]);
  const dwt = rawNum(raw, ["deadweight_lbs"]);
  return `<div class="req-dims">${[
    dimCell("LOA", loa, "ft", 0),
    dimCell("Beam", beam, "ft", 0),
    dimCell("Draft", draft, "ft", 1),
    dimCell("DWT", dwt, "lb", 0),
  ].join("")}</div>`;
}
function dimCell(label, val, unit, dp) {
  const ok = val != null && !isNaN(Number(val));
  const body = ok ? `${fmtNum(val, dp)}<span class="u">${unit}</span>` : "—";
  return `<div class="req-dim${ok ? "" : " blank"}"><span class="l">${esc(label)}</span><span class="n">${body}</span></div>`;
}

// The collapsed "all info" as grouped label-over-value sections. Known fields are
// placed into Vessel / Schedule / Cargo / Contact / Meta; any unrecognized key
// still surfaces under "Other" (nothing silently dropped), except source_raw (a
// JSON blob) and source (already the header badge). Units ride with the value.
function groupedInfo(raw, r) {
  if (!raw || typeof raw !== "object") return `<div class="meta">No details.</div>`;
  const seen = new Set(["source_raw", "source", "vessel", "Vessel", "imo", "IMO Number", "notes"]);
  const pick = (keys, opt = {}) => {
    keys.forEach((k) => seen.add(k));
    let v = null;
    for (const k of keys) { if (has(raw[k])) { v = raw[k]; break; } }
    if (!has(v)) return null;
    if (opt.bool) return v ? "Yes" : null;
    if (opt.dt) return esc(fmtCentral(v)) + " CT";
    let s = esc(String(v));
    if (opt.copy) s = `<span class="req-copy" data-copy="${esc(String(v))}">${s}</span>`;
    if (opt.unit) s += ` <span class="u">${opt.unit}</span>`;
    return s;
  };
  const rows = (pairs) => pairs
    .filter((p) => p && has(p[1]))
    .map(([k, v]) => `<span class="k">${esc(k)}</span><span class="v">${v}</span>`).join("");
  const kv = (pairs) => { const inner = rows(pairs); return inner ? `<div class="req-kv">${inner}</div>` : ""; };
  const group = (title, inner) => inner ? `<div class="req-group"><h4>${title}</h4>${inner}</div>` : "";

  const vessel = group("Vessel", kv([
    ["S/S Line", pick(["ss_line"])],
    ["Flag", pick(["flag"])],
    ["Destinations", pick(["destinations"])],
    ["LOA", pick(["length_ft"], { unit: "ft" })],
    ["Beam", pick(["beam_ft"], { unit: "ft" })],
    ["Draft", pick(["draft_ft"], { unit: "ft" })],
    ["Deadweight", pick(["deadweight_lbs"], { unit: "lb" })],
    ["Taking bunkers", pick(["bunkers"], { bool: true })],
    ["Bunker type", pick(["bunker_type"])],
    ["Bunker fuel", pick(["bunker_qty_mt"], { unit: "mt" })],
    ["Bunkering ack.", pick(["bunkering_acknowledged"], { bool: true })],
  ]));
  const schedule = group("Schedule", kv([
    ["Request date", pick(["request_date"])],
    ["Due from", pick(["due_from"])],
    ["Arrival (ETB)", pick(["etb", "Port Arrival Date"], { dt: true })],
    ["To sail for", pick(["sail_for"])],
    ["Departure (ETD)", pick(["etd", "Port Departure Date"], { dt: true })],
    ["Outbound starts", pick(["outbound_cargo_start"])],
    ["Assigned berth", pick(["Assigned Berth"])],
  ]));
  const inb = kv([
    ["Inbound", pick(["inbound_cargo", "Inbound Cargo"])],
    ["In weight", pick(["inbound_tons"], { unit: "nt" })],
  ]);
  const outb = kv([
    ["Outbound", pick(["outbound_cargo", "Outbound Cargo"])],
    ["Out weight", pick(["outbound_tons"], { unit: "nt" })],
  ]);
  // Structured in/out when the raw carries it; otherwise fall back to the
  // reservation's flat one-line `cargo` string (phone/AI rows) so nothing is lost.
  const cargoFlat = (!inb && !outb && has(r && r.cargo))
    ? group("Cargo", kv([["Cargo", esc(String(r.cargo))]]))
    : "";
  const cargo = (inb || outb) ? group("Cargo", `<div class="req-cargo2">${inb}${outb}</div>`) : cargoFlat;
  const contact = group("Contact", kv([
    ["Agency / Owner", pick(["agency", "Agency/Owner"])],
    ["Requestor", pick(["requestor_name"])],
    ["Email", pick(["requestor_email"], { copy: true })],
    ["Phone", pick(["requestor_phone"], { copy: true })],
  ]));
  const conf = parseConfidence(raw.notes);
  const meta = group("Meta", kv([
    ["Received via", esc(channelBadge(r.source))],
    ["Source tag", esc(r.source || "")],
    ["Confidence", conf != null ? conf + "%" : null],
    ["Notes", has(raw.notes) ? esc(String(raw.notes)) : null],
    ["Received", r.received_at ? esc(fmtCentral(r.received_at)) + " CT" : null],
  ]));

  const others = [];
  for (const k of Object.keys(raw)) {
    if (seen.has(k) || !has(raw[k])) continue;
    const v = raw[k];
    const label = k.replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
    others.push([label, esc(typeof v === "object" ? JSON.stringify(v) : String(v))]);
  }
  const other = others.length ? group("Other", `<div class="req-kv">${rows(others)}</div>`) : "";

  const html = [vessel, schedule, cargo, contact, meta, other].filter(Boolean).join("");
  return html ? `<div class="req-groups">${html}</div>` : `<div class="meta">No details.</div>`;
}

// The reservation card's "all info": a small Reservation group (scheduling fields
// that aren't on the face) followed by the grouped raw intake details — the same
// groupedInfo() the berth-requests tab uses. Raw is absent for an AIS/dredge row,
// so skip it then rather than showing a bare "No details."
function resAllInfo(r) {
  const dock = r.station_unassigned ? null
    : `${Math.round(Math.min(r.station_lo_dock, r.station_hi_dock))}–${Math.round(Math.max(r.station_lo_dock, r.station_hi_dock))}`;
  const rows = [
    ["Type", r.type],
    ["Direction", r.direction || null],
    ["Priority", r.priority != null ? String(r.priority) : null],
    ["Berth", r.berth_name || null],
    ["Dock No.", dock],
  ].filter(([, v]) => has(v))
    .map(([k, v]) => `<span class="k">${esc(k)}</span><span class="v">${esc(String(v))}</span>`).join("");
  const resGroup = rows
    ? `<div class="req-groups"><div class="req-group"><h4>Reservation</h4><div class="req-kv">${rows}</div></div></div>`
    : "";
  const hasRaw = r.raw && typeof r.raw === "object" && Object.keys(r.raw).length > 0;
  return resGroup + (hasRaw ? groupedInfo(r.raw, r) : "");
}

function reqCard(r) {
  const raw = r.raw || {};
  const vessel = rawField(raw, ["vessel", "Vessel"]);
  const imo = rawField(raw, ["imo", "IMO Number"]);
  const flag = rawField(raw, ["flag"]);
  const st = statusInfo(r);
  const rel = relTime(r.received_at);
  // Editable channels (phone/email/operator + the AI-parsed 'ai' channel) carry
  // this form's lowercase keys, so they can be re-edited here. Editability is a
  // separate axis from provenance: 'ai' is a distinct source tag but still
  // operator-correctable; a legacy online-form ('form') row stays immutable.
  const editable = ["phone", "email", "operator", "ai"].includes(r.source);

  const idbits = [];
  if (imo) idbits.push(`IMO ${esc(imo)}`);
  if (flag) idbits.push(esc(String(flag).toUpperCase()));
  const idLine = idbits.length ? `<span class="req-id">${idbits.join(" · ")}</span>` : "";

  const badges = [
    `<span class="status-badge" style="color:${st.color}">${esc(st.label)}</span>`,
    `<span class="status-badge" style="color:var(--muted)">${esc(channelBadge(r.source))}</span>`,
  ];
  if (rel) badges.push(`<span class="req-time" title="${esc(fmtCentral(r.received_at))} CT">${esc(rel)}</span>`);

  const actions = editable ? `
      <div class="req-actions">
        <button class="btn-sm" data-edit-req="${r.id}">Edit</button>
        <span class="sp"></span>
        <button class="req-kebab" data-kebab="${r.id}" aria-label="More actions" aria-haspopup="true">⋮</button>
        <div class="req-menu" data-menu="${r.id}"><button class="danger" data-delete-req="${r.id}">Delete request</button></div>
      </div>` : "";

  return `
    <div class="card req-card">
      <div class="req-head">
        <div class="req-idwrap">
          <div class="req-name">${esc(vessel || "(no vessel name)")}</div>
          ${idLine}
        </div>
        <div class="req-badges">${badges.join("")}</div>
      </div>
      ${schedStrip(raw)}
      ${dimStrip(r)}
      ${actions}
      <details class="req-all">
        <summary class="req-more">All info</summary>
        ${groupedInfo(raw, r)}
      </details>
    </div>`;
}

// Segmented channel filter -> set the active channel and re-render.
(function () {
  const seg = document.getElementById("reqSourceSeg");
  if (!seg) return;
  seg.addEventListener("click", (e) => {
    const b = e.target.closest("[data-seg]");
    if (!b || b.dataset.seg === _reqChannel) return;
    _reqChannel = b.dataset.seg;
    localStorage.setItem("reqChannel", _reqChannel);
    loadBerthRequests();
  });
})();
