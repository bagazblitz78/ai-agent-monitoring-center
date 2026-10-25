# AgentScope — Milestones & Task Tracker

> Companion to [PROJECT_PLAN.md](PROJECT_PLAN.md). **Living document** — status is updated continuously during Build Mode.
> A milestone may only be marked `[x]` when every task below it is done **and** its acceptance criteria and required tests pass.

**Overall status:** M1 — ✅ approved 2026-10-25 (Build Mode active) · M2 — 🟡 in progress
**Rules:** never mark an incomplete feature as completed; record blockers and failed tests in the "Deviations & Blockers" section at the bottom; an unverifiable capability is reported as `unverified`, not as done.

---

## M1 — Requirements and architecture — ✅ complete

- [x] Inspect workspace, OS, Python, Node, Git, package managers
- [x] Inspect Git state and target remote (`origin` empty, no local repo)
- [x] Research agent integration capabilities (OpenCode verified, Claude Code documented, Copilot/Codex unverifiable here)
- [x] Design architecture, DB schema, API, frontend structure, monitoring engine, security model
- [x] Identify technical limitations and compatibility risks (PROJECT_PLAN §7)
- [x] Produce `docs/PROJECT_PLAN.md`, `docs/ARCHITECTURE.md`, `docs/TECHNICAL_DECISIONS.md`, `docs/SECURITY_PLAN.md`, `docs/MILESTONES.md`
- [x] **User approval of the plan** ← gate (approved 2026-10-25, "Approve as written")
- [x] (post-approval) `git init` on `main`, local identity, `.gitignore`, initial commit `docs: add approved planning documents`

**Acceptance:** ✅ all five docs exist, cover spec §§1–20, and the user explicitly approves.

---

## M2 — Backend foundation — 🟡 in progress

Depends on: M1 approval.

- [ ] Project scaffold: `backend/`, venv, `requirements.txt` + `requirements.lock`, `requirements-dev.txt`, `ruff` config
- [ ] `config.py` (pydantic-settings, `platformdirs` paths, `AGENTSCOPE_HOME` override)
- [ ] `db.py` (engine, WAL/FK pragmas, session factory, single-writer helper)
- [ ] SQLAlchemy models for all 15 tables + FK/index definitions
- [ ] Alembic init + initial migration; `alembic upgrade head` idempotent on empty DB
- [ ] Pydantic request/response schemas with `extra="forbid"` and length caps
- [ ] Error envelope + request-ID middleware + generic 401/403/404/422/429/413/500 handlers
- [ ] `GET /api/v1/health` (liveness, db check, engine stats, queue depth)
- [ ] `GET /api/v1/settings`, `PATCH /api/v1/settings`
- [ ] App factory wired for test client + dev reload + static serving of `frontend/dist`
- [ ] Test harness: `conftest.py` with tmp data dir, test DB, TestClient fixture
- [ ] SSE endpoint skeleton (`/api/v1/stream`) replaying persisted events

**Tests:** migrations create/upgrade, health, settings round-trip, error envelope shape, 401/404 behavior.
**Acceptance:** `pytest` green; `uvicorn` boots and `/docs` loads; DB file created in the app data dir, not the repo.

---

## M3 — Project and process monitoring — ⬜ not started

Depends on: M2.

- [ ] Path validation service (`realpath`, existence, readability, non-directory rejection, traversal rejection)
- [ ] `POST /api/v1/projects/inspect` → exists? readable? git? branch? (no writes)
- [ ] Project CRUD (create/list/get/patch/archive/delete = unregister only, files untouched)
- [ ] Git service: branch, status counts, modified/untracked, last N commits, diff-on-demand
- [ ] `GitMonitor` polling loop emitting `git.commit` events
- [ ] `ProcessMonitor`: registered agent matchers → PID discovery, snapshots (cpu/mem/ppid/start time), redacted cmdline, ambiguous-match → `needs_confirmation`
- [ ] `FileMonitor` (watchdog) with default exclude list, 500 ms debounce, event cap, escape-drop on symlink
- [ ] `file_changes` API with pagination
- [ ] Multi-agent-per-project support and non-git project support
- [ ] Inaccessible-directory and permission-error handling → surfaced status, no crash

**Tests:** path traversal (3 vectors), non-existent path, non-git project, temp git repo status/commit detection, spawned dummy process discovery + snapshot + exit detection, watchdog create/modify events with debounce, excluded `.env` not reported, symlink escape dropped, permission-denied path.
**Acceptance:** register 3 directories (one non-git, one on another drive), see live file changes and git commits in API; no file in any project is modified (asserted by snapshotting project mtimes/hashes).

---

## M4 — Agent integration — ⬜ not started

Depends on: M3.

- [ ] Event schema v1 (Pydantic) with closed enums, size/depth caps, `schema_version` negotiation
- [ ] `POST /api/v1/events` batch ingest (1–100), idempotent via `event_uid`, returns per-item results
- [ ] `POST /api/v1/agents/{id}/heartbeat` (updates `last_heartbeat_at`, sets `working`/`idle` per payload)
- [ ] Agent + session CRUD; status/status-source (`observed|reported|estimated|unknown`) bookkeeping
- [ ] Event → task/milestone side effects (`task.started/completed/failed`, `milestone.*`, `blocker.*`, `approval.*`) as idempotent upserts keyed on `external_key`
- [ ] JSONL spool reader (AdapterHub) with resumable read offset and crash-safe re-read
- [ ] `scripts/agentscope-report` helper CLI (`init`, `task`, `milestone`, `blocker`, `heartbeat`) using `httpx`
- [ ] OpenCode adapter: read-only `opencode.db` polling, schema introspection, session/todo → AgentScope mapping, `credential`/`auth.json` untouched, `degraded` state on schema drift
- [ ] Claude Code hook helper + documented `settings.json` snippet (shipped as **unverified**)
- [ ] Adapter capability matrix endpoint + UI flag (`verified` / `unverified` / `unavailable`)
- [ ] Opt-in in-project JSONL reporting (default off, warning in UI)

**Tests:** valid ingest, malformed JSON, oversize batch, unknown event type → 400, future `schema_version` → 426, duplicate event_uid → single row, JSONL re-read after crash → no dup tasks, synthetic OpenCode DB fixture mapping, missing-column fixture → `degraded`, heartbeat timeout → `stale` (not `failed`).
**Acceptance:** a mock agent built from `scripts/agentscope-report` can create tasks, progress them, complete a milestone, and raise/resolve a blocker — all visible via API with correct idempotency.

## M5 — Task and milestone engine — ⬜ not started

Depends on: M4 (event side effects), can start after M2 for CRUD.

- [ ] Task CRUD with all spec fields (parent, priority, status, weight, due, dependencies, evidence, blocker, error)
- [ ] 9 task statuses; status transition validation (e.g. blocked → completed requires criteria)
- [ ] Task dependencies (DAG) with cycle rejection; dependent blocked while prerequisites incomplete
- [ ] Milestone CRUD, ordering, weights, completion criteria, milestone dependencies
- [ ] Progress engine (`services/progress.py`): task/milestone/project scores, `null` on zero denominator
- [ ] Completion policy: `require_verification` / `allow_reported`, `satisfied()` logic
- [ ] `GET /api/v1/milestones/{id}/progress` returning the full calculation breakdown
- [ ] Verification runs: allowlisted checks, argv-only, timeout, exit code, excerpt, `verification_status` update
- [ ] Kanban-supporting endpoints (bulk status change, ordering)

**Tests:** every status→score branch, cancelled exclusion, blocked never reaches 1.0, failed = 0, unreported/awaiting-verification completion blocked when `require_verification`, weighted math with non-uniform weights, zero-denominator → `null`, dependency cycle rejected, dependent blocked, restart persistence.
**Acceptance:** progress numbers shown by the API match hand-computed expectations for a fixture project; each response includes the calculation metadata.

## M6 — Frontend dashboard — ⬜ not started

Depends on: M2 (frozen OpenAPI contract, D-15).

- [ ] Vite + React + TS + Tailwind scaffold, dark/light theme, design tokens, no-template component system
- [ ] App shell: sidebar nav (Dashboard, Projects, Agents, Tasks, Milestones, Activity, Notifications, Settings), topbar with global search (`Ctrl-K`), theme toggle, notification bell
- [ ] Dashboard: 10 summary cards (projects, agents, running, working, idle/waiting, blocked, failed tasks, completed tasks, milestones completed, overall progress) — every card shows its source/denominator on hover
- [ ] Agent overview table/cards: name, type, project, absolute path, current task, current milestone, process status, last activity, last event, %, blocker, CPU/mem
- [ ] Projects list: folder picker + manual absolute path, git badge + branch, validation errors
- [ ] Project detail tabs: Overview, Kanban board (drag-drop, filters: agent/milestone/priority/status), Milestone timeline, Activity feed (source labels), File changes (+diff on demand), Logs (search/filter), Verification
- [ ] Empty/loading/error states everywhere; skeleton loaders; confirmation dialogs; tooltips on technical fields
- [ ] Preferences persistence (theme, filters, column config, density)
- [ ] Responsive ≥1024 px; keyboard navigation and visible focus rings
- [ ] Vitest + RTL component tests

**Tests:** status-badge matrix, progress formatting incl. `unknown`, filter logic, Kanban move reducer, empty/error states, preferences persistence.
**Acceptance:** dashboard answers all 8 questions in spec §11 against a seeded fixture; `npm run build` produces `dist/` served by FastAPI.

## M7 — Real-time events and notifications — ⬜ not started

Depends on: M4, M6.

- [ ] SSE hub with `Last-Event-ID` replay, bounded buffers, slow-consumer disconnect, 1 s coalescing digests
- [ ] Client `useSSE` hook: auto-reconnect, cursor resume, `resync` handling, visibility-aware pause
- [ ] Live updates for: agent start/stop, task status, milestone completion, blocker, file change, git commit, verification result, unexpected process exit
- [ ] Notification rules engine (11 default rules, enable/severity/cooldown/scope)
- [ ] Notifications API + bell badge + Notifications page (mark read, filter, jump-to-source)
- [ ] `heartbeat.missing` → `stale`/`unresponsive` state only (never `failed`)
- [ ] Optional desktop notifications behind an opt-in setting

**Tests:** SSE delivery of a persisted event, resume from `since_seq`, coalescing, reconnect after server restart, rule fires once within cooldown, stale-vs-failed distinction, frontend hook reconnect behavior.
**Acceptance:** an event posted by the mock agent appears in the browser without a page refresh within ~1 s.

## M8 — Security and reliability — ⬜ not started

Depends on: M4 (real traffic to protect).

- [ ] All controls in [SECURITY_PLAN.md](SECURITY_PLAN.md) §2 implemented
- [ ] `security.py`: token issue/verify, cookie exchange, Origin/Host checks, custom-header CSRF rule, rate limiter, body-size cap
- [ ] `redaction.py` applied to logs, cmdline snapshots, error details, SSE payloads
- [ ] Sensitive-file exclusion defaults + per-project override
- [ ] Log rotation + retention job (all rows in SECURITY_PLAN §5)
- [ ] Backup (`VACUUM INTO`) + restore path + startup `PRAGMA quick_check`
- [ ] Safe error handling (no tracebacks in responses when not debug)
- [ ] Non-loopback bind refused without explicit opt-in

**Tests:** the full checklist in SECURITY_PLAN §7.
**Acceptance:** every checklist item automated or explicitly recorded as a manual verification with evidence.

## M9 — Testing and quality assurance — ⬜ not started

Depends on: M3–M8.

- [ ] End-to-end integration test: mock agent → events → tasks → SSE → UI-visible state
- [ ] Restart/state-recovery test (kill app mid-run, restart, verify dashboard reconstructs)
- [ ] Failure-injection: process exit, git repo locked, unreadable directory, malformed adapter data, DB locked
- [ ] Frontend suite complete; `npm run test` green
- [ ] `ruff check` clean; `pip check` clean; `npm audit` reviewed
- [ ] Windows runtime verification (this machine); Linux static verification + documented as unverified
- [ ] Defect fixes with regression tests for each

**Acceptance:** `./test.bat` (and `./test.sh`) green end-to-end; defects found are logged below with their fix commits.

## M10 — Documentation and release — ⬜ not started

Depends on: M9.

- [ ] `README.md`: what it is, capability matrix (verified/unverified/not implemented), install, run, stop, first-run walkthrough, screenshots description
- [ ] `install.bat` / `start.bat` / `stop.bat` / `test.bat` and `.sh` equivalents (no admin rights, PID-file shutdown, prints/opens URL)
- [ ] `.env.example` (placeholders only), `.gitignore` complete
- [ ] DB init/migration commands documented; backup/restore instructions
- [ ] Troubleshooting doc (port in use, DB locked, adapter unavailable, permission errors, stale PID)
- [ ] Integration guide: event schema reference, `agentscope-report` usage, Claude Code hook snippet, OpenCode adapter notes, terminal-capture limitations
- [ ] Restart-recovery smoke test; startup ≤ 5 s to interactive dashboard
- [ ] Git verification: repo healthy, remote URL correct, identity correct, no sensitive files staged, tests pass
- [ ] Final status report: completed features, verification results, known limitations, run instructions

**Acceptance:** a fresh clone + `install` + `start` works from a clean state; documentation matches the actual implementation; repo verification in PROJECT_PLAN §20 passes.

---

## Dependency graph

```
M1 ─► M2 ─┬─► M3 ─┬─► M4 ─┬─► M5 ─┬─► M9 ─► M10
          │       │       │       │
          └─► M6 ─┴─► M7 ◄┘       │
                       │           │
                       └─► M8 ─────┘
```
M6 starts once the OpenAPI contract is frozen (end of M2) and completes against fixtures; M7 joins M6 + M4.

## Deviations & Blockers

| Date | Item | Detail | Approval needed? |
|---|---|---|---|
| — | none yet | — | — |

## Test Status Log

| Milestone | Suite | Result | Notes |
|---|---|---|---|
| M1 | planning docs | n/a | 5 docs written; no code to test. Approved 2026-10-25. |
| M2 | backend suite | running | — |
