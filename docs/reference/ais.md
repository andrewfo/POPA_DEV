# AIS Ingestion

*Reference for `app/ais/`. POPA wharf data layer — roadmap → [`../PLAN.md`](../PLAN.md); design contract → [`../../CLAUDE.md`](../../CLAUDE.md).*

## Purpose

Build step 4 seeds the data layer from live vessel positions rather than manual entry. The `app/ais/` package connects to the aisstream.io websocket, normalizes raw wire frames into two plain dataclasses (`AISPosition` / `AISStatic`), and persists them into `position_report` and `vessel` without any knowledge of which source produced them. Occupancy derivation (step 5) runs as a separate pass over the landed rows.

The package is deliberately split so adding a second provider — e.g. USCG/NOAA Marine Cadastre historical CSVs — requires only a new `AISSource` implementation. The `Ingestor` and everything downstream must never change.

## Files

| File | What it does |
| --- | --- |
| `app/ais/messages.py` | Defines `AISPosition` and `AISStatic` dataclasses (the only objects the rest of the pipeline ever sees), plus `parse_aisstream()` which maps one aisstream.io envelope to one of them or `None`. Handles AIS sentinel values (`SOG=102.3`, `heading=511`, `COG=360`) and `@`-padded string fields. Derives `loa = dim_a + dim_b` and `beam = dim_c + dim_d` from the raw A/B/C/D dimension offsets; keeps `dim_a` / `dim_b` individually for bow/stern projection. |
| `app/ais/source.py` | `AISSource` abstract base class (one `stream()` async-iterator method). `AisStreamSource` is the live implementation: opens a websocket to `wss://stream.aisstream.io/v0/stream`, sends the subscription JSON immediately on connect (must be within 3 s or the server drops the connection), then yields parsed messages. Filters to `PositionReport` and `ShipStaticData` message types. |
| `app/ais/ingest.py` | `Ingestor` — source-agnostic. `handle()` dispatches on message type: `AISStatic` → `_upsert_vessel_static` (PostgreSQL `INSERT … ON CONFLICT DO UPDATE` with `COALESCE(new, existing)` so known values survive a null feed); `AISPosition` → `_ensure_vessel` (stub row) + `_insert_position`. Batches commits every `commit_every` rows or `commit_interval_s` seconds. Fires an optional `on_commit` callback after each real commit (used for heartbeats). Respects `vessel.dims_locked` (migration 0015): dimension columns (`loa`, `beam`, `draft`, `dim_a`, `dim_b`) are frozen when an operator has pinned a corrected value — the upsert keeps the stored value instead of overwriting from the feed. |
| `app/ais/run.py` | Runnable entry point (`python -m app.ais.run`). Wires `AisStreamSource` → `Ingestor`, reconnects with exponential backoff (1 s → 60 s cap) on errors, and beats `app/workers.beat("ais", …)` on each real commit so `GET /workers` reports liveness. |

## Key concepts & invariants

- **Source-agnostic by design.** `Ingestor.run(source)` takes any `AISSource`. A new provider must yield the same `AISPosition`/`AISStatic` objects; the ingestor and everything downstream stay unchanged.
- **AIS is authoritative for MMSI-keyed vessel dimensions — unless `dims_locked`.** `_upsert_vessel_static` uses `COALESCE(new, existing)` so a later null never wipes a stored value, and additionally gates `loa`/`beam`/`draft`/`dim_a`/`dim_b` on `dims_locked`: when set, the stored (operator-corrected) value is kept even when the feed carries a different one. Non-dimension fields (`name`, `callsign`, `destination`, etc.) always merge from AIS regardless of the lock.
- **`loa = dim_a + dim_b` is derived at parse time, never stored separately.** Both the sum and the individual offsets are preserved on `vessel` so the occupancy projector can place the antenna correctly within the hull.
- **`msg_ts` is real UTC.** The aisstream `MetaData.time_utc` field is a genuine instant; it is stored as-is, never stamped Central. (Wall-clock operator inputs are Central — see `app/tz.py` — but AIS timestamps are not operator inputs.)
- **Occupancy derivation is a separate pass.** The ingestor only lands raw data. Nothing in `app/ais/` derives `reservation` rows; that is `app/occupancy/`.
- **Heartbeat cadence follows real commits, not wall time.** A snug bounding box sees only occasional messages, so a count-only flush threshold could leave rows uncommitted for minutes. The `commit_interval_s` flush ensures rows reach the DB and the heartbeat fires even on a quiet feed.

## Connections

- **Upstream:** aisstream.io websocket (live) or any future `AISSource` implementation (e.g. Marine Cadastre CSV — see `/skills/add-ais-source`).
- **Downstream (data):** `position_report` table (all positions), `vessel` table (upserted from `ShipStaticData`). The occupancy derivation step (`app/occupancy/`) reads these — see [`occupancy.md`](occupancy.md).
- **Downstream (ops):** `app/workers.beat("ais", …)` → `worker_heartbeat` table → `GET /workers`.
- **Config:** `Settings.aisstream_api_key`, `Settings.ais_bounding_box`, `Settings.aisstream_url` from `app/config.py`.
- **`dims_locked` escape hatch:** defined in migration 0015, respected here; set via `PATCH /vessels/{id}` (`app/edit.py`) or the "Manual override" button in the intake form.
