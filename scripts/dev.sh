#!/usr/bin/env bash
#
# One-command local dev stack for the POPA wharf data layer.
#
# Brings up everything needed to see the system working:
#   1. starts the PostGIS container (docker compose) and waits until healthy
#   2. runs Alembic migrations and seeds the wharf segment (both idempotent)
#   3. launches the FastAPI app (uvicorn)
#   4. launches the live aisstream.io ingestor
#   5. (optional) loops occupancy derivation
#
# Usage:
#   ./scripts/dev.sh                  # DB + API + AIS
#   ./scripts/dev.sh --occupancy      # also run occupancy loop
#   ./scripts/dev.sh --occ-every 30   # occupancy every 30s (implies --occupancy)
#   ./scripts/dev.sh --no-api         # skip uvicorn
#   ./scripts/dev.sh --no-ais         # skip AIS ingestor
#   ./scripts/dev.sh --port 9000      # API on port 9000
#   ./scripts/dev.sh --down           # stop the DB container and exit

set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

# Activate the venv if present, otherwise pick system python3
if [[ -f "$REPO/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$REPO/.venv/bin/activate"
fi
PYTHON="${PYTHON:-$(command -v python3 || command -v python)}"
if [[ -z "$PYTHON" ]]; then
  echo "No python3 or python found on PATH." >&2
  exit 1
fi

# --- defaults ---
NO_API=false
NO_AIS=false
OCCUPANCY=false
OCC_EVERY=60
PORT=8000
DOWN=false

# --- parse args ---
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-api)      NO_API=true;    shift ;;
    --no-ais)      NO_AIS=true;    shift ;;
    --occupancy)   OCCUPANCY=true; shift ;;
    --occ-every)   OCCUPANCY=true; OCC_EVERY="$2"; shift 2 ;;
    --port)        PORT="$2";      shift 2 ;;
    --down)        DOWN=true;      shift ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# --- helpers ---
step()  { printf '\033[36m==> %s\033[0m\n' "$1"; }
ok()    { printf '\033[32m    %s\033[0m\n' "$1"; }
warn()  { printf '\033[33m!!  %s\033[0m\n' "$1"; }

PIDS=()
cleanup() {
  echo ""
  step "Shutting down background processes"
  if [[ ${#PIDS[@]} -gt 0 ]]; then
    for pid in "${PIDS[@]}"; do
      kill "$pid" 2>/dev/null && ok "stopped PID $pid" || true
    done
  fi
}
trap cleanup EXIT INT TERM

# --- down mode ---
if $DOWN; then
  step "Stopping the database container (docker compose stop)"
  docker compose stop
  ok "Stopped."
  trap - EXIT  # skip cleanup, nothing launched
  exit 0
fi

# --- 1. DB up + wait for healthy ---
step "Starting PostGIS (docker compose up -d)"
docker compose up -d

deadline=$((SECONDS + 90))
health=""
while [[ $SECONDS -lt $deadline ]]; do
  health=$(docker inspect --format '{{.State.Health.Status}}' popa_wharf_db 2>/dev/null || echo "")
  if [[ "$health" == "healthy" ]]; then break; fi
  echo "    waiting for db (health=$health)..."
  sleep 2
done
if [[ "$health" != "healthy" ]]; then
  echo "Database did not become healthy within 90s." >&2
  exit 1
fi
ok "db healthy"

# --- 2. Migrate + seed (idempotent) ---
step "alembic upgrade head"
$PYTHON -m alembic upgrade head

step "Seeding wharf segment"
$PYTHON -m app.seed.wharf_seed

# --- 3. Read AIS key from .env ---
AIS_KEY=""
if [[ -f .env ]]; then
  AIS_KEY=$(grep -E '^\s*AISSTREAM_API_KEY\s*=' .env | head -1 | sed 's/^[^=]*=\s*//' | xargs) || true
fi

RUN_AIS=false
if ! $NO_AIS; then
  if [[ -n "$AIS_KEY" ]]; then
    RUN_AIS=true
  else
    warn "AISSTREAM_API_KEY is empty in .env -> skipping the AIS ingestor."
    warn "Get a free key at https://aisstream.io, set it in .env, and re-run."
  fi
fi

# --- 4. Launch long-lived processes in background ---
if ! $NO_API; then
  step "API  -> http://localhost:$PORT"
  $PYTHON -m uvicorn app.main:app --reload --port "$PORT" &
  PIDS+=($!)
fi

if $RUN_AIS; then
  step "AIS  -> aisstream.io live feed"
  $PYTHON -m app.ais.run &
  PIDS+=($!)
fi

if $OCCUPANCY; then
  step "Occupancy derivation every ${OCC_EVERY}s"
  (while true; do $PYTHON -m app.occupancy.run; sleep "$OCC_EVERY"; done) &
  PIDS+=($!)
fi

echo ""
ok "Up. Open http://localhost:$PORT  (map) and http://localhost:$PORT/stats (live counts)."
echo "    Ctrl-C to stop all processes; './scripts/dev.sh --down' stops the DB."

# keep script alive so trap can catch Ctrl-C
wait
