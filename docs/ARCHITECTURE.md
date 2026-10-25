# AgentScope — Architecture

> Companion to [PROJECT_PLAN.md](PROJECT_PLAN.md). Plan Mode document — no code written yet.

## 1. System Overview

```
                       ┌────────────────────────────────────────────────────┐
                       │  AgentScope process (one venv, one port)           │
                       │  uvicorn ─ FastAPI ─ 127.0.0.1:8787                │
                       │                                                    │
  monitored projects ──┼─► Monitoring engine (asyncio)                      │
   (read-only)         │     ProcessMonitor   (psutil, 3–5 s poll)          │
                       │     FileMonitor      (watchdog, debounced)         │
                       │     GitMonitor       (poll, per repo)              │
                       │     LogMonitor       (rotating file tail)          │
                       │     AdapterHub       (OpenCode DB / JSONL / HTTP)  │
                       │            │                                       │
                       │            ▼                                       │
                       │      EventBus  (bounded asyncio queue)            │
                       │       ├─► persister ─► SQLite (single writer)      │
                       │       └─► broadcaster ─► SSE hub ─► browsers       │
                       │                                                    │
                       │      NotificationEngine (rules, dedupe)            │
                       │      ProgressEngine (pure functions, on read)      │
                       │      RetentionWorker (rotate, prune, backup)       │
                       └────────────────────────────────────────────────────┘
                                     ▲                    │
                     REST /api/v1/*  │                    │ SSE /api/v1/stream
                                     │                    ▼
                       ┌────────────────────────────────────────────────────┐
                       │ React SPA (built to frontend/dist, served by FastAPI)│
                       └────────────────────────────────────────────────────┘
```

**Invariants**

1. AgentScope **never writes** into a monitored project directory unless the user explicitly opts in (JSONL in-project reporting is opt-in and off by default).
2. All application data lives in one per-user data directory (`platformdirs`).
3. Exactly one coroutine writes to SQLite; everything else enqueues.
4. Every externally observable number is derived from persisted rows — no fabricated progress.
5. Unknown values render as `unknown`, never as an inferred default.

## 2. Process & Deployment Modes

| Mode | Frontend | Backend | Use |
|---|---|---|---|
| `dev` | Vite dev server on `5173`, `/api` proxied to `8787` | `uvicorn --reload` | development |
| `local` (production-like) | static files from `frontend/dist` | single uvicorn, no reload | daily use |
| `test` | — | `fastapi.testclient` (in-process) | automated tests |

Startup: `start.bat` / `start.sh` → activate venv → run migrations → launch uvicorn → write `agentscope.pid` → open `http://127.0.0.1:8787/?t=<token>` (or print the URL). `stop.bat` / `stop.sh` read the PID file and send a graceful shutdown (Windows: `taskkill /PID`, Linux: `SIGTERM`), with a forced kill fallback.

## 3. Repository Layout

```
ai_agent_monitor_center/
├── backend/
│   ├── app/
│   │   ├── main.py               # app factory, middleware, router mount, static
│   │   ├── config.py             # pydantic-settings, paths, tunables
│   │   ├── db.py                 # engine, session, single-writer helper
│   │   ├── models/               # SQLAlchemy ORM (one file per aggregate)
│   │   ├── schemas/              # Pydantic request/response/event schemas
│   │   ├── api/                  # routers: projects, agents, tasks, milestones,
│   │   │                         #   events, activity, logs, verification,
│   │   │                         #   notifications, settings, health, stream
│   │   ├── engine/
│   │   │   ├── bus.py            # EventBus: bounded queue, dedupe, fan-out
│   │   │   ├── process_monitor.py
│   │   │   ├── file_monitor.py
│   │   │   ├── git_monitor.py
│   │   │   ├── log_monitor.py
│   │   │   └── retention.py
│   │   ├── adapters/
│   │   │   ├── base.py           # Adapter protocol + capability flags
│   │   │   ├── opencode_db.py    # read-only opencode.db polling
│   │   │   ├── jsonl_spool.py    # spool/*.jsonl reader
│   │   │   └── claude_hooks.py   # helper-side normalization (server accepts)
│   │   ├── services/
│   │   │   ├── progress.py       # pure progress functions
│   │   │   ├── notifications.py  # rule evaluation
│   │   │   ├── git.py            # GitPython wrappers
│   │   │   ├── redaction.py      # secret/token scrubbing
│   │   │   └── backup.py
│   │   └── security.py           # token, origin, rate limit, size caps
│   ├── alembic/                  # migrations
│   └── tests/
├── frontend/
│   ├── src/
│   │   ├── main.tsx  App.tsx  router.tsx
│   │   ├── api/                  # typed client generated from OpenAPI types
│   │   ├── hooks/                # useSSE, usePolling, usePreferences
│   │   ├── stores/               # zustand: session, filters, theme
│   │   ├── components/ui/        # shadcn-style primitives
│   │   └── features/             # dashboard, projects, tasks, milestones,
│   │                             #   agents, activity, notifications, settings
│   └── vitest.config.ts
├── scripts/                       # agentscope-report (event helper), hooks template
├── docs/
├── requirements.txt  requirements-dev.txt  requirements.lock
├── install.bat start.bat stop.bat test.bat
├── install.sh  start.sh  stop.sh  test.sh
├── .env.example  .gitignore  README.md
```

## 4. Runtime Data Flow

1. A component observes something (process change, fs event, adapter row, API POST).
2. It builds a normalized `ActivityEvent` (schema version 1) and calls `bus.emit(event)`.
3. `bus.emit` is non-blocking; on a full queue it applies **coalescing** (high-frequency fs/process events collapse by key) and only then drops with a `event.dropped` counter exposed on `/api/v1/health`.
4. The single persister inserts with `event_uid` uniqueness → duplicates become no-ops.
5. After a successful commit, the event is broadcast to all SSE subscribers.
6. Notification rules are evaluated on the persisted event; matching rules insert a `notifications` row (deduped by `dedupe_key` within a cooldown window).

**Backpressure:** queue bounded at 1000 items; SSE per-client outbound buffer bounded at 200 messages with `slow_consumer` disconnect; no busy loops — all timers are `asyncio.sleep` with jitter, watchdog is callback-driven, Git polling is per-project with a 5 s cadence.

## 5. Database Schema (SQLite)

Conventions: `id` = UUIDv4 text PK; all timestamps = UTC ISO-8601 text (`2026-10-25T09:30:00.000Z`); booleans = 0/1 integers; JSON columns are TEXT validated at the Pydantic boundary; every child table has `created_at`/`updated_at`.

```
projects(
  id PK, name, path UNIQUE, path_key UNIQUE,   -- path_key = normalized case-folded path
  is_git INT, default_branch, current_branch,
  watch_enabled INT, watch_rules_json,         -- include/exclude globs
  in_project_reporting INT DEFAULT 0,
  status, created_at, updated_at, archived_at NULL)

agents(
  id PK, project_id FK→projects ON DELETE CASCADE, name,
  adapter TEXT,              -- 'generic_process' | 'event_api' | 'jsonl' | 'opencode' | 'claude_hooks'
  provider TEXT,             -- 'opencode' | 'claude_code' | 'copilot' | 'codex' | 'other'
  match_kind TEXT,           -- 'exe' | 'cmdline_regex' | 'pid' | 'none'
  match_value TEXT NULL, confirmed_pid INT NULL,
  status TEXT,               -- running|working|idle|waiting|blocked|completed|failed|stopped|unknown
  status_source TEXT,        -- 'observed' | 'reported' | 'estimated' | 'unknown'
  last_activity_at, last_event_message, last_error,
  created_at, updated_at)

agent_sessions(
  id PK, agent_id FK, project_id FK, external_key TEXT NULL,  -- adapter session id
  status, started_at, ended_at, last_heartbeat_at,
  heartbeat_interval_s INT, source TEXT, metadata_json,
  UNIQUE(agent_id, external_key))

milestones(
  id PK, project_id FK, title, description, position INT, weight REAL DEFAULT 1.0,
  status TEXT,              -- backlog|planned|in_progress|completed|blocked|failed|cancelled
  completion_criteria TEXT NULL,
  started_at, completed_at, created_at, updated_at)

milestone_dependencies(milestone_id FK, depends_on_id FK, PRIMARY KEY(milestone_id, depends_on_id))

tasks(
  id PK, project_id FK, milestone_id FK NULL, parent_id FK→tasks NULL, agent_id FK NULL,
  external_key TEXT NULL,   -- adapter-supplied id → idempotent upsert
  title, description, priority TEXT,   -- low|medium|high|critical
  status TEXT,              -- backlog|planned|in_progress|waiting|blocked|in_review|
                             -- completed|failed|cancelled
  weight REAL DEFAULT 1.0, progress_pct INT DEFAULT 0,
  verification_status TEXT, -- unverified|reported|verified|failed
  criteria_json TEXT NULL, evidence_json TEXT NULL,
  blocker TEXT NULL, error_details TEXT NULL,
  due_at, started_at, completed_at, reported_completed_at, verified_at,
  created_at, updated_at,
  UNIQUE(project_id, external_key))

task_dependencies(task_id FK, depends_on_id FK, PRIMARY KEY(task_id, depends_on_id))

activity_events(
  seq INTEGER PK AUTOINCREMENT,   -- monotonic cursor for SSE resume
  event_uid UNIQUE,               -- idempotency key (client-supplied or hash)
  schema_version INT, ts, type TEXT, source TEXT,  -- agent_report|os_observed|git|user|verification|system
  severity TEXT, message TEXT, payload_json TEXT,
  project_id, agent_id, session_id, task_id, milestone_id  -- all NULLable FKs
)
  INDEX(ts), INDEX(project_id, ts), INDEX(agent_id, ts), INDEX(type, ts)

process_snapshots(
  id PK, ts, agent_id, pid, ppid, exe, cmdline_redacted,
  state TEXT, cpu_pct REAL NULL, rss_bytes INT NULL, create_time REAL NULL)
  INDEX(agent_id, ts)      -- pruned by retention (keep latest N per agent + 7 days)

file_changes(
  id PK, ts, project_id, path_relative, action TEXT,   -- created|modified|deleted|renamed
  size_bytes, git_status TEXT NULL, source TEXT)       -- watchdog|git_poll
  INDEX(project_id, ts)

git_commits(
  id PK, project_id, sha, short_sha, author_name, author_email_redacted,
  committed_at, subject, files_json, source TEXT, UNIQUE(project_id, sha))

log_entries(
  id PK, ts, level, source, project_id NULL, agent_id NULL, task_id NULL,
  message, detail, stream TEXT, file_ref TEXT)         -- file_ref → rotated JSONL file
  INDEX(ts), INDEX(project_id, ts), INDEX(level, ts)   -- pruned by retention

verification_runs(
  id PK, project_id, task_id NULL, milestone_id NULL,
  check_name, command TEXT, argv_json TEXT, exit_code INT NULL,
  status TEXT,               -- passed|failed|error|skipped
  duration_ms INT, summary, output_excerpt, started_at, finished_at, source TEXT)

notifications(
  id PK, created_at, rule_key, severity, title, body,
  project_id NULL, agent_id NULL, task_id NULL,
  dedupe_key TEXT NULL, read_at NULL, INDEX(created_at))

settings(key PK, value_json, updated_at)
```

**Migrations:** Alembic, one revision per schema change, `alembic upgrade head` runs at every startup. **Integrity:** `PRAGMA foreign_keys=ON`, `journal_mode=WAL`, `busy_timeout=5000`, explicit transactions around multi-row writes. **Backup:** nightly-copy via SQLite `VACUUM INTO` to `backups/`, keep last 10; documented restore procedure.

## 6. Progress Engine

Pure, unit-tested functions in `services/progress.py`; never persisted, always recomputed (cheap) so policy changes apply retroactively.

```
task_score(task, policy):
  cancelled            → excluded from denominator
  failed               → 0.0, counted in denominator, flagged
  blocked / waiting    → min(progress_pct, 99) / 100   (blocked never reaches 1.0)
  completed            → 1.0 only if satisfied(task, policy) else 0.0
  in_review            → min(progress_pct,99)/100
  else                 → progress_pct / 100

satisfied(task, policy):
  policy = { require_verification: bool, allow_reported: bool }
  verification_status == 'verified'                        → True
  verification_status == 'failed'                          → False
  criteria_json empty and allow_reported                   → True  (labelled "reported")
  otherwise                                                → False (labelled "awaiting verification")

milestone_progress(m) = Σ(weight_i · task_score_i) / Σ(weight_i)     over tasks in m
project_progress(p)   = Σ(weight_m · milestone_progress_m) / Σ(weight_m)
                        tasks without a milestone form an "unscheduled" bucket
                        with a configurable weight (default 0 → shown separately)
```

- Zero-denominator → progress is `null` (displayed as `unknown`), never `0%` or `100%`.
- Each response ships `calculation` metadata: policy used, denominators, per-child scores → the UI can show *why* a number is what it is.
- `verification_status` and `status` are always shown separately: **reported completion ≠ verified completion.**

## 7. Monitoring Engine Details

| Component | Trigger | Output | Guardrails |
|---|---|---|---|
| ProcessMonitor | `asyncio` loop, 3–5 s | process_snapshots, agent status transitions | only inspects PIDs whose exe/cmdline matches a **registered** agent matcher; skips own PID tree; psutil access-denied → `unknown` |
| FileMonitor | watchdog callbacks | file_changes, activity_events | only registered roots; default excludes: `node_modules/`, `.venv/`, `venv/`, `__pycache__/`, `.git/objects`, `dist/`, `build/`, `target/`, `.next/`, `*.lock`, `.env*`, `*.pem`, `id_rsa*`, `*.key`, files > 2 MB; 500 ms debounce per path; 10 events/path/sec cap |
| GitMonitor | 5 s poll per repo | git_commits, current_branch, status counts | `GitPython` read-only; repo lock/permission errors → status `unavailable`, never a crash |
| LogMonitor | file tail | log_entries | reads only user-configured paths; 64 KB max per line; redaction applied before store |
| AdapterHub | per-adapter cadence | normalized events | read-only; defensive column checks; adapter failure → capability flag `degraded`, surfaced in UI |

## 8. API Surface (v1)

Consistent envelope: `{"data": ..., "meta": {...}}` / errors `{"error": {"code", "message", "details", "request_id"}}`. All list endpoints support `page`, `page_size` (≤200), `sort`, and resource-specific filters. OpenAPI at `/docs` in dev.

```
GET/POST/PATCH/DELETE /api/v1/projects[/{id}]
POST /api/v1/projects/{id}/inspect        # path validation, git detection, status
POST /api/v1/projects/{id}/refresh        # force git/file rescan
GET/POST/PATCH        /api/v1/agents[/{id}]
POST /api/v1/agents/{id}/confirm-pid      # user confirms ambiguous process association
GET/POST/PATCH        /api/v1/sessions[/{id}]
GET/POST/PATCH/DELETE /api/v1/tasks[/{id}]
POST /api/v1/tasks/{id}/dependencies
GET/POST/PATCH/DELETE /api/v1/milestones[/{id}]
GET  /api/v1/milestones/{id}/progress     # full calculation breakdown
POST /api/v1/events                       # batch ingest (1..100 events), idempotent
POST /api/v1/agents/{id}/heartbeat
GET  /api/v1/activity?project=&agent=&source=&since_seq=
GET  /api/v1/logs?level=&source=&q=&from=&to=
GET  /api/v1/git/{project_id}/diff?path=  # only on explicit user request
POST /api/v1/verification                 # run an allowlisted check
GET  /api/v1/verification/{id}
GET/POST /api/v1/notifications[/{id}/read]
GET/PATCH /api/v1/settings
GET  /api/v1/health                       # liveness + engine stats + queue depth
GET  /api/v1/stream?since_seq=            # SSE, resumable via Last-Event-ID
```

**Event schema (v1)** — `event_id`, `schema_version=1`, `agent_id`, `project_id`, `task_id?`, `milestone_id?`, `event_type`, `timestamp`, `message?`, `metadata?`, `session_id?`. Event types: `agent.started`, `agent.heartbeat`, `agent.waiting`, `agent.stopped`, `task.created`, `task.started`, `task.progress`, `task.completed`, `task.failed`, `milestone.started`, `milestone.completed`, `blocker.created`, `blocker.resolved`, `approval.required`, `approval.granted`. Unknown types → `400` with a machine-readable code; `schema_version > 1` → `426`.

## 9. Frontend Structure

**Routes:** `/` Dashboard · `/projects` · `/projects/:id` (tabs: Overview, Board, Milestones, Activity, Files, Logs, Verification) · `/agents` · `/tasks` · `/milestones` · `/activity` · `/notifications` · `/settings`.

**Design system:** charcoal `#17181C` / warm gray neutrals / white surfaces in light mode; single restrained accent (amber-neutral for attention, green/amber/red reserved strictly for status semantics); 1px borders, no gradients, no purple-blue AI palette; Inter/system font stack; 4px spacing scale; tables with sticky headers; every status badge carries text **and** color (WCAG AA contrast) plus a tooltip explaining the source of the value (`observed` / `reported` / `estimated` / `unknown`).

**Key components:** `StatCard`, `AgentCard`/`AgentTable`, `StatusBadge`, `ProgressMeter` (shows calculation breakdown on hover), `KanbanBoard` (dnd, configurable columns, filters), `MilestoneTimeline`, `ActivityFeed` (source-labelled), `FileChangeList`, `LogViewer` (virtualized, filter, search), `VerificationPanel`, `NotificationCenter`, `CommandPalette` (global search, `Ctrl/Cmd-K`), `ConfirmDialog`, `EmptyState`, `ErrorState`, `SkeletonLoader`.

**State:** server state via a thin fetch layer + SSE invalidation (no heavyweight cache lib); zustand for UI prefs (theme, filters, column config, density) persisted to `localStorage` and synced to `/api/v1/settings`.

**UX rules:** every list has empty/loading/error states; consequential actions require confirmation; keyboard reachable (`tabIndex`, focus ring); tooltips on all technical fields; responsive down to 1024 px (mobile is out of scope for v1, stated in README).

## 10. Real-Time Design

SSE (decision D-05): unidirectional server→client is all that is needed, commands go over REST. Advantages over WebSocket here: automatic browser reconnection, no upgrade/proxy handling, trivially debuggable with `curl`, works through the static-file middleware.

- `GET /api/v1/stream` sends `id: <seq>` events; the browser reconnects with `Last-Event-ID`, and the server replays from `activity_events.seq > since` (bounded to the last 500 rows, older → `resync` event telling the client to refetch).
- Event classes broadcast live: agent status change, task/milestone change, blocker, file change (debounced 1 s), git commit, verification result, notification, process exit.
- High-volume noise (per-file events, heartbeats) is **coalesced** into a 1 s digest per project.

## 11. Notification Rules (default set, all configurable)

`agent.exited_unexpectedly`, `task.failed`, `milestone.blocked`, `approval.requested`, `verification.failed`, `heartbeat.missing` (→ status becomes `stale`/`unresponsive`, **never** `failed`), `task.stale` (no change > configurable hours), `milestone.reached`. Each rule: enabled flag, severity, cooldown, scope filter. Delivery: in-app first; desktop notifications behind an opt-in setting.

## 12. Extension Points

`Adapter` protocol: `id`, `capabilities()`, `start(ctx)`, `stop()`, `health()`. Adapters are registered in a factory dict — adding one requires no changes elsewhere. Third-party/agent-specific adapters ship in their own module so they can be updated without touching the core.
