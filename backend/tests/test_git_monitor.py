"""Tests for the git monitor (M3, Task 7).

The repository fixtures are ordinary ``tmp_path`` repos created with
**repo-local** identity, and every mutation between two ``tick()`` calls (a new
commit, a new branch, a removed ``.git``) plays the role of the developer — the
monitor itself only reads, which one test asserts byte for byte.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from app import db
from app.engine.git_monitor import GitMonitor
from app.models import ActivityEvent, GitCommit, Project
from app.services.git_info import inspect_repo
from app.services.paths import path_key_for
from git import Repo
from sqlalchemy import select


@pytest.fixture
def project_repo(tmp_path):
    """A two-commit repository plus a helper that adds a third one on demand."""
    root = tmp_path / "project"
    root.mkdir()
    repo = Repo.init(root)
    with repo.config_writer() as writer:
        writer.set_value("user", "name", "Test User")
        writer.set_value("user", "email", "tester@example.com")

    (root / "a.txt").write_text("one\n", encoding="utf-8")
    (root / "b.txt").write_text("two\n", encoding="utf-8")
    repo.index.add(["a.txt", "b.txt"])
    repo.index.commit("initial commit")

    (root / "b.txt").write_text("two\nchanged\n", encoding="utf-8")
    repo.index.add(["b.txt"])
    repo.index.commit("second commit")

    def add_commit(message: str) -> str:
        (root / "c.txt").write_text(message + "\n", encoding="utf-8")
        repo.index.add(["c.txt"])
        return repo.index.commit(message).hexsha

    return repo, root, add_commit


def _register(session, root: Path, *, is_git: bool, status: str = "active") -> Project:
    """Register ``root`` the way ``POST /api/v1/projects`` does."""
    project = Project(
        name="proj",
        path=str(root),
        path_key=path_key_for(root),
        is_git=is_git,
        status=status,
        current_branch=inspect_repo(root).branch if is_git else None,
    )
    session.add(project)
    session.commit()
    return project


@pytest.fixture
def monitor():
    """One monitor per test, reused across ticks.

    ``git.unavailable`` deduplication is state on the instance ? exactly like
    the supervisor-owned monitor of a running application.
    """

    return GitMonitor(db.new_session)


def _project(session, project_id: str) -> Project:
    session.expire_all()
    row = session.get(Project, project_id)
    assert row is not None
    return row


def _commits(session, project_id: str) -> list[GitCommit]:
    """Rows for one project; git timestamps have second resolution, so callers
    assert on the set of subjects/shas and take ordering from the events."""
    session.expire_all()
    return list(
        session.execute(select(GitCommit).where(GitCommit.project_id == project_id)).scalars()
    )


def _event_shas(session) -> list[str]:
    return [
        json.loads(event.payload_json)["sha"] for event in _events(session, "git.commit")
    ]


def _events(session, event_type: str) -> list[ActivityEvent]:
    session.expire_all()
    return list(
        session.execute(
            select(ActivityEvent)
            .where(ActivityEvent.type == event_type)
            .order_by(ActivityEvent.seq)
        ).scalars()
    )


def _snapshot(root: Path) -> dict[str, tuple[int, str]]:
    """mtime + sha256 of every file under ``root`` (``.git`` included)."""
    out: dict[str, tuple[int, str]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            blob = path.read_bytes()
            out[path.relative_to(root).as_posix()] = (
                path.stat().st_mtime_ns,
                hashlib.sha256(blob).hexdigest(),
            )
    return out


# --- seeding and idempotence ---------------------------------------------


def test_first_tick_seeds_commits_and_emits_events(session, project_repo, monitor):
    _repo, root, _add = project_repo
    project = _register(session, root, is_git=True)
    info = inspect_repo(root)

    monitor.tick()

    commits = _commits(session, project.id)
    assert {c.subject for c in commits} == {"second commit", "initial commit"}
    assert commits[0].source == "git_poll"
    newest = next(c for c in commits if c.sha == info.last_commits[0].sha)
    assert newest.short_sha == newest.sha[:8]
    assert newest.author_name == "Test User"
    assert "tester" not in newest.author_email_redacted
    assert json.loads(newest.files_json)["files"]
    assert newest.committed_at.endswith("Z")

    # events arrive newest-first, one per commit
    assert _event_shas(session) == [c.sha for c in info.last_commits]
    events = _events(session, "git.commit")
    assert all(e.source == "git_monitor" and e.project_id == project.id for e in events)
    payload = json.loads(events[0].payload_json)
    assert payload["branch"]


def test_second_tick_without_new_commits_changes_nothing(session, project_repo, monitor):
    _repo, root, _add = project_repo
    project = _register(session, root, is_git=True)
    monitor.tick()
    before = sorted(c.sha for c in _commits(session, project.id))

    monitor.tick()

    assert sorted(c.sha for c in _commits(session, project.id)) == before
    assert len(_events(session, "git.commit")) == len(before)


def test_new_commit_between_ticks_adds_exactly_one_row_and_event(session, project_repo, monitor):
    repo, root, add_commit = project_repo
    project = _register(session, root, is_git=True)
    monitor.tick()

    sha = add_commit("third commit")

    monitor.tick()

    commits = _commits(session, project.id)
    assert len(commits) == 3
    assert {c.sha for c in commits} == {
        c.sha for c in inspect_repo(root, commits=10).last_commits
    }
    events = _events(session, "git.commit")
    assert len(events) == 3
    assert json.loads(events[-1].payload_json)["sha"] == sha
    assert repo.head.commit.hexsha == sha  # the monitor never committed itself


# --- scoping --------------------------------------------------------------


def test_non_git_project_is_skipped(tmp_path, session, monitor):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "file.txt").write_text("data\n", encoding="utf-8")
    project = _register(session, plain, is_git=False)

    monitor.tick()

    assert _commits(session, project.id) == []
    assert _events(session, "git.commit") == []
    assert _events(session, "git.unavailable") == []
    assert _project(session, project.id).status == "active"


def test_archived_project_is_not_polled(session, project_repo, monitor):
    _repo, root, _add = project_repo
    project = _register(session, root, is_git=True, status="archived")

    monitor.tick()

    assert _commits(session, project.id) == []
    assert _events(session, "git.commit") == []
    assert _project(session, project.id).status == "archived"


# --- availability (R-3.4) -------------------------------------------------


def test_unavailable_repository_is_reported_once(session, project_repo, monitor):
    _repo, root, _add = project_repo
    project = _register(session, root, is_git=True)
    monitor.tick()

    # parked, not deleted: git object files are read-only on Windows
    (root / ".git").rename(root / ".git.disabled")

    monitor.tick()

    row = _project(session, project.id)
    assert row.status == "unavailable"
    events = _events(session, "git.unavailable")
    assert len(events) == 1
    assert events[0].severity == "warning"
    assert json.loads(events[0].payload_json)["error"]

    monitor.tick()  # still broken: same project + same reason → no repeat

    assert _project(session, project.id).status == "unavailable"
    assert len(_events(session, "git.unavailable")) == 1


def test_restored_repository_recovers_to_active(session, project_repo, monitor):
    _repo, root, _add = project_repo
    project = _register(session, root, is_git=True)
    git_dir = root / ".git"
    parked = root / ".git.parked"

    git_dir.rename(parked)
    monitor.tick()
    assert _project(session, project.id).status == "unavailable"

    parked.rename(git_dir)
    monitor.tick()

    row = _project(session, project.id)
    assert row.status == "active"
    recovered = _events(session, "git.recovered")
    assert len(recovered) == 1
    assert json.loads(recovered[0].payload_json)["previous_error"]
    assert _events(session, "git.commit")  # seeding resumes after recovery


def test_missing_project_directory_is_unavailable_not_a_crash(session, tmp_path, monitor):
    gone = tmp_path / "vanished"
    project = _register(session, tmp_path, is_git=False)
    project.path = str(gone)
    project.path_key = path_key_for(gone)
    project.is_git = True
    session.commit()

    monitor.tick()

    assert _project(session, project.id).status == "unavailable"
    assert _events(session, "git.unavailable")


# --- branch tracking ------------------------------------------------------


def test_branch_change_is_tracked_and_reported(session, project_repo, monitor):
    repo, root, _add = project_repo
    project = _register(session, root, is_git=True)
    original = project.current_branch
    assert original

    monitor.tick()  # unchanged branch → no event
    assert _project(session, project.id).current_branch == original
    assert _events(session, "git.branch_changed") == []

    repo.git.checkout("-b", "feature/x")  # the developer's action

    monitor.tick()

    row = _project(session, project.id)
    assert row.current_branch == "feature/x"
    events = _events(session, "git.branch_changed")
    assert len(events) == 1
    payload = json.loads(events[0].payload_json)
    assert payload == {"branch": "feature/x", "previous_branch": original}


# --- read-only guarantee --------------------------------------------------


def test_tick_never_touches_the_repository(session, project_repo, monitor):
    _repo, root, _add = project_repo
    project = _register(session, root, is_git=True)

    before = _snapshot(root)
    monitor.tick()
    monitor.tick()

    assert _snapshot(root) == before
    assert _project(session, project.id).status == "active"


# --- loop wrapper ---------------------------------------------------------


async def test_start_and_stop_manage_the_task():
    monitor = GitMonitor(db.new_session, interval_s=0.01)

    monitor.start()
    await asyncio.sleep(0.05)
    assert monitor._task is not None and not monitor._task.done()

    monitor.start()  # double start is a no-op
    assert not monitor._task.done()

    monitor.stop()
    await asyncio.sleep(0.05)
    assert monitor._task is None
