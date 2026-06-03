# Deploying the POPA Wharf Data Layer (Tailscale)

This runs the full production stack on a Linux host and serves it to your
operators over a private **Tailscale** network — encrypted end to end, no public
ports, free HTTPS, **no domain required**. The right fit for an internal tool
used by a handful of people.

For the artifacts themselves (image, compose, auth) see the **Deploy** section of
[`README.md`](./README.md); this file is the host + network playbook.

---

## What you need

- **A Linux host that runs 24/7** with Docker. A $4–6/mo VPS (Hetzner,
  DigitalOcean, Linode, Vultr) or an Oracle Cloud *Always Free* VM both work. It
  must stay on — the AIS ingestor is a long-lived connection; a host that sleeps
  loses the live feed.
- **~2 GB RAM** (Postgres + PostGIS + gunicorn + the AIS/occupancy workers).
- **A free Tailscale account** (<https://tailscale.com>). Each operator who needs
  access installs the Tailscale client and joins the same *tailnet*.

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
# Use main once the deployment PR is merged; until then:
#   git checkout add-deployment-packaging
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

## 5. Put it on Tailscale

Install Tailscale on the host and join your tailnet:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up        # opens a login URL; authenticate once
```

Enable HTTPS for your tailnet **once** in the admin console
(<https://login.tailscale.com/admin/dns> → enable **MagicDNS** and
**HTTPS Certificates**). Then expose the local API over the tailnet with a real
certificate:

```bash
sudo tailscale serve --bg 8000
sudo tailscale serve status        # prints the URL
```

`tailscale serve` proxies `https://<host>.<your-tailnet>.ts.net` → the
localhost-only `:8000`. Nothing is exposed publicly; the WireGuard tunnel
encrypts everything.

## 6. Use it

On any computer (or phone) signed into the same tailnet, open

```
https://<host>.<your-tailnet>.ts.net
```

and log in with the operator credentials. No install on the client beyond the
Tailscale app, no domain, no certificate warnings.

To grant an operator access: invite them to the tailnet and have them install
Tailscale. To revoke: remove them from the tailnet (and/or rotate the operator
password — see below).

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
  operator team; revisit OIDC/SSO if that's needed.
