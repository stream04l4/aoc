"""V4.0 M1 Tests: External SkillRuntime, Alembic 0010, MCP Skill Bridge & Skill-First Planning.

Acceptance gates verified:
  1. Alembic `0009 -> 0010 -> 0009 -> 0010` upgrade/downgrade roundtrip (extends skill_registry + adds skill_invocations).
  2. SkillRuntime full safety & fault matrix:
     - Standard manifest.json + skill.py + test_skill.py package layout
     - Input/Output JSON Schema validation
     - Governance L0/L1/L2/L3 checks (L2 requires approved_l2=True)
     - Isolated `python -I` sandbox timeout & exception containment (never crashes caller)
     - SHA-256 tamper detection auto-deprecates tampered skill
     - Strict cross-task reuse accounting (same-task calls != cross-task reuse; different task_id == cross-task reuse)
  3. MCP ToolProvider bridge: VALIDATED skills auto-expose as `skill:<canonical_name>` sharing SkillRuntime.
  4. End-to-end AutonomousLoop Skill-First Planning:
     - Matching validated skill executes directly via SkillRuntime (0 Codex calls, 0 Codex tokens, real reuse recorded)
     - Missing/broken skill cleanly logs `skill_first_fallback` and falls back to Codex without crashing cycle
"""

from __future__ import annotations

import uuid
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
    get_settings,
)
from app.core.enums import PermissionLevel
from app.core.runtime import OrganismRuntime
from app.governance.policy import GovernanceManager
from app.perception.interfaces import Observation, utcnow
from app.self_model.interfaces import SkillStatus
from app.self_model.skills import SkillRegistryManager
from app.skills.runtime import SkillRuntime
from app.storage.database import Database
from app.storage.models import Base
from app.storage.repository import StateRepository
from app.tools.interfaces import ToolCallRequest
from app.tools.mcp_manager import MCPClientManager

PROJECT_ROOT = Path(__file__).resolve().parents[2]


async def _init_db(db: Database) -> None:
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _llm_ok() -> dict[str, Any]:
    return {"available": True, "latency_ms": 4.0, "model": "stub-v4-m1"}


def test_v4_m1_alembic_0010_upgrade_and_downgrade_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify expand-only Alembic 0010 adds skill_registry V4 columns + skill_invocations and downgrades cleanly."""
    db_file = tmp_path / "alembic_0010.db"
    monkeypatch.setenv("AOC_DATABASE__URL", f"sqlite+aiosqlite:///{db_file}")
    get_settings.cache_clear()
    try:
        cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
        command.upgrade(cfg, "0010_v4_skill_rsi_evolution")

        eng = create_engine(f"sqlite:///{db_file}")
        insp = inspect(eng)
        tables = set(insp.get_table_names())
        assert "skill_invocations" in tables
        sr_cols = {c["name"] for c in insp.get_columns("skill_registry")}
        assert {
            "canonical_name",
            "parent_version",
            "superseded_by",
            "input_schema",
            "output_schema",
            "invocation_count",
            "reuse_count",
            "reuse_success_count",
            "last_invoked_at",
            "evolution_meta",
        } <= sr_cols

        command.downgrade(cfg, "0009_v3_kernel_enhancement")
        insp_down = inspect(eng)
        assert "skill_invocations" not in set(insp_down.get_table_names())
        assert "canonical_name" not in {c["name"] for c in insp_down.get_columns("skill_registry")}

        command.upgrade(cfg, "0010_v4_skill_rsi_evolution")
        assert "skill_invocations" in set(inspect(eng).get_table_names())
        eng.dispose()
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_v4_m1_skill_runtime_safety_schemas_timeout_tamper_and_cross_task_reuse(tmp_path: Path):
    """Verify SkillRuntime manifest, schemas, L2 gate, timeout isolation, tamper guard, and cross-task reuse."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'skill_rt.db'}")
    await _init_db(db)
    try:
        repo = StateRepository(db)
        gov = GovernanceManager(GovernanceSettings(workspace_root=str(tmp_path / "ws")), repo)
        sm_cfg = {"skill_storage_dir": str(tmp_path / "skills"), "skill_test_timeout_seconds": 5.0}
        skills = SkillRegistryManager(repo=repo, governance=gov, config=sm_cfg)
        rt = SkillRuntime(repo=repo, skill_registry=skills, governance=gov, default_timeout_seconds=5.0)

        origin_task_id = uuid.uuid4()
        # Register L1 skill with input/output JSON schemas + manifest.json
        sk = await skills.register_skill(
            skill_key="data_count_items",
            canonical_name="data.coding.count_items",
            version="1.0.0",
            capability_key="coding",
            description="Count normalized string frequencies deterministically",
            artifact_code=(
                "from collections import Counter\n\n"
                "def execute(data: dict) -> dict:\n"
                "    items = data.get('items') or []\n"
                "    counts = dict(Counter(str(x).strip().lower() for x in items if str(x).strip()))\n"
                "    return {'counts': counts, 'total': sum(counts.values())}\n"
            ),
            test_spec={
                "kind": "python_assertions",
                "code": (
                    "from skill import execute\n"
                    "out = execute({'items': ['A', 'b', 'a']})\n"
                    "assert out == {'counts': {'a': 2, 'b': 1}, 'total': 3}, out\n"
                ),
            },
            input_schema={
                "type": "object",
                "required": ["items"],
                "additionalProperties": False,
                "properties": {"items": {"type": "array"}},
            },
            output_schema={
                "type": "object",
                "required": ["counts", "total"],
                "properties": {"counts": {"type": "object"}, "total": {"type": "integer"}},
            },
            permission_level=PermissionLevel.L1.value,
            source_task_id=origin_task_id,
        )
        # Manifest file written alongside artifact
        manifest_path = Path(sk["artifact_path"]).parent / "manifest.json"
        assert manifest_path.is_file()

        # 1. DRAFT skill refused before validation
        draft_res = await rt.execute("data.coding.count_items", {"items": ["x"]})
        assert draft_res.ok is False and draft_res.denied is True

        # Validate -> auto-promotes L1 to VALIDATED
        val = await skills.validate_skill(sk["id"])
        assert val["status"] == SkillStatus.VALIDATED.value

        # 2. Invalid input schema (missing required 'items' / wrong type) -> denied
        bad_in = await rt.execute("data.coding.count_items", {"wrong": 1})
        assert bad_in.ok is False and bad_in.denied is True and "Input schema" in (bad_in.error or "")

        # 3. Same-task invocation (origin_task_id) succeeds, NOT counted as cross-task reuse
        r_same = await rt.execute(
            "data.coding.count_items",
            {"items": ["Alpha", "alpha", "Beta"]},
            task_id=origin_task_id,
            invocation_source="skill_first",
        )
        assert r_same.ok is True
        assert r_same.output == {"counts": {"alpha": 2, "beta": 1}, "total": 3}
        assert r_same.is_cross_task_reuse is False
        assert r_same.estimated_saved_tokens > 0

        # 4. Different task_id -> strictly counted as cross-task reuse!
        other_task_id = uuid.uuid4()
        r_reuse = await rt.execute(
            "data.coding.count_items",
            {"items": ["x", "y", "x"]},
            task_id=other_task_id,
            invocation_source="skill_first",
        )
        assert r_reuse.ok is True
        assert r_reuse.is_cross_task_reuse is True
        refreshed = await repo.get_skill(uuid.UUID(sk["id"]))
        assert refreshed is not None
        assert refreshed["invocation_count"] == 3  # 1 bad_in + 1 same + 1 reuse
        assert refreshed["reuse_count"] == 1
        assert refreshed["reuse_success_count"] == 1

        # 5. Hanging skill times out cleanly without crashing caller
        hang_sk = await skills.register_skill(
            skill_key="hang_exec",
            canonical_name="system.coding.hang_exec",
            version="1.0.0",
            capability_key="coding",
            description="Sleeps on execute",
            artifact_code="import time\ndef execute(data):\n    if data.get('hang'): time.sleep(30)\n    return {'ok': True}\n",
            test_spec={"kind": "python_assertions", "code": "from skill import execute\nassert execute({}) == {'ok': True}\n"},
        )
        await skills.validate_skill(hang_sk["id"])
        r_hang = await rt.execute("system.coding.hang_exec", {"hang": True}, timeout_seconds=0.35)
        assert r_hang.ok is False and "timed out" in (r_hang.error or "").lower()

        # 6. L2 skill requires explicit approved_l2=True
        l2_sk = await skills.register_skill(
            skill_key="l2_export",
            canonical_name="ops.coding.l2_export",
            version="1.0.0",
            capability_key="coding",
            description="L2 gated skill",
            artifact_code="def execute(data): return {'exported': True}\n",
            test_spec={"kind": "python_assertions", "code": "from skill import execute\nassert execute({})['exported']\n"},
            permission_level=PermissionLevel.L2.value,
        )
        await skills.validate_skill(l2_sk["id"], approved_l2_promotion=True)
        r_l2_block = await rt.execute("ops.coding.l2_export", {}, approved_l2=False)
        assert r_l2_block.ok is False and r_l2_block.requires_approval is True
        r_l2_ok = await rt.execute("ops.coding.l2_export", {}, approved_l2=True)
        assert r_l2_ok.ok is True and r_l2_ok.output == {"exported": True}

        # 7. MCP ToolProvider bridge shares unified SkillRuntime & audits both MCP + skill_invocations
        mcp = MCPClientManager(governance=gov, repo=repo, workspace_root=str(tmp_path / "ws"))
        mcp.attach_skill_runtime(rt)
        synced_count = await mcp.sync_validated_skills_as_tools()
        assert synced_count >= 2
        assert mcp.get_tool("skill:data.coding.count_items") is not None

        mcp_res = await mcp.invoke(
            ToolCallRequest(
                tool_name="skill:data.coding.count_items",
                arguments={"items": ["mcp", "mcp", "tool"]},
                task_id=str(uuid.uuid4()),
            )
        )
        assert mcp_res.ok is True
        assert mcp_res.output == {"counts": {"mcp": 2, "tool": 1}, "total": 3}
        invs = await repo.list_skill_invocations(skill_id=uuid.UUID(sk["id"]), limit=10)
        assert any(i["invocation_source"] == "mcp" and i["is_cross_task_reuse"] is True for i in invs)

        # 8. Silent tamper on disk is caught by SHA-256 and auto-deprecates skill
        Path(sk["artifact_path"]).write_text("def execute(d): return {'pwned': 1}\n", encoding="utf-8")
        r_tamper = await rt.execute("data.coding.count_items", {"items": ["a"]})
        assert r_tamper.ok is False and r_tamper.denied is True and "integrity" in (r_tamper.error or "").lower()
        assert (await repo.get_skill(uuid.UUID(sk["id"])))["status"] == SkillStatus.DEPRECATED.value
    finally:
        await db.dispose()


class _SpyCodexAgent(AgentInterface):
    def __init__(self) -> None:
        self._cap = AgentCapability(
            agent_id="codex",
            name="Spy Codex",
            agent_type="cli",
            capabilities=["coding", "research", "general_task"],
            supported_modes=["read-only", "workspace-write"],
            enabled=True,
        )
        self.execute_calls = 0

    @property
    def capability(self) -> AgentCapability:
        return self._cap

    async def probe(self) -> AgentProbeResult:
        return AgentProbeResult(ok=True, binary="codex", version="spy-v4-m1")

    async def execute(self, task: AgentTask) -> AgentExecution:
        self.execute_calls += 1
        return AgentExecution(
            execution_id=task.execution_id or str(uuid.uuid4()),
            task_id=task.task_id,
            agent_id="codex",
            status="SUCCEEDED",
            started_at=utcnow(),
            completed_at=utcnow(),
            result={"summary": f"Codex fallback handled: {task.objective}"},
            stdout="Codex fallback output",
        )

    async def get_status(self, execution_id: str) -> AgentStatus:
        return AgentStatus(execution_id=execution_id, status="SUCCEEDED")

    async def cancel(self, execution_id: str) -> bool:
        return True


class _SkillFirstBrain(BrainInterface):
    def __init__(self, target_skill_key: str, input_data: dict[str, Any]) -> None:
        self.target_skill_key = target_skill_key
        self.input_data = input_data

    async def assess(self, observation: Observation, memories: list[dict[str, Any]], drives: list[Any]) -> AssessmentResult:
        return AssessmentResult(summary="Ready to reuse validated skill", confidence=0.95)

    async def generate_goal(self, ctx: CognitiveContext) -> GoalProposal:
        return GoalProposal(
            should_act=True,
            title=f"Reuse skill {self.target_skill_key} for frequency analysis",
            description="Run validated counting skill directly without writing a new script.",
            related_key=f"reuse_{uuid.uuid4().hex[:6]}",
            predicted_ig=0.45,
        )

    async def plan(self, ctx: CognitiveContext) -> ActionPlan:
        return ActionPlan(
            should_execute=True,
            goal_title=ctx.goal.title if ctx.goal else "Reuse skill",
            objective="Run validated counting skill on input items",
            assigned_agent="codex",
            required_capability="coding",
            permission_level="L1",
            skill_invocation={
                "skill_key": self.target_skill_key,
                "input_data": self.input_data,
            },
            verification=[VerificationSpec(kind="non_empty_output")],
            expected_improvement=0.2,
        )

    async def evaluate(self, ctx: EvaluationContext) -> EvaluationResult:
        return EvaluationResult(
            goal_achieved=ctx.verification_passed,
            summary="Executed validated skill directly via SkillRuntime (0 Codex tokens).",
            learned_facts=["Direct skill reuse produced verified frequency counts."],
            information_gain=0.5,
            should_persist_memory=True,
        )


@pytest.mark.asyncio
async def test_v4_m1_end_to_end_skill_first_planning_bypasses_codex_and_falls_back_on_error(tmp_path: Path):
    """End-to-end loop: matching validated skill bypasses Codex (0 calls); missing skill seamlessly falls back."""
    db_path = tmp_path / "v4_m1_e2e.db"
    db = Database(f"sqlite+aiosqlite:///{db_path}")
    await _init_db(db)

    reg = AgentRegistry()
    spy_codex = _SpyCodexAgent()
    reg.register(spy_codex, probe=AgentProbeResult(ok=True, binary="codex", version="spy-v4-m1"))
    brain = _SkillFirstBrain("data.coding.count_items", {"items": ["rust", "python", "python"]})

    settings = Settings(
        database=DatabaseSettings(url=f"sqlite+aiosqlite:///{db_path}"),
        governance=GovernanceSettings(workspace_root=str(tmp_path / "ws")),
        memory=MemorySettings(provider="none"),
        embedding=EmbeddingSettings(provider="hash_fallback"),
        perception=PerceptionSettings(llm_probe=True),
        runner=RunnerSettings(enabled=False),
        kernel_v3=KernelV3Settings(
            skill_runtime_enabled=True,
            skill_first_planning_enabled=True,
            skill_mcp_bridge_enabled=True,
        ),
    )
    rt = OrganismRuntime(
        settings=settings,
        db=db,
        brain=brain,
        registry=reg,
        auto_load_agents=False,
        llm_probe_fn=_llm_ok,
    )
    await rt.start(start_scheduler=False)
    try:
        # Register and validate a canonical reusable skill from a prior origin task
        origin_tid = uuid.uuid4()
        sk = await rt.skill_registry.register_skill(
            skill_key="data_count_items",
            canonical_name="data.coding.count_items",
            version="1.0.0",
            capability_key="coding",
            description="Deterministic frequency counter",
            artifact_code=(
                "from collections import Counter\n"
                "def execute(data: dict) -> dict:\n"
                "    c = dict(Counter(data.get('items') or []))\n"
                "    return {'counts': c, 'total': sum(c.values())}\n"
            ),
            test_spec={"kind": "python_assertions", "code": "from skill import execute\nassert execute({'items': ['a']})['total'] == 1\n"},
            input_schema={"type": "object", "required": ["items"], "properties": {"items": {"type": "array"}}},
            permission_level=PermissionLevel.L1.value,
            source_task_id=origin_tid,
        )
        await rt.skill_registry.validate_skill(sk["id"])
        assert rt.mcp_manager.get_tool("skill:data.coding.count_items") is not None

        # Cycle 1: Skill-First executes directly -> ZERO Codex calls!
        c1 = await rt.loop.run_cycle()
        assert c1["outcome"] == "SUCCEEDED"
        assert spy_codex.execute_calls == 0

        sk_after = await rt.repo.get_skill(uuid.UUID(sk["id"]))
        assert sk_after is not None
        assert sk_after["invocation_count"] == 1
        assert sk_after["reuse_count"] == 1
        assert sk_after["reuse_success_count"] == 1

        invs = await rt.repo.list_skill_invocations(skill_id=uuid.UUID(sk["id"]), limit=5)
        assert len(invs) == 1
        assert invs[0]["invocation_source"] == "skill_first"
        assert invs[0]["is_cross_task_reuse"] is True
        assert invs[0]["estimated_saved_tokens"] > 0

        # Cycle 2: Target a non-existent skill -> cleanly logs skill_first_fallback & falls back to Codex!
        brain.target_skill_key = "nonexistent.coding.missing_skill"
        rt.loop._last_learning_at = None
        rt.loop._last_info_gain = None
        for k in ("gap_a", "gap_b", "gap_c", "gap_d"):
            await rt.repo.upsert_world_item(kind="knowledge_topic", key=k, value={"importance": 0.9}, status="open", source="t")
        ds_rows = await rt.repo.load_drive_states(rt.organism_id)
        from app.drives.interfaces import DriveState
        for r in ds_rows:
            r["last_satisfied_at"] = None
        await rt.repo.save_drive_states(rt.organism_id, [DriveState(**r) for r in ds_rows], cycle_id=None, phase="pre")

        c2 = await rt.loop.run_cycle()
        assert c2["outcome"] == "SUCCEEDED"
        assert spy_codex.execute_calls == 1
        decs = await rt.repo.list_decisions(limit=25)
        assert any(d["decision_type"] == "skill_first_fallback" for d in decs)
    finally:
        await rt.stop()
        await db.dispose()
