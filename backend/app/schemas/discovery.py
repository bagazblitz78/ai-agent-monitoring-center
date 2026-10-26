"""Schemas for scanning a root directory and batch-registering what it holds."""

from __future__ import annotations

from app.schemas.projects import MAX_PATH_CHARS
from app.services.discovery import DISCOVER_DEPTH
from pydantic import BaseModel, ConfigDict, Field

#: Longest batch ``POST /api/v1/projects/discover/register`` accepts at once.
MAX_BATCH_PATHS = 100


class DiscoverRequest(BaseModel):
    """Body of ``POST /api/v1/projects/discover`` — a root directory to scan.

    Validation stays shallow (as in ``InspectRequest``): any 1..4096 character
    string reaches the walk, which answers **200** with ``errors`` and
    ``messages`` instead of a 422. Only a structurally invalid *body* — a
    missing field, an unknown field, a wrong type — produces 422.
    """

    model_config = ConfigDict(extra="forbid")

    root: str = Field(..., min_length=1, max_length=MAX_PATH_CHARS)
    depth: int = Field(DISCOVER_DEPTH, ge=0)


class DiscoverRegisterRequest(BaseModel):
    """Body of ``POST /api/v1/projects/discover/register`` — one batch of paths.

    Each entry is validated on its own downstream so one bad path never aborts
    the batch; the batch itself is capped, so a single request cannot turn into
    an unbounded registration storm.
    """

    model_config = ConfigDict(extra="forbid")

    paths: list[str] = Field(..., max_length=MAX_BATCH_PATHS)
