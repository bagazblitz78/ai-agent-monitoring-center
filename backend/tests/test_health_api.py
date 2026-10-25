"""Tests for the public health endpoint: exact shape, version and degradation."""

from __future__ import annotations

from pathlib import Path

import pytest
from app import db
from sqlalchemy import text


@pytest.fixture
def fresh_db(client):
    """Point the lazy engine at this test's temp home before probing it."""
    db.reset_engine()
    yield client
    db.reset_engine()


def test_health_is_unauthenticated_and_reports_exact_shape(fresh_db):
    r = fresh_db.get("/api/v1/health")

    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"data", "meta"}
    assert body["meta"] == {}

    data = body["data"]
    assert set(data) == {"status", "version", "database", "uptime_seconds", "engine"}
    assert data["status"] == "ok"
    assert data["version"] == "0.1.0"

    database = data["database"]
    assert set(database) == {"ok", "path", "schema_version"}
    assert database["ok"] is True
    assert Path(database["path"]).is_absolute()
    # fresh temporary database: no migrations have been applied
    assert database["schema_version"] is None

    assert isinstance(data["uptime_seconds"], float)
    assert data["uptime_seconds"] >= 0
    engine = data["engine"]
    assert set(engine) == {"queue_depth", "events_processed", "sse_clients"}
    assert engine["queue_depth"] == 0 and engine["events_processed"] == 0
    # Live value from the hub attached by create_app; an int >= 0 either way.
    assert isinstance(engine["sse_clients"], int)
    assert engine["sse_clients"] >= 0


def test_health_sse_clients_reflect_attached_hub(fresh_db):
    """With a hub on ``app.state`` the probe reports its live subscriber count."""
    from app.engine.hub import EventHub

    fresh_db.app.state.hub = EventHub()

    assert fresh_db.get("/api/v1/health").json()["data"]["engine"]["sse_clients"] == 0

    queue = fresh_db.app.state.hub.subscribe()
    try:
        r = fresh_db.get("/api/v1/health")
        assert r.json()["data"]["engine"]["sse_clients"] == 1
    finally:
        fresh_db.app.state.hub.unsubscribe(queue)


def test_health_sse_clients_zero_when_no_hub_is_attached(fresh_db):
    """A bare app without a hub degrades the count to 0 instead of failing."""
    del fresh_db.app.state.hub

    r = fresh_db.get("/api/v1/health")

    assert r.status_code == 200
    assert r.json()["data"]["engine"]["sse_clients"] == 0


def test_health_reports_alembic_schema_version(fresh_db):
    with db.new_session() as session:
        session.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        session.execute(text("INSERT INTO alembic_version (version_num) VALUES ('0003')"))
        session.commit()

    r = fresh_db.get("/api/v1/health")

    assert r.status_code == 200
    assert r.json()["data"]["database"]["schema_version"] == "0003"


def test_health_reports_degraded_when_database_is_unreachable(fresh_db, monkeypatch):
    from app.api import health

    def explode():
        raise OSError("database is gone")

    monkeypatch.setattr(health, "new_session", explode)

    r = fresh_db.get("/api/v1/health")

    assert r.status_code == 200
    data = r.json()["data"]
    assert data["status"] == "degraded"
    assert data["database"]["ok"] is False
    assert data["database"]["schema_version"] is None
