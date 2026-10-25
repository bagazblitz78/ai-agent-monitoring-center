"""Public health probe: database reachability, version and uptime.

``GET /api/v1/health`` is the only API route without the session dependency;
it always answers HTTP 200 and reports degradation inside the payload.
"""

from __future__ import annotations

import logging
import time
from contextlib import closing
from typing import Any

from app.config import resolve_paths, settings_singleton
from app.db import new_session
from app.schemas.common import ok
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

router = APIRouter()

# Uptime is measured from module import, which happens when the app is built.
_STARTED_AT = time.monotonic()

SELECT_ONE = text("SELECT 1")
ALEMBIC_VERSION = text("SELECT version_num FROM alembic_version")


def _schema_version(session: Session) -> str | None:
    """Return the current alembic revision, or None before migrations ran."""
    if not sa_inspect(session.get_bind()).has_table("alembic_version"):
        return None
    return session.execute(ALEMBIC_VERSION).scalar_one_or_none()


def _database_status() -> dict[str, Any]:
    """Probe with ``SELECT 1``; any failure degrades the report, never raises."""
    paths = resolve_paths()
    report: dict[str, Any] = {"ok": False, "path": str(paths.db), "schema_version": None}
    try:
        with closing(new_session()) as session:
            session.execute(SELECT_ONE)
            report["ok"] = True
            report["schema_version"] = _schema_version(session)
    except Exception:
        logger.warning("health check could not reach the database", exc_info=True)
        report["ok"] = False
        report["schema_version"] = None
    return report


def _sse_clients(request: Request) -> int:
    """Live SSE subscriber count from the ``EventHub`` on ``app.state.hub``.

    ``create_app`` attaches the hub the SSE stream subscribes to, so the value
    tracks actual connected streams; without a hub (e.g. a bare test app) the
    report degrades to 0 rather than failing the probe.
    """
    hub = getattr(getattr(request.app, "state", None), "hub", None)
    if hub is None:
        return 0
    return int(hub.subscriber_count())


@router.get("/api/v1/health")
def health(request: Request) -> JSONResponse:
    """Report liveness without authentication; degraded checks still return 200."""
    database = _database_status()
    return JSONResponse(
        ok(
            {
                "status": "ok" if database["ok"] else "degraded",
                "version": settings_singleton().version,
                "database": database,
                "uptime_seconds": time.monotonic() - _STARTED_AT,
                # ``sse_clients`` is read live from the hub; ``queue_depth`` and
                # ``events_processed`` are engine counters nothing tracks yet,
                # so they stay at their zero defaults.
                "engine": {
                    "queue_depth": 0,
                    "events_processed": 0,
                    "sse_clients": _sse_clients(request),
                },
            }
        )
    )
