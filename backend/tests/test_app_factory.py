"""Tests for the app factory: assembly, mounting, error handlers and the root route."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
from app.errors import ApiError
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException

# The route-sweep helper understands how FastAPI nests included routers; reuse
# it instead of teaching a second traversal the same internals.
from tests.test_security import _iter_api_routes

REPO_ROOT = Path(__file__).resolve().parents[2]


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
    dist = REPO_ROOT / "frontend" / "dist"
    if dist.is_dir():
        pytest.skip("frontend/dist exists in this checkout")

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
    r = client.get("/")

    body = r.json()
    assert set(body) == {"data", "meta"}
    assert isinstance(body["data"], dict)
    assert json.dumps(body["data"])  # JSON-serialisable, human-readable text
