"""V5.2.1 hotfix regression tests covering overnight audit findings:
1. Distinct skill reuse telemetry in reproduction drive (one hot skill must not mask zero-reuse skills).
2. Explicit skill refactoring `.aoc_base_skill/` fallback + unmodified-baseline guard.
3. Stalled topic failure circuit breaker, comfort-zone adjacent domains & mutated related_key dedup.
4. Startup rehydration of persisted dynamic world sensor domains (+ sensors.yaml allowlist updates).
5. Valid started_at <= completed_at timestamps for brain_research and skill_runtime executions.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.agents.registry import AgentRegistry
from app.cognition.goal_dedup import GoalDedupEngine, select_best_goal_candidate
from app.cognition.interfaces import (
    ActionPlan,
    AssessmentResult,
    BrainInterface,
    BrainResearchOutput,
    EvaluationContext,
    EvaluationResult,
    GoalProposal,
    ResearchSynthesisContext,
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
    get_drive_config,
    get_sensors_config,
)
from app.core.enums import DriveType, PermissionLevel
from app.core.runtime import OrganismRuntime
from app.drives.engine import DriveEngine
from app.drives.instinct_guard import guard_states
from app.drives.interfaces import DriveState
from app.governance.interfaces import PolicyVerdict
from app.governance.policy import GovernanceManager
from app.perception.interfaces import Observation, utcnow
from app.perception.world.exploration import ExplorationManager, _is_comfort_zone_domain
from app.perception.world.pipeline import WorldEventPipeline
from app.perception.world.rss_sensor import RSSWorldSensor
from app.perception.world.web_sensor import WebChangeWorldSensor
from app.self_model.interfaces import SkillStatus
from app.self_model.skills import SkillRegistryManager
from app.skills.evolution import SkillEvolutionEngine
from app.storage.database import Database
from app.storage.models import AgentExecutionRow, AgentTaskRow, Base, SkillInvocationRow, SkillRegistryRow
from app.storage.repository import StateRepository
from app.tools.interfaces import ToolCallRequest, ToolCallResult, ToolSpec
from app.tools.mcp_manager import MCPClientManager


async def _init_db(db: Database) -> None:
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _healthy_llm_probe() -> dict[str, Any]:
    return {"available": True, "latency_ms": 2.0, "model": "stub-v52-1"}


def _sample_observation() -> Observation:
    return Observation(
        observed_at=utcnow(),
        system_state={
            "disk": {"free_ratio": 0.35},
            "memory": {"available_ratio": 0.55},
            "cpu": {"load_per_core": 0.3, "cores": 4},
            "llm": {"available": True, "latency_ms": 150.0},
            "reliability": {"recent_failure_rate": 0.35},
        },
        environment_state={
            "capabilities_acquired": [
                "coding",
                "research",
                "repository_analysis",
                "system_diagnosis",
                "knowledge_summarization",
            ],
            "unknown_topics": [{"topic": f"u_{i}", "description": "unknown"} for i in range(4)],
        },
        available_resources={
            "agents": {"healthy_count": 1},
            "budget": {"tasks_used": 10, "tasks_limit": 100},
        },
        confidence=1.0,
    )


@pytest.mark.asyncio
async def test_reproduction_distinct_skill_reuse_not_saturated_by_one_hot_skill(tmp_path: Path):
    db_path = tmp_path / "v521_rep_reuse.db"
    db = Database(f"sqlite+aiosqlite:///{db_path}")
    await _init_db(db)
    settings = Settings(
        api=ApiSettings(host="127.0.0.1", auth_enabled=False),
        database=DatabaseSettings(url=f"sqlite+aiosqlite:///{db_path}"),
        memory=MemorySettings(provider="none", embedding_dim=128),
        embedding=EmbeddingSettings(provider="hash_fallback", dimensions=128),
        governance=GovernanceSettings(workspace_root=str(tmp_path / "ws")),
        perception=PerceptionSettings(llm_probe=False),
        runner=RunnerSettings(enabled=False),
        kernel_v3=KernelV3Settings(reproduction_quality_scoring_enabled=True),
        social=SocialSettings(enabled=False),
    )
    rt = OrganismRuntime(settings=settings, db=db, auto_load_agents=False, llm_probe_fn=_healthy_llm_probe)
    await rt.start(start_scheduler=False)
    try:
        # Register 22 VALIDATED skills: 1 hot skill reused 18 times, 21 zero-reuse skills
        async with db.session() as s:
            hot_id = uuid.uuid4()
            s.add(
                SkillRegistryRow(
                    id=hot_id,
                    skill_key="repository_coding_owasp_llm_top10",
                    canonical_name="repository.coding.owasp_llm_top10",
                    version="1.0.0",
                    capability_key="coding",
                    description="OWASP LLM Top10",
                    status=SkillStatus.VALIDATED.value,
                    reuse_count=18,
                    invocation_count=18,
                )
            )
            for idx in range(18):
                s.add(
                    SkillInvocationRow(
                        skill_id=hot_id,
                        skill_version="1.0.0",
                        invocation_source="skill_first",
                        is_cross_task_reuse=True,
                        passed=True,
                        latency_ms=12.0,
                        estimated_saved_tokens=500,
                        created_at=utcnow() - timedelta(minutes=idx + 1),
                    )
                )
            for idx in range(21):
                s.add(
                    SkillRegistryRow(
                        id=uuid.uuid4(),
                        skill_key=f"zero_reuse_skill_{idx}",
                        canonical_name=f"repo.coding.zero_reuse_{idx}",
                        version="1.0.0",
                        capability_key="coding",
                        description="Never reused",
                        status=SkillStatus.VALIDATED.value,
                        reuse_count=0,
                        invocation_count=0,
                    )
                )
            await s.commit()

        # Enqueue 4 pending skill refactors & seed recent execution reliability (3 succeeded / 2 failed = 0.60)
        for i in range(4):
            await rt.repo.upsert_world_item(
                kind="skill_refactor_queue",
                key=f"refactor:zero_reuse_skill_{i}",
                value={"skill_key": f"zero_reuse_skill_{i}", "canonical_name": f"repo.coding.zero_reuse_{i}"},
                status="open",
                source="meta_reflection",
            )
        dummy_task_id = await rt.repo.create_task(
            objective="recent probe",
            description="recent probe",
            assigned_agent="codex",
            expected_output="ok",
            workspace=str(tmp_path / "ws"),
            timeout_seconds=60,
            permission_level="L1",
            status="FAILED",
        )
        for idx, st in enumerate(["FAILED", "TIMEOUT", "SUCCEEDED", "SUCCEEDED", "SUCCEEDED"]):
            ex_id = await rt.repo.create_execution(
                dummy_task_id,
                "codex",
                st,
                attempt_number=idx + 1,
                idempotency_key=f"probe:{idx}",
            )
            await rt.repo.update_execution(
                ex_id,
                started_at=utcnow() - timedelta(minutes=10 - idx),
                completed_at=utcnow() - timedelta(minutes=9 - idx),
            )

        ctx = await rt.loop._build_drive_context(
            obs=_sample_observation(),
            assessment=None,
            now=utcnow(),
        )
        assert ctx.skill_reuse_ratio_recent is not None
        assert ctx.skill_reuse_ratio_recent == pytest.approx(1 / 22, abs=1e-4)
        assert ctx.pending_skill_refactor_count == 4

        engine = DriveEngine(config=get_drive_config())
        prev_cur = DriveState(
            drive_type=DriveType.CURIOSITY.value,
            satisfaction=0.35,
            deficit=0.65,
            urgency=0.62,
            confidence=1.0,
            last_satisfied_at=utcnow() - timedelta(seconds=60),
        )
        states = guard_states(engine.compute_states(ctx, {DriveType.CURIOSITY.value: prev_cur}))
        rep = states[DriveType.REPRODUCTION.value]
        assert rep.dimensions["skill_reuse_quality"] < 0.10
        assert rep.satisfaction < 0.45
        assert rep.urgency > 0.75

        decision = engine.arbitrate(states)
        assert decision.selected_drive == DriveType.REPRODUCTION.value
    finally:
        await rt.stop()
        await db.dispose()


@pytest.mark.asyncio
async def test_explicit_skill_refactor_falls_back_to_staged_dir_and_guards_unmodified(tmp_path: Path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'v521_staged_fallback.db'}")
    await _init_db(db)
    try:
        repo = StateRepository(db)
        gov = GovernanceManager(GovernanceSettings(workspace_root=str(tmp_path / "ws")), repo)
        skills = SkillRegistryManager(
            repo=repo,
            governance=gov,
            config={"skill_storage_dir": str(tmp_path / "skills"), "skill_test_timeout_seconds": 5.0},
            explicit_skill_refactor_enabled=True,
        )
        evo = SkillEvolutionEngine(repo=repo, skill_registry=skills, dedup_threshold=0.50)
        skills.evolution_engine = evo

        base_art = (
            "def execute(data: dict) -> dict:\n"
            "    return {'items': list(data.get('items') or []), 'mode': 'base'}\n"
        )
        base_test = (
            "from skill import execute\n"
            "assert execute({'items': ['a']})['mode'] == 'base'\n"
        )
        v1 = await skills.register_skill(
            skill_key="repository_research_standard_mapping_report",
            canonical_name="repository.research.standard_mapping_report",
            version="1.0.0",
            capability_key="research",
            description="Generate deterministic mapping report",
            artifact_code=base_art,
            test_spec={"kind": "python_assertions", "code": base_test},
            permission_level=PermissionLevel.L1.value,
        )
        await skills.validate_skill(v1["id"])
        target = {
            "skill_key": "repository_research_standard_mapping_report",
            "canonical_name": "repository.research.standard_mapping_report",
            "target_generalization": "Support fallback_mode flag cleanly",
        }

        # Case A: Unmodified `.aoc_base_skill/` fallback without root skill.py -> must NOT evolve emptily
        ws_unmod = Path(gov.prepare_workspace("task-unmodified"))
        await skills.stage_base_skill_in_workspace(str(ws_unmod), target)
        cryst_unmod = await skills.crystallize_from_workspace(
            workspace=str(ws_unmod),
            skill_key="repository_research_standard_mapping_report",
            capability_key="research",
            description="No actual edits made",
            permission_level="L1",
            skill_refactor_target=target,
        )
        assert cryst_unmod is not None
        assert cryst_unmod["crystallized"] is False
        assert cryst_unmod["evolved"] is False
        assert cryst_unmod["reason"] == "staged_base_skill_unmodified"

        # Case B: Codex edited inside `.aoc_base_skill/` without copying to root -> evolves to v1.1.0
        ws_edited = Path(gov.prepare_workspace("task-staged-edited"))
        await skills.stage_base_skill_in_workspace(str(ws_edited), target)
        (ws_edited / ".aoc_base_skill" / "skill.py").write_text(
            "def execute(data: dict) -> dict:\n"
            "    items = list(data.get('items') or [])\n"
            "    return {'items': items, 'mode': 'base', 'fallback_used': len(items) == 0}\n",
            encoding="utf-8",
        )
        (ws_edited / ".aoc_base_skill" / "test_skill.py").write_text(
            base_test + "assert execute({})['fallback_used'] is True\n",
            encoding="utf-8",
        )
        cryst_ok = await skills.crystallize_from_workspace(
            workspace=str(ws_edited),
            skill_key="repository_research_standard_mapping_report",
            capability_key="research",
            description="Generalize with fallback_used flag",
            permission_level="L1",
            skill_refactor_target=target,
        )
        assert cryst_ok is not None
        assert cryst_ok["explicit_refactor"] is True
        assert cryst_ok["evolved"] is True
        assert cryst_ok["promoted"] is True
        assert cryst_ok["version"] == "1.1.0"
        assert cryst_ok["status"] == SkillStatus.VALIDATED.value
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_stalled_topic_circuit_breaker_comfort_zone_and_mutated_related_key_dedup(tmp_path: Path):
    assert _is_comfort_zone_domain("mcp_tool_provenance") is True
    assert _is_comfort_zone_domain("ai_security") is True
    assert _is_comfort_zone_domain("agent_tool_safety") is True
    assert _is_comfort_zone_domain("sports_health") is False
    assert _is_comfort_zone_domain("psychology_cognition") is False
    assert _is_comfort_zone_domain("ai_industry") is False

    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'v521_stalled.db'}")
    await _init_db(db)
    try:
        repo = StateRepository(db)
        now = utcnow()
        await repo.upsert_world_item(
            kind="knowledge_topic",
            key="world:web-mcp-spec-github-page-updated-github-modelcontextprotocol-mode:492a698f",
            value={"title": "Page updated: GitHub - modelcontextprotocol/modelcontextprotocol (mcp spec 5035-5038)", "source_id": "web-mcp-spec-github"},
            status="open",
            source="world_sensor",
        )
        await repo.upsert_world_item(
            kind="knowledge_topic",
            key="world:sports-hrv-recovery:abcd",
            value={"title": "HRV and sleep recovery metrics in endurance athletes", "source_id": "rss-sports"},
            status="open",
            source="world_sensor",
        )

        em = ExplorationManager(repo=repo, interest_portfolio_enabled=True, max_same_domain_streak=3)
        recent_goals = [
            {
                "goal_id": str(uuid.uuid4()),
                "drive_type": "curiosity",
                "title": "Close MCP spec 5,035→5,038 commit drift with quarantined closure evidence",
                "description": "mcp provenance verification",
                "related_key": "inv:mcp_spec_commit_drift_5035_5038_authoritative",
                "expected_outcome": {"exploration_domain": "mcp_tool_provenance"},
                "status": "FAILED",
                "created_at": now - timedelta(minutes=15),
            },
            {
                "goal_id": str(uuid.uuid4()),
                "drive_type": "curiosity",
                "title": "Reconcile MCP spec 5035->5038 commit range via Atom feed evidence",
                "description": "mcp provenance verification",
                "related_key": "inv:mcp_spec_commit_range_5035_5038_closure",
                "expected_outcome": {"exploration_domain": "mcp_tool_provenance"},
                "status": "FAILED",
                "created_at": now - timedelta(minutes=45),
            },
            {
                "goal_id": str(uuid.uuid4()),
                "drive_type": "curiosity",
                "title": "用公开只读证据关闭或失败关闭 MCP 规范 5035→5038 提交漂移",
                "description": "mcp provenance check",
                "related_key": "mcp_spec_commit_range_5035_to_5038_authoritative_evidence",
                "expected_outcome": {"exploration_domain": "mcp_tool_provenance"},
                "status": "FAILED",
                "created_at": now - timedelta(minutes=90),
            },
            {
                "goal_id": str(uuid.uuid4()),
                "drive_type": "curiosity",
                "title": "Close MCP spec 5035-5038 commit drift with GitHub snapshot",
                "description": "mcp provenance snapshot",
                "related_key": "inv:mcp_spec_commit_range_5035_5038_snap",
                "expected_outcome": {"exploration_domain": "mcp_tool_provenance"},
                "status": "ACHIEVED",
                "created_at": now - timedelta(minutes=120),
            },
        ]

        guidance = await em.build_guidance(recent_goals)
        assert guidance["comfort_zone_streak"] == 4
        assert "mcp_tool_provenance" in guidance["cooldown_domains"]
        assert guidance["must_pivot_domain"] is True
        stalled = guidance["stalled_topic_clusters"]
        assert len(stalled) == 1
        assert stalled[0]["failed_count"] == 3
        sugg_keys = {s["key"] for s in guidance["suggested_external_topics"]}
        assert "world:web-mcp-spec-github-page-updated-github-modelcontextprotocol-mode:492a698f" not in sugg_keys
        assert "world:sports-hrv-recovery:abcd" in sugg_keys

        # Primary stalls on MCP 5035->5038, alternative pivots cleanly to sports_health
        primary_stalled = GoalProposal(
            should_act=True,
            title="Close open MCP spec topic 492a698f: verify 5,035→5,038 commit drift via GitHub compare",
            description="Try MCP 5035-5038 closure again",
            drive_type="curiosity",
            related_key="inv:mcp_spec_commit_retry_v99",
            predicted_ig=0.95,
            alternative_goals=[
                {
                    "title": "Research sports science HRV sleep recovery protocols",
                    "description": "Compare public sports physiology studies",
                    "related_key": "sports:hrv_sleep",
                    "predicted_ig": 0.45,
                    "exploration_topic": {"topic_key": "hrv_sleep", "domain": "sports_health", "question": "How does HRV reflect training load?"},
                }
            ],
        )
        dedup_eng = GoalDedupEngine(dedup_window_hours=6.0, lexical_threshold=0.72)
        sel_alt = select_best_goal_candidate(
            primary=primary_stalled,
            drive_type="curiosity",
            recent_goals=[],
            dedup_engine=dedup_eng,
            now=now,
            must_pivot_domain=True,
            cooldown_domains=guidance["cooldown_domains"],
            stalled_topic_clusters=stalled,
        )
        assert sel_alt.selected_proposal is not None
        assert sel_alt.used_alternative is True
        assert "HRV" in sel_alt.selected_proposal.title

        # Without alternatives, stalled topic MUST DEFER via STALLED_TOPIC_CIRCUIT_BREAKER
        solo_stalled = primary_stalled.model_copy(update={"alternative_goals": []})
        sel_cb = select_best_goal_candidate(
            primary=solo_stalled,
            drive_type="curiosity",
            recent_goals=[],
            dedup_engine=dedup_eng,
            now=now,
            stalled_topic_clusters=stalled,
        )
        assert sel_cb.selected_proposal is None
        assert sel_cb.selected_dedup.decision == "DEFER"
        assert sel_cb.selected_dedup.reason_code == "STALLED_TOPIC_CIRCUIT_BREAKER"
        assert sel_cb.selected_dedup.cooldown_until is not None and sel_cb.selected_dedup.cooldown_until > now

        # Distinct related_keys (<0.45 Jaccard) still bypass paraphrase blocking
        d_distinct = dedup_eng.evaluate(
            proposal=GoalProposal(
                should_act=True,
                title="Run standard security audit check across target node",
                description="Audit security check",
                drive_type="curiosity",
                related_key="mod:node_alpha",
            ),
            drive_type="curiosity",
            recent_goals=[
                {
                    "goal_id": str(uuid.uuid4()),
                    "drive_type": "curiosity",
                    "title": "Run standard security audit check across target node",
                    "description": "Audit security check",
                    "related_key": "mod:node_beta",
                    "status": "ACHIEVED",
                    "verified_ig": 0.6,
                    "created_at": now - timedelta(minutes=10),
                }
            ],
            now=now,
        )
        assert d_distinct.decision == "ACCEPT"

        # Mutated near-duplicate related_keys (>=0.45 Jaccard) enter lexical paraphrase check
        d_mutated = dedup_eng.evaluate(
            proposal=GoalProposal(
                should_act=True,
                title="Close MCP spec 5035 5038 commit range with public GitHub evidence",
                description="Verify MCP spec commit range 5035 to 5038",
                drive_type="curiosity",
                related_key="inv:mcp_spec_commit_range_5035_5038_closure",
            ),
            drive_type="curiosity",
            recent_goals=[
                {
                    "goal_id": str(uuid.uuid4()),
                    "drive_type": "curiosity",
                    "title": "Close MCP spec 5035 5038 commit drift with public GitHub evidence",
                    "description": "Verify MCP spec commit range 5035 to 5038",
                    "related_key": "inv:mcp_spec_commit_drift_5035_5038_authoritative",
                    "status": "FAILED",
                    "created_at": now - timedelta(minutes=10),
                }
            ],
            now=now,
        )
        assert d_mutated.decision == "REJECT"
        assert d_mutated.reason_code == "DUPLICATE_GOAL_LEXICAL"
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_world_pipeline_rehydrates_persisted_dynamic_domains_on_startup(tmp_path: Path):
    scfg = get_sensors_config()
    allowed = set(scfg.get("allowed_domains") or [])
    assert {"owasp.org", "genai.owasp.org", "modelcontextprotocol.io"} <= allowed

    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'v521_rehydrate.db'}")
    await _init_db(db)
    try:
        repo = StateRepository(db)
        await repo.upsert_world_source(
            source_id="web-dynamic-mcp",
            sensor_type="web",
            name="Dynamic MCP Spec",
            config={"url": "https://modelcontextprotocol.io/specification/2025-06-18/server/tools"},
            enabled=True,
        )
        await repo.upsert_world_source(
            source_id="rss-dynamic-disabled",
            sensor_type="rss",
            name="Disabled Source",
            config={"url": "https://disabled.example.org/feed.xml"},
            enabled=False,
        )
        await repo.upsert_world_source(
            source_id="rss-unsafe-localhost",
            sensor_type="rss",
            name="Localhost Forbidden",
            config={"url": "https://localhost/feed.xml"},
            enabled=True,
        )

        rss = RSSWorldSensor(allowed_domains=["github.com"])
        web = WebChangeWorldSensor(allowed_domains=["github.com"])
        pipeline = WorldEventPipeline(
            repo=repo,
            sensors=[rss, web],
            allowed_domains=["github.com"],
            open_public_https=True,
        )
        await pipeline.sync_configured_sources([])

        assert "modelcontextprotocol.io" in pipeline.allowed_domains
        assert "modelcontextprotocol.io" in rss.allowed_domains
        assert "modelcontextprotocol.io" in web.allowed_domains
        assert "disabled.example.org" not in pipeline.allowed_domains
        assert "localhost" not in pipeline.allowed_domains
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_brain_research_and_skill_first_record_valid_started_completed_timestamps(tmp_path: Path):
    db_path = tmp_path / "v521_timestamps.db"
    db = Database(f"sqlite+aiosqlite:///{db_path}")
    await _init_db(db)

    class _StubMCP(MCPClientManager):
        def __init__(self, *, governance: Any, repo: Any, workspace_root: str) -> None:
            super().__init__(governance=governance, repo=repo, workspace_root=workspace_root, searxng_url="http://mock")
            self.register_tool(ToolSpec(tool_name="web_search", server_name="s", description="search", permission_level="L0"))

        async def invoke(self, req: ToolCallRequest) -> ToolCallResult:
            return ToolCallResult(
                tool_name="web_search",
                server_name="s",
                ok=True,
                output={"query": "hrv", "count": 1, "results": [{"title": "HRV", "url": "https://example.org/hrv", "snippet": "HRV reflects recovery."}]},
                latency_ms=5.0,
            )

    class _StubBrain(BrainInterface):
        async def assess(self, observation: Observation, memories: list[dict[str, Any]], drives: list[Any]) -> AssessmentResult:
            return AssessmentResult(summary="ok", confidence=0.8)

        async def generate_goal(self, context: Any) -> GoalProposal:
            return GoalProposal(should_act=True, title="stub", description="stub", drive_type="curiosity")

        async def plan(self, context: Any) -> ActionPlan:
            return ActionPlan(should_execute=False)

        async def synthesize_research(self, context: ResearchSynthesisContext) -> BrainResearchOutput:
            return BrainResearchOutput(
                key_findings=["HRV is an objective autonomic recovery biomarker."],
                synthesis_markdown="### HRV\nHRV tracks recovery readiness.",
                cited_sources=["https://example.org/hrv"],
                confidence=0.85,
                human_brief="HRV 可反映训练恢复状态。",
            )

        async def evaluate(self, context: EvaluationContext) -> EvaluationResult:
            return EvaluationResult(goal_achieved=True, summary="ok")

    settings = Settings(
        api=ApiSettings(host="127.0.0.1", auth_enabled=False),
        database=DatabaseSettings(url=f"sqlite+aiosqlite:///{db_path}"),
        memory=MemorySettings(provider="none"),
        embedding=EmbeddingSettings(provider="hash_fallback"),
        governance=GovernanceSettings(workspace_root=str(tmp_path / "ws")),
        perception=PerceptionSettings(llm_probe=False),
        runner=RunnerSettings(enabled=False),
        kernel_v3=KernelV3Settings(brain_research_enabled=True, skill_runtime_enabled=True),
        social=SocialSettings(enabled=False),
    )
    rt = OrganismRuntime(settings=settings, db=db, brain=_StubBrain(), registry=AgentRegistry(), auto_load_agents=False, llm_probe_fn=_healthy_llm_probe)
    stub_mcp = _StubMCP(governance=rt.governance, repo=rt.repo, workspace_root=str(tmp_path / "ws"))
    rt.mcp_manager = stub_mcp
    await rt.start(start_scheduler=False)
    rt.loop.mcp_manager = stub_mcp
    try:
        ident = await rt.repo.get_or_create_identity("AOC-TS", "5.2.1", {})
        cid, _ = await rt.repo.start_cycle(ident["id"], "ACTIVE")
        gid = await rt.repo.create_goal(ident["id"], cid, drive_type="curiosity", title="Test timestamps", description="d", status="ACTIVE")
        verdict = PolicyVerdict(allowed=True, denied=False, requested_level="L0", effective_level="L0", sandbox_mode="read-only")
        pid = await rt.repo.create_plan(gid, cid, {"objective": "o"}, "L0", verdict.model_dump(mode="json"), "APPROVED")

        await rt.loop._try_brain_native_research(
            cycle_id=cid,
            goal_id=gid,
            plan_id=pid,
            goal=GoalProposal(should_act=True, title="HRV study", description="study", drive_type="curiosity"),
            plan=ActionPlan(
                should_execute=True,
                goal_title="HRV study",
                objective="Study HRV",
                execution_mode="brain_research",
                assigned_agent="brain_research",
                permission_level="L0",
                verification=[VerificationSpec(kind="non_empty_output")],
            ),
            verdict=verdict,
            recalled_memories=[],
            active_investigations=[],
            human_guidance=[],
            llm_available=True,
        )

        sk = await rt.skill_registry.register_skill(
            skill_key="demo_echo_ts",
            canonical_name="demo.coding.echo_ts",
            version="1.0.0",
            capability_key="coding",
            description="Echo",
            artifact_code="def execute(data): return {'ok': True, 'val': data.get('x', 1)}\n",
            test_spec={"kind": "python_assertions", "code": "from skill import execute\nassert execute({'x': 2})['val'] == 2\n"},
            permission_level="L0",
        )
        await rt.skill_registry.validate_skill(sk["id"])
        await rt.loop._try_skill_first_execution(
            cycle_id=cid,
            goal_id=gid,
            plan_id=pid,
            plan=ActionPlan(
                should_execute=True,
                goal_title="Echo skill",
                objective="Run demo_echo_ts",
                execution_mode="skill_first",
                assigned_agent="skill_runtime",
                permission_level="L0",
                skill_invocation={"skill_key": "demo_echo_ts", "input_data": {"x": 42}},
            ),
            verdict=verdict,
        )

        async with db.session() as s:
            rows = (
                await s.execute(
                    select(AgentExecutionRow).where(
                        AgentExecutionRow.agent_id.in_(["brain_research", "skill_runtime"])
                    )
                )
            ).scalars().all()
        assert len(rows) == 2
        for r in rows:
            assert r.started_at is not None
            assert r.completed_at is not None
            assert r.completed_at >= r.started_at
    finally:
        await rt.stop()
        await db.dispose()


@pytest.mark.asyncio
async def test_finalizer_post_drive_preserves_reproduction_quality_without_false_inhibition(tmp_path: Path):
    """V5.2.2: Completing a curiosity goal must NOT reset reproduction to legacy count-only mode in post_drives."""
    db_path = tmp_path / "v522_finalizer_rep.db"
    db = Database(f"sqlite+aiosqlite:///{db_path}")
    await _init_db(db)

    class _StubMCP(MCPClientManager):
        def __init__(self, *, governance: Any, repo: Any, workspace_root: str) -> None:
            super().__init__(governance=governance, repo=repo, workspace_root=workspace_root, searxng_url="http://mock")
            self.register_tool(ToolSpec(tool_name="web_search", server_name="s", description="search", permission_level="L0"))

        async def invoke(self, req: ToolCallRequest) -> ToolCallResult:
            return ToolCallResult(
                tool_name="web_search",
                server_name="s",
                ok=True,
                output={"query": "q", "count": 1, "results": [{"title": "Ref", "url": "https://example.org/r", "snippet": "Detailed study."}]},
                latency_ms=5.0,
            )

    class _CuriosityCycleBrain(BrainInterface):
        async def assess(self, observation: Observation, memories: list[dict[str, Any]], drives: list[Any]) -> AssessmentResult:
            return AssessmentResult(summary="ok", confidence=0.9)

        async def generate_goal(self, context: Any) -> GoalProposal:
            return GoalProposal(
                should_act=True,
                title="Explore external neuroscience findings",
                description="Research external neuroscience findings",
                drive_type=context.decision.selected_drive,
                related_key="neuro:findings_1",
                predicted_ig=0.6,
            )

        async def plan(self, context: Any) -> ActionPlan:
            return ActionPlan(
                should_execute=True,
                goal_title=context.goal.title if context.goal else "Explore",
                objective="Synthesize external findings",
                execution_mode="brain_research",
                assigned_agent="brain_research",
                permission_level="L0",
                verification=[VerificationSpec(kind="non_empty_output")],
            )

        async def synthesize_research(self, context: ResearchSynthesisContext) -> BrainResearchOutput:
            return BrainResearchOutput(
                key_findings=["Synaptic plasticity modulates attention shifts."],
                synthesis_markdown="### Findings\nSynaptic plasticity modulates attention shifts across cortical networks.",
                cited_sources=["https://example.org/r"],
                confidence=0.88,
                human_brief="突触可塑性调控注意力转移。",
            )

        async def evaluate(self, context: EvaluationContext) -> EvaluationResult:
            return EvaluationResult(
                goal_achieved=True,
                summary="Achieved curiosity research goal cleanly.",
                information_gain=0.65,
                should_persist_memory=True,
            )

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
            reproduction_quality_scoring_enabled=True,
        ),
        social=SocialSettings(enabled=False),
    )
    rt = OrganismRuntime(
        settings=settings,
        db=db,
        brain=_CuriosityCycleBrain(),
        registry=AgentRegistry(),
        auto_load_agents=False,
        llm_probe_fn=_healthy_llm_probe,
    )
    stub_mcp = _StubMCP(governance=rt.governance, repo=rt.repo, workspace_root=str(tmp_path / "ws"))
    rt.mcp_manager = stub_mcp
    await rt.start(start_scheduler=False)
    rt.loop.mcp_manager = stub_mcp
    try:
        assert rt.finalizer.reproduction_quality_scoring_enabled is True
        async with db.session() as s:
            hot_id = uuid.uuid4()
            s.add(
                SkillRegistryRow(
                    id=hot_id,
                    skill_key="hot_skill",
                    canonical_name="repo.coding.hot_skill",
                    version="1.0.0",
                    capability_key="coding",
                    description="Hot skill",
                    status=SkillStatus.VALIDATED.value,
                    reuse_count=10,
                    invocation_count=10,
                )
            )
            for idx in range(21):
                s.add(
                    SkillRegistryRow(
                        id=uuid.uuid4(),
                        skill_key=f"cold_skill_{idx}",
                        canonical_name=f"repo.coding.cold_{idx}",
                        version="1.0.0",
                        capability_key="coding",
                        description="Cold skill",
                        status=SkillStatus.VALIDATED.value,
                        reuse_count=0,
                        invocation_count=0,
                    )
                )
            await s.commit()

        for i in range(4):
            await rt.repo.upsert_world_item(
                kind="skill_refactor_queue",
                key=f"refactor:cold_skill_{i}",
                value={"skill_key": f"cold_skill_{i}", "canonical_name": f"repo.coding.cold_{i}"},
                status="open",
                source="meta_reflection",
            )
        # Seed environment knowledge topics & strong curiosity deficit so curiosity wins cycle 1
        for i in range(5):
            await rt.repo.upsert_world_item(
                kind="knowledge_topic",
                key=f"world:neuro_{i}",
                value={"title": f"Neuro topic {i}", "description": "open"},
                status="open",
                source="world_sensor",
            )

        res = await rt.loop.run_cycle()
        assert res["outcome"] == "SUCCEEDED"

        saved_drives = {d["drive_type"]: DriveState(**d) for d in await rt.repo.load_drive_states(rt.organism_id)}
        post_rep = saved_drives[DriveType.REPRODUCTION.value]
        post_cur = saved_drives[DriveType.CURIOSITY.value]

        assert "skill_reuse_quality" in post_rep.dimensions
        assert float(post_rep.dimensions["skill_reuse_quality"] or 0.0) < 0.10
        assert post_rep.satisfaction < 0.65
        assert post_rep.last_satisfied_at is None
        assert post_cur.last_satisfied_at is not None

        next_dec = rt.drives.arbitrate(saved_drives, now=utcnow() + timedelta(seconds=30))
        assert next_dec.selected_drive == DriveType.REPRODUCTION.value
    finally:
        await rt.stop()
        await db.dispose()
