"""Tests for the engine supervisor that owns the M3 monitors (Task 9).

Every test runs a real application instance: the supervisor only ever does its
work inside the app's lifespan, so the assertions observe the same code paths
production uses (``TestClient`` context = startup/shutdown).
"""

from __future__ import annotations

import os
from pathlib import Path

from app.engine.file_monitor import FileMonitor
from fastapi.testclient import TestClient

PROJECTS = "/api/v1/projects"
SESSION_COOKIE = "agentscope_session"


def _register(client, root: Path, name: str = "proj", **extra) -> dict:
    r = client.post(PROJECTS, json={"name": name, "path": str(root), **extra})
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _supervisor(client) -> object:
    return client.app.state.supervisor


def test_registering_starts_exactly_one_observer_for_its_root(monitored_client, tmp_path):
    root = tmp_path / "alpha"
    root.mkdir()
    supervisor = _supervisor(monitored_client)
    assert supervisor._monitors == {}

    project = _register(monitored_client, root, "alpha")

    assert list(supervisor._monitors) == [project["id"]]
    monitor = supervisor._monitors[project["id"]]
    assert isinstance(monitor, FileMonitor)
    assert Path(monitor.root) == Path(os.path.realpath(str(root)))
    assert monitor._observer is not None
    assert sum(m._observer is not None for m in supervisor._monitors.values()) == 1

    # a patch (rename) must not start a second observer for the same root
    r = monitored_client.patch(f"{PROJECTS}/{project['id']}", json={"name": "renamed"})
    assert r.status_code == 200
    assert list(supervisor._monitors) == [project["id"]]


def test_archiving_stops_the_observer(monitored_client, tmp_path):
    root = tmp_path / "alpha"
    root.mkdir()
    supervisor = _supervisor(monitored_client)
    project = _register(monitored_client, root, "alpha")
    monitor = supervisor._monitors[project["id"]]

    r = monitored_client.post(f"{PROJECTS}/{project['id']}/archive")

    assert r.status_code == 200
    assert project["id"] not in supervisor._monitors
    assert monitor._observer is None  # stopped and joined by stop()

    # unarchiving brings the watcher back
    r = monitored_client.post(f"{PROJECTS}/{project['id']}/unarchive")
    assert r.status_code == 200
    assert list(supervisor._monitors) == [project["id"]]


def test_deleting_stops_and_removes_the_observer(monitored_client, tmp_path):
    root = tmp_path / "alpha"
    root.mkdir()
    supervisor = _supervisor(monitored_client)
    project = _register(monitored_client, root, "alpha")
    monitor = supervisor._monitors[project["id"]]

    r = monitored_client.delete(f"{PROJECTS}/{project['id']}")

    assert r.status_code == 200
    assert supervisor._monitors == {}
    assert monitor._observer is None


def test_watch_disabled_project_starts_no_observer(monitored_client, tmp_path):
    root = tmp_path / "alpha"
    root.mkdir()
    supervisor = _supervisor(monitored_client)

    project = _register(monitored_client, root, "alpha", watch_enabled=False)

    assert supervisor._monitors == {}
    assert project["watch_enabled"] is False

    # turning the watch on picks the project up
    r = monitored_client.patch(f"{PROJECTS}/{project['id']}", json={"watch_enabled": True})
    assert r.status_code == 200
    assert list(supervisor._monitors) == [project["id"]]


def test_lifecycle_starts_and_stops_with_the_client_context(monitored_app, tmp_path):
    root = tmp_path / "live"
    root.mkdir()
    supervisor = monitored_app.state.supervisor

    with TestClient(monitored_app, raise_server_exceptions=False) as client:
        client.cookies.set(SESSION_COOKIE, monitored_app.state.session_token)
        project = _register(client, root, "live")
        monitor = supervisor._monitors[project["id"]]

        assert supervisor._started is True
        assert supervisor._process is not None and supervisor._process._task is not None
        assert supervisor._git is not None and supervisor._git._task is not None
        observer = monitor._observer
        assert observer is not None

    # shutdown: no observer left, no monitor task left, no leaked thread
    assert supervisor._started is False
    assert supervisor._monitors == {}
    assert supervisor._process is None and supervisor._git is None
    assert monitor._observer is None
    assert observer is not None and not observer.is_alive()


def test_double_start_is_a_no_op(monitored_app):
    import asyncio

    supervisor = monitored_app.state.supervisor

    async def scenario() -> None:
        await supervisor.start(monitored_app)
        first_process = supervisor._process
        await supervisor.start(monitored_app)
        assert supervisor._process is first_process
        await supervisor.stop()

    asyncio.run(scenario())


def test_settings_flag_keeps_the_engine_off(app, session, tmp_path):
    """``AGENTSCOPE_DISABLE_MONITORS=1`` (the unit-test default) starts nothing."""
    root = tmp_path / "alpha"
    root.mkdir()
    supervisor = app.state.supervisor

    with TestClient(app, raise_server_exceptions=False) as client:
        client.cookies.set(SESSION_COOKIE, app.state.session_token)
        _register(client, root, "alpha")

        assert supervisor._started is False
        assert supervisor._monitors == {}
        assert supervisor.stats() == {"file": 0, "process": False, "git": False}

    assert supervisor._process is None and supervisor._git is None


def test_health_reports_monitor_stats(monitored_client, tmp_path):
    root = tmp_path / "alpha"
    root.mkdir()

    _register(monitored_client, root, "alpha")
    r = monitored_client.get("/api/v1/health")

    assert r.status_code == 200
    assert r.json()["data"]["engine"]["monitors"] == {
        "file": 1,
        "process": True,
        "git": True,
    }


def test_health_without_a_started_supervisor_reports_zeros(client):
    r = client.get("/api/v1/health")

    assert r.status_code == 200
    assert r.json()["data"]["engine"]["monitors"] == {
        "file": 0,
        "process": False,
        "git": False,
    }
