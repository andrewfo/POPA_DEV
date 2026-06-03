# Deploying the POPA Wharf Data Layer

This runs the full production stack on a Linux host and serves it to your
operators over a network IT sanctions, behind a reverse proxy that terminates
TLS. The app itself is just an HTTP service on a port with HTTP Basic auth — how
it's reached (corporate VPN, internal host, Azure) is an infrastructure choice;
this file covers the host + the safe-exposure options.

For the artifacts themselves (image, compose, auth) see the **Deploy** section of
[`README.md`](./README.md).

---

## What you need

- **A host that runs 24/7** with Docker. An IT-managed internal server, a VM, or
  a small cloud instance all work. It must stay on — the AIS ingestor is a
  long-lived connection; a host that sleeps loses the live feed.
- **~2 GB RAM** (Postgres + PostGIS + gunicorn + the AIS/occupancy workers).
- **A way for operators to reach it** that IT allows — see step 5.

---

## 1. Install Docker on the host

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"   # log out/in so `docker` works without sudo
```

## 2. Get the code

```bash
git clone https://github.com/andrewfo/SlackWater.git
cd SlackWater
```

## 3. Configure secrets

```bash
cp .env.prod.example .env.prod
```

Edit `.env.prod` (it's gitignored — it never leaves the host):

- `POSTGRES_PASSWORD` — a strong, unique password.
- `OPERATOR_USER` / `OPERATOR_PASSWORD` — the single login operators use. Setting
  **both** is what turns the app's HTTP Basic auth on.
- `AISSTREAM_API_KEY` — your free key from <https://aisstream.io>.

## 4. Start the stack

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

`migrate` runs once (`alembic upgrade head` + seed), then `api`, `ais`, and
`occupancy` come up. The API is published on **`127.0.0.1:8000` only** — not the
public internet. Confirm:

```bash
docker compose -f docker-compose.prod.yml ps         # all healthy/running
curl -s http://localhost:8000/health                 # {"status":"ok",...}
```

## 5. Expose it safely (reverse proxy + TLS over a sanctioned network)

The stack serves plain HTTP on `127.0.0.1:8000`. Put it behind something that
(a) terminates **TLS** and (b) is reachable only over a network IT allows. HTTP
Basic only base64-encodes credentials, so TLS in front is mandatory. Pick what
IT sanctions:

- **Internal host + corporate VPN/LAN** — run it on a server inside the corporate
  network; operators reach it over the company VPN or on-site. Front it with a
  reverse proxy (nginx / Caddy / IIS ARR) holding a TLS cert from your internal
  CA, on a corporate DNS name. Nothing leaves the corp network.
- **Azure behind Entra ID SSO** (you already use the `portpa.com` tenant for the
  SharePoint intake) — deploy the image to Azure (App Service for Containers /
  Container Apps / a VM) behind Azure's TLS and gate it with Entra ID SSO
  (App Service "Easy Auth" or Application Gateway). Reuses identity you already
  have; usually the most IT-friendly path.
- **Public reverse proxy + cert** — only if it must be reachable beyond the corp
  network; pair a domain + TLS cert with SSO in front.

Whatever the front door, keep the app bound to `127.0.0.1` (or restrict it to the
proxy host) so the only way in is through the TLS/auth layer. If your proxy runs
on a *different* host, change the api `ports` bind in `docker-compose.prod.yml`
(see the comment there) to the interface the proxy can reach.

## 6. Use it

Operators open the URL your reverse proxy serves and log in with the operator
credentials. To grant/revoke access, use whatever gate fronts it (VPN
membership, Entra ID group, etc.); rotating the operator password (below) is the
app-level backstop.

---

## Updating

From your dev machine, push changes; on the host:

```bash
git pull
docker compose -f docker-compose.prod.yml up -d --build
```

`--build` rebuilds the image and recreates only changed services. The DB volume
(`popa_wharf_pgdata_prod`) **persists**, so data survives. The one-shot `migrate`
re-runs and `alembic upgrade head` is idempotent — **new migrations apply
automatically**, existing ones are skipped. That's the whole release process.

## Operations

- **Logs:** `docker compose -f docker-compose.prod.yml logs -f [api|ais|occupancy]`
- **Stop / start:** `... stop` / `... start`. **Tear down:** `... down`
  (add `-v` to also delete the data volume — destructive).
- **Rotate the operator password:** edit `.env.prod`, then
  `docker compose -f docker-compose.prod.yml up -d` (recreates the API).
- **Backups:** the database lives in the `popa_wharf_pgdata_prod` volume.
  Schedule a dump, e.g.
  `docker compose -f docker-compose.prod.yml exec db pg_dump -U popa popa_wharf > backup.sql`.

## Known gaps (carried from PLAN.md)

- **No AIS staleness alarm.** If the feed drops, the `ais` container keeps running
  but data silently stops — there's no "last-message-age" healthcheck yet.
- **Single shared login**, no per-user accounts/roles/audit. Fine for a small
  operator team; revisit OIDC/SSO (e.g. Entra ID, per step 5) if that's needed.
