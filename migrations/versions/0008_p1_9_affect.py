"""P1-9 optional bounded Affect modulation layer: affect_history table.

Revision ID: 0008_p1_9_affect
Revises: 0007_p1_7_self_model
Create Date: 2026-11-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_p1_9_affect"
down_revision = "0007_p1_7_self_model"
branch_labels = None
depends_on = None


def _json_type():
    return sa.JSON().with_variant(postgresql.JSONB, "postgresql")


def _ts_type():
    return sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "affect_history",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organism_id", sa.Uuid(), sa.ForeignKey("organism_identity.id"), nullable=False),
        sa.Column("cycle_id", sa.Uuid(), nullable=True),
        sa.Column("valence", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("arousal", sa.Float(), nullable=False, server_default="0.2"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("frustration", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("boredom", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("modulation_applied", _json_type(), nullable=False, server_default="{}"),
        sa.Column("trigger_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", _ts_type(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_affect_history_org_time", "affect_history", ["organism_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_affect_history_org_time", table_name="affect_history")
    op.drop_table("affect_history")
