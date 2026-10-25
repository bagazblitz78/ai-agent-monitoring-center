"""Shared FastAPI dependencies for the API routers."""

from __future__ import annotations

from collections.abc import Generator

from app.db import get_session
from sqlalchemy.orm import Session


def get_db() -> Generator[Session, None, None]:
    """Thin alias of :func:`app.db.get_session` — one entry point for routers."""
    yield from get_session()
