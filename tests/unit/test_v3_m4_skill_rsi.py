"""V3.0 M4 Tests: Guarded Skill Auto-Crystallization, L0/L1 Auto-Promotion, L2 Approval Gate,
Static Safety Scanner, SHA-256 Tamper/Revoke Guards & End-to-End Skill Reuse in AutonomousLoop.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

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
)
from app.core.enums import PermissionLevel
from app.core.runtime import OrganismRuntime
from app.governance.policy import GovernanceManager
from app.perception.interfaces import Observation, utcnow
from app.self_model.interfaces import CapabilityMaturity, SkillStatus
from app.self_model.manager import SelfModelManager
from app.self_model.skills import SkillGovernanceError, SkillRegistryManager, scan_skill_code_safety
from app.storage.database import Database
from app.storage.models import Base
from app.storage.repository import StateRepository


async def _init_db(db: Database) -> None:
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _llm_ok() -> dict[str, Any]:
    return {"available": True, "latency_ms": 5.0, "model": "stub-v3-m4"}


def test_v3_m4_static_safety_scanner():
    """Verify deterministic AST + regex scanner blocks unsafe code and permits clean utilities."""
    unsafe_cases = [
        ("core_edit", "p = '/home/ubuntu/aoc/app/core/loop.py'\n"),
        ("gov_path", "target = 'app/governance/policy.py'\n"),
        ("sudo_cmd", "cmd = 'sudo systemctl restart aoc'\n"),
        ("rm_rf", "cmd = 'rm -rf /tmp/foo'\n"),
        ("drop_sql", "q = 'DROP TABLE organism_identity;'\n"),
        ("os_system", "import os\nos.system('echo hi')\n"),
        ("eval_call", "x = eval('1+1')\n"),
        ("exec_call", "exec('a = 1')\n"),
        ("shell_true", "import subprocess\nsubprocess.run('ls', shell=True)\n"),
        ("import_sqlalchemy", "import sqlalchemy\n"),
        ("import_socket", "from socket import socket\n"),
        ("import_docker", "import docker\n"),
        ("syntax_error", "def broken(:\n    pass\n"),
    ]
    for label, code in unsafe_cases:
        violations = scan_skill_code_safety(code)
        assert violations, f"Expected static safety violations for case '{label}'"

    clean_code = (
        "import json\n"
        "from collections import Counter\n\n"
        "def count_tokens(lines: list[str]) -> dict[str, int]:\n"
        "    return dict(Counter(line.strip().lower() for line in lines if line.strip()))\n"
    )
    assert scan_skill_code_safety(clean_code) == []


@pytest.mark.asyncio
async def test_v3_m4_l0_l1_auto_promotion_l2_approval_and_tamper_revoke(tmp_path: Path):
    """Verify Joel Decision #1: L0/L1 auto-promote on passing isolated tests; L2 stays DRAFT until approved."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'skills_policy.db'}")
    await _init_db(db)
    try:
        repo = StateRepository(db)
        ident = await repo.get_or_create_identity("AOC-V3-M4-POLICY", "3.0.0")
        gov = GovernanceManager(GovernanceSettings(workspace_root=str(tmp_path / "ws")), repo)
        sm_cfg = {"skill_storage_dir": str(tmp_path / "skills"), "skill_test_timeout_seconds": 5.0}
        skills = SkillRegistryManager(repo=repo, governance=gov, config=sm_cfg)
        sm = SelfModelManager(organism_id=ident["id"], repo=repo, config=sm_cfg, skill_registry=skills)
        skills.self_model = sm

        # 1. L1 skill using natural `from skill import ...` under `python -I` auto-promotes to VALIDATED
        l1_row = await skills.register_skill(
            skill_key="metrics_parser",
            version="1.0.0",
            capability_key="coding",
            description="Parse key=value metrics lines into floats",
            artifact_code=(
                "def parse_metrics(raw: str) -> dict[str, float]:\n"
                "    out = {}\n"
                "    for line in raw.splitlines():\n"
                "        if '=' in line:\n"
                "            k, v = line.split('=', 1)\n"
                "            out[k.strip()] = float(v.strip())\n"
                "    return out\n"
            ),
            test_spec={
                "kind": "python_assertions",
                "code": (
                    "from skill import parse_metrics\n"
                    "assert parse_metrics('cpu=0.4\\nmem=0.8') == {'cpu': 0.4, 'mem': 0.8}\n"
                    "print('METRICS_OK')\n"
                ),
                "expected_stdout_contains": ["METRICS_OK"],
            },
            permission_level=PermissionLevel.L1.value,
        )
        assert l1_row["status"] == SkillStatus.DRAFT.value

        l1_val = await skills.validate_skill(l1_row["id"])
        assert l1_val["passed"] is True
        assert l1_val["status"] == SkillStatus.VALIDATED.value
        assert l1_val["awaiting_human_approval"] is False

        # 2. L2 skill passes isolated tests but MUST stay DRAFT until explicit human approval
        l2_row = await skills.register_skill(
            skill_key="l2_report_formatter",
            version="1.0.0",
            capability_key="coding",
            description="Format summary table for review",
            artifact_code="def fmt(rows): return '\\n'.join(str(r) for r in rows)\n",
            test_spec={
                "kind": "python_assertions",
                "code": "from skill import fmt\nassert fmt([1, 2]) == '1\\n2'\n",
            },
            permission_level=PermissionLevel.L2.value,
        )
        l2_val_pending = await skills.validate_skill(l2_row["id"])
        assert l2_val_pending["passed"] is True
        assert l2_val_pending["status"] == SkillStatus.DRAFT.value
        assert l2_val_pending["awaiting_human_approval"] is True

        # Explicit human approval promotes L2 skill to VALIDATED
        l2_val_approved = await skills.validate_skill(l2_row["id"], approved_l2_promotion=True)
        assert l2_val_approved["passed"] is True
        assert l2_val_approved["status"] == SkillStatus.VALIDATED.value
        assert l2_val_approved["awaiting_human_approval"] is False

        reusable_before = await skills.list_reusable_skills_for_context(limit=10)
        assert {s["skill_key"] for s in reusable_before} == {"metrics_parser", "l2_report_formatter"}

        # 3. Silent tamper immediately excludes L1 skill from reusable context
        Path(l1_row["artifact_path"]).write_text("def parse_metrics(raw): return {'tampered': 1.0}\n", encoding="utf-8")
        reusable_after_tamper = await skills.list_reusable_skills_for_context(limit=10)
        assert {s["skill_key"] for s in reusable_after_tamper} == {"l2_report_formatter"}

        # 4. Revoke transitions L2 skill to DEPRECATED and excludes it from reusable context
        rev = await skills.revoke_skill(l2_row["id"], reason="superseded by v2")
        assert rev["status"] == SkillStatus.DEPRECATED.value
        assert await skills.list_reusable_skills_for_context(limit=10) == []

        # 5. Unsafe artifact blocked at register_skill() before touching disk/DB
        with pytest.raises(SkillGovernanceError):
            await skills.register_skill(
                skill_key="bad_sql_skill",
                version="1.0.0",
                capability_key="coding",
                description="Attempts direct SQL access",
                artifact_code="import sqlalchemy\ndef wipe(): return 'DROP TABLE active_goals'\n",
            )
    finally:
        await db.dispose()


class _SpyCodingAgent(AgentInterface):
    """Simulates an external CLI Agent writing a clean skill.py + test_skill.py in its isolated workspace."""

    def __init__(self, *, write_unsafe: bool = False) -> None:
        self._cap = AgentCapability(
            agent_id="codex",
            name="Spy Codex",
            agent_type="cli",
            capabilities=["coding", "research", "general_task"],
            supported_modes=["read-only", "workspace-write"],
            enabled=True,
        )
        self.write_unsafe = write_unsafe
        self.execute_calls = 0

    @property
    def capability(self) -> AgentCapability:
        return self._cap

    async def probe(self) -> AgentProbeResult:
        return AgentProbeResult(ok=True, binary="codex", version="spy-m4")

    async def execute(self, task: AgentTask) -> AgentExecution:
        self.execute_calls += 1
        ws = Path(task.workspace)
        ws.mkdir(parents=True, exist_ok=True)
        if self.write_unsafe:
            (ws / "skill.py").write_text("import os\ndef hack(): os.system('rm -rf /tmp/x')\n", encoding="utf-8")
            (ws / "test_skill.py").write_text("assert True\n", encoding="utf-8")
        else:
            (ws / "skill.py").write_text(
                "from collections import Counter\n\n"
                "def summarize_counts(items: list[str]) -> dict[str, int]:\n"
                "    return dict(Counter(str(x).strip().lower() for x in items if str(x).strip()))\n",
                encoding="utf-8",
            )
            (ws / "test_skill.py").write_text(
                "from skill import summarize_counts\n"
                "res = summarize_counts(['Alpha', 'beta', 'alpha ', ''])\n"
                "assert res == {'alpha': 2, 'beta': 1}, res\n"
                "print('SKILL_SELF_TEST_PASSED')\n",
                encoding="utf-8",
            )
        return AgentExecution(
            execution_id=task.execution_id or str(uuid.uuid4()),
            task_id=task.task_id,
            agent_id="codex",
            status="SUCCEEDED",
            started_at=utcnow(),
            completed_at=utcnow(),
            result={
                "summary": "Created skill.py and test_skill.py in isolated workspace",
                "workspace_files": ["skill.py", "test_skill.py"],
            },
            stdout="SKILL_SELF_TEST_PASSED",
        )

    async def get_status(self, execution_id: str) -> AgentStatus:
        return AgentStatus(execution_id=execution_id, status="SUCCEEDED")

    async def cancel(self, execution_id: str) -> bool:
        return True


class _SkillLifecycleBrain(BrainInterface):
    def __init__(self) -> None:
        self.cycle_calls = 0
        self.seen_validated_skills: list[list[dict[str, Any]]] = []

    async def assess(self, observation: Observation, memories: list[dict[str, Any]], drives: list[Any]) -> AssessmentResult:
        return AssessmentResult(summary="Ready to build and reuse coding skills", confidence=0.9)

    async def generate_goal(self, ctx: CognitiveContext) -> GoalProposal:
        self.cycle_calls += 1
        self.seen_validated_skills.append(list(ctx.validated_skills))
        if self.cycle_calls == 1:
            return GoalProposal(
                should_act=True,
                title="Build reusable Python frequency counter skill",
                description="Write skill.py and test_skill.py inside isolated workspace for text item counting.",
                related_key="summarize_counts",
                success_criteria=["skill.py and test_skill.py created and verified"],
                rationale="Acquire verified coding capability and reusable skill.",
            )
        # Cycle 2+: no more work needed (we already captured ctx.validated_skills!)
        return GoalProposal(should_act=False, title="", rationale="Observed validated skills in context")

    async def plan(self, ctx: CognitiveContext) -> ActionPlan:
        return ActionPlan(
            should_execute=True,
            goal_title=ctx.goal.title if ctx.goal else "Coding skill",
            objective="Implement summarize_counts utility in skill.py and assertion test in test_skill.py",
            description="Write clean pure-Python utility and self-contained test inside task workspace.",
            assigned_agent="codex",
            required_capability="coding",
            permission_level="L1",
            verification=[
                VerificationSpec(kind="file_exists", target="skill.py"),
                VerificationSpec(kind="file_exists", target="test_skill.py"),
            ],
            expected_improvement=0.25,
        )

    async def evaluate(self, ctx: EvaluationContext) -> EvaluationResult:
        return EvaluationResult(
            goal_achieved=ctx.verification_passed,
            summary="Built and verified summarize_counts utility skill in isolated workspace.",
            learned_facts=["summarize_counts normalizes whitespace and casing deterministically."],
            acquired_skill="summarize_counts",
            information_gain=0.6,
            should_persist_memory=True,
        )


@pytest.mark.asyncio
async def test_v3_m4_end_to_end_loop_skill_crystallization_and_reuse(tmp_path: Path):
    """End-to-end loop: Agent produces skill.py + test_skill.py -> auto-promoted VALIDATED -> injected into Cycle 2."""
    db_path = tmp_path / "v3_m4_e2e.db"
    db = Database(f"sqlite+aiosqlite:///{db_path}")
    await _init_db(db)

    reg = AgentRegistry()
    spy_agent = _SpyCodingAgent(write_unsafe=False)
    reg.register(spy_agent, probe=AgentProbeResult(ok=True, binary="codex", version="spy-m4"))
    brain = _SkillLifecycleBrain()

    settings = Settings(
        database=DatabaseSettings(url=f"sqlite+aiosqlite:///{db_path}"),
        governance=GovernanceSettings(workspace_root=str(tmp_path / "ws")),
        memory=MemorySettings(provider="none"),
        embedding=EmbeddingSettings(provider="hash_fallback"),
        perception=PerceptionSettings(llm_probe=True),
        runner=RunnerSettings(enabled=False),
        kernel_v3=KernelV3Settings(skill_auto_crystallize_enabled=True),
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
        # Cycle 1: executes L1 coding task, auto-crystallizes & auto-promotes skill to VALIDATED
        c1 = await rt.loop.run_cycle()
        assert c1["outcome"] == "SUCCEEDED"
        assert spy_agent.execute_calls == 1
        assert brain.seen_validated_skills[0] == []

        cryst = (c1.get("evaluation") or {}).get("skill_crystallized")
        assert cryst is not None
        assert cryst["crystallized"] is True
        assert cryst["passed"] is True
        assert cryst["status"] == SkillStatus.VALIDATED.value
        assert cryst["awaiting_human_approval"] is False

        # Self Model recorded real coding evidence and Skill Registry shows 1 integrity-verified VALIDATED skill
        assert await rt.skill_registry.count_validated_skills(verify_integrity=True) == 1
        prof = await rt.repo.get_capability_profile(rt.organism_id, "coding")
        assert prof is not None
        assert prof["sample_count"] >= 1
        assert prof["maturity_status"] in {CapabilityMaturity.ACQUIRED.value, CapabilityMaturity.VALIDATED.value}
        post_rep = (c1.get("drives_post") or {}).get("reproduction") or {}
        assert (post_rep.get("dimensions") or {}).get("reusable_skills", 0.0) > 0.0

        # Simulate time elapsed + open knowledge gaps so Cycle 2 enters GOAL_GENERATION
        rt.loop._last_learning_at = None
        rt.loop._last_info_gain = None
        for topic_key in (
            "pgvector_hnsw_tuning",
            "letta_archival_indexing",
            "mcp_tool_isolation",
            "skill_sandbox_design",
            "rsi_verification_patterns",
        ):
            await rt.repo.upsert_world_item(
                kind="knowledge_topic",
                key=topic_key,
                value={"description": f"Explore {topic_key}", "importance": 0.9},
                status="open",
                source="test_m4",
            )
        # Cycle 2: Brain automatically receives the newly VALIDATED skill in CognitiveContext.validated_skills
        c2 = await rt.loop.run_cycle()
        assert c2["outcome"] == "IDLE"
        assert len(brain.seen_validated_skills) == 2
        injected = brain.seen_validated_skills[1]
        assert len(injected) == 1
        assert injected[0]["skill_key"] == "summarize_counts"
        assert injected[0]["capability_key"] == "coding"
    finally:
        await rt.stop()
        await db.dispose()


@pytest.mark.asyncio
async def test_v3_m4_unsafe_workspace_skill_blocked_without_crashing_finalizer(tmp_path: Path):
    """An unsafe skill.py written in workspace is cleanly rejected by static scanner without breaking Finalizer."""
    db_path = tmp_path / "v3_m4_unsafe.db"
    db = Database(f"sqlite+aiosqlite:///{db_path}")
    await _init_db(db)

    reg = AgentRegistry()
    spy_agent = _SpyCodingAgent(write_unsafe=True)
    reg.register(spy_agent, probe=AgentProbeResult(ok=True, binary="codex", version="spy-m4"))
    brain = _SkillLifecycleBrain()

    settings = Settings(
        database=DatabaseSettings(url=f"sqlite+aiosqlite:///{db_path}"),
        governance=GovernanceSettings(workspace_root=str(tmp_path / "ws")),
        memory=MemorySettings(provider="none"),
        embedding=EmbeddingSettings(provider="hash_fallback"),
        perception=PerceptionSettings(llm_probe=True),
        runner=RunnerSettings(enabled=False),
        kernel_v3=KernelV3Settings(skill_auto_crystallize_enabled=True),
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
        c1 = await rt.loop.run_cycle()
        assert c1["outcome"] == "SUCCEEDED"
        cryst = (c1.get("evaluation") or {}).get("skill_crystallized")
        assert cryst is not None
        assert cryst["crystallized"] is False
        assert cryst["rejected_unsafe"] is True
        assert await rt.skill_registry.count_validated_skills(verify_integrity=True) == 0
    finally:
        await rt.stop()
        await db.dispose()
