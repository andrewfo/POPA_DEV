# Berth Request Intake — Power Pages + Dynamics (Dataverse)

Plan to replace the current **Adobe Sign form → SharePoint list** berth-request
intake on the port website with a **Power Pages form backed by Dynamics /
Dataverse**, built as a self-contained managed solution and handed to IT to
deploy.

> **Context / heads-up.** The `popa-wharf` data layer deliberately moved to
> **manual-only intake** (`app/intake/manual.py`); the automated online-form /
> SharePoint feed and an earlier Dynamics-365 idea were both **retired in
> 2026-06**. Reviving an automated channel is a real product decision. If these
> Dynamics requests must also reach this system (not just live in Dynamics), the
> integration point is a Power Automate flow / webhook that calls
> `POST /intake/berth-request` — which lands the request raw in `intake_event`
> (content-hash deduped) and creates a `requested` reservation with an **empty
> `station_range`** until an operator assigns a berth.

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
6. Add columns (**+ New → Column** for each, then Save):

   | Display name | Data type | Notes |
   |---|---|---|
   | Vessel Name | Single line of text | required |
   | IMO | Single line of text | max 20 |
   | MMSI | Single line of text | |
   | LOA (m) | Decimal number | |
   | Beam (m) | Decimal number | |
   | Draft (m) | Decimal number | |
   | Requested ETB | Date and Time | |
   | Requested ETD | Date and Time | |
   | Cargo | Single line of text | |
   | Requestor Name | Single line of text | |
   | Requestor Email | Single line of text | format **Email** |
   | Requestor Phone | Single line of text | format **Phone** |
   | Notes | Multiple lines of text | |
   | Raw Submission | Multiple lines of text | audit copy |
   | Request Status | **Choice** | `New` / `Rejected`, default `New` |

---

## Phase 3 — Public form

7. On the table → **Forms** tab → **+ New form → Main form**.
8. Drag on the **public** fields: vessel name, IMO, LOA/beam/draft, ETB/ETD,
   cargo, requestor name/email/phone, notes. **Leave off** Request Status and Raw
   Submission (internal).
9. Mark **Vessel Name** and **Requestor Email** required.
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

## Phase 8 — (Optional) raw copy + status workflow

24. Solution → **+ New → Automation → Cloud flow** → trigger **"When a row is
    added"** (Dataverse, this table) → **Update a row**: copy submitted fields
    into **Raw Submission**, set **Request Status = New**.
25. *This is also where a future `POST /intake/berth-request` outbound call would
    live — but it uses the **HTTP connector**, which IT's DLP policy often
    blocks. Leave it out of v1; name it in the handoff doc as an integration ask.*

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
