"""Tests for project registration and management (M3, Task 4).

Covers the full CRUD surface plus archive/unarchive and refresh. The delete
test snapshots every file (mtime, size, hash) before and after: unregistering a
project must leave the directory byte-for-byte untouched.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from app.models import Project
from app.services.paths import path_key_for
from git import Repo
from sqlalchemy import func, select

PROJECTS = "/api/v1/projects"


@pytest.fixture
def project_dir(tmp_path):
    root = tmp_path / "alpha"
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


def _create(client, name: str, path: Path, **extra):
    return client.post(PROJECTS, json={"name": name, "path": str(path), **extra})


def _snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    """mtime_ns + size + sha256 for every file under ``root``."""
    out: dict[str, tuple[int, int, str]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            stat = path.stat()
            out[str(path.relative_to(root))] = (
                stat.st_mtime_ns,
                stat.st_size,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
    return out


def _count(session) -> int:
    return session.execute(select(func.count(Project.id))).scalar_one()


# --- create ---------------------------------------------------------------


def test_create_returns_full_resource(client, session, project_dir):
    r = _create(client, "alpha", project_dir, watch_enabled=False, in_project_reporting=True)

    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"data", "meta"}
    data = body["data"]
    for key in (
        "id",
        "name",
        "path",
        "path_key",
        "is_git",
        "default_branch",
        "current_branch",
        "watch_enabled",
        "watch_rules_json",
        "in_project_reporting",
        "status",
        "created_at",
        "updated_at",
        "archived_at",
    ):
        assert key in data, key
    assert data["name"] == "alpha"
    assert data["path"] == str(project_dir)
    assert data["path_key"] == path_key_for(project_dir)
    assert data["status"] == "active"
    assert data["watch_enabled"] is False
    assert data["in_project_reporting"] is True
    assert data["created_at"].endswith("Z")
    assert _count(session) == 1


def test_create_git_project_records_branch(client, session, git_dir):
    r = _create(client, "repo", git_dir)

    assert r.status_code == 200
    data = r.json()["data"]
    assert data["is_git"] is True
    assert data["current_branch"]
    assert data["default_branch"] is None


def test_create_non_git_project(client, session, project_dir):
    r = _create(client, "plain", project_dir)

    assert r.status_code == 200
    data = r.json()["data"]
    assert data["is_git"] is False
    assert data["current_branch"] is None
    assert data["default_branch"] is None


def test_create_duplicate_path_is_conflict(client, session, project_dir):
    assert _create(client, "first", project_dir).status_code == 200

    r = _create(client, "second", project_dir)

    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"
    assert _count(session) == 1


def test_create_traversal_path_rejected_with_error_codes(client, session, project_dir):
    r = _create(client, "evil", project_dir / ".." / "escape")

    assert r.status_code == 422
    error = r.json()["error"]
    assert error["code"] == "validation_error"
    codes = [detail.get("code") for detail in error["details"]]
    assert "traversal" in codes
    assert _count(session) == 0


def test_create_missing_directory_rejected(client, session, tmp_path):
    r = _create(client, "ghost", tmp_path / "nope")

    assert r.status_code == 422
    codes = [detail.get("code") for detail in r.json()["error"]["details"]]
    assert "not_found" in codes
    assert _count(session) == 0


def test_create_rejects_extra_field(client, session, project_dir):
    r = client.post(PROJECTS, json={"name": "x", "path": str(project_dir), "bogus": 1})

    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"
    assert _count(session) == 0


@pytest.mark.parametrize("name", ["", "x" * 121])
def test_create_validates_name_length(client, session, project_dir, name):
    r = _create(client, name, project_dir)

    assert r.status_code == 422
    assert _count(session) == 0


def test_create_requires_session(client, project_dir):
    r = _create(client, "alpha", project_dir)

    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


# --- list -----------------------------------------------------------------


def test_list_pagination_and_total(client, session, tmp_path):
    for index in range(3):
        folder = tmp_path / f"p{index}"
        folder.mkdir()
        assert _create(client, f"p{index}", folder).status_code == 200

    r = client.get(PROJECTS, params={"page": 1, "page_size": 1})

    assert r.status_code == 200
    body = r.json()
    assert len(body["data"]) == 1
    assert body["meta"] == {"page": 1, "page_size": 1, "total": 3}

    r2 = client.get(PROJECTS, params={"page": 3, "page_size": 1})
    assert len(r2.json()["data"]) == 1
    assert r2.json()["meta"]["total"] == 3


def test_list_rejects_page_size_over_cap(client, session, project_dir):
    _create(client, "alpha", project_dir)

    r = client.get(PROJECTS, params={"page_size": 201})

    assert r.status_code == 422


def test_list_rejects_unknown_status(client, session, project_dir):
    _create(client, "alpha", project_dir)

    r = client.get(PROJECTS, params={"status": "weird"})

    assert r.status_code == 422


def test_list_empty_returns_empty_items(client, session):
    r = client.get(PROJECTS)

    assert r.status_code == 200
    assert r.json() == {"data": [], "meta": {"page": 1, "page_size": 50, "total": 0}}


def test_list_excludes_archived_by_default(client, session, tmp_path):
    active = tmp_path / "active"
    archived = tmp_path / "archived"
    active.mkdir()
    archived.mkdir()
    active_id = _create(client, "active", active).json()["data"]["id"]
    archived_id = _create(client, "archived", archived).json()["data"]["id"]
    assert client.post(f"{PROJECTS}/{archived_id}/archive").status_code == 200

    default = client.get(PROJECTS).json()
    assert [p["id"] for p in default["data"]] == [active_id]
    assert default["meta"]["total"] == 1

    explicit = client.get(PROJECTS, params={"status": "archived"}).json()
    assert [p["id"] for p in explicit["data"]] == [archived_id]

    everything = client.get(PROJECTS, params={"status": "all"}).json()
    assert {p["id"] for p in everything["data"]} == {active_id, archived_id}


def test_list_requires_session(client):
    assert client.get(PROJECTS).status_code == 401


# --- get ------------------------------------------------------------------


def test_get_project(client, session, project_dir):
    created = _create(client, "alpha", project_dir).json()["data"]

    r = client.get(f"{PROJECTS}/{created['id']}")

    assert r.status_code == 200
    assert r.json()["data"]["id"] == created["id"]


def test_get_unknown_project_is_404(client, session):
    r = client.get(f"{PROJECTS}/{'0' * 32}")

    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_get_requires_session(client, project_dir):
    assert client.get(PROJECTS).status_code == 401


# --- patch ----------------------------------------------------------------


def test_patch_updates_name(client, session, project_dir):
    created = _create(client, "alpha", project_dir).json()["data"]

    r = client.patch(f"{PROJECTS}/{created['id']}", json={"name": "renamed"})

    assert r.status_code == 200
    assert r.json()["data"]["name"] == "renamed"
    assert client.get(f"{PROJECTS}/{created['id']}").json()["data"]["name"] == "renamed"


def test_patch_rejects_unknown_field(client, session, project_dir):
    created = _create(client, "alpha", project_dir).json()["data"]

    r = client.patch(f"{PROJECTS}/{created['id']}", json={"bogus": 1})

    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_patch_rejects_path_change(client, session, project_dir):
    created = _create(client, "alpha", project_dir).json()["data"]
    other = tmp_other = project_dir.parent / "beta"
    other.mkdir()

    r = client.patch(f"{PROJECTS}/{created['id']}", json={"path": str(other)})

    assert r.status_code == 422
    error = r.json()["error"]
    assert error["code"] == "validation_error"
    assert any(detail.get("code") == "path_immutable" for detail in error["details"])
    assert client.get(f"{PROJECTS}/{created['id']}").json()["data"]["path"] == str(project_dir)
    assert tmp_other.exists()


def test_patch_watch_rules_json_roundtrip(client, session, project_dir):
    created = _create(client, "alpha", project_dir).json()["data"]
    rules = json.dumps({"ignore": ["*.log"], "max_file_bytes": 1_000_000})

    r = client.patch(f"{PROJECTS}/{created['id']}", json={"watch_rules_json": rules})

    assert r.status_code == 200
    assert r.json()["data"]["watch_rules_json"] == rules
    assert client.get(f"{PROJECTS}/{created['id']}").json()["data"]["watch_rules_json"] == rules


@pytest.mark.parametrize(
    "value",
    ["not json", "[]", '"a string"', "12345", "x" * 4097],
    ids=["invalid-json", "array", "string", "number", "too-long"],
)
def test_patch_watch_rules_json_rejects_bad_values(client, session, project_dir, value):
    created = _create(client, "alpha", project_dir).json()["data"]

    r = client.patch(f"{PROJECTS}/{created['id']}", json={"watch_rules_json": value})

    assert r.status_code == 422


def test_patch_rejects_null_values(client, session, project_dir):
    created = _create(client, "alpha", project_dir).json()["data"]

    r = client.patch(f"{PROJECTS}/{created['id']}", json={"name": None})

    assert r.status_code == 422


def test_patch_unknown_project_is_404(client, session, project_dir):
    r = client.patch(f"{PROJECTS}/{'0' * 32}", json={"name": "x"})

    assert r.status_code == 404


def test_patch_requires_session(client, project_dir):
    assert client.patch(f"{PROJECTS}/{'0' * 32}", json={"name": "x"}).status_code == 401


# --- archive / unarchive --------------------------------------------------


def test_archive_and_unarchive(client, session, tmp_path):
    root = tmp_path / "target"
    root.mkdir()
    created = _create(client, "target", root).json()["data"]

    archived = client.post(f"{PROJECTS}/{created['id']}/archive")
    assert archived.status_code == 200
    data = archived.json()["data"]
    assert data["status"] == "archived"
    assert data["archived_at"] is not None

    fetched = client.get(f"{PROJECTS}/{created['id']}")
    assert fetched.json()["data"]["status"] == "archived"

    restored = client.post(f"{PROJECTS}/{created['id']}/unarchive")
    assert restored.status_code == 200
    assert restored.json()["data"]["status"] == "active"
    assert restored.json()["data"]["archived_at"] is None

    listed = client.get(PROJECTS).json()
    assert [p["id"] for p in listed["data"]] == [created["id"]]


def test_archive_twice_is_idempotent(client, session, tmp_path):
    root = tmp_path / "target"
    root.mkdir()
    created = _create(client, "target", root).json()["data"]

    assert client.post(f"{PROJECTS}/{created['id']}/archive").status_code == 200
    assert client.post(f"{PROJECTS}/{created['id']}/archive").status_code == 200
    assert client.get(f"{PROJECTS}/{created['id']}").json()["data"]["status"] == "archived"


def test_archive_unknown_project_is_404(client, session):
    assert client.post(f"{PROJECTS}/{'0' * 32}/archive").status_code == 404


# --- delete ---------------------------------------------------------------


def test_delete_removes_row_and_touches_no_files(client, session, tmp_path):
    root = tmp_path / "victim"
    root.mkdir()
    (root / "keep.txt").write_text("keep me\n", encoding="utf-8")
    (root / "nested").mkdir()
    (root / "nested" / "data.bin").write_bytes(b"\x00\x01\x02")
    before = _snapshot(root)
    created = _create(client, "victim", root).json()["data"]

    r = client.delete(f"{PROJECTS}/{created['id']}")

    assert r.status_code == 200
    assert r.json() == {"data": {"deleted": True}, "meta": {}}
    assert session.get(Project, created["id"]) is None
    assert _count(session) == 0
    assert _snapshot(root) == before
    assert sorted(p.name for p in root.iterdir()) == ["keep.txt", "nested"]


def test_delete_twice_is_404(client, session, project_dir):
    created = _create(client, "alpha", project_dir).json()["data"]

    assert client.delete(f"{PROJECTS}/{created['id']}").status_code == 200
    second = client.delete(f"{PROJECTS}/{created['id']}")

    assert second.status_code == 404
    assert second.json()["error"]["code"] == "not_found"


def test_delete_unknown_project_is_404(client, session):
    assert client.delete(f"{PROJECTS}/{'0' * 32}").status_code == 404


def test_delete_requires_session(client, project_dir):
    assert client.delete(f"{PROJECTS}/{'0' * 32}").status_code == 401


# --- refresh --------------------------------------------------------------


def test_refresh_updates_branch(client, session, git_dir):
    created = _create(client, "repo", git_dir).json()["data"]
    repo = Repo(git_dir)
    repo.git.checkout("-b", "feature-refresh")
    assert created["current_branch"] != "feature-refresh"

    r = client.post(f"{PROJECTS}/{created['id']}/refresh")

    assert r.status_code == 200
    data = r.json()["data"]
    assert data["current_branch"] == "feature-refresh"
    assert data["is_git"] is True


def test_refresh_unknown_project_is_404(client, session):
    assert client.post(f"{PROJECTS}/{'0' * 32}/refresh").status_code == 404


def test_refresh_requires_session(client):
    assert client.post(f"{PROJECTS}/{'0' * 32}/refresh").status_code == 401


# --- auth sweep over every mutation --------------------------------------


@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("patch", "", {"name": "x"}),
        ("delete", "", None),
        ("post", "/archive", None),
        ("post", "/unarchive", None),
        ("post", "/refresh", None),
    ],
)
def test_mutations_require_session(client, method, suffix, body):
    url = f"{PROJECTS}/{'0' * 32}{suffix}"
    # httpx has no ``json`` argument on ``delete``; only send a body when there is one.
    r = client.delete(url) if method == "delete" else getattr(client, method)(url, json=body)

    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"
