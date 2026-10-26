# Security — AgentScope

How this project is built, what is protected today, what is still on the
roadmap, and how to report a problem.

- **Supported version:** `0.1.x` on `main`.
- **In-repo security design (target state):**
  [docs/SECURITY_PLAN.md](docs/SECURITY_PLAN.md) — threat model + full control matrix.
- **This file** describes what is *actually implemented and tested right now*.
  Anything listed as *planned* ships with milestone **M8 — Security and
  reliability** (see [docs/MILESTONES.md](docs/MILESTONES.md)).

---

## 1. Threat model (summary)

AgentScope is a **single-user, local service** that reads development
directories and observes running processes. Assets:

| Asset | Why it matters |
|---|---|
| Source code in monitored projects | may contain secrets and proprietary code |
| Credentials in agent tooling (`.env`, keys, `auth.json`) | high-value target |
| AgentScope's own DB and logs | an aggregate of everything observed |
| The machine itself | AgentScope must never become a way to run code |

Adversaries considered:

1. a malicious or buggy web page in the user's browser calling `127.0.0.1:8787`
   (CSRF, DNS rebinding);
2. a malicious or compromised AI agent submitting hostile payloads (oversized
   bodies, forged IDs, path traversal, injected markup);
3. local unprivileged software probing the port;
4. accidental disclosure — indexing secrets, leaking them into logs or errors;
5. dependency supply-chain issues.

**Non-goals:** defending against an attacker who already has the user's OS
privileges; encrypting data at rest (single-user local machine); remote access
(disabled by default).

---

## 2. Implemented and tested controls

| Control | Where | Verified by |
|---|---|---|
| **Loopback-only by default.** `host=127.0.0.1`; a non-loopback host requires `AGENTSCOPE_ALLOW_NON_LOCALHOST=1` and otherwise raises at bind time. | `backend/app/config.py` | `tests/test_config.py` |
| **Local session token.** A 256-bit `secrets.token_urlsafe(32)` token is minted at startup, persisted at `<AGENTSCOPE_HOME>/session.token` created with `os.open(..., 0o600)` + `chmod`, and never logged. A corrupt/empty token file is regenerated instead of crashing startup. | `backend/app/security.py` | `tests/test_security.py` |
| **Cookie exchange.** `GET /?t=<token>` trades the token for an `HttpOnly; SameSite=Strict; Path=/` cookie (24 h) and redirects the secret out of the URL. Comparison is `secrets.compare_digest` (constant time, UTF-8 encoded so hostile input yields 401, not 500). | `backend/app/security.py` | `tests/test_security.py` |
| **Every API route is guarded.** Routes are mounted on `app.state.api_router`, which carries `Depends(require_session)`; two sweep tests assert that **every** `/api/v1/*` route requires the session and that `/api/v1/health` is the *only* exemption — including routes added later through `include_router`. | `backend/app/security.py`, `backend/app/main.py` | `tests/test_security.py::test_every_api_route_requires_session`, `::test_health_is_the_only_unguarded_api_route` |
| **`AGENTSCOPE_AUTH_MODE=off`** skips the check — development only, never the default, and still loopback-only. | `backend/app/security.py` | `tests/test_security.py` |
| **Safe error handling.** Every failure renders `{"error":{code,message,details,request_id}}`; tracebacks go to the server log only and appear in a response **only** when `AGENTSCOPE_DEBUG=true`. 500 bodies are generic by default. | `backend/app/errors.py` | `tests/test_errors.py::test_500_hides_traceback` |
| **Request correlation.** Every request gets an `X-Request-Id` (uuid4 hex), echoed on responses — so incidents can be traced without exposing internals. | `backend/app/middleware.py` | `tests/test_errors.py` |
| **Strict input validation.** Pydantic models with `extra="forbid"` and length caps on every body; pagination rejects `page_size > 200` with 422; unknown fields are errors, not silently dropped. | `backend/app/schemas/` | `tests/test_projects_api.py`, `tests/test_file_changes_api.py` |
| **Path traversal defence.** Project paths must be absolute, are `os.path.realpath`-normalised, and are rejected when they escape an allowed root or resolve through symlinks; registered paths are re-validated on every mutation. | `backend/app/services/paths.py` | `tests/test_paths_service.py` (incl. `..%2f`, `C:\Windows\..`, symlink cases) |
| **Filesystem boundary enforcement.** Watchdog events whose real path leaves the project root are dropped; a project that becomes unreadable surfaces as `permission_denied` in the API instead of crashing the server. | `backend/app/engine/file_monitor.py` | `tests/test_file_monitor.py` (symlink-escape), `tests/test_m3_acceptance.py` (permission-denied) |
| **Read-only monitoring.** Monitors never write into a monitored project: Git runs with `GIT_OPTIONAL_LOCKS=0` so even `git status` cannot rewrite `.git/index`, and the acceptance test hashes every file in three registered directories before and after a full monitoring run and requires byte-for-byte equality. | `backend/app/engine/*`, `backend/app/services/git_info.py` | `tests/test_git_monitor.py::test_tick_never_touches_the_repository`, `tests/test_m3_acceptance.py` |
| **Secrets excluded from observation.** The default file-monitor exclude list drops `.env*`, `*.pem`, `id_rsa*`, `*.key`, lock files and build/virtualenv trees before any event is recorded. | `backend/app/engine/file_monitor.py` | `tests/test_file_monitor.py::test_excluded_files_are_not_recorded` |
| **Credential-shaped data redacted at rest.** Git author e-mails are stored as `author_email_redacted` (`t***@example.com`); process command lines are scrubbed before the snapshot row is written — values of secret-named flags (`--token`, `--password`, `--key`, `--secret`, `--api-key`, `--credential`, `--private-key`, …) and any standalone opaque string of 20+ characters become `***`, while paths and short arguments stay readable. | `backend/app/services/redact.py` | `tests/test_process_monitor.py`, `tests/test_git_service.py` (redaction section) |
| **No outbound network.** The server makes no external requests: monitoring is local (filesystem, `psutil`, `GitPython`), storage is local SQLite, and there is no telemetry, cloud call or auto-update. | whole backend | code review; `httpx` is a test/TestClient dependency only |
| **No secrets in the repo or the DB.** Only `.env`-style placeholders are ever committed; the session token lives under `AGENTSCOPE_HOME`, outside version control. | `.gitignore`, `config.py` | repository contents |

---

## 3. Planned controls (M8 — not implemented yet)

`docs/SECURITY_PLAN.md` specifies more than is currently enforced. The
following are **designed and specified but not yet running in the server** —
do not assume they protect you today:

- **Rate limiting** (token bucket per client IP; the `429 rate_limited` error
  code exists, the limiter does not).
- **Request body size cap** (the `413 payload_too_large` code exists, the
  middleware does not).
- **CSRF hardening** — mandatory `X-AgentScope-Req` header on mutations plus an
  `Origin` allowlist (today's mitigation is `SameSite=Strict` + the absence of
  any CORS headers).
- **Host-header validation** (DNS-rebinding defence → `421`).
- **Token rotation endpoint** (`POST /api/v1/settings/rotate-token`).
- **Full redaction** of stored payloads, logs and SSE messages
  (`services/redact.py` currently covers e-mails and command lines only).
- **Backups and retention** — `VACUUM INTO` snapshots, event/log pruning,
  startup integrity check.
- **Dependency auditing** in the test script (`pip check`, `npm audit`).
- **Safe subprocess execution** for verification checks (allow-listed commands,
  `shell=False`, timeouts) — lands with M5/M8; nothing in the current code
  executes agent-supplied commands.

Also pending: `assert_bind_allowed` is implemented and unit-tested but is not
yet called from the startup path — the start scripts that will do that arrive
with M10. Until then, always start uvicorn with `--host 127.0.0.1`.

---

## 4. Using AgentScope safely

- **Keep it on loopback.** Do not set `AGENTSCOPE_ALLOW_NON_LOCALHOST=1` unless
  you understand that the API (and your project metadata) becomes reachable
  from the network.
- **Treat the token URL as a password.** `http://…:8787/?t=<token>` grants a
  24-hour session; don't paste it into chats, logs or issue reports. Delete
  `session.token` to force a fresh token on next start.
- **Never run with `AGENTSCOPE_AUTH_MODE=off`** on a machine you share or on a
  port that isn't loopback.
- **Register only directories you are willing to have observed.** AgentScope
  records file-change metadata (path, size, timestamp) and Git commit metadata —
  not file contents — but that is still information about your work.
- **Process snapshots** contain executable path, state and a redacted command
  line of matching agents. Assume they are sensitive; the retention/pruning
  worker that would auto-expire them is not implemented yet, so data
  accumulates until M8.
- **Run it as your own user.** AgentScope has no privilege separation; it can
  read whatever the account that started it can read.
- **Keep dependencies current** (`pip install -r requirements.txt` uses pinned
  versions; review the lock file when updating).

---

## 5. Reporting a vulnerability

1. Prefer **private reporting**: open a
   [GitHub security advisory](https://github.com/bagazblitz78/ai-agent-monitoring-center/security/advisories/new)
   on this repository so the report stays confidential until a fix ships.
2. If private reporting is unavailable, contact the repository owner directly
   — **please do not file a public issue for an exploitable bug**.
3. Include: version/commit, affected endpoint or component, reproduction steps,
   and impact. Minimal proof-of-concept code is welcome; please avoid accessing
   data that isn't yours while testing.

What to expect: acknowledgement as soon as I can, a fix or mitigation plan
before any public disclosure, and credit in the release notes if you want it.
Reports that only restate an already-documented limitation from section 3 are
still appreciated but will be closed as "known / planned in M8".

---

## 6. Security guarantees the tests enforce

These are the invariants that make the rest of the design trustworthy — each one
has a test that fails if it regresses:

1. no `/api/v1/*` route other than `/api/v1/health` can be reached without a
   valid session;
2. no 500 response leaks a stack trace while `debug=false`;
3. no user-supplied path can point outside the registered project root;
4. no monitor writes, deletes or mutates a single byte inside a monitored
   project — asserted by hashing every file before and after monitoring;
5. no symlink inside a project can make the watcher observe files outside it.
