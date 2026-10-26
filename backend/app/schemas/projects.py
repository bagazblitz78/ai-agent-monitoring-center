"""Schemas for project inspection, registration and management (M3)."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_PATH_CHARS = 4096
MAX_NAME_CHARS = 120
MAX_WATCH_RULES_CHARS = 4096


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


class ProjectCreate(BaseModel):
    """Body of ``POST /api/v1/projects`` — register one directory."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=MAX_NAME_CHARS)
    path: str = Field(..., min_length=1, max_length=MAX_PATH_CHARS)
    watch_enabled: bool = True
    in_project_reporting: bool = False


class ProjectPatch(BaseModel):
    """Body of ``PATCH /api/v1/projects/{id}`` — every field optional.

    ``path`` is declared only so a client that sends it gets a 422 whose
    ``details`` carry ``path_immutable`` instead of a generic unknown-field
    error: a registered project's path is its identity and is never rewritten.
    """

    model_config = ConfigDict(extra="forbid")

    path: str | None = None
    name: str | None = Field(None, min_length=1, max_length=MAX_NAME_CHARS)
    watch_enabled: bool | None = None
    watch_rules_json: str | None = Field(None, max_length=MAX_WATCH_RULES_CHARS)
    in_project_reporting: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_null_values(cls, data: Any) -> Any:
        # ``None`` would otherwise be accepted by the Optional annotations and
        # written straight into a NOT NULL column (a 500 instead of a 422).
        if isinstance(data, dict):
            for key in ("name", "watch_enabled", "watch_rules_json", "in_project_reporting"):
                if key in data and data[key] is None:
                    raise ValueError(f"{key} must not be null")
        return data

    @field_validator("watch_rules_json")
    @classmethod
    def _rules_must_be_a_json_object(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) > MAX_WATCH_RULES_CHARS:
            raise ValueError(f"watch_rules_json must be at most {MAX_WATCH_RULES_CHARS} characters")
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("watch_rules_json must be valid JSON text") from exc
        if not isinstance(decoded, dict):
            raise ValueError("watch_rules_json must encode a JSON object")
        return value
