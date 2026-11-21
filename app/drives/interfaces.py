"""Drive engine interfaces (task book section 4)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.perception.interfaces import Observation, utcnow


def clamp01(v: float) -> float:
    if v != v:  # NaN guard
        return 0.0
    return max(0.0, min(1.0, float(v)))


def clamp_signed(v: float) -> float:
    if v != v:
        return 0.0
    return max(-1.0, min(1.0, float(v)))


class DriveState(BaseModel):
    drive_type: str

    satisfaction: float
    deficit: float = 0.0
    urgency: float
    confidence: float

    persistence: float = 0.0
    trend: float = 0.0  # signed rate of change in [-1, 1]

    estimated_cost: float = 0.0
    estimated_risk: float = 0.0

    updated_at: datetime = Field(default_factory=utcnow)

    # extensions
    last_satisfied_at: datetime | None = None
    dimensions: dict[str, float | None] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)

    @field_validator("satisfaction", "urgency", "confidence", "persistence", "estimated_cost", "estimated_risk", "deficit")
    @classmethod
    def _clamp01(cls, v: float) -> float:
        return clamp01(v)

    @field_validator("trend")
    @classmethod
    def _clamp_trend(cls, v: float) -> float:
        return clamp_signed(v)

    @field_validator("updated_at", "last_satisfied_at")
    @classmethod
    def _ensure_utc(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v

    @model_validator(mode="after")
    def _deficit_rule(self) -> "DriveState":
        # Task book 4.4: deficit = 1 - satisfaction (always derived, never set independently)
        object.__setattr__(self, "deficit", clamp01(1.0 - self.satisfaction))
        return self


class DriveDecision(BaseModel):
    selected_drive: str  # a DriveType value, or "none" for IDLE
    priority_score: float

    alternative_drives: list[str] = Field(default_factory=list)

    reason: str
    constraints: dict[str, Any] = Field(default_factory=dict)

    timestamp: datetime = Field(default_factory=utcnow)

    # extension: full scoring table for auditability
    scores: dict[str, dict[str, Any]] = Field(default_factory=dict)


class KnowledgeGap(BaseModel):
    topic: str
    description: str = ""
    importance: float = 0.5
    confidence: float = 0.5
    source: str = "environment"


class DriveContext(BaseModel):
    """Everything the evaluators may look at. Assembled by the core loop from perception + storage."""

    now: datetime = Field(default_factory=utcnow)
    observation: Observation

    # survival
    recent_execution_statuses: list[str] = Field(default_factory=list)
    budget: dict[str, Any] = Field(default_factory=dict)  # tasks_used, tasks_limit, tokens_used, tokens_limit

    # reproduction (V2.0 P1-7: evidence-driven capabilities & validated skills only)
    capabilities_acquired: list[str] = Field(default_factory=list)
    validated_capabilities: list[str] = Field(default_factory=list)
    validated_skill_count: int | None = None
    procedural_skill_count: int | None = None
    capability_goals_completed_recent: int = 0
    memory_available: bool | None = None
    identity_persisted: bool | None = None
    self_model_synced: bool | None = None
    knowledge_transfer_readiness: float | None = None

    # curiosity
    open_knowledge_gaps: list[KnowledgeGap] = Field(default_factory=list)
    unknown_topics: list[KnowledgeGap] = Field(default_factory=list)
    exploration_attempted_recent: int = 0
    exploration_succeeded_recent: int = 0
    new_knowledge_recent: int | None = None
    last_learning_at: datetime | None = None
    last_information_gain: float | None = None
    last_information_gain_at: datetime | None = None
    gap_assessment_confidence: float | None = None  # confidence of LLM gap assessment, if used


class DriveEvaluation(BaseModel):
    drive_type: str
    satisfaction: float
    urgency: float
    confidence: float
    estimated_cost: float
    estimated_risk: float
    dimensions: dict[str, float | None] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    critical: bool = False


class DriveEvaluator(ABC):
    """One independent evaluator per drive (task book 4.4: no single prompt decides all drives)."""

    drive_type: str

    @abstractmethod
    def evaluate(self, ctx: DriveContext) -> DriveEvaluation: ...


class DriveEngineInterface(ABC):
    @abstractmethod
    def compute_states(
        self, ctx: DriveContext, previous: dict[str, DriveState]
    ) -> dict[str, DriveState]: ...

    @abstractmethod
    def arbitrate(
        self, states: dict[str, DriveState], constraints: dict[str, Any] | None = None
    ) -> DriveDecision: ...
