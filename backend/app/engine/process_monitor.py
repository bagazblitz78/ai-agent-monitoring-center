"""Process monitor: match registered agent matchers against the process table.

Everything here is **read-only** with respect to the machine: psutil is asked
for a process listing, a matching agent row is updated in AgentScope's own
database and one snapshot row is written. Nothing is signalled, started,
stopped or otherwise influenced outside the app's own database.

Scoping rules (M3 review focus):

* only processes matching a *registered* agent matcher are looked at;
* the app's own process and its ancestors are never candidates (we would
  otherwise confirm ourselves), and PIDs 1-4 are kernel/system slots;
* ``AccessDenied``/``ZombieProcess`` degrade a snapshot to sentinel values and
  the agent status to ``unknown`` — they never raise out of ``tick()``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import psutil
from app.engine.events import persist_event
from app.models import Agent, ProcessSnapshot
from app.models.base import utc_now
from app.services.redact import redact_cmdline
from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Session]

#: psutil attributes fetched in one pass per tick.
PROCESS_ATTRS = [
    "pid",
    "ppid",
    "name",
    "exe",
    "cmdline",
    "create_time",
    "cpu_percent",
    "memory_info",
    "status",
]

#: 0-4 are kernel/system slots on every supported platform.
MIN_PID = 5

#: Statuses this monitor is allowed to overwrite; anything else (``working``,
#: ``blocked``, ...) is owned by the agent's own reporting in M4.
OBSERVED_STATUSES = ("stopped", "unknown", "running")

INVALID_MATCHER = "invalid_matcher"


@dataclass
class ProcessInfo:
    """One process as seen by a single tick; ``None`` means "could not read"."""

    pid: int
    ppid: int | None = None
    name: str | None = None
    exe: str | None = None
    cmdline: list[str] | None = None
    create_time: float | None = None
    cpu_pct: float | None = None
    rss_bytes: int | None = None
    state: str | None = None
    accessible: bool = True


class ProcessMonitor:
    """Scan the process table for registered agents on a fixed interval."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        interval_s: float = 4.0,
        psutil_process_iter: Callable[..., Iterable] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self.interval_s = float(interval_s)
        self._process_iter = psutil_process_iter or psutil.process_iter
        self._task: asyncio.Task | None = None
        #: agent id → candidate pids of the last ambiguous tick, so an unchanged
        #: situation emits ``agent.match_ambiguous`` exactly once (R-3.3).
        self._ambiguous: dict[str, tuple[int, ...]] = {}

    # --- asyncio wrapper ----------------------------------------------------

    def start(self) -> None:
        """Schedule the loop on the running event loop; a second call is a no-op."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval_s)
            try:
                # Off the event loop: a full process scan must not stall SSE.
                await asyncio.to_thread(self.tick)
            except Exception:  # a failed tick must not kill the loop
                logger.exception("process monitor tick failed")

    def stop(self) -> asyncio.Task | None:
        """Cancel the loop and return its task so callers can await it.

        Safe to call twice, or without ``start`` (then it returns ``None``).
        """
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
        return task

    # --- one scan -----------------------------------------------------------

    def tick(self) -> None:
        """One synchronous scan: the test seam for every behaviour below."""
        session = self._session_factory()
        try:
            agents = list(
                session.execute(select(Agent).where(Agent.match_kind != "none")).scalars()
            )
            if not agents:
                return
            processes = self._scan()
            if not processes:
                # A scan that yields nothing is a failed scan, not an empty
                # machine: marking every agent stopped on it would be a lie.
                logger.warning("process scan returned no processes; skipping tick")
                return
            visible = [p for p in processes if p.pid >= MIN_PID]
            own = self._own_pids()
            visible = [p for p in visible if p.pid not in own]
            for agent in agents:
                try:
                    self._evaluate(session, agent, visible)
                except Exception:
                    logger.exception("process monitor failed evaluating agent %s", agent.id)
            session.commit()
        finally:
            session.close()

    def _scan(self) -> list[ProcessInfo]:
        try:
            processes = list(self._process_iter(PROCESS_ATTRS))
        except psutil.Error as exc:
            logger.warning("process scan failed: %s", exc)
            return []
        scanned: list[ProcessInfo] = []
        for proc in processes:
            info = self._read(proc)
            if info is not None:
                scanned.append(info)
        return scanned

    def _read(self, proc: object) -> ProcessInfo | None:
        try:
            pid = int(proc.pid)  # type: ignore[attr-defined]
        except (TypeError, ValueError, AttributeError):
            return None
        try:
            data = getattr(proc, "info", None)
        except psutil.Error:
            data = None
        if not isinstance(data, dict):
            data = {}
        memory = data.get("memory_info")
        info = ProcessInfo(
            pid=pid,
            ppid=data.get("ppid"),
            name=data.get("name"),
            exe=data.get("exe"),
            cmdline=data.get("cmdline") if isinstance(data.get("cmdline"), list) else None,
            create_time=data.get("create_time"),
            cpu_pct=data.get("cpu_percent"),
            rss_bytes=getattr(memory, "rss", None),
            state=data.get("status"),
        )
        # psutil reports AccessDenied attributes as ``None`` (ad_value), so a
        # process we can identify but cannot read looks exactly like this.
        info.accessible = not (info.state is None and info.exe is None and info.cmdline is None)
        return info

    def _own_pids(self) -> set[int]:
        """Our PID plus every ancestor: the app must never confirm itself."""
        pids: set[int] = set()
        try:
            current: psutil.Process | None = psutil.Process()
            while current is not None:
                pids.add(current.pid)
                current = current.parent()
        except psutil.Error:
            pids.add(os.getpid())
        return pids

    # --- matching -----------------------------------------------------------

    def _candidates(self, agent: Agent, processes: Iterable[ProcessInfo]) -> list[ProcessInfo]:
        """Processes matching this agent's matcher; invalid matchers yield none."""
        kind = agent.match_kind
        value = agent.match_value
        if kind == "pid":
            try:
                target = int(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return []
            return [p for p in processes if p.pid == target]
        if kind == "exe":
            if not value:
                return []
            wanted = value.casefold()
            return [p for p in processes if p.exe and p.exe.casefold() == wanted]
        if kind == "cmdline_regex":
            if not value:
                return []
            try:
                pattern = re.compile(value)
            except re.error:
                return []
            return [
                p
                for p in processes
                if p.cmdline is not None and pattern.search(" ".join(p.cmdline))
            ]
        return []

    def _evaluate(self, session: Session, agent: Agent, processes: Iterable[ProcessInfo]) -> None:
        if agent.match_kind == "cmdline_regex":
            try:
                re.compile(agent.match_value or "")
            except re.error:
                agent.last_error = INVALID_MATCHER
                return

        candidates = self._candidates(agent, processes)
        if len(candidates) == 1:
            self._confirm(session, agent, candidates[0])
        elif len(candidates) > 1:
            self._mark_ambiguous(session, agent, candidates)
        else:
            self._handle_absent(session, agent)

    def _confirm(self, session: Session, agent: Agent, info: ProcessInfo) -> None:
        agent.confirmed_pid = info.pid
        self._ambiguous.pop(agent.id, None)
        if info.accessible:
            if agent.status in OBSERVED_STATUSES:
                agent.status = "running"
                agent.status_source = "observed"
            agent.last_error = None
        else:
            # Identified but unreadable: honest about what we do not know, and
            # never allowed to overwrite an agent's own reported status.
            if agent.status in OBSERVED_STATUSES:
                agent.status = "unknown"
                agent.status_source = "observed"
        session.add(
            ProcessSnapshot(
                ts=utc_now(),
                agent_id=agent.id,
                pid=info.pid,
                ppid=info.ppid if info.ppid is not None else 0,
                exe=info.exe or "",
                cmdline_redacted=redact_cmdline(info.cmdline) if info.cmdline else "",
                state=(info.state or "unknown") if info.accessible else "unknown",
                cpu_pct=info.cpu_pct if info.accessible else None,
                rss_bytes=info.rss_bytes if info.accessible else None,
                create_time=info.create_time if info.accessible else None,
            )
        )

    def _mark_ambiguous(
        self, session: Session, agent: Agent, candidates: list[ProcessInfo]
    ) -> None:
        key = tuple(sorted(p.pid for p in candidates))
        if self._ambiguous.get(agent.id) == key:
            return  # unchanged candidate set: already reported (R-3.3)
        self._ambiguous[agent.id] = key
        persist_event(
            self._session_factory,
            type="agent.match_ambiguous",
            source="process_monitor",
            severity="warning",
            message=f"agent {agent.name!r} matched {len(key)} processes",
            payload={"state": "needs_confirmation", "candidate_pids": list(key)},
            project_id=agent.project_id,
            agent_id=agent.id,
        )

    def _handle_absent(self, session: Session, agent: Agent) -> None:
        self._ambiguous.pop(agent.id, None)
        previous = agent.confirmed_pid
        if previous is None:
            return  # never confirmed: nothing to declare as exited
        agent.confirmed_pid = None
        if agent.status in OBSERVED_STATUSES:
            agent.status = "stopped"
            agent.status_source = "observed"
        persist_event(
            self._session_factory,
            type="agent.process_exited",
            source="process_monitor",
            severity="info",
            message=f"agent {agent.name!r} is no longer running",
            payload={"previous_pid": previous},
            project_id=agent.project_id,
            agent_id=agent.id,
        )
