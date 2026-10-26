"""Schemas for project inspection and (from Task 4) project management."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

MAX_PATH_CHARS = 4096


class InspectRequest(BaseModel):
    """Body of ``POST /api/v1/projects/inspect`` — a candidate directory path.

    Validation is intentionally shallow: any 1..4096 character string is
    accepted so the endpoint can answer **200** with machine-readable error
    codes (``traversal``, ``not_found``, ...) instead of a 422. Only a
    structurally invalid *body* (missing field, unknown field, wrong type)
    produces 422.
    """

    model_config = ConfigDict(extra="forbid")

    path: str = Field(..., min_length=1, max_length=MAX_PATH_CHARS)
