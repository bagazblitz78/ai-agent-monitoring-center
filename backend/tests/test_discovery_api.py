"""Tests for the project discovery endpoints (find + batch register).

``POST /api/v1/projects/discover`` is a *read*: it always answers 200 and puts
the path policy's verdict in ``errors``/``messages`` so a caller can show why a
root is unusable (422 stays reserved for a malformed body, exactly like
``/projects/inspect``).

``POST /api/v1/projects/discover/register`` is a *bounded batch write*: one bad
path never aborts the rest, a path that is already registered is **skipped**
rather than answered with 409, and the monitors are refreshed once for the whole
batch. Not a single file may appear in the directories it touches — every test
here snapshots the tree.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from app.models import Project
from app.services.paths import ERROR_MESSAGES, path_key_for
from git import Repo
from sqlalchemy import func, select

PROJECTS = "/api/v1/projects"
DISCOVER = f"{PROJECTS}/discover"
REGISTER = f"{DISCOVER}/register"
SESSION_COOKIE = "agentscope_session"
# The fixture points AGENTSCOPE_HOME inside this temp root: the app's own data
# directory is where rows are *supposed* to change, so it is never part of the
# "no writes" snapshot of the scanned tree.
APP_HOME_DIR = "agentscope-home"


@pytest.fixture
def workspace(tmp_path):
    """A root holding a git project, an agent-marker folder and a plain folder."""
    repo_root = tmp_path / "alpha"
    repo_root.mkdir()
    repo = Repo.init(repo_root)
    with repo.config_writer() as writer:
        writer.set_value("user", "name", "Test User")
        writer.set_value("user", "email", "tester@example.com")
    (repo_root / "a.txt").write_text("one\n", encoding="utf-8")
    repo.index.add(["a.txt"])
    repo.index.commit("initial commit")

    (tmp_path / "chat" / ".opencode").mkdir(parents=True)
    (tmp_path / "plain").mkdir()
    return tmp_path


def _count(session) -> int:
    return session.execute(select(func.count(Project.id))).scalar_one()


def _tree_state(root: Path) -> dict[str, tuple[int, str]]:
    """Every file's size and content hash — byte for byte, additions included.

    Files under :data:`APP_HOME_DIR` are skipped (the database legitimately
    grows when a row is written); everything else must come back identical.
    """
    state: dict[str, tuple[int, str]] = {}
    for path in sorted(root.rglob("*")):
        rel_parts = path.relative_to(root).parts
        if rel_parts and rel_parts[0] == APP_HOME_DIR:
            continue
        rel = str(path.relative_to(root))
        if path.is_dir():
            state[rel] = (-1, "dir")
            continue
        data = path.read_bytes()
        state[rel] = (len(data), hashlib.sha256(data).hexdigest())
    return state


def _names(data: dict) -> list[str]:
    return [candidate["name"] for candidate in data["candidates"]]


# --- POST /api/v1/projects/discover ----------------------------------------


def test_discover_reports_the_projects_under_a_root(client, session, workspace):
    r = client.post(DISCOVER, json={"root": str(workspace)})

    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"data", "meta"}
    data = body["data"]
    assert set(data) == {
        "input",
        "root",
        "path_key",
        "depth",
        "scanned",
        "truncated",
        "candidates",
        "errors",
        "messages",
    }
    assert data["input"] == str(workspace)
    assert data["errors"] == [] and data["messages"] == []
    assert data["truncated"] is False
    assert _names(data) == ["alpha", "chat"]
    assert [c["reason"] for c in data["candidates"]] == ["git", ".opencode/"]
    assert set(data["candidates"][0]) == {
        "path",
        "path_key",
        "name",
        "reason",
        "already_registered",
    }


def test_discover_flags_projects_that_are_already_registered(client, session, workspace):
    session.add(
        Project(
            name="alpha",
            path=str(workspace / "alpha"),
            path_key=path_key_for(workspace / "alpha"),
            is_git=True,
        )
    )
    session.commit()

    data = client.post(DISCOVER, json={"root": str(workspace)}).json()["data"]

    assert [c["already_registered"] for c in data["candidates"]] == [True, False]


def test_discover_honours_the_depth_body_field(client, session, workspace):
    deep = workspace / "a" / "b" / "deep"
    deep.mkdir(parents=True)
    (deep / "CLAUDE.md").write_text("rules\n", encoding="utf-8")

    shallow = client.post(DISCOVER, json={"root": str(workspace), "depth": 1}).json()["data"]
    full = client.post(DISCOVER, json={"root": str(workspace), "depth": 4}).json()["data"]

    assert "deep" not in _names(shallow)
    assert "deep" in _names(full)


def test_discover_always_answers_200_with_readable_errors(client, session, tmp_path):
    missing = client.post(DISCOVER, json={"root": str(tmp_path / "nope")})
    relative = client.post(DISCOVER, json={"root": "some/relative/dir"})

    for r, code in ((missing, "not_found"), (relative, "not_absolute")):
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["errors"] == [code]
        assert data["messages"] == [ERROR_MESSAGES[code]]
        assert data["messages"] != data["errors"]  # prose, not codes
        assert data["candidates"] == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"root": str(Path.cwd()), "bogus": 1},
        {"root": 123},
        {"root": str(Path.cwd()), "depth": "deep"},
    ],
)
def test_discover_rejects_a_malformed_body(client, session, body):
    r = client.post(DISCOVER, json=body)

    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_discover_requires_a_session(client, workspace):
    client.cookies.delete(SESSION_COOKIE)

    r = client.post(DISCOVER, json={"root": str(workspace)})

    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_discover_route_is_not_captured_by_the_project_id_routes(client, session, workspace):
    r = client.post(DISCOVER, json={"root": str(workspace)})

    # /api/v1/projects/{project_id} exists as GET/PATCH/DELETE: the discovery
    # POST must land on its own route instead of a 404/405 from the shadow.
    body = r.json()
    assert r.status_code == 200
    assert "candidates" in body["data"]
    assert "error" not in body


def test_discover_creates_no_rows_and_no_files(client, session, workspace):
    rows_before = _count(session)
    state_before = _tree_state(workspace)

    r = client.post(DISCOVER, json={"root": str(workspace)})
    session.expire_all()

    assert r.status_code == 200
    assert _count(session) == rows_before == 0
    assert _tree_state(workspace) == state_before


# --- POST /api/v1/projects/discover/register -------------------------------


def test_register_adds_every_path_it_is_given(client, session, workspace):
    paths = [str(workspace / "alpha"), str(workspace / "chat")]

    r = client.post(REGISTER, json={"paths": paths})
    session.expire_all()

    assert r.status_code == 200
    data = r.json()["data"]
    assert data["counts"] == {"added": 2, "skipped": 0, "failed": 0}
    assert data["skipped"] == [] and data["failed"] == []
    assert _count(session) == 2
    added = {project["path"]: project for project in data["added"]}
    assert set(added) == set(paths)
    assert added[paths[0]]["name"] == "alpha" and added[paths[0]]["is_git"] is True
    assert added[paths[1]]["name"] == "chat" and added[paths[1]]["is_git"] is False


def test_register_skips_duplicates_instead_of_conflicting(client, session, workspace):
    alpha = str(workspace / "alpha")
    first = client.post(REGISTER, json={"paths": [alpha]})
    session.expire_all()

    second = client.post(REGISTER, json={"paths": [alpha]})

    assert first.json()["data"]["counts"]["added"] == 1
    assert second.status_code == 200  # not 409: a batch must not be all-or-nothing
    assert second.json()["data"]["counts"] == {"added": 0, "skipped": 1, "failed": 0}
    assert second.json()["data"]["skipped"] == [
        {"path": alpha, "reason": "already_registered"}
    ]
    session.expire_all()
    assert _count(session) == 1

    # the single-register route keeps its documented 409 for the same path
    r = client.post(PROJECTS, json={"name": "again", "path": alpha})
    assert r.status_code == 409


def test_register_keeps_going_after_a_bad_path(client, session, workspace):
    missing = str(workspace / "nope")

    r = client.post(REGISTER, json={"paths": [missing, str(workspace / "alpha")]})
    session.expire_all()

    assert r.status_code == 200
    data = r.json()["data"]
    assert data["counts"] == {"added": 1, "skipped": 0, "failed": 1}
    assert data["failed"] == [{"path": missing, "errors": ["not_found"]}]
    assert _count(session) == 1


def test_register_deduplicates_within_one_batch(client, session, workspace):
    alpha = str(workspace / "alpha")

    r = client.post(REGISTER, json={"paths": [alpha, alpha]})
    session.expire_all()

    assert r.status_code == 200
    assert r.json()["data"]["counts"] == {"added": 1, "skipped": 1, "failed": 0}
    assert _count(session) == 1


def test_register_empty_batch_is_a_no_op(client, session):
    r = client.post(REGISTER, json={"paths": []})
    session.expire_all()

    assert r.status_code == 200
    assert r.json()["data"]["counts"] == {"added": 0, "skipped": 0, "failed": 0}
    assert _count(session) == 0


def test_register_caps_a_batch_at_one_hundred_paths(client, session):
    r = client.post(REGISTER, json={"paths": ["x"] * 101})

    assert r.status_code == 422
    error = r.json()["error"]
    assert error["code"] == "validation_error"
    assert any("paths" in str(detail.get("loc", "")) for detail in error["details"])


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"paths": "not-a-list"},
        {"paths": [1, 2]},
        {"paths": ["ok"], "bogus": 1},
    ],
)
def test_register_rejects_malformed_bodies(client, session, body):
    r = client.post(REGISTER, json=body)

    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_register_requires_a_session(client, workspace):
    client.cookies.delete(SESSION_COOKIE)

    r = client.post(REGISTER, json={"paths": [str(workspace / "alpha")]})

    assert r.status_code == 401


def test_register_route_is_not_captured_by_the_project_id_routes(client, session, workspace):
    r = client.post(REGISTER, json={"paths": [str(workspace / "alpha")]})

    assert r.status_code == 200
    assert r.json()["data"]["counts"]["added"] == 1


def test_register_starts_one_watcher_per_added_project(monitored_client, workspace):
    supervisor = monitored_client.app.state.supervisor
    assert supervisor._monitors == {}

    r = monitored_client.post(
        REGISTER, json={"paths": [str(workspace / "alpha"), str(workspace / "chat")]}
    )

    assert r.status_code == 200
    added = r.json()["data"]["added"]
    assert sorted(supervisor._monitors) == sorted(project["id"] for project in added)
    roots = {os.path.realpath(str(monitor.root)) for monitor in supervisor._monitors.values()}
    assert roots == {
        os.path.realpath(str(workspace / "alpha")),
        os.path.realpath(str(workspace / "chat")),
    }


def test_register_creates_no_files(client, session, workspace):
    state_before = _tree_state(workspace)

    r = client.post(
        REGISTER, json={"paths": [str(workspace / "alpha"), str(workspace / "chat")]}
    )

    assert r.status_code == 200
    assert _tree_state(workspace) == state_before
