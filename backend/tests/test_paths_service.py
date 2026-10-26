"""Unit tests for the project path validation service (M3, Task 1).

Every registered project path must be absolute, traversal-free, outside
``AGENTSCOPE_HOME``, and an existing readable directory — otherwise no DB row and
no watcher may ever be created for it.
"""

from __future__ import annotations

import os
import urllib.parse
from pathlib import Path

import app.config as config
import pytest
from app.services import paths as paths_service

WIN = os.name == "nt"


@pytest.fixture
def home(tmp_path_factory, monkeypatch):
    """Point ``AGENTSCOPE_HOME`` at a temp directory for the policy checks."""
    home_dir = tmp_path_factory.mktemp("agentscope-home")
    monkeypatch.setenv("AGENTSCOPE_HOME", str(home_dir))
    monkeypatch.setattr(config, "SETTINGS", None)
    return home_dir


def make_dir_link(link: Path, target: Path) -> None:
    """Create a directory link, falling back to a Windows junction.

    ``os.symlink`` needs a privilege most developer machines lack; a junction
    resolves through ``realpath`` the same way and needs none.
    """
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError, AttributeError):
        pass
    if os.name == "nt":
        import _winapi

        try:
            _winapi.CreateJunction(str(target), str(link))
            return
        except (OSError, AttributeError) as exc:
            pytest.skip(f"directory links unavailable: {exc}")
    pytest.skip("directory links unavailable on this platform")


# --- traversal ------------------------------------------------------------


def test_traversal_percent_encoded_decoded_by_caller(home):
    raw = urllib.parse.unquote("..%2F..%2Fetc")
    result = paths_service.validate_project_path(raw)
    assert result.ok is False
    assert "traversal" in result.errors
    assert result.path is None


def test_traversal_parent_segment_in_absolute_path(tmp_path, home):
    raw = str(tmp_path / ".." / "escape")
    result = paths_service.validate_project_path(raw)
    assert result.ok is False
    assert "traversal" in result.errors
    assert result.path is None


def test_traversal_windows_drive_path(tmp_path, home):
    result = paths_service.validate_project_path(r"C:\Windows\..\Windows\System32")
    assert result.ok is False
    assert "traversal" in result.errors


def test_traversal_from_percent_encoding_left_undecoded(tmp_path, home):
    # A caller that forgot to decode still must not smuggle separators through.
    result = paths_service.validate_project_path(str(tmp_path) + r"\..\..%2F..\etc")
    assert result.ok is False
    assert "traversal" in result.errors


# --- shape ----------------------------------------------------------------


def test_relative_path_rejected(tmp_path, home):
    result = paths_service.validate_project_path("some/relative/dir")
    assert result.ok is False
    assert "not_absolute" in result.errors


def test_unc_path_rejected(home):
    result = paths_service.validate_project_path(r"\\server\share\proj")
    assert result.ok is False
    assert "unc_or_device" in result.errors


def test_device_path_rejected(home):
    result = paths_service.validate_project_path(r"\\?\C:\x")
    assert result.ok is False
    assert "unc_or_device" in result.errors


def test_filesystem_root_rejected(home):
    root = "C:\\" if WIN else "/"
    result = paths_service.validate_project_path(root)
    assert result.ok is False
    assert "filesystem_root" in result.errors


def test_empty_path_rejected(home):
    result = paths_service.validate_project_path("")
    assert result.ok is False
    assert result.errors


# --- application home boundary -------------------------------------------


def test_path_inside_app_home_rejected(tmp_path, home):
    inside = home / "projects" / "demo"
    result = paths_service.validate_project_path(str(inside))
    assert result.ok is False
    assert "inside_app_home" in result.errors
    assert result.path is None


def test_path_equal_to_app_home_rejected(home):
    result = paths_service.validate_project_path(str(home))
    assert result.ok is False
    assert "inside_app_home" in result.errors


def test_symlink_into_app_home_rejected(tmp_path, home):
    """``realpath`` is applied: a link outside home pointing inside is refused."""
    target = home / "data"
    target.mkdir(parents=True, exist_ok=True)
    link = tmp_path / "looks-innocent"
    make_dir_link(link, target)
    result = paths_service.validate_project_path(str(link))
    assert result.ok is False
    assert "inside_app_home" in result.errors


def test_valid_directory_outside_home_accepted(tmp_path, home):
    project = tmp_path / "proj"
    project.mkdir()
    result = paths_service.validate_project_path(str(project))
    assert result.ok is True
    assert result.errors == []
    assert result.path is not None
    assert result.path.is_dir()
    assert result.path_key is not None


# --- existence / readability ---------------------------------------------


def test_nonexistent_path_not_found(tmp_path, home):
    result = paths_service.validate_project_path(str(tmp_path / "nope"))
    assert result.ok is False
    assert "not_found" in result.errors


def test_nonexistent_path_allowed_when_existence_not_required(tmp_path, home):
    result = paths_service.validate_project_path(str(tmp_path / "nope"), must_exist=False)
    assert result.ok is True
    assert result.path_key


def test_plain_file_not_a_directory(tmp_path, home):
    target = tmp_path / "file.txt"
    target.write_text("x", encoding="utf-8")
    result = paths_service.validate_project_path(str(target))
    assert result.ok is False
    assert "not_a_directory" in result.errors


def test_permission_error_from_stat_maps_to_permission_denied(tmp_path, home, monkeypatch):
    locked = tmp_path / "locked"
    locked.mkdir()

    def deny(path):
        if Path(path) == locked:
            raise PermissionError(13, "Permission denied")
        return Path(path).exists()

    monkeypatch.setattr(paths_service, "_exists", deny)
    result = paths_service.validate_project_path(str(locked))
    assert result.ok is False
    assert "permission_denied" in result.errors


@pytest.mark.skipif(WIN, reason="Windows chmod does not remove read access")
def test_unreadable_directory_maps_to_permission_denied(tmp_path, home):
    locked = tmp_path / "locked"
    locked.mkdir()
    os.chmod(locked, 0o000)
    try:
        result = paths_service.validate_project_path(str(locked))
        assert result.ok is False
        assert "permission_denied" in result.errors
    finally:
        os.chmod(locked, 0o755)


# --- path_key -------------------------------------------------------------


def test_path_key_casefolds_on_windows():
    if not WIN:
        pytest.skip("case-insensitive path keys are a Windows concern")
    assert paths_service.path_key_for(Path(r"C:\Foo")) == paths_service.path_key_for(
        Path(r"c:\foo")
    )


def test_path_key_resolves_parent_segments(tmp_path):
    plain = tmp_path / "b"
    assert paths_service.path_key_for(tmp_path / "a" / ".." / "b") == paths_service.path_key_for(
        plain
    )


def test_path_key_is_a_string(tmp_path):
    key = paths_service.path_key_for(tmp_path)
    assert isinstance(key, str)
    assert key == paths_service.path_key_for(tmp_path)


# --- git detection --------------------------------------------------------


def test_is_git_worktree_true_for_initialized_repo(tmp_path):
    git_path = tmp_path / "repo"
    git_path.mkdir()
    from git import Repo

    Repo.init(git_path)
    assert paths_service.is_git_worktree(git_path) is True


def test_is_git_worktree_false_for_plain_directory(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert paths_service.is_git_worktree(plain) is False


def test_is_git_worktree_true_for_worktree_file_marker(tmp_path):
    """Submodules/worktrees store ``.git`` as a file, not a directory."""
    marked = tmp_path / "worktree"
    marked.mkdir()
    (marked / ".git").write_text("gitdir: ../.git/worktrees/x\n", encoding="utf-8")
    assert paths_service.is_git_worktree(marked) is True

# --- pasted quotes and human wording ----------------------------------------


def test_surrounding_quotes_are_trimmed(tmp_path, home):
    """A path copied out of a terminal keeps its quotes; the policy tolerates them."""
    target = tmp_path / "quoted"
    target.mkdir()
    for raw in (f'"{target}"', f"'{target}'", f"`{target}`", f'  "{target}"  '):
        result = paths_service.validate_project_path(raw)
        assert result.ok is True, (raw, result.errors)
        assert result.path == Path(os.path.realpath(str(target)))


def test_quote_only_input_is_not_absolute(home):
    for raw in ('""', "'", "`", '  "  '):
        result = paths_service.validate_project_path(raw)
        assert result.ok is False
        assert "not_absolute" in result.errors


def test_error_messages_cover_the_whole_code_vocabulary():
    missing = [
        code
        for code in paths_service.ERROR_CODES
        if not paths_service.ERROR_MESSAGES.get(code)
    ]
    assert missing == []
    assert all(len(text) > 10 for text in paths_service.ERROR_MESSAGES.values())
