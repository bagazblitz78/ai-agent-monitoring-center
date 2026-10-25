"""Task and TaskDependency models: the work-graph nodes and their blocking edges."""
from __future__ import annotations

from app.models.base import Base, TimestampMixin, new_id
from sqlalchemy import Float, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column


class Task(Base, TimestampMixin):
    """One unit of work; ``external_key`` is the adapter-supplied id for upserts."""

    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("project_id", "external_key"),
        Index("ix_tasks_project_id_status", "project_id", "status"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(Text, ForeignKey("projects.id", ondelete="CASCADE"))
    milestone_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("milestones.id", ondelete="SET NULL"), default=None
    )
    parent_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("tasks.id", ondelete="SET NULL"), default=None
    )
    agent_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("agents.id", ondelete="SET NULL"), default=None
    )
    external_key: Mapped[str | None] = mapped_column(Text, default=None)
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    priority: Mapped[str] = mapped_column(Text, default="medium")
    status: Mapped[str] = mapped_column(Text, default="backlog")
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    progress_pct: Mapped[int] = mapped_column(Integer, default=0)
    verification_status: Mapped[str] = mapped_column(Text, default="unverified")
    criteria_json: Mapped[str | None] = mapped_column(Text, default=None)
    evidence_json: Mapped[str | None] = mapped_column(Text, default=None)
    blocker: Mapped[str | None] = mapped_column(Text, default=None)
    error_details: Mapped[str | None] = mapped_column(Text, default=None)
    due_at: Mapped[str | None] = mapped_column(Text, default=None)
    started_at: Mapped[str | None] = mapped_column(Text, default=None)
    completed_at: Mapped[str | None] = mapped_column(Text, default=None)
    reported_completed_at: Mapped[str | None] = mapped_column(Text, default=None)
    verified_at: Mapped[str | None] = mapped_column(Text, default=None)


class TaskDependency(Base):
    """Blocking edge: ``task_id`` waits for ``depends_on_id`` to complete."""

    __tablename__ = "task_dependencies"

    task_id: Mapped[str] = mapped_column(
        Text, ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    depends_on_id: Mapped[str] = mapped_column(
        Text, ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
