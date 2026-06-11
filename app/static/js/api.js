// Shared kernel: pure helpers + constants every other module imports. No DOM,
// no Leaflet. Split out of the former single inline <script> in index.html.

export const api = (p) => fetch(p).then((r) => { if (!r.ok) throw new Error(p + " → " + r.status); return r.json(); });

// Design tokens, read once from the CSS custom properties so the JS-drawn layers
// (Leaflet styles, the SVG timeline, vessel outlines) share the stylesheet's
// palette — no duplicated hex. The colour tokens are literal values (no nested
// var()), so getPropertyValue returns a usable colour string.
export const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
export const PAL = {
  bg: cssVar("--bg"), panel: cssVar("--panel"), panel2: cssVar("--panel-2"),
  line: cssVar("--line"), ink: cssVar("--ink"), inkDim: cssVar("--ink-dim"), muted: cssVar("--muted"),
  blue: cssVar("--blue"), green: cssVar("--green"), amber: cssVar("--amber"),
  red: cssVar("--red"), cyan: cssVar("--cyan"), dredge: cssVar("--dredge"),
};
export const STATUS_COLORS = {
  observed: cssVar("--st-observed"), requested: cssVar("--st-requested"),
  tentative: cssVar("--st-tentative"), confirmed: cssVar("--st-confirmed"),
  completed: cssVar("--st-completed"), cancelled: cssVar("--st-cancelled"),
};
// Status -> badge colour (mirrors the occupancy-timeline palette).
export const RES_STATUS_COLOR = STATUS_COLORS;   // status -> badge colour (chart palette)
// Chip text needs more luminance than a chart bar: the archive tones (completed/
// cancelled) are deliberately dark in STATUS_COLORS, so lift them for badge text
// while everything else keeps the chart palette.
export const BADGE_COLORS = Object.assign({}, STATUS_COLORS, {
  completed: "#7e93a4", cancelled: "#62788a",
});

// Metres -> feet. The canonical store is metres (vessel LOA/beam/draft); feet is
// display only. One constant so every readout converts identically.
export const FT_PER_M = 3.280839895;

// AIS numeric ship-type code -> a coarse category that drives the map dot fill,
// the popup label, and the legend swatches. One source of truth; keep the colour
// here in sync with the --ship-* CSS variables it mirrors.
export const SHIP_CATEGORIES = {
  tanker:    { label: "Tanker",            color: "var(--ship-tanker)" },
  cargo:     { label: "Cargo",             color: "var(--ship-cargo)" },
  tug:       { label: "Tug / towing",      color: "var(--ship-tug)" },
  dredger:   { label: "Dredger",           color: "var(--ship-dredger)" },
  pilot:     { label: "Pilot",             color: "var(--ship-pilot)" },
  passenger: { label: "Passenger",         color: "var(--ship-passenger)" },
  other:     { label: "Other / unknown",   color: "var(--ship-other)" },
};
// AIS type code (0-99) -> category key, per the ITU-R M.1371 ship-type table
// (80s tanker, 70s cargo, 60s passenger, 31/32/52 tug, 33 dredger, 50 pilot).
// 56/57 are the standard's "spare — local vessels" slots, but on the
// Sabine-Neches inland waterway the towboat fleet broadcasts them en masse
// (Cenac/Ingram/Martin etc.), so they read here as tug/towing rather than the
// "other" grey they'd otherwise land in — verified against the live vessel list.
// Anything else — including a NULL/absent ship_type from the API, code 0
// ("not available"), and 90-99 (the spec's own "Other type") — stays "other".
// Only the commercial-traffic buckets relevant to this port are named; fishing,
// sailing/pleasure, etc. fall into "other".
export function shipTypeCategory(t) {
  const n = Number(t);
  if (t == null || Number.isNaN(n)) return "other";
  if (n >= 80 && n <= 89) return "tanker";
  if (n >= 70 && n <= 79) return "cargo";
  if (n === 31 || n === 32 || n === 52 || n === 56 || n === 57) return "tug";
  if (n === 33) return "dredger";
  if (n === 50) return "pilot";
  if (n >= 60 && n <= 69) return "passenger";
  return "other";
}
// Kept as a thin alias so the history log's tug tagging stays one definition.
export const isTugType = (t) => shipTypeCategory(t) === "tug";
// AIS ship-type code -> a readable label ("Tanker (80)"), reusing the same
// category table the map dots / legend use. null when no type is on file.
export function shipTypeLabel(t) {
  if (t == null) return null;
  const c = SHIP_CATEGORIES[shipTypeCategory(t)];
  return c ? `${c.label} (${t})` : `type ${t}`;
}

// AIS nav-status code -> label (ITU-R M.1371); only the codes we actually see.
export const NAV_STATUS = {
  0: "under way (engine)", 1: "at anchor", 2: "not under command",
  3: "restricted manoeuvrability", 4: "constrained by draught", 5: "moored",
  6: "aground", 7: "engaged in fishing", 8: "under way (sailing)", 15: "undefined",
};

export const fmtSta = (ft) => {
  if (ft === null || ft === undefined) return "—";
  const neg = ft < 0 ? "-" : "";
  const a = Math.abs(ft);
  const whole = Math.floor(a / 100);
  const rem = (a - whole * 100).toFixed(2).padStart(5, "0");
  return `${neg}${whole}+${rem}`;
};
// Escape DB-sourced free text (AIS names, cargo, notes) before innerHTML.
export const esc = (v) => (v == null ? "" : String(v).replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])));

// JSON write helper: returns {ok, status, data}; 204 (delete) has no body.
export async function apiWrite(method, url, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const r = await fetch(url, opts);
  let data = null;
  if (r.status !== 204) { try { data = await r.json(); } catch (e) { /* empty body */ } }
  return { ok: r.ok, status: r.status, data };
}
// Pull a typed payload out of a form: skip blanks, coerce the named numeric fields.
export function formPayload(form, numFields) {
  const nums = new Set(numFields || []);
  const out = {};
  for (const [k, v] of new FormData(form).entries()) {
    if (v === "" || v == null) continue;
    out[k] = nums.has(k) ? Number(v) : v;
  }
  return out;
}

// --- Central-Time date helpers ---------------------------------------------
// Everything is Central Time (the DB runs at America/Chicago — see app/tz.py).
// <input type="datetime-local"> is wall-clock with no zone, so fill it from the
// timestamp's CENTRAL components and send the entered value back WITHOUT a zone —
// the server stamps it Central. Round-trips in Central, never shifted by the
// browser's own zone. These live in the kernel because both timeline.js and
// forms.js import them.
export const CENTRAL_TZ = "America/Chicago";
export function centralParts(d) {
  // y/m/d/h/min rendered in Central (handles the CST/CDT switch), zero-padded.
  const f = new Intl.DateTimeFormat("en-CA", {
    timeZone: CENTRAL_TZ, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).formatToParts(d).reduce((o, p) => ((o[p.type] = p.value), o), {});
  return { y: f.year, m: f.month, d: f.day, hh: f.hour === "24" ? "00" : f.hour, mm: f.minute };
}
export function isoToLocalInput(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const p = centralParts(d);
  return `${p.y}-${p.m}-${p.d}T${p.hh}:${p.mm}`;
}
export function localInputToIso(v) {
  if (!v) return null;                  // blank -> caller omits the field
  return v.length === 16 ? v + ":00" : v;   // naive Central wall-clock; server stamps the zone
}
// Central-Time datetime, short form. 24-hour clock — every readout on the
// console matches the header clock (no AM/PM; tighter, sorts at a glance).
export function fmtCentral(s) {
  if (!s) return "—";
  const d = new Date(s);
  if (isNaN(d)) return esc(s);
  return d.toLocaleString(undefined, {
    timeZone: CENTRAL_TZ, month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit", hour12: false,
  });
}
