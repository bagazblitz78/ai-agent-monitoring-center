"""Tests for the watchdog-backed file monitor (M3, Task 5).

Watchdog delivers events asynchronously, so every assertion waits with a
bounded ``wait_for`` (≤ 5 s) instead of a fixed sleep. The monitor must never
write inside the watched tree: it only reads it and records rows in the app
database.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from app import db
from app.engine.file_monitor import FileMonitor, default_excludes
from app.models import FileChange, Project
from app.services.paths import path_key_for
from sqlalchemy import select

MB = 1024 * 1024

# ARCHITECTURE §7 — the exact default exclude list the UI documents.
EXPECTED_EXCLUDES = (
    "node_modules/",
    ".venv/",
    "venv/",
    "__pycache__/",
    ".git/objects",
    "dist/",
    "build/",
    "target/",
    ".next/",
    "*.lock",
    ".env*",
    "*.pem",
    "id_rsa*",
    "*.key",
)


def wait_for(predicate: Callable[[], bool], timeout: float = 5.0, interval: float = 0.05) -> bool:
    """Poll ``predicate`` until it is true or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture
def project(session, tmp_path):
    """A registered project whose root lives outside ``AGENTSCOPE_HOME``."""
    root = tmp_path / "proj"
    root.mkdir()
    row = Project(name="proj", path=str(root), path_key=path_key_for(root), is_git=False)
    session.add(row)
    session.commit()
    return row, root


@pytest.fixture
def monitor_factory():
    """Build monitors that are always stopped, even when a test fails."""
    started: list[FileMonitor] = []

    def make(root: Path, project_id: str, **kwargs) -> FileMonitor:
        monitor = FileMonitor(root, project_id, db.new_session, **kwargs)
        started.append(monitor)
        return monitor

    yield make
    for monitor in started:
        monitor.stop()


def _rows(session, project_id: str) -> list[FileChange]:
    session.expire_all()
    return list(
        session.execute(
            select(FileChange)
            .where(FileChange.project_id == project_id)
            .order_by(FileChange.ts, FileChange.id)
        ).scalars()
    )


def _paths(session, project_id: str) -> set[str]:
    return {row.path_relative for row in _rows(session, project_id)}


def _write(root: Path, relative: str, content: str = "x\n") -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


# --- default excludes -----------------------------------------------------


def test_default_excludes_match_architecture_list():
    excludes = default_excludes()

    for pattern in EXPECTED_EXCLUDES:
        assert pattern in excludes, pattern


# --- recorded changes -----------------------------------------------------


def test_create_and_modify_are_recorded(session, project, monitor_factory):
    row, root = project
    monitor = monitor_factory(root, row.id, debounce_s=0.05)
    monitor.start()

    target = root / "a.txt"
    target.write_text("one\n", encoding="utf-8")
    assert wait_for(lambda: any(r.action == "created" for r in _rows(session, row.id)))

    time.sleep(0.2)  # clear the 50 ms debounce window
    target.write_text("one\ntwo\n", encoding="utf-8")
    assert wait_for(lambda: any(r.action == "modified" for r in _rows(session, row.id)))

    rows = _rows(session, row.id)
    assert {r.path_relative for r in rows} == {"a.txt"}
    assert all(r.source == "watchdog" for r in rows)
    assert any(r.size_bytes == target.stat().st_size for r in rows)


def test_path_relative_is_posix_style(session, project, monitor_factory):
    row, root = project
    monitor = monitor_factory(root, row.id, debounce_s=0.05)
    monitor.start()

    _write(root, "sub/deep/notes.txt")

    assert wait_for(lambda: bool(_rows(session, row.id)))
    time.sleep(0.2)
    assert _paths(session, row.id) == {"sub/deep/notes.txt"}


def test_delete_is_recorded(session, project, monitor_factory):
    row, root = project
    target = root / "gone.txt"
    target.write_text("bye\n", encoding="utf-8")
    monitor = monitor_factory(root, row.id, debounce_s=0.05)
    monitor.start()

    target.unlink()

    assert wait_for(
        lambda: any(r.action == "deleted" for r in _rows(session, row.id))
    ), [r.action for r in _rows(session, row.id)]


def test_stop_halts_recording(session, project, monitor_factory):
    row, root = project
    monitor = monitor_factory(root, row.id, debounce_s=0.05)
    monitor.start()
    monitor.stop()

    _write(root, "after-stop.txt")
    time.sleep(0.6)

    assert _rows(session, row.id) == []


# --- exclusions -----------------------------------------------------------


def test_excluded_files_are_not_recorded(session, project, monitor_factory):
    row, root = project
    monitor = monitor_factory(root, row.id, debounce_s=0.05)
    monitor.start()

    excluded = [
        ".env",
        ".env.local",
        "config/.env.production",
        "node_modules/pkg/index.js",
        ".venv/lib/site.py",
        "venv/bin/activate",
        "__pycache__/mod.pyc",
        ".git/objects/ab/cdef01",
        "dist/bundle.js",
        "build/out.js",
        "target/debug/bin",
        ".next/page.js",
        "src/app.lock",
        "certs/server.pem",
        "id_rsa",
        "ssh/id_rsa.pub",
        "secrets/secret.key",
    ]
    for relative in excluded:
        _write(root, relative)
    _write(root, "visible.txt")

    assert wait_for(lambda: "visible.txt" in _paths(session, row.id))
    time.sleep(0.3)
    assert _paths(session, row.id) == {"visible.txt"}


def test_large_file_is_not_recorded(session, project, monitor_factory):
    row, root = project
    monitor = monitor_factory(root, row.id, debounce_s=0.05)
    monitor.start()

    (root / "big.bin").write_bytes(b"\x00" * (3 * MB))
    _write(root, "small.txt")

    assert wait_for(lambda: "small.txt" in _paths(session, row.id))
    time.sleep(0.3)
    assert "big.bin" not in _paths(session, row.id)


def test_symlink_escape_is_dropped(session, project, monitor_factory, tmp_path):
    row, root = project
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "target.txt"
    target.write_text("outside\n", encoding="utf-8")
    link = root / "link.txt"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("file symlinks unavailable without the developer mode privilege")

    monitor = monitor_factory(root, row.id, debounce_s=0.05)
    monitor.start()
    link.write_text("written through the link\n", encoding="utf-8")  # resolves outside root
    _write(root, "visible.txt")

    assert wait_for(lambda: "visible.txt" in _paths(session, row.id))
    time.sleep(0.3)
    assert "link.txt" not in _paths(session, row.id)


def test_paths_outside_the_root_are_dropped(session, project, monitor_factory, tmp_path):
    """Deterministic twin of the symlink test: the *policy* refuses an escaping
    path, independent of whether watchdog happens to follow the link here."""
    row, root = project
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    inside = root / "inside.txt"
    inside.write_text("inside\n", encoding="utf-8")
    monitor = monitor_factory(root, row.id, debounce_s=0.05)

    monitor._handle(str(outside), "modified")
    monitor._handle(str(inside), "modified")

    assert _paths(session, row.id) == {"inside.txt"}


# --- debounce and rate cap ------------------------------------------------


def test_rapid_writes_are_coalesced(session, project, monitor_factory):
    row, root = project
    (root / "hot.txt").write_text("start\n", encoding="utf-8")  # exists before watching
    monitor = monitor_factory(root, row.id)  # default 500 ms debounce
    monitor.start()

    for index in range(3):
        (root / "hot.txt").write_text(f"write {index}\n", encoding="utf-8")
        time.sleep(0.03)

    assert wait_for(lambda: monitor.dropped > 0, timeout=3.0)
    rows = [r for r in _rows(session, row.id) if r.path_relative == "hot.txt"]
    assert 1 <= len(rows) <= 3, [r.action for r in rows]


def test_rate_cap_limits_rows_per_path_per_second(session, project, monitor_factory):
    row, root = project
    (root / "hot.txt").write_text("start\n", encoding="utf-8")
    monitor = monitor_factory(root, row.id, debounce_s=0.0)
    monitor.start()

    for _ in range(20):
        (root / "hot.txt").write_text("x\n", encoding="utf-8")
        time.sleep(0.02)

    assert wait_for(lambda: monitor.dropped > 0, timeout=5.0)
    rows = [r for r in _rows(session, row.id) if r.path_relative == "hot.txt"]
    assert len(rows) <= 10, len(rows)


def test_rate_limiter_counts_overflow_deterministically(session, project):
    row, root = project
    monitor = FileMonitor(
        root, row.id, db.new_session, debounce_s=0.0, max_events_per_path_per_s=3
    )

    now = 1_000.0
    accepted = [monitor._try_accept("a.txt", now) for _ in range(5)]

    assert accepted == [True, True, True, False, False]
    assert monitor.dropped == 2
    # the window is one second wide: the same path is eligible again after it
    assert monitor._try_accept("a.txt", now + 1.0) is True
