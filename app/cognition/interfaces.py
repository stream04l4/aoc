"""Cognition / Brain interfaces (task book section 6).

The Brain is strictly advisory: it receives immutable snapshots and returns
Pydantic-validated proposals. It NEVER holds DB sessions, policy mutators,
or agent execution handles.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.core.enums import MemoryType, PermissionLevel
from app.drives.interfaces import DriveDecision, DriveState, KnowledgeGap, clamp01
from app.perception.interfaces import Observation


class BrainError(RuntimeError):
    """Base error raised by the Brain / LLM adapter."""


class BrainSchemaError(BrainError):
    """Raised when the LLM fails to produce schema-valid output after retries."""


class LLMUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: float = 0.0
    model: str = ""


class AssessmentResult(BaseModel):
    summary: str
    anomalies: list[str] = Field(default_factory=list)
    knowledge_gaps: list[KnowledgeGap] = Field(default_factory=list)
    capability_gaps: list[str] = Field(default_factory=list)
    recommended_focus: str = ""
    confidence: float = 0.7
    usage: LLMUsage = Field(default_factory=LLMUsage)

    @field_validator("confidence")
    @classmethod
    def _clamp_conf(cls, v: float) -> float:
        return clamp01(v)


class GoalProposal(BaseModel):
    should_act: bool = True
    title: str = ""
    description: str = ""
    drive_type: str = ""
    related_key: str | None = None
    success_criteria: list[str] = Field(default_factory=list)
    expected_outcome: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    usage: LLMUsage = Field(default_factory=LLMUsage)


class VerificationSpec(BaseModel):
    """Deterministic check performed by AOC after an external agent finishes."""

    kind: str  # non_empty_output | file_exists | min_output_chars | contains_keywords
    target: str = ""
    min_value: int = 1
    keywords: list[str] = Field(default_factory=list)


class ActionPlan(BaseModel):
    should_execute: bool = True
    goal_title: str = ""
    objective: str = ""
    description: str = ""
    assigned_agent: str = "codex"
    required_capability: str = "general_task"
    expected_output: str = ""
    timeout_seconds: int = 300
    permission_level: str = PermissionLevel.L0.value
    success_criteria: list[str] = Field(default_factory=list)
    verification: list[VerificationSpec] = Field(default_factory=list)
    # Advisory prediction ONLY — never directly updates drive satisfaction (task book 9.1)
    expected_improvement: float = 0.0
    rationale: str = ""
    usage: LLMUsage = Field(default_factory=LLMUsage)

    @field_validator("permission_level")
    @classmethod
    def _validate_level(cls, v: str) -> str:
        val = str(v).upper().strip()
        if val not in {lv.value for lv in PermissionLevel}:
            return PermissionLevel.L2.value  # conservative fallback for unknown levels
        return val

    @field_validator("timeout_seconds")
    @classmethod
    def _clamp_timeout(cls, v: int) -> int:
        return max(15, min(int(v), 3600))

    @field_validator("expected_improvement")
    @classmethod
    def _clamp_imp(cls, v: float) -> float:
        return max(-1.0, min(1.0, float(v)))


class CognitiveContext(BaseModel):
    observation: Observation
    drives: list[DriveState]
    decision: DriveDecision
    assessment: AssessmentResult | None = None
    goal: GoalProposal | None = None
    memories: list[dict[str, Any]] = Field(default_factory=list)
    available_agents: list[dict[str, Any]] = Field(default_factory=list)
    available_tools: list[dict[str, Any]] = Field(default_factory=list)
    budget_remaining: dict[str, Any] = Field(default_factory=dict)
    active_goals: list[dict[str, Any]] = Field(default_factory=list)


class EvaluationContext(BaseModel):
    goal: dict[str, Any]
    plan: dict[str, Any]
    execution: dict[str, Any]
    verification_passed: bool
    verification_details: dict[str, Any] = Field(default_factory=dict)
    pre_observation: Observation
    post_observation: Observation


class EvaluationResult(BaseModel):
    goal_achieved: bool = False
    summary: str
    actual_outcome: dict[str, Any] = Field(default_factory=dict)
    information_gain: float = 0.0
    learned_facts: list[str] = Field(default_factory=list)
    acquired_skill: str | None = None
    resolved_topic: str | None = None
    should_persist_memory: bool = False
    memory_type: str = MemoryType.EPISODIC.value
    memory_importance: float = 0.5
    notes: str = ""
    usage: LLMUsage = Field(default_factory=LLMUsage)

    @field_validator("information_gain", "memory_importance")
    @classmethod
    def _clamp_unit(cls, v: float) -> float:
        return clamp01(v)

    @field_validator("memory_type")
    @classmethod
    def _validate_mem_type(cls, v: str) -> str:
        val = str(v).lower().strip()
        if val not in {m.value for m in MemoryType}:
            return MemoryType.EPISODIC.value
        return val


class BrainInterface(ABC):
    """Unified Brain contract (task book section 6)."""

    @abstractmethod
    async def assess(
        self,
        observation: Observation,
        memories: list[dict[str, Any]],
        drives: list[DriveState],
    ) -> AssessmentResult: ...

    @abstractmethod
    async def generate_goal(self, context: CognitiveContext) -> GoalProposal: ...

    @abstractmethod
    async def plan(self, context: CognitiveContext) -> ActionPlan: ...

    @abstractmethod
    async def evaluate(self, context: EvaluationContext) -> EvaluationResult: ...
