"""Tests for the database engine: pragmas, FK enforcement, IDs and timestamps."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta

import pytest
from app import config, db
from app.models import Agent, AgentSession, Base, Project
from app.models.base import utc_now
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

PROJECT_KWARGS = {
    "name": "demo",
    "path": "D:\\code\\demo",
    "path_key": "d:\\code\\demo",
    "is_git": True,
    "default_branch": "main",
    "current_branch": "main",
}


@pytest.fixture(autouse=True)
def app_home(tmp_path, monkeypatch):
    """Point the app home at an empty temp dir; never touch the real data dir."""
    monkeypatch.setenv("AGENTSCOPE_HOME", str(tmp_path))
    monkeypatch.setattr(config, "SETTINGS", None)
    db.reset_engine()
    Base.metadata.create_all(db.engine)
    yield tmp_path
    db.reset_engine()


def test_pragmas_are_applied(app_home):
    engine = db.engine
    with engine.connect() as connection:
        journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar()
        foreign_keys = connection.exec_driver_sql("PRAGMA foreign_keys").scalar()
        busy_timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar()
    assert journal_mode.lower() == "wal"
    assert foreign_keys == 1
    assert busy_timeout == 5000


def test_foreign_keys_enforced(app_home):
    agent = Agent(
        project_id="does-not-exist",
        name="ghost",
        adapter="generic_process",
        provider="other",
        match_kind="none",
        status="unknown",
        status_source="unknown",
        last_activity_at=utc_now(),
        last_event_message="",
        last_error="",
    )
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        with db.new_session() as session:
            session.add(agent)
            session.commit()


def test_timestamps_are_utc_zulu(app_home):
    with db.new_session() as session:
        project = Project(**PROJECT_KWARGS)
        session.add(project)
        session.commit()
        created_at, updated_at = project.created_at, project.updated_at
    for value in (created_at, updated_at):
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", value)
        parsed = datetime.fromisoformat(value)
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == timedelta(0)


def test_ids_are_uuid4_strings(app_home):
    with db.new_session() as session:
        project = Project(**PROJECT_KWARGS)
        session.add(project)
        session.commit()
        identifier = project.id
    assert len(identifier) == 32
    assert uuid.UUID(identifier).version == 4


def test_new_agent_and_session_have_no_activity_yet(app_home):
    with db.new_session() as session:
        project = Project(**PROJECT_KWARGS)
        session.add(project)
        session.flush()

        agent = Agent(
            project_id=project.id,
            name="fresh",
            adapter="generic_process",
            provider="other",
            match_kind="none",
            status="unknown",
            status_source="unknown",
        )
        session.add(agent)
        session.flush()

        agent_session = AgentSession(
            agent_id=agent.id,
            project_id=project.id,
            status="running",
            started_at=utc_now(),
            heartbeat_interval_s=30,
            source="opencode",
            metadata_json="{}",
        )
        session.add(agent_session)
        session.commit()

        assert agent.last_activity_at is None
        assert agent.last_event_message is None
        assert agent.last_error is None
        assert agent_session.external_key is None
        assert agent_session.ended_at is None
        assert agent_session.last_heartbeat_at is None


def test_agent_and_session_construct_with_required_fields_only(app_home):
    with db.new_session() as session:
        project = Project(**PROJECT_KWARGS)
        session.add(project)
        session.flush()

        agent = Agent(project_id=project.id, name="minimal")
        session.add(agent)
        session.flush()

        agent_session = AgentSession(agent_id=agent.id, project_id=project.id)
        session.add(agent_session)
        session.commit()

        assert agent.adapter == "generic_process"
        assert agent.provider == "other"
        assert agent.match_kind == "none"
        assert agent.status == "unknown"
        assert agent.status_source == "unknown"

        assert agent_session.status == "running"
        started_at = agent_session.started_at
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", started_at)
        assert agent_session.heartbeat_interval_s == 30
        assert agent_session.source == "reported"
        assert agent_session.metadata_json == "{}"


def test_agent_sessions_project_id_is_indexed(app_home):
    inspector = inspect(db.engine)
    assert "ix_agent_sessions_project_id" in {
        index["name"] for index in inspector.get_indexes("agent_sessions")
    }


def test_non_git_project_has_null_branch(app_home):
    with db.new_session() as session:
        project = Project(
            name="plain",
            path="D:\\code\\plain",
            path_key="d:\\code\\plain",
            is_git=False,
        )
        session.add(project)
        session.commit()
    assert project.is_git is False
    assert project.default_branch is None
    assert project.current_branch is None
