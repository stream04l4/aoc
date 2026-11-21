"""V3.0 M1 expand-only kernel enhancement columns (0 new tables).

Adds deterministic preflight audit columns to `action_plans`, goal deduplication /
cooldown / IG tracking columns to `active_goals`, and idempotent trigger / decay
columns to `affect_history`. Fully backward-compatible and reversible.

Revision ID: 0009_v3_kernel_enhancement
Revises: 0008_p1_9_affect
Create Date: 2026-11-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_v3_kernel_enhancement"
down_revision = "0008_p1_9_affect"
branch_labels = None
depends_on = None


def _json_type():
    return sa.JSON().with_variant(postgresql.JSONB, "postgresql")


def _ts_type():
    return sa.DateTime(timezone=True)


def upgrade() -> None:
    # 1. action_plans: ActionPreflight audit fields
    op.add_column("action_plans", sa.Column("preflight_status", sa.String(length=24), nullable=True))
    op.add_column("action_plans", sa.Column("preflight_reason_codes", _json_type(), nullable=True))
    op.add_column("action_plans", sa.Column("preflight_checked_at", _ts_type(), nullable=True))

    # 2. active_goals: goal fingerprint, deduplication, cooldown & IG tracking
    op.add_column("active_goals", sa.Column("goal_fingerprint", sa.String(length=64), nullable=True))
    op.add_column("active_goals", sa.Column("duplicate_of", sa.Uuid(), nullable=True))
    op.add_column("active_goals", sa.Column("cooldown_until", _ts_type(), nullable=True))
    op.add_column("active_goals", sa.Column("predicted_ig", sa.Float(), nullable=True))
    op.add_column("active_goals", sa.Column("verified_ig", sa.Float(), nullable=True))
    op.create_index("ix_active_goals_fingerprint", "active_goals", ["goal_fingerprint"])

    # 3. affect_history: idempotent event key & decay record (prepared for M2)
    op.add_column("affect_history", sa.Column("trigger_event_id", sa.String(length=128), nullable=True))
    op.add_column("affect_history", sa.Column("decay_applied", sa.Float(), nullable=True))
    op.create_index("uq_affect_history_trigger_event", "affect_history", ["trigger_event_id"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_affect_history_trigger_event", table_name="affect_history")
    op.drop_column("affect_history", "decay_applied")
    op.drop_column("affect_history", "trigger_event_id")

    op.drop_index("ix_active_goals_fingerprint", table_name="active_goals")
    op.drop_column("active_goals", "verified_ig")
    op.drop_column("active_goals", "predicted_ig")
    op.drop_column("active_goals", "cooldown_until")
    op.drop_column("active_goals", "duplicate_of")
    op.drop_column("active_goals", "goal_fingerprint")

    op.drop_column("action_plans", "preflight_checked_at")
    op.drop_column("action_plans", "preflight_reason_codes")
    op.drop_column("action_plans", "preflight_status")
