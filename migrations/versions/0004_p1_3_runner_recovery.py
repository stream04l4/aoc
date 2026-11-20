"""P1-3 host systemd Agent Runner registry and crash recovery audit records.

Revision ID: 0004_p1_3_runner_recovery
Revises: 0003_p1_2_durable_tasks
Create Date: 2026-11-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_p1_3_runner_recovery"
down_revision = "0003_p1_2_durable_tasks"
branch_labels = None
depends_on = None


def _json_type():
    return sa.JSON().with_variant(postgresql.JSONB, "postgresql")


def _ts_type():
    return sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "runner_registry",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("runner_id", sa.String(64), nullable=False, unique=True),
        sa.Column("socket_path", sa.String(256), nullable=False),
        sa.Column("host_boot_id", sa.String(64), nullable=True),
        sa.Column("systemd_unit", sa.String(128), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="online"),
        sa.Column("capabilities", _json_type(), nullable=False, server_default="{}"),
        sa.Column("last_heartbeat_at", _ts_type(), nullable=True),
        sa.Column("updated_at", _ts_type(), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "execution_recovery_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("execution_id", sa.Uuid(), sa.ForeignKey("agent_executions.id"), nullable=False),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("agent_tasks.id"), nullable=False),
        sa.Column("runner_id", sa.String(64), nullable=True),
        sa.Column("trigger", sa.String(32), nullable=False, server_default="startup"),
        sa.Column("observed_identity", _json_type(), nullable=False, server_default="{}"),
        sa.Column("previous_status", sa.String(32), nullable=True),
        sa.Column("resolved_status", sa.String(32), nullable=True),
        sa.Column("action_taken", sa.String(64), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", _ts_type(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_recovery_exec", "execution_recovery_records", ["execution_id"])


def downgrade() -> None:
    op.drop_index("ix_recovery_exec", table_name="execution_recovery_records")
    op.drop_table("execution_recovery_records")
    op.drop_table("runner_registry")
