---
name: add-ais-source
description: Add a new AIS data provider (e.g. USCG/NOAA Marine Cadastre historical CSV) to the ingestion pipeline. Use when wiring up a second/alternate source of vessel positions or static data. The new source must yield the same normalized AISPosition/AISStatic objects so the Ingestor never changes — source-agnostic by design.
---

# Adding a new AIS source

**Rule from CLAUDE.md: new AIS providers = a new `AISSource` that yields the
same normalized `AISPosition` / `AISStatic`; the ingestor must not change.**
Keep ingestion source-agnostic — `app/ais/ingest.py` already upserts `vessel` by
MMSI and lands raw `position_report` rows for *any* source.

## The contract

`app/ais/source.py` defines:

```python
class AISSource(abc.ABC):
    @abc.abstractmethod
    def stream(self) -> AsyncIterator[AISMessage]: ...   # AISMessage = AISPosition | AISStatic
```

`app/ais/messages.py` defines the normalized `AISPosition` and `AISStatic`
dataclasses (lat/lon, SOG, COG, heading, nav_status, msg_ts, raw; and name/IMO/
callsign/ship_type/LOA/beam/draft/destination respectively). Your job is to
map the provider's wire format into these — nothing downstream changes.

## Steps

1. **Add a parser** in `app/ais/messages.py` (alongside `parse_aisstream`) that
   converts one provider record → `AISPosition` / `AISStatic` (or `None` to
   skip). Normalize units to the canonical ones (knots, degrees, metres,
   tz-aware UTC `msg_ts`) and **stash the untouched provider record in `raw`** —
   `position_report.raw` is the audit trail.
2. **Add the source class** in `app/ais/source.py` subclassing `AISSource` and
   implementing `async def stream(self)` as an async iterator. Map MMSI/IMO
   carefully — MMSI is the upsert key; the ingestor merges static detail with
   `COALESCE(new, existing)` so never emit a record that nulls known fields.
3. **Wire a runnable** if it's a batch/file source (mirror `app/ais/run.py`).
   For historical CSV (Marine Cadastre), read rows → parse → `Ingestor.handle`,
   relying on `commit_every` batching.
4. **Tests** (`tests/test_ais_messages.py` is pure, no DB): feed representative
   provider records through your parser and assert the normalized output. These
   run under plain `pytest`.

## Don't

- Don't touch `Ingestor` or the `position_report` / `vessel` schema to fit a new
  provider — adapt at the parser/source boundary.
- Don't drop the original record; keep it in `raw`.
- Don't use vessel **name** as a key (non-unique, misspelled) — key on MMSI,
  carry IMO when present.

## Reference: the live source

`AisStreamSource` (aisstream.io websocket) shows the websocket pattern: send the
`BoundingBox` subscription **within 3s** of connecting, skip error/non-JSON
frames, `yield` parsed messages. A historical source has no such timing
constraint but produces identical normalized output.
