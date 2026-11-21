"""Autonomous Loop (task book section 9).

Executes the strict 16-stage autonomous organism cycle:
  WAKE_UP -> LOAD_INTERNAL_STATE -> PERCEPTION -> SELF_ASSESSMENT ->
  DRIVE_SATISFACTION_CALCULATION -> DRIVE_ARBITRATION -> GOAL_GENERATION ->
  COGNITIVE_PLANNING -> POLICY_VALIDATION -> AGENT_DISPATCH -> WAIT_MONITOR ->
  OUTCOME_EVALUATION -> MEMORY_CONSOLIDATION -> STATE_UPDATE -> SLEEP_NEXT_CYCLE.

Key invariants:
  - Drive satisfaction is ALWAYS re-calculated from fresh post-action perception,
    never mutated by the brain's predicted improvement (task book 9.1).
  - Expected Outcome, Actual Outcome, and Measured Satisfaction Change are stored separately.
  - When all drives are satisfied (IDLE), no task is forced (Scenario D).
  - Long-running tasks do not block the loop beyond max_wait_per_cycle_seconds.
"""

from __future__ import annotations

import asyncio
import secrets
import time
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta
from typing import Any

from app.agents.registry import AgentRegistry
from app.cognition.brain import CognitionBrain
from app.cognition.gateway import LLMGateway, LLMGatewayInterface
from app.cognition.interfaces import (
    ActionPlan,
    AssessmentResult,
    BrainError,
    BrainInterface,
    CognitiveContext,
    GoalProposal,
    VerificationSpec,
)
from app.core.config import Settings
from app.core.enums import (
    NO_DRIVE,
    DriveType,
    GoalStatus,
    LifecycleState,
    MemoryType,
    PermissionLevel,
    QuotaStatus,
    TaskStatus,
)
from app.core.lifecycle import LifecycleManager
from app.drives.engine import DriveEngine
from app.drives.interfaces import DriveContext, DriveDecision, DriveState, KnowledgeGap
from app.governance.interfaces import GovernanceInterface
from app.memory.experience_manager import ExperienceManager
from app.memory.retrieval import build_contextual_query
from app.observability.logging import get_logger
from app.observability.metrics import CYCLE_DURATION, CYCLES_TOTAL
from app.orchestration.finalizer import TaskFinalizer
from app.orchestration.orchestrator import AgentOrchestrator, compute_params_digest, compute_plan_hash
from app.perception.interfaces import Observation, utcnow
from app.perception.manager import SensorManager
from app.storage.outbox import OutboxDispatcher
from app.storage.repository import StateRepository, day_start

logger = get_logger("aoc.loop")


class CycleInProgressError(RuntimeError):
    """Raised when a cycle trigger arrives while another cycle is actively executing."""


class AutonomousLoop:
    def __init__(
        self,
        organism_id: uuid.UUID,
        settings: Settings,
        repo: StateRepository,
        lifecycle: LifecycleManager,
        perception: SensorManager,
        drives: DriveEngine,
        brain: BrainInterface,
        governance: GovernanceInterface,
        registry: AgentRegistry,
        orchestrator: AgentOrchestrator,
        memory: ExperienceManager,
        llm_gateway: LLMGatewayInterface | None = None,
        finalizer: TaskFinalizer | None = None,
        outbox: OutboxDispatcher | None = None,
    ) -> None:
        self.organism_id = organism_id
        self.settings = settings
        self.repo = repo
        self.lifecycle = lifecycle
        self.perception = perception
        self.drives = drives
        self.brain = brain
        self.governance = governance
        self.registry = registry
        self.orchestrator = orchestrator
        self.memory = memory
        self.llm_gateway = llm_gateway
        self.outbox = outbox

        # In-memory recent learning signals hydrated from DB on startup
        self._last_learning_at: datetime | None = None
        self._last_info_gain: float | None = None
        self._last_info_gain_at: datetime | None = None
        # Re-entrancy guard preventing Scheduler and API /control/cycle from overlapping (P1-1a)
        self._cycle_lock = asyncio.Lock()

        self.finalizer = finalizer or TaskFinalizer(
            organism_id=organism_id,
            repo=repo,
            perception=perception,
            drives=drives,
            brain=brain,
            governance=governance,
            memory=memory,
            verifier=orchestrator.verifier,
            llm_gateway=llm_gateway,
            on_learning=self._record_learning_signal,
        )

    def _record_learning_signal(self, when: datetime, gain: float) -> None:
        self._last_learning_at = when
        self._last_info_gain = gain
        self._last_info_gain_at = when

    async def recover(self) -> dict[str, Any]:
        """Startup recovery (Scenario E): restore identity, memory, probes, and orphan tasks."""
        closed_cycles = await self.repo.close_unfinished_cycles(
            self.organism_id, note="Interrupted before completion (recovered on startup)"
        )
        orch_rec = await self.orchestrator.recover_on_startup()
        probes = await self.registry.probe_all()
        mem_info = await self.memory.initialize(
            self.organism_id,
            self.settings.organism.name,
            (
                f"I am {self.settings.organism.name} (AOC v{self.settings.organism.version}), "
                "a minimal autonomous digital organism governed by survival, reproduction (virtual capability building), "
                "and curiosity drives within L0-L1 safety boundaries."
            ),
        )
        await self.lifecycle.sync_from_db()
        if self.lifecycle.state == LifecycleState.STOPPED:
            await self.lifecycle.transition(LifecycleState.INITIALIZING, reason="restarting from stopped state")

        # Hydrate last learning timestamp & persisted information_gain from DB (removes hardcoded 0.5, V2.0 P1-4)
        latest_mem = await self.repo.get_latest_learning_memory()
        if latest_mem is not None:
            self._last_learning_at = latest_mem["created_at"]
            ig = latest_mem.get("information_gain")
            self._last_info_gain = float(ig) if ig is not None else 0.0
            self._last_info_gain_at = latest_mem["created_at"]
        else:
            sem = await self.repo.list_memory_index(memory_type=MemoryType.SEMANTIC.value, limit=1)
            if sem:
                self._last_learning_at = sem[0]["created_at"]
                ig = sem[0].get("information_gain")
                self._last_info_gain = float(ig) if ig is not None else 0.0
                self._last_info_gain_at = sem[0]["created_at"]

        summary = {
            "closed_interrupted_cycles": closed_cycles,
            "orchestrator": orch_rec,
            "agents_healthy": sum(1 for p in probes.values() if p.ok),
            "memory": mem_info,
        }
        await self.repo.log_event("runtime.recovered", message="Startup recovery completed", payload=summary)
        if not self.lifecycle.is_paused:
            await self.lifecycle.transition(LifecycleState.ACTIVE, reason="startup recovery complete")
        return summary

    async def run_cycle(self, fail_if_busy: bool = False) -> dict[str, Any]:
        """Execute ONE complete autonomous cycle in strict 16-step order (re-entrancy protected)."""
        if fail_if_busy and self._cycle_lock.locked():
            raise CycleInProgressError("An autonomous cycle is already in progress")
        async with self._cycle_lock:
            return await self._run_cycle_locked()

    async def _run_cycle_locked(self) -> dict[str, Any]:
        t0 = time.perf_counter()
        now = utcnow()

        # ── 1. WAKE UP ──────────────────────────────────────────────────────
        if not self.lifecycle.is_paused and self.lifecycle.state not in {
            LifecycleState.CRITICAL,
            LifecycleState.THROTTLED,
        }:
            await self.lifecycle.transition(LifecycleState.ACTIVE, reason="cycle start")
        cycle_id, cycle_num = await self.repo.start_cycle(self.organism_id, self.lifecycle.state.value)
        logger.info("cycle.start", cycle_id=str(cycle_id), cycle_number=cycle_num)

        outcome = "COMPLETED"
        err_text: str | None = None
        state_changes: dict[str, Any] = {}

        try:
            # ── 2. LOAD INTERNAL STATE ──────────────────────────────────────
            await self.repo.update_cycle(cycle_id, phase="LOAD_INTERNAL_STATE")
            prev_rows = await self.repo.load_drive_states(self.organism_id)
            prev_drives = {r["drive_type"]: DriveState(**r) for r in prev_rows}

            # Check if an in-flight async task from a prior cycle finished
            await self._poll_existing_inflight(cycle_id)

            # ── 3. PERCEPTION ───────────────────────────────────────────────
            await self.repo.update_cycle(cycle_id, phase="PERCEPTION")
            pre_obs = await self.perception.perceive(cycle_id=cycle_id, phase="pre", persist=True)
            if self.perception.last_observation_id is not None:
                await self.repo.update_cycle(cycle_id, observation_id=self.perception.last_observation_id)

            # P1-1: Pre-check Economic Quota BEFORE invoking LLM to prevent budget-exhausted spin loops!
            pre_budget = await self.governance.check_budget()
            if pre_budget.is_quota_exhausted:
                prelim_ctx = await self._build_drive_context(pre_obs, None, now)
                prelim_drives = self.drives.compute_states(prelim_ctx, prev_drives)
                prelim_decision = self.drives.arbitrate(prelim_drives, now=now)
                is_physical_emergency = bool(prelim_decision.constraints.get("emergency_mode"))

                if not is_physical_emergency:
                    # Pure Economic Quota exhaustion (NOT a survival crisis): throttle without calling LLM
                    outcome = "BUDGET_EXHAUSTED"
                    await self.repo.save_drive_states(
                        self.organism_id, prelim_drives.values(), cycle_id=cycle_id, phase="pre"
                    )
                    await self.repo.update_organism_state(
                        self.organism_id,
                        quota_reset_at=pre_budget.quota_reset_at,
                        throttled_reason=pre_budget.reason,
                    )
                    await self.lifecycle.transition(
                        LifecycleState.THROTTLED,
                        reason=pre_budget.reason,
                        next_wake_at=pre_budget.quota_reset_at,
                        cycle_id=cycle_id,
                    )
                    await self.repo.update_cycle(
                        cycle_id,
                        drives_pre={k: v.model_dump(mode="json") for k, v in prelim_drives.items()},
                        decision={
                            "selected_drive": NO_DRIVE,
                            "priority_score": 0.0,
                            "alternative_drives": list(prelim_drives.keys()),
                            "reason": f"Quota exhausted ({pre_budget.quota_status}): {pre_budget.reason}; throttled until reset.",
                            "constraints": {
                                "quota_status": pre_budget.quota_status,
                                "quota_reset_at": pre_budget.quota_reset_at.isoformat() if pre_budget.quota_reset_at else None,
                            },
                            "timestamp": now.isoformat(),
                        },
                    )
                    await self._finalize_cycle(
                        cycle_id=cycle_id,
                        outcome=outcome,
                        pre_drives=prelim_drives,
                        post_drives=prelim_drives,
                        state_changes={
                            "action": "throttled",
                            "quota_status": pre_budget.quota_status,
                            "quota_reset_at": pre_budget.quota_reset_at.isoformat() if pre_budget.quota_reset_at else None,
                            "budget": pre_budget.model_dump(mode="json"),
                        },
                        t0=t0,
                    )
                    return await self.repo.get_cycle(cycle_id) or {"cycle_id": str(cycle_id), "outcome": outcome}
            else:
                # Quota has recovered or is OK: clear any prior throttle markers
                if self.lifecycle.state == LifecycleState.THROTTLED:
                    await self.repo.update_organism_state(
                        self.organism_id, quota_reset_at=None, throttled_reason=None
                    )
                    await self.lifecycle.transition(
                        LifecycleState.ACTIVE, reason="quota refreshed", cycle_id=cycle_id
                    )

            # ── 4. SELF ASSESSMENT ──────────────────────────────────────────
            await self.repo.update_cycle(cycle_id, phase="SELF_ASSESSMENT")
            active_goals_for_recall = await self.repo.list_goals(statuses=[GoalStatus.ACTIVE.value], limit=3)
            recall_query = build_contextual_query(
                observation=pre_obs,
                drives=list(prev_drives.values()),
                active_goals=active_goals_for_recall,
            )
            recalled_memories = await self.memory.recall(query=recall_query, limit=8)
            mem_dicts = [m.model_dump(mode="json") for m in recalled_memories]

            assessment: AssessmentResult | None = None
            llm_available_for_brain = True
            llm_probe = (pre_obs.system_state or {}).get("llm")
            if isinstance(llm_probe, dict) and llm_probe.get("available") is False:
                llm_available_for_brain = False
            if pre_budget.quota_status == QuotaStatus.QUOTA_EXHAUSTED.value:
                # Hard cap reached: even emergency LLM calls are disabled; deterministic L0 fallback only
                llm_available_for_brain = False

            emergency_llm = pre_budget.quota_status == QuotaStatus.QUOTA_EMERGENCY_ONLY.value

            if llm_available_for_brain:
                try:
                    with self._gateway_scope(cycle_id=cycle_id, emergency=emergency_llm):
                        assessment = await self.brain.assess(pre_obs, mem_dicts, list(prev_drives.values()))
                    await self._record_llm_usage("assess", assessment.usage, cycle_id)
                except BrainError as exc:
                    logger.warning("cycle.assess_failed", cycle_id=str(cycle_id), error=str(exc))
                    assessment = AssessmentResult(summary=f"Brain assessment unavailable: {exc}", confidence=0.2)
            else:
                assessment = AssessmentResult(
                    summary="LLM endpoint reported unavailable or hard quota exhausted; skipping LLM assessment",
                    anomalies=["llm_unavailable"],
                    confidence=0.3,
                )

            # ── 5. DRIVE SATISFACTION CALCULATION ───────────────────────────
            await self.repo.update_cycle(cycle_id, phase="DRIVE_SATISFACTION_CALCULATION")
            drive_ctx = await self._build_drive_context(pre_obs, assessment, now)
            pre_drives = self.drives.compute_states(drive_ctx, prev_drives)
            await self.repo.save_drive_states(self.organism_id, pre_drives.values(), cycle_id=cycle_id, phase="pre")
            await self.repo.update_cycle(
                cycle_id,
                drives_pre={k: v.model_dump(mode="json") for k, v in pre_drives.items()},
                brain_output={"assessment": assessment.model_dump(mode="json")},
            )

            # ── 6. DRIVE ARBITRATION ────────────────────────────────────────
            await self.repo.update_cycle(cycle_id, phase="DRIVE_ARBITRATION")
            decision: DriveDecision = self.drives.arbitrate(pre_drives, now=now)
            await self.repo.log_decision(
                decision_type="drive_arbitration",
                selected=decision.selected_drive,
                alternatives=decision.alternative_drives,
                reason=decision.reason,
                payload=decision.model_dump(mode="json"),
                cycle_id=cycle_id,
            )
            await self.repo.update_cycle(cycle_id, decision=decision.model_dump(mode="json"))

            if decision.constraints.get("emergency_mode"):
                await self.lifecycle.transition(LifecycleState.CRITICAL, reason=decision.reason, cycle_id=cycle_id)

            # IDLE check (Scenario D: never force a task when all drives are satisfied)
            if decision.selected_drive == NO_DRIVE:
                outcome = "IDLE"
                await self.lifecycle.transition(LifecycleState.IDLE, reason=decision.reason, cycle_id=cycle_id)
                await self._finalize_cycle(
                    cycle_id=cycle_id,
                    outcome=outcome,
                    pre_drives=pre_drives,
                    post_drives=pre_drives,
                    state_changes={"action": "none", "reason": "all drives satisfied or below threshold"},
                    t0=t0,
                )
                return await self.repo.get_cycle(cycle_id) or {"cycle_id": str(cycle_id), "outcome": outcome}

            # If another task is still running in-flight (concurrency limit), do not double-dispatch
            budget = await self.governance.check_budget()
            if not budget.allowed:
                outcome = "BUDGET_DEFERRED"
                await self.repo.log_event(
                    "cycle.budget_deferred", message=budget.reason, payload=budget.model_dump(mode="json"), cycle_id=cycle_id
                )
                await self.lifecycle.transition(LifecycleState.IDLE, reason=budget.reason, cycle_id=cycle_id)
                await self._finalize_cycle(
                    cycle_id=cycle_id,
                    outcome=outcome,
                    pre_drives=pre_drives,
                    post_drives=pre_drives,
                    state_changes={"action": "deferred", "budget": budget.model_dump(mode="json")},
                    t0=t0,
                )
                return await self.repo.get_cycle(cycle_id) or {"cycle_id": str(cycle_id), "outcome": outcome}

            # ── 7. GOAL GENERATION ──────────────────────────────────────────
            await self.repo.update_cycle(cycle_id, phase="GOAL_GENERATION")
            cog_ctx = CognitiveContext(
                observation=pre_obs,
                drives=list(pre_drives.values()),
                decision=decision,
                assessment=assessment,
                memories=mem_dicts,
                available_agents=self.registry.summary_for_brain(),
                budget_remaining=budget.model_dump(mode="json"),
                active_goals=await self.repo.list_goals(statuses=[GoalStatus.ACTIVE.value], limit=5),
            )

            goal_proposal = await self._generate_goal_safe(cog_ctx, llm_available_for_brain, cycle_id)
            if not goal_proposal.should_act or not goal_proposal.title.strip():
                outcome = "IDLE"
                await self.lifecycle.transition(LifecycleState.IDLE, reason="Brain decided no action needed", cycle_id=cycle_id)
                await self._finalize_cycle(
                    cycle_id=cycle_id,
                    outcome=outcome,
                    pre_drives=pre_drives,
                    post_drives=pre_drives,
                    state_changes={"action": "none", "goal_rationale": goal_proposal.rationale},
                    t0=t0,
                )
                return await self.repo.get_cycle(cycle_id) or {"cycle_id": str(cycle_id), "outcome": outcome}

            goal_id = await self.repo.create_goal(
                self.organism_id,
                cycle_id,
                drive_type=decision.selected_drive,
                title=goal_proposal.title[:500],
                description=goal_proposal.description,
                success_criteria=goal_proposal.success_criteria,
                expected_outcome=goal_proposal.expected_outcome,
                related_key=goal_proposal.related_key,
                status=GoalStatus.ACTIVE.value,
            )

            # ── 8. COGNITIVE PLANNING ───────────────────────────────────────
            await self.repo.update_cycle(cycle_id, phase="COGNITIVE_PLANNING")
            plan = await self._plan_safe(
                cog_ctx.model_copy(update={"goal": goal_proposal}), llm_available_for_brain, cycle_id
            )
            await self.repo.update_cycle(
                cycle_id,
                brain_output={
                    "assessment": assessment.model_dump(mode="json"),
                    "goal": goal_proposal.model_dump(mode="json"),
                },
                plan=plan.model_dump(mode="json"),
            )

            if not plan.should_execute or not plan.objective.strip():
                await self.repo.transition_goal(goal_id, GoalStatus.ABANDONED.value, note="planner chose not to execute")
                outcome = "IDLE"
                await self.lifecycle.transition(LifecycleState.IDLE, reason="Planner produced no executable action", cycle_id=cycle_id)
                await self._finalize_cycle(cycle_id, outcome, pre_drives, pre_drives, {"action": "abandoned"}, t0)
                return await self.repo.get_cycle(cycle_id) or {"cycle_id": str(cycle_id), "outcome": outcome}

            # ── 9. POLICY VALIDATION ────────────────────────────────────────
            await self.repo.update_cycle(cycle_id, phase="POLICY_VALIDATION")
            verdict = await self.governance.validate_plan(plan, decision.selected_drive)
            plan_status = "APPROVED" if verdict.allowed else ("REQUIRES_APPROVAL" if verdict.requires_approval else "DENIED")
            plan_hash = compute_plan_hash(plan)
            plan_id = await self.repo.create_plan(
                goal_id=goal_id,
                cycle_id=cycle_id,
                plan=plan,
                permission_level=verdict.effective_level,
                verdict=verdict,
                status=plan_status,
                plan_hash=plan_hash,
            )
            await self.repo.record_policy_evaluation(
                subject_type="plan",
                subject_id=plan_id,
                cycle_id=cycle_id,
                requested_level=verdict.requested_level,
                deterministic_level=verdict.effective_level,
                semantic_level=None,
                effective_level=verdict.effective_level,
                verdict=plan_status.lower(),
                violations=verdict.violations,
                reason=verdict.reason,
            )
            await self.repo.log_decision(
                decision_type="policy_validation",
                selected=plan_status,
                alternatives=[],
                reason=verdict.reason,
                payload=verdict.model_dump(mode="json"),
                cycle_id=cycle_id,
            )

            if not verdict.allowed:
                if verdict.requires_approval:
                    ws = self.governance.prepare_workspace(f"pending-{str(plan_id)[:8]}")
                    pending_task_id = await self.repo.create_task(
                        plan_id=plan_id,
                        goal_id=goal_id,
                        cycle_id=cycle_id,
                        objective=plan.objective,
                        description=plan.description,
                        assigned_agent=plan.assigned_agent,
                        expected_output=plan.expected_output,
                        workspace=ws,
                        timeout_seconds=verdict.effective_timeout_seconds,
                        resource_budget={"sandbox_mode": verdict.sandbox_mode},
                        permission_level=verdict.effective_level,
                        success_criteria=plan.success_criteria,
                        verification=[v.model_dump(mode="json") for v in plan.verification],
                        required_capability=plan.required_capability,
                        status=TaskStatus.REQUIRES_APPROVAL.value,
                    )
                    params_digest = compute_params_digest(
                        {
                            "objective": plan.objective,
                            "description": plan.description,
                            "assigned_agent": plan.assigned_agent,
                            "permission_level": verdict.effective_level,
                            "timeout_seconds": verdict.effective_timeout_seconds,
                            "workspace": ws,
                        }
                    )
                    await self.repo.create_approval_record(
                        task_id=pending_task_id,
                        plan_id=plan_id,
                        plan_hash=plan_hash,
                        params_digest=params_digest,
                        permission_level=verdict.effective_level,
                        nonce=secrets.token_hex(16),
                        expires_at=utcnow() + timedelta(hours=24),
                    )
                    outcome = "REQUIRES_APPROVAL"
                else:
                    await self.repo.transition_goal(goal_id, GoalStatus.REJECTED.value, note=verdict.reason)
                    outcome = "POLICY_DENIED"
                await self._finalize_cycle(
                    cycle_id,
                    outcome,
                    pre_drives,
                    pre_drives,
                    {"policy_verdict": verdict.model_dump(mode="json")},
                    t0,
                )
                return await self.repo.get_cycle(cycle_id) or {"cycle_id": str(cycle_id), "outcome": outcome}

            # ── 10. AGENT DISPATCH ──────────────────────────────────────────
            await self.repo.update_cycle(cycle_id, phase="AGENT_DISPATCH")
            dispatch_info = await self.orchestrator.submit_plan(
                plan=plan, verdict=verdict, goal_id=goal_id, plan_id=plan_id, cycle_id=cycle_id
            )
            task_id = dispatch_info["task_id"]

            # ── 11. WAIT / MONITOR ──────────────────────────────────────────
            await self.repo.update_cycle(cycle_id, phase="WAIT_MONITOR")
            task_state = await self.orchestrator.wait_for_task(
                task_id=task_id,
                max_wait_seconds=self.settings.runtime.max_wait_per_cycle_seconds,
                poll_interval=self.settings.runtime.monitor_poll_seconds,
            )
            await self.repo.update_cycle(cycle_id, execution_summary=task_state)

            if not TaskStatus(task_state["status"]).is_terminal:
                outcome = "IN_FLIGHT"
                await self._finalize_cycle(
                    cycle_id,
                    outcome,
                    pre_drives,
                    pre_drives,
                    {"task_id": task_id, "status": task_state["status"]},
                    t0,
                )
                return await self.repo.get_cycle(cycle_id) or {"cycle_id": str(cycle_id), "outcome": outcome}

            # ── 12. OUTCOME EVALUATION + POST-PERCEPTION RE-EVALUATION ──────
            # Unified TaskFinalizer.finalize_execution() (V2.0 P1-2: single completion path)
            await self.repo.update_cycle(cycle_id, phase="OUTCOME_EVALUATION")
            exec_id = (task_state.get("outcome") or {}).get("execution_id")
            if exec_id:
                fin_res = await self.finalizer.finalize_execution(
                    exec_id,
                    pre_obs_override=pre_obs,
                    pre_drives_override=pre_drives,
                    llm_available_override=llm_available_for_brain,
                )
                post_drives = (
                    {k: DriveState(**v) for k, v in fin_res.post_drives.items()}
                    if fin_res.post_drives
                    else pre_drives
                )
                state_changes = fin_res.evaluation.get("measured_satisfaction_change") and {
                    "expected_outcome": fin_res.evaluation.get("expected_outcome", {}),
                    "actual_outcome": fin_res.evaluation.get("actual_outcome", {}),
                    "measured_satisfaction_change": fin_res.measured_satisfaction_change,
                } or {}
                outcome = "SUCCEEDED" if fin_res.goal_achieved else "ACTION_FAILED"
            else:
                # Edge case: task failed before any execution was created (e.g. no healthy agent)
                await self.repo.transition_goal(
                    goal_id,
                    GoalStatus.FAILED.value,
                    note=(task_state.get("outcome") or {}).get("error", "task failed before dispatch"),
                )
                post_drives = pre_drives
                state_changes = {"task_status": task_state["status"], "error": (task_state.get("outcome") or {}).get("error")}
                outcome = "ACTION_FAILED"

            # ── 14. STATE UPDATE & 15. SLEEP / NEXT CYCLE ───────────────────
            await self._finalize_cycle(cycle_id, outcome, pre_drives, post_drives, state_changes, t0)
            return await self.repo.get_cycle(cycle_id) or {"cycle_id": str(cycle_id), "outcome": outcome}

        except Exception as exc:
            err_text = str(exc)
            outcome = "ERROR"
            logger.exception("cycle.error", cycle_id=str(cycle_id), error=err_text)
            await self.repo.update_cycle(cycle_id, ended_at=utcnow(), outcome="ERROR", error=err_text)
            await self.lifecycle.transition(LifecycleState.ERROR, reason=err_text, cycle_id=cycle_id)
            CYCLES_TOTAL.labels(outcome="ERROR").inc()
            CYCLE_DURATION.observe(time.perf_counter() - t0)
            return {"cycle_id": str(cycle_id), "outcome": "ERROR", "error": err_text}

    async def _poll_existing_inflight(self, cycle_id: uuid.UUID) -> list[dict[str, Any]]:
        """Poll in-flight cross-cycle tasks and finalize ALL unfinalized terminal executions (P1-2 / T2)."""
        finalized: list[dict[str, Any]] = []

        # 1. Poll active background executions tracked by the backend
        for tid in list(self.orchestrator.list_inflight_task_ids()):
            st = await self.orchestrator.poll_task(tid)
            if TaskStatus(st["status"]).is_terminal:
                exec_id = (st.get("outcome") or {}).get("execution_id")
                await self.repo.log_event(
                    "orchestrator.inflight_completed",
                    message=f"Prior in-flight task {tid} finished with {st['status']}; invoking TaskFinalizer",
                    payload={"task_id": tid, "execution_id": exec_id, "status": st["status"]},
                    cycle_id=cycle_id,
                )
                if exec_id:
                    fin = await self.finalizer.finalize_execution(exec_id)
                    finalized.append(fin.model_dump(mode="json"))

        # 2. Catch any terminal executions persisted in DB that have not yet been finalized
        #    (e.g. post-approval tasks, restart recovery, or prior cycle crashes)
        unfinalized = await self.repo.list_unfinalized_terminal_executions(limit=20)
        for ex in unfinalized:
            fin = await self.finalizer.finalize_execution(ex["execution_id"])
            if not fin.already_finalized:
                finalized.append(fin.model_dump(mode="json"))

        # 3. Drain due outbox events (e.g. execution.finalize / memory.sync_letta)
        if self.outbox is not None:
            try:
                await self.outbox.dispatch_once()
            except Exception as exc:
                logger.warning("cycle.outbox_dispatch_error", cycle_id=str(cycle_id), error=str(exc))

        return finalized

    async def _build_drive_context(
        self,
        obs: Observation,
        assessment: AssessmentResult | None,
        now: datetime,
    ) -> DriveContext:
        recent_execs = await self.repo.list_executions(limit=15)
        recent_statuses = [e["status"] for e in recent_execs]
        budget = await self.governance.check_budget()

        env = obs.environment_state or {}
        env_topics = [
            KnowledgeGap(
                topic=str(t.get("topic") or t),
                description=str(t.get("description", "")) if isinstance(t, dict) else "",
                importance=float(t.get("importance", 0.7)) if isinstance(t, dict) else 0.7,
                confidence=1.0,
                source="environment",
            )
            for t in (env.get("unknown_topics") or [])
        ]

        # Merge environment topics + LLM-identified knowledge gaps (dedup by topic)
        gaps_by_topic: dict[str, KnowledgeGap] = {g.topic.lower(): g for g in env_topics}
        if assessment is not None:
            for g in assessment.knowledge_gaps:
                key = g.topic.lower().strip()
                if key and key not in gaps_by_topic:
                    gaps_by_topic[key] = g

        since_24h = day_start(now)
        since_7d = now - timedelta(days=7)
        proc_count = await self.repo.count_memories(MemoryType.PROCEDURAL.value)
        sem_recent = await self.repo.count_memories(MemoryType.SEMANTIC.value, since=since_7d)
        cap_goals_done = await self.repo.count_goals_since(
            DriveType.REPRODUCTION.value, GoalStatus.ACHIEVED.value, since_7d
        )
        cur_done = await self.repo.count_goals_since(DriveType.CURIOSITY.value, GoalStatus.ACHIEVED.value, since_7d)
        cur_failed = await self.repo.count_goals_since(DriveType.CURIOSITY.value, GoalStatus.FAILED.value, since_7d)

        return DriveContext(
            now=now,
            observation=obs,
            recent_execution_statuses=recent_statuses,
            budget={
                "tasks_used": budget.tasks_used_today,
                "tasks_limit": budget.tasks_limit_per_day,
                "tokens_used": budget.tokens_used_today,
                "tokens_limit": budget.tokens_limit_per_day,
            },
            capabilities_acquired=list(env.get("capabilities_acquired") or []),
            procedural_skill_count=proc_count,
            capability_goals_completed_recent=cap_goals_done,
            memory_available=self.memory.backend_available,
            identity_persisted=True,
            open_knowledge_gaps=list(gaps_by_topic.values()),
            unknown_topics=env_topics,
            exploration_attempted_recent=cur_done + cur_failed,
            exploration_succeeded_recent=cur_done,
            new_knowledge_recent=sem_recent,
            last_learning_at=self._last_learning_at,
            last_information_gain=self._last_info_gain,
            last_information_gain_at=self._last_info_gain_at,
            gap_assessment_confidence=assessment.confidence if assessment else None,
        )

    async def _generate_goal_safe(
        self, cog_ctx: CognitiveContext, llm_available: bool, cycle_id: uuid.UUID
    ) -> GoalProposal:
        is_em = bool(cog_ctx.decision.constraints.get("emergency_mode"))
        if llm_available:
            try:
                with self._gateway_scope(cycle_id=cycle_id, emergency=is_em):
                    gp = await self.brain.generate_goal(cog_ctx)
                await self._record_llm_usage("generate_goal", gp.usage, cycle_id)
                return gp
            except BrainError as exc:
                logger.warning("cycle.goal_gen_failed", error=str(exc))

        # Deterministic survival fallback when LLM is down in emergency mode
        if cog_ctx.decision.selected_drive == DriveType.SURVIVAL.value:
            return GoalProposal(
                should_act=True,
                title="Diagnose survival health issue",
                description="Run a read-only diagnostic of system resources and LLM connectivity.",
                drive_type=DriveType.SURVIVAL.value,
                success_criteria=["Produce concise diagnostic report"],
                expected_outcome={"survival_diagnosis": True},
                rationale="Deterministic emergency fallback while LLM unavailable",
            )
        return GoalProposal(should_act=False, rationale="LLM unavailable for non-emergency goal generation")

    async def _plan_safe(
        self, cog_ctx: CognitiveContext, llm_available: bool, cycle_id: uuid.UUID
    ) -> ActionPlan:
        is_em = bool(cog_ctx.decision.constraints.get("emergency_mode"))
        if llm_available:
            try:
                with self._gateway_scope(cycle_id=cycle_id, emergency=is_em):
                    ap = await self.brain.plan(cog_ctx)
                await self._record_llm_usage("plan", ap.usage, cycle_id)
                return ap
            except BrainError as exc:
                logger.warning("cycle.plan_failed", error=str(exc))

        if cog_ctx.decision.selected_drive == DriveType.SURVIVAL.value and cog_ctx.goal:
            return ActionPlan(
                should_execute=True,
                goal_title=cog_ctx.goal.title,
                objective="Inspect system disk, memory, and service health (read-only) and summarize findings.",
                description="Read-only survival diagnostic.",
                assigned_agent="codex",
                required_capability="system_diagnosis",
                expected_output="Diagnostic summary",
                timeout_seconds=120,
                permission_level=PermissionLevel.L0.value,
                success_criteria=["Non-empty diagnostic summary"],
                verification=[VerificationSpec(kind="non_empty_output")],
                rationale="Deterministic emergency fallback plan",
            )
        return ActionPlan(should_execute=False, rationale="LLM unavailable for planning")

    async def _finalize_cycle(
        self,
        cycle_id: uuid.UUID,
        outcome: str,
        pre_drives: dict[str, DriveState],
        post_drives: dict[str, DriveState],
        state_changes: dict[str, Any],
        t0: float,
    ) -> None:
        # ── 14. STATE UPDATE ────────────────────────────────────────────────
        await self.repo.update_cycle(cycle_id, phase="STATE_UPDATE")
        await self.repo.save_drive_states(self.organism_id, post_drives.values(), cycle_id=cycle_id, phase="post")
        await self.repo.update_cycle(
            cycle_id,
            ended_at=utcnow(),
            phase="SLEEP_NEXT_CYCLE",
            outcome=outcome,
            lifecycle_state=self.lifecycle.state.value,
            drives_post={k: v.model_dump(mode="json") for k, v in post_drives.items()},
            state_changes=state_changes,
        )
        CYCLES_TOTAL.labels(outcome=outcome).inc()
        CYCLE_DURATION.observe(time.perf_counter() - t0)
        logger.info("cycle.finished", cycle_id=str(cycle_id), outcome=outcome, seconds=round(time.perf_counter() - t0, 2))

    def _gateway_scope(self, *, cycle_id: uuid.UUID, emergency: bool = False):
        if isinstance(self.llm_gateway, LLMGateway):
            return self.llm_gateway.scope(cycle_id=cycle_id, emergency=emergency)
        return nullcontext()

    async def _record_llm_usage(self, operation: str, usage: Any, cycle_id: uuid.UUID) -> None:
        # When CognitionBrain runs through LLMGateway, every HTTP attempt (success/retry/failure)
        # is already persisted atomically by the gateway — avoid double-counting.
        if isinstance(self.brain, CognitionBrain) and self.llm_gateway is not None:
            return
        if usage and getattr(usage, "total_tokens", 0) > 0:
            await self.repo.record_usage(
                category="llm",
                amount=float(usage.total_tokens),
                seconds=float(usage.latency_ms) / 1000.0,
                details={"operation": operation, "model": usage.model},
                cycle_id=cycle_id,
            )
