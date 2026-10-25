"""ActivityEvent: the append-only event log that powers SSE replay."""
from __future__ import annotations

from app.models.base import Base
from sqlalchemy import ForeignKey, Index, Integer, Text, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column


class ActivityEvent(Base):
    """One persisted activity event; ``seq`` is the SSE resume cursor."""

    __tablename__ = "activity_events"
    __table_args__ = (
        Index("ix_activity_events_project_id_ts", "project_id", "ts"),
        Index("ix_activity_events_agent_id_ts", "agent_id", "ts"),
        Index("ix_activity_events_type_ts", "type", "ts"),
        # AUTOINCREMENT: seq is the SSE resume cursor, so a deleted row's id
        # must never be recycled for a later event.
        {"sqlite_autoincrement": True},
    )

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_uid: Mapped[str] = mapped_column(Text, unique=True, index=True)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    ts: Mapped[str] = mapped_column(Text, index=True)
    type: Mapped[str] = mapped_column(Text, index=True)
    source: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text, default="info")
    message: Mapped[str] = mapped_column(Text, default="")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    project_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("projects.id", ondelete="SET NULL"), index=True, default=None
    )
    agent_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("agents.id", ondelete="SET NULL"), index=True, default=None
    )
    session_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("agent_sessions.id", ondelete="SET NULL"), default=None
    )
    task_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("tasks.id", ondelete="SET NULL"), default=None
    )
    milestone_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("milestones.id", ondelete="SET NULL"), default=None
    )


def count_events(session: Session) -> int:
    """Return how many activity events are persisted."""
    return session.execute(select(func.count()).select_from(ActivityEvent)).scalar_one()
