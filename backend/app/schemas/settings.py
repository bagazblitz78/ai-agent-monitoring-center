"""Schema for the ``PATCH /api/v1/settings`` payload."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, Json, field_validator

MAX_KEYS = 64
MAX_VALUE_CHARS = 4096


class SettingsPatch(BaseModel):
    """A non-empty, closed set of settings to upsert.

    ``values`` is declared as ``dict[str, Json]``; the ``before`` validator
    re-encodes native JSON values (``{"theme": "dark"}``) as JSON text so the
    ``Json`` field type validates them, and enforces the payload bounds: at
    most 64 keys, and at most 4096 serialized characters per value.
    """

    model_config = ConfigDict(extra="forbid")

    values: dict[str, Json] = Field(..., min_length=1)

    @field_validator("values", mode="before")
    @classmethod
    def _bounded_values(cls, values: Any) -> Any:
        # Anything that is not a dict is left for the field type to reject.
        if not isinstance(values, dict):
            return values
        if len(values) > MAX_KEYS:
            raise ValueError(f"at most {MAX_KEYS} keys are allowed, got {len(values)}")
        encoded: dict[str, str] = {}
        for key, value in values.items():
            text = json.dumps(value)
            if len(text) > MAX_VALUE_CHARS:
                raise ValueError(f"value for {key!r} exceeds {MAX_VALUE_CHARS} characters")
            encoded[key] = text
        return encoded
