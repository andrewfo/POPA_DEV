# POPA Wharf Data Layer — Roadmap

**What's left to build and the open issues**, not a description of what exists.
For "how the code works today" see the per-section reference docs in
[`reference/`](./reference/); for the design rules see
[`../CLAUDE.md`](../CLAUDE.md) (authority — when this file disagrees, it wins).

## Where we are

Build steps 1–6 are complete (schema + exclusion constraint, crosswalk, real
measured centerline, AIS ingestion, occupancy derivation, conflict service, and
the draft-vs-controlling-depth gate). Step 7 is mostly done: intake **capture**
(manual + AI-assisted Dataverse pull), the manual **edit** surface, and the AIS
**verification** layer + auto status-mutation all ship. The system is deployable
(prod Docker image + compose behind HTTP Basic). Pulled forward ahead of the
original order, at the user's request: the read-only + write Leaflet UI, intake
capture, the edit surface, and the read-only feasibility oracle.

| # | Step | Status | Reference |
|---|------|--------|-----------|
| 1 | Schema + Alembic + exclusion constraint | ✅ | [migrations.md](./reference/migrations.md) |
| 2 | Stationing crosswalk | ✅ | [crosswalk.md](./reference/crosswalk.md) |
| 3 | Real measured wharf centerline | ✅ | [crosswalk.md](./reference/crosswalk.md) |
| 4 | AIS ingestion | ✅ | [ais.md](./reference/ais.md) |
| 5 | Occupancy derivation | ✅ | [occupancy.md](./reference/occupancy.md) |
| 6 | Conflict service + draft/depth gate | ✅ | [scheduling.md](./reference/scheduling.md), [depth.md](./reference/depth.md) |
| 7 | Intake + AIS verification + legacy backfill | 🟡 | [intake.md](./reference/intake.md), [scheduling.md](./reference/scheduling.md) — **only the legacy-spreadsheet backfill *commit* remains** |

---

## Active feature batch (director demo)

A prioritized set of operator-facing improvements ahead of the director demo. All
build on surfaces that already exist; none change the core model or the invariants
in [`../CLAUDE.md`](../CLAUDE.md). Ordered by value/effort.

| # | Item | Effort | Value | Touches |
|---|------|--------|-------|---------|
| A | IMO auto-fill on intake | ✅ done | High | [routers.md](./reference/routers.md), [frontend.md](./reference/frontend.md) |
| B | Test & harden the reservation form | ~1 h | High | [tests.md](./reference/tests.md), [scheduling.md](./reference/scheduling.md) |
| C | UI simplification pass | ~3 h | High | [frontend.md](./reference/frontend.md) |
| D | Manual vs. agent write precedence (provenance badge) | ✅ done | High | [intake.md](./reference/intake.md), [migrations.md](./reference/migrations.md), [frontend.md](./reference/frontend.md) |
| E | Projected / preview vessel view (no reservation) | ~2 h | Med–High | [frontend.md](./reference/frontend.md), [scheduling.md](./reference/scheduling.md) |
| F | Dredge GPS information layer | ~4 h | High | [frontend.md](./reference/frontend.md), [occupancy.md](./reference/occupancy.md) |
| G | Dredging XYZ survey analytics | ~6–10 h | Highest | [depth.md](./reference/depth.md) |

### A — IMO auto-fill on intake ✅
**Shipped (Tier 1).** The create form's IMO field now looks the ship up **before
submit** via `GET /vessels/lookup?imo=` (`app/routers/read_only.py` →
`app/vessel_lookup.py`) and pre-fills name + LOA/beam/draft from the on-file
`vessel` row (AIS-populated). AIS-authoritative dims fill + flag as editable
(tinted, no longer read-only); non-AIS dims fill only when blank. Correcting a
wrong autofill is now a one-confirm motion: overtyping an AIS dim arms a
**confirm-to-pin** on save (`offerOverrideConfirm` → `PATCH /vessels/{id}` with
`dims_locked: true`) across the intake create/edit forms and the vessel editor —
the server authority rule is unchanged (an un-pinned typed value is still
dropped), only the escape hatch got easier. Covered by
`tests/test_vessels_lookup.py`.

**Tier-2 seam left wired, not implemented.** `vessel_lookup.lookup()` calls
`lookup_onfile` then falls through to `lookup_external` (a stub returning `None`).
Adding an external provider (MarineTraffic / VesselFinder / Datalastic / Equasis)
for a not-yet-on-AIS ship touches only `lookup_external` + a `VESSEL_REF_*` setting
in `app/config.py` — the endpoint and form don't change (same source-agnostic
contract as `AISSource`). Flag/callsign auto-fill also belongs to Tier 2 (`vessel`
has no `flag` column and the form no callsign field).

### B — Test & harden the reservation form
This is the form directors will see demoed — it must be reliable end to end. The
create/edit/promote-to-confirmed/unconfirm flows and AIS-override handling exist;
add coverage and fix the edges surfaced. Cover create, edit, promote-to-confirmed,
and the **409 conflict** path (a `confirmed`-overlap `IntegrityError` surfacing as
409 — never pre-empted by blocking `observed`/`tentative`). Test through
`TestClient`, not by committing rows.

### C — UI simplification pass 🟡 in progress
Tighten the operator screens so the primary actions are obvious and noise is
hidden. Tabs, filters, and map-layer toggles are in place. Reduce clutter, group
the common actions, and collapse advanced fields behind disclosure. Keep the UI
**thin and over the API** — presentation only, no stationing/precedence logic
moving client-side.

**Landed so far** (all presentation-only, over read-only additive API fields):
- Berth-request tab redesigned into grouped cards + a segmented channel filter.
- Reservation cards restyled to **mirror** the berth-request card layout
  (structured header, boxed schedule strip, dim grid, kebab overflow, collapsible
  ALL INFO) — reuses `dimStrip`/`groupedInfo`.
- **AIS verification folded into "Alongside now"**: each moored ship carries a
  plan-vs-observed badge + craft status bar, with a "Future planned" sub-section;
  the standalone verification section is gone and the harbor-craft toggle is now a
  single global Overview filter. The panel reads the read-only `GET /verification`
  (archiving stays in the occupancy worker).
- Saved-ships roster trimmed to name/dims, searchable by name or IMO.

### D — Manual vs. agent write precedence ✅
**Shipped.** The one rule: **the operator is authoritative; the agent proposes.**
Three mechanisms (no fourth needed): an operator *edit* overwrites in place
(`update_manual_request`); the agent **only appends**, never mutating an existing
row; two independent creates for the **same IMO over an overlapping window** are
**flagged as possible duplicates, never auto-merged** (`_find_duplicate` + the
`GET /intake/berth-requests` LATERAL, derived live so it clears on withdraw/settle).
Every record now carries `source` (provenance, unchanged) **plus `updated_at` /
`last_actor`** (migration 0017 — when + who last wrote it; NULL actor = agent/AIS).
The UI renders the existing channel badge, a "⚠ duplicate?" badge, and an
"edited by … · …" line (`resCard`/`reqCard`, `app/static/js/forms.js`). Covered by
`tests/test_intake_manual.py` (last-writer stamping, duplicate flag on/off/clear,
endpoint surfacing). Editability stays the separate `EDITABLE_SOURCES` axis — not
conflated with provenance.

### E — Projected / preview vessel view (no reservation)
Let an operator sketch a vessel onto the map for planning — without all required
fields and **without creating a reservation row** (a what-if view). The map
already has a "Planned" outline mode for confirmed bookings; add a lightweight,
**non-persisted** projected footprint (likely its own tab). Reuse the read-only
feasibility oracle (`app/feasibility.py`) for the footprint math — it **proposes,
never places**, which is exactly this. Nothing lands in `reservation`.

### F — Dredge GPS information layer
Surface dredging activity by GPS location on the map so directors can see where
work is happening against the berths. A `dredge` reservation type and dredge/feet
marker layers already exist; extend them to carry and display **GPS positions for
active dredging**. Positions reconcile to station through the one crosswalk, same
as vessels; a dredge op is still a `[station]×[time]` rectangle (the conflict
primitive is unchanged).

### G — Dredging XYZ survey analytics
Turn the hydrographic `.XYZ` surveys into **trend analytics** — shoaling vs.
dredging over time, controlling-depth change per station, before/after
comparisons. The highest long-term value item. Versioned depth surveys are already
ingested and reduced to per-station controlling depth in PostGIS
([depth.md](./reference/depth.md)); the data to compare across dates exists. Build
the **diffing, charts, and reporting** on top. Read the latest `active` for the
gate as today — analytics diff *across* survey versions, they don't change which
survey the draft gate reads.

---

## Open work

### Step 7 — remaining
- **Legacy-spreadsheet backfill commit.** The conservative parser exists and is
  tested; the parse → **human review** → commit pipeline is not wired. Source is
  messy (`"Chem Orchard - 607'"`, ambiguous `"X or Y"`, inline `CANCELLED`/`TBA`/
  `?`) — parse-then-review, never parse-then-trust.
- **Promote raw-only intake fields to columns** *if/when queried* (flag, DWT,
  agency, cargo weights, bunkering detail). Today they live only in
  `intake_event.raw` + a `reservation.notes` summary — fine until something needs
  to filter on them.

### Known gaps carried forward
- **Apron seed + `ST_Contains` predicate unexercised.** The digitized apron
  polygon and its "alongside" predicate are built (migration 0005) but the DB
  path only runs against live PostGIS. Bring PostGIS up → `alembic upgrade head`
  → `app.seed.wharf_seed` → run the db-marked tests to exercise it. Backward
  compatible today (NULL apron → centerline-buffer fallback).
- **Coarse centerline.** The measured line is real but a 7-vertex chain (one
  chord per berth — the max fidelity the rectangular berth polygons allow). A
  finer quay survey would densify it via the same `build_centerline.py`.
- **AIS client has no reconnect/health proof.** `app/ais/run.py` is
  long-running but there's no soak test or metrics (see backlog below).
- **Auth is all-or-nothing HTTP Basic** — one shared operator credential, no
  per-user accounts/roles. Fine for a small team; the audit_log records *that*
  the operator acted. Revisit OIDC/SSO against the `portpa.com` tenant only if
  real per-user identity is needed.

---

## Cross-cutting backlog

Not tied to one step; pick up as the system matures.

### Geometry / crosswalk
- Verify the crosswalk affine params against the port's **published stationing
  crosswalk** at several known points (Corps offset 12,040.65; Dock No. 3365−POPA).
- Add an end-to-end fixture comparing a few surveyed lat/lon → station points.

### AIS ingestion hardening (highest-leverage backlog item)
- Reconnect with backoff + jitter; re-send the subscription within 3 s of every
  (re)connect (aisstream drops the socket otherwise).
- Metrics: messages/sec, last-message age, distinct MMSI, commit lag — via a
  `/metrics` endpoint or structured logs.
- A **last-AIS-message-age healthcheck** (a stale feed is currently a silent
  failure; the worker heartbeat proves the process is alive, not that data flows).
- A second `AISSource` for **Marine Cadastre** historical backfill — must yield
  the same normalized `AISPosition`/`AISStatic`; the `Ingestor` must not change.
- `position_report` **retention/partitioning** (monthly partitions by `msg_ts` +
  a downsampling/archival job) — this table grows fast.

### Observability & ops
- Structured logging config, request IDs, Prometheus-style counters.
- Docker `secrets:` instead of the env file; backups of the DB volume.

### Data quality
- Vessel identity merge: link an MMSI-only row when its IMO later appears; handle
  MMSI reuse / mismatched IMO.
- Sanity bounds on AIS values (lat/lon in bbox, SOG/heading ranges) before
  trusting them in derivation.

### CI / tests
- Add a migration round-trip test (`upgrade` → `downgrade` → `upgrade`).
- Drop `continue-on-error` on the mypy job once the tree type-checks clean.

### Schema evolution (always via Alembic)
- Keep `app/models.py` enum tuples and the migrations in lockstep.
- The `no_wharf_overlap` exclusion constraint stays **`confirmed`-only** — do not
  extend it to block `observed`. Change the 75 ft mooring gap only via a new
  migration (never config).

---

## Out of scope (still)

- **Scheduling optimizer / auto-assignment (OR-Tools).** This — not AIS — is what
  would ever *place* ships automatically (from requests + berth availability). Its
  first, non-optimizing step already exists as the read-only **feasibility oracle**
  ([scheduling.md](./reference/scheduling.md)); the optimizer proper (objective
  over many vessels, rolling horizon, churn minimization) stays out, and even it
  would only *propose* for operator confirmation.
- **Anything that blocks `observed` overlaps.** Re-read the Core model section of
  `CLAUDE.md` first — observed overlap is the signal, not an error.

---

## Near-term sequence

> Ahead of the director demo, the **Active feature batch (A–G)** above takes
> precedence — the sequence below is the underlying-reliability track to resume
> after (or interleave with) it.

1. Bring PostGIS up and run the db-marked tests to exercise the apron seed +
   `ST_Contains` predicate (closes a carried-forward gap with zero new code).
2. **AIS ingestion hardening** — reconnect/backoff, metrics, and the
   last-message-age healthcheck (the biggest reliability gap for a 24/7 feed).
3. Migration round-trip test; drop the mypy `continue-on-error` once clean.
4. Data-quality guards — vessel identity merge + AIS sanity bounds.
5. *(Optional)* wire the legacy-spreadsheet backfill review → commit pipeline.
