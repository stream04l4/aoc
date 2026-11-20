"""P1-2 Durable Task State Machine, TaskFinalizer, and Transactional Outbox/Inbox.

Revision ID: 0003_p1_2_durable_tasks
Revises: 0002_p1_1_security_budget
Create Date: 2026-11-20
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_p1_2_durable_tasks"
down_revision: Union[str, None] = "0002_p1_1_security_budget"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # Additive columns on agent_tasks & agent_executions (backward compatible with V1.0 snapshot)
    op.add_column("agent_tasks", sa.Column("state_version", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("agent_tasks", sa.Column("finalized_at", TS, nullable=True))
    op.add_column("agent_tasks", sa.Column("idempotency_key", sa.String(128), nullable=True))

    op.add_column("agent_executions", sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("agent_executions", sa.Column("idempotency_key", sa.String(128), nullable=True))
    op.add_column("agent_executions", sa.Column("runner_id", sa.String(64), nullable=True))
    op.add_column("agent_executions", sa.Column("process_identity", JSON_TYPE, nullable=False, server_default="{}"))
    op.add_column("agent_executions", sa.Column("thread_id", sa.String(128), nullable=True))
    op.add_column("agent_executions", sa.Column("finalized_at", TS, nullable=True))

    # 1. task_lifecycle_events
    op.create_table(
        "task_lifecycle_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("agent_tasks.id"), nullable=False),
        sa.Column("execution_id", sa.Uuid(), sa.ForeignKey("agent_executions.id"), nullable=True),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("from_status", sa.String(32), nullable=True),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("payload", JSON_TYPE, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.UniqueConstraint("task_id", "state_version", name="uq_task_lifecycle_ver"),
        sa.UniqueConstraint("idempotency_key", name="uq_task_lifecycle_idem"),
    )
    op.create_index("ix_task_lifecycle_task", "task_lifecycle_events", ["task_id"])

    # 2. outbox_events
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("aggregate_type", sa.String(32), nullable=True),
        sa.Column("aggregate_id", sa.Uuid(), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("payload", JSON_TYPE, nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("next_retry_at", TS, nullable=False),
        sa.Column("locked_at", TS, nullable=True),
        sa.Column("dispatched_at", TS, nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", TS, nullable=False),
    )
    if is_pg:
        op.create_index(
            "ix_outbox_dispatch",
            "outbox_events",
            ["status", "next_retry_at"],
            postgresql_where=sa.text("status IN ('pending', 'processing')"),
        )
    else:
        op.create_index("ix_outbox_dispatch", "outbox_events", ["status", "next_retry_at"])

    # 3. inbox_events
    op.create_table(
        "inbox_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("consumer_name", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=True),
        sa.Column("processed_at", TS, nullable=False),
        sa.Column("result_summary", JSON_TYPE, nullable=False),
        sa.UniqueConstraint("consumer_name", "idempotency_key", name="uq_inbox_consumer_key"),
    )


def downgrade() -> None:
    op.drop_table("inbox_events")

    op.drop_index("ix_outbox_dispatch", table_name="outbox_events")
    op.drop_table("outbox_events")

    op.drop_index("ix_task_lifecycle_task", table_name="task_lifecycle_events")
    op.drop_table("task_lifecycle_events")

    with op.batch_alter_table("agent_executions") as batch_op:
        batch_op.drop_column("finalized_at")
        batch_op.drop_column("thread_id")
        batch_op.drop_column("process_identity")
        batch_op.drop_column("runner_id")
        batch_op.drop_column("idempotency_key")
        batch_op.drop_column("attempt_number")

    with op.batch_alter_table("agent_tasks") as batch_op:
        batch_op.drop_column("idempotency_key")
        batch_op.drop_column("finalized_at")
        batch_op.drop_column("state_version")
