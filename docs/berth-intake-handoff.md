# Berth Request Intake — One-Page Handoff

**What this is:** a Power Pages form (backed by a Dynamics/Dataverse table) that
replaces the old Adobe Sign → SharePoint berth-request form on the port website.
It's packaged as a self-contained **managed solution** (`.zip`) so you can deploy
it. This page is the cliff-notes; the full build steps live in
`power-pages-berth-intake.md`.

---

## The table

One Dataverse table, **`popa_berthrequest`** ("Berth Request"). It mirrors the
paper request form: vessel info (name, IMO, flag, LOA/beam/draft, deadweight),
schedule (ETB/ETD, due-from, sail-for), cargo (in/out type + tons), bunkering
(taking bunkers Y/N, fuel type, MT, acknowledged), requestor block (name, email,
phone), notes, and a typed signature.

A couple of fields are internal and **not** on the public form:
- **Request Status** — `New` / `Triaged` / `Reconciled` / `Rejected`. New rows
  start at `New`; the integration flips them to `Triaged` once read.
- **Raw Submission** — an audit copy of the submitted values.

Heads-up for whoever maintains it: dimensions are in **feet** and cargo in **net
tons** — that's on purpose, the downstream app converts. Don't switch the form to
metres.

## Auth model

- Public berth-request page → **Anonymous** access, table permission scoped
  **Global / Create** for Anonymous Users. (Without that the form submits but
  saves nothing — it's the easy thing to miss.)
- Any staff-only pages → Authenticated Users.

## Connectors / DLP

- The form itself is plain Power Pages + Dataverse — **no external connectors**,
  nothing to clear with DLP.
- The downstream integration (below) is **outbound only** and lives outside Power
  Platform, so it also needs **no HTTP-connector DLP exception**. This was a
  deliberate choice over the earlier "flow calls our API" idea.

## The integration (how requests reach the scheduling system)

The `popa-wharf` data layer **pulls** from this table — a small worker polls for
rows where Status = `New`, normalizes them, records them, and sets Status =
`Triaged`. It only ever makes outbound calls to `login.microsoftonline.com` and
`*.crm.dynamics.com`. No inbound hole, no public API exposure.

**What we need from IT for it to run:**
1. An **Entra app registration** with a client secret.
2. A **Dataverse application user** for that app, with a role granting
   **read + write** on the Berth Request table (read new rows, write Status back).

## Public URL

We'd like to publish at: **`<fill in the desired path, e.g. ports website /berth-request>`**.
We've only *previewed* the site so far — publishing the live URL is your call.

## Ownership after handoff

- **App / solution maintenance:** `<team/person>`
- **The pull worker + scheduling system:** `<team/person>`
- **Day-to-day request triage:** port operations

---

**Deliverable:** the managed-solution `.zip` (Solution Checker clean) + this page.
That's what makes it official.
