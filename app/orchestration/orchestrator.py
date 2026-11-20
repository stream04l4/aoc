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
import os
import uuid
from typing import Any

from app.agents.base import AgentExecution, AgentTask
from app.agents.registry import AgentRegistry
from app.cognition.interfaces import ActionPlan
from app.core.enums import ExecutionStatus, TaskStatus
from app.governance.interfaces import GovernanceInterface, PolicyVerdict
from app.observability.logging import get_logger
from app.observability.metrics import AGENT_EXECUTIONS
from app.orchestration.verifier import ResultVerifier, VerificationResult
from app.perception.interfaces import utcnow
from app.storage.repository import StateRepository

logger = get_logger("aoc.orchestration")


class AgentOrchestrator:
    def __init__(
        self,
        registry: AgentRegistry,
        governance: GovernanceInterface,
        repo: StateRepository,
        verifier: ResultVerifier | None = None,
        max_retries: int = 2,
    ) -> None:
        self.registry = registry
        self.governance = governance
        self.repo = repo
        self.verifier = verifier or ResultVerifier()
        self.max_retries = max(0, int(max_retries))
        # In-process handles for currently running background executions: task_id -> (exec_id, asyncio.Task)
        self._inflight: dict[str, tuple[str, asyncio.Task[AgentExecution]]] = {}

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
            status=TaskStatus.PENDING.value,
            attempt=0,
            max_attempts=1 + self.max_retries,
            tried_agents=[],
            outcome={},
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
        await self.repo.create_execution(task_uuid, agent_id, ExecutionStatus.RUNNING.value, execution_id=exec_uuid)
        await self.repo.update_task(
            task_uuid,
            assigned_agent=agent_id,
            status=TaskStatus.RUNNING.value,
            attempt=attempt,
            tried_agents=tried,
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

        async def _run() -> AgentExecution:
            try:
                return await adapter.execute(agent_task)
            except Exception as exc:
                return AgentExecution(
                    execution_id=exec_id,
                    task_id=str(task_uuid),
                    agent_id=agent_id,
                    status=ExecutionStatus.FAILED.value,
                    completed_at=utcnow(),
                    error=f"Unhandled adapter exception: {exc}",
                )

        bg_task = asyncio.create_task(_run(), name=f"aoc-exec-{exec_id[:8]}")
        self._inflight[str(task_uuid)] = (exec_id, bg_task)
        logger.info("orchestrator.dispatched", task_id=str(task_uuid), execution_id=exec_id, agent=agent_id, attempt=attempt)
        return exec_id

    async def poll_task(self, task_id: str) -> dict[str, Any]:
        """Non-blocking check: if the current execution finished, verify it and handle retry/fallback."""
        task_uuid = uuid.UUID(task_id)
        entry = self._inflight.get(task_id)
        if entry is None:
            row = await self.repo.get_task(task_uuid)
            return row or {"task_id": task_id, "status": TaskStatus.FAILED.value, "error": "not found"}

        exec_id, bg_task = entry
        if not bg_task.done():
            # Keep DB updated with live PID if available
            row = await self.repo.get_task(task_uuid)
            return row or {"task_id": task_id, "status": TaskStatus.RUNNING.value}

        self._inflight.pop(task_id, None)
        execution = bg_task.result()
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
            entry = self._inflight.get(task_id)
            if entry is not None:
                _, bg = entry
                try:
                    await asyncio.wait_for(asyncio.shield(bg), timeout=min(poll_interval, remaining))
                except asyncio.TimeoutError:
                    pass
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

        if execution.status == ExecutionStatus.SUCCEEDED.value and verification.passed:
            outcome = {
                "execution_id": execution.execution_id,
                "agent_id": execution.agent_id,
                "verification": verification.model_dump(mode="json"),
                "result": execution.result,
                "status": ExecutionStatus.SUCCEEDED.value,
            }
            await self.repo.update_task(task_uuid, status=TaskStatus.SUCCEEDED.value, outcome=outcome)
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
        await self.repo.update_task(task_uuid, status=TaskStatus.FAILED.value, outcome=outcome)
        return (await self.repo.get_task(task_uuid)) or outcome

    async def cancel_task(self, task_id: str) -> bool:
        entry = self._inflight.get(task_id)
        if entry is None:
            return False
        exec_id, bg_task = entry
        task_row = await self.repo.get_task(uuid.UUID(task_id))
        if task_row:
            adapter = self.registry.get(task_row["assigned_agent"])
            if adapter:
                await adapter.cancel(exec_id)
        bg_task.cancel()
        try:
            await bg_task
        except Exception:
            pass
        self._inflight.pop(task_id, None)
        await self.repo.update_execution(
            uuid.UUID(exec_id),
            status=ExecutionStatus.CANCELLED.value,
            completed_at=utcnow(),
            error="Cancelled by operator/lifecycle",
        )
        await self.repo.update_task(uuid.UUID(task_id), status=TaskStatus.ABANDONED.value)
        return True

    async def recover_on_startup(self) -> dict[str, int]:
        """Reconcile any tasks/executions left RUNNING or DISPATCHED after an abrupt crash.

        Never re-runs already SUCCEEDED tasks (task book Scenario E).
        """
        reconciled_execs = 0
        reconciled_tasks = 0

        running_execs = await self.repo.list_executions(
            statuses=[ExecutionStatus.RUNNING.value, ExecutionStatus.PENDING.value], limit=100
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
            statuses=[TaskStatus.RUNNING.value, TaskStatus.DISPATCHED.value, TaskStatus.VERIFYING.value], limit=100
        )
        for t in inflight_tasks:
            if t["task_id"] in self._inflight:
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
        return len(self._inflight) > 0

    def list_inflight_task_ids(self) -> list[str]:
        return list(self._inflight.keys())
