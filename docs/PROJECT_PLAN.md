# AgentScope — Project Plan

> **Status:** PLAN MODE — awaiting explicit user approval. No application code has been written.
> **Last updated:** 2026-10-25

**Goal:** A production-quality, local-first desktop application ("AgentScope") that monitors multiple AI coding agents across multiple project directories on one machine, showing what they are doing, what is done, what remains, which milestones are reached, and what is blocked or failing.

**Architecture:** Python 3.13 / FastAPI local server bound to `127.0.0.1`, SQLite persistence (SQLAlchemy + Alembic), an asyncio monitoring engine (process, filesystem, Git, log, adapter ingestion), an SSE event stream to a React + TypeScript + Vite + Tailwind SPA served by the same server. All application state lives in a dedicated per-user data directory, never inside monitored projects.

**Tech stack:** see [TECHNICAL_DECISIONS.md](TECHNICAL_DECISIONS.md) §1.

**Spec:** the user requirement document "Project Prompt: Local AI Agent Task Monitor & Project Control Center" (sections 1–20). This plan argues from that spec.

---

## 1. Environment Discovery (completed in Plan Mode)

| Item | Result |
|---|---|
| OS | Windows 11 Pro 26200, 64-bit (primary target) |
| Secondary target | Linux (Ubuntu/Debian class); no Linux host available in this workspace — Linux scripts are written and reviewed, runtime verification deferred and will be reported as *unverified* |
| Python | **3.13.5** at `D:\piye\python.exe` (spec asks for 3.12 or compatible → see decision D-01) |
| pip | 25.1.1; PyPI reachable (verified by downloading a wheel) |
| uv | not installed → plain `venv` + `pip` (decision D-02) |
| Node / npm | v24.19.0 / 11.17.0 |
| Git | 2.56.0.windows.1 |
| Workspace | `D:\projek_belajar\ai_agent_monitor_center` — **empty, not a Git repository** |
| Git identity (global) | already `bagazblitz78 <bagasiqbal577@gmail.com>` — matches required identity |
| Remote | `https://github.com/bagazblitz78/ai-agent-monitoring-center.git` reachable and **empty** (`git ls-remote` exit 0, no refs) |
| Agent CLIs present | `opencode` **v2.0.29** (installed), `copilot` (VS Code shim only). `claude`, `codex`, `gh`, `aider`, `gemini` **not installed** |
| Available disk | C: ~8.7 GB free, D: ~52 GB free, G: ~8.2 GB free |
| Pre-existing Python packages | `pytest 9.1.1`, `python-dotenv 1.2.4` (global only — app will use its own venv) |

### 1.1 Workspace & Git findings

- No repository exists → `git init` is deferred until **after** plan approval (per spec §20).
- No existing files, branches, commits, or uncommitted changes exist that need preserving.
- Proposed workflow (pending approval): initialize on `main`, apply repository-local identity (`git config user.name/user.email`), add `.gitignore` before any commit, commit with Conventional Commits, **push only on explicit user authorization**.

---

## 2. Agent Integration Research (completed in Plan Mode)

Research was performed against real artifacts on this machine plus vendor documentation. Findings drive the adapter roadmap.

### 2.1 OpenCode — **verified, strongest integration target**

Local data directory `C:\Users\Bagas\.local\share\opencode` contains a SQLite database `opencode.db` with, verified by direct schema inspection:

| Table | Rows now | Useful for |
|---|---|---|
| `project` | 11 | registered OpenCode projects with `worktree` path → maps to AgentScope projects |
| `session` / `session_v2` | 11 / 19 | agent sessions, `directory`, `agent`, `model`, `time_updated`, token/cost counters |
| `session_message` / `message` / `part` | 1021 / 755 / 3550 | chronological activity, tool calls, assistant output |
| `todo` | 38 | task list with `status` + `priority` + `position` → maps to tasks |
| `worktree`, `project_directory` | 3 / 2 | directory ↔ project association |
| `credential`, (`auth.json`) | 2 / present | **must never be read or indexed** |

Additional OpenCode capabilities (from official docs, opencode v2):

- `opencode serve` starts a local v2 API + web server with an event subscription stream; `@opencode-ai/sdk` provides a client.
- Plugin system exposes `session.created/updated/idle/error`, `todo.updated`, `tool.execute.before/after`, `file.edited`, `permission.asked/replied`, `message.*`.

**Adapter plan:** read-only polling of `opencode.db` (opened with a read-only SQLite URI from a worker thread) as the default; optional live adapter against a running `opencode serve` instance. No writes to OpenCode's data. Plugin-based push is documented as an optional future path.

### 2.2 Claude Code — documented mechanism, **not installed here → cannot be verified**

Claude Code exposes deterministic hooks (`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `Notification`, `Stop`, `SubagentStop`, `SessionEnd`, `TaskCreated`, `TaskCompleted`, …) configured in `.claude/settings.json`, supporting `command`, `http`, `prompt` and `agent` hook types.

**Adapter plan:** ship a tiny `agentscope-report` hook helper that POSTs normalized events to the local API (and a `hook type: http` JSON template users can paste). Status in this project: **implemented-but-unverified** until a Claude Code installation is available. It will be labelled as such in the UI and docs.

### 2.3 GitHub Copilot CLI — **not verifiable in this workspace**

Only the VS Code shim `copilot.exe` exists. No documented local session/event API confirmed.

**Adapter plan:** no adapter in v1. Generic process monitoring covers it. Documented as "process monitoring only".

### 2.4 Codex CLI / others — **not installed, not verified**

Same treatment as Copilot: generic process monitoring only, adapter listed as a future, isolated module.

### 2.5 Integration priority realized (spec §6)

| Priority | Mechanism | v1 status |
|---|---|---|
| 1 | Generic process + resource monitoring (psutil) | **in scope** |
| 2 | Local structured event API (HTTP, versioned schema) | **in scope** |
| 3 | JSONL file reporting (spool dir, opt-in in-project) | **in scope** |
| 4 | Agent-specific adapters | **OpenCode (verified)**, Claude Code (ships unverified), Copilot/Codex (not in v1) |
| 5 | Hooks / session logs / CLI output | OpenCode DB + optional `agentscope run -- <cmd>` output tee |

---

## 3. Architecture & Design Summary

Full detail: [ARCHITECTURE.md](ARCHITECTURE.md). Key points:

1. **One process, one port.** FastAPI/uvicorn serves REST + SSE + the built frontend on `127.0.0.1:8787` (configurable).
2. **Dedicated data directory** via `platformdirs` (Windows `%LOCALAPPDATA%\AgentScope`, Linux `~/.local/share/agentscope`): SQLite DB, rotating logs, spool, backups, config. Monitored projects are read-only to AgentScope.
3. **Monitoring engine** = 5 cooperating asyncio components with bounded queues and debounce: `ProcessMonitor`, `FileMonitor` (watchdog), `GitMonitor` (polling), `LogMonitor`, `AdapterHub`.
4. **Single write path.** All components emit normalized `ActivityEvent`s into an `EventBus`; a single persister writes to SQLite and fans out over SSE. Idempotency via `event_uid`.
5. **Progress engine** is deterministic and inspectable: task → milestone → project weighted roll-up with an explicit verification policy (see ARCHITECTURE §6).
6. **Frontend** = React SPA with 8 routes (Dashboard, Projects, Project detail ×6 tabs, Agents, Tasks, Milestones, Activity, Notifications, Settings), dark/light themes, Recharts, Lucide.

### 3.1 Database

15 tables: `projects`, `agents`, `agent_sessions`, `milestones`, `milestone_dependencies`, `tasks`, `task_dependencies`, `activity_events`, `process_snapshots`, `log_entries`, `verification_runs`, `notifications`, `settings`, `git_commits`, `file_changes`. Full DDL in [ARCHITECTURE.md](ARCHITECTURE.md) §5.

---

## 4. Proposed Dependencies

Pins are *proposed*; exact versions are resolved and frozen into `requirements.lock` during M2 after a real install (any pin that fails on Python 3.13 will be bumped and recorded in TECHNICAL_DECISIONS).

### Backend (`requirements.txt`)

```
fastapi==0.115.*          uvicorn[standard]==0.32.*
sqlalchemy==2.0.*         alembic==1.14.*
pydantic==2.10.*          pydantic-settings==2.7.*
psutil==7.1.1             # wheel verified downloadable on this machine
watchdog==6.0.*           GitPython==3.1.*
sse-starlette==2.2.*      httpx==0.28.*
platformdirs==4.*         aiofiles==24.*
python-dateutil==2.9.*
```

### Dev / test (`requirements-dev.txt`)

```
pytest==9.*   pytest-asyncio==1.*   pytest-cov==6.*   ruff==0.9.*   mypy (optional, non-blocking)
```

### Frontend (`frontend/package.json`)

```
react@19  react-dom@19  typescript@5.*  vite@7  tailwindcss@4  @vitejs/plugin-react
recharts  lucide-react  @radix-ui/* (primitives used by the shadcn-style components)
zustand (client state)  @tanstack/react-table (dense tables)
vitest  @testing-library/react  @testing-library/user-event  jsdom
```

Nothing requires Docker, admin rights, or a cloud account.

---

## 5. Milestone Breakdown, Dependencies, Deliverables, Acceptance

Full tracker with per-task checkboxes: [MILESTONES.md](MILESTONES.md). Summary:

| # | Milestone | Depends on | Primary deliverables |
|---|---|---|---|
| M1 | Requirements & architecture | — | 5 planning docs, env report, integration constraints |
| M2 | Backend foundation | M1 | FastAPI app, venv, Alembic migrations, 15 models, error envelope, health API, test harness |
| M3 | Project & process monitoring | M2 | project CRUD + validation, Git inspection, psutil process monitor, watchdog file monitor |
| M4 | Agent integration | M3 | versioned event API, heartbeat API, JSONL spool reader, OpenCode adapter, Claude Code hook helper |
| M5 | Task & milestone engine | M4 | task/milestone CRUD, dependencies, progress calculator, verification runs |
| M6 | Frontend dashboard | M2 (API contract frozen) | SPA shell, dashboard, project detail 6 tabs, Kanban, charts, themes |
| M7 | Real-time events & notifications | M4, M6 | SSE hub, event history, rules engine, in-app notifications |
| M8 | Security & reliability | M4 | token auth, origin checks, rate/size limits, redaction, log rotation, retention, backup/restore |
| M9 | Testing & QA | M3–M8 | backend + frontend suites, mock agent, failure/permission/path-traversal tests |
| M10 | Documentation & release | M9 | install/start/stop/test scripts (Win+Linux), README, troubleshooting, restart recovery |

M6 can start against the frozen OpenAPI contract in parallel with M4/M5 (contract-first). M7's SSE hub lands in M2 skeleton form (endpoint exists, events are few) and is completed in M7.

### 5.1 Definition of Done mapping

| Spec §18 requirement | Verified by |
|---|---|
| 1 Runs locally on target laptop | M10 smoke script `test.bat` |
| 2 Register & monitor multiple directories | M3 acceptance + API tests |
| 3 Process status & resource usage | M3 `ProcessMonitor` tests with a spawned dummy process |
| 4 Structured events update tasks/milestones in real time | M4 + M7 integration test (mock agent → SSE assertion) |
| 5 Progress persists across restart | M9 restart-recovery test |
| 6 Inspect completed/pending/blocked/failed work | M5 + M6 filters and tests |
| 7 Source changes & Git info when supported | M3 Git tests against a generated temp repo |
| 8 Reported vs verified completion distinguished | M5 progress-policy tests |
| 9 Missing integrations / inaccessible paths handled | M3 permission tests + M4 "adapter unavailable" states |
| 10 Security & automated tests pass | M8 + M9 suites |
| 11 Install & troubleshooting docs | M10 |
| 12 Nothing falsely represented as implemented | MILESTONES.md status discipline + capability matrix in README |

---

## 6. Testing Requirements (summary)

- **Backend:** pytest + `fastapi.testclient`. Coverage targets: migrations, CRUD, API validation, event ingestion & duplicate suppression, progress math, task dependencies, process monitor, watchdog events, Git integration, redaction, permission errors, malformed input, unauthorized access, path traversal, SSE delivery, restart recovery.
- **Fixtures:** `tmp_path` projects, a generated `git init` repo, a spawned long-lived Python dummy process as a mock agent, a JSONL spool file writer, an OpenCode-DB-shaped SQLite fixture (schema copied from the real DB, zero real data).
- **Frontend:** Vitest + React Testing Library + jsdom for components, status-badge logic, progress formatting, Kanban column moves, filter state.
- **No paid agent subscription is required by any test.** OpenCode adapter tests run against a synthetic DB fixture, never the user's real `opencode.db`.
- Commands: `./test.bat` / `./test.sh` → `pytest` + `ruff check` + `npm run test`.

---

## 7. Risks & Limitations (declared up front)

| # | Risk / limitation | Mitigation |
|---|---|---|
| R1 | Cannot capture output of **already-running** terminal sessions (no OS-level hook without injection) | Document honestly; offer `agentscope run -- <cmd>` wrapper, log-file tailing, JSONL/API reporting, and adapter DB reads |
| R2 | Full-screen TUI apps do not emit linear stdout | Wrapper marks such sessions `output_capturable=false`; rely on adapter/process signals |
| R3 | Process→project association is often ambiguous | Never auto-associate on name match alone; require explicit registration or user confirmation |
| R4 | OpenCode DB schema may change across versions | Adapter reads defensively (column presence checks), version-gated, degraded to `unavailable` state |
| R5 | Claude Code / Copilot / Codex not installed → cannot be verified | Ship only what is tested; capability matrix marks each adapter `verified` / `unverified` / `not implemented` |
| R6 | No Linux host in this workspace | Scripts reviewed statically; status recorded as *unverified* in MILESTONES.md |
| R7 | Python 3.13 vs spec's 3.12 | Decision D-01: target 3.11–3.13, verified on 3.13.5 |
| R8 | Disk pressure on C: (8.7 GB free) | venv and data dir on D:; log retention caps |
| R9 | False "completed" reporting from agents | Separate `reported` vs `verified` states; progress policy blocks silent success (ARCHITECTURE §6) |
| R10 | Local server abuse via browser (DNS rebinding / CSRF) | Token cookie + Origin/Host validation + custom-header requirement (SECURITY_PLAN) |

---

## 8. Review Focus (most likely failure modes)

1. **A hostile or malformed agent event** (huge payload, forged IDs, path traversal in filenames, HTML/script in messages) → schema validation, size caps, sanitization on render, per-task tests in M4/M8.
2. **Cross-site request to `127.0.0.1:8787` from a random web page** → token + Origin checks; test in M8.
3. **Progress math silently counting blocked/failed/unverified work as done** → property tests over task states in M5.
4. **Duplicate event delivery creating duplicate tasks/milestones** → `event_uid` idempotency test in M4.
5. **Watchdog/psutil loops causing CPU churn or unbounded memory** → debounce, bounded queues, snapshot retention test in M3/M9.
6. **Restart losing or corrupting state** → migration + recovery test in M9.

---

## 9. Approval Gate

**STOP. Implementation (including `git init`) begins only after explicit user approval of this plan and the four companion documents.**

Companion documents:

- [ARCHITECTURE.md](ARCHITECTURE.md) — components, data flow, DB schema, API, UI structure
- [TECHNICAL_DECISIONS.md](TECHNICAL_DECISIONS.md) — decisions with alternatives and rationale
- [SECURITY_PLAN.md](SECURITY_PLAN.md) — threat model and control matrix
- [MILESTONES.md](MILESTONES.md) — task-level tracker with acceptance criteria

Requested from the user at approval time:

1. Approve or amend the plan (scope, priorities, adapter list).
2. Confirm whether Git repository initialization should proceed at the start of Build Mode.
3. Confirm whether push to `origin` is authorized later, or only local commits.
4. Confirm the preferred execution method for Build Mode (subagent-driven vs. in-session implementation).
