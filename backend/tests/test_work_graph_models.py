"""Tests for the work-graph models: milestones, tasks and their dependency edges."""
from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from app import config, db
from app.models import Agent, Milestone, MilestoneDependency, Project, Task, TaskDependency
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

REPO_ROOT = Path(__file__).resolve().parents[2]

PROJECT_KWARGS = {
    "name": "demo",
    "path": "D:\\code\\demo",
    "path_key": "d:\\code\\demo",
    "is_git": True,
}

OTHER_PROJECT_KWARGS = {
    "name": "other",
    "path": "D:\\code\\other",
    "path_key": "d:\\code\\other",
    "is_git": False,
}

# The binding nullability ruling: event timestamps and optional descriptive or
# error columns are nullable; required names and defaulted columns stay NOT NULL.
MILESTONE_NULLABLE = [
    "description",
    "completion_criteria",
    "started_at",
    "completed_at",
]
MILESTONE_NOT_NULL = [
    "id",
    "project_id",
    "title",
    "position",
    "weight",
    "status",
    "created_at",
    "updated_at",
]
TASK_NULLABLE = [
    "milestone_id",
    "parent_id",
    "agent_id",
    "external_key",
    "description",
    "due_at",
    "started_at",
    "completed_at",
    "reported_completed_at",
    "verified_at",
    "blocker",
    "error_details",
    "criteria_json",
    "evidence_json",
]
TASK_NOT_NULL = [
    "id",
    "project_id",
    "title",
    "priority",
    "status",
    "weight",
    "progress_pct",
    "verification_status",
    "created_at",
    "updated_at",
]


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


def _new_project(**overrides) -> Project:
    return Project(**(overrides or PROJECT_KWARGS))


def test_external_key_duplicate_in_same_project_rejected(app_home):
    with db.new_session() as session:
        project = _new_project()
        session.add(project)
        session.flush()
        project_id = project.id
        session.add(Task(project_id=project_id, title="first", external_key="EXT-1"))
        session.commit()

    with db.new_session() as session:
        session.add(Task(project_id=project_id, title="second", external_key="EXT-1"))
        with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
            session.commit()


def test_external_key_duplicate_across_projects_allowed(app_home):
    with db.new_session() as session:
        first, second = _new_project(), _new_project(**OTHER_PROJECT_KWARGS)
        session.add_all([first, second])
        session.flush()
        session.add(Task(project_id=first.id, title="one", external_key="EXT-1"))
        session.add(Task(project_id=second.id, title="two", external_key="EXT-1"))
        session.commit()

    with db.new_session() as session:
        tasks = session.query(Task).order_by(Task.title).all()
        assert [task.external_key for task in tasks] == ["EXT-1", "EXT-1"]
        assert tasks[0].project_id != tasks[1].project_id


def test_parent_id_self_fk_resolves(app_home):
    with db.new_session() as session:
        project = _new_project()
        session.add(project)
        session.flush()
        parent = Task(project_id=project.id, title="parent")
        session.add(parent)
        session.flush()
        child = Task(project_id=project.id, title="child", parent_id=parent.id)
        session.add(child)
        # A task may also point at itself: the self-FK resolves both ways.
        parent.parent_id = parent.id
        session.commit()
        parent_id, child_id = parent.id, child.id

    with db.new_session() as session:
        assert session.get(Task, child_id).parent_id == parent_id
        assert session.get(Task, parent_id).parent_id == parent_id


def test_parent_id_unknown_task_rejected(app_home):
    with db.new_session() as session:
        project = _new_project()
        session.add(project)
        session.flush()
        session.add(
            Task(project_id=project.id, title="orphan", parent_id="does-not-exist")
        )
        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            session.commit()


def test_task_status_and_verification_defaults(app_home):
    with db.new_session() as session:
        project = _new_project()
        session.add(project)
        session.flush()
        session.add(Task(project_id=project.id, title="minimal"))
        session.commit()
        task_id = session.query(Task).one().id

    with db.new_session() as session:
        task = session.get(Task, task_id)
        assert task.status == "backlog"
        assert task.verification_status == "unverified"
        assert task.priority == "medium"
        assert task.weight == 1.0
        assert task.progress_pct == 0


def test_task_optional_columns_default_to_null(app_home):
    with db.new_session() as session:
        project = _new_project()
        session.add(project)
        session.flush()
        session.add(Task(project_id=project.id, title="bare"))
        session.commit()
        task_id = session.query(Task).one().id

    with db.new_session() as session:
        task = session.get(Task, task_id)
        for column in TASK_NULLABLE:
            assert getattr(task, column) is None, f"tasks.{column} must default to NULL"


def test_milestone_defaults_and_optional_columns_null(app_home):
    with db.new_session() as session:
        project = _new_project()
        session.add(project)
        session.flush()
        session.add(Milestone(project_id=project.id, title="first"))
        session.commit()
        milestone_id = session.query(Milestone).one().id

    with db.new_session() as session:
        milestone = session.get(Milestone, milestone_id)
        assert milestone.status == "planned"
        assert milestone.weight == 1.0
        assert milestone.position == 0
        for column in MILESTONE_NULLABLE:
            assert getattr(milestone, column) is None, (
                f"milestones.{column} must default to NULL"
            )


def test_nullability_ruling_in_migrated_schema(app_home):
    inspector = inspect(db.engine)
    columns = {
        table_name: {column["name"]: column for column in inspector.get_columns(table_name)}
        for table_name in ("milestones", "tasks")
    }
    for table_name, nullable_names, not_null_names in (
        ("milestones", MILESTONE_NULLABLE, MILESTONE_NOT_NULL),
        ("tasks", TASK_NULLABLE, TASK_NOT_NULL),
    ):
        for column_name in nullable_names:
            assert columns[table_name][column_name]["nullable"] is True, (
                f"{table_name}.{column_name} must be nullable"
            )
        for column_name in not_null_names:
            assert columns[table_name][column_name]["nullable"] is False, (
                f"{table_name}.{column_name} must stay NOT NULL"
            )
    for table_name, column_names in (
        ("milestone_dependencies", ["milestone_id", "depends_on_id"]),
        ("task_dependencies", ["task_id", "depends_on_id"]),
    ):
        for column in inspector.get_columns(table_name):
            assert column["nullable"] is False, (
                f"{table_name}.{column['name']} must stay NOT NULL"
            )
        assert [column["name"] for column in inspector.get_columns(table_name)] == column_names


def test_milestone_dependency_rejects_duplicate_edge(app_home):
    with db.new_session() as session:
        project = _new_project()
        session.add(project)
        session.flush()
        first = Milestone(project_id=project.id, title="one")
        second = Milestone(project_id=project.id, title="two")
        session.add_all([first, second])
        session.flush()
        session.add(MilestoneDependency(milestone_id=first.id, depends_on_id=second.id))
        session.commit()
        first_id, second_id = first.id, second.id

    with db.new_session() as session:
        session.add(MilestoneDependency(milestone_id=first_id, depends_on_id=second_id))
        with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
            session.commit()


def test_task_dependency_rejects_duplicate_edge(app_home):
    with db.new_session() as session:
        project = _new_project()
        session.add(project)
        session.flush()
        first = Task(project_id=project.id, title="one")
        second = Task(project_id=project.id, title="two")
        session.add_all([first, second])
        session.flush()
        session.add(TaskDependency(task_id=first.id, depends_on_id=second.id))
        session.commit()
        first_id, second_id = first.id, second.id

    with db.new_session() as session:
        session.add(TaskDependency(task_id=first_id, depends_on_id=second_id))
        with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
            session.commit()


def test_on_delete_set_null_for_milestone_parent_and_agent(app_home):
    with db.new_session() as session:
        project = _new_project()
        session.add(project)
        session.flush()
        agent = Agent(
            project_id=project.id,
            name="worker",
            adapter="generic_process",
            provider="other",
            match_kind="none",
            status="unknown",
            status_source="unknown",
        )
        milestone = Milestone(project_id=project.id, title="milestone")
        parent = Task(project_id=project.id, title="parent")
        session.add_all([agent, milestone, parent])
        session.flush()
        child = Task(
            project_id=project.id,
            title="child",
            milestone_id=milestone.id,
            parent_id=parent.id,
            agent_id=agent.id,
        )
        session.add(child)
        session.commit()
        child_id, milestone_id, parent_id, agent_id = (
            child.id,
            milestone.id,
            parent.id,
            agent.id,
        )

    with db.new_session() as session:
        session.delete(session.get(Milestone, milestone_id))
        session.delete(session.get(Task, parent_id))
        session.delete(session.get(Agent, agent_id))
        session.commit()

    with db.new_session() as session:
        child = session.get(Task, child_id)
        assert child.milestone_id is None
        assert child.parent_id is None
        assert child.agent_id is None


def test_dependency_edges_cascade_when_target_deleted(app_home):
    with db.new_session() as session:
        project = _new_project()
        session.add(project)
        session.flush()
        first = Milestone(project_id=project.id, title="one")
        second = Milestone(project_id=project.id, title="two")
        task_one = Task(project_id=project.id, title="t-one")
        task_two = Task(project_id=project.id, title="t-two")
        session.add_all([first, second, task_one, task_two])
        session.flush()
        session.add_all(
            [
                MilestoneDependency(milestone_id=first.id, depends_on_id=second.id),
                TaskDependency(task_id=task_one.id, depends_on_id=task_two.id),
            ]
        )
        session.commit()
        second_id, task_two_id = second.id, task_two.id

    with db.new_session() as session:
        session.delete(session.get(Milestone, second_id))
        session.delete(session.get(Task, task_two_id))
        session.commit()

    with db.new_session() as session:
        assert session.query(MilestoneDependency).count() == 0
        assert session.query(TaskDependency).count() == 0


def test_dependency_edges_cascade_when_source_deleted(app_home):
    with db.new_session() as session:
        project = _new_project()
        session.add(project)
        session.flush()
        first = Milestone(project_id=project.id, title="one")
        second = Milestone(project_id=project.id, title="two")
        task_one = Task(project_id=project.id, title="t-one")
        task_two = Task(project_id=project.id, title="t-two")
        session.add_all([first, second, task_one, task_two])
        session.flush()
        session.add_all(
            [
                MilestoneDependency(milestone_id=first.id, depends_on_id=second.id),
                TaskDependency(task_id=task_one.id, depends_on_id=task_two.id),
            ]
        )
        session.commit()
        first_id, task_one_id = first.id, task_one.id

    with db.new_session() as session:
        session.delete(session.get(Milestone, first_id))
        session.delete(session.get(Task, task_one_id))
        session.commit()

    with db.new_session() as session:
        assert session.query(MilestoneDependency).count() == 0
        assert session.query(TaskDependency).count() == 0


def test_deleting_project_cascades_tasks_and_milestones(app_home):
    with db.new_session() as session:
        project = _new_project()
        session.add(project)
        session.flush()
        project_id = project.id
        session.add(Milestone(project_id=project_id, title="milestone"))
        session.add(Task(project_id=project_id, title="task"))
        session.commit()

    with db.new_session() as session:
        session.delete(session.get(Project, project_id))
        session.commit()

    with db.new_session() as session:
        assert session.query(Milestone).count() == 0
        assert session.query(Task).count() == 0


def test_work_graph_indexes_and_composite_keys(app_home):
    inspector = inspect(db.engine)

    milestone_indexes = {index["name"] for index in inspector.get_indexes("milestones")}
    assert "ix_milestones_project_id" in milestone_indexes

    task_indexes = {index["name"] for index in inspector.get_indexes("tasks")}
    assert "ix_tasks_project_id_status" in task_indexes
    task_uniques = inspector.get_unique_constraints("tasks")
    assert {"project_id", "external_key"} in [
        set(constraint["column_names"]) for constraint in task_uniques
    ]

    assert inspector.get_pk_constraint("milestone_dependencies")["constrained_columns"] == [
        "milestone_id",
        "depends_on_id",
    ]
    assert inspector.get_pk_constraint("task_dependencies")["constrained_columns"] == [
        "task_id",
        "depends_on_id",
    ]
