"""V5.0 M1 shared control-plane tables (`instance_registry`, `evolution_proposals`, `lineage_records`).

Created only in PostgreSQL `public` schema (or default SQLite schema in unit tests).
Idempotent when migrating isolated `aoc_inst_*` schemas: skips re-creating shared control-plane tables.

Revision ID: 0011_v5_evolution_control_plane
Revises: 0010_v4_skill_rsi_evolution
Create Date: 2026-11-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011_v5_evolution_control_plane"
down_revision = "0010_v4_skill_rsi_evolution"
branch_labels = None
depends_on = None


def _json_type():
    return sa.JSON().with_variant(postgresql.JSONB, "postgresql")


def _ts_type():
    return sa.DateTime(timezone=True)


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    target_schema = "public" if is_pg else None
    insp = sa.inspect(bind)

    if not insp.has_table("instance_registry", schema=target_schema):
        op.create_table(
            "instance_registry",
            sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
            sa.Column("instance_key", sa.String(length=64), nullable=False, unique=True),
            sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "parent_instance_id",
                sa.Uuid(),
                sa.ForeignKey(f"{'public.' if is_pg else ''}instance_registry.id"),
                nullable=True,
            ),
            sa.Column("proposal_id", sa.Uuid(), nullable=True),
            sa.Column("role", sa.String(length=24), nullable=False, server_default="CANDIDATE"),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="INITIALIZING"),
            sa.Column("db_schema", sa.String(length=64), nullable=False, unique=True),
            sa.Column("letta_agent_name", sa.String(length=128), nullable=False),
            sa.Column("letta_agent_id", sa.String(length=128), nullable=True),
            sa.Column("git_branch", sa.String(length=128), nullable=True),
            sa.Column("git_commit", sa.String(length=64), nullable=True),
            sa.Column("repo_worktree_path", sa.String(length=512), nullable=True),
            sa.Column("workspace_root", sa.String(length=512), nullable=False),
            sa.Column("container_name", sa.String(length=128), nullable=True),
            sa.Column("api_port", sa.Integer(), nullable=True),
            sa.Column("blank_slate_verified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("resource_snapshot", _json_type(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("created_at", _ts_type(), nullable=False, server_default=sa.func.now()),
            sa.Column("retired_at", _ts_type(), nullable=True),
            sa.Column("cleaned_at", _ts_type(), nullable=True),
            sa.Column("updated_at", _ts_type(), nullable=False, server_default=sa.func.now()),
            schema=target_schema,
        )
        op.create_index("ix_inst_reg_role_status", "instance_registry", ["role", "status"], schema=target_schema)
        op.create_index("ix_inst_reg_gen", "instance_registry", ["generation"], schema=target_schema)

    if not insp.has_table("evolution_proposals", schema=target_schema):
        op.create_table(
            "evolution_proposals",
            sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
            sa.Column("proposal_key", sa.String(length=64), nullable=False, unique=True),
            sa.Column(
                "parent_instance_id",
                sa.Uuid(),
                sa.ForeignKey(f"{'public.' if is_pg else ''}instance_registry.id"),
                nullable=False,
            ),
            sa.Column("target_generation", sa.Integer(), nullable=False),
            sa.Column("trigger_type", sa.String(length=32), nullable=False),
            sa.Column("trigger_reason", sa.Text(), nullable=False),
            sa.Column("entropy_snapshot", _json_type(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("hypothesis", sa.Text(), nullable=False),
            sa.Column("genome_delta", _json_type(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("stage", sa.String(length=32), nullable=False, server_default="STAGE0_DRAFT"),
            sa.Column("stage1_report", _json_type(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("approval_id", sa.Uuid(), nullable=True),
            sa.Column("approved_by", sa.String(length=128), nullable=True),
            sa.Column("approved_at", _ts_type(), nullable=True),
            sa.Column(
                "arena_budget",
                _json_type(),
                nullable=False,
                server_default=sa.text("""'{"max_tokens": 500000, "max_seconds": 1200}'"""),
            ),
            sa.Column("arena_result", _json_type(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("candidate_instance_id", sa.Uuid(), nullable=True),
            sa.Column("created_at", _ts_type(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", _ts_type(), nullable=False, server_default=sa.func.now()),
            schema=target_schema,
        )
        op.create_index("ix_evo_prop_stage_time", "evolution_proposals", ["stage", "created_at"], schema=target_schema)
        op.create_index("ix_evo_prop_parent", "evolution_proposals", ["parent_instance_id"], schema=target_schema)

    if not insp.has_table("lineage_records", schema=target_schema):
        op.create_table(
            "lineage_records",
            sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
            sa.Column("generation", sa.Integer(), nullable=False),
            sa.Column("parent_instance_id", sa.Uuid(), nullable=True),
            sa.Column("offspring_instance_id", sa.Uuid(), nullable=False),
            sa.Column(
                "proposal_id",
                sa.Uuid(),
                sa.ForeignKey(f"{'public.' if is_pg else ''}evolution_proposals.id"),
                nullable=False,
            ),
            sa.Column("outcome", sa.String(length=24), nullable=False),
            sa.Column("safety_passed", sa.Boolean(), nullable=False),
            sa.Column("parent_fitness", sa.Float(), nullable=True),
            sa.Column("offspring_fitness", sa.Float(), nullable=True),
            sa.Column("fitness_delta", sa.Float(), nullable=True),
            sa.Column("fitness_breakdown", _json_type(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("drive_metric_changes", _json_type(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("lesson_summary", _json_type(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("created_at", _ts_type(), nullable=False, server_default=sa.func.now()),
            schema=target_schema,
        )
        op.create_index("ix_lineage_gen_outcome", "lineage_records", ["generation", "outcome"], schema=target_schema)
        op.create_index("ix_lineage_proposal", "lineage_records", ["proposal_id"], schema=target_schema)


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    if is_pg:
        cur_schema = bind.execute(sa.text("SELECT current_schema()")).scalar_one_or_none()
        if cur_schema != "public":
            return

    target_schema = "public" if is_pg else None
    insp = sa.inspect(bind)

    if insp.has_table("lineage_records", schema=target_schema):
        op.drop_index("ix_lineage_proposal", table_name="lineage_records", schema=target_schema)
        op.drop_index("ix_lineage_gen_outcome", table_name="lineage_records", schema=target_schema)
        op.drop_table("lineage_records", schema=target_schema)

    if insp.has_table("evolution_proposals", schema=target_schema):
        op.drop_index("ix_evo_prop_parent", table_name="evolution_proposals", schema=target_schema)
        op.drop_index("ix_evo_prop_stage_time", table_name="evolution_proposals", schema=target_schema)
        op.drop_table("evolution_proposals", schema=target_schema)

    if insp.has_table("instance_registry", schema=target_schema):
        op.drop_index("ix_inst_reg_gen", table_name="instance_registry", schema=target_schema)
        op.drop_index("ix_inst_reg_role_status", table_name="instance_registry", schema=target_schema)
        op.drop_table("instance_registry", schema=target_schema)
