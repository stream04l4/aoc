"""P1-5 real digital world perception: world_event_sources, sensor_checkpoints, world_events.

Revision ID: 0006_p1_5_world_sensors
Revises: 0005_p1_4_semantic_memory
Create Date: 2026-11-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006_p1_5_world_sensors"
down_revision = "0005_p1_4_semantic_memory"
branch_labels = None
depends_on = None


def _json_type():
    return sa.JSON().with_variant(postgresql.JSONB, "postgresql")


def _ts_type():
    return sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "world_event_sources",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("source_id", sa.String(128), nullable=False, unique=True),
        sa.Column("sensor_type", sa.String(32), nullable=False),
        sa.Column("name", sa.String(256), nullable=False, server_default=""),
        sa.Column("config", _json_type(), nullable=False, server_default="{}"),
        sa.Column("reliability_score", sa.Float(), nullable=False, server_default="0.7"),
        sa.Column("poll_interval_seconds", sa.Integer(), nullable=False, server_default="900"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("health_status", sa.String(24), nullable=False, server_default="unknown"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("updated_at", _ts_type(), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "sensor_checkpoints",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "source_id",
            sa.String(128),
            sa.ForeignKey("world_event_sources.source_id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("cursor", _json_type(), nullable=False, server_default="{}"),
        sa.Column("last_polled_at", _ts_type(), nullable=True),
        sa.Column("last_success_at", _ts_type(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", _ts_type(), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "world_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "source_id",
            sa.String(128),
            sa.ForeignKey("world_event_sources.source_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("title", sa.String(512), nullable=False, server_default=""),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("source_reliability", sa.Float(), nullable=False, server_default="0.7"),
        sa.Column("importance_score", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("status", sa.String(24), nullable=False, server_default="new"),
        sa.Column("metadata", _json_type(), nullable=False, server_default="{}"),
        sa.Column("observed_at", _ts_type(), nullable=False),
        sa.Column("created_at", _ts_type(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("source_id", "content_hash", name="uq_world_events_dedup"),
    )
    op.create_index("ix_world_events_status_time", "world_events", ["status", "observed_at"])


def downgrade() -> None:
    op.drop_index("ix_world_events_status_time", table_name="world_events")
    op.drop_table("world_events")
    op.drop_table("sensor_checkpoints")
    op.drop_table("world_event_sources")
