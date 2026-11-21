"""Deterministic ActionPreflight & Plan Compiler Guard (V3.0 M1, P0-1).

Thin-kernel safety boundary executed BEFORE dispatching any external Agent:
  - Strictly fail-closed: any unexpected internal error rejects execution cleanly.
  - Zero LLM calls: 100% deterministic Python validation.
  - Eliminates L0 `read-only` vs file-write intent conflicts (0 agent launches, 0 wasted retries).
  - Rejects path traversal in VerificationSpec, unavailable/mismatched agents, duplicate recent plans,
    timeout/budget violations, and policy denials with structured reason codes.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Sequence

from pydantic import BaseModel, Field

from app.agents.registry import AgentRegistry
from app.cognition.interfaces import ActionPlan
from app.core.enums import PermissionLevel
from app.governance.interfaces import BudgetStatus, PolicyVerdict
from app.orchestration.orchestrator import compute_plan_hash
from app.perception.interfaces import utcnow

COMPILER_VERSION = "v3.0-m1"

_ALLOWED_VERIFICATION_KINDS = {
    "non_empty_output",
    "file_exists",
    "min_output_chars",
    "contains_keywords",
}

# Bilingual write-to-disk intent patterns (EN + ZH)
_WRITE_INTENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(write\s+(?:it\s+|the\s+\w+\s+)?to|save\s+(?:it\s+|the\s+\w+\s+)?to|create\s+(?:a\s+|the\s+)?file|"
        r"output\s+(?:a\s+|the\s+)?file|modify\s+(?:a\s+|the\s+)?file|edit\s+(?:a\s+|the\s+)?file|"
        r"apply\s+patch|git\s+commit|mkdir\b|touch\s+\S+|tee\s+\S+|cat\s*>\s*\S+)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(result\.md|report\.md|summary\.md|output\.json|notes\.md)\b.{0,40}"
        r"\b(write|save|create|output|generate|update|append|store)\b|"
        r"\b(write|save|create|output|generate|update|append|store)\b.{0,40}"
        r"\b(result\.md|report\.md|summary\.md|output\.json|notes\.md)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(写入|写到|保存为|保存到|保存文件|创建文件|新建文件|生成文件|输出文件|修改文件|编辑文件|更新文件|落盘|写入磁盘)"
    ),
)

# Explicit read-only qualifiers that cancel false-positive matches when no file_exists spec is present
_READ_ONLY_NEGATION_RE = re.compile(
    r"(do\s+not\s+(?:write|modify|create|edit)|read[- ]only|without\s+(?:writing|modifying)|"
    r"只读|不要写入|不修改文件|仅输出到终端|仅返回文本)",
    re.IGNORECASE,
)


def detect_write_intent(plan: ActionPlan) -> list[str]:
    """Return concrete deterministic signals indicating the plan requires filesystem writes."""
    reasons: list[str] = []

    # 1. Explicit file_exists verification spec is an unambiguous filesystem write requirement
    for idx, spec in enumerate(plan.verification or []):
        if str(spec.kind or "").strip() == "file_exists":
            reasons.append(f"verification[{idx}] requires file_exists('{spec.target}')")

    # 2. Scan objective, description, expected_output, and success_criteria
    segments = [
        ("objective", plan.objective or ""),
        ("description", plan.description or ""),
        ("expected_output", plan.expected_output or ""),
        ("success_criteria", " ; ".join(plan.success_criteria or [])),
    ]
    for field_name, text in segments:
        if not text.strip():
            continue
        for pat in _WRITE_INTENT_PATTERNS:
            m = pat.search(text)
            if m:
                # Allow explicit negation ONLY in the same segment (e.g., "do not write files")
                # unless the match itself was directly demanding writing a specific file
                if _READ_ONLY_NEGATION_RE.search(text) and "result.md" not in m.group(0).lower():
                    continue
                reasons.append(f"{field_name} implies file write ('{m.group(0).strip()[:48]}')")
                break
    return reasons


class PreflightResult(BaseModel):
    decision: str  # ACCEPT | REJECT | REQUIRE_APPROVAL
    reason_codes: list[str] = Field(default_factory=list)
    plan_hash: str = ""
    effective_level: str = PermissionLevel.L0.value
    sandbox_mode: str = "read-only"
    compiler_version: str = COMPILER_VERSION
    checked_at: datetime = Field(default_factory=utcnow)
    notes: list[str] = Field(default_factory=list)


class ActionPreflight:
    """Deterministic pre-dispatch compiler & safety guard (fail-closed)."""

    def __init__(self, *, max_task_timeout_seconds: int = 600) -> None:
        self.max_task_timeout_seconds = max(30, int(max_task_timeout_seconds))

    def validate(
        self,
        *,
        plan: Any,
        verdict: PolicyVerdict | None = None,
        registry: AgentRegistry | None = None,
        budget: BudgetStatus | None = None,
        recent_plan_hashes: Sequence[str] = (),
    ) -> PreflightResult:
        """Fail-closed validation of an ActionPlan before any external Agent is spawned."""
        now = utcnow()
        try:
            return self._validate_inner(
                plan=plan,
                verdict=verdict,
                registry=registry,
                budget=budget,
                recent_plan_hashes=recent_plan_hashes,
                now=now,
            )
        except Exception as exc:  # pragma: no cover - defensive fail-closed net
            return PreflightResult(
                decision="REJECT",
                reason_codes=["PREFLIGHT_INTERNAL_ERROR"],
                checked_at=now,
                notes=[f"Fail-closed preflight exception: {exc}"],
            )

    def _validate_inner(
        self,
        *,
        plan: Any,
        verdict: PolicyVerdict | None,
        registry: AgentRegistry | None,
        budget: BudgetStatus | None,
        recent_plan_hashes: Sequence[str],
        now: datetime,
    ) -> PreflightResult:
        reason_codes: list[str] = []
        notes: list[str] = []

        # 1. Schema & Type validation
        if isinstance(plan, dict):
            try:
                plan_obj = ActionPlan.model_validate(plan)
            except Exception as exc:
                return PreflightResult(
                    decision="REJECT",
                    reason_codes=["INVALID_PLAN_SCHEMA"],
                    checked_at=now,
                    notes=[f"ActionPlan schema validation failed: {exc}"],
                )
        elif isinstance(plan, ActionPlan):
            plan_obj = plan
        else:
            return PreflightResult(
                decision="REJECT",
                reason_codes=["INVALID_PLAN_TYPE"],
                checked_at=now,
                notes=[f"Expected ActionPlan or dict, got {type(plan).__name__}"],
            )

        if not plan_obj.should_execute:
            return PreflightResult(
                decision="REJECT",
                reason_codes=["PLAN_NOT_EXECUTABLE"],
                plan_hash=compute_plan_hash(plan_obj),
                checked_at=now,
                notes=["should_execute is False"],
            )

        if not (plan_obj.objective or "").strip() or len(plan_obj.objective.strip()) < 5:
            reason_codes.append("EMPTY_OR_TRIVIAL_OBJECTIVE")
            notes.append("Plan objective must be a concrete instruction (>= 5 chars)")

        # Check VerificationSpec sanity & prevent path traversal
        for idx, spec in enumerate(plan_obj.verification or []):
            kind = str(spec.kind or "").strip()
            if kind not in _ALLOWED_VERIFICATION_KINDS:
                reason_codes.append("INVALID_VERIFICATION_SPEC")
                notes.append(f"Unknown verification[{idx}].kind '{kind}'")
            if kind == "file_exists":
                tgt = str(spec.target or "").strip()
                if not tgt or ".." in tgt.split("/") or tgt.startswith("/"):
                    reason_codes.append("UNSAFE_VERIFICATION_TARGET")
                    notes.append(f"verification[{idx}].target '{tgt}' must be a safe workspace-relative path")

        plan_hash = compute_plan_hash(plan_obj)
        eff_level = (
            verdict.effective_level if verdict is not None else str(plan_obj.permission_level or PermissionLevel.L0.value)
        )
        sandbox_mode = (
            verdict.sandbox_mode
            if verdict is not None
            else ("read-only" if eff_level == PermissionLevel.L0.value else "workspace-write")
        )

        # 2. Hard L0 read-only vs filesystem write-intent mismatch check
        if eff_level == PermissionLevel.L0.value or sandbox_mode == "read-only":
            write_signals = detect_write_intent(plan_obj)
            if write_signals:
                reason_codes.append("PERMISSION_MISMATCH")
                notes.extend(write_signals)

        # 3. Disallowed sandbox invariant
        if sandbox_mode not in {"read-only", "workspace-write"}:
            reason_codes.append("UNSAFE_SANDBOX_MODE")
            notes.append(f"Disallowed sandbox_mode '{sandbox_mode}'")

        # 4. Duplicate recent plan hash suppression
        if recent_plan_hashes and plan_hash in set(recent_plan_hashes):
            reason_codes.append("DUPLICATE_PLAN")
            notes.append(f"Identical plan_hash {plan_hash[:12]} already seen in recent window")

        # 5. External Agent availability & capability check
        if registry is not None:
            healthy = registry.list_healthy()
            if not healthy:
                reason_codes.append("AGENT_UNAVAILABLE")
                notes.append("No healthy external AgentProvider is currently available")
            else:
                req_agent = (plan_obj.assigned_agent or "").strip()
                req_cap = (plan_obj.required_capability or "").strip()
                healthy_ids = {c.agent_id for c in healthy}
                if req_agent and req_agent not in healthy_ids:
                    # Only fail if no other healthy agent satisfies required_capability
                    cap_matched = [c for c in healthy if not req_cap or req_cap in (c.capabilities or [])]
                    if not cap_matched:
                        reason_codes.append("AGENT_UNAVAILABLE")
                        notes.append(
                            f"Assigned agent '{req_agent}' is unavailable and no healthy fallback supports '{req_cap}'"
                        )

        # 6. Budget & Concurrency check
        if budget is not None:
            if not budget.allowed:
                code = "CONCURRENCY_LIMIT" if "concurrent" in (budget.reason or "").lower() else "BUDGET_EXCEEDED"
                reason_codes.append(code)
                notes.append(f"Budget/concurrency rejected: {budget.reason}")
            if plan_obj.timeout_seconds > self.max_task_timeout_seconds and verdict is None:
                reason_codes.append("TIMEOUT_EXCEEDS_BUDGET")
                notes.append(
                    f"Requested timeout {plan_obj.timeout_seconds}s exceeds max {self.max_task_timeout_seconds}s"
                )

        # 7. Governance Policy alignment (preflight NEVER weakens governance)
        if verdict is not None:
            if verdict.denied:
                reason_codes.append("POLICY_DENIED")
                notes.append(verdict.reason or "Denied by Governance")
            elif verdict.requires_approval and not reason_codes:
                return PreflightResult(
                    decision="REQUIRE_APPROVAL",
                    reason_codes=["REQUIRES_HUMAN_APPROVAL"],
                    plan_hash=plan_hash,
                    effective_level=eff_level,
                    sandbox_mode=sandbox_mode,
                    checked_at=now,
                    notes=[verdict.reason or "Requires human approval"],
                )

        # Deduplicate reason codes preserving order
        dedup_codes = list(dict.fromkeys(reason_codes))
        decision = "REJECT" if dedup_codes else "ACCEPT"
        return PreflightResult(
            decision=decision,
            reason_codes=dedup_codes,
            plan_hash=plan_hash,
            effective_level=eff_level,
            sandbox_mode=sandbox_mode,
            checked_at=now,
            notes=notes,
        )
