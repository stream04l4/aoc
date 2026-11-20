"""Governance interfaces (task book section 11)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field

from app.cognition.interfaces import ActionPlan


class BudgetStatus(BaseModel):
    allowed: bool
    tasks_used_today: int = 0
    tasks_limit_per_day: int = 20
    tokens_used_today: int = 0
    tokens_limit_per_day: int = 2_000_000
    running_tasks: int = 0
    max_concurrent_tasks: int = 1
    reason: str = ""

    @property
    def tasks_remaining(self) -> int:
        return max(0, self.tasks_limit_per_day - self.tasks_used_today)

    @property
    def tokens_remaining(self) -> int:
        return max(0, self.tokens_limit_per_day - self.tokens_used_today)


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
