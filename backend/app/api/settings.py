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

    The routes are added directly instead of through ``include_router``
    because ``add_api_route`` merges the router's ``require_session``
    dependency into each route, which the security sweep asserts on every
    ``/api/v1/*`` route.
    """
    router.add_api_route("/api/v1/settings", list_settings, methods=["GET"])
    router.add_api_route("/api/v1/settings", patch_settings, methods=["PATCH"])
