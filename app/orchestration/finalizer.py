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
import re
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, Field

from app.agents.base import AgentExecution
from app.cognition.gateway import LLMGateway, LLMGatewayInterface
from app.cognition.goal_dedup import compute_cooldown_until
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
from app.drives.interfaces import DriveContext, DriveState, KnowledgeGap, clamp01
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

_UNRESOLVED_SUMMARY_RE = re.compile(
    r"\b(remains_open|keep_open|evidence_missing|missing_evidence|unverifiable|insufficient|channel_absent|not_verified)\b",
    re.I,
)
_GENERIC_TOPIC_TOKENS = frozenset(
    {
        "world",
        "sensor",
        "rss",
        "web",
        "git",
        "github",
        "releases",
        "aoc",
        "local",
        "repo",
        "repository",
        "read",
        "only",
        "feat",
        "items",
    }
)


def _match_resolved_world_topics(pre_obs: Observation, goal_title: str, summary: str) -> list[str]:
    """Match verified research/triage outcomes back to open `world:*` unknown_topics conservatively."""
    if _UNRESOLVED_SUMMARY_RE.search(summary or ""):
        return []
    haystack = f"{goal_title} {summary}".lower()
    if not haystack.strip():
        return []
    unknown = (pre_obs.environment_state or {}).get("unknown_topics") or []
    matched: list[str] = []
    for item in unknown:
        if not isinstance(item, dict):
            continue
        tkey = str(item.get("topic") or "").strip()
        if not tkey.startswith("world:") or tkey in matched:
            continue
        title_tok = str(item.get("title") or "").strip().lower()
        if len(title_tok) < 4 or title_tok not in haystack:
            continue
        src_blob = f"{item.get('source') or ''} {tkey}"
        src_toks = [
            tok
            for tok in re.split(r"[^a-z0-9]+", src_blob.lower())
            if len(tok) >= 3 and tok not in _GENERIC_TOPIC_TOKENS
        ]
        if src_toks and not any(tok in haystack for tok in src_toks):
            continue
        matched.append(tkey)
    return matched


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
        self_model: Any = None,
        skill_registry: Any = None,
        skill_auto_crystallize: bool = True,
        on_prediction_error: Callable[[float], None] | None = None,
        playbook_manager: Any = None,
        exploration_manager: Any = None,
        human_bridge: Any = None,
        investigation_manager: Any = None,
        world_pipeline: Any = None,
        meta_reflection_manager: Any = None,
        reproduction_quality_scoring_enabled: bool = False,
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
        self._on_prediction_error = on_prediction_error
        self.embedding_service = embedding_service
        self.info_gain_evaluator = info_gain_evaluator or InformationGainEvaluator()
        self.self_model = self_model
        self.skill_registry = skill_registry
        self.skill_auto_crystallize = bool(skill_auto_crystallize)
        self.playbook_manager = playbook_manager
        self.exploration_manager = exploration_manager
        self.human_bridge = human_bridge
        self.investigation_manager = investigation_manager
        self.world_pipeline = world_pipeline
        self.meta_reflection_manager = meta_reflection_manager
        self.reproduction_quality_scoring_enabled = bool(reproduction_quality_scoring_enabled)
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

            # Hard invariant (Task Book 17.7): LLM cannot declare success or confirm hypotheses if verification failed
            if not verification_passed and (
                eval_res.goal_achieved
                or eval_res.hypothesis_verdict == "confirmed"
                or eval_res.playbook_update is not None
                or isinstance(eval_res.investigation_update, dict)
            ):
                safe_inv_upd = eval_res.investigation_update
                if isinstance(safe_inv_upd, dict):
                    cleaned_inv = {
                        k: v
                        for k, v in safe_inv_upd.items()
                        if k not in {"finding", "register_feed_source", "discovered_channel"}
                    }
                    if str(cleaned_inv.get("stage_suggestion") or "").strip().lower() == "resolved":
                        cleaned_inv["stage_suggestion"] = "stalled"
                    safe_inv_upd = cleaned_inv or None
                eval_res = eval_res.model_copy(
                    update={
                        "goal_achieved": False,
                        "resolved_topic": None,
                        "resolved_topics": [],
                        "acquired_skill": None,
                        "hypothesis_verdict": None,
                        "playbook_update": None,
                        "investigation_update": safe_inv_upd,
                    }
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
                resolved_key_preview = (
                    eval_res.resolved_topic
                    or (eval_res.resolved_topics[0] if eval_res.resolved_topics else None)
                    or (goal_dict.get("related_key") if selected_drive == DriveType.CURIOSITY.value else None)
                )
                has_workspace_files = bool(
                    verification_passed and ((exec_row.get("result") or {}).get("workspace_files") or [])
                )
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
                    topic_resolved=bool(resolved_key_preview),
                    new_capability_evidence=bool(self.self_model is not None and verification_passed),
                    verified_code_artifact=has_workspace_files,
                )
                measured_info_gain = ig_breakdown.information_gain
                info_gain_details = ig_breakdown.model_dump(mode="json")

            # 4. Atomic Transaction: World State + Memory Index + Outbox + Finalized Markers + Inbox
            now = utcnow()
            memory_ids: list[str] = []
            memory_writes: list[dict[str, Any]] = []
            effective_cap_key: str = "general_task"
            updated_outcome: dict[str, Any] = dict(task_row.get("outcome") or {})

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
                    keys_to_resolve: list[str] = []
                    for candidate in [*(eval_res.resolved_topics or []), eval_res.resolved_topic]:
                        c_str = str(candidate or "").strip()
                        if c_str and c_str not in keys_to_resolve:
                            keys_to_resolve.append(c_str)
                    if verification_passed and selected_drive == DriveType.CURIOSITY.value:
                        for mk in _match_resolved_world_topics(
                            pre_obs, str(goal_dict.get("title") or ""), eval_res.summary
                        ):
                            if mk not in keys_to_resolve:
                                keys_to_resolve.append(mk)
                    if not keys_to_resolve and selected_drive == DriveType.CURIOSITY.value and goal_dict.get("related_key"):
                        keys_to_resolve.append(str(goal_dict["related_key"]))
                    for resolved_key in keys_to_resolve:
                        await self.repo.upsert_world_item(
                            kind="knowledge_topic",
                            key=resolved_key,
                            value={"summary": eval_res.summary, "learned_facts": eval_res.learned_facts},
                            status="resolved",
                            source=f"execution:{exec_key}",
                            session=s,
                        )
                    if selected_drive == DriveType.REPRODUCTION.value:
                        raw_acq = str(eval_res.acquired_skill or "").strip()
                        raw_rel = str(goal_dict.get("related_key") or "").strip()
                        if raw_acq and len(raw_acq) <= 100 and " " not in raw_acq:
                            skill_key = raw_acq
                        elif raw_rel:
                            skill_key = raw_rel[:120]
                        elif raw_acq:
                            skill_key = raw_acq[:120]
                        else:
                            skill_key = ""
                        if skill_key:
                            val_payload = {"summary": eval_res.summary}
                            if raw_acq:
                                val_payload["acquired_skill"] = raw_acq[:500]
                            await self.repo.upsert_world_item(
                                kind="capability",
                                key=skill_key,
                                value=val_payload,
                                status="acquired",
                                source=f"execution:{exec_key}",
                                session=s,
                            )

                # V3.1: Lightweight Hypothesis Verdict Update (observed facts vs unverified hypotheses)
                hyp_key = eval_res.hypothesis_key or (goal_dict.get("expected_outcome") or {}).get("hypothesis_key")
                hyp_verdict = eval_res.hypothesis_verdict
                if hyp_key and hyp_verdict:
                    # Hard guard: NEVER mark hypothesis confirmed unless deterministic verification passed
                    eff_verdict = hyp_verdict if (verification_passed or hyp_verdict != "confirmed") else "inconclusive"
                    existing_hyps = await self.repo.list_world_items(kind="hypothesis")
                    prev_hyp = next((h for h in existing_hyps if h.get("key") == str(hyp_key)), None)
                    prev_val = dict(prev_hyp.get("value") or {}) if prev_hyp and isinstance(prev_hyp.get("value"), dict) else {}
                    ev_list = list(prev_val.get("evidence") or [])
                    ev_list.append(
                        {
                            "execution_id": exec_key,
                            "verdict": eff_verdict,
                            "verified": verification_passed,
                            "summary": eval_res.summary[:300],
                            "observed_at": now.isoformat(),
                        }
                    )
                    new_conf = 0.9 if eff_verdict == "confirmed" else (0.15 if eff_verdict == "refuted" else 0.45)
                    prev_val["evidence"] = ev_list[-10:]
                    prev_val["confidence"] = new_conf
                    prev_val["last_verdict_summary"] = eval_res.summary[:300]
                    prev_val["verified_at"] = now.isoformat()
                    await self.repo.upsert_world_item(
                        kind="hypothesis",
                        key=str(hyp_key)[:120],
                        value=prev_val,
                        status=eff_verdict,
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
                    sem_title = (
                        eval_res.resolved_topic
                        or (eval_res.resolved_topics[0] if eval_res.resolved_topics else None)
                        or goal_dict.get("title", "knowledge")
                    )
                    items_to_store.append(
                        MemoryItem(
                            memory_type=MemoryType.SEMANTIC.value,
                            title=sem_title,
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

                # Update Self Model capability_evidence & capability_profiles atomically (V2.0 P1-7)
                self_model_evidence: dict[str, Any] | None = None
                if self.self_model is not None:
                    raw_skill = (eval_res.acquired_skill or "").strip()
                    short_skill = raw_skill if (0 < len(raw_skill) <= 64 and " " not in raw_skill) else None
                    raw_rel = str(goal_dict.get("related_key") or "").strip()
                    short_rel = (
                        raw_rel
                        if (selected_drive == DriveType.REPRODUCTION.value and 0 < len(raw_rel) <= 64 and " " not in raw_rel)
                        else None
                    )
                    req_cap = str(
                        plan_dict.get("required_capability") or task_row.get("required_capability") or ""
                    ).strip()
                    explicit_cap = req_cap if (req_cap and req_cap != "general_task") else None
                    cap_key = (
                        explicit_cap
                        or short_skill
                        or short_rel
                        or req_cap
                        or "general_task"
                    )
                    effective_cap_key = str(cap_key)[:120]
                    wall_sec = (exec_row.get("resource_usage") or {}).get("wall_seconds")
                    self_model_evidence = await self.self_model.record_execution_evidence(
                        capability_key=effective_cap_key,
                        execution_id=exec_uuid,
                        task_id=task_uuid,
                        verified_success=bool(verification_passed and eval_res.goal_achieved),
                        verification_summary=str(
                            verification_dict.get("summary") or eval_res.summary or ""
                        )[:800],
                        agent_id=exec_row.get("agent_id"),
                        wall_seconds=float(wall_sec) if wall_sec is not None else None,
                        session=s,
                    )

                # Mark execution, any superseded earlier attempts, & task finalized inside the same atomic transaction
                await self.repo.update_execution(exec_uuid, finalized_at=now, session=s)
                await self.repo.finalize_superseded_executions(
                    task_uuid, now, exclude_execution_id=exec_uuid, session=s
                )
                updated_outcome["goal_achieved"] = eval_res.goal_achieved
                updated_outcome["finalized_at"] = now.isoformat()
                if info_gain_details:
                    updated_outcome["info_gain_breakdown"] = info_gain_details
                if self_model_evidence is not None:
                    updated_outcome["self_model_evidence"] = {
                        "inserted": self_model_evidence.get("inserted"),
                        "evidence_id": self_model_evidence.get("evidence_id"),
                        "capability_key": (self_model_evidence.get("profile") or {}).get("capability_key"),
                        "maturity_status": (self_model_evidence.get("profile") or {}).get("maturity_status"),
                    }
                await self.repo.update_task(task_uuid, finalized_at=now, outcome=updated_outcome, session=s)

            # V3.0 M4: Fault-isolated Skill Auto-Crystallization outside DB transaction
            # Runs before post-drive recalculation so newly auto-promoted L0/L1 VALIDATED skills
            # immediately reflect in ReproductionDriveEvaluator's validated_skill_count.
            skill_crystallized: dict[str, Any] | None = None
            if (
                eval_res.goal_achieved
                and verification_passed
                and self.skill_registry is not None
                and self.skill_auto_crystallize
                and task_row.get("workspace")
                and str(task_row.get("assigned_agent") or "") not in {"brain_research", "skill_runtime"}
            ):
                try:
                    raw_skey = (
                        eval_res.acquired_skill
                        or goal_dict.get("related_key")
                        or goal_dict.get("title")
                        or "skill_artifact"
                    )
                    cryst_cap = (
                        effective_cap_key
                        if effective_cap_key and effective_cap_key != "general_task"
                        else (task_row.get("required_capability") or "coding")
                    )
                    res_budget = (
                        task_row.get("resource_budget")
                        if isinstance(task_row.get("resource_budget"), dict)
                        else {}
                    )
                    refactor_target = res_budget.get("skill_refactor_target")
                    if not isinstance(refactor_target, dict):
                        exp_ref = (goal_dict.get("expected_outcome") or {}).get("skill_refactor_target")
                        refactor_target = exp_ref if isinstance(exp_ref, dict) else None
                    skill_crystallized = await self.skill_registry.crystallize_from_workspace(
                        workspace=str(task_row["workspace"]),
                        skill_key=str(raw_skey),
                        capability_key=str(cryst_cap),
                        description=str(eval_res.summary or goal_dict.get("description") or task_row["objective"]),
                        permission_level=str(task_row.get("permission_level") or "L1"),
                        source_task_id=task_uuid,
                        source_execution_id=exec_uuid,
                        skill_refactor_target=refactor_target if isinstance(refactor_target, dict) else None,
                    )
                    if skill_crystallized is not None:
                        updated_outcome["skill_crystallized"] = skill_crystallized
                        await self.repo.update_task(task_uuid, outcome=updated_outcome)
                        if skill_crystallized.get("status") == "VALIDATED" and (
                            isinstance(refactor_target, dict) or skill_crystallized.get("evolved")
                        ):
                            c_target = str(
                                (refactor_target or {}).get("canonical_name")
                                or (refactor_target or {}).get("skill_key")
                                or skill_crystallized.get("canonical_name")
                                or skill_crystallized.get("skill_key")
                                or ""
                            ).strip()
                            if c_target:
                                if self.meta_reflection_manager is not None and hasattr(
                                    self.meta_reflection_manager, "mark_skill_refactor_consumed"
                                ):
                                    await self.meta_reflection_manager.mark_skill_refactor_consumed(
                                        c_target, execution_id=exec_key
                                    )
                                merge_ids: list[str] = [
                                    str(x)
                                    for x in list((refactor_target or {}).get("merge_candidate_ids") or [])
                                    if x
                                ]
                                merge_keys: list[str] = [
                                    str(x)
                                    for x in list((refactor_target or {}).get("merge_candidates") or [])
                                    if x
                                ]
                                q_rows = await self.repo.list_world_items(
                                    kind="skill_refactor_queue", status="open"
                                )
                                for qr in q_rows:
                                    qv = dict(qr.get("value") or {}) if isinstance(qr.get("value"), dict) else {}
                                    if (
                                        qr.get("key") in {f"refactor:{c_target}", f"consolidate:{c_target}"}
                                        or qv.get("skill_key") == c_target
                                        or qv.get("canonical_name") == c_target
                                        or qv.get("primary_skill_key") == c_target
                                    ):
                                        for x in list(qv.get("merge_candidate_ids") or []):
                                            if x and str(x) not in merge_ids:
                                                merge_ids.append(str(x))
                                        for x in list(qv.get("merge_candidates") or []):
                                            if x and str(x) not in merge_keys:
                                                merge_keys.append(str(x))
                                        qv["consumed_at"] = utcnow().isoformat()
                                        qv["consumed_execution_id"] = exec_key
                                        await self.repo.upsert_world_item(
                                            kind="skill_refactor_queue",
                                            key=str(qr["key"]),
                                            value=qv,
                                            status="completed",
                                            source="skill_refactor_consumed",
                                        )
                                new_sid_str = str(skill_crystallized.get("skill_id") or "").strip()
                                if new_sid_str and (merge_ids or merge_keys):
                                    try:
                                        sup_uuid = uuid.UUID(new_sid_str)
                                        val_rows = await self.repo.list_skills(status="VALIDATED", limit=300)
                                        for vr in val_rows:
                                            vid = str(vr.get("id") or "")
                                            if not vid or vid == new_sid_str:
                                                continue
                                            if vid in merge_ids or vr.get("skill_key") in merge_keys or vr.get("canonical_name") in merge_keys:
                                                await self.repo.update_skill_metadata(
                                                    uuid.UUID(vid),
                                                    status="DEPRECATED",
                                                    superseded_by=sup_uuid,
                                                    evolution_meta={
                                                        "deprecated_reason": "consolidated_superseded",
                                                        "superseded_at": utcnow().isoformat(),
                                                    },
                                                )
                                    except Exception as exc:
                                        logger.warning("finalizer.consolidate_supersede_degraded", error=str(exc))
                except Exception as exc:
                    logger.warning("finalizer.skill_crystallize_degraded", execution_id=exec_key, error=str(exc))

            # V4.0 M3: External Playbook outcome tracking, auto-rollback & autonomous evolution (fault-isolated)
            playbook_evolution: dict[str, Any] = {}
            if self.playbook_manager is not None:
                try:
                    exp_out = goal_dict.get("expected_outcome") or {}
                    cand_domains = list(exp_out.get("playbook_domains") or [])
                    for extra_d in (selected_drive, effective_cap_key):
                        if extra_d and extra_d not in cand_domains:
                            cand_domains.append(str(extra_d))
                    tracked_outcomes: list[dict[str, Any]] = []
                    for dom in cand_domains:
                        tr = await self.playbook_manager.record_outcome_and_check_rollback(
                            domain=dom,
                            succeeded=bool(eval_res.goal_achieved and verification_passed),
                            verified_ig=measured_info_gain,
                        )
                        if tr.get("tracked"):
                            tracked_outcomes.append(tr)
                    if tracked_outcomes:
                        playbook_evolution["outcomes"] = tracked_outcomes

                    if eval_res.goal_achieved and verification_passed and isinstance(eval_res.playbook_update, dict):
                        pb_dom = str(eval_res.playbook_update.get("domain") or selected_drive or "general").strip()
                        pb_content = str(eval_res.playbook_update.get("content") or "").strip()
                        pb_summary = str(eval_res.playbook_update.get("change_summary") or eval_res.summary or "")[:300]
                        if pb_content:
                            rev = await self.playbook_manager.save_revision(
                                domain=pb_dom,
                                content=pb_content,
                                change_summary=pb_summary,
                                source=f"execution:{exec_key}",
                            )
                            playbook_evolution["revision"] = rev
                except Exception as exc:
                    logger.warning("finalizer.playbook_evolution_degraded", execution_id=exec_key, error=str(exc))

            # V4.0 M3 + V5.2 M2: Mark Curiosity exploration topic verified & update persistent interest portfolio
            if self.exploration_manager is not None and eval_res.goal_achieved and verification_passed:
                try:
                    exp_out = goal_dict.get("expected_outcome") or {}
                    exp_key = exp_out.get("exploration_topic_key") or (
                        eval_res.resolved_topic if selected_drive == DriveType.CURIOSITY.value else None
                    )
                    if exp_key:
                        fact_txt = (
                            str(eval_res.learned_facts[0])
                            if eval_res.learned_facts
                            else str(eval_res.summary or "")[:300]
                        )
                        await self.exploration_manager.upsert_topic(
                            topic_key=str(exp_key),
                            question=str(goal_dict.get("title") or exp_key),
                            domain=exp_out.get("exploration_domain"),
                            status="verified",
                            source=f"execution:{exec_key}",
                            verified_fact=fact_txt,
                            information_gain=measured_info_gain,
                        )
                    if getattr(self.exploration_manager, "interest_portfolio_enabled", False):
                        ip_upd = await self.exploration_manager.mark_interest_explored(
                            goal_title=str(goal_dict.get("title") or ""),
                            goal_description=str(goal_dict.get("description") or ""),
                            related_key=goal_dict.get("related_key"),
                            portfolio_key=exp_out.get("interest_portfolio_key"),
                            domain=exp_out.get("exploration_domain"),
                        )
                        if ip_upd is not None:
                            updated_outcome["interest_portfolio_updated"] = {
                                "key": ip_upd.get("key"),
                                "domain": ip_upd.get("domain"),
                                "touched_count": ip_upd.get("touched_count"),
                                "last_explored_at": ip_upd.get("last_explored_at"),
                            }
                except Exception as exc:
                    logger.warning("finalizer.exploration_topic_degraded", execution_id=exec_key, error=str(exc))

            if playbook_evolution or "interest_portfolio_updated" in updated_outcome:
                if playbook_evolution:
                    updated_outcome["playbook_evolution"] = playbook_evolution
                await self.repo.update_task(task_uuid, outcome=updated_outcome)

            # V4.2: Emergent multi-hop investigation progression, feed subscription & stall bounds
            investigation_outcome: dict[str, Any] | None = None
            inv_upd = eval_res.investigation_update if isinstance(eval_res.investigation_update, dict) else {}
            exp_out_inv = goal_dict.get("expected_outcome") or {}
            rel_key_str = str(goal_dict.get("related_key") or "").strip()
            inv_thread_key = str(
                exp_out_inv.get("investigation_thread_key")
                or inv_upd.get("thread_key")
                or (rel_key_str if rel_key_str.lower().startswith("inv:") else "")
            ).strip()

            registered_source_id: str | None = None
            if (
                eval_res.goal_achieved
                and verification_passed
                and isinstance(inv_upd.get("register_feed_source"), dict)
                and self.world_pipeline is not None
            ):
                req = inv_upd["register_feed_source"]
                req_sid = str(req.get("source_id") or "").strip()[:80]
                req_type = str(req.get("sensor_type") or "rss").strip().lower()
                req_url = str(req.get("url") or "").strip()
                req_name = str(req.get("name") or req_sid).strip()[:160]
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
                        registered_source_id = reg_src["source_id"]
                        await self.repo.log_event(
                            "world.source_registered_emergent",
                            message=f"Investigation registered public feed source '{registered_source_id}'",
                            payload={"source_id": registered_source_id, "url": req_url, "thread_key": inv_thread_key},
                            cycle_id=cycle_uuid,
                            task_id=task_uuid,
                        )
                    except Exception as exc:
                        logger.warning("finalizer.emergent_feed_register_rejected", source_id=req_sid, error=str(exc))

            if self.investigation_manager is not None and inv_thread_key:
                try:
                    cryst_skey = skill_crystallized.get("skill_key") if isinstance(skill_crystallized, dict) else None
                    pb_dom = (playbook_evolution.get("revision") or {}).get("domain") if isinstance(playbook_evolution, dict) else None
                    investigation_outcome = await self.investigation_manager.record_hop_outcome(
                        thread_key=inv_thread_key,
                        verification_passed=verification_passed,
                        goal_achieved=eval_res.goal_achieved,
                        measured_ig=measured_info_gain,
                        update_spec=inv_upd or None,
                        registered_source_id=registered_source_id,
                        skill_key=cryst_skey,
                        playbook_domain=pb_dom,
                        cycle_id=cycle_uuid,
                    )
                    if investigation_outcome is not None:
                        updated_outcome["investigation_outcome"] = {
                            "thread_key": investigation_outcome.get("thread_key"),
                            "stage": investigation_outcome.get("stage"),
                            "hops_taken": investigation_outcome.get("hops_taken"),
                            "stop_reason": investigation_outcome.get("stop_reason"),
                        }
                        await self.repo.update_task(task_uuid, outcome=updated_outcome)
                except Exception as exc:
                    logger.warning("finalizer.investigation_update_degraded", execution_id=exec_key, error=str(exc))

            # V4.2.2: Automatically populate discovered_channel map from verified high-IG web_fetch_readonly prefetches
            if (
                self.investigation_manager is not None
                and eval_res.goal_achieved
                and verification_passed
                and measured_info_gain >= 0.30
            ):
                seen_ch_urls: set[str] = set()
                for raw_call in plan_dict.get("mcp_prefetch_calls") or []:
                    if not isinstance(raw_call, dict) or raw_call.get("tool_name") != "web_fetch_readonly":
                        continue
                    raw_args = raw_call.get("arguments")
                    if not isinstance(raw_args, dict):
                        raw_args = raw_call.get("args")
                    ch_url = str((raw_args or {}).get("url") or "").strip()
                    if not ch_url or ch_url in seen_ch_urls:
                        continue
                    seen_ch_urls.add(ch_url)
                    try:
                        await self.investigation_manager.record_discovered_channel(
                            {
                                "entry_url": ch_url,
                                "notes": (
                                    f"Verified high-IG ({measured_info_gain:.2f}) source for: "
                                    f"{str(goal_dict.get('title') or '')[:120]}"
                                ),
                            },
                            source=f"prefetch:{exec_key}",
                        )
                    except Exception as exc:
                        logger.warning("finalizer.discovered_channel_degraded", url=ch_url, error=str(exc))

            # V4.1: Rate-limited proactive human outreach after evaluation (blocker ask or verified milestone)
            if self.human_bridge is not None and isinstance(eval_res.message_to_human, dict):
                try:
                    m = eval_res.message_to_human
                    m_kind = str(m.get("kind") or "milestone_report").strip().lower()
                    allow_out = (m_kind == "ask_human") or (eval_res.goal_achieved and verification_passed)
                    if allow_out and str(m.get("message") or "").strip():
                        await self.human_bridge.maybe_send_outreach(
                            kind=m_kind,
                            topic_key=str(m.get("topic_key") or m.get("title") or goal_dict.get("related_key") or "eval"),
                            title=str(m.get("title") or goal_dict.get("title") or "AOC Report")[:160],
                            message=str(m.get("message") or ""),
                            reason=str(m.get("reason") or ""),
                            expected_benefit=str(m.get("expected_benefit") or ""),
                            options=m.get("options") if isinstance(m.get("options"), list) else None,
                            cycle_id=cycle_uuid,
                        )
                except Exception as exc:
                    logger.warning("finalizer.human_outreach_degraded", execution_id=exec_key, error=str(exc))

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
            arb_cfg = (getattr(self.drives, "config", None) or {}).get("arbitration") or {}
            post_goal_satiation = bool(arb_cfg.get("post_goal_satiation_enabled", False))
            if post_goal_satiation and eval_res.goal_achieved and verification_passed:
                goal_drive = str(goal_dict.get("drive_type") or selected_drive or "").strip().lower()
                if goal_drive in post_drives and goal_drive != DriveType.SURVIVAL.value:
                    # Verified goal completion yields bounded satiation/inhibition so a saturated
                    # gap signal does not lock one non-emergency drive permanently across cycles.
                    post_drives[goal_drive] = post_drives[goal_drive].model_copy(
                        update={
                            "last_satisfied_at": now,
                            "persistence": round(post_drives[goal_drive].persistence * 0.6, 6),
                        }
                    )
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
                # V3.0 M1 + V3.1: persist deterministic verified_ig, cooldown, and prediction_error calibration
                verified_ig_val = round(float(measured_info_gain), 4) if eval_res.goal_achieved else 0.0
                cooldown_at = compute_cooldown_until(
                    now=now,
                    goal_achieved=eval_res.goal_achieved,
                    verified_ig=verified_ig_val,
                    failure_streak=1 if not eval_res.goal_achieved else 0,
                )
                await self.repo.update_goal(goal_uuid, verified_ig=verified_ig_val, cooldown_until=cooldown_at)

            # V3.1: Prediction Error calibration (verified_ig - predicted_ig -> feeds Affect surprise!)
            predicted_ig_raw = (goal_row or {}).get("predicted_ig")
            verified_ig_final = round(float(measured_info_gain), 4) if eval_res.goal_achieved else 0.0
            prediction_calibration: dict[str, Any] | None = None
            if predicted_ig_raw is not None:
                pred_val = round(float(predicted_ig_raw), 4)
                pred_err = round(verified_ig_final - pred_val, 4)
                prediction_calibration = {
                    "predicted_ig": pred_val,
                    "verified_ig": verified_ig_final,
                    "prediction_error": pred_err,
                }
                if self._on_prediction_error is not None:
                    self._on_prediction_error(pred_err)

            eval_payload = {
                **eval_res.model_dump(mode="json"),
                **expected_vs_actual,
                "memory_writes": memory_writes,
                "skill_crystallized": skill_crystallized,
                "playbook_evolution": playbook_evolution or None,
                "investigation_outcome": updated_outcome.get("investigation_outcome"),
                "prediction_calibration": prediction_calibration,
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

    @staticmethod
    def _parse_utc_dt(raw: Any) -> datetime | None:
        if isinstance(raw, datetime):
            return raw.replace(tzinfo=timezone.utc) if raw.tzinfo is None else raw.astimezone(timezone.utc)
        if isinstance(raw, str) and raw.strip():
            try:
                dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
                return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
            except ValueError:
                return None
        return None

    async def _collect_reproduction_quality_telemetry(
        self,
        *,
        now: datetime,
        since_7d: datetime,
        recent_statuses: list[str],
    ) -> tuple[float | None, float | None, float | None, int, float]:
        """Collect reproduction quality metrics identically to pre-cycle evaluation (V5.2.2 + V5.3 M3)."""
        skill_reuse_ratio_recent: float | None = None
        execution_reliability_recent: float | None = None
        playbook_freshness_score: float | None = None
        pending_skill_refactor_count = 0
        unresolved_pain_score = 0.0
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
                    if (self._parse_utc_dt(iv.get("created_at")) or since_7d) < since_7d:
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
                self._parse_utc_dt((r.get("value") or {}).get("updated_at"))
                or self._parse_utc_dt(r.get("updated_at"))
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
            logger.warning("finalizer.reproduction_quality_telemetry_degraded", error=str(exc))

        return (
            skill_reuse_ratio_recent,
            execution_reliability_recent,
            playbook_freshness_score,
            pending_skill_refactor_count,
            unresolved_pain_score,
        )

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
        if self.investigation_manager is not None:
            try:
                seen_topics = {g.topic.lower() for g in env_topics}
                for g in await self.investigation_manager.pending_leads_as_knowledge_gaps(limit=3):
                    if g.topic.lower() not in seen_topics:
                        env_topics.append(g)
                        seen_topics.add(g.topic.lower())
            except Exception as exc:
                logger.warning("finalizer.investigation_post_gaps_degraded", error=str(exc))
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

        if self.self_model is not None:
            acquired_caps, validated_caps = await self.self_model.get_evidence_backed_capabilities()
        else:
            acquired_caps = list(env.get("capabilities_acquired") or [])
            validated_caps = []
        validated_skill_count: int | None = None
        if self.skill_registry is not None:
            validated_skill_count = await self.skill_registry.count_validated_skills(verify_integrity=True)

        quality_enabled = bool(self.reproduction_quality_scoring_enabled)
        skill_reuse_ratio_recent: float | None = None
        execution_reliability_recent: float | None = None
        playbook_freshness_score: float | None = None
        pending_skill_refactor_count = 0
        unresolved_pain_score = 0.0
        if quality_enabled:
            (
                skill_reuse_ratio_recent,
                execution_reliability_recent,
                playbook_freshness_score,
                pending_skill_refactor_count,
                unresolved_pain_score,
            ) = await self._collect_reproduction_quality_telemetry(
                now=now,
                since_7d=since_7d,
                recent_statuses=recent_statuses,
            )

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
            reproduction_quality_scoring_enabled=quality_enabled,
            skill_reuse_ratio_recent=skill_reuse_ratio_recent,
            execution_reliability_recent=execution_reliability_recent,
            playbook_freshness_score=playbook_freshness_score,
            pending_skill_refactor_count=pending_skill_refactor_count,
            unresolved_pain_score=unresolved_pain_score,
            open_knowledge_gaps=env_topics,
            unknown_topics=env_topics,
            exploration_attempted_recent=cur_done + cur_failed,
            exploration_succeeded_recent=cur_done,
            new_knowledge_recent=sem_recent,
            last_learning_at=last_learning_at,
            last_information_gain=last_info_gain,
            last_information_gain_at=last_learning_at,
        )
