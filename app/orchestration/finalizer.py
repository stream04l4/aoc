"""Unified Idempotent Execution Finalizer (V2.0 P1-2, Task Book Section 5.2.2).

Single authoritative completion handler for ALL execution paths:
  - Same-cycle completion
  - Cross-cycle IN_FLIGHT completion
  - Post-approval async completion
  - Runner callback / startup crash recovery
  - Outbox `execution.finalize` event replay (strictly idempotent across N replays)

Reconstructs full cognitive/goal/observation/drive context strictly from PostgreSQL,
requiring ZERO in-memory state from the originating cycle.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, Field

from app.agents.base import AgentExecution
from app.cognition.gateway import LLMGateway, LLMGatewayInterface
from app.cognition.interfaces import (
    ActionPlan,
    BrainError,
    BrainInterface,
    EvaluationContext,
    EvaluationResult,
    GoalProposal,
)
from app.core.enums import DriveType, GoalStatus, MemoryType, TaskStatus
from app.drives.engine import DriveEngine
from app.drives.interfaces import DriveContext, DriveState, KnowledgeGap
from app.governance.interfaces import GovernanceInterface
from app.memory.embedding import EmbeddingError, EmbeddingService
from app.memory.experience_manager import ExperienceManager, compute_content_hash
from app.memory.info_gain import ExistingMemorySample, InformationGainEvaluator
from app.memory.interfaces import MemoryItem
from app.observability.logging import get_logger
from app.orchestration.verifier import ResultVerifier
from app.perception.interfaces import Observation, utcnow
from app.perception.manager import SensorManager
from app.storage.outbox import OutboxEventType
from app.storage.repository import StateRepository

logger = get_logger("aoc.finalizer")

CONSUMER_NAME = "task_finalizer"
FinalizedHook = Callable[["FinalizationResult"], Awaitable[None]]


class FinalizationResult(BaseModel):
    execution_id: str
    task_id: str
    goal_id: str | None = None
    cycle_id: str | None = None
    already_finalized: bool = False
    goal_achieved: bool = False
    task_status: str = ""
    evaluation: dict[str, Any] = Field(default_factory=dict)
    measured_satisfaction_change: dict[str, float] = Field(default_factory=dict)
    post_drives: dict[str, dict[str, Any]] = Field(default_factory=dict)
    memory_ids: list[str] = Field(default_factory=list)


class TaskFinalizer:
    def __init__(
        self,
        organism_id: uuid.UUID | None,
        repo: StateRepository,
        perception: SensorManager,
        drives: DriveEngine,
        brain: BrainInterface,
        governance: GovernanceInterface,
        memory: ExperienceManager,
        verifier: ResultVerifier | None = None,
        llm_gateway: LLMGatewayInterface | None = None,
        on_learning: Callable[[datetime, float], None] | None = None,
        embedding_service: EmbeddingService | None = None,
        info_gain_evaluator: InformationGainEvaluator | None = None,
    ) -> None:
        self.organism_id = organism_id
        self.repo = repo
        self.perception = perception
        self.drives = drives
        self.brain = brain
        self.governance = governance
        self.memory = memory
        self.verifier = verifier or ResultVerifier()
        self.llm_gateway = llm_gateway
        self._on_learning = on_learning
        self.embedding_service = embedding_service
        self.info_gain_evaluator = info_gain_evaluator or InformationGainEvaluator()
        self._hooks: list[FinalizedHook] = []
        self._locks: dict[str, asyncio.Lock] = {}

    def register_hook(self, hook: FinalizedHook) -> None:
        """Extension point (e.g. P1-7 SelfModelManager updates after verified finalization)."""
        self._hooks.append(hook)

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def finalize_execution(
        self,
        execution_id: uuid.UUID | str,
        *,
        pre_obs_override: Observation | None = None,
        pre_drives_override: dict[str, DriveState] | None = None,
        llm_available_override: bool | None = None,
    ) -> FinalizationResult:
        """Idempotently finalize one terminal execution using DB-reconstructed context."""
        exec_uuid = uuid.UUID(str(execution_id))
        exec_key = str(exec_uuid)
        idem_key = f"finalize:{exec_key}"

        async with self._lock_for(exec_key):
            exec_row = await self.repo.get_execution(exec_uuid)
            if exec_row is None:
                raise ValueError(f"Execution {exec_key} not found")
            task_uuid = uuid.UUID(exec_row["task_id"])
            task_row = await self.repo.get_task(task_uuid)
            if task_row is None:
                raise ValueError(f"Task {exec_row['task_id']} not found for execution {exec_key}")

            # 1. Strict idempotency check (both execution.finalized_at and inbox_events)
            if (
                exec_row.get("finalized_at") is not None
                or task_row.get("finalized_at") is not None
                or await self.repo.has_inbox_event(CONSUMER_NAME, idem_key)
            ):
                logger.info("finalizer.idempotent_noop", execution_id=exec_key, task_id=str(task_uuid))
                return FinalizationResult(
                    execution_id=exec_key,
                    task_id=str(task_uuid),
                    goal_id=task_row.get("goal_id"),
                    cycle_id=task_row.get("cycle_id"),
                    already_finalized=True,
                    goal_achieved=bool((task_row.get("outcome") or {}).get("goal_achieved", task_row["status"] == TaskStatus.SUCCEEDED.value)),
                    task_status=task_row["status"],
                )

            # Ensure deterministic verification is present
            verification_dict = exec_row.get("verification") or (task_row.get("outcome") or {}).get("verification") or {}
            if not verification_dict:
                agent_exec = AgentExecution(
                    execution_id=exec_key,
                    task_id=str(task_uuid),
                    agent_id=exec_row["agent_id"],
                    status=exec_row["status"],
                    started_at=exec_row["started_at"],
                    completed_at=exec_row.get("completed_at"),
                    result=exec_row.get("result"),
                    stdout=exec_row.get("stdout"),
                    stderr=exec_row.get("stderr"),
                    error=exec_row.get("error"),
                    resource_usage=exec_row.get("resource_usage") or {},
                    pid=exec_row.get("pid"),
                )
                ver_res = self.verifier.verify(
                    execution=agent_exec,
                    workspace=task_row["workspace"],
                    specs=task_row.get("verification") or [],
                )
                verification_dict = ver_res.model_dump(mode="json")
                await self.repo.update_execution(exec_uuid, verification=verification_dict)

            verification_passed = bool(
                task_row["status"] == TaskStatus.SUCCEEDED.value
                and exec_row["status"] == "SUCCEEDED"
                and verification_dict.get("passed", False)
            )

            # 2. Pure-DB Context Reconstruction (works seamlessly across cycles & after restarts!)
            cycle_uuid = uuid.UUID(task_row["cycle_id"]) if task_row.get("cycle_id") else None
            goal_uuid = uuid.UUID(task_row["goal_id"]) if task_row.get("goal_id") else None
            plan_uuid = uuid.UUID(task_row["plan_id"]) if task_row.get("plan_id") else None

            goal_row = await self.repo.get_goal(goal_uuid) if goal_uuid else None
            plan_row = await self.repo.get_plan(plan_uuid) if plan_uuid else None
            cycle_row = await self.repo.get_cycle(cycle_uuid) if cycle_uuid else None

            org_id = self.organism_id or await self.repo.get_default_organism_id()
            selected_drive = (
                (goal_row or {}).get("drive_type")
                or ((cycle_row or {}).get("decision") or {}).get("selected_drive")
                or DriveType.CURIOSITY.value
            )
            goal_dict = goal_row or {
                "title": task_row["objective"][:200],
                "description": task_row.get("description", ""),
                "drive_type": selected_drive,
                "related_key": None,
                "expected_outcome": {},
            }
            plan_dict = (plan_row or {}).get("plan") or {
                "goal_title": goal_dict.get("title", ""),
                "objective": task_row["objective"],
                "description": task_row.get("description", ""),
                "assigned_agent": task_row["assigned_agent"],
                "expected_improvement": 0.0,
            }

            if pre_obs_override is not None:
                pre_obs = pre_obs_override
            elif cycle_uuid is not None:
                obs_row = await self.repo.get_observation_for_cycle(cycle_uuid, phase="pre")
                pre_obs = Observation(**{k: v for k, v in obs_row.items() if k != "observation_id"}) if obs_row else Observation()
            else:
                pre_obs = Observation()

            if pre_drives_override is not None:
                pre_drives = pre_drives_override
            else:
                hist_rows = await self.repo.get_drive_history_for_cycle(cycle_uuid, phase="pre") if cycle_uuid else []
                if hist_rows:
                    pre_drives = {r["drive_type"]: DriveState(**r) for r in hist_rows}
                elif org_id is not None:
                    cur_rows = await self.repo.load_drive_states(org_id)
                    pre_drives = {r["drive_type"]: DriveState(**r) for r in cur_rows}
                else:
                    pre_drives = {}

            # 3. Post-raw perception + Brain Evaluation (guarded by deterministic verification)
            post_obs_initial = await self.perception.perceive(cycle_id=cycle_uuid, phase="post_raw", persist=False)
            if llm_available_override is not None:
                llm_available = llm_available_override
            else:
                llm_probe = (post_obs_initial.system_state or {}).get("llm")
                llm_available = not (isinstance(llm_probe, dict) and llm_probe.get("available") is False)

            eval_ctx = EvaluationContext(
                goal=goal_dict,
                plan=plan_dict,
                execution=exec_row,
                verification_passed=verification_passed,
                verification_details=verification_dict,
                pre_observation=pre_obs,
                post_observation=post_obs_initial,
            )

            if llm_available:
                try:
                    scope_cm = (
                        self.llm_gateway.scope(
                            cycle_id=cycle_uuid,
                            task_id=task_uuid,
                            emergency=(selected_drive == DriveType.SURVIVAL.value),
                        )
                        if isinstance(self.llm_gateway, LLMGateway)
                        else nullcontext()
                    )
                    with scope_cm:
                        eval_res = await self.brain.evaluate(eval_ctx)
                except BrainError as exc:
                    logger.warning("finalizer.evaluate_failed", execution_id=exec_key, error=str(exc))
                    eval_res = EvaluationResult(
                        goal_achieved=verification_passed,
                        summary=f"Deterministic evaluation fallback (LLM error: {exc})",
                        information_gain=0.3 if verification_passed else 0.0,
                        should_persist_memory=True,
                    )
            else:
                eval_res = EvaluationResult(
                    goal_achieved=verification_passed,
                    summary="Deterministic verification evaluation (LLM unavailable)",
                    information_gain=0.2 if verification_passed else 0.0,
                    should_persist_memory=True,
                )

            # Hard invariant (Task Book 17.7): LLM cannot declare success if verification failed
            if not verification_passed and eval_res.goal_achieved:
                eval_res = eval_res.model_copy(
                    update={"goal_achieved": False, "resolved_topic": None, "acquired_skill": None}
                )

            # 3b. Compute deterministic Four-Factor Information Gain (V2.0 P1-4, Task Book 5.4.3)
            measured_info_gain = 0.0
            info_gain_details: dict[str, Any] = {}
            if eval_res.goal_achieved:
                candidate_text = "\n".join([eval_res.summary, *eval_res.learned_facts]).strip()
                existing_samples: list[ExistingMemorySample] = []
                new_emb: list[float] | None = None
                if self.embedding_service is not None and candidate_text:
                    try:
                        mid_active, new_emb = await self.embedding_service.embed_query(candidate_text)
                        emb_rows = await self.repo.list_embeddings_for_model(mid_active, limit=80)
                        existing_samples = [
                            ExistingMemorySample(
                                memory_id=r["memory_id"],
                                content_hash=r["content_hash"],
                                source=r["source"],
                                text=f"{r['title']}\n{r['summary']}",
                                embedding=r["embedding"],
                            )
                            for r in emb_rows
                        ]
                    except EmbeddingError as exc:
                        logger.warning("finalizer.info_gain_embed_degraded", error=str(exc))
                if not existing_samples:
                    recent_mems = await self.repo.list_memory_index(limit=60)
                    existing_samples = [
                        ExistingMemorySample(
                            memory_id=r["memory_id"],
                            content_hash=r["content_hash"],
                            source=r["source"],
                            text=f"{r['title']}\n{r['summary']}",
                        )
                        for r in recent_mems
                    ]
                ig_breakdown = self.info_gain_evaluator.evaluate(
                    new_text=candidate_text,
                    new_facts=eval_res.learned_facts,
                    new_source=f"execution:{exec_key}",
                    new_embedding=new_emb,
                    existing_memories=existing_samples,
                    source_reliability=0.9 if verification_passed else 0.3,
                    goal_relevance=1.0,
                    verification_confidence=1.0 if verification_passed else 0.0,
                    learning_progress_factor=1.0,
                )
                measured_info_gain = ig_breakdown.information_gain
                info_gain_details = ig_breakdown.model_dump(mode="json")

            # 4. Atomic Transaction: World State + Memory Index + Outbox + Finalized Markers + Inbox
            now = utcnow()
            memory_ids: list[str] = []
            memory_writes: list[dict[str, Any]] = []

            async with self.repo.transaction() as s:
                # Double-check inbox inside transaction for strict concurrent idempotency
                newly_claimed = await self.repo.mark_inbox_processed(
                    consumer_name=CONSUMER_NAME,
                    idempotency_key=idem_key,
                    event_type=OutboxEventType.EXECUTION_FINALIZE,
                    result_summary={"execution_id": exec_key, "task_id": str(task_uuid), "goal_achieved": eval_res.goal_achieved},
                    session=s,
                )
                if not newly_claimed:
                    return FinalizationResult(
                        execution_id=exec_key,
                        task_id=str(task_uuid),
                        goal_id=str(goal_uuid) if goal_uuid else None,
                        cycle_id=str(cycle_uuid) if cycle_uuid else None,
                        already_finalized=True,
                        goal_achieved=eval_res.goal_achieved,
                        task_status=task_row["status"],
                    )

                # Update digital world state ONLY on verified achievement
                if eval_res.goal_achieved:
                    resolved_key = eval_res.resolved_topic or (
                        goal_dict.get("related_key") if selected_drive == DriveType.CURIOSITY.value else None
                    )
                    if resolved_key:
                        await self.repo.upsert_world_item(
                            kind="knowledge_topic",
                            key=str(resolved_key),
                            value={"summary": eval_res.summary, "learned_facts": eval_res.learned_facts},
                            status="resolved",
                            source=f"execution:{exec_key}",
                            session=s,
                        )
                    if selected_drive == DriveType.REPRODUCTION.value:
                        skill_key = eval_res.acquired_skill or goal_dict.get("related_key")
                        if skill_key:
                            await self.repo.upsert_world_item(
                                kind="capability",
                                key=str(skill_key),
                                value={"summary": eval_res.summary},
                                status="acquired",
                                source=f"execution:{exec_key}",
                                session=s,
                            )

                # Persist memories atomically into memory_index + enqueue async Letta/Embed outbox events
                items_to_store: list[MemoryItem] = []
                if (
                    eval_res.should_persist_memory
                    or eval_res.goal_achieved
                    or task_row["status"] in {TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}
                ):
                    items_to_store.append(
                        MemoryItem(
                            memory_type=MemoryType.EPISODIC.value,
                            title=f"[{selected_drive}] {goal_dict.get('title', task_row['objective'][:80])}",
                            content=f"Outcome: {'ACHIEVED' if eval_res.goal_achieved else 'FAILED'}. {eval_res.summary}",
                            source=f"execution:{exec_key}",
                            importance=max(eval_res.memory_importance, 0.7 if eval_res.goal_achieved else 0.6),
                            tags=[selected_drive, "achieved" if eval_res.goal_achieved else "failed"],
                        )
                    )
                if eval_res.goal_achieved and eval_res.learned_facts:
                    items_to_store.append(
                        MemoryItem(
                            memory_type=MemoryType.SEMANTIC.value,
                            title=eval_res.resolved_topic or goal_dict.get("title", "knowledge"),
                            content="\n".join(f"- {f}" for f in eval_res.learned_facts),
                            source=f"execution:{exec_key}",
                            importance=max(eval_res.memory_importance, 0.8),
                            tags=[selected_drive, "knowledge"],
                        )
                    )
                if eval_res.goal_achieved and (eval_res.acquired_skill or selected_drive == DriveType.REPRODUCTION.value):
                    items_to_store.append(
                        MemoryItem(
                            memory_type=MemoryType.PROCEDURAL.value,
                            title=eval_res.acquired_skill or goal_dict.get("title", "skill"),
                            content=eval_res.summary,
                            source=f"execution:{exec_key}",
                            importance=max(eval_res.memory_importance, 0.8),
                            tags=["reproduction", "skill"],
                        )
                    )

                for item in items_to_store:
                    chash = compute_content_hash(item.memory_type, item.title, item.content)
                    existing_mem = await self.repo.find_memory_by_hash(chash)
                    if existing_mem is not None:
                        mid = existing_mem["memory_id"]
                        memory_ids.append(mid)
                        memory_writes.append({"stored_index_id": mid, "deduplicated": True, "memory_type": item.memory_type})
                    else:
                        idx_id = await self.repo.add_memory_index(
                            memory_type=item.memory_type,
                            title=item.title[:500],
                            summary=item.content,
                            content_hash=chash,
                            source=item.source,
                            importance=item.normalized_importance(),
                            tags=item.tags,
                            cycle_id=cycle_uuid,
                            sync_status="pending",
                            embedding_status="pending",
                            reliability=0.9 if verification_passed else 0.5,
                            information_gain=measured_info_gain if eval_res.goal_achieved else 0.0,
                            session=s,
                        )
                        mid = str(idx_id)
                        memory_ids.append(mid)
                        memory_writes.append({"stored_index_id": mid, "deduplicated": False, "memory_type": item.memory_type})
                        # Enqueue async Embedding & Letta sync via Transactional Outbox (V2.0 P1-4)
                        if self.embedding_service is not None:
                            await self.repo.enqueue_outbox_event(
                                event_type=OutboxEventType.MEMORY_EMBED,
                                idempotency_key=f"embed:{mid}",
                                payload={"memory_id": mid},
                                aggregate_type="memory",
                                aggregate_id=idx_id,
                                session=s,
                            )
                        await self.repo.enqueue_outbox_event(
                            event_type=OutboxEventType.MEMORY_SYNC_LETTA,
                            idempotency_key=f"sync_letta:{mid}",
                            payload={"memory_id": mid, "memory_type": item.memory_type},
                            aggregate_type="memory",
                            aggregate_id=idx_id,
                            session=s,
                        )

                # Mark execution & task finalized inside the same atomic transaction
                await self.repo.update_execution(exec_uuid, finalized_at=now, session=s)
                updated_outcome = dict(task_row.get("outcome") or {})
                updated_outcome["goal_achieved"] = eval_res.goal_achieved
                updated_outcome["finalized_at"] = now.isoformat()
                if info_gain_details:
                    updated_outcome["info_gain_breakdown"] = info_gain_details
                await self.repo.update_task(task_uuid, finalized_at=now, outcome=updated_outcome, session=s)

            # Notify in-memory learning tracker (used by CuriosityDriveEvaluator)
            info_gain = 0.0
            if eval_res.goal_achieved and (measured_info_gain > 0 or eval_res.information_gain > 0 or eval_res.learned_facts):
                info_gain = measured_info_gain if info_gain_details else max(
                    eval_res.information_gain, 0.25 if eval_res.learned_facts else 0.0
                )
                if self._on_learning is not None:
                    self._on_learning(now, info_gain)

            # 5. Post-perception & Drive Re-calculation (Task Book 9.1 hard invariant)
            post_obs = await self.perception.perceive(cycle_id=cycle_uuid, phase="post", persist=True)
            post_ctx = await self._build_post_drive_context(post_obs, now, info_gain=info_gain)
            post_drives = self.drives.compute_states(post_ctx, pre_drives)
            if org_id is not None:
                await self.repo.save_drive_states(org_id, post_drives.values(), cycle_id=cycle_uuid, phase="post")

            measured_change = {
                dtype: round(post_drives[dtype].satisfaction - pre_drives[dtype].satisfaction, 4)
                for dtype in post_drives
                if dtype in pre_drives
            }
            expected_vs_actual = {
                "expected_outcome": {
                    **(goal_dict.get("expected_outcome") or {}),
                    "expected_improvement": plan_dict.get("expected_improvement", 0.0),
                },
                "actual_outcome": {
                    "goal_achieved": eval_res.goal_achieved,
                    "task_status": task_row["status"],
                    "information_gain": eval_res.information_gain,
                    "verification_passed": verification_passed,
                    **eval_res.actual_outcome,
                },
                "measured_satisfaction_change": measured_change,
            }

            # 6. Goal Finalization & Cycle Record Backfill
            if goal_uuid is not None:
                target_goal_status = (
                    GoalStatus.ACHIEVED.value
                    if eval_res.goal_achieved
                    else (GoalStatus.ABANDONED.value if task_row["status"] == TaskStatus.CANCELLED.value else GoalStatus.FAILED.value)
                )
                await self.repo.transition_goal(
                    goal_uuid,
                    target_goal_status,
                    note=eval_res.summary,
                    actual_outcome=expected_vs_actual["actual_outcome"],
                    measured_change=measured_change,
                )

            eval_payload = {
                **eval_res.model_dump(mode="json"),
                **expected_vs_actual,
                "memory_writes": memory_writes,
                "finalized_at": now.isoformat(),
            }
            if cycle_uuid is not None:
                cycle_updates: dict[str, Any] = {
                    "evaluation": eval_payload,
                    "drives_post": {k: v.model_dump(mode="json") for k, v in post_drives.items()},
                    "state_changes": expected_vs_actual,
                }
                if cycle_row and cycle_row.get("outcome") in {"IN_FLIGHT", None}:
                    cycle_updates["outcome"] = "SUCCEEDED" if eval_res.goal_achieved else "ACTION_FAILED"
                await self.repo.update_cycle(cycle_uuid, **cycle_updates)

            # Best-effort immediate Letta sync for newly added memories (Outbox handles retries if down)
            if self.memory.backend is not None and self.memory.backend_available:
                try:
                    await self.memory.flush_pending(max_items=len(items_to_store) + 5)
                except Exception as exc:
                    logger.warning("finalizer.letta_flush_deferred", error=str(exc))

            result = FinalizationResult(
                execution_id=exec_key,
                task_id=str(task_uuid),
                goal_id=str(goal_uuid) if goal_uuid else None,
                cycle_id=str(cycle_uuid) if cycle_uuid else None,
                already_finalized=False,
                goal_achieved=eval_res.goal_achieved,
                task_status=task_row["status"],
                evaluation=eval_payload,
                measured_satisfaction_change=measured_change,
                post_drives={k: v.model_dump(mode="json") for k, v in post_drives.items()},
                memory_ids=memory_ids,
            )

            for hook in self._hooks:
                try:
                    await hook(result)
                except Exception as exc:
                    logger.warning("finalizer.hook_error", execution_id=exec_key, error=str(exc))

            logger.info(
                "finalizer.completed",
                execution_id=exec_key,
                task_id=str(task_uuid),
                goal_achieved=eval_res.goal_achieved,
                memories=len(memory_ids),
            )
            return result

    async def _build_post_drive_context(self, obs: Observation, now: datetime, info_gain: float = 0.0) -> DriveContext:
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
        since_7d = now - timedelta(days=7)
        proc_count = await self.repo.count_memories(MemoryType.PROCEDURAL.value)
        sem_recent = await self.repo.count_memories(MemoryType.SEMANTIC.value, since=since_7d)
        cap_goals_done = await self.repo.count_goals_since(
            DriveType.REPRODUCTION.value, GoalStatus.ACHIEVED.value, since_7d
        )
        cur_done = await self.repo.count_goals_since(DriveType.CURIOSITY.value, GoalStatus.ACHIEVED.value, since_7d)
        cur_failed = await self.repo.count_goals_since(DriveType.CURIOSITY.value, GoalStatus.FAILED.value, since_7d)

        last_learning_at = now if info_gain > 0 else None
        last_info_gain = info_gain if info_gain > 0 else None
        if last_learning_at is None:
            sem = await self.repo.list_memory_index(memory_type=MemoryType.SEMANTIC.value, limit=1)
            if sem:
                last_learning_at = sem[0]["created_at"]
                last_info_gain = 0.5

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
            open_knowledge_gaps=env_topics,
            unknown_topics=env_topics,
            exploration_attempted_recent=cur_done + cur_failed,
            exploration_succeeded_recent=cur_done,
            new_knowledge_recent=sem_recent,
            last_learning_at=last_learning_at,
            last_information_gain=last_info_gain,
            last_information_gain_at=last_learning_at,
        )
