# AgentScope — AI Agent Monitoring Center

**AgentScope** is a local-first service that watches AI coding agents while they work:
which processes belong to which agent, what files they change, what lands in Git,
and (later) what tasks and milestones are done — all from one dashboard on your own
machine. It reads your project directories **read-only**, stores everything in a
per-user data directory, and talks to nothing outside `127.0.0.1`.

> **Status:** backend in active build. `M1` (requirements & architecture), `M2`
> (backend foundation) and `M3` (project & process monitoring) are complete;
> `M4`–`M10` (agent adapters, task engine, frontend, events, security hardening,
> QA, release) are not started. The React frontend does not exist yet — the API
> is usable today through `/docs` and `curl`. See [docs/MILESTONES.md](docs/MILESTONES.md).

---

## One-file edition: `agentscope_lite.py`

Want the monitoring without the stack? **`agentscope_lite.py`** is the same
idea compressed into a single Python file that uses **only the standard
library** — no `pip install`, no database setup, no build step, no second
process to manage. Run it and a dashboard opens in your browser:

```bash
python agentscope_lite.py                                  # opens http://127.0.0.1:8790
python agentscope_lite.py --project D:/code/my-repo         # pre-register a project
python agentscope_lite.py --no-browser --port 8791          # headless
python agentscope_lite.py --match opencode --match claude   # custom agent matchers
```

| | Full backend (this repo) | `agentscope_lite.py` |
|---|---|---|
| UI | React SPA (M6, not built yet) | embedded HTML/CSS/JS dashboard |
| Server | FastAPI + uvicorn + Alembic | `http.server` in-process, 127.0.0.1 only |
| Storage | SQLite (WAL) + migrations | one JSON file, `~/.agentscope_lite.json` |
| File watching | `watchdog` events | polling every 2 s |
| Git | `GitPython`, 5 s poll | `git` CLI, 5 s poll, `GIT_OPTIONAL_LOCKS=0` |
| Processes | `psutil` matchers | PowerShell/`ps` poll every 3 s |
| Auth | `?t=` token → `HttpOnly` cookie | session token injected into the page, required on mutations |
| **Never writes into a monitored project** | asserted by the test suite | asserted by the test suite |

Same invariants: project paths are validated (`absolute`, exists, no
traversal, must be a directory), `.env`/`*.pem`/`*.key` and build trees are
excluded from file events, author e-mails and secret-looking command-line
values are redacted before anything is stored, and the monitor itself is never
listed among the agent processes. Run `python agentscope_lite.py --help` for
all flags.

---

## What works today

- **Project registration** — add a directory, and AgentScope validates the path
  (`realpath`, existence, readability, no traversal), detects whether it is a Git
  repository, and records its branch. Registering, archiving and deleting never
  touch the files themselves.
- **`POST /api/v1/projects/inspect`** — dry-run a path before registering it:
  exists? readable? git? branch? status counts — completely read-only.
- **File monitoring** — one `watchdog` observer per active, watched project with
  500 ms debounce, a rate cap, a 2 MB size cap, a default exclude list
  (`node_modules/`, `.venv/`, `.git/objects`, `.env*`, `*.pem`, `*.key`, …) and
  symlink-escape dropping. Changes are persisted and served by the
  `file-changes` API.
- **Process monitoring** — discovers PIDs that match a registered agent matcher
  (`exe` / `cmdline` regex / `pid`), snapshots CPU, RSS, PPID, state and start
  time, stores a **redacted** command line, and raises `needs_confirmation` when
  a matcher is ambiguous.
- **Git monitoring** — polls each repository every 5 s with strictly read-only
  Git commands (`GIT_OPTIONAL_LOCKS=0`, so even `git status` cannot rewrite
  `.git/index`) and records new commits.
- **Supervised engine** — a `Supervisor` owns the watchers and monitor tasks,
  starts them in the FastAPI lifespan, re-syncs them when projects change, and
  tears everything down on shutdown.
- **Local session auth** — a 256-bit token is minted at startup and exchanged
  once via `GET /?t=<token>` for an `HttpOnly; SameSite=Strict` cookie; every
  `/api/v1/*` route except `/api/v1/health` requires it.
- **SSE stream** — `GET /api/v1/stream` replays persisted events and resumes from
  `Last-Event-ID`.
- **Health** — `GET /api/v1/health` reports DB/schema status, uptime, engine
  queue depth and `engine.monitors` liveness.

**The one invariant everything hangs on:** AgentScope never writes, deletes or
mutates anything inside a monitored project directory. Tests assert it
byte-for-byte (every file hashed before and after a full monitoring run).

---

## Tech stack

| Layer | Choice |
|---|---|
| Language | Python ≥ 3.11 (developed on 3.13) |
| API | FastAPI + Uvicorn, OpenAPI at `/docs` |
| Persistence | SQLite (WAL) via SQLAlchemy 2 + Alembic migrations |
| Settings | `pydantic-settings`, `AGENTSCOPE_*` environment variables, `platformdirs` |
| Monitoring | `watchdog` (files), `psutil` (processes), `GitPython` (repos) |
| Real-time | `sse-starlette` (SSE), asyncio |
| Frontend | React + TypeScript + Vite + Tailwind — **not built yet** |
| Quality | `pytest` (+ `pytest-asyncio`), `ruff` |

---

## Getting started

```powershell
# 1. virtualenv + dependencies (from the repository root)
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt -r requirements-dev.txt

# 2. create the SQLite schema (repo-local alembic.ini, DB under AGENTSCOPE_HOME)
.venv\Scripts\python -m alembic upgrade head

# 3. run the server (loopback only)
.venv\Scripts\python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8787
```

macOS/Linux: use `.venv/bin/pip`, `.venv/bin/python` and the same commands.

Then open the token URL printed by the app (or read `<home>/session.token`):

```
http://127.0.0.1:8787/?t=<token>
```

That single request swaps the token for the session cookie and redirects you to
`/`. Without a frontend build, `/` answers with a JSON hint — use `/docs`
(Swagger UI) or `curl` until M6 ships the dashboard.

A quick round-trip:

```powershell
# register a project (send the session cookie with every call)
curl.exe -c jar.txt -X POST http://127.0.0.1:8787/api/v1/projects `
        -H "Content-Type: application/json" `
        -d '{"name":"demo","path":"D:/code/my-repo"}'

curl.exe -b jar.txt http://127.0.0.1:8787/api/v1/health
curl.exe -b jar.txt "http://127.0.0.1:8787/api/v1/projects/<id>/file-changes"
```

---

## Configuration

All settings are read from `AGENTSCOPE_*` environment variables
(`backend/app/config.py`).

| Variable | Default | Meaning |
|---|---|---|
| `AGENTSCOPE_HOME` | platform user data dir (`%LOCALAPPDATA%\AgentScope`, `~/.local/share/AgentScope`, …) | root for **all** application data; must be absolute |
| `AGENTSCOPE_HOST` | `127.0.0.1` | bind address; non-loopback requires `AGENTSCOPE_ALLOW_NON_LOCALHOST=1` |
| `AGENTSCOPE_PORT` | `8787` | documented port (pass `--host/--port` to uvicorn yourself) |
| `AGENTSCOPE_AUTH_MODE` | `token` | `off` disables the session check — **development only** |
| `AGENTSCOPE_DEBUG` | `false` | include tracebacks in 500 responses (never in production) |
| `AGENTSCOPE_ALLOW_NON_LOCALHOST` | `false` | explicit opt-in for a non-loopback bind |
| `AGENTSCOPE_DISABLE_MONITORS` | `false` | run the API without any file/process/git watchers |

Data layout under `AGENTSCOPE_HOME`:

```
AgentScope/
├── data/agent_scope.db   # SQLite (WAL)
├── session.token         # 256-bit local session token
├── logs/  spool/  backups/
└── agentscope.pid
```

Nothing in the application itself is ever written into the repository or into a
monitored project (test caches such as `.pytest_cache`/`.ruff_cache` are dev
artifacts, not app data).

---

## API surface (implemented)

Consistent envelopes: success `{"data": ..., "meta": {...}}`, failure
`{"error": {"code", "message", "details", "request_id"}}`. List endpoints take
`page` / `page_size` (≤ 200) and return `meta.total`.

| Method & path | Purpose |
|---|---|
| `GET /` | token exchange (`?t=`) / frontend / "not built" hint |
| `GET /api/v1/health` | liveness, DB + schema version, engine stats — **the only unauthenticated API route** |
| `GET /api/v1/session` | session probe (mode, authenticated, token preview) |
| `GET`/`PATCH /api/v1/settings` | application settings |
| `GET /api/v1/stream` | SSE event stream, resumable via `Last-Event-ID` |
| `POST /api/v1/projects/inspect` | validate/inspect an arbitrary path (read-only) |
| `POST /api/v1/projects/{id}/inspect` | re-inspect a registered project |
| `GET`/`POST /api/v1/projects` | list / register projects |
| `GET`/`PATCH`/`DELETE /api/v1/projects/{id}` | read / update / unregister (`path` is immutable) |
| `POST /api/v1/projects/{id}/archive` · `/unarchive` · `/refresh` | lifecycle actions |
| `GET /api/v1/projects/{id}/file-changes` | paginated file-change history |

Planned for later milestones: agents, sessions, tasks, milestones, event ingest,
activity, logs, verification, notifications, diff-on-demand — see
[docs/ARCHITECTURE.md §8](docs/ARCHITECTURE.md).

---

## Development

```powershell
.venv\Scripts\python -m pytest -q        # backend/tests — currently 262 passed, 4 skipped
.venv\Scripts\python -m ruff check .     # lint (line length 100, py311 target)
```

The suite is hermetic: every test builds its temp projects under pytest's
`tmp_path`, uses a repo-local git identity, spawns its own dummy processes, and
never touches the network or your real app home.

House rules (enforced by the plan that governs this build):

- test first — write the failing test, watch it fail, then implement;
- Conventional Commits, one commit per task, explicit paths;
- record every deviation from the plan in the Deviations table of
  [docs/MILESTONES.md](docs/MILESTONES.md) instead of silently diverging;
- never mark a feature complete that has not been demonstrated by a test.

Security posture, threat model and reporting: **[SECURITY.md](SECURITY.md)**.

---

## Repository layout

```
backend/
├── app/
│   ├── main.py          # app factory, lifespan, router mount, static/SPA
│   ├── config.py        # settings + path layout (AGENTSCOPE_*)
│   ├── db.py            # engine, WAL pragmas, session factory
│   ├── errors.py        # error envelope + handlers
│   ├── security.py      # local session token, guarded router
│   ├── api/             # health, settings, stream, projects, inspect, file_changes
│   ├── engine/          # supervisor, file/process/git monitors, event hub
│   ├── models/          # SQLAlchemy ORM (15 tables)
│   ├── schemas/         # Pydantic request/response models (extra="forbid")
│   └── services/        # paths, git_info, redact
├── alembic/             # migrations (alembic.ini lives at the repo root)
└── tests/               # pytest suite
docs/                    # PROJECT_PLAN, ARCHITECTURE, SECURITY_PLAN, MILESTONES, …
requirements*.txt        # pinned runtime + dev dependencies
```

---

## Documentation

| Document | Contents |
|---|---|
| [docs/PROJECT_PLAN.md](docs/PROJECT_PLAN.md) | goal, requirements, environment research, spec mapping |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | system diagram, data flow, DB schema, API and frontend design |
| [docs/TECHNICAL_DECISIONS.md](docs/TECHNICAL_DECISIONS.md) | numbered decisions and their trade-offs |
| [docs/SECURITY_PLAN.md](docs/SECURITY_PLAN.md) | full threat model and control matrix (target state) |
| [docs/MILESTONES.md](docs/MILESTONES.md) | milestone tracker, deviations, test status log |
| [SECURITY.md](SECURITY.md) | security posture **as implemented today** |

---

## Roadmap

| Milestone | Scope | Status |
|---|---|---|
| M1 Requirements & architecture | planning docs, user approval | ✅ |
| M2 Backend foundation | scaffold, DB, errors, auth, health, SSE skeleton | ✅ |
| M3 Project & process monitoring | paths, git, project API, 3 monitors, supervisor | ✅ |
| M4 Agent integration | adapters (OpenCode, Claude Code hooks, …) | ⬜ |
| M5 Task & milestone engine | tasks, dependencies, progress, verification | ⬜ |
| M6 Frontend dashboard | React SPA | ⬜ |
| M7 Real-time events & notifications | ingest, rules, SSE fan-out | ⬜ |
| M8 Security & reliability | rate limits, CSRF/Host checks, redaction, backups | ⬜ |
| M9 Testing & QA | coverage push, load/soak tests | ⬜ |
| M10 Documentation & release | packaging, start/stop scripts, release docs | ⬜ |

No license file has been chosen yet — treat the code as all rights reserved
until one is added.
