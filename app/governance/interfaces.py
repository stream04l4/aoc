"""Governance interfaces (task book section 11)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.cognition.interfaces import ActionPlan
from app.core.enums import QuotaStatus


class BudgetStatus(BaseModel):
    allowed: bool
    quota_status: str = QuotaStatus.QUOTA_OK.value
    quota_reset_at: datetime | None = None
    tasks_used_today: int = 0
    tasks_limit_per_day: int = 20
    tokens_used_today: int = 0
    tokens_limit_per_day: int = 2_000_000
    emergency_llm_calls_used_today: int = 0
    emergency_llm_calls_limit_per_day: int = 10
    emergency_llm_tokens_used_today: int = 0
    emergency_llm_tokens_limit_per_day: int = 100_000
    hard_daily_llm_tokens_cap: int = 2_200_000
    running_tasks: int = 0
    max_concurrent_tasks: int = 1
    reason: str = ""

    @property
    def tasks_remaining(self) -> int:
        return max(0, self.tasks_limit_per_day - self.tasks_used_today)

    @property
    def tokens_remaining(self) -> int:
        return max(0, self.tokens_limit_per_day - self.tokens_used_today)

    @property
    def is_quota_exhausted(self) -> bool:
        return self.quota_status in {QuotaStatus.QUOTA_EXHAUSTED.value, QuotaStatus.QUOTA_EMERGENCY_ONLY.value}


class PolicyVerdict(BaseModel):
    allowed: bool
    requires_approval: bool = False
    denied: bool = False
    requested_level: str
    effective_level: str
    sandbox_mode: str = "read-only"
    effective_timeout_seconds: int = 300
    violations: list[str] = Field(default_factory=list)
    reason: str = ""


class GovernanceInterface(ABC):
    @abstractmethod
    async def check_budget(self) -> BudgetStatus: ...

    @abstractmethod
    async def validate_plan(self, plan: ActionPlan, drive_type: str) -> PolicyVerdict: ...

    @abstractmethod
    def prepare_workspace(self, task_id: str) -> str: ...
