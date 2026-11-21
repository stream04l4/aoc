"""P1-4 semantic memory: pgvector + pg_trgm extensions, embedding_model_registry, memory_embeddings, and memory_index extensions.

Revision ID: 0005_p1_4_semantic_memory
Revises: 0004_p1_3_runner_recovery
Create Date: 2026-11-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_p1_4_semantic_memory"
down_revision = "0004_p1_3_runner_recovery"
branch_labels = None
depends_on = None


def _ts_type():
    return sa.DateTime(timezone=True)


def _pg_has_extension_available(conn, ext_name: str) -> bool:
    row = conn.execute(
        sa.text("SELECT 1 FROM pg_available_extensions WHERE name = :name"),
        {"name": ext_name},
    ).scalar_one_or_none()
    return row is not None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    has_pgvector = False

    if is_pg:
        if _pg_has_extension_available(bind, "pg_trgm"):
            op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        if _pg_has_extension_available(bind, "vector"):
            op.execute("CREATE EXTENSION IF NOT EXISTS vector")
            has_pgvector = True

    # Extend existing memory_index table (preserves all V1.0 rows; does NOT auto-enqueue historical pending rows!)
    op.add_column(
        "memory_index",
        sa.Column("embedding_status", sa.String(24), nullable=False, server_default="pending"),
    )
    op.add_column(
        "memory_index",
        sa.Column("reliability", sa.Float(), nullable=False, server_default="0.8"),
    )
    op.add_column(
        "memory_index",
        sa.Column("information_gain", sa.Float(), nullable=True),
    )

    if is_pg and _pg_has_extension_available(bind, "pg_trgm"):
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_memory_index_trgm "
            "ON memory_index USING gin ((title || ' ' || summary) gin_trgm_ops)"
        )

    op.create_table(
        "embedding_model_registry",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("model_key", sa.String(128), nullable=False, unique=True),
        sa.Column("provider", sa.String(32), nullable=False, server_default="local_onnx"),
        sa.Column("model_name", sa.String(128), nullable=False),
        sa.Column("model_version", sa.String(64), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("normalized", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", _ts_type(), nullable=False, server_default=sa.func.now()),
    )

    if is_pg and has_pgvector:
        from pgvector.sqlalchemy import Vector

        embedding_col_type = Vector()
    elif is_pg:
        embedding_col_type = postgresql.JSONB()
    else:
        embedding_col_type = sa.JSON()

    op.create_table(
        "memory_embeddings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "memory_id",
            sa.Uuid(),
            sa.ForeignKey("memory_index.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("model_id", sa.Uuid(), sa.ForeignKey("embedding_model_registry.id"), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("embedding", embedding_col_type, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", _ts_type(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("memory_id", "model_id", name="uq_mem_emb_model"),
    )
    op.create_index("ix_mem_emb_model", "memory_embeddings", ["model_id"])


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    op.drop_index("ix_mem_emb_model", table_name="memory_embeddings")
    op.drop_table("memory_embeddings")
    op.drop_table("embedding_model_registry")

    if is_pg:
        op.execute("DROP INDEX IF EXISTS ix_memory_index_trgm")

    with op.batch_alter_table("memory_index") as batch_op:
        batch_op.drop_column("information_gain")
        batch_op.drop_column("reliability")
        batch_op.drop_column("embedding_status")
