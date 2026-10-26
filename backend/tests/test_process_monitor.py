"""Tests for the process monitor (M3, Task 6).

A real dummy process is spawned per test (``python -c "import time;
time.sleep(120)"`` plus a unique marker in argv) so discovery, snapshots and
exit detection are exercised against the actual OS process table. Every dummy
is terminated in the fixture's teardown even when an assertion fails.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid

import psutil
import pytest
from app import db
from app.engine.process_monitor import ProcessMonitor
from app.models import ActivityEvent, Agent, ProcessSnapshot, Project
from app.services.paths import path_key_for
from sqlalchemy import select

#: The *base* interpreter: ``.venv\Scripts\python.exe`` is a launcher that
#: re-runs itself as a child with the same command line, which would make one
#: dummy look like two matching processes (and orphan the child on terminate).
DUMMY_EXECUTABLE = getattr(sys, "_base_executable", None) or sys.executable
DUMMY_SLEEP = "import time; time.sleep(120)"
TOKEN = "supersecrettokenvalue1234"
FAKE_PID = 424242


@pytest.fixture
def marker() -> str:
    """A matcher unique to this test: no other test's dummy can ever match."""
    return f"zzmk{uuid.uuid4().hex[:10]}"


@pytest.fixture
def project_agent(session, tmp_path, marker):
    """A registered project with one regex-matched agent, freshly observed."""
    root = tmp_path / "proj"
    root.mkdir()
    project = Project(name="proj", path=str(root), path_key=path_key_for(root), is_git=False)
    session.add(project)
    session.flush()
    agent = Agent(
        project_id=project.id,
        name="dummy-agent",
        adapter="generic_process",
        match_kind="cmdline_regex",
        match_value=marker,
        status="unknown",
        status_source="unknown",
    )
    session.add(agent)
    session.commit()
    return project, agent


@pytest.fixture
def spawn(marker):
    """Spawn long-lived dummy processes; whole trees terminated at teardown."""
    processes: list[subprocess.Popen] = []

    def _spawn() -> subprocess.Popen:
        proc = subprocess.Popen(
            [DUMMY_EXECUTABLE, "-c", DUMMY_SLEEP, marker, "--token", TOKEN],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
        processes.append(proc)
        return proc

    yield _spawn
    for proc in processes:
        try:
            parent = psutil.Process(proc.pid)
            for child in parent.children(recursive=True):
                child.kill()
            parent.kill()
        except psutil.Error:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            pass


@pytest.fixture
def monitor():
    return ProcessMonitor(db.new_session, interval_s=0.01)


def _agent(session, agent_id: str) -> Agent:
    session.expire_all()
    row = session.get(Agent, agent_id)
    assert row is not None
    return row


def _events(session, event_type: str) -> list[ActivityEvent]:
    session.expire_all()
    return list(
        session.execute(
            select(ActivityEvent)
            .where(ActivityEvent.type == event_type)
            .order_by(ActivityEvent.seq)
        ).scalars()
    )


def _snapshots(session, agent_id: str) -> list[ProcessSnapshot]:
    session.expire_all()
    return list(
        session.execute(
            select(ProcessSnapshot).where(ProcessSnapshot.agent_id == agent_id)
        ).scalars()
    )


def _wait_for_pid_gone(pid: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not psutil.pid_exists(pid):
            return
        time.sleep(0.05)
    raise AssertionError(f"pid {pid} still exists after {timeout}s")


# --- discovery ------------------------------------------------------------


def test_discovers_spawned_process(session, project_agent, spawn, monitor, marker):
    _project, agent = project_agent
    proc = spawn()

    monitor.tick()

    row = _agent(session, agent.id)
    assert row.confirmed_pid == proc.pid
    assert row.status == "running"
    assert row.status_source == "observed"
    assert row.last_error is None

    snapshots = _snapshots(session, agent.id)
    assert len(snapshots) == 1
    snap = snapshots[0]
    assert snap.pid == proc.pid
    assert snap.ppid == os.getpid()
    assert marker in snap.cmdline_redacted  # the matcher itself stays visible
    assert TOKEN not in snap.cmdline_redacted  # secret-looking args are masked
    assert snap.exe
    assert snap.rss_bytes is None or snap.rss_bytes >= 0
    assert snap.create_time is not None


def test_non_matching_process_is_never_snapshotted(session, project_agent, monitor):
    _project, agent = project_agent  # no dummy spawned

    monitor.tick()

    assert _agent(session, agent.id).confirmed_pid is None
    assert _snapshots(session, agent.id) == []
    assert _events(session, "agent.match_ambiguous") == []


# --- lifecycle ------------------------------------------------------------


def test_detects_process_exit(session, project_agent, spawn, monitor):
    _project, agent = project_agent
    proc = spawn()
    monitor.tick()
    assert _agent(session, agent.id).confirmed_pid == proc.pid

    proc.terminate()
    proc.wait(timeout=10)
    _wait_for_pid_gone(proc.pid)

    monitor.tick()

    row = _agent(session, agent.id)
    assert row.confirmed_pid is None
    assert row.status == "stopped"
    assert row.status_source == "observed"

    events = _events(session, "agent.process_exited")
    assert len(events) == 1
    payload = json.loads(events[0].payload_json)
    assert payload["previous_pid"] == proc.pid
    assert events[0].agent_id == agent.id


# --- ambiguity ------------------------------------------------------------


def test_ambiguous_match_reports_needs_confirmation(session, project_agent, spawn, monitor):
    _project, agent = project_agent
    first = spawn()
    second = spawn()

    monitor.tick()

    row = _agent(session, agent.id)
    assert row.confirmed_pid is None
    events = _events(session, "agent.match_ambiguous")
    assert len(events) == 1
    payload = json.loads(events[0].payload_json)
    assert payload["state"] == "needs_confirmation"
    assert sorted(payload["candidate_pids"]) == sorted([first.pid, second.pid])
    assert events[0].agent_id == agent.id
    assert _snapshots(session, agent.id) == []

    time.sleep(1.1)  # leave the one-second event bucket: only the monitor can dedupe
    monitor.tick()

    assert _agent(session, agent.id).confirmed_pid is None
    assert len(_events(session, "agent.match_ambiguous")) == 1


# --- scoping --------------------------------------------------------------


def test_own_process_is_never_confirmed(session, project_agent, monitor):
    _project, agent = project_agent
    agent.match_kind = "pid"
    agent.match_value = str(os.getpid())
    session.commit()

    monitor.tick()

    row = _agent(session, agent.id)
    assert row.confirmed_pid is None
    assert _snapshots(session, agent.id) == []
    assert _events(session, "agent.match_ambiguous") == []


def test_invalid_regex_sets_last_error_and_others_still_run(
    session, project_agent, spawn, monitor
):
    _project, agent = project_agent
    broken = Agent(
        project_id=agent.project_id,
        name="broken-matcher",
        match_kind="cmdline_regex",
        match_value="([unclosed",
        status="unknown",
        status_source="unknown",
    )
    session.add(broken)
    session.commit()
    proc = spawn()

    monitor.tick()

    assert _agent(session, broken.id).last_error == "invalid_matcher"
    assert _agent(session, broken.id).confirmed_pid is None
    healthy = _agent(session, agent.id)
    assert healthy.confirmed_pid == proc.pid
    assert healthy.last_error is None


def test_exe_matcher_uses_casefolded_full_path(project_agent, monitor):
    from app.engine.process_monitor import ProcessInfo

    _project, agent = project_agent
    agent.match_kind = "exe"
    agent.match_value = r"c:\python\python.exe"
    processes = [
        ProcessInfo(pid=10, exe=r"C:\Python\python.exe"),
        ProcessInfo(pid=11, exe=r"D:\other\python.exe"),
        ProcessInfo(pid=12, exe=None),  # unreadable: cannot match an exe it has not read
    ]

    matches = monitor._candidates(agent, processes)

    assert [p.pid for p in matches] == [10]


# --- error containment ----------------------------------------------------


def test_access_denied_degrades_to_unknown_status(session, project_agent, monitor):
    _project, agent = project_agent
    agent.match_kind = "pid"
    agent.match_value = str(FAKE_PID)
    agent.status = "running"
    agent.status_source = "observed"
    session.commit()

    class DeniedProcess:
        pid = FAKE_PID

        @property
        def info(self):
            raise psutil.AccessDenied(pid=self.pid)

    monitor._process_iter = lambda attrs=None: iter([DeniedProcess()])

    monitor.tick()  # must not raise

    row = _agent(session, agent.id)
    assert row.status == "unknown"
    assert row.confirmed_pid == FAKE_PID

    snapshots = _snapshots(session, agent.id)
    assert len(snapshots) == 1
    snap = snapshots[0]
    assert snap.pid == FAKE_PID
    assert snap.exe == ""
    assert snap.state == "unknown"
    assert snap.cmdline_redacted == ""
    assert snap.cpu_pct is None
    assert snap.rss_bytes is None
    assert snap.create_time is None


def test_scan_failure_skips_the_tick_without_raising(session, project_agent, monitor):
    _project, agent = project_agent
    agent.match_kind = "pid"
    agent.match_value = "999999"
    agent.status = "working"
    session.commit()

    def exploding_iter(attrs=None):
        raise psutil.AccessDenied(pid=1)

    monitor._process_iter = exploding_iter

    monitor.tick()

    row = _agent(session, agent.id)
    assert row.status == "working"  # unchanged: no evidence either way
    assert _snapshots(session, agent.id) == []


# --- loop wrapper ---------------------------------------------------------


async def test_start_and_stop_manage_the_task(project_agent, spawn):
    monitor = ProcessMonitor(db.new_session, interval_s=0.01)

    monitor.start()
    await asyncio.sleep(0.05)
    assert monitor._task is not None and not monitor._task.done()

    monitor.start()  # double start is a no-op
    assert not monitor._task.done()

    monitor.stop()
    await asyncio.sleep(0.05)
    assert monitor._task is None
