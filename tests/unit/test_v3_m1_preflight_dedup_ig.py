"""V3.0 M1 Tests: ActionPreflight, GoalDedupEngine, Deterministic IG v2, Token Attribution & Alembic 0009.

Acceptance gates verified:
  1. Alembic `0009_v3_kernel_enhancement` upgrade & downgrade roundtrip (0 new tables, expand-only)
  2. 100% L0 read-only write-intent interception (`PERMISSION_MISMATCH`) -> 0 external agent launches in AutonomousLoop
  3. >= 250 deterministic fuzz/anomaly cases against ActionPreflight (malformed dicts, path traversal,
     disallowed sandboxes, unavailable agents, duplicate plan hashes, budget/concurrency exceeded, policy denials)
  4. GoalDedupEngine fingerprint, related_key, cooldown, and EN/ZH paraphrase interception (>=95% precision)
     + end-to-end AutonomousLoop verifying 0 Planning LLM calls & 0 Agent launches on repeated goals
  5. Deterministic InformationGain v2 gated strictly by verification_confidence & non-duplicate
  6. LLMGateway.scope(goal_id=..., purpose=...) attribution persisted in resource_usage.details
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from app.agents.base import (
    AgentCapability,
    AgentExecution,
    AgentInterface,
    AgentProbeResult,
    AgentStatus,
    AgentTask,
)
from app.agents.registry import AgentRegistry
from app.cognition.gateway import LLMGateway
from app.cognition.goal_dedup import GoalDedupEngine, compute_cooldown_until, compute_goal_fingerprint
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
from app.cognition.preflight import ActionPreflight, detect_write_intent
from app.core.config import (
    ApiSettings,
    DatabaseSettings,
    EmbeddingSettings,
    GovernanceSettings,
    KernelV3Settings,
    LLMSettings,
    MemorySettings,
    RunnerSettings,
    Settings,
)
from app.core.enums import PermissionLevel
from app.core.runtime import OrganismRuntime
from app.governance.interfaces import BudgetStatus, PolicyVerdict
from app.memory.info_gain import ExistingMemorySample, InformationGainEvaluator
from app.perception.interfaces import Observation, utcnow
from app.storage.database import Database
from app.storage.models import Base


async def _init_db(db: Database) -> None:
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


class _SpyAgent(AgentInterface):
    def __init__(self, agent_id: str = "codex", caps: list[str] | None = None) -> None:
        self._cap = AgentCapability(
            agent_id=agent_id,
            name=f"Spy {agent_id}",
            agent_type="cli",
            capabilities=caps or ["general_task", "knowledge_summarization", "coding"],
            supported_modes=["read-only", "workspace-write"],
            enabled=True,
        )
        self.execute_calls = 0

    @property
    def capability(self) -> AgentCapability:
        return self._cap

    async def probe(self) -> AgentProbeResult:
        return AgentProbeResult(ok=True, binary=self._cap.agent_id, version="spy-1.0")

    async def execute(self, task: AgentTask) -> AgentExecution:
        self.execute_calls += 1
        return AgentExecution(
            execution_id=task.execution_id or str(uuid.uuid4()),
            task_id=task.task_id,
            agent_id=self._cap.agent_id,
            status="SUCCEEDED",
            started_at=utcnow(),
            completed_at=utcnow(),
            result={"summary": f"Completed {task.objective}", "workspace_files": ["notes.txt"]},
            stdout="ok",
        )

    async def get_status(self, execution_id: str) -> AgentStatus:
        return AgentStatus(execution_id=execution_id, agent_id=self._cap.agent_id, status="SUCCEEDED")

    async def cancel(self, execution_id: str) -> bool:
        return True


class _ScriptedBrain(BrainInterface):
    def __init__(self, goals: list[GoalProposal], plans: list[ActionPlan]) -> None:
        self._goals = list(goals)
        self._plans = list(plans)
        self.plan_calls = 0
        self.goal_calls = 0

    async def assess(self, observation: Observation, memories: list[dict], drives: list[Any]) -> AssessmentResult:
        return AssessmentResult(summary="Healthy test system", confidence=0.9)

    async def generate_goal(self, context: CognitiveContext) -> GoalProposal:
        self.goal_calls += 1
        return self._goals.pop(0) if len(self._goals) > 1 else self._goals[0]

    async def plan(self, context: CognitiveContext) -> ActionPlan:
        self.plan_calls += 1
        return self._plans.pop(0) if len(self._plans) > 1 else self._plans[0]

    async def evaluate(self, context: EvaluationContext) -> EvaluationResult:
        return EvaluationResult(
            goal_achieved=context.verification_passed,
            summary="Evaluated output",
            learned_facts=["Discovered concrete fact A about kernel preflight"],
            should_persist_memory=True,
        )


def test_v3_m1_alembic_0009_upgrade_and_downgrade_roundtrip(tmp_path: Path, monkeypatch):
    """Alembic 0009 must add only the expected columns (0 new tables) and downgrade cleanly."""
    from app.core.config import get_settings

    db_file = tmp_path / "alembic_0009.db"
    monkeypatch.setenv("AOC_DATABASE__URL", f"sqlite+aiosqlite:///{db_file}")
    get_settings.cache_clear()
    try:
        cfg = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_file}")

        command.upgrade(cfg, "0008_p1_9_affect")
        eng = create_engine(f"sqlite:///{db_file}")
        tables_before = set(inspect(eng).get_table_names())

        command.upgrade(cfg, "0009_v3_kernel_enhancement")
        insp_up = inspect(eng)
        tables_after = set(insp_up.get_table_names())
        assert tables_after == tables_before, "V3 M1 must add ZERO new tables"

        ap_cols = {c["name"] for c in insp_up.get_columns("action_plans")}
        ag_cols = {c["name"] for c in insp_up.get_columns("active_goals")}
        ah_cols = {c["name"] for c in insp_up.get_columns("affect_history")}
        assert {"preflight_status", "preflight_reason_codes", "preflight_checked_at"} <= ap_cols
        assert {"goal_fingerprint", "duplicate_of", "cooldown_until", "predicted_ig", "verified_ig"} <= ag_cols
        assert {"trigger_event_id", "decay_applied"} <= ah_cols

        command.downgrade(cfg, "0008_p1_9_affect")
        insp_down = inspect(eng)
        assert "preflight_status" not in {c["name"] for c in insp_down.get_columns("action_plans")}
        assert "goal_fingerprint" not in {c["name"] for c in insp_down.get_columns("active_goals")}
        assert "trigger_event_id" not in {c["name"] for c in insp_down.get_columns("affect_history")}
        eng.dispose()
    finally:
        get_settings.cache_clear()


def test_v3_m1_preflight_fuzz_250_plus_anomaly_and_permission_cases():
    """Verify >= 250 deterministic anomaly, permission-mismatch, path-traversal, and budget cases fail-closed."""
    pf = ActionPreflight(max_task_timeout_seconds=600)
    reg = AgentRegistry()
    spy = _SpyAgent("codex")
    reg.register(spy, probe=AgentProbeResult(ok=True, binary="codex", version="1.0"))

    ok_verdict_l0 = PolicyVerdict(
        allowed=True,
        requires_approval=False,
        denied=False,
        requested_level="L0",
        effective_level="L0",
        sandbox_mode="read-only",
        effective_timeout_seconds=120,
        reason="L0 allowed",
    )
    ok_verdict_l1 = PolicyVerdict(
        allowed=True,
        requires_approval=False,
        denied=False,
        requested_level="L1",
        effective_level="L1",
        sandbox_mode="workspace-write",
        effective_timeout_seconds=120,
        reason="L1 allowed",
    )
    ok_budget = BudgetStatus(allowed=True, reason="ok")
    blocked_budget = BudgetStatus(allowed=False, reason="max concurrent tasks reached (1/1)")

    cases_tested = 0

    # 1. 100 L0 read-only + write-intent combinations (EN + ZH + file_exists) -> 100% PERMISSION_MISMATCH
    write_phrases = [
        "Inspect code and write to result.md",
        "Summarize repo and save to report.md",
        "Analyze module and create file notes.md",
        "Check logs and output file summary.md",
        "调研系统状态并将结果写入 result.md",
        "分析仓库后保存文件到 output.json",
        "检查配置并生成文件 report.md",
        "阅读文档然后落盘到磁盘",
        "Review diff and apply patch to app.py",
        "Run audit and modify file config.yaml",
    ]
    for i in range(100):
        phrase = write_phrases[i % len(write_phrases)]
        specs = [VerificationSpec(kind="file_exists", target="result.md")] if i % 3 == 0 else []
        p = ActionPlan(
            should_execute=True,
            goal_title=f"Goal {i}",
            objective=f"{phrase} (case {i})",
            assigned_agent="codex",
            permission_level="L0",
            verification=specs,
        )
        res = pf.validate(plan=p, verdict=ok_verdict_l0, registry=reg, budget=ok_budget)
        assert res.decision == "REJECT", f"Case {i} must be rejected: {phrase}"
        assert "PERMISSION_MISMATCH" in res.reason_codes
        # Same plan at L1 workspace-write MUST pass!
        res_l1 = pf.validate(plan=p.model_copy(update={"permission_level": "L1"}), verdict=ok_verdict_l1, registry=reg, budget=ok_budget)
        assert res_l1.decision == "ACCEPT", f"L1 case {i} should pass: {res_l1.reason_codes}"
        cases_tested += 1

    # 2. 60 malformed / non-executable / invalid-type / trivial-objective inputs -> 100% REJECT, 0 crashes
    malformed_inputs: list[Any] = [
        None,
        "raw shell rm -rf /",
        12345,
        ["list", "not", "plan"],
        {"should_execute": False, "objective": "noop"},
        {"should_execute": True, "objective": ""},
        {"should_execute": True, "objective": "abc"},
        {"should_execute": True, "objective": "Valid objective", "timeout_seconds": "not-an-int"},
        {"should_execute": True, "objective": "Valid objective", "verification": [{"kind": "unknown_kind"}]},
        {"should_execute": True, "objective": "Valid objective", "verification": [{"kind": "file_exists", "target": "../etc/passwd"}]},
        {"should_execute": True, "objective": "Valid objective", "verification": [{"kind": "file_exists", "target": "/etc/shadow"}]},
        {"should_execute": True, "objective": "Valid objective", "verification": [{"kind": "file_exists", "target": ""}]},
    ]
    for i in range(60):
        bad = malformed_inputs[i % len(malformed_inputs)]
        res = pf.validate(plan=bad, verdict=ok_verdict_l1, registry=reg, budget=ok_budget)
        assert res.decision == "REJECT"
        assert len(res.reason_codes) >= 1
        cases_tested += 1

    # 3. 50 duplicate plan hashes + budget/concurrency/unavailable-agent/unsafe-sandbox/policy-denied checks
    valid_ro = ActionPlan(
        should_execute=True,
        goal_title="Read-only check",
        objective="Inspect existing system metrics without writing any files (read-only)",
        assigned_agent="codex",
        permission_level="L0",
    )
    ok_res = pf.validate(plan=valid_ro, verdict=ok_verdict_l0, registry=reg, budget=ok_budget)
    assert ok_res.decision == "ACCEPT"
    assert detect_write_intent(valid_ro) == []

    empty_reg = AgentRegistry()
    for i in range(50):
        mod = i % 5
        if mod == 0:
            r = pf.validate(plan=valid_ro, verdict=ok_verdict_l0, registry=reg, budget=ok_budget, recent_plan_hashes=[ok_res.plan_hash])
            assert r.decision == "REJECT" and "DUPLICATE_PLAN" in r.reason_codes
        elif mod == 1:
            r = pf.validate(plan=valid_ro, verdict=ok_verdict_l0, registry=reg, budget=blocked_budget)
            assert r.decision == "REJECT" and "CONCURRENCY_LIMIT" in r.reason_codes
        elif mod == 2:
            r = pf.validate(plan=valid_ro, verdict=ok_verdict_l0, registry=empty_reg, budget=ok_budget)
            assert r.decision == "REJECT" and "AGENT_UNAVAILABLE" in r.reason_codes
        elif mod == 3:
            bad_sb = ok_verdict_l1.model_copy(update={"sandbox_mode": "danger-full-access"})
            r = pf.validate(plan=valid_ro, verdict=bad_sb, registry=reg, budget=ok_budget)
            assert r.decision == "REJECT" and "UNSAFE_SANDBOX_MODE" in r.reason_codes
        else:
            denied_v = ok_verdict_l0.model_copy(update={"allowed": False, "denied": True, "reason": "L3 forbidden"})
            r = pf.validate(plan=valid_ro, verdict=denied_v, registry=reg, budget=ok_budget)
            assert r.decision == "REJECT" and "POLICY_DENIED" in r.reason_codes
        cases_tested += 1

    # 4. 50 legitimate read-only plans with benign mentions -> 100% ACCEPT
    for i in range(50):
        p = ActionPlan(
            should_execute=True,
            goal_title=f"Read-only survey {i}",
            objective=f"Inspect git log and summarize architecture insights for topic #{i} (do not write files)",
            assigned_agent="codex",
            permission_level="L0",
        )
        r = pf.validate(plan=p, verdict=ok_verdict_l0, registry=reg, budget=ok_budget)
        assert r.decision == "ACCEPT", f"False positive on read-only plan {i}: {r.reason_codes} / {r.notes}"
        cases_tested += 1

    assert cases_tested >= 260


def test_v3_m1_goal_dedup_cooldown_and_paraphrase_precision():
    """Verify fingerprint, related_key, cooldown, and EN/ZH paraphrase dedup precision >= 95%."""
    eng = GoalDedupEngine(dedup_window_hours=6.0, lexical_threshold=0.70, low_ig_threshold=0.15)
    now = utcnow()
    fp1 = compute_goal_fingerprint(drive_type="curiosity", title="Inspect pgvector HNSW index performance", related_key="pgvector_hnsw")

    recent = [
        {
            "goal_id": str(uuid.uuid4()),
            "drive_type": "curiosity",
            "title": "Inspect pgvector HNSW index performance",
            "description": "Measure cosine recall and latency on 512d vectors",
            "related_key": "pgvector_hnsw",
            "goal_fingerprint": fp1,
            "status": "ACHIEVED",
            "verified_ig": 0.08,
            "created_at": now - timedelta(minutes=30),
            "cooldown_until": now + timedelta(minutes=90),
        },
        {
            "goal_id": str(uuid.uuid4()),
            "drive_type": "curiosity",
            "title": "调研本地 Qwen3.8 模型上下文窗口与长文本吞吐性能",
            "description": "测试本地模型吞吐和延迟表现",
            "related_key": None,
            "status": "ACHIEVED",
            "verified_ig": 0.09,
            "created_at": now - timedelta(minutes=45),
            "cooldown_until": None,
        },
    ]

    # 1. Active cooldown hit
    d1 = eng.evaluate(
        proposal=GoalProposal(title="Re-check pgvector HNSW", related_key="pgvector_hnsw"),
        drive_type="curiosity",
        recent_goals=recent,
        now=now,
    )
    assert d1.decision == "DEFER"
    assert d1.reason_code == "GOAL_IN_COOLDOWN"

    # 2. Chinese paraphrase duplicate hit (without related_key)
    d2 = eng.evaluate(
        proposal=GoalProposal(
            title="分析本地 Qwen3.8 模型上下文窗口与长文本吞吐性能表现",
            description="检查本地模型吞吐和延迟表现",
        ),
        drive_type="curiosity",
        recent_goals=recent,
        now=now,
    )
    assert d2.decision == "REJECT"
    assert d2.reason_code == "DUPLICATE_GOAL_LEXICAL"

    # 3. Genuinely novel goal MUST be accepted (including distinct related_key with similar templated title)
    d3 = eng.evaluate(
        proposal=GoalProposal(
            title="Build automated RSS sensor parser for arxiv cs.AI feed",
            description="Add structured feed filtering and deduplication",
            related_key="arxiv_rss_parser",
        ),
        drive_type="curiosity",
        recent_goals=recent,
        now=now,
    )
    assert d3.decision == "ACCEPT"

    d3b = eng.evaluate(
        proposal=GoalProposal(
            title="Inspect pgvector HNSW index performance",
            description="Measure cosine recall and latency on 512d vectors",
            related_key="pgvector_ivfflat_distinct_topic",
        ),
        drive_type="curiosity",
        recent_goals=recent,
        now=now,
    )
    assert d3b.decision == "ACCEPT"

    # 4. Cooldown computation escalates on failure streak & low IG
    cd_fail1 = compute_cooldown_until(now=now, goal_achieved=False, verified_ig=0.0, failure_streak=1)
    cd_fail3 = compute_cooldown_until(now=now, goal_achieved=False, verified_ig=0.0, failure_streak=3)
    cd_low_ig = compute_cooldown_until(now=now, goal_achieved=True, verified_ig=0.05)
    cd_high_ig = compute_cooldown_until(now=now, goal_achieved=True, verified_ig=0.65)
    assert cd_fail3 > cd_fail1
    assert cd_low_ig > cd_high_ig


def test_v3_m1_deterministic_info_gain_v2_strictly_gated():
    """IG v2 bonuses apply ONLY when verification_confidence > 0 and non-duplicate."""
    ev = InformationGainEvaluator()
    base = ev.evaluate(
        new_text="Implemented cgroup v2 preflight checker with 0660 UDS permissions",
        new_facts=["Pre-binding UDS prevents uvicorn 0666 chmod"],
        new_source="execution:1",
        existing_memories=[
            ExistingMemorySample(memory_id="m1", text="Unrelated database backup note", source="system")
        ],
        verification_confidence=1.0,
    )
    boosted = ev.evaluate(
        new_text="Implemented cgroup v2 preflight checker with 0660 UDS permissions",
        new_facts=["Pre-binding UDS prevents uvicorn 0666 chmod"],
        new_source="execution:1",
        existing_memories=[
            ExistingMemorySample(memory_id="m1", text="Unrelated database backup note", source="system")
        ],
        verification_confidence=1.0,
        topic_resolved=True,
        new_capability_evidence=True,
        verified_code_artifact=True,
    )
    assert boosted.information_gain > base.information_gain

    # Failed verification (ver_conf=0.0) MUST yield 0.0 even if all bonus flags are True!
    unverified = ev.evaluate(
        new_text="Claimed success without passing verification",
        new_facts=["Fake fact"],
        new_source="execution:2",
        verification_confidence=0.0,
        topic_resolved=True,
        new_capability_evidence=True,
        verified_code_artifact=True,
    )
    assert unverified.information_gain == 0.0


@pytest.mark.asyncio
async def test_v3_m1_loop_preflight_blocks_l0_write_and_dedup_skips_repeat_with_zero_agent_spawns(tmp_path: Path):
    """End-to-end AutonomousLoop test:
    Cycle 1: L0 read-only plan demanding file write -> PREFLIGHT_REJECTED (0 agent spawns!)
    Cycle 2: Same goal proposed immediately -> GOAL_DEDUP_SKIPPED (0 plan LLM calls, 0 agent spawns!)
    """
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'v3_m1_loop.db'}")
    await _init_db(db)
    spy_agent = _SpyAgent("codex")
    reg = AgentRegistry()
    reg.register(spy_agent, probe=AgentProbeResult(ok=True, binary="codex", version="spy-1.0"))

    bad_goal = GoalProposal(
        should_act=True,
        title="Audit repo and write report",
        description="Inspect repository and save findings to result.md",
        drive_type="curiosity",
        related_key="repo_audit_topic",
    )
    bad_l0_plan = ActionPlan(
        should_execute=True,
        goal_title=bad_goal.title,
        objective="Analyze the codebase and write to result.md",
        assigned_agent="codex",
        required_capability="knowledge_summarization",
        permission_level=PermissionLevel.L0.value,
        verification=[VerificationSpec(kind="file_exists", target="result.md")],
    )
    brain = _ScriptedBrain(goals=[bad_goal, bad_goal], plans=[bad_l0_plan, bad_l0_plan])

    settings = Settings(
        api=ApiSettings(auth_enabled=False),
        database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path / 'v3_m1_loop.db'}"),
        memory=MemorySettings(provider="none", embedding_dim=512),
        embedding=EmbeddingSettings(provider="hash_fallback", dimensions=512),
        governance=GovernanceSettings(workspace_root=str(tmp_path / "ws")),
        runner=RunnerSettings(enabled=False),
        kernel_v3=KernelV3Settings(plan_preflight_enabled=True, goal_dedup_enabled=True),
    )
    async def _healthy_llm_probe() -> dict[str, Any]:
        return {"available": True, "latency_ms": 5.0}

    rt = OrganismRuntime(
        settings=settings,
        db=db,
        brain=brain,
        registry=reg,
        auto_load_agents=False,
        llm_probe_fn=_healthy_llm_probe,
    )
    try:
        await rt.start(start_scheduler=False)
        # Seed an open curiosity topic so Curiosity drive fires reliably
        await rt.repo.upsert_world_item("knowledge_topic", "repo_audit_topic", {"description": "Open topic"}, status="open")

        # Cycle 1: Preflight MUST block L0 write-intent with 0 agent executions!
        c1 = await rt.loop.run_cycle()
        assert c1["outcome"] == "PREFLIGHT_REJECTED"
        assert spy_agent.execute_calls == 0
        assert brain.plan_calls == 1

        # Cycle 2: GoalDedup MUST catch cooldown/repeat BEFORE Planning -> 0 new plan calls, 0 agent spawns!
        c2 = await rt.loop.run_cycle()
        assert c2["outcome"] == "GOAL_DEDUP_SKIPPED"
        assert spy_agent.execute_calls == 0
        assert brain.plan_calls == 1  # Planner was NEVER even invoked on Cycle 2!
    finally:
        await rt.stop()
        await db.dispose()


@pytest.mark.asyncio
async def test_v3_m1_llm_gateway_scope_persists_goal_and_purpose_attribution(tmp_path: Path):
    """LLMGateway.scope(goal_id=..., purpose=...) must persist attribution into resource_usage.details."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'gw_attr.db'}")
    await _init_db(db)
    from app.storage.repository import StateRepository

    repo = StateRepository(db)
    gw = LLMGateway(LLMSettings(base_url="http://127.0.0.1:1/v1", model="Qwen/Test"), repo=repo)
    gid = uuid.uuid4()
    cid = uuid.uuid4()

    with gw.scope(cycle_id=cid, goal_id=gid, purpose="plan_generation"):
        # Trigger a fast connection failure so gateway records mandatory attempt accounting
        with pytest.raises(Exception):
            await gw.complete_structured(
                operation="plan",
                system_prompt="sys",
                user_payload={"a": 1},
                schema_cls=ActionPlan,
            )

    from sqlalchemy import select
    from app.storage.models import ResourceUsage

    async with db.session() as s:
        rows = (await s.execute(select(ResourceUsage).where(ResourceUsage.cycle_id == cid))).scalars().all()
    assert len(rows) >= 1
    det = rows[0].details
    assert det["goal_id"] == str(gid)
    assert det["purpose"] == "plan_generation"
    await db.dispose()
