"""M3 acceptance: three real directories, live monitoring, read-only guarantee.

Automates the milestone's acceptance paragraph:

1. three project directories — a git repository, a plain non-git directory and
   a plain directory under the system temp dir;
2. all three registered through the API;
3. with the engine running, a file change in the git project and one in the
   non-git project show up in ``file-changes``, and a new commit shows up in
   ``git_commits`` while the non-git project stays out of it;
4. every file in all three directories is byte- and mtime-identical before
   registration and after all monitoring activity, apart from the files this
   test itself wrote (whose content is exactly what it wrote) — and, once the
   test stops writing, monitoring alone changes nothing at all;
5. a permission-denied path is reported as such and the server stays healthy.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest
from app import db
from app.models import GitCommit
from fastapi.testclient import TestClient
from git import Repo
from sqlalchemy import func, select

PROJECTS = "/api/v1/projects"
SESSION_COOKIE = "agentscope_session"
WIN = os.name == "nt"


def wait_for(predicate, *, timeout: float = 5.0, interval: float = 0.1) -> bool:
    """Poll ``predicate`` until it is true or ``timeout`` seconds have passed."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def _snapshot(roots: dict[str, Path]) -> dict[str, tuple[int, int, str]]:
    """mtime_ns + size + sha256 of every file under every root, keyed by ``root/rel``."""
    out: dict[str, tuple[int, int, str]] = {}
    for name, root in roots.items():
        for path in sorted(root.rglob("*")):
            if path.is_file():
                stat = path.stat()
                out[f"{name}/{path.relative_to(root).as_posix()}"] = (
                    stat.st_mtime_ns,
                    stat.st_size,
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
    return out


def _register(client, name: str, root: Path) -> dict:
    r = client.post(PROJECTS, json={"name": name, "path": str(root)})
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _changes(client, project_id: str) -> list[dict]:
    r = client.get(f"{PROJECTS}/{project_id}/file-changes")
    assert r.status_code == 200, r.text
    return r.json()["data"]["items"]


def _commit_count(project_id: str) -> int:
    with db.new_session() as session:
        return session.execute(
            select(func.count(GitCommit.id)).where(GitCommit.project_id == project_id)
        ).scalar_one()


def _has_sha(project_id: str, sha: str) -> bool:
    with db.new_session() as session:
        rows = session.execute(
            select(GitCommit.sha).where(GitCommit.project_id == project_id)
        ).scalars()
        return sha in set(rows)


@pytest.fixture
def three_projects(tmp_path):
    """The git repo and the plain directory on ``tmp_path``, plus a system-temp dir.

    The system-temp directory lives outside ``tmp_path`` (and outside pytest's
    cleanup), so the test removes it itself.
    """
    git_root = tmp_path / "git-project"
    git_root.mkdir()
    plain_root = tmp_path / "plain-project"
    plain_root.mkdir()
    temp_root = Path(tempfile.mkdtemp(prefix="agentscope-acceptance-"))
    temp_root.mkdir(exist_ok=True)

    repo = Repo.init(git_root)
    with repo.config_writer() as writer:
        writer.set_value("user", "name", "Test User")
        writer.set_value("user", "email", "tester@example.com")
    (git_root / "initial.txt").write_text("one\n", encoding="utf-8")
    repo.index.add(["initial.txt"])
    repo.index.commit("initial commit")

    try:
        yield git_root, plain_root, temp_root, repo
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def test_three_projects_go_live_and_monitoring_never_writes(
    monitored_app, three_projects, tmp_path
):
    git_root, plain_root, temp_root, repo = three_projects
    roots = {"git": git_root, "plain": plain_root, "temp": temp_root}

    # --- 4 (part 1): snapshot before anything is registered -----------------
    before = _snapshot(roots)

    # --- 2: register all three through the API (engine not started yet) -----
    client = TestClient(monitored_app, raise_server_exceptions=False)
    client.cookies.set(SESSION_COOKIE, monitored_app.state.session_token)
    git_project = _register(client, "git-project", git_root)
    plain_project = _register(client, "plain-project", plain_root)
    temp_project = _register(client, "temp-project", temp_root)

    assert git_project["is_git"] is True
    assert plain_project["is_git"] is False
    assert temp_project["is_git"] is False

    listed = client.get(PROJECTS, params={"status": "all"}).json()["data"]
    assert {p["id"] for p in listed} >= {
        git_project["id"],
        plain_project["id"],
        temp_project["id"],
    }

    # --- 3: start the engine, then act like a developer ---------------------
    with TestClient(monitored_app, raise_server_exceptions=False) as live:
        live.cookies.set(SESSION_COOKIE, monitored_app.state.session_token)
        supervisor = monitored_app.state.supervisor
        assert len(supervisor._monitors) == 3  # pre-registered projects picked up

        # bytes so the assertion below is byte-exact (text mode would add \r)
        (git_root / "watched.txt").write_bytes(b"git side\n")
        (plain_root / "touched.txt").write_bytes(b"plain side\n")

        assert wait_for(
            lambda: len(_changes(live, git_project["id"])) >= 1
            and len(_changes(live, plain_project["id"])) >= 1,
            timeout=5.0,
        ), "file changes must surface in the API within five seconds"
        assert _changes(live, temp_project["id"]) == []  # nothing was written there

        (git_root / "committed.txt").write_bytes(b"committed\n")
        repo.index.add(["committed.txt"])
        sha = repo.index.commit("acceptance commit").hexsha

        assert wait_for(
            lambda: _has_sha(git_project["id"], sha), timeout=15.0
        ), "the new commit must land in git_commits"
        assert _commit_count(plain_project["id"]) == 0  # non-git project untouched

        # --- 4 (part 2): once the test stops writing, monitoring alone must
        #     not change a single byte anywhere (a full git poll is 5 s).
        settled = _snapshot(roots)
        time.sleep(6.0)
        final = _snapshot(roots)

    assert final == settled, (
        "monitoring modified the projects: "
        + str(
            sorted(
                key
                for key in set(final) | set(settled)
                if settled.get(key) != final.get(key)
            )
        )
    )

    # --- 4 (part 3): the only changes since the snapshot are our own writes --
    ours = {"git/watched.txt", "git/committed.txt", "plain/touched.txt"}
    git_internals = [key for key in final if key.startswith("git/.git/")]
    unexpected = sorted(
        key
        for key in set(before) | set(final)
        if key not in ours
        and key not in git_internals  # the test's own commit rewrote .git
        and before.get(key) != final.get(key)
    )
    assert not unexpected, f"unexpected differences: {unexpected}"
    assert final["git/watched.txt"][2] == hashlib.sha256(b"git side\n").hexdigest()
    assert final["git/committed.txt"][2] == hashlib.sha256(b"committed\n").hexdigest()
    assert final["plain/touched.txt"][2] == hashlib.sha256(b"plain side\n").hexdigest()
    assert set(final) - set(before) <= ours | set(git_internals)


def test_project_on_another_drive_is_watched(monitored_app, tmp_path):
    """The system-temp registration (plan: skipped when it shares ``tmp_path``'s drive)."""
    other = Path(tempfile.mkdtemp(prefix="agentscope-other-drive-"))
    try:
        if other.drive.lower() == tmp_path.drive.lower():
            pytest.skip(
                f"tmp_path and the system temp dir are both on {tmp_path.drive or 'C:'}"
            )

        client = TestClient(monitored_app, raise_server_exceptions=False)
        client.cookies.set(SESSION_COOKIE, monitored_app.state.session_token)
        project = _register(client, "other-drive", other)

        with TestClient(monitored_app, raise_server_exceptions=False) as live:
            live.cookies.set(SESSION_COOKIE, monitored_app.state.session_token)
            (other / "crossed.txt").write_text("other drive\n", encoding="utf-8")
            assert wait_for(
                lambda: len(_changes(live, project["id"])) >= 1, timeout=5.0
            ), "a project on another drive must be watched like any other"
    finally:
        shutil.rmtree(other, ignore_errors=True)


@pytest.mark.skipif(
    WIN, reason="Windows chmod does not remove read access for the owner"
)
def test_permission_denied_path_is_reported_and_server_stays_healthy(client, session, tmp_path):
    """Real ``chmod 000``: the API answers ``permission_denied``, health stays 200."""
    locked = tmp_path / "locked"
    locked.mkdir()
    os.chmod(locked, 0o000)
    try:
        r = client.post(PROJECTS, json={"name": "locked", "path": str(locked)})
        assert r.status_code == 422
        details = r.json()["error"]["details"]
        assert "permission_denied" in [item["code"] for item in details]

        health = client.get("/api/v1/health")
        assert health.status_code == 200
        assert health.json()["data"]["status"] == "ok"
    finally:
        os.chmod(locked, 0o755)


def test_permission_denied_from_stat_is_reported(client, session, tmp_path, monkeypatch):
    """The monkeypatch variant: runs on every platform, including Windows."""
    from app.services import paths as paths_service

    locked = tmp_path / "locked"
    locked.mkdir()

    def deny(path):
        if Path(path) == locked:
            raise PermissionError(13, "Permission denied")
        return Path(path).exists()

    monkeypatch.setattr(paths_service, "_exists", deny)

    r = client.post(PROJECTS, json={"name": "locked", "path": str(locked)})

    assert r.status_code == 422
    details = r.json()["error"]["details"]
    assert "permission_denied" in [item["code"] for item in details]
    assert client.get("/api/v1/health").status_code == 200
