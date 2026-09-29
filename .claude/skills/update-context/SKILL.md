---
name: update-context
description: Reconcile everything that persists across sessions with what actually changed — PLAN.md (roadmap/plans), the per-section reference docs (docs/reference/*.md), CLAUDE.md (design contract: build order + current-state line), the auto-memory (MEMORY.md index + memory files), and README.md. Use at the end of a chunk of work, when a build step lands, or when the user says "update context / the plan / the docs / your memory". It edits the durable record; it does not change app code.
---

# Update the cross-session record

Code changes are durable on their own. The *context* about that code is not — it
lives in a handful of files that the next session (and the next engineer) reads
to orient. This skill walks those files and reconciles each against what the
working tree now says, so the design contract, the roadmap, and your memory stop
drifting from reality.

**This skill edits documentation/memory only.** Do not touch `app/`, `alembic/`,
`tests/`, or any code. If reconciling reveals a *code* problem (e.g. the plan
says step 5 is done but the module is missing), surface it — don't silently
"fix" the docs to match broken code.

## 0. See what actually changed

Ground every edit in the diff, not in memory of the conversation:

```powershell
git status
git diff --stat main...HEAD          # what landed on this branch
git diff --stat                      # uncommitted working-tree changes
git log --oneline -15
```

Read the actual changes for anything you're about to describe. The artifacts
below should reflect the *tree*, not your impression of it.

## The persistent artifacts (reconcile each)

### 1. `docs/PLAN.md` — roadmap (plans, not documentation)
The most volatile file; update it first. PLAN.md holds **what's left to build and
the open issues** — it is deliberately NOT a description of what exists (that's
the reference docs, artifact 2). Keep it lean. Specifically:
- **The status table** ("Where we are"): flip a step's box (`⬜`/`🟡`→`✅`) only
  when the code and its tests actually exist; the table's last column points at
  the relevant `reference/*.md` doc, not at code paths.
- **"Where we are" prose**: move the pointer to the step that is now genuinely
  next. If work closed an item, delete it from "Open work" — do NOT rewrite it
  into a "done, here's how it works" paragraph (that belongs in the reference
  doc). A completed thing leaves PLAN.md and lands (or is already) in `reference/`.
- **Open work / Known gaps carried forward**: remove gaps the work closed; add
  any new gap or TODO it introduced.
- **Cross-cutting backlog** and **Near-term sequence**: re-order / prune to match
  what's actually left.

### 2. `docs/reference/*.md` — per-section reference (what the code IS)
One doc per code section (`core`, `crosswalk`, `migrations`, `ais`, `occupancy`,
`depth`, `scheduling`, `intake`, `routers`, `frontend`, `deployment`, `tests`),
indexed by [`docs/reference/README.md`](../../../docs/reference/README.md). These
absorb the descriptive "how it works" content so PLAN.md can stay plans-only.
When this session changed code, update the reference doc(s) for the section(s)
touched:
- Keep each doc's **Files** table current — add a row when a file lands, drop one
  when a file is removed, fix the "What it does" cell when behavior changes.
- Update **Key concepts & invariants** when a rule the doc states changed (mirror
  the CLAUDE.md edit, artifact 3).
- **New code section / top-level dir** (e.g. a new `app/foo/`) → add a new
  `reference/foo.md` (follow the template of the existing docs: Purpose · Files
  table · Key concepts & invariants · Connections) **and** a row in
  `reference/README.md`.
- Fix **Connections** cross-links if the data flow between sections changed.
- Reference docs describe; they don't govern. If a doc and CLAUDE.md disagree,
  fix the doc to match CLAUDE.md (which is authority).

### 3. `CLAUDE.md` — design contract (authority)
Edit *narrowly*. CLAUDE.md is the contract; it changes only when the contract
does, not on every feature.
- **Build order list + the bold "Current state:" line** at the end of that
  section — keep the `✅/⬜` and the current-state sentence in lockstep with
  PLAN.md's table. This is the single most important sync.
- The **Repo layout** block — add new top-level modules/dirs that landed
  (e.g. `app/occupancy/`, a new `app/conflicts.py`). A new top-level dir also
  gets its own `docs/reference/*.md` (artifact 2).
- Touch **Core model / Schema / Conventions / Working agreements** *only* if a
  genuine decision changed (new enum value, new stationing system, a convention
  the user established this session). A new affine system, a new `AISSource`, a
  schema rule — those belong here. Routine progress does not.
- When PLAN.md and CLAUDE.md disagree, **CLAUDE.md wins** (PLAN.md says so) — so
  if the work changed a *rule*, fix CLAUDE.md and make PLAN.md follow.

### 4. Auto-memory — `~/.claude/projects/-Users-andrew-AMA-Projects-Popa-POPA-DEV/memory/`
Per the memory rules in the system prompt:
- Capture only what's **not derivable from the repo** — decisions, the *why*
  behind a non-obvious approach, user preferences/feedback, external pointers.
  Do **not** mirror code structure, file lists, or "step N done" (that's the
  repo's and PLAN.md's job).
- One fact per file with the required frontmatter (`name`, `description`,
  `metadata.type` = user|feedback|project|reference). Update an existing file
  rather than duplicating; delete memories the work proved wrong.
- Add/maintain the one-line pointer in `MEMORY.md` for any file you create
  (`- [Title](file.md) — hook`). `MEMORY.md` is index only — never put content
  there.
- Link related memories with `[[name]]`.

### 5. `README.md` — setup/run (if present and affected)
Only if this session changed how someone **installs, configures, migrates, or
runs** the project (new env var, new `python -m ...` entrypoint, new migration to
apply, new service in compose). Skip if untouched.

## Consistency pass (do this before declaring done)

Cross-check the step-trackers say the same thing:
- PLAN.md status table ↔ CLAUDE.md build-order list ↔ CLAUDE.md "Current state:"
  line — same step marked current, same boxes ticked.
- The reference doc(s) for any section touched this session match the code that
  landed (Files table rows exist; invariants match CLAUDE.md).
- Any path you cited actually exists (`git ls-files` / Glob it), and every
  `reference/*.md` has a row in `reference/README.md`.
- No artifact claims a step is done that the diff doesn't support.

## Output

Summarize what you reconciled as a short list — `file → what changed and why` —
and call out anything you intentionally left alone (e.g. "CLAUDE.md Core model
untouched; no rule changed this session"). If you found a doc-vs-code
contradiction, report it as a finding rather than papering over it.
