"""Tests for the API error envelope, the request context and the shared schemas."""

import uuid

import app.config as config
import pytest
from app.config import Settings
from pydantic import BaseModel, ValidationError


class _ProbeBody(BaseModel):
    """Body model for the probe endpoint used to trigger a 422 response."""

    name: str


def test_404_uses_error_envelope(client):
    r = client.get("/api/v1/does-not-exist")

    assert r.status_code == 404
    body = r.json()["error"]
    assert body["code"] == "not_found" and body["details"] == []
    assert len(body["request_id"]) == 32


def test_422_validation_error_shape(client):
    @client.app.post("/_probe_validation")
    async def probe_validation(body: _ProbeBody) -> dict[str, dict[str, str]]:
        return {"data": body.model_dump(), "meta": {}}

    r = client.post("/_probe_validation", json={"name": 123})

    assert r.status_code == 422
    error = r.json()["error"]
    assert error["code"] == "validation_error"
    assert isinstance(error["details"], list) and error["details"]
    assert "name" in str(error["details"])
    assert len(error["request_id"]) == 32


def test_request_id_header_present(client):
    r = client.get("/api/v1/does-not-exist")

    header_request_id = r.headers["X-Request-Id"]
    assert header_request_id == r.json()["error"]["request_id"]
    assert uuid.UUID(hex=header_request_id).version == 4


def test_500_hides_traceback(client, monkeypatch):
    monkeypatch.setattr(config, "SETTINGS", Settings(debug=False))

    @client.app.get("/_probe_boom")
    async def probe_boom() -> None:
        raise ValueError("boom")

    r = client.get("/_probe_boom")

    assert r.status_code == 500
    error = r.json()["error"]
    assert error["code"] == "internal_error"
    assert error["message"] == "An internal error occurred."
    assert error["details"] == []
    assert "Traceback" not in r.text
    assert "boom" not in r.text
    assert len(error["request_id"]) == 32
    assert r.headers["X-Request-Id"] == error["request_id"]


def test_500_includes_traceback_when_debug(client, monkeypatch):
    monkeypatch.setattr(config, "SETTINGS", Settings(debug=True))

    @client.app.get("/_probe_boom_debug")
    async def probe_boom_debug() -> None:
        raise ValueError("boom")

    r = client.get("/_probe_boom_debug")

    assert r.status_code == 500
    error = r.json()["error"]
    assert error["code"] == "internal_error"
    assert any("Traceback" in str(detail) for detail in error["details"])


def test_api_error_status_mapping():
    from app.errors import (
        ApiError,
        Conflict,
        Forbidden,
        NotFound,
        PayloadTooLarge,
        RateLimited,
        Unauthorized,
        ValidationFailed,
    )

    cases = [
        (ApiError, 500, "api_error"),
        (NotFound, 404, "not_found"),
        (Unauthorized, 401, "unauthorized"),
        (Forbidden, 403, "forbidden"),
        (ValidationFailed, 422, "validation_failed"),
        (Conflict, 409, "conflict"),
        (RateLimited, 429, "rate_limited"),
        (PayloadTooLarge, 413, "payload_too_large"),
    ]
    for cls, status, code in cases:
        exc = cls()
        assert isinstance(exc, ApiError) and isinstance(exc, Exception)
        assert exc.status == status
        assert exc.code == code
        assert isinstance(exc.details, list)
        assert exc.message


def test_ok_and_paged_build_success_envelopes():
    from app.schemas.common import ok, paged

    assert ok({"id": "abc"}) == {"data": {"id": "abc"}, "meta": {}}
    assert ok("x", meta={"cursor": "c1"}) == {"data": "x", "meta": {"cursor": "c1"}}
    assert paged([1, 2], 2, 50, 120) == {
        "data": [1, 2],
        "meta": {"page": 2, "page_size": 50, "total": 120},
    }


def test_page_params_enforce_bounds():
    from app.schemas.common import PageParams

    assert PageParams().page == 1
    assert PageParams().page_size == 50
    with pytest.raises(ValidationError):
        PageParams(page=0)
    with pytest.raises(ValidationError):
        PageParams(page_size=0)
    with pytest.raises(ValidationError):
        PageParams(page_size=201)
