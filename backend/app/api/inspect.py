"""Project inspection endpoints.

Two routes share one payload builder:

* ``POST /api/v1/projects/inspect`` validates a *candidate* path before anything
  is registered (MILESTONES M3);
* ``POST /api/v1/projects/{id}/inspect`` re-inspects an already registered
  project's stored path (ARCHITECTURE §8).

Both are read-only: they stat and ``realpath`` the directory and — when it is a
git repository — run read-only git commands. Neither creates rows, files,
watchers or directories, which the tests assert.
"""

from __future__ import annotations

import os
from typing import Annotated, Any

from app.api.deps import get_db
from app.errors import NotFound
from app.models import Project
from app.schemas.common import ok
from app.schemas.projects import InspectRequest
from app.services.git_info import inspect_repo
from app.services.paths import validate_project_path
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

DbSession = Annotated[Session, Depends(get_db)]

PROJECT_NOT_FOUND = "Project not found."


def _report(raw: str) -> dict[str, Any]:
    """Build the inspect payload for ``raw`` without touching anything writable."""
    validation = validate_project_path(raw)
    path = validation.path
    exists = is_directory = readable = False
    if path is not None:
        try:
            exists = path.exists()
            is_directory = exists and path.is_dir()
            readable = is_directory and os.access(path, os.R_OK)
        except OSError:
            # A path we cannot even stat reports as absent rather than raising.
            exists = is_directory = readable = False

    data: dict[str, Any] = {
        "input": raw,
        "path": str(path) if path is not None else None,
        "path_key": validation.path_key,
        "exists": exists,
        "is_directory": is_directory,
        "readable": readable,
        "is_git": False,
        "branch": None,
        "default_branch": None,
        "errors": list(validation.errors),
    }
    if is_directory and readable:
        info = inspect_repo(path)
        data["is_git"] = info.is_git
        data["branch"] = info.branch
        data["default_branch"] = info.default_branch
    return data


def inspect_candidate(payload: InspectRequest) -> dict[str, Any]:
    """``POST /api/v1/projects/inspect`` — validate a path before registering it.

    Always 200: the payload carries ``errors`` so the caller can show why a path
    is unusable. 422 is reserved for body-shape violations.
    """
    return ok(_report(payload.path))


def inspect_registered(project_id: str, session: DbSession) -> dict[str, Any]:
    """``POST /api/v1/projects/{id}/inspect`` — re-inspect a registered project."""
    project = session.get(Project, project_id)
    if project is None:
        raise NotFound(PROJECT_NOT_FOUND)
    return ok(_report(project.path))


def install_inspect(router: APIRouter) -> None:
    """Register both inspect routes on the guarded API router.

    ``add_api_route`` (like settings/stream) bakes the router's
    ``require_session`` dependency into each route, so the security sweep sees
    them as guarded.
    """
    router.add_api_route("/api/v1/projects/inspect", inspect_candidate, methods=["POST"])
    router.add_api_route(
        "/api/v1/projects/{project_id}/inspect", inspect_registered, methods=["POST"]
    )
