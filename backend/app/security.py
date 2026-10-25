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
    except FileNotFoundError:
        existing = ""
    if existing:
        return existing
    token = mint_token()
    paths.token.parent.mkdir(parents=True, exist_ok=True)
    paths.token.write_text(token, encoding="utf-8")
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


def _token_exchange(request: Request) -> Response:
    """Trade ``?t=<token>`` for the session cookie, then redirect the secret away."""
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
    """Store the session token, guard the API router and register the auth routes.

    Creates ``app.state.api_router`` (empty apart from the session probe) so
    later tasks mount their routers onto it — every route added there inherits
    ``require_session``. ``GET /`` (the token exchange) is registered directly
    on the app because it must stay reachable without a cookie.
    """
    app.state.session_token = token
    router = APIRouter(prefix="", dependencies=[Depends(require_session)])
    router.add_api_route("/api/v1/session", _session_status, methods=["GET"])
    app.state.api_router = router
    app.add_api_route("/", _token_exchange, methods=["GET"])
