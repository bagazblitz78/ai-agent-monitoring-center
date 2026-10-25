# AgentScope — Technical Decisions

> Companion to [PROJECT_PLAN.md](PROJECT_PLAN.md). Each entry records the decision, the alternatives considered, and why. Status: **Proposed** (approved on user approval of the plan). Any decision changed during Build Mode is amended here with a date and reason.

## D-01 — Python version: target 3.11–3.13, verified on 3.13.5

- **Context:** Spec asks for "Python 3.12 or a compatible, supported Python version". Only 3.13.5 is installed on this machine.
- **Decision:** Write for 3.11+ (`X | None` syntax, `asyncio.TaskGroup` avoided in favor of explicit task lists for clearer shutdown), test on 3.13.5.
- **Alternatives:** install 3.12 alongside (extra toolchain, no benefit); target 3.13 only (drops 3.11/3.12 users).
- **Consequence:** `requirements.lock` produced on 3.13.5; Linux/3.12 behavior recorded as unverified until tested.

## D-02 — Environment manager: `venv` + `pip`, not `uv`/`poetry`

- **Context:** `uv` is not installed; spec requires no admin privileges and no exotic tooling.
- **Decision:** `python -m venv .venv` + `pip install -r requirements.txt`, pinned via a generated `requirements.lock` (`pip freeze`).
- **Alternatives:** `uv` (faster, but an extra install step and a bootstrap download); `poetry` (heavier, lockfile format churn).

## D-03 — ORM/migrations: SQLAlchemy 2.0 + Alembic

- **Context:** Spec requires SQLAlchemy, schema migrations, referential integrity.
- **Decision:** declarative 2.0-style models, `create_all` disabled in app runs, Alembic owns schema; initial migration auto-generated then hand-reviewed.
- **Alternatives:** raw `sqlite3` (no migration story); Tortoise/Peewee (spec asks for SQLAlchemy).

## D-04 — Storage location: `platformdirs` user data dir

- **Decision:** Windows `%LOCALAPPDATA%\AgentScope`, Linux `~/.local/share/agentscope`; override via `AGENTSCOPE_HOME`. Contains `data/agent_scope.db`, `logs/`, `spool/`, `backups/`, `agentscope.pid`, `session.token`.
- **Rationale:** spec §1 requires app data separate from monitored projects; per-user paths need no admin rights.
- **Rejected:** storing DB next to the code (breaks when the repo is moved/cloned) or in the project dirs (violates read-only invariant).

## D-05 — Real-time transport: Server-Sent Events

- **Context:** Spec allows WebSocket or SSE.
- **Decision:** SSE for server→client, REST for client→server.
- **Rationale:** one-directional flow is all we need; SSE has native reconnect with `Last-Event-ID` (→ cursor resume), works with plain HTTP/1.1 and the static file middleware, is debuggable with `curl`, and avoids WebSocket lifecycle/backpressure bugs in both FastAPI and the browser.
- **Alternative rejected:** WebSocket — bidirectional capability unused, more code (ping/pong, half-open detection, proxy issues) for no gain.

## D-06 — Database: SQLite (spec-mandated), WAL mode, single writer

- **Decision:** one connection pool; writes serialized through a single persister coroutine; `PRAGMA journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`.
- **Rationale:** eliminates `database is locked` races between monitors and API, keeps write ordering deterministic for SSE cursors.

## D-07 — Progress is computed, never stored

- **Decision:** `milestone_progress`/`project_progress` are pure functions evaluated on read; only raw task states and weights are persisted.
- **Rationale:** policy edits apply retroactively; no drift between stored and actual progress; auditable.
- **Rejected:** caching progress in a column (needs invalidation on every state change — a classic source of wrong numbers).

## D-08 — Reported vs. verified completion are separate states

- **Decision:** `tasks.verification_status ∈ {unverified, reported, verified, failed}` independent of `tasks.status`; the completion policy decides whether `reported` counts.
- **Rationale:** spec §7 explicitly requires that an agent's claim of completion is not treated as proof.
- **Default policy:** `require_verification=false`, `allow_reported=true` — but the UI always labels the result `(reported)` until verified.

## D-09 — Idempotency by `event_uid`

- **Decision:** every ingested event carries or derives a unique uid (`event_id` if supplied, else SHA-256 of `agent_id|type|task_id|ts|message`); `activity_events.event_uid` is UNIQUE; duplicates return `200` with `{"deduplicated": true}` and insert nothing.
- **Rationale:** adapter retries and JSONL re-reads are then harmless; milestones/tasks upsert on `(project_id, external_key)`.

## D-10 — Adapter strategy: read-only first, explicit reporting preferred

| Adapter | Reads | Writes | Status |
|---|---|---|---|
| `event_api` (Option B) | HTTP POST from agent/helper | inserts via validated events | v1 core |
| `jsonl` (Option B) | spool file in AgentScope data dir | append-only by helper, read by hub | v1 core |
| `opencode` (Option C) | `opencode.db` via `file:...?mode=ro` URI, 5 s poll, thread offloaded | **none** | v1, verified on this machine |
| `claude_hooks` (Option C) | HTTP hook posts → same event API | none | v1 ships helper + docs, **unverified** (agent absent) |
| `generic_process` (Option A) | psutil | none; statuses marked `observed`/`estimated` | v1 core |
| Copilot CLI / Codex CLI | — | — | **not in v1** (nothing verifiable) |

- **Rejected:** writing into a monitored project by default (violates read-only invariant); injecting into terminals or reading process memory (forbidden by spec §6 and unsafe).
- **OpenCode DB safety:** open read-only, never touch `credential`/`auth.json`, tolerate schema drift by checking `PRAGMA table_info` before selecting, and mark the adapter `degraded` rather than crashing if columns are missing.

## D-11 — In-project JSONL reporting is opt-in (default off)

- **Decision:** default spool = `<data>/spool/<project_id>.jsonl` read by AgentScope. Writing `.agentscope/report.jsonl` inside a monitored project requires an explicit per-project setting, shown as a warning in the UI.
- **Rationale:** satisfies "do not modify monitored project files" by default while still supporting agents that can only write locally.

## D-12 — Terminal output capture: wrapper + log tail only

- **Decision:** `agentscope run --project <id> -- <command>` (spawns the child with piped stdout/stderr, tees to a rotated file, records PID) and tailing explicitly configured log files.
- **Declared limitation:** output of *already-running* sessions is not capturable without injection, which we do not do; full-screen TUI programs do not emit linear output. The UI states this instead of pretending to full visibility.

## D-13 — Test execution: explicit allowlist, never agent-supplied commands

- **Decision:** `/api/v1/verification` runs only commands listed in the project's `allowed_checks` config (argv array, `shell=False`, cwd = project path, timeout, output capped). Agent events may reference a check by name but can never supply a command string.
- **Rationale:** spec §12/§9 forbid executing arbitrary commands received from an agent.

## D-14 — Frontend: hand-built shadcn-style system, no dashboard template

- **Decision:** Vite + React 19 + TS + Tailwind v4, Radix primitives with locally authored components (no template generator runtime), Recharts, Lucide, zustand for UI state, Vitest + RTL for tests.
- **Rationale:** spec forbids generic AI dashboard templates and requires a consistent component system; local primitives avoid vendor lock-in while keeping the shadcn conventions.

## D-15 — API contract-first

- **Decision:** Pydantic schemas + OpenAPI define the contract; frontend `src/api/types.ts` is derived from the generated OpenAPI types during M2 and frozen before M6 starts, so M6 can run in parallel with M4/M5.

## D-16 — Log strategy: rotated files for volume, DB rows for search

- **Decision:** raw/verbose output → `logs/agentscope.jsonl` with `RotatingFileHandler`-equivalent (10 MB × 5 files); searchable metadata → `log_entries` with retention (default 30 days / 200 000 rows). Large outputs are never stored in the DB.

## D-17 — Dependency pinning

- **Decision:** `requirements.txt` = compatible ranges for readability; `requirements.lock` = exact `pip freeze` output used by `install.*`; frontend uses `package-lock.json`. Both committed.
- **Note:** proposed versions in PROJECT_PLAN §4 are resolved for real during M2 and any Python-3.13 incompatibility is corrected and recorded here.

## D-18 — Git workflow

- **Decision (Build Mode, after approval):** `git init` on `main` in the project root; repository-local `user.name=bagazblitz78`, `user.email=bagasiqbal577@gmail.com` (global config already matches but is set locally regardless); `origin` = `https://github.com/bagazblitz78/ai-agent-monitoring-center.git` (verified empty); `.gitignore` written **before** the first commit; Conventional Commits; staged-content review before each commit; **no push without explicit user authorization**; no force-push, no history rewrite, no remote branch deletion.
- **Never committed:** `.env`, `*.db`, `logs/`, `spool/`, `.venv/`, `node_modules/`, `frontend/dist/`, `session.token`, `*.pem`, `backups/`.

## D-19 — Platform support

- **Decision:** all path/process/fs code goes through `pathlib`, `platformdirs`, `watchdog`, `psutil`, and `subprocess` with argument lists (never `shell=True`). PID-file shutdown uses `taskkill` on Windows and `SIGTERM` on POSIX. Drive-letter paths (`D:\...`) are first-class (`path_key` normalization handles case-insensitivity on Windows, case-sensitivity on Linux).
- **Honest status:** Windows verified at build time; Linux statically reviewed only in this workspace.

## D-20 — Out of scope for v1 (explicitly)

Packaged desktop shell (Tauri/pystray installer), remote/cloud access, mobile layout, multi-user auth, auto-execution of agent-suggested commands, Copilot/Codex adapters, desktop notification polish beyond the opt-in basic version, plugin marketplace.
