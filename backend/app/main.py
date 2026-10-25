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

    Unknown *page* paths (no file extension) return ``index.html`` so
    client-side routes deep-link correctly; missing assets still 404.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 404 and "." not in Path(path).name:
            return await super().get_response("index.html", scope)
        return response


def _install_frontend(app: FastAPI) -> None:
    """Mount ``frontend/dist`` when it exists; ``GET /`` covers the exact root."""
    if (FRONTEND_DIST / "index.html").is_file():
        app.mount(
            "/",
            _SpaStaticFiles(directory=str(FRONTEND_DIST), html=True),
            name="frontend",
        )


def create_app() -> FastAPI:
    """Build the fully assembled application."""
    settings = settings_singleton()
    app = FastAPI(title=settings.app_name, version=settings.version)
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    install_auth(app, load_or_create_token(resolve_paths(settings)))
    # The root route: token exchange when ?t= is present, frontend otherwise.
    # Registered before the frontend mount below so it wins the exact "/" match.
    app.add_api_route("/", _root, methods=["GET"])
    install_settings(app.state.api_router)
    install_stream(app.state.api_router)
    # The single mount of the guarded router — a second one would register
    # every API route twice and list each of them twice in OpenAPI.
    app.include_router(app.state.api_router)
    # Health is intentionally public, so it is mounted on the app, outside the
    # guarded api_router; it stays the one auth-exempt /api/v1 route.
    app.include_router(health_router)
    _install_frontend(app)
    return app


# ``uvicorn app.main:app``
app = create_app()
