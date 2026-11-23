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
from app.cognition.goal_dedup import (
    GoalDedupEngine,
    compute_cooldown_until,
    compute_goal_fingerprint,
    select_best_goal_candidate,
)
from app.cognition.interfaces import (
    ActionPlan,
    AssessmentResult,
    BrainError,
    BrainInterface,
    CognitiveContext,
    GoalProposal,
    VerificationSpec,
)
from app.cognition.preflight import ActionPreflight
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
from app.perception.world.security import detect_prompt_injection, sanitize_external_text, wrap_untrusted_world_event
from app.storage.outbox import OutboxDispatcher, OutboxEventType
from app.storage.repository import StateRepository, day_start
from app.tools.interfaces import ToolCallRequest

logger = get_logger("aoc.loop")


class CycleInProgressError(RuntimeError):
    """Raised when a cycle trigger arrives while another cycle is actively executing."""


def _format_mcp_output_for_prefetch(tool_name: str, output: Any) -> str:
    """Format structured MCP outputs cleanly while preserving outbound_links & discovered_feeds."""
    if isinstance(output, str):
        return output
    if not isinstance(output, dict):
        return str(output)
    if tool_name == "web_search":
        lines = [f"Search query: {output.get('query', '')} (results={output.get('count', 0)})"]
        for i, item in enumerate(list(output.get("results") or [])[:6], start=1):
            if not isinstance(item, dict):
                continue
            lines.append(
                f"{i}. [{item.get('domain', '')}] {item.get('title', '')} | URL: {item.get('url', '')} | {item.get('snippet', '')}"
            )
        return "\n".join(lines)
    if tool_name == "web_fetch_readonly":
        lines = [
            f"Fetched URL: {output.get('url', '')} (status={output.get('status_code', '')})",
            f"Excerpt: {str(output.get('markdown_excerpt') or '')[:2200]}",
        ]
        feeds = list(output.get("discovered_feeds") or [])
        if feeds:
            lines.append("Discovered Feeds: " + "; ".join(f"{f.get('title', '')} -> {f.get('url', '')}" for f in feeds[:3]))
        links = list(output.get("outbound_links") or [])
        if links:
            lines.append("Outbound Links: " + "; ".join(f"{l.get('text', '')} ({l.get('url', '')})" for l in links[:8]))
        return "\n".join(lines)
    return str(output)


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
        approval_service: Any = None,
        self_model: Any = None,
        skill_registry: Any = None,
        skill_runtime: Any = None,
        affect: Any = None,
        mcp_manager: Any = None,
        world_pipeline: Any = None,
        playbook_manager: Any = None,
        exploration_manager: Any = None,
        human_bridge: Any = None,
        investigation_manager: Any = None,
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
        self.approval_service = approval_service
        self.self_model = self_model
        self.skill_registry = skill_registry
        self.skill_runtime = skill_runtime
        self.affect = affect
        self.mcp_manager = mcp_manager
        self.world_pipeline = world_pipeline
        self.playbook_manager = playbook_manager
        self.exploration_manager = exploration_manager
        self.human_bridge = human_bridge
        self.investigation_manager = investigation_manager
        self._consecutive_idle_cycles: int = 0
        self._low_ig_streak: int = 0
        self._last_prediction_error: float = 0.0
        self._consecutive_dedup_skips: dict[str, int] = {}

        # In-memory recent learning signals hydrated from DB on startup
        self._last_learning_at: datetime | None = None
        self._last_info_gain: float | None = None
        self._last_info_gain_at: datetime | None = None
        # Re-entrancy guard preventing Scheduler and API /control/cycle from overlapping (P1-1a)
        self._cycle_lock = asyncio.Lock()

        # V3.0 M1 thin-kernel deterministic guards
        kv3 = settings.kernel_v3
        self.preflight = ActionPreflight(
            max_task_timeout_seconds=settings.governance.budget.max_task_timeout_seconds,
        )
        self.goal_dedup = GoalDedupEngine(
            dedup_window_hours=kv3.goal_dedup_window_hours,
            lexical_threshold=kv3.goal_dedup_lexical_threshold,
            semantic_threshold=kv3.goal_dedup_semantic_threshold,
            failed_cooldown_minutes=kv3.goal_failed_cooldown_minutes,
            low_ig_cooldown_minutes=kv3.goal_low_ig_cooldown_minutes,
            low_ig_threshold=kv3.goal_low_ig_threshold,
        )

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
            on_prediction_error=self._record_prediction_error,
            self_model=self_model,
            skill_registry=skill_registry,
            skill_auto_crystallize=kv3.skill_auto_crystallize_enabled,
            playbook_manager=playbook_manager,
            exploration_manager=exploration_manager,
            human_bridge=human_bridge,
            investigation_manager=investigation_manager,
            world_pipeline=world_pipeline,
        )

    def _record_prediction_error(self, error: float) -> None:
        self._last_prediction_error = max(-1.0, min(1.0, float(error)))

    def _record_learning_signal(self, when: datetime, gain: float) -> None:
        self._last_learning_at = when
        self._last_info_gain = gain
        self._last_info_gain_at = when
        low_thresh = float(self.settings.kernel_v3.goal_low_ig_threshold)
        if gain >= low_thresh:
            self._low_ig_streak = 0
        else:
            self._low_ig_streak += 1

    async def recover(self) -> dict[str, Any]:
        """Startup recovery (Scenario E): restore identity, memory, probes, and orphan tasks."""
        closed_cycles = await self.repo.close_unfinished_cycles(
            self.organism_id, note="Interrupted before completion (recovered on startup)"
        )
        orch_rec = await self.orchestrator.recover_on_startup()
        probes = await self.registry.probe_all()
        # V2.0 P1-7: Register external Agent static capabilities ONLY as DECLARED/AVAILABLE (never ACQUIRED)
        if self.self_model is not None:
            agent_caps = [
                (
                    cap.agent_id,
                    list(cap.capabilities),
                    bool(probes.get(cap.agent_id).ok) if probes.get(cap.agent_id) is not None else False,
                )
                for cap in self.registry.list_capabilities()
            ]
            await self.self_model.sync_declared_capabilities(agent_caps)
            await self.self_model.get_latest_snapshot()
        if self.affect is not None:
            try:
                await self.affect.load_from_db()
            except Exception as exc:
                logger.warning("loop.affect_load_degraded", error=str(exc))
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
                    await self.repo.enqueue_outbox_event(
                        event_type=OutboxEventType.NOTIFICATION_DISPATCH,
                        idempotency_key=f"notify:quota_recovered:{cycle_id}",
                        payload={"kind": "quota_recovered", "cycle_id": str(cycle_id), "message": "Daily quota refreshed; organism resumed ACTIVE."},
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
            affect_mod_payload: dict[str, Any] | None = None
            affect_state_snapshot: dict[str, Any] | None = None
            if self.affect is not None and getattr(self.affect, "enabled", False):
                try:
                    ext_events = pre_obs.external_events or []
                    world_novelty = min(1.0, len(ext_events) * 0.25) if ext_events else 0.0
                    sm_conf: float | None = None
                    if self.self_model is not None:
                        try:
                            caps = await self.self_model.list_capabilities()
                            sampled = [
                                float(c.get("estimated_success_rate", 0.5))
                                for c in caps
                                if int(c.get("sample_count", 0) or 0) > 0
                            ]
                            if sampled:
                                sm_conf = round(sum(sampled) / len(sampled), 4)
                        except Exception:
                            sm_conf = None
                    pred_surprise = self._last_prediction_error
                    self._last_prediction_error = 0.0
                    mod = await self.affect.evaluate_safe(
                        recent_execution_statuses=drive_ctx.recent_execution_statuses,
                        last_information_gain=self._last_info_gain,
                        idle_cycles=self._consecutive_idle_cycles,
                        cycle_id=cycle_id,
                        trigger_reason="drive_arbitration",
                        trigger_event_id=f"cycle:{cycle_id}:drive_arbitration",
                        low_ig_streak=self._low_ig_streak,
                        world_event_novelty=world_novelty,
                        self_model_confidence=sm_conf,
                        prediction_surprise=pred_surprise,
                    )
                    if mod.enabled:
                        affect_mod_payload = mod.model_dump(mode="json")
                        affect_state_snapshot = self.affect.state.model_dump(mode="json")
                except Exception as exc:
                    logger.warning("cycle.affect_fault_degraded", cycle_id=str(cycle_id), error=str(exc))
                    affect_mod_payload = None
                    affect_state_snapshot = None

            decision: DriveDecision = self.drives.arbitrate(
                pre_drives, now=now, affect_modulation=affect_mod_payload
            )
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
                await self.repo.enqueue_outbox_event(
                    event_type=OutboxEventType.NOTIFICATION_DISPATCH,
                    idempotency_key=f"notify:critical:{cycle_id}",
                    payload={"kind": "critical_alert", "cycle_id": str(cycle_id), "reason": decision.reason, "message": "Organism entered CRITICAL emergency mode"},
                )

            # IDLE check (Scenario D: never force a task when all drives are satisfied)
            if decision.selected_drive == NO_DRIVE:
                self._consecutive_idle_cycles += 1
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

            self._consecutive_idle_cycles = 0
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
            reusable_skills: list[dict[str, Any]] = []
            if self.skill_registry is not None:
                try:
                    reusable_skills = await self.skill_registry.list_reusable_skills_for_context(limit=8)
                except Exception as exc:
                    logger.warning("cycle.skill_context_degraded", cycle_id=str(cycle_id), error=str(exc))

            active_hypotheses: list[dict[str, Any]] = []
            try:
                hyp_rows = await self.repo.list_world_items(kind="hypothesis")
                for hr in hyp_rows[:8]:
                    val = hr.get("value") if isinstance(hr.get("value"), dict) else {}
                    active_hypotheses.append(
                        {
                            "key": hr.get("key"),
                            "status": hr.get("status", "unverified"),
                            "claim": val.get("claim", ""),
                            "verification_hint": val.get("verification_hint", ""),
                            "confidence": val.get("confidence", 0.5),
                            "evidence_count": len(val.get("evidence") or []),
                        }
                    )
            except Exception as exc:
                logger.warning("cycle.hypotheses_load_degraded", cycle_id=str(cycle_id), error=str(exc))

            kv3 = self.settings.kernel_v3
            lookback_h = max(24.0, float(kv3.goal_dedup_window_hours))
            recent_goals = await self.repo.list_recent_goals(
                since=now - timedelta(hours=lookback_h), limit=80
            )
            exploration_guidance: dict[str, Any] | None = None
            if self.exploration_manager is not None:
                try:
                    exploration_guidance = await self.exploration_manager.build_guidance(recent_goals)
                except Exception as exc:
                    logger.warning("cycle.exploration_guidance_degraded", cycle_id=str(cycle_id), error=str(exc))

            external_playbooks: list[dict[str, Any]] = []
            if self.playbook_manager is not None:
                try:
                    external_playbooks = await self.playbook_manager.list_playbooks_for_context(
                        domains=[decision.selected_drive, "coding", "general"],
                        limit=3,
                    )
                except Exception as exc:
                    logger.warning("cycle.playbooks_load_degraded", cycle_id=str(cycle_id), error=str(exc))

            pending_human_guidance: list[dict[str, Any]] = []
            if self.human_bridge is not None:
                try:
                    pending_human_guidance = await self.human_bridge.list_pending_guidance(limit=5)
                except Exception as exc:
                    logger.warning("cycle.human_guidance_load_degraded", cycle_id=str(cycle_id), error=str(exc))
            outreach_avail = bool(self.human_bridge is not None and getattr(self.human_bridge, "enabled", False))

            active_investigations: list[dict[str, Any]] = []
            discovered_channels: list[dict[str, Any]] = []
            if self.investigation_manager is not None:
                try:
                    active_investigations, discovered_channels = await self.investigation_manager.build_context_snapshot()
                except Exception as exc:
                    logger.warning("cycle.investigations_load_degraded", cycle_id=str(cycle_id), error=str(exc))

            cog_ctx = CognitiveContext(
                observation=pre_obs,
                drives=list(pre_drives.values()),
                decision=decision,
                assessment=assessment,
                memories=mem_dicts,
                available_agents=self.registry.summary_for_brain(),
                available_tools=(
                    self.mcp_manager.summary_for_brain()
                    if (self.mcp_manager is not None and self.settings.kernel_v3.mcp_in_loop_enabled)
                    else []
                ),
                validated_skills=reusable_skills,
                affect_state=affect_state_snapshot,
                active_hypotheses=active_hypotheses,
                external_playbooks=external_playbooks,
                exploration_guidance=exploration_guidance,
                human_guidance=pending_human_guidance,
                human_outreach_available=outreach_avail,
                active_investigations=active_investigations,
                discovered_channels=discovered_channels,
                budget_remaining=budget.model_dump(mode="json"),
                active_goals=await self.repo.list_goals(statuses=[GoalStatus.ACTIVE.value], limit=5),
            )

            goal_proposal = await self._generate_goal_safe(cog_ctx, llm_available_for_brain, cycle_id)
            if self.human_bridge is not None and isinstance(goal_proposal.message_to_human, dict):
                try:
                    m = goal_proposal.message_to_human
                    if str(m.get("message") or "").strip():
                        outreach_res = await self.human_bridge.maybe_send_outreach(
                            kind=str(m.get("kind") or "ask_human"),
                            topic_key=str(m.get("topic_key") or m.get("title") or goal_proposal.title[:40]),
                            title=str(m.get("title") or goal_proposal.title[:80]),
                            message=str(m.get("message") or ""),
                            reason=str(m.get("reason") or ""),
                            expected_benefit=str(m.get("expected_benefit") or ""),
                            cycle_id=cycle_id,
                        )
                        state_changes["human_outreach"] = outreach_res
                except Exception as exc:
                    logger.warning("cycle.human_outreach_degraded", cycle_id=str(cycle_id), error=str(exc))
            if not goal_proposal.should_act or not goal_proposal.title.strip():
                if self.human_bridge is not None and pending_human_guidance:
                    try:
                        await self.human_bridge.acknowledge_guidance(
                            [str(g["key"]) for g in pending_human_guidance if g.get("key")],
                            cycle_id=cycle_id,
                        )
                    except Exception as exc:
                        logger.warning("cycle.human_guidance_ack_degraded", cycle_id=str(cycle_id), error=str(exc))
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

            # V3.0 M1 + V3.1 + V4.0 M3: Multi-Candidate Deterministic Goal Scoring, Dedup & External Exploration Bias
            goal_fp = compute_goal_fingerprint(
                drive_type=decision.selected_drive,
                title=goal_proposal.title,
                related_key=goal_proposal.related_key,
                description=goal_proposal.description,
            )
            goal_candidates_audit: list[dict[str, Any]] = []
            if kv3.goal_dedup_enabled and not bool(decision.constraints.get("emergency_mode")):
                prefer_external = bool(
                    decision.selected_drive == DriveType.CURIOSITY.value
                    and (exploration_guidance or {}).get("must_explore_external")
                )
                sel_res = select_best_goal_candidate(
                    primary=goal_proposal,
                    drive_type=decision.selected_drive,
                    recent_goals=recent_goals,
                    dedup_engine=self.goal_dedup,
                    now=now,
                    affect_state=affect_state_snapshot,
                    prefer_external=prefer_external,
                    has_human_guidance=bool(pending_human_guidance),
                )
                goal_candidates_audit = [c.model_dump(mode="json") for c in sel_res.candidates]
                dedup_res = sel_res.selected_dedup
                goal_fp = dedup_res.fingerprint
                if len(sel_res.candidates) > 1:
                    await self.repo.log_decision(
                        decision_type="goal_ranking",
                        selected=(sel_res.selected_proposal.title[:200] if sel_res.selected_proposal else "ALL_SKIPPED"),
                        alternatives=[c.title[:120] for c in sel_res.candidates if not c.selected],
                        reason=(
                            "Fallback to novel alternative candidate after primary hit dedup/cooldown"
                            if sel_res.used_alternative
                            else "Multi-candidate deterministic ranking completed"
                        ),
                        payload={
                            "used_alternative": sel_res.used_alternative,
                            "candidates": goal_candidates_audit,
                        },
                        cycle_id=cycle_id,
                    )

                if sel_res.selected_proposal is None:
                    dup_uuid = uuid.UUID(dedup_res.duplicate_of) if dedup_res.duplicate_of else None
                    await self.repo.create_goal(
                        self.organism_id,
                        cycle_id,
                        drive_type=decision.selected_drive,
                        title=goal_proposal.title[:500],
                        description=goal_proposal.description,
                        success_criteria=goal_proposal.success_criteria,
                        expected_outcome=goal_proposal.expected_outcome,
                        related_key=goal_proposal.related_key,
                        status=GoalStatus.REJECTED.value if dedup_res.decision == "REJECT" else GoalStatus.ABANDONED.value,
                        goal_fingerprint=goal_fp,
                        duplicate_of=dup_uuid,
                        cooldown_until=None if dedup_res.decision == "DEFER" else dedup_res.cooldown_until,
                        predicted_ig=round(float(goal_proposal.predicted_ig or 0.0), 4),
                    )
                    await self.repo.log_decision(
                        decision_type="goal_dedup",
                        selected=dedup_res.decision,
                        alternatives=[],
                        reason="; ".join(dedup_res.notes) or str(dedup_res.reason_code or "duplicate_goal"),
                        payload={**dedup_res.model_dump(mode="json"), "candidates": goal_candidates_audit},
                        cycle_id=cycle_id,
                    )
                    await self.repo.update_cycle(
                        cycle_id,
                        brain_output={
                            "assessment": assessment.model_dump(mode="json"),
                            "goal": goal_proposal.model_dump(mode="json"),
                            "goal_dedup": dedup_res.model_dump(mode="json"),
                            "goal_candidates": goal_candidates_audit,
                            "affect_snapshot": affect_state_snapshot,
                        },
                    )
                    outcome = "GOAL_DEDUP_SKIPPED"
                    self._low_ig_streak += 1
                    cur_drive = decision.selected_drive
                    self._consecutive_dedup_skips[cur_drive] = self._consecutive_dedup_skips.get(cur_drive, 0) + 1
                    post_drives_dedup = dict(pre_drives)
                    arb_cfg = (getattr(self.drives, "config", None) or {}).get("arbitration") or {}
                    if (
                        cur_drive != DriveType.SURVIVAL.value
                        and bool(arb_cfg.get("post_goal_satiation_enabled", False))
                        and self._consecutive_dedup_skips[cur_drive] >= 2
                        and cur_drive in post_drives_dedup
                    ):
                        st = post_drives_dedup[cur_drive]
                        post_drives_dedup[cur_drive] = st.model_copy(
                            update={"last_satisfied_at": now, "persistence": round(st.persistence * 0.5, 6)}
                        )
                        self._consecutive_dedup_skips[cur_drive] = 0
                    await self.lifecycle.transition(
                        LifecycleState.IDLE,
                        reason=f"Goal deduplicated ({dedup_res.reason_code})",
                        cycle_id=cycle_id,
                    )
                    await self._finalize_cycle(
                        cycle_id,
                        outcome,
                        pre_drives,
                        post_drives_dedup,
                        {"action": "dedup_skipped", "goal_dedup": dedup_res.model_dump(mode="json")},
                        t0,
                    )
                    return await self.repo.get_cycle(cycle_id) or {"cycle_id": str(cycle_id), "outcome": outcome}
                # Adopt the highest-scoring non-duplicate candidate (primary or alternative fallback!)
                goal_proposal = sel_res.selected_proposal

            self._consecutive_dedup_skips[decision.selected_drive] = 0
            if self.human_bridge is not None and pending_human_guidance:
                try:
                    await self.human_bridge.acknowledge_guidance(
                        [str(g["key"]) for g in pending_human_guidance if g.get("key")],
                        cycle_id=cycle_id,
                    )
                except Exception as exc:
                    logger.warning("cycle.human_guidance_ack_degraded", cycle_id=str(cycle_id), error=str(exc))

            # V3.1: Record lightweight world hypothesis (distinct from verified facts) if proposed
            expected_outcome_payload = dict(goal_proposal.expected_outcome or {})
            if isinstance(goal_proposal.hypothesis_to_test, dict):
                h_key = str(goal_proposal.hypothesis_to_test.get("key") or goal_proposal.related_key or "").strip()[:120]
                h_claim = str(goal_proposal.hypothesis_to_test.get("claim") or "").strip()[:500]
                if h_key and h_claim:
                    expected_outcome_payload["hypothesis_key"] = h_key
                    try:
                        await self.repo.upsert_world_item(
                            kind="hypothesis",
                            key=h_key,
                            value={
                                "claim": h_claim,
                                "verification_hint": str(goal_proposal.hypothesis_to_test.get("verification_hint") or "")[:300],
                                "confidence": 0.5,
                                "evidence": [],
                                "created_cycle_id": str(cycle_id),
                                "created_at": now.isoformat(),
                            },
                            status="unverified",
                            source=f"cycle:{cycle_id}",
                        )
                    except Exception as exc:
                        logger.warning("cycle.hypothesis_upsert_degraded", error=str(exc))

            # V3.1: Governed Curiosity-Driven Dynamic World Source Discovery (strictly allowlisted)
            if (
                decision.selected_drive == DriveType.CURIOSITY.value
                and isinstance(goal_proposal.discover_world_source, dict)
                and self.world_pipeline is not None
            ):
                src_req = goal_proposal.discover_world_source
                req_sid = str(src_req.get("source_id") or "").strip()[:80]
                req_type = str(src_req.get("sensor_type") or "rss").strip().lower()
                req_url = str(src_req.get("url") or "").strip()
                req_name = str(src_req.get("name") or req_sid).strip()[:160]
                if req_sid and req_url:
                    try:
                        reg_src = await self.world_pipeline.register_dynamic_source(
                            source_id=req_sid,
                            sensor_type=req_type,
                            name=req_name,
                            config={"url": req_url, "max_items": 8},
                            enabled=True,
                            reliability_score=0.75,
                            poll_interval_seconds=3600,
                        )
                        state_changes["discovered_world_source"] = {"accepted": True, "source_id": reg_src["source_id"]}
                        await self.repo.log_decision(
                            decision_type="active_perception_source",
                            selected="REGISTERED",
                            alternatives=[],
                            reason=f"Curiosity registered allowlisted dynamic source '{req_sid}'",
                            payload={"source_id": req_sid, "sensor_type": req_type, "url": req_url},
                            cycle_id=cycle_id,
                        )
                    except Exception as exc:
                        state_changes["discovered_world_source"] = {"accepted": False, "source_id": req_sid, "reason": str(exc)}
                        await self.repo.log_decision(
                            decision_type="active_perception_source",
                            selected="REJECTED",
                            alternatives=[],
                            reason=f"Dynamic source '{req_sid}' rejected by allowlist/governance: {exc}",
                            payload={"source_id": req_sid, "sensor_type": req_type, "url": req_url, "error": str(exc)},
                            cycle_id=cycle_id,
                        )

            # V4.0 M3: Track active external playbooks & Curiosity exploration topic lifecycle
            if external_playbooks:
                expected_outcome_payload["playbook_domains"] = [
                    str(pb["domain"]) for pb in external_playbooks if pb.get("domain")
                ]
            if self.exploration_manager is not None and isinstance(goal_proposal.exploration_topic, dict):
                et = goal_proposal.exploration_topic
                et_key = str(et.get("topic_key") or goal_proposal.related_key or goal_proposal.title[:60]).strip()
                et_q = str(et.get("question") or goal_proposal.title).strip()
                et_dom = et.get("domain")
                if et_key:
                    try:
                        tracked = await self.exploration_manager.upsert_topic(
                            topic_key=et_key,
                            question=et_q,
                            domain=str(et_dom) if et_dom else None,
                            status="exploring",
                            source=f"cycle:{cycle_id}",
                        )
                        expected_outcome_payload["exploration_topic_key"] = tracked["key"]
                        expected_outcome_payload["exploration_domain"] = tracked["domain"]
                    except Exception as exc:
                        logger.warning("cycle.exploration_topic_upsert_degraded", error=str(exc))

            # V4.2: Apply multi-hop investigation action or link explicit inv:<key>
            if self.investigation_manager is not None:
                if isinstance(goal_proposal.investigation_action, dict):
                    try:
                        inv_res = await self.investigation_manager.apply_goal_action(
                            goal_proposal.investigation_action,
                            origin_drive=decision.selected_drive,
                            origin_trigger=goal_proposal.description or goal_proposal.title,
                            cycle_id=cycle_id,
                        )
                        if inv_res and inv_res.get("thread_key"):
                            expected_outcome_payload["investigation_thread_key"] = inv_res["thread_key"]
                            state_changes["investigation_action"] = {
                                "thread_key": inv_res["thread_key"],
                                "stage": inv_res.get("stage"),
                            }
                    except Exception as exc:
                        logger.warning("cycle.investigation_action_degraded", error=str(exc))
                elif isinstance(goal_proposal.related_key, str) and goal_proposal.related_key.strip().lower().startswith("inv:"):
                    expected_outcome_payload["investigation_thread_key"] = goal_proposal.related_key.strip().lower()

            goal_id = await self.repo.create_goal(
                self.organism_id,
                cycle_id,
                drive_type=decision.selected_drive,
                title=goal_proposal.title[:500],
                description=goal_proposal.description,
                success_criteria=goal_proposal.success_criteria,
                expected_outcome=expected_outcome_payload,
                related_key=goal_proposal.related_key,
                status=GoalStatus.ACTIVE.value,
                goal_fingerprint=goal_fp,
                predicted_ig=round(float(goal_proposal.predicted_ig or 0.0), 4),
            )

            # ── 8. COGNITIVE PLANNING ───────────────────────────────────────
            await self.repo.update_cycle(cycle_id, phase="COGNITIVE_PLANNING")
            plan = await self._plan_safe(
                cog_ctx.model_copy(update={"goal": goal_proposal}), llm_available_for_brain, cycle_id, goal_id=goal_id
            )
            await self.repo.update_cycle(
                cycle_id,
                brain_output={
                    "assessment": assessment.model_dump(mode="json"),
                    "goal": goal_proposal.model_dump(mode="json"),
                    "goal_candidates": goal_candidates_audit,
                    "affect_snapshot": affect_state_snapshot,
                },
                plan=plan.model_dump(mode="json"),
            )

            if not plan.should_execute or not plan.objective.strip():
                await self.repo.transition_goal(goal_id, GoalStatus.ABANDONED.value, note="planner chose not to execute")
                outcome = "IDLE"
                await self.lifecycle.transition(LifecycleState.IDLE, reason="Planner produced no executable action", cycle_id=cycle_id)
                await self._finalize_cycle(cycle_id, outcome, pre_drives, pre_drives, {"action": "abandoned"}, t0)
                return await self.repo.get_cycle(cycle_id) or {"cycle_id": str(cycle_id), "outcome": outcome}

            # ── 9. POLICY VALIDATION & DETERMINISTIC ACTION PREFLIGHT ───────
            await self.repo.update_cycle(cycle_id, phase="POLICY_VALIDATION")
            verdict = await self.governance.validate_plan(plan, decision.selected_drive)
            plan_hash = compute_plan_hash(plan)

            recent_hashes = (
                await self.repo.list_recent_plan_hashes(
                    since=now - timedelta(minutes=max(1, int(kv3.duplicate_plan_window_minutes)))
                )
                if kv3.plan_preflight_enabled
                else []
            )
            preflight_res = (
                self.preflight.validate(
                    plan=plan,
                    verdict=verdict,
                    registry=self.registry,
                    budget=budget,
                    recent_plan_hashes=recent_hashes,
                )
                if kv3.plan_preflight_enabled
                else None
            )

            if preflight_res is not None and preflight_res.decision == "REJECT" and verdict.allowed:
                plan_status = "PREFLIGHT_REJECTED"
            else:
                plan_status = "APPROVED" if verdict.allowed else ("REQUIRES_APPROVAL" if verdict.requires_approval else "DENIED")

            plan_id = await self.repo.create_plan(
                goal_id=goal_id,
                cycle_id=cycle_id,
                plan=plan,
                permission_level=verdict.effective_level,
                verdict=verdict,
                status=plan_status,
                plan_hash=plan_hash,
                preflight_status=preflight_res.decision if preflight_res else None,
                preflight_reason_codes=preflight_res.reason_codes if preflight_res else None,
                preflight_checked_at=preflight_res.checked_at if preflight_res else None,
            )
            await self.repo.record_policy_evaluation(
                subject_type="plan",
                subject_id=plan_id,
                cycle_id=cycle_id,
                requested_level=verdict.requested_level,
                deterministic_level=verdict.deterministic_level or verdict.effective_level,
                semantic_level=verdict.semantic_level,
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
                payload={
                    **verdict.model_dump(mode="json"),
                    "preflight": preflight_res.model_dump(mode="json") if preflight_res else None,
                },
                cycle_id=cycle_id,
            )

            if preflight_res is not None and preflight_res.decision == "REJECT" and verdict.allowed:
                cd_until = compute_cooldown_until(
                    now=now,
                    goal_achieved=False,
                    verified_ig=0.0,
                    failure_streak=1,
                    failed_cooldown_minutes=kv3.goal_failed_cooldown_minutes,
                )
                await self.repo.update_goal(goal_id, cooldown_until=cd_until, verified_ig=0.0)
                await self.repo.transition_goal(
                    goal_id,
                    GoalStatus.REJECTED.value,
                    note=f"ActionPreflight rejected ({', '.join(preflight_res.reason_codes)}): {'; '.join(preflight_res.notes)}",
                )
                await self.repo.log_decision(
                    decision_type="plan_preflight",
                    selected="REJECT",
                    alternatives=[],
                    reason="; ".join(preflight_res.notes) or ",".join(preflight_res.reason_codes),
                    payload=preflight_res.model_dump(mode="json"),
                    cycle_id=cycle_id,
                )
                outcome = "PREFLIGHT_REJECTED"
                await self._finalize_cycle(
                    cycle_id,
                    outcome,
                    pre_drives,
                    pre_drives,
                    {"preflight": preflight_res.model_dump(mode="json")},
                    t0,
                )
                return await self.repo.get_cycle(cycle_id) or {"cycle_id": str(cycle_id), "outcome": outcome}

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
                    if self.approval_service is not None:
                        await self.approval_service.create_approval_request(
                            task_id=pending_task_id,
                            plan_id=plan_id,
                            plan_hash=plan_hash,
                            params_digest=params_digest,
                            permission_level=verdict.effective_level,
                            objective=plan.objective,
                            assigned_agent=plan.assigned_agent,
                            cycle_id=cycle_id,
                        )
                    else:
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

            # ── 10. BOUNDED L0 MCP PREFETCH + AGENT DISPATCH ────────────────
            if (
                self.mcp_manager is not None
                and self.settings.kernel_v3.mcp_in_loop_enabled
                and plan.mcp_prefetch_calls
            ):
                mcp_summary, enriched_desc = await self._run_mcp_prefetch(cycle_id=cycle_id, plan=plan)
                if mcp_summary:
                    state_changes["mcp_prefetch"] = mcp_summary
                if enriched_desc:
                    plan = plan.model_copy(update={"description": enriched_desc})

            await self.repo.update_cycle(cycle_id, phase="AGENT_DISPATCH")
            task_state: dict[str, Any] | None = None
            # V4.0 M1: Skill-First Execution — run matching VALIDATED skill directly via SkillRuntime (0 Codex tokens!)
            if (
                kv3.skill_first_planning_enabled
                and self.skill_runtime is not None
                and isinstance(plan.skill_invocation, dict)
                and plan.skill_invocation.get("skill_key")
            ):
                task_state = await self._try_skill_first_execution(
                    cycle_id=cycle_id,
                    goal_id=goal_id,
                    plan_id=plan_id,
                    plan=plan,
                    verdict=verdict,
                )

            if task_state is None:
                retry_backoff_mult = float(
                    ((decision.constraints or {}).get("affect_modulation") or {}).get("retry_backoff_multiplier", 1.0) or 1.0
                )
                dispatch_info = await self.orchestrator.submit_plan(
                    plan=plan,
                    verdict=verdict,
                    goal_id=goal_id,
                    plan_id=plan_id,
                    cycle_id=cycle_id,
                    retry_backoff_multiplier=retry_backoff_mult,
                )
                task_id = dispatch_info["task_id"]

                # ── 11. WAIT / MONITOR ──────────────────────────────────────────
                await self.repo.update_cycle(cycle_id, phase="WAIT_MONITOR")
                task_state = await self.orchestrator.wait_for_task(
                    task_id=task_id,
                    max_wait_seconds=self.settings.runtime.max_wait_per_cycle_seconds,
                    poll_interval=self.settings.runtime.monitor_poll_seconds,
                )
            else:
                task_id = task_state["task_id"]
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

        # Include unresolved active investigation leads as genuine environment unknowns (V4.2)
        seen_env_topics = {g.topic.lower().strip() for g in env_topics}
        if self.investigation_manager is not None:
            try:
                for g in await self.investigation_manager.pending_leads_as_knowledge_gaps(limit=3):
                    key = g.topic.lower().strip()
                    if key and key not in seen_env_topics:
                        env_topics.append(g)
                        seen_env_topics.add(key)
            except Exception as exc:
                logger.warning("cycle.investigation_gaps_degraded", error=str(exc))

        # Merge environment topics + LLM-identified gaps (dedup by topic)
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

        if self.self_model is not None:
            acquired_caps, validated_caps = await self.self_model.get_evidence_backed_capabilities()
        else:
            acquired_caps = list(env.get("capabilities_acquired") or [])
            validated_caps = []
        validated_skill_count: int | None = None
        if self.skill_registry is not None:
            validated_skill_count = await self.skill_registry.count_validated_skills(verify_integrity=True)

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
            capabilities_acquired=acquired_caps,
            validated_capabilities=validated_caps,
            validated_skill_count=validated_skill_count,
            procedural_skill_count=proc_count,
            capability_goals_completed_recent=cap_goals_done,
            memory_available=self.memory.backend_available,
            identity_persisted=True,
            knowledge_transfer_readiness=1.0 if sem_recent > 0 else 0.5,
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
        self,
        cog_ctx: CognitiveContext,
        llm_available: bool,
        cycle_id: uuid.UUID,
        goal_id: uuid.UUID | None = None,
    ) -> ActionPlan:
        is_em = bool(cog_ctx.decision.constraints.get("emergency_mode"))
        if llm_available:
            try:
                with self._gateway_scope(cycle_id=cycle_id, goal_id=goal_id, purpose="plan", emergency=is_em):
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

    async def _try_skill_first_execution(
        self,
        *,
        cycle_id: uuid.UUID,
        goal_id: uuid.UUID,
        plan_id: uuid.UUID,
        plan: ActionPlan,
        verdict: Any,
    ) -> dict[str, Any] | None:
        """Attempt direct isolated SkillRuntime execution before falling back to Codex (V4.0 M1)."""
        inv_spec = plan.skill_invocation or {}
        skill_ref = str(inv_spec.get("skill_key") or "").strip()
        skill_ver = str(inv_spec.get("version")).strip() if inv_spec.get("version") else None
        in_data = inv_spec.get("input_data") if isinstance(inv_spec.get("input_data"), dict) else {}
        if not skill_ref or self.skill_runtime is None:
            return None

        task_uuid = uuid.uuid4()
        try:
            s_res = await self.skill_runtime.execute(
                skill_ref,
                in_data,
                version=skill_ver,
                invocation_source="skill_first",
                task_id=task_uuid,
                cycle_id=cycle_id,
            )
        except Exception as exc:
            logger.warning("loop.skill_first_exception", skill_ref=skill_ref, error=str(exc))
            await self.repo.log_decision(
                decision_type="skill_first_fallback",
                selected=plan.assigned_agent,
                alternatives=[skill_ref],
                reason=f"Skill-first execution raised exception ({exc}); falling back to {plan.assigned_agent}",
                payload={"skill_key": skill_ref, "error": str(exc)},
                cycle_id=cycle_id,
            )
            return None

        if not s_res.ok:
            await self.repo.log_decision(
                decision_type="skill_first_fallback",
                selected=plan.assigned_agent,
                alternatives=[skill_ref],
                reason=f"Skill-first '{skill_ref}' unavailable/failed ({s_res.error}); falling back to {plan.assigned_agent}",
                payload=s_res.model_dump(mode="json"),
                cycle_id=cycle_id,
            )
            return None

        import json as _json

        now = utcnow()
        exec_uuid = uuid.uuid4()
        ws = self.governance.prepare_workspace(str(task_uuid))
        out_str = _json.dumps(s_res.output, ensure_ascii=False, default=str) if s_res.output is not None else s_res.stdout_excerpt
        verif_dict = {
            "passed": True,
            "summary": f"SkillRuntime verified execution of {s_res.canonical_name or s_res.skill_key}@{s_res.version}",
            "checks": [
                {"kind": "skill_runtime_exit", "passed": True, "detail": f"latency_ms={s_res.latency_ms}"},
                {"kind": "skill_schema_and_integrity", "passed": True, "detail": f"reuse={s_res.is_cross_task_reuse}"},
            ],
        }
        outcome_dict = {
            "execution_id": str(exec_uuid),
            "agent_id": "skill_runtime",
            "status": "SUCCEEDED",
            "verification": verif_dict,
            "skill_first_execution": {
                "skill_id": s_res.skill_id,
                "skill_key": s_res.skill_key,
                "canonical_name": s_res.canonical_name,
                "version": s_res.version,
                "is_cross_task_reuse": s_res.is_cross_task_reuse,
                "estimated_saved_tokens": s_res.estimated_saved_tokens,
                "invocation_id": s_res.invocation_id,
            },
        }
        await self.repo.create_task(
            id=task_uuid,
            plan_id=plan_id,
            goal_id=goal_id,
            cycle_id=cycle_id,
            objective=plan.objective,
            description=plan.description,
            assigned_agent="skill_runtime",
            expected_output=plan.expected_output,
            workspace=ws,
            timeout_seconds=int(self.skill_runtime.default_timeout_seconds),
            resource_budget={"sandbox_mode": verdict.sandbox_mode, "skill_first": True},
            permission_level=verdict.effective_level,
            success_criteria=plan.success_criteria,
            verification=[v.model_dump(mode="json") for v in plan.verification],
            required_capability=plan.required_capability,
            status=TaskStatus.SUCCEEDED.value,
            attempt=1,
            max_attempts=1,
            tried_agents=["skill_runtime"],
            outcome=outcome_dict,
        )
        await self.repo.create_execution(
            task_uuid,
            "skill_runtime",
            "SUCCEEDED",
            execution_id=exec_uuid,
            attempt_number=1,
            idempotency_key=f"skill_first:{task_uuid}",
        )
        await self.repo.update_execution(
            exec_uuid,
            completed_at=now,
            result={"summary": f"SkillRuntime executed {s_res.canonical_name or s_res.skill_key}@{s_res.version}", "output": s_res.output},
            stdout=out_str[:8000],
            verification=verif_dict,
            resource_usage={"wall_seconds": round(s_res.latency_ms / 1000.0, 3), "estimated_saved_tokens": s_res.estimated_saved_tokens},
        )
        await self.repo.log_decision(
            decision_type="skill_first_execution",
            selected=s_res.canonical_name or s_res.skill_key,
            alternatives=[plan.assigned_agent],
            reason=f"Skill-first directly executed '{s_res.canonical_name or s_res.skill_key}@{s_res.version}' in {s_res.latency_ms}ms (bypassed {plan.assigned_agent})",
            payload=outcome_dict["skill_first_execution"],
            cycle_id=cycle_id,
        )
        return (await self.repo.get_task(task_uuid)) or {
            "task_id": str(task_uuid),
            "status": TaskStatus.SUCCEEDED.value,
            "outcome": outcome_dict,
        }

    async def _run_mcp_prefetch(
        self,
        *,
        cycle_id: uuid.UUID,
        plan: ActionPlan,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Execute strictly bounded L0 read-only MCP prefetch calls inside AutonomousLoop (V3.0 M3).

        Hard guarantees:
          - Never bypasses MCPClientManager Governance / policy_evaluations / resource_usage.
          - Strictly refuses any L1+ / mutating tool call during autonomous prefetch.
          - Quarantines Prompt Injection payloads; wraps clean results as untrusted external data.
          - Fault-isolated: timeouts or server crashes NEVER block or fail the cycle.
        """
        if self.mcp_manager is None or not plan.mcp_prefetch_calls:
            return [], None

        max_calls = max(1, int(self.settings.kernel_v3.max_mcp_calls_per_cycle))
        audit_records: list[dict[str, Any]] = []
        clean_excerpts: list[str] = []

        for raw_call in list(plan.mcp_prefetch_calls)[:max_calls]:
            if not isinstance(raw_call, dict):
                continue
            tool_name = str(raw_call.get("tool_name") or "").strip()
            raw_args = raw_call.get("arguments")
            if not isinstance(raw_args, dict):
                raw_args = raw_call.get("args")
            args = dict(raw_args) if isinstance(raw_args, dict) else {}
            if not tool_name:
                continue

            # Hard L0 read-only gate for autonomous prefetch
            spec = self.mcp_manager.get_tool(tool_name)
            if spec is not None:
                action_val = str(args.get("action") or "").strip().lower()
                eff_req_level = spec.action_levels.get(action_val, spec.permission_level)
                if str(eff_req_level).upper() != PermissionLevel.L0.value:
                    audit_records.append(
                        {
                            "tool_name": tool_name,
                            "ok": False,
                            "blocked": True,
                            "reason": f"Autonomous MCP prefetch allows L0 read-only only (got {eff_req_level})",
                        }
                    )
                    await self.repo.log_event(
                        "loop.mcp_prefetch_blocked",
                        message=f"Blocked non-L0 MCP prefetch call {tool_name} ({eff_req_level})",
                        payload={"tool_name": tool_name, "requested_level": eff_req_level},
                        cycle_id=cycle_id,
                    )
                    continue

            try:
                res = await self.mcp_manager.invoke(
                    ToolCallRequest(
                        tool_name=tool_name,
                        arguments=args,
                        cycle_id=str(cycle_id),
                        workspace=self.settings.governance.workspace_root,
                    )
                )
            except Exception as exc:
                logger.warning("loop.mcp_prefetch_degraded", cycle_id=str(cycle_id), tool=tool_name, error=str(exc))
                audit_records.append({"tool_name": tool_name, "ok": False, "error": str(exc)[:300]})
                continue

            if not res.ok:
                audit_records.append(
                    {
                        "tool_name": tool_name,
                        "ok": False,
                        "denied": res.denied,
                        "requires_approval": res.requires_approval,
                        "error": res.error,
                    }
                )
                continue

            # Sanitize & guard against Prompt Injection before surfacing to external Agent
            raw_text = _format_mcp_output_for_prefetch(tool_name, res.output)
            clean_text = sanitize_external_text(raw_text, max_chars=3600)
            inj_flags = detect_prompt_injection(raw_text) or detect_prompt_injection(clean_text)
            if inj_flags:
                audit_records.append(
                    {
                        "tool_name": tool_name,
                        "ok": False,
                        "quarantined_injection": True,
                        "injection_flags": inj_flags,
                    }
                )
                await self.repo.log_event(
                    "loop.mcp_injection_quarantined",
                    message=f"Quarantined prompt injection in MCP output from {tool_name}",
                    payload={"tool_name": tool_name, "flags": inj_flags},
                    cycle_id=cycle_id,
                )
                continue

            wrapped = wrap_untrusted_world_event(f"mcp:{tool_name}", f"MCP prefetch ({tool_name})", clean_text)
            clean_excerpts.append(wrapped)
            audit_records.append(
                {
                    "tool_name": tool_name,
                    "ok": True,
                    "latency_ms": res.latency_ms,
                    "truncated": res.truncated,
                    "policy_evaluation_id": res.policy_evaluation_id,
                }
            )

        enriched_desc: str | None = None
        if clean_excerpts:
            base_desc = (plan.description or "").strip()
            block = "\n\n".join(clean_excerpts)
            enriched_desc = f"{base_desc}\n\n[Read-Only MCP Prefetched Context]\n{block}".strip()
        return audit_records, enriched_desc

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

    def _gateway_scope(
        self,
        *,
        cycle_id: uuid.UUID | None = None,
        goal_id: uuid.UUID | None = None,
        purpose: str | None = None,
        emergency: bool = False,
    ):
        if isinstance(self.llm_gateway, LLMGateway):
            return self.llm_gateway.scope(cycle_id=cycle_id, goal_id=goal_id, purpose=purpose, emergency=emergency)
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
