"""Shared response envelope helpers and pagination parameters.

Routers build the envelope themselves (``JSONResponse({"data": ..., "meta": ...})``)
rather than serializing a response model, so these helpers are the single place
that defines the success envelope shape.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


def ok(data: Any, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the success envelope ``{"data": <payload>, "meta": {...}}``."""
    return {"data": data, "meta": meta if meta is not None else {}}


def paged(items: list[Any], page: int, page_size: int, total: int) -> dict[str, Any]:
    """Build a paginated success envelope for one page of ``items``."""
    return {
        "data": items,
        "meta": {"page": page, "page_size": page_size, "total": total},
    }


class PageParams(BaseModel):
    """Pagination query parameters with safe bounds (1-based, at most 200 per page)."""

    page: int = Field(1, ge=1)
    page_size: int = Field(50, ge=1, le=200)
