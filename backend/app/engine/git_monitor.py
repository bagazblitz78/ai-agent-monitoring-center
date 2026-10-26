"""Git monitor: poll every registered repository and record what changed.

Read-only by construction — the monitor only calls ``services.git_info``
(``Repo``, ``git status --porcelain``, ``iter_commits``), never GitPython write
 APIs, and writes exclusively to AgentScope's own database.

A repository that disappears, locks up or turns into garbage is a *status*, not
an exception (R-3.4): ``projects.status`` becomes ``unavailable`` together with
a ``git.unavailable`` event (deduped per project *and* reason) and recovers to
``active`` with a ``git.recovered`` event on the next successful poll.

Events are collected during the scan and published **after** the scan's
transaction commits: :func:`persist_event` opens its own session, and SQLite
allows a single writer at a time, so publishing while holding the write lock
would be a guaranteed ``database is locked``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.engine.events import persist_event
from app.models import GitCommit, Project
from app.services.git_info import GitInfo, inspect_repo
from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Session]
Inspector = Callable[..., GitInfo]

SOURCE = "git_monitor"
ARCHIVED = "archived"
ACTIVE = "active"
UNAVAILABLE = "unavailable"


class GitMonitor:
    """Poll registered git projects on a fixed interval."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        interval_s: float = 5.0,
        commits_per_poll: int = 10,
        inspect: Inspector = inspect_repo,
    ) -> None:
        self._session_factory = session_factory
        self.interval_s = float(interval_s)
        self.commits_per_poll = int(commits_per_poll)
        self._inspect = inspect
        self._task: asyncio.Task | None = None
        #: project id → last reported reason, so an unchanged failure emits
        #: ``git.unavailable`` exactly once (deduped per project + reason).
        self._unavailable: dict[str, str] = {}

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
                # Off the event loop: a git poll must not stall SSE.
                await asyncio.to_thread(self.tick)
            except Exception:  # a failed tick must not kill the loop
                logger.exception("git monitor tick failed")

    def stop(self) -> None:
        """Cancel the loop; safe to call twice, or without ``start``."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()

    # --- one scan -----------------------------------------------------------

    def tick(self) -> None:
        """One synchronous poll of every live git project (the test seam)."""
        session = self._session_factory()
        events: list[dict[str, Any]] = []
        try:
            projects = list(
                session.execute(
                    select(Project).where(
                        Project.is_git.is_(True),
                        Project.status != ARCHIVED,
                    )
                ).scalars()
            )
            for project in projects:
                try:
                    self._poll(session, project, events)
                except Exception:
                    logger.exception("git monitor failed polling %s", project.path)
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("git monitor tick failed; no events published")
            return
        finally:
            session.close()
        self._publish(events)

    def _poll(self, session: Session, project: Project, events: list[dict[str, Any]]) -> None:
        root = Path(project.path)
        # ``inspect_repo`` opens with the cheap ``is_git_worktree`` probe, so a
        # directory that stopped being a repository costs one ``exists()``.
        info = self._inspect(root, commits=self.commits_per_poll)
        if not info.available:
            self._mark_unavailable(project, info, events)
            return
        self._mark_recovered(project, info, events)
        self._sync_branch(project, info, events)
        self._sync_commits(session, project, info, events)

    # --- availability (R-3.4) ----------------------------------------------

    def _mark_unavailable(
        self, project: Project, info: GitInfo, events: list[dict[str, Any]]
    ) -> None:
        reason = info.error or "unavailable"
        project.status = UNAVAILABLE
        if self._unavailable.get(project.id) == reason:
            return  # same project, same reason: already reported
        self._unavailable[project.id] = reason
        events.append(
            {
                "type": "git.unavailable",
                "source": SOURCE,
                "severity": "warning",
                "message": f"git repository unavailable for {project.name}: {reason}",
                "payload": {"error": reason},
                "project_id": project.id,
            }
        )

    def _mark_recovered(
        self, project: Project, info: GitInfo, events: list[dict[str, Any]]
    ) -> None:
        if project.status != UNAVAILABLE:
            return
        reason = self._unavailable.pop(project.id, None)
        project.status = ACTIVE
        events.append(
            {
                "type": "git.recovered",
                "source": SOURCE,
                "severity": "info",
                "message": f"git repository recovered for {project.name}",
                "payload": {"previous_error": reason, "branch": info.branch},
                "project_id": project.id,
            }
        )

    # --- repository state ---------------------------------------------------

    def _sync_branch(
        self, project: Project, info: GitInfo, events: list[dict[str, Any]]
    ) -> None:
        branch = info.branch
        if not branch or branch == project.current_branch:
            return
        previous = project.current_branch
        project.current_branch = branch
        events.append(
            {
                "type": "git.branch_changed",
                "source": SOURCE,
                "severity": "info",
                "message": f"{project.name}: {previous or '<none>'} → {branch}",
                "payload": {"branch": branch, "previous_branch": previous},
                "project_id": project.id,
            }
        )

    def _sync_commits(
        self, session: Session, project: Project, info: GitInfo, events: list[dict[str, Any]]
    ) -> None:
        known = set(
            session.execute(
                select(GitCommit.sha).where(GitCommit.project_id == project.id)
            ).scalars()
        )
        for commit in info.last_commits:  # newest first, as reported by git
            if commit.sha in known:
                continue
            known.add(commit.sha)
            session.add(
                GitCommit(
                    project_id=project.id,
                    sha=commit.sha,
                    short_sha=commit.short_sha,
                    author_name=commit.author_name,
                    author_email_redacted=commit.author_email_redacted,
                    committed_at=commit.committed_at,
                    subject=commit.subject,
                    # object (not array): the column's default is ``{}``
                    files_json=json.dumps({"files": commit.files}, ensure_ascii=False),
                    source="git_poll",
                )
            )
            events.append(
                {
                    "type": "git.commit",
                    "source": SOURCE,
                    "severity": "info",
                    "message": commit.subject or f"commit {commit.short_sha}",
                    "payload": {
                        "sha": commit.sha,
                        "subject": commit.subject,
                        "branch": project.current_branch,
                    },
                    "project_id": project.id,
                }
            )

    # --- publishing ---------------------------------------------------------

    def _publish(self, events: list[dict[str, Any]]) -> None:
        """Fan out after the state is committed (one writer at a time in SQLite)."""
        for event in events:
            try:
                persist_event(self._session_factory, **event)
            except Exception:
                logger.exception("git monitor failed to publish %s", event.get("type"))
