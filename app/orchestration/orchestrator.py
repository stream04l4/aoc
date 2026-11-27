"""Agent Orchestrator: TaskDispatcher, ExecutionMonitor, ResultCollector, Retry/Fallback & Crash Recovery.

Execution is decoupled from the autonomous loop (task book section 10):
  - dispatch() spawns the external agent asynchronously as a tracked asyncio.Task
    and returns immediately with the persisted execution_id.
  - wait_or_poll() monitors running executions up to a bounded wait window per cycle;
    long-running tasks keep executing in the background across cycles.
  - Failed/timed-out executions are retried or switched to an alternative healthy agent
    up to max_attempts (NEVER infinite retries, task book Scenario F).
  - recover_on_startup() safely reconciles in-flight executions after a crash without
    re-running already SUCCEEDED tasks (task book Scenario E).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.agents.base import AgentExecution, AgentTask
from app.agents.registry import AgentRegistry
from app.agents.router import AgentRouter
from app.cognition.gateway import LLMGatewayInterface
from app.cognition.interfaces import ActionPlan, VerificationSpec
from app.core.enums import ExecutionStatus, GoalStatus, PermissionLevel, TaskStatus
from app.governance.interfaces import GovernanceInterface, PolicyVerdict
from app.observability.logging import get_logger
from app.observability.metrics import AGENT_EXECUTIONS
from app.orchestration.backend import ExecutionBackendInterface, LocalSubprocessBackend
from app.orchestration.state_machine import TaskStateMachine
from app.orchestration.verifier import ResultVerifier, VerificationResult
from app.perception.interfaces import utcnow
from app.storage.outbox import OutboxEventType
from app.storage.repository import StateRepository

logger = get_logger("aoc.orchestration")

ALLOWED_APPROVAL_SANDBOXES = {"read-only", "workspace-write"}


class ApprovalError(RuntimeError):
    """Base error raised by approval verification and dispatch."""


class ApprovalNotFoundError(ApprovalError):
    """Raised when the target approval task does not exist."""


class ApprovalConflictError(ApprovalError):
    """Raised on duplicate/concurrent approval, expiry, or plan tampering."""


class ApprovalDeniedError(ApprovalError):
    """Raised when post-approval policy, budget, agent, or workspace recheck fails."""


def compute_plan_hash(plan_data: dict[str, Any] | ActionPlan) -> str:
    """Compute canonical SHA-256 hash over security-relevant ActionPlan fields."""
    raw = plan_data.model_dump(mode="json") if isinstance(plan_data, ActionPlan) else dict(plan_data)
    canonical = {
        "goal_title": raw.get("goal_title", ""),
        "objective": raw.get("objective", ""),
        "description": raw.get("description", ""),
        "assigned_agent": raw.get("assigned_agent", ""),
        "required_capability": raw.get("required_capability", ""),
        "expected_output": raw.get("expected_output", ""),
        "timeout_seconds": raw.get("timeout_seconds", 300),
        "permission_level": raw.get("permission_level", PermissionLevel.L0.value),
        "success_criteria": raw.get("success_criteria", []),
        "verification": raw.get("verification", []),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def compute_params_digest(task_data: dict[str, Any]) -> str:
    """Compute canonical SHA-256 digest of execution-bound task parameters."""
    canonical = {
        "objective": task_data.get("objective", ""),
        "description": task_data.get("description", ""),
        "assigned_agent": task_data.get("assigned_agent", ""),
        "permission_level": task_data.get("permission_level", ""),
        "timeout_seconds": task_data.get("timeout_seconds", 0),
        "workspace": task_data.get("workspace", ""),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _ensure_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def classify_attempt_failure_bucket(status: str, fail_reason: str) -> str:
    """Classify execution attempt failures into deterministic root-cause buckets (V5.3 M3)."""
    st_u = str(status or "").upper()
    low = str(fail_reason or "").lower()
    if st_u == "TIMEOUT" or "timed out" in low or "timeout" in low:
        return "timeout"
    if any(
        tok in low
        for tok in ("permission denied", "eacces", "read-only file system", "operation not permitted", "policy denied")
    ):
        return "permission_denied"
    if any(tok in low for tok in ("sandbox", "bwrap", "cgroup", "runner", "socket", "broken pipe")):
        return "sandbox_error"
    if any(
        tok in low
        for tok in ("verification", "file_exists", "contains_keywords", "min_output_chars", "non_empty_output")
    ):
        return "verification_failed"
    return "other"


class AgentOrchestrator:
    def __init__(
        self,
        registry: AgentRegistry,
        governance: GovernanceInterface,
        repo: StateRepository,
        verifier: ResultVerifier | None = None,
        max_retries: int = 2,
        llm_gateway: LLMGatewayInterface | None = None,
        backend: ExecutionBackendInterface | None = None,
        state_machine: TaskStateMachine | None = None,
        router: AgentRouter | None = None,
        skill_registry: Any = None,
        explicit_skill_refactor_enabled: bool = False,
    ) -> None:
        self.registry = registry
        self.governance = governance
        self.repo = repo
        self.verifier = verifier or ResultVerifier()
        self.max_retries = max(0, int(max_retries))
        self.llm_gateway = llm_gateway
        self.backend: ExecutionBackendInterface = backend or LocalSubprocessBackend()
        self.state_machine = state_machine or TaskStateMachine(repo)
        self.router = router or AgentRouter(registry, repo)
        self.skill_registry = skill_registry
        self.explicit_skill_refactor_enabled = bool(explicit_skill_refactor_enabled)
        self._approval_locks: dict[str, asyncio.Lock] = {}

    def _approval_lock(self, task_id: str) -> asyncio.Lock:
        lock = self._approval_locks.get(task_id)
        if lock is None:
            lock = asyncio.Lock()
            self._approval_locks[task_id] = lock
        return lock

    async def approve_and_dispatch(
        self,
        task_id: uuid.UUID,
        approver: str = "operator",
        reason: str = "",
        channel: str = "api",
    ) -> dict[str, Any]:
        """Public L2 approval service (P1-1a).

        Re-validates task state, approval expiry, plan_hash integrity, governance policy (L3 block),
        budget, target agent health, and workspace isolation before atomically claiming and launching.
        """
        tid_str = str(task_id)
        async with self._approval_lock(tid_str):
            task = await self.repo.get_task(task_id)
            if task is None:
                raise ApprovalNotFoundError(f"Task {tid_str} not found")
            if task["status"] != TaskStatus.REQUIRES_APPROVAL.value:
                raise ApprovalConflictError(
                    f"Task {tid_str} is not awaiting approval (current status: {task['status']})"
                )

            now = utcnow()
            approval = await self.repo.get_pending_approval_for_task(task_id)
            if approval is not None and _ensure_utc(approval["expires_at"]) <= now:
                await self.repo.update_approval_record(
                    uuid.UUID(approval["approval_id"]),
                    status="EXPIRED",
                    decision_note="Approval expired before decision",
                    decided_at=now,
                )
                raise ApprovalConflictError(f"Approval for task {tid_str} has expired")

            plan_row = await self.repo.get_plan(uuid.UUID(task["plan_id"])) if task.get("plan_id") else None
            if plan_row is not None:
                if plan_row["status"] not in {"REQUIRES_APPROVAL", "APPROVED"}:
                    raise ApprovalConflictError(
                        f"Associated plan is not approvable (status={plan_row['status']})"
                    )
                current_hash = compute_plan_hash(plan_row["plan"])
                if plan_row.get("plan_hash") and plan_row["plan_hash"] != current_hash:
                    raise ApprovalConflictError("Plan integrity check failed: plan_hash mismatch")
                if approval is not None and approval["plan_hash"] != current_hash:
                    raise ApprovalConflictError("Plan changed since approval request was created")
                reconstructed_plan = ActionPlan.model_validate(plan_row["plan"])
            else:
                verif_specs = [
                    v if isinstance(v, VerificationSpec) else VerificationSpec.model_validate(v)
                    for v in (task.get("verification") or [])
                ]
                reconstructed_plan = ActionPlan(
                    goal_title=task["objective"][:120],
                    objective=task["objective"],
                    description=task.get("description", ""),
                    assigned_agent=task["assigned_agent"],
                    required_capability=task.get("required_capability") or "general_task",
                    expected_output=task.get("expected_output", ""),
                    timeout_seconds=int(task["timeout_seconds"]),
                    permission_level=task["permission_level"],
                    success_criteria=list(task.get("success_criteria") or []),
                    verification=verif_specs,
                )

            goal_row = await self.repo.get_goal(uuid.UUID(task["goal_id"])) if task.get("goal_id") else None
            drive_type = goal_row["drive_type"] if goal_row else "unknown"
            cycle_uuid = uuid.UUID(task["cycle_id"]) if task.get("cycle_id") else None

            # 1. Re-validate Governance policy (human approval can NEVER override L3 hard prohibitions)
            verdict = await self.governance.validate_plan(reconstructed_plan, drive_type)
            verdict_label = (
                "denied"
                if verdict.effective_level == PermissionLevel.L3.value
                else ("requires_approval" if verdict.requires_approval else ("allowed" if verdict.allowed else "denied"))
            )
            await self.repo.record_policy_evaluation(
                subject_type="approval_recheck",
                subject_id=task_id,
                cycle_id=cycle_uuid,
                requested_level=verdict.requested_level,
                deterministic_level=verdict.deterministic_level or verdict.effective_level,
                semantic_level=verdict.semantic_level,
                effective_level=verdict.effective_level,
                verdict=verdict_label,
                violations=verdict.violations,
                reason=verdict.reason,
            )
            if verdict.effective_level == PermissionLevel.L3.value:
                raise ApprovalDeniedError(f"Governance recheck forbids L3 operation: {verdict.reason}")

            # 2. Re-validate Budget
            budget = await self.governance.check_budget()
            if not budget.allowed:
                raise ApprovalDeniedError(f"Budget recheck failed: {budget.reason}")

            # 3. Re-validate assigned external agent is registered, enabled, and healthy
            assigned_agent = task["assigned_agent"]
            adapter = self.registry.get(assigned_agent)
            probe = self.registry.get_probe(assigned_agent)
            if adapter is None or not adapter.capability.enabled or probe is None or not probe.ok:
                raise ApprovalDeniedError(
                    f"Assigned agent '{assigned_agent}' is unavailable, disabled, or unhealthy"
                )

            # 4. Re-validate workspace isolation and sandbox level
            ws_root = getattr(self.governance, "workspace_root", None)
            workspace = task.get("workspace") or ""
            if ws_root is not None:
                ws_root_resolved = Path(ws_root).resolve()
                ws_path = Path(workspace).resolve() if workspace else None
                if ws_path is None or ws_root_resolved not in ws_path.parents:
                    workspace = self.governance.prepare_workspace(tid_str)
                    await self.repo.update_task(task_id, workspace=workspace)
                else:
                    os.makedirs(ws_path, exist_ok=True)
            else:
                workspace = self.governance.prepare_workspace(tid_str)

            req_sandbox = (task.get("resource_budget") or {}).get("sandbox_mode")
            if req_sandbox in ALLOWED_APPROVAL_SANDBOXES:
                sandbox_mode = req_sandbox
            elif task["permission_level"] == PermissionLevel.L0.value:
                sandbox_mode = "read-only"
            else:
                sandbox_mode = "workspace-write"

            # 5. Atomic DB claim prevents concurrent double-dispatch
            claimed = await self.repo.claim_task_for_approval(
                task_id,
                allowed_statuses=[TaskStatus.REQUIRES_APPROVAL.value],
                new_status=TaskStatus.DISPATCHED.value,
            )
            if not claimed:
                raise ApprovalConflictError(f"Task {tid_str} approval was already processed concurrently")

            if plan_row is not None:
                await self.repo.update_plan_status(uuid.UUID(task["plan_id"]), "APPROVED")
            if approval is not None:
                await self.repo.update_approval_record(
                    uuid.UUID(approval["approval_id"]),
                    status="CONSUMED",
                    approver=approver,
                    channel=channel,
                    decision_note=reason or "approved",
                    decided_at=now,
                    consumed_at=now,
                )
            await self.repo.log_decision(
                decision_type="human_approval",
                selected="APPROVED",
                alternatives=["REJECTED"],
                reason=f"Approved task {tid_str} by {approver}" + (f": {reason}" if reason else ""),
                payload={
                    "task_id": tid_str,
                    "approver": approver,
                    "channel": channel,
                    "assigned_agent": assigned_agent,
                    "sandbox_mode": sandbox_mode,
                    "effective_level": verdict.effective_level,
                },
                cycle_id=cycle_uuid,
            )

            exec_id = await self._launch_attempt(task_id, assigned_agent, sandbox_mode, cycle_id=cycle_uuid)
            return {
                "task_id": tid_str,
                "execution_id": exec_id,
                "assigned_agent": assigned_agent,
                "sandbox_mode": sandbox_mode,
                "status": TaskStatus.RUNNING.value,
            }

    async def reject_approval(
        self,
        task_id: uuid.UUID,
        approver: str = "operator",
        reason: str = "operator rejected",
        channel: str = "api",
    ) -> dict[str, Any]:
        """Public L2 rejection service syncing task, plan, goal, approval record, and audit log."""
        tid_str = str(task_id)
        async with self._approval_lock(tid_str):
            task = await self.repo.get_task(task_id)
            if task is None:
                raise ApprovalNotFoundError(f"Task {tid_str} not found")
            if task["status"] != TaskStatus.REQUIRES_APPROVAL.value:
                raise ApprovalConflictError(
                    f"Task {tid_str} is not awaiting approval (current status: {task['status']})"
                )

            now = utcnow()
            claimed = await self.repo.claim_task_for_approval(
                task_id,
                allowed_statuses=[TaskStatus.REQUIRES_APPROVAL.value],
                new_status=TaskStatus.REJECTED.value,
                outcome={"rejected_reason": reason, "rejected_by": approver, "rejected_at": now.isoformat()},
            )
            if not claimed:
                raise ApprovalConflictError(f"Task {tid_str} approval was already processed concurrently")

            if task.get("plan_id"):
                await self.repo.update_plan_status(uuid.UUID(task["plan_id"]), "REJECTED")
            if task.get("goal_id"):
                await self.repo.transition_goal(
                    uuid.UUID(task["goal_id"]),
                    GoalStatus.REJECTED.value,
                    note=f"Approval rejected by {approver}: {reason}",
                )
            approval = await self.repo.get_pending_approval_for_task(task_id)
            if approval is not None:
                await self.repo.update_approval_record(
                    uuid.UUID(approval["approval_id"]),
                    status="REJECTED",
                    approver=approver,
                    channel=channel,
                    decision_note=reason,
                    decided_at=now,
                )
            cycle_uuid = uuid.UUID(task["cycle_id"]) if task.get("cycle_id") else None
            await self.repo.log_decision(
                decision_type="human_approval",
                selected="REJECTED",
                alternatives=["APPROVED"],
                reason=f"Rejected task {tid_str} by {approver}: {reason}",
                payload={"task_id": tid_str, "approver": approver, "channel": channel},
                cycle_id=cycle_uuid,
            )
            return {"task_id": tid_str, "status": TaskStatus.REJECTED.value}

    async def submit_plan(
        self,
        plan: ActionPlan,
        verdict: PolicyVerdict,
        goal_id: uuid.UUID | None = None,
        plan_id: uuid.UUID | None = None,
        cycle_id: uuid.UUID | None = None,
        retry_backoff_multiplier: float = 1.0,
        workspace_override: str | None = None,
        resource_budget_extra: dict[str, Any] | None = None,
        description_suffix: str = "",
    ) -> dict[str, Any]:
        """Persist a task from an approved ActionPlan and dispatch it asynchronously."""
        task_uuid = uuid.uuid4()
        task_id = str(task_uuid)
        if workspace_override:
            ws_root = Path(str(getattr(self.governance, "workspace_root", "/home/ubuntu/aoc-workspaces"))).resolve()
            ws_cand = Path(str(workspace_override)).resolve()
            if ws_cand != ws_root and ws_root not in ws_cand.parents:
                raise PermissionError(f"workspace_override escape blocked: {ws_cand}")
            ws_cand.mkdir(parents=True, exist_ok=True)
            workspace = str(ws_cand)
        else:
            workspace = self.governance.prepare_workspace(task_id)
        safe_backoff_mult = max(1.0, min(1.15, float(retry_backoff_multiplier or 1.0)))
        res_budget: dict[str, Any] = {
            "sandbox_mode": verdict.sandbox_mode,
            "retry_backoff_multiplier": round(safe_backoff_mult, 4),
        }
        if isinstance(resource_budget_extra, dict) and resource_budget_extra:
            res_budget.update(resource_budget_extra)

        effective_description = plan.description
        if description_suffix and description_suffix.strip():
            effective_description = f"{(effective_description or '').strip()}\n\n{description_suffix.strip()}".strip()
        if (
            self.explicit_skill_refactor_enabled
            and self.skill_registry is not None
            and isinstance(plan.skill_refactor_target, dict)
            and plan.skill_refactor_target
        ):
            try:
                staged = await self.skill_registry.stage_base_skill_in_workspace(
                    workspace, plan.skill_refactor_target
                )
                if staged is not None:
                    res_budget["skill_refactor_target"] = staged
                    failure_cases_note = (
                        f"- {int(staged.get('failure_cases_count') or 0)} historical failure case(s) are recorded in `./.aoc_base_skill/failure_cases.json`; you MUST inspect them and add regression assertions covering those exact failure modes in `./test_skill.py` (Gate B).\n"
                        if int(staged.get("failure_cases_count") or 0) > 0
                        else ""
                    )
                    merge_note = (
                        f"- Consolidation target: fold capabilities from overlapping candidates {staged['merge_candidates']} cleanly into `./skill.py` while keeping public signatures compatible.\n"
                        if staged.get("merge_candidates")
                        else ""
                    )
                    refactor_note = (
                        "\n\n[Explicit Skill Refactoring Instructions]\n"
                        f"- Baseline skill '{staged['canonical_name']}' (v{staged['base_version']}) has been "
                        "staged read-only inside `./.aoc_base_skill/` (`skill.py`, `test_skill.py`, `metadata.json`, `failure_cases.json`).\n"
                        "- You MUST read `./.aoc_base_skill/skill.py`, `./.aoc_base_skill/test_skill.py`, `./.aoc_base_skill/metadata.json`, and `./.aoc_base_skill/failure_cases.json` first.\n"
                        f"- Target generalization: {staged.get('target_generalization') or staged.get('reason') or 'preserve existing contracts while improving edge-case handling and reusability'}.\n"
                        f"{failure_cases_note}"
                        f"{merge_note}"
                        "- Preserve ALL existing public entrypoint signatures (`execute`/`run`/etc.) and backward-compatible input/output contracts.\n"
                        "- Write the refactored skill code and expanded unit tests directly to workspace root `./skill.py` and `./test_skill.py` (do NOT edit only inside `./.aoc_base_skill/`, which is the staged read-only baseline) so ALL assertions in `./.aoc_base_skill/test_skill.py` pass unchanged (Gate A), AND expand `./test_skill.py` with new boundary assertions (Gate B).\n"
                        "- Keep the task focused cleanly on refactoring and testing this skill without bundling unrelated heavy external multi-hop research.\n"
                        "- Optionally write `./skill_meta.json` (`{\"input_schema\": ..., \"output_schema\": ...}`) so AOC publishes crisp parameter schemas for `skill_first` reuse."
                    )
                    effective_description = f"{(plan.description or '').strip()}{refactor_note}".strip()
                    await self.repo.log_event(
                        "orchestrator.skill_refactor_staged",
                        message=f"Staged baseline skill {staged['canonical_name']}@{staged['base_version']} in {task_id}",
                        payload={"task_id": task_id, **staged},
                        cycle_id=cycle_id,
                    )
            except Exception as exc:
                logger.warning("orchestrator.skill_refactor_stage_degraded", task_id=task_id, error=str(exc))

        adapter, _routing_dec = await self.router.select(
            preferred_agent=plan.assigned_agent,
            required_capability=plan.required_capability,
            sandbox_mode=verdict.sandbox_mode,
            cycle_id=cycle_id,
            log_decision=True,
        )
        if adapter is None:
            await self.repo.create_task(
                id=task_uuid,
                plan_id=plan_id,
                goal_id=goal_id,
                cycle_id=cycle_id,
                objective=plan.objective,
                description=effective_description,
                assigned_agent=plan.assigned_agent,
                expected_output=plan.expected_output,
                workspace=workspace,
                timeout_seconds=verdict.effective_timeout_seconds,
                resource_budget=res_budget,
                permission_level=verdict.effective_level,
                success_criteria=plan.success_criteria,
                verification=[v.model_dump(mode="json") for v in plan.verification],
                required_capability=plan.required_capability,
                status=TaskStatus.FAILED.value,
                attempt=0,
                max_attempts=1 + self.max_retries,
                tried_agents=[],
                outcome={"error": "no healthy external agent available"},
            )
            return {
                "task_id": task_id,
                "status": TaskStatus.FAILED.value,
                "error": "no healthy external agent available",
            }

        chosen_id = adapter.capability.agent_id
        await self.repo.create_task(
            id=task_uuid,
            plan_id=plan_id,
            goal_id=goal_id,
            cycle_id=cycle_id,
            objective=plan.objective,
            description=effective_description,
            assigned_agent=chosen_id,
            expected_output=plan.expected_output,
            workspace=workspace,
            timeout_seconds=verdict.effective_timeout_seconds,
            resource_budget=res_budget,
            permission_level=verdict.effective_level,
            success_criteria=plan.success_criteria,
            verification=[v.model_dump(mode="json") for v in plan.verification],
            required_capability=plan.required_capability,
            status=TaskStatus.CREATED.value,
            idempotency_key=f"task:{task_id}",
            attempt=0,
            max_attempts=1 + self.max_retries,
            tried_agents=[],
            outcome={},
        )
        await self.state_machine.transition(
            task_uuid,
            TaskStatus.QUEUED,
            actor="orchestrator",
            reason="plan approved and queued for dispatch",
            idempotency_key=f"queue:{task_id}:init",
        )

        exec_id = await self._launch_attempt(task_uuid, chosen_id, verdict.sandbox_mode, cycle_id=cycle_id)
        return {
            "task_id": task_id,
            "execution_id": exec_id,
            "assigned_agent": chosen_id,
            "workspace": workspace,
            "status": TaskStatus.RUNNING.value,
        }

    async def _launch_attempt(
        self,
        task_uuid: uuid.UUID,
        agent_id: str,
        sandbox_mode: str,
        cycle_id: uuid.UUID | None = None,
    ) -> str:
        task_row = await self.repo.get_task(task_uuid)
        assert task_row is not None
        adapter = self.registry.get(agent_id)
        if adapter is None:
            raise RuntimeError(f"Agent '{agent_id}' not registered")

        attempt = int(task_row["attempt"]) + 1
        tried = list(task_row.get("tried_agents") or [])
        if agent_id not in tried:
            tried.append(agent_id)

        exec_uuid = uuid.uuid4()
        exec_id = str(exec_uuid)
        exec_idem = f"exec:{task_uuid}:{attempt}"

        # Atomically persist execution record + DISPATCHING transition in a single DB transaction
        async with self.repo.transaction() as s:
            await self.repo.create_execution(
                task_uuid,
                agent_id,
                ExecutionStatus.DISPATCHING.value,
                execution_id=exec_uuid,
                attempt_number=attempt,
                idempotency_key=exec_idem,
                session=s,
            )
            cur_status = TaskStatus(task_row["status"])
            if cur_status not in {TaskStatus.DISPATCHING, TaskStatus.DISPATCHED}:
                await self.state_machine.transition(
                    task_uuid,
                    TaskStatus.DISPATCHING,
                    execution_id=exec_uuid,
                    actor="orchestrator",
                    reason=f"dispatching attempt {attempt} on '{agent_id}'",
                    idempotency_key=f"dispatch:{exec_id}",
                    task_updates={"assigned_agent": agent_id, "attempt": attempt, "tried_agents": tried},
                    session=s,
                )
            else:
                await self.repo.update_task(
                    task_uuid,
                    assigned_agent=agent_id,
                    attempt=attempt,
                    tried_agents=tried,
                    session=s,
                )

        await self.repo.record_usage(
            category="agent_task",
            amount=1.0,
            seconds=0.0,
            details={"agent_id": agent_id, "attempt": attempt, "execution_id": exec_id},
            cycle_id=cycle_id,
            task_id=task_uuid,
        )

        agent_task = AgentTask(
            task_id=str(task_uuid),
            execution_id=exec_id,
            objective=task_row["objective"],
            description=task_row["description"],
            assigned_agent=agent_id,
            expected_output=task_row["expected_output"],
            workspace=task_row["workspace"],
            timeout_seconds=task_row["timeout_seconds"],
            resource_budget={**(task_row["resource_budget"] or {}), "execution_idempotency_key": exec_idem},
            permission_level=task_row["permission_level"],
            success_criteria=task_row["success_criteria"],
            sandbox_mode=sandbox_mode,
        )

        try:
            await self.backend.launch(adapter, agent_task)
        except Exception as exc:
            logger.error("orchestrator.launch_failed", task_id=str(task_uuid), execution_id=exec_id, error=str(exc))
            await self.state_machine.transition(
                task_uuid,
                TaskStatus.RECOVERY_REQUIRED,
                execution_id=exec_uuid,
                actor="orchestrator",
                reason=f"launch handshake failed: {exc}",
                idempotency_key=f"launch_err:{exec_id}",
            )
            await self.repo.create_recovery_record(
                execution_id=exec_uuid,
                task_id=task_uuid,
                trigger="launch_handshake_error",
                previous_status=ExecutionStatus.DISPATCHING.value,
                resolved_status=TaskStatus.RECOVERY_REQUIRED.value,
                action_taken="marked_recovery_required",
                notes=str(exc),
            )
            raise

        launch_meta = self.backend.get_launch_metadata(str(task_uuid))
        exec_updates: dict[str, Any] = {"status": ExecutionStatus.RUNNING.value}
        if launch_meta.get("runner_id"):
            exec_updates["runner_id"] = launch_meta["runner_id"]
        if launch_meta.get("pid") is not None:
            exec_updates["pid"] = int(launch_meta["pid"])
        if launch_meta.get("process_identity"):
            exec_updates["process_identity"] = launch_meta["process_identity"]

        async with self.repo.transaction() as s:
            await self.repo.update_execution(exec_uuid, session=s, **exec_updates)
            await self.state_machine.transition(
                task_uuid,
                TaskStatus.RUNNING,
                execution_id=exec_uuid,
                actor="orchestrator",
                reason=f"execution {exec_id} running on '{agent_id}'",
                idempotency_key=f"running:{exec_id}",
                session=s,
            )
        logger.info("orchestrator.dispatched", task_id=str(task_uuid), execution_id=exec_id, agent=agent_id, attempt=attempt)
        return exec_id

    async def poll_task(self, task_id: str) -> dict[str, Any]:
        """Non-blocking check: if the current execution finished, verify it and handle retry/fallback."""
        task_uuid = uuid.UUID(task_id)
        if not self.backend.is_running(task_id):
            row = await self.repo.get_task(task_uuid)
            return row or {"task_id": task_id, "status": TaskStatus.FAILED.value, "error": "not found"}

        execution = await self.backend.poll(task_id)
        if execution is None:
            row = await self.repo.get_task(task_uuid)
            return row or {"task_id": task_id, "status": TaskStatus.RUNNING.value}

        return await self._collect_and_decide(task_uuid, execution)

    async def wait_for_task(
        self, task_id: str, max_wait_seconds: float, poll_interval: float = 1.0
    ) -> dict[str, Any]:
        """Wait up to ``max_wait_seconds`` for task completion (including retries) without blocking forever."""
        deadline = asyncio.get_running_loop().time() + max(0.1, max_wait_seconds)
        while True:
            state = await self.poll_task(task_id)
            status = TaskStatus(state["status"])
            if status.is_terminal:
                return state
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return state
            if self.backend.is_running(task_id):
                await self.backend.wait_step(task_id, timeout=min(poll_interval, remaining))
            else:
                await asyncio.sleep(min(poll_interval, remaining))

    async def _collect_and_decide(self, task_uuid: uuid.UUID, execution: AgentExecution) -> dict[str, Any]:
        task_row = await self.repo.get_task(task_uuid)
        assert task_row is not None
        exec_uuid = uuid.UUID(execution.execution_id)

        verification: VerificationResult = self.verifier.verify(
            execution=execution,
            workspace=task_row["workspace"],
            specs=task_row.get("verification") or [],
        )

        AGENT_EXECUTIONS.labels(agent=execution.agent_id, status=execution.status).inc()
        await self.repo.update_execution(
            exec_uuid,
            status=execution.status,
            pid=execution.pid,
            thread_id=execution.thread_id,
            completed_at=execution.completed_at or utcnow(),
            result=execution.result,
            stdout=execution.stdout,
            stderr=execution.stderr,
            error=execution.error,
            resource_usage=execution.resource_usage,
            verification=verification.model_dump(mode="json"),
        )

        # P1-1 / T9: attribute external agent tokens (e.g. Codex sharing AOC vLLM) to unified LLM ledger
        res_usage = execution.resource_usage or {}
        in_tok = int(res_usage.get("input_tokens") or res_usage.get("prompt_tokens") or 0)
        out_tok = int(res_usage.get("output_tokens") or res_usage.get("completion_tokens") or 0)
        tot_tok = int(res_usage.get("total_tokens") or (in_tok + out_tok))
        if tot_tok > 0:
            cycle_uuid = uuid.UUID(task_row["cycle_id"]) if task_row.get("cycle_id") else None
            wall_s = float(res_usage.get("wall_seconds", 0.0))
            if self.llm_gateway is not None:
                await self.llm_gateway.record_external_usage(
                    source=execution.agent_id,
                    total_tokens=tot_tok,
                    prompt_tokens=in_tok,
                    completion_tokens=out_tok,
                    seconds=wall_s,
                    cycle_id=cycle_uuid,
                    task_id=task_uuid,
                    details={"execution_id": execution.execution_id},
                )
            else:
                await self.repo.record_usage(
                    category="llm",
                    amount=float(tot_tok),
                    seconds=wall_s,
                    details={
                        "source": execution.agent_id,
                        "external_agent": True,
                        "prompt_tokens": in_tok,
                        "completion_tokens": out_tok,
                        "execution_id": execution.execution_id,
                    },
                    cycle_id=cycle_uuid,
                    task_id=task_uuid,
                )

        # Durable State Machine transitions for execution completion
        if execution.status == ExecutionStatus.TIMEOUT.value:
            await self.state_machine.transition(
                task_uuid,
                TaskStatus.TIMEOUT,
                execution_id=exec_uuid,
                actor="orchestrator",
                reason=execution.error or "execution timed out",
                idempotency_key=f"timeout:{execution.execution_id}",
            )
        else:
            await self.state_machine.transition(
                task_uuid,
                TaskStatus.RESULT_RECEIVED,
                execution_id=exec_uuid,
                actor="orchestrator",
                reason=f"result received ({execution.status})",
                idempotency_key=f"recv:{execution.execution_id}",
            )
            await self.state_machine.transition(
                task_uuid,
                TaskStatus.VERIFYING,
                execution_id=exec_uuid,
                actor="orchestrator",
                reason="deterministic verification started",
                idempotency_key=f"verify:{execution.execution_id}",
            )

        if execution.status == ExecutionStatus.SUCCEEDED.value and verification.passed:
            outcome = {
                "execution_id": execution.execution_id,
                "agent_id": execution.agent_id,
                "verification": verification.model_dump(mode="json"),
                "result": execution.result,
                "status": ExecutionStatus.SUCCEEDED.value,
            }
            await self.state_machine.transition(
                task_uuid,
                TaskStatus.SUCCEEDED,
                execution_id=exec_uuid,
                actor="orchestrator",
                reason="deterministic verification passed",
                idempotency_key=f"succeeded:{execution.execution_id}",
                task_updates={"outcome": outcome},
                outbox_events=[
                    {
                        "event_type": OutboxEventType.EXECUTION_FINALIZE,
                        "idempotency_key": f"finalize:{execution.execution_id}",
                        "payload": {"execution_id": execution.execution_id, "task_id": str(task_uuid)},
                        "aggregate_type": "execution",
                        "aggregate_id": exec_uuid,
                    }
                ],
            )
            logger.info("orchestrator.succeeded", task_id=str(task_uuid), agent=execution.agent_id)
            return (await self.repo.get_task(task_uuid)) or outcome

        # Failure path (either execution failed/timed out OR deterministic verification failed)
        fail_reason = execution.error or verification.summary
        attempt = int(task_row["attempt"])
        max_attempts = int(task_row["max_attempts"])
        is_timeout = execution.status == ExecutionStatus.TIMEOUT.value
        effective_max_attempts = min(max_attempts, 2) if is_timeout else max_attempts
        tried = set(task_row.get("tried_agents") or [])
        sandbox_mode = (task_row.get("resource_budget") or {}).get("sandbox_mode", "read-only")

        logger.warning(
            "orchestrator.attempt_failed",
            task_id=str(task_uuid),
            agent=execution.agent_id,
            status=execution.status,
            attempt=attempt,
            max_attempts=effective_max_attempts,
            reason=fail_reason,
        )
        try:
            cycle_uuid_log = uuid.UUID(task_row["cycle_id"]) if task_row.get("cycle_id") else None
            bucket = classify_attempt_failure_bucket(execution.status, fail_reason)
            await self.repo.log_event(
                "orchestrator.attempt_failed",
                level="warning",
                message=(
                    f"Attempt {attempt}/{effective_max_attempts} failed [{bucket}] on "
                    f"{execution.agent_id}: {str(fail_reason or '')[:200]}"
                ),
                payload={
                    "task_id": str(task_uuid),
                    "execution_id": execution.execution_id,
                    "agent_id": execution.agent_id,
                    "status": execution.status,
                    "attempt": attempt,
                    "max_attempts": effective_max_attempts,
                    "failure_bucket": bucket,
                    "reason": str(fail_reason or "")[:300],
                    "objective": str(task_row.get("objective") or "")[:160],
                },
                cycle_id=cycle_uuid_log,
            )
        except Exception:
            pass

        if attempt < effective_max_attempts:
            # Prefer switching to an untried healthy agent via explainable AgentRouter; otherwise retry same healthy agent
            cycle_uuid = uuid.UUID(task_row["cycle_id"]) if task_row.get("cycle_id") else None
            alt, _ = await self.router.select(
                required_capability=task_row.get("required_capability"),
                sandbox_mode=sandbox_mode,
                exclude=tried,
                cycle_id=cycle_uuid,
                log_decision=True,
            )
            if alt is not None:
                next_adapter = alt
            else:
                next_adapter, _ = await self.router.select(
                    preferred_agent=execution.agent_id,
                    required_capability=task_row.get("required_capability"),
                    sandbox_mode=sandbox_mode,
                    cycle_id=cycle_uuid,
                    log_decision=True,
                )
            if next_adapter is not None:
                boosted_timeout: int | None = None
                if is_timeout:
                    max_budget_t = int(
                        getattr(
                            getattr(getattr(self.governance, "settings", None), "budget", None),
                            "max_task_timeout_seconds",
                            600,
                        )
                        or 600
                    )
                    cur_t = int(task_row.get("timeout_seconds") or 300)
                    target_t = min(max_budget_t, max(cur_t, 500))
                    if target_t > cur_t:
                        boosted_timeout = target_t
                        await self.repo.update_task(task_uuid, timeout_seconds=boosted_timeout)

                # V2.1 P0-1: explicitly finalize superseded retry attempt at execution level
                superseded_ver = dict(verification.model_dump(mode="json"))
                superseded_ver["superseded_by_retry"] = True
                superseded_ver["superseded_at"] = utcnow().isoformat()
                superseded_ver["next_attempt"] = attempt + 1
                await self.repo.update_execution(
                    exec_uuid,
                    finalized_at=utcnow(),
                    verification=superseded_ver,
                )
                raw_backoff_mult = float((task_row.get("resource_budget") or {}).get("retry_backoff_multiplier", 1.0) or 1.0)
                backoff_mult = max(1.0, min(1.15, raw_backoff_mult))
                backoff_seconds = round(min(5.0, max(0.0, (backoff_mult - 1.0) * 10.0)), 3)
                await self.state_machine.transition(
                    task_uuid,
                    TaskStatus.RETRY_WAIT,
                    execution_id=exec_uuid,
                    actor="orchestrator",
                    reason=f"attempt {attempt} failed ({fail_reason}); scheduling retry (backoff={backoff_seconds}s)",
                    idempotency_key=f"retry_wait:{execution.execution_id}",
                )
                if backoff_seconds > 0.0:
                    await asyncio.sleep(backoff_seconds)
                await self.state_machine.transition(
                    task_uuid,
                    TaskStatus.QUEUED,
                    execution_id=exec_uuid,
                    actor="orchestrator",
                    reason=f"re-queued for attempt {attempt + 1} on '{next_adapter.capability.agent_id}'",
                    idempotency_key=f"requeue:{execution.execution_id}",
                )
                retry_payload: dict[str, Any] = {
                    "task_id": str(task_uuid),
                    "attempt": attempt + 1,
                    "retry_backoff_multiplier": round(backoff_mult, 4),
                    "backoff_seconds": backoff_seconds,
                }
                if boosted_timeout is not None:
                    retry_payload["boosted_timeout_seconds"] = boosted_timeout
                await self.repo.log_decision(
                    decision_type="agent_fallback_retry",
                    selected=next_adapter.capability.agent_id,
                    alternatives=sorted(tried),
                    reason=f"Previous attempt {attempt} on '{execution.agent_id}' failed ({fail_reason}); retrying on '{next_adapter.capability.agent_id}'",
                    payload=retry_payload,
                    cycle_id=cycle_uuid,
                )
                await self._launch_attempt(task_uuid, next_adapter.capability.agent_id, sandbox_mode, cycle_id=cycle_uuid)
                return (await self.repo.get_task(task_uuid)) or {"task_id": str(task_uuid), "status": TaskStatus.RUNNING.value}

        # Retries exhausted or no healthy agent left -> terminal FAILED (never mark as succeeded!)
        outcome = {
            "execution_id": execution.execution_id,
            "agent_id": execution.agent_id,
            "status": execution.status,
            "error": fail_reason,
            "verification": verification.model_dump(mode="json"),
            "attempts": attempt,
        }
        await self.state_machine.transition(
            task_uuid,
            TaskStatus.FAILED,
            execution_id=exec_uuid,
            actor="orchestrator",
            reason=fail_reason,
            idempotency_key=f"failed:{execution.execution_id}",
            task_updates={"outcome": outcome},
            outbox_events=[
                {
                    "event_type": OutboxEventType.EXECUTION_FINALIZE,
                    "idempotency_key": f"finalize:{execution.execution_id}",
                    "payload": {"execution_id": execution.execution_id, "task_id": str(task_uuid)},
                    "aggregate_type": "execution",
                    "aggregate_id": exec_uuid,
                }
            ],
        )
        return (await self.repo.get_task(task_uuid)) or outcome

    async def cancel_task(self, task_id: str, actor: str = "operator", reason: str = "Cancelled by operator/lifecycle") -> bool:
        task_uuid = uuid.UUID(task_id)
        task_row = await self.repo.get_task(task_uuid)
        if task_row is None:
            return False
        cur_status = TaskStatus(task_row["status"])
        if cur_status.is_terminal:
            return False

        adapter = self.registry.get(task_row["assigned_agent"])
        cancelled_bg, exec_id = await self.backend.cancel(task_id, adapter=adapter)

        outbox_evs: list[dict[str, Any]] = []
        exec_uuid: uuid.UUID | None = None
        if exec_id:
            exec_uuid = uuid.UUID(exec_id)
            await self.repo.update_execution(
                exec_uuid,
                status=ExecutionStatus.CANCELLED.value,
                completed_at=utcnow(),
                error=reason,
            )
            outbox_evs.append(
                {
                    "event_type": OutboxEventType.EXECUTION_FINALIZE,
                    "idempotency_key": f"finalize:{exec_id}",
                    "payload": {"execution_id": exec_id, "task_id": task_id},
                    "aggregate_type": "execution",
                    "aggregate_id": exec_uuid,
                }
            )
        elif not cancelled_bg and cur_status not in {
            TaskStatus.QUEUED,
            TaskStatus.PENDING,
            TaskStatus.RETRY_WAIT,
            TaskStatus.REQUIRES_APPROVAL,
            TaskStatus.AWAITING_APPROVAL,
            TaskStatus.RUNNING,
            TaskStatus.DISPATCHING,
            TaskStatus.DISPATCHED,
        }:
            return False

        await self.state_machine.transition(
            task_uuid,
            TaskStatus.CANCELLED,
            execution_id=exec_uuid,
            actor=actor,
            reason=reason,
            idempotency_key=f"cancel:{task_id}",
            task_updates={"outcome": {"cancelled_by": actor, "reason": reason, "execution_id": exec_id}},
            outbox_events=outbox_evs,
        )
        return True

    async def sync_runner_heartbeat(self) -> dict[str, Any] | None:
        """Query host Runner health over UDS and persist status in runner_registry (P1-3)."""
        from app.runner.client import RunnerUnavailableError, UnixSocketRunnerBackend

        if not isinstance(self.backend, UnixSocketRunnerBackend):
            return None
        try:
            h = await self.backend.client.health()
            return await self.repo.upsert_runner(
                runner_id=h.runner_id,
                socket_path=h.socket_path,
                host_boot_id=h.host_boot_id,
                systemd_unit=h.systemd_unit,
                status="online",
                capabilities={
                    "cgroup_delegated": h.cgroup_delegated,
                    "cgroup_root": h.cgroup_root,
                    "state_dir": h.state_dir,
                    "active_executions": h.active_executions,
                },
                last_heartbeat_at=utcnow(),
            )
        except RunnerUnavailableError as exc:
            logger.warning("orchestrator.runner_heartbeat_failed", runner_id=self.backend.runner_id, error=str(exc))
            return await self.repo.upsert_runner(
                runner_id=self.backend.runner_id,
                socket_path=self.backend.client.socket_path,
                status="offline",
                capabilities={"last_error": str(exc)},
            )

    def _build_agent_task_from_row(self, task_row: dict[str, Any], exec_id: str) -> AgentTask:
        sandbox_mode = (task_row.get("resource_budget") or {}).get("sandbox_mode", "read-only")
        return AgentTask(
            task_id=task_row["task_id"],
            execution_id=exec_id,
            objective=task_row["objective"],
            description=task_row.get("description", ""),
            assigned_agent=task_row["assigned_agent"],
            expected_output=task_row.get("expected_output", ""),
            workspace=task_row["workspace"],
            timeout_seconds=int(task_row["timeout_seconds"]),
            resource_budget=dict(task_row.get("resource_budget") or {}),
            permission_level=task_row["permission_level"],
            success_criteria=list(task_row.get("success_criteria") or []),
            sandbox_mode=sandbox_mode,
        )

    async def reconcile_task(self, task_id: str, trigger: str = "manual_reconcile") -> dict[str, Any]:
        """Reconcile a single task's executions against Host Agent Runner or local OS state (P1-3)."""
        from app.runner.client import RunnerUnavailableError, UnixSocketRunnerBackend

        task_uuid = uuid.UUID(task_id)
        task_row = await self.repo.get_task(task_uuid)
        if task_row is None:
            return {"task_id": task_id, "action": "not_found"}

        cur_status = TaskStatus(task_row["status"])
        execs = await self.repo.list_executions(task_id=task_uuid, limit=10)
        latest_exec = execs[0] if execs else None

        # Case 6: terminal execution persisted in DB, but finalization Outbox/TaskFinalizer hasn't completed yet
        if cur_status.is_terminal and cur_status != TaskStatus.RECOVERY_REQUIRED:
            if latest_exec and latest_exec.get("finalized_at") is None:
                exec_uuid = uuid.UUID(latest_exec["execution_id"])
                await self.repo.enqueue_outbox_event(
                    event_type=OutboxEventType.EXECUTION_FINALIZE,
                    idempotency_key=f"finalize:{latest_exec['execution_id']}",
                    payload={"execution_id": latest_exec["execution_id"], "task_id": task_id},
                    aggregate_type="execution",
                    aggregate_id=exec_uuid,
                )
                await self.repo.create_recovery_record(
                    execution_id=exec_uuid,
                    task_id=task_uuid,
                    runner_id=latest_exec.get("runner_id"),
                    trigger=trigger,
                    observed_identity=latest_exec.get("process_identity") or {},
                    previous_status=cur_status.value,
                    resolved_status=cur_status.value,
                    action_taken="reenqueued_unfinalized_outbox",
                    notes="Terminal execution lacked finalized_at; ensured execution.finalize outbox event exists",
                )
                return {"task_id": task_id, "status": cur_status.value, "action": "reenqueued_unfinalized_outbox"}
            return {"task_id": task_id, "status": cur_status.value, "action": "already_terminal"}

        if not isinstance(self.backend, UnixSocketRunnerBackend):
            return {"task_id": task_id, "status": cur_status.value, "action": "noop_local_backend"}

        if latest_exec is None:
            return {"task_id": task_id, "status": cur_status.value, "action": "no_execution_to_reconcile"}

        exec_id = latest_exec["execution_id"]
        exec_uuid = uuid.UUID(exec_id)
        idem_key = latest_exec.get("idempotency_key") or ""

        try:
            rec = await self.backend.client.get_execution(exec_id)
            if rec is None and idem_key:
                rec = await self.backend.client.get_execution_by_key(idem_key)
        except RunnerUnavailableError as exc:
            # Runner unreachable -> NEVER guess success or blindly re-run!
            if cur_status != TaskStatus.RECOVERY_REQUIRED:
                await self.state_machine.transition(
                    task_uuid,
                    TaskStatus.RECOVERY_REQUIRED,
                    execution_id=exec_uuid,
                    actor="reconciler",
                    reason=f"Runner unreachable during {trigger}: {exc}",
                    idempotency_key=f"reconcile_offline:{exec_id}:{trigger}:{task_row['state_version']}",
                )
            await self.repo.create_recovery_record(
                execution_id=exec_uuid,
                task_id=task_uuid,
                runner_id=latest_exec.get("runner_id") or self.backend.runner_id,
                trigger=trigger,
                observed_identity=latest_exec.get("process_identity") or {},
                previous_status=cur_status.value,
                resolved_status=TaskStatus.RECOVERY_REQUIRED.value,
                action_taken="marked_recovery_required_runner_offline",
                notes=str(exc),
            )
            return {
                "task_id": task_id,
                "execution_id": exec_id,
                "status": TaskStatus.RECOVERY_REQUIRED.value,
                "action": "marked_recovery_required_runner_offline",
            }

        # Case A: Execution record not found on reachable Runner (e.g. Core died in DISPATCHING before RPC reached Runner)
        if rec is None:
            is_idempotent_l0 = task_row["permission_level"] == PermissionLevel.L0.value
            if is_idempotent_l0 and cur_status in {TaskStatus.DISPATCHING, TaskStatus.DISPATCHED, TaskStatus.RECOVERY_REQUIRED}:
                await self.repo.update_execution(
                    exec_uuid,
                    status=ExecutionStatus.FAILED.value,
                    completed_at=utcnow(),
                    error="Execution never reached host runner before crash (L0 confirmed absent)",
                )
                if cur_status != TaskStatus.RECOVERY_REQUIRED:
                    await self.state_machine.transition(
                        task_uuid,
                        TaskStatus.RECOVERY_REQUIRED,
                        execution_id=exec_uuid,
                        actor="reconciler",
                        reason="Execution absent on runner after crash",
                        idempotency_key=f"reconcile_missing_rr:{exec_id}",
                    )
                await self.state_machine.transition(
                    task_uuid,
                    TaskStatus.FAILED,
                    execution_id=exec_uuid,
                    actor="reconciler",
                    reason="Confirmed absent on host runner (L0 read-only)",
                    idempotency_key=f"reconcile_missing_fail:{exec_id}",
                    task_updates={"outcome": {"execution_id": exec_id, "error": "Execution absent on host runner"}},
                    outbox_events=[
                        {
                            "event_type": OutboxEventType.EXECUTION_FINALIZE,
                            "idempotency_key": f"finalize:{exec_id}",
                            "payload": {"execution_id": exec_id, "task_id": task_id},
                            "aggregate_type": "execution",
                            "aggregate_id": exec_uuid,
                        }
                    ],
                )
                await self.repo.create_recovery_record(
                    execution_id=exec_uuid,
                    task_id=task_uuid,
                    runner_id=self.backend.runner_id,
                    trigger=trigger,
                    previous_status=cur_status.value,
                    resolved_status=TaskStatus.FAILED.value,
                    action_taken="marked_failed_absent_on_runner",
                    notes="Execution record not present on host runner; L0 task safely failed",
                )
                return {"task_id": task_id, "execution_id": exec_id, "status": TaskStatus.FAILED.value, "action": "marked_failed_absent_on_runner"}

            if cur_status != TaskStatus.RECOVERY_REQUIRED:
                await self.state_machine.transition(
                    task_uuid,
                    TaskStatus.RECOVERY_REQUIRED,
                    execution_id=exec_uuid,
                    actor="reconciler",
                    reason="Non-L0 execution absent on runner; requires operator confirmation",
                    idempotency_key=f"reconcile_missing_nonl0:{exec_id}:{task_row['state_version']}",
                )
            await self.repo.create_recovery_record(
                execution_id=exec_uuid,
                task_id=task_uuid,
                runner_id=self.backend.runner_id,
                trigger=trigger,
                previous_status=cur_status.value,
                resolved_status=TaskStatus.RECOVERY_REQUIRED.value,
                action_taken="marked_recovery_required_absent",
                notes="Execution absent on host runner for non-L0 task; halted for safety",
            )
            return {"task_id": task_id, "execution_id": exec_id, "status": TaskStatus.RECOVERY_REQUIRED.value, "action": "marked_recovery_required_absent"}

        obs_ident = rec.process_identity.model_dump(mode="json") if rec.process_identity else {}

        # Case B: Process confirmed alive & running on Runner -> re-attach monitoring without re-running!
        if rec.status in {ExecutionStatus.RUNNING.value, ExecutionStatus.STARTING.value} and rec.alive:
            adapter = self.registry.get(task_row["assigned_agent"])
            agent_task = self._build_agent_task_from_row(task_row, exec_id)
            if adapter is not None:
                self.backend.reattach(
                    task_id=task_id,
                    execution_id=exec_id,
                    adapter=adapter,
                    task=agent_task,
                    runner_id=rec.runner_id,
                    process_identity=obs_ident,
                )
            await self.repo.update_execution(
                exec_uuid,
                status=ExecutionStatus.RUNNING.value,
                runner_id=rec.runner_id,
                pid=rec.process_identity.pid if rec.process_identity else None,
                process_identity=obs_ident,
            )
            if cur_status != TaskStatus.RUNNING:
                await self.state_machine.transition(
                    task_uuid,
                    TaskStatus.RUNNING,
                    execution_id=exec_uuid,
                    actor="reconciler",
                    reason=f"Re-attached to live host runner execution ({rec.identity_check})",
                    idempotency_key=f"reattach:{exec_id}:{task_row['state_version']}",
                )
            await self.repo.create_recovery_record(
                execution_id=exec_uuid,
                task_id=task_uuid,
                runner_id=rec.runner_id,
                trigger=trigger,
                observed_identity=obs_ident,
                previous_status=cur_status.value,
                resolved_status=TaskStatus.RUNNING.value,
                action_taken="reattached_running",
                notes=f"Verified live process via {rec.identity_check}",
            )
            return {"task_id": task_id, "execution_id": exec_id, "status": TaskStatus.RUNNING.value, "action": "reattached_running"}

        # Case C: Runner already holds terminal execution result -> recover & feed through _collect_and_decide!
        if rec.status in {
            ExecutionStatus.SUCCEEDED.value,
            ExecutionStatus.FAILED.value,
            ExecutionStatus.TIMEOUT.value,
            ExecutionStatus.CANCELLED.value,
        }:
            await self.repo.update_execution(
                exec_uuid,
                runner_id=rec.runner_id,
                pid=rec.process_identity.pid if rec.process_identity else None,
                process_identity=obs_ident,
            )
            if cur_status in {TaskStatus.DISPATCHING, TaskStatus.DISPATCHED, TaskStatus.RECOVERY_REQUIRED}:
                await self.state_machine.transition(
                    task_uuid,
                    TaskStatus.RUNNING,
                    execution_id=exec_uuid,
                    actor="reconciler",
                    reason="Runner holds terminal execution result; advancing to collect",
                    idempotency_key=f"reconcile_to_run:{exec_id}:{task_row['state_version']}",
                )
            adapter = self.registry.get(task_row["assigned_agent"])
            agent_task = self._build_agent_task_from_row(task_row, exec_id)
            agent_exec = self.backend.convert_record(adapter, agent_task, rec)
            final_task = await self._collect_and_decide(task_uuid, agent_exec)
            resolved_st = str(final_task.get("status", TaskStatus.FAILED.value))
            await self.repo.create_recovery_record(
                execution_id=exec_uuid,
                task_id=task_uuid,
                runner_id=rec.runner_id,
                trigger=trigger,
                observed_identity=obs_ident,
                previous_status=cur_status.value,
                resolved_status=resolved_st,
                action_taken="recovered_terminal_result",
                notes=f"Recovered runner terminal status={rec.status} exit_code={rec.exit_code}",
            )
            return {
                "task_id": task_id,
                "execution_id": exec_id,
                "status": resolved_st,
                "action": "recovered_terminal_result",
            }

        # Fallback: indeterminate runner state -> RECOVERY_REQUIRED
        if cur_status != TaskStatus.RECOVERY_REQUIRED:
            await self.state_machine.transition(
                task_uuid,
                TaskStatus.RECOVERY_REQUIRED,
                execution_id=exec_uuid,
                actor="reconciler",
                reason=f"Indeterminate runner state ({rec.status}, alive={rec.alive}, check={rec.identity_check})",
                idempotency_key=f"reconcile_indet:{exec_id}:{task_row['state_version']}",
            )
        await self.repo.create_recovery_record(
            execution_id=exec_uuid,
            task_id=task_uuid,
            runner_id=rec.runner_id,
            trigger=trigger,
            observed_identity=obs_ident,
            previous_status=cur_status.value,
            resolved_status=TaskStatus.RECOVERY_REQUIRED.value,
            action_taken="marked_recovery_required_indeterminate",
            notes=f"runner_status={rec.status} alive={rec.alive} check={rec.identity_check}",
        )
        return {
            "task_id": task_id,
            "execution_id": exec_id,
            "status": TaskStatus.RECOVERY_REQUIRED.value,
            "action": "marked_recovery_required_indeterminate",
        }

    async def recover_on_startup(self) -> dict[str, int]:
        """Reconcile any tasks/executions left in-flight after an abrupt crash (P1-3 + Scenario E).

        Never re-runs already SUCCEEDED tasks.
        """
        from app.runner.client import UnixSocketRunnerBackend

        if isinstance(self.backend, UnixSocketRunnerBackend):
            await self.sync_runner_heartbeat()
            reattached = 0
            recovered_terminal = 0
            marked_recovery = 0
            reenqueued = 0

            inflight_tasks = await self.repo.list_tasks(
                statuses=[
                    TaskStatus.RUNNING.value,
                    TaskStatus.DISPATCHING.value,
                    TaskStatus.DISPATCHED.value,
                    TaskStatus.VERIFYING.value,
                    TaskStatus.RECOVERY_REQUIRED.value,
                    TaskStatus.ORPHANED.value,
                ],
                limit=100,
            )
            for t in inflight_tasks:
                res = await self.reconcile_task(t["task_id"], trigger="startup")
                act = res.get("action", "")
                if act == "reattached_running":
                    reattached += 1
                elif act in {"recovered_terminal_result", "marked_failed_absent_on_runner"}:
                    recovered_terminal += 1
                elif act.startswith("marked_recovery_required"):
                    marked_recovery += 1

            # Also guarantee Case 6: terminal executions without finalized_at get outbox re-enqueued
            unfinalized = await self.repo.list_unfinalized_terminal_executions(limit=50)
            for ex in unfinalized:
                await self.repo.enqueue_outbox_event(
                    event_type=OutboxEventType.EXECUTION_FINALIZE,
                    idempotency_key=f"finalize:{ex['execution_id']}",
                    payload={"execution_id": ex["execution_id"], "task_id": ex["task_id"]},
                    aggregate_type="execution",
                    aggregate_id=uuid.UUID(ex["execution_id"]),
                )
                reenqueued += 1

            summary = {
                "reconciled_tasks": reattached + recovered_terminal + marked_recovery,
                "reconciled_executions": reattached + recovered_terminal + marked_recovery,
                "reattached_running": reattached,
                "recovered_terminal": recovered_terminal,
                "recovery_required": marked_recovery,
                "reenqueued_unfinalized": reenqueued,
            }
            if any(summary.values()):
                await self.repo.log_event(
                    "orchestrator.recovered",
                    message="Host Runner startup reconciliation completed",
                    payload=summary,
                    level="warning" if marked_recovery else "info",
                )
            return summary

        # LocalSubprocessBackend fallback path (unit tests & non-runner mode)
        reconciled_execs = 0
        reconciled_tasks = 0

        running_execs = await self.repo.list_executions(
            statuses=[ExecutionStatus.RUNNING.value, ExecutionStatus.DISPATCHING.value, ExecutionStatus.PENDING.value],
            limit=100,
        )
        for ex in running_execs:
            pid = ex.get("pid")
            alive = False
            if pid:
                try:
                    os.kill(int(pid), 0)
                    alive = True
                except OSError:
                    alive = False
            if not alive:
                await self.repo.update_execution(
                    uuid.UUID(ex["execution_id"]),
                    status=ExecutionStatus.FAILED.value,
                    completed_at=utcnow(),
                    error="Orphaned execution recovered after AOC restart (process no longer running)",
                )
                await self.repo.create_recovery_record(
                    execution_id=uuid.UUID(ex["execution_id"]),
                    task_id=uuid.UUID(ex["task_id"]),
                    trigger="startup",
                    previous_status=ex["status"],
                    resolved_status=ExecutionStatus.FAILED.value,
                    action_taken="local_orphan_marked_failed",
                    notes="Process no longer running on local backend startup",
                )
                reconciled_execs += 1

        inflight_tasks = await self.repo.list_tasks(
            statuses=[
                TaskStatus.RUNNING.value,
                TaskStatus.DISPATCHING.value,
                TaskStatus.DISPATCHED.value,
                TaskStatus.VERIFYING.value,
            ],
            limit=100,
        )
        for t in inflight_tasks:
            if self.backend.is_running(t["task_id"]):
                continue
            await self.repo.update_task(
                uuid.UUID(t["task_id"]),
                status=TaskStatus.FAILED.value,
                outcome={
                    "error": "Interrupted by AOC process restart",
                    "recovered_at": utcnow().isoformat(),
                },
            )
            reconciled_tasks += 1

        if reconciled_execs or reconciled_tasks:
            await self.repo.log_event(
                "orchestrator.recovered",
                message=f"Recovered {reconciled_tasks} task(s) and {reconciled_execs} execution(s) after restart",
                payload={"tasks": reconciled_tasks, "executions": reconciled_execs},
                level="warning",
            )
        return {"reconciled_tasks": reconciled_tasks, "reconciled_executions": reconciled_execs}

    def has_inflight(self) -> bool:
        return len(self.backend.list_running_task_ids()) > 0

    def list_inflight_task_ids(self) -> list[str]:
        return self.backend.list_running_task_ids()
