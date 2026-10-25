"""Local session token authentication.

A single random token is minted at startup, persisted under the application
home with owner-only permissions, and exchanged once via ``GET /?t=<token>``
for an ``HttpOnly`` cookie. Every ``/api/v1/*`` route except ``/api/v1/health``
then requires that cookie, compared in constant time.
"""

from __future__ import annotations

import os
import secrets

from app.config import AppPaths, settings_singleton
from app.errors import Unauthorized
from app.schemas.common import ok
from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

SESSION_COOKIE = "agentscope_session"
SESSION_MAX_AGE = 86400  # 24 hours
UNAUTHORIZED_MESSAGE = "Missing or invalid local session."


def mint_token() -> str:
    """Return a fresh 256-bit URL-safe token."""
    return secrets.token_urlsafe(32)


def load_or_create_token(paths: AppPaths) -> str:
    """Return the persisted token, minting and storing one when absent or empty.

    The file is created with ``0o600`` permissions where the OS supports them;
    on platforms without owner-only permission bits this is best-effort and
    never fails.
    """
    try:
        existing = paths.token.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, UnicodeDecodeError):
        # A corrupt (non-UTF-8) token file is treated the same as a missing one:
        # regenerate rather than crash at startup.
        existing = ""
    if existing:
        return existing
    token = mint_token()
    paths.token.parent.mkdir(parents=True, exist_ok=True)
    # Create through ``os.open`` with ``0o600`` so the secret never exists even
    # briefly with umask-derived permissions; the chmod below also covers a
    # pre-existing file, whose mode ``os.open`` would leave untouched.
    fd = os.open(paths.token, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token)
    try:
        os.chmod(paths.token, 0o600)
    except OSError:  # pragma: no cover - depends on platform capability
        pass
    return token


def _matches(provided: str, expected: str) -> bool:
    """Compare two tokens in constant time; bytes avoid ``compare_digest``
    rejecting non-ASCII input as a 500 instead of a 401."""
    return secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


async def require_session(request: Request) -> str:
    """Return the session token when the request carries the valid session cookie.

    With ``AGENTSCOPE_AUTH_MODE=off`` (development only) the check is skipped
    and the token is returned as-is.
    """
    if settings_singleton().auth_mode == "off":
        return request.app.state.session_token
    expected = request.app.state.session_token
    provided = request.cookies.get(SESSION_COOKIE, "")
    if not _matches(provided, expected):
        raise Unauthorized(UNAUTHORIZED_MESSAGE)
    return expected


def _session_status(request: Request) -> dict:
    """Authenticated probe: confirms the cookie and reports the active auth mode."""
    settings = settings_singleton()
    token = request.app.state.session_token
    return ok(
        {
            "mode": settings.auth_mode,
            "authenticated": True,
            "token_preview": f"{token[:6]}…",
        }
    )


def token_exchange(request: Request) -> Response:
    """Trade ``?t=<token>`` for the session cookie, then redirect the secret away.

    Registered by :func:`app.main.create_app` as part of ``GET /`` (together
    with the frontend fallback), because Task 9 takes ownership of the root
    route once it serves static files.
    """
    provided = request.query_params.get("t", "")
    expected = request.app.state.session_token
    if not _matches(provided, expected):
        return JSONResponse(ok({"status": "ok"}))
    response: Response = RedirectResponse("/", status_code=307)
    response.set_cookie(
        SESSION_COOKIE,
        expected,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="strict",
        path="/",
    )
    return response


def install_auth(app: FastAPI, token: str) -> None:
    """Store the session token and guard the API router.

    Creates ``app.state.api_router`` (empty apart from the session probe) so
    feature tasks mount their routers onto it — every route added there
    inherits ``require_session``. ``GET /`` (the token exchange plus the
    frontend) is registered by ``create_app``, not here, so the root route
    stays a single assembly-level concern.
    """
    app.state.session_token = token
    router = APIRouter(prefix="", dependencies=[Depends(require_session)])
    router.add_api_route("/api/v1/session", _session_status, methods=["GET"])
    app.state.api_router = router
