"""initial schema: projects, agents, agent_sessions, settings

Revision ID: 0001
Revises:
Create Date: 2026-10-25

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("path_key", sa.Text(), nullable=False),
        sa.Column("is_git", sa.Boolean(), nullable=False),
        sa.Column("default_branch", sa.Text(), nullable=True),
        sa.Column("current_branch", sa.Text(), nullable=True),
        sa.Column("watch_enabled", sa.Boolean(), nullable=False),
        sa.Column("watch_rules_json", sa.Text(), nullable=False),
        sa.Column("in_project_reporting", sa.Boolean(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("archived_at", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("path"),
        sa.UniqueConstraint("path_key"),
    )
    op.create_index("ix_projects_path_key", "projects", ["path_key"], unique=False)

    op.create_table(
        "agents",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("adapter", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("match_kind", sa.Text(), nullable=False),
        sa.Column("match_value", sa.Text(), nullable=True),
        sa.Column("confirmed_pid", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("status_source", sa.Text(), nullable=False),
        sa.Column("last_activity_at", sa.Text(), nullable=True),
        sa.Column("last_event_message", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agents_project_id", "agents", ["project_id"], unique=False)

    op.create_table(
        "agent_sessions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("external_key", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", sa.Text(), nullable=False),
        sa.Column("ended_at", sa.Text(), nullable=True),
        sa.Column("last_heartbeat_at", sa.Text(), nullable=True),
        sa.Column("heartbeat_interval_s", sa.Integer(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_id", "external_key"),
    )

    op.create_table(
        "settings",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value_json", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("settings")
    op.drop_table("agent_sessions")
    op.drop_index("ix_agents_project_id", table_name="agents")
    op.drop_table("agents")
    op.drop_index("ix_projects_path_key", table_name="projects")
    op.drop_table("projects")
