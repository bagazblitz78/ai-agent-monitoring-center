"""FastAPI application factory.

``create_app`` wires the request context middleware, the error handlers, the
local session token auth and the feature routers: settings onto the guarded
``app.state.api_router``, health onto the app itself so it stays public.
"""

from __future__ import annotations

from app.api.health import router as health_router
from app.api.settings import install_settings
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
    install_settings(app.state.api_router)
    # Testing only: Task 9 performs the real mount as part of final assembly,
    # but the router's routes must be reachable before that task lands.
    app.include_router(app.state.api_router)
    # Health is intentionally public, so it is mounted on the app, outside the
    # guarded api_router; Task 9 keeps this exemption during final assembly.
    app.include_router(health_router)
    return app
