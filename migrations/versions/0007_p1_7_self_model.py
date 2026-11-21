"""P1-7 evidence-driven Self Model, Capability Profiles, Skill Registry, and agent_registry.stats.

Revision ID: 0007_p1_7_self_model
Revises: 0006_p1_5_world_sensors
Create Date: 2026-11-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007_p1_7_self_model"
down_revision = "0006_p1_5_world_sensors"
branch_labels = None
depends_on = None


def _json_type():
    return sa.JSON().with_variant(postgresql.JSONB, "postgresql")


def _ts_type():
    return sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column(
        "agent_registry",
        sa.Column("stats", _json_type(), nullable=False, server_default="{}"),
    )

    op.create_table(
        "self_model_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organism_id", sa.Uuid(), sa.ForeignKey("organism_identity.id"), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("summary_text", sa.Text(), nullable=False),
        sa.Column("profile_snapshot", _json_type(), nullable=False, server_default="{}"),
        sa.Column("trigger_execution_id", sa.Uuid(), nullable=True),
        sa.Column("letta_synced_at", _ts_type(), nullable=True),
        sa.Column("created_at", _ts_type(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("organism_id", "version_number", name="uq_self_model_org_ver"),
    )

    op.create_table(
        "capability_profiles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organism_id", sa.Uuid(), sa.ForeignKey("organism_identity.id"), nullable=False),
        sa.Column("capability_key", sa.String(128), nullable=False),
        sa.Column("maturity_status", sa.String(24), nullable=False, server_default="DECLARED"),
        sa.Column("alpha", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("beta", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("estimated_success_rate", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("confidence_low", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("confidence_high", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("updated_at", _ts_type(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("organism_id", "capability_key", name="uq_cap_profile_org_key"),
    )

    op.create_table(
        "capability_evidence",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "profile_id",
            sa.Uuid(),
            sa.ForeignKey("capability_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("execution_id", sa.Uuid(), sa.ForeignKey("agent_executions.id"), nullable=False),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("agent_tasks.id"), nullable=False),
        sa.Column("outcome", sa.String(24), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("verification_summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", _ts_type(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("profile_id", "execution_id", name="uq_cap_evidence_prof_exec"),
    )

    op.create_table(
        "skill_registry",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("skill_key", sa.String(128), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("capability_key", sa.String(128), nullable=False),
        sa.Column("source_task_id", sa.Uuid(), sa.ForeignKey("agent_tasks.id"), nullable=True),
        sa.Column("source_execution_id", sa.Uuid(), sa.ForeignKey("agent_executions.id"), nullable=True),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("applicable_scenarios", _json_type(), nullable=False, server_default="[]"),
        sa.Column("steps", _json_type(), nullable=False, server_default="[]"),
        sa.Column("artifact_path", sa.String(512), nullable=True),
        sa.Column("artifact_checksum", sa.String(64), nullable=True),
        sa.Column("test_spec", _json_type(), nullable=False, server_default="{}"),
        sa.Column("permission_level", sa.String(4), nullable=False, server_default="L1"),
        sa.Column("status", sa.String(24), nullable=False, server_default="DRAFT"),
        sa.Column("created_at", _ts_type(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", _ts_type(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("skill_key", "version", name="uq_skill_key_ver"),
    )

    op.create_table(
        "skill_validation_results",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "skill_id",
            sa.Uuid(),
            sa.ForeignKey("skill_registry.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("checks", _json_type(), nullable=False, server_default="[]"),
        sa.Column("stdout_excerpt", sa.Text(), nullable=False, server_default=""),
        sa.Column("executed_at", _ts_type(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_skill_val_skill_time", "skill_validation_results", ["skill_id", "executed_at"])


def downgrade() -> None:
    op.drop_index("ix_skill_val_skill_time", table_name="skill_validation_results")
    op.drop_table("skill_validation_results")
    op.drop_table("skill_registry")
    op.drop_table("capability_evidence")
    op.drop_table("capability_profiles")
    op.drop_table("self_model_versions")
    with op.batch_alter_table("agent_registry") as batch_op:
        batch_op.drop_column("stats")
