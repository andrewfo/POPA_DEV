---
name: code-audit
description: Audit the POPA wharf data layer against its project-specific invariants — the design rules in CLAUDE.md that a generic code review misses. Use when asked to audit, review for convention/invariant violations, or sanity-check a change against the core model before merge. Complements (does not replace) the built-in /code-review (bugs) and /security-review.
---

# Project invariant audit

A generic reviewer catches bugs; this audit catches **violations of the design
contract** in CLAUDE.md — the ones that quietly corrupt the data model. Run it
over the working diff (or a path the user names). For each finding, cite the
`file:line` and the invariant it breaks, and propose the fix.

Scope the audit to changed files unless asked for a full sweep:
```powershell
git diff --name-only main...HEAD
```

## The invariants (check each)

### 1. All stationing math lives in `app/crosswalk.py`
No affine transforms (`* scale + offset`, POPA↔Corps↔Dock conversions, station
arithmetic) anywhere else. New systems = new `AffineParams` + wrappers in
`crosswalk.py`, never inline.
```
grep for: 12040.65, 3365, "* scale", from_popa, to_popa, popa_station   outside crosswalk.py
```
Also flag: code that assumes the **default** params instead of reading a
segment's own `*_scale`/`*_offset`.

### 2. Migration ⇄ `models.py` are in sync
Every schema change is a paired edit. Enum value tuples in `models.py`
(`RESERVATION_STATUSES`, etc.) must match the migration's `postgresql.ENUM(...)`.
Columns/constraints/indexes must agree. Flag a `models.py` change with no
corresponding `alembic/versions/*` revision (and vice versa), and any migration
whose `downgrade()` doesn't reverse `upgrade()`.

### 3. The exclusion constraint stays `confirmed`-only
`no_wharf_overlap` must remain `WHERE (status = 'confirmed')`. **Flag any change
that widens it to block `observed` rows** — observed AIS overlap is the signal
to surface, not block. This is a hard line (re-read CLAUDE.md "Core model").

### 4. Conflict detection is the one primitive
A conflict = time ranges overlap **AND** station ranges overlap, for
vessel-vs-vessel and vessel-vs-dredge alike. Flag any bespoke per-type collision
logic instead of the single time×station test.

### 5. Ingestion stays source-agnostic
`app/ais/ingest.py` (`Ingestor`) must not gain provider-specific branches. New
providers adapt at the parser/source boundary and emit normalized
`AISPosition`/`AISStatic`. Flag provider names or wire-format handling leaking
into the ingestor or schema.

### 6. Identity & normalization on ingest
- Vessel key is **MMSI** (IMO when present); **name is never a key** (non-unique,
  misspelled). Flag joins/lookups/upserts keyed on name.
- Static-data upserts must not null known fields — preserve the
  `COALESCE(new, existing)` merge.
- Raw input is preserved: `position_report.raw` / `intake_event.raw` keep the
  untouched record. Flag parsers that discard the source payload.
- Units normalized to canonical (knots, degrees, metres, tz-aware UTC, feet for
  station).

### 7. No hand-edited schema
DDL only via Alembic. Flag raw `CREATE/ALTER/DROP TABLE` or ad-hoc DB mutation
in app code or scripts outside `alembic/versions/`.

### 8. Tests exist for the risky surfaces
CLAUDE.md requires tests for the **crosswalk** and **conflict-detection** logic.
Flag changes to `crosswalk.py` (or new conflict logic) with no matching test
update in `tests/`. Pure tests must stay runnable without a DB; DB-dependent
tests carry the `db` marker.

### 9. Phase-1 scope guard
Out of scope right now: scheduling optimizer / auto-assignment, front-end UI, and
berth-request intake (form/operator/phone). Flag new code that starts building
these — note it as scope creep, not a bug. (The `intake_event` *table* existing
is fine; an intake *pipeline* is not.)

## Output format

Group findings by severity:
- **Contract violation** — breaks an invariant above (must fix).
- **Drift risk** — technically works but invites future violation (e.g. params
  read inconsistently).
- **Scope creep** — building deferred phases.

For each: `file:line`, the rule, why it matters, and the minimal fix. If the
diff is clean against all nine, say so explicitly rather than inventing nits.
Then suggest running `/code-review` for correctness bugs and `/security-review`
if input handling or the AIS websocket changed.
