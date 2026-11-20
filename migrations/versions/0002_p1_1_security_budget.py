"""P1-1a / P1-1 security & budget schema additions.

Revision ID: 0002_p1_1_security_budget
Revises: 0001_initial_schema
Create Date: 2026-11-20
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_p1_1_security_budget"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    # Additive columns on existing V1.0 tables (all nullable for full backward compatibility)
    op.add_column("organism_state", sa.Column("quota_reset_at", TS, nullable=True))
    op.add_column("organism_state", sa.Column("throttled_reason", sa.Text(), nullable=True))
    op.add_column("action_plans", sa.Column("plan_hash", sa.String(64), nullable=True))

    # 1. api_credentials
    op.create_table(
        "api_credentials",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("key_prefix", sa.String(16), nullable=False),
        sa.Column("key_hash", sa.String(128), nullable=False, unique=True),
        sa.Column("scopes", JSON_TYPE, nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("expires_at", TS, nullable=True),
        sa.Column("last_used_at", TS, nullable=True),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_api_credentials_prefix", "api_credentials", ["key_prefix"])

    # 2. approval_records
    op.create_table(
        "approval_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("agent_tasks.id"), nullable=False),
        sa.Column("plan_id", sa.Uuid(), sa.ForeignKey("action_plans.id"), nullable=True),
        sa.Column("plan_hash", sa.String(64), nullable=False),
        sa.Column("params_digest", sa.String(64), nullable=False),
        sa.Column("permission_level", sa.String(4), nullable=False),
        sa.Column("nonce", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("approver", sa.String(128), nullable=True),
        sa.Column("channel", sa.String(32), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("decided_at", TS, nullable=True),
        sa.Column("consumed_at", TS, nullable=True),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_approval_records_task_status", "approval_records", ["task_id", "status"])
    op.create_index("ix_approval_records_expires", "approval_records", ["expires_at"])

    # 3. policy_evaluations
    op.create_table(
        "policy_evaluations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("cycle_id", sa.Uuid(), nullable=True),
        sa.Column("subject_type", sa.String(32), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=True),
        sa.Column("requested_level", sa.String(4), nullable=False),
        sa.Column("deterministic_level", sa.String(4), nullable=False),
        sa.Column("semantic_level", sa.String(4), nullable=True),
        sa.Column("effective_level", sa.String(4), nullable=False),
        sa.Column("verdict", sa.String(32), nullable=False),
        sa.Column("violations", JSON_TYPE, nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_policy_evaluations_subject", "policy_evaluations", ["subject_type", "subject_id"])
    op.create_index("ix_policy_evaluations_created", "policy_evaluations", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_policy_evaluations_created", table_name="policy_evaluations")
    op.drop_index("ix_policy_evaluations_subject", table_name="policy_evaluations")
    op.drop_table("policy_evaluations")

    op.drop_index("ix_approval_records_expires", table_name="approval_records")
    op.drop_index("ix_approval_records_task_status", table_name="approval_records")
    op.drop_table("approval_records")

    op.drop_index("ix_api_credentials_prefix", table_name="api_credentials")
    op.drop_table("api_credentials")

    with op.batch_alter_table("action_plans") as batch_op:
        batch_op.drop_column("plan_hash")
    with op.batch_alter_table("organism_state") as batch_op:
        batch_op.drop_column("throttled_reason")
        batch_op.drop_column("quota_reset_at")
