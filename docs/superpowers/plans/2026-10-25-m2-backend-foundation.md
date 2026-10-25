# M2 — Backend Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A runnable, tested FastAPI/SQLite foundation: project scaffold, config/paths, 15-table schema with migrations, shared API envelope + error handling, local token auth, settings & health endpoints, and an SSE endpoint that replays persisted events.

**Architecture:** Single Python package `backend/app` behind an app factory. Paths come from `platformdirs` (overridable with `AGENTSCOPE_HOME`), schema is owned by Alembic, one SQLAlchemy engine with WAL/FK pragmas. Routers are mounted by the factory; `security.require_session` is the single auth dependency installed in this milestone so later milestones never retrofit it.

**Tech Stack:** Python 3.13.5, FastAPI, Uvicorn, SQLAlchemy 2.0, Alembic, Pydantic v2 (`pydantic-settings`), `sse-starlette`-style manual SSE, pytest + pytest-asyncio + httpx, ruff.

**Spec:** `docs/PROJECT_PLAN.md` (approved 2026-10-25) with `docs/ARCHITECTURE.md`, `docs/TECHNICAL_DECISIONS.md`, `docs/SECURITY_PLAN.md` as the binding detail documents. The user requirement document (sections 1–20) is the ultimate authority.

## Global Constraints

- Python **3.13.5** at `D:\piye\python.exe`; venv at repo-root `.venv` (created by `python -m venv .venv`), activated by `.venv\Scripts\python.exe`. No `uv`, no `poetry`, no Docker, no admin rights.
- Server binds **`127.0.0.1:8787`** by default; a non-loopback bind must be refused unless `AGENTSCOPE_ALLOW_NON_LOCALHOST=1`.
- App data lives under `platformdirs` user data dir `AgentScope` (Windows `%LOCALAPPDATA%\AgentScope`, Linux `~/.local/share/agentscope`), overridable by `AGENTSCOPE_HOME`. **Never** inside the repo, **never** inside a monitored project.
- IDs: `str` UUIDv4 (`uuid.uuid4().hex` is acceptable only where a task states it). Timestamps: UTC ISO-8601 text with `Z`, e.g. `2026-10-25T09:30:00.000Z`, column type `Text` (models) / `datetime` (Pydantic, always timezone-aware, serialized with `Z`).
- Success envelope: `{"data": <payload>, "meta": {...}}` (`meta` may be `{}`). Error envelope: `{"error": {"code": "<snake_case>", "message": "<human text>", "details": <list>, "request_id": "<uuid4>"}}`.
- Auth dependency `app.security.require_session` guards every `/api/v1/*` route **except** `/api/v1/health`.
- No f-string / `%`-format SQL anywhere. No `shell=True`. No `dangerouslySetInnerHTML` concerns in this milestone.
- Lint: `ruff check .` clean. Tests: `pytest` green. Tests run with `.venv/Scripts/python.exe -m pytest`.
- **Commits:** Conventional Commits (`feat:`, `test:`, `docs:`, `refactor:`), run the test suite before every commit, inspect `git status` before staging, stage explicit paths (never `git add -A` from outside the repo root). Repository-local identity `bagazblitz78 <bagasiqbal577@gmail.com>` is already configured — do not touch global config.
- **Never `git push`.** The controller pushes after each green milestone. Never `git reset --hard`, never rewrite history, never `git clean`.
- Never write, delete, or move files inside a monitored project directory.
- Do not dispatch subagents (no helpers, no reviewers). Review is dispatched by the controller after your report.
- Python import root: `backend/` (configured via `pythonpath = ["backend"]`), so modules import as `app.config`, `app.models.project`, etc.

## Review Focus

1. **Path handling** — a `..`, a UNC path, or a symlinked directory reaching `resolve_paths`/`ensure_dirs` must not create directories outside the app home. Covered by `test_config_resolve_paths_rejects_relative_home` and `test_ensure_dirs_creates_only_under_home`.
2. **Auth bypass** — `/api/v1/health` is intentionally public; every other route must 401 without a valid cookie. Covered by `test_require_session_rejects_missing_cookie`, `test_require_session_rejects_wrong_token`, and a router-sweep test asserting the dependency is present on every mounted API route.
3. **Schema drift** — models and migrations must agree; `alembic upgrade head` on an empty DB followed by SQLAlchemy metadata comparison must report no diff. Covered by `test_migrations_match_metadata`.
4. **Envelope violations** — every error path (401/404/422/500) must return the exact error envelope with a `request_id`, and 500 bodies must never contain a traceback in non-debug mode. Covered by `test_error_envelope_*`.
5. **Timestamp/ID correctness** — every persisted timestamp round-trips as timezone-aware UTC and every id is a UUID string. Covered by `test_timestamps_are_utc_zulu` and `test_ids_are_uuid4_strings`.

---

### Task 1: Scaffold, venv, lint and test harness

**Files:**
- Create: `pyproject.toml`, `requirements.txt`, `requirements-dev.txt`, `requirements.lock`
- Create: `backend/__init__.py` (empty, not required for imports — see note), `backend/app/__init__.py`, `backend/tests/__init__.py`, `backend/tests/test_smoke.py`, `backend/tests/conftest.py`
- Test: `backend/tests/test_smoke.py`

**Interfaces:**
- Consumes: nothing.
- Produces: runnable `.venv`, `pyproject.toml` with `[tool.pytest.ini_options]` (`testpaths = ["backend/tests"]`, `pythonpath = ["backend"]`, `asyncio_mode = "auto"`) and `[tool.ruff]` (`target-version = "py311"`, `line-length = 100`, `lint.select = ["E","F","I","UP","B"]`); the `app` importable; the conftest fixture `app_paths` (see Task 2) is added in Task 2, so this task's conftest only defines `client` (added in Task 6) — for now an empty-but-present `conftest.py` plus `test_smoke.py`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_smoke.py
def test_app_package_importable():
    import app  # noqa: F401
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv\Scripts\python.exe -m pytest backend/tests/test_smoke.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app'` (or the venv not existing yet).

- [ ] **Step 3: Create the scaffold**

`python -m venv .venv`, then `.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt`.

`requirements.txt` (resolve the real versions with `.venv\Scripts\python.exe -m pip index versions <pkg>` or install and `pip freeze`; every line must be `name==version`, no ranges):
`fastapi`, `uvicorn[standard]`, `sqlalchemy`, `alembic`, `pydantic`, `pydantic-settings`, `psutil`, `watchdog`, `GitPython`, `sse-starlette`, `httpx`, `platformdirs`, `aiofiles`, `python-dateutil`.

`requirements-dev.txt`: `pytest`, `pytest-asyncio`, `pytest-cov`, `ruff`, `httpx` (already in runtime, repeated for TestClient use).

`requirements.lock`: `.venv\Scripts\python.exe -m pip freeze > requirements.lock`.

Create `backend/app/__init__.py` (docstring only), `backend/tests/__init__.py`, `backend/tests/conftest.py` (empty file is fine), and `pyproject.toml` as specified above. Add `[project]` metadata: `name = "agentscope"`, `version = "0.1.0"`, `requires-python = ">=3.11"`.

- [ ] **Step 4: Run the test and the linter**

Run: `.venv\Scripts\python.exe -m pytest -v` then `.venv\Scripts\python.exe -m ruff check .`
Expected: PASS (1 test), ruff reports 0 issues (add `[tool.ruff]` excludes for `.venv`, `frontend`, `.superpowers`).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml requirements.txt requirements-dev.txt requirements.lock backend/
git commit -m "chore: scaffold backend package, venv and test harness"
```

---

### Task 2: Configuration and application paths

**Files:**
- Create: `backend/app/config.py`
- Test: `backend/tests/test_config.py`

**Interfaces:**
- Consumes: nothing (Task 1 scaffold).
- Produces:
  - `class Settings(BaseSettings)` with fields `app_name: str = "AgentScope"`, `version: str = "0.1.0"`, `host: str = "127.0.0.1"`, `port: int = 8787`, `home: Path | None = None`, `auth_mode: Literal["token", "off"] = "token"`, `debug: bool = False`, `allow_non_localhost: bool = False`; `model_config = SettingsConfigDict(env_prefix="AGENTSCOPE_", extra="ignore")`.
  - `@dataclass(frozen=True) class AppPaths` with `home, data, logs, spool, backups: Path` and `db, token, pid: Path`.
  - `resolve_paths(settings: Settings | None = None) -> AppPaths`
  - `ensure_dirs(paths: AppPaths) -> None`
  - `settings_singleton() -> Settings` (lazily constructed, cached in module global `SETTINGS`).
  - `assert_bind_allowed(settings: Settings) -> None` → raises `RuntimeError` when `host` is not `127.0.0.1`/`localhost`/`::1` and `allow_non_localhost` is false.

Layout: `home/data/agent_scope.db`, `home/logs/`, `home/spool/`, `home/backups/`, `home/session.token`, `home/agentscope.pid`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_config.py
from pathlib import Path
import platformdirs
from app.config import Settings, ensure_dirs, resolve_paths, assert_bind_allowed

def test_resolve_paths_uses_agent_scope_home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTSCOPE_HOME", str(tmp_path))
    p = resolve_paths(Settings())
    assert p.home == tmp_path
    assert p.db == tmp_path / "data" / "agent_scope.db"
    assert p.token == tmp_path / "session.token"

def test_resolve_paths_default_uses_platformdirs(monkeypatch):
    monkeypatch.delenv("AGENTSCOPE_HOME", raising=False)
    p = resolve_paths(Settings())
    assert p.home == Path(platformdirs.user_data_dir("AgentScope"))

def test_ensure_dirs_creates_only_under_home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTSCOPE_HOME", str(tmp_path))
    p = resolve_paths(Settings())
    ensure_dirs(p)
    for d in (p.data, p.logs, p.spool, p.backups):
        assert d.is_dir() and tmp_path in d.parents

def test_resolve_paths_rejects_relative_home(monkeypatch):
    monkeypatch.setenv("AGENTSCOPE_HOME", "relative/dir")
    import pytest
    with pytest.raises(ValueError):
        resolve_paths(Settings())

def test_default_bind_is_loopback_and_non_loopback_refused():
    s = Settings()
    assert s.host == "127.0.0.1"
    assert_bind_allowed(s)            # must not raise
    import pytest
    with pytest.raises(RuntimeError):
        assert_bind_allowed(Settings(host="0.0.0.0"))
    assert_bind_allowed(Settings(host="0.0.0.0", allow_non_localhost=True))
```

- [ ] **Step 2: Run and confirm failure**
  Run: `.venv\Scripts\python.exe -m pytest backend/tests/test_config.py -v` → FAIL (`ModuleNotFoundError: app.config`).

- [ ] **Step 3: Implement `backend/app/config.py`**
  Use `pydantic_settings.BaseSettings`; resolve `home` from `settings.home` or env `AGENTSCOPE_HOME` or `platformdirs.user_data_dir("AgentScope")`; every path is derived with the `/` operator from an absolute `home`; `resolve_paths` raises `ValueError("AGENTSCOPE_HOME must be an absolute path")` when the resolved home is not absolute.

- [ ] **Step 4: Run tests + ruff** → PASS, 0 issues.

- [ ] **Step 5: Commit** — `git add backend/app/config.py backend/tests/test_config.py && git commit -m "feat: add configuration and application path resolution"`

---

### Task 3: Database core, first models, initial migration

**Files:**
- Create: `backend/app/db.py`, `backend/app/models/__init__.py`, `backend/app/models/base.py`, `backend/app/models/project.py`, `backend/app/models/agent.py`, `backend/app/models/setting.py`
- Create: `alembic.ini`, `backend/alembic/env.py`, `backend/alembic/versions/0001_initial.py`
- Create: `backend/app/models/project.py` etc. as above; `backend/scripts/migrate.py` is **not** created (use the `alembic` CLI).
- Test: `backend/tests/test_db.py`, `backend/tests/test_migrations.py`

**Interfaces:**
- Consumes: `AppPaths` and `resolve_paths()` from Task 2.
- Produces:
  - `db.py`: `engine` (module-level, built lazily from `resolve_paths().db`), `SessionLocal: sessionmaker`, `get_session() -> Iterator[Session]` (FastAPI dependency, `yield`), `new_session() -> Session`, `apply_pragmas(connection, record) -> None` setting `PRAGMA journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`.
  - `models/base.py`: `class Base(DeclarativeBase)`; mixins `TimestampMixin` (`created_at: Mapped[str]`, `updated_at: Mapped[str]`, defaults `utc_now()`), `def utc_now() -> str` returning `datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")`, `def new_id() -> str` returning `uuid.uuid4().hex`.
  - `models/project.py`: `class Project(Base)` — table `projects`, columns exactly as ARCHITECTURE §5 (`id` str PK default `new_id`, `name`, `path`, `path_key`, `is_git: bool`, `default_branch`, `current_branch`, `watch_enabled: bool = True`, `watch_rules_json: str = "{}"`, `in_project_reporting: bool = False`, `status: str = "active"`, timestamps, `archived_at: str | None`), `UniqueConstraint("path_key")`, index on `path_key`.
  - `models/agent.py`: `class Agent(Base)` (table `agents`, columns per ARCHITECTURE §5 including `project_id` FK `ondelete="CASCADE"` with `index=True`, `adapter`, `provider`, `match_kind`, `match_value`, `confirmed_pid`, `status`, `status_source`, `last_activity_at`, `last_event_message`, `last_error`) and `class AgentSession(Base)` (table `agent_sessions`, `UniqueConstraint("agent_id", "external_key")`, columns per ARCHITECTURE §5).
  - `models/setting.py`: `class Setting(Base)` — table `settings`, `key: str` PK, `value_json: str`, `updated_at: str`.
  - `models/__init__.py` re-exports `Base`, `Project`, `Agent`, `AgentSession`, `Setting`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_db.py
def test_pragmas_are_applied(tmp_path, monkeypatch): ...        # WAL + foreign_keys=ON via a connection
def test_foreign_keys_enforced(tmp_path, monkeypatch): ...      # inserting agents.project_id = "nope" raises IntegrityError
def test_timestamps_are_utc_zulu(): ...                         # Project().created_at endswith "Z" and parses with tzinfo=utc
def test_ids_are_uuid4_strings(): ...                           # len(Project().id) == 32 and uuid.UUID(...).version == 4
# backend/tests/test_migrations.py
def test_upgrade_head_creates_all_tables(): ...                 # alembic upgrade head on empty tmp DB → expected table set
def test_migrations_match_metadata(): ...                       # alembic compare_metadata (autogenerate against a stub) reports no diff
```

- [ ] **Step 2: Run and confirm failure**

- [ ] **Step 3: Implement** the files above; configure `alembic.ini` (`script_location = backend/alembic`, `sqlalchemy.url` set from `app.config.resolve_paths().db` inside `backend/alembic/env.py`, importing `app.models` so `Base.metadata` is populated). Make `env.py` importable both from repo root and from `backend/` by inserting `backend` on `sys.path`.
- [ ] **Step 4: Run tests + ruff** → PASS.
- [ ] **Step 5: Commit** — `chore: add database core, initial models and migration 0001`

---

### Task 4: Work-graph models (milestones, tasks, dependencies)

**Files:**
- Create: `backend/app/models/milestone.py`, `backend/app/models/task.py`
- Create: `backend/alembic/versions/0002_work_graph.py`
- Test: `backend/tests/test_work_graph_models.py`

**Interfaces:**
- Consumes: `Base`, `TimestampMixin`, `new_id` from Task 3; `Project` FK target.
- Produces:
  - `class Milestone(Base)` — table `milestones`, columns per ARCHITECTURE §5 (`project_id` FK CASCADE indexed, `title`, `description`, `position: int`, `weight: float = 1.0`, `status` default `"planned"`, `completion_criteria`, `started_at`, `completed_at`).
  - `class MilestoneDependency(Base)` — table `milestone_dependencies`, composite PK `(milestone_id, depends_on_id)`, both FK CASCADE.
  - `class Task(Base)` — table `tasks`, columns per ARCHITECTURE §5 including `milestone_id` FK `SET NULL`, `parent_id` self-FK `SET NULL`, `agent_id` FK `SET NULL`, `external_key`, `priority` default `"medium"`, `status` default `"backlog"`, `weight: float = 1.0`, `progress_pct: int = 0`, `verification_status` default `"unverified"`, `criteria_json`, `evidence_json`, `blocker`, `error_details`, `due_at`, `started_at`, `completed_at`, `reported_completed_at`, `verified_at`; `UniqueConstraint("project_id", "external_key")`; index on `(project_id, status)`.
  - `class TaskDependency(Base)` — table `task_dependencies`, composite PK `(task_id, depends_on_id)`.
  - All exported from `models/__init__.py`.

- [ ] **Step 1: Write failing tests**: task with `external_key` duplicated inside the same project raises `IntegrityError`; duplicate across two projects is allowed; self-referencing `parent_id` FK resolves; `Task.status` default is `"backlog"` and `verification_status` default `"unverified"`.
- [ ] **Step 2: Run, confirm failure.**
- [ ] **Step 3: Implement models + migration `0002`.**
- [ ] **Step 4: Tests + ruff PASS.**
- [ ] **Step 5: Commit** — `feat: add milestone, task and dependency models`

---

### Task 5: Telemetry models (events, snapshots, changes, logs, verification, notifications)

**Files:**
- Create: `backend/app/models/activity.py`, `backend/app/models/telemetry.py`
- Create: `backend/alembic/versions/0003_telemetry.py`
- Test: `backend/tests/test_telemetry_models.py`

**Interfaces:**
- Consumes: Task 3 base.
- Produces:
  - `class ActivityEvent(Base)` — table `activity_events`, `seq: int` `Integer primary_key=True, autoincrement=True` (declared **first**), `event_uid: str` `unique=True, index=True`, `schema_version: int = 1`, `ts: str`, `type: str`, `source: str`, `severity: str = "info"`, `message: str = ""`, `payload_json: str = "{}"`, nullable FKs `project_id/agent_id/session_id/task_id/milestone_id` with `index=True` on `project_id`, `agent_id`, `type`; index on `ts`.
  - `class ProcessSnapshot(Base)` — table `process_snapshots` (per ARCHITECTURE §5) with index `(agent_id, ts)`.
  - `class FileChange(Base)` — table `file_changes`, index `(project_id, ts)`.
  - `class GitCommit(Base)` — table `git_commits`, `UniqueConstraint("project_id", "sha")`.
  - `class LogEntry(Base)` — table `log_entries`, indexes `ts`, `(project_id, ts)`, `level`.
  - `class VerificationRun(Base)` — table `verification_runs`.
  - `class Notification(Base)` — table `notifications`, index `created_at`.
  - `class ActivityEvent.total_count(session) -> int` classmethod not required; provide module function `count_events(session) -> int` in `models/activity.py`.

- [ ] **Step 1: Failing tests**: `seq` autoincrements across two inserts (1 then 2); duplicate `event_uid` raises `IntegrityError`; nullable FKs default to `NULL`; table/index set matches the migration (compare `Base.metadata.tables` names against `inspector.get_table_names()`).
- [ ] **Step 2: Confirm failure.**
- [ ] **Step 3: Implement + migration `0003`.**
- [ ] **Step 4: Tests + ruff PASS.**
- [ ] **Step 5: Commit** — `feat: add activity, telemetry and notification models`

---

### Task 6: Shared schemas, error envelope and request middleware

**Files:**
- Create: `backend/app/schemas/__init__.py`, `backend/app/schemas/common.py`, `backend/app/errors.py`, `backend/app/middleware.py`, `backend/app/main.py`
- Modify: `backend/tests/conftest.py` (add `client` fixture)
- Test: `backend/tests/test_errors.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `schemas/common.py`: `class Envelope(BaseModel)` is **not** used to wrap responses (routers return `JSONResponse({"data":..., "meta":...})`); instead provide helpers `def ok(data, meta=None) -> dict` and `def paged(items, page, page_size, total) -> dict`. Also `class PageParams(BaseModel)` with `page: int = Field(1, ge=1)`, `page_size: int = Field(50, ge=1, le=200)`.
  - `errors.py`: `class ApiError(Exception)` with `code: str`, `message: str`, `status: int`, `details: list`; concrete classes `NotFound(code="not_found", status=404)`, `Unauthorized(status=401)`, `Forbidden(status=403)`, `ValidationFailed(status=422)`, `Conflict(status=409)`, `RateLimited(status=429)`, `PayloadTooLarge(status=413)`; `def install_error_handlers(app: FastAPI) -> None` registering handlers for `ApiError`, `RequestValidationError`, `HTTPException`, and `Exception`. Each handler returns `JSONResponse(status_code=..., content={"error": {"code", "message", "details", "request_id": request.state.request_id}})`. The generic `Exception` handler must log the traceback server-side and return `code="internal_error"`, `message="An internal error occurred."` with **no** traceback text when `settings.debug` is false.
  - `middleware.py`: `class RequestContextMiddleware(BaseHTTPMiddleware)` assigning `request.state.request_id = uuid.uuid4().hex` and echoing it in the `X-Request-Id` response header.
  - `conftest.py`: `client` fixture — builds the app with `app.main.create_app()` and returns `TestClient(app)`; also expose `app` itself for route introspection. `backend/app/main.py` provides `def create_app() -> FastAPI` that installs middleware + error handlers and mounts no routers yet — Task 7 creates `app.state.api_router`, Task 8/9 mount routes.

- [ ] **Step 1: Failing tests**

```python
def test_404_uses_error_envelope(client):
    r = client.get("/api/v1/does-not-exist")
    assert r.status_code == 404
    body = r.json()["error"]
    assert body["code"] == "not_found" and body["details"] == []
    assert len(body["request_id"]) == 32

def test_422_validation_error_shape(client): ...   # POST bad body → code "validation_error", details is a list
def test_request_id_header_present(client): ...    # response has X-Request-Id matching body request_id
def test_500_hides_traceback(client, monkeypatch): ...  # route raising ValueError → message has no "Traceback", code "internal_error"
def test_api_error_status_mapping(): ...           # each class in errors.py has the documented status
```

- [ ] **Step 2: Confirm failure.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Tests + ruff PASS.**
- [ ] **Step 5: Commit** — `feat: add error envelope, request context and shared schemas`

---

### Task 7: Local session token authentication

**Files:**
- Create: `backend/app/security.py`
- Modify: `backend/app/main.py` (install auth, add `GET /` exchange route)
- Test: `backend/tests/test_security.py`

**Interfaces:**
- Consumes: `resolve_paths()`, `Settings` (Task 2), `errors.Unauthorized` (Task 6).
- Produces:
  - `def mint_token() -> str` → `secrets.token_urlsafe(32)`.
  - `def load_or_create_token(paths: AppPaths) -> str` — reads `paths.token` if present and non-empty, else mints, writes with `0o600` permissions where the OS supports it, returns the value.
  - `async def require_session(request: Request) -> str` — returns the token when `settings.auth_mode == "off"`; otherwise compares the `agentscope_session` cookie to the app token with `secrets.compare_digest`; raises `Unauthorized` (code `unauthorized`, message `"Missing or invalid local session."`) on mismatch. Reads the app token from `request.app.state.session_token`.
  - `def install_auth(app: FastAPI, token: str) -> None` — stores `app.state.session_token = token`, adds `require_session` as `dependencies=[Depends(require_session)]` on `app.state.api_router` **and** registers the route `GET /` which, when `?t=<token>` matches, sets the cookie (`HttpOnly`, `SameSite=strict`, `path=/`, `max_age=86400`) and 307-redirects to `/`; when `t` is absent or wrong, returns `JSONResponse({"data": {"status": "ok"}})` until Task 9 serves static files there.
  - `app.state.api_router = APIRouter(prefix="", dependencies=[Depends(require_session)])` is **created here** (empty) so Task 8 and Task 9 mount their routers onto it; it is mounted onto the app in Task 9.
  - Also registers `GET /api/v1/session` → `{"data": {"mode": "token", "authenticated": true, "token_preview": "<first 6 chars>…"}, "meta": {}}`. This route is the authenticated probe used by tests and by the frontend to confirm its cookie; it lives on `api_router` so it carries the auth dependency.

- [ ] **Step 1: Failing tests**

```python
def test_missing_cookie_is_rejected(client):        # GET /api/v1/session with no cookie → 401
def test_wrong_cookie_is_rejected(client): ...      # cookie set to "nope" → 401, code "unauthorized"
def test_valid_cookie_is_accepted(client): ...      # cookie from app.state.session_token → 200 and data.authenticated is true
def test_token_exchange_sets_cookie_and_redirects(client): ...  # GET /?t=<token> → 307, Set-Cookie agentscope_session, Location "/"
def test_bad_token_exchange_does_not_set_cookie(client): ...
def test_token_file_created_once(tmp_path, monkeypatch): ...    # two calls return the same token; file exists
def test_auth_mode_off_bypasses_check(monkeypatch): ...
def test_every_api_route_requires_session(client): ...  # iterate app.routes; all mounted under /api/v1 except /api/v1/health carry the dependency
```

- [ ] **Step 2: Confirm failure.**
- [ ] **Step 3: Implement** as specified. The sweep test inspects `route.dependencies` for `Depends(require_session)` on `APIRoute` objects whose `path.startswith("/api/v1")` and `path != "/api/v1/health"`.
- [ ] **Step 4: Tests + ruff PASS.**
- [ ] **Step 5: Commit** — `feat: add local session token authentication`

---

### Task 8: Settings and health endpoints

**Files:**
- Create: `backend/app/schemas/settings.py`, `backend/app/api/__init__.py`, `backend/app/api/deps.py`, `backend/app/api/settings.py`, `backend/app/api/health.py`
- Modify: `backend/app/main.py` (mount routers on `app.state.api_router`)
- Test: `backend/tests/test_settings_api.py`, `backend/tests/test_health_api.py`

**Interfaces:**
- Consumes: `Setting` model (Task 3), `ok()` (Task 6), `require_session` (Task 7), `get_session()` (Task 3).
- Produces:
  - `api/deps.py`: `def get_db() -> Generator[Session]` (thin alias of `app.db.get_session`).
  - `schemas/settings.py`: `class SettingsPatch(BaseModel)` with `values: dict[str, Json] = Field(..., min_length=1)`, `model_config = ConfigDict(extra="forbid")`; validator rejects more than 64 keys or any serialized value longer than 4096 characters (raises `ValueError`).
  - `GET /api/v1/settings` → `{"data": {"theme": ...}, "meta": {}}` — all rows from `settings` decoded from `value_json`.
  - `PATCH /api/v1/settings` with `SettingsPatch` → upserts each key in one transaction, returns the full merged map. Duplicates within one payload are impossible (dict).
  - `GET /api/v1/health` → `{"data": {"status": "ok", "version": "0.1.0", "database": {"ok": true, "path": "<absolute>", "schema_version": "<alembic version or null>"}, "uptime_seconds": <float>, "engine": {"queue_depth": 0, "events_processed": 0, "sse_clients": 0}}, "meta": {}}` — **no auth**. Database check runs `SELECT 1` and reports `status: "degraded"` with `database.ok=false` on failure while still returning HTTP 200.

- [ ] **Step 1: Failing tests**: settings GET empty map; PATCH persists and GET reflects it; PATCH with `extra` field → 422; PATCH with 65 keys → 422; PATCH with a 5000-char value → 422; health unauthenticated → 200 with the exact key set above; health reports `version == "0.1.0"`.
- [ ] **Step 2: Confirm failure.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Tests + ruff PASS.**
- [ ] **Step 5: Commit** — `feat: add settings and health endpoints`

---

### Task 9: App factory, SSE replay endpoint and engine hub

**Files:**
- Create: `backend/app/engine/__init__.py`, `backend/app/engine/hub.py`, `backend/app/api/stream.py`
- Modify: `backend/app/main.py` (full assembly: config → paths → auth → error handlers → middleware → `app.state.api_router` mounted at `/` → static files from `frontend/dist` when present, with an HTML fallback returning a helpful message when `dist` is missing)
- Test: `backend/tests/test_stream.py`, `backend/tests/test_app_factory.py`

**Interfaces:**
- Consumes: Tasks 2, 6, 7, 8; `ActivityEvent` from Task 5.
- Produces:
  - `engine/hub.py`: `class EventHub` with `def __init__(self, max_history: int = 500)`, `def subscribe(self) -> asyncio.Queue`, `def unsubscribe(self, q: asyncio.Queue) -> None`, `async def publish(self, message: dict) -> int` (assigns a monotonically increasing `hub_seq`, appends to a bounded `deque`, fans out to all subscriber queues, returns `hub_seq`), `def subscriber_count(self) -> int`. Module singleton `hub = EventHub()`.
  - `api/stream.py`: `@router.get("/api/v1/stream") async def stream(since_seq: int = Query(0, ge=0), replay_only: bool = Query(False)) -> StreamingResponse`.
    - Reads `activity_events` where `seq > since_seq` ordered by `seq` ascending, limit 501. If more than 500 rows exist, emit `event: resync\ndata: {"reason":"overflow"}\n\n` first and skip the replay body.
    - Each replayed row is emitted as `id: <seq>` / `event: activity` / `data: <json {"seq","type","source","severity","message","ts","project_id","agent_id","task_id","milestone_id","payload"}>` with `\n\n` terminator.
    - If `replay_only` is true the generator returns immediately after replay (connection closes).
    - Otherwise it subscribes to `hub` and forwards published messages as `event: activity` frames, sending `: ping\n\n` every 15 s while idle, and exits when the client disconnects.
  - `main.py::create_app()` returns the fully assembled app; module-level `app = create_app()` for `uvicorn app.main:app`.

- [ ] **Step 1: Failing tests**

```python
def test_create_app_wires_everything(): ...     # app.state.api_router has routes for /api/v1/settings, /api/v1/health, /api/v1/stream; error handlers installed
def test_stream_replay_returns_persisted_events_in_order(client, session): ...  # insert 3 ActivityEvent rows, GET /api/v1/stream?replay_only=true&since_seq=0 with cookie → 200, content-type text/event-stream, three `id:` lines ascending, one `event: activity` per row
def test_stream_since_seq_filters(client, session): ...   # since_seq=2 returns only rows with seq 3+
def test_stream_replay_overflow_emits_resync(client, session): ...  # insert 501 rows → body starts with event: resync and contains no activity payload lines
def test_stream_requires_auth(client): ...      # no cookie → 401
def test_hub_publish_reaches_subscriber(): ...  # asyncio test: subscribe, publish, queue gets the message with hub_seq == 1
def test_hub_history_is_bounded(): ...          # publish 600 with max_history=500 → len(deque) == 500
```

- [ ] **Step 2: Confirm failure.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Full suite + ruff** → `.venv\Scripts\python.exe -m pytest -v` and `.venv\Scripts\python.exe -m ruff check .` → PASS, 0 issues. Then boot smoke: `.venv\Scripts\python.exe -c "from app.main import create_app; create_app()"`.
- [ ] **Step 5: Commit** — `feat: add app factory and SSE event stream`

---

## Definition of Done for this plan

- `pytest` green (all 9 tasks), `ruff check .` clean.
- `.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8787` starts, `/api/v1/health` answers unauthenticated, `/api/v1/settings` answers only with the session cookie, `/docs` loads.
- `alembic upgrade head` on an empty database creates all 15 tables and is idempotent on a second run.
- `docs/MILESTONES.md` M2 checkboxes updated to reflect reality; no task marked complete without passing tests.
- Commits are logical, Conventional, and pushed by the controller only after the milestone is green.
