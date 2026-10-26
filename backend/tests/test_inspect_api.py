"""Tests for the pre-registration and registered-project inspect endpoints (M3, Task 3).

Both routes are pure *reads*: they must never create a project row, never touch
the filesystem beyond stat/realpath, and always answer 200 with the error codes
in the payload (422 is reserved for body-shape violations).
"""

from __future__ import annotations

import pytest
from app.models import Project
from app.services.paths import path_key_for
from git import Repo
from sqlalchemy import func, select

SESSION_COOKIE = "agentscope_session"

INSPECT = "/api/v1/projects/inspect"


@pytest.fixture
def project_dir(tmp_path):
    root = tmp_path / "demo"
    root.mkdir()
    return root


@pytest.fixture
def git_dir(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    repo = Repo.init(root)
    with repo.config_writer() as writer:
        writer.set_value("user", "name", "Test User")
        writer.set_value("user", "email", "tester@example.com")
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    repo.index.add(["a.txt"])
    repo.index.commit("initial commit")
    return root


def _project_count(session) -> int:
    return session.execute(select(func.count(Project.id))).scalar_one()


# --- POST /api/v1/projects/inspect ----------------------------------------


def test_inspect_valid_directory(client, session, project_dir):
    r = client.post(INSPECT, json={"path": str(project_dir)})

    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"data", "meta"}
    data = body["data"]
    assert data["input"] == str(project_dir)
    assert data["exists"] is True
    assert data["is_directory"] is True
    assert data["readable"] is True
    assert data["is_git"] is False
    assert data["branch"] is None
    assert data["default_branch"] is None
    assert data["errors"] == []
    assert data["path_key"] == path_key_for(project_dir)


def test_inspect_reports_traversal_without_touching_path(client, session, project_dir):
    r = client.post(INSPECT, json={"path": str(project_dir / ".." / "escape")})

    assert r.status_code == 200
    data = r.json()["data"]
    assert data["errors"] == ["traversal"]
    assert data["path"] is None
    assert data["path_key"] is None
    assert data["exists"] is False


def test_inspect_reports_missing_directory(client, session, tmp_path):
    r = client.post(INSPECT, json={"path": str(tmp_path / "nope")})

    assert r.status_code == 200
    data = r.json()["data"]
    assert data["errors"] == ["not_found"]
    assert data["path"] is not None
    assert data["exists"] is False


def test_inspect_detects_git_repository(client, session, git_dir):
    r = client.post(INSPECT, json={"path": str(git_dir)})

    assert r.status_code == 200
    data = r.json()["data"]
    assert data["errors"] == []
    assert data["is_git"] is True
    assert data["branch"]
    assert data["branch"] != "HEAD detached"


def test_inspect_writes_nothing_to_the_database(client, session, project_dir):
    before = _project_count(session)
    r = client.post(INSPECT, json={"path": str(project_dir)})
    session.expire_all()

    assert r.status_code == 200
    assert _project_count(session) == before == 0


def test_inspect_creates_no_files(client, session, tmp_path):
    root = tmp_path / "clean"
    root.mkdir()
    before = sorted(p.name for p in root.iterdir())

    client.post(INSPECT, json={"path": str(root)})

    assert sorted(p.name for p in root.iterdir()) == before


def test_inspect_rejects_extra_body_fields(client, session, project_dir):
    r = client.post(INSPECT, json={"path": str(project_dir), "bogus": 1})

    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_inspect_rejects_missing_body(client, session):
    r = client.post(INSPECT, json={})

    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_inspect_requires_session(client, project_dir):
    r = client.post(INSPECT, json={"path": str(project_dir)})

    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


# --- POST /api/v1/projects/{id}/inspect -----------------------------------


def test_registered_project_reinspect(client, session, project_dir):
    project = Project(
        name="demo",
        path=str(project_dir),
        path_key=path_key_for(project_dir),
        is_git=False,
    )
    session.add(project)
    session.commit()

    r = client.post(f"/api/v1/projects/{project.id}/inspect")

    assert r.status_code == 200
    data = r.json()["data"]
    assert data["input"] == str(project_dir)
    assert data["exists"] is True
    assert data["is_git"] is False
    assert data["errors"] == []


def test_registered_project_reinspect_detects_git(client, session, git_dir):
    project = Project(
        name="repo",
        path=str(git_dir),
        path_key=path_key_for(git_dir),
        is_git=True,
        current_branch=None,
    )
    session.add(project)
    session.commit()

    r = client.post(f"/api/v1/projects/{project.id}/inspect")

    assert r.status_code == 200
    data = r.json()["data"]
    assert data["is_git"] is True
    assert data["branch"]


def test_registered_project_unknown_id_is_404(client, session, tmp_path):
    r = client.post(f"/api/v1/projects/{'0' * 32}/inspect")

    assert r.status_code == 404
    error = r.json()["error"]
    assert error["code"] == "not_found"
    assert set(error) == {"code", "message", "details", "request_id"}


def test_registered_project_reinspect_requires_session(client, session, project_dir):
    project = Project(
        name="demo",
        path=str(project_dir),
        path_key=path_key_for(project_dir),
        is_git=False,
    )
    session.add(project)
    session.commit()
    client.cookies.delete(SESSION_COOKIE)

    r = client.post(f"/api/v1/projects/{project.id}/inspect")

    assert r.status_code == 401


def test_registered_project_reinspect_creates_no_files(client, session, tmp_path):
    root = tmp_path / "untouched"
    root.mkdir()
    (root / "keep.txt").write_text("keep\n", encoding="utf-8")
    before = {
        p.name: (p.stat().st_mtime_ns, p.stat().st_size)
        for p in sorted(root.iterdir())
        if p.is_file()
    }
    project = Project(
        name="keep",
        path=str(root),
        path_key=path_key_for(root),
        is_git=False,
    )
    session.add(project)
    session.commit()

    client.post(f"/api/v1/projects/{project.id}/inspect")

    after = {
        p.name: (p.stat().st_mtime_ns, p.stat().st_size)
        for p in sorted(root.iterdir())
        if p.is_file()
    }
    assert after == before


def test_inspect_payload_paths_are_absolute(client, session):
    r = client.post(INSPECT, json={"path": "some/relative/dir"})

    assert r.status_code == 200
    data = r.json()["data"]
    assert "not_absolute" in data["errors"]
    assert data["path"] is None
