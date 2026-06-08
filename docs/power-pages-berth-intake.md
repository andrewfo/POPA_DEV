# Berth Request Intake — Power Pages + Dynamics (Dataverse)

Plan to replace the current **Adobe Sign form → SharePoint list** berth-request
intake on the port website with a **Power Pages form backed by Dynamics /
Dataverse**, built as a self-contained managed solution and handed to IT to
deploy.

> **Context / heads-up.** The `popa-wharf` data layer deliberately moved to
> **manual-only intake** (`app/intake/manual.py`); the automated online-form /
> SharePoint feed and an earlier Dynamics-365 idea were both **retired in
> 2026-06**. An automated channel has since been **re-introduced as an
> AI-assisted pull worker** (`app/intake/dataverse_run.py` + `app/intake/llm.py`,
> 2026-06): it polls this Dataverse table outbound, LLM-normalizes each new row,
> and records it raw in `intake_event` (content-hash deduped) + a `requested`
> reservation with an **empty `station_range`** until an operator assigns a
> berth. See **Phase 8** for the wiring and the IT ask. (The earlier idea — a
> Power Automate flow making an *inbound* `POST /intake/berth-request` call — was
> dropped in favour of the outbound pull, which needs no DLP exception.)

---

## Step 0 — Access (blocker first)

You currently get **"no environments found"** at make.powerapps.com, which means
you have **no Power Platform license / environment / role yet**. Nothing can be
built until IT provisions access. This is **step 1**, not step 9.

**Send IT this request:**

> Subject: Power Platform access for a Berth Request intake form (Power Pages + Dataverse)
>
> I'm prototyping a replacement for the Adobe Sign → SharePoint berth-request
> form on the port website, using Power Pages backed by Dynamics/Dataverse. When
> I sign into make.powerapps.com it says "no environments found," so I can't
> build yet. Could you set me up with:
>
> 1. A **Power Apps license** (or Power Platform / Dynamics license with maker access) on my account.
> 2. A **non-production (Dev) environment with a Dataverse database** — I don't need prod.
> 3. The **System Customizer** (or System Administrator) security role in that environment.
> 4. Confirmation that **Power Pages** is available there (I'll only *preview* the site, not publish a public URL without sign-off).
>
> Goal: build a self-contained **managed solution** to hand back for official deployment.

**Verify access is working before continuing:** make.powerapps.com → environment
picker shows a Dev environment → left nav **Solutions** loads without a license
error.

---

## Phase 1 — Create the Solution (your container)

Build *everything* inside one unmanaged solution so it packages cleanly for IT.

1. make.powerapps.com → confirm the **environment picker** (top-right) is on your Dev environment.
2. Left nav → **Solutions** → **+ New solution**.
3. Fill in:
   - **Display name:** `POPA Berth Intake`
   - **Publisher:** **+ New publisher** → Display name `Port of Port Arthur`, **Prefix `popa`** → Save → select it.
   - **Version:** `1.0.0.0`
4. **Create.** Build all components below **from inside this solution**.

> If **+ New → Table** is ever greyed out: you opened a **Managed** solution (can't
> add tables — use your unmanaged one), the environment has **no Dataverse
> database**, or you lack the **System Customizer** role. All three trace back to
> Step 0.

---

## Phase 2 — Dataverse table

5. Inside the solution → **+ New → Table → Table (advanced properties)**.
   - **Display name:** `Berth Request` (plural `Berth Requests`) → becomes `popa_berthrequest`.
   - **Save**, wait for provisioning.
6. Add columns (**+ New → Column** for each, then Save). The set below mirrors
   the `popa-wharf` intake form (`app/intake/manual.py` `BerthRequestForm`) field
   for field, so a submission normalizes cleanly. **Dimensions are in FEET and
   cargo in net tons** — the app converts feet→metres on ingest; do **not**
   pre-convert to metres on the form, or values land doubly converted. The
   `Maps to` column is the canonical `BerthRequestForm` key the LLM normalizer
   (`app/intake/llm.py`) extracts to.

   | Display name | Data type | Maps to | Notes |
   |---|---|---|---|
   | Request Date | Date Only | `request_date` | defaults to today |
   | Vessel Name | Single line of text | `vessel` | **required** |
   | S/S Line | Single line of text | `ss_line` | |
   | Flag | Single line of text | `flag` | |
   | Destinations | Single line of text | `destinations` | |
   | IMO | Whole number | `imo` | **required**; 7-digit, check-digit validated server-side |
   | LOA (ft) | Decimal number | `length_ft` | |
   | Beam (ft) | Decimal number | `beam_ft` | |
   | Draft (ft) | Decimal number | `draft_ft` | **required** |
   | Deadweight (lbs) | Decimal number | `deadweight_lbs` | |
   | Due From | Single line of text | `due_from` | origin port |
   | Requested ETB | Date and Time | `etb` | arrival |
   | To Sail For | Single line of text | `sail_for` | destination port |
   | Requested ETD | Date and Time | `etd` | departure |
   | Inbound Cargo Type | Single line of text | `inbound_cargo` | |
   | Inbound Cargo Weight (net tons) | Decimal number | `inbound_tons` | |
   | Outbound Cargo Type | Single line of text | `outbound_cargo` | |
   | Outbound Cargo Weight (net tons) | Decimal number | `outbound_tons` | |
   | Begin Receiving Outbound On | Date Only | `outbound_cargo_start` | |
   | Taking Bunkers | **Yes/No** | `bunkers` | default No |
   | Bunkering Type | **Choice** | `bunker_type` | options below; store the **code** as the value |
   | Approx Bunker Fuel (metric tons) | Decimal number | `bunker_qty_mt` | bunkers are quoted in MT, not net tons |
   | Bunkering Acknowledged | **Yes/No** | `bunkering_acknowledged` | default No |
   | Requestor Name | Single line of text | `requestor_name` | |
   | Requestor Email | Single line of text | `requestor_email` | format **Email** |
   | Requestor Phone | Single line of text | `requestor_phone` | format **Phone** |
   | Notes | Multiple lines of text | `notes` | |
   | Signature | Single line of text | `signature` | typed/drawn acceptance; audit only |
   | Raw Submission | Multiple lines of text | — | internal audit copy (Phase 8) |
   | Request Status | **Choice** | — | `New` / `Triaged` / `Reconciled` / `Rejected`, default `New` (internal; the worker filters on it) |

   **Bunkering Type choice** (label → stored value):

   | Choice label | Value |
   |---|---|
   | Biofuels/Alternative Fuels (BIO) | `BIO` |
   | Heavy Fuel Oil (HFO) | `HFO` |
   | Liquified Natural Gas (LNG) | `LNG` |
   | Marine Gas Oil (MGO) | `MGO` |
   | Very Low-Sulfur Fuel Oil (VLSFO) | `VLSFO` |

   > **No MMSI on the public form.** The app keys manual requests by **IMO** (AIS
   > supplies MMSI); a requestor rarely knows the MMSI, so it's dropped. The
   > bunkering type/quantity/acknowledgement, requestor block, and signature have
   > **no dedicated columns in `popa-wharf`** — they land verbatim in
   > `intake_event.raw` and fold into the reservation `notes`, like flag/agency.
   > That's intentional; don't add reservation columns for them.

---

## Phase 3 — Public form

7. On the table → **Forms** tab → **+ New form → Main form**.
8. Drag on the **public** fields, grouped to match the paper request: request
   date; vessel (name, S/S line, flag, destinations, IMO, LOA/beam/draft,
   deadweight); schedule (due from, ETB, to sail for, ETD); cargo (in/out type +
   weight, begin-receiving date); bunkering (taking bunkers, type, metric tons,
   acknowledged); requestor (name, email, phone); notes; signature. **Leave off**
   Request Status and Raw Submission (internal).
9. Mark **Vessel Name**, **IMO**, and **Draft (ft)** required (matches the
   `popa-wharf` form's required set); **Requestor Email** is recommended.
10. **Save → Publish.** Note the form name (e.g. "Berth Request — Public").

---

## Phase 4 — Power Pages site

11. New tab → **make.powerpages.microsoft.com** → confirm the **same environment**.
12. **+ Create a site** → blank/starter template → name `popa-berth-intake` → **Done** (provisioning takes several minutes).
13. The design studio opens (Pages / Styling / Data / Set up tabs).

---

## Phase 5 — Put the form on a page

14. **Pages → + Page** → blank layout → name `Berth Request`.
15. In a section → **+ Component → Form**.
16. Right panel:
    - **Table:** `Berth Request (popa_berthrequest)`
    - **Form:** "Berth Request — Public"
    - **Mode:** **Create** (Insert)
    - **On submit:** success message or redirect to a Thank-you page.
17. **OK** — the form renders.

---

## Phase 6 — Table permissions (the step that silently breaks)

18. **Set up → Table permissions → + New permission.**
    - **Name:** `Berth Request Create`
    - **Table:** `Berth Request`
    - **Access type:** **Global**
    - **Permission to:** **Create** (+ Append if you later add lookups)
    - **Roles:** **+ Add roles → Anonymous Users** (public form).
19. **Save.** Without this the form submits but saves nothing.
20. Auth model: public form → keep the page **anonymous** (page → settings →
    permissions → anyone). Staff-only → restrict to Authenticated Users.

---

## Phase 7 — Test live

21. Top-right **Preview → Desktop** → open the Berth Request page → fill → **Submit**.
22. make.powerapps.com → table → **Data** tab → confirm the row landed.
23. If empty: re-check Phase 6 (Global scope + Anonymous role).

---

## Phase 8 — Feed requests into the popa-wharf data layer (AI-assisted, BUILT)

The integration now exists in this repo as a **pull worker** —
`app/intake/dataverse_run.py` — rather than a Power Automate **push**. The worker
polls this table, hands each new row to a cheap LLM (`app/intake/llm.py`, via
**OpenRouter**, default Gemini Flash) that normalizes the messy/partial fields
into the canonical berth-request shape, then records it through the same pipeline
as a hand-typed request: the raw row is preserved in `intake_event`, and a
low-stakes `requested` reservation is projected with an **empty station range**
(it never places a vessel or trips the confirmed-only overlap constraint — an
operator still assigns the berth and confirms). The LLM's confidence + caveats
ride onto the reservation note (`AI-parsed (confidence 80%) — …`) so an operator
knows which auto-parsed cards to eyeball.

**Why a pull, not a Power Automate HTTP push:** the worker authenticates with an
Azure AD **app-registration (client-credentials)** and only makes **outbound**
calls to `login.microsoftonline.com` + your `*.crm.dynamics.com`. That sidesteps
both blockers of a push: no inbound hole in the api (which binds to `127.0.0.1`)
and **no HTTP-connector DLP exception** to negotiate.

**What this needs from IT (add to the handoff ask):**
1. An **app registration** (Entra) with a client secret.
2. A **Dataverse application user** for that app in the environment, granted a
   security role with **read + write** on the `Berth Request` table (read new
   rows, write the status back to *Triaged*).

**What you must read off your solution** and put in `.env` (see `.env.example`,
the *AI-assisted intake* block): the **Request Status** choice column's integer
option values for **New** and **Triaged** (make.powerapps.com → the choice column
shows each option's value) → `DATAVERSE_STATUS_NEW` / `DATAVERSE_STATUS_TRIAGED`.
The worker filters on *= New* and PATCHes to *Triaged* after recording, so each
row is parsed exactly once; both values must be set or it refuses to start.

> Keep the Phase 6 **Cloud flow** idea (copy fields into **Raw Submission**, set
> **Request Status = New** on insert) — it's still the clean way to stamp the
> initial status the worker filters on, and it stays **inside** Dataverse (no DLP
> issue). The retired alternative was the *outbound* `POST /intake/berth-request`
> HTTP call from a flow; the pull worker replaces it.

---

## Phase 9 — Package for IT

26. make.powerapps.com → **Solutions** → open `POPA Berth Intake`.
27. Add the site if needed: **+ Add existing → More → Site** → select your Power Pages site.
28. Select the solution → **… → Export solution** → run **Solution checker** (fix red) → choose **Managed** → **Export** → download `.zip`.
29. Attach a one-page doc: table + fields, connectors used (maps to their DLP
    policy), auth model, the public URL you want, the integration ask (HTTP
    connector DLP exception if it must call the popa-wharf API), and who **owns**
    it after handoff.

That `.zip` + doc is what makes it official.

---

## Things that will trip you up

- **Stay in one environment** the whole time — Phase 1's environment must equal
  Phase 4's. Mismatched environments = the Power Pages site can't see your table.
- **Don't publish the public URL yourself** — licensing/security is IT's call.
  **Preview** is enough to prove it works.
- **DLP** is the usual integration blocker: the HTTP connector needed to call
  this app's API is often in IT's blocked/business-only group. Confirm before
  designing around it.
- Adding a table to a **managed** solution is impossible — always work in your
  **unmanaged** `POPA Berth Intake` solution.
