"""ORM models: importing this package populates ``Base.metadata``."""
from app.models.activity import ActivityEvent, count_events
from app.models.agent import Agent, AgentSession
from app.models.base import Base
from app.models.milestone import Milestone, MilestoneDependency
from app.models.project import Project
from app.models.setting import Setting
from app.models.task import Task, TaskDependency
from app.models.telemetry import (
    FileChange,
    GitCommit,
    LogEntry,
    Notification,
    ProcessSnapshot,
    VerificationRun,
)

__all__ = [
    "ActivityEvent",
    "Agent",
    "AgentSession",
    "Base",
    "FileChange",
    "GitCommit",
    "LogEntry",
    "Milestone",
    "MilestoneDependency",
    "Notification",
    "ProcessSnapshot",
    "Project",
    "Setting",
    "Task",
    "TaskDependency",
    "VerificationRun",
    "count_events",
]
