"""File changes listing API (M3, Task 8).

``GET /api/v1/projects/{id}/file-changes`` returns one page of the changes
observed inside a registered project, newest first, with the project-scoped
filters (``action``, ``path_prefix``, ``q``, ``source``).

Envelope note: this route follows the M3 plan literally — the rows live in
``data.items`` and ``meta`` carries ``page``/``page_size``/``total`` — where
``GET /api/v1/projects`` uses the ``paged()`` helper and puts the rows straight
into ``data``.

An archived project stays readable: archiving stops the watchers, it does not
erase the history.
"""

from __future__ import annotations

from typing import Annotated, Any

from app.api.deps import get_db
from app.errors import NotFound
from app.models import FileChange, Project
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

DbSession = Annotated[Session, Depends(get_db)]

PROJECT_NOT_FOUND = "Project not found."

#: Columns a client is allowed to see, in a stable order.
ITEM_FIELDS = (
    "id",
    "ts",
    "path_relative",
    "action",
    "size_bytes",
    "git_status",
    "source",
)


def resource(change: FileChange) -> dict[str, Any]:
    """One file change as returned by the route (DB columns, verbatim)."""
    return {field: getattr(change, field) for field in ITEM_FIELDS}


def list_file_changes(
    project_id: str,
    session: DbSession,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
    action: Annotated[str | None, Query()] = None,
    path_prefix: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    source: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """``GET /api/v1/projects/{id}/file-changes`` — one page, newest first."""
    project = session.get(Project, project_id)
    if project is None:
        raise NotFound(PROJECT_NOT_FOUND)

    filters = [FileChange.project_id == project_id]
    if action:
        filters.append(FileChange.action == action)
    if path_prefix:
        filters.append(FileChange.path_relative.startswith(path_prefix))
    if q:
        # literal substring, case-insensitive: ``%``/``_`` are not wildcards
        filters.append(func.lower(FileChange.path_relative).contains(q.lower(), autoescape=True))
    if source:
        filters.append(FileChange.source == source)

    total = session.execute(select(func.count(FileChange.id)).where(*filters)).scalar_one()
    rows = (
        session.execute(
            select(FileChange)
            .where(*filters)
            .order_by(FileChange.ts.desc(), FileChange.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        .scalars()
        .all()
    )
    return {
        "data": {"items": [resource(row) for row in rows]},
        "meta": {"page": page, "page_size": page_size, "total": total},
    }


def install_file_changes(router: APIRouter) -> None:
    """Register the route on the guarded API router (session required)."""
    router.add_api_route(
        "/api/v1/projects/{project_id}/file-changes", list_file_changes, methods=["GET"]
    )
