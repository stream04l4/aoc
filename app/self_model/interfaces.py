"""Self Model & Skill Registry interfaces and exact Beta-Binomial math (V2.0 P1-7).

Implements Task Book Section 5.8:
  - 5-state capability maturity lifecycle: DECLARED -> AVAILABLE -> ACQUIRED -> VALIDATED -> DEPRECATED
  - Exact pure-Python Beta-Binomial posterior & quantile confidence bounds (zero scipy dependency)
  - Strict separation between static external Agent declarations and AOC empirical evidence
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.perception.interfaces import utcnow


class CapabilityMaturity(str, Enum):
    DECLARED = "DECLARED"
    AVAILABLE = "AVAILABLE"
    ACQUIRED = "ACQUIRED"
    VALIDATED = "VALIDATED"
    DEPRECATED = "DEPRECATED"


class SkillStatus(str, Enum):
    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    DEPRECATED = "DEPRECATED"


class SelfModelConfig(BaseModel):
    alpha_prior: float = 1.0
    beta_prior: float = 1.0
    minimum_samples: int = 3
    confidence_threshold: float = 0.55
    confidence_level: float = 0.90
    skill_validation_required: bool = True
    skill_test_timeout_seconds: float = 15.0
    skill_storage_dir: str = "/home/ubuntu/aoc-workspaces/skills"
    deprecate_after_failures: int = 5
    deprecate_max_success_rate: float = 0.25
    sync_letta_via_outbox: bool = True


class BetaBinomialPosterior(BaseModel):
    alpha: float
    beta: float
    sample_count: int
    estimated_success_rate: float
    confidence_low: float
    confidence_high: float


class CapabilityEvidenceModel(BaseModel):
    id: str | None = None
    profile_id: str | None = None
    execution_id: str
    task_id: str
    outcome: str  # verified_success | verified_failure
    weight: float = 1.0
    verification_summary: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class CapabilityProfileModel(BaseModel):
    id: str | None = None
    organism_id: str
    capability_key: str
    maturity_status: str = CapabilityMaturity.DECLARED.value
    alpha: float = 1.0
    beta: float = 1.0
    sample_count: int = 0
    estimated_success_rate: float = 0.5
    confidence_low: float = 0.0
    confidence_high: float = 1.0
    validated_skill_count: int = 0
    evidence_count: int = 0
    updated_at: datetime = Field(default_factory=utcnow)


class SelfModelSnapshot(BaseModel):
    id: str | None = None
    organism_id: str
    version_number: int
    summary_text: str
    profile_snapshot: dict[str, Any] = Field(default_factory=dict)
    trigger_execution_id: str | None = None
    letta_synced_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)


class SkillDefinition(BaseModel):
    id: str | None = None
    skill_key: str
    version: str = "1.0.0"
    capability_key: str
    source_task_id: str | None = None
    source_execution_id: str | None = None
    description: str = ""
    applicable_scenarios: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    artifact_path: str | None = None
    artifact_checksum: str | None = None
    test_spec: dict[str, Any] = Field(default_factory=dict)
    permission_level: str = "L1"
    status: str = SkillStatus.DRAFT.value
    integrity_ok: bool | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class SkillValidationOutcome(BaseModel):
    id: str | None = None
    skill_id: str
    passed: bool
    checks: list[dict[str, Any]] = Field(default_factory=list)
    stdout_excerpt: str = ""
    executed_at: datetime = Field(default_factory=utcnow)


# ── Exact Pure-Python Regularized Incomplete Beta & Quantile ────────────────
def _betacf(a: float, b: float, x: float) -> float:
    """Modified Lentz's method for continued fraction of regularized incomplete beta."""
    max_iter = 200
    eps = 3.0e-12
    fpmin = 1.0e-300

    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c

        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def regularized_beta_cdf(x: float, a: float, b: float) -> float:
    """Exact CDF I_x(a, b) of the Beta(a, b) distribution for x in [0, 1]."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_bt = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1.0 - x)
    bt = math.exp(ln_bt)
    if x < (a + 1.0) / (a + b + 2.0):
        return max(0.0, min(1.0, bt * _betacf(a, b, x) / a))
    return max(0.0, min(1.0, 1.0 - bt * _betacf(b, a, 1.0 - x) / b))


def beta_quantile(p: float, a: float, b: float) -> float:
    """Inverse CDF of Beta(a, b) via deterministic bisection to 1e-9 accuracy."""
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if regularized_beta_cdf(mid, a, b) < p:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-9:
            break
    return 0.5 * (lo + hi)


def compute_beta_binomial_stats(
    *,
    alpha_prior: float = 1.0,
    beta_prior: float = 1.0,
    success_weight: float = 0.0,
    failure_weight: float = 0.0,
    sample_count: int = 0,
    confidence_level: float = 0.90,
) -> BetaBinomialPosterior:
    """Compute exact Beta-Binomial posterior mean and confidence bounds.

    `confidence_low` is the one-sided `confidence_level` lower bound: P(p >= low) = confidence_level
    (i.e. quantile q = 1 - confidence_level, default 0.10 for 90% lower bound).
    `confidence_high` is the symmetric upper quantile `confidence_level` (default 0.90).
    """
    a = max(1e-6, float(alpha_prior) + max(0.0, float(success_weight)))
    b = max(1e-6, float(beta_prior) + max(0.0, float(failure_weight)))
    mean = a / (a + b)
    lvl = max(0.51, min(0.999, float(confidence_level)))
    q_low = 1.0 - lvl
    q_high = lvl
    low = beta_quantile(q_low, a, b)
    high = beta_quantile(q_high, a, b)
    return BetaBinomialPosterior(
        alpha=round(a, 6),
        beta=round(b, 6),
        sample_count=max(0, int(sample_count)),
        estimated_success_rate=round(mean, 6),
        confidence_low=round(low, 6),
        confidence_high=round(high, 6),
    )


class SelfModelInterface(ABC):
    @abstractmethod
    async def sync_declared_capabilities(self, agent_capabilities: list[tuple[str, list[str], bool]]) -> int: ...

    @abstractmethod
    async def record_execution_evidence(
        self,
        *,
        capability_key: str,
        execution_id: Any,
        task_id: Any,
        verified_success: bool,
        verification_summary: str = "",
        agent_id: str | None = None,
        wall_seconds: float | None = None,
        weight: float = 1.0,
        session: Any = None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def get_latest_snapshot(self) -> dict[str, Any]: ...

    @abstractmethod
    async def list_capabilities(self) -> list[dict[str, Any]]: ...


class SkillRegistryInterface(ABC):
    @abstractmethod
    async def register_skill(self, **kwargs: Any) -> dict[str, Any]: ...

    @abstractmethod
    async def validate_skill(self, skill_id: Any) -> dict[str, Any]: ...

    @abstractmethod
    async def verify_skill_integrity(self, skill_id: Any) -> tuple[bool, str]: ...
