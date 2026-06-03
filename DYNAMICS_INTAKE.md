# Dynamics 365 Berth-Request Intake — Integration Contract (for IT)

**Goal:** the online berth-request form writes into a **Dynamics 365 / Dataverse
table** instead of the Adobe Sign → SharePoint list. The wharf data layer then
reads that table directly (app-only, over the Dataverse Web API) and lands each
request into its `intake_event` audit table + a `requested` reservation — exactly
as it does today for SharePoint, with **no change to any downstream logic**.

This document is the hand-off: what IT builds on the Dynamics side, and the
connection values to hand back. It splits into three pieces — (1) the table,
(2) the columns, (3) the app registration. Get these three right and the data
layer needs only a config block (no code change for IT to worry about).

---

## 1. The table (entity)

Create **one custom table** in the target Dynamics environment — one **row per
berth request** (one row per form submission).

| Property | Value |
|---|---|
| Display name | `Berth Request` (suggested) |
| Logical name | e.g. `popa_berthrequest` (your publisher prefix) — **send this to us** |
| Entity set name | e.g. `popa_berthrequests` — **send this to us** (this is what the Web API path uses) |
| Ownership | Either (User/Team or Organization) — doesn't matter to us; affects which security role scope you grant in §3 |

The online form (Power Apps / model-driven / canvas, IT's choice) writes a new
row to this table on each submission. That's the only write path; the data layer
is **read-only** against this table.

---

## 2. The columns

The data layer's parser keys on a fixed set of **field names**. The simplest,
zero-config path: **set each Dataverse column's _Display Name_ to exactly the
string in the "Display name (match exactly)" column below.** Our reader maps a
column's Display Name → the parser key automatically (same mechanism we use for
the SharePoint list today), so matching the display names means it just works.

> If you'd rather use your own display names, that's fine — just send us the
> `logical_name → meaning` mapping instead and we'll configure the map on our
> side. But matching exactly is the least error-prone.

| Display name (match exactly) | Meaning | Type | Required? | Format / notes |
|---|---|---|---|---|
| `Vessel` | Vessel name | Single line text | **Yes** | Free text; barge/tow strings OK (we de-tangle later) |
| `IMO Number` | IMO number | Single line text or Whole number | No | A real IMO is **7 digits**. Text is fine — we extract the 7-digit run and warn on junk |
| `Assigned Berth` | Berth (if the port pre-assigns) | Single line text | No | Usually blank at request time; port assigns later |
| `Agency/Owner` | Submitting agent/agency | Single line text | No | |
| `Port Arrival Date` | ETB (arrival) | **Date and Time** | No* | *No arrival date ⇒ no time window ⇒ raw row lands but **no reservation** is created. Strongly encourage requiring it on the form |
| `Port Departure Date` | ETD (departure) | Date and Time | No | Open-ended window if blank |
| `Length (feet)` | LOA | Decimal / float | No | **Feet.** We convert to metres on store |
| `Beam Width` | Beam | Decimal / float | No | **Feet** |
| `Draft (feet)` | Draft | Decimal / float | No | **Feet** |
| `Vessel Due From` | Origin | Single line text | No | |
| `Vessel To Sail For` | Destination | Single line text | No | We use this; falls back to `Destination` if absent |
| `Inbound Cargo` | Inbound cargo | Single line text | No | |
| `Outbound Cargo` | Outbound cargo | Single line text | No | |
| `AgreementStatus` | Signed / Cancelled status | Single line / Choice | No | Send the label text (e.g. `signed`, `cancelled`) |
| `SenderInfo` | Who submitted (email/etc.) | Single line text | No | |

Notes that matter:

- **Units are feet** for Length / Beam / Draft (the form is in feet today; keep
  it). The data layer stores metres and converts on the way in.
- **Dates:** Dataverse returns date/time columns as **ISO-8601 UTC** over the Web
  API (e.g. `2026-06-01T07:00:00Z`). Our parser already accepts ISO-8601, so this
  is the happy path — no format coordination needed.
- **Nothing is hard-required by the data layer** (it lands the raw row and records
  a warning for anything missing), but a request with **no `Vessel` and no
  `Port Arrival Date`** produces only an audit row, not a schedulable reservation.
  Make at least those two required on the form.

---

## 3. App registration (how the data layer authenticates)

The data layer connects **app-only / client-credentials** (a service principal,
no signed-in user) — identical to the SharePoint Graph integration. Steps:

1. **Register an Azure AD application** in the tenant
   (Entra ID → App registrations → New). Note the **Application (client) ID** and
   the **Directory (tenant) ID**.
2. **Create a client secret** on that app (Certificates & secrets → New client
   secret). Note the secret **value** (shown once).
3. **Create a Dataverse Application User** bound to that app:
   - Power Platform Admin Center → the target environment → **Settings → Users +
     permissions → Application users → New app user** → pick the app registration
     from step 1.
   - Assign a **security role** that grants **Read** on the `Berth Request` table.
     A custom role with just **Read** (at the appropriate scope — Organization if
     the table is org-owned, otherwise Business Unit) is ideal; least privilege.
     The role also needs read on entity metadata, which the platform's basic read
     access already covers.
4. No Dynamics UI license is needed for an application user — it's a non-interactive
   service principal.

> The data layer only ever **reads** this table. It does not write back to
> Dynamics (status/berth assignment stays in the wharf data layer). If you later
> want assignment/confirmation pushed back into Dynamics, that's a separate
> follow-on (we'd add Write to the role and a write path) — out of scope here.

---

## 4. Values to send back to us

Once the above exists, hand us these (we'll drop them into the data layer's
config — they stay blank/inert until then, so nothing breaks in the meantime):

| Value | Example | Where it comes from |
|---|---|---|
| Tenant ID | `xxxxxxxx-xxxx-…` | App registration → Directory (tenant) ID |
| Client ID | `xxxxxxxx-xxxx-…` | App registration → Application (client) ID |
| Client secret | `(secret value)` | App registration → Certificates & secrets |
| Environment / org URL | `https://orgname.crm.dynamics.com` | Power Platform env URL |
| Table logical name | `popa_berthrequest` | Your table |
| Entity set name | `popa_berthrequests` | Your table (used in the Web API path) |

Send the **client secret** over a secure channel (not email/Teams chat) — treat
it like a password.

---

## 5. What changes on our side (FYI — no action for IT)

For completeness, the wharf data-layer work this enables:

- A new read source (`DataverseSource`) that authenticates client-credentials,
  pages the table over the Dataverse Web API (`/api/data/v9.2/<entityset>`,
  following `@odata.nextLink`), and maps columns → the same parser keys. It feeds
  the **identical** ingestion path — `intake_event` (raw, deduped by content
  hash), then a projected `requested` reservation.
- The existing SharePoint sources (CSV export + Graph live) are **retired** once
  Dynamics is live, since the form no longer lands in SharePoint.
- Idempotent by design: re-reading the whole table is a no-op for unchanged rows
  (content-hash dedupe), so we can poll/schedule it safely.

---

## 6. One thing to confirm before building the form

**Who is the system of record for the form itself?** Today it's Adobe Sign →
SharePoint. After this, it's the Dynamics table. Make sure the old form is
**decommissioned or redirected** at cut-over so requests don't split across two
destinations — otherwise some land in SharePoint (which we'll have stopped
reading) and silently vanish from scheduling.
