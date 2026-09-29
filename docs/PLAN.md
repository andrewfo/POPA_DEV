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

1. Bring PostGIS up and run the db-marked tests to exercise the apron seed +
   `ST_Contains` predicate (closes a carried-forward gap with zero new code).
2. **AIS ingestion hardening** — reconnect/backoff, metrics, and the
   last-message-age healthcheck (the biggest reliability gap for a 24/7 feed).
3. Migration round-trip test; drop the mypy `continue-on-error` once clean.
4. Data-quality guards — vessel identity merge + AIS sanity bounds.
5. *(Optional)* wire the legacy-spreadsheet backfill review → commit pipeline.
