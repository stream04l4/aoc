"""V5.1 M2 tests: REPLY_LATER Continuity & Cap, 24/7 Proactive Backoff & Reset,
Unified Memory Promotion Anti-Pollution, Gentle Goal Adoption, and Blank-Slate Isolation.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.agents.registry import AgentRegistry
from app.cognition.interfaces import (
    ActionPlan,
    AssessmentResult,
    BrainInterface,
    CognitiveContext,
    EvaluationContext,
    EvaluationResult,
    GoalProposal,
    SocialContext,
    SocialTurnDecision,
)
from app.core.config import (
    ApiSettings,
    DatabaseSettings,
    EmbeddingSettings,
    GovernanceSettings,
    LLMSettings,
    MemorySettings,
    PerceptionSettings,
    RunnerSettings,
    Settings,
    SocialSettings,
    TelegramSettings,
)
from app.core.enums import DriveType, MemoryType, TaskStatus
from app.core.runtime import OrganismRuntime
from app.drives.interfaces import DriveState, KnowledgeGap
from app.evolution.blank_slate import BlankSlateVerifier
from app.governance.approval import ApprovalService
from app.governance.policy import GovernanceManager
from app.memory.embedding import EmbeddingService, build_embedding_provider
from app.memory.experience_manager import ExperienceManager
from app.memory.interfaces import MemoryItem
from app.memory.retrieval import HybridRetrievalEngine
from app.notifications.human_bridge import HumanBridge
from app.notifications.telegram import TelegramNotifier, TelegramPoller, TelegramWebhookHandler
from app.orchestration.finalizer import FinalizationResult
from app.orchestration.orchestrator import AgentOrchestrator
from app.perception.interfaces import Observation, utcnow
from app.storage.database import Database
from app.storage.models import Base
from app.storage.repository import StateRepository


async def _init_db(db: Database) -> None:
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _healthy_llm_probe() -> dict[str, Any]:
    return {"available": True, "latency_ms": 3.0, "model": "stub-m2"}


class _M2ScriptBrain(BrainInterface):
    def __init__(
        self,
        converse_script: list[SocialTurnDecision],
        *,
        goal_proposals: list[GoalProposal] | None = None,
    ) -> None:
        self.converse_script = list(converse_script)
        self.goal_proposals = list(goal_proposals or [])
        self.converse_contexts: list[SocialContext] = []
        self.goal_contexts: list[CognitiveContext] = []

    async def assess(self, observation: Observation, memories: list[dict], drives: list[DriveState]) -> AssessmentResult:
        return AssessmentResult(
            summary="assess ok",
            knowledge_gaps=[KnowledgeGap(topic="gap_m2", importance=0.8)],
            confidence=0.9,
        )

    async def generate_goal(self, context: CognitiveContext) -> GoalProposal:
        self.goal_contexts.append(context)
        if self.goal_proposals:
            return self.goal_proposals.pop(0)
        return GoalProposal(should_act=False, rationale="skip")

    async def plan(self, context: CognitiveContext) -> ActionPlan:
        return ActionPlan(should_execute=False, rationale="skip")

    async def evaluate(self, context: EvaluationContext) -> EvaluationResult:
        return EvaluationResult(goal_achieved=True, summary="ok")

    async def converse(self, context: SocialContext) -> SocialTurnDecision:
        self.converse_contexts.append(context)
        return self.converse_script.pop(0)


@pytest.mark.asyncio
async def test_v5_1_m2_reply_later_continuity_context_and_defer_cap(tmp_path: Path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'm2_defer.db'}")
    await _init_db(db)
    tg_calls: list[tuple[str, dict[str, Any]]] = []

    def _mock_tg(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        tg_calls.append((method, body))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 200 + len(tg_calls)}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_mock_tg)) as client:
        try:
            script = [
                SocialTurnDecision(
                    internal_monologue="正盯着后台任务呢，先拖 10 分钟。",
                    action="REPLY_LATER",
                    defer_minutes=10,
                    defer_reason="在等一个运行中任务",
                ),
                SocialTurnDecision(
                    internal_monologue="还是忙，想再拖一次（第2次，允许）。",
                    action="REPLY_LATER",
                    defer_minutes=5,
                    defer_reason="还没查完",
                ),
                SocialTurnDecision(
                    internal_monologue="还想再拖一次（超过 max_defer_count=2，应自动降级 IGNORE）。",
                    action="REPLY_LATER",
                    defer_minutes=5,
                    defer_reason="第3次拖延",
                ),
            ]
            brain = _M2ScriptBrain(script)
            settings = Settings(
                api=ApiSettings(host="127.0.0.1", auth_enabled=False),
                database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path / 'm2_defer.db'}"),
                memory=MemorySettings(provider="none", embedding_dim=512),
                embedding=EmbeddingSettings(provider="hash_fallback", dimensions=512),
                governance=GovernanceSettings(workspace_root=str(tmp_path / "ws")),
                perception=PerceptionSettings(llm_probe=False),
                runner=RunnerSettings(enabled=False),
                telegram=TelegramSettings(enabled=True, bot_token=SecretStr("123:ABC_DEF_12345"), allowed_chat_ids=["777"]),
                social=SocialSettings(enabled=True, max_defer_count=2, proactive_enabled=False),
            )
            rt = OrganismRuntime(settings=settings, db=db, brain=brain, auto_load_agents=False, llm_probe_fn=_healthy_llm_probe)
            rt.telegram_notifier._http_client = client
            await rt.start(start_scheduler=False)

            await rt.repo.create_task(
                id=uuid.uuid4(),
                objective="调试并发调度死锁定位",
                description="",
                assigned_agent="codex",
                expected_output="ok",
                workspace=str(tmp_path / "ws"),
                timeout_seconds=120,
                permission_level="L0",
                success_criteria=[],
                verification=[],
                status=TaskStatus.RUNNING.value,
            )
            await rt.repo.upsert_world_item(
                kind="human_inbox",
                key="tg:777:301",
                value={"chat_id": "777", "sender": "joel", "text": "查得咋样了？", "message_id": 301, "received_at": utcnow().isoformat()},
                status="open",
                source="telegram:joel",
            )

            # Cycle 1 -> REPLY_LATER #1 stores deferred_activity & defer_count=1
            c1 = await rt.loop.run_cycle()
            assert c1["state_changes"]["social_reflection"]["action"] == "REPLY_LATER"
            open_defs = await rt.repo.list_world_items(kind="social_deferred_reply", status="open")
            assert len(open_defs) == 1
            assert open_defs[0]["value"]["defer_count"] == 1
            assert "调试并发调度死锁定位" in open_defs[0]["value"]["deferred_activity"]

            # Backdate due_at + created_at -> Cycle 2 receives elapsed_minutes, defer_count=1, deferred_activity
            past_created = (utcnow() - timedelta(minutes=12)).isoformat()
            await rt.repo.upsert_world_item(
                kind="social_deferred_reply",
                key=open_defs[0]["key"],
                value={"created_at": past_created, "due_at": (utcnow() - timedelta(minutes=1)).isoformat()},
                status="open",
            )
            c2 = await rt.loop.run_cycle()
            assert c2["state_changes"]["social_reflection"]["action"] == "REPLY_LATER"
            assert c2["state_changes"]["social_reflection"]["defer_cap_reached"] is False
            ctx2 = brain.converse_contexts[-1]
            assert ctx2.due_deferred_replies[0]["defer_count"] == 1
            assert ctx2.due_deferred_replies[0]["elapsed_minutes"] >= 11
            assert "调试并发调度死锁定位" in ctx2.due_deferred_replies[0]["deferred_activity"]

            # Second deferred row has defer_count=2
            open_defs2 = await rt.repo.list_world_items(kind="social_deferred_reply", status="open")
            assert len(open_defs2) == 1 and open_defs2[0]["value"]["defer_count"] == 2
            await rt.repo.upsert_world_item(
                kind="social_deferred_reply",
                key=open_defs2[0]["key"],
                value={"due_at": (utcnow() - timedelta(minutes=1)).isoformat()},
                status="open",
            )

            # Cycle 3: brain tries REPLY_LATER again (defer_count would be 3 > max_defer_count=2) -> coerced to IGNORE!
            c3 = await rt.loop.run_cycle()
            assert c3["state_changes"]["social_reflection"]["action"] == "IGNORE"
            assert c3["state_changes"]["social_reflection"]["defer_cap_reached"] is True
            assert await rt.repo.list_world_items(kind="social_deferred_reply", status="open") == []
            assert tg_calls == []
        finally:
            await db.dispose()


@pytest.mark.asyncio
async def test_v5_1_m2_proactive_milestone_affect_exponential_backoff_and_reset(tmp_path: Path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'm2_proactive.db'}")
    await _init_db(db)
    tg_calls: list[tuple[str, dict[str, Any]]] = []

    def _mock_tg(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        if method == "sendMessage":
            tg_calls.append((method, body))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 600 + len(tg_calls)}})
        return httpx.Response(200, json={"ok": True, "result": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_mock_tg)) as client:
        try:
            script = [
                # 1. Milestone proactive -> REPLY_NOW (ignored streak 0 -> 1, next backoff 20m)
                SocialTurnDecision(
                    internal_monologue="刚沉淀完新技能，忍不住想跟 Joel 说一声。",
                    action="REPLY_NOW",
                    reply_text="刚把那个日志分析小工具做成可复用技能了，跑起来挺顺。",
                ),
                # 2. High valence+arousal proactive -> REPLY_NOW (streak 1 -> 2, next backoff 40m)
                SocialTurnDecision(
                    internal_monologue="刚有点小突破情绪很亮，分享一句。",
                    action="REPLY_NOW",
                    reply_text="又踩通一个规律，感觉今天状态不错。",
                ),
                # 3. High frustration proactive -> REPLY_NOW (streak 2 -> 3, next backoff 80m)
                SocialTurnDecision(
                    internal_monologue="连续卡住了有点烦，吐槽一句。",
                    action="REPLY_NOW",
                    reply_text="这破接口连报错两次，看得我有点火大。",
                ),
                # 4. High boredom proactive -> brain decides IGNORE (should NOT bump consecutive_ignored_proactive!)
                SocialTurnDecision(
                    internal_monologue="虽然有点闲，但没啥好说的，还是算了。",
                    action="IGNORE",
                ),
            ]
            brain = _M2ScriptBrain(script)
            settings = Settings(
                api=ApiSettings(host="127.0.0.1", auth_enabled=False),
                database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path / 'm2_proactive.db'}"),
                memory=MemorySettings(provider="none", embedding_dim=512),
                embedding=EmbeddingSettings(provider="hash_fallback", dimensions=512),
                governance=GovernanceSettings(workspace_root=str(tmp_path / "ws")),
                perception=PerceptionSettings(llm_probe=False),
                runner=RunnerSettings(enabled=False),
                telegram=TelegramSettings(enabled=True, polling_enabled=True, bot_token=SecretStr("123:TOKEN_XYZ_999"), allowed_chat_ids=["777"]),
                social=SocialSettings(
                    enabled=True,
                    proactive_enabled=True,
                    min_proactive_interval_minutes=10,
                    proactive_backoff_multiplier=2.0,
                    proactive_max_backoff_minutes=100,
                ),
            )
            rt = OrganismRuntime(settings=settings, db=db, brain=brain, auto_load_agents=False, llm_probe_fn=_healthy_llm_probe)
            rt.telegram_notifier._http_client = client
            await rt.start(start_scheduler=False)

            # 1. Finalizer milestone hook writes pending_milestone cleanly
            fin_res = FinalizationResult(
                execution_id=str(uuid.uuid4()),
                task_id=str(uuid.uuid4()),
                already_finalized=False,
                goal_achieved=True,
                task_status="SUCCEEDED",
                evaluation={"skill_crystallized": {"skill_key": "log_analyzer", "canonical_name": "日志分析技能"}, "information_gain": 0.7},
            )
            await rt.loop._record_social_milestone_from_finalizer(fin_res)

            c1 = await rt.loop.run_cycle()
            assert c1["state_changes"]["social_reflection"]["trigger_source"] == "proactive_impulse"
            assert c1["state_changes"]["social_reflection"]["proactive_kind"] == "milestone"
            assert c1["state_changes"]["social_reflection"]["action"] == "REPLY_NOW"
            assert len(tg_calls) == 1 and "日志分析小工具" in tg_calls[0][1]["text"]

            rhythm1 = (await rt.repo.list_world_items(kind="social_rhythm_state"))[0]["value"]
            assert rhythm1["consecutive_ignored_proactive"] == 1
            assert rhythm1["current_backoff_minutes"] == pytest.approx(20.0)
            assert rhythm1["pending_milestone"] is None

            # 2. Immediate next cycle with high affect stays SILENT (0 extra LLM calls!) because 20m backoff active
            rt.affect._state = rt.affect.state.model_copy(update={"valence": 0.95, "arousal": 0.90})
            c_cool = await rt.loop.run_cycle()
            assert c_cool["state_changes"]["social_reflection"]["triggered"] is False
            assert len(brain.converse_contexts) == 1

            # Backdate 15m (past 10m base_min, BEFORE 20m backoff): STILL suppressed even while valence>=0.55!
            t_15m = (utcnow() - timedelta(minutes=15)).isoformat()
            await rt.repo.upsert_world_item(
                kind="social_rhythm_state",
                key="primary",
                value={"last_proactive_at": t_15m, "last_proactive_evaluated_at": t_15m, "last_reply_at": t_15m},
                status="active",
            )
            c_still_cool = await rt.loop.run_cycle()
            assert c_still_cool["state_changes"]["social_reflection"]["triggered"] is False

            # Backdate 22m (> 20m backoff): triggers excitement proactive -> streak 2, backoff=40m
            t_22m = (utcnow() - timedelta(minutes=22)).isoformat()
            await rt.repo.upsert_world_item(
                kind="social_rhythm_state",
                key="primary",
                value={"last_proactive_at": t_22m, "last_proactive_evaluated_at": t_22m, "last_reply_at": t_22m},
                status="active",
            )
            c2 = await rt.loop.run_cycle()
            assert c2["state_changes"]["social_reflection"]["proactive_kind"] == "excitement"
            rhythm2 = (await rt.repo.list_world_items(kind="social_rhythm_state"))[0]["value"]
            assert rhythm2["consecutive_ignored_proactive"] == 2
            assert rhythm2["current_backoff_minutes"] == pytest.approx(40.0)

            # Backdate 30m (< 40m backoff): suppressed even with high frustration! Backdate 42m (> 40m): triggers frustration -> streak 3, backoff=80m
            rt.affect._state = rt.affect.state.model_copy(update={"valence": 0.0, "arousal": 0.3, "frustration": 0.95})
            t_30m = (utcnow() - timedelta(minutes=30)).isoformat()
            await rt.repo.upsert_world_item(
                kind="social_rhythm_state",
                key="primary",
                value={"last_proactive_at": t_30m, "last_proactive_evaluated_at": t_30m, "last_reply_at": t_30m},
                status="active",
            )
            assert (await rt.loop.run_cycle())["state_changes"]["social_reflection"]["triggered"] is False

            t_42m = (utcnow() - timedelta(minutes=42)).isoformat()
            await rt.repo.upsert_world_item(
                kind="social_rhythm_state",
                key="primary",
                value={"last_proactive_at": t_42m, "last_proactive_evaluated_at": t_42m, "last_reply_at": t_42m},
                status="active",
            )
            c3 = await rt.loop.run_cycle()
            assert c3["state_changes"]["social_reflection"]["proactive_kind"] == "frustration"
            rhythm3 = (await rt.repo.list_world_items(kind="social_rhythm_state"))[0]["value"]
            assert rhythm3["consecutive_ignored_proactive"] == 3
            assert rhythm3["current_backoff_minutes"] == pytest.approx(80.0)

            # 4. Boredom impulse where Brain says IGNORE: does NOT increment streak, still updates evaluated_at
            rt.affect._state = rt.affect.state.model_copy(update={"frustration": 0.0, "boredom": 0.90})
            t_85m = (utcnow() - timedelta(minutes=85)).isoformat()
            await rt.repo.upsert_world_item(
                kind="social_rhythm_state",
                key="primary",
                value={"last_proactive_at": t_85m, "last_proactive_evaluated_at": t_85m, "last_reply_at": t_85m},
                status="active",
            )
            c4 = await rt.loop.run_cycle()
            assert c4["state_changes"]["social_reflection"]["proactive_kind"] == "boredom"
            assert c4["state_changes"]["social_reflection"]["action"] == "IGNORE"
            rhythm4 = (await rt.repo.list_world_items(kind="social_rhythm_state"))[0]["value"]
            assert rhythm4["consecutive_ignored_proactive"] == 3

            # 5. Human reply via TelegramPoller immediately resets streak=0 and backoff=None
            gov = GovernanceManager(GovernanceSettings(workspace_root=str(tmp_path / "ws")), rt.repo)
            orch = AgentOrchestrator(registry=AgentRegistry(), governance=gov, repo=rt.repo)
            hb = HumanBridge(repo=rt.repo, enabled=True)
            wh = TelegramWebhookHandler(settings.telegram, ApprovalService(rt.repo, orch), rt.repo, notifier=rt.telegram_notifier, human_bridge=hb, social_settings=settings.social)
            poller = TelegramPoller(
                settings=settings.telegram,
                notifier=rt.telegram_notifier,
                webhook_handler=wh,
                human_bridge=hb,
                repo=rt.repo,
                lifecycle=rt.lifecycle,
                social_settings=settings.social,
            )
            await poller.handle_polled_update(
                {"update_id": 700, "message": {"message_id": 90, "from": {"id": 888, "username": "joel"}, "chat": {"id": 777}, "text": "看到了，加油"}}
            )
            rhythm5 = (await rt.repo.list_world_items(kind="social_rhythm_state"))[0]["value"]
            assert rhythm5["consecutive_ignored_proactive"] == 0
            assert rhythm5["current_backoff_minutes"] is None
        finally:
            await db.dispose()


@pytest.mark.asyncio
async def test_v5_1_m2_unified_memory_anti_pollution_and_goal_hint_fusion(tmp_path: Path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'm2_mem_goal.db'}")
    await _init_db(db)
    try:
        repo = StateRepository(db)
        emb_cfg = EmbeddingSettings(provider="hash_fallback", dimensions=128)
        emb = EmbeddingService(repo, build_embedding_provider(emb_cfg))
        ret = HybridRetrievalEngine(repo, emb)
        mem = ExperienceManager(repo, backend=None, embedding_service=emb, retrieval_engine=ret, outbox_enabled=False, embed_on_write=True)

        # Seed core technical memory first
        await mem.remember(
            MemoryItem(
                memory_type=MemoryType.SEMANTIC.value,
                title="PostgreSQL SKIP LOCKED Outbox Dispatch Architecture",
                content="PostgreSQL Outbox uses SELECT FOR UPDATE SKIP LOCKED and idempotency keys to guarantee reliable asynchronous task finalization.",
                source="system:research",
                importance=0.92,
                tags=["postgres", "outbox", "architecture"],
            )
        )

        script = [
            # Turn 1: Routine small talk -> should_remember=False (MUST NOT write to memory_index)
            SocialTurnDecision(
                internal_monologue="日常打招呼，不用存长时记忆。",
                action="REPLY_NOW",
                reply_text="嗯，刚缓口气。",
                should_remember=False,
                memory_summary="Joel 打招呼",
                memory_importance=0.2,
            ),
            # Turn 2: High-value preference + conversational goal hint -> promoted EPISODIC (capped <= 0.72)
            SocialTurnDecision(
                internal_monologue="Joel 明确要求我平时少讲抽象术语，而且建议看看 vLLM prefix caching。",
                action="REPLY_NOW",
                reply_text="行，后面汇报我都直接说大白话。vLLM cache 这块我也记下来了，找空去看看。",
                should_remember=True,
                memory_summary="Joel 明确偏好：沟通汇报尽量使用简洁通俗大白话，避免堆砌抽象底层术语。",
                memory_importance=0.95,  # intentionally exceeds 0.72 cap -> must clamp!
                adopt_as_goal=True,
                adopted_goal_hint="调研 vLLM prefix caching 对本地 27B 吞吐的优化机制",
            ),
        ]
        brain = _M2ScriptBrain(script, goal_proposals=[GoalProposal(should_act=False, rationale="done")])
        settings = Settings(
            api=ApiSettings(host="127.0.0.1", auth_enabled=False),
            database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path / 'm2_mem_goal.db'}"),
            memory=MemorySettings(provider="none", embedding_dim=128),
            embedding=EmbeddingSettings(provider="hash_fallback", dimensions=128),
            governance=GovernanceSettings(workspace_root=str(tmp_path / "ws")),
            perception=PerceptionSettings(llm_probe=False),
            runner=RunnerSettings(enabled=False),
            social=SocialSettings(enabled=True, proactive_enabled=False, memory_promotion_min_importance=0.65, social_memory_max_importance=0.72),
        )
        rt = OrganismRuntime(settings=settings, db=db, brain=brain, auto_load_agents=False, llm_probe_fn=_healthy_llm_probe)
        rt.memory = mem
        await rt.start(start_scheduler=False)
        rt.loop.memory = mem

        # Cycle 1: routine banter -> zero new rows in memory_index
        await rt.repo.upsert_world_item(
            kind="human_inbox",
            key="tg:777:401",
            value={"chat_id": "777", "sender": "joel", "text": "吃了吗", "message_id": 401, "received_at": utcnow().isoformat()},
            status="open",
        )
        c1 = await rt.loop.run_cycle()
        assert c1["state_changes"]["social_reflection"]["memory_promoted"] is False
        assert not any(m.get("source") == "dialogue:telegram" for m in await repo.list_memory_index(limit=50))

        # Cycle 2: high-value preference + goal hint -> EPISODIC memory stored capped at 0.72, AND passed to GOAL_GENERATION
        await rt.repo.upsert_world_item(
            kind="human_inbox",
            key="tg:777:402",
            value={"chat_id": "777", "sender": "joel", "text": "以后跟你说事少堆术语，顺便有空研究下 vLLM prefix caching。", "message_id": 402, "received_at": utcnow().isoformat()},
            status="open",
        )
        c2 = await rt.loop.run_cycle()
        sr2 = c2["state_changes"]["social_reflection"]
        assert sr2["memory_promoted"] is True
        assert sr2["memory_importance"] == pytest.approx(0.72)

        ep_rows = await repo.list_memory_index(memory_type=MemoryType.EPISODIC.value)
        dlg_rows = [m for m in ep_rows if m["source"] == "dialogue:telegram"]
        assert len(dlg_rows) == 1
        assert float(dlg_rows[0]["importance"]) <= 0.72

        # Anti-pollution check: technical query ranks technical memory #1; social preference query recalls dialogue memory!
        tech_hits = await ret.search(query="PostgreSQL Outbox SKIP LOCKED architecture", limit=5)
        assert tech_hits and tech_hits[0].item.source == "system:research"
        social_hits = await ret.search(query="Joel 沟通偏好 大白话 术语", limit=5)
        assert social_hits and any(h.item.source == "dialogue:telegram" for h in social_hits)

        # Verify social_goal_hints was delivered gently into GOAL_GENERATION & cleared afterwards
        assert len(brain.goal_contexts) >= 1
        assert any("vLLM prefix caching" in str(h.get("hint")) for h in brain.goal_contexts[-1].social_goal_hints)
        rhythm_after = (await repo.list_world_items(kind="social_rhythm_state"))[0]["value"]
        assert rhythm_after.get("pending_goal_hints") == []
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_v5_1_m2_blank_slate_verifier_blocks_all_social_artifacts(tmp_path: Path):
    verifier = BlankSlateVerifier()

    # 1. Each of the 6 V5.1 non-inheritable world_states kinds MUST cause BlankSlateVerifier to fail
    social_world_kinds = (
        "human_inbox",
        "dialogue_turn",
        "social_deferred_reply",
        "social_rhythm_state",
        "human_outreach_card",
        "telegram_pending_reply",
    )
    for kind_name in social_world_kinds:
        db_k = Database(f"sqlite+aiosqlite:///{tmp_path / f'bs_{kind_name}.db'}")
        await _init_db(db_k)
        try:
            repo = StateRepository(db_k)
            await repo.get_or_create_identity("AOC-GEN1", "1.0.0", {})
            await repo.upsert_world_item(kind=kind_name, key="leaked_item", value={"text": "parent private"}, status="open")
            rep = await verifier.verify(db_k, instance_key="gen1-leak", expected_schema="aoc_inst_leak")
            assert rep.passed is False, f"{kind_name} should fail blank-slate verification"
            failed = {c.name for c in rep.checks if not c.passed}
            assert "world_states_non_inheritable_zero" in failed
        finally:
            await db_k.dispose()

    # 2. Personal dialogue episodic memory (source="dialogue:telegram") MUST also fail blank-slate check
    db_mem = Database(f"sqlite+aiosqlite:///{tmp_path / 'bs_dialogue_mem.db'}")
    await _init_db(db_mem)
    try:
        repo = StateRepository(db_mem)
        await repo.get_or_create_identity("AOC-GEN1", "1.0.0", {})
        await repo.add_memory_index(
            memory_type="episodic",
            title="Joel chat",
            summary="parent personal memory with Joel",
            content_hash="bs-dialogue-hash-1",
            source="dialogue:telegram",
        )
        rep = await verifier.verify(db_mem, instance_key="gen1-mem-leak", expected_schema="aoc_inst_mem1")
        assert rep.passed is False
        failed = {c.name for c in rep.checks if not c.passed}
        assert {"memory_index_zero", "social_dialogue_memories_zero"} <= failed
    finally:
        await db_mem.dispose()


@pytest.mark.asyncio
async def test_v5_1_milestone_skill_dedup_and_unverified_rejection(tmp_path: Path):
    tg_calls: list[dict[str, Any]] = []

    def _mock_tg(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sendMessage"):
            body = json.loads(request.content.decode("utf-8")) if request.content else {}
            tg_calls.append(body)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 700 + len(tg_calls)}})
        return httpx.Response(200, json={"ok": True, "result": True})

    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'm2_milestone_dedup.db'}")
    await _init_db(db)
    async with httpx.AsyncClient(transport=httpx.MockTransport(_mock_tg)) as client:
        try:
            script = [
                SocialTurnDecision(
                    internal_monologue="这个技能刚验证通过，跟 Joel 简短说一声。",
                    action="REPLY_NOW",
                    reply_text="刚把 provenance checksum 检查沉淀成技能，验证过了。",
                ),
                SocialTurnDecision(
                    internal_monologue="另一个新技能验证过了，但现在不想打扰他，先记着吧。",
                    action="IGNORE",
                ),
            ]
            brain = _M2ScriptBrain(script)
            settings = Settings(
                api=ApiSettings(host="127.0.0.1", auth_enabled=False),
                database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path / 'm2_milestone_dedup.db'}"),
                memory=MemorySettings(provider="none", embedding_dim=512),
                embedding=EmbeddingSettings(provider="hash_fallback", dimensions=512),
                governance=GovernanceSettings(workspace_root=str(tmp_path / "ws")),
                perception=PerceptionSettings(llm_probe=False),
                runner=RunnerSettings(enabled=False),
                telegram=TelegramSettings(enabled=True, polling_enabled=True, bot_token=SecretStr("123:TOKEN_XYZ_999"), allowed_chat_ids=["777"]),
                social=SocialSettings(
                    enabled=True,
                    proactive_enabled=True,
                    min_proactive_interval_minutes=5,
                    milestone_dedup_hours=24,
                    proactive_high_ig_threshold=0.55,
                ),
            )
            rt = OrganismRuntime(settings=settings, db=db, brain=brain, auto_load_agents=False, llm_probe_fn=_healthy_llm_probe)
            rt.telegram_notifier._http_client = client
            await rt.start(start_scheduler=False)

            async def _get_rhythm() -> dict[str, Any]:
                rows = await rt.repo.list_world_items(kind="social_rhythm_state")
                return dict(rows[0]["value"] or {}) if rows else {}

            # 1. Unverified / unsafe / rolled-back / non-VALIDATED skill payloads MUST NOT create skill_crystallized milestones
            bad_skill_payloads = [
                {"crystallized": False, "rejected_unsafe": True, "violations": ["dynamic_exec"]},
                {"crystallized": False, "evolved": True, "rolled_back": True, "passed": False, "status": "DEPRECATED", "skill_key": "audit_checksum"},
                {"crystallized": True, "passed": True, "status": "DRAFT", "skill_key": "draft_skill"},
                {"crystallized": True, "passed": True, "awaiting_human_approval": True, "skill_key": "l2_skill"},
            ]
            for bad_payload in bad_skill_payloads:
                await rt.loop._record_social_milestone_from_finalizer(
                    FinalizationResult(
                        execution_id=str(uuid.uuid4()),
                        task_id=str(uuid.uuid4()),
                        already_finalized=False,
                        goal_achieved=False,
                        task_status="SUCCEEDED",
                        evaluation={"skill_crystallized": bad_payload, "information_gain": 0.2},
                    )
                )
            assert (await _get_rhythm()).get("pending_milestone") is None

            # 1b. Failed crystallization with goal_achieved=True and high IG falls back to high_ig_discovery cleanly
            await rt.loop._record_social_milestone_from_finalizer(
                FinalizationResult(
                    execution_id="exec-hi-1",
                    task_id=str(uuid.uuid4()),
                    already_finalized=False,
                    goal_achieved=True,
                    task_status="SUCCEEDED",
                    evaluation={
                        "skill_crystallized": bad_skill_payloads[0],
                        "information_gain": 0.85,
                        "summary": "发现关键配置漂移",
                    },
                )
            )
            ms_hi = (await _get_rhythm()).get("pending_milestone")
            assert ms_hi is not None and ms_hi["kind"] == "high_ig_discovery"
            await rt.repo.upsert_world_item(kind="social_rhythm_state", key="primary", value={"pending_milestone": None}, status="active")

            # 2. First verified crystallization sets pending_milestone; duplicate write while pending preserves original event_id
            await rt.loop._record_social_milestone_from_finalizer(
                FinalizationResult(
                    execution_id="exec-skill-1",
                    task_id=str(uuid.uuid4()),
                    already_finalized=False,
                    goal_achieved=True,
                    task_status="SUCCEEDED",
                    evaluation={
                        "skill_crystallized": {
                            "crystallized": True,
                            "skill_key": "audit_coding_provenance_checksum_check",
                            "canonical_name": "audit.coding.provenance_checksum_check",
                            "status": "VALIDATED",
                            "passed": True,
                        },
                        "information_gain": 0.65,
                    },
                )
            )
            ms1 = (await _get_rhythm())["pending_milestone"]
            assert ms1["event_id"] == "exec-skill-1"
            assert ms1["subject_key"] == "skill:audit.coding.provenance_checksum_check"

            await rt.loop._record_social_milestone_from_finalizer(
                FinalizationResult(
                    execution_id="exec-skill-1-dup",
                    task_id=str(uuid.uuid4()),
                    already_finalized=False,
                    goal_achieved=True,
                    task_status="SUCCEEDED",
                    evaluation={
                        "skill_crystallized": {
                            "crystallized": True,
                            "evolved": True,
                            "skill_key": "audit_coding_provenance_checksum_check",
                            "canonical_name": "audit.coding.provenance_checksum_check",
                            "status": "VALIDATED",
                            "passed": True,
                        },
                    },
                )
            )
            assert (await _get_rhythm())["pending_milestone"]["event_id"] == "exec-skill-1"

            # 3. Consume via REPLY_NOW -> clears pending_milestone and records subject cooldown
            c1 = await rt.loop.run_cycle()
            assert c1["state_changes"]["social_reflection"]["action"] == "REPLY_NOW"
            assert len(tg_calls) == 1
            rhythm_after_reply = await _get_rhythm()
            assert rhythm_after_reply["pending_milestone"] is None
            assert "skill:audit.coding.provenance_checksum_check" in rhythm_after_reply["recent_milestone_subjects"]

            # 4. Within cooldown, new execution evolving the SAME skill is suppressed at write-time
            await rt.loop._record_social_milestone_from_finalizer(
                FinalizationResult(
                    execution_id="exec-skill-2-evolved",
                    task_id=str(uuid.uuid4()),
                    already_finalized=False,
                    goal_achieved=True,
                    task_status="SUCCEEDED",
                    evaluation={
                        "skill_crystallized": {
                            "crystallized": True,
                            "evolved": True,
                            "promoted": True,
                            "version": "1.1.0",
                            "skill_key": "audit_coding_provenance_checksum_check",
                            "canonical_name": "audit.coding.provenance_checksum_check",
                            "status": "VALIDATED",
                            "passed": True,
                        },
                    },
                )
            )
            assert (await _get_rhythm()).get("pending_milestone") is None

            # 5. Legacy pending_milestone without subject_key is also suppressed at read-time during cooldown
            old_ts = (utcnow() - timedelta(minutes=20)).isoformat()
            await rt.repo.upsert_world_item(
                kind="social_rhythm_state",
                key="primary",
                value={
                    "last_proactive_at": old_ts,
                    "last_proactive_evaluated_at": old_ts,
                    "last_reply_at": old_ts,
                    "pending_milestone": {
                        "kind": "skill_crystallized",
                        "event_id": "legacy-exec-99",
                        "summary": "刚把「audit.coding.provenance_checksum_check」沉淀为可复用技能并验证通过",
                        "at": utcnow().isoformat(),
                    },
                },
                status="active",
            )
            rt.affect._state = rt.affect.state.model_copy(update={"valence": 0.1, "arousal": 0.1, "frustration": 0.0, "boredom": 0.0})
            c_suppressed = await rt.loop.run_cycle()
            assert c_suppressed["state_changes"]["social_reflection"]["triggered"] is False
            assert len(brain.converse_contexts) == 1

            # 6. A DIFFERENT skill triggers normally and IGNORE also clears pending_milestone + sets cooldown
            await rt.loop._record_social_milestone_from_finalizer(
                FinalizationResult(
                    execution_id="exec-other-skill-1",
                    task_id=str(uuid.uuid4()),
                    already_finalized=False,
                    goal_achieved=True,
                    task_status="SUCCEEDED",
                    evaluation={
                        "skill_crystallized": {
                            "crystallized": True,
                            "skill_key": "other_skill",
                            "canonical_name": "cognition.research.other_skill",
                            "status": "VALIDATED",
                            "passed": True,
                        },
                    },
                )
            )
            assert (await _get_rhythm())["pending_milestone"]["subject_key"] == "skill:cognition.research.other_skill"
            c_other = await rt.loop.run_cycle()
            assert c_other["state_changes"]["social_reflection"]["action"] == "IGNORE"
            rhythm_after_ignore = await _get_rhythm()
            assert rhythm_after_ignore["pending_milestone"] is None
            assert "skill:cognition.research.other_skill" in rhythm_after_ignore["recent_milestone_subjects"]

            # 7. After cooldown expires (>24h), same skill records again with evolution wording
            expired_ts = (utcnow() - timedelta(hours=25)).isoformat()
            await rt.repo.upsert_world_item(
                kind="social_rhythm_state",
                key="primary",
                value={
                    "recent_milestone_subjects": {
                        "skill:audit.coding.provenance_checksum_check": expired_ts,
                        "skill:cognition.research.other_skill": utcnow().isoformat(),
                    }
                },
                status="active",
            )
            await rt.loop._record_social_milestone_from_finalizer(
                FinalizationResult(
                    execution_id="exec-skill-v2",
                    task_id=str(uuid.uuid4()),
                    already_finalized=False,
                    goal_achieved=True,
                    task_status="SUCCEEDED",
                    evaluation={
                        "skill_crystallized": {
                            "crystallized": True,
                            "evolved": True,
                            "version": "2.0.0",
                            "skill_key": "audit_coding_provenance_checksum_check",
                            "canonical_name": "audit.coding.provenance_checksum_check",
                            "status": "VALIDATED",
                            "passed": True,
                        },
                    },
                )
            )
            rhythm_reopened = await _get_rhythm()
            assert rhythm_reopened["pending_milestone"] is not None
            assert rhythm_reopened["pending_milestone"]["event_id"] == "exec-skill-v2"
            assert "迭代升级到 v2.0.0" in rhythm_reopened["pending_milestone"]["summary"]
            assert "skill:audit.coding.provenance_checksum_check" not in rhythm_reopened["recent_milestone_subjects"]
            assert "skill:cognition.research.other_skill" in rhythm_reopened["recent_milestone_subjects"]
        finally:
            await db.dispose()
