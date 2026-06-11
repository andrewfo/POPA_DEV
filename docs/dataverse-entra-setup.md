# Setting up Dataverse + Entra ID for the berth-request pull worker

This is the IT/admin side: provisioning the bits that let the `popa-wharf`
pull worker (`app/intake/dataverse_run.py`) read berth requests out of the Power
Pages "Berth Request" Dataverse table. The worker only ever makes **outbound**
calls (to `login.microsoftonline.com` and your `*.crm.dynamics.com`), so there's
no inbound exposure and **no HTTP-connector DLP exception** to negotiate.

By the end you'll have filled in the `Dataverse` block of `.env` (see
`.env.example`). The table/form itself is built per `power-pages-berth-intake.md`
— this doc assumes the `Berth Request` table already exists.

> You need **System Administrator** (or equivalent) on the target Power Platform
> environment and the right to create app registrations in Entra. If you can't do
> both, you'll need to loop in whoever can for the steps below.

---

## Part A — Register the app in Entra ID

This is the identity the worker logs in as (client-credentials, no human).

1. Go to **[entra.microsoft.com](https://entra.microsoft.com)** → **Applications
   → App registrations → + New registration**.
2. Name it something obvious, e.g. `popa-berth-intake-worker`.
3. **Supported account types:** *Accounts in this organizational directory only*
   (single tenant).
4. Leave **Redirect URI** blank — this app never does an interactive login.
5. **Register.**

On the app's **Overview** page, copy these two — you'll need them later:
- **Application (client) ID** → `DATAVERSE_CLIENT_ID`
- **Directory (tenant) ID** → `DATAVERSE_TENANT_ID`

### Create a client secret

6. Left nav → **Certificates & secrets → + New client secret**.
7. Add a description and an expiry (12 or 24 months — set a calendar reminder to
   rotate it; when it expires the worker stops pulling).
8. **Add**, then **immediately copy the secret *Value*** (not the Secret ID) →
   `DATAVERSE_CLIENT_SECRET`. You can't see it again after you leave the page.

> No API permissions / admin consent needed here. Access to Dataverse is granted
> on the Dataverse side (Part B) by adding this app as an *application user*, not
> via Entra API permissions.

---

## Part B — Give the app access to Dataverse

Now let that app read/write the Berth Request table in the environment.

### B1. Create a security role (least-privilege)

1. **[admin.powerplatform.microsoft.com](https://admin.powerplatform.microsoft.com)**
   → **Environments** → open the environment that holds the Berth Request table.
2. **Settings → Users + permissions → Security roles → + New role**.
3. Name it e.g. `Berth Intake Worker`. The modern role editor also asks for an
   **Applies to** value — this is just a free-text *description* of who the role
   is for; it grants nothing and doesn't scope any privilege, the form merely
   requires it to be non-empty. Put something like
   `Berth intake worker (service principal / application user)`.
4. On the **Custom Entities** (modern UI: **Tables**) tab, find **Berth Request**
   and grant:
   - **Read** and **Write** — set the privilege depth to **Organization** (the
     full circle) so the app sees rows created by anonymous public submissions.
   - (Create/Delete/Append not required — the worker only reads rows and writes
     the status field back.)
5. **Save and Close.**

> If you'd rather not build a custom role, you *can* assign a broad built-in role
> instead, but read+write on the single table is the clean least-privilege option.

### B2. Add the app as an application user

1. Same environment → **Settings → Users + permissions → Application users → +
   New app user**.
2. **+ Add an app** → search for the app registration by name (`popa-berth-intake-worker`)
   → select it.
3. Set the **Business unit** (the org root is fine).
4. Under **Security roles**, add the `Berth Intake Worker` role from B1.
5. **Create.**

The app can now authenticate to `https://<yourorg>.crm.dynamics.com` and
read/write Berth Request rows.

---

## Part C — Read off the table values

A few values come from the table itself, not from Entra.

### C1. Environment URL

Your Dataverse Web API base. Power Platform Admin Center → the environment →
**Environment URL** (e.g. `https://yourorg.crm.dynamics.com`) → `DATAVERSE_URL`.

### C2. Table + key field

These default correctly if you used the `popa` publisher prefix and named the
table `Berth Request`, but confirm against the table's properties:
- **Entity set name** (plural logical name) → `DATAVERSE_TABLE`
  (default `popa_berthrequests`).
- **Primary ID column** → `DATAVERSE_ID_FIELD` (default `popa_berthrequestid`).

### C3. Request Status option values (the easy thing to get wrong)

The worker pulls rows where Status = **New** and flips them to **Triaged** after
recording, so it needs the **integer** values behind those choice labels.

1. make.powerapps.com → your solution → the **Berth Request** table → **Columns**
   → open the **Request Status** choice column.
2. Each option shows a label *and* an integer **Value** (usually like
   `100000000`, `100000001`, …). Note the integers for **New** and **Triaged**.

- `DATAVERSE_STATUS_FIELD` = `popa_requeststatus` (the column's logical name)
- `DATAVERSE_STATUS_NEW` = the integer for **New**
- `DATAVERSE_STATUS_TRIAGED` = the integer for **Triaged**

> Both status integers must be set or the worker **refuses to start** — that's a
> guard against it silently re-processing or skipping every row.

---

## Part D — Fill in `.env` and verify

Copy `.env.example` → `.env` (if you haven't) and fill the Dataverse block:

```ini
DATAVERSE_URL=https://yourorg.crm.dynamics.com
DATAVERSE_TENANT_ID=<Directory (tenant) ID from A>
DATAVERSE_CLIENT_ID=<Application (client) ID from A>
DATAVERSE_CLIENT_SECRET=<secret Value from A>
DATAVERSE_TABLE=popa_berthrequests
DATAVERSE_ID_FIELD=popa_berthrequestid
DATAVERSE_STATUS_FIELD=popa_requeststatus
DATAVERSE_STATUS_NEW=<integer from C3>
DATAVERSE_STATUS_TRIAGED=<integer from C3>
DATAVERSE_POLL_SECONDS=300
DATAVERSE_BATCH_LIMIT=25
```

You also need an **OpenRouter API key** for the LLM normalizer (separate from all
the above — get one at <https://openrouter.ai/keys>):

```ini
OPENROUTER_API_KEY=<your key>
INTAKE_LLM_MODEL=google/gemini-2.0-flash-lite-001
```

The worker treats the whole Dataverse channel as **optional** and only turns on
when `DATAVERSE_URL` + tenant + client id/secret are all set, so a blank block
just leaves it off.

### Quick verification

1. Submit a test row through the Power Pages form (or set one row's Status = New
   in the table's **Data** tab).
2. Start the worker: `python -m app.intake.dataverse_run`.
3. Expected: it logs in, pulls the New row, records a `requested` reservation in
   `popa-wharf`, and the Dataverse row's **Request Status flips to Triaged**.
4. If it errors on startup about missing status values → C3. If it gets a 401/403
   reaching Dataverse → re-check Part B (app user + role + Organization depth). If
   it authenticates but finds no rows → confirm `DATAVERSE_STATUS_NEW` matches the
   integer for *New*, not the label.

---

## Things to hand back / remember

- **Secret expiry:** the client secret from A expires — set a reminder to rotate
  it before then, or the worker quietly stops pulling.
- **Owner:** record who owns the app registration and the application-user role
  so renewals don't fall through the cracks.
- This whole setup is read-mostly and outbound-only; if anyone asks about DLP or
  inbound firewall holes, the answer is there are none — that was the point of the
  pull design.
