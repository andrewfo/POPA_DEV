# POPA Wharf Data Layer — Roadmap & Build Plan

This document outlines what's built, what's next, and the longer-term shape of
the system. It is the planning companion to [`CLAUDE.md`](./CLAUDE.md) (design
contract) and [`README.md`](./README.md) (setup/run). When the two disagree,
`CLAUDE.md` wins — this file is intent, not authority.

---

## 1. Where we are today

**Phase 1 (steps 1–4) is complete and pushed.** The repo is a conflict-safe
data layer that can be seeded from live AIS.

| # | Step | Status | Lands in |
|---|------|--------|----------|
| 1 | Schema + Alembic + exclusion constraint | ✅ | `alembic/versions/0001_initial_schema.py`, `app/models.py` |
| 2 | Stationing crosswalk (POPA ↔ Corps ↔ Dock No.) | ✅ | `app/crosswalk.py`, `tests/test_crosswalk.py` |
| 3 | Measured wharf centerline (placeholder geom) | ✅ | `app/seed/wharf_seed.py` |
| 4 | AIS ingestion (aisstream.io → DB) | ✅ | `app/ais/*` |
| 5 | Occupancy derivation | ⬜ | — |
| 6 | Conflict-detection service | ⬜ | — |
| 7 | Request intake + reconciliation; legacy backfill | ⬜ (later) | — |

### Known gaps carried forward (do these regardless of feature work)

- **Placeholder centerline geometry.** `app/seed/wharf_seed.py` uses fake
  lat/lon vertices. The `M` (POPA station) values are meaningful but the lat/lon
  must be digitized from the port aerial/GIS along the real quay face before
  `geo_to_station` returns correct stations. **This blocks step 5 from being
  trustworthy** — everything downstream inherits the error.
- **No wharf polygon / quay buffer.** We have a centerline only. Berthing
  detection needs an "alongside" test (apron polygon or N-metre buffer).
- **No automated reconnect/health proof for the AIS client.** `app/ais/run.py`
  is long-running but we have no soak test or metrics.
- **DB tests are opt-in** and auto-skip without a database; CI needs a real
  PostGIS service to exercise them.

---

## 2. Step 5 — Occupancy derivation (next up)

**Goal:** read `position_report`, decide when a vessel is *berthed*, and write
`observed` reservations with a correct `[stern_sta, bow_sta]` station range and
`[ETB, ETD]` time range. No human intake involved.

### 5.1 Prerequisites
- Replace placeholder centerline with digitized geometry (see §1).
- Add a **wharf polygon or quay buffer** to `wharf_segment` (or a new
  `wharf_area` table). Recommended: store an apron polygon; fall back to
  `ST_Buffer(centerline, N)` if no polygon is available. Decide `N` from the
  widest expected vessel beam + fender allowance.

### 5.2 Berthed-state detector
- A vessel is **berthed** when its positions sit inside the wharf buffer at
  **SOG ≈ 0** (e.g. `< 0.5 kn`) for a **sustained dwell** (e.g. ≥ 20–30 min).
- Use **hysteresis** to avoid flapping: enter "berthed" after the dwell
  threshold; exit only after a sustained departure (e.g. SOG > 1 kn or outside
  buffer for ≥ a few minutes). Two thresholds, not one.
- Also consider AIS `nav_status` (`5 = moored`, `1 = at anchor`) as a strong
  hint, but never as sole truth — many vessels don't set it correctly.
- Output of this stage: a **berthing event** `(vessel_id, t_start, t_end)` where
  `t_end` is open while the vessel is still alongside.

### 5.3 Bow/stern projection → station range
- AIS gives a single antenna position plus dimensions A/B/C/D (→ LOA/beam) and
  heading. Reconstruct bow and stern points:
  - bow = antenna position projected forward by `A` along heading;
  - stern = projected aft by `B` along heading.
- `geo_to_station` each endpoint → `[stern_sta, bow_sta]`. Normalize so
  `lower ≤ upper` for the `numrange`.
- If heading is missing, fall back to COG; if both missing, center an
  LOA-wide interval on the projected antenna station and flag low confidence.

### 5.4 Direction
- Compare heading/COG against the **channel axis** (derivable from the
  centerline bearing at that station) → set `upstream | downstream`.

### 5.5 Idempotent writes
- Re-deriving over the same window **must update**, not duplicate. Need a
  **stable key**: `(vessel_id, berthing_event_id)` or
  `(vessel_id, t_start_bucket)`. Add a nullable `derived_key` column +
  unique index on `reservation`, or a side table mapping berthing events →
  reservation ids.
- Status is always `observed`, source `ais`. These rows are allowed to overlap
  planned reservations by design (the exclusion constraint is `confirmed`-only).

### 5.6 Module shape (proposed)
```
app/occupancy/
  detect.py     # berthing-event detection from position_report (hysteresis)
  project.py    # bow/stern projection + geo_to_station -> station range
  derive.py     # orchestrates: events -> observed reservations (idempotent)
  run.py        # python -m app.occupancy.run  (batch or continuous)
```
- Run it as a **periodic batch** first (simplest, testable), then optionally a
  continuous worker. Keep detection pure/Postgres-driven so it's unit-testable
  against fixture `position_report` rows.

### 5.7 Tests (required)
- Detector: synthetic position tracks (arrive → dwell → depart) produce exactly
  one berthing event with correct `[ETB, ETD]`; flapping inputs don't.
- Projection: known heading + LOA → expected station range (pure math).
- Idempotency: running derive twice over the same data yields one reservation.

---

## 3. Step 6 — Conflict-detection service

**Goal:** surface conflicts as a query/service. One primitive: *time ranges
overlap AND station ranges overlap*. Covers vessel-vs-vessel, vessel-vs-dredge,
and observed-vs-planned alike.

### 3.1 Core query
- A conflict between reservations `a` and `b` is
  `a.time_range && b.time_range AND a.station_range && b.station_range`.
- Implement once as a SQL function or SQLAlchemy query in `app/conflicts.py`.
  Parameterize by status filter so callers choose what counts:
  - **observed-vs-planned** (the headline signal: a vessel sitting where
    something is planned) — `observed` × (`tentative|confirmed`);
  - **planned-vs-planned** — sanity check even though the DB blocks confirmed
    overlaps;
  - **dredge-vs-vessel** — same primitive, no special-casing.

### 3.2 Service / API surface
- `GET /conflicts?from=..&to=..&status=..` → list of conflict pairs with the
  overlapping time and station sub-intervals (compute the intersection so the UI
  can highlight the exact rectangle).
- Optionally `GET /reservations` with time/station window filters (foundation
  for the later Leaflet view).

### 3.3 Draft vs controlling depth
- `CLAUDE.md` requires: **draft must be validated against controlling depth for
  the station/time window before a reservation can be confirmed.**
- Needs a **controlling-depth source**: a `controlling_depth` table keyed by
  station range + effective time window (depths change with dredging and
  shoaling). Seed from the latest hydrographic survey / Corps condition survey.
- Add a check in the confirm path (and a `GET /conflicts` "depth" category):
  `vessel.draft > controlling_depth(station_range, time_range)` → block/flag.

### 3.4 Tests (required)
- Overlap matrix: pairs that touch only in time, only in station, in both,
  or in neither → correct classification.
- Half-open range edge cases (`[a,b)` adjacency must NOT count as overlap).
- Dredge-vs-vessel uses the identical path.
- Depth: under/over controlling depth at a station/time window.

---

## 4. Step 7 — Intake + reconciliation (later layer)

Deferred per `CLAUDE.md`. Outline only:

- **Three channels** (online form, dock-operator entry, phone) all land raw in
  `intake_event` *before* normalization — audit + reconciliation trail. The
  table already exists.
- **Normalize on ingest**: canonical units, enums, IMO/MMSI resolution; keep the
  raw payload. Produce a candidate `reservation` (status `requested` →
  `tentative` → `confirmed`).
- **Reconcile against observed AIS**: match a request to the `observed`
  reservation(s) for the same vessel/window; surface agreement and discrepancy
  (requested a berth a vessel isn't actually at, or vice versa).
- **Legacy spreadsheet backfill** (optional): parse → **human review** → commit.
  Source is messy — merged cells, free text (`"Chem Orchard - 607'"`), ambiguous
  `"X or Y"`, inline `CANCELLED`/`TBA`/`?`. Never assume clean auto-parse; the
  pipeline is parse-then-review, not parse-then-trust.

---

## 5. Cross-cutting / infrastructure backlog

Not tied to a single step — pick up as the system matures.

### 5.1 Real geometry (highest leverage)
- Digitize the wharf centerline and apron polygon from the port aerial/GIS.
- Verify the crosswalk affine params against the port's published stationing
  crosswalk at several known points (Corps offset 12,040.65; Dock No. 3365−POPA).
- Add a fixture comparing a few surveyed (lat/lon → station) points end-to-end.

### 5.2 CI / test infrastructure
- GitHub Actions: lint (ruff), type-check (mypy), `pytest` (pure) on every push;
  a job that spins up a **PostGIS service container**, runs `alembic upgrade
  head`, and runs the `db`-marked tests.
- Add a migration round-trip test (`upgrade` → `downgrade` → `upgrade`).

### 5.3 AIS ingestion hardening
- Reconnect with backoff + jitter; re-send the subscription within 3 s of every
  (re)connect (aisstream drops the socket otherwise).
- Metrics: messages/sec, last-message age, distinct MMSI, commit lag. Expose via
  a `/metrics` endpoint or structured logs.
- Dead-letter / raw-capture for unparseable frames (we already keep `raw`).
- A second `AISSource` for **Marine Cadastre** historical backfill — must yield
  the same normalized `AISPosition`/`AISStatic`; the `Ingestor` must not change.
- Position-report **retention/partitioning** policy: this table grows fast.
  Consider monthly partitions by `msg_ts` and a downsampling/archival job.

### 5.4 Observability & ops
- Structured logging config, request IDs, basic Prometheus-style counters.
- A `docker-compose` profile that runs API + ingestion + occupancy worker
  together for a realistic local stack.
- Healthcheck that reports last-AIS-message age (stale feed = silent failure).

### 5.5 Data quality
- Vessel identity merge: when IMO appears later for an MMSI-only row, link them;
  handle MMSI reuse / mismatched IMO.
- Sanity bounds on AIS values (lat/lon in bbox, SOG/heading ranges) before
  trusting them in derivation.

### 5.6 Schema evolution (always via Alembic)
- `controlling_depth` table (§3.3).
- Wharf apron polygon / `wharf_area` (§2.1).
- `reservation.derived_key` + unique index for idempotent observed rows (§2.5).
- Keep `app/models.py` enum tuples and the migration in lockstep. The exclusion
  constraint stays **`confirmed`-only** — do not extend it to block `observed`.

---

## 6. Out of scope (still)

- **Scheduling optimizer / auto-assignment** (OR-Tools) — much later.
- **Front end** — Leaflet over the existing GIS basemap comes after the conflict
  service exists; phase 1/2 build no UI.
- Anything that requires blocking `observed` overlaps. Re-read the Core model
  section of `CLAUDE.md` before reaching for that.

---

## 7. Suggested near-term sequence

1. **Digitize real centerline + apron polygon**, re-seed, add an end-to-end
   geo→station fixture. *(Unblocks everything.)*
2. **Step 5**: berthing detector → bow/stern projection → idempotent `observed`
   reservations, with tests.
3. **Step 6**: conflict query/service + API, with the overlap test matrix.
4. **Controlling-depth** table + draft validation in the confirm path.
5. **CI** with a PostGIS service container; AIS reconnect/metrics hardening.
6. *(Later)* intake + reconciliation; optional legacy backfill.
