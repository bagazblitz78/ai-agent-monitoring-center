"""Tests for the local session token authentication."""

import os
import tempfile

import app.config as config
from app.config import resolve_paths
from app.security import load_or_create_token, require_session
from fastapi.routing import APIRoute
from starlette.testclient import TestClient

SESSION_COOKIE = "agentscope_session"


def _iter_api_routes(routes, prefix=""):
    """Yield ``(effective_path, route)`` for every mounted API route.

    FastAPI 0.162 nests included routers (``_IncludedRouter``) in ``app.routes``
    instead of flattening them, so recurse into included routers and mounts,
    accumulating prefixes to recover each route's effective path.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route
            continue
        included = getattr(route, "original_router", None)
        if included is not None:
            context = getattr(route, "include_context", None)
            nested_prefix = prefix + (getattr(context, "prefix", "") or "")
            yield from _iter_api_routes(included.routes, nested_prefix)
            continue
        nested = getattr(route, "routes", None)
        if nested is not None:
            mount_path = getattr(route, "path", "") or ""
            yield from _iter_api_routes(nested, prefix + mount_path)


def test_missing_cookie_is_rejected(client):
    r = client.get("/api/v1/session")

    assert r.status_code == 401
    error = r.json()["error"]
    assert error["code"] == "unauthorized"
    assert error["message"] == "Missing or invalid local session."


def test_wrong_cookie_is_rejected(client):
    client.cookies.set(SESSION_COOKIE, "nope")

    r = client.get("/api/v1/session")

    assert r.status_code == 401
    error = r.json()["error"]
    assert error["code"] == "unauthorized"
    assert error["message"] == "Missing or invalid local session."


def test_valid_cookie_is_accepted(client):
    token = client.app.state.session_token
    client.cookies.set(SESSION_COOKIE, token)

    r = client.get("/api/v1/session")

    assert r.status_code == 200
    data = r.json()["data"]
    assert data["authenticated"] is True
    assert data["mode"] == "token"
    assert data["token_preview"] == token[:6] + "…"


def test_token_exchange_sets_cookie_and_redirects(client):
    token = client.app.state.session_token

    r = client.get("/", params={"t": token}, follow_redirects=False)

    assert r.status_code == 307
    assert r.headers["location"] == "/"
    assert (
        r.headers["set-cookie"]
        == f"agentscope_session={token}; HttpOnly; Max-Age=86400; Path=/; SameSite=strict"
    )


def test_bad_token_exchange_does_not_set_cookie(client):
    r = client.get("/", params={"t": "nope"}, follow_redirects=False)

    assert r.status_code == 200
    assert "set-cookie" not in r.headers
    assert r.json() == {"data": {"status": "ok"}, "meta": {}}


def test_token_file_created_once(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTSCOPE_HOME", str(tmp_path))
    monkeypatch.setattr(config, "SETTINGS", None)
    paths = resolve_paths()

    first = load_or_create_token(paths)
    second = load_or_create_token(paths)

    assert first == second
    assert paths.token.is_file()
    assert paths.token.read_text(encoding="utf-8") == first
    if os.name == "posix":  # owner-only permissions where the OS supports them
        assert paths.token.stat().st_mode & 0o777 == 0o600

    # an empty token file is regenerated
    paths.token.write_text("", encoding="utf-8")
    third = load_or_create_token(paths)
    assert third and third != first


def test_auth_mode_off_bypasses_check(monkeypatch):
    from app.main import create_app

    with tempfile.TemporaryDirectory() as home:
        monkeypatch.setenv("AGENTSCOPE_HOME", home)
        monkeypatch.setenv("AGENTSCOPE_AUTH_MODE", "off")
        monkeypatch.setattr(config, "SETTINGS", None)
        client = TestClient(create_app(), raise_server_exceptions=False)

        without_cookie = client.get("/api/v1/session")
        client.cookies.set(SESSION_COOKIE, "nope")
        with_wrong_cookie = client.get("/api/v1/session")

    assert without_cookie.status_code == 200
    data = without_cookie.json()["data"]
    assert data["authenticated"] is True
    assert data["mode"] == "off"
    assert with_wrong_cookie.status_code == 200


def test_every_api_route_requires_session(client):
    guarded = [
        (path, route)
        for path, route in _iter_api_routes(client.app.routes)
        if path.startswith("/api/v1") and path != "/api/v1/health"
    ]

    assert guarded, "expected at least one mounted /api/v1 route"
    for path, route in guarded:
        assert any(dep.dependency is require_session for dep in route.dependencies), path
