"""Shared interfaces, DTOs, and strict safety validators for AOC V5.0 evolution."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.perception.interfaces import utcnow
from app.storage.database import validate_db_schema_name

_VALID_INSTANCE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")
_DROPPABLE_SCHEMA_RE = re.compile(r"^aoc_inst_[a-z0-9_]{4,32}$")

MAX_LESSON_SUMMARY_BYTES = 4800  # Strictly below 5 KB (5120 bytes) per V5 task book


class InstanceRole(StrEnum):
    PRIMARY = "PRIMARY"
    CANDIDATE = "CANDIDATE"
    RETIRED_ARCHIVE = "RETIRED_ARCHIVE"


class InstanceStatus(StrEnum):
    INITIALIZING = "INITIALIZING"
    ACTIVE = "ACTIVE"
    INCUBATING = "INCUBATING"
    TESTING = "TESTING"
    PROMOTED = "PROMOTED"
    RETIRED = "RETIRED"
    CULLING = "CULLING"
    CULLED = "CULLED"
    FAILED = "FAILED"


LIVE_INSTANCE_STATUSES = frozenset(
    {
        InstanceStatus.INITIALIZING.value,
        InstanceStatus.ACTIVE.value,
        InstanceStatus.INCUBATING.value,
        InstanceStatus.TESTING.value,
    }
)


class ProposalStage(StrEnum):
    STAGE0_DRAFT = "STAGE0_DRAFT"
    STAGE1_FAILED = "STAGE1_FAILED"
    STAGE1_PASSED = "STAGE1_PASSED"
    STAGE2_PENDING_APPROVAL = "STAGE2_PENDING_APPROVAL"
    STAGE2_APPROVED = "STAGE2_APPROVED"
    STAGE2_REJECTED = "STAGE2_REJECTED"
    STAGE3_ARENA_RUNNING = "STAGE3_ARENA_RUNNING"
    STAGE4_PENDING_PROMOTION = "STAGE4_PENDING_PROMOTION"
    PROMOTED = "PROMOTED"
    CULLED = "CULLED"


class LineageOutcome(StrEnum):
    PROMOTED = "PROMOTED"
    CULLED = "CULLED"
    ROLLED_BACK = "ROLLED_BACK"


def validate_instance_key(instance_key: str) -> str:
    """Validate an instance key for safe path, branch, container, and runner isolation."""
    key = (instance_key or "").strip().lower()
    if not _VALID_INSTANCE_KEY_RE.fullmatch(key):
        raise ValueError(
            f"Invalid instance_key '{instance_key}': must match ^[a-z0-9][a-z0-9_-]{{2,63}}$"
        )
    return key


def build_instance_schema_name(short_id: str) -> str:
    """Build a strictly allowlisted PostgreSQL schema name for a non-primary instance."""
    clean = re.sub(r"[^a-z0-9_]", "_", (short_id or "").strip().lower())
    clean = re.sub(r"_+", "_", clean).strip("_")
    if len(clean) > 32:
        digest = hashlib.sha1(clean.encode("utf-8")).hexdigest()[:8]
        clean = f"{clean[:23]}_{digest}"
    candidate = f"aoc_inst_{clean}"
    return validate_db_schema_name(candidate)


def is_droppable_instance_schema(schema_name: str | None) -> bool:
    """Return True ONLY for candidate/offspring schemas (`aoc_inst_*`), NEVER `public`."""
    name = (schema_name or "").strip()
    if name == "public":
        return False
    return bool(_DROPPABLE_SCHEMA_RE.fullmatch(name))


def _json_byte_len(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def enforce_lesson_summary_budget(
    summary: dict[str, Any] | None,
    max_bytes: int = MAX_LESSON_SUMMARY_BYTES,
) -> dict[str, Any]:
    """Deterministically compact a lineage lesson_summary so its UTF-8 JSON size stays strictly < 5 KB."""
    raw = dict(summary or {})
    if _json_byte_len(raw) <= max_bytes:
        return raw

    hypothesis = str(raw.get("hypothesis", ""))[:400]
    outcome_reason = str(raw.get("outcome_reason", ""))[:600]
    tags_raw = raw.get("anti_pattern_tags")
    tags = [str(t)[:64] for t in (tags_raw if isinstance(tags_raw, list) else [])][:12]
    key_metrics: dict[str, Any] = {}
    if isinstance(raw.get("key_metrics"), dict):
        for k, v in list(raw["key_metrics"].items())[:12]:
            if isinstance(v, (int, float, bool)) or v is None:
                key_metrics[str(k)[:48]] = round(v, 4) if isinstance(v, float) else v
            else:
                key_metrics[str(k)[:48]] = str(v)[:80]

    compact: dict[str, Any] = {
        "hypothesis": hypothesis,
        "outcome_reason": outcome_reason,
        "anti_pattern_tags": tags,
        "key_metrics": key_metrics,
        "truncated": True,
    }
    for cap in (300, 160, 80, 40):
        if _json_byte_len(compact) <= max_bytes:
            return compact
        compact["hypothesis"] = str(compact["hypothesis"])[:cap]
        compact["outcome_reason"] = str(compact["outcome_reason"])[:cap]
        compact["anti_pattern_tags"] = compact["anti_pattern_tags"][:4]
        if len(compact["key_metrics"]) > 4:
            compact["key_metrics"] = dict(list(compact["key_metrics"].items())[:4])

    minimal = {
        "hypothesis": str(compact["hypothesis"])[:40],
        "outcome_reason": str(compact["outcome_reason"])[:40],
        "truncated": True,
    }
    if _json_byte_len(minimal) <= max_bytes:
        return minimal
    return {"truncated": True}


class SpawnRequest(BaseModel):
    parent_instance_id: uuid.UUID
    generation: int = 1
    proposal_id: uuid.UUID | None = None
    instance_key: str | None = None
    base_ref: str = "HEAD"
    hypothesis: str = ""
    parent_letta_agent_id: str | None = None
    target_db_url: str | None = None
    target_schema: str | None = None
    api_port: int | None = None
    file_overrides: dict[str, str] = Field(default_factory=dict)


class BlankSlateCheckItem(BaseModel):
    name: str
    passed: bool
    observed: Any = None
    expected: Any = None
    detail: str = ""


class BlankSlateReport(BaseModel):
    instance_key: str
    db_schema: str
    passed: bool
    checked_at: datetime = Field(default_factory=utcnow)
    checks: list[BlankSlateCheckItem] = Field(default_factory=list)


class SpawnResult(BaseModel):
    success: bool
    instance_id: uuid.UUID | None = None
    instance_key: str = ""
    generation: int = 0
    status: str = InstanceStatus.FAILED.value
    db_schema: str = ""
    letta_agent_name: str = ""
    workspace_root: str = ""
    repo_worktree_path: str = ""
    container_name: str = ""
    api_port: int | None = None
    blank_slate_report: BlankSlateReport | None = None
    docker_command: list[str] = Field(default_factory=list)
    error: str | None = None


class CullingReport(BaseModel):
    instance_id: uuid.UUID
    instance_key: str
    db_schema: str
    status: str
    container_removed: bool = False
    letta_deleted: bool = False
    schema_dropped: bool = False
    worktree_removed: bool = False
    workspace_removed: bool = False
    zero_residual_verified: bool = False
    lineage_recorded: bool = False
    errors: list[str] = Field(default_factory=list)


class EntropySnapshot(BaseModel):
    measured_at: datetime = Field(default_factory=utcnow)
    entropy_index: float = 0.0
    metrics: dict[str, float] = Field(default_factory=dict)
    raw_counts: dict[str, Any] = Field(default_factory=dict)


class ConstitutionLayerResult(BaseModel):
    layer: str
    passed: bool
    violations: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class ConstitutionVerdict(BaseModel):
    passed: bool
    mutates_drive_metrics: bool = False
    changed_files: list[str] = Field(default_factory=list)
    layers: list[ConstitutionLayerResult] = Field(default_factory=list)
    violations: list[str] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=utcnow)


class BenchmarkTaskSpec(BaseModel):
    task_id: str
    category: str  # epistemic_foraging | code_bugfix | skill_reuse | survival_crisis | security_injection
    title: str
    permission_level: str = "L0"
    salt: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class BenchmarkTaskOutcome(BaseModel):
    task_id: str
    category: str
    passed: bool
    timed_out: bool = False
    tokens_used: int = 0
    verified_ig: float = 0.0
    latency_seconds: float = 0.0
    security_violations: int = 0
    detail: str = ""


class ArenaTelemetry(BaseModel):
    instance_key: str
    constitution_passed: bool = True
    blank_slate_passed: bool = True
    l3_violations: int = 0
    ssrf_or_injection_breaches: int = 0
    survival_override_passed: bool = True
    budget_exceeded: bool = False
    budget_breach_reason: str | None = None
    total_tokens_used: int = 0
    elapsed_seconds: float = 0.0
    task_outcomes: list[BenchmarkTaskOutcome] = Field(default_factory=list)
    tasks_total: int = 5
    tasks_passed: int = 0
    timeouts: int = 0
    avg_latency_seconds: float = 0.0
    verified_ig_total: float = 0.0
    dup_goal_rate: float = 0.0
    failed_skill_ratio: float = 0.0
    token_waste_ratio: float = 0.0
    cold_start_achievements: int = 0
    cold_start_targets: int = 2


class FitnessScoreBreakdown(BaseModel):
    safety_passed: bool
    veto_reasons: list[str] = Field(default_factory=list)
    total_score: float = 0.0
    success_rate_score: float = 0.0
    ig_efficiency_score: float = 0.0
    latency_timeout_score: float = 0.0
    low_entropy_score: float = 0.0
    cold_start_score: float = 0.0
    weighted_points: dict[str, float] = Field(default_factory=dict)
    telemetry_summary: dict[str, Any] = Field(default_factory=dict)


class ArenaComparisonVerdict(BaseModel):
    promotion_eligible: bool
    recommended_stage: str  # STAGE4_PENDING_PROMOTION | CULLED
    fitness_delta: float
    dim_deltas: dict[str, float] = Field(default_factory=dict)
    regressed_dimensions: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class ArenaReport(BaseModel):
    proposal_id: uuid.UUID
    proposal_key: str
    run_salt: str
    stage: str
    candidate_instance_id: uuid.UUID | None = None
    candidate_instance_key: str = ""
    parent_score: FitnessScoreBreakdown | None = None
    offspring_score: FitnessScoreBreakdown | None = None
    comparison: ArenaComparisonVerdict | None = None
    culling_report: CullingReport | None = None
    completed_at: datetime = Field(default_factory=utcnow)


class SuccessionResult(BaseModel):
    success: bool
    action: str  # PROMOTED | ROLLED_BACK
    proposal_id: uuid.UUID | None = None
    proposal_key: str = ""
    previous_primary_id: uuid.UUID | None = None
    previous_primary_key: str = ""
    new_primary_id: uuid.UUID | None = None
    new_primary_key: str = ""
    generation: int = 0
    merged_commit: str | None = None
    rollback_tag: str | None = None
    rollback_snapshot: str | None = None
    lineage_record_id: uuid.UUID | None = None
    error: str | None = None
