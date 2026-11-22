"""V4.0 M1 expand-only Skill Runtime, Versioning Metadata & Real Reuse Audit.

Extends `skill_registry` with canonical naming, parent version / supersession pointers,
input/output JSON schemas, reuse counters, and evolution metadata; adds ONE lightweight
table `skill_invocations` to record real Skill Runtime / MCP executions and cross-task reuse.
Fully backward-compatible and reversible.

Revision ID: 0010_v4_skill_rsi_evolution
Revises: 0009_v3_kernel_enhancement
Create Date: 2026-11-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_v4_skill_rsi_evolution"
down_revision = "0009_v3_kernel_enhancement"
branch_labels = None
depends_on = None


def _json_type():
    return sa.JSON().with_variant(postgresql.JSONB, "postgresql")


def _ts_type():
    return sa.DateTime(timezone=True)


def upgrade() -> None:
    # 1. Extend existing `skill_registry` (already has unique (skill_key, version))
    op.add_column("skill_registry", sa.Column("canonical_name", sa.String(length=128), nullable=True))
    op.add_column("skill_registry", sa.Column("parent_version", sa.String(length=32), nullable=True))
    op.add_column("skill_registry", sa.Column("superseded_by", sa.Uuid(), nullable=True))
    op.add_column("skill_registry", sa.Column("input_schema", _json_type(), nullable=True))
    op.add_column("skill_registry", sa.Column("output_schema", _json_type(), nullable=True))
    op.add_column("skill_registry", sa.Column("invocation_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("skill_registry", sa.Column("reuse_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("skill_registry", sa.Column("reuse_success_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("skill_registry", sa.Column("last_invoked_at", _ts_type(), nullable=True))
    op.add_column("skill_registry", sa.Column("evolution_meta", _json_type(), nullable=True))
    op.create_index("ix_skill_registry_canonical_name", "skill_registry", ["canonical_name"])

    # 2. Add ONE lightweight real execution audit table `skill_invocations`
    op.create_table(
        "skill_invocations",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "skill_id",
            sa.Uuid(),
            sa.ForeignKey("skill_registry.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("skill_version", sa.String(length=32), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("cycle_id", sa.Uuid(), nullable=True),
        sa.Column("invocation_source", sa.String(length=24), nullable=False, server_default="skill_first"),
        sa.Column("is_cross_task_reuse", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("passed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("latency_ms", sa.Float(), nullable=False, server_default="0"),
        sa.Column("estimated_saved_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_digest", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("output_excerpt", sa.Text(), nullable=False, server_default=""),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", _ts_type(), nullable=False),
    )
    op.create_index("ix_skill_invocations_skill_time", "skill_invocations", ["skill_id", "created_at"])
    op.create_index("ix_skill_invocations_task", "skill_invocations", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_skill_invocations_task", table_name="skill_invocations")
    op.drop_index("ix_skill_invocations_skill_time", table_name="skill_invocations")
    op.drop_table("skill_invocations")

    op.drop_index("ix_skill_registry_canonical_name", table_name="skill_registry")
    op.drop_column("skill_registry", "evolution_meta")
    op.drop_column("skill_registry", "last_invoked_at")
    op.drop_column("skill_registry", "reuse_success_count")
    op.drop_column("skill_registry", "reuse_count")
    op.drop_column("skill_registry", "invocation_count")
    op.drop_column("skill_registry", "output_schema")
    op.drop_column("skill_registry", "input_schema")
    op.drop_column("skill_registry", "superseded_by")
    op.drop_column("skill_registry", "parent_version")
    op.drop_column("skill_registry", "canonical_name")
