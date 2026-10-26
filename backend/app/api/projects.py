"""Project registration and management API.

Create / list / get / patch / archive / unarchive / refresh / delete — all
guarded, all enveloped, all paginated where they return a collection.

``DELETE`` means **unregister**: the row goes away, project-scoped telemetry
cascades through the schema's foreign keys, and not a single byte on disk is
touched (the acceptance test snapshots every file's mtime, size and hash
before and after). ``archive`` is the reversible alternative.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from app.api.deps import get_db
from app.engine.supervisor import refresh_project_monitors
from app.errors import Conflict, NotFound, ValidationFailed
from app.models import Project
from app.models.base import utc_now
from app.schemas.common import ok, paged
from app.schemas.projects import ProjectCreate, ProjectPatch
from app.services.git_info import inspect_repo
from app.services.paths import validate_project_path
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

DbSession = Annotated[Session, Depends(get_db)]

PROJECT_NOT_FOUND = "Project not found."
DUPLICATE_PATH_MESSAGE = "A project is already registered for this path."

ARCHIVED = "archived"
ACTIVE = "active"


def _require(session: Session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise NotFound(PROJECT_NOT_FOUND)
    return project


def resource(project: Project) -> dict[str, Any]:
    """Project resource as returned by every route (DB columns, verbatim)."""
    return {
        "id": project.id,
        "name": project.name,
        "path": project.path,
        "path_key": project.path_key,
        "is_git": project.is_git,
        "default_branch": project.default_branch,
        "current_branch": project.current_branch,
        "watch_enabled": project.watch_enabled,
        "watch_rules_json": project.watch_rules_json,
        "in_project_reporting": project.in_project_reporting,
        "status": project.status,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
        "archived_at": project.archived_at,
    }


def build_project(
    session: Session,
    *,
    name: str,
    path: Path,
    path_key: str,
    watch_enabled: bool = True,
    in_project_reporting: bool = False,
) -> Project | None:
    """Insert one project row for an already validated path.

    Returns ``None`` when the path is taken — either a row already exists or a
    concurrent registration won the race (``IntegrityError``). Callers decide
    what "taken" means: :func:`create_project` answers **409**, the batch
    registration route skips the path and carries on.
    """
    existing = session.execute(
        select(Project.id).where(Project.path_key == path_key)
    ).first()
    if existing is not None:
        return None

    info = inspect_repo(path)
    project = Project(
        name=name,
        path=str(path),
        path_key=path_key,
        is_git=info.is_git,
        default_branch=info.default_branch,
        current_branch=info.branch,
        watch_enabled=watch_enabled,
        in_project_reporting=in_project_reporting,
    )
    session.add(project)
    try:
        session.commit()
    except IntegrityError:
        # A concurrent registration of the same path_key loses the race here
        # rather than blowing up with a 500.
        session.rollback()
        return None
    session.refresh(project)
    return project


def create_project(payload: ProjectCreate, session: DbSession, request: Request) -> dict[str, Any]:
    """``POST /api/v1/projects`` — validate, inspect, then insert one row."""
    validation = validate_project_path(payload.path)
    if not validation.ok:
        raise ValidationFailed(
            details=[{"field": "path", "code": code} for code in validation.errors]
        )
    assert validation.path is not None and validation.path_key is not None

    project = build_project(
        session,
        name=payload.name,
        path=validation.path,
        path_key=validation.path_key,
        watch_enabled=payload.watch_enabled,
        in_project_reporting=payload.in_project_reporting,
    )
    if project is None:
        # The path is already registered here: the single-register route keeps
        # its documented 409, unlike the batch route, which skips instead.
        raise Conflict(DUPLICATE_PATH_MESSAGE)
    # A brand-new registration must be watched right away, not on restart.
    refresh_project_monitors(request.app)
    return ok(resource(project))


def list_projects(
    session: DbSession,
    status: Annotated[Literal["active", "archived", "all"], Query()] = "active",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """``GET /api/v1/projects`` — one page, newest first, with ``meta.total``."""
    filters = [] if status == "all" else [Project.status == status]
    total = session.execute(select(func.count(Project.id)).where(*filters)).scalar_one()
    rows = (
        session.execute(
            select(Project)
            .where(*filters)
            .order_by(Project.created_at.desc(), Project.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        .scalars()
        .all()
    )
    return paged([resource(row) for row in rows], page, page_size, total)


def get_project(project_id: str, session: DbSession) -> dict[str, Any]:
    """``GET /api/v1/projects/{id}`` — one project (archived included)."""
    return ok(resource(_require(session, project_id)))


def patch_project(
    project_id: str, payload: ProjectPatch, session: DbSession, request: Request
) -> dict[str, Any]:
    """``PATCH /api/v1/projects/{id}`` — rename and reconfigure, never relocate."""
    project = _require(session, project_id)
    changed = payload.model_dump(exclude_unset=True)
    if "path" in changed:
        # The envelope keeps the canonical 422 code (M2 invariant: a 422 raised
        # here and a 422 from FastAPI parsing are indistinguishable), and the
        # domain code travels in ``details`` like every other path error.
        raise ValidationFailed(details=[{"field": "path", "code": "path_immutable"}])
    for field, value in changed.items():
        setattr(project, field, value)
    session.commit()
    session.refresh(project)
    # ``watch_enabled`` and the path decide whether this project is watched.
    refresh_project_monitors(request.app)
    return ok(resource(project))


def delete_project(project_id: str, session: DbSession, request: Request) -> dict[str, Any]:
    """``DELETE /api/v1/projects/{id}`` — unregister; files on disk stay put."""
    project = _require(session, project_id)
    session.delete(project)
    session.commit()
    refresh_project_monitors(request.app)  # unregistering stops the watcher
    return ok({"deleted": True})


def archive_project(project_id: str, session: DbSession, request: Request) -> dict[str, Any]:
    """``POST /api/v1/projects/{id}/archive`` — reversible: excluded from monitoring."""
    project = _require(session, project_id)
    project.status = ARCHIVED
    project.archived_at = utc_now()
    session.commit()
    session.refresh(project)
    refresh_project_monitors(request.app)  # archived projects are not watched
    return ok(resource(project))


def unarchive_project(project_id: str, session: DbSession, request: Request) -> dict[str, Any]:
    """``POST /api/v1/projects/{id}/unarchive`` — back to ``active``."""
    project = _require(session, project_id)
    project.status = ACTIVE
    project.archived_at = None
    session.commit()
    session.refresh(project)
    refresh_project_monitors(request.app)  # back to active: watch it again
    return ok(resource(project))


def refresh_project(project_id: str, session: DbSession) -> dict[str, Any]:
    """``POST /api/v1/projects/{id}/refresh`` — re-read git state for the stored path."""
    project = _require(session, project_id)
    info = inspect_repo(Path(project.path))
    project.is_git = info.is_git
    project.default_branch = info.default_branch
    project.current_branch = info.branch
    session.commit()
    session.refresh(project)
    return ok(resource(project))


def install_projects(router: APIRouter) -> None:
    """Register every project route on the guarded API router."""
    router.add_api_route("/api/v1/projects", create_project, methods=["POST"])
    router.add_api_route("/api/v1/projects", list_projects, methods=["GET"])
    router.add_api_route("/api/v1/projects/{project_id}", get_project, methods=["GET"])
    router.add_api_route("/api/v1/projects/{project_id}", patch_project, methods=["PATCH"])
    router.add_api_route("/api/v1/projects/{project_id}", delete_project, methods=["DELETE"])
    router.add_api_route(
        "/api/v1/projects/{project_id}/archive", archive_project, methods=["POST"]
    )
    router.add_api_route(
        "/api/v1/projects/{project_id}/unarchive", unarchive_project, methods=["POST"]
    )
    router.add_api_route(
        "/api/v1/projects/{project_id}/refresh", refresh_project, methods=["POST"]
    )
