"""Milestone and MilestoneDependency models: ordered goals and their blocking edges."""
from __future__ import annotations

from app.models.base import Base, TimestampMixin, new_id
from sqlalchemy import Float, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column


class Milestone(Base, TimestampMixin):
    """One project milestone; ``position`` orders it inside the milestone board."""

    __tablename__ = "milestones"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        Text, ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    position: Mapped[int] = mapped_column(Integer, default=0)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    status: Mapped[str] = mapped_column(Text, default="planned")
    completion_criteria: Mapped[str | None] = mapped_column(Text, default=None)
    started_at: Mapped[str | None] = mapped_column(Text, default=None)
    completed_at: Mapped[str | None] = mapped_column(Text, default=None)


class MilestoneDependency(Base):
    """Blocking edge: ``milestone_id`` waits for ``depends_on_id`` to complete."""

    __tablename__ = "milestone_dependencies"

    milestone_id: Mapped[str] = mapped_column(
        Text, ForeignKey("milestones.id", ondelete="CASCADE"), primary_key=True
    )
    depends_on_id: Mapped[str] = mapped_column(
        Text, ForeignKey("milestones.id", ondelete="CASCADE"), primary_key=True
    )
