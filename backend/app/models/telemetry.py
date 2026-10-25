"""Telemetry tables: snapshots, file changes, git commits, logs, runs, notifications."""
from __future__ import annotations

from app.models.base import Base, new_id, utc_now
from sqlalchemy import Float, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column


class ProcessSnapshot(Base):
    """One observed process snapshot for a registered agent (retention-pruned)."""

    __tablename__ = "process_snapshots"
    __table_args__ = (Index("ix_process_snapshots_agent_id_ts", "agent_id", "ts"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    ts: Mapped[str] = mapped_column(Text)
    agent_id: Mapped[str] = mapped_column(
        Text, ForeignKey("agents.id", ondelete="CASCADE")
    )
    pid: Mapped[int] = mapped_column(Integer)
    ppid: Mapped[int] = mapped_column(Integer)
    exe: Mapped[str] = mapped_column(Text)
    cmdline_redacted: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[str] = mapped_column(Text)
    cpu_pct: Mapped[float | None] = mapped_column(Float, default=None)
    rss_bytes: Mapped[int | None] = mapped_column(Integer, default=None)
    create_time: Mapped[float | None] = mapped_column(Float, default=None)


class FileChange(Base):
    """One filesystem change observed inside a registered project root."""

    __tablename__ = "file_changes"
    __table_args__ = (Index("ix_file_changes_project_id_ts", "project_id", "ts"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    ts: Mapped[str] = mapped_column(Text)
    project_id: Mapped[str] = mapped_column(
        Text, ForeignKey("projects.id", ondelete="CASCADE")
    )
    path_relative: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    git_status: Mapped[str | None] = mapped_column(Text, default=None)
    source: Mapped[str] = mapped_column(Text)


class GitCommit(Base):
    """One commit discovered by polling a project's git repository."""

    __tablename__ = "git_commits"
    __table_args__ = (UniqueConstraint("project_id", "sha"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        Text, ForeignKey("projects.id", ondelete="CASCADE")
    )
    sha: Mapped[str] = mapped_column(Text)
    short_sha: Mapped[str] = mapped_column(Text)
    author_name: Mapped[str] = mapped_column(Text)
    author_email_redacted: Mapped[str] = mapped_column(Text)
    committed_at: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text)
    files_json: Mapped[str] = mapped_column(Text, default="{}")
    source: Mapped[str] = mapped_column(Text)


class LogEntry(Base):
    """One log line captured from a configured source (retention-pruned)."""

    __tablename__ = "log_entries"
    __table_args__ = (
        Index("ix_log_entries_project_id_ts", "project_id", "ts"),
        Index("ix_log_entries_level_ts", "level", "ts"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    ts: Mapped[str] = mapped_column(Text, index=True)
    level: Mapped[str] = mapped_column(Text, index=True)
    source: Mapped[str] = mapped_column(Text)
    project_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("projects.id", ondelete="SET NULL"), default=None
    )
    agent_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("agents.id", ondelete="SET NULL"), default=None
    )
    task_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("tasks.id", ondelete="SET NULL"), default=None
    )
    message: Mapped[str] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text, default=None)
    stream: Mapped[str] = mapped_column(Text, default="unknown")
    file_ref: Mapped[str | None] = mapped_column(Text, default=None)


class VerificationRun(Base):
    """One executed verification check attached to a project, task or milestone."""

    __tablename__ = "verification_runs"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        Text, ForeignKey("projects.id", ondelete="CASCADE")
    )
    task_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("tasks.id", ondelete="SET NULL"), default=None
    )
    milestone_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("milestones.id", ondelete="SET NULL"), default=None
    )
    check_name: Mapped[str] = mapped_column(Text)
    command: Mapped[str] = mapped_column(Text)
    argv_json: Mapped[str | None] = mapped_column(Text, default=None)
    exit_code: Mapped[int | None] = mapped_column(Integer, default=None)
    status: Mapped[str] = mapped_column(Text)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(Text, default="")
    output_excerpt: Mapped[str | None] = mapped_column(Text, default=None)
    started_at: Mapped[str] = mapped_column(Text)
    finished_at: Mapped[str | None] = mapped_column(Text, default=None)
    source: Mapped[str] = mapped_column(Text)


class Notification(Base):
    """One in-app notification produced by a rule (deduped by ``dedupe_key``)."""

    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_created_at", "created_at"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    created_at: Mapped[str] = mapped_column(Text, default=utc_now)
    rule_key: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    project_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("projects.id", ondelete="SET NULL"), default=None
    )
    agent_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("agents.id", ondelete="SET NULL"), default=None
    )
    task_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("tasks.id", ondelete="SET NULL"), default=None
    )
    dedupe_key: Mapped[str | None] = mapped_column(Text, default=None)
    read_at: Mapped[str | None] = mapped_column(Text, default=None)
