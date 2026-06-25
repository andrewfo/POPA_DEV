"""FastAPI app assembly. The HTTP surface itself lives in ``app/routers/`` split
by concern — read-only reads, manual berth-request intake (the phone/email
channel; ``app/intake/manual.py``), the manual edit surface for ship data and
scheduling (``app/edit.py``), and the read-only analysis surfaces (the conflict
service ``GET /conflicts`` / ``app/conflicts.py`` and AIS verification
``GET /verification`` / ``app/verification.py``). This module wires those routers
onto the app and owns what is genuinely app-level: the HTTP Basic middleware, the
OperationalError handler, the static map mount, and the ``/`` + ``/health``
endpoints.

Confirmed-vs-confirmed overlaps are blocked by the DB exclusion constraint and
surfaced as a 409 (see ``app/routers/common.py``).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import OperationalError

from app import __version__
from app.auth import BasicAuthMiddleware
from app.routers import (
    analysis_router,
    depth_router,
    edit_router,
    intake_router,
    read_only_router,
)

app = FastAPI(title="POPA Wharf Data Layer", version=__version__)

# Gate the whole app (map + reads + writes) behind HTTP Basic. No-op unless
# OPERATOR_USER/OPERATOR_PASSWORD are set, so dev and tests run open; /health
# stays exempt for container probes. See app/auth.py.
app.add_middleware(BasicAuthMiddleware)


@app.exception_handler(OperationalError)
def _db_unavailable(request: Request, exc: OperationalError) -> JSONResponse:
    """The database is down/unreachable (e.g. Postgres not started). Return a
    clean 503 the frontend can show as a banner, instead of dumping a multi-page
    connection-timeout traceback on every poll of the read-only endpoints."""
    return JSONResponse(
        status_code=503,
        content={
            "detail": "database unavailable",
            "hint": "start Postgres/PostGIS (see docker-compose.yml) and retry",
        },
    )


_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """Serve the read-only Leaflet map over the data layer."""
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}


# The HTTP surface, split by concern (see app/routers/__init__.py).
app.include_router(read_only_router)
app.include_router(intake_router)
app.include_router(edit_router)
app.include_router(analysis_router)
app.include_router(depth_router)
