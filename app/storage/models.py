"""SQLAlchemy 2.x ORM models (task book section 12).

All primary entities use UUID keys; every state change carries a timestamp.
No secrets are ever stored in these tables.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid


def _now() -> datetime:
    return datetime.now(timezone.utc)


JsonType = JSON().with_variant(JSONB(), "postgresql")
TS = DateTime(timezone=True)


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


# ── identity & state ────────────────────────────────────────────────────────
class OrganismIdentity(Base):
    __tablename__ = "organism_identity"
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(128), unique=True)
    version: Mapped[str] = mapped_column(String(32))
    traits: Mapped[dict] = mapped_column(JsonType, default=dict)
    memory_agent_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=_now)
    updated_at: Mapped[datetime] = mapped_column(TS, default=_now, onupdate=_now)


class OrganismState(Base):
    __tablename__ = "organism_state"
    id: Mapped[uuid.UUID] = _uuid_pk()
    organism_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organism_identity.id"), unique=True)
    lifecycle_state: Mapped[str] = mapped_column(String(32))
    previous_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    state_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_cycle_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    cycle_count: Mapped[int] = mapped_column(Integer, default=0)
    emotion_reserved: Mapped[dict] = mapped_column(JsonType, default=dict)  # reserved, V1 unused
    pause_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    last_wake_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    next_wake_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(TS, default=_now, onupdate=_now)


# ── drives ──────────────────────────────────────────────────────────────────
class DriveStateRow(Base):
    __tablename__ = "drive_states"
    __table_args__ = (UniqueConstraint("organism_id", "drive_type"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    organism_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organism_identity.id"))
    drive_type: Mapped[str] = mapped_column(String(32))
    satisfaction: Mapped[float] = mapped_column(Float)
    deficit: Mapped[float] = mapped_column(Float)
    urgency: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    persistence: Mapped[float] = mapped_column(Float)
    trend: Mapped[float] = mapped_column(Float)
    estimated_cost: Mapped[float] = mapped_column(Float)
    estimated_risk: Mapped[float] = mapped_column(Float)
    last_satisfied_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    dimensions: Mapped[dict] = mapped_column(JsonType, default=dict)
    notes: Mapped[list] = mapped_column(JsonType, default=list)
    updated_at: Mapped[datetime] = mapped_column(TS, default=_now)


class DriveHistory(Base):
    __tablename__ = "drive_history"
    __table_args__ = (Index("ix_drive_history_cycle", "cycle_id"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    organism_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organism_identity.id"))
    cycle_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    phase: Mapped[str] = mapped_column(String(16))  # pre | post
    drive_type: Mapped[str] = mapped_column(String(32))
    satisfaction: Mapped[float] = mapped_column(Float)
    deficit: Mapped[float] = mapped_column(Float)
    urgency: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    persistence: Mapped[float] = mapped_column(Float)
    trend: Mapped[float] = mapped_column(Float)
    dimensions: Mapped[dict] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = mapped_column(TS, default=_now)


# ── perception ──────────────────────────────────────────────────────────────
class ObservationRecord(Base):
    __tablename__ = "observation_records"
    id: Mapped[uuid.UUID] = _uuid_pk()
    cycle_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    phase: Mapped[str] = mapped_column(String(16), default="pre")
    observed_at: Mapped[datetime] = mapped_column(TS)
    system_state: Mapped[dict] = mapped_column(JsonType, default=dict)
    environment_state: Mapped[dict] = mapped_column(JsonType, default=dict)
    available_resources: Mapped[dict] = mapped_column(JsonType, default=dict)
    active_tasks: Mapped[list] = mapped_column(JsonType, default=list)
    external_events: Mapped[list] = mapped_column(JsonType, default=list)
    sensor_status: Mapped[dict] = mapped_column(JsonType, default=dict)
    confidence: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(TS, default=_now)


class WorldState(Base):
    """Key/value view of the digital environment (knowledge topics, capabilities, simulated inputs)."""

    __tablename__ = "world_states"
    __table_args__ = (UniqueConstraint("kind", "key"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[str] = mapped_column(String(32))  # knowledge_topic | capability | simulation | fact
    key: Mapped[str] = mapped_column(String(256))
    value: Mapped[dict] = mapped_column(JsonType, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="open")
    source: Mapped[str] = mapped_column(String(64), default="environment")
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(TS, default=_now)
    updated_at: Mapped[datetime] = mapped_column(TS, default=_now, onupdate=_now)


# ── goals & plans ───────────────────────────────────────────────────────────
class ActiveGoal(Base):
    __tablename__ = "active_goals"
    id: Mapped[uuid.UUID] = _uuid_pk()
    organism_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organism_identity.id"))
    cycle_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    drive_type: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(512))
    description: Mapped[str] = mapped_column(Text, default="")
    success_criteria: Mapped[list] = mapped_column(JsonType, default=list)
    expected_outcome: Mapped[dict] = mapped_column(JsonType, default=dict)
    related_key: Mapped[str | None] = mapped_column(String(256), nullable=True)  # e.g. knowledge topic key
    status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(TS, default=_now)
    updated_at: Mapped[datetime] = mapped_column(TS, default=_now, onupdate=_now)


class GoalHistory(Base):
    __tablename__ = "goal_history"
    id: Mapped[uuid.UUID] = _uuid_pk()
    goal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("active_goals.id"), index=True)
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str] = mapped_column(String(32))
    note: Mapped[str] = mapped_column(Text, default="")
    expected_outcome: Mapped[dict] = mapped_column(JsonType, default=dict)
    actual_outcome: Mapped[dict] = mapped_column(JsonType, default=dict)
    measured_change: Mapped[dict] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = mapped_column(TS, default=_now)


class ActionPlanRow(Base):
    __tablename__ = "action_plans"
    id: Mapped[uuid.UUID] = _uuid_pk()
    goal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("active_goals.id"), index=True)
    cycle_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    plan: Mapped[dict] = mapped_column(JsonType, default=dict)
    permission_level: Mapped[str] = mapped_column(String(4))
    policy_verdict: Mapped[dict] = mapped_column(JsonType, default=dict)
    status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(TS, default=_now)
    updated_at: Mapped[datetime] = mapped_column(TS, default=_now, onupdate=_now)


# ── agents ──────────────────────────────────────────────────────────────────
class AgentRegistryRow(Base):
    __tablename__ = "agent_registry"
    id: Mapped[uuid.UUID] = _uuid_pk()
    agent_id: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    agent_type: Mapped[str] = mapped_column(String(32))
    capabilities: Mapped[list] = mapped_column(JsonType, default=list)
    supported_modes: Mapped[list] = mapped_column(JsonType, default=list)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    experimental: Mapped[bool] = mapped_column(Boolean, default=False)
    probe: Mapped[dict] = mapped_column(JsonType, default=dict)
    updated_at: Mapped[datetime] = mapped_column(TS, default=_now, onupdate=_now)


class AgentTaskRow(Base):
    __tablename__ = "agent_tasks"
    id: Mapped[uuid.UUID] = _uuid_pk()  # task_id
    plan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("action_plans.id"), nullable=True, index=True)
    goal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("active_goals.id"), nullable=True, index=True)
    cycle_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    objective: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    assigned_agent: Mapped[str] = mapped_column(String(64))
    expected_output: Mapped[str] = mapped_column(Text, default="")
    workspace: Mapped[str] = mapped_column(String(512))
    timeout_seconds: Mapped[int] = mapped_column(Integer)
    resource_budget: Mapped[dict] = mapped_column(JsonType, default=dict)
    permission_level: Mapped[str] = mapped_column(String(4))
    success_criteria: Mapped[list] = mapped_column(JsonType, default=list)
    verification: Mapped[list] = mapped_column(JsonType, default=list)
    required_capability: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32))
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=1)
    tried_agents: Mapped[list] = mapped_column(JsonType, default=list)
    outcome: Mapped[dict] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = mapped_column(TS, default=_now)
    updated_at: Mapped[datetime] = mapped_column(TS, default=_now, onupdate=_now)

    executions: Mapped[list["AgentExecutionRow"]] = relationship(back_populates="task")


class AgentExecutionRow(Base):
    __tablename__ = "agent_executions"
    id: Mapped[uuid.UUID] = _uuid_pk()  # execution_id
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_tasks.id"), index=True)
    agent_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(TS, default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    result: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    stdout: Mapped[str | None] = mapped_column(Text, nullable=True)
    stderr: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    resource_usage: Mapped[dict] = mapped_column(JsonType, default=dict)
    verification: Mapped[dict] = mapped_column(JsonType, default=dict)
    updated_at: Mapped[datetime] = mapped_column(TS, default=_now, onupdate=_now)

    task: Mapped[AgentTaskRow] = relationship(back_populates="executions")


# ── memory index ────────────────────────────────────────────────────────────
class MemoryIndex(Base):
    __tablename__ = "memory_index"
    id: Mapped[uuid.UUID] = _uuid_pk()
    memory_type: Mapped[str] = mapped_column(String(32), index=True)
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)  # Letta passage id
    title: Mapped[str] = mapped_column(String(512), default="")
    summary: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(128))
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    tags: Mapped[list] = mapped_column(JsonType, default=list)
    cycle_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    sync_status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|synced|failed
    sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=_now)
    updated_at: Mapped[datetime] = mapped_column(TS, default=_now, onupdate=_now)


# ── audit ───────────────────────────────────────────────────────────────────
class CycleRecord(Base):
    __tablename__ = "cycle_records"
    id: Mapped[uuid.UUID] = _uuid_pk()  # cycle_id
    organism_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organism_identity.id"))
    cycle_number: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(TS, default=_now)
    ended_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    phase: Mapped[str] = mapped_column(String(32))
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lifecycle_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    observation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    drives_pre: Mapped[dict] = mapped_column(JsonType, default=dict)
    drives_post: Mapped[dict] = mapped_column(JsonType, default=dict)
    decision: Mapped[dict] = mapped_column(JsonType, default=dict)
    brain_output: Mapped[dict] = mapped_column(JsonType, default=dict)
    plan: Mapped[dict] = mapped_column(JsonType, default=dict)
    execution_summary: Mapped[dict] = mapped_column(JsonType, default=dict)
    evaluation: Mapped[dict] = mapped_column(JsonType, default=dict)
    state_changes: Mapped[dict] = mapped_column(JsonType, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class DecisionLog(Base):
    __tablename__ = "decision_logs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    cycle_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    decision_type: Mapped[str] = mapped_column(String(64))
    selected: Mapped[str | None] = mapped_column(String(256), nullable=True)
    alternatives: Mapped[list] = mapped_column(JsonType, default=list)
    reason: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = mapped_column(TS, default=_now)


class EventLog(Base):
    __tablename__ = "event_logs"
    __table_args__ = (Index("ix_event_logs_type_time", "event_type", "created_at"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    cycle_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    level: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = mapped_column(TS, default=_now)


class ResourceUsage(Base):
    __tablename__ = "resource_usage"
    __table_args__ = (Index("ix_resource_usage_cat_time", "category", "created_at"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    cycle_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    category: Mapped[str] = mapped_column(String(32))  # llm | agent_task
    amount: Mapped[float] = mapped_column(Float, default=0.0)  # tokens, or 1 per task
    seconds: Mapped[float] = mapped_column(Float, default=0.0)
    details: Mapped[dict] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = mapped_column(TS, default=_now)


class SystemConfig(Base):
    __tablename__ = "system_config"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict] = mapped_column(JsonType, default=dict)
    updated_by: Mapped[str] = mapped_column(String(64), default="operator")
    updated_at: Mapped[datetime] = mapped_column(TS, default=_now, onupdate=_now)
