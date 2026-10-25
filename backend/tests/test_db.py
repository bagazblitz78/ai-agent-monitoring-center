"""Tests for the database engine: pragmas, FK enforcement, IDs and timestamps."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta

import pytest
from app import config, db
from app.models import Agent, Base, Project
from app.models.base import utc_now
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
