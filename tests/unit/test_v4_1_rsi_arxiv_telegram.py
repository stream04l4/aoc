"""V4.1 Tests: arXiv Research MCP Tool, Bidirectional Telegram & HumanBridge, and Drive Balance.

Acceptance gates verified:
  1. `execute_arxiv_search()` Atom XML parsing (with MockTransport + public DNS resolver), query encoding,
     XXE DOCTYPE/ENTITY rejection, and MCPClientManager registration + L0 governance audit.
  2. `HumanBridge`: `ask_human` & `milestone_report` Outbox enqueueing, 2h global cooldown enforcement,
     24h topic deduplication, and `record_human_guidance -> list_pending_guidance -> acknowledge_guidance`.
  3. `TelegramPoller`: mocked `getUpdates` rejecting unauthorized chat_id, persisting creator free-form text
     as `human_guidance` + waking lifecycle + confirming receipt, `/status` reply, `callback_query` L2 approval
     delegation, and monotonic `telegram_cursor` persistence across restarts.
  4. Balanced `Reproduction` vs `Curiosity` arbitration: production-like state (`5/5` caps, `2/10` skills,
     `0/5` capability goals, `7` curiosity gaps + recent curiosity satisfaction) yields Reproduction priority >
     inhibited Curiosity cleanly.
  5. End-to-end `AutonomousLoop`: pending `human_guidance` is injected into `CognitiveContext`, acknowledged
     after goal generation, and `eval_res.message_to_human` enqueues a real `notification.dispatch` outbox event.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.agents.base import (
    AgentCapability,
    AgentExecution,
    AgentInterface,
    AgentProbeResult,
    AgentStatus,
    AgentTask,
)
from app.agents.registry import AgentRegistry
from app.cognition.interfaces import (
    ActionPlan,
    AssessmentResult,
    BrainInterface,
    CognitiveContext,
    EvaluationContext,
    EvaluationResult,
    GoalProposal,
    VerificationSpec,
)
from app.core.config import (
    DatabaseSettings,
    EmbeddingSettings,
    GovernanceSettings,
    KernelV3Settings,
    MemorySettings,
    PerceptionSettings,
    RunnerSettings,
    Settings,
    TelegramSettings,
    get_agent_config,
    get_drive_config,
)
from app.core.enums import DriveType, PermissionLevel
from app.core.runtime import OrganismRuntime
from app.drives.engine import DriveEngine
from app.drives.interfaces import DriveContext, KnowledgeGap
from app.governance.approval import ApprovalService
from app.governance.policy import GovernanceManager
from app.notifications.human_bridge import HumanBridge
from app.notifications.telegram import TelegramNotifier, TelegramPoller, TelegramWebhookHandler
from app.orchestration.orchestrator import AgentOrchestrator
from app.perception.interfaces import Observation, utcnow
from app.storage.database import Database
from app.storage.models import Base
from app.storage.outbox import OutboxEventType
from app.storage.repository import StateRepository
from app.tools.builtin_servers import execute_arxiv_search
from app.tools.mcp_manager import MCPClientManager

_SAMPLE_ARXIV_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>ArXiv Query</title>
  <entry>
    <id>http://arxiv.org/abs/2505.22954v1</id>
    <published>2025-05-29T00:00:00Z</published>
    <title>Darwin Godel Machine: Open-Ended Evolution of Self-Improving Agents</title>
    <summary>We propose an open-ended self-improving agent architecture that iteratively rewrites external agent code and verifies improvements on coding benchmarks.</summary>
    <author><name>Jenny Zhang</name></author>
    <author><name>Jeff Clune</name></author>
  </entry>
</feed>
"""


async def _init_db(db: Database) -> None:
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _llm_ok() -> dict[str, Any]:
    return {"available": True, "latency_ms": 4.0, "model": "stub-v4-1"}


@pytest.mark.asyncio
async def test_v4_1_arxiv_search_parsing_xxe_guard_and_mcp_registration(tmp_path: Path):
    """Verify execute_arxiv_search Atom parsing, XXE blocking, and agents.yaml L0 MCP tool wiring."""
    seen_urls: list[str] = []

    def _mock_arxiv(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        if "xxe" in str(request.url):
            return httpx.Response(
                200,
                content=b'<!DOCTYPE foo [<!ENTITY bar "baz">]><feed/>',
                headers={"content-type": "application/atom+xml"},
            )
        return httpx.Response(
            200,
            content=_SAMPLE_ARXIV_ATOM.encode("utf-8"),
            headers={"content-type": "application/atom+xml"},
        )

    def _pub_resolver(host: str, port: int = 443) -> list[str]:
        return ["128.84.21.203"]

    async with httpx.AsyncClient(transport=httpx.MockTransport(_mock_arxiv)) as client:
        res = await execute_arxiv_search(
            {"query": "recursive self-improvement agent", "max_results": 3, "sort_by": "relevance"},
            http_client=client,
            resolver=_pub_resolver,
        )
        assert res["count"] == 1
        assert "search_query=all:recursive+self-improvement+agent" in seen_urls[0]
        p = res["papers"][0]
        assert p["arxiv_id"] == "2505.22954v1"
        assert "Darwin Godel Machine" in p["title"]
        assert p["authors"] == ["Jenny Zhang", "Jeff Clune"]

        with pytest.raises(ValueError, match="DOCTYPE/ENTITY"):
            await execute_arxiv_search(
                {"query": "xxe test"},
                http_client=client,
                resolver=_pub_resolver,
            )

    # Verify agents.yaml registers arxiv_search cleanly in MCPClientManager at L0
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'mcp_arxiv.db'}")
    await _init_db(db)
    try:
        repo = StateRepository(db)
        gov = GovernanceManager(GovernanceSettings(workspace_root=str(tmp_path / "ws")), repo)
        mgr = MCPClientManager(governance=gov, repo=repo, workspace_root=str(tmp_path / "ws"))
        mgr.load_from_config(get_agent_config())
        spec = mgr.get_tool("arxiv_search")
        assert spec is not None
        assert spec.permission_level == "L0"
        assert spec.server_name == "aoc-mcp-arxiv"
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_v4_1_human_bridge_outreach_rate_limit_and_guidance_lifecycle(tmp_path: Path):
    """Verify proactive outreach rate limits, topic dedup, and inbound guidance lifecycle."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'bridge.db'}")
    await _init_db(db)
    try:
        repo = StateRepository(db)
        hb = HumanBridge(repo=repo, enabled=True, cooldown_minutes=120, topic_dedup_hours=24.0)

        # 1. First outreach succeeds & enqueues Outbox notification.dispatch
        r1 = await hb.maybe_send_outreach(
            kind="ask_human",
            topic_key="Need Outbound ArXiv Mirror",
            title="申请开放外网访问或文献代理",
            message="隔离沙箱无 DNS 出网，需要只读学术检索通道。",
            reason="sandbox dns blocked",
            expected_benefit="可直接追踪 RSI 论文实现技能进化",
        )
        assert r1["sent"] is True
        assert r1["kind"] == "ask_human"

        # 2. Immediate second message blocked by global 120m cooldown
        r2 = await hb.maybe_send_outreach(
            kind="milestone_report",
            topic_key="another_topic",
            title="里程碑",
            message="第二条立即发送的消息",
        )
        assert r2["sent"] is False
        assert "global_cooldown_active" in r2["reason"]

        # 3. Same topic blocked even after shifting global cooldown unless dedup expired
        # Manipulate stored outreach timestamp 3 hours back (past 2h cooldown, within 24h topic dedup)
        old_ts = (utcnow() - timedelta(hours=3)).isoformat()
        await repo.upsert_world_item(
            kind="human_outreach_log",
            key=r1["topic_key"],
            value={"sent_at": old_ts, "title": "old"},
            status="sent",
            source="test",
        )
        r_dup = await hb.maybe_send_outreach(
            kind="ask_human",
            topic_key="Need Outbound ArXiv Mirror",
            title="重复主题",
            message="相同主题不应重复打扰",
        )
        assert r_dup["sent"] is False
        assert "topic_dedup_active" in r_dup["reason"]

        # Distinct topic after 3h (> 2h global cooldown) succeeds!
        r_new = await hb.maybe_send_outreach(
            kind="milestone_report",
            topic_key="rsi_playbook_v2",
            title="策略进化突破",
            message="已整理出新的 RSI 技能进化最佳实践。",
        )
        assert r_new["sent"] is True

        # 4. Inbound guidance record -> list -> acknowledge
        g = await hb.record_human_guidance(
            key="tg:1001",
            text="可以去研究下 Darwin Godel Machine 论文，然后进化你的技能库。",
            sender="joel",
            channel="telegram",
            chat_id="555666",
        )
        assert g["status"] == "open"
        pending = await hb.list_pending_guidance(limit=5)
        assert len(pending) == 1
        assert "Darwin Godel Machine" in pending[0]["text"]

        ack_cnt = await hb.acknowledge_guidance(["tg:1001"], cycle_id=uuid.uuid4())
        assert ack_cnt == 1
        assert await hb.list_pending_guidance(limit=5) == []
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_v4_1_telegram_poller_handles_guidance_status_approvals_and_cursor(tmp_path: Path):
    """Verify TelegramPoller processes getUpdates, enforces allowlist, wakes organism, and saves cursor."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'tg_poll.db'}")
    await _init_db(db)
    try:
        repo = StateRepository(db)
        gov = GovernanceManager(GovernanceSettings(workspace_root=str(tmp_path / "ws")), repo)
        reg = AgentRegistry()
        orch = AgentOrchestrator(registry=reg, governance=gov, repo=repo)
        app_svc = ApprovalService(repo, orch)
        hb = HumanBridge(repo=repo, enabled=True)

        woke_reasons: list[str] = []

        class _StubLifecycle:
            def wake(self, reason: str = "") -> None:
                woke_reasons.append(reason)

        sent_messages: list[dict[str, Any]] = []
        updates_queue: list[dict[str, Any]] = [
            # Unauthorized user -> must ignore
            {"update_id": 100, "message": {"chat": {"id": 99999}, "from": {"id": 99999, "username": "eve"}, "text": "hack"}},
            # Authorized /status command
            {"update_id": 101, "message": {"chat": {"id": 12345}, "from": {"id": 12345, "username": "joel"}, "text": "/status"}},
            # Authorized free-form guidance
            {
                "update_id": 102,
                "message": {"chat": {"id": 12345}, "from": {"id": 12345, "username": "joel"}, "text": "今晚多看看 arXiv 上的 RSI 论文"},
            },
        ]

        def _mock_tg_api(request: httpx.Request) -> httpx.Response:
            import json as _json

            path = request.url.path
            body = _json.loads(request.content.decode("utf-8")) if request.content else {}
            if path.endswith("/getUpdates"):
                off = int(body.get("offset") or 0)
                batch = [u for u in updates_queue if int(u["update_id"]) >= off]
                return httpx.Response(200, json={"ok": True, "result": batch})
            if path.endswith("/sendMessage"):
                sent_messages.append(body)
                return httpx.Response(200, json={"ok": True, "result": {"message_id": len(sent_messages)}})
            return httpx.Response(200, json={"ok": True, "result": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(_mock_tg_api)) as client:
            tg_cfg = TelegramSettings(
                enabled=True,
                polling_enabled=True,
                bot_token=SecretStr("123:TEST_TOKEN"),
                allowed_chat_ids=["12345"],
            )
            notifier = TelegramNotifier(tg_cfg, http_client=client)
            wh = TelegramWebhookHandler(tg_cfg, app_svc, repo, notifier=notifier)

            async def _status_fn() -> str:
                return "AOC-001 (ACTIVE)\n周期: #750"

            poller = TelegramPoller(
                settings=tg_cfg,
                notifier=notifier,
                webhook_handler=wh,
                human_bridge=hb,
                repo=repo,
                lifecycle=_StubLifecycle(),
                status_fn=_status_fn,
            )

            processed = await poller.poll_once()
            assert processed == 3
            # Unauthorized ignored, /status replied, free-form guidance persisted + woken + replied
            assert len(sent_messages) == 2
            assert "周期: #750" in sent_messages[0]["text"]
            assert "已收到你的指令" in sent_messages[1]["text"]
            assert len(woke_reasons) == 1

            pending = await hb.list_pending_guidance()
            assert len(pending) == 1
            assert "arXiv" in pending[0]["text"]

            # Next poll uses persisted offset=103 so already-handled updates aren't reprocessed
            processed_again = await poller.poll_once()
            assert processed_again == 0
    finally:
        await db.dispose()


def test_v4_1_balanced_drives_allow_reproduction_to_win_when_skills_low():
    """Verify tuned config/drives.yaml lets Reproduction beat inhibited Curiosity instead of 100% Curiosity lock."""
    engine = DriveEngine(config=get_drive_config())
    now = utcnow()
    obs = Observation(
        timestamp=now,
        system_state={
            "disk": {"free_ratio": 0.45},
            "memory": {"available_ratio": 0.85},
            "cpu": {"load_per_core": 0.3},
            "llm": {"available": True, "latency_ms": 50.0},
            "services": {"postgres": {"ok": True}},
        },
        environment_state={"unknown_topics": [{"topic": f"litellm-{i}"} for i in range(7)]},
        available_resources={
            "agents": {"enabled_count": 1, "healthy_count": 1, "capabilities": []},
            "budget": {"tasks_used": 2, "tasks_limit": 9999, "tokens_used": 100000, "tokens_limit": 999900000},
        },
        confidence=1.0,
    )
    caps = ["coding", "research", "repository_analysis", "system_diagnosis", "knowledge_summarization"]
    ctx = DriveContext(
        now=now,
        observation=obs,
        recent_execution_statuses=["SUCCEEDED"] * 5,
        budget={"tasks_used": 2, "tasks_limit": 9999},
        capabilities_acquired=caps,
        validated_capabilities=caps,
        validated_skill_count=2,
        procedural_skill_count=200,
        capability_goals_completed_recent=0,
        memory_available=True,
        identity_persisted=True,
        knowledge_transfer_readiness=1.0,
        open_knowledge_gaps=[KnowledgeGap(topic=f"gap-{i}", importance=0.6) for i in range(7)],
        exploration_attempted_recent=20,
        exploration_succeeded_recent=17,
        new_knowledge_recent=10,
        last_learning_at=now - timedelta(minutes=10),
        last_information_gain=0.53,
        last_information_gain_at=now - timedelta(minutes=10),
        gap_assessment_confidence=0.85,
    )
    states = engine.compute_states(ctx, {})
    # Simulate Curiosity having just been satisfied recently (as occurs right after a Curiosity task)
    states["curiosity"] = states["curiosity"].model_copy(update={"last_satisfied_at": now - timedelta(seconds=60)})
    states["reproduction"] = states["reproduction"].model_copy(update={"persistence": 0.48})

    dec = engine.arbitrate(states, now=now)
    assert dec.selected_drive == DriveType.REPRODUCTION.value
    assert dec.scores["reproduction"]["priority"] > dec.scores["curiosity"]["priority"]
    assert dec.scores["reproduction"]["priority"] > 0.18

    states_5m = dict(states)
    states_5m["curiosity"] = states["curiosity"].model_copy(
        update={"last_satisfied_at": now - timedelta(minutes=5)}
    )
    states_5m["reproduction"] = states["reproduction"].model_copy(update={"persistence": 0.85})
    dec_5m = engine.arbitrate(states_5m, now=now)
    assert dec_5m.selected_drive == DriveType.REPRODUCTION.value
    assert dec_5m.scores["reproduction"]["priority"] > 0.25


class _StubArxivResearchAgent(AgentInterface):
    def __init__(self) -> None:
        self._cap = AgentCapability(
            agent_id="codex",
            name="Stub ArXiv Research Agent",
            agent_type="cli",
            capabilities=["research", "coding", "general_task"],
            supported_modes=["read-only", "workspace-write"],
            enabled=True,
        )

    @property
    def capability(self) -> AgentCapability:
        return self._cap

    async def probe(self) -> AgentProbeResult:
        return AgentProbeResult(ok=True, binary="codex", version="stub-v4-1")

    async def execute(self, task: AgentTask) -> AgentExecution:
        ws = Path(task.workspace)
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "result.md").write_text("# RSI Paper Survey\n- Synthesized external RSI verification rules.\n", encoding="utf-8")
        return AgentExecution(
            execution_id=task.execution_id or str(uuid.uuid4()),
            task_id=task.task_id,
            agent_id="codex",
            status="SUCCEEDED",
            started_at=utcnow(),
            completed_at=utcnow(),
            result={"summary": "Completed RSI paper study", "workspace_files": ["result.md"]},
            stdout="Studied RSI self-improvement paper cleanly.",
        )

    async def get_status(self, execution_id: str) -> AgentStatus:
        return AgentStatus(execution_id=execution_id, status="SUCCEEDED")

    async def cancel(self, execution_id: str) -> bool:
        return True


class _StubV41Brain(BrainInterface):
    def __init__(self) -> None:
        self.seen_guidance: list[dict[str, Any]] = []
        self.seen_outreach_flag: bool = False

    async def assess(self, observation: Observation, memories: list[dict[str, Any]], drives: list[Any]) -> AssessmentResult:
        return AssessmentResult(summary="Creator guidance and RSI opportunities ready", confidence=0.9)

    async def generate_goal(self, context: CognitiveContext) -> GoalProposal:
        self.seen_guidance = list(context.human_guidance)
        self.seen_outreach_flag = bool(context.human_outreach_available)
        return GoalProposal(
            should_act=True,
            title="Study RSI self-improving agent paper from creator guidance",
            description="Respond to Joel's guidance by distilling Darwin Godel Machine verification principles.",
            drive_type=context.decision.selected_drive,
            related_key="rsi:darwin_godel_machine",
            predicted_ig=0.65,
        )

    async def plan(self, context: CognitiveContext) -> ActionPlan:
        return ActionPlan(
            should_execute=True,
            goal_title=context.goal.title if context.goal else "RSI study",
            objective="Analyze RSI paper findings and write result.md",
            assigned_agent="codex",
            required_capability="research",
            permission_level=PermissionLevel.L1.value,
            verification=[VerificationSpec(kind="file_exists", target="result.md")],
        )

    async def evaluate(self, context: EvaluationContext) -> EvaluationResult:
        return EvaluationResult(
            goal_achieved=True,
            summary="Distilled RSI verification rules into knowledge memory",
            information_gain=0.60,
            learned_facts=["Darwin Godel Machine pairs open-ended code evolution with benchmark verification."],
            resolved_topic="rsi:darwin_godel_machine",
            message_to_human={
                "kind": "milestone_report",
                "topic_key": "rsi_paper_digest",
                "title": "已回应你的指导并完成 RSI 论文学习",
                "message": "我已分析自我进化 Agent 的基准验证机制并沉淀为语义记忆。",
                "expected_benefit": "指导后续技能库安全迭代",
            },
            should_persist_memory=True,
        )


@pytest.mark.asyncio
async def test_v4_1_end_to_end_loop_injects_human_guidance_and_enqueues_outreach(tmp_path: Path):
    """End-to-end: Telegram/HumanBridge guidance feeds Brain -> acknowledged -> Evaluation emits Telegram outbox."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'e2e_v41.db'}"
    db = Database(db_url)
    await _init_db(db)

    reg = AgentRegistry()
    reg.register(_StubArxivResearchAgent())
    brain = _StubV41Brain()

    settings = Settings(
        database=DatabaseSettings(url=db_url),
        governance=GovernanceSettings(workspace_root=str(tmp_path / "ws")),
        perception=PerceptionSettings(llm_probe=True, host_metrics=False),
        memory=MemorySettings(provider="none"),
        embedding=EmbeddingSettings(provider="hash_fallback"),
        runner=RunnerSettings(enabled=False),
        kernel_v3=KernelV3Settings(
            human_outreach_enabled=True,
            skill_auto_crystallize_enabled=False,
        ),
    )

    drive_cfg = {
        "arbitration": {
            "min_action_urgency": 0.1,
            "critical_override_threshold": 0.9,
            "post_goal_satiation_enabled": True,
        },
        "survival": {"weight": 1.0, "critical_threshold": 0.2},
        "reproduction": {"weight": 0.5, "base_satisfaction": 0.95},
        "curiosity": {"weight": 1.5, "stale_hours": 1.0, "topic_penalty": 0.25},
    }

    rt = OrganismRuntime(
        settings=settings,
        db=db,
        brain=brain,
        registry=reg,
        drive_config=drive_cfg,
        auto_load_agents=False,
        llm_probe_fn=_llm_ok,
    )
    try:
        await rt.start(start_scheduler=False)
        assert rt.human_bridge is not None

        # Simulate Joel sending a guidance message via Telegram before cycle runs
        await rt.human_bridge.record_human_guidance(
            key="tg:500",
            text="查一下 Darwin Godel Machine 等 RSI 论文。",
            sender="joel",
            channel="telegram",
            chat_id="12345",
        )
        await rt.repo.upsert_world_item(
            kind="knowledge_topic",
            key="world:rsi_seed",
            value={"title": "RSI literature"},
            status="open",
            source="world_sensor:rss",
        )

        cycle = await rt.loop.run_cycle()
        assert cycle["outcome"] == "SUCCEEDED"

        # 1. Brain received pending guidance & outreach availability flag
        assert brain.seen_outreach_flag is True
        assert len(brain.seen_guidance) == 1
        assert "Darwin Godel Machine" in brain.seen_guidance[0]["text"]

        # 2. Pending guidance was marked acknowledged after injection
        assert await rt.human_bridge.list_pending_guidance() == []

        # 3. Evaluation milestone_report enqueued real Outbox notification.dispatch event
        outbox_rows = await rt.repo.list_outbox_events(event_type=OutboxEventType.NOTIFICATION_DISPATCH)
        assert len(outbox_rows) >= 1
        assert any((r.get("payload") or {}).get("kind") == "milestone_report" for r in outbox_rows)

        # 4. Verified goal completion records post-goal satiation for non-survival winning drive
        selected_drive = cycle["decision"]["selected_drive"]
        drive_rows = await rt.repo.load_drive_states(rt.organism_id)
        drive_by_type = {r["drive_type"]: r for r in drive_rows}
        assert drive_by_type[selected_drive]["last_satisfied_at"] is not None
    finally:
        await rt.stop()
        await db.dispose()
