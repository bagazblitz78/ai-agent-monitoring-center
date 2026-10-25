"""FastAPI application factory.

``create_app`` wires the request context middleware and the error handlers.
Routers are added by later tasks — this factory mounts none.
"""

from __future__ import annotations

from app.config import settings_singleton
from app.errors import install_error_handlers
from app.middleware import RequestContextMiddleware
from fastapi import FastAPI


def create_app() -> FastAPI:
    """Build the application with request context and error handling installed."""
    settings = settings_singleton()
    app = FastAPI(title=settings.app_name, version=settings.version)
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    return app
