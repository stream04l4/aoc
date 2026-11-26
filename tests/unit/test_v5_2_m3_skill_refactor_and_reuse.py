"""V5.2 M3 tests: Explicit Directed Skill Refactoring, Stronger `skill_first` Routing & Stale `DRAFT` Cleanup."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import update

from app.agents.base import AgentCapability, AgentExecution, AgentInterface, AgentProbeResult, AgentStatus, AgentTask
from app.agents.registry import AgentRegistry
from app.api.app import create_app
from app.cognition.context_compactor import rank_and_compact_skills
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
from app.cognition.preflight import ActionPreflight
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
)
from app.core.enums import ExecutionStatus, MemoryType, PermissionLevel
from app.core.runtime import OrganismRuntime
from app.drives.interfaces import DriveState
from app.governance.interfaces import BudgetStatus, PolicyVerdict
from app.governance.policy import GovernanceManager
from app.perception.interfaces import Observation, utcnow
from app.self_model.interfaces import SkillStatus
from app.self_model.skills import SkillRegistryManager
from app.skills.evolution import SkillEvolutionEngine
from app.storage.database import Database
from app.storage.models import Base, SkillRegistryRow
from app.storage.repository import StateRepository


async def _init_db(db: Database) -> None:
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _healthy_llm_probe() -> dict[str, Any]:
    return {"available": True, "latency_ms": 2.0, "model": "stub-v52-m3"}


@pytest.mark.asyncio
async def test_blind_duplicate_collision_skipped_without_deprecated_version(tmp_path: Path):
    """When explicit_skill_refactor_enabled=True, normal task collisions MUST NOT trigger blind evolve_skill()."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'm3_blind_skip.db'}")
    await _init_db(db)
    try:
        repo = StateRepository(db)
        gov = GovernanceManager(GovernanceSettings(workspace_root=str(tmp_path / "ws")), repo)
        sm_cfg = {"skill_storage_dir": str(tmp_path / "skills"), "skill_test_timeout_seconds": 5.0}
        skills = SkillRegistryManager(
            repo=repo,
            governance=gov,
            config=sm_cfg,
            explicit_skill_refactor_enabled=True,
        )
        evo = SkillEvolutionEngine(repo=repo, skill_registry=skills, dedup_threshold=0.50)
        skills.evolution_engine = evo

        v1 = await skills.register_skill(
            skill_key="data_coding_count_items",
            canonical_name="data.coding.count_items",
            version="1.0.0",
            capability_key="coding",
            description="Count item frequencies case-insensitively",
            artifact_code=(
                "from collections import Counter\n"
                "def execute(data: dict) -> dict:\n"
                "    c = dict(Counter(str(x).lower() for x in (data.get('items') or [])))\n"
                "    return {'counts': c, 'total': sum(c.values())}\n"
            ),
            test_spec={
                "kind": "python_assertions",
                "code": "from skill import execute\nassert execute({'items': ['A', 'a']})['total'] == 2\n",
            },
            permission_level=PermissionLevel.L1.value,
        )
        await skills.validate_skill(v1["id"])

        # Colliding one-off workspace WITHOUT explicit skill_refactor_target
        ws = tmp_path / "colliding_ws"
        ws.mkdir()
        (ws / "skill.py").write_text("def execute(data): return {'unrelated': 1}\n", encoding="utf-8")
        (ws / "test_skill.py").write_text(
            "from skill import execute\nassert execute({})['unrelated'] == 1\n",
            encoding="utf-8",
        )

        cryst = await skills.crystallize_from_workspace(
            workspace=str(ws),
            skill_key="data_coding_count_items_oneoff",
            capability_key="coding",
            description="Count item frequencies case-insensitively",
        )
        assert cryst is not None
        assert cryst["skipped_existing_validated"] is True
        assert cryst["crystallized"] is False
        assert cryst["evolved"] is False

        all_rows = await repo.list_skills(limit=20)
        assert len(all_rows) == 1
        assert all_rows[0]["status"] == SkillStatus.VALIDATED.value
        assert len(await repo.list_skills(status=SkillStatus.DEPRECATED.value, limit=20)) == 0

        events = await repo.list_events(limit=20, event_type="skill.crystallize_skipped_existing_validated")
        assert len(events) == 1
    finally:
        await db.dispose()


class _StubRefactorAgent(AgentInterface):
    def __init__(self) -> None:
        self._cap = AgentCapability(
            agent_id="codex",
            name="Stub Refactor Agent",
            agent_type="cli",
            capabilities=["coding", "research", "general_task"],
            supported_modes=["read-only", "workspace-write"],
            enabled=True,
        )
        self.seen_tasks: list[AgentTask] = []

    @property
    def capability(self) -> AgentCapability:
        return self._cap

    async def probe(self) -> AgentProbeResult:
        return AgentProbeResult(ok=True, binary="codex", version="stub-v52-m3")

    async def execute(self, task: AgentTask) -> AgentExecution:
        self.seen_tasks.append(task)
        ws = Path(task.workspace)
        base_dir = ws / ".aoc_base_skill"
        assert (base_dir / "skill.py").is_file()
        assert (base_dir / "test_skill.py").is_file()
        assert (base_dir / "metadata.json").is_file()
        assert "[Explicit Skill Refactoring Instructions]" in task.description

        meta = json.loads((base_dir / "metadata.json").read_text(encoding="utf-8"))
        assert meta["canonical_name"] == "data.coding.count_items"
        assert meta["version"] == "1.0.0"

        # Generalized backward-compatible skill + expanded tests + schemas
        (ws / "skill.py").write_text(
            "from collections import Counter\n"
            "def execute(data: dict) -> dict:\n"
            "    items = data.get('items') or []\n"
            "    cleaned = [str(x).strip().lower() for x in items if str(x).strip()]\n"
            "    c = dict(Counter(cleaned))\n"
            "    return {'counts': c, 'total': sum(c.values())}\n",
            encoding="utf-8",
        )
        (ws / "test_skill.py").write_text(
            "from skill import execute\n"
            "assert execute({'items': ['A', 'a', 'b']}) == {'counts': {'a': 2, 'b': 1}, 'total': 3}\n"
            "assert execute({'items': [' A ', '', '   ', 'a']}) == {'counts': {'a': 2}, 'total': 2}\n"
            "assert execute({'items': []}) == {'counts': {}, 'total': 0}\n",
            encoding="utf-8",
        )
        (ws / "skill_meta.json").write_text(
            json.dumps(
                {
                    "input_schema": {
                        "type": "object",
                        "required": ["items"],
                        "properties": {"items": {"type": "array"}},
                    },
                    "output_schema": {
                        "type": "object",
                        "properties": {"counts": {"type": "object"}, "total": {"type": "integer"}},
                    },
                }
            ),
            encoding="utf-8",
        )
        return AgentExecution(
            execution_id=task.execution_id or str(uuid.uuid4()),
            task_id=task.task_id,
            agent_id="codex",
            status=ExecutionStatus.SUCCEEDED.value,
            completed_at=utcnow(),
            result={"summary": "Refactored data.coding.count_items and verified Gate A + Gate B"},
            stdout="All baseline and boundary tests passed cleanly.",
        )

    async def get_status(self, execution_id: str) -> AgentStatus:
        return AgentStatus(execution_id=execution_id, agent_id="codex", status=ExecutionStatus.SUCCEEDED.value)

    async def cancel(self, execution_id: str) -> bool:
        return True


class _ExplicitRefactorBrain(BrainInterface):
    def __init__(self) -> None:
        self.plan_contexts: list[CognitiveContext] = []

    async def assess(self, observation: Observation, memories: list[dict[str, Any]], drives: list[Any]) -> AssessmentResult:
        return AssessmentResult(summary="Ready to upgrade count_items skill", recommended_focus="skill_refactor", confidence=0.88)

    async def generate_goal(self, context: CognitiveContext) -> GoalProposal:
        return GoalProposal(
            should_act=True,
            title="定向升级 data.coding.count_items：支持空白裁剪与空项过滤",
            description="重构已有技能并补充 Schema 和边界测试",
            drive_type=context.decision.selected_drive,
            related_key="refactor:data.coding.count_items",
            success_criteria=["兼容旧断言并支持空白/空值过滤"],
            predicted_ig=0.70,
        )

    async def plan(self, context: CognitiveContext) -> ActionPlan:
        self.plan_contexts.append(context)
        return ActionPlan(
            should_execute=True,
            goal_title=context.goal.title if context.goal else "",
            objective="Read .aoc_base_skill and refactor data.coding.count_items cleanly",
            description="Write updated skill.py, test_skill.py, and skill_meta.json.",
            execution_mode="codex",
            assigned_agent="codex",
            required_capability="coding",
            expected_output="skill.py + test_skill.py + skill_meta.json",
            timeout_seconds=300,
            permission_level=PermissionLevel.L1.value,
            success_criteria=["Gate A & B assertions pass"],
            verification=[VerificationSpec(kind="file_exists", target="skill.py")],
            skill_refactor_target={
                "skill_key": "data_coding_count_items",
                "canonical_name": "data.coding.count_items",
                "reason": "Strip whitespace and filter empty entries",
                "target_generalization": "Strip whitespace, skip empty items, and publish schemas",
            },
            rationale="Explicit directed refactor requested",
        )

    async def evaluate(self, context: EvaluationContext) -> EvaluationResult:
        return EvaluationResult(
            goal_achieved=bool(context.verification_passed),
            summary="Successfully refactored data.coding.count_items to v1.1.0.",
            information_gain=0.65,
            should_persist_memory=True,
            memory_type=MemoryType.SEMANTIC.value,
            memory_importance=0.75,
        )


@pytest.mark.asyncio
async def test_explicit_skill_refactor_stages_base_and_promotes_v1_1_0(tmp_path: Path):
    db_path = tmp_path / "m3_refactor_e2e.db"
    db = Database(f"sqlite+aiosqlite:///{db_path}")
    await _init_db(db)
    reg = AgentRegistry()
    stub_agent = _StubRefactorAgent()
    reg.register(stub_agent, probe=AgentProbeResult(ok=True, binary="codex", version="stub-v52-m3"))
    brain = _ExplicitRefactorBrain()
    settings = Settings(
        api=ApiSettings(host="127.0.0.1", auth_enabled=False),
        database=DatabaseSettings(url=f"sqlite+aiosqlite:///{db_path}"),
        memory=MemorySettings(provider="none", embedding_dim=128),
        embedding=EmbeddingSettings(provider="hash_fallback", dimensions=128),
        governance=GovernanceSettings(workspace_root=str(tmp_path / "ws")),
        perception=PerceptionSettings(llm_probe=True),
        runner=RunnerSettings(enabled=False),
        kernel_v3=KernelV3Settings(
            skill_auto_crystallize_enabled=True,
            skill_runtime_enabled=True,
            skill_evolution_enabled=True,
            explicit_skill_refactor_enabled=True,
        ),
        social=SocialSettings(enabled=False),
    )
    rt = OrganismRuntime(settings=settings, db=db, brain=brain, registry=reg, auto_load_agents=False, llm_probe_fn=_healthy_llm_probe)
    await rt.start(start_scheduler=False)
    try:
        # Seed baseline v1.0.0 VALIDATED skill
        v1 = await rt.skill_registry.register_skill(
            skill_key="data_coding_count_items",
            canonical_name="data.coding.count_items",
            version="1.0.0",
            capability_key="coding",
            description="Case-insensitive item counter",
            artifact_code=(
                "from collections import Counter\n"
                "def execute(data: dict) -> dict:\n"
                "    c = dict(Counter(str(x).lower() for x in (data.get('items') or [])))\n"
                "    return {'counts': c, 'total': sum(c.values())}\n"
            ),
            test_spec={
                "kind": "python_assertions",
                "code": "from skill import execute\nassert execute({'items': ['A', 'a', 'b']}) == {'counts': {'a': 2, 'b': 1}, 'total': 3}\n",
            },
            permission_level=PermissionLevel.L1.value,
        )
        await rt.skill_registry.validate_skill(v1["id"])

        cycle = await rt.loop.run_cycle()
        assert cycle["outcome"] == "SUCCEEDED"
        assert len(stub_agent.seen_tasks) == 1
        assert brain.plan_contexts[0].explicit_skill_refactor_enabled is True

        active_skills = await rt.repo.list_skills(status=SkillStatus.VALIDATED.value, limit=20)
        assert len(active_skills) == 1
        v1_1 = active_skills[0]
        assert v1_1["canonical_name"] == "data.coding.count_items"
        assert v1_1["version"] == "1.1.0"
        assert v1_1["parent_version"] == "1.0.0"
        assert "items" in ((v1_1.get("input_schema") or {}).get("properties") or {})
        assert "counts" in ((v1_1.get("output_schema") or {}).get("properties") or {})

        v1_after = await rt.repo.get_skill(uuid.UUID(v1["id"]))
        assert v1_after is not None
        assert v1_after["status"] == SkillStatus.DEPRECATED.value
        assert v1_after["superseded_by"] == v1_1["id"]

        exec_res = await rt.skill_runtime.execute("data.coding.count_items", {"items": [" A ", "", "   ", "a"]})
        assert exec_res.ok is True
        assert exec_res.output == {"counts": {"a": 2}, "total": 2}
    finally:
        await rt.stop()
        await db.dispose()


@pytest.mark.asyncio
async def test_explicit_refactor_kill_switch_disables_staging(tmp_path: Path):
    db_path = tmp_path / "m3_refactor_off.db"
    db = Database(f"sqlite+aiosqlite:///{db_path}")
    await _init_db(db)
    reg = AgentRegistry()

    class _NoStageSpyAgent(_StubRefactorAgent):
        async def execute(self, task: AgentTask) -> AgentExecution:
            self.seen_tasks.append(task)
            ws = Path(task.workspace)
            assert not (ws / ".aoc_base_skill").exists()
            assert "[Explicit Skill Refactoring Instructions]" not in task.description
            return AgentExecution(
                execution_id=task.execution_id or str(uuid.uuid4()),
                task_id=task.task_id,
                agent_id="codex",
                status=ExecutionStatus.SUCCEEDED.value,
                completed_at=utcnow(),
                stdout="ok without staging",
            )

    spy = _NoStageSpyAgent()
    reg.register(spy, probe=AgentProbeResult(ok=True, binary="codex", version="stub"))
    settings = Settings(
        api=ApiSettings(host="127.0.0.1", auth_enabled=False),
        database=DatabaseSettings(url=f"sqlite+aiosqlite:///{db_path}"),
        memory=MemorySettings(provider="none"),
        embedding=EmbeddingSettings(provider="hash_fallback"),
        governance=GovernanceSettings(workspace_root=str(tmp_path / "ws")),
        perception=PerceptionSettings(llm_probe=False),
        runner=RunnerSettings(enabled=False),
        kernel_v3=KernelV3Settings(explicit_skill_refactor_enabled=False),
        social=SocialSettings(enabled=False),
    )
    rt = OrganismRuntime(settings=settings, db=db, registry=reg, auto_load_agents=False, llm_probe_fn=_healthy_llm_probe)
    await rt.start(start_scheduler=False)
    try:
        assert rt.skill_registry.explicit_skill_refactor_enabled is False
        assert rt.orchestrator.explicit_skill_refactor_enabled is False
        v1 = await rt.skill_registry.register_skill(
            skill_key="base_demo",
            canonical_name="demo.coding.base",
            version="1.0.0",
            capability_key="coding",
            description="demo",
            artifact_code="def execute(d): return {'ok': True}\n",
            test_spec={"kind": "python_assertions", "code": "from skill import execute\nassert execute({})['ok']\n"},
        )
        await rt.skill_registry.validate_skill(v1["id"])

        plan = ActionPlan(
            should_execute=True,
            goal_title="No staging when off",
            objective="Run simple task cleanly",
            description="Base description only.",
            assigned_agent="codex",
            permission_level="L1",
            skill_refactor_target={"skill_key": "base_demo", "canonical_name": "demo.coding.base"},
        )
        verdict = PolicyVerdict(allowed=True, denied=False, requested_level="L1", effective_level="L1", sandbox_mode="workspace-write")
        res = await rt.orchestrator.submit_plan(plan=plan, verdict=verdict)
        done = await rt.orchestrator.wait_for_task(task_id=res["task_id"], max_wait_seconds=10.0, poll_interval=0.1)
        assert done["status"] == "SUCCEEDED"
        assert len(spy.seen_tasks) == 1
    finally:
        await rt.stop()
        await db.dispose()


@pytest.mark.asyncio
async def test_expire_stale_draft_skills_preserves_fresh_and_l2_passed(tmp_path: Path):
    db_path = tmp_path / "m3_draft_gc.db"
    db = Database(f"sqlite+aiosqlite:///{db_path}")
    await _init_db(db)
    try:
        repo = StateRepository(db)
        gov = GovernanceManager(GovernanceSettings(workspace_root=str(tmp_path / "ws")), repo)
        skills = SkillRegistryManager(
            repo=repo,
            governance=gov,
            config={"skill_storage_dir": str(tmp_path / "skills"), "skill_test_timeout_seconds": 5.0},
        )
        now = utcnow()
        old_dt = now - timedelta(hours=60)
        fresh_dt = now - timedelta(hours=10)

        stale_broken = await skills.register_skill(
            skill_key="stale_broken_draft",
            version="1.0.0",
            capability_key="coding",
            description="Broken draft left behind",
            artifact_code="def execute(d): return {}\n",
            test_spec={"kind": "python_assertions", "code": "assert False\n"},
            permission_level=PermissionLevel.L1.value,
        )
        fresh_draft = await skills.register_skill(
            skill_key="fresh_draft",
            version="1.0.0",
            capability_key="coding",
            description="Recent unvalidated draft",
            artifact_code="def execute(d): return {}\n",
            test_spec={"kind": "python_assertions", "code": "assert True\n"},
            permission_level=PermissionLevel.L1.value,
        )
        l2_awaiting = await skills.register_skill(
            skill_key="l2_passed_draft",
            version="1.0.0",
            capability_key="coding",
            description="L2 skill passing assertions awaiting human approval",
            artifact_code="def execute(d): return {'ok': True}\n",
            test_spec={"kind": "python_assertions", "code": "from skill import execute\nassert execute({})['ok']\n"},
            permission_level=PermissionLevel.L2.value,
        )
        l2_val = await skills.validate_skill(l2_awaiting["id"])
        assert l2_val["passed"] is True and l2_val["awaiting_human_approval"] is True

        async with db.session() as s:
            await s.execute(
                update(SkillRegistryRow)
                .where(SkillRegistryRow.id.in_([uuid.UUID(stale_broken["id"]), uuid.UUID(l2_awaiting["id"])]))
                .values(created_at=old_dt, updated_at=old_dt)
            )
            await s.execute(
                update(SkillRegistryRow)
                .where(SkillRegistryRow.id == uuid.UUID(fresh_draft["id"]))
                .values(created_at=fresh_dt, updated_at=fresh_dt)
            )

        res = await skills.expire_stale_draft_skills(expiry_hours=48, now=now)
        assert res["expired_count"] == 1
        assert res["expired_skill_ids"] == [stale_broken["id"]]

        stale_row = await repo.get_skill(uuid.UUID(stale_broken["id"]))
        assert stale_row is not None
        assert stale_row["status"] == SkillStatus.DEPRECATED.value
        assert (stale_row.get("evolution_meta") or {}).get("cleanup_reason") == "stale_draft_expired"

        assert (await repo.get_skill(uuid.UUID(fresh_draft["id"])))["status"] == SkillStatus.DRAFT.value
        assert (await repo.get_skill(uuid.UUID(l2_awaiting["id"])))["status"] == SkillStatus.DRAFT.value

        ev = await repo.list_events(limit=10, event_type="skill.stale_draft_expired")
        assert len(ev) == 1
    finally:
        await db.dispose()


class _SkillFirstM3Brain(BrainInterface):
    def __init__(self, target_skill_key: str) -> None:
        self.target_skill_key = target_skill_key

    async def assess(self, observation: Observation, memories: list[dict[str, Any]], drives: list[Any]) -> AssessmentResult:
        return AssessmentResult(summary="Ready to reuse skills", recommended_focus="skill_first", confidence=0.85)

    async def generate_goal(self, context: CognitiveContext) -> GoalProposal:
        return GoalProposal(
            should_act=True,
            title=f"Run validated skill {self.target_skill_key}",
            description="Reuse existing validated skill when available",
            drive_type=context.decision.selected_drive,
            related_key=f"reuse:{self.target_skill_key}",
            success_criteria=["Return computed frequency dict"],
            predicted_ig=0.50,
        )

    async def plan(self, context: CognitiveContext) -> ActionPlan:
        return ActionPlan(
            should_execute=True,
            goal_title=context.goal.title if context.goal else "",
            objective="Execute matching validated skill directly via SkillRuntime",
            description="Direct Skill-First reuse.",
            execution_mode="skill_first",
            assigned_agent="skill_runtime",
            required_capability="coding",
            expected_output="JSON frequency dict",
            permission_level=PermissionLevel.L0.value,
            success_criteria=["Skill returns counts"],
            verification=[VerificationSpec(kind="non_empty_output")],
            skill_invocation={"skill_key": self.target_skill_key, "input_data": {"items": ["A", "a"]}},
        )

    async def evaluate(self, context: EvaluationContext) -> EvaluationResult:
        return EvaluationResult(
            goal_achieved=bool(context.verification_passed),
            summary="Skill executed or fell back cleanly.",
            information_gain=0.45 if context.verification_passed else 0.0,
        )


@pytest.mark.asyncio
async def test_skill_first_enriched_context_preflight_execution_and_fallback(tmp_path: Path):
    # 1. Context compactor enrichment
    compacted = rank_and_compact_skills(
        [
            {
                "skill_key": "data_count_items",
                "canonical_name": "data.coding.count_items",
                "version": "1.1.0",
                "capability_key": "coding",
                "description": "Count items cleanly",
                "input_schema": {"type": "object", "properties": {"items": {"type": "array"}}},
                "output_schema": {"type": "object", "properties": {"total": {"type": "integer"}}},
                "reuse_count": 2,
                "reuse_success_count": 2,
            }
        ],
        query_text="count items",
        top_k=6,
    )
    assert len(compacted) == 1
    assert compacted[0]["reuse_count"] == 2
    assert compacted[0]["output_schema"]["properties"]["total"]["type"] == "integer"
    assert "execution_mode='skill_first'" in compacted[0]["invocation_hint"]
    assert "'items'" in compacted[0]["invocation_hint"]

    # 2. ActionPreflight allows skill_first with empty AgentRegistry, still enforces traversal & policy blocks
    pf = ActionPreflight(max_task_timeout_seconds=600)
    empty_reg = AgentRegistry()
    budget = BudgetStatus(allowed=True, remaining_tasks=5, remaining_tokens=50000)
    sf_plan = ActionPlan(
        should_execute=True,
        goal_title="Count items",
        objective="Execute skill_first and save summary into result.md",
        execution_mode="skill_first",
        assigned_agent="skill_runtime",
        permission_level="L0",
        skill_invocation={"skill_key": "data.coding.count_items", "input_data": {"items": ["a"]}},
        verification=[VerificationSpec(kind="non_empty_output")],
    )
    assert pf.validate(plan=sf_plan, registry=empty_reg, budget=budget).decision == "ACCEPT"

    bad_path_sf = sf_plan.model_copy(update={"verification": [VerificationSpec(kind="file_exists", target="../etc/passwd")]})
    assert "UNSAFE_VERIFICATION_TARGET" in pf.validate(plan=bad_path_sf, registry=empty_reg, budget=budget).reason_codes

    denied_verdict = PolicyVerdict(allowed=False, denied=True, requested_level="L0", effective_level="L0", reason="blocked")
    assert "POLICY_DENIED" in pf.validate(plan=sf_plan, verdict=denied_verdict, registry=empty_reg, budget=budget).reason_codes

    # 3. End-to-end Skill-First execution + missing-skill fallback + Control Center 12 domains
    db_path = tmp_path / "m3_sf_e2e.db"
    db = Database(f"sqlite+aiosqlite:///{db_path}")
    await _init_db(db)

    class _FallbackSpyCodex(_StubRefactorAgent):
        async def execute(self, task: AgentTask) -> AgentExecution:
            self.seen_tasks.append(task)
            assert task.assigned_agent == "codex"
            return AgentExecution(
                execution_id=task.execution_id or str(uuid.uuid4()),
                task_id=task.task_id,
                agent_id="codex",
                status=ExecutionStatus.SUCCEEDED.value,
                completed_at=utcnow(),
                stdout="Fallback Codex execution succeeded",
            )

    reg = AgentRegistry()
    spy_codex = _FallbackSpyCodex()
    reg.register(spy_codex, probe=AgentProbeResult(ok=True, binary="codex", version="stub"))
    brain = _SkillFirstM3Brain("data.coding.count_items")
    settings = Settings(
        api=ApiSettings(host="127.0.0.1", auth_enabled=False),
        database=DatabaseSettings(url=f"sqlite+aiosqlite:///{db_path}"),
        memory=MemorySettings(provider="none"),
        embedding=EmbeddingSettings(provider="hash_fallback"),
        governance=GovernanceSettings(workspace_root=str(tmp_path / "ws")),
        perception=PerceptionSettings(llm_probe=True),
        runner=RunnerSettings(enabled=False),
        kernel_v3=KernelV3Settings(
            skill_runtime_enabled=True,
            skill_first_planning_enabled=True,
            explicit_skill_refactor_enabled=True,
        ),
        social=SocialSettings(enabled=False),
    )
    rt = OrganismRuntime(settings=settings, db=db, brain=brain, registry=reg, auto_load_agents=False, llm_probe_fn=_healthy_llm_probe)
    await rt.start(start_scheduler=False)
    try:
        sk = await rt.skill_registry.register_skill(
            skill_key="data_count_items",
            canonical_name="data.coding.count_items",
            version="1.0.0",
            capability_key="coding",
            description="Deterministic counter",
            artifact_code=(
                "from collections import Counter\n"
                "def execute(data: dict) -> dict:\n"
                "    c = dict(Counter(str(x).lower() for x in (data.get('items') or [])))\n"
                "    return {'counts': c, 'total': sum(c.values())}\n"
            ),
            test_spec={"kind": "python_assertions", "code": "from skill import execute\nassert execute({'items': ['A']})['total'] == 1\n"},
            input_schema={"type": "object", "required": ["items"], "properties": {"items": {"type": "array"}}},
            permission_level=PermissionLevel.L1.value,
            source_task_id=uuid.uuid4(),
        )
        await rt.skill_registry.validate_skill(sk["id"])

        # Cycle 1: Direct SkillRuntime execution (0 Codex calls)
        c1 = await rt.loop.run_cycle()
        assert c1["outcome"] == "SUCCEEDED"
        assert len(spy_codex.seen_tasks) == 0
        sk_after = await rt.repo.get_skill(uuid.UUID(sk["id"]))
        assert sk_after is not None and sk_after["reuse_count"] == 1

        # Cycle 2: Missing skill falls back cleanly from skill_runtime -> codex
        brain.target_skill_key = "nonexistent.coding.missing_skill"
        ds_rows = await rt.repo.load_drive_states(rt.organism_id)
        for r in ds_rows:
            r["last_satisfied_at"] = None
        await rt.repo.save_drive_states(rt.organism_id, [DriveState(**r) for r in ds_rows], cycle_id=None, phase="pre")
        c2 = await rt.loop.run_cycle()
        assert c2["outcome"] == "SUCCEEDED"
        assert len(spy_codex.seen_tasks) == 1

        # Control Center exposes kernel_v3.explicit_skill_refactor_enabled inside 12 domains
        app = create_app(settings=settings, db=db, runtime=rt, start_scheduler=False)
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
            brain_dom = next(d for d in cfg_json["domains"] if d["id"] == "brain_llm")
            assert "kernel_v3.explicit_skill_refactor_enabled" in {it["key"] for it in brain_dom["items"]}
    finally:
        await rt.stop()
        await db.dispose()
