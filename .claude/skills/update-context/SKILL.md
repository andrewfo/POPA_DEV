---
name: update-context
description: Reconcile everything that persists across sessions with what actually changed — PLAN.md (roadmap/status), CLAUDE.md (design contract: build order + current-state line), the auto-memory (MEMORY.md index + memory files), and README.md. Use at the end of a chunk of work, when a build step lands, or when the user says "update context / the plan / the docs / your memory". It edits the durable record; it does not change app code.
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

## The four persistent artifacts (reconcile each)

### 1. `PLAN.md` — roadmap & status (intent)
The most volatile file; update it first. Specifically:
- **The status table** (§1): flip a step's box (`⬜`→`✅`) only when the code and
  its tests actually exist; fill the **"Lands in"** column with the real paths
  (e.g. `app/occupancy/*`, `tests/test_occupancy_*`, the migration number).
- **"Current state" / "next up"** prose: move the pointer to the step that is now
  genuinely next. If a step's dedicated section (e.g. §2) is now built, condense
  it from a plan into a one-line "done, see <paths>" and promote the following
  step's detail.
- **Known gaps carried forward** (§1): tick off gaps the work closed; add any new
  gap or TODO the work introduced (placeholder geometry, opt-in DB tests, etc.).
- **Near-term sequence** (§7): re-order to match what's left.

### 2. `CLAUDE.md` — design contract (authority)
Edit *narrowly*. CLAUDE.md is the contract; it changes only when the contract
does, not on every feature.
- **Build order list + the bold "Current state:" line** at the end of that
  section — keep the `✅/⬜` and the current-state sentence in lockstep with
  PLAN.md's table. This is the single most important sync.
- The **Repo layout** block — add new top-level modules/dirs that landed
  (e.g. `app/occupancy/`, a new `app/conflicts.py`).
- Touch **Core model / Schema / Conventions / Working agreements** *only* if a
  genuine decision changed (new enum value, new stationing system, a convention
  the user established this session). A new affine system, a new `AISSource`, a
  schema rule — those belong here. Routine progress does not.
- When PLAN.md and CLAUDE.md disagree, **CLAUDE.md wins** (PLAN.md says so) — so
  if the work changed a *rule*, fix CLAUDE.md and make PLAN.md follow.

### 3. Auto-memory — `C:\Users\andrew\.claude\projects\C--Users-andrew-popa-wharf\memory\`
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

### 4. `README.md` — setup/run (if present and affected)
Only if this session changed how someone **installs, configures, migrates, or
runs** the project (new env var, new `python -m ...` entrypoint, new migration to
apply, new service in compose). Skip if untouched.

## Consistency pass (do this before declaring done)

Cross-check the three step-trackers say the same thing:
- PLAN.md status table ↔ CLAUDE.md build-order list ↔ CLAUDE.md "Current state:"
  line — same step marked current, same boxes ticked.
- Any path you cited actually exists (`git ls-files` / Glob it).
- No artifact claims a step is done that the diff doesn't support.

## Output

Summarize what you reconciled as a short list — `file → what changed and why` —
and call out anything you intentionally left alone (e.g. "CLAUDE.md Core model
untouched; no rule changed this session"). If you found a doc-vs-code
contradiction, report it as a finding rather than papering over it.
