"""Discovery walk of the one-file edition (``agentscope_lite.py``).

Registering every project by hand is the slow part of using the lite monitor,
so it can scan a root directory and propose the agent projects underneath it.
The walk must stay read-only (the project invariant every other test enforces),
must prune dependency/build trees, and must never propose a subfolder of a repo
that is already covered by its own root.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

LITE_PATH = Path(__file__).resolve().parents[2] / "agentscope_lite.py"
_spec = importlib.util.spec_from_file_location("agentscope_lite", LITE_PATH)
lite = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lite)


# -- fixtures ---------------------------------------------------------------

def _link(source: Path, target: Path) -> bool:
    """Directory link at ``target`` pointing at ``source`` (junction on Windows)."""
    if os.name == "nt":
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(target), str(source)],
                                capture_output=True)
        return result.returncode == 0 and target.exists()
    try:
        target.symlink_to(source, target_is_directory=True)
    except OSError:
        return False
    return True

def _git(path: Path) -> Path:
    """A minimal git repository at ``path`` (committed, so it is a real root)."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Tester"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@example.com"],
                   check=True, capture_output=True)
    (path / "seed.txt").write_bytes(b"seed\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "seed"],
                   check=True, capture_output=True)
    return path


def _paths(result: dict) -> list[str]:
    return [Path(c["path"]).name for c in result["candidates"]]


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """(mtime_ns, size) for every file and directory below ``root``."""
    out: dict[str, tuple[int, int]] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_dir():
            out[rel] = (path.stat().st_mtime_ns, -1)
        else:
            stat = path.stat()
            out[rel] = (stat.st_mtime_ns, stat.st_size)
    return out


# -- what counts as a candidate ---------------------------------------------

def test_a_plain_tree_has_nothing_to_propose(tmp_path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alpha" / "main.py").write_bytes(b"x = 1\n")

    result = lite.discover_candidates(str(tmp_path))

    assert result["errors"] == []
    assert result["candidates"] == []
    assert result["scanned"] == 2
    assert result["truncated"] is False


def test_nested_git_repos_are_proposed_shallowest_first(tmp_path):
    _git(tmp_path / "alpha")
    _git(tmp_path / "team" / "beta")

    result = lite.discover_candidates(str(tmp_path))

    assert _paths(result) == ["alpha", "beta"]
    assert [c["reason"] for c in result["candidates"]] == ["git", "git"]


def test_the_root_itself_is_a_candidate_and_stops_the_walk(tmp_path):
    _git(tmp_path)

    result = lite.discover_candidates(str(tmp_path))

    assert _paths(result) == [tmp_path.name]
    assert result["candidates"][0]["reason"] == "git"
    assert result["scanned"] == 1  # its children belong to it


def test_a_folder_inside_a_repo_is_not_a_repo_root(tmp_path):
    """``git rev-parse --show-toplevel`` must equal the directory itself."""
    repo = _git(tmp_path / "alpha")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_bytes(b"pass\n")

    result = lite.discover_candidates(str(repo / "src"))

    assert result["errors"] == []
    assert result["candidates"] == []


def test_agent_marker_directories_and_files_are_candidates(tmp_path):
    (tmp_path / "chat" / ".opencode").mkdir(parents=True)
    (tmp_path / "docs" / "CLAUDE.md").parent.mkdir()
    (tmp_path / "docs" / "CLAUDE.md").write_bytes(b"# rules\n")
    (tmp_path / "plain").mkdir()

    result = lite.discover_candidates(str(tmp_path))

    assert _paths(result) == ["chat", "docs"]
    assert result["candidates"][0]["reason"] == ".opencode/"
    assert result["candidates"][1]["reason"] == "CLAUDE.md"


# -- pruning, depth and caps ------------------------------------------------

def test_dependency_trees_are_pruned(tmp_path):
    _git(tmp_path / "app" / "node_modules" / "left-pad")
    (tmp_path / "app" / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / "app" / "build" / "out").mkdir(parents=True)

    result = lite.discover_candidates(str(tmp_path))

    assert result["candidates"] == []
    # only the root and ``app`` were opened; node_modules/.venv/build were not
    assert result["scanned"] == 2


def test_depth_counts_levels_below_the_root(tmp_path):
    _git(tmp_path / "shallow")
    _git(tmp_path / "a" / "b" / "deep")

    shallow_only = lite.discover_candidates(str(tmp_path), depth=2)
    both = lite.discover_candidates(str(tmp_path), depth=4)

    assert _paths(shallow_only) == ["shallow"]
    assert _paths(both) == ["shallow", "deep"]
    assert lite.discover_candidates(str(tmp_path), depth=0)["candidates"] == []


def test_an_already_registered_project_is_flagged_not_dropped(tmp_path):
    alpha = _git(tmp_path / "alpha")
    _git(tmp_path / "beta")

    result = lite.discover_candidates(str(tmp_path), registered=[str(alpha)])

    assert _paths(result) == ["alpha", "beta"]
    assert [c["already_registered"] for c in result["candidates"]] == [True, False]


def test_the_visit_cap_stops_the_walk_and_says_so(tmp_path):
    for index in range(6):
        (tmp_path / f"dir{index}").mkdir()

    result = lite.discover_candidates(str(tmp_path), max_visited=3)

    assert result["scanned"] == 3
    assert result["truncated"] is True


def test_the_candidate_cap_reports_the_overflow(tmp_path):
    for name in ("alpha", "beta", "gamma"):
        _git(tmp_path / name)

    result = lite.discover_candidates(str(tmp_path), max_candidates=2)

    assert _paths(result) == ["alpha", "beta"]
    assert result["truncated"] is True


# -- invariants -------------------------------------------------------------

def test_discovery_never_touches_the_tree(tmp_path):
    repo = _git(tmp_path / "alpha")
    (tmp_path / "chat" / ".claude").mkdir(parents=True)
    (repo / "new.txt").write_bytes(b"later\n")
    before = _snapshot(tmp_path)

    lite.discover_candidates(str(tmp_path))

    assert _snapshot(tmp_path) == before


def test_symlinks_are_not_followed(tmp_path):
    """A link must not pull a repository in from outside the scanned tree."""
    target = tmp_path / "target"
    _git(target / "repo")
    tree = tmp_path / "tree"
    tree.mkdir()
    try:
        (tree / "shortcut").symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not permitted here")

    result = lite.discover_candidates(str(tree))

    assert result["candidates"] == []
    assert result["scanned"] == 1


def test_a_directory_reachable_twice_is_proposed_once(tmp_path):
    """A junction (or a second spelling of a path) must not duplicate a project."""
    real = tmp_path / "real"
    (real / ".claude").mkdir(parents=True)
    if not _link(real, tmp_path / "link"):
        pytest.skip("directory links are not permitted here")

    result = lite.discover_candidates(str(tmp_path))

    assert len(result["candidates"]) == 1, result["candidates"]
    candidate = result["candidates"][0]
    assert candidate["path"] == os.path.realpath(str(real))
    assert candidate["name"] == "real"


def test_a_cyclic_tree_does_not_re_walk_itself(tmp_path):
    """A link back to an ancestor must not burn the walk's whole budget."""
    (tmp_path / "plain").mkdir()
    (tmp_path / "loop").mkdir()
    if not _link(tmp_path, tmp_path / "loop" / "back"):
        pytest.skip("directory links are not permitted here")

    result = lite.discover_candidates(str(tmp_path), depth=10)

    assert result["truncated"] is False, result
    assert result["scanned"] == 3, result  # root, loop, plain — "back" is the root again


def test_an_invalid_root_reports_the_same_errors_as_registration(tmp_path):
    missing = lite.discover_candidates(str(tmp_path / "nope"))
    relative = lite.discover_candidates("relative/path")

    assert missing["errors"] == ["not_found"]
    assert relative["errors"] == ["path must be absolute"]
    assert missing["candidates"] == [] and relative["candidates"] == []
    assert missing["scanned"] == 0


def test_windows_paths_with_quotes_and_spaces_still_resolve(tmp_path):
    repo = _git(tmp_path / "spaced repo")

    result = lite.discover_candidates(f'  "{repo}"  ')

    assert result["errors"] == []
    assert result["candidates"][0]["path"] == os.path.realpath(str(repo))


# -- CLI --------------------------------------------------------------------

def test_parse_args_takes_roots_and_a_depth():
    args = lite.parse_args(["--discover", r"D:\one", "--discover", r"D:\two",
                            "--depth", "2"])

    assert args.discover == [r"D:\one", r"D:\two"]
    assert args.depth == 2
    assert lite.parse_args([]).discover == []
    assert lite.parse_args([]).depth is None  # the built-in depth applies


def test_startup_discovery_registers_everything_under_each_root(tmp_path):
    _git(tmp_path / "alpha")
    _git(tmp_path / "nested" / "beta")
    state = lite.State(tmp_path / "data.json")

    summary = lite.register_discovered(state, [str(tmp_path)])

    assert summary["roots"] == 1
    assert summary["added"] == 2
    assert summary["skipped"] == 0 and summary["failed"] == 0
    assert {p["name"] for p in state.projects} == {"alpha", "beta"}

    again = lite.register_discovered(state, [str(tmp_path)])

    assert again["added"] == 0 and again["skipped"] == 2
    assert len(state.projects) == 2  # never registered twice


def test_startup_discovery_survives_a_root_that_cannot_be_scanned(tmp_path, capsys):
    state = lite.State(tmp_path / "data.json")

    summary = lite.register_discovered(state, [str(tmp_path / "nope"), str(tmp_path)])

    assert summary["roots"] == 1
    assert "cannot scan" in capsys.readouterr().err


def test_startup_discovery_honours_the_depth_flag(tmp_path):
    _git(tmp_path / "a" / "b" / "deep")
    state = lite.State(tmp_path / "data.json")

    summary = lite.register_discovered(state, [str(tmp_path)], depth=1)

    assert summary["added"] == 0
    assert state.projects == []


def test_main_discovers_before_it_serves(tmp_path, monkeypatch):
    """``--discover`` must finish registering before the monitors start."""
    _git(tmp_path / "alpha")
    seen: dict = {}

    monkeypatch.setattr(lite.LiteApp, "start_monitors",
                        lambda self: seen.setdefault("monitors", True))
    monkeypatch.setattr(
        lite.LiteApp, "serve",
        lambda self, port, open_browser, verbose=False: seen.setdefault(
            "served", [p["name"] for p in self.state.projects]),
    )

    code = lite.main(["--discover", str(tmp_path), "--no-browser",
                      "--data", str(tmp_path / "data.json")])

    assert code == 0
    assert seen.get("monitors") is True
    assert seen["served"] == ["alpha"]


def test_the_startup_summary_survives_a_redirected_stdout(tmp_path):
    """``--discover > log`` must show what happened while the server is running."""
    _git(tmp_path / "alpha")
    log = tmp_path / "startup.log"

    with log.open("w", encoding="utf-8") as handle:
        proc = subprocess.Popen(
            [sys.executable, str(LITE_PATH), "--discover", str(tmp_path),
             "--no-browser", "--port", "8799", "--data", str(tmp_path / "data.json")],
            stdout=handle, stderr=subprocess.STDOUT,
        )
        try:
            seen = ""
            deadline = time.time() + 15
            while time.time() < deadline and "registered" not in seen:
                time.sleep(0.5)
                try:
                    seen = log.read_text(encoding="utf-8")
                except OSError:
                    pass
            assert "registered" in seen, seen
        finally:
            proc.kill()
            proc.wait(timeout=10)
