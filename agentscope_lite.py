#!/usr/bin/env python3
"""AgentScope Lite — a one-file local monitor for AI coding agents.

Everything (backend, monitoring engine and frontend) lives in this single file
and uses only the Python standard library: no pip install, no database setup,
no build step, no second process to manage.

    python agentscope_lite.py                              # start + open browser
    python agentscope_lite.py --project D:\\code\\my-repo    # pre-register a project
    python agentscope_lite.py --no-browser --port 8790     # headless

What it does
------------
* registers project directories, validating them read-only (absolute, existing,
  no traversal, must be a directory),
* watches them for file changes by polling — it never writes, deletes or
  mutates a single byte inside a monitored project,
* polls git with ``GIT_OPTIONAL_LOCKS=0`` (so even ``git status`` cannot rewrite
  ``.git/index``) for branch, status counts and new commits,
* discovers running agent processes (opencode, claude, codex, aider, ...) with
  CPU/memory and a redacted command line,
* serves a single-page dashboard on 127.0.0.1 protected by a session token.

Data (projects + capped event history) lives in ``~/.agentscope_lite.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_NAME = "AgentScope Lite"
VERSION = "0.1.0"
DATA_FILE = Path.home() / ".agentscope_lite.json"

FILE_INTERVAL_S = 2.0
GIT_INTERVAL_S = 5.0
PROC_INTERVAL_S = 3.0

MAX_EVENTS = 500          # file changes kept
MAX_COMMITS = 300         # commits kept
MAX_EVENTS_PER_SCAN = 200  # events emitted by a single project scan
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_BODY_BYTES = 64 * 1024

#: Directory subtrees that are never worth reporting (and would be pure noise).
SKIP_DIRS = {"node_modules", ".venv", "venv", "__pycache__", "dist", "build",
             "target", ".next", ".git", ".hg", ".idea", ".vscode"}
SKIP_SUFFIXES = (".lock", ".pem", ".key", ".p12", ".pfx")
SKIP_PREFIXES = (".env", "id_rsa", "id_ed25519", "credentials", "auth.json")

# -- project discovery ------------------------------------------------------
#: Subtrees the discovery walk never descends into (dependencies, build output,
#: VCS internals, recycle bins): listing them would be pure noise.
DISCOVER_PRUNE_DIRS = {
    "node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build",
    "target", ".next", ".git", ".hg", ".svn", "vendor", "site-packages",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".cache",
    ".idea", ".vscode", "$RECYCLE.BIN", "System Volume Information",
}
#: Agent-owned markers: a directory holding one of these is a project even
#: when it is not a git repository.
DISCOVER_MARKER_DIRS = {".aider", ".claude", ".codex", ".cursor", ".gemini", ".opencode"}
DISCOVER_MARKER_FILES = {
    "AGENTS.md", "CLAUDE.md", "GEMINI.md", "opencode.json",
    ".aider.conf.yml", ".cursorrules",
}
DISCOVER_DEPTH = 4          # levels below the root that are scanned
DISCOVER_MAX_VISITED = 5000  # directories opened by a single walk
DISCOVER_MAX_CANDIDATES = 100  # proposals returned by a single walk

DEFAULT_MATCHERS = ["opencode", "claude", "codex", "aider", "gemini"]

#: Flags whose value is always a secret, whatever it looks like.
SENSITIVE_FLAGS = (
    "-p", "--p", "--pass", "--password", "--passwd", "--token", "--key",
    "--secret", "--api-key", "--apikey", "--auth", "--authorization",
    "--credential", "--credentials", "--private-key", "--access-key",
    "--session-key", "--client-secret",
)
OPAQUE_MIN_LENGTH = 20

GIT = shutil.which("git")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def project_id_for(path: str) -> str:
    return hashlib.sha1(path.encode("utf-8")).hexdigest()[:12]


def redact_cmdline(text: str) -> str:
    """Mask secret-looking values in a command line before storing/showing it."""
    if not text:
        return ""
    out = text
    for flag in SENSITIVE_FLAGS:
        # --token value  /  --token=value
        out = re.sub(
            rf"(?i)(?<![\w-])({re.escape(flag)})=(\"[^\"]*\"|\S+)",
            r"\1=***",
            out,
        )
        out = re.sub(
            rf"(?i)(?<![\w-])({re.escape(flag)})(\s+)(\S+)",
            r"\1\2***",
            out,
        )
    # standalone opaque strings (20+ chars, no path/URL structure)
    out = re.sub(
        rf"(?<![\w/\\])([A-Za-z0-9_\-]{{{OPAQUE_MIN_LENGTH},}})(?![\w/\\])",
        "***",
        out,
    )
    return out


def redact_email(value: str) -> str:
    value = (value or "").strip()
    if "@" not in value:
        return (value[:1] + "***") if value else "***"
    local, _, domain = value.rpartition("@")
    if not local:
        return "***@" + domain
    return local[0] + "***@" + domain


def _cmd_tokens(cmdline: str) -> list[str]:
    """Tokens of a command line, honouring quotes, minus flags and assignments."""
    tokens: list[str] = []
    for match in re.finditer(r'"([^"]*)"|\'([^\']*)\'|(\S+)', cmdline or ""):
        token = next((g for g in match.groups() if g is not None), "")
        if not token or token.startswith("-") or "=" in token:
            continue  # --flag, --flag=value, k=v: somebody else's business
        tokens.append(token)
    return tokens


def matches_agent(matchers: list[str], name: str, cmdline: str) -> bool:
    """True when a matcher appears in the executable or a positional argument.

    Deliberately *not* the whole command line: an unrelated process whose
    ``--annotation=...gemini...`` value happens to contain a matcher must not
    land on the dashboard.
    """
    haystack = " ".join([name or "", *_cmd_tokens(cmdline)])
    return any(re.search(re.escape(m), haystack, re.I) for m in matchers if m)


# --------------------------------------------------------------------------
# path validation (read-only, mirrors the full app's services/paths.py)
# --------------------------------------------------------------------------

def validate_project_path(raw: str) -> dict:
    """Return ``{"path", "errors"}`` for a candidate project directory."""
    errors: list[str] = []
    # trim whitespace and the quotes people paste along with a path
    text = (raw or "").strip().strip("\"'`").strip()
    if not text:
        return {"path": "", "errors": ["required"]}

    # percent-encoded traversal is decoded once, exactly like a URL path would be
    decoded = urllib.parse.unquote(text)
    for candidate in (decoded, text):
        if ".." in Path(candidate).parts:
            errors.append("traversal")
            break

    expanded = Path(decoded).expanduser()
    if not expanded.is_absolute():
        errors.append("path must be absolute")
        return {"path": decoded, "errors": errors}

    resolved = Path(os.path.realpath(expanded))
    if not resolved.exists():
        errors.append("not_found")
    elif not resolved.is_dir():
        errors.append("not_a_directory")
    else:
        try:
            next(resolved.iterdir(), None)
        except OSError:
            errors.append("permission_denied")
        if not errors and not os.access(resolved, os.R_OK):
            errors.append("permission_denied")
    return {"path": str(resolved), "errors": errors}


def git_inspect(root: Path) -> dict:
    """Read-only git facts: is it a repo, branch, status counts."""
    info = {"is_git": False, "git_available": bool(GIT), "branch": None,
            "status": {"staged": 0, "modified": 0, "untracked": 0, "clean": True}}
    if not GIT:
        return info
    try:
        inside = _git(root, "rev-parse", "--is-inside-work-tree")
    except (OSError, subprocess.SubprocessError):
        return info
    if inside.returncode != 0 or inside.stdout.strip() != b"true":
        return info
    info["is_git"] = True

    head = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    if head.returncode == 0:
        info["branch"] = head.stdout.decode("utf-8", "replace").strip()
    else:
        # unborn branch (repo with no commits yet)
        info["branch"] = None

    status = _git(root, "status", "--porcelain")
    if status.returncode == 0:
        staged = modified = untracked = 0
        for line in status.stdout.decode("utf-8", "replace").splitlines():
            if line.startswith("??"):
                untracked += 1
            else:
                x = line[:1]
                y = line[1:2] if len(line) > 1 else " "
                if x not in (" ", "?", "!"):
                    staged += 1
                if y not in (" ", "?", "!"):
                    modified += 1
        info["status"] = {
            "staged": staged, "modified": modified, "untracked": untracked,
            "clean": not (staged or modified or untracked),
        }
    return info


def git_log(root: Path, limit: int = 20) -> list[dict]:
    """Newest-first commit list; author e-mail always redacted."""
    if not GIT:
        return []
    fmt = "%H%x1f%h%x1f%an%x1f%ae%x1f%aI%x1f%s%x1e"
    try:
        cp = _git(root, "log", f"-n{limit}", f"--pretty=format:{fmt}")
    except (OSError, subprocess.SubprocessError):
        return []
    if cp.returncode != 0:
        return []  # empty repo, shallow/unknown history, not a repo: all fine
    out = []
    for chunk in cp.stdout.decode("utf-8", "replace").split("\x1e"):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        parts = chunk.split("\x1f")
        if len(parts) != 6:
            continue
        sha, short, author, email, when, subject = parts
        out.append({
            "sha": sha, "short": short, "subject": subject, "author": author,
            "email_redacted": redact_email(email), "at": when,
        })
    return out


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0",
                "GIT_PAGER": "cat", "LC_ALL": "C"})
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, env=env, timeout=15,
    )


# --------------------------------------------------------------------------
# project discovery (read-only walk over a root directory)
# --------------------------------------------------------------------------

def _is_repo_root(directory: Path) -> bool:
    """True only for the *top level* of a git work tree.

    The ``.git`` pre-filter keeps ordinary folders from spawning a process at
    all; the toplevel comparison stops a subfolder of a repository from being
    reported as a project of its own.
    """
    if not GIT:
        return False
    try:
        if not (directory / ".git").exists():
            return False
        probe = _git(directory, "rev-parse", "--show-toplevel")
    except (OSError, subprocess.SubprocessError):
        return False
    if probe.returncode != 0:
        return False
    top = probe.stdout.decode("utf-8", "replace").strip()
    if not top:
        return False
    return os.path.realpath(top) == os.path.realpath(str(directory))


def _candidate_reason(directory: Path) -> str | None:
    """Why ``directory`` is an agent project, or ``None`` when it is not."""
    if _is_repo_root(directory):
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
    root: str,
    *,
    depth: int = DISCOVER_DEPTH,
    registered: list[str] | None = None,
    max_visited: int = DISCOVER_MAX_VISITED,
    max_candidates: int = DISCOVER_MAX_CANDIDATES,
) -> dict:
    """Scan ``root`` read-only and propose the agent projects underneath it.

    A directory qualifies when it is the root of a git repository or when it
    holds an agent marker; once something qualifies the walk stops there,
    because everything inside belongs to that project. Dependency and build
    subtrees are pruned, symlinks are not followed, and nothing on disk is
    written, touched or even opened for writing.

    Returns ``{"root", "errors", "depth", "scanned", "truncated",
    "candidates": [{"path", "name", "reason", "already_registered"}]}``.
    """
    validation = validate_project_path(root)
    out = {
        "root": validation["path"],
        "errors": list(validation["errors"]),
        "depth": max(0, int(depth)),
        "scanned": 0,
        "truncated": False,
        "candidates": [],
    }
    if out["errors"]:
        return out

    known = {os.path.realpath(str(path)) for path in (registered or [])}
    queue: list[tuple[Path, int]] = [(Path(out["root"]), 0)]
    scanned = 0

    while queue:
        if scanned >= max_visited:
            out["truncated"] = True
            break
        directory, level = queue.pop(0)  # breadth first: shallow projects first
        scanned += 1

        reason = _candidate_reason(directory)
        if reason:
            if len(out["candidates"]) >= max_candidates:
                out["truncated"] = True
                break
            path = os.path.realpath(str(directory))
            out["candidates"].append({
                "path": path,
                "name": directory.name or path,
                "reason": reason,
                "already_registered": path in known,
            })
            continue  # do not descend: the contents belong to this project

        if level >= out["depth"]:
            continue
        try:
            children = sorted(directory.iterdir(), key=lambda p: p.name.lower())
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

    out["scanned"] = scanned
    return out


# --------------------------------------------------------------------------
# process discovery (pure stdlib: PowerShell on Windows, ps elsewhere)
# --------------------------------------------------------------------------

_PS_TEMPLATE = r"""
$ErrorActionPreference='SilentlyContinue'
try { [Console]::OutputEncoding=[System.Text.Encoding]::UTF8 } catch {}
$pat = '__PAT__'
$by = @{}
Get-Process | ForEach-Object {
  try { $by[[int]$_.Id] = @{ cpu = $_.CPU; ws = $_.WS; st = $_.StartTime } } catch {}
}
$acc = New-Object System.Collections.ArrayList
foreach ($p in (Get-CimInstance Win32_Process)) {
  $nm = "$($p.Name)"
  $cl = "$($p.CommandLine)"
  if ("$nm $cl" -notmatch $pat) { continue }
  $m = $by[[int]$p.ProcessId]
  $cpu = $null; $ws = $null; $st = ''
  if ($null -ne $m) {
    if ($null -ne $m.cpu) { try { $cpu = [double]$m.cpu } catch {} }
    if ($null -ne $m.ws)  { try { $ws  = [int64]$m.ws  } catch {} }
    if ($null -ne $m.st)  { try { $st  = $m.st.ToString('o') } catch {} }
  }
  [void]$acc.Add([pscustomobject]@{
    pid  = [int]$p.ProcessId
    ppid = [int]$p.ParentProcessId
    name = $nm
    cpu  = $cpu
    ws   = $ws
    st   = $st
    cmd  = $cl
  })
}
ConvertTo-Json -InputObject @($acc.ToArray()) -Compress -Depth 4
"""


def _matcher_pattern(matchers: list[str]) -> str:
    safe = [re.sub(r"[^A-Za-z0-9_.\-]", "", m) for m in matchers]
    safe = [m for m in safe if m]
    return "|".join(safe) if safe else "opencode"


def _list_processes_windows(matchers: list[str]) -> list[dict]:
    script = _PS_TEMPLATE.replace("__PAT__", _matcher_pattern(matchers))
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        return _list_processes_tasklist(matchers)
    try:
        cp = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", script],
            capture_output=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return _list_processes_tasklist(matchers)
    raw = cp.stdout
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("mbcs", "replace") if os.name == "nt" else raw.decode("utf-8", "replace")
    text = text.strip()
    if not text:
        return _list_processes_tasklist(matchers)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return _list_processes_tasklist(matchers)
    if isinstance(data, dict):
        data = [data]
    return [
        {
            "pid": int(row.get("pid") or 0),
            "ppid": int(row.get("ppid") or 0),
            "name": str(row.get("name") or ""),
            "cpu_seconds": row.get("cpu"),
            "rss_bytes": row.get("ws"),
            "started_at": row.get("st") or "",
            "cmdline": str(row.get("cmd") or ""),
        }
        for row in data
        if isinstance(row, dict) and row.get("pid")
    ]


def _list_processes_tasklist(matchers: list[str]) -> list[dict]:
    """Last-resort Windows fallback: names and PIDs only."""
    exe = shutil.which("tasklist")
    if not exe:
        return []
    try:
        cp = subprocess.run([exe, "/FO", "CSV", "/NH"], capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    pattern = re.compile(_matcher_pattern(matchers), re.I)
    for line in cp.stdout.decode("utf-8", "replace").splitlines():
        parts = [p.strip() for p in line.split('","')]
        if len(parts) < 2:
            continue
        name = parts[0].strip('"')
        if not pattern.search(name):
            continue
        try:
            pid = int(parts[1].strip('"'))
        except ValueError:
            continue
        rows.append({"pid": pid, "ppid": 0, "name": name, "cpu_seconds": None,
                     "rss_bytes": None, "started_at": "", "cmdline": ""})
    return rows


def _list_processes_posix(matchers: list[str]) -> list[dict]:
    pattern = re.compile(_matcher_pattern(matchers), re.I)
    rows = []
    for cmd in (["ps", "-eo", "pid=,ppid=,pcpu=,rss=,etimes=,comm=,args="],
                ["ps", "-axo", "pid=,ppid=,pcpu=,rss=,comm=,args="]):
        try:
            cp = subprocess.run(cmd, capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return rows
        if cp.returncode != 0:
            continue
        lines = cp.stdout.decode("utf-8", "replace").splitlines()
        seven = "etimes" in cmd[2]
        for line in lines:
            parts = line.strip().split(None, 6)
            if len(parts) < 6:
                continue
            pid, ppid, pcpu, rss = parts[0], parts[1], parts[2], parts[3]
            rest = parts[4:]
            name = rest[1] if seven else rest[0]
            cmdline = rest[2] if seven else rest[1]
            if not pattern.search(f"{name} {cmdline}"):
                continue
            try:
                cpu = float(pcpu)
            except ValueError:
                cpu = None
            try:
                rss_kb = int(rss)
            except ValueError:
                rss_kb = None
            try:
                elapsed = int(rest[0]) if seven else None
            except ValueError:
                elapsed = None
            started = ""
            if elapsed:
                started = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - elapsed)
                )
            rows.append({
                "pid": int(pid), "ppid": int(ppid), "name": name,
                "cpu_seconds": None, "cpu_pct": cpu,
                "rss_bytes": None if rss_kb is None else rss_kb * 1024,
                "started_at": started, "cmdline": cmdline,
            })
        break  # first command that worked wins
    return rows


def list_processes(matchers: list[str]) -> list[dict]:
    if os.name == "nt":
        rows = _list_processes_windows(matchers)
    else:
        rows = _list_processes_posix(matchers)
    own_pid = os.getpid()
    out = []
    for row in rows:
        if row.get("pid") == own_pid:
            continue  # never report the monitor itself
        if not matches_agent(matchers, row.get("name", ""), row.get("cmdline", "")):
            continue  # broad fetch, precise filter (flag values must not match)
        row["cmdline"] = redact_cmdline(row.get("cmdline", ""))
        out.append(row)
    return out


# --------------------------------------------------------------------------
# application state
# --------------------------------------------------------------------------

class State:
    """Thread-safe application state, persisted to one JSON file."""

    def __init__(self, data_file: Path) -> None:
        self.data_file = data_file
        self.lock = threading.RLock()
        self.started_at = time.time()
        self.projects: list[dict] = []
        self.file_changes: list[dict] = []
        self.commits: list[dict] = []
        self.processes: list[dict] = []
        self.stats = {"scans": 0, "file_events": 0, "commits_seen": 0,
                      "process_polls": 0, "last_file_scan": None,
                      "last_git_poll": None, "last_process_poll": None}
        self.settings = {"matchers": list(DEFAULT_MATCHERS)}
        self._dirty = False
        #: set by LiteApp.start_monitors(); None until the engine runs
        self.scanner: FileScanner | None = None
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        try:
            raw = json.loads(self.data_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        self.projects = [
            p for p in raw.get("projects", []) if isinstance(p, dict) and p.get("path")
        ]
        self.file_changes = list(raw.get("file_changes", []))[:MAX_EVENTS]
        self.commits = list(raw.get("commits", []))[:MAX_COMMITS]
        counters = raw.get("counters") or {}
        for key in ("file_events", "commits_seen"):
            if isinstance(counters.get(key), int):
                self.stats[key] = counters[key]
        settings = raw.get("settings") or {}
        if isinstance(settings.get("matchers"), list) and settings["matchers"]:
            self.settings["matchers"] = [str(m) for m in settings["matchers"]]

    def save(self) -> None:
        with self.lock:
            payload = {
                "version": VERSION,
                "updated_at": now_iso(),
                "settings": self.settings,
                "counters": {"file_events": self.stats["file_events"],
                             "commits_seen": self.stats["commits_seen"]},
                "projects": self.projects,
                "file_changes": self.file_changes[:200],
                "commits": self.commits[:200],
            }
            self._dirty = False
        try:
            self.data_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.data_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.data_file)
        except OSError as exc:  # never crash the monitor over persistence
            print(f"[lite] could not save {self.data_file}: {exc}", file=sys.stderr)

    # -- projects ----------------------------------------------------------
    def add_project(self, path: str) -> dict:
        """Validate and register a project directory. Read-only."""
        result = validate_project_path(path)
        if result["errors"]:
            return {"ok": False, "errors": result["errors"]}

        resolved = result["path"]
        pid = project_id_for(resolved)
        with self.lock:
            for existing in self.projects:
                if existing["id"] == pid or existing["path"] == resolved:
                    return {"ok": False, "errors": ["already_registered"]}

        git = git_inspect(Path(resolved))
        project = {
            "id": pid,
            "name": Path(resolved).name or resolved,
            "path": resolved,
            "is_git": git["is_git"],
            "branch": git["branch"],
            "status": git["status"],
            "added_at": now_iso(),
            "change_count": 0,
            "commit_count": 0,
        }
        history = git_log(Path(resolved)) if git["is_git"] else []
        with self.lock:
            self.projects.append(project)
            # import the recent history so the dashboard is useful immediately,
            # newest first, and attribute every row to this project
            known = {c["sha"] for c in self.commits}
            imported = [
                dict(commit, project_id=pid, project_name=project["name"])
                for commit in history
                if commit["sha"] not in known
            ]
            self.commits = imported + self.commits
            self.commits = self.commits[:MAX_COMMITS]
            project["commit_count"] = len(history)
            self._dirty = True
        self.save()
        if self.scanner is not None:
            self.scanner.take_baseline(pid, resolved)
        return {"ok": True, "project": project}

    def remove_project(self, pid: str) -> bool:
        with self.lock:
            before = len(self.projects)
            self.projects = [p for p in self.projects if p["id"] != pid]
            removed = len(self.projects) != before
            if removed:
                self.file_changes = [c for c in self.file_changes if c.get("project_id") != pid]
                self.commits = [c for c in self.commits if c.get("project_id") != pid]
                self._dirty = True
        if self.scanner is not None:
            self.scanner.forget(pid)
        if removed:
            self.save()
        return removed

    def note_change(self, project: dict, events: list[dict]) -> None:
        with self.lock:
            project["change_count"] = project.get("change_count", 0) + len(events)
            self.file_changes = events + self.file_changes
            self.file_changes = self.file_changes[:MAX_EVENTS]
            self.stats["file_events"] += len(events)
            self._dirty = True

    def note_commits(self, project: dict, commits: list[dict]) -> None:
        with self.lock:
            known = {c["sha"] for c in self.commits}
            # build fresh dicts: rebinding the loop variable would leave the
            # list holding the original, unattributed commit
            fresh = [
                dict(commit, project_id=project["id"], project_name=project["name"])
                for commit in commits
                if commit["sha"] not in known
            ]
            self.commits = fresh + self.commits
            self.commits = self.commits[:MAX_COMMITS]
            if fresh:
                project["commit_count"] = project.get("commit_count", 0) + len(fresh)
                self.stats["commits_seen"] += len(fresh)
                self._dirty = True

    def update_git(self, project: dict, git: dict) -> None:
        with self.lock:
            project["is_git"] = git["is_git"]
            project["branch"] = git["branch"]
            project["status"] = git["status"]

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "app": {
                    "name": APP_NAME, "version": VERSION,
                    "started_at": self.started_at,
                    "uptime_s": round(time.time() - self.started_at, 1),
                    "read_only": True,
                },
                "health": {
                    "status": "ok",
                    "projects": len(self.projects),
                    "monitors": {"file": True, "git": bool(GIT), "process": True},
                },
                "projects": [dict(p) for p in self.projects],
                "file_changes": [dict(c) for c in self.file_changes],
                "commits": [dict(c) for c in self.commits],
                "processes": [dict(p) for p in self.processes],
                "stats": dict(self.stats),
                "settings": dict(self.settings),
            }


# --------------------------------------------------------------------------
# monitors (polling threads — read-only against monitored projects)
# --------------------------------------------------------------------------

class FileScanner(threading.Thread):
    """Polls every registered project and records created/modified/deleted."""

    def __init__(self, state: State) -> None:
        super().__init__(name="lite-file", daemon=True)
        self.state = state
        self.stop_event = threading.Event()
        self._baselines: dict[str, dict[str, tuple[int, int]]] = {}
        self._baseline_lock = threading.Lock()

    def take_baseline(self, pid: str, path: str) -> None:
        """Record the current contents without reporting them as changes."""
        self._scan_one(pid, path)

    def forget(self, pid: str) -> None:
        with self._baseline_lock:
            self._baselines.pop(pid, None)

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        last_save = time.time()
        while not self.stop_event.wait(FILE_INTERVAL_S):
            for project in list(self.state.projects):
                if self.stop_event.is_set():
                    return
                try:
                    self._scan_one(project["id"], project["path"])
                except Exception as exc:  # noqa: BLE001 — a broken project must not kill the loop
                    print(f"[lite] file scan failed for {project['path']}: {exc}", file=sys.stderr)
            with self.state.lock:
                self.state.stats["scans"] += 1
                self.state.stats["last_file_scan"] = now_iso()
                dirty = self.state._dirty
            if dirty and time.time() - last_save > 60:
                self.state.save()
                last_save = time.time()

    # -- internals ---------------------------------------------------------
    def _scan_one(self, pid: str, path: str) -> None:
        root = Path(path)
        current: dict[str, tuple[int, int]] = {}
        for rel, full in _walk_project(root):
            try:
                st = os.stat(full)
            except OSError:
                continue
            current[rel] = (st.st_mtime_ns, st.st_size)

        with self._baseline_lock:
            previous = self._baselines.get(pid)
            self._baselines[pid] = current
        if previous is None:
            return  # first sight of this project: baseline, never a flood of events

        project = next((p for p in self.state.projects if p["id"] == pid), None)
        if project is None:
            return

        events: list[dict] = []
        for rel, cur in current.items():
            prev = previous.get(rel)
            if prev is None:
                action = "created"
            elif prev != cur:
                action = "modified"
            else:
                continue
            if cur[1] > MAX_FILE_BYTES:
                continue  # oversized files are never reported
            events.append({"ts": now_iso(), "project_id": pid,
                           "project_name": project["name"], "path": rel,
                           "action": action, "size": cur[1]})
        for rel, prev in previous.items():
            if rel in current:
                continue
            events.append({"ts": now_iso(), "project_id": pid,
                           "project_name": project["name"], "path": rel,
                           "action": "deleted", "size": prev[1]})

        if len(events) > MAX_EVENTS_PER_SCAN:
            events = events[:MAX_EVENTS_PER_SCAN]
        if events:
            self.state.note_change(project, events)


def _walk_project(root: Path):
    """Yield ``(relative_path, absolute_path)`` inside ``root``, honouring excludes.

    Symlinked/junctioned directories that resolve outside the project are
    pruned, so a link can never make the scanner observe files elsewhere.
    """
    if not root.is_dir():
        return
    real_root = os.path.realpath(root)
    prefix = real_root if real_root.endswith(os.sep) else real_root + os.sep
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        kept = []
        for name in dirnames:
            if name in SKIP_DIRS:
                continue
            full = os.path.join(dirpath, name)
            if not os.path.realpath(full).startswith(prefix):
                continue  # escapes the project (symlink/junction): drop it
            kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            if name.endswith(SKIP_SUFFIXES) or name.startswith(SKIP_PREFIXES):
                continue
            full = os.path.join(dirpath, name)
            if not os.path.realpath(full).startswith(prefix):
                continue
            rel = os.path.relpath(full, real_root).replace(os.sep, "/")
            yield rel, full


class GitPoller(threading.Thread):
    """Read-only git polling: branch, status counts, new commits."""

    def __init__(self, state: State) -> None:
        super().__init__(name="lite-git", daemon=True)
        self.state = state
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        while not self.stop_event.wait(GIT_INTERVAL_S):
            for project in list(self.state.projects):
                if self.stop_event.is_set():
                    return
                if not project.get("is_git"):
                    continue
                root = Path(project["path"])
                try:
                    self.state.update_git(project, git_inspect(root))
                    commits = git_log(root, limit=15)
                except Exception as exc:  # noqa: BLE001
                    print(f"[lite] git poll failed for {root}: {exc}", file=sys.stderr)
                    continue
                if commits:
                    self.state.note_commits(project, commits)
            with self.state.lock:
                self.state.stats["last_git_poll"] = now_iso()


class ProcessPoller(threading.Thread):
    """Discovers agent processes and derives CPU% from consecutive samples."""

    def __init__(self, state: State) -> None:
        super().__init__(name="lite-proc", daemon=True)
        self.state = state
        self.stop_event = threading.Event()
        self._prev_cpu: dict[int, tuple[float, float]] = {}

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        while not self.stop_event.wait(PROC_INTERVAL_S):
            matchers = list(self.state.settings.get("matchers") or DEFAULT_MATCHERS)
            try:
                rows = list_processes(matchers)
            except Exception as exc:  # noqa: BLE001
                print(f"[lite] process poll failed: {exc}", file=sys.stderr)
                rows = []
            now = time.time()
            out = []
            seen = set()
            for row in rows:
                pid = row["pid"]
                seen.add(pid)
                cpu_pct = row.get("cpu_pct")
                seconds = row.get("cpu_seconds")
                if seconds is not None:
                    previous = self._prev_cpu.get(pid)
                    if previous is not None and now > previous[1]:
                        delta = float(seconds) - previous[0]
                        cpu_pct = max(0.0, delta / (now - previous[1]) * 100.0)
                    self._prev_cpu[pid] = (float(seconds), now)
                out.append({
                    "pid": pid, "ppid": row.get("ppid", 0), "name": row.get("name", ""),
                    "cpu_pct": round(cpu_pct, 1) if cpu_pct is not None else None,
                    "rss_bytes": row.get("rss_bytes"),
                    "started_at": row.get("started_at", ""),
                    "cmdline": row.get("cmdline", ""),
                })
            for pid in list(self._prev_cpu):
                if pid not in seen:
                    del self._prev_cpu[pid]
            with self.state.lock:
                self.state.processes = out
                self.state.stats["process_polls"] += 1
                self.state.stats["last_process_poll"] = now_iso()


# --------------------------------------------------------------------------
# HTTP API + embedded dashboard
# --------------------------------------------------------------------------

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AgentScope Lite</title>
<style>
  :root{
    --bg:#17181C; --surface:#1F2126; --surface2:#24262C; --border:#2E3138;
    --text:#E7E8EC; --muted:#9A9EA8; --accent:#E8A33D; --green:#4CAF7D;
    --red:#E0655B; --blue:#6C9BE0;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font:14px/1.5 Inter,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
  code,.mono{font-family:"Cascadia Code",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
  header{display:flex;align-items:center;gap:16px;flex-wrap:wrap;
         padding:14px 22px;background:var(--surface);border-bottom:1px solid var(--border);
         position:sticky;top:0;z-index:5}
  .brand{font-size:17px;font-weight:650;letter-spacing:.2px}
  .brand span{color:var(--accent)}
  .pill{font-size:12px;padding:3px 9px;border-radius:999px;border:1px solid var(--border);
        color:var(--muted);background:var(--surface2)}
  .pill.ok{color:#9BE3C2;border-color:#2F5B47;background:#1B2A24}
  .pill.warn{color:#F3C88B;border-color:#5B4A2F;background:#2A2419}
  .spacer{flex:1}
  main{padding:20px 22px 40px;display:grid;gap:18px;
       grid-template-columns:repeat(auto-fit,minmax(430px,1fr))}
  .card{background:var(--surface);border:1px solid var(--border);border-radius:10px;
        display:flex;flex-direction:column;min-width:0}
  .card h2{margin:0;padding:13px 16px;font-size:13px;letter-spacing:.8px;
           text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--border);
           display:flex;align-items:center;gap:8px}
  .card h2 .count{color:var(--text);background:var(--surface2);border:1px solid var(--border);
                  border-radius:999px;padding:0 8px;font-size:11px}
  .card .body{padding:14px 16px}
  form{display:flex;gap:8px;flex-wrap:wrap}
  input[type=text]{flex:1;min-width:220px;background:var(--bg);color:var(--text);
       border:1px solid var(--border);border-radius:7px;padding:8px 10px;font-size:13px}
  input[type=text]:focus{outline:none;border-color:var(--accent)}
  button{background:var(--accent);color:#20160A;border:0;border-radius:7px;
         padding:8px 14px;font-size:13px;font-weight:600;cursor:pointer}
  button.ghost{background:transparent;color:var(--muted);border:1px solid var(--border);
               font-weight:500;padding:4px 9px;font-size:12px}
  button.ghost:hover{color:var(--text);border-color:var(--muted)}
  button:hover{filter:brightness(1.07)}
  .err{color:#F0A19A;font-size:12.5px;min-height:18px;margin-top:6px}
  .err.ok{color:#9BE3C2}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th{text-align:left;font-size:11px;letter-spacing:.6px;text-transform:uppercase;
     color:var(--muted);font-weight:600;padding:7px 8px;border-bottom:1px solid var(--border)}
  td{padding:8px;border-bottom:1px solid var(--border);vertical-align:top}
  tr:last-child td{border-bottom:0}
  .path{color:var(--muted);font-size:12px;word-break:break-all}
  .tag{font-size:11px;padding:1px 7px;border-radius:4px;border:1px solid var(--border);
       background:var(--surface2);color:var(--muted);white-space:nowrap}
  .tag.created{color:#9BE3C2;border-color:#2F5B47}
  .tag.modified{color:#F3C88B;border-color:#5B4A2F}
  .tag.deleted{color:#F0A19A;border-color:#5B2F2F}
  .tag.git{color:#A9C7F2;border-color:#2F415B}
  .list{display:flex;flex-direction:column;max-height:330px;overflow:auto}
  .group{display:flex;align-items:center;gap:8px;position:sticky;top:0;z-index:2;
         background:var(--surface);border-bottom:1px solid var(--border);
         padding:9px 0 6px;font-size:11px;letter-spacing:.6px;text-transform:uppercase;
         color:var(--accent);font-weight:650}
  .group .count{color:var(--muted);background:var(--surface2);
                border:1px solid var(--border);border-radius:999px;
                padding:0 7px;font-size:10.5px;letter-spacing:0;text-transform:none}
  .link{background:none;border:0;padding:0;color:var(--text);font:inherit;font-weight:600;
        cursor:pointer;text-align:left}
  .link:hover{color:var(--accent);text-decoration:underline}
  tr.sel td{background:#242730}
  tr.sel .link{color:var(--accent)}
  .pill.focus{color:#F3C88B;border-color:#5B4A2F;background:#2A2419;cursor:pointer}
  .row{display:flex;gap:10px;align-items:baseline;padding:7px 0;
       border-bottom:1px solid var(--border);font-size:13px}
  .row:last-child{border-bottom:0}
  .row .when{color:var(--muted);font-size:11.5px;white-space:nowrap;min-width:56px}
  .row .who{color:var(--blue);font-size:12px;white-space:nowrap}
  .empty{color:var(--muted);font-size:13px;padding:8px 0}
  .num{text-align:right;font-variant-numeric:tabular-nums}
  footer{padding:0 22px 30px;color:var(--muted);font-size:12.5px}
  a{color:var(--accent)}
</style>
</head>
<body>
<header>
  <div class="brand">AgentScope <span>Lite</span></div>
  <div class="pill ok" id="status">connecting…</div>
  <div class="pill" id="uptime">—</div>
  <div class="pill" id="port">—</div>
  <div class="pill focus" id="focus-pill" style="display:none"
       title="Click to show every project again"></div>
  <div class="spacer"></div>
  <div class="pill" title="This tool never writes into a monitored project">read-only</div>
</header>

<main>
  <section class="card">
    <h2>Projects <span class="count" id="project-count">0</span></h2>
    <div class="body">
      <form id="add-form">
        <input type="text" id="path" placeholder="Absolute path to a project, e.g. D:\code\my-repo"
               autocomplete="off" spellcheck="false">
        <button type="submit">Register</button>
      </form>
      <div class="err" id="add-error"></div>
      <table>
        <thead><tr><th>Project</th><th class="num">Files</th>
          <th class="num">Commits</th><th></th></tr></thead>
        <tbody id="projects"></tbody>
      </table>
      <div class="empty" id="projects-empty">No projects registered yet.</div>
    </div>
  </section>

  <section class="card">
    <h2>Agent processes <span class="count" id="proc-count">0</span></h2>
    <div class="body" style="padding-top:0">
      <div class="list" id="processes"></div>
      <div class="empty" id="processes-empty">No matching process running.</div>
    </div>
  </section>

  <section class="card">
    <h2>File changes <span class="count" id="change-count">0</span></h2>
    <div class="body" style="padding-top:0">
      <div class="list" id="changes"></div>
      <div class="empty" id="changes-empty">Nothing has changed yet.</div>
    </div>
  </section>

  <section class="card">
    <h2>Git commits <span class="count" id="commit-count">0</span></h2>
    <div class="body" style="padding-top:0">
      <div class="list" id="commits"></div>
      <div class="empty" id="commits-empty">No commits recorded.</div>
    </div>
  </section>
</main>

<footer>
  Everything runs locally on <code id="footer-addr">127.0.0.1</code> and stays on this machine ·
  data file <code id="footer-data"></code> ·
  polling: files 2s · git 5s · processes 3s
</footer>

<script>
const TOKEN = __TOKEN__;
const DATA_FILE = __DATA_FILE__;
const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const num = n => (n ?? 0).toLocaleString();
const bytes = b => {
  if (b == null) return "—";
  const u = ["B","KB","MB","GB"]; let i = 0, v = Number(b);
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (i === 0 ? v.toFixed(0) : v.toFixed(1)) + " " + u[i];
};
const ago = iso => {
  if (!iso) return "—";
  const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
  const s = Math.max(0, (Date.now() - d.getTime()) / 1000);
  if (s < 60) return Math.round(s) + "s ago";
  if (s < 3600) return Math.round(s / 60) + "m ago";
  if (s < 86400) return Math.round(s / 3600) + "h ago";
  return Math.round(s / 86400) + "d ago";
};
const clock = s => {
  s = Math.floor(s || 0);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return (h ? h + "h " : "") + m + "m";
};

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {}, {"X-AgentScope-Token": TOKEN});
  const res = await fetch(path, Object.assign({}, opts, {headers}));
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = body.error || {};
    const codes = err.errors || [];
    const reasons = codes.map(code => WHY[code] || code);
    const message = reasons.length ? reasons.join(" · ")
                                   : (err.message || "HTTP " + res.status);
    const failure = new Error(message);
    failure.codes = codes;
    throw failure;
  }
  return body.data;
}

const $ = id => document.getElementById(id);

// server-side error codes -> something a human can act on
const WHY = {
  "required": "type a directory path first",
  "path must be absolute": "use an absolute path, e.g. D:\\code\\my-repo",
  "traversal": "the path must not contain ..",
  "not_found": "that directory does not exist here",
  "not_a_directory": "that path is a file, not a directory",
  "permission_denied": "that directory cannot be read (permission denied)",
  "already_registered": "that project is already registered",
};

// --- per-project reading: one project can be focused, feeds are grouped ----
let focusId = null;
try { focusId = localStorage.getItem("scope-focus") || null; } catch (e) { focusId = null; }

function setFocus(id) {
  focusId = (focusId === id) ? null : id;
  try { localStorage.setItem("scope-focus", focusId || ""); } catch (e) { /* private mode */ }
  tick();
}

function scoped(items) {
  return focusId ? items.filter(item => item.project_id === focusId) : items;
}

/** Bucket a feed by project, keeping project-registration order. */
function grouped(items, projects) {
  const order = projects.map(p => ({id: p.id, name: p.name}));
  const known = new Set(order.map(p => p.id));
  for (const item of items) {
    if (!known.has(item.project_id)) {           // defensive: never lose rows
      known.add(item.project_id);
      order.push({id: item.project_id, name: item.project_name || item.project_id});
    }
  }
  return order
    .map(p => ({name: p.name, items: items.filter(i => i.project_id === p.id)}))
    .filter(group => group.items.length);
}

/** Render buckets with sticky headers, capped to ``limit`` rows in total. */
function renderGroups(groups, rowHtml, limit, showHeaders) {
  let budget = limit;
  let html = "";
  for (const group of groups) {
    if (budget <= 0) break;
    const slice = group.items.slice(0, budget);
    budget -= slice.length;
    if (showHeaders) {
      html += `<div class="group">${esc(group.name)}` +
              `<span class="count">${group.items.length}</span></div>`;
    }
    html += slice.map(rowHtml).join("");
  }
  return html;
}

function render(s) {
  $("status").textContent = "live · " + s.health.status;
  $("uptime").textContent = clock(s.app.uptime_s) + " uptime";
  $("footer-data").textContent = DATA_FILE;

  const focused = focusId ? s.projects.find(p => p.id === focusId) : null;
  if (focusId && !focused) {                 // the focused project was removed
    focusId = null;
    try { localStorage.setItem("scope-focus", ""); } catch (e) { /* ignore */ }
  }
  const pill = $("focus-pill");
  pill.style.display = focused ? "" : "none";
  if (focused) pill.textContent = "focused: " + focused.name + "  ✕";

  // projects
  $("project-count").textContent = s.projects.length;
  $("projects-empty").style.display = s.projects.length ? "none" : "block";
  $("projects").innerHTML = s.projects.map(p => {
    const st = p.status || {};
    const dirty = st.clean ? "" :
      ` <span class="tag modified">${st.modified}M ${st.staged}S ${st.untracked}U</span>`;
    const badge = p.is_git
      ? `<span class="tag git">${esc(p.branch || "no branch")}</span>` + dirty
      : `<span class="tag">not git</span>`;
    const selected = p.id === focusId ? " sel" : "";
    const hint = p.id === focusId ? "click to show every project" : "show only this project";
    return `<tr class="${selected}">
      <td><button class="link" data-focus="${esc(p.id)}" title="${hint}">${esc(p.name)}</button>
          <div class="path">${esc(p.path)}</div><div>${badge}</div></td>
      <td class="num">${num(p.change_count)}</td>
      <td class="num">${num(p.commit_count)}</td>
      <td><button class="ghost" data-del="${esc(p.id)}">remove</button></td>
    </tr>`;
  }).join("");
  document.querySelectorAll("[data-focus]").forEach(btn => {
    btn.onclick = () => setFocus(btn.dataset.focus);
  });
  document.querySelectorAll("[data-del]").forEach(btn => btn.onclick = async () => {
    if (!confirm("Unregister this project? No files are touched.")) return;
    try { await api("/api/projects/" + btn.dataset.del, {method: "DELETE"}); await tick(); }
    catch (e) { alert(e.message); }
  });

  // processes
  $("proc-count").textContent = s.processes.length;
  $("processes-empty").style.display = s.processes.length ? "none" : "block";
  $("processes").innerHTML = s.processes.map(p => `<div class="row">
      <span class="when mono">#${p.pid}</span>
      <span><b>${esc(p.name)}</b>
        <span class="path">${esc((p.cmdline || "").slice(0, 150))}</span></span>
      <span class="spacer"></span>
      <span class="tag">${p.cpu_pct == null ? "—" : p.cpu_pct + "%"}</span>
      <span class="tag">${bytes(p.rss_bytes)}</span>
    </div>`).join("");

  // file changes — bucketed by project so feeds never interleave
  const changeRows = scoped(s.file_changes);
  const changeGroups = grouped(changeRows, s.projects);
  $("change-count").textContent = focused ? num(focused.change_count) : num(s.stats.file_events);
  $("changes-empty").style.display = changeRows.length ? "none" : "block";
  $("changes-empty").textContent = focused
    ? "Nothing has changed in this project yet." : "Nothing has changed yet.";
  $("changes").innerHTML = renderGroups(changeGroups, c => `<div class="row">
      <span class="when">${esc(ago(c.ts))}</span>
      <span class="tag ${esc(c.action)}">${esc(c.action)}</span>
      <span class="mono path" style="color:var(--text)">${esc(c.path)}</span>
    </div>`, 80, !focusId);

  // git commits — same treatment
  const commitRows = scoped(s.commits);
  const commitGroups = grouped(commitRows, s.projects);
  $("commit-count").textContent = focused ? num(focused.commit_count) : num(s.commits.length);
  $("commits-empty").style.display = commitRows.length ? "none" : "block";
  $("commits-empty").textContent = focused
    ? "No commits recorded for this project." : "No commits recorded.";
  $("commits").innerHTML = renderGroups(commitGroups, c => `<div class="row">
      <span class="when">${esc(ago(c.at))}</span>
      <span class="mono tag git" title="${esc(c.sha)}">${esc(c.short)}</span>
      <span>${esc(c.subject)}</span>
      <span class="who">${esc(c.author)}</span>
    </div>`, 80, !focusId);
}

let busy = false;
async function tick() {
  if (busy) return;
  busy = true;
  try {
    const data = await api("/api/state");
    render(data);
    $("status").className = "pill ok";
  } catch (e) {
    $("status").className = "pill warn";
    $("status").textContent = "offline — " + e.message;
  } finally {
    busy = false;
  }
}

$("add-form").onsubmit = async ev => {
  ev.preventDefault();
  const input = $("path"), note = $("add-error");
  note.className = "err";
  note.textContent = "checking…";
  try {
    const data = await api("/api/projects", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({path: input.value}),
    });
    input.value = "";
    if (data.ok) {
      note.className = "err ok";
      note.textContent = "registered " + data.project.path;
    }
    await tick();
  } catch (e) {
    note.className = "err";
    note.textContent = e.message;
  }
};

$("focus-pill").onclick = () => { if (focusId) setFocus(focusId); };  // clear the focus

setInterval(tick, 2000);
tick();
</script>
</body>
</html>
"""


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "AgentScopeLite/" + VERSION
    protocol_version = "HTTP/1.1"

    # -- plumbing ----------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:  # keep the console readable
        if getattr(self.server, "verbose", False):
            sys.stderr.write(f"{self.address_string()} - {fmt % args}\n")

    @property
    def app(self):
        return self.server.app

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, code: str, message: str, errors=None) -> None:
        self._json({"error": {"code": code, "message": message,
                              "errors": list(errors or [])}}, status)

    def _authorized(self) -> bool:
        """Mutations need the session token and a same-origin origin."""
        provided = self.headers.get("X-AgentScope-Token", "")
        expected = getattr(self.server, "token", "")
        if not (expected and hmac.compare_digest(provided, expected)):
            self._error(401, "unauthorized", "Missing or invalid local session.")
            return False
        origin = self.headers.get("Origin")
        if origin:
            allowed = {f"http://127.0.0.1:{self.server.port}",
                       f"http://localhost:{self.server.port}",
                       f"http://[::1]:{self.server.port}"}
            if origin not in allowed:
                self._error(403, "forbidden", "Cross-origin request rejected.")
                return False
        return True

    def _body(self) -> dict | None:
        """Parse a JSON body; ``None`` means an error response was already sent."""
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._error(413, "payload_too_large", "Request body too large.")
            return None
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            self._error(400, "bad_request", "Body must be JSON.")
            return None
        return data if isinstance(data, dict) else None

    # -- routes ------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            html = (
                INDEX_HTML
                .replace("__TOKEN__", json.dumps(getattr(self.server, "token", "")))
                .replace("__DATA_FILE__", json.dumps(str(self.app.state.data_file)))
            )
            return self._html(html)
        if path == "/api/state":
            return self._json({"data": self.app.state.snapshot()})
        if path == "/api/health":
            snap = self.app.state.snapshot()
            return self._json({"data": {"status": "ok", "version": VERSION,
                                        **snap["health"], "uptime_s": snap["app"]["uptime_s"]}})
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._error(404, "not_found", "Unknown path.")

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if not self._authorized():
            return
        if path == "/api/projects":
            body = self._body()
            if body is None:
                return
            result = self.app.state.add_project(str(body.get("path", "")))
            if not result["ok"]:
                return self._error(422, "validation_error",
                                   "That path cannot be registered.", result["errors"])
            return self._json({"data": result})
        if path == "/api/shutdown" and getattr(self.server, "allow_shutdown", False):
            self._json({"data": {"stopping": True}})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        self._error(404, "not_found", "Unknown path.")

    def do_DELETE(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if not self._authorized():
            return
        prefix = "/api/projects/"
        if path.startswith(prefix):
            pid = path[len(prefix):]
            if self.app.state.remove_project(pid):
                return self._json({"data": {"id": pid}})
            return self._error(404, "not_found", "No such project.")
        self._error(404, "not_found", "Unknown path.")


class LiteServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # never squat on a port somebody else owns

    def handle_error(self, request, client_address) -> None:
        """A browser closing a keep-alive connection is normal, not a crash."""
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class LiteApp:
    def __init__(self, data_file: Path, matchers: list[str] | None = None) -> None:
        self.state = State(data_file)
        if matchers:
            self.state.settings["matchers"] = matchers
        self.token = uuid.uuid4().hex + uuid.uuid4().hex
        self.server: LiteServer | None = None

    # -- monitors ----------------------------------------------------------
    def start_monitors(self) -> None:
        scanner = FileScanner(self.state)
        self.state.scanner = scanner
        self._monitors: list[threading.Thread] = [
            scanner,
            GitPoller(self.state),
            ProcessPoller(self.state),
        ]
        # seed baselines so the first poll does not report pre-existing files
        for project in self.state.projects:
            scanner.take_baseline(project["id"], project["path"])
        for monitor in self._monitors:
            monitor.start()

    def stop_monitors(self) -> None:
        for monitor in getattr(self, "_monitors", []):
            stop = getattr(monitor, "stop", None)
            if stop:
                stop()
        self.state.save()

    # -- serving -----------------------------------------------------------
    @staticmethod
    def pick_port(preferred: int) -> int:
        for port in range(preferred, preferred + 30):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    probe.bind(("127.0.0.1", port))
                except OSError:
                    continue
            return port
        raise SystemExit(f"no free TCP port between {preferred} and {preferred + 29}")

    def serve(self, port: int, open_browser: bool, verbose: bool = False) -> None:
        chosen = self.pick_port(port)
        self.server = LiteServer(("127.0.0.1", chosen), ApiHandler)
        self.server.app = self
        self.server.token = self.token
        self.server.port = chosen
        self.server.verbose = verbose
        self.server.allow_shutdown = True

        url = f"http://127.0.0.1:{chosen}/"
        print(f"{APP_NAME} {VERSION}")
        print(f"  dashboard : {url}")
        print(f"  token     : {self.token}")
        print(f"  data      : {self.state.data_file}")
        print(f"  projects  : {len(self.state.projects)} registered")
        print("  Ctrl+C to stop\n")

        if open_browser:
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        try:
            self.server.serve_forever(poll_interval=0.3)
        except KeyboardInterrupt:
            print("\n[lite] shutting down…")
        finally:
            try:
                self.server.server_close()
            except OSError:
                pass
            self.stop_monitors()
            print("[lite] stopped. Nothing in a monitored project was modified.")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="agentscope_lite.py",
        description="One-file local monitor for AI coding agents (stdlib only).",
    )
    parser.add_argument("--project", action="append", default=[], metavar="PATH",
                        help="register this project directory at startup (repeatable)")
    parser.add_argument("--port", type=int, default=8790,
                        help="preferred port (default 8790, next free port is used)")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open the browser automatically")
    parser.add_argument("--data", metavar="FILE", default=None,
                        help="data file (default ~/.agentscope_lite.json)")
    parser.add_argument("--match", action="append", default=[], metavar="NAME",
                        help="process matcher, e.g. --match opencode (repeatable)")
    parser.add_argument("--verbose", action="store_true",
                        help="log every HTTP request to the console")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    data_file = Path(args.data).expanduser() if args.data else DATA_FILE

    app = LiteApp(data_file, matchers=args.match or None)
    for raw in args.project:
        result = app.state.add_project(raw)
        if not result["ok"]:
            print(f"[lite] could not register {raw!r}: {', '.join(result['errors'])}",
                  file=sys.stderr)

    app.start_monitors()
    app.serve(args.port, open_browser=not args.no_browser, verbose=args.verbose)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
