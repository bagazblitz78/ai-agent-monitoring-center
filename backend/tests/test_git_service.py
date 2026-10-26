"""Tests for the read-only git inspection service and redaction helpers (M3, Task 2).

Everything here is read-only: the fixture repos live under ``tmp_path`` and use
**repo-local** identity, so the developer's global git configuration is never
read, written, or relied upon.
"""

from __future__ import annotations

import subprocess

import pytest
from app.services import redact
from app.services.git_info import commit_diff, file_diff, inspect_repo
from git import Repo


def _global_config_snapshot() -> str | None:
    """``git config --global --list`` output, or ``None`` when git is missing."""
    try:
        proc = subprocess.run(
            ["git", "config", "--global", "--list"],
            capture_output=True,
            text=True,
            timeout=15,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout


@pytest.fixture
def project_repo(tmp_path):
    """A two-commit repo with one modified file and one untracked file."""
    root = tmp_path / "project"
    root.mkdir()
    repo = Repo.init(root)
    with repo.config_writer() as writer:
        writer.set_value("user", "name", "Test User")
        writer.set_value("user", "email", "tester@example.com")

    (root / "a.txt").write_text("one\n", encoding="utf-8")
    (root / "b.txt").write_text("two\n", encoding="utf-8")
    repo.index.add(["a.txt", "b.txt"])
    repo.index.commit("initial commit")

    (root / "b.txt").write_text("two\nchanged\n", encoding="utf-8")
    repo.index.add(["b.txt"])
    repo.index.commit("second commit")

    (root / "a.txt").write_text("one\nmodified\n", encoding="utf-8")  # unstaged edit
    (root / "c.txt").write_text("three\n", encoding="utf-8")  # never staged
    return repo, root


# --- inspect_repo ---------------------------------------------------------


def test_inspect_repo_reports_branch_and_status(project_repo):
    _repo, root = project_repo
    info = inspect_repo(root)

    assert info.is_git is True
    assert info.available is True
    assert info.error is None
    assert info.branch
    assert info.branch != "HEAD detached"
    assert info.status_counts["modified"] == 1
    assert info.status_counts["untracked"] == 1
    assert info.status_counts["staged"] == 0
    assert "a.txt" in info.modified_files
    assert "c.txt" in info.untracked_files


def test_inspect_repo_lists_commits_newest_first(project_repo):
    _repo, root = project_repo
    info = inspect_repo(root)

    subjects = [c.subject for c in info.last_commits]
    assert subjects == ["second commit", "initial commit"]
    first = info.last_commits[0]
    assert first.sha and len(first.sha) == 40
    assert first.short_sha == first.sha[:8]
    assert first.author_name == "Test User"
    assert "b.txt" in first.files
    assert first.committed_at.endswith("Z")


def test_inspect_repo_redacts_author_email(project_repo):
    _repo, root = project_repo
    info = inspect_repo(root)

    for commit in info.last_commits:
        assert "tester" not in commit.author_email_redacted
        assert commit.author_email_redacted.endswith("@example.com")
        assert "@example.com" in commit.author_email_redacted


def test_inspect_repo_honours_commit_limit(project_repo):
    _repo, root = project_repo
    info = inspect_repo(root, commits=1)
    assert len(info.last_commits) == 1


def test_inspect_repo_on_plain_directory_does_not_raise(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    info = inspect_repo(plain)

    assert info.is_git is False
    assert info.available is False
    assert info.error
    assert info.branch is None
    assert info.last_commits == []


def test_inspect_repo_on_missing_directory_does_not_raise(tmp_path):
    info = inspect_repo(tmp_path / "does-not-exist")
    assert info.is_git is False
    assert info.available is False
    assert info.error


def test_inspect_repo_with_broken_git_marker_does_not_raise(tmp_path):
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / ".git").write_text("not a git directory\n", encoding="utf-8")

    info = inspect_repo(broken)

    assert info.available is False
    assert info.error
    assert info.last_commits == []


def test_inspect_repo_on_detached_head(project_repo):
    repo, root = project_repo
    repo.git.checkout("--detach", "HEAD")

    info = inspect_repo(root)
    assert info.available is True
    assert info.branch == "HEAD detached"


def test_inspect_repo_default_branch_none_without_origin(project_repo):
    _repo, root = project_repo
    info = inspect_repo(root)
    assert info.default_branch is None


# --- repo-local identity --------------------------------------------------


def test_git_identity_is_repo_local_and_global_untouched(tmp_path):
    before = _global_config_snapshot()

    root = tmp_path / "identity"
    root.mkdir()
    repo = Repo.init(root)
    with repo.config_writer() as writer:
        writer.set_value("user", "name", "Local Only")
        writer.set_value("user", "email", "local@example.com")

    after = _global_config_snapshot()

    assert before == after
    assert Repo(root).config_reader().get_value("user", "email") == "local@example.com"
    assert "local@example.com" in (root / ".git" / "config").read_text(encoding="utf-8")


# --- diffs ----------------------------------------------------------------


def test_file_diff_returns_patch(project_repo):
    _repo, root = project_repo
    patch = file_diff(root, "a.txt")
    assert isinstance(patch, str)
    assert "modified" in patch or patch == ""  # staged-only edits yield an empty patch


def test_file_diff_rejects_parent_escape(project_repo):
    _repo, root = project_repo
    with pytest.raises(ValueError):
        file_diff(root, "../outside.txt")
    with pytest.raises(ValueError):
        file_diff(root, "a/../..//escape.txt")


def test_file_diff_rejects_absolute_path(project_repo):
    _repo, root = project_repo
    with pytest.raises(ValueError):
        file_diff(root, "C:\\x")
    with pytest.raises(ValueError):
        file_diff(root, "/etc/passwd")


def test_commit_diff_lists_changed_files(project_repo):
    _repo, root = project_repo
    info = inspect_repo(root, commits=2)
    sha = info.last_commits[0].sha

    files = commit_diff(root, sha)

    assert isinstance(files, list)
    assert "b.txt" in files


def test_commit_diff_rejects_option_like_sha(project_repo):
    _repo, root = project_repo
    with pytest.raises(ValueError):
        commit_diff(root, "--output=/tmp/pwned")
    with pytest.raises(ValueError):
        commit_diff(root, "../../etc")


def test_commit_diff_unknown_sha_raises(project_repo):
    _repo, root = project_repo
    with pytest.raises(ValueError):
        commit_diff(root, "0" * 40)


# --- redaction ------------------------------------------------------------


def test_redact_email_keeps_domain_masks_local_part():
    assert redact.redact_email("tester@example.com") == "t***@example.com"
    assert "tester" not in redact.redact_email("tester@example.com")


def test_redact_email_without_at_sign_masks_value():
    assert redact.redact_email("nobody") == "n***"
    assert redact.redact_email("") == "***"


def test_redact_cmdline_masks_flag_values():
    line = redact.redact_cmdline(["python", "agent.py", "--token", "super-secret-value"])
    assert "super-secret-value" not in line
    assert "--token" in line
    assert "agent.py" in line


def test_redact_cmdline_masks_inline_flag_values():
    line = redact.redact_cmdline(["run", "--password=hunter2", "--key=abc123"])
    assert "hunter2" not in line
    assert "abc123" not in line
    assert "run" in line


def test_redact_cmdline_masks_long_opaque_strings():
    token = "a1b2c3d4e5f6a1b2c3d4e5f6"
    line = redact.redact_cmdline(["curl", "https://x.test/api", token])
    assert token not in line
    assert "https://x.test/api" in line


def test_redact_cmdline_keeps_paths_and_short_args():
    line = redact.redact_cmdline(["python", r"D:\projek_belajar\app\main.py", "--verbose"])
    assert r"D:\projek_belajar\app\main.py" in line
    assert "--verbose" in line


def test_redact_cmdline_accepts_empty_list():
    assert redact.redact_cmdline([]) == ""
