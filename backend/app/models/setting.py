"""Setting model: key/value rows persisted as JSON text."""
from __future__ import annotations

from app.models.base import Base, utc_now
from sqlalchemy import Text
from sqlalchemy.orm import Mapped, mapped_column


class Setting(Base):
    """A single user setting, stored as serialized JSON in ``value_json``."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text, default=utc_now, onupdate=utc_now)
