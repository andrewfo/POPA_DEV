# Deploying the POPA Wharf Data Layer

This runs the full production stack on an **Ubuntu machine with Docker** and
serves it to your operators behind a reverse proxy that terminates TLS. The app
is just an HTTP service on a port with HTTP Basic auth; how operators reach it
(corporate VPN, internal host, Azure) is an infrastructure choice covered in
step 6.

For the artifacts themselves (image, compose, auth) see the **Deploy** section of
[`README.md`](../README.md).

---

## What you need

- **An Ubuntu machine that runs 24/7** — 22.04 or 24.04 LTS, physical, VM, or
  cloud instance. It must stay on: the AIS ingestor is a long-lived websocket; a
  host that sleeps loses the live feed.
- **~2 GB RAM and ~10 GB disk** (Postgres/PostGIS + gunicorn + the AIS/occupancy
  workers + the database volume).
- **`sudo` on the box** and a way for operators to reach it that IT allows
  (step 6).

---

## 1. Install Docker

On a clean Ubuntu host, install Docker Engine + the Compose plugin from Docker's
official convenience script, then add yourself to the `docker` group so you don't
need `sudo` for every command:

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"
```

Log out and back in (or run `newgrp docker`) so the group change takes effect,
then confirm both are present:

```bash
docker --version            # Docker Engine
docker compose version      # Compose v2 plugin (note: `docker compose`, not `docker-compose`)
```

## 2. Get the code

```bash
git clone https://github.com/andrewfo/SlackWater.git
cd SlackWater
```

## 3. Configure secrets

```bash
cp .env.example .env
nano .env
```

`.env` is gitignored — it never leaves the host. Set at minimum:

- `POSTGRES_PASSWORD` — a strong, unique password.
- `OPERATOR_USER` / `OPERATOR_PASSWORD` — the single login operators use. Setting
  **both** is what turns the app's HTTP Basic auth on. Leave them blank and the
  app runs open — never do that on a reachable host.
- `AISSTREAM_API_KEY` — your free key from <https://aisstream.io>.

Everything else in `.env` has working defaults; the AI-intake and depth knobs are
optional.

## 4. Start the stack

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

`migrate` runs once (`alembic upgrade head` + seed the wharf), then `api`, `ais`,
and `occupancy` come up and stay up (`restart: unless-stopped`, so they survive
reboots via the Docker daemon). The API is published on **`127.0.0.1:8000` only**
— not the public internet.

Confirm:

```bash
docker compose -f docker-compose.prod.yml ps      # db/api/ais/occupancy up; migrate exited 0
curl -s http://localhost:8000/health              # {"status":"ok",...}
```

## 5. Reboot survival

`restart: unless-stopped` brings the containers back after a crash, and Docker's
service starts on boot by default. Make sure that's enabled so the stack returns
after an OS reboot:

```bash
sudo systemctl enable docker
```

## 6. Expose it safely (reverse proxy + TLS)

The stack serves plain HTTP on `127.0.0.1:8000`. Put a reverse proxy in front
that (a) terminates **TLS** and (b) is reachable only over a network IT allows.
HTTP Basic only base64-encodes credentials, so TLS in front is mandatory.

A minimal on-host proxy with **Caddy** (automatic TLS) looks like:

```bash
sudo apt install -y caddy
```

`/etc/caddy/Caddyfile`:

```
wharf.internal.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

```bash
sudo systemctl restart caddy
```

Point operators at `https://wharf.internal.example.com`. Alternatives IT may
prefer: nginx/IIS ARR with an internal-CA cert over corporate VPN/LAN, or
deploying the same image to Azure behind Entra ID SSO (reuses the `portpa.com`
tenant already used for intake). Whatever the front door, keep the app bound to
`127.0.0.1` so the only way in is through the TLS/auth layer. If the proxy runs
on a **different** host, change the api `ports` bind in
`docker-compose.prod.yml` (see the comment there) to the interface the proxy can
reach.

## 7. Use it

Operators open the proxy URL and log in with the operator credentials. To
grant/revoke access, use whatever gate fronts it (VPN membership, Entra ID
group); rotating the operator password (below) is the app-level backstop.

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
- **Rotate the operator password:** edit `.env`, then
  `docker compose -f docker-compose.prod.yml up -d` (recreates the API).
- **Backups:** the database lives in the `popa_wharf_pgdata_prod` volume.
  Schedule a dump, e.g.
  `docker compose -f docker-compose.prod.yml exec db pg_dump -U popa popa_wharf > backup.sql`.

## Known gaps (carried from PLAN.md)

- **No AIS staleness alarm.** If the feed drops, the `ais` container keeps running
  but data silently stops — there's no "last-message-age" healthcheck yet.
- **Single shared login**, no per-user accounts/roles/audit. Fine for a small
  operator team; revisit OIDC/SSO (e.g. Entra ID, per step 6) if that's needed.
