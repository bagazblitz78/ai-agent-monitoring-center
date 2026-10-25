"""work graph: milestones, tasks, dependencies

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-25

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "milestones",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("completion_criteria", sa.Text(), nullable=True),
        sa.Column("started_at", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_milestones_project_id", "milestones", ["project_id"], unique=False)

    op.create_table(
        "milestone_dependencies",
        sa.Column("milestone_id", sa.Text(), nullable=False),
        sa.Column("depends_on_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["depends_on_id"], ["milestones.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["milestone_id"], ["milestones.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("milestone_id", "depends_on_id"),
    )

    op.create_table(
        "tasks",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("milestone_id", sa.Text(), nullable=True),
        sa.Column("parent_id", sa.Text(), nullable=True),
        sa.Column("agent_id", sa.Text(), nullable=True),
        sa.Column("external_key", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("priority", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("progress_pct", sa.Integer(), nullable=False),
        sa.Column("verification_status", sa.Text(), nullable=False),
        sa.Column("criteria_json", sa.Text(), nullable=True),
        sa.Column("evidence_json", sa.Text(), nullable=True),
        sa.Column("blocker", sa.Text(), nullable=True),
        sa.Column("error_details", sa.Text(), nullable=True),
        sa.Column("due_at", sa.Text(), nullable=True),
        sa.Column("started_at", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.Text(), nullable=True),
        sa.Column("reported_completed_at", sa.Text(), nullable=True),
        sa.Column("verified_at", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["milestone_id"], ["milestones.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["parent_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "external_key"),
    )
    op.create_index(
        "ix_tasks_project_id_status", "tasks", ["project_id", "status"], unique=False
    )

    op.create_table(
        "task_dependencies",
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("depends_on_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["depends_on_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("task_id", "depends_on_id"),
    )


def downgrade() -> None:
    op.drop_table("task_dependencies")
    op.drop_index("ix_tasks_project_id_status", table_name="tasks")
    op.drop_table("tasks")
    op.drop_table("milestone_dependencies")
    op.drop_index("ix_milestones_project_id", table_name="milestones")
    op.drop_table("milestones")
