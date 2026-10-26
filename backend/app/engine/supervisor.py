"""Engine supervisor: owns the monitors of one running application.

One :class:`Supervisor` lives on ``app.state`` and is driven by the app's
lifespan:

* ``start(app)`` — a no-op when the engine is disabled
  (``AGENTSCOPE_DISABLE_MONITORS``), otherwise it registers the publish loop,
  starts one :class:`~app.engine.file_monitor.FileMonitor` per *active* and
  *watched* project and brings up the process and git monitors once;
* ``refresh_monitors()`` — re-syncs the per-project watchers with the database;
  project mutations (create/patch/archive/unarchive/delete) call it so a new
  registration is watched immediately;
* ``stop()`` — stops every observer and monitor task so shutdown leaves no
  thread behind.

Mutating anything outside AgentScope's own database is done by the monitors
themselves, never here: this class only creates, starts and stops them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.config import settings_singleton
from app.db import new_session
from app.engine.events import set_publish_loop
from app.engine.file_monitor import FileMonitor
from app.engine.git_monitor import GitMonitor
from app.engine.process_monitor import ProcessMonitor
from app.models import Project
from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Session]

ACTIVE = "active"


def _running(monitor: Any) -> bool:
    """True when the monitor's asyncio loop is scheduled and not finished."""
    task = getattr(monitor, "_task", None)
    return task is not None and not task.done()


class Supervisor:
    """Create, refresh and tear down the monitoring engine for one app."""

    def __init__(self, session_factory: SessionFactory = new_session) -> None:
        self._session_factory = session_factory
        #: project id → its watcher (``_monitors`` is the plan's inspection seam)
        self._monitors: dict[str, FileMonitor] = {}
        self._process: ProcessMonitor | None = None
        self._git: GitMonitor | None = None
        self._started = False

    # --- lifecycle ----------------------------------------------------------

    async def start(self, app: Any) -> None:
        """Start the engine; a second call, or a disabled engine, does nothing."""
        if self._started:
            return
        if settings_singleton().disable_monitors:
            logger.info("monitoring engine disabled (AGENTSCOPE_DISABLE_MONITORS)")
            return
        self._started = True
        app.state.supervisor = self
        # Watchdog threads have no event loop of their own: publishing through
        # the startup loop is what makes file events reach SSE subscribers live.
        set_publish_loop(asyncio.get_running_loop())
        self.refresh_monitors()
        self._process = ProcessMonitor(self._session_factory)
        self._process.start()
        self._git = GitMonitor(self._session_factory)
        self._git.start()

    async def stop(self) -> None:
        """Stop every watcher and monitor task; safe to call twice."""
        if not self._started:
            return
        self._started = False
        set_publish_loop(None)
        for monitor in list(self._monitors.values()):
            monitor.stop()
        self._monitors.clear()

        tasks: list[asyncio.Task] = []
        for monitor in (self._process, self._git):
            if monitor is None:
                continue
            task = monitor.stop()
            if task is not None:
                tasks.append(task)
        self._process = None
        self._git = None
        for task in tasks:
            # ``stop()`` cancels; awaiting here lets the task unwind before the
            # loop goes away instead of printing "Task was destroyed but it is
            # pending".
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # --- per-project watchers ----------------------------------------------

    def refresh_monitors(self) -> None:
        """Start watchers for newly watchable projects, stop the rest."""
        if not self._started:
            return
        wanted = self._watchable()
        for project_id, monitor in list(self._monitors.items()):
            if project_id in wanted:
                continue
            monitor.stop()
            del self._monitors[project_id]
            logger.info("stopped watcher for project %s", project_id)
        for project_id, root in wanted.items():
            if project_id not in self._monitors:
                self._start_one(project_id, root)

    def _watchable(self) -> dict[str, Path]:
        session = self._session_factory()
        try:
            rows = session.execute(
                select(Project).where(
                    Project.status == ACTIVE,
                    Project.watch_enabled.is_(True),
                )
            ).scalars()
            return {project.id: Path(project.path) for project in rows}
        finally:
            session.close()

    def _start_one(self, project_id: str, root: Path) -> None:
        monitor = FileMonitor(root, project_id, self._session_factory)
        try:
            monitor.start()
        except Exception:  # a missing/unreadable root must not kill startup
            logger.exception("could not watch project %s at %s", project_id, root)
            return
        self._monitors[project_id] = monitor
        logger.info("watching %s", root)

    # --- reporting ----------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """What ``GET /api/v1/health`` reports under ``engine.monitors``."""
        return {
            "file": len(self._monitors),
            "process": _running(self._process),
            "git": _running(self._git),
        }


def refresh_project_monitors(app: Any) -> None:
    """Re-sync watchers after a project mutation; a request must never fail here."""
    supervisor = getattr(getattr(app, "state", None), "supervisor", None)
    if supervisor is None:
        return
    try:
        supervisor.refresh_monitors()
    except Exception:
        logger.exception("monitor refresh failed")
