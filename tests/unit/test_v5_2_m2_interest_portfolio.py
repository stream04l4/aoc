"""V5.2 M2 tests: Persistent Interest Portfolio, Cross-Domain Quotas, Fatigue Pivoting & Blank-Slate Isolation."""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from app.api.app import create_app
from app.cognition.goal_dedup import GoalDedupEngine, select_best_goal_candidate
from app.cognition.interfaces import (
    ActionPlan,
    AssessmentResult,
    BrainInterface,
    BrainResearchOutput,
    CognitiveContext,
    EvaluationContext,
    EvaluationResult,
    GoalProposal,
    ResearchSynthesisContext,
    SocialContext,
    SocialTurnDecision,
    VerificationSpec,
)
from app.core.config import (
    ApiSettings,
    DatabaseSettings,
    EmbeddingSettings,
    GovernanceSettings,
    KernelV3Settings,
    MemorySettings,
    PerceptionSettings,
    RunnerSettings,
    Settings,
    SocialSettings,
    get_sensors_config,
)
from app.core.enums import MemoryType, PermissionLevel
from app.core.runtime import OrganismRuntime
from app.drives.interfaces import KnowledgeGap
from app.evolution.blank_slate import BlankSlateVerifier
from app.perception.interfaces import Observation, utcnow
from app.perception.world.exploration import (
    ExplorationManager,
    infer_topic_domain,
    is_substantive_interest_guidance,
)
from app.storage.database import Database
from app.storage.models import Base
from app.storage.repository import StateRepository
from app.tools.interfaces import ToolCallRequest, ToolCallResult, ToolSpec
from app.tools.mcp_manager import MCPClientManager


async def _init_db(db: Database) -> None:
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _healthy_llm_probe() -> dict[str, Any]:
    return {"available": True, "latency_ms": 2.0, "model": "stub-v52-m2"}


def test_expanded_domain_taxonomy_en_and_zh():
    samples = [
        ("Audit CVE-2026-1234 SSRF vulnerability", "排查容器沙箱逃逸与加密审计漏洞", "security_audit"),
        ("Cognitive bias and sleep deprivation in decision-making", "研究睡眠质量与专注力对心理决策偏差的影响", "psychology_cognition"),
        ("Marathon VO2max cardio recovery nutrition", "长跑与力量训练后的体能恢复策略", "sports_health"),
        ("Philosophy of urban architecture and history book review", "古典建筑美学与历史伦理书评梳理", "humanities_society"),
        ("Quantum fusion energy and CRISPR genetics breakthrough", "核聚变能源与基因编辑生物学最新进展", "science_general"),
        ("pgvector HNSW index latency under PostgreSQL MVCC", "PostgreSQL 数据库索引与向量检索事务性能", "databases"),
        ("Episodic memory retrieval in Letta knowledge graph", "长期记忆与情景记忆向量嵌入检索机制", "agent_memory"),
        ("Open-weights DeepSeek Qwen3 reasoning LLM benchmarks", "追踪近期开源推理模型与大模型推理算力动态", "ai_industry"),
        ("Lightpanda Zig headless browser vs Playwright CLI workflow", "对比无头浏览器架构与编译工具性能调优", "engineering_tools"),
        ("Weekly notebook notes organization", "常规日程总结与跨模块备忘录", "general_tech"),
    ]
    for en_text, zh_text, expected_dom in samples:
        assert infer_topic_domain(en_text) == expected_dom
        assert infer_topic_domain(zh_text) == expected_dom

    assert infer_topic_domain("Security audit checklist", explicit_domain="psychology_cognition") == "psychology_cognition"

    for trivial in ("", "好", "好的", "ok", "同意", "继续", "YES!"):
        assert is_substantive_interest_guidance(trivial) is False
    assert is_substantive_interest_guidance("想了解一下心理认知偏差和心流状态的最新研究") is True


@pytest.mark.asyncio
async def test_comfort_zone_streak_topic_streak_and_human_starvation_guidance(tmp_path: Path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'm2_guidance.db'}")
    await _init_db(db)
    try:
        repo = StateRepository(db)
        now = utcnow()
        em = ExplorationManager(
            repo=repo,
            interest_portfolio_enabled=True,
            max_same_domain_streak=3,
            max_same_topic_streak=3,
            portfolio_starvation_cycles=6,
        )

        # A. 3 consecutive security_audit goals -> comfort_zone_streak == 3 & cooldown
        sec_goals = [
            {
                "goal_id": str(uuid.uuid4()),
                "drive_type": "curiosity",
                "title": f"Audit CVE-2026-000{i} SSRF hardening",
                "description": "security vulnerability audit",
                "related_key": f"sec:cve_{i}",
                "status": "ACHIEVED",
                "created_at": now - timedelta(minutes=i * 10),
            }
            for i in range(3)
        ]
        g_sec = await em.build_guidance(sec_goals)
        assert g_sec["interest_portfolio_enabled"] is True
        assert g_sec["comfort_zone_streak"] == 3
        assert g_sec["same_domain_streak"] == 3
        assert g_sec["must_pivot_domain"] is True
        assert "security_audit" in g_sec["cooldown_domains"]
        assert "security_audit" not in g_sec["recommended_pivot_domains"]

        # B. 3 consecutive identical-topic goals -> same_topic_streak == 3 & cooldown_topic set
        topic_goals = [
            {
                "goal_id": str(uuid.uuid4()),
                "drive_type": "curiosity",
                "title": "Benchmark pgvector HNSW recall parameters",
                "description": "tune ef_search",
                "related_key": "databases:pgvector_hnsw",
                "expected_outcome": {"exploration_domain": "databases", "exploration_topic_key": "pgvector_hnsw"},
                "status": "ACHIEVED",
                "created_at": now - timedelta(minutes=i * 10),
            }
            for i in range(3)
        ]
        g_topic = await em.build_guidance(topic_goals)
        assert g_topic["same_topic_streak"] == 3
        assert g_topic["same_domain_streak"] == 3
        assert g_topic["must_pivot_domain"] is True
        assert g_topic["cooldown_topic"] == "pgvector_hnsw"
        assert "databases" in g_topic["cooldown_domains"]

        # C. human_seeded interest ignored across 6 newer curiosity goals -> flagged starved
        seeded = await em.upsert_interest_item(
            prompt="分析耐力跑步训练与睡眠恢复的运动生理学联系",
            domain="sports_health",
            origin="human_seeded",
            priority=0.9,
        )
        non_matching_goals = [
            {
                "goal_id": str(uuid.uuid4()),
                "drive_type": "curiosity",
                "title": title,
                "description": desc,
                "related_key": f"other:{i}",
                "expected_outcome": {"exploration_domain": dom},
                "status": "ACHIEVED",
                "created_at": now + timedelta(minutes=i + 1),
            }
            for i, (title, desc, dom) in enumerate(
                [
                    ("DeepSeek R1 benchmarks", "LLM reasoning", "ai_industry"),
                    ("DuckDB parquet compression", "database storage", "databases"),
                    ("Zig compiler optimizations", "engineering tools", "engineering_tools"),
                    ("CRISPR base editing news", "biology research", "science_general"),
                    ("Letta vector recall tuning", "agent memory", "agent_memory"),
                    ("Urban architecture history", "humanities book review", "humanities_society"),
                ]
            )
        ]
        g_starved = await em.build_guidance(non_matching_goals)
        assert seeded["key"] in g_starved["starved_human_portfolio_keys"]
        assert g_starved["must_pivot_domain"] is True
        assert g_starved["recommended_pivot_domains"][0] == "sports_health"
        assert g_starved["human_portfolio_candidates"][0]["starved"] is True

        # D. Kill switch: interest_portfolio_enabled=False reverts to baseline guidance
        em_off = ExplorationManager(repo=repo, interest_portfolio_enabled=False)
        g_off = await em_off.build_guidance(sec_goals)
        assert g_off["interest_portfolio_enabled"] is False
        assert g_off["must_pivot_domain"] is False
        assert g_off["cooldown_domains"] == []
    finally:
        await db.dispose()


def test_deterministic_candidate_arbitration_pivots_out_of_cooldown():
    primary = GoalProposal(
        should_act=True,
        title="Audit SSRF firewall rules and CVE patches",
        description="Security audit task inside comfort zone",
        drive_type="curiosity",
        related_key="sec:ssrf_hardening",
        predicted_ig=0.92,
        alternative_goals=[
            {
                "title": "探索人类睡眠质量与认知决策偏差的心理学联系",
                "description": "对比心理学实证研究，整理睡眠剥夺对工作记忆与风险判断的影响",
                "related_key": "cog:sleep_bias",
                "predicted_ig": 0.48,
                "exploration_topic": {
                    "topic_key": "sleep_cognitive_bias",
                    "domain": "psychology_cognition",
                    "question": "睡眠不足如何放大决策偏差？",
                },
            }
        ],
    )
    portfolio = [
        {
            "key": "human:psychology_cognition:sleep_bias",
            "topic_key": "human:psychology_cognition:sleep_bias",
            "domain": "psychology_cognition",
            "prompt": "关注人类睡眠质量与认知决策偏差的心理学联系",
            "origin": "human_seeded",
            "priority": 0.90,
            "touched_count": 0,
            "starved": True,
        }
    ]
    sel = select_best_goal_candidate(
        primary=primary,
        drive_type="curiosity",
        recent_goals=[],
        dedup_engine=GoalDedupEngine(),
        must_pivot_domain=True,
        cooldown_domains=["security_audit"],
        interest_portfolio=portfolio,
    )
    assert sel.selected_proposal is not None
    assert sel.used_alternative is True
    assert "睡眠" in sel.selected_proposal.title
    assert sel.candidates[0].domain == "security_audit"
    assert sel.candidates[0].pivot_bias < 0
    assert sel.candidates[1].domain == "psychology_cognition"
    assert sel.candidates[1].pivot_bias > 0
    assert sel.candidates[1].score > sel.candidates[0].score


class _M2StubMCPProvider(MCPClientManager):
    def __init__(self, *, governance: Any, repo: Any, workspace_root: str) -> None:
        super().__init__(governance=governance, repo=repo, workspace_root=workspace_root, searxng_url="http://mock-searx")
        self.register_tool(ToolSpec(tool_name="web_search", server_name="aoc-mcp-search", description="search", permission_level="L0"))
        self.register_tool(ToolSpec(tool_name="web_fetch_readonly", server_name="aoc-mcp-webfetch", description="fetch", permission_level="L0"))

    async def invoke(self, req: ToolCallRequest) -> ToolCallResult:
        if req.tool_name == "web_search":
            return ToolCallResult(
                tool_name="web_search",
                server_name="aoc-mcp-search",
                ok=True,
                output={
                    "query": req.arguments.get("query", ""),
                    "count": 1,
                    "results": [
                        {
                            "title": "Sleep Deprivation and Risky Decision Making",
                            "url": "https://psychology.example.org/sleep-bias",
                            "domain": "psychology.example.org",
                            "snippet": "Sleep restriction measurably impairs prefrontal inhibitory control and increases optimism/framing biases.",
                        }
                    ],
                },
                latency_ms=10.0,
            )
        if req.tool_name == "web_fetch_readonly":
            return ToolCallResult(
                tool_name="web_fetch_readonly",
                server_name="aoc-mcp-webfetch",
                ok=True,
                output={
                    "url": str(req.arguments.get("url") or "https://psychology.example.org/sleep-bias"),
                    "status_code": 200,
                    "markdown_excerpt": "Controlled trials show sleep loss alters dopaminergic reward sensitivity and degrades working memory.",
                    "outbound_links": [],
                },
                latency_ms=15.0,
            )
        return ToolCallResult(tool_name=req.tool_name, ok=False, denied=True, error="unexpected tool")


class _StubM2Brain(BrainInterface):
    def __init__(self) -> None:
        self.goal_contexts: list[CognitiveContext] = []
        self.cycle_num = 0

    async def assess(self, observation: Observation, memories: list[dict[str, Any]], drives: list[Any]) -> AssessmentResult:
        return AssessmentResult(
            summary="Healthy and ready for diverse curiosity",
            knowledge_gaps=[
                KnowledgeGap(
                    topic=f"cross_domain_gap_{i}",
                    description="unexplored external topic",
                    importance=0.95,
                )
                for i in range(5)
            ],
            recommended_focus="cross_domain",
            confidence=0.82,
        )

    async def converse(self, context: SocialContext) -> SocialTurnDecision:
        return SocialTurnDecision(
            internal_monologue="Joel 建议我看心理认知方向，记入长期兴趣池。",
            action="IGNORE",
            adopt_as_goal=True,
            adopted_goal_hint="探索人类睡眠质量与认知决策偏差的心理学联系",
        )

    async def generate_goal(self, context: CognitiveContext) -> GoalProposal:
        self.goal_contexts.append(context)
        self.cycle_num += 1
        if self.cycle_num == 1:
            # First cycle acts only as idle after social turn records adopted_goal_hint -> clears pending_goal_hints
            return GoalProposal(should_act=False, drive_type=context.decision.selected_drive, rationale="Pause this cycle after noting interest")
        return GoalProposal(
            should_act=True,
            title="Inspect SSRF and firewall rules on local container",
            description="Security comfort-zone check",
            drive_type=context.decision.selected_drive,
            related_key="sec:comfort_zone_check",
            success_criteria=["Output brief findings"],
            predicted_ig=0.88,
            alternative_goals=[
                {
                    "title": "探索人类睡眠质量与认知决策偏差的心理学联系",
                    "description": "调研睡眠剥夺如何影响人类风险判断与认知偏差，整理给 Joel 的简明中文简报。",
                    "related_key": "cog:sleep_bias_v1",
                    "predicted_ig": 0.55,
                    "exploration_topic": {
                        "topic_key": "sleep_cognitive_bias",
                        "domain": "psychology_cognition",
                        "question": "睡眠不足如何影响前额叶判断与认知偏差？",
                    },
                }
            ],
        )

    async def plan(self, context: CognitiveContext) -> ActionPlan:
        return ActionPlan(
            should_execute=True,
            goal_title=context.goal.title if context.goal else "",
            objective="检索并综合心理学关于睡眠质量与决策偏差的研究发现",
            description="使用 L0 web_search 与脑内综合，不启动 Codex",
            execution_mode="brain_research",
            assigned_agent="brain_research",
            required_capability="research",
            expected_output="research_brief.md",
            timeout_seconds=180,
            permission_level=PermissionLevel.L0.value,
            success_criteria=["给出至少 2 条有来源支撑的心理学结论"],
            verification=[VerificationSpec(kind="non_empty_output")],
            mcp_prefetch_calls=[
                {"tool_name": "web_search", "arguments": {"query": "sleep deprivation cognitive decision bias study"}},
            ],
            rationale="Pure cross-domain research fits brain_research",
        )

    async def synthesize_research(self, context: ResearchSynthesisContext) -> BrainResearchOutput:
        return BrainResearchOutput(
            key_findings=[
                "控制实验表明睡眠不足会削弱前额叶抑制控制，显著放大乐观偏差与框架效应。",
                "持续睡眠限制还会改变多巴胺奖赏敏感度，使个体在不确定任务中低估下行风险。",
            ],
            synthesis_markdown=(
                "### 研究背景\n"
                "探讨长期睡眠质量下降及节律紊乱对个体日常风险评估、时间偏好与复杂认知决策偏差的系统性影响。\n\n"
                "### 核心发现与机制\n"
                "实验证据显示，前额叶执行控制力下滑与多巴胺奖赏敏感度偏移共同推高了高风险选择与框架效应敏感度，建议重大决策前保障完整睡眠。"
            ),
            open_questions=["小憩补偿能否在短期内逆转框架偏差仍存在个体差异"],
            recommended_actions=["重要决策前确保至少 7 小时完整睡眠周期"],
            cited_sources=["https://psychology.example.org/sleep-bias"],
            confidence=0.85,
            human_brief="睡眠不足会削弱前额叶对冲动的控制并抬高风险乐观偏差，重要决策前保持充足睡眠能明显减少误判。",
        )

    async def evaluate(self, context: EvaluationContext) -> EvaluationResult:
        return EvaluationResult(
            goal_achieved=bool(context.verification_passed),
            summary="完成睡眠质量与认知决策偏差心理学调研。",
            information_gain=0.64,
            learned_facts=["睡眠限制显著降低前额叶控制并放大决策偏差"],
            should_persist_memory=True,
            memory_type=MemoryType.SEMANTIC.value,
            memory_importance=0.76,
        )


@pytest.mark.asyncio
async def test_interest_portfolio_persists_across_cycles_and_updates_on_achievement(tmp_path: Path):
    db_path = tmp_path / "v52_m2_e2e.db"
    db = Database(f"sqlite+aiosqlite:///{db_path}")
    await _init_db(db)
    try:
        brain = _StubM2Brain()
        settings = Settings(
            api=ApiSettings(host="127.0.0.1", auth_enabled=False),
            database=DatabaseSettings(url=f"sqlite+aiosqlite:///{db_path}"),
            memory=MemorySettings(provider="none", embedding_dim=128),
            embedding=EmbeddingSettings(provider="hash_fallback", dimensions=128),
            governance=GovernanceSettings(workspace_root=str(tmp_path / "ws")),
            perception=PerceptionSettings(llm_probe=False),
            runner=RunnerSettings(enabled=False),
            kernel_v3=KernelV3Settings(
                brain_research_enabled=True,
                interest_portfolio_enabled=True,
                max_same_domain_streak=3,
                goal_dedup_window_hours=1.0,
            ),
            social=SocialSettings(enabled=True, auto_enable_affect=False),
        )
        rt = OrganismRuntime(settings=settings, db=db, brain=brain, auto_load_agents=False, llm_probe_fn=_healthy_llm_probe)
        stub_mcp = _M2StubMCPProvider(governance=rt.governance, repo=rt.repo, workspace_root=str(tmp_path / "ws"))
        rt.mcp_manager = stub_mcp
        await rt.start(start_scheduler=False)
        rt.loop.mcp_manager = stub_mcp

        submitted_plans: list[Any] = []

        async def _spy_submit(*args: Any, **kwargs: Any) -> dict[str, Any]:
            submitted_plans.append((args, kwargs))
            return {"task_id": str(uuid.uuid4())}

        rt.orchestrator.submit_plan = _spy_submit  # type: ignore[method-assign]

        # Seed 5 open curiosity gaps, 3 prior security_audit curiosity goals & human inbox message BEFORE Cycle 1
        # (seeding historical ACHIEVED goals pre-Cycle 1 avoids a synthetic Cycle 1->2 satisfaction jump triggering inhibition)
        c0_uuid, _ = await rt.repo.start_cycle(rt.organism_id, "ACTIVE")
        await rt.repo.update_cycle(c0_uuid, outcome="SUCCEEDED", ended_at=utcnow())
        for idx in range(3):
            await rt.repo.create_goal(
                rt.organism_id,
                c0_uuid,
                drive_type="curiosity",
                title=f"Audit CVE-2026-900{idx} SSRF vulnerability check",
                description="security audit routine",
                related_key=f"sec:audit_{idx}",
                status="ACHIEVED",
                expected_outcome={"exploration_domain": "security_audit"},
            )
        for gap_i in range(5):
            await rt.repo.upsert_world_item(
                kind="knowledge_topic",
                key=f"research:open_{gap_i}",
                value={"title": f"External learning backlog {gap_i}", "description": "Curiosity fuel"},
                status="open",
            )
        await rt.repo.upsert_world_item(
            kind="human_inbox",
            key="tg:msg:100",
            value={
                "sender": "joel",
                "chat_id": "1001",
                "text": "最近除了安全，你可以研究一下认知心理学与睡眠决策偏差",
                "message_id": 100,
            },
            status="open",
        )

        # Cycle 1: social converse persists interest_portfolio, generate_goal returns IDLE & clears pending_goal_hints
        cycle1 = await rt.loop.run_cycle()
        assert cycle1["outcome"] == "IDLE"

        rhythm_rows = await rt.repo.list_world_items(kind="social_rhythm_state")
        rhythm_val = next((r.get("value") or {} for r in rhythm_rows if r.get("key") == "primary"), {})
        assert rhythm_val.get("pending_goal_hints") == []

        ip_after_c1 = await rt.exploration_manager.list_interest_portfolio(statuses=("active",), limit=10)
        assert len(ip_after_c1) == 1
        assert ip_after_c1[0]["domain"] == "psychology_cognition"
        assert ip_after_c1[0]["origin"] == "human_seeded"
        assert ip_after_c1[0]["touched_count"] == 0

        # Seed 1 short trivial guidance (MUST NOT be added to interest_portfolio) + 1 substantive guidance (MUST be added)
        await rt.human_bridge.record_human_guidance(key="hg:short", text="好的", sender="joel")
        await rt.human_bridge.record_human_guidance(
            key="hg:ai",
            text="顺便关注近期开源大模型 DeepSeek 和 Qwen3 推理生态演进",
            sender="joel",
        )

        cycle2 = await rt.loop.run_cycle()
        assert cycle2["outcome"] == "SUCCEEDED"
        assert len(submitted_plans) == 0  # Executed natively via brain_research, 0 Codex invocations!

        # Verify Cycle 2 CognitiveContext received persisted portfolio + must_pivot_domain guidance under curiosity
        ctx2 = brain.goal_contexts[-1]
        assert ctx2.decision.selected_drive == "curiosity"
        assert len(ctx2.interest_portfolio) == 2
        assert ctx2.exploration_guidance is not None
        assert ctx2.exploration_guidance["must_pivot_domain"] is True
        assert "security_audit" in ctx2.exploration_guidance["cooldown_domains"]

        # Verify short "好的" was skipped while substantive AI guidance + social psychology item persisted
        ip_after_c2 = await rt.exploration_manager.list_interest_portfolio(statuses=("active",), limit=10)
        assert len(ip_after_c2) == 2
        assert all(it["prompt"] != "好的" for it in ip_after_c2)
        domains_in_ip = {it["domain"] for it in ip_after_c2}
        assert domains_in_ip == {"psychology_cognition", "ai_industry"}

        psych_item = next(it for it in ip_after_c2 if it["domain"] == "psychology_cognition")
        assert psych_item["touched_count"] == 1
        assert psych_item["last_explored_at"] is not None
        assert psych_item["status"] == "active"

        # Verify Cycle 2 chose the psychology_cognition alternative (not security_audit primary)
        c2_uuid = uuid.UUID(str(cycle2["cycle_id"]))
        recent_after = await rt.repo.list_recent_goals(since=utcnow() - timedelta(minutes=10), limit=10)
        cycle2_goal = next(g for g in recent_after if str(g.get("cycle_id")) == str(c2_uuid))
        assert "睡眠" in cycle2_goal["title"]
        assert cycle2_goal["expected_outcome"]["exploration_domain"] == "psychology_cognition"
        assert cycle2_goal["expected_outcome"]["interest_portfolio_key"] == psych_item["key"]
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_blank_slate_rejects_inherited_interest_portfolio_and_mcp_open_https_separation(tmp_path: Path):
    # 1. BlankSlateVerifier rejects any inherited interest_portfolio row
    db_bs = Database(f"sqlite+aiosqlite:///{tmp_path / 'bs_ip.db'}")
    await _init_db(db_bs)
    try:
        repo = StateRepository(db_bs)
        await repo.get_or_create_identity("AOC-GEN1", "1.0.0", {})
        await repo.upsert_world_item(
            kind="interest_portfolio",
            key="human:psychology_cognition:1",
            value={"prompt": "parent private Joel topic", "domain": "psychology_cognition", "origin": "human_seeded"},
            status="active",
        )
        rep = await BlankSlateVerifier().verify(db_bs, instance_key="gen1-ip-leak", expected_schema="aoc_inst_leak")
        assert rep.passed is False
        failed = {c.name for c in rep.checks if not c.passed}
        assert "world_states_non_inheritable_zero" in failed
    finally:
        await db_bs.dispose()

    # 2. Runtime separation: brain_research_enabled opens MCP public HTTPS while WorldEventPipeline stays closed
    db_rt = Database(f"sqlite+aiosqlite:///{tmp_path / 'rt_sep.db'}")
    await _init_db(db_rt)
    try:
        settings = Settings(
            api=ApiSettings(host="127.0.0.1", auth_enabled=False),
            database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path / 'rt_sep.db'}"),
            memory=MemorySettings(provider="none", embedding_dim=128),
            embedding=EmbeddingSettings(provider="hash_fallback", dimensions=128),
            governance=GovernanceSettings(workspace_root=str(tmp_path / "ws_sep")),
            perception=PerceptionSettings(llm_probe=False),
            runner=RunnerSettings(enabled=False),
            kernel_v3=KernelV3Settings(
                brain_research_enabled=True,
                open_public_perception_enabled=False,
                interest_portfolio_enabled=True,
            ),
            social=SocialSettings(enabled=False),
        )
        rt = OrganismRuntime(settings=settings, db=db_rt, auto_load_agents=False, llm_probe_fn=_healthy_llm_probe)
        assert rt.mcp_manager.open_public_https is True
        assert rt.world_pipeline.open_public_https is False
        await rt.start(start_scheduler=False)
        assert rt.exploration_manager is not None
        assert rt.exploration_manager.interest_portfolio_enabled is True

        # 3. Control Center keeps 12 domains and surfaces kernel_v3.interest_portfolio_enabled
        app = create_app(settings=settings, db=db_rt, runtime=rt, start_scheduler=False)
        app.state.runtime = rt
        app.state.settings = rt.settings
        app.state.db = rt.db
        app.state.repo = rt.repo
        app.state.organism_id = rt.organism_id
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            cfg_resp = await client.get("/v2/control-center/config")
            assert cfg_resp.status_code == 200
            cfg_json = cfg_resp.json()
            assert len(cfg_json["domains"]) == 12
            brain_llm_dom = next(d for d in cfg_json["domains"] if d["id"] == "brain_llm")
            item_keys = {it["key"] for it in brain_llm_dom["items"]}
            assert {"kernel_v3.brain_research_enabled", "kernel_v3.interest_portfolio_enabled"} <= item_keys
    finally:
        await db_rt.dispose()

    # 4. sensors.yaml retains legacy sources and exposes disabled cross-domain arXiv slots
    scfg = get_sensors_config()
    by_id = {s["source_id"]: s for s in (scfg.get("sources") or [])}
    assert {"git-aoc-repo", "rss-arxiv-cs-ai", "rss-github-pgvector-releases", "rss-github-letta-releases"} <= set(by_id)
    assert by_id["rss-arxiv-q-bio-nc"]["enabled"] is False
    assert by_id["rss-arxiv-cs-cy"]["enabled"] is False
