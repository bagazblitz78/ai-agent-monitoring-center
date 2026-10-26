"""Project path validation.

Every path that a caller wants to register as a monitored project must pass
through :func:`validate_project_path` **before** any database row is written or
any filesystem watcher starts. The rules are deliberately conservative:

* absolute input only, never a raw ``..`` segment (checked on the raw and on the
  percent-decoded form, so an undecoded ``%2f`` cannot smuggle separators);
* no UNC (``\\server\\share``) or device (``\\?\\``) paths;
* no filesystem roots;
* after ``realpath`` resolution, never inside ``AGENTSCOPE_HOME`` — watching our
  own database and logs would be a feedback loop;
* when existence is required: must be an existing, readable directory.

The service never creates, writes, deletes or moves anything: it only stats,
``realpath``s and ``os.access``s.
"""

from __future__ import annotations

import os
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from app.config import resolve_paths

__all__ = [
    "ERROR_CODES",
    "ERROR_MESSAGES",
    "PathValidation",
    "is_git_worktree",
    "is_within",
    "path_key_for",
    "validate_project_path",
]

#: Machine-readable error codes a validation may report.
ERROR_CODES: tuple[str, ...] = (
    "not_absolute",
    "traversal",
    "unc_or_device",
    "filesystem_root",
    "inside_app_home",
    "not_found",
    "not_a_directory",
    "not_readable",
    "permission_denied",
)

#: Human wording for every :data:`ERROR_CODES` entry. The API keeps returning
#: the codes (stable and translatable) and carries this text next to them, so a
#: caller never has to invent its own copy of the wording — one map, one truth.
ERROR_MESSAGES: dict[str, str] = {
    "not_absolute": "use an absolute path, e.g. D:\\code\\my-repo",
    "traversal": "the path must not contain ..",
    "unc_or_device": "network shares (\\\\server\\share) and device paths are not allowed",
    "filesystem_root": "a drive or filesystem root cannot be a project",
    "inside_app_home": "that directory is inside the AgentScope data directory",
    "not_found": "that directory does not exist here",
    "not_a_directory": "that path is a file, not a directory",
    "not_readable": "that directory cannot be read",
    "permission_denied": "that directory cannot be read (permission denied)",
}

#: UNC shares and DOS device paths (``\\server\share``, ``\\?\C:\x``, ``//host/p``).
_DEVICE_PREFIXES = ("\\\\", "//")

_CASEFOLD = os.name == "nt"


@dataclass(frozen=True)
class PathValidation:
    """Outcome of :func:`validate_project_path`."""

    ok: bool
    path: Path | None = None
    path_key: str | None = None
    errors: list[str] = field(default_factory=list)


def _exists(path: Path) -> bool:
    """Existence probe (indirection kept so tests can inject ``PermissionError``)."""
    return path.exists()


def _segments(text: str) -> list[str]:
    """Split on both separators so ``a/b`` and ``a\\b`` are treated alike."""
    return [part for part in text.replace("/", "\\").split("\\") if part]


def _has_traversal(text: str) -> bool:
    if ".." in _segments(text):
        return True
    decoded = urllib.parse.unquote(text)
    return decoded != text and ".." in _segments(decoded)


def _case(text: str) -> str:
    return text.casefold() if _CASEFOLD else text


def is_within(child: Path, parent: Path) -> bool:
    """True when ``child`` is ``parent`` itself or lives underneath it.

    Comparison is casefolded on Windows, where ``D:\\Proj`` and ``d:\\proj`` are
    the same directory.
    """
    child_s = _case(str(child))
    parent_s = _case(str(parent)).rstrip("\\/")
    return (
        child_s == parent_s
        or child_s.startswith(parent_s + os.sep)
        or child_s.startswith(parent_s + "/")
    )


def path_key_for(path: Path) -> str:
    """Return the canonical uniqueness key for ``path``.

    ``realpath`` collapses symlinks and ``..``; the key is casefolded on Windows
    where the filesystem is case-insensitive.
    """
    key = os.path.realpath(str(path))
    return key.casefold() if _CASEFOLD else key


def is_git_worktree(path: Path) -> bool:
    """True when ``path`` contains a ``.git`` marker (directory or worktree file)."""
    try:
        return (Path(path) / ".git").exists()
    except OSError:
        return False


def validate_project_path(raw: str, *, must_exist: bool = True) -> PathValidation:
    """Validate ``raw`` against the project path policy.

    Structural problems (shape, traversal, home boundary) short-circuit with
    ``path=None``; existence problems keep the resolved ``path``/``path_key`` so
    callers can report *which* directory was unusable.

    Surrounding quotes are trimmed first: a path copied out of a shell or a
    terminal keeps them (``"D:\\code"``), and rejecting a well-formed directory
    over a pair of quotes helps nobody.
    """
    text = (raw or "").strip().strip("\"'`").strip()

    errors: list[str] = []
    if text.lstrip().startswith(_DEVICE_PREFIXES):
        errors.append("unc_or_device")
    if _has_traversal(text):
        errors.append("traversal")

    candidate = Path(text) if text else Path(".")
    if not candidate.is_absolute():
        errors.append("not_absolute")

    normalized: Path | None = None
    if not errors:
        normalized = Path(os.path.normpath(text))
        if _case(str(normalized)) == _case(str(normalized.anchor)):
            errors.append("filesystem_root")

    if errors:
        return PathValidation(ok=False, path=None, path_key=None, errors=errors)

    assert normalized is not None  # unreachable: structural errors returned above
    resolved = Path(os.path.realpath(str(normalized)))
    home = Path(os.path.realpath(str(resolve_paths().home)))
    if is_within(resolved, home):
        return PathValidation(
            ok=False, path=None, path_key=None, errors=["inside_app_home"]
        )

    key = path_key_for(resolved)
    if not must_exist:
        return PathValidation(ok=True, path=resolved, path_key=key, errors=[])

    try:
        if not _exists(resolved):
            return PathValidation(
                ok=False, path=resolved, path_key=key, errors=["not_found"]
            )
        if not resolved.is_dir():
            return PathValidation(
                ok=False, path=resolved, path_key=key, errors=["not_a_directory"]
            )
        if not os.access(resolved, os.R_OK):
            # ``not_readable`` stays in the vocabulary for API-level reporting;
            # an existing path that cannot be read is a permission problem.
            return PathValidation(
                ok=False, path=resolved, path_key=key, errors=["permission_denied"]
            )
    except PermissionError:
        return PathValidation(
            ok=False, path=resolved, path_key=key, errors=["permission_denied"]
        )
    except OSError:
        return PathValidation(
            ok=False, path=resolved, path_key=key, errors=["not_readable"]
        )

    return PathValidation(ok=True, path=resolved, path_key=key, errors=[])
