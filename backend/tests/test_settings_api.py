"""Tests for the settings API: authentication, persistence and payload bounds."""

from __future__ import annotations

import json

import pytest
from app import db
from app.models import Base, Setting

SESSION_COOKIE = "agentscope_session"


@pytest.fixture
def authed_client(client):
    """A cookie-authenticated client backed by a freshly created schema.

    The lazy engine is reset before and after so the test only ever touches
    this test's temporary ``AGENTSCOPE_HOME``.
    """
    db.reset_engine()
    Base.metadata.create_all(db.engine)
    client.cookies.set(SESSION_COOKIE, client.app.state.session_token)
    yield client
    db.reset_engine()


def test_settings_requires_session(client):
    r = client.get("/api/v1/settings")

    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_get_returns_empty_map_on_fresh_database(authed_client):
    r = authed_client.get("/api/v1/settings")

    assert r.status_code == 200
    assert r.json() == {"data": {}, "meta": {}}


def test_patch_persists_and_get_reflects(authed_client):
    r = authed_client.patch(
        "/api/v1/settings", json={"values": {"theme": "dark", "refresh_s": 30}}
    )

    assert r.status_code == 200
    assert r.json() == {"data": {"theme": "dark", "refresh_s": 30}, "meta": {}}

    again = authed_client.get("/api/v1/settings")
    assert again.json() == {"data": {"theme": "dark", "refresh_s": 30}, "meta": {}}

    # the value really is persisted, not just echoed back
    with db.new_session() as session:
        row = session.get(Setting, "theme")
        assert row is not None
        assert json.loads(row.value_json) == "dark"


def test_patch_merges_and_replaces_existing_keys(authed_client):
    first = authed_client.patch("/api/v1/settings", json={"values": {"theme": "dark"}})
    assert first.status_code == 200

    second = authed_client.patch(
        "/api/v1/settings",
        json={"values": {"theme": "light", "filters": {"level": "warn"}}},
    )

    assert second.status_code == 200
    assert second.json()["data"] == {"theme": "light", "filters": {"level": "warn"}}


def test_patch_rejects_extra_field(authed_client):
    r = authed_client.patch(
        "/api/v1/settings", json={"values": {"theme": "dark"}, "extra": 1}
    )

    assert r.status_code == 422
    error = r.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"]


def test_patch_rejects_more_than_64_keys(authed_client):
    payload = {"values": {f"key{i}": i for i in range(65)}}

    r = authed_client.patch("/api/v1/settings", json=payload)

    assert r.status_code == 422
    assert "at most 64" in str(r.json()["error"]["details"])


def test_patch_accepts_exactly_64_keys(authed_client):
    payload = {"values": {f"key{i}": i for i in range(64)}}

    r = authed_client.patch("/api/v1/settings", json=payload)

    assert r.status_code == 200
    assert len(r.json()["data"]) == 64


def test_patch_rejects_value_longer_than_4096_characters(authed_client):
    r = authed_client.patch("/api/v1/settings", json={"values": {"big": "x" * 5000}})

    assert r.status_code == 422
    assert "4096" in str(r.json()["error"]["details"])


def test_patch_rejects_empty_values(authed_client):
    r = authed_client.patch("/api/v1/settings", json={"values": {}})

    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"
