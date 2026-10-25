"""Shared declarative base and the ID/timestamp helpers used by every model."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> str:
    """Return the current UTC time as ISO-8601 text ending in ``Z``."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id() -> str:
    """Return a new UUIDv4 identifier as a 32-character hex string."""
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    """Declarative base shared by all AgentScope tables."""


class TimestampMixin:
    """``created_at``/``updated_at`` text columns maintained by :func:`utc_now`."""

    created_at: Mapped[str] = mapped_column(Text, default=utc_now)
    updated_at: Mapped[str] = mapped_column(Text, default=utc_now, onupdate=utc_now)
