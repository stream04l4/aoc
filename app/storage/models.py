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
from sqlalchemy.types import TypeDecorator, Uuid


def _now() -> datetime:
    return datetime.now(timezone.utc)


JsonType = JSON().with_variant(JSONB(), "postgresql")
TS = DateTime(timezone=True)


class VectorType(TypeDecorator):
    """Dialect-aware embedding vector type: pgvector.Vector on PostgreSQL, JSON list[float] on SQLite."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            try:
                from pgvector.sqlalchemy import Vector

                class _SafePgVector(Vector):
                    cache_ok = True

                    def result_processor(self, dialect_, coltype):
                        base_proc = super().result_processor(dialect_, coltype)

                        def process(value):
                            if value is None:
                                return None
                            if isinstance(value, (list, tuple)):
                                return [float(x) for x in value]
                            if hasattr(value, "tolist"):
                                return [float(x) for x in value.tolist()]
                            res = base_proc(value) if base_proc else value
                            return [float(x) for x in res] if res is not None else None

                        return process

                return dialect.type_descriptor(_SafePgVector())
            except ImportError:
                return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return [float(x) for x in value]

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, (list, tuple)):
            return [float(x) for x in value]
        if hasattr(value, "tolist"):
            return [float(x) for x in value.tolist()]
        if isinstance(value, str):
            raw = value.strip().strip("[]")
            if not raw:
                return []
            return [float(part) for part in raw.split(",")]
        return [float(x) for x in value]


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
    quota_reset_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    throttled_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    # V3.0 M1: goal deduplication, cooldown & deterministic IG tracking
    goal_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    duplicate_of: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    cooldown_until: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    predicted_ig: Mapped[float | None] = mapped_column(Float, nullable=True)
    verified_ig: Mapped[float | None] = mapped_column(Float, nullable=True)
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
    plan_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    permission_level: Mapped[str] = mapped_column(String(4))
    policy_verdict: Mapped[dict] = mapped_column(JsonType, default=dict)
    status: Mapped[str] = mapped_column(String(32))
    # V3.0 M1: deterministic ActionPreflight audit
    preflight_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    preflight_reason_codes: Mapped[list | None] = mapped_column(JsonType, nullable=True)
    preflight_checked_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
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
    stats: Mapped[dict] = mapped_column(JsonType, default=dict)
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
    state_version: Mapped[int] = mapped_column(Integer, default=1)
    finalized_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
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
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    runner_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    process_identity: Mapped[dict] = mapped_column(JsonType, default=dict)
    thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    started_at: Mapped[datetime] = mapped_column(TS, default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    finalized_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
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
    embedding_status: Mapped[str] = mapped_column(String(24), default="pending")  # pending|embedded|failed
    reliability: Mapped[float] = mapped_column(Float, default=0.8)
    information_gain: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=_now)
    updated_at: Mapped[datetime] = mapped_column(TS, default=_now, onupdate=_now)


class EmbeddingModelRegistry(Base):
    __tablename__ = "embedding_model_registry"
    id: Mapped[uuid.UUID] = _uuid_pk()
    model_key: Mapped[str] = mapped_column(String(128), unique=True)
    provider: Mapped[str] = mapped_column(String(32), default="local_onnx")  # remote_openai | local_onnx | hash_fallback
    model_name: Mapped[str] = mapped_column(String(128))
    model_version: Mapped[str] = mapped_column(String(64))
    dimensions: Mapped[int] = mapped_column(Integer)
    normalized: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(TS, default=_now)


class MemoryEmbedding(Base):
    __tablename__ = "memory_embeddings"
    __table_args__ = (
        UniqueConstraint("memory_id", "model_id", name="uq_mem_emb_model"),
        Index("ix_mem_emb_model", "model_id"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    memory_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("memory_index.id", ondelete="CASCADE"))
    model_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("embedding_model_registry.id"))
    dimensions: Mapped[int] = mapped_column(Integer)
    embedding: Mapped[list[float]] = mapped_column(VectorType)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TS, default=_now)


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


# ── security, approvals & policy audit (P1-1a / V2.0) ───────────────────────
class ApiCredential(Base):
    __tablename__ = "api_credentials"
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(128), unique=True)
    key_prefix: Mapped[str] = mapped_column(String(16), index=True)
    key_hash: Mapped[str] = mapped_column(String(128), unique=True)
    scopes: Mapped[list] = mapped_column(JsonType, default=list)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=_now)


class ApprovalRecord(Base):
    __tablename__ = "approval_records"
    __table_args__ = (
        Index("ix_approval_records_task_status", "task_id", "status"),
        Index("ix_approval_records_expires", "expires_at"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_tasks.id"))
    plan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("action_plans.id"), nullable=True)
    plan_hash: Mapped[str] = mapped_column(String(64))
    params_digest: Mapped[str] = mapped_column(String(64))
    permission_level: Mapped[str] = mapped_column(String(4))
    nonce: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="PENDING")
    approver: Mapped[str | None] = mapped_column(String(128), nullable=True)
    channel: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(TS)
    decided_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=_now)


class PolicyEvaluation(Base):
    __tablename__ = "policy_evaluations"
    __table_args__ = (
        Index("ix_policy_evaluations_subject", "subject_type", "subject_id"),
        Index("ix_policy_evaluations_created", "created_at"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    cycle_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    subject_type: Mapped[str] = mapped_column(String(32))  # plan | approval_recheck | mcp_tool
    subject_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    requested_level: Mapped[str] = mapped_column(String(4))
    deterministic_level: Mapped[str] = mapped_column(String(4))
    semantic_level: Mapped[str | None] = mapped_column(String(4), nullable=True)
    effective_level: Mapped[str] = mapped_column(String(4))
    verdict: Mapped[str] = mapped_column(String(32))  # allowed | requires_approval | denied
    violations: Mapped[list] = mapped_column(JsonType, default=list)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(TS, default=_now)


# ── durable task lifecycle & transactional outbox/inbox (P1-2) ──────────────
class TaskLifecycleEvent(Base):
    __tablename__ = "task_lifecycle_events"
    __table_args__ = (
        UniqueConstraint("task_id", "state_version", name="uq_task_lifecycle_ver"),
        UniqueConstraint("idempotency_key", name="uq_task_lifecycle_idem"),
        Index("ix_task_lifecycle_task", "task_id"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_tasks.id"))
    execution_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_executions.id"), nullable=True)
    state_version: Mapped[int] = mapped_column(Integer)
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(64), default="orchestrator")
    reason: Mapped[str] = mapped_column(Text, default="")
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[dict] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = mapped_column(TS, default=_now)


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (Index("ix_outbox_dispatch", "status", "next_retry_at"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    event_type: Mapped[str] = mapped_column(String(64))
    aggregate_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    aggregate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    payload: Mapped[dict] = mapped_column(JsonType, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="pending")  # pending|processing|dispatched|dead_letter
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    next_retry_at: Mapped[datetime] = mapped_column(TS, default=_now)
    locked_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=_now)


class InboxEvent(Base):
    __tablename__ = "inbox_events"
    __table_args__ = (UniqueConstraint("consumer_name", "idempotency_key", name="uq_inbox_consumer_key"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    consumer_name: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    event_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    processed_at: Mapped[datetime] = mapped_column(TS, default=_now)
    result_summary: Mapped[dict] = mapped_column(JsonType, default=dict)


# ── host systemd agent runner & crash recovery (P1-3) ───────────────────────
class RunnerRegistryRow(Base):
    __tablename__ = "runner_registry"
    id: Mapped[uuid.UUID] = _uuid_pk()
    runner_id: Mapped[str] = mapped_column(String(64), unique=True)
    socket_path: Mapped[str] = mapped_column(String(256))
    host_boot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    systemd_unit: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="online")  # online | degraded | offline
    capabilities: Mapped[dict] = mapped_column(JsonType, default=dict)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(TS, default=_now, onupdate=_now)


class ExecutionRecoveryRecord(Base):
    __tablename__ = "execution_recovery_records"
    __table_args__ = (Index("ix_recovery_exec", "execution_id"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    execution_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_executions.id"))
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_tasks.id"))
    runner_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trigger: Mapped[str] = mapped_column(String(32), default="startup")  # startup | manual_reconcile | heartbeat_timeout
    observed_identity: Mapped[dict] = mapped_column(JsonType, default=dict)
    previous_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolved_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action_taken: Mapped[str] = mapped_column(String(64))
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(TS, default=_now)


# ── digital world perception sensors (P1-5) ─────────────────────────────────
class WorldEventSource(Base):
    __tablename__ = "world_event_sources"
    id: Mapped[uuid.UUID] = _uuid_pk()
    source_id: Mapped[str] = mapped_column(String(128), unique=True)
    sensor_type: Mapped[str] = mapped_column(String(32))  # rss | git | web | system
    name: Mapped[str] = mapped_column(String(256), default="")
    config: Mapped[dict] = mapped_column(JsonType, default=dict)
    reliability_score: Mapped[float] = mapped_column(Float, default=0.7)
    poll_interval_seconds: Mapped[int] = mapped_column(Integer, default=900)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    health_status: Mapped[str] = mapped_column(String(24), default="unknown")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(TS, default=_now, onupdate=_now)


class SensorCheckpoint(Base):
    __tablename__ = "sensor_checkpoints"
    id: Mapped[uuid.UUID] = _uuid_pk()
    source_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("world_event_sources.source_id", ondelete="CASCADE"), unique=True
    )
    cursor: Mapped[dict] = mapped_column(JsonType, default=dict)
    last_polled_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(TS, default=_now, onupdate=_now)


class WorldEventRow(Base):
    __tablename__ = "world_events"
    __table_args__ = (
        UniqueConstraint("source_id", "content_hash", name="uq_world_events_dedup"),
        Index("ix_world_events_status_time", "status", "observed_at"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    source_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("world_event_sources.source_id", ondelete="CASCADE")
    )
    event_type: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(512), default="")
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    source_reliability: Mapped[float] = mapped_column(Float, default=0.7)
    importance_score: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(24), default="new")  # new | queued_topic | ignored | duplicate
    meta: Mapped[dict] = mapped_column("metadata", JsonType, default=dict)
    observed_at: Mapped[datetime] = mapped_column(TS, default=_now)
    created_at: Mapped[datetime] = mapped_column(TS, default=_now)


# ── dynamic self model, capability profiles & skill registry (P1-7) ─────────
class SelfModelVersionRow(Base):
    __tablename__ = "self_model_versions"
    __table_args__ = (UniqueConstraint("organism_id", "version_number", name="uq_self_model_org_ver"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    organism_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organism_identity.id"))
    version_number: Mapped[int] = mapped_column(Integer)
    summary_text: Mapped[str] = mapped_column(Text)
    profile_snapshot: Mapped[dict] = mapped_column(JsonType, default=dict)
    trigger_execution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    letta_synced_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=_now)


class CapabilityProfileRow(Base):
    __tablename__ = "capability_profiles"
    __table_args__ = (UniqueConstraint("organism_id", "capability_key", name="uq_cap_profile_org_key"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    organism_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organism_identity.id"))
    capability_key: Mapped[str] = mapped_column(String(128))
    maturity_status: Mapped[str] = mapped_column(String(24), default="DECLARED")  # DECLARED|AVAILABLE|ACQUIRED|VALIDATED|DEPRECATED
    alpha: Mapped[float] = mapped_column(Float, default=1.0)
    beta: Mapped[float] = mapped_column(Float, default=1.0)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    estimated_success_rate: Mapped[float] = mapped_column(Float, default=0.5)
    confidence_low: Mapped[float] = mapped_column(Float, default=0.0)
    confidence_high: Mapped[float] = mapped_column(Float, default=1.0)
    updated_at: Mapped[datetime] = mapped_column(TS, default=_now, onupdate=_now)


class CapabilityEvidenceRow(Base):
    __tablename__ = "capability_evidence"
    __table_args__ = (UniqueConstraint("profile_id", "execution_id", name="uq_cap_evidence_prof_exec"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("capability_profiles.id", ondelete="CASCADE"))
    execution_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_executions.id"))
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_tasks.id"))
    outcome: Mapped[str] = mapped_column(String(24))  # verified_success | verified_failure
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    verification_summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(TS, default=_now)


class SkillRegistryRow(Base):
    __tablename__ = "skill_registry"
    __table_args__ = (
        UniqueConstraint("skill_key", "version", name="uq_skill_key_ver"),
        Index("ix_skill_registry_canonical_name", "canonical_name"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    skill_key: Mapped[str] = mapped_column(String(128))
    version: Mapped[str] = mapped_column(String(32))
    capability_key: Mapped[str] = mapped_column(String(128))
    source_task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_tasks.id"), nullable=True)
    source_execution_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_executions.id"), nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    applicable_scenarios: Mapped[list] = mapped_column(JsonType, default=list)
    steps: Mapped[list] = mapped_column(JsonType, default=list)
    artifact_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    artifact_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    test_spec: Mapped[dict] = mapped_column(JsonType, default=dict)
    permission_level: Mapped[str] = mapped_column(String(4), default="L1")
    status: Mapped[str] = mapped_column(String(24), default="DRAFT")  # DRAFT | VALIDATED | DEPRECATED
    # V4.0 M1/M2: canonical naming, versioning, schemas & real reuse counters
    canonical_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    parent_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    input_schema: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    output_schema: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    invocation_count: Mapped[int] = mapped_column(Integer, default=0)
    reuse_count: Mapped[int] = mapped_column(Integer, default=0)
    reuse_success_count: Mapped[int] = mapped_column(Integer, default=0)
    last_invoked_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    evolution_meta: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=_now)
    updated_at: Mapped[datetime] = mapped_column(TS, default=_now, onupdate=_now)


class SkillInvocationRow(Base):
    __tablename__ = "skill_invocations"
    __table_args__ = (
        Index("ix_skill_invocations_skill_time", "skill_id", "created_at"),
        Index("ix_skill_invocations_task", "task_id"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    skill_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("skill_registry.id", ondelete="CASCADE"))
    skill_version: Mapped[str] = mapped_column(String(32))
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    cycle_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    invocation_source: Mapped[str] = mapped_column(String(24), default="skill_first")  # skill_first|mcp|agent|api
    is_cross_task_reuse: Mapped[bool] = mapped_column(Boolean, default=False)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    estimated_saved_tokens: Mapped[int] = mapped_column(Integer, default=0)
    input_digest: Mapped[str] = mapped_column(String(64), default="")
    output_excerpt: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=_now)


class SkillValidationResultRow(Base):
    __tablename__ = "skill_validation_results"
    __table_args__ = (Index("ix_skill_val_skill_time", "skill_id", "executed_at"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    skill_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("skill_registry.id", ondelete="CASCADE"))
    passed: Mapped[bool] = mapped_column(Boolean)
    checks: Mapped[list] = mapped_column(JsonType, default=list)
    stdout_excerpt: Mapped[str] = mapped_column(Text, default="")
    executed_at: Mapped[datetime] = mapped_column(TS, default=_now)


# ── optional bounded affect modulation history (P1-9, disabled by default) ──
class AffectHistoryRow(Base):
    __tablename__ = "affect_history"
    __table_args__ = (Index("ix_affect_history_org_time", "organism_id", "created_at"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    organism_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organism_identity.id"))
    cycle_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    valence: Mapped[float] = mapped_column(Float, default=0.0)
    arousal: Mapped[float] = mapped_column(Float, default=0.2)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    frustration: Mapped[float] = mapped_column(Float, default=0.0)
    boredom: Mapped[float] = mapped_column(Float, default=0.0)
    modulation_applied: Mapped[dict] = mapped_column(JsonType, default=dict)
    trigger_reason: Mapped[str] = mapped_column(Text, default="")
    # V3.0 M1/M2: idempotent trigger key & decay tracking
    trigger_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    decay_applied: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=_now)


# ── V5.0 evolutionary reproduction shared control-plane tables (`public`) ────
class InstanceRegistryRow(Base):
    __tablename__ = "instance_registry"
    __table_args__ = (
        Index("ix_inst_reg_role_status", "role", "status"),
        Index("ix_inst_reg_gen", "generation"),
        {"schema": "public"},
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    instance_key: Mapped[str] = mapped_column(String(64), unique=True)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    parent_instance_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("public.instance_registry.id"), nullable=True
    )
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    role: Mapped[str] = mapped_column(String(24), default="CANDIDATE")  # PRIMARY | CANDIDATE | RETIRED_ARCHIVE
    status: Mapped[str] = mapped_column(String(24), default="INITIALIZING")
    db_schema: Mapped[str] = mapped_column(String(64), unique=True)
    letta_agent_name: Mapped[str] = mapped_column(String(128))
    letta_agent_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    git_branch: Mapped[str | None] = mapped_column(String(128), nullable=True)
    git_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    repo_worktree_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    workspace_root: Mapped[str] = mapped_column(String(512))
    container_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    api_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    blank_slate_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    resource_snapshot: Mapped[dict] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = mapped_column(TS, default=_now)
    retired_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    cleaned_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(TS, default=_now, onupdate=_now)


class EvolutionProposalRow(Base):
    __tablename__ = "evolution_proposals"
    __table_args__ = (
        Index("ix_evo_prop_stage_time", "stage", "created_at"),
        Index("ix_evo_prop_parent", "parent_instance_id"),
        {"schema": "public"},
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    proposal_key: Mapped[str] = mapped_column(String(64), unique=True)
    parent_instance_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("public.instance_registry.id"))
    target_generation: Mapped[int] = mapped_column(Integer)
    trigger_type: Mapped[str] = mapped_column(String(32))  # ENTROPY_PRESSURE | BOTTLENECK | OPERATOR_INITIATED
    trigger_reason: Mapped[str] = mapped_column(Text)
    entropy_snapshot: Mapped[dict] = mapped_column(JsonType, default=dict)
    hypothesis: Mapped[str] = mapped_column(Text)
    genome_delta: Mapped[dict] = mapped_column(JsonType, default=dict)
    stage: Mapped[str] = mapped_column(String(32), default="STAGE0_DRAFT")
    stage1_report: Mapped[dict] = mapped_column(JsonType, default=dict)
    approval_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    arena_budget: Mapped[dict] = mapped_column(
        JsonType, default=lambda: {"max_tokens": 500000, "max_seconds": 1200}
    )
    arena_result: Mapped[dict] = mapped_column(JsonType, default=dict)
    candidate_instance_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=_now)
    updated_at: Mapped[datetime] = mapped_column(TS, default=_now, onupdate=_now)


class LineageRecordRow(Base):
    __tablename__ = "lineage_records"
    __table_args__ = (
        Index("ix_lineage_gen_outcome", "generation", "outcome"),
        Index("ix_lineage_proposal", "proposal_id"),
        {"schema": "public"},
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    generation: Mapped[int] = mapped_column(Integer)
    parent_instance_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    offspring_instance_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    proposal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("public.evolution_proposals.id"))
    outcome: Mapped[str] = mapped_column(String(24))  # PROMOTED | CULLED | ROLLED_BACK
    safety_passed: Mapped[bool] = mapped_column(Boolean)
    parent_fitness: Mapped[float | None] = mapped_column(Float, nullable=True)
    offspring_fitness: Mapped[float | None] = mapped_column(Float, nullable=True)
    fitness_delta: Mapped[float | None] = mapped_column(Float, nullable=True)
    fitness_breakdown: Mapped[dict] = mapped_column(JsonType, default=dict)
    drive_metric_changes: Mapped[dict] = mapped_column(JsonType, default=dict)
    lesson_summary: Mapped[dict] = mapped_column(JsonType, default=dict)  # enforced < 5KB
    created_at: Mapped[datetime] = mapped_column(TS, default=_now)
