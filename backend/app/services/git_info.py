"""Read-only git inspection for registered project directories.

Everything in this module only *reads* a repository: ``Repo(...)``,
``git status --porcelain``, ``iter_commits``, ``git show`` and ``git diff``.
No index manipulation, no commits, no checkouts, no ``chdir`` — a monitored
project must never be modified by the monitor, and the acceptance tests assert
that byte for byte.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.services.paths import is_git_worktree, is_within
from app.services.redact import redact_email
from git import Repo
from git.exc import GitCommandError, InvalidGitRepositoryError, NoSuchPathError

__all__ = ["GitCommitInfo", "GitInfo", "commit_diff", "file_diff", "inspect_repo"]

_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_MAX_FILES_PER_COMMIT = 100
_MAX_ERROR_LEN = 200

#: Passed to ``git status`` so it may not take git's *optional* locks — see the
#: comment at the call site. GitPython merges this into the process environment.
_READ_ONLY_ENV = {"GIT_OPTIONAL_LOCKS": "0"}


@dataclass
class GitCommitInfo:
    """One commit as surfaced by the API (e-mail always redacted)."""

    sha: str
    short_sha: str
    author_name: str
    author_email_redacted: str
    committed_at: str
    subject: str
    files: list[str] = field(default_factory=list)


@dataclass
class GitInfo:
    """Snapshot of a repository's current state; ``available`` is the health flag."""

    is_git: bool = False
    available: bool = False
    error: str | None = None
    branch: str | None = None
    default_branch: str | None = None
    status_counts: dict[str, int] = field(
        default_factory=lambda: {"modified": 0, "untracked": 0, "staged": 0, "conflicted": 0}
    )
    modified_files: list[str] = field(default_factory=list)
    untracked_files: list[str] = field(default_factory=list)
    last_commits: list[GitCommitInfo] = field(default_factory=list)


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat().replace("+00:00", "Z")


def _sanitize(error: BaseException, root: Path) -> str:
    """First line of the error, project path masked, hard length cap."""
    lines = str(error).strip().splitlines() or [error.__class__.__name__]
    text = lines[0].replace(str(root), "<project>")
    return text[:_MAX_ERROR_LEN] or error.__class__.__name__


def _apply_status(info: GitInfo, porcelain: str) -> None:
    for line in porcelain.splitlines():
        if len(line) < 3:
            continue
        x, y = line[0], line[1]
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ")[-1]
        path = path.strip().strip('"')
        if x == "?" and y == "?":
            info.status_counts["untracked"] += 1
            info.untracked_files.append(path)
            continue
        if "U" in (x, y) or (x, y) in {("A", "A"), ("D", "D")}:
            info.status_counts["conflicted"] += 1
            continue
        if x != " ":
            info.status_counts["staged"] += 1
        if y != " ":
            info.status_counts["modified"] += 1
            info.modified_files.append(path)


def _branch(repo: Repo) -> str:
    """Current branch name; ``symbolic-ref`` also works on an unborn HEAD."""
    try:
        ref = repo.git.symbolic_ref("--quiet", "HEAD").strip()
    except Exception:  # detached HEAD exits non-zero
        return "HEAD detached"
    prefix = "refs/heads/"
    return ref[len(prefix) :] if ref.startswith(prefix) else ref


def _default_branch(repo: Repo) -> str | None:
    try:
        ref = repo.git.symbolic_ref("--quiet", "refs/remotes/origin/HEAD").strip()
    except Exception:  # no origin/HEAD configured
        return None
    return ref.rsplit("/", 1)[-1] if ref else None


def _recent_commits(repo: Repo, limit: int) -> list[GitCommitInfo]:
    if limit <= 0:
        return []
    commits: list[GitCommitInfo] = []
    for commit in repo.iter_commits(max_count=limit):
        try:
            files = list(commit.stats.files)[:_MAX_FILES_PER_COMMIT]
        except Exception:  # stats are best-effort
            files = []
        author = commit.author
        subject = (commit.summary or "").strip().splitlines()
        commits.append(
            GitCommitInfo(
                sha=commit.hexsha,
                short_sha=commit.hexsha[:8],
                author_name=(author.name or "") if author else "",
                author_email_redacted=redact_email(author.email or "") if author else "***",
                committed_at=_iso(commit.committed_date),
                subject=subject[0][:_MAX_ERROR_LEN] if subject else "",
                files=files,
            )
        )
    return commits


def inspect_repo(path: Path, *, commits: int = 5) -> GitInfo:
    """Inspect the repository at ``path``. Never raises; errors become ``error``."""
    root = Path(path)
    if not is_git_worktree(root):
        code = "not_found" if not root.exists() else "not_a_git_repository"
        return GitInfo(is_git=False, available=False, error=code)

    try:
        repo = Repo(str(root), search_parent_directories=False)
    except (InvalidGitRepositoryError, NoSuchPathError):
        return GitInfo(is_git=True, available=False, error="not_a_git_repository")
    except Exception as exc:  # repo locked, unreadable, ...
        return GitInfo(is_git=True, available=False, error=_sanitize(exc, root))

    info = GitInfo(is_git=True)
    try:
        # ``GIT_OPTIONAL_LOCKS=0`` keeps the poll strictly read-only: plain
        # ``git status`` rewrites ``.git/index`` (stat cache) even when nothing
        # changed, and a monitor must not modify the project it watches.
        _apply_status(info, repo.git.status("--porcelain", env=_READ_ONLY_ENV))
        info.branch = _branch(repo)
        info.default_branch = _default_branch(repo)
        info.last_commits = _recent_commits(repo, commits)
        info.available = True
    except Exception as exc:  # surface as status, never crash
        info.error = _sanitize(exc, root)
    return info


def commit_diff(path: Path, sha: str) -> list[str]:
    """Files changed by ``sha``. Read-only; ``sha`` must look like a full hash."""
    if not _SHA_RE.match(sha or ""):
        raise ValueError("invalid commit sha")
    root = Path(path)
    try:
        repo = Repo(str(root), search_parent_directories=False)
    except (InvalidGitRepositoryError, NoSuchPathError) as exc:
        raise ValueError("not a git repository") from exc
    try:
        out = repo.git.show("--name-only", "--pretty=format:", sha)
    except GitCommandError as exc:
        raise ValueError("commit not found") from exc
    return [line for line in out.splitlines() if line.strip()]


def file_diff(path: Path, relpath: str, *, staged: bool = False) -> str:
    """Diff for one file inside the repository, rejecting any escaping ``relpath``."""
    if not relpath or relpath.startswith(("/", "\\")) or _DRIVE_RE.match(relpath):
        raise ValueError("path escapes repository root")
    if ".." in relpath.replace("/", "\\").split("\\") or Path(relpath).is_absolute():
        raise ValueError("path escapes repository root")

    root = Path(path)
    real_root = Path(os.path.realpath(str(root)))
    target = Path(os.path.realpath(str(root / relpath)))
    if not is_within(target, real_root) or target == real_root:
        raise ValueError("path escapes repository root")

    try:
        repo = Repo(str(root), search_parent_directories=False)
    except (InvalidGitRepositoryError, NoSuchPathError) as exc:
        raise ValueError("not a git repository") from exc

    rel = target.relative_to(real_root).as_posix() if target.is_relative_to(real_root) else relpath
    try:
        if staged:
            return repo.git.diff("--cached", "--", rel)
        return repo.git.diff("--", rel)
    except GitCommandError as exc:
        raise ValueError(_sanitize(exc, root)) from exc
