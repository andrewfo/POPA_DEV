"""HTTP Basic auth gating the whole app (map + reads + writes).

Active ONLY when both ``OPERATOR_USER`` and ``OPERATOR_PASSWORD`` are configured
(see ``app/config.py``); if either is blank, auth is disabled (open) — so local
dev and the TestClient suite run without credentials, and a deployment turns it
on purely by setting the two env vars. The ``/health`` liveness route is always
exempt so a container/orchestrator healthcheck can probe without credentials.

A Starlette middleware (not a route dependency) so it also covers the mounted
static map at ``/`` and ``/static/*`` — mounted sub-apps bypass FastAPI route
dependencies. Basic auth only base64-encodes the credentials on the wire, so it
MUST run behind TLS (a reverse proxy / load balancer terminates it).
"""
from __future__ import annotations

import base64
import binascii
import secrets

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from app.config import get_settings

# Reachable without credentials even when auth is on. Liveness only — the
# container healthcheck must not need a secret to probe. /health/db stays gated.
_EXEMPT_PATHS = frozenset({"/health"})
_REALM = "POPA Wharf"


def check_credentials(auth_header: str | None, user: str, password: str) -> bool:
    """True if the request is authorized for ``user``/``password``.

    If either configured value is blank the gate is disabled — returns True for
    everything (the middleware short-circuits on this too; encoding the rule here
    keeps it unit-testable in one place). Otherwise ``auth_header`` must be a
    well-formed ``Basic`` header whose decoded ``user:password`` matches, compared
    in constant time (both fields always, to avoid short-circuit timing leaks)."""
    if not user or not password:
        return True
    if not auth_header:
        return False
    scheme, _, encoded = auth_header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return False
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    got_user, sep, got_password = decoded.partition(":")
    if not sep:
        return False
    user_ok = secrets.compare_digest(got_user, user)
    pass_ok = secrets.compare_digest(got_password, password)
    return user_ok and pass_ok


def _challenge() -> Response:
    """401 with the WWW-Authenticate header so browsers prompt (and then cache)."""
    return PlainTextResponse(
        "Authentication required",
        status_code=401,
        headers={"WWW-Authenticate": f'Basic realm="{_REALM}"'},
    )


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """Gate every request behind HTTP Basic, except the exempt liveness path.

    Credentials are read per-request from settings (so tests can override them);
    blank creds disable the gate entirely."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)
        settings = get_settings()
        user, password = settings.operator_user, settings.operator_password
        if not (user and password):
            # Auth disabled (open) — no authenticated principal to attribute
            # writes to. The audit_log actor is NULL in this mode (dev / tests).
            request.state.operator = None
            return await call_next(request)
        if not check_credentials(request.headers.get("Authorization"), user, password):
            return _challenge()
        # Authenticated. With a single shared credential the principal is always
        # the configured OPERATOR_USER; expose it on request.state so write
        # endpoints can stamp it onto audit rows (app/audit.py). When multiple
        # credentials are added later, this is the one place to derive the real
        # username from the header.
        request.state.operator = user
        return await call_next(request)
