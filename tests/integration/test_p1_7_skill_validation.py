"""P1-7 real integration tests:
1. Real task closure -> capability_profiles / capability_evidence / agent_registry.stats /
   self_model_versions -> Outbox dynamic projection to live Letta `self_model` Core Block ->
   3 real validated reusable Skills -> full restart consistency (100%).
2. Real PostgreSQL 16 production snapshot migration 0001 -> 0007_p1_7_self_model + PG16 CRUD.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
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
from app.core.config import GovernanceSettings, MemorySettings, PerceptionSettings, Settings
from app.core.enums import ExecutionStatus, MemoryType, PermissionLevel
from app.core.runtime import OrganismRuntime
from app.memory.letta_adapter import LettaAdapter
from app.perception.interfaces import Observation, utcnow
from app.self_model.interfaces import CapabilityMaturity, SkillStatus
from app.storage.database import Database
from app.storage.models import Base
from app.storage.repository import StateRepository


class _P17ScriptedBrain(BrainInterface):
    async def assess(self, observation: Observation, memories: list[dict[str, Any]], drives: list[Any]) -> AssessmentResult:
        return AssessmentResult(summary="Assessing capability deficits", confidence=0.95)

    async def generate_goal(self, ctx: CognitiveContext) -> GoalProposal:
        return GoalProposal(
            should_act=True,
            title="Build verified system_diagnosis capability",
            description="Execute read-only system health verification and persist evidence",
            drive_type=ctx.decision.selected_drive,
            related_key="system_diagnosis",
            success_criteria=["Output report file created"],
            expected_outcome={"capability": "system_diagnosis"},
            rationale="Improve reproduction capability coverage via verified evidence",
        )

    async def plan(self, ctx: CognitiveContext) -> ActionPlan:
        return ActionPlan(
            should_execute=True,
            goal_title=ctx.goal.title if ctx.goal else "Capability task",
            objective="Generate verified diagnostic summary report",
            description="Deterministic capability verification",
            assigned_agent="worker-p17",
            required_capability="system_diagnosis",
            timeout_seconds=30,
            permission_level=PermissionLevel.L1.value,
            verification=[VerificationSpec(kind="file_exists", target="diag_report.txt")],
        )

    async def evaluate(self, ctx: EvaluationContext) -> EvaluationResult:
        ok = ctx.verification_passed
        return EvaluationResult(
            goal_achieved=ok,
            summary="Verified system_diagnosis execution completed" if ok else "Failed",
            learned_facts=["System headroom verified deterministically"] if ok else [],
            acquired_skill="system_diagnosis" if ok else None,
            should_persist_memory=True,
            memory_type=MemoryType.PROCEDURAL.value,
            memory_importance=0.85,
        )


class _DeterministicWorker(AgentInterface):
    def __init__(self, agent_id: str = "worker-p17") -> None:
        self._cap = AgentCapability(
            agent_id=agent_id,
            name="P1-7 Deterministic Worker",
            agent_type="cli",
            capabilities=["system_diagnosis", "repository_analysis", "knowledge_summarization"],
            supported_modes=["read-only", "workspace-write"],
        )

    @property
    def capability(self) -> AgentCapability:
        return self._cap

    async def probe(self) -> AgentProbeResult:
        return AgentProbeResult(ok=True, binary="worker-p17", version="1.0")

    async def get_status(self, execution_id: str) -> AgentStatus:
        return AgentStatus(execution_id=execution_id, agent_id=self._cap.agent_id, status="SUCCEEDED")

    async def cancel(self, execution_id: str) -> bool:
        return True

    async def execute(self, task: AgentTask) -> AgentExecution:
        ws = Path(task.workspace)
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "diag_report.txt").write_text("disk_ok=true\nmem_ok=true\n", encoding="utf-8")
        return AgentExecution(
            execution_id=task.execution_id or str(uuid.uuid4()),
            task_id=task.task_id,
            agent_id=self._cap.agent_id,
            status=ExecutionStatus.SUCCEEDED.value,
            started_at=utcnow(),
            completed_at=utcnow(),
            result={"summary": "System diagnostic report written cleanly."},
            stdout="ok",
            resource_usage={"wall_seconds": 0.12},
        )


def test_real_task_closure_letta_self_model_3_skills_and_restart_consistency():
    """End-to-end integration test against live Letta 0.16.4 (when running on localhost:8283)."""

    async def _run() -> None:
        with tempfile.TemporaryDirectory(prefix="aoc-p1-7-int-") as tmpdir:
            tmp_path = Path(tmpdir)
            db_url = f"sqlite+aiosqlite:///{tmp_path / 'p1_7_int.db'}"

            # Spin up a tiny local /v1/embeddings endpoint reachable from Dockerized aoc-letta (172.18.0.1)
            from fastapi import FastAPI
            import uvicorn

            emb_app = FastAPI()

            @emb_app.post("/v1/embeddings")
            async def _fake_emb(body: dict[str, Any]) -> dict[str, Any]:
                inp = body.get("input", "")
                items = [inp] if isinstance(inp, str) else list(inp)
                return {
                    "object": "list",
                    "model": body.get("model", "BAAI/bge-small-zh-v1.5"),
                    "data": [{"object": "embedding", "index": i, "embedding": [0.01] * 512} for i in range(len(items))],
                    "usage": {"prompt_tokens": 1, "total_tokens": 1},
                }

            emb_port = 19242
            emb_server = uvicorn.Server(uvicorn.Config(emb_app, host="0.0.0.0", port=emb_port, log_level="warning"))
            emb_task = asyncio.create_task(emb_server.serve())
            for _ in range(50):
                if emb_server.started:
                    break
                await asyncio.sleep(0.05)

            letta_settings = MemorySettings(
                provider="letta",
                letta_base_url=os.environ.get("AOC_LETTA_URL", "http://127.0.0.1:8283"),
                agent_name=f"aoc-p17-test-{uuid.uuid4().hex[:8]}",
                embedding_endpoint=f"http://172.18.0.1:{emb_port}/v1",
                embedding_model="BAAI/bge-small-zh-v1.5",
                embedding_dim=512,
                timeout_seconds=10.0,
            )
            letta_probe = LettaAdapter(letta_settings)
            letta_live = await letta_probe.health()
            if not letta_live:
                emb_server.should_exit = True
                await emb_task
                pytest.fail("Live Letta server on 127.0.0.1:8283 is required for P1-7 Letta projection test")

            settings = Settings(
                database={"url": db_url},
                governance=GovernanceSettings(workspace_root=str(tmp_path / "ws")),
                memory=letta_settings,
                perception=PerceptionSettings(llm_probe=False),
            )
            created_letta_agent_id: str | None = None

            # ── Phase 1: Boot Runtime #1, run real task closure & sync Letta ──
            db1 = Database(db_url)
            async with db1.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            reg1 = AgentRegistry(repo=StateRepository(db1))
            reg1.register(_DeterministicWorker("worker-p17"))

            rt1 = OrganismRuntime(
                settings=settings,
                db=db1,
                brain=_P17ScriptedBrain(),
                registry=reg1,
                auto_load_agents=False,
            )
            try:
                await rt1.start(start_scheduler=False)
                if isinstance(rt1.memory.backend, LettaAdapter):
                    created_letta_agent_id = rt1.memory.backend.agent_id
                assert rt1.self_model is not None and rt1.skill_registry is not None

                # Confirm static external Agent capabilities started strictly as AVAILABLE (0 acquired!)
                acq_before, val_before = await rt1.self_model.get_evidence_backed_capabilities()
                assert acq_before == [] and val_before == []

                # Seed curiosity satisfied so Reproduction cleanly wins the cycle
                rt1.loop._last_learning_at = utcnow()
                rt1.loop._last_info_gain = 0.95
                rt1.loop._last_info_gain_at = utcnow()
                for i in range(5):
                    await rt1.repo.add_memory_index(
                        memory_type=MemoryType.SEMANTIC.value,
                        title=f"seed-fact-{i}",
                        summary=f"fact {i}",
                        content_hash=f"p17-seed-{i}",
                        source="seed",
                        importance=0.8,
                        tags=[],
                        sync_status="synced",
                    )

                rec = await rt1.loop.run_cycle()
                assert rec["decision"]["selected_drive"] == "reproduction"
                assert rec["outcome"] == "SUCCEEDED"

                # Capability evidence & profile updated atomically inside TaskFinalizer
                caps = await rt1.self_model.list_capabilities()
                sys_cap = next(c for c in caps if c["capability_key"] == "system_diagnosis")
                assert sys_cap["maturity_status"] == CapabilityMaturity.ACQUIRED.value
                assert sys_cap["sample_count"] == 1
                assert len(sys_cap["recent_evidence"]) == 1

                # agent_registry.stats updated empirically
                agents = await rt1.repo.list_agents()
                w_row = next(a for a in agents if a["agent_id"] == "worker-p17")
                assert w_row["stats"]["verified_successes"] == 1

                # ── Phase 2: Register & isolate-validate 3 REAL reusable Skills ──
                skill_specs = [
                    {
                        "skill_key": "disk_headroom_inspector",
                        "version": "1.0.0",
                        "capability_key": "system_diagnosis",
                        "description": "Checks disk usage ratio for a given path and classifies health band.",
                        "applicable_scenarios": ["survival disk pressure diagnosis", "capacity audit"],
                        "steps": ["Call shutil.disk_usage(path)", "Compute free_ratio", "Classify status"],
                        "artifact_code": (
                            "import shutil\n\n"
                            "def inspect_disk(path: str = '/') -> dict:\n"
                            "    u = shutil.disk_usage(path)\n"
                            "    ratio = u.free / u.total if u.total else 0.0\n"
                            "    band = 'healthy' if ratio >= 0.20 else ('warning' if ratio >= 0.05 else 'critical')\n"
                            "    return {'path': path, 'free_ratio': round(ratio, 4), 'band': band}\n"
                        ),
                        "test_spec": {
                            "kind": "python_assertions",
                            "code": (
                                "import importlib.util, os\n"
                                "spec = importlib.util.spec_from_file_location('s', os.environ['AOC_SKILL_ARTIFACT'])\n"
                                "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
                                "res = mod.inspect_disk(os.environ['AOC_SKILL_WORKDIR'])\n"
                                "assert 0.0 <= res['free_ratio'] <= 1.0\n"
                                "assert res['band'] in {'healthy', 'warning', 'critical'}\n"
                                "print('SKILL_1_OK')\n"
                            ),
                            "expected_stdout_contains": ["SKILL_1_OK"],
                        },
                    },
                    {
                        "skill_key": "git_commit_summarizer",
                        "version": "1.0.0",
                        "capability_key": "repository_analysis",
                        "description": "Parses conventional commit messages into structured type/scope/subject records.",
                        "applicable_scenarios": ["git changelog analysis", "release audit"],
                        "steps": ["Parse prefix type(scope): subject", "Group by commit type"],
                        "artifact_code": (
                            "import re\n\n"
                            "CONV_RE = re.compile(r'^(?P<type>[a-z]+)(?:\\((?P<scope>[^)]+)\\))?:\\s*(?P<subject>.+)$')\n\n"
                            "def summarize_commits(subjects: list[str]) -> dict:\n"
                            "    counts: dict[str, int] = {}\n"
                            "    for s in subjects:\n"
                            "        m = CONV_RE.match(s.strip())\n"
                            "        ctype = m.group('type') if m else 'other'\n"
                            "        counts[ctype] = counts.get(ctype, 0) + 1\n"
                            "    return {'total': len(subjects), 'by_type': counts}\n"
                        ),
                        "test_spec": {
                            "kind": "python_assertions",
                            "code": (
                                "import importlib.util, os\n"
                                "spec = importlib.util.spec_from_file_location('s', os.environ['AOC_SKILL_ARTIFACT'])\n"
                                "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
                                "r = mod.summarize_commits(['feat(p1-7): self model', 'fix(api): auth', 'misc'])\n"
                                "assert r == {'total': 3, 'by_type': {'feat': 1, 'fix': 1, 'other': 1}}, r\n"
                                "print('SKILL_2_OK')\n"
                            ),
                            "expected_stdout_contains": ["SKILL_2_OK"],
                        },
                    },
                    {
                        "skill_key": "markdown_fact_extractor",
                        "version": "1.0.0",
                        "capability_key": "knowledge_summarization",
                        "description": "Extracts deduplicated bullet-point facts from Markdown research notes.",
                        "applicable_scenarios": ["curiosity research consolidation", "memory indexing"],
                        "steps": ["Scan markdown bullet lines", "Deduplicate preserving order"],
                        "artifact_code": (
                            "def extract_facts(md_text: str) -> list[str]:\n"
                            "    seen = set()\n"
                            "    facts = []\n"
                            "    for line in md_text.splitlines():\n"
                            "        s = line.strip()\n"
                            "        if s.startswith(('- ', '* ')):\n"
                            "            body = s[2:].strip()\n"
                            "            if body and body.lower() not in seen:\n"
                            "                seen.add(body.lower())\n"
                            "                facts.append(body)\n"
                            "    return facts\n"
                        ),
                        "test_spec": {
                            "kind": "python_assertions",
                            "code": (
                                "import importlib.util, os\n"
                                "spec = importlib.util.spec_from_file_location('s', os.environ['AOC_SKILL_ARTIFACT'])\n"
                                "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
                                "f = mod.extract_facts('# Title\\n- Fact A\\n* fact a\\n- Fact B\\n')\n"
                                "assert f == ['Fact A', 'Fact B'], f\n"
                                "print('SKILL_3_OK')\n"
                            ),
                            "expected_stdout_contains": ["SKILL_3_OK"],
                        },
                    },
                ]

                validated_ids: list[str] = []
                for spec in skill_specs:
                    reg = await rt1.skill_registry.register_skill(**spec)
                    assert reg["status"] == SkillStatus.DRAFT.value
                    val = await rt1.skill_registry.validate_skill(reg["id"])
                    assert val["passed"] is True, f"Skill {spec['skill_key']} failed: {val}"
                    assert val["status"] == SkillStatus.VALIDATED.value
                    validated_ids.append(reg["id"])

                assert await rt1.skill_registry.count_validated_skills(verify_integrity=True) == 3

                # Drain Outbox so `self_model.sync_letta` projects latest version to LIVE Letta Core Block!
                for _ in range(5):
                    st = await rt1.outbox.dispatch_once(batch_size=50)
                    if st["claimed"] == 0:
                        break

                snap_before_restart = await rt1.self_model.get_latest_snapshot()
                assert snap_before_restart["letta_synced_at"] is not None
                assert set(snap_before_restart["profile_snapshot"]["validated_capabilities"]) == {
                    "system_diagnosis",
                    "repository_analysis",
                    "knowledge_summarization",
                }
                assert len(snap_before_restart["profile_snapshot"]["validated_skills"]) == 3

                # Verify live Letta Core Block actually contains our dynamic Self Model projection!
                assert isinstance(rt1.memory.backend, LettaAdapter)
                blocks = await rt1.memory.backend.get_core_blocks()
                assert "self_model" in blocks
                assert "disk_headroom_inspector@1.0.0" in blocks["self_model"]
                assert "markdown_fact_extractor@1.0.0" in blocks["self_model"]
                caps_before = await rt1.self_model.list_capabilities()
            finally:
                await rt1.stop()
                await db1.dispose()

            # ── Phase 3: Full Restart Consistency Check (Brand-new Runtime #2) ──
            db2 = Database(db_url)
            reg2 = AgentRegistry(repo=StateRepository(db2))
            reg2.register(_DeterministicWorker("worker-p17"))
            rt2 = OrganismRuntime(
                settings=settings,
                db=db2,
                brain=_P17ScriptedBrain(),
                registry=reg2,
                auto_load_agents=False,
            )
            await rt2.start(start_scheduler=False)
            try:
                assert rt2.self_model is not None and rt2.skill_registry is not None
                snap_after_restart = await rt2.self_model.get_latest_snapshot()
                assert snap_after_restart["version_number"] == snap_before_restart["version_number"]
                assert snap_after_restart["summary_text"] == snap_before_restart["summary_text"]
                assert snap_after_restart["profile_snapshot"]["validated_capabilities"] == (
                    snap_before_restart["profile_snapshot"]["validated_capabilities"]
                )
                assert await rt2.skill_registry.count_validated_skills(verify_integrity=True) == 3

                caps_after = await rt2.self_model.list_capabilities()
                assert [
                    (c["capability_key"], c["maturity_status"], c["sample_count"], c["estimated_success_rate"])
                    for c in caps_after
                ] == [
                    (c["capability_key"], c["maturity_status"], c["sample_count"], c["estimated_success_rate"])
                    for c in caps_before
                ]
            finally:
                await rt2.stop()
                await db2.dispose()
                # Clean up ephemeral Letta test agent & local embedding server
                if created_letta_agent_id:
                    try:
                        await letta_probe._req("DELETE", f"/v1/agents/{created_letta_agent_id}")
                    except Exception:
                        pass
                emb_server.should_exit = True
                await emb_task

    asyncio.run(_run())


def test_pg16_production_snapshot_migration_0001_to_0007():
    """Verify disposable PG16 snapshot restored from backups/aoc_pg_dump_20261120.sql upgrades cleanly to 0007."""
    pg_url = os.environ.get("AOC_TEST_PG_URL") or os.environ.get("AOC_PG_SNAPSHOT_TEST_URL")
    if not pg_url:
        pytest.skip("Set AOC_TEST_PG_URL (via run_pg_snapshot_mig_test.py) to run PG16 snapshot test")

    import subprocess
    import sys
    from sqlalchemy import inspect, text

    project_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["AOC_DATABASE__URL"] = pg_url

    def _run_alembic(*args: str) -> None:
        res = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=str(project_root),
            env=env,
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, f"alembic {args} failed:\n{res.stdout}\n{res.stderr}"

    # Upgrade from V1.0 snapshot (0001) to head (0007_p1_7_self_model)
    _run_alembic("upgrade", "head")
    # Re-run upgrade to verify strict idempotency
    _run_alembic("upgrade", "head")

    # Inspect PG16 schema, verify snapshot integrity, and run async SelfModel + Skill CRUD on PG16
    async def _pg_crud() -> None:
        db = Database(pg_url)
        repo = StateRepository(db)
        try:
            async with db.engine.connect() as conn:
                tables = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
                for tbl in (
                    "self_model_versions",
                    "capability_profiles",
                    "capability_evidence",
                    "skill_registry",
                    "skill_validation_results",
                ):
                    assert tbl in tables, f"Missing table {tbl} after 0007 migration"
                agent_cols = await conn.run_sync(lambda c: {col["name"] for col in inspect(c).get_columns("agent_registry")})
                assert "stats" in agent_cols

                org_row = (await conn.execute(text("SELECT id, name FROM organism_identity WHERE name = 'AOC-001'"))).fetchone()
                assert org_row is not None
                outbox_cnt = (await conn.execute(text("SELECT count(*) FROM outbox_events"))).scalar_one()
                assert outbox_cnt == 0

            org_id = await repo.get_default_organism_id()
            assert org_id is not None
            from app.self_model.manager import SelfModelManager
            from app.self_model.skills import SkillRegistryManager

            with tempfile.TemporaryDirectory(prefix="aoc-pg-p17-") as td:
                skills = SkillRegistryManager(repo=repo, config={"skill_storage_dir": td})
                sm = SelfModelManager(organism_id=org_id, repo=repo, config={"skill_storage_dir": td}, skill_registry=skills)
                skills.self_model = sm

                sk = await skills.register_skill(
                    skill_key="pg16_probe_skill",
                    version="1.0.0",
                    capability_key="system_diagnosis",
                    description="PG16 snapshot skill verification",
                    artifact_code="def ok(): return 'pg16-ok'\n",
                    test_spec={
                        "kind": "python_assertions",
                        "code": (
                            "import importlib.util, os\n"
                            "spec = importlib.util.spec_from_file_location('s', os.environ['AOC_SKILL_ARTIFACT'])\n"
                            "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
                            "assert mod.ok() == 'pg16-ok'\n"
                        ),
                    },
                )
                val = await skills.validate_skill(sk["id"])
                assert val["passed"] is True
                assert val["status"] == SkillStatus.VALIDATED.value
                snap = await sm.get_latest_snapshot()
                assert snap["version_number"] >= 1
                assert "system_diagnosis" in snap["profile_snapshot"]["validated_capabilities"]
        finally:
            await db.dispose()

    asyncio.run(_pg_crud())
