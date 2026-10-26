"""Project discovery endpoints: find agent projects under a root, register them.

Two routes, two different kinds of request:

* ``POST /api/v1/projects/discover`` **reads**. It always answers 200 and puts
  the path policy's verdict in ``errors`` (stable codes) and ``messages`` (the
  human wording from ``ERROR_MESSAGES``), so a caller can show *why* a root is
  unusable — the same contract as ``/projects/inspect``, where the single-file
  edition used to answer 422 instead.
* ``POST /api/v1/projects/discover/register`` **writes, in a bounded batch**.
  One bad path never aborts the rest, a path that is already registered is
  skipped (no 409: a batch is not all-or-nothing), and the monitors are
  refreshed once for the whole batch rather than once per path.

Neither route creates a file on disk: the scan only reads directories, and
registration only writes database rows (the acceptance tests snapshot the tree
byte for byte).
"""

from __future__ import annotations

from typing import Annotated, Any

from app.api.deps import get_db
from app.api.projects import build_project, resource
from app.engine.supervisor import refresh_project_monitors
from app.models import Project
from app.schemas.common import ok
from app.schemas.discovery import DiscoverRegisterRequest, DiscoverRequest
from app.services.discovery import discover_candidates
from app.services.paths import validate_project_path
from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

DbSession = Annotated[Session, Depends(get_db)]


def discover_projects(payload: DiscoverRequest, session: DbSession) -> dict[str, Any]:
    """``POST /api/v1/projects/discover`` — scan ``root`` read-only.

    Always 200: ``errors`` carries the machine-readable codes and ``messages``
    the human wording for them. 422 is reserved for body-shape violations.
    """
    registered = session.execute(select(Project.path_key)).scalars().all()
    result = discover_candidates(
        payload.root, depth=payload.depth, registered_keys=registered
    )
    data = result.as_dict()
    data["input"] = payload.root
    return ok(data)


def discover_register(
    payload: DiscoverRegisterRequest, session: DbSession, request: Request
) -> dict[str, Any]:
    """``POST /api/v1/projects/discover/register`` — add every usable path.

    Returns ``added`` (project resources), ``skipped`` (already registered),
    ``failed`` (path policy errors) and a ``counts`` summary of the three.
    """
    added: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for raw in payload.paths:
        validation = validate_project_path(raw)
        if not validation.ok or validation.path is None or validation.path_key is None:
            failed.append({"path": raw, "errors": list(validation.errors)})
            continue
        project = build_project(
            session,
            name=validation.path.name or str(validation.path),
            path=validation.path,
            path_key=validation.path_key,
        )
        if project is None:
            skipped.append({"path": raw, "reason": "already_registered"})
            continue
        added.append(resource(project))

    if added:
        # one refresh per batch: a 100-path request must not trigger 100 rescans
        refresh_project_monitors(request.app)
    return ok(
        {
            "added": added,
            "skipped": skipped,
            "failed": failed,
            "counts": {"added": len(added), "skipped": len(skipped), "failed": len(failed)},
        }
    )


def install_discovery(router: APIRouter) -> None:
    """Register both discovery routes on the guarded API router.

    ``add_api_route`` bakes the router's ``require_session`` dependency into
    each route (as with settings/stream/inspect), so the security sweep sees
    them as guarded. ``discover`` is a literal segment, registered next to the
    ``/projects/{project_id}`` routes rather than behind them.
    """
    router.add_api_route("/api/v1/projects/discover", discover_projects, methods=["POST"])
    router.add_api_route(
        "/api/v1/projects/discover/register", discover_register, methods=["POST"]
    )
