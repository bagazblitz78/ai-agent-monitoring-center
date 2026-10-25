# AgentScope — Security Plan

> Companion to [PROJECT_PLAN.md](PROJECT_PLAN.md). Status: **Proposed**, pending approval.

## 1. Threat Model

AgentScope is a local service that can read development directories and (if enabled) run allowlisted commands. Assets:

| Asset | Why it matters |
|---|---|
| Source code in monitored projects | may contain secrets, proprietary code |
| Credentials in agent tooling (`auth.json`, `credential` tables, `.env`, keys) | high-value target |
| AgentScope DB and logs | aggregate of everything observed |
| Ability to run commands | code execution on the user's machine |

Adversaries considered:

1. **A malicious or compromised web page** open in the user's browser that tries to call `http://127.0.0.1:8787` (CSRF / DNS rebinding).
2. **A malicious or buggy AI agent** submitting hostile event payloads (huge bodies, forged IDs, path traversal, script injection, prompt content in messages).
3. **Local unprivileged software** on the same machine probing the port.
4. **Oversight** — indexing or logging secrets unintentionally; leaking them into error messages.
5. **Dependency supply chain** — vulnerable or typosquatted packages.

Non-goals: defending against an attacker with the user's own OS privileges; encrypting data at rest (single-user local machine); remote access (disabled by default).

## 2. Control Matrix (maps to spec §12)

| Spec requirement | Control | Where | Verified by |
|---|---|---|---|
| Bind to localhost | `host="127.0.0.1"` default; `AGENTSCOPE_HOST` change requires `AGENTSCOPE_ALLOW_NON_LOCALHOST=1` plus a warning at startup | `config.py` | test: startup refuses non-loopback without flag |
| No external exposure | no cloud calls, no telemetry, no auto-update; outbound requests only from user-initiated Git remote ops (none in v1) | config | code review + test asserting no `requests`/`httpx` client calls in server path |
| Input validation | Pydantic models on every endpoint; `extra="forbid"`; length caps (`message ≤ 4000`, `metadata ≤ 16 KB`, batch ≤ 100) | schemas | malformed-input tests |
| Auth / local session | per-run 256-bit token minted at startup, stored at `<data>/session.token` (mode 0600); delivered once via `/?t=<token>` → set as `HttpOnly; SameSite=Strict; Path=/` cookie; all `/api/v1/*` except `/health` and `/api/v1/events/public`-style probes require it | `security.py` | unauthorized tests (401 without cookie) |
| CSRF | cookie is `SameSite=Strict` **and** every mutating request must carry header `X-AgentScope-Req: 1` (custom header ⇒ cross-origin preflight fails) **and** `Origin` must be in the allowlist | `security.py` | cross-origin POST test → 403 |
| CORS | no CORS headers at all by default (same-origin only); dev mode allows only `http://127.0.0.1:5173` and `http://localhost:5173` | `security.py` | test asserting absent `Access-Control-Allow-Origin` in prod |
| DNS rebinding | `Host` header must be `127.0.0.1[:port]` or `localhost[:port]` | middleware | forged-Host test → 421 |
| Rate limiting | token bucket per client IP: 300 req/min general, 60 req/min for `/api/v1/events`, in-memory, bounded | `security.py` | burst test → 429 |
| Request size limits | middleware rejects `Content-Length > 256 KB` (and reads defensively when the header is absent) | middleware | oversize body test → 413 |
| Path traversal | every user path is `os.path.realpath`-normalized; rejected if it resolves outside an allowed root or contains `..` after normalization; project paths must be absolute and existing; Git diff/file endpoints are constrained to the registered project root | `services/paths.py` | traversal tests (`..%2f`, `C:\Windows\..`, symlink escape) |
| Symlinks / filesystem boundaries | real path must stay inside the registered project path after `realpath`; watchdog events whose real path escapes are dropped | `file_monitor.py` | symlink-escape test |
| Parameterized SQL | SQLAlchemy ORM/`text()` with bound params everywhere; no string-built SQL; identifiers never come from input | whole backend | code review + grep test for f-string SQL |
| Safe subprocess | `shell=False`, argv lists, `cwd` set, timeout, output cap; explicit per-project allowlist for verification commands | `services/verification.py` | test: command from agent event is rejected |
| No auto-execution of agent commands | events may only *reference* a configured check by name | same | negative test |
| Credential redaction | `redaction.py` scrubs `Authorization`, `Bearer …`, `api[_-]?key`, `token`, `password`, `secret`, `ghp_…`, `sk-…`, AWS keys, private-key PEM blocks, and `.env`-style `KEY=value` — applied to logs, cmdline snapshots, error details, and SSE payloads | `services/redaction.py` | redaction tests on representative samples |
| Sensitive file exclusion | default excludes `.env*`, `*.pem`, `*.key`, `id_rsa*`, `*.p12`, `*.pfx`, `credentials*`, `.aws/`, `.ssh/`, `*.keystore`; never read by the file monitor or log indexer; adapter never touches `auth.json` / `credential` table | `file_monitor.py`, adapters | exclusion tests |
| Secure config/secret storage | token file 0600 (POSIX) / ACL-restricted (Windows, inherited user profile); no secrets in DB, no secrets in the repo; `.env.example` placeholders only | `config.py` | test asserting token not logged |
| Dependency checks | `pip check` + `npm audit --audit-level=high` in `test.*`; documented in README | scripts | CI-style step in test script |
| Safe error handling | generic `{"error":{code,message}}`; stack traces only in server logs (redacted), never in responses when `debug=false` | error handlers | test asserting no traceback in 500 body |
| Backup & recovery | `VACUUM INTO` backups, retention 10, `restore` documented and guarded by confirmation; DB integrity check on startup (`PRAGMA quick_check`) | `services/backup.py` | backup/restore test |

## 3. Session & Auth Design (simple by design)

1. Startup generates `secrets.token_urlsafe(32)`; writes it to `<data>/session.token` (created with owner-only permissions).
2. Startup prints and (if enabled) opens `http://127.0.0.1:8787/?t=<token>`; the first request exchanges `t` for the `HttpOnly` cookie and the query string is removed via redirect.
3. Every API request validates: cookie present → constant-time compare against the current token; `Host` allowed; `Origin` allowed when present; `X-AgentScope-Req` present for mutations; rate limit not exceeded.
4. Token rotation: `POST /api/v1/settings/rotate-token` (requires current token) → all sessions invalidated. Stale `session.token` file from a previous run is regenerated on start.
5. Dev mode may set `AGENTSCOPE_AUTH_MODE=off` to skip the token (still loopback-only) — documented as dev-only and never the default. (Corrected 2026-10-26: the setting is `Settings.auth_mode` with the `AGENTSCOPE_` env prefix, so the variable is `AGENTSCOPE_AUTH_MODE`.)

## 4. Untrusted Input Handling

Everything an agent supplies is treated as hostile text:

- Rendered as **text**, never `dangerouslySetInnerHTML`; React escaping by default; any Markdown rendering is disabled for agent messages in v1.
- Length-capped before persisting; overlong messages truncated with a visible `[truncated]` marker (original length recorded).
- `metadata` is size-limited and depth-limited JSON; rejected if it contains more than 3 nested levels or 100 keys.
- Filenames/paths from events are validated against the project root before any use.
- Event types, statuses, and enums are closed sets — unknown values rejected with `400`, not silently coerced.

## 5. Log & Data Retention

| Data | Retention default | Action |
|---|---|---|
| `activity_events` | 90 days | hard delete + `seq` continuity preserved (cursor uses `seq`, not row numbers) |
| `process_snapshots` | 7 days, max 500 per agent | prune oldest |
| `log_entries` | 30 days, max 200 000 rows | prune oldest |
| raw JSONL log files | 10 MB × 5 rotations, 14 days | rotate + delete |
| `file_changes` | 30 days | prune |
| `verification_runs.output_excerpt` | 64 KB each, 90 days | truncate + prune |
| `notifications` | 60 days or read+7 days | prune |
| backups | last 10 | delete older |

Retention job runs daily (and on startup if missed). All values configurable in Settings.

## 6. Backup & Restore

- `POST /api/v1/settings/backup` → `VACUUM INTO backups/agent_scope-<utc>.db`, size logged, retention enforced.
- Restore: stop app → replace `data/agent_scope.db` (WAL files removed) → start (migrations auto-run) → `PRAGMA quick_check` reported in `/health`.
- Documented step-by-step in README "Backup & Restore" with a Windows and a Linux path example.

## 7. Security Verification Checklist (M8/M9)

- [ ] 401 without session cookie on a sensitive endpoint
- [ ] 403 on cross-origin mutating request (Origin `https://evil.example`)
- [ ] 421 on forged `Host` header
- [ ] 413 on oversized body; 429 on rate-limit burst
- [ ] Path traversal: `..%2F`, `C:\Windows\System32`, UNC path, symlink escape all rejected
- [ ] Malformed and oversized event batches rejected with stable error codes; duplicates deduplicated
- [ ] Secrets in a fake `.env`, PEM key, and `Bearer` token never appear in API responses, logs, or SSE
- [ ] No f-string SQL anywhere (`grep` test)
- [ ] Agent-supplied command string cannot trigger execution
- [ ] No traceback in production error responses
- [ ] `pip check` and `npm audit` clean (or documented, accepted exceptions)
- [ ] Startup refuses non-loopback bind without explicit opt-in flag
