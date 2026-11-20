"""Governance Manager: least-privilege L0-L3 policy, resource budgets, workspace isolation."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from app.cognition.interfaces import ActionPlan
from app.core.config import GovernanceSettings, get_policy_config
from app.core.enums import DriveType, PermissionLevel, TaskStatus
from app.governance.interfaces import BudgetStatus, GovernanceInterface, PolicyVerdict
from app.observability.metrics import POLICY_VERDICTS
from app.storage.repository import StateRepository, day_start


class GovernanceManager(GovernanceInterface):
    def __init__(
        self,
        settings: GovernanceSettings,
        repo: StateRepository,
        policy_cfg: dict[str, Any] | None = None,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.policy = policy_cfg if policy_cfg is not None else get_policy_config()
        self.workspace_root = Path(settings.workspace_root).resolve()

    async def check_budget(self) -> BudgetStatus:
        since = day_start()
        task_usage = await self.repo.usage_since("agent_task", since)
        llm_usage = await self.repo.usage_since("llm", since)
        active = await self.repo.list_tasks(
            statuses=[TaskStatus.DISPATCHED.value, TaskStatus.RUNNING.value], limit=50
        )

        b = self.settings.budget
        tasks_used = int(task_usage["count"])
        tokens_used = int(llm_usage["amount"])
        running = len(active)

        reasons: list[str] = []
        if tasks_used >= b.max_agent_tasks_per_day:
            reasons.append(f"daily agent task limit reached ({tasks_used}/{b.max_agent_tasks_per_day})")
        if tokens_used >= b.max_llm_tokens_per_day:
            reasons.append(f"daily LLM token limit reached ({tokens_used}/{b.max_llm_tokens_per_day})")
        if running >= b.max_concurrent_tasks:
            reasons.append(f"max concurrent tasks reached ({running}/{b.max_concurrent_tasks})")

        return BudgetStatus(
            allowed=len(reasons) == 0,
            tasks_used_today=tasks_used,
            tasks_limit_per_day=b.max_agent_tasks_per_day,
            tokens_used_today=tokens_used,
            tokens_limit_per_day=b.max_llm_tokens_per_day,
            running_tasks=running,
            max_concurrent_tasks=b.max_concurrent_tasks,
            reason="; ".join(reasons) if reasons else "within budget",
        )

    async def validate_plan(self, plan: ActionPlan, drive_type: str) -> PolicyVerdict:
        req_level = PermissionLevel(plan.permission_level)
        eff_level = req_level
        violations: list[str] = []

        corpus = "\n".join([plan.goal_title, plan.objective, plan.description, plan.expected_output])

        # 1. Pattern-based escalation rules from config/policy.yaml
        for rule in self.policy.get("escalation_rules", []):
            rule_level = PermissionLevel(rule["level"])
            for pat in rule.get("patterns", []):
                if re.search(pat, corpus, flags=re.IGNORECASE):
                    eff_level = PermissionLevel.max(eff_level, rule_level)
                    violations.append(f"[{rule_level.value}] {rule.get('reason', pat)}")
                    break

        # 2. Reproduction drive hard boundary (task book 4.2 & 11)
        if drive_type == DriveType.REPRODUCTION.value:
            for forbidden in self.policy.get("reproduction_forbidden", []):
                if re.search(forbidden, corpus, flags=re.IGNORECASE):
                    eff_level = PermissionLevel.L3
                    violations.append(f"[L3] reproduction drive forbidden action: {forbidden}")

        # 3. Budget check
        budget = await self.check_budget()
        eff_timeout = min(plan.timeout_seconds, self.settings.budget.max_task_timeout_seconds)

        # 4. Decision
        if eff_level == PermissionLevel.L3:
            POLICY_VERDICTS.labels(verdict="denied_l3").inc()
            return PolicyVerdict(
                allowed=False,
                requires_approval=False,
                denied=True,
                requested_level=req_level.value,
                effective_level=eff_level.value,
                sandbox_mode="read-only",
                effective_timeout_seconds=eff_timeout,
                violations=violations or ["L3 operations are forbidden for autonomous execution"],
                reason="Denied: L3 core system / forbidden action",
            )

        auto_max = PermissionLevel(self.settings.auto_approve_max_level)
        if eff_level.rank > auto_max.rank or eff_level == PermissionLevel.L2:
            POLICY_VERDICTS.labels(verdict="requires_approval").inc()
            return PolicyVerdict(
                allowed=False,
                requires_approval=True,
                denied=False,
                requested_level=req_level.value,
                effective_level=eff_level.value,
                sandbox_mode="read-only",
                effective_timeout_seconds=eff_timeout,
                violations=violations or [f"{eff_level.value} exceeds auto-approve level {auto_max.value}"],
                reason=f"Requires human approval ({eff_level.value})",
            )

        if not budget.allowed:
            POLICY_VERDICTS.labels(verdict="budget_exceeded").inc()
            return PolicyVerdict(
                allowed=False,
                requires_approval=False,
                denied=True,
                requested_level=req_level.value,
                effective_level=eff_level.value,
                sandbox_mode="read-only",
                effective_timeout_seconds=eff_timeout,
                violations=[budget.reason],
                reason=f"Denied: {budget.reason}",
            )

        level_sandbox = self.policy.get("level_sandbox", {"L0": "read-only", "L1": "workspace-write"})
        sandbox_mode = level_sandbox.get(eff_level.value, "read-only")
        if sandbox_mode not in {"read-only", "workspace-write"}:
            sandbox_mode = "read-only"

        POLICY_VERDICTS.labels(verdict="allowed").inc()
        return PolicyVerdict(
            allowed=True,
            requires_approval=False,
            denied=False,
            requested_level=req_level.value,
            effective_level=eff_level.value,
            sandbox_mode=sandbox_mode,
            effective_timeout_seconds=eff_timeout,
            violations=violations,
            reason=f"Allowed at {eff_level.value} ({sandbox_mode})",
        )

    def prepare_workspace(self, task_id: str) -> str:
        """Create and validate an isolated directory strictly inside workspace_root."""
        safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", task_id)
        if not safe_id:
            raise ValueError("invalid task_id for workspace")
        target = (self.workspace_root / safe_id).resolve()
        if self.workspace_root not in target.parents:
            raise PermissionError(f"workspace path escape blocked: {target}")
        os.makedirs(target, exist_ok=True)
        return str(target)
