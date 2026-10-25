"""Alembic tests: ``upgrade head`` on an empty DB must create and match the schema."""
from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from app import config
from app.models import Base
from sqlalchemy import create_engine, inspect

REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_COLUMNS = {
    "projects": {
        "id",
        "name",
        "path",
        "path_key",
        "is_git",
        "default_branch",
        "current_branch",
        "watch_enabled",
        "watch_rules_json",
        "in_project_reporting",
        "status",
        "created_at",
        "updated_at",
        "archived_at",
    },
    "agents": {
        "id",
        "project_id",
        "name",
        "adapter",
        "provider",
        "match_kind",
        "match_value",
        "confirmed_pid",
        "status",
        "status_source",
        "last_activity_at",
        "last_event_message",
        "last_error",
        "created_at",
        "updated_at",
    },
    "agent_sessions": {
        "id",
        "agent_id",
        "project_id",
        "external_key",
        "status",
        "started_at",
        "ended_at",
        "last_heartbeat_at",
        "heartbeat_interval_s",
        "source",
        "metadata_json",
        "created_at",
        "updated_at",
    },
    "settings": {"key", "value_json", "updated_at"},
}

# The nullability ruling: "present" columns are nullable unless in the
# allowed NOT NULL set (id, created_at, updated_at, required names, defaults).
NULLABLE_COLUMNS = [
    ("projects", "default_branch"),
    ("projects", "current_branch"),
    ("projects", "archived_at"),
    ("agents", "match_value"),
    ("agents", "confirmed_pid"),
    ("agents", "last_activity_at"),
    ("agents", "last_event_message"),
    ("agents", "last_error"),
    ("agent_sessions", "external_key"),
    ("agent_sessions", "ended_at"),
    ("agent_sessions", "last_heartbeat_at"),
]
NOT_NULL_COLUMNS = [
    ("projects", "id"),
    ("projects", "created_at"),
    ("projects", "updated_at"),
    ("projects", "path_key"),
]


@pytest.fixture
def upgraded_home(tmp_path, monkeypatch):
    """Run ``alembic upgrade head`` against an empty temp database."""
    monkeypatch.setenv("AGENTSCOPE_HOME", str(tmp_path))
    monkeypatch.setattr(config, "SETTINGS", None)
    alembic_cfg = Config(str(REPO_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option(
        "script_location", str(REPO_ROOT / "backend" / "alembic")
    )
    command.upgrade(alembic_cfg, "head")
    yield tmp_path, alembic_cfg


def test_upgrade_head_creates_all_tables(upgraded_home):
    home, alembic_cfg = upgraded_home
    # A second run on the same database must succeed (idempotent).
    command.upgrade(alembic_cfg, "head")

    db_file = home / "data" / "agent_scope.db"
    assert db_file.is_file()
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        inspector = inspect(engine)
        assert set(inspector.get_table_names()) == {
            "agents",
            "agent_sessions",
            "alembic_version",
            "milestone_dependencies",
            "milestones",
            "projects",
            "settings",
            "task_dependencies",
            "tasks",
        }
        for table_name, expected in EXPECTED_COLUMNS.items():
            actual = {column["name"] for column in inspector.get_columns(table_name)}
            assert actual == expected

        assert "ix_projects_path_key" in {ix["name"] for ix in inspector.get_indexes("projects")}
        assert "ix_agents_project_id" in {ix["name"] for ix in inspector.get_indexes("agents")}

        project_uniques = inspector.get_unique_constraints("projects")
        assert {"path"} in [set(c["column_names"]) for c in project_uniques]
        assert {"path_key"} in [set(c["column_names"]) for c in project_uniques]
        session_uniques = inspector.get_unique_constraints("agent_sessions")
        assert {"agent_id", "external_key"} in [set(c["column_names"]) for c in session_uniques]

        agent_fks = inspector.get_foreign_keys("agents")
        assert any(
            fk["referred_table"] == "projects"
            and fk["constrained_columns"] == ["project_id"]
            and fk.get("options", {}).get("ondelete") == "CASCADE"
            for fk in agent_fks
        )

        with engine.connect() as connection:
            version = connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
        assert version == "0002"
    finally:
        engine.dispose()


def test_migrations_match_metadata(upgraded_home):
    home, _ = upgraded_home
    engine = create_engine(f"sqlite:///{home / 'data' / 'agent_scope.db'}")
    try:
        with engine.connect() as connection:
            migration_context = MigrationContext.configure(connection)
            diff = compare_metadata(migration_context, Base.metadata)
        assert diff == []
    finally:
        engine.dispose()


def test_migrations_create_nullable_columns(upgraded_home):
    home, _ = upgraded_home
    engine = create_engine(f"sqlite:///{home / 'data' / 'agent_scope.db'}")
    try:
        inspector = inspect(engine)
        columns = {
            table_name: {c["name"]: c for c in inspector.get_columns(table_name)}
            for table_name, _ in NULLABLE_COLUMNS + NOT_NULL_COLUMNS
        }
        for table_name, column_name in NULLABLE_COLUMNS:
            assert columns[table_name][column_name]["nullable"] is True, (
                f"{table_name}.{column_name} must be nullable"
            )
        for table_name, column_name in NOT_NULL_COLUMNS:
            assert columns[table_name][column_name]["nullable"] is False, (
                f"{table_name}.{column_name} must stay NOT NULL"
            )
    finally:
        engine.dispose()
