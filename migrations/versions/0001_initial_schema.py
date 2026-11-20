"""Initial AOC schema (all 18 core tables).

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-11-20
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "organism_identity",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("traits", JSON_TYPE, nullable=False),
        sa.Column("memory_agent_id", sa.String(128), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )

    op.create_table(
        "organism_state",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organism_id", sa.Uuid(), sa.ForeignKey("organism_identity.id"), nullable=False, unique=True),
        sa.Column("lifecycle_state", sa.String(32), nullable=False),
        sa.Column("previous_state", sa.String(32), nullable=True),
        sa.Column("state_reason", sa.Text(), nullable=True),
        sa.Column("current_cycle_id", sa.Uuid(), nullable=True),
        sa.Column("cycle_count", sa.Integer(), nullable=False),
        sa.Column("emotion_reserved", JSON_TYPE, nullable=False),
        sa.Column("pause_requested", sa.Boolean(), nullable=False),
        sa.Column("last_wake_at", TS, nullable=True),
        sa.Column("next_wake_at", TS, nullable=True),
        sa.Column("heartbeat_at", TS, nullable=True),
        sa.Column("updated_at", TS, nullable=False),
    )

    op.create_table(
        "drive_states",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organism_id", sa.Uuid(), sa.ForeignKey("organism_identity.id"), nullable=False),
        sa.Column("drive_type", sa.String(32), nullable=False),
        sa.Column("satisfaction", sa.Float(), nullable=False),
        sa.Column("deficit", sa.Float(), nullable=False),
        sa.Column("urgency", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("persistence", sa.Float(), nullable=False),
        sa.Column("trend", sa.Float(), nullable=False),
        sa.Column("estimated_cost", sa.Float(), nullable=False),
        sa.Column("estimated_risk", sa.Float(), nullable=False),
        sa.Column("last_satisfied_at", TS, nullable=True),
        sa.Column("dimensions", JSON_TYPE, nullable=False),
        sa.Column("notes", JSON_TYPE, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.UniqueConstraint("organism_id", "drive_type", name="uq_drive_states_org_type"),
    )

    op.create_table(
        "drive_history",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organism_id", sa.Uuid(), sa.ForeignKey("organism_identity.id"), nullable=False),
        sa.Column("cycle_id", sa.Uuid(), nullable=True),
        sa.Column("phase", sa.String(16), nullable=False),
        sa.Column("drive_type", sa.String(32), nullable=False),
        sa.Column("satisfaction", sa.Float(), nullable=False),
        sa.Column("deficit", sa.Float(), nullable=False),
        sa.Column("urgency", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("persistence", sa.Float(), nullable=False),
        sa.Column("trend", sa.Float(), nullable=False),
        sa.Column("dimensions", JSON_TYPE, nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_drive_history_cycle", "drive_history", ["cycle_id"])

    op.create_table(
        "observation_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("cycle_id", sa.Uuid(), nullable=True),
        sa.Column("phase", sa.String(16), nullable=False),
        sa.Column("observed_at", TS, nullable=False),
        sa.Column("system_state", JSON_TYPE, nullable=False),
        sa.Column("environment_state", JSON_TYPE, nullable=False),
        sa.Column("available_resources", JSON_TYPE, nullable=False),
        sa.Column("active_tasks", JSON_TYPE, nullable=False),
        sa.Column("external_events", JSON_TYPE, nullable=False),
        sa.Column("sensor_status", JSON_TYPE, nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_observation_records_cycle_id", "observation_records", ["cycle_id"])

    op.create_table(
        "world_states",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("key", sa.String(256), nullable=False),
        sa.Column("value", JSON_TYPE, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("is_simulated", sa.Boolean(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.UniqueConstraint("kind", "key", name="uq_world_states_kind_key"),
    )

    op.create_table(
        "active_goals",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organism_id", sa.Uuid(), sa.ForeignKey("organism_identity.id"), nullable=False),
        sa.Column("cycle_id", sa.Uuid(), nullable=False),
        sa.Column("drive_type", sa.String(32), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("success_criteria", JSON_TYPE, nullable=False),
        sa.Column("expected_outcome", JSON_TYPE, nullable=False),
        sa.Column("related_key", sa.String(256), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )
    op.create_index("ix_active_goals_cycle_id", "active_goals", ["cycle_id"])

    op.create_table(
        "goal_history",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("goal_id", sa.Uuid(), sa.ForeignKey("active_goals.id"), nullable=False),
        sa.Column("from_status", sa.String(32), nullable=True),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("expected_outcome", JSON_TYPE, nullable=False),
        sa.Column("actual_outcome", JSON_TYPE, nullable=False),
        sa.Column("measured_change", JSON_TYPE, nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_goal_history_goal_id", "goal_history", ["goal_id"])

    op.create_table(
        "action_plans",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("goal_id", sa.Uuid(), sa.ForeignKey("active_goals.id"), nullable=False),
        sa.Column("cycle_id", sa.Uuid(), nullable=False),
        sa.Column("plan", JSON_TYPE, nullable=False),
        sa.Column("permission_level", sa.String(4), nullable=False),
        sa.Column("policy_verdict", JSON_TYPE, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )
    op.create_index("ix_action_plans_goal_id", "action_plans", ["goal_id"])
    op.create_index("ix_action_plans_cycle_id", "action_plans", ["cycle_id"])

    op.create_table(
        "agent_registry",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("agent_id", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("agent_type", sa.String(32), nullable=False),
        sa.Column("capabilities", JSON_TYPE, nullable=False),
        sa.Column("supported_modes", JSON_TYPE, nullable=False),
        sa.Column("max_concurrency", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("experimental", sa.Boolean(), nullable=False),
        sa.Column("probe", JSON_TYPE, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )

    op.create_table(
        "agent_tasks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("plan_id", sa.Uuid(), sa.ForeignKey("action_plans.id"), nullable=True),
        sa.Column("goal_id", sa.Uuid(), sa.ForeignKey("active_goals.id"), nullable=True),
        sa.Column("cycle_id", sa.Uuid(), nullable=True),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("assigned_agent", sa.String(64), nullable=False),
        sa.Column("expected_output", sa.Text(), nullable=False),
        sa.Column("workspace", sa.String(512), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("resource_budget", JSON_TYPE, nullable=False),
        sa.Column("permission_level", sa.String(4), nullable=False),
        sa.Column("success_criteria", JSON_TYPE, nullable=False),
        sa.Column("verification", JSON_TYPE, nullable=False),
        sa.Column("required_capability", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("tried_agents", JSON_TYPE, nullable=False),
        sa.Column("outcome", JSON_TYPE, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )
    op.create_index("ix_agent_tasks_plan_id", "agent_tasks", ["plan_id"])
    op.create_index("ix_agent_tasks_goal_id", "agent_tasks", ["goal_id"])
    op.create_index("ix_agent_tasks_cycle_id", "agent_tasks", ["cycle_id"])

    op.create_table(
        "agent_executions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("agent_tasks.id"), nullable=False),
        sa.Column("agent_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("completed_at", TS, nullable=True),
        sa.Column("result", JSON_TYPE, nullable=True),
        sa.Column("stdout", sa.Text(), nullable=True),
        sa.Column("stderr", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("resource_usage", JSON_TYPE, nullable=False),
        sa.Column("verification", JSON_TYPE, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )
    op.create_index("ix_agent_executions_task_id", "agent_executions", ["task_id"])

    op.create_table(
        "memory_index",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("memory_type", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(128), nullable=True),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("source", sa.String(128), nullable=False),
        sa.Column("importance", sa.Float(), nullable=False),
        sa.Column("tags", JSON_TYPE, nullable=False),
        sa.Column("cycle_id", sa.Uuid(), nullable=True),
        sa.Column("sync_status", sa.String(16), nullable=False),
        sa.Column("sync_error", sa.Text(), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )
    op.create_index("ix_memory_index_memory_type", "memory_index", ["memory_type"])
    op.create_index("ix_memory_index_content_hash", "memory_index", ["content_hash"])

    op.create_table(
        "cycle_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organism_id", sa.Uuid(), sa.ForeignKey("organism_identity.id"), nullable=False),
        sa.Column("cycle_number", sa.Integer(), nullable=False),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("ended_at", TS, nullable=True),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=True),
        sa.Column("lifecycle_state", sa.String(32), nullable=True),
        sa.Column("observation_id", sa.Uuid(), nullable=True),
        sa.Column("drives_pre", JSON_TYPE, nullable=False),
        sa.Column("drives_post", JSON_TYPE, nullable=False),
        sa.Column("decision", JSON_TYPE, nullable=False),
        sa.Column("brain_output", JSON_TYPE, nullable=False),
        sa.Column("plan", JSON_TYPE, nullable=False),
        sa.Column("execution_summary", JSON_TYPE, nullable=False),
        sa.Column("evaluation", JSON_TYPE, nullable=False),
        sa.Column("state_changes", JSON_TYPE, nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
    )

    op.create_table(
        "decision_logs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("cycle_id", sa.Uuid(), nullable=True),
        sa.Column("decision_type", sa.String(64), nullable=False),
        sa.Column("selected", sa.String(256), nullable=True),
        sa.Column("alternatives", JSON_TYPE, nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("payload", JSON_TYPE, nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_decision_logs_cycle_id", "decision_logs", ["cycle_id"])

    op.create_table(
        "event_logs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("cycle_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("level", sa.String(16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("payload", JSON_TYPE, nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_event_logs_cycle_id", "event_logs", ["cycle_id"])
    op.create_index("ix_event_logs_type_time", "event_logs", ["event_type", "created_at"])

    op.create_table(
        "resource_usage",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("cycle_id", sa.Uuid(), nullable=True),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("seconds", sa.Float(), nullable=False),
        sa.Column("details", JSON_TYPE, nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_resource_usage_cat_time", "resource_usage", ["category", "created_at"])

    op.create_table(
        "system_config",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("value", JSON_TYPE, nullable=False),
        sa.Column("updated_by", sa.String(64), nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )


def downgrade() -> None:
    for table in (
        "system_config",
        "resource_usage",
        "event_logs",
        "decision_logs",
        "cycle_records",
        "memory_index",
        "agent_executions",
        "agent_tasks",
        "agent_registry",
        "action_plans",
        "goal_history",
        "active_goals",
        "world_states",
        "observation_records",
        "drive_history",
        "drive_states",
        "organism_state",
        "organism_identity",
    ):
        op.drop_table(table)
