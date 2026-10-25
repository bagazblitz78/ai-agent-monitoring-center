"""Tests for the telemetry models: events, snapshots, changes, commits, logs, runs."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from app import config, db
from app.models import (
    ActivityEvent,
    Agent,
    Base,
    FileChange,
    GitCommit,
    LogEntry,
    Notification,
    ProcessSnapshot,
    Project,
    VerificationRun,
)
from app.models.activity import count_events
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

REPO_ROOT = Path(__file__).resolve().parents[2]

PROJECT_KWARGS = {
    "name": "demo",
    "path": "D:\\code\\demo",
    "path_key": "d:\\code\\demo",
    "is_git": True,
}

TELEMETRY_TABLES = {
    "activity_events",
    "file_changes",
    "git_commits",
    "log_entries",
    "notifications",
    "process_snapshots",
    "verification_runs",
}

TS = "2026-10-25T09:30:00.000Z"

# Column inventory per ARCHITECTURE §5 split by the binding nullability ruling:
# "present" columns are nullable unless in the allowed NOT NULL set (id,
# created_at, required names, defaulted columns, event timestamps that exist).
NULLABLE_COLUMNS: dict[str, set[str]] = {
    "activity_events": {"project_id", "agent_id", "session_id", "task_id", "milestone_id"},
    "process_snapshots": {"cpu_pct", "rss_bytes", "create_time"},
    "file_changes": {"git_status"},
    "git_commits": set(),
    "log_entries": {"project_id", "agent_id", "task_id", "detail", "file_ref"},
    "verification_runs": {
        "task_id",
        "milestone_id",
        "argv_json",
        "exit_code",
        "output_excerpt",
        "finished_at",
    },
    "notifications": {"project_id", "agent_id", "task_id", "dedupe_key", "read_at"},
}

NOT_NULL_COLUMNS: dict[str, set[str]] = {
    "activity_events": {
        "seq",
        "event_uid",
        "schema_version",
        "ts",
        "type",
        "source",
        "severity",
        "message",
        "payload_json",
    },
    "process_snapshots": {
        "id",
        "ts",
        "agent_id",
        "pid",
        "ppid",
        "exe",
        "cmdline_redacted",
        "state",
    },
    "file_changes": {
        "id",
        "ts",
        "project_id",
        "path_relative",
        "action",
        "size_bytes",
        "source",
    },
    "git_commits": {
        "id",
        "project_id",
        "sha",
        "short_sha",
        "author_name",
        "author_email_redacted",
        "committed_at",
        "subject",
        "files_json",
        "source",
    },
    "log_entries": {"id", "ts", "level", "source", "message", "stream"},
    "verification_runs": {
        "id",
        "project_id",
        "check_name",
        "command",
        "status",
        "duration_ms",
        "summary",
        "started_at",
        "source",
    },
    "notifications": {"id", "created_at", "rule_key", "severity", "title", "body"},
}

EXPECTED_INDEXES: dict[str, set[str]] = {
    "activity_events": {
        "ix_activity_events_event_uid",
        "ix_activity_events_ts",
        "ix_activity_events_project_id",
        "ix_activity_events_agent_id",
        "ix_activity_events_type",
        "ix_activity_events_project_id_ts",
        "ix_activity_events_agent_id_ts",
        "ix_activity_events_type_ts",
    },
    "process_snapshots": {"ix_process_snapshots_agent_id_ts"},
    "file_changes": {"ix_file_changes_project_id_ts"},
    "git_commits": set(),
    "log_entries": {
        "ix_log_entries_ts",
        "ix_log_entries_level",
        "ix_log_entries_project_id_ts",
        "ix_log_entries_level_ts",
    },
    "verification_runs": set(),
    "notifications": {"ix_notifications_created_at"},
}

EVENT_KWARGS = {"event_uid": "evt-1", "ts": TS, "type": "system", "source": "system"}


@pytest.fixture(autouse=True)
def app_home(tmp_path, monkeypatch):
    """Empty temp home whose schema comes from Alembic, never the real app dir."""
    monkeypatch.setenv("AGENTSCOPE_HOME", str(tmp_path))
    monkeypatch.setattr(config, "SETTINGS", None)
    db.reset_engine()
    alembic_cfg = Config(str(REPO_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(REPO_ROOT / "backend" / "alembic"))
    command.upgrade(alembic_cfg, "head")
    yield tmp_path
    db.reset_engine()


def test_seq_autoincrements_across_two_inserts(app_home):
    with db.new_session() as session:
        session.add(ActivityEvent(**EVENT_KWARGS))
        session.add(ActivityEvent(**{**EVENT_KWARGS, "event_uid": "evt-2"}))
        session.commit()
        events = session.query(ActivityEvent).order_by(ActivityEvent.seq).all()
    # seq is the SSE resume cursor: it must be a monotonic 1-based integer.
    assert [event.seq for event in events] == [1, 2]


def test_duplicate_event_uid_rejected(app_home):
    with db.new_session() as session:
        session.add(ActivityEvent(**EVENT_KWARGS))
        session.commit()

    with db.new_session() as session:
        session.add(ActivityEvent(**EVENT_KWARGS))
        with pytest.raises(IntegrityError, match="UNIQUE constraint"):
            session.commit()


def test_event_optional_columns_default_to_null(app_home):
    with db.new_session() as session:
        session.add(ActivityEvent(**EVENT_KWARGS))
        session.commit()
        event = session.query(ActivityEvent).one()

    assert event.schema_version == 1
    assert event.severity == "info"
    assert event.message == ""
    assert event.payload_json == "{}"
    for column in ("project_id", "agent_id", "session_id", "task_id", "milestone_id"):
        assert getattr(event, column) is None, f"activity_events.{column} must default to NULL"


def test_count_events_counts_persisted_rows(app_home):
    with db.new_session() as session:
        assert count_events(session) == 0
        for index in range(3):
            session.add(ActivityEvent(**{**EVENT_KWARGS, "event_uid": f"evt-{index}"}))
        session.commit()
        assert count_events(session) == 3


def test_telemetry_tables_and_indexes_match_migration(app_home):
    inspector = inspect(db.engine)
    metadata_tables = set(Base.metadata.tables)
    migrated_tables = set(inspector.get_table_names()) - {"alembic_version"}
    assert metadata_tables == migrated_tables
    assert TELEMETRY_TABLES <= metadata_tables

    for table_name, expected in EXPECTED_INDEXES.items():
        actual = {index["name"] for index in inspector.get_indexes(table_name)}
        assert actual == expected, f"{table_name} indexes: {actual!r}"


def test_activity_event_foreign_keys_point_at_real_tables(app_home):
    inspector = inspect(db.engine)
    foreign_keys = inspector.get_foreign_keys("activity_events")
    references = {
        (tuple(fk["constrained_columns"]), fk["referred_table"]) for fk in foreign_keys
    }
    assert references == {
        (("project_id",), "projects"),
        (("agent_id",), "agents"),
        (("session_id",), "agent_sessions"),
        (("task_id",), "tasks"),
        (("milestone_id",), "milestones"),
    }


def test_nullability_ruling_in_telemetry_schema(app_home):
    inspector = inspect(db.engine)
    columns = {
        table_name: {column["name"]: column for column in inspector.get_columns(table_name)}
        for table_name in TELEMETRY_TABLES
    }
    for table_name in sorted(TELEMETRY_TABLES):
        nullable, not_null = NULLABLE_COLUMNS[table_name], NOT_NULL_COLUMNS[table_name]
        assert set(columns[table_name]) == nullable | not_null, (
            f"{table_name} column inventory drifted"
        )
        for column_name in sorted(nullable):
            assert columns[table_name][column_name]["nullable"] is True, (
                f"{table_name}.{column_name} must be nullable"
            )
        for column_name in sorted(not_null):
            assert columns[table_name][column_name]["nullable"] is False, (
                f"{table_name}.{column_name} must stay NOT NULL"
            )


def test_optional_telemetry_columns_default_to_null(app_home):
    with db.new_session() as session:
        project = Project(**PROJECT_KWARGS)
        session.add(project)
        session.flush()
        agent = Agent(project_id=project.id, name="worker")
        session.add(agent)
        session.flush()

        session.add(
            ProcessSnapshot(
                ts=TS,
                agent_id=agent.id,
                pid=1234,
                ppid=1,
                exe="C:\\Python\\python.exe",
                state="running",
            )
        )
        session.add(
            FileChange(
                ts=TS,
                project_id=project.id,
                path_relative="src/app.py",
                action="modified",
                source="watchdog",
            )
        )
        session.add(
            GitCommit(
                project_id=project.id,
                sha="a" * 40,
                short_sha="a" * 7,
                author_name="Dev",
                author_email_redacted="d***@example.com",
                committed_at=TS,
                subject="initial commit",
                source="git_poll",
            )
        )
        session.add(LogEntry(ts=TS, level="info", source="opencode", message="hello"))
        session.add(
            VerificationRun(
                project_id=project.id,
                check_name="pytest",
                command="pytest -q",
                status="passed",
                started_at=TS,
                source="manual",
            )
        )
        session.add(
            Notification(
                rule_key="task.failed",
                severity="critical",
                title="Task failed",
                body="task-1 failed",
            )
        )
        session.commit()

        snapshot, change, commit = (
            session.query(ProcessSnapshot).one(),
            session.query(FileChange).one(),
            session.query(GitCommit).one(),
        )
        log, run, notification = (
            session.query(LogEntry).one(),
            session.query(VerificationRun).one(),
            session.query(Notification).one(),
        )

    for column in ("cpu_pct", "rss_bytes", "create_time"):
        assert getattr(snapshot, column) is None, f"process_snapshots.{column}"
    assert snapshot.cmdline_redacted == ""

    assert change.git_status is None
    assert change.size_bytes == 0
    assert commit.files_json == "{}"

    for column in ("project_id", "agent_id", "task_id", "detail", "file_ref"):
        assert getattr(log, column) is None, f"log_entries.{column}"
    assert log.stream == "unknown"

    for column in (
        "task_id",
        "milestone_id",
        "argv_json",
        "exit_code",
        "output_excerpt",
        "finished_at",
    ):
        assert getattr(run, column) is None, f"verification_runs.{column}"
    assert run.duration_ms == 0
    assert run.summary == ""

    for column in ("project_id", "agent_id", "task_id", "dedupe_key", "read_at"):
        assert getattr(notification, column) is None, f"notifications.{column}"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", notification.created_at)


def test_git_commit_rejects_duplicate_sha_within_project(app_home):
    with db.new_session() as session:
        project = Project(**PROJECT_KWARGS)
        session.add(project)
        session.flush()
        project_id = project.id
        commit_kwargs = {
            "project_id": project_id,
            "sha": "b" * 40,
            "short_sha": "b" * 7,
            "author_name": "Dev",
            "author_email_redacted": "d***@example.com",
            "committed_at": TS,
            "subject": "second",
            "source": "git_poll",
        }
        session.add(GitCommit(**commit_kwargs))
        session.commit()

    with db.new_session() as session:
        session.add(GitCommit(**commit_kwargs))
        with pytest.raises(IntegrityError, match="UNIQUE constraint"):
            session.commit()
