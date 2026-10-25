"""FastAPI application factory.

``create_app`` wires the request context middleware, the error handlers and
the local session token auth. Feature routers are mounted onto
``app.state.api_router`` by later tasks.
"""

from __future__ import annotations

from app.config import resolve_paths, settings_singleton
from app.errors import install_error_handlers
from app.middleware import RequestContextMiddleware
from app.security import install_auth, load_or_create_token
from fastapi import FastAPI


def create_app() -> FastAPI:
    """Build the application with request context, error handling and auth installed."""
    settings = settings_singleton()
    app = FastAPI(title=settings.app_name, version=settings.version)
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    install_auth(app, load_or_create_token(resolve_paths(settings)))
    # Testing only: Task 9 performs the real mount as part of final assembly,
    # but the router's routes must be reachable before that task lands.
    app.include_router(app.state.api_router)
    return app
