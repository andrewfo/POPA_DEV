"""Tests for the HTTP Basic auth gate (app/auth.py).

Pure tests for the credential check plus TestClient tests for the middleware.
None need a database: the middleware decides before any route handler runs, and
the routes exercised either serve a static file (``/``) or are rejected (401) /
fail body validation (422) before touching the DB.
"""
from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from app.auth import check_credentials
from app.config import get_settings
from app.main import app


def _basic(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return f"Basic {token}"


# --- pure: check_credentials -------------------------------------------------

def test_correct_credentials_pass():
    assert check_credentials(_basic("op", "secret"), "op", "secret") is True


@pytest.mark.parametrize(
    "header",
    [
        None,                                              # missing
        "",                                                # empty
        "Bearer abc",                                      # wrong scheme
        "Basic",                                           # no token
        "Basic !!!not-base64!!!",                          # undecodable
        _basic("op", "wrong"),                             # bad password
        _basic("nope", "secret"),                          # bad user
        "Basic " + base64.b64encode(b"nocolon").decode(),  # no ':' separator
    ],
)
def test_bad_credentials_fail(header):
    assert check_credentials(header, "op", "secret") is False


def test_blank_config_disables_gate():
    # Either side blank => gate disabled (True regardless of the header).
    assert check_credentials(None, "", "") is True
    assert check_credentials(None, "op", "") is True
    assert check_credentials(_basic("x", "y"), "", "") is True


# --- middleware via TestClient ----------------------------------------------

@pytest.fixture
def auth_on(monkeypatch):
    """Turn the gate on by setting credentials on the cached settings instance;
    monkeypatch restores them after the test."""
    s = get_settings()
    monkeypatch.setattr(s, "operator_user", "op")
    monkeypatch.setattr(s, "operator_password", "secret")
    return TestClient(app)


def test_health_is_exempt(auth_on):
    # Liveness is reachable without credentials even when auth is on.
    assert auth_on.get("/health").status_code == 200


def test_gated_route_401_without_credentials(auth_on):
    r = auth_on.get("/")
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"].startswith("Basic")


def test_gated_route_ok_with_credentials(auth_on):
    r = auth_on.get("/", headers={"Authorization": _basic("op", "secret")})
    assert r.status_code == 200  # serves the map HTML


def test_write_rejected_without_credentials(auth_on):
    # A write is rejected at the middleware, before the handler/DB.
    r = auth_on.post("/intake/berth-request", json={})
    assert r.status_code == 401


def test_open_when_unconfigured(monkeypatch):
    # Default deployment-of-nothing: no creds => open. The request passes the
    # gate (a write reaches body validation -> 422, not a 401 from the gate).
    s = get_settings()
    monkeypatch.setattr(s, "operator_user", "")
    monkeypatch.setattr(s, "operator_password", "")
    client = TestClient(app)
    assert client.get("/").status_code == 200
    assert client.post("/intake/berth-request", json={}).status_code != 401
