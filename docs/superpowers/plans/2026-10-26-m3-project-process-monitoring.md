# M3 — Project & Process Monitoring Implementation Plan

> **For agentic workers:** implement this plan task-by-task with TDD. Steps use checkbox (`- [ ]`) syntax for tracking. Review is dispatched by the controller after each task report.

**Goal:** register and monitor project directories safely: path validation service, inspect endpoint, project CRUD, Git inspection (status/commits/diff-on-demand), `GitMonitor`, `ProcessMonitor`, `FileMonitor`, the `file_changes` API, and multi-agent/non-git/permission resilience — with the hard guarantee that **no file in any monitored project is ever written, deleted, or moved**.

**Architecture:** new package `backend/app/services` (pure logic: `paths.py`, `git_info.py`) and `backend/app/engine` additions (`file_monitor.py`, `process_monitor.py`, `git_monitor.py`, `supervisor.py`, `events.py`). Routers live in `backend/app/api/projects.py` mounted on `app.state.api_router` (auth inherited). Monitors are read-only consumers: watchdog callbacks and psutil scans only ever *read* the project tree and *write* to AgentScope's own DB + SSE hub.

**Tech Stack:** as M2 plus `GitPython`, `psutil`, `watchdog` (already pinned in `requirements.txt`).

**Spec:** `docs/PROJECT_PLAN.md` + `docs/ARCHITECTURE.md` §5/§7/§8, `docs/SECURITY_PLAN.md` (path traversal + symlink rows, §7 checklist), `docs/MILESTONES.md` M3 checklist. The M2 plan's envelope/auth/timestamp rules remain binding.

## Global Constraints

- Python **3.13.5**; venv at repo-root `.venv`, run everything as `.venv\Scripts\python.exe -m ...`. No `uv`, no Docker, no admin rights.
- Server binds **`127.0.0.1`**; app data under `AGENTSCOPE_HOME`/platformdirs — **never** in the repo, **never** inside a monitored project.
- IDs UUIDv4 `str`; timestamps UTC ISO-8601 text with `Z`; success envelope `{"data": ..., "meta": {...}}`; error envelope `{"error": {code, message, details, request_id}}`.
- Every `/api/v1/*` route except `/api/v1/health` stays behind `require_session` (installed in M2) — new routers are mounted **on** `app.state.api_router` or must be swept if mounted elsewhere.
- **No writes to monitored projects. Ever.** No `git` commands that mutate (no `add`/`commit`/`checkout`), no file creation, no `chdir` into a project. GitPython is used read-only (`Repo`, `iter_commits`, `git.status` only).
- No `shell=True`, no f-string/`%` SQL, parameterized queries only. Redact email addresses in git author fields (`author_email_redacted`) and command lines before persisting (`redaction` logic is minimal here — full `redaction.py` lands in M8; M3 ships a local `redact_cmdline`/`redact_email` helper in `services/redact.py` used by monitors).
- Lint `ruff check .` clean; tests green (`pytest`); Conventional Commits with explicit paths; **never `git push`** (controller pushes after the milestone is green); never `git reset --hard`/`git clean`.
- Tests must be hermetic: temp projects under `tmp_path` (and one under the system temp dir for the second-drive case), no network, no dependency on any AI-agent subscription, no reliance on the developer's real git identity (fixtures set **repo-local** config inside the temp repo only).
- Import root `backend/` (`pythonpath = ["backend"]`); modules import as `app.services.paths`, `app.engine.file_monitor`, …

## Review Focus

1. **Traversal & boundary rejection** — `..%2F`, `C:\Windows\..`, relative path, UNC/device path, symlink escape, and `AGENTSCOPE_HOME`/repo-root registrations must all be rejected by `services/paths.py` before any DB row or watcher starts. Covered by `test_paths_service.py` (≥3 traversal vectors).
2. **Read-only guarantee** — registering + watching + an on-disk change must not modify anything else: acceptance test snapshots `st_mtime_ns` + size + content hash of every file in the temp project before/after and asserts equality (excluding the file the test itself mutates).
3. **Monitor scoping** — `ProcessMonitor` only inspects PIDs matching a *registered* agent matcher and skips the app's own PID tree; `FileMonitor` only watches registered roots; excludes (`.env*`, `node_modules/`, `.venv/`, `__pycache__/`, `.git/objects`, `dist/`, `build/`, `target/`, `*.lock`, `*.pem`, `*.key`, `id_rsa*`, files > 2 MB) must be asserted by test.
4. **Error containment** — permission-denied, non-git, repo-locked, and vanished directories surface as status fields/events (`unavailable`, `unknown`, `needs_confirmation`), never as a crash or unhandled 500.
5. **API contract** — envelope shape, `extra="forbid"` bodies, `page`/`page_size` (≤200) + `meta.total` pagination, 401 without cookie, 409 on duplicate `path_key`, 422 on invalid paths.

## Rulings made at planning time (recorded for reviewers)

- **R-3.1 — two inspect routes:** `POST /api/v1/projects/inspect` (body `{path}`) exists for *pre-registration* validation (MILESTONES M3 wording); `POST /api/v1/projects/{id}/inspect` re-inspects a *registered* project (ARCHITECTURE §8 wording). Both ship; they share `services/paths.py` + `services/git_info.py`.
- **R-3.2 — path policy:** reject non-absolute input, raw `..` segments, UNC (`\\…`) and device (`\\?\…`) paths, filesystem roots, and anything resolving inside `AGENTSCOPE_HOME` (watching our own DB is a feedback loop). `realpath`-resolved + casefolded → `path_key` for uniqueness. Any other existing readable local directory is acceptable (no OS-specific deny-list).
- **R-3.3 — `needs_confirmation`:** the 9-value `Agent.status` enum is closed, so ambiguity is *not* a status. `ProcessMonitor` emits `agent.match_ambiguous` activity events with `payload.state = "needs_confirmation"` (deduped while the candidate set is unchanged), leaves `confirmed_pid` NULL, and `POST /api/v1/agents/{id}/confirm-pid` (M4) resolves it.
- **R-3.4 — git unavailable:** `GitMonitor` errors set `projects.status = "unavailable"` (the column is free-text `status`, default `active`) and restore `active` on the next successful poll.
- **R-3.5 — event persistence in M3:** M4 owns the ingest bus/coalescing. M3 adds `engine/events.py::persist_event(...)` — inserts one `ActivityEvent` (deterministic `event_uid` = sha1 of type+scope+payload key) and `hub.publish`-es it on commit. Monitors use it; M4 replaces the internals without touching callers.
- **R-3.6 — DELETE = unregister:** `DELETE /api/v1/projects/{id}` removes the row; project-scoped telemetry cascades per the schema FKs; files on disk are untouched (asserted). Archive (`status=archived`) is the reversible path.
- **R-3.7 — monitors and tests:** monitor loops take an injectable interval/clock so tests drive them synchronously; no `sleep`-based flakiness beyond watchdog's inherent async nature (tests poll with a bounded wait helper, ≤5 s).

---

### Task 1: Path validation service

**Files:** Create `backend/app/services/__init__.py`, `backend/app/services/paths.py`; Test `backend/tests/test_paths_service.py`

**Interfaces:**
- Consumes: `resolve_paths()` (M2 config) to learn the forbidden home subtree.
- Produces:
  - `@dataclass(frozen=True) class PathValidation`: `ok: bool`, `path: Path | None`, `path_key: str | None`, `errors: list[str]` (machine codes: `not_absolute`, `traversal`, `unc_or_device`, `filesystem_root`, `inside_app_home`, `not_found`, `not_a_directory`, `not_readable`, `permission_denied`).
  - `validate_project_path(raw: str, *, must_exist: bool = True) -> PathValidation` — full policy R-3.2: rejects raw `..` segments (decoded input included), non-absolute, UNC/device prefixes, roots (`Path("C:\\").anchor == path` after normalize), and anything under `resolve_paths().home` (compared on `realpath`, casefolded on Windows); when `must_exist`, requires `exists/is_dir/os.access(R_OK)` and maps `PermissionError` → `permission_denied`.
  - `path_key_for(path: Path) -> str` — `os.path.realpath(path)` then `.casefold()` on Windows, `str` result (DB unique key).
  - `is_git_worktree(path: Path) -> bool` — true when `<path>/.git` exists as dir **or** file (worktree/submodule case).

- [x] **Step 1: Write failing tests** (`test_paths_service.py`):
  - traversal vectors (≥3): `"..%2F..%2Fetc"` (percent-decoded by caller test → the service must also see the decoded form), `str(tmp_path / ".." / "escape")`, `C:\Windows\..\Windows\System32` → each `ok=False`, `"traversal"` in errors;
  - relative path `"some/relative/dir"` → `not_absolute`; UNC `\\server\share\proj` and `\\?\C:\x` → `unc_or_device`; `C:\` → `filesystem_root`;
  - registering a path inside `AGENTSCOPE_HOME` → `inside_app_home`;
  - non-existent → `not_found`; a plain file → `not_a_directory`; unreadable/permission path → `permission_denied` (real chmod attempt, `skipif`-guarded on Windows if the OS still allows reads; plus a monkeypatched `PermissionError` variant that always runs);
  - `path_key_for` casefolds (`C:\Foo` == `c:\foo` on Windows) and realpath-resolves;
  - `is_git_worktree` true for `Repo.init`-ed tmp dir, false for plain dir.
- [x] **Step 2: Run and confirm failure** → `ModuleNotFoundError: app.services`.
- [x] **Step 3: Implement** `services/paths.py` per Interfaces; no I/O other than stat/access/realpath; never create directories.
- [x] **Step 4: Tests + ruff PASS** — `.venv\Scripts\python.exe -m pytest backend/tests/test_paths_service.py -v` then full suite.
- [x] **Step 5: Commit** — `feat: add project path validation service`

---

### Task 2: Git inspection service

**Files:** Create `backend/app/services/git_info.py`, `backend/app/services/redact.py`; Test `backend/tests/test_git_service.py`

**Interfaces:**
- Consumes: `is_git_worktree` (Task 1).
- Produces:
  - `@dataclass class GitInfo`: `is_git: bool`, `available: bool` (False on any GitPython error), `error: str | None`, `branch: str | None`, `default_branch: str | None`, `status_counts: dict[str, int]` (`modified`, `untracked`, `staged`, `conflicted`), `modified_files: list[str]`, `untracked_files: list[str]`, `last_commits: list[GitCommitInfo]`.
  - `@dataclass class GitCommitInfo`: `sha`, `short_sha`, `author_name`, `author_email_redacted`, `committed_at` (UTC `Z`), `subject`, `files: list[str]`.
  - `inspect_repo(path: Path, *, commits: int = 5) -> GitInfo` — `Repo(path, search_parent_directories=False)`; `repo.git.status --porcelain` parsed for counts/lists; branch via `repo.active_branch` (detached → `"HEAD detached"`); `default_branch` from `refs/remotes/origin/HEAD` if present else `None`; `list(repo.iter_commits(max_count=commits))` with file lists from `commit.stats.files` keys (cap 100 files/commit); every exception → `available=False, error=<sanitized short reason>` — **never raises**.
  - `commit_diff(path: Path, sha: str) -> list[str]` and `file_diff(path: Path, relpath: str, *, staged: bool = False) -> str` — read-only diff on demand; `file_diff` must reject `relpath` escaping the repo root (`..`, absolute) → `ValueError` (used by the M4+ diff endpoint, tested here).
  - `redact.py`: `redact_email(s: str) -> str` (`"b***@example.com"` style), `redact_cmdline(args: list[str]) -> str` (masks values that look like tokens/keys: flags named `--token/--key/--password/--secret/-p` and 20+ char opaque strings).
- [x] **Step 1: Failing tests** — fixture builds a temp repo **inside `tmp_path`** with repo-local identity (`repo.config_writer().set_value("user", "name", "Test") …` + `email`), commits 2 files, modifies one, leaves one untracked:
  - `inspect_repo` → `is_git, available, branch` correct, `status_counts["modified"] == 1`, `untracked == 1`;
  - `last_commits` newest-first, sha/short_sha/subject match, email redacted (no raw `@` local-part),
  - plain dir → `is_git False, available False-ish but no exception`;
  - `file_diff` with `"../outside.txt"` and `"C:\\x"` → `ValueError`;
  - git identity in effect for the test repo is **repo-local** (assert `repo.config_reader().get_value("user","email")` — GitPython's real method name; the plan originally wrote `getvalue`), global config untouched.
- [x] **Step 2: Confirm failure.**
- [x] **Step 3: Implement** (read-only GitPython only: `Repo`, `repo.git.status`, `iter_commits`, `diff`; no `index.add`, no `commit`).
- [x] **Step 4: Tests + ruff PASS.**
- [x] **Step 5: Commit** — `feat: add read-only git inspection service`

---

### Task 3: Inspect endpoints

**Files:** Create `backend/app/api/inspect.py`, `backend/app/schemas/projects.py`; Modify `backend/app/main.py` (mount); Test `backend/tests/test_inspect_api.py`

**Interfaces:**
- Consumes: Tasks 1–2, `ok()` envelope, `get_db` dep, auth via `api_router`.
- Produces:
  - `schemas/projects.py`: `class InspectRequest(BaseModel)` `path: str = Field(..., min_length=1, max_length=4096)`, `model_config = ConfigDict(extra="forbid")`.
  - `POST /api/v1/projects/inspect` → `{"data": {"input": str, "path": <abs>|null, "path_key": str|null, "exists": bool, "is_directory": bool, "readable": bool, "is_git": bool, "branch": str|null, "default_branch": str|null, "errors": [codes]}, "meta": {}}`. **Writes nothing** (assert: no `projects` row created, no files created). HTTP 200 even for invalid paths (the payload carries `errors`); 422 only for body-shape violations.
  - `POST /api/v1/projects/{id}/inspect` → same payload for a registered project's stored path; unknown id → 404 `not_found`.
- [x] **Step 1: Failing tests**: valid tmp dir → 200 `exists/is_directory/readable true`; traversal input → 200 with `errors=["traversal"]` and `path=None`; non-existent → `not_found` in errors; git fixture repo → `is_git true` + branch; no DB rows created before/after (count query); unknown `{id}` → 404 envelope; unauthenticated → 401; `extra` body field → 422.
- [x] **Step 2: Confirm failure.**
- [x] **Step 3: Implement** and mount on `app.state.api_router` in `create_app`.
- [x] **Step 4: Tests + ruff PASS** (auth-sweep test from M2 must still pass — new routes are guarded).
- [x] **Step 5: Commit** — `feat: add project inspect endpoints`

> **Fixture note:** the shared `app` fixture in `backend/tests/conftest.py` now points `AGENTSCOPE_HOME` at `tmp_path / "agentscope-home"` instead of `tmp_path` itself. The path policy refuses to watch anything inside the app home, so test projects must live outside it — otherwise every M3 registration would be (correctly) rejected as `inside_app_home`.

---

### Task 4: Project CRUD API

**Files:** Create `backend/app/api/projects.py`; extend `backend/app/schemas/projects.py`; Modify `backend/app/main.py`; Test `backend/tests/test_projects_api.py`

**Interfaces:**
- Consumes: Tasks 1–3; `Project` model (M2).
- Produces (all guarded, all enveloped, lists paginated `page`/`page_size` ≤ 200 with `meta.total`):
  - `POST /api/v1/projects` body `{name (1..120), path, watch_enabled?: bool, in_project_reporting?: bool}` `extra="forbid"` → validates path (422 + `details` with error codes when `validate_project_path.ok` is false), runs `inspect_repo`, inserts row (`is_git`, `default_branch`, `current_branch`, `path_key`); duplicate `path_key` → 409 `conflict`; returns `data` = full project resource. Non-git path registers fine with `is_git=false`, branch fields `null`.
  - `GET /api/v1/projects?status=active|archived|all&page&page_size` → list + `meta.total`.
  - `GET /api/v1/projects/{id}` → resource; 404 unknown.
  - `PATCH /api/v1/projects/{id}` → `name?`, `watch_enabled?`, `watch_rules_json?` (JSON-string object validated: must be an object, ≤ 4096 chars), `in_project_reporting?`; unknown field → 422; returns updated resource; `path` is **immutable** (field present → 422 `path_immutable`).
  - `DELETE /api/v1/projects/{id}` → unregister: row gone, **zero files touched on disk** (snapshot assertion), 204-equivalent → use `{"data": {"deleted": true}, "meta": {}}` with 200 (envelope consistency over bare 204); second delete → 404. Also covered: `status=archived` PATCH-equivalent via `POST /api/v1/projects/{id}/archive` is **not** required — archive via `PATCH` of `status`? **No**: keep `status` out of PATCH; archive = `POST /api/v1/projects/{id}/archive` and unarchive = `POST /api/v1/projects/{id}/unarchive` (explicit actions, `status` transitions `active ↔ archived`, archived projects excluded from default list and from monitoring).
  - `POST /api/v1/projects/{id}/refresh` → re-runs `inspect_repo`, updates `current_branch`/`is_git`, returns resource (ARCHITECTURE §8).
- [x] **Step 1: Failing tests**: create → 201-equivalent 200 with envelope; duplicate path → 409; traversal path → 422 with error codes; non-git create → `is_git=false`; list pagination (`page_size=1` of 3 → 1 item, `meta.total=3`); PATCH name ok / unknown key 422 / `path` immutability 422; archive → excluded from default list, still fetchable by id with `status=archived`, unarchive restores; delete → row gone + **file mtime/hash snapshot unchanged**; refresh updates branch after test repo creates a new branch (repo-local config only); unknown ids → 404; all unauthenticated → 401.
- [x] **Step 2: Confirm failure.**
- [x] **Step 3: Implement.**
- [x] **Step 4: Tests + ruff PASS.**
- [x] **Step 5: Commit** — `feat: add project registration and management API`

> **Ruling deviation (recorded for reviewers):** the plan says a PATCH carrying `path` fails with code `path_immutable`. M2's invariant test (`test_errors.py::test_api_error_status_mapping`) requires every non-500 `ApiError` to use the canonical code for its status — a 422 must say `validation_error` so a raised 422 and a FastAPI body-validation 422 are indistinguishable to clients. The endpoint therefore raises `ValidationFailed` (422 `validation_error`) with `details: [{"field": "path", "code": "path_immutable"}]` — the same delivery mechanism the create endpoint already uses for `traversal`/`not_found`. The M2 invariant was left untouched rather than weakened.

---

### Task 5: Event persistence helper + FileMonitor

**Files:** Create `backend/app/engine/events.py`, `backend/app/engine/file_monitor.py`; Test `backend/tests/test_file_monitor.py`, `backend/tests/test_persist_event.py`

**Interfaces:**
- Consumes: `FileChange`, `ActivityEvent` models (M2), `hub` (M2), R-3.5.
- Produces:
  - `events.py`: `persist_event(session_factory, *, type, source, severity="info", message="", payload=None, project_id=None, agent_id=None, task_id=None, milestone_id=None, event_uid=None) -> int` — deterministic default `event_uid = sha1(type|project_id|path|action|second-bucket)`, insert (duplicate `event_uid` → no-op), commit, then `asyncio`-thread-safe `hub.publish` (via `asyncio.get_running_loop().call_soon_threadsafe` when called from watchdog threads, else direct); returns `seq`.
  - `file_monitor.py`: `class FileMonitor` with `__init__(self, root: Path, project_id: str, session_factory, *, excludes: list[str] | None = None, debounce_s: float = 0.5, max_events_per_path_per_s: float = 10, max_file_bytes: int = 2 * 1024 * 1024)`, `start()/stop()`, plus `default_excludes()` matching ARCHITECTURE §7 exactly.
    - watchdog `FileSystemEventHandler`; on event: resolve `realpath` — **escape (outside `root`) → drop**; skip if any exclude pattern matches relative POSIX path (patterns: dir prefixes `node_modules/`, `.venv/`, `venv/`, `__pycache__/`, `.git/objects`, `dist/`, `build/`, `target/`, `.next/`, suffix `*.lock`, globs `.env*`, `*.pem`, `id_rsa*`, `*.key`); skip if size > `max_file_bytes` (size read on created/modified; missing file on delete → size `None`); debounce per path (500 ms) and rate cap (10/s/path) → overflow drops are **counted** (`self.dropped`), not raised.
    - each accepted change → `FileChange` row (`path_relative` POSIX-style, `action` `created|modified|deleted|renamed`, `source="watchdog"`) **and** `persist_event(type="file.changed")`; renames mapped (`moved` → `renamed`, dest path used).
- [x] **Step 1: Failing tests**:
  - `test_persist_event`: inserts row + returns seq; duplicate `event_uid` → single row, no exception.
  - `test_file_monitor`: helper `wait_for(predicate, timeout=5)`; in `tmp_path` project: create file → `file_changes` row `created`; modify → `modified`; `.env` write → **no** row; write inside `node_modules/` → no row; 3 rapid writes to one path within 500 ms → ≤ the debounce-allowed rows (coalesced); >10 writes in a second → rows for that path ≤ 10 + `dropped > 0`; 3 MB file → no row; symlink escape (create symlink in root pointing outside; **skip when `os.symlink` unavailable on Windows**) → event for the escaped target is dropped; `stop()` → subsequent writes produce no rows (bounded wait).
- [x] **Step 2: Confirm failure.**
- [x] **Step 3: Implement** — watchdog runs its own thread; DB writes use a **fresh short-lived session** from `session_factory` per batch (never the request-scoped session); observer daemonized, `stop()` joins.
- [x] **Step 4: Tests + ruff PASS** (watchdog tests may take a few seconds — keep total suite well under 60 s).
- [x] **Step 5: Commit** — `feat: add file monitor with debounce, excludes and event persistence`

---

### Task 6: ProcessMonitor

**Files:** Create `backend/app/engine/process_monitor.py`; Test `backend/tests/test_process_monitor.py`

**Interfaces:**
- Consumes: `Agent` model (M2 — matchers are `match_kind` ∈ `exe|cmdline_regex|pid|none`, `match_value`, `confirmed_pid`), `process_snapshots`, `events.persist_event`, `redact_cmdline`.
- Produces:
  - `class ProcessMonitor` with `__init__(self, session_factory, *, interval_s: float = 4.0, psutil_process_iter=psutil.process_iter)`, `tick() -> None` (one synchronous scan — **the test seam**), `start()/stop()` (asyncio task wrapper), `run_once()` alias not needed.
  - `tick()` semantics:
    1. load registered agents (`status` rows with a matcher ≠ `none`);
    2. `psutil.process_iter(["pid","ppid","name","exe","cmdline","create_time","cpu_percent","memory_info"])`;
    3. **skip** own PID tree (walk `psutil.Process().pid` parents up) and pid ≤ 4;
    4. match: `pid` → exact; `exe` → casefolded full-path equality; `cmdline_regex` → `re.search` over joined cmdline (invalid regex on the agent row → agent `last_error="invalid_matcher"`, no crash);
    5. per agent: exactly 1 candidate → `confirmed_pid` set, `status` → `running` (only if current status is `stopped`/`unknown` — never clobber `working`/`blocked` etc. reported by agents in M4), `status_source="observed"`, insert `process_snapshots` row (pid, ppid, exe, **redacted** cmdline, state, cpu_pct, rss_bytes, create_time);
    6. multiple candidates → no `confirmed_pid` change, emit `agent.match_ambiguous` with `payload.state="needs_confirmation"` and `payload.candidate_pids` — **deduped** while the candidate set is unchanged (R-3.3);
    7. zero candidates but `confirmed_pid` set and PID gone → `status="stopped"`, `status_source="observed"`, emit `agent.process_exited`, clear `confirmed_pid`;
    8. `psutil.AccessDenied` / `ZombieProcess` on a candidate → snapshot fields `None`, agent `status="unknown"`; never raises.
- [x] **Step 1: Failing tests** (use a **spawned dummy process**: `sys.executable -c "import time; time.sleep(60)"` with a unique marker string in argv, terminated in teardown; register an `Agent` with `match_kind="cmdline_regex", match_value=<marker>`):
  - discovery: after `tick()`, `confirmed_pid` equals the dummy's pid and a `process_snapshots` row exists with redacted cmdline (marker still visible — it's the matcher — but token-like args masked);
  - exit detection: kill the dummy, `tick()` → `status="stopped"`, event `agent.process_exited`, `confirmed_pid` NULL;
  - ambiguity: two dummies sharing the marker → no `confirmed_pid`, `agent.match_ambiguous` event with `state=needs_confirmation`, second `tick()` does **not** duplicate the event;
  - own-tree skip: agent `match_kind="pid", match_value=<our own pid>` → no confirmation of self;
  - access-denied: inject a fake `process_iter` raising `psutil.AccessDenied` for one pid → `status="unknown"`, no exception;
  - invalid regex matcher → `last_error="invalid_matcher"`, others still processed;
  - non-matching process never snapshotted.
- [x] **Step 2: Confirm failure.**
- [x] **Step 3: Implement** — `tick()` fully synchronous and injectable; `start()` schedules `tick` on an asyncio loop with `asyncio.sleep(interval_s)`.
- [x] **Step 4: Tests + ruff PASS** (terminate dummies in fixtures even on failure — `try/finally`).
- [x] **Step 5: Commit** — `feat: add process monitor with matcher scoping and ambiguity events`

---

### Task 7: GitMonitor

**Files:** Create `backend/app/engine/git_monitor.py`; Test `backend/tests/test_git_monitor.py`

**Interfaces:**
- Consumes: `services/git_info.inspect_repo`, `Project`/`GitCommit` models, `events.persist_event`, R-3.4.
- Produces:
  - `class GitMonitor` with `__init__(self, session_factory, *, interval_s: float = 5.0, commits_per_poll: int = 10)`, `tick() -> None`, `start()/stop()`.
  - `tick()`: for every `Project` with `status != "archived"` and `is_git=True` (re-check `is_git_worktree` cheaply): `inspect_repo`; on `available=False` → `projects.status="unavailable"`, `persist_event(type="git.unavailable", severity="warning")` (deduped per project+reason) **and no exception**; on recovery → status back to `active` + `git.recovered` event.
  - New commits (sha not in `git_commits` for that project, newest-first insert) → `GitCommit` rows (`author_email_redacted`, `files_json`, `source="git_poll"`) + one `git.commit` activity event per new commit (payload: sha, subject, branch); `current_branch` updated when it changes (`git.branch_changed` event).
- [x] **Step 1: Failing tests** (temp git fixture from Task 2):
  - first `tick()` seeds `git_commits` with the 2 pre-existing commits + emits `git.commit` events; second `tick()` without new commits → no new rows/events (idempotent);
  - new commit between ticks → exactly one new row + event;
  - non-git project → skipped silently (no rows, no crash);
  - `available=False` (delete `.git` after registration, or point at a locked/garbage dir) → `status="unavailable"` + event, no raise; restore `.git` → `active` + `git.recovered`;
  - archived project → not polled.
- [x] **Step 2: Confirm failure.**
- [x] **Step 3: Implement** (asyncio loop wrapper like ProcessMonitor; all GitPython access read-only).
- [x] **Step 4: Tests + ruff PASS.**
- [x] **Step 5: Commit** — `feat: add git monitor polling with commit capture`

---

### Task 8: `file_changes` API

**Files:** Create `backend/app/api/file_changes.py`; Modify `backend/app/main.py`; Test `backend/tests/test_file_changes_api.py`

**Interfaces:**
- Consumes: `FileChange` model, envelope helpers, auth via `api_router`.
- Produces:
  - `GET /api/v1/projects/{id}/file-changes?page&page_size&action&path_prefix&q&source` → `{"data": {"items": [FileChange…], …}, "meta": {total, page, page_size}}` — items ordered `ts`/`id` newest-first, each item carries `id, ts, path_relative, action, size_bytes, git_status, source`; unknown project → 404; `page_size > 200` → 422; archived project still readable.
  - Empty result → `items: []`, `meta.total: 0` (not an error).
- [ ] **Step 1: Failing tests**: seed 5 rows across 2 projects → pagination math + `total` scoped to the project; `action` filter; `path_prefix` filter; other project's rows never leak (security: cross-project isolation); 404; 422 over-cap; 401 unauthenticated; empty project → empty items.
- [ ] **Step 2: Confirm failure.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Tests + ruff PASS.**
- [ ] **Step 5: Commit** — `feat: add file changes listing API`

---

### Task 9: Engine supervisor, wiring and M3 acceptance

**Files:** Create `backend/app/engine/supervisor.py`; Modify `backend/app/main.py` (start/stop on app startup/shutdown — **lifespan**), `backend/app/api/health.py` (engine stats: monitored project count, monitor liveness flags); Test `backend/tests/test_supervisor.py`, `backend/tests/test_m3_acceptance.py`

**Interfaces:**
- Consumes: Tasks 5–7 monitors, project store.
- Produces:
  - `class Supervisor` with `start(app) / stop()` — builds/starts one `FileMonitor` per `active + watch_enabled` project (created **after** registration too: a `refresh_monitors()` method is called at the end of project create/archive/delete/patch handlers via `request.app.state.supervisor`), starts `ProcessMonitor` + `GitMonitor` once; `stop()` stops all observers/tasks cleanly on shutdown; double-start is a no-op; supervisor is **not** started when `Settings().disable_monitors` (new config flag, default False — tests set it to keep unit tests deterministic; TestClient fixture may run with monitors off unless a test opts in).
  - `GET /api/v1/health` gains `engine.monitors: {"file": n, "process": bool, "git": bool}` (documented in the M2 key-set? — additive key, acceptable; update the health shape test).
- [ ] **Step 1: Failing tests**:
  - `test_supervisor.py`: registering a project starts exactly one observer for its root (inspect `supervisor._monitors`); archiving stops it; deleting stops + removes; `watch_enabled=false` → no observer; app startup/shutdown lifecycle via TestClient context manager starts/stops without leaked threads (assert observer count 0 after exit).
  - `test_m3_acceptance.py` (the milestone acceptance, automated):
    1. create **three** project dirs: git repo on `tmp_path` (drive D:), plain non-git dir on `tmp_path`, and a plain dir under `tempfile.gettempdir()` (drive C: — the "another drive" case; skip that sub-case if it resolves to the same drive);
    2. register all three via API → 200s; non-git shows `is_git=false`; all listed;
    3. start monitors, mutate one file in the git project and one in the non-git project → `file_changes` API shows both within 5 s; create a commit → `git_commits` has the new sha, non-git project unaffected;
    4. **read-only assertion**: hash+mtime snapshot of every file in all three dirs taken before registration and after all monitoring activity → identical (except the two files the test itself wrote, whose *content is byte-equal to what the test wrote*, and no *new* files exist beyond those the test created);
    5. permission-denied path (chmod-0 dir with `skipif` fallback + monkeypatch variant) → API returns `permission_denied`, server still healthy (`/api/v1/health` 200).
- [ ] **Step 2: Confirm failure.**
- [ ] **Step 3: Implement** — wire monitors into `create_app` **lifespan** (not import time); project mutation endpoints call `refresh_monitors`.
- [ ] **Step 4: Full suite + ruff** — `.venv\Scripts\python.exe -m pytest -q` all green; `.venv\Scripts\python.exe -m ruff check .` clean. Boot smoke: uvicorn starts with monitors enabled, `/api/v1/health` 200, clean shutdown without dangling-thread warnings.
- [ ] **Step 5: Commit** — `feat: wire monitoring engine supervisor and M3 acceptance tests`

---

## Definition of Done for this plan

- `pytest` green (all 9 tasks, ≥ the M3 checklist tests in `docs/MILESTONES.md` line "Tests: path traversal (3 vectors) … permission-denied path"), `ruff check .` clean.
- `docs/MILESTONES.md` M3 checkboxes updated to match reality; untestable items marked `unverified` in the Deviations table, never silently ticked.
- uvicorn boots with monitors running; registering a directory via API makes file changes and commits visible in the API; no file in any monitored project modified (test-asserted).
- Commits are Conventional, one per task, explicit paths; **the controller pushes** after the milestone is green.
