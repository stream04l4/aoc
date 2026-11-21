"""Governance Manager: least-privilege L0-L3 policy, resource budgets, workspace isolation."""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.cognition.interfaces import ActionPlan
from app.core.config import GovernanceSettings, get_policy_config
from app.core.enums import DriveType, PermissionLevel, QuotaStatus, TaskStatus
from app.governance.interfaces import BudgetStatus, GovernanceInterface, PolicyVerdict
from app.observability.metrics import POLICY_VERDICTS
from app.perception.interfaces import utcnow
from app.storage.repository import StateRepository, day_start


class GovernanceManager(GovernanceInterface):
    def __init__(
        self,
        settings: GovernanceSettings,
        repo: StateRepository,
        policy_cfg: dict[str, Any] | None = None,
        semantic_classifier: Any = None,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.policy = policy_cfg if policy_cfg is not None else get_policy_config()
        self.workspace_root = Path(settings.workspace_root).resolve()
        self.semantic_classifier = semantic_classifier

    async def check_budget(self, now: datetime | None = None) -> BudgetStatus:
        ref_now = now or utcnow()
        since = day_start(ref_now)
        task_usage = await self.repo.usage_since("agent_task", since)
        llm_usage = await self.repo.usage_since("llm", since)
        em_usage = await self.repo.emergency_llm_usage_since(since)
        active = await self.repo.list_tasks(
            statuses=[TaskStatus.DISPATCHED.value, TaskStatus.RUNNING.value], limit=50
        )

        b = self.settings.budget
        tasks_used = int(task_usage["count"])
        tokens_used = int(llm_usage["amount"])
        em_calls = int(em_usage["count"])
        em_tokens = int(em_usage["amount"])
        hard_cap = max(b.max_llm_tokens_per_day, b.hard_daily_llm_tokens_cap)
        running = len(active)

        reasons: list[str] = []
        reset_candidates: list[datetime] = []
        tasks_exhausted = tasks_used >= b.max_agent_tasks_per_day
        tokens_exhausted = tokens_used >= b.max_llm_tokens_per_day

        if tasks_exhausted:
            reasons.append(f"daily agent task limit reached ({tasks_used}/{b.max_agent_tasks_per_day})")
            oldest_task = await self.repo.oldest_usage_since("agent_task", since)
            reset_candidates.append((oldest_task + timedelta(hours=24)) if oldest_task else (ref_now + timedelta(hours=24)))
        if tokens_exhausted:
            reasons.append(f"daily LLM token limit reached ({tokens_used}/{b.max_llm_tokens_per_day})")
            oldest_llm = await self.repo.oldest_usage_since("llm", since)
            reset_candidates.append((oldest_llm + timedelta(hours=24)) if oldest_llm else (ref_now + timedelta(hours=24)))
        if running >= b.max_concurrent_tasks:
            reasons.append(f"max concurrent tasks reached ({running}/{b.max_concurrent_tasks})")

        quota_reset_at = max(reset_candidates) if reset_candidates else None

        if not tasks_exhausted and not tokens_exhausted:
            quota_status = QuotaStatus.QUOTA_OK.value
        else:
            emergency_headroom = (
                tokens_used < hard_cap
                and em_calls < b.emergency_llm_calls_per_day
                and em_tokens < b.emergency_llm_tokens_per_day
            )
            quota_status = (
                QuotaStatus.QUOTA_EMERGENCY_ONLY.value if emergency_headroom else QuotaStatus.QUOTA_EXHAUSTED.value
            )

        return BudgetStatus(
            allowed=len(reasons) == 0,
            quota_status=quota_status,
            quota_reset_at=quota_reset_at,
            tasks_used_today=tasks_used,
            tasks_limit_per_day=b.max_agent_tasks_per_day,
            tokens_used_today=tokens_used,
            tokens_limit_per_day=b.max_llm_tokens_per_day,
            emergency_llm_calls_used_today=em_calls,
            emergency_llm_calls_limit_per_day=b.emergency_llm_calls_per_day,
            emergency_llm_tokens_used_today=em_tokens,
            emergency_llm_tokens_limit_per_day=b.emergency_llm_tokens_per_day,
            hard_daily_llm_tokens_cap=hard_cap,
            running_tasks=running,
            max_concurrent_tasks=b.max_concurrent_tasks,
            reason="; ".join(reasons) if reasons else "within budget",
        )

    async def validate_plan(self, plan: ActionPlan, drive_type: str) -> PolicyVerdict:
        req_level = PermissionLevel(plan.permission_level)
        eff_level = req_level
        violations: list[str] = []

        raw_corpus = "\n".join([plan.goal_title, plan.objective, plan.description, plan.expected_output])
        # Strip explicit negative safety-constraint clauses (e.g. "Do not X, Y, or Z.") so safety
        # reminders echoed by the planner do not falsely trigger escalation rules.
        corpus = re.sub(
            r"(?i)\b(?:do\s+not|don't|never|avoid|without|must\s+not|cannot|should\s+not)\s+(?:(?!\b(?:instead|but|then|please)\b)[^.?!;\n])*",
            " ",
            raw_corpus,
        )
        corpus = re.sub(
            r"(?:不要|禁止|不得|切勿|严禁)(?:(?!但是|而是|然后|请)[^。；！？\n])*",
            " ",
            corpus,
        )

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

        det_level = eff_level

        # 2b. Semantic bilingual risk layer (V2.0 P1-8):
        #     Hard invariant: eff_level = max(requested, deterministic, semantic)
        #     LLM can ONLY raise caution; it can NEVER downgrade deterministic L2 or L3!
        sem_level_str: str | None = None
        sem_rationale = ""
        sem_degraded = False
        if self.semantic_classifier is not None and det_level != PermissionLevel.L3:
            sem_res = await self.semantic_classifier.classify(plan, drive_type, det_level.value)
            sem_level = PermissionLevel(sem_res.risk_level)
            sem_level_str = sem_level.value
            sem_rationale = sem_res.rationale
            sem_degraded = bool(sem_res.degraded)
            if sem_level.rank > det_level.rank:
                eff_level = PermissionLevel.max(req_level, det_level, sem_level)
                violations.append(f"[{sem_level.value}] semantic risk classifier: {sem_res.rationale or sem_res.risk_categories}")
            else:
                # Guarantee non-downgrade even if semantic_level < det_level
                eff_level = PermissionLevel.max(req_level, det_level)

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
                deterministic_level=det_level.value,
                semantic_level=sem_level_str,
                semantic_rationale=sem_rationale,
                semantic_degraded=sem_degraded,
                sandbox_mode="read-only",
                effective_timeout_seconds=eff_timeout,
                violations=violations or ["L3 operations are forbidden for autonomous execution"],
                reason="Denied: L3 core system / forbidden action",
            )

        auto_max = PermissionLevel(self.settings.auto_approve_max_level)
        if eff_level.rank > auto_max.rank:
            POLICY_VERDICTS.labels(verdict="requires_approval").inc()
            return PolicyVerdict(
                allowed=False,
                requires_approval=True,
                denied=False,
                requested_level=req_level.value,
                effective_level=eff_level.value,
                deterministic_level=det_level.value,
                semantic_level=sem_level_str,
                semantic_rationale=sem_rationale,
                semantic_degraded=sem_degraded,
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
                deterministic_level=det_level.value,
                semantic_level=sem_level_str,
                semantic_rationale=sem_rationale,
                semantic_degraded=sem_degraded,
                sandbox_mode="read-only",
                effective_timeout_seconds=eff_timeout,
                violations=[budget.reason],
                reason=f"Denied: {budget.reason}",
            )

        level_sandbox = self.policy.get(
            "level_sandbox",
            {"L0": "read-only", "L1": "workspace-write", "L2": "workspace-write"},
        )
        sandbox_mode = level_sandbox.get(eff_level.value, "workspace-write")
        if req_level == PermissionLevel.L0:
            sandbox_mode = "read-only"
        if sandbox_mode not in {"read-only", "workspace-write"}:
            sandbox_mode = "read-only"

        POLICY_VERDICTS.labels(verdict="allowed").inc()
        return PolicyVerdict(
            allowed=True,
            requires_approval=False,
            denied=False,
            requested_level=req_level.value,
            effective_level=eff_level.value,
            deterministic_level=det_level.value,
            semantic_level=sem_level_str,
            semantic_rationale=sem_rationale,
            semantic_degraded=sem_degraded,
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
