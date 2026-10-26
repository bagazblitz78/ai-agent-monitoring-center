"""Unit tests for the project discovery service (ported from ``agentscope_lite``).

Registering every project by hand is slow, so the backend can scan a root
directory and propose the agent projects underneath it. The walk must obey the
path policy (``AGENTSCOPE_HOME`` stays off limits), must prune dependency and
build trees, must never propose a subfolder of a repository that its own root
already covers — and, like every other service, must not write a single byte
into the tree it reads.
"""

from __future__ import annotations

import os
from pathlib import Path

import app.config as config
import pytest
from app.services import discovery
from app.services.paths import path_key_for
from git import Repo


@pytest.fixture
def home(tmp_path_factory, monkeypatch):
    """Point ``AGENTSCOPE_HOME`` at a temp directory for the policy checks."""
    home_dir = tmp_path_factory.mktemp("agentscope-home")
    monkeypatch.setenv("AGENTSCOPE_HOME", str(home_dir))
    monkeypatch.setattr(config, "SETTINGS", None)
    return home_dir


def _repo(path: Path) -> Path:
    """A minimal committed git repository at ``path`` (so it is a real root)."""
    path.mkdir(parents=True, exist_ok=True)
    repo = Repo.init(path)
    with repo.config_writer() as writer:
        writer.set_value("user", "name", "Test User")
        writer.set_value("user", "email", "tester@example.com")
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    repo.index.add(["seed.txt"])
    repo.index.commit("seed")
    return path


def _make_link(source: Path, target: Path) -> None:
    """Directory link at ``target`` pointing at ``source`` (junction fallback)."""
    try:
        os.symlink(source, target, target_is_directory=True)
        return
    except (OSError, NotImplementedError, AttributeError):
        pass
    if os.name == "nt":
        import _winapi

        try:
            _winapi.CreateJunction(str(source), str(target))
            return
        except (OSError, AttributeError) as exc:
            pytest.skip(f"directory links unavailable: {exc}")
    pytest.skip("directory links unavailable on this platform")


def _names(result: discovery.DiscoveryResult) -> list[str]:
    return [candidate.name for candidate in result.candidates]


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_dir():
            out[rel] = (path.stat().st_mtime_ns, -1)
        else:
            stat = path.stat()
            out[rel] = (stat.st_mtime_ns, stat.st_size)
    return out


# --- what counts as a candidate ---------------------------------------------


def test_a_plain_tree_has_nothing_to_propose(tmp_path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alpha" / "main.py").write_text("x = 1\n", encoding="utf-8")

    result = discovery.discover_candidates(str(tmp_path))

    assert result.ok is True
    assert result.errors == []
    assert result.candidates == []
    assert result.scanned == 2
    assert result.truncated is False


def test_nested_git_repos_are_proposed_shallowest_first(tmp_path):
    _repo(tmp_path / "alpha")
    _repo(tmp_path / "team" / "beta")

    result = discovery.discover_candidates(str(tmp_path))

    assert _names(result) == ["alpha", "beta"]
    assert [c.reason for c in result.candidates] == ["git", "git"]


def test_the_root_itself_is_a_candidate_and_stops_the_walk(tmp_path):
    _repo(tmp_path)

    result = discovery.discover_candidates(str(tmp_path))

    assert _names(result) == [tmp_path.name]
    assert result.scanned == 1  # its children belong to it


def test_a_folder_inside_a_repo_is_not_a_repo_root(tmp_path):
    repo = _repo(tmp_path / "alpha")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("pass\n", encoding="utf-8")

    result = discovery.discover_candidates(str(repo / "src"))

    assert result.ok is True
    assert result.candidates == []


def test_agent_marker_directories_and_files_are_candidates(tmp_path):
    (tmp_path / "chat" / ".opencode").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "CLAUDE.md").write_text("# rules\n", encoding="utf-8")
    (tmp_path / "plain").mkdir()

    result = discovery.discover_candidates(str(tmp_path))

    assert _names(result) == ["chat", "docs"]
    assert [c.reason for c in result.candidates] == [".opencode/", "CLAUDE.md"]


# --- pruning, depth, caps ----------------------------------------------------


def test_dependency_trees_are_pruned(tmp_path):
    _repo(tmp_path / "app" / "node_modules" / "left-pad")
    (tmp_path / "app" / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / "app" / "build" / "out").mkdir(parents=True)

    result = discovery.discover_candidates(str(tmp_path))

    assert result.candidates == []
    assert result.scanned == 2  # root and app; the noise was never entered


def test_depth_counts_levels_below_the_root(tmp_path):
    _repo(tmp_path / "shallow")
    _repo(tmp_path / "a" / "b" / "deep")

    shallow_only = discovery.discover_candidates(str(tmp_path), depth=2)
    both = discovery.discover_candidates(str(tmp_path), depth=4)

    assert _names(shallow_only) == ["shallow"]
    assert _names(both) == ["shallow", "deep"]
    assert discovery.discover_candidates(str(tmp_path), depth=0).candidates == []


def test_already_registered_projects_are_flagged_not_dropped(tmp_path):
    alpha = _repo(tmp_path / "alpha")
    _repo(tmp_path / "beta")

    result = discovery.discover_candidates(
        str(tmp_path), registered_keys=[path_key_for(alpha)]
    )

    assert _names(result) == ["alpha", "beta"]
    assert [c.already_registered for c in result.candidates] == [True, False]


def test_the_visit_cap_stops_the_walk_and_says_so(tmp_path):
    for index in range(6):
        (tmp_path / f"dir{index}").mkdir()

    result = discovery.discover_candidates(str(tmp_path), max_visited=3)

    assert result.scanned == 3
    assert result.truncated is True


def test_the_candidate_cap_reports_the_overflow(tmp_path):
    for name in ("alpha", "beta", "gamma"):
        _repo(tmp_path / name)

    result = discovery.discover_candidates(str(tmp_path), max_candidates=2)

    assert _names(result) == ["alpha", "beta"]
    assert result.truncated is True


# --- path policy and invariants ----------------------------------------------


def test_candidates_inside_app_home_are_excluded(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTSCOPE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(config, "SETTINGS", None)
    _repo(tmp_path / "home" / "internal")
    _repo(tmp_path / "visible")

    result = discovery.discover_candidates(str(tmp_path))

    assert _names(result) == ["visible"]


def test_a_directory_reachable_twice_is_proposed_once(tmp_path):
    real = tmp_path / "real"
    (real / ".claude").mkdir(parents=True)
    _make_link(real, tmp_path / "link")

    result = discovery.discover_candidates(str(tmp_path))

    assert len(result.candidates) == 1, result.candidates
    assert result.candidates[0].path_key == path_key_for(real)


def test_a_cyclic_tree_does_not_re_walk_itself(tmp_path):
    (tmp_path / "plain").mkdir()
    (tmp_path / "loop").mkdir()
    _make_link(tmp_path, tmp_path / "loop" / "back")

    result = discovery.discover_candidates(str(tmp_path), depth=10)

    assert result.truncated is False, result
    assert result.scanned == 3, result  # root, loop, plain — "back" is the root again


def test_discovery_never_touches_the_tree(tmp_path):
    repo = _repo(tmp_path / "alpha")
    (tmp_path / "chat" / ".claude").mkdir(parents=True)
    (repo / "new.txt").write_text("later\n", encoding="utf-8")
    before = _snapshot(tmp_path)

    discovery.discover_candidates(str(tmp_path))

    assert _snapshot(tmp_path) == before


def test_an_invalid_root_reports_the_path_policy_errors(tmp_path):
    missing = discovery.discover_candidates(str(tmp_path / "nope"))
    relative = discovery.discover_candidates("relative/path")

    assert missing.ok is False and missing.errors == ["not_found"]
    assert relative.ok is False and relative.errors == ["not_absolute"]
    assert missing.candidates == [] and relative.candidates == []


def test_a_quoted_path_is_accepted(tmp_path):
    repo = _repo(tmp_path / "spaced repo")

    result = discovery.discover_candidates(f'  "{repo}"  ')

    assert result.ok is True
    assert result.candidates[0].path == Path(os.path.realpath(str(repo)))
