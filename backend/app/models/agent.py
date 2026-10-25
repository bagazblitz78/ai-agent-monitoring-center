"""Agent and AgentSession models: discovered processes and their live sessions."""
from __future__ import annotations

from app.models.base import Base, TimestampMixin, new_id
from sqlalchemy import ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column


class Agent(Base, TimestampMixin):
    """A registered AI-agent process matched inside a project."""

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        Text, ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(Text)
    adapter: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    match_kind: Mapped[str] = mapped_column(Text)
    match_value: Mapped[str | None] = mapped_column(Text, default=None)
    confirmed_pid: Mapped[int | None] = mapped_column(Integer, default=None)
    status: Mapped[str] = mapped_column(Text)
    status_source: Mapped[str] = mapped_column(Text)
    last_activity_at: Mapped[str | None] = mapped_column(Text, default=None)
    last_event_message: Mapped[str | None] = mapped_column(Text, default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)


class AgentSession(Base, TimestampMixin):
    """One adapter session reported by an agent (``external_key`` is its id)."""

    __tablename__ = "agent_sessions"
    __table_args__ = (UniqueConstraint("agent_id", "external_key"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    agent_id: Mapped[str] = mapped_column(Text, ForeignKey("agents.id", ondelete="CASCADE"))
    project_id: Mapped[str] = mapped_column(Text, ForeignKey("projects.id", ondelete="CASCADE"))
    external_key: Mapped[str | None] = mapped_column(Text, default=None)
    status: Mapped[str] = mapped_column(Text)
    started_at: Mapped[str] = mapped_column(Text)
    ended_at: Mapped[str | None] = mapped_column(Text, default=None)
    last_heartbeat_at: Mapped[str | None] = mapped_column(Text, default=None)
    heartbeat_interval_s: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text)
