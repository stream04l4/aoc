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
    ) -> None:
        self.registry = registry
        self.governance = governance
        self.repo = repo
        self.verifier = verifier or ResultVerifier()
        self.max_retries = max(0, int(max_retries))
        self.llm_gateway = llm_gateway
        self.backend: ExecutionBackendInterface = backend or LocalSubprocessBackend()
        self.state_machine = state_machine or TaskStateMachine(repo)
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
                deterministic_level=verdict.effective_level,
                semantic_level=None,
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
    ) -> dict[str, Any]:
        """Persist a task from an approved ActionPlan and dispatch it asynchronously."""
        task_uuid = uuid.uuid4()
        task_id = str(task_uuid)
        workspace = self.governance.prepare_workspace(task_id)

        adapter = self.registry.select_agent(
            preferred_agent=plan.assigned_agent,
            required_capability=plan.required_capability,
        )
        if adapter is None:
            await self.repo.create_task(
                id=task_uuid,
                plan_id=plan_id,
                goal_id=goal_id,
                cycle_id=cycle_id,
                objective=plan.objective,
                description=plan.description,
                assigned_agent=plan.assigned_agent,
                expected_output=plan.expected_output,
                workspace=workspace,
                timeout_seconds=verdict.effective_timeout_seconds,
                resource_budget={"sandbox_mode": verdict.sandbox_mode},
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
            description=plan.description,
            assigned_agent=chosen_id,
            expected_output=plan.expected_output,
            workspace=workspace,
            timeout_seconds=verdict.effective_timeout_seconds,
            resource_budget={"sandbox_mode": verdict.sandbox_mode},
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
            resource_budget=task_row["resource_budget"],
            permission_level=task_row["permission_level"],
            success_criteria=task_row["success_criteria"],
            sandbox_mode=sandbox_mode,
        )

        await self.backend.launch(adapter, agent_task)
        async with self.repo.transaction() as s:
            await self.repo.update_execution(exec_uuid, status=ExecutionStatus.RUNNING.value, session=s)
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
        tried = set(task_row.get("tried_agents") or [])
        sandbox_mode = (task_row.get("resource_budget") or {}).get("sandbox_mode", "read-only")

        logger.warning(
            "orchestrator.attempt_failed",
            task_id=str(task_uuid),
            agent=execution.agent_id,
            status=execution.status,
            attempt=attempt,
            max_attempts=max_attempts,
            reason=fail_reason,
        )

        if attempt < max_attempts:
            # Prefer switching to an untried healthy agent first; otherwise retry same healthy agent
            alt = self.registry.select_agent(
                required_capability=task_row.get("required_capability"),
                exclude=tried,
            )
            next_adapter = alt or self.registry.select_agent(
                preferred_agent=execution.agent_id,
                required_capability=task_row.get("required_capability"),
            )
            if next_adapter is not None:
                cycle_uuid = uuid.UUID(task_row["cycle_id"]) if task_row.get("cycle_id") else None
                await self.state_machine.transition(
                    task_uuid,
                    TaskStatus.RETRY_WAIT,
                    execution_id=exec_uuid,
                    actor="orchestrator",
                    reason=f"attempt {attempt} failed ({fail_reason}); scheduling retry",
                    idempotency_key=f"retry_wait:{execution.execution_id}",
                )
                await self.state_machine.transition(
                    task_uuid,
                    TaskStatus.QUEUED,
                    execution_id=exec_uuid,
                    actor="orchestrator",
                    reason=f"re-queued for attempt {attempt + 1} on '{next_adapter.capability.agent_id}'",
                    idempotency_key=f"requeue:{execution.execution_id}",
                )
                await self.repo.log_decision(
                    decision_type="agent_fallback_retry",
                    selected=next_adapter.capability.agent_id,
                    alternatives=sorted(tried),
                    reason=f"Previous attempt {attempt} on '{execution.agent_id}' failed ({fail_reason}); retrying on '{next_adapter.capability.agent_id}'",
                    payload={"task_id": str(task_uuid), "attempt": attempt + 1},
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

    async def recover_on_startup(self) -> dict[str, int]:
        """Reconcile any tasks/executions left RUNNING or DISPATCHED after an abrupt crash.

        Never re-runs already SUCCEEDED tasks (task book Scenario E).
        """
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
