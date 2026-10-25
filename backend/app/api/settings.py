"""Settings API: read and upsert the persisted key/value settings."""

from __future__ import annotations

import json
from typing import Annotated, Any

from app.api.deps import get_db
from app.models import Setting
from app.schemas.common import ok
from app.schemas.settings import SettingsPatch
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

DbSession = Annotated[Session, Depends(get_db)]


def _load_values(session: Session) -> dict[str, Any]:
    """Return every stored setting with its ``value_json`` decoded."""
    rows = session.execute(select(Setting)).scalars().all()
    return {row.key: json.loads(row.value_json) for row in rows}


def list_settings(session: DbSession) -> dict[str, Any]:
    """``GET /api/v1/settings`` — the full settings map."""
    return ok(_load_values(session))


def patch_settings(payload: SettingsPatch, session: DbSession) -> dict[str, Any]:
    """``PATCH /api/v1/settings`` — upsert every key in one transaction."""
    for key, value in payload.values.items():
        encoded = json.dumps(value)
        row = session.get(Setting, key)
        if row is None:
            session.add(Setting(key=key, value_json=encoded))
        else:
            row.value_json = encoded
    session.commit()
    return ok(_load_values(session))


def install_settings(router: APIRouter) -> None:
    """Register both settings routes on the guarded API router.

    The routes are registered straight onto the shared router with
    ``add_api_route`` — there is only one pair of sibling routes, so creating a
    child router just to ``include_router`` it adds an indirection without
    changing behaviour: FastAPI merges the router's ``require_session``
    dependency into every route either way, and the security sweep resolves
    dependencies through both registration styles.
    """
    router.add_api_route("/api/v1/settings", list_settings, methods=["GET"])
    router.add_api_route("/api/v1/settings", patch_settings, methods=["PATCH"])
