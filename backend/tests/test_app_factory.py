"""Tests for the app factory: assembly, mounting, error handlers and the root route."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import app.config as config
import pytest
from app.errors import ApiError
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException
from starlette.testclient import TestClient

# The route-sweep helper understands how FastAPI nests included routers; reuse
# it instead of teaching a second traversal the same internals.
from tests.test_security import _iter_api_routes

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"


def _skip_when_frontend_built():
    """Skip a not-built-state test when this checkout has a built frontend.

    ``GET /`` serves ``index.html`` exactly when ``dist/index.html`` exists —
    the same condition ``_install_frontend`` uses. The built state itself is
    covered unconditionally by ``test_spa_fallback_serves_index_for_client_routes``.
    """
    if (FRONTEND_DIST / "index.html").is_file():
        pytest.skip("frontend/dist is built in this checkout")


def test_create_app_wires_everything(app):
    guarded_paths = {
        route.path for route in app.state.api_router.routes if isinstance(route, APIRoute)
    }
    # the guarded router carries the settings and stream routes...
    assert {"/api/v1/settings", "/api/v1/stream"} <= guarded_paths
    # ...but health is the one auth-exempt route, so it lives on the app itself
    assert "/api/v1/health" not in guarded_paths

    api_paths = [
        path for path, _route, _deps in _iter_api_routes(app.routes) if path.startswith("/api/v1")
    ]
    assert "/api/v1/health" in api_paths
    # exactly one registration per method: a second mount of api_router would
    # double every entry (settings legitimately appears twice: GET + PATCH)
    registrations = Counter(
        (path, method)
        for path, route, _deps in _iter_api_routes(app.routes)
        if path.startswith("/api/v1")
        for method in route.methods
        if method in {"GET", "POST", "PATCH", "PUT", "DELETE"}
    )
    for key in (
        ("/api/v1/health", "GET"),
        ("/api/v1/session", "GET"),
        ("/api/v1/settings", "GET"),
        ("/api/v1/settings", "PATCH"),
        ("/api/v1/stream", "GET"),
    ):
        assert registrations[key] == 1, f"{key} must be registered exactly once"

    # the error handlers that render every failure as the envelope are installed
    for handler in (ApiError, RequestValidationError, HTTPException, Exception):
        assert handler in app.exception_handlers


def test_health_is_unguarded_and_settings_is_guarded(client):
    """Behavioural twin of the wiring test: health answers without a cookie,
    the guarded routes answer 401 without one."""
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/settings").status_code == 401
    assert client.get("/api/v1/stream").status_code == 401


def test_root_reports_missing_frontend_build(client):
    _skip_when_frontend_built()

    r = client.get("/")

    assert r.status_code == 200
    payload = r.json()["data"]
    assert payload["status"] == "frontend_not_built"
    assert "not been built" in payload["message"]
    assert "dist" in payload["message"]


def test_root_still_answers_the_token_exchange(client):
    """The helpful message must not swallow ``?t=``: a wrong token still gets
    the plain probe response and never a cookie."""
    r = client.get("/", params={"t": "nope"}, follow_redirects=False)

    assert r.status_code == 200
    assert "set-cookie" not in r.headers
    assert r.json() == {"data": {"status": "ok"}, "meta": {}}


def test_root_message_is_a_valid_success_envelope(client):
    _skip_when_frontend_built()

    r = client.get("/")

    body = r.json()
    assert set(body) == {"data", "meta"}
    assert isinstance(body["data"], dict)
    assert json.dumps(body["data"])  # JSON-serialisable, human-readable text


def test_spa_fallback_serves_index_for_client_routes(tmp_path, monkeypatch):
    """With a built dist, ``GET /`` and extension-less deep links serve
    ``index.html``; missing assets and unknown API paths keep real 404s.

    Runs unconditionally against a temp ``FRONTEND_DIST`` (monkeypatched and
    restored by pytest), so the suite exercises the built state even in a
    checkout where ``frontend/dist`` does not exist.
    """
    dist = tmp_path / "dist"
    dist.mkdir()
    index_body = "<!doctype html><html><body>AgentScope SPA</body></html>"
    (dist / "index.html").write_text(index_body, encoding="utf-8")

    # Patch the env first: importing app.main builds a module-level app that
    # must land in the temp home, never the real app data directory.
    monkeypatch.setenv("AGENTSCOPE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(config, "SETTINGS", None)
    import app.main as main

    monkeypatch.setattr(main, "FRONTEND_DIST", dist)
    spa = TestClient(main.create_app(), raise_server_exceptions=False)

    root = spa.get("/")
    assert root.status_code == 200
    assert "text/html" in root.headers["content-type"]
    assert root.text == index_body

    deep_link = spa.get("/some/client/route")
    assert deep_link.status_code == 200
    assert "text/html" in deep_link.headers["content-type"]
    assert deep_link.text == index_body  # SPA fallback, not a JSON 404

    missing_asset = spa.get("/missing.js")
    assert missing_asset.status_code == 404
    assert missing_asset.json()["error"]["code"] == "not_found"

    unknown_api = spa.get("/api/v1/does-not-exist")
    assert unknown_api.status_code == 404
    assert unknown_api.json()["error"]["code"] == "not_found"
