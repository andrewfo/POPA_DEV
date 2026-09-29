# Berth-Request Intake

*Reference for `app/intake/manual.py`, `app/intake/llm.py`, `app/intake/dataverse_run.py`. POPA wharf data layer — roadmap → [`../PLAN.md`](../PLAN.md); design contract → [`../../CLAUDE.md`](../../CLAUDE.md).*

## Purpose

The intake pipeline captures berth requests from all channels and lands them as a two-layer record: a verbatim raw entry in `intake_event` (the audit + reconciliation trail) and a projected `status='requested'` reservation with an **empty station range** (berth unassigned until an operator places the vessel). The station range is left empty deliberately — an unassigned row never triggers the conflict primitive or the exclusion constraint, so the scheduling view stays clean until actual placement happens.

Three channels feed the same pipeline: manual phone/email/operator entry (`manual.py`), an optional AI-assisted pull from Power Pages / Dataverse (`llm.py` + `dataverse_run.py`). All three routes through `record_manual_request`, so the same deduplication, normalization, and audit logic applies regardless of channel.

## Files

| File | What it does |
| --- | --- |
| `app/intake/manual.py` | Core intake pipeline. `BerthRequestForm` (Pydantic model: all form fields in feet/lbs + dates + source), `normalize_form` (pure: form → `NormalizedRequest`, feet → metres, warnings), `record_manual_request` (DB: deduped raw landing + vessel upsert + reservation insert), `update_manual_request` (in-place edit: the one sanctioned raw mutation), `delete_manual_request` (soft-delete + reservation hard-delete). Also defines `EDITABLE_SOURCES`, `MANUAL_SOURCES`, `ACCEPTED_SOURCES`, `dedupe_key`, `valid_imo`, and the AIS-override helpers. |
| `app/intake/llm.py` | LLM-assisted normalizer. `LlmExtraction` (Pydantic model for the model's JSON output), `SYSTEM_PROMPT` (static, cacheable), `build_messages` (few-shot chat assembly), `extraction_from_json` (tolerant JSON parse — never raises), `to_form` (maps extraction → `BerthRequestForm`), `parse_request` (end-to-end: `complete(messages)` → `ParseResult`), `openrouter_complete` (live `httpx` factory — no SDK dep). The `complete` callable is injectable so the whole pipeline is unit-testable without a network. |
| `app/intake/dataverse_run.py` | Outbound poll worker. `DataverseClient` (client-credentials token + `fetch_new` + `mark_triaged`), `process_batch` (fetch → parse → record → mark, one transaction per row), `main` (CLI with `--once` / `--dry-run` / `--input`). Authenticates with Azure AD client-credentials; makes only outbound calls to `login.microsoftonline.com` and `*.crm.dynamics.com`. Calls `beat("intake-dataverse", ...)` for the worker-heartbeat telemetry. |

## Key concepts & invariants

**Every channel lands raw before normalization.** `record_manual_request` inserts into `intake_event` as step 1 before touching `vessel` or `reservation`. A channel that skips this landing violates the audit contract.

**Deduplication** (`dedupe_key`): a SHA-256 content hash of the normalized form fields (excluding `source_raw`, which carries volatile Dataverse system columns that drift between polls). The `intake_event` table has a partial unique index on `dedupe_key WHERE dedupe_key IS NOT NULL AND deleted_at IS NULL`. An identical re-submission hits `ON CONFLICT DO NOTHING` and returns `{"duplicate": true}` without creating a second reservation. A soft-deleted row's dedupe key is excluded from the index, so re-submitting previously-deleted content lands a fresh row.

**Empty station range on intake.** `_insert_reservation` always writes `'empty'::numrange`. This is intentional: a phone/email request has no berth yet. An empty range never matches the `&&` operator in conflict detection or the exclusion constraint. **Do not "fix" this with a placeholder span.**

**`source='ai'` is its own provenance** (migration 0009), not the `email` channel it previously borrowed. `EDITABLE_SOURCES = ("phone", "email", "operator", "ai")` — editability is a **separate** axis from provenance. The AI channel is editable; the legacy `form` source (retired Adobe Sign feed) is not. The constants are:

| Constant | Values | Meaning |
| --- | --- | --- |
| `MANUAL_SOURCES` | `phone, email, operator` | Human entry channels |
| `ACCEPTED_SOURCES` | `phone, email, operator, ai` | Valid new-request sources |
| `EDITABLE_SOURCES` | `phone, email, operator, ai` | Requests an operator may edit/delete |

**IMO uniqueness** (`_resolve_imo_vessel`): one IMO = one ship. If an incoming IMO is already on file under a **different** ship name, `record_manual_request` raises `ValueError` → 422 **before** landing anything (no orphan audit row). If the IMO resolves to the **same** ship: a manual-only vessel (no MMSI) is overwritten (`overwrite=True`) so a corrected LOA/dims takes effect; an AIS-tracked vessel (has MMSI) is only NULL-filled (`overwrite=False`) — its dimensions stay authoritative.

**AIS-tracked vessel dimension authority.** For a vessel with an MMSI, entered `loa`/`beam`/`draft` that differ from the stored AIS values are **dropped** — the vessel row is NULL-filled, never overwritten, on both create and edit paths. The discrepancy is surfaced:
- Per-dimension `_override_warnings` shown at submit time.
- A durable `[AIS override] entered X→Y ft; AIS values kept (authoritative).` note stamped onto `reservation.notes` (via `_override_note`), so the override is visible on the request card and in History.
- The API response carries `ais_overrides: [{field, entered_m, entered_ft, ais_ft}]` (via `_override_payload`) so the UI can offer a **"Manual override"** button that PATCHes `/vessels/{id}` with `{dims_locked: true, <field>: entered_m}`.

**The one sanctioned raw mutation** (`update_manual_request`): editing an `intake_event` overwrites the `raw` column and recomputes `dedupe_key` in place. This is the only place `intake_event.raw` is mutated after landing. It is restricted to `EDITABLE_SOURCES`. The reservation's `berth_id`, `station_range`, `direction`, and `status` are preserved — an edit governs vessel/time/cargo only. The pre-edit raw is returned as `prior_raw` for the caller to log in `audit_log`.

**Soft-delete** (`delete_manual_request`): `deleted_at` is stamped; the row and its verbatim payload are kept for the audit trail. The projected reservation is hard-dropped (a withdrawn request is no longer scheduled). The `ON DELETE SET NULL` FK on `intake_event.reservation_id` clears the link. The deleted row no longer appears in `GET /intake/berth-requests` (filtered `deleted_at IS NULL`) and no longer blocks re-submission (the partial unique index excludes it).

**LOA re-projection** (`_reproject_placements`): whenever an authoritative edit reaches a vessel's LOA (a manual-only ship on create or edit), any already-placed planned reservation for that vessel has its `station_range` re-derived holding the bow fixed. This keeps the map footprint in sync with the corrected length. Berth-assigned rows, unplaced rows, un-oriented rows, and `observed` rows are untouched. A longer footprint that now collides with a `confirmed` row surfaces as a 409 at commit.

**Content-empty guard**: a submission with no vessel name, no IMO, and no ETB is refused outright (`record_manual_request` returns `{"skipped": true}`) — a stray POST can't create a blank request card.

**LLM path** (`llm.py`):
- The model receives a static `SYSTEM_PROMPT` (cacheable across calls) and a single worked example (few-shot), then the raw submission.
- `LlmExtraction` carries `extra="ignore"` so extra keys never break validation. A `@model_validator` strips null-valued keys upfront so non-Optional fields (e.g. `bunkers`, `confidence`) fall back to their defaults rather than raising.
- Tolerant by design: a bad IMO is dropped with a note (not an exception), an unparseable date becomes `None`, ambiguous "X or Y" / "TBA" becomes `None`. The `complete` callable is injected so tests use a fake.
- `_scrub_unit_claims`: removes any semicolon-delimited parse-note fragment that claims a metre↔feet unit conversion (a persistent LLM narration tic; the form is always in feet, so such claims are always wrong).
- `temperature=0` and `max_tokens=1024` for stable, bounded extraction.

**Dataverse worker** (`dataverse_run.py`):
- Outbound only: polls `*.crm.dynamics.com` (read new rows) then marks each row "triaged". No inbound webhook, no exposure of the 127.0.0.1-bound API.
- One transaction per row: a bad row rolls back and logs without killing the batch.
- Rows recorded but whose `mark_triaged` call failed are remembered in `recorded_ids` across polls — re-marked next cycle without re-billing the LLM.
- `--dry-run`: parse + print, no writes. `--input FILE`: parse a local JSON file, no Dataverse or DB needed.

## Connections

- **Upstream**: `app/tz.py` (`assume_central`) is the sole time-zone handler for form dates. `app/models.py` (`IntakeEvent`, `Vessel`) are the ORM models written. `app/edit.py` (`VesselUpdate`, `_apply_ais_overrides` pattern) is mirrored — the same AIS-authority rules apply on both paths.
- **Downstream**: `app/routers/intake.py` is the thin HTTP surface (`POST /intake/berth-request`, `PATCH`/`DELETE /intake/berth-requests/{id}`, `GET /intake/berth-requests`). The projected `requested` reservation immediately participates in `GET /conflicts` and `GET /verification` (via the scheduling modules).
- **IMO auto-fill (pre-submit)**: `app/vessel_lookup.py` backs `GET /vessels/lookup`, which the create form calls to pre-fill name + dims from an IMO *before* a request is recorded. It only **reads** (Tier 1 = on-file `vessel`; Tier 2 = external provider, stubbed) — it does not land an `intake_event`; the normal `record_manual_request` path still runs on submit. Same source-agnostic contract as `AISSource`. See `docs/reference/routers.md` + `docs/reference/frontend.md`.
- **Sibling reference docs**: `docs/reference/scheduling.md` (conflict detection and verification that operates on the rows created here), `docs/reference/routers.md` (HTTP wiring).
