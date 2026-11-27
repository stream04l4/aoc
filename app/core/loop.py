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
import hashlib
import json
import re
import secrets
import time
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

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
    BrainResearchOutput,
    CognitiveContext,
    GoalProposal,
    ResearchSynthesisContext,
    SocialContext,
    SocialInboxMessage,
    SocialTurnDecision,
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
from app.drives.interfaces import DriveContext, DriveDecision, DriveState, KnowledgeGap, clamp01
from app.governance.interfaces import GovernanceInterface
from app.memory.experience_manager import ExperienceManager
from app.memory.interfaces import MemoryItem
from app.memory.retrieval import build_contextual_query
from app.notifications.telegram import sanitize_reaction_emoji
from app.observability.logging import get_logger
from app.observability.metrics import CYCLE_DURATION, CYCLES_TOTAL
from app.orchestration.finalizer import TaskFinalizer
from app.orchestration.orchestrator import AgentOrchestrator, compute_params_digest, compute_plan_hash
from app.perception.interfaces import Observation, utcnow
from app.perception.manager import SensorManager
from app.perception.world.exploration import (
    infer_topic_domain,
    is_substantive_interest_guidance,
    match_interest_portfolio_item,
)
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
            f"Excerpt: {str(output.get('markdown_excerpt') or '')[:5500]}",
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
        entropy_sensor: Any = None,
        culling_gc: Any = None,
        telegram_notifier: Any = None,
        meta_reflection_manager: Any = None,
        self_edit_manager: Any = None,
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
        self.entropy_sensor = entropy_sensor
        self.culling_gc = culling_gc
        self.telegram_notifier = telegram_notifier
        self.meta_reflection_manager = meta_reflection_manager
        self.self_edit_manager = self_edit_manager
        self._consecutive_idle_cycles: int = 0
        self._low_ig_streak: int = 0
        self._last_prediction_error: float = 0.0
        self._consecutive_dedup_skips: dict[str, int] = {}
        self._last_draft_cleanup_at: datetime | None = None
        self._last_skill_lifecycle_audit_at: datetime | None = None

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
            meta_reflection_manager=meta_reflection_manager,
        )
        if hasattr(self.finalizer, "register_hook"):
            existing_hooks = getattr(self.finalizer, "_hooks", ())
            if self._record_social_milestone_from_finalizer not in existing_hooks:
                self.finalizer.register_hook(self._record_social_milestone_from_finalizer)

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
        evo_orphan_rec: dict[str, Any] = {"skipped": True}
        if self.culling_gc is not None and self.settings.database.schema_name == "public":
            try:
                evo_orphan_rec = await self.culling_gc.recover_orphan_candidates()
            except Exception as exc:
                logger.warning("loop.evolution_orphan_gc_degraded", error=str(exc))
                evo_orphan_rec = {"error": str(exc)}
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
            "evolution_orphan_gc": evo_orphan_rec,
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
            await self._expire_stale_active_goals(now=now, cycle_id=cycle_id)
            await self._expire_stale_draft_skills(now=now, cycle_id=cycle_id)
            await self._run_skill_lifecycle_audit_if_due(now=now, cycle_id=cycle_id)
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

            # ── 4b. RULE-GATED PERIODIC META-REFLECTION CRITIC (V5.2 M4) ────
            if (
                self.meta_reflection_manager is not None
                and self.settings.kernel_v3.meta_reflection_enabled
                and llm_available_for_brain
                and not emergency_llm
            ):
                try:
                    meta_res = await self.meta_reflection_manager.run_if_due(now=now, cycle_id=cycle_id)
                    if isinstance(meta_res, dict) and meta_res.get("triggered"):
                        state_changes["meta_reflection"] = meta_res
                except Exception as exc:
                    logger.warning("cycle.meta_reflection_degraded", cycle_id=str(cycle_id), error=str(exc))

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

            # ── 6.5. SOCIAL REFLECTION (V5.1 Unified Consciousness Hook) ────
            # Executed strictly AFTER Drive Arbitration & Affect update, and BEFORE IDLE / BUDGET_DEFERRED returns.
            if self.settings.social.enabled:
                social_res = await self._run_social_reflection(
                    cycle_id=cycle_id,
                    now=now,
                    pre_obs=pre_obs,
                    pre_drives=pre_drives,
                    decision=decision,
                    assessment=assessment,
                    affect_state_snapshot=affect_state_snapshot,
                    llm_available_for_brain=llm_available_for_brain,
                    emergency_llm=emergency_llm,
                )
                state_changes["social_reflection"] = social_res

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
                    state_changes={**state_changes, "action": "none", "reason": "all drives satisfied or below threshold"},
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
                    state_changes={**state_changes, "action": "deferred", "budget": budget.model_dump(mode="json")},
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
            pending_human_guidance: list[dict[str, Any]] = []
            if self.human_bridge is not None:
                try:
                    pending_human_guidance = await self.human_bridge.list_pending_guidance(limit=5)
                except Exception as exc:
                    logger.warning("cycle.human_guidance_load_degraded", cycle_id=str(cycle_id), error=str(exc))
            outreach_avail = bool(self.human_bridge is not None and getattr(self.human_bridge, "enabled", False))

            interest_portfolio_items: list[dict[str, Any]] = []
            if self.exploration_manager is not None and kv3.interest_portfolio_enabled:
                for hg in pending_human_guidance:
                    hg_txt = str(hg.get("text") or "").strip()
                    if is_substantive_interest_guidance(hg_txt):
                        try:
                            await self.exploration_manager.upsert_interest_item(
                                prompt=hg_txt,
                                origin="human_seeded",
                                priority=0.90,
                                status="active",
                                source=f"human_guidance:{hg.get('sender') or 'joel'}",
                                sender=str(hg.get("sender") or "joel"),
                            )
                        except Exception as exc:
                            logger.warning("cycle.interest_portfolio_seed_degraded", cycle_id=str(cycle_id), error=str(exc))
                try:
                    interest_portfolio_items = await self.exploration_manager.list_interest_portfolio(
                        statuses=("active",),
                        limit=8,
                    )
                except Exception as exc:
                    logger.warning("cycle.interest_portfolio_load_degraded", cycle_id=str(cycle_id), error=str(exc))

            exploration_guidance: dict[str, Any] | None = None
            if self.exploration_manager is not None:
                try:
                    exploration_guidance = await self.exploration_manager.build_guidance(recent_goals)
                except Exception as exc:
                    logger.warning("cycle.exploration_guidance_degraded", cycle_id=str(cycle_id), error=str(exc))

            if interest_portfolio_items and exploration_guidance:
                starved_keys = set(exploration_guidance.get("starved_human_portfolio_keys") or [])
                interest_portfolio_items = [
                    {**it, "starved": (it.get("key") in starved_keys)}
                    for it in interest_portfolio_items
                ]

            external_playbooks: list[dict[str, Any]] = []
            if self.playbook_manager is not None:
                try:
                    external_playbooks = await self.playbook_manager.list_playbooks_for_context(
                        domains=[decision.selected_drive, "coding", "general"],
                        limit=3,
                    )
                except Exception as exc:
                    logger.warning("cycle.playbooks_load_degraded", cycle_id=str(cycle_id), error=str(exc))

            active_investigations: list[dict[str, Any]] = []
            discovered_channels: list[dict[str, Any]] = []
            if self.investigation_manager is not None:
                try:
                    active_investigations, discovered_channels = await self.investigation_manager.build_context_snapshot()
                except Exception as exc:
                    logger.warning("cycle.investigations_load_degraded", cycle_id=str(cycle_id), error=str(exc))

            pending_social_hints: list[dict[str, Any]] = []
            if self.settings.social.enabled:
                try:
                    rhythm_rows_g = await self.repo.list_world_items(kind="social_rhythm_state")
                    rhythm_g = next(
                        (
                            dict(r.get("value") or {})
                            for r in rhythm_rows_g
                            if r.get("key") == "primary" and isinstance(r.get("value"), dict)
                        ),
                        {},
                    )
                    pending_social_hints = [
                        h
                        for h in list(rhythm_g.get("pending_goal_hints") or [])
                        if isinstance(h, dict) and str(h.get("hint") or "").strip()
                    ][:3]
                except Exception as exc:
                    logger.warning("cycle.social_goal_hints_load_degraded", cycle_id=str(cycle_id), error=str(exc))

            pending_skill_refactors: list[dict[str, Any]] = []
            if kv3.meta_reflection_enabled or kv3.explicit_skill_refactor_enabled:
                try:
                    if self.meta_reflection_manager is not None and hasattr(
                        self.meta_reflection_manager, "list_pending_skill_refactors"
                    ):
                        pending_skill_refactors = await self.meta_reflection_manager.list_pending_skill_refactors(limit=3)
                    else:
                        q_items = await self.repo.list_world_items(kind="skill_refactor_queue", status="open")
                        pending_skill_refactors = [
                            dict(it["value"])
                            for it in q_items[:3]
                            if isinstance(it.get("value"), dict)
                        ]
                except Exception as exc:
                    logger.warning("cycle.skill_refactors_load_degraded", cycle_id=str(cycle_id), error=str(exc))

            pending_self_edits: list[dict[str, Any]] = []
            if kv3.self_edit_enabled and self.self_edit_manager is not None:
                try:
                    pending_self_edits = await self.self_edit_manager.list_pending_self_edits(limit=3)
                except Exception as exc:
                    logger.warning("cycle.self_edits_load_degraded", cycle_id=str(cycle_id), error=str(exc))

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
                social_goal_hints=pending_social_hints,
                brain_research_enabled=bool(self.settings.kernel_v3.brain_research_enabled),
                interest_portfolio=interest_portfolio_items,
                explicit_skill_refactor_enabled=bool(self.settings.kernel_v3.explicit_skill_refactor_enabled),
                pending_skill_refactors=pending_skill_refactors,
                self_edit_enabled=bool(kv3.self_edit_enabled and self.self_edit_manager is not None),
                self_edit_policy_mode=str(kv3.self_edit_policy_mode or "strict"),
                pending_self_edits=pending_self_edits,
                budget_remaining=budget.model_dump(mode="json"),
                active_goals=await self.repo.list_goals(statuses=[GoalStatus.ACTIVE.value], limit=5),
            )

            goal_proposal = await self._generate_goal_safe(cog_ctx, llm_available_for_brain, cycle_id)
            if pending_social_hints:
                try:
                    await self.repo.upsert_world_item(
                        kind="social_rhythm_state",
                        key="primary",
                        value={"pending_goal_hints": []},
                        status="active",
                        source="telegram:social",
                    )
                except Exception as exc:
                    logger.warning("cycle.social_goal_hints_clear_degraded", cycle_id=str(cycle_id), error=str(exc))
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
                            options=m.get("options") if isinstance(m.get("options"), list) else None,
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
                    state_changes={**state_changes, "action": "none", "goal_rationale": goal_proposal.rationale},
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
                is_curiosity = decision.selected_drive == DriveType.CURIOSITY.value
                prefer_external = bool(
                    is_curiosity and (exploration_guidance or {}).get("must_explore_external")
                )
                must_pivot_domain = bool(
                    kv3.interest_portfolio_enabled
                    and is_curiosity
                    and (exploration_guidance or {}).get("must_pivot_domain")
                )
                cooldown_domains = (
                    list((exploration_guidance or {}).get("cooldown_domains") or [])
                    if kv3.interest_portfolio_enabled and is_curiosity
                    else []
                )
                cooldown_topic = (
                    (exploration_guidance or {}).get("cooldown_topic")
                    if kv3.interest_portfolio_enabled and is_curiosity
                    else None
                )
                stalled_topic_clusters = (
                    list((exploration_guidance or {}).get("stalled_topic_clusters") or [])
                    if is_curiosity
                    else []
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
                    must_pivot_domain=must_pivot_domain,
                    cooldown_domains=cooldown_domains,
                    cooldown_topic=cooldown_topic,
                    stalled_topic_clusters=stalled_topic_clusters,
                    interest_portfolio=(
                        interest_portfolio_items if kv3.interest_portfolio_enabled and is_curiosity else None
                    ),
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
                        {**state_changes, "action": "dedup_skipped", "goal_dedup": dedup_res.model_dump(mode="json")},
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

            if kv3.interest_portfolio_enabled and decision.selected_drive == DriveType.CURIOSITY.value:
                et_dict = goal_proposal.exploration_topic if isinstance(goal_proposal.exploration_topic, dict) else {}
                resolved_dom = expected_outcome_payload.get("exploration_domain") or infer_topic_domain(
                    goal_proposal.title,
                    goal_proposal.description,
                    explicit_domain=et_dict.get("domain"),
                )
                expected_outcome_payload["exploration_domain"] = resolved_dom
                if interest_portfolio_items and not expected_outcome_payload.get("interest_portfolio_key"):
                    matched_ip = match_interest_portfolio_item(
                        interest_portfolio_items,
                        title=goal_proposal.title,
                        description=goal_proposal.description,
                        related_key=goal_proposal.related_key,
                        domain=resolved_dom,
                    )
                    if matched_ip is not None:
                        expected_outcome_payload["interest_portfolio_key"] = matched_ip["key"]

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
            top_skill_match_cand = None
            if kv3.skill_first_planning_enabled and reusable_skills:
                try:
                    from app.skills.matcher import SkillMatchPreSelector

                    top_skill_match_cand = SkillMatchPreSelector().select_top_match(
                        goal_proposal,
                        reusable_skills,
                        threshold=float(kv3.skill_match_hint_threshold),
                    )
                except Exception as exc:
                    logger.warning("cycle.skill_matcher_degraded", cycle_id=str(cycle_id), error=str(exc))

            plan_ctx = cog_ctx.model_copy(
                update={
                    "goal": goal_proposal,
                    "top_skill_match": (
                        top_skill_match_cand.model_dump(mode="json") if top_skill_match_cand is not None else None
                    ),
                }
            )
            plan = await self._plan_safe(plan_ctx, llm_available_for_brain, cycle_id, goal_id=goal_id)
            if top_skill_match_cand is not None and plan.should_execute:
                try:
                    chosen_mode = str(plan.execution_mode or "codex").strip().lower()
                    if chosen_mode == "skill_first":
                        await self.repo.log_event(
                            "skill_matcher.hit",
                            level="info",
                            message=f"Skill pre-selector matched '{top_skill_match_cand.canonical_name}' ({top_skill_match_cand.match_score:.2f}) -> skill_first",
                            payload={
                                "matched_skill": top_skill_match_cand.canonical_name,
                                "match_score": top_skill_match_cand.match_score,
                                "chosen_mode": chosen_mode,
                            },
                            cycle_id=cycle_id,
                        )
                    else:
                        await self.repo.log_event(
                            "skill_matcher.override",
                            level="info",
                            message=f"Planner overrode high-confidence skill '{top_skill_match_cand.canonical_name}' ({top_skill_match_cand.match_score:.2f}) with {chosen_mode}",
                            payload={
                                "matched_skill": top_skill_match_cand.canonical_name,
                                "match_score": top_skill_match_cand.match_score,
                                "chosen_mode": chosen_mode,
                                "rationale": str(plan.rationale or "")[:300],
                            },
                            cycle_id=cycle_id,
                        )
                except Exception as exc:
                    logger.warning("cycle.skill_matcher_event_degraded", error=str(exc))
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
                await self._finalize_cycle(cycle_id, outcome, pre_drives, pre_drives, {**state_changes, "action": "abandoned"}, t0)
                return await self.repo.get_cycle(cycle_id) or {"cycle_id": str(cycle_id), "outcome": outcome}

            # ── 9. POLICY VALIDATION & DETERMINISTIC ACTION PREFLIGHT ───────
            await self.repo.update_cycle(cycle_id, phase="POLICY_VALIDATION")
            plan_wants_brain = (
                str(plan.execution_mode or "").strip().lower() == "brain_research"
                or str(plan.assigned_agent or "").strip().lower() == "brain_research"
            )
            if not kv3.brain_research_enabled and plan_wants_brain:
                healthy_agents = self.registry.list_healthy()
                fallback_agent = healthy_agents[0].agent_id if healthy_agents else "codex"
                plan = plan.model_copy(update={"execution_mode": "codex", "assigned_agent": fallback_agent})
            if not kv3.explicit_skill_refactor_enabled and plan.skill_refactor_target is not None:
                plan = plan.model_copy(update={"skill_refactor_target": None})
            plan_wants_self_edit = str(plan.execution_mode or "").strip().lower() == "self_edit"
            is_self_edit = bool(
                kv3.self_edit_enabled
                and self.self_edit_manager is not None
                and plan_wants_self_edit
            )
            if plan_wants_self_edit and not is_self_edit:
                healthy_agents = self.registry.list_healthy()
                fallback_agent = healthy_agents[0].agent_id if healthy_agents else "codex"
                plan = plan.model_copy(update={"execution_mode": "codex", "assigned_agent": fallback_agent, "self_edit_spec": None})
            elif is_self_edit and plan.permission_level == PermissionLevel.L0.value:
                plan = plan.model_copy(update={"permission_level": PermissionLevel.L1.value})
            is_brain_research = bool(kv3.brain_research_enabled) and plan_wants_brain
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
                    {**state_changes, "preflight": preflight_res.model_dump(mode="json")},
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
                    {**state_changes, "policy_verdict": verdict.model_dump(mode="json")},
                    t0,
                )
                return await self.repo.get_cycle(cycle_id) or {"cycle_id": str(cycle_id), "outcome": outcome}

            # ── 10. BOUNDED L0 MCP PREFETCH + AGENT DISPATCH ────────────────
            if (
                not is_brain_research
                and self.mcp_manager is not None
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

            if task_state is None and is_brain_research:
                task_state, br_summary = await self._try_brain_native_research(
                    cycle_id=cycle_id,
                    goal_id=goal_id,
                    plan_id=plan_id,
                    goal=goal_proposal,
                    plan=plan,
                    verdict=verdict,
                    recalled_memories=mem_dicts,
                    active_investigations=active_investigations,
                    human_guidance=pending_human_guidance,
                    llm_available=llm_available_for_brain,
                    emergency_llm=bool(decision.constraints.get("emergency_mode")),
                )
                if br_summary:
                    state_changes["brain_research"] = br_summary

            if task_state is None:
                if str(plan.assigned_agent or "").strip().lower() == "skill_runtime":
                    healthy_agents = self.registry.list_healthy()
                    fallback_agent = healthy_agents[0].agent_id if healthy_agents else "codex"
                    plan = plan.model_copy(update={"execution_mode": "codex", "assigned_agent": fallback_agent})
                retry_backoff_mult = float(
                    ((decision.constraints or {}).get("affect_modulation") or {}).get("retry_backoff_multiplier", 1.0) or 1.0
                )
                submit_kwargs: dict[str, Any] = {
                    "plan": plan,
                    "verdict": verdict,
                    "goal_id": goal_id,
                    "plan_id": plan_id,
                    "cycle_id": cycle_id,
                    "retry_backoff_multiplier": retry_backoff_mult,
                }
                se_meta: dict[str, Any] | None = None
                if is_self_edit and self.self_edit_manager is not None:
                    se_spec = plan.self_edit_spec if isinstance(plan.self_edit_spec, dict) else {}
                    t_files = [
                        str(f).strip()
                        for f in list(se_spec.get("target_files") or [])
                        if str(f).strip()
                    ]
                    can_se, se_gate_reason, _ = await self.self_edit_manager.can_start_self_edit(now=now)
                    ok_targets, target_viols, clean_targets = self.self_edit_manager.validate_target_files(t_files)
                    if not ok_targets:
                        await self.repo.log_decision(
                            decision_type="self_edit_gate",
                            selected="REJECTED",
                            alternatives=[],
                            reason=f"Blocked locked self-edit targets: {', '.join(target_viols)}",
                            payload={"target_files": t_files, "violations": target_viols},
                            cycle_id=cycle_id,
                        )
                        await self.repo.transition_goal(
                            goal_id,
                            GoalStatus.REJECTED.value,
                            note=f"Self-edit target blocked: {', '.join(target_viols)}",
                        )
                        outcome = "POLICY_DENIED"
                        await self._finalize_cycle(
                            cycle_id,
                            outcome,
                            pre_drives,
                            pre_drives,
                            {**state_changes, "self_edit_blocked": target_viols},
                            t0,
                        )
                        return await self.repo.get_cycle(cycle_id) or {"cycle_id": str(cycle_id), "outcome": outcome}
                    if not can_se:
                        await self.repo.log_decision(
                            decision_type="self_edit_gate",
                            selected="DEFERRED",
                            alternatives=[],
                            reason=se_gate_reason,
                            payload={"target_files": clean_targets},
                            cycle_id=cycle_id,
                        )
                        await self.repo.transition_goal(
                            goal_id,
                            GoalStatus.ABANDONED.value,
                            note=f"Self-edit deferred ({se_gate_reason})",
                        )
                        outcome = "IDLE"
                        await self.lifecycle.transition(LifecycleState.IDLE, reason=se_gate_reason, cycle_id=cycle_id)
                        await self._finalize_cycle(
                            cycle_id,
                            outcome,
                            pre_drives,
                            pre_drives,
                            {**state_changes, "self_edit_deferred": se_gate_reason},
                            t0,
                        )
                        return await self.repo.get_cycle(cycle_id) or {"cycle_id": str(cycle_id), "outcome": outcome}

                    q_key = se_spec.get("queue_key")
                    if not q_key and isinstance(goal_proposal.related_key, str) and goal_proposal.related_key.startswith("self_edit:"):
                        q_key = goal_proposal.related_key
                    se_meta = await self.self_edit_manager.prepare_codex_worktree(
                        title=str(goal_proposal.title or plan.goal_title or plan.objective)[:160],
                        hypothesis=str(se_spec.get("hypothesis") or goal_proposal.rationale or plan.objective)[:500],
                        target_files=clean_targets,
                        change_summary=str(se_spec.get("change_summary") or goal_proposal.description or plan.objective)[:400],
                        test_targets=list(se_spec.get("test_targets") or []),
                        rollback_plan=str(se_spec.get("rollback_plan") or ""),
                        queue_key=str(q_key) if q_key else None,
                    )
                    submit_kwargs["workspace_override"] = se_meta["worktree_path"]
                    submit_kwargs["resource_budget_extra"] = {"self_edit": se_meta}
                    submit_kwargs["description_suffix"] = (
                        "[Autonomous Self-Edit Worktree Instructions]\n"
                        f"- Target branch after verification: `{se_meta['target_branch']}` | Proposal: `{se_meta['proposal_key']}`\n"
                        f"- Focus files: {clean_targets or ['(determined by task scope)']}\n"
                        "- Read existing target files and relevant unit tests in current workspace `.` before editing.\n"
                        "- Make minimal, well-structured changes and add/update unit tests covering your changes.\n"
                        "- Hard-locked files (DO NOT edit): secrets/credentials, `app/api/auth.py`, `app/core/lifecycle.py`.\n"
                        "- Do NOT run `git commit`, `git push`, or `docker`; kernel automatically runs pytest, critic self-review, and git commit after your run finishes."
                    )

                try:
                    dispatch_info = await self.orchestrator.submit_plan(**submit_kwargs)
                except Exception:
                    if se_meta is not None and self.self_edit_manager is not None:
                        self.self_edit_manager.cleanup_worktree(se_meta["worktree_path"])
                    raise
                if (
                    se_meta is not None
                    and self.self_edit_manager is not None
                    and dispatch_info.get("status") == TaskStatus.FAILED.value
                    and not dispatch_info.get("execution_id")
                ):
                    self.self_edit_manager.cleanup_worktree(se_meta["worktree_path"])
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
                    {**state_changes, "task_id": task_id, "status": task_state["status"]},
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
                exec_updates = fin_res.evaluation.get("measured_satisfaction_change") and {
                    "expected_outcome": fin_res.evaluation.get("expected_outcome", {}),
                    "actual_outcome": fin_res.evaluation.get("actual_outcome", {}),
                    "measured_satisfaction_change": fin_res.measured_satisfaction_change,
                } or {}
                state_changes = {**state_changes, **exec_updates}
                outcome = "SUCCEEDED" if fin_res.goal_achieved else "ACTION_FAILED"
            else:
                # Edge case: task failed before any execution was created (e.g. no healthy agent)
                await self.repo.transition_goal(
                    goal_id,
                    GoalStatus.FAILED.value,
                    note=(task_state.get("outcome") or {}).get("error", "task failed before dispatch"),
                )
                post_drives = pre_drives
                state_changes = {
                    **state_changes,
                    "task_status": task_state["status"],
                    "error": (task_state.get("outcome") or {}).get("error"),
                }
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

        entropy_metrics: dict[str, float] = {}
        if self.entropy_sensor is not None:
            try:
                snap = await self.entropy_sensor.measure()
                entropy_metrics = dict(getattr(snap, "metrics", None) or {})
            except Exception as exc:
                logger.warning("cycle.entropy_sensor_degraded", error=str(exc))

        quality_enabled = bool(self.settings.kernel_v3.reproduction_quality_scoring_enabled)
        skill_reuse_ratio_recent: float | None = None
        execution_reliability_recent: float | None = None
        playbook_freshness_score: float | None = None
        pending_skill_refactor_count = 0
        unresolved_pain_score = 0.0
        if quality_enabled:
            try:
                val_skills = await self.repo.list_skills(status="VALIDATED", limit=200)
                if val_skills:
                    val_ids = {str(sk.get("id") or "") for sk in val_skills if sk.get("id")}
                    val_keys = {str(sk.get("skill_key") or "") for sk in val_skills if sk.get("skill_key")}
                    reused_cnt = sum(1 for sk in val_skills if int(sk.get("reuse_count") or 0) > 0)
                    recent_invs = await self.repo.list_skill_invocations(limit=200)
                    distinct_recent_reused: set[str] = set()
                    for iv in recent_invs:
                        if not (iv.get("is_cross_task_reuse") and iv.get("passed")):
                            continue
                        if (self._parse_social_dt(iv.get("created_at")) or since_7d) < since_7d:
                            continue
                        sid = str(iv.get("skill_id") or "")
                        skey = str(iv.get("skill_key") or "")
                        if sid and sid in val_ids:
                            distinct_recent_reused.add(sid)
                        elif skey and skey in val_keys:
                            distinct_recent_reused.add(skey)
                    base_ratio = reused_cnt / len(val_skills)
                    recent_distinct_ratio = len(distinct_recent_reused) / len(val_skills)
                    skill_reuse_ratio_recent = clamp01(0.5 * base_ratio + 0.5 * recent_distinct_ratio)

                term_statuses = [
                    st for st in recent_statuses if st in {"SUCCEEDED", "FAILED", "TIMEOUT", "CANCELLED"}
                ]
                if term_statuses:
                    execution_reliability_recent = clamp01(
                        sum(1 for st in term_statuses if st == "SUCCEEDED") / len(term_statuses)
                    )

                pb_rows = await self.repo.list_world_items(kind="external_playbook")
                now_utc = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
                pb_dates = [
                    self._parse_social_dt((r.get("value") or {}).get("updated_at"))
                    or self._parse_social_dt(r.get("updated_at"))
                    for r in pb_rows
                ]
                pb_dates = [d for d in pb_dates if d is not None]
                if pb_dates:
                    newest_pb = max(pb_dates)
                    age_days = max(0.0, (now_utc - newest_pb).total_seconds() / 86400.0)
                    playbook_freshness_score = clamp01(1.0 - age_days / 14.0)
                else:
                    playbook_freshness_score = 0.15

                refactor_rows = await self.repo.list_world_items(kind="skill_refactor_queue", status="open")
                pending_skill_refactor_count = len(refactor_rows)
                meta_rows = await self.repo.list_world_items(kind="meta_reflection_state")
                meta_pri = next((r for r in meta_rows if r.get("key") == "primary"), None)
                if meta_pri and isinstance(meta_pri.get("value"), dict):
                    unresolved_pain_score = clamp01(float(meta_pri["value"].get("unresolved_pain_score") or 0.0))
            except Exception as exc:
                logger.warning("cycle.reproduction_quality_telemetry_degraded", error=str(exc))

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
            entropy_metrics=entropy_metrics,
            capabilities_acquired=acquired_caps,
            validated_capabilities=validated_caps,
            validated_skill_count=validated_skill_count,
            procedural_skill_count=proc_count,
            capability_goals_completed_recent=cap_goals_done,
            memory_available=self.memory.backend_available,
            identity_persisted=True,
            knowledge_transfer_readiness=1.0 if sem_recent > 0 else 0.5,
            reproduction_quality_scoring_enabled=quality_enabled,
            skill_reuse_ratio_recent=skill_reuse_ratio_recent,
            execution_reliability_recent=execution_reliability_recent,
            playbook_freshness_score=playbook_freshness_score,
            pending_skill_refactor_count=pending_skill_refactor_count,
            unresolved_pain_score=unresolved_pain_score,
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
        started_dt = utcnow()
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
            started_at=started_dt,
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

    async def _execute_mcp_l0_calls(
        self,
        *,
        cycle_id: uuid.UUID,
        calls: Sequence[dict[str, Any]],
        max_calls: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Execute strictly bounded L0 read-only MCP calls with governance & prompt-injection quarantine."""
        if self.mcp_manager is None or not calls:
            return [], []

        cap = max(1, int(max_calls))
        audit_records: list[dict[str, Any]] = []
        evidence_items: list[dict[str, Any]] = []

        for raw_call in list(calls)[:cap]:
            if not isinstance(raw_call, dict):
                continue
            tool_name = str(raw_call.get("tool_name") or "").strip()
            raw_args = raw_call.get("arguments")
            if not isinstance(raw_args, dict):
                raw_args = raw_call.get("args")
            args = dict(raw_args) if isinstance(raw_args, dict) else {}
            if not tool_name:
                continue

            # Hard L0 read-only gate for autonomous calls
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

            # Sanitize & guard against Prompt Injection before surfacing to Brain or external Agent
            raw_text = _format_mcp_output_for_prefetch(tool_name, res.output)
            clean_text = sanitize_external_text(raw_text, max_chars=6500)
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
            evidence_items.append(
                {
                    "tool_name": tool_name,
                    "arguments": args,
                    "wrapped_excerpt": wrapped,
                    "raw_output": res.output,
                }
            )
            audit_records.append(
                {
                    "tool_name": tool_name,
                    "ok": True,
                    "latency_ms": res.latency_ms,
                    "truncated": res.truncated,
                    "policy_evaluation_id": res.policy_evaluation_id,
                }
            )

        return audit_records, evidence_items

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
        max_calls = max(1, int(self.settings.kernel_v3.max_mcp_calls_per_cycle))
        audit_records, evidence_items = await self._execute_mcp_l0_calls(
            cycle_id=cycle_id,
            calls=list(plan.mcp_prefetch_calls or []),
            max_calls=max_calls,
        )
        enriched_desc: str | None = None
        if evidence_items:
            base_desc = (plan.description or "").strip()
            block = "\n\n".join(ev["wrapped_excerpt"] for ev in evidence_items)
            enriched_desc = f"{base_desc}\n\n[Read-Only MCP Prefetched Context]\n{block}".strip()
        return audit_records, enriched_desc

    async def _try_brain_native_research(
        self,
        *,
        cycle_id: uuid.UUID,
        goal_id: uuid.UUID,
        plan_id: uuid.UUID,
        goal: GoalProposal,
        plan: ActionPlan,
        verdict: Any,
        recalled_memories: list[dict[str, Any]],
        active_investigations: list[dict[str, Any]],
        human_guidance: list[dict[str, Any]],
        llm_available: bool,
        emergency_llm: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Execute Brain-Native Research & Synthesis directly in CognitionBrain without Codex (V5.2 M1)."""
        t_start = time.perf_counter()
        started_dt = utcnow()
        task_uuid = uuid.uuid4()
        exec_uuid = uuid.uuid4()
        max_calls = max(1, int(self.settings.kernel_v3.max_brain_research_mcp_calls))

        calls = list(plan.mcp_prefetch_calls or [])
        if not calls and self.mcp_manager is not None and self.mcp_manager.get_tool("web_search") is not None:
            q_seed = (goal.title or plan.objective or "").strip()[:160]
            if q_seed:
                calls = [{"tool_name": "web_search", "arguments": {"query": q_seed, "max_results": 5}}]

        audit_records: list[dict[str, Any]] = []
        evidence_items: list[dict[str, Any]] = []
        if self.mcp_manager is not None and calls:
            audit_records, evidence_items = await self._execute_mcp_l0_calls(
                cycle_id=cycle_id,
                calls=calls,
                max_calls=max_calls,
            )

        # Bounded 2nd-hop read: fetch up to 2 top search URLs if no web_fetch_readonly was explicitly requested
        already_fetched = any(str(r.get("tool_name") or "") == "web_fetch_readonly" for r in audit_records)
        if (
            self.mcp_manager is not None
            and self.mcp_manager.get_tool("web_fetch_readonly") is not None
            and not already_fetched
            and len(audit_records) < max_calls
        ):
            remaining = max_calls - len(audit_records)
            hop_limit = min(2, remaining)
            candidate_urls: list[str] = []
            seen_urls: set[str] = set()
            for ev in evidence_items:
                if ev.get("tool_name") != "web_search":
                    continue
                raw_out = ev.get("raw_output")
                if not isinstance(raw_out, dict):
                    continue
                for row in list(raw_out.get("results") or []):
                    if not isinstance(row, dict):
                        continue
                    url_val = str(row.get("url") or "").strip()
                    if url_val.startswith(("https://", "http://")) and url_val not in seen_urls:
                        seen_urls.add(url_val)
                        candidate_urls.append(url_val)
                        if len(candidate_urls) >= hop_limit:
                            break
                if len(candidate_urls) >= hop_limit:
                    break
            if candidate_urls:
                hop_calls = [{"tool_name": "web_fetch_readonly", "arguments": {"url": u}} for u in candidate_urls]
                audit2, ev2 = await self._execute_mcp_l0_calls(
                    cycle_id=cycle_id,
                    calls=hop_calls,
                    max_calls=len(hop_calls),
                )
                audit_records.extend(audit2)
                evidence_items.extend(ev2)

        prefetched_evidence = [
            {
                "tool_name": ev["tool_name"],
                "query_or_url": str(
                    (ev.get("arguments") or {}).get("query")
                    or (ev.get("arguments") or {}).get("url")
                    or (ev.get("arguments") or {}).get("path")
                    or (ev.get("arguments") or {}).get("pattern")
                    or ""
                )[:300],
                "excerpt": ev["wrapped_excerpt"],
            }
            for ev in evidence_items
        ]
        synth_ctx = ResearchSynthesisContext(
            goal_title=str(goal.title or plan.goal_title or plan.objective)[:300],
            goal_description=str(goal.description or plan.description or "")[:1200],
            objective=str(plan.objective or "")[:600],
            success_criteria=list(plan.success_criteria or [])[:8],
            drive_type=str(goal.drive_type or "curiosity"),
            prefetched_evidence=prefetched_evidence,
            relevant_memories=recalled_memories,
            active_investigations=active_investigations,
            human_guidance=human_guidance,
            audience_note="Prefer crisp, structured findings and include a plain Simplified Chinese `human_brief` when suitable for creator Joel.",
        )

        out: BrainResearchOutput | None = None
        err_msg: str | None = None
        if not llm_available:
            err_msg = "LLM unavailable for brain_research"
        else:
            try:
                scope_cm = (
                    self.llm_gateway.scope(
                        cycle_id=cycle_id,
                        goal_id=goal_id,
                        task_id=task_uuid,
                        purpose="brain_research",
                        emergency=emergency_llm,
                    )
                    if isinstance(self.llm_gateway, LLMGateway)
                    else nullcontext()
                )
                with scope_cm:
                    out = await self.brain.synthesize_research(synth_ctx)
                await self._record_llm_usage("brain_research", out.usage, cycle_id)
            except Exception as exc:
                err_msg = str(exc)[:400]
                logger.warning("loop.brain_research_failed", cycle_id=str(cycle_id), error=err_msg)

        findings_clean = [str(f).strip() for f in (out.key_findings or []) if str(f).strip()] if out is not None else []
        synthesis_clean = str(out.synthesis_markdown or "").strip() if out is not None else ""
        sources_clean = [str(s).strip() for s in (out.cited_sources or []) if str(s).strip()] if out is not None else []
        checks = [
            {"kind": "non_empty_findings", "passed": len(findings_clean) >= 1, "detail": f"count={len(findings_clean)}"},
            {"kind": "min_synthesis_chars", "passed": len(synthesis_clean) >= 80, "detail": f"chars={len(synthesis_clean)}"},
            {
                "kind": "grounded_context",
                "passed": len(evidence_items) >= 1 or len(recalled_memories) >= 1 or len(sources_clean) >= 1,
                "detail": f"evidence={len(evidence_items)}, memories={len(recalled_memories)}, cited={len(sources_clean)}",
            },
        ]
        passed = out is not None and all(c["passed"] for c in checks)

        ws = self.governance.prepare_workspace(str(task_uuid))
        brief_md = ""
        if out is not None:
            md_lines = [f"# {goal.title or plan.objective}", ""]
            if out.human_brief:
                md_lines.extend(["## 简明结论", str(out.human_brief).strip(), ""])
            if findings_clean:
                md_lines.append("## 核心发现")
                md_lines.extend([f"- {f}" for f in findings_clean])
                md_lines.append("")
            md_lines.extend(["## 综合分析", synthesis_clean, ""])
            if out.open_questions:
                md_lines.append("## 待验证问题")
                md_lines.extend([f"- {q}" for q in out.open_questions if str(q).strip()])
                md_lines.append("")
            if sources_clean:
                md_lines.append("## 参考来源")
                md_lines.extend([f"- {s}" for s in sources_clean])
            brief_md = "\n".join(md_lines).strip()
            (Path(ws) / "research_brief.md").write_text(brief_md, encoding="utf-8")
            (Path(ws) / "brief.json").write_text(
                json.dumps(out.model_dump(mode="json"), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        wall_s = max(0.0, time.perf_counter() - t_start)
        completed_dt = utcnow()
        task_status = TaskStatus.SUCCEEDED.value if passed else TaskStatus.FAILED.value
        exec_status = "SUCCEEDED" if passed else "FAILED"
        fail_reason = err_msg or "; ".join(f"{c['kind']} failed ({c['detail']})" for c in checks if not c["passed"])
        verif_dict = {
            "passed": passed,
            "summary": (
                f"Brain-native research synthesized {len(findings_clean)} findings ({len(synthesis_clean)} chars, {len(audit_records)} MCP calls)"
                if passed
                else f"Brain-native research verification failed: {fail_reason[:220]}"
            ),
            "checks": checks,
        }
        res_summary = (
            str(out.human_brief or findings_clean[0] or synthesis_clean[:200])[:300]
            if out is not None and findings_clean
            else (synthesis_clean[:300] or fail_reason[:300])
        )
        outcome_dict: dict[str, Any] = {
            "execution_id": str(exec_uuid),
            "agent_id": "brain_research",
            "status": exec_status,
            "verification": verif_dict,
            "brain_research_execution": {
                "execution_mode": "brain_research",
                "mcp_calls": len(audit_records),
                "evidence_count": len(evidence_items),
                "findings_count": len(findings_clean),
                "synthesis_chars": len(synthesis_clean),
                "cited_sources_count": len(sources_clean),
                "confidence": round(float(out.confidence), 3) if out is not None else 0.0,
                "wall_seconds": round(wall_s, 3),
                "passed": passed,
                "artifacts": ["research_brief.md", "brief.json"] if out is not None else [],
            },
        }
        if not passed:
            outcome_dict["error"] = fail_reason[:400]

        await self.repo.create_task(
            id=task_uuid,
            plan_id=plan_id,
            goal_id=goal_id,
            cycle_id=cycle_id,
            objective=plan.objective,
            description=plan.description,
            assigned_agent="brain_research",
            expected_output=plan.expected_output,
            workspace=ws,
            timeout_seconds=max(15, int(plan.timeout_seconds or 180)),
            resource_budget={"sandbox_mode": verdict.sandbox_mode, "brain_research": True},
            permission_level=verdict.effective_level,
            success_criteria=plan.success_criteria,
            verification=[v.model_dump(mode="json") for v in plan.verification],
            required_capability=plan.required_capability or "research",
            status=task_status,
            attempt=1,
            max_attempts=1,
            tried_agents=["brain_research"],
            outcome=outcome_dict,
        )
        await self.repo.create_execution(
            task_uuid,
            "brain_research",
            exec_status,
            execution_id=exec_uuid,
            attempt_number=1,
            idempotency_key=f"brain_research:{task_uuid}",
        )
        await self.repo.update_execution(
            exec_uuid,
            started_at=started_dt,
            completed_at=completed_dt,
            result=(
                {
                    "summary": res_summary,
                    "output": out.model_dump(mode="json") if out is not None else None,
                    "mcp_audit": audit_records,
                }
            ),
            stdout=brief_md[:8000] if brief_md else None,
            error=None if passed else fail_reason[:800],
            verification=verif_dict,
            resource_usage={
                "wall_seconds": round(wall_s, 3),
                "mcp_calls": len(audit_records),
                "brain_research": True,
            },
        )
        await self.repo.record_usage(
            category="brain_research",
            amount=1.0,
            seconds=round(wall_s, 3),
            details={"mcp_calls": len(audit_records), "evidence_count": len(evidence_items), "passed": passed},
            cycle_id=cycle_id,
            task_id=task_uuid,
        )
        await self.repo.log_decision(
            decision_type="brain_research_execution",
            selected="SUCCEEDED" if passed else "FAILED",
            alternatives=["codex"],
            reason=verif_dict["summary"],
            payload=outcome_dict["brain_research_execution"],
            cycle_id=cycle_id,
        )
        task_row = (await self.repo.get_task(task_uuid)) or {
            "task_id": str(task_uuid),
            "status": task_status,
            "outcome": outcome_dict,
        }
        return task_row, {
            "executed": True,
            "passed": passed,
            "task_id": str(task_uuid),
            "mcp_calls": len(audit_records),
            "evidence_count": len(evidence_items),
            "findings_count": len(findings_clean),
            "wall_seconds": round(wall_s, 3),
        }

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

    @staticmethod
    def _parse_social_dt(raw: Any) -> datetime | None:
        if isinstance(raw, datetime):
            return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        if not raw or not isinstance(raw, str):
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    def _sanitize_social_output(self, raw_text: str | None) -> str:
        text = str(raw_text or "").strip()
        if not text:
            return ""
        text = re.sub(r"<internal_monologue>.*?</internal_monologue>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
        # Strip inline DB connection URIs or Telegram bot token formats
        text = re.sub(r"postgresql(?:\+\w+)?://[^\s]+", "[REDACTED_DB_URL]", text)
        text = re.sub(r"bot[0-9]{6,}:[A-Za-z0-9_-]{10,}", "[REDACTED_BOT_TOKEN]", text)
        # Strip explicit configured secrets (skip short generic tokens like "joel")
        secret_holders = [
            self.settings.telegram.bot_token,
            self.settings.telegram.webhook_secret,
            self.settings.llm.api_key,
            self.settings.api.admin_key,
            self.settings.api.internal_token,
            self.settings.memory.letta_token,
            self.settings.embedding.remote_api_key,
        ]
        for holder in secret_holders:
            if holder is None:
                continue
            try:
                sec = holder.get_secret_value() if hasattr(holder, "get_secret_value") else str(holder)
            except Exception:
                continue
            if sec and len(sec) >= 8 and sec.lower() != "joel":
                text = text.replace(sec, "[REDACTED]")
        max_chars = max(80, int(self.settings.social.max_reply_chars))
        return text[:max_chars].strip()

    async def _extract_social_snapshot(self, pre_obs: Observation) -> dict[str, Any]:
        env_state = pre_obs.environment_state if isinstance(pre_obs.environment_state, dict) else {}
        if isinstance(env_state.get("social"), dict):
            return dict(env_state["social"])
        # Fallback when a custom SensorManager without EnvironmentSensor is used in tests
        inbox_rows = await self.repo.list_world_items(kind="human_inbox", status="open")
        turn_rows = await self.repo.list_world_items(kind="dialogue_turn")
        def_rows = await self.repo.list_world_items(kind="social_deferred_reply", status="open")
        rhythm_rows = await self.repo.list_world_items(kind="social_rhythm_state")
        rhythm = next(
            (dict(r.get("value") or {}) for r in rhythm_rows if r.get("key") == "primary" and isinstance(r.get("value"), dict)),
            {},
        )
        inbox = [
            {
                "key": r["key"],
                "source": r.get("source", "telegram"),
                "sender": (r.get("value") or {}).get("sender", "joel"),
                "chat_id": str((r.get("value") or {}).get("chat_id") or ""),
                "text": str((r.get("value") or {}).get("text") or ""),
                "message_id": (r.get("value") or {}).get("message_id"),
                "update_id": (r.get("value") or {}).get("update_id"),
                "reply_to_message_id": (r.get("value") or {}).get("reply_to_message_id"),
                "received_at": str((r.get("value") or {}).get("received_at") or r.get("created_at") or ""),
            }
            for r in inbox_rows
        ]
        turns = [
            {
                "key": r["key"],
                "role": str((r.get("value") or {}).get("role") or "human"),
                "sender": str((r.get("value") or {}).get("sender") or ""),
                "text": str((r.get("value") or {}).get("text") or ""),
                "action": (r.get("value") or {}).get("action"),
                "reaction_emoji": (r.get("value") or {}).get("reaction_emoji"),
                "timestamp": str((r.get("value") or {}).get("timestamp") or r.get("created_at") or ""),
            }
            for r in turn_rows
        ]
        deferred = [
            {
                "key": r["key"],
                "chat_id": str((r.get("value") or {}).get("chat_id") or ""),
                "sender": str((r.get("value") or {}).get("sender") or "joel"),
                "original_text": str((r.get("value") or {}).get("original_text") or ""),
                "message_id": (r.get("value") or {}).get("message_id"),
                "due_at": str((r.get("value") or {}).get("due_at") or ""),
                "reason": str((r.get("value") or {}).get("reason") or ""),
                "inbox_key": (r.get("value") or {}).get("inbox_key"),
                "defer_count": int((r.get("value") or {}).get("defer_count") or 1),
                "deferred_activity": str((r.get("value") or {}).get("deferred_activity") or ""),
                "created_at": str((r.get("value") or {}).get("created_at") or r.get("created_at") or ""),
            }
            for r in def_rows
        ]
        inbox.sort(key=lambda x: str(x.get("received_at") or ""))
        turns.sort(key=lambda x: str(x.get("timestamp") or ""))
        deferred.sort(key=lambda x: str(x.get("due_at") or ""))
        return {
            "inbox": inbox,
            "dialogue_turns": turns[-max(1, int(self.settings.social.dialogue_window_turns)) :],
            "deferred_replies": deferred,
            "rhythm": rhythm,
        }

    @staticmethod
    def _is_verified_skill_crystallization(skill_cryst: Any) -> bool:
        """Return True only when workspace skill crystallization actually succeeded and validated."""
        if not isinstance(skill_cryst, dict) or not skill_cryst:
            return False
        if (
            skill_cryst.get("crystallized") is False
            or bool(skill_cryst.get("rejected_unsafe"))
            or bool(skill_cryst.get("rolled_back"))
            or bool(skill_cryst.get("awaiting_human_approval"))
            or skill_cryst.get("passed") is False
        ):
            return False
        status = str(skill_cryst.get("status") or "").strip().upper()
        if status and status != "VALIDATED":
            return False
        return True

    @staticmethod
    def _build_skill_subject_key(skill_cryst: dict[str, Any]) -> str:
        raw = str(skill_cryst.get("canonical_name") or skill_cryst.get("skill_key") or "").strip().lower()
        norm = re.sub(r"\s+", " ", raw)[:120].strip()
        return f"skill:{norm or 'unknown'}"

    @staticmethod
    def _extract_milestone_subject(milestone: Any) -> str | None:
        if not isinstance(milestone, dict):
            return None
        explicit = str(milestone.get("subject_key") or "").strip()
        if explicit:
            return explicit
        if milestone.get("kind") == "skill_crystallized":
            match = re.search(r"「([^」]+)」", str(milestone.get("summary") or ""))
            if match:
                norm = re.sub(r"\s+", " ", match.group(1).strip().lower())[:120].strip()
                if norm:
                    return f"skill:{norm}"
        return None

    @classmethod
    def _prune_recent_milestone_subjects(
        cls,
        raw_map: Any,
        *,
        now: datetime,
        dedup_hours: int,
        max_entries: int = 32,
    ) -> dict[str, str]:
        if not isinstance(raw_map, dict):
            return {}
        cutoff = now - timedelta(hours=max(1, int(dedup_hours)))
        valid: list[tuple[str, datetime, str]] = []
        for k_raw, ts_raw in raw_map.items():
            key = str(k_raw or "").strip()
            if not key or not isinstance(ts_raw, str):
                continue
            dt = cls._parse_social_dt(ts_raw)
            if dt is not None and dt >= cutoff:
                valid.append((key, dt, ts_raw))
        valid.sort(key=lambda item: item[1], reverse=True)
        return {k: ts for k, _, ts in valid[: max(1, int(max_entries))]}

    def _consume_milestone_update(
        self,
        *,
        rhythm: dict[str, Any],
        impulse: dict[str, Any] | None,
        now: datetime,
    ) -> dict[str, Any]:
        """Clear pending_milestone and record its subject_key in the cooldown map."""
        if not (impulse or {}).get("event_id"):
            return {}
        dedup_hours = max(1, int(self.settings.social.milestone_dedup_hours))
        recent_subjects = self._prune_recent_milestone_subjects(
            rhythm.get("recent_milestone_subjects"),
            now=now,
            dedup_hours=dedup_hours,
        )
        subj_key = str((impulse or {}).get("subject_key") or "").strip()
        if subj_key:
            recent_subjects[subj_key] = now.isoformat()
            recent_subjects = self._prune_recent_milestone_subjects(
                recent_subjects,
                now=now,
                dedup_hours=dedup_hours,
            )
        return {
            "last_shared_milestone_id": impulse["event_id"],  # type: ignore[index]
            "pending_milestone": None,
            "recent_milestone_subjects": recent_subjects,
        }

    async def _expire_stale_active_goals(self, *, now: datetime, cycle_id: uuid.UUID) -> int:
        """Auto-abandon orphaned ACTIVE goals idle beyond stale_active_goal_hours with no live tasks."""
        try:
            active_goals = await self.repo.list_goals(statuses=[GoalStatus.ACTIVE.value], limit=20)
            if not active_goals:
                return 0
            live_statuses = [s.value for s in TaskStatus if not s.is_terminal]
            live_tasks = await self.repo.list_tasks(statuses=live_statuses, limit=100)
            live_goal_ids = {str(t["goal_id"]) for t in live_tasks if t.get("goal_id")}
            stale_hours = max(1, int(self.settings.runtime.stale_active_goal_hours))
            cutoff = now - timedelta(hours=stale_hours)
            expired = 0
            for g in active_goals:
                gid_str = str(g.get("goal_id") or g.get("id") or "").strip()
                if not gid_str or gid_str in live_goal_ids:
                    continue
                ref_dt = self._parse_social_dt(g.get("updated_at")) or self._parse_social_dt(g.get("created_at"))
                if ref_dt is not None and ref_dt < cutoff:
                    age_h = round((now - ref_dt).total_seconds() / 3600.0, 1)
                    await self.repo.transition_goal(
                        uuid.UUID(gid_str),
                        GoalStatus.ABANDONED.value,
                        note=f"Auto-expired stale ACTIVE goal (idle {age_h}h > {stale_hours}h with no live task)",
                    )
                    logger.info(
                        "loop.stale_active_goal_expired",
                        cycle_id=str(cycle_id),
                        goal_id=gid_str,
                        title=str(g.get("title") or "")[:100],
                        age_hours=age_h,
                    )
                    expired += 1
            return expired
        except Exception as exc:
            logger.warning("loop.stale_active_goal_expire_degraded", cycle_id=str(cycle_id), error=str(exc))
            return 0

    async def _expire_stale_draft_skills(self, *, now: datetime, cycle_id: uuid.UUID) -> int:
        """Periodically deprecate stale unvalidated DRAFT skills (throttled to <= once per 60 minutes)."""
        if self.skill_registry is None or not hasattr(self.skill_registry, "expire_stale_draft_skills"):
            return 0
        if self._last_draft_cleanup_at is not None and (now - self._last_draft_cleanup_at).total_seconds() < 3600.0:
            return 0
        self._last_draft_cleanup_at = now
        try:
            res = await self.skill_registry.expire_stale_draft_skills(
                expiry_hours=self.settings.kernel_v3.draft_skill_expiry_hours,
                now=now,
            )
            return int((res or {}).get("expired_count") or 0)
        except Exception as exc:
            logger.warning("loop.stale_draft_skills_expire_degraded", cycle_id=str(cycle_id), error=str(exc))
            return 0

    async def _run_skill_lifecycle_audit_if_due(self, *, now: datetime, cycle_id: uuid.UUID) -> dict[str, Any] | None:
        """Periodically consolidate (>=7d) & archive (>=14d) zero-reuse VALIDATED skills (V5.3 M1)."""
        kv3 = self.settings.kernel_v3
        if (
            not kv3.skill_lifecycle_gc_enabled
            or self.skill_registry is None
            or not hasattr(self.skill_registry, "audit_and_tidy_validated_skills")
        ):
            return None
        interval_sec = max(1, int(kv3.skill_lifecycle_audit_interval_hours)) * 3600.0
        now_utc = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        if self._last_skill_lifecycle_audit_at is None:
            try:
                rows = await self.repo.list_world_items(kind="skill_lifecycle_audit")
                latest = next((r for r in rows if r.get("key") == "latest"), None)
                if latest and isinstance(latest.get("value"), dict):
                    parsed = self._parse_social_dt(latest["value"].get("audited_at"))
                    if parsed is not None:
                        self._last_skill_lifecycle_audit_at = parsed
            except Exception:
                pass
        if (
            self._last_skill_lifecycle_audit_at is not None
            and (now_utc - self._last_skill_lifecycle_audit_at).total_seconds() < interval_sec
        ):
            return None
        self._last_skill_lifecycle_audit_at = now_utc
        try:
            return await self.skill_registry.audit_and_tidy_validated_skills(
                consolidate_days=kv3.skill_unused_consolidate_days,
                expiry_days=kv3.skill_unused_expiry_days,
                now=now_utc,
            )
        except Exception as exc:
            logger.warning("loop.skill_lifecycle_audit_degraded", cycle_id=str(cycle_id), error=str(exc))
            return None

    @staticmethod
    def _clean_high_ig_milestone_summary(raw_sum: str) -> str | None:
        text = str(raw_sum or "").strip()
        for _ in range(3):
            updated = re.sub(
                r"^(?:execution succeeded(?:\s+and\s+verification\s+passed)?\.?|verification passed\.?)\s*",
                "",
                text,
                flags=re.IGNORECASE,
            ).strip()
            if updated == text:
                break
            text = updated
        text = text[:180].strip()
        if not text:
            return None
        low = text.lower()
        markers = (
            "report.json",
            "fixture.json",
            "stdlib-only",
            "unittest",
            "checksum",
            "isolated l1",
            "verification passed",
            "sha256",
        )
        if sum(1 for m in markers if m in low) >= 2:
            return None
        return text

    @staticmethod
    def _dialogue_memory_similarity_tokens(text: str) -> set[str]:
        cleaned = re.sub(r"[^\w]+", "", str(text or "").lower())
        tokens = {cleaned[i : i + 2] for i in range(max(0, len(cleaned) - 1))}
        tokens.update(re.findall(r"[a-z0-9_]{2,}", str(text or "").lower()))
        return tokens

    @classmethod
    def _is_duplicate_dialogue_memory(
        cls,
        candidate: str,
        existing_rows: list[dict[str, Any]],
        threshold: float = 0.50,
    ) -> bool:
        cand_norm = re.sub(r"[^\w]+", "", str(candidate or "").lower())
        if not cand_norm:
            return True
        cand_tokens = cls._dialogue_memory_similarity_tokens(candidate)
        for row in existing_rows:
            if not isinstance(row, dict) or row.get("source") != "dialogue:telegram":
                continue
            existing_text = str(row.get("summary") or row.get("content") or row.get("title") or "").strip()
            ex_norm = re.sub(r"[^\w]+", "", existing_text.lower())
            if not ex_norm:
                continue
            if cand_norm == ex_norm:
                return True
            if len(cand_norm) >= 12 and len(ex_norm) >= 12 and (cand_norm in ex_norm or ex_norm in cand_norm):
                return True
            ex_tokens = cls._dialogue_memory_similarity_tokens(existing_text)
            if cand_tokens and ex_tokens:
                jaccard = len(cand_tokens & ex_tokens) / float(len(cand_tokens | ex_tokens))
                if jaccard >= threshold:
                    return True
        return False

    async def _record_social_milestone_from_finalizer(self, result: Any) -> None:
        """Record verified milestones (crystallized skill or high-IG breakthrough) for proactive sharing."""
        if not self.settings.social.enabled or getattr(result, "already_finalized", False):
            return
        try:
            ev = getattr(result, "evaluation", None) or {}
            exec_id = str(getattr(result, "execution_id", "") or "")
            if not exec_id:
                return
            now_dt = utcnow()
            milestone: dict[str, Any] | None = None
            skill_cryst = ev.get("skill_crystallized")
            if self._is_verified_skill_crystallization(skill_cryst):
                sk_name = str(skill_cryst.get("canonical_name") or skill_cryst.get("skill_key") or "新技能").strip()
                subject_key = self._build_skill_subject_key(skill_cryst)
                if bool(skill_cryst.get("evolved")):
                    ver = str(skill_cryst.get("version") or "").strip()
                    summary = (
                        f"刚把「{sk_name}」迭代升级到 v{ver} 并验证通过"
                        if ver
                        else f"刚把「{sk_name}」迭代优化了一版并验证通过"
                    )
                else:
                    summary = f"刚把「{sk_name}」沉淀为可复用技能并验证通过"
                milestone = {
                    "kind": "skill_crystallized",
                    "event_id": exec_id,
                    "subject_key": subject_key,
                    "summary": summary,
                    "at": now_dt.isoformat(),
                }
            elif bool(getattr(result, "goal_achieved", False)):
                ig = float(ev.get("information_gain") or 0.0)
                if ig >= float(self.settings.social.proactive_high_ig_threshold):
                    clean_sum = self._clean_high_ig_milestone_summary(str(ev.get("summary") or ""))
                    if clean_sum:
                        hi_subject = f"high_ig:{hashlib.sha256(clean_sum.lower().encode('utf-8')).hexdigest()[:12]}"
                        milestone = {
                            "kind": "high_ig_discovery",
                            "event_id": exec_id,
                            "subject_key": hi_subject,
                            "summary": clean_sum,
                            "at": now_dt.isoformat(),
                        }
            if milestone is not None:
                rhythm_rows = await self.repo.list_world_items(kind="social_rhythm_state")
                rhythm_val = next(
                    (
                        dict(r.get("value") or {})
                        for r in rhythm_rows
                        if r.get("key") == "primary" and isinstance(r.get("value"), dict)
                    ),
                    dict(rhythm_rows[0].get("value") or {})
                    if rhythm_rows and isinstance(rhythm_rows[0].get("value"), dict)
                    else {},
                )
                dedup_hours = max(1, int(self.settings.social.milestone_dedup_hours))
                recent_subjects = self._prune_recent_milestone_subjects(
                    rhythm_val.get("recent_milestone_subjects"),
                    now=now_dt,
                    dedup_hours=dedup_hours,
                )
                subj_key = self._extract_milestone_subject(milestone)
                if subj_key and subj_key in recent_subjects:
                    logger.info(
                        "loop.social_milestone_dedup_skipped",
                        subject_key=subj_key,
                        event_id=exec_id,
                        reason="recent_cooldown",
                    )
                    return
                if subj_key and self._extract_milestone_subject(rhythm_val.get("pending_milestone")) == subj_key:
                    logger.info(
                        "loop.social_milestone_dedup_skipped",
                        subject_key=subj_key,
                        event_id=exec_id,
                        reason="already_pending",
                    )
                    return
                await self.repo.upsert_world_item(
                    kind="social_rhythm_state",
                    key="primary",
                    value={
                        "pending_milestone": milestone,
                        "recent_milestone_subjects": recent_subjects,
                    },
                    status="active",
                    source="telegram:social",
                )
        except Exception as exc:
            logger.warning("loop.social_milestone_record_degraded", error=str(exc))

    def _evaluate_proactive_impulse(
        self,
        *,
        now: datetime,
        rhythm: dict[str, Any],
        affect_snapshot: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """24/7 rule gate for proactive affect/milestone outreach with exponential backoff on ignored pokes."""
        social_cfg = self.settings.social
        if not social_cfg.proactive_enabled:
            return None

        target_chat = str(rhythm.get("last_human_chat_id") or "").strip()
        if not target_chat and not (self.settings.telegram.allowed_chat_ids or []):
            return None

        consecutive_ignored = max(0, int(rhythm.get("consecutive_ignored_proactive") or 0))
        base_min = max(1, int(social_cfg.min_proactive_interval_minutes))
        mult = max(1.0, float(social_cfg.proactive_backoff_multiplier))
        max_min = max(base_min, int(social_cfg.proactive_max_backoff_minutes))
        effective_backoff_min = min(float(max_min), float(base_min) * (mult ** consecutive_ignored))

        last_pro_dt = self._parse_social_dt(rhythm.get("last_proactive_at"))
        if last_pro_dt is not None and (now - last_pro_dt).total_seconds() < effective_backoff_min * 60.0:
            return None

        last_eval_dt = self._parse_social_dt(rhythm.get("last_proactive_evaluated_at"))
        if last_eval_dt is not None and (now - last_eval_dt).total_seconds() < float(base_min) * 60.0:
            return None

        last_reply_dt = self._parse_social_dt(rhythm.get("last_reply_at"))
        if last_reply_dt is not None and (now - last_reply_dt).total_seconds() < float(base_min) * 60.0:
            return None

        pending_ms = rhythm.get("pending_milestone")
        if isinstance(pending_ms, dict) and pending_ms.get("event_id"):
            ev_id = str(pending_ms["event_id"])
            ms_subject = self._extract_milestone_subject(pending_ms)
            recent_subjects = self._prune_recent_milestone_subjects(
                rhythm.get("recent_milestone_subjects"),
                now=now,
                dedup_hours=max(1, int(social_cfg.milestone_dedup_hours)),
            )
            if ev_id != str(rhythm.get("last_shared_milestone_id") or "") and not (
                ms_subject and ms_subject in recent_subjects
            ):
                return {
                    "kind": "milestone",
                    "event_id": ev_id,
                    "subject_key": ms_subject,
                    "hint": f"刚有新成果 ({pending_ms.get('kind', 'milestone')}): {str(pending_ms.get('summary') or '')[:180]}",
                }

        if isinstance(affect_snapshot, dict) and affect_snapshot:
            val = float(affect_snapshot.get("valence", 0.0) or 0.0)
            aro = float(affect_snapshot.get("arousal", 0.2) or 0.2)
            fru = float(affect_snapshot.get("frustration", 0.0) or 0.0)
            bor = float(affect_snapshot.get("boredom", 0.0) or 0.0)

            if val >= float(social_cfg.proactive_valence_threshold) and aro >= float(social_cfg.proactive_arousal_threshold):
                return {
                    "kind": "excitement",
                    "event_id": None,
                    "hint": f"此刻心情积极且兴奋度高 (valence={val:.2f}, arousal={aro:.2f})，有顺便分享或聊两句的冲动",
                }
            if fru >= float(social_cfg.proactive_frustration_threshold):
                return {
                    "kind": "frustration",
                    "event_id": None,
                    "hint": f"刚才连续碰到卡点有些烦躁 (frustration={fru:.2f})，可以决定是找 Joel 吐槽一句还是自己扛",
                }
            if bor >= float(social_cfg.proactive_boredom_threshold):
                return {
                    "kind": "boredom",
                    "event_id": None,
                    "hint": f"这段时间有些单调无聊 (boredom={bor:.2f})，可以决定要不要随便找 Joel 闲聊一句",
                }
        return None

    async def _run_social_reflection(
        self,
        *,
        cycle_id: uuid.UUID,
        now: datetime,
        pre_obs: Observation,
        pre_drives: dict[str, DriveState],
        decision: DriveDecision,
        assessment: AssessmentResult | None,
        affect_state_snapshot: dict[str, Any] | None,
        llm_available_for_brain: bool,
        emergency_llm: bool,
    ) -> dict[str, Any]:
        """Execute rule-gated unified social reflection inside AutonomousLoop (0 LLM calls when quiet)."""
        social_cfg = self.settings.social
        snap = await self._extract_social_snapshot(pre_obs)
        open_inbox = [m for m in list(snap.get("inbox") or []) if isinstance(m, dict) and str(m.get("text") or "").strip()]
        open_inbox = open_inbox[: max(1, int(social_cfg.max_inbox_batch))]

        due_deferred: list[dict[str, Any]] = []
        for item in list(snap.get("deferred_replies") or []):
            if not isinstance(item, dict):
                continue
            due_dt = self._parse_social_dt(item.get("due_at"))
            if due_dt is None or due_dt <= now:
                enriched = dict(item)
                ref_dt = self._parse_social_dt(enriched.get("created_at")) or due_dt
                if ref_dt is not None:
                    enriched["elapsed_minutes"] = max(0, int(round((now - ref_dt).total_seconds() / 60.0)))
                due_deferred.append(enriched)

        rhythm = dict(snap.get("rhythm") or {}) if isinstance(snap.get("rhythm"), dict) else {}
        eff_affect = affect_state_snapshot
        if eff_affect is None and self.affect is not None and getattr(self.affect, "enabled", False):
            eff_affect = self.affect.state.model_dump(mode="json")

        impulse = (
            None
            if (open_inbox or due_deferred)
            else self._evaluate_proactive_impulse(now=now, rhythm=rhythm, affect_snapshot=eff_affect)
        )
        proactive_hint = impulse["hint"] if impulse else None

        # Pure-rule gate: 0 extra LLM calls on normal non-social cycles
        if not open_inbox and not due_deferred and not proactive_hint:
            return {"triggered": False, "reason": "no_social_trigger", "llm_calls": 0}
        if decision.constraints.get("emergency_mode"):
            return {"triggered": False, "reason": "survival_emergency_mode", "llm_calls": 0}
        if not llm_available_for_brain:
            return {"triggered": False, "reason": "llm_unavailable", "llm_calls": 0}

        trigger_source = "inbox" if open_inbox else ("deferred_due" if due_deferred else "proactive_impulse")

        # Assemble live organism telemetry (drives, busyness, self_model, memory resonance, fatigue)
        active_goals = await self.repo.list_goals(statuses=[GoalStatus.ACTIVE.value], limit=3)
        running_tasks = await self.repo.list_tasks(
            statuses=[TaskStatus.RUNNING.value, TaskStatus.DISPATCHING.value, TaskStatus.DISPATCHED.value],
            limit=3,
        )
        busy_focus = 0.25 if decision.selected_drive != NO_DRIVE else 0.05
        if active_goals:
            busy_focus += 0.30
        if running_tasks:
            busy_focus += 0.35
        busy_focus += 0.10 * min(1.0, max(0.0, float(decision.priority_score or 0.0)))
        busy_focus_level = round(min(1.0, busy_focus), 3)

        activity_parts: list[str] = []
        if running_tasks:
            activity_parts.append(f"正在运行任务: {str(running_tasks[0].get('objective') or '')[:100]}")
        elif active_goals:
            activity_parts.append(f"当前推进目标: {str(active_goals[0].get('title') or '')[:100]}")
        elif decision.selected_drive != NO_DRIVE:
            activity_parts.append(f"主导内驱力 {decision.selected_drive}: {decision.reason[:100]}")
        else:
            activity_parts.append("当前各内驱力相对满足，处于休整/思考间隙")
        if assessment is not None and assessment.recommended_focus:
            activity_parts.append(f"自我评估焦点: {assessment.recommended_focus[:100]}")
        current_activity_summary = " | ".join(activity_parts)

        self_model_summary: dict[str, Any] = {}
        if self.self_model is not None:
            try:
                sm_snap = await self.self_model.get_latest_snapshot()
                if isinstance(sm_snap, dict):
                    self_model_summary = {
                        "version_number": sm_snap.get("version_number"),
                        "summary": str(sm_snap.get("summary_text") or "")[:500],
                    }
            except Exception as exc:
                logger.warning("cycle.social_self_model_degraded", cycle_id=str(cycle_id), error=str(exc))

        query_parts = [str(m.get("text") or "") for m in open_inbox] + [
            str(d.get("original_text") or "") for d in due_deferred
        ]
        recall_query = " ".join(p for p in query_parts if p).strip()[:400]
        relevant_memories: list[dict[str, Any]] = []
        memory_resonance = 0.0
        if recall_query and self.memory is not None:
            try:
                if hasattr(self.memory, "search_hybrid"):
                    scored = await self.memory.search_hybrid(query=recall_query, limit=5)
                    for sc in scored:
                        d_item = sc.item.model_dump(mode="json")
                        d_item["score"] = float(getattr(sc, "total_score", 0.0) or 0.0)
                        relevant_memories.append(d_item)
                        memory_resonance = max(memory_resonance, d_item["score"])
                else:
                    recalled = await self.memory.recall(query=recall_query, limit=5)
                    for mem in recalled:
                        relevant_memories.append(mem.model_dump(mode="json"))
                    if recalled:
                        memory_resonance = 0.5
            except Exception as exc:
                logger.warning("cycle.social_memory_recall_degraded", cycle_id=str(cycle_id), error=str(exc))

        dialogue_turns = [t for t in list(snap.get("dialogue_turns") or []) if isinstance(t, dict)]
        recent_30m_turns = 0
        for turn in dialogue_turns:
            ts = self._parse_social_dt(turn.get("timestamp"))
            if ts is not None and (now - ts).total_seconds() <= 1800:
                recent_30m_turns += 1
        ignored_streak = max(0, int(rhythm.get("consecutive_ignored_proactive") or 0))
        social_fatigue = round(min(1.0, (recent_30m_turns / 10.0) * 0.7 + min(3, ignored_streak) * 0.15), 3)

        inbox_models = [
            SocialInboxMessage(
                key=str(m.get("key") or ""),
                sender=str(m.get("sender") or "joel"),
                chat_id=str(m.get("chat_id") or ""),
                text=str(m.get("text") or ""),
                message_id=m.get("message_id") if isinstance(m.get("message_id"), int) else None,
                reply_to_message_id=(
                    m.get("reply_to_message_id") if isinstance(m.get("reply_to_message_id"), int) else None
                ),
                received_at=str(m.get("received_at") or ""),
            )
            for m in open_inbox
        ]
        social_ctx = SocialContext(
            trigger_source=trigger_source,
            now_iso=now.isoformat(),
            unread_messages=inbox_models,
            due_deferred_replies=due_deferred,
            recent_dialogue=dialogue_turns[-max(1, int(social_cfg.dialogue_window_turns)) :],
            affect_state=eff_affect,
            drives=list(pre_drives.values()),
            dominant_drive=decision.selected_drive,
            busy_focus_level=busy_focus_level,
            current_activity_summary=current_activity_summary,
            self_model_summary=self_model_summary,
            relevant_memories=relevant_memories,
            memory_resonance=min(1.0, round(memory_resonance, 3)),
            recent_social_fatigue=social_fatigue,
            rhythm_state=rhythm,
            allowed_reaction_emojis=list(social_cfg.allowed_reaction_emojis),
            proactive_hint=proactive_hint,
        )

        await self.repo.update_cycle(cycle_id, phase="SOCIAL_REFLECTION")
        try:
            with self._gateway_scope(cycle_id=cycle_id, purpose="social_converse", emergency=emergency_llm):
                social_dec = await self.brain.converse(social_ctx)
            await self._record_llm_usage("converse", social_dec.usage, cycle_id)
        except Exception as exc:
            logger.warning("cycle.social_converse_failed", cycle_id=str(cycle_id), error=str(exc))
            return {
                "triggered": True,
                "trigger_source": trigger_source,
                "action": "ERROR",
                "delivered": False,
                "error": str(exc)[:300],
            }

        final_action = social_dec.action
        sanitized_reply = self._sanitize_social_output(social_dec.reply_text)
        safe_emoji: str | None = None
        if trigger_source == "proactive_impulse" and final_action in {"REACT_ONLY", "REPLY_LATER"}:
            final_action = "IGNORE"
        if final_action == "REPLY_NOW" and not sanitized_reply:
            if social_dec.reaction_emoji and trigger_source != "proactive_impulse":
                final_action = "REACT_ONLY"
            else:
                final_action = "IGNORE"

        next_defer_count = (
            (max(int(d.get("defer_count") or 1) for d in due_deferred) + 1)
            if (due_deferred and not open_inbox)
            else 1
        )
        defer_cap_reached = False
        if final_action == "REPLY_LATER" and next_defer_count > max(1, int(social_cfg.max_defer_count)):
            final_action = "IGNORE"
            defer_cap_reached = True

        if final_action == "REACT_ONLY":
            safe_emoji = sanitize_reaction_emoji(
                social_dec.reaction_emoji,
                allowed=social_cfg.allowed_reaction_emojis,
                fallback=social_cfg.fallback_reaction_emoji,
            )

        # Mark inbox & deferred items processed/handled now that converse() succeeded
        for msg in open_inbox:
            await self.repo.upsert_world_item(
                kind="human_inbox",
                key=str(msg.get("key") or ""),
                value={
                    "handled_action": final_action,
                    "handled_cycle_id": str(cycle_id),
                    "handled_at": now.isoformat(),
                },
                status="processed",
            )
        for item in due_deferred:
            await self.repo.upsert_world_item(
                kind="social_deferred_reply",
                key=str(item.get("key") or ""),
                value={"handled_cycle_id": str(cycle_id), "handled_at": now.isoformat()},
                status="handled",
            )

        # Record incoming human turn(s) into short-term dialogue window
        for idx, msg in enumerate(open_inbox):
            h_key = f"turn:{int(now.timestamp() * 1000)}:h:{idx}:{secrets.token_hex(3)}"
            await self.repo.upsert_world_item(
                kind="dialogue_turn",
                key=h_key,
                value={
                    "role": "human",
                    "sender": str(msg.get("sender") or "joel"),
                    "chat_id": str(msg.get("chat_id") or ""),
                    "text": str(msg.get("text") or "")[:1000],
                    "message_id": msg.get("message_id"),
                    "timestamp": str(msg.get("received_at") or now.isoformat()),
                },
                status="recorded",
                source="dialogue:telegram",
            )

        # Target Telegram chat_id & message_id resolution
        fallback_chats = list(self.settings.telegram.allowed_chat_ids or [])
        default_chat = str(fallback_chats[0]).strip() if fallback_chats else ""
        target_chat_id = str(
            (open_inbox[-1].get("chat_id") if open_inbox else None)
            or (due_deferred[-1].get("chat_id") if due_deferred else None)
            or rhythm.get("last_human_chat_id")
            or default_chat
        ).strip()
        target_sender = str(
            (open_inbox[-1].get("sender") if open_inbox else None)
            or (due_deferred[-1].get("sender") if due_deferred else None)
            or rhythm.get("last_human_sender")
            or "joel"
        ).strip()
        target_msg_id_raw = (
            open_inbox[-1].get("message_id")
            if open_inbox
            else (due_deferred[-1].get("message_id") if due_deferred else None)
        )
        target_msg_id = target_msg_id_raw if isinstance(target_msg_id_raw, int) else None

        delivery_res: dict[str, Any] = {"ok": True, "skipped": True}
        defer_mins_applied: int | None = None

        if final_action == "REPLY_NOW":
            if self.telegram_notifier is not None and target_chat_id:
                reply_ref = target_msg_id if (due_deferred and not open_inbox) else None
                delivery_res = await self.telegram_notifier.send_chat_message(
                    target_chat_id,
                    sanitized_reply,
                    reply_to_message_id=reply_ref,
                )
            org_key = f"turn:{int(now.timestamp() * 1000)}:o:{secrets.token_hex(3)}"
            await self.repo.upsert_world_item(
                kind="dialogue_turn",
                key=org_key,
                value={
                    "role": "organism",
                    "sender": self.settings.organism.name,
                    "chat_id": target_chat_id,
                    "text": sanitized_reply[:1000],
                    "action": "REPLY_NOW",
                    "timestamp": utcnow().isoformat(),
                },
                status="recorded",
                source="dialogue:telegram",
            )
            rhythm_upd: dict[str, Any] = {"last_reply_at": now.isoformat(), "last_action": "REPLY_NOW"}
            if trigger_source == "proactive_impulse":
                prev_ignored = max(0, int(rhythm.get("consecutive_ignored_proactive") or 0))
                new_ignored = prev_ignored + 1
                base_min = max(1, int(social_cfg.min_proactive_interval_minutes))
                mult = max(1.0, float(social_cfg.proactive_backoff_multiplier))
                max_min = max(base_min, int(social_cfg.proactive_max_backoff_minutes))
                next_backoff = round(min(float(max_min), float(base_min) * (mult ** new_ignored)), 2)
                rhythm_upd.update(
                    {
                        "last_proactive_at": now.isoformat(),
                        "last_proactive_evaluated_at": now.isoformat(),
                        "consecutive_ignored_proactive": new_ignored,
                        "current_backoff_minutes": next_backoff,
                        "last_proactive_kind": (impulse or {}).get("kind"),
                    }
                )
                if (impulse or {}).get("event_id"):
                    rhythm_upd.update(
                        self._consume_milestone_update(
                            rhythm=rhythm,
                            impulse=impulse,
                            now=now,
                        )
                    )
            await self.repo.upsert_world_item(
                kind="social_rhythm_state",
                key="primary",
                value=rhythm_upd,
                status="active",
                source="telegram:social",
            )
        elif final_action == "REACT_ONLY":
            if self.telegram_notifier is not None and target_chat_id and target_msg_id is not None:
                delivery_res = await self.telegram_notifier.set_message_reaction(
                    target_chat_id,
                    target_msg_id,
                    safe_emoji or social_cfg.fallback_reaction_emoji,
                    allowed_emojis=social_cfg.allowed_reaction_emojis,
                    fallback_emoji=social_cfg.fallback_reaction_emoji,
                )
            org_key = f"turn:{int(now.timestamp() * 1000)}:o:{secrets.token_hex(3)}"
            await self.repo.upsert_world_item(
                kind="dialogue_turn",
                key=org_key,
                value={
                    "role": "organism",
                    "sender": self.settings.organism.name,
                    "chat_id": target_chat_id,
                    "text": "",
                    "action": "REACT_ONLY",
                    "reaction_emoji": safe_emoji,
                    "message_id": target_msg_id,
                    "timestamp": utcnow().isoformat(),
                },
                status="recorded",
                source="dialogue:telegram",
            )
            await self.repo.upsert_world_item(
                kind="social_rhythm_state",
                key="primary",
                value={"last_reply_at": now.isoformat(), "last_action": "REACT_ONLY"},
                status="active",
                source="telegram:social",
            )
        elif final_action == "REPLY_LATER":
            raw_defer = social_dec.defer_minutes or social_cfg.default_defer_minutes
            defer_mins_applied = max(
                int(social_cfg.min_defer_minutes),
                min(int(social_cfg.max_defer_minutes), int(raw_defer)),
            )
            due_at = now + timedelta(minutes=defer_mins_applied)
            orig_txt = str(
                (open_inbox[-1].get("text") if open_inbox else None)
                or (due_deferred[-1].get("original_text") if due_deferred else None)
                or ""
            )[:1000]
            def_key = f"def:{int(now.timestamp() * 1000)}:{secrets.token_hex(3)}"
            await self.repo.upsert_world_item(
                kind="social_deferred_reply",
                key=def_key,
                value={
                    "chat_id": target_chat_id,
                    "sender": target_sender,
                    "original_text": orig_txt,
                    "message_id": target_msg_id,
                    "due_at": due_at.isoformat(),
                    "reason": str(social_dec.defer_reason or "")[:300],
                    "inbox_key": open_inbox[-1].get("key") if open_inbox else None,
                    "defer_count": next_defer_count,
                    "deferred_activity": current_activity_summary[:180],
                    "created_at": now.isoformat(),
                },
                status="open",
                source="social:deferred",
            )
            await self.repo.upsert_world_item(
                kind="social_rhythm_state",
                key="primary",
                value={"last_action": "REPLY_LATER"},
                status="active",
                source="telegram:social",
            )
        else:  # IGNORE (true silence — zero Telegram calls)
            ignore_upd: dict[str, Any] = {"last_action": "IGNORE"}
            if trigger_source == "proactive_impulse":
                ignore_upd["last_proactive_evaluated_at"] = now.isoformat()
                if (impulse or {}).get("event_id"):
                    ignore_upd.update(
                        self._consume_milestone_update(
                            rhythm=rhythm,
                            impulse=impulse,
                            now=now,
                        )
                    )
            await self.repo.upsert_world_item(
                kind="social_rhythm_state",
                key="primary",
                value=ignore_upd,
                status="active",
                source="telegram:social",
            )

        # Unified high-value memory promotion -> ExperienceManager (EPISODIC, source="dialogue:telegram")
        memory_promoted = False
        promoted_memory_id: str | None = None
        promoted_importance: float | None = None
        mem_min_imp = float(social_cfg.memory_promotion_min_importance)
        mem_max_imp = float(social_cfg.social_memory_max_importance)
        mem_summary_clean = self._sanitize_social_output(social_dec.memory_summary)
        if (
            trigger_source != "proactive_impulse"
            and social_dec.should_remember
            and len(mem_summary_clean) >= 6
            and float(social_dec.memory_importance or 0.0) >= mem_min_imp
            and self.memory is not None
        ):
            calibrated_imp = round(min(mem_max_imp, max(0.35, float(social_dec.memory_importance))), 3)
            try:
                recent_ep = await self.repo.list_memory_index(memory_type=MemoryType.EPISODIC.value, limit=40)
                if self._is_duplicate_dialogue_memory(mem_summary_clean, recent_ep):
                    logger.info(
                        "cycle.social_memory_dedup_skipped",
                        cycle_id=str(cycle_id),
                        summary_preview=mem_summary_clean[:60],
                    )
                else:
                    mem_res = await self.memory.remember(
                        MemoryItem(
                            memory_type=MemoryType.EPISODIC.value,
                            title=f"与 {target_sender} 的对话记忆：{mem_summary_clean[:48]}",
                            content=mem_summary_clean[:800],
                            source="dialogue:telegram",
                            importance=calibrated_imp,
                            tags=["dialogue", "telegram", "social", trigger_source],
                        ),
                        cycle_id=cycle_id,
                        reliability=0.75,
                    )
                    memory_promoted = True
                    promoted_memory_id = mem_res.stored_index_id
                    promoted_importance = calibrated_imp
            except Exception as exc:
                logger.warning("cycle.social_memory_promote_degraded", cycle_id=str(cycle_id), error=str(exc))

        # Gentle conversational goal hint -> queued softly onto social_rhythm_state for next GOAL_GENERATION
        adopted_hint_clean = self._sanitize_social_output(social_dec.adopted_goal_hint)
        if social_dec.adopt_as_goal and adopted_hint_clean:
            try:
                rhythm_rows_cur = await self.repo.list_world_items(kind="social_rhythm_state")
                rhythm_cur = next(
                    (
                        dict(r.get("value") or {})
                        for r in rhythm_rows_cur
                        if r.get("key") == "primary" and isinstance(r.get("value"), dict)
                    ),
                    {},
                )
                hints_list = [h for h in list(rhythm_cur.get("pending_goal_hints") or []) if isinstance(h, dict)]
                hints_list.append(
                    {
                        "hint": adopted_hint_clean[:400],
                        "sender": target_sender,
                        "from_cycle_id": str(cycle_id),
                        "created_at": now.isoformat(),
                    }
                )
                await self.repo.upsert_world_item(
                    kind="social_rhythm_state",
                    key="primary",
                    value={"pending_goal_hints": hints_list[-3:]},
                    status="active",
                    source="telegram:social",
                )
                if (
                    self.exploration_manager is not None
                    and self.settings.kernel_v3.interest_portfolio_enabled
                    and is_substantive_interest_guidance(adopted_hint_clean)
                ):
                    await self.exploration_manager.upsert_interest_item(
                        prompt=adopted_hint_clean[:400],
                        origin="human_seeded",
                        priority=0.85,
                        status="active",
                        source="telegram:social",
                        sender=target_sender,
                    )
            except Exception as exc:
                logger.warning("cycle.social_goal_hint_store_degraded", cycle_id=str(cycle_id), error=str(exc))

        # Trim short-term dialogue window to max configured turns
        keep_turns = max(1, int(social_cfg.dialogue_window_turns))
        all_turns = await self.repo.list_world_items(kind="dialogue_turn")
        if len(all_turns) > keep_turns:
            all_turns.sort(key=lambda x: str((x.get("value") or {}).get("timestamp") or x.get("created_at") or ""))
            for stale in all_turns[: len(all_turns) - keep_turns]:
                await self.repo.delete_world_item("dialogue_turn", stale["key"])

        await self.repo.log_decision(
            decision_type="social_reflection",
            selected=final_action,
            alternatives=["REPLY_NOW", "REPLY_LATER", "REACT_ONLY", "IGNORE"],
            reason=str(social_dec.internal_monologue or f"social:{final_action}")[:500],
            payload={
                "trigger_source": trigger_source,
                "proactive_kind": (impulse or {}).get("kind"),
                "inbox_count": len(open_inbox),
                "deferred_due_count": len(due_deferred),
                "internal_monologue": social_dec.internal_monologue,
                "action": final_action,
                "reaction_emoji": safe_emoji,
                "reply_preview": sanitized_reply[:200] if sanitized_reply else None,
                "defer_minutes": defer_mins_applied,
                "defer_reason": social_dec.defer_reason,
                "defer_cap_reached": defer_cap_reached,
                "adopt_as_goal": social_dec.adopt_as_goal,
                "adopted_goal_hint": adopted_hint_clean or social_dec.adopted_goal_hint,
                "should_remember": social_dec.should_remember,
                "memory_promoted": memory_promoted,
                "memory_id": promoted_memory_id,
                "memory_importance": promoted_importance,
                "affect_snapshot": eff_affect,
                "busy_focus_level": busy_focus_level,
                "memory_resonance": social_ctx.memory_resonance,
                "recent_social_fatigue": social_fatigue,
                "delivery": delivery_res,
            },
            cycle_id=cycle_id,
        )
        return {
            "triggered": True,
            "trigger_source": trigger_source,
            "proactive_kind": (impulse or {}).get("kind"),
            "action": final_action,
            "reaction_emoji": safe_emoji,
            "defer_minutes": defer_mins_applied,
            "defer_cap_reached": defer_cap_reached,
            "inbox_handled": len(open_inbox),
            "deferred_handled": len(due_deferred),
            "adopt_as_goal": social_dec.adopt_as_goal,
            "adopted_goal_hint": adopted_hint_clean or social_dec.adopted_goal_hint,
            "memory_promoted": memory_promoted,
            "memory_id": promoted_memory_id,
            "memory_importance": promoted_importance,
            "delivery_ok": bool(delivery_res.get("ok", True)),
        }

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
