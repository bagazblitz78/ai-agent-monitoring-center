"""Watchdog-backed filesystem monitor for registered project roots.

The monitor is a *read-only consumer*: its observer thread watches one project
tree, and for every accepted change it writes a ``file_changes`` row plus a
``file.changed`` activity event into AgentScope's own database. It never
creates, writes, deletes or moves anything inside the watched project.

Policy, in order, for each incoming event:

1. directories are ignored (only files are changes);
2. ``realpath`` must stay inside the project root — a symlink escaping the tree
   is dropped;
3. the default exclude list (ARCHITECTURE §7) filters secrets, build output and
   dependency trees;
4. files above ``max_file_bytes`` are ignored — the size is re-read until two
   consecutive samples agree, so a file that is still being written is judged on
   its finished size (and never recorded mid-write);
5. a per-path debounce window and a per-path rate cap coalesce bursts — every
   coalesced drop is counted in :attr:`FileMonitor.dropped`, never raised.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import time
from collections import defaultdict, deque
from collections.abc import Callable
from pathlib import Path

from app.engine.events import persist_event
from app.models import FileChange
from app.models.base import utc_now
from app.services.paths import is_within
from sqlalchemy.orm import Session
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Session]

DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024
DEFAULT_DEBOUNCE_S = 0.5
DEFAULT_MAX_EVENTS_PER_PATH_PER_S = 10.0
# A write can still be in flight when its event arrives: re-stat until two
# consecutive reads agree (or this budget runs out) so the size cap and the
# recorded ``size_bytes`` both describe the finished file, not a half-written one.
DEFAULT_SETTLE_S = 0.02
DEFAULT_SETTLE_POLLS = 3

#: Directory subtrees that are never worth reporting (ARCHITECTURE §7).
DIR_EXCLUDES: tuple[str, ...] = (
    "node_modules/",
    ".venv/",
    "venv/",
    "__pycache__/",
    ".git/objects",
    "dist/",
    "build/",
    "target/",
    ".next/",
)

#: Basename patterns that are never worth reporting (secrets and lock files).
BASENAME_EXCLUDES: tuple[str, ...] = ("*.lock", ".env*", "*.pem", "id_rsa*", "*.key")


def default_excludes() -> list[str]:
    """The default exclude list, exactly as documented in ARCHITECTURE §7."""
    return [*DIR_EXCLUDES, *BASENAME_EXCLUDES]


class FileMonitor(FileSystemEventHandler):
    """Watch one project root and record the file changes it contains."""

    def __init__(
        self,
        root: Path,
        project_id: str,
        session_factory: SessionFactory,
        *,
        excludes: list[str] | None = None,
        debounce_s: float = DEFAULT_DEBOUNCE_S,
        max_events_per_path_per_s: float = DEFAULT_MAX_EVENTS_PER_PATH_PER_S,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        settle_s: float = DEFAULT_SETTLE_S,
        settle_polls: int = DEFAULT_SETTLE_POLLS,
    ) -> None:
        self.root = Path(os.path.realpath(str(root)))
        self.project_id = project_id
        self._session_factory = session_factory
        self.excludes = default_excludes() if excludes is None else list(excludes)
        self.debounce_s = float(debounce_s)
        self.max_rate = float(max_events_per_path_per_s)
        self.max_file_bytes = int(max_file_bytes)
        self.settle_s = float(settle_s)
        self.settle_polls = int(settle_polls)
        self.dropped = 0
        self._observer: Observer | None = None
        self._last_accept: dict[str, float] = {}
        self._window: dict[str, deque[float]] = defaultdict(deque)
        # A pattern with a separator is a subtree prefix ("node_modules/",
        # ".git/objects"); a bare pattern is matched against the basename.
        self._prefixes = [p.strip("/") for p in self.excludes if "/" in p]
        self._globs = [p for p in self.excludes if "/" not in p]

    # --- observer lifecycle -------------------------------------------------

    def start(self) -> None:
        """Start the watchdog observer; calling it twice is a no-op."""
        if self._observer is not None:
            return
        observer = Observer()
        observer.schedule(self, str(self.root), recursive=True)
        observer.daemon = True
        observer.start()
        self._observer = observer

    def stop(self) -> None:
        """Stop and join the observer; safe to call twice, or without ``start``."""
        observer, self._observer = self._observer, None
        if observer is None:
            return
        observer.stop()
        observer.join(timeout=5)

    # --- watchdog callbacks -------------------------------------------------

    def on_created(self, event) -> None:  # noqa: ANN001 - watchdog's event type
        self._dispatch(event, "created")

    def on_modified(self, event) -> None:  # noqa: ANN001
        self._dispatch(event, "modified")

    def on_deleted(self, event) -> None:  # noqa: ANN001
        self._dispatch(event, "deleted")

    def on_moved(self, event) -> None:  # noqa: ANN001
        self._dispatch(event, "renamed")

    def _dispatch(self, event, action: str) -> None:  # noqa: ANN001
        if getattr(event, "is_directory", False):
            return
        path = event.dest_path if action == "renamed" else event.src_path
        if not path:
            return
        try:
            self._handle(path, action)
        except Exception:  # never let a watcher thread die on one bad event
            logger.exception("file monitor failed handling %s of %s", action, path)

    # --- policy -------------------------------------------------------------

    def _is_excluded(self, relative: str) -> bool:
        for prefix in self._prefixes:
            if (
                relative == prefix
                or relative.startswith(prefix + "/")
                or f"/{prefix}/" in f"/{relative}"
            ):
                return True
        name = relative.rsplit("/", 1)[-1]
        return any(fnmatch.fnmatch(name, pattern) for pattern in self._globs)

    def _try_accept(self, relative: str, now: float | None = None) -> bool:
        """Debounce + rate-limit one path; overflow increments ``dropped``."""
        moment = time.monotonic() if now is None else now
        last = self._last_accept.get(relative)
        if last is not None and (moment - last) < self.debounce_s:
            self.dropped += 1
            return False
        window = self._window[relative]
        cutoff = moment - 1.0
        while window and window[0] <= cutoff:
            window.popleft()
        if len(window) >= self.max_rate:
            self.dropped += 1
            return False
        window.append(moment)
        self._last_accept[relative] = moment
        return True

    def _settled_size(self, path: str) -> int | None:
        """Final size of ``path``, or ``None`` while it is still being written.

        ``0`` means "gone" (a delete event whose file has already disappeared).
        """
        previous = -1
        for _ in range(self.settle_polls + 1):
            try:
                current = os.path.getsize(path)
            except OSError:
                return 0
            if current > self.max_file_bytes:
                return current  # already past the cap: no reason to wait for the rest
            if current == previous:
                return current
            previous = current
            if self.settle_s > 0:
                time.sleep(self.settle_s)
        return None

    def _handle(self, path: str, action: str) -> None:
        real = os.path.realpath(path)
        if not is_within(Path(real), self.root) or Path(real) == self.root:
            return  # symlink escaping the project root: dropped
        relative = os.path.relpath(real, str(self.root)).replace(os.sep, "/")
        if self._is_excluded(relative):
            return

        size = 0
        if action != "deleted":
            settled = self._settled_size(real)
            if settled is None:
                return  # still growing: a later event will report the final size
            size = settled
            if size > self.max_file_bytes:
                return

        if not self._try_accept(relative):
            return

        self._record(relative, action, size)

    # --- persistence --------------------------------------------------------

    def _record(self, relative: str, action: str, size: int) -> None:
        change = FileChange(
            ts=utc_now(),
            project_id=self.project_id,
            path_relative=relative,
            action=action,
            size_bytes=size,
            git_status=None,
            source="watchdog",
        )
        session = self._session_factory()
        try:
            session.add(change)
            session.commit()
        finally:
            session.close()
        persist_event(
            self._session_factory,
            type="file.changed",
            source="watchdog",
            message=f"{action}: {relative}",
            payload={"path": relative, "action": action, "size_bytes": size},
            project_id=self.project_id,
        )
