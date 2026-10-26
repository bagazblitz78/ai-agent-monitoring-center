"""Read-only discovery of agent projects underneath a root directory.

Registering a whole tree of repositories one path at a time is the slow part
of using AgentScope, so :func:`discover_candidates` scans a root and reports
what is underneath it. The rules mirror the single-file edition, with the
backend's path policy layered on top:

* a directory qualifies when it is the **root of a git work tree** (so a
  subfolder of a repository is never proposed as a project of its own) or when
  it holds an **agent marker** (``.opencode/``, ``CLAUDE.md``, …);
* once something qualifies the walk stops there — everything inside belongs to
  that project;
* dependency, build and VCS subtrees are pruned, symlinks are never followed,
  and each directory is visited at most once (a junction back to an ancestor
  cannot re-walk the tree);
* candidates inside ``AGENTSCOPE_HOME`` are dropped: the path policy refuses
  them, so proposing them would only produce failures;
* the walk is bounded (``DISCOVER_MAX_VISITED`` directories,
  ``DISCOVER_MAX_CANDIDATES`` proposals) and says so through ``truncated``
  rather than silently losing projects.

Nothing here writes, creates or moves: it ``iterdir``s, ``realpath``s and — only
for directories that actually carry a ``.git`` marker — opens a read-only
``Repo``. The tests assert the tree byte for byte before and after.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from app.config import resolve_paths
from app.services.paths import (
    ERROR_MESSAGES,
    is_git_worktree,
    is_within,
    path_key_for,
    validate_project_path,
)
from git import Repo
from git.exc import GitCommandError, InvalidGitRepositoryError, NoSuchPathError

__all__ = [
    "DISCOVER_DEPTH",
    "DISCOVER_MARKER_DIRS",
    "DISCOVER_MARKER_FILES",
    "DISCOVER_MAX_CANDIDATES",
    "DISCOVER_MAX_VISITED",
    "DISCOVER_PRUNE_DIRS",
    "Candidate",
    "DiscoveryResult",
    "discover_candidates",
]

#: Levels below the root that a default walk scans.
DISCOVER_DEPTH = 4
#: Directories opened by a single walk before it stops and reports ``truncated``.
DISCOVER_MAX_VISITED = 5000
#: Proposals a single walk returns before it stops and reports ``truncated``.
DISCOVER_MAX_CANDIDATES = 100

#: Subtrees the walk never descends into: dependencies, build output, VCS
#: internals, recycle bins — listing any of them would be pure noise.
DISCOVER_PRUNE_DIRS = frozenset(
    {
        "node_modules",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        "dist",
        "build",
        "target",
        ".next",
        ".git",
        ".hg",
        ".svn",
        "vendor",
        "site-packages",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".cache",
        ".idea",
        ".vscode",
        "$RECYCLE.BIN",
        "System Volume Information",
    }
)
#: Agent-owned marker *directories*: a folder holding one is a project even
#: without a repository.
DISCOVER_MARKER_DIRS = frozenset({".aider", ".claude", ".codex", ".cursor", ".gemini", ".opencode"})
#: Agent-owned marker *files*, checked when no marker directory matched.
DISCOVER_MARKER_FILES = frozenset(
    {
        "AGENTS.md",
        "CLAUDE.md",
        "GEMINI.md",
        "opencode.json",
        ".aider.conf.yml",
        ".cursorrules",
    }
)


@dataclass(frozen=True)
class Candidate:
    """One directory the walk proposes as a project."""

    path: Path
    path_key: str
    name: str
    #: Why it qualified: ``"git"``, a marker directory (``".opencode/"``) or a
    #: marker file (``"CLAUDE.md"``).
    reason: str
    already_registered: bool = False

    def as_dict(self) -> dict[str, object]:
        """JSON-ready view used by the API layer."""
        return {
            "path": str(self.path),
            "path_key": self.path_key,
            "name": self.name,
            "reason": self.reason,
            "already_registered": self.already_registered,
        }


@dataclass(frozen=True)
class DiscoveryResult:
    """Outcome of :func:`discover_candidates`."""

    ok: bool
    root: Path | None
    path_key: str | None
    depth: int
    scanned: int
    truncated: bool
    candidates: list[Candidate] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        """JSON-ready view used by the API layer."""
        return {
            "root": str(self.root) if self.root is not None else None,
            "path_key": self.path_key,
            "depth": self.depth,
            "scanned": self.scanned,
            "truncated": self.truncated,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "errors": list(self.errors),
            "messages": [ERROR_MESSAGES.get(code, code) for code in self.errors],
        }


def _repo_root(directory: Path) -> bool:
    """True only for the *top level* of a git work tree.

    ``is_git_worktree`` is the cheap pre-filter that keeps ordinary folders
    from opening a repository at all; the ``working_tree_dir`` comparison is
    what stops ``repo/src`` from being reported as a repository of its own.
    """
    if not is_git_worktree(directory):
        return False
    try:
        repo = Repo(str(directory), search_parent_directories=False)
    except (InvalidGitRepositoryError, NoSuchPathError, GitCommandError, OSError):
        return False
    top = repo.working_tree_dir
    if not top:
        return False
    return os.path.realpath(top) == os.path.realpath(str(directory))


def _candidate_reason(directory: Path) -> str | None:
    """Why ``directory`` is an agent project, or ``None`` when it is not."""
    if _repo_root(directory):
        return "git"
    for name in sorted(DISCOVER_MARKER_DIRS):
        try:
            if (directory / name).is_dir():
                return f"{name}/"
        except OSError:
            continue
    for name in sorted(DISCOVER_MARKER_FILES):
        try:
            if (directory / name).is_file():
                return name
        except OSError:
            continue
    return None


def discover_candidates(
    raw: str,
    *,
    depth: int = DISCOVER_DEPTH,
    registered_keys: Iterable[str] = (),
    max_visited: int = DISCOVER_MAX_VISITED,
    max_candidates: int = DISCOVER_MAX_CANDIDATES,
) -> DiscoveryResult:
    """Scan ``raw`` read-only and propose the agent projects underneath it.

    ``registered_keys`` are :func:`app.services.paths.path_key_for` values of
    already registered projects: they come back flagged
    (``already_registered=True``) instead of being dropped, so a caller can
    show them as "already there".
    """
    validation = validate_project_path(raw)
    walk_depth = max(0, int(depth))
    if not validation.ok or validation.path is None:
        return DiscoveryResult(
            ok=validation.ok,
            root=validation.path,
            path_key=validation.path_key,
            depth=walk_depth,
            scanned=0,
            truncated=False,
            errors=list(validation.errors),
        )

    home = Path(os.path.realpath(str(resolve_paths().home)))
    known = set(registered_keys)
    visited: set[str] = set()
    candidates: list[Candidate] = []
    queue: list[tuple[Path, int]] = [(validation.path, 0)]
    scanned = 0
    truncated = False

    while queue:
        if scanned >= max_visited:
            truncated = True
            break
        directory, level = queue.pop(0)  # breadth first: shallow projects first
        key = path_key_for(directory)
        if key in visited:
            continue  # reached another way (junction, case variant): one visit is enough
        visited.add(key)
        scanned += 1

        reason = _candidate_reason(directory)
        if reason:
            if len(candidates) >= max_candidates:
                truncated = True
                break
            if not is_within(directory, home):
                resolved = Path(os.path.realpath(str(directory)))
                candidates.append(
                    Candidate(
                        path=resolved,
                        path_key=key,
                        name=resolved.name or str(resolved),
                        reason=reason,
                        already_registered=key in known,
                    )
                )
            continue  # do not descend: the contents belong to this project

        if level >= walk_depth:
            continue
        try:
            children = sorted(directory.iterdir(), key=lambda child: child.name.lower())
        except OSError:
            continue  # unreadable here: skip it rather than fail the walk
        for child in children:
            if child.name in DISCOVER_PRUNE_DIRS:
                continue
            try:
                if child.is_symlink() or not child.is_dir():
                    continue
            except OSError:
                continue
            queue.append((child, level + 1))

    return DiscoveryResult(
        ok=True,
        root=validation.path,
        path_key=validation.path_key,
        depth=walk_depth,
        scanned=scanned,
        truncated=truncated,
        candidates=candidates,
        errors=[],
    )
