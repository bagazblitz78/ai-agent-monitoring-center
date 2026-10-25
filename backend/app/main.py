"""FastAPI application factory.

``create_app`` wires the request context middleware, the error handlers, the
local session token auth, the guarded ``app.state.api_router`` (mounted
exactly once), the public health route and the frontend: ``frontend/dist``
when it has been built (with an SPA HTML fallback for client-side routes), or
a helpful JSON hint when it has not. ``GET /`` combines the ``?t=`` cookie
exchange with that frontend fallback.
"""

from __future__ import annotations

from pathlib import Path

from app.api.health import router as health_router
from app.api.settings import install_settings
from app.api.stream import install_stream
from app.config import resolve_paths, settings_singleton
from app.errors import install_error_handlers
from app.middleware import RequestContextMiddleware
from app.schemas.common import ok
from app.security import install_auth, load_or_create_token, token_exchange
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.exceptions import HTTPException
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"

NOT_BUILT_MESSAGE = "The frontend has not been built yet: frontend/dist is missing."
NOT_BUILT_HINT = "Build the frontend so its output lands in frontend/dist, then reload."


def _root(request: Request) -> Response:
    """``GET /`` — trade ``?t=`` for the session cookie, else serve the frontend.

    With a token to exchange the existing behaviour is unchanged (cookie +
    redirect, or the plain probe response for a wrong token). Without one,
    serve ``index.html`` when the frontend has been built and a JSON hint
    explaining how to build it when ``frontend/dist`` is missing.
    """
    if request.query_params.get("t") is not None:
        return token_exchange(request)
    index = FRONTEND_DIST / "index.html"
    if index.is_file():
        return FileResponse(index)
    return JSONResponse(
        ok(
            {
                "status": "frontend_not_built",
                "message": NOT_BUILT_MESSAGE,
                "hint": NOT_BUILT_HINT,
            }
        )
    )


class _SpaStaticFiles(StaticFiles):
    """Serve ``frontend/dist`` with an SPA HTML fallback.

    Starlette 1.10 *raises* ``HTTPException(404)`` for a missing path rather
    than returning a 404 response, so the fallback lives in ``except``:
    extension-less *page* paths outside the API get ``index.html`` so
    client-side routes deep-link correctly, while unknown ``api/`` paths and
    missing assets keep their real 404s.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            # ``path`` comes from ``StaticFiles.get_path``: OS-normalised
            # (backslashes on Windows) with no leading slash, so match the
            # first component instead of the literal "api/".
            parts = Path(path).parts
            if "." in Path(path).name or (parts and parts[0] == "api"):
                raise  # missing asset or unknown API route: keep the real 404
            return await super().get_response("index.html", scope)


def _install_frontend(app: FastAPI) -> None:
    """Serve ``frontend/dist`` (when built) through the 404 *status* handler.

    A catch-all ``app.mount("/")`` would shadow every route registered after
    ``create_app``, because the mount sits in front of them and answers any
    unmatched path itself. Hooking the not-found path instead guarantees that
    matched routes always win, while unmatched ``GET``/``HEAD`` paths still
    get their static file — or the SPA ``index.html`` — and everything else
    keeps the standard JSON error envelope.
    """
    if not (FRONTEND_DIST / "index.html").is_file():
        return
    static = _SpaStaticFiles(directory=str(FRONTEND_DIST), html=True)
    # The envelope handler installed by ``install_error_handlers`` (which
    # ``create_app`` always runs before this), used for every 404 that is not
    # a static file or an SPA page.
    envelope_404 = app.exception_handlers.get(HTTPException)
    assert envelope_404 is not None, "install_error_handlers must run before _install_frontend"

    async def handler(request: Request, exc: HTTPException) -> Response:
        if request.method in ("GET", "HEAD"):
            try:
                return await static.get_response(static.get_path(request.scope), request.scope)
            except HTTPException as not_found:
                if not_found.status_code != 404:
                    raise
        # Not a static file or SPA page: render the standard 404 envelope.
        return await envelope_404(request, exc)

    app.add_exception_handler(404, handler)


def create_app() -> FastAPI:
    """Build the fully assembled application."""
    settings = settings_singleton()
    app = FastAPI(title=settings.app_name, version=settings.version)
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    install_auth(app, load_or_create_token(resolve_paths(settings)))
    # The root route: token exchange when ?t= is present, frontend otherwise.
    app.add_api_route("/", _root, methods=["GET"])
    install_settings(app.state.api_router)
    install_stream(app.state.api_router)
    # The single mount of the guarded router — a second one would register
    # every API route twice and list each of them twice in OpenAPI.
    app.include_router(app.state.api_router)
    # Health is intentionally public, so it is mounted on the app, outside the
    # guarded api_router; it stays the one auth-exempt /api/v1 route.
    app.include_router(health_router)
    # Static/SPA serving hooks into the 404 path when dist exists — never a
    # catch-all route, so it cannot shadow routes registered later.
    _install_frontend(app)
    return app


# ``uvicorn app.main:app``
app = create_app()
