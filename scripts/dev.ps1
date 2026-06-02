<#
.SYNOPSIS
    One-command local dev stack for the POPA wharf data layer.

.DESCRIPTION
    Brings up everything needed to see the system working:
      1. starts the PostGIS container (docker compose) and waits until healthy
      2. runs Alembic migrations and seeds the wharf segment (both idempotent)
      3. launches the FastAPI app (uvicorn)            -> own window "POPA API"
      4. launches the live aisstream.io ingestor        -> own window "POPA AIS"
      5. (optional) loops occupancy derivation          -> own window "POPA Occupancy"

    The API and ingestor are long-lived, so each opens in its own PowerShell
    window where you can watch its logs and Ctrl-C it independently. The DB /
    migrate / seed steps run inline here.

    The AIS ingestor is skipped automatically if AISSTREAM_API_KEY is empty in
    .env (get a free key at https://aisstream.io).

.PARAMETER NoApi
    Skip launching uvicorn.

.PARAMETER NoAis
    Skip the AIS ingestor even if a key is present.

.PARAMETER Occupancy
    Also launch a window that re-runs occupancy derivation every -OccEvery seconds.

.PARAMETER OccEvery
    Seconds between occupancy passes (default 60). Implies -Occupancy.

.PARAMETER Port
    API port (default 8000).

.PARAMETER Down
    Don't start anything; stop the DB container (docker compose stop) and exit.

.EXAMPLE
    .\scripts\dev.ps1
    Bring up DB + API + AIS (AIS auto-skips if no key).

.EXAMPLE
    .\scripts\dev.ps1 -Occupancy
    Same, plus a loop that turns berthed vessels into observed reservations.

.EXAMPLE
    .\scripts\dev.ps1 -Down
    Stop the database container.
#>
[CmdletBinding()]
param(
    [switch]$NoApi,
    [switch]$NoAis,
    [switch]$Occupancy,
    [int]$OccEvery = 60,
    [int]$Port = 8000,
    [switch]$Down
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

function Write-Step($msg)  { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)    { Write-Host "    $msg" -ForegroundColor Green }
function Write-Warn2($msg) { Write-Host "!!  $msg" -ForegroundColor Yellow }

# --- Make `docker` callable even if Docker Desktop was installed after this
#     terminal opened (its PATH entry only applies to new shells). ---
function Resolve-Docker {
    if (Get-Command docker -ErrorAction SilentlyContinue) { return }
    $candidates = @(
        "$env:ProgramFiles\Docker\Docker\resources\bin",
        "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin"
    )
    foreach ($d in $candidates) {
        if (Test-Path (Join-Path $d 'docker.exe')) {
            $env:Path = "$d;" + $env:Path
            return
        }
    }
    throw "docker not found. Is Docker Desktop installed and running?"
}
Resolve-Docker

if ($Down) {
    Write-Step "Stopping the database container (docker compose stop)"
    docker compose stop
    Write-Ok "Stopped. (Close the POPA API / AIS / Occupancy windows to stop those.)"
    return
}

# --- 1. DB up + wait for healthy ---
Write-Step "Starting PostGIS (docker compose up -d)"
docker compose up -d
$health = ''
$deadline = (Get-Date).AddSeconds(90)
do {
    try { $health = (docker inspect --format '{{.State.Health.Status}}' popa_wharf_db 2>$null) } catch { $health = '' }
    if ($health -eq 'healthy') { break }
    Write-Host "    waiting for db (health=$health)..."
    Start-Sleep -Seconds 2
} while ((Get-Date) -lt $deadline)
if ($health -ne 'healthy') { throw "Database did not become healthy within 90s." }
Write-Ok "db healthy"

# --- 2. Migrate + seed (idempotent) ---
Write-Step "alembic upgrade head"
python -m alembic upgrade head
Write-Step "Seeding wharf segment"
python -m app.seed.wharf_seed

# --- 3. Read AIS key from .env (skip ingestor if empty) ---
$aisKey = ''
if (Test-Path .env) {
    $m = Select-String -Path .env -Pattern '^\s*AISSTREAM_API_KEY\s*=\s*(\S.*)$'
    if ($m) { $aisKey = $m.Matches[0].Groups[1].Value.Trim() }
}
$runAis = (-not $NoAis) -and -not [string]::IsNullOrWhiteSpace($aisKey)
if (-not $NoAis -and [string]::IsNullOrWhiteSpace($aisKey)) {
    Write-Warn2 "AISSTREAM_API_KEY is empty in .env -> skipping the AIS ingestor."
    Write-Warn2 "Get a free key at https://aisstream.io, set it in .env, and re-run."
}

# --- 4. Launch long-lived processes, each in its own window ---
function Start-DevWindow($title, $command) {
    $inner = "`$Host.UI.RawUI.WindowTitle='$title'; Set-Location '$repo'; $command"
    Start-Process powershell -ArgumentList @('-NoExit', '-Command', $inner) | Out-Null
}

if (-not $NoApi) {
    Write-Step "API  -> http://localhost:$Port   (window: POPA API)"
    Start-DevWindow 'POPA API' "python -m uvicorn app.main:app --reload --port $Port"
}
if ($runAis) {
    Write-Step "AIS  -> aisstream.io live feed     (window: POPA AIS)"
    Start-DevWindow 'POPA AIS' "python -m app.ais.run"
}
if ($Occupancy -or $PSBoundParameters.ContainsKey('OccEvery')) {
    Write-Step "Occupancy derivation every ${OccEvery}s  (window: POPA Occupancy)"
    Start-DevWindow 'POPA Occupancy' "while (`$true) { python -m app.occupancy.run; Start-Sleep -Seconds $OccEvery }"
}

Write-Host ""
Write-Ok "Up. Open http://localhost:$Port  (map) and http://localhost:$Port/stats (live counts)."
Write-Host "    Stop: close the spawned windows; '.\scripts\dev.ps1 -Down' stops the DB." -ForegroundColor DarkGray
