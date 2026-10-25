"""Project model: a monitored repository or directory."""
from __future__ import annotations

from app.models.base import Base, TimestampMixin, new_id
from sqlalchemy import Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column


class Project(Base, TimestampMixin):
    """One watched project; ``path_key`` is the case-folded ``path``."""

    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("path_key"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text, unique=True)
    path_key: Mapped[str] = mapped_column(Text, index=True)
    is_git: Mapped[bool] = mapped_column()
    default_branch: Mapped[str | None] = mapped_column(Text, default=None)
    current_branch: Mapped[str | None] = mapped_column(Text, default=None)
    watch_enabled: Mapped[bool] = mapped_column(default=True)
    watch_rules_json: Mapped[str] = mapped_column(Text, default="{}")
    in_project_reporting: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(Text, default="active")
    archived_at: Mapped[str | None] = mapped_column(Text, default=None)
