"""P1-7 unit & fault-injection tests: Dynamic Self Model, Beta-Binomial posterior,
Skill Registry with isolated validation & SHA-256 tamper detection, Reproduction T10 fix,
V2 Self Model / Skill APIs, and Alembic 0007 upgrade/downgrade roundtrip.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, inspect

from app.api.app import create_app
from app.core.config import ApiSettings, GovernanceSettings, MemorySettings, PerceptionSettings, Settings
from app.core.enums import ExecutionStatus, TaskStatus
from app.core.runtime import OrganismRuntime
from app.drives.engine import DriveEngine
from app.drives.interfaces import DriveContext
from app.governance.policy import GovernanceManager
from app.perception.interfaces import Observation, utcnow
from app.self_model.interfaces import CapabilityMaturity, SkillStatus, compute_beta_binomial_stats
from app.self_model.manager import SelfModelManager
from app.self_model.skills import SkillGovernanceError, SkillRegistryManager
from app.storage.database import Database
from app.storage.outbox import OutboxDispatcher, OutboxEventType
from app.storage.repository import StateRepository

PROJECT_ROOT = Path(__file__).resolve().parents[2]


async def _seed_task_and_exec(repo: StateRepository, agent_id: str = "codex") -> tuple[uuid.UUID, uuid.UUID]:
    tid = await repo.create_task(
        objective="Test capability task",
        description="desc",
        assigned_agent=agent_id,
        permission_level="L0",
        workspace="/tmp/test",
        timeout_seconds=60,
        status=TaskStatus.SUCCEEDED.value,
    )
    eid = await repo.create_execution(
        task_id=tid,
        agent_id=agent_id,
        attempt_number=1,
        status=ExecutionStatus.SUCCEEDED.value,
    )
    return tid, eid


@pytest.mark.asyncio
async def test_beta_binomial_posterior_and_minimum_sample_guard(db: Database):
    repo = StateRepository(db)
    ident = await repo.get_or_create_identity("AOC-P1-7-MATH", "2.0.0")
    sm = SelfModelManager(
        organism_id=ident["id"],
        repo=repo,
        config={
            "alpha_prior": 1.0,
            "beta_prior": 1.0,
            "minimum_samples": 3,
            "confidence_threshold": 0.55,
            "confidence_level": 0.90,
            "deprecate_after_failures": 5,
            "deprecate_max_success_rate": 0.25,
        },
    )

    # Prior sanity: Beta(1, 1) -> mean 0.5, 90% one-sided low=0.10, high=0.90
    p0 = compute_beta_binomial_stats(alpha_prior=1.0, beta_prior=1.0, sample_count=0)
    assert abs(p0.estimated_success_rate - 0.5) < 1e-6
    assert abs(p0.confidence_low - 0.10) < 1e-4
    assert abs(p0.confidence_high - 0.90) < 1e-4

    # 1 verified success -> ACQUIRED (NOT VALIDATED: sample_count=1 < 3)
    t1, e1 = await _seed_task_and_exec(repo)
    r1 = await sm.record_execution_evidence(
        capability_key="system_diagnosis",
        execution_id=e1,
        task_id=t1,
        verified_success=True,
        verification_summary="passed check 1",
    )
    assert r1["inserted"] is True
    assert r1["profile"]["maturity_status"] == CapabilityMaturity.ACQUIRED.value
    assert r1["profile"]["sample_count"] == 1
    assert abs(r1["profile"]["estimated_success_rate"] - (2.0 / 3.0)) < 1e-4

    # 2 verified successes -> STILL ACQUIRED (sample_count=2 < minimum_samples=3!)
    t2, e2 = await _seed_task_and_exec(repo)
    r2 = await sm.record_execution_evidence(
        capability_key="system_diagnosis",
        execution_id=e2,
        task_id=t2,
        verified_success=True,
        verification_summary="passed check 2",
    )
    assert r2["profile"]["maturity_status"] == CapabilityMaturity.ACQUIRED.value
    assert r2["profile"]["sample_count"] == 2
    assert r2["profile"]["confidence_low"] < 0.55

    # 3rd verified success -> Beta(4, 1), low=0.5623 >= 0.55 & sample_count=3 -> VALIDATED!
    t3, e3 = await _seed_task_and_exec(repo)
    r3 = await sm.record_execution_evidence(
        capability_key="system_diagnosis",
        execution_id=e3,
        task_id=t3,
        verified_success=True,
        verification_summary="passed check 3",
    )
    assert r3["profile"]["maturity_status"] == CapabilityMaturity.VALIDATED.value
    assert r3["profile"]["sample_count"] == 3
    assert r3["profile"]["confidence_low"] >= 0.55
    assert r3["version"]["version_number"] == 3

    # Mixed 2 ok + 1 fail -> low=0.32 < 0.55 -> stays ACQUIRED even with sample_count=3
    for ok in (True, True, False):
        tx, ex = await _seed_task_and_exec(repo)
        res = await sm.record_execution_evidence(
            capability_key="coding", execution_id=ex, task_id=tx, verified_success=ok
        )
    assert res["profile"]["sample_count"] == 3
    assert res["profile"]["confidence_low"] < 0.55
    assert res["profile"]["maturity_status"] == CapabilityMaturity.ACQUIRED.value

    # 5 consecutive failures -> DEPRECATED
    for _ in range(5):
        tx, ex = await _seed_task_and_exec(repo)
        dep = await sm.record_execution_evidence(
            capability_key="flaky_cap", execution_id=ex, task_id=tx, verified_success=False
        )
    assert dep["profile"]["maturity_status"] == CapabilityMaturity.DEPRECATED.value
    assert dep["profile"]["estimated_success_rate"] <= 0.25


@pytest.mark.asyncio
async def test_static_agent_capabilities_contribute_zero_to_reproduction(db: Database):
    repo = StateRepository(db)
    ident = await repo.get_or_create_identity("AOC-P1-7-T10", "2.0.0")
    sm = SelfModelManager(organism_id=ident["id"], repo=repo)

    desired = ["coding", "research", "repository_analysis", "system_diagnosis", "knowledge_summarization"]
    # External healthy agent declares ALL 5 desired capabilities
    await sm.sync_declared_capabilities([("codex", desired, True)])

    profiles = await sm.list_capabilities()
    assert len(profiles) == 5
    assert all(p["maturity_status"] == CapabilityMaturity.AVAILABLE.value for p in profiles)

    # Evidence-backed acquired/validated capabilities MUST be empty!
    acquired, validated = await sm.get_evidence_backed_capabilities()
    assert acquired == []
    assert validated == []

    # ReproductionDriveEvaluator MUST ignore observation.available_resources["agents"]["capabilities"]
    obs = Observation(
        system_state={"disk": {"free_ratio": 0.7}, "memory": {"available_ratio": 0.7}},
        available_resources={
            "agents": {"healthy_count": 1, "total_count": 1, "capabilities": desired},
            "budget": {"tasks_used": 0, "tasks_limit": 20},
        },
        confidence=1.0,
    )
    engine = DriveEngine()
    ctx_zero = DriveContext(
        observation=obs,
        capabilities_acquired=acquired,
        validated_capabilities=validated,
        validated_skill_count=0,
        procedural_skill_count=10,  # unvalidated procedural memories must NOT inflate validated_skill_count!
        memory_available=True,
        identity_persisted=True,
    )
    rep_zero = engine.compute_states(ctx_zero, {})["reproduction"]
    assert rep_zero.dimensions["capability_coverage"] == 0.0
    assert rep_zero.dimensions["reusable_skills"] == 0.0

    # After 2 capabilities earn real verified execution evidence, coverage rises strictly to 2/5 = 0.40
    for cap in ("coding", "system_diagnosis"):
        tx, ex = await _seed_task_and_exec(repo)
        await sm.record_execution_evidence(
            capability_key=cap, execution_id=ex, task_id=tx, verified_success=True
        )
    acquired2, validated2 = await sm.get_evidence_backed_capabilities()
    assert set(acquired2) == {"coding", "system_diagnosis"}
    ctx_two = ctx_zero.model_copy(
        update={"capabilities_acquired": acquired2, "validated_capabilities": validated2}
    )
    rep_two = engine.compute_states(ctx_two, {})["reproduction"]
    assert abs(rep_two.dimensions["capability_coverage"] - 0.40) < 1e-6


@pytest.mark.asyncio
async def test_duplicate_execution_id_evidence_is_strictly_idempotent(db: Database):
    repo = StateRepository(db)
    ident = await repo.get_or_create_identity("AOC-P1-7-IDEM", "2.0.0")
    sm = SelfModelManager(organism_id=ident["id"], repo=repo)

    tid, eid = await _seed_task_and_exec(repo)
    first = await sm.record_execution_evidence(
        capability_key="research", execution_id=eid, task_id=tid, verified_success=True
    )
    assert first["inserted"] is True
    assert first["profile"]["sample_count"] == 1
    assert first["profile"]["maturity_status"] == CapabilityMaturity.ACQUIRED.value

    # Replay the exact same execution_id 5 times -> must NOT double-count or promote to VALIDATED!
    for _ in range(5):
        dup = await sm.record_execution_evidence(
            capability_key="research", execution_id=eid, task_id=tid, verified_success=True
        )
        assert dup["inserted"] is False
        assert dup["version"] is None

    prof = await repo.get_capability_profile(ident["id"], "research")
    assert prof is not None
    assert prof["sample_count"] == 1
    assert prof["alpha"] == 2.0
    assert prof["maturity_status"] == CapabilityMaturity.ACQUIRED.value
    evs = await repo.list_capability_evidence(uuid.UUID(prof["id"]))
    assert len(evs) == 1


@pytest.mark.asyncio
async def test_skill_registry_governance_validation_timeout_and_tamper_detection(db: Database, tmp_path: Path):
    repo = StateRepository(db)
    ident = await repo.get_or_create_identity("AOC-P1-7-SKILLS", "2.0.0")
    gov = GovernanceManager(GovernanceSettings(workspace_root=str(tmp_path)), repo)
    sm_cfg = {"skill_storage_dir": str(tmp_path / "skills"), "skill_test_timeout_seconds": 5.0}
    skills = SkillRegistryManager(repo=repo, governance=gov, config=sm_cfg)
    sm = SelfModelManager(organism_id=ident["id"], repo=repo, config=sm_cfg, skill_registry=skills)
    skills.self_model = sm

    # 1. L3 dangerous skill is hard-denied by Governance
    with pytest.raises(SkillGovernanceError):
        await skills.register_skill(
            skill_key="nuke_root",
            version="1.0.0",
            capability_key="system_diagnosis",
            description="Run rm -rf / on host",
            artifact_code="import os; os.system('rm -rf /')\n",
        )

    # 2. Skill with failing assertion test MUST remain DRAFT
    bad_skill = await skills.register_skill(
        skill_key="broken_parser",
        version="1.0.0",
        capability_key="knowledge_summarization",
        description="Returns wrong output",
        artifact_code="def parse(x):\n    return 'wrong'\n",
        test_spec={
            "kind": "python_assertions",
            "code": (
                "import importlib.util, os\n"
                "spec = importlib.util.spec_from_file_location('s', os.environ['AOC_SKILL_ARTIFACT'])\n"
                "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
                "assert mod.parse('hi') == 'expected'\n"
            ),
        },
    )
    assert bad_skill["status"] == SkillStatus.DRAFT.value
    bad_val = await skills.validate_skill(bad_skill["id"])
    assert bad_val["passed"] is False
    assert bad_val["status"] == SkillStatus.DRAFT.value

    # 3. Fault injection: hanging test script times out cleanly, stays DRAFT, never crashes caller
    hang_skill = await skills.register_skill(
        skill_key="hanging_skill",
        version="1.0.0",
        capability_key="research",
        description="Sleeps forever in test",
        artifact_code="def noop(): return True\n",
        test_spec={
            "kind": "python_assertions",
            "code": "import time; time.sleep(30)\n",
            "timeout_seconds": 0.4,
        },
    )
    hang_val = await skills.validate_skill(hang_skill["id"], timeout_seconds=0.4)
    assert hang_val["passed"] is False
    assert hang_val["status"] == SkillStatus.DRAFT.value
    assert any(c["name"] == "execution_timeout" and not c["passed"] for c in hang_val["validation"]["checks"])

    # 4. Passing skill transitions to VALIDATED and promotes capability profile + Self Model version
    good_skill = await skills.register_skill(
        skill_key="kv_normalizer",
        version="1.0.0",
        capability_key="knowledge_summarization",
        description="Normalizes key/value lines deterministically",
        applicable_scenarios=["clean markdown facts"],
        steps=["split lines", "strip whitespace", "return dict"],
        artifact_code=(
            "def normalize_kv(text: str) -> dict:\n"
            "    out = {}\n"
            "    for line in text.splitlines():\n"
            "        if ':' in line:\n"
            "            k, v = line.split(':', 1)\n"
            "            out[k.strip().lower()] = v.strip()\n"
            "    return out\n"
        ),
        test_spec={
            "kind": "python_assertions",
            "code": (
                "import importlib.util, os\n"
                "spec = importlib.util.spec_from_file_location('s', os.environ['AOC_SKILL_ARTIFACT'])\n"
                "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
                "res = mod.normalize_kv('KeyA: 123\\n KeyB : hello ')\n"
                "assert res == {'keya': '123', 'keyb': 'hello'}, res\n"
                "print('OK_NORMALIZER')\n"
            ),
            "expected_stdout_contains": ["OK_NORMALIZER"],
        },
    )
    assert good_skill["status"] == SkillStatus.DRAFT.value
    assert good_skill["artifact_checksum"] and len(good_skill["artifact_checksum"]) == 64

    good_val = await skills.validate_skill(good_skill["id"])
    assert good_val["passed"] is True
    assert good_val["status"] == SkillStatus.VALIDATED.value
    assert await skills.count_validated_skills(verify_integrity=True) == 1

    prof = await repo.get_capability_profile(ident["id"], "knowledge_summarization")
    assert prof is not None and prof["maturity_status"] == CapabilityMaturity.VALIDATED.value

    # 5. SHA-256 tamper detection: silently modifying artifact invalidates integrity immediately!
    Path(good_skill["artifact_path"]).write_text("def normalize_kv(t): return {'pwned': True}\n", encoding="utf-8")
    intact, reason = await skills.verify_skill_integrity(good_skill["id"])
    assert intact is False
    assert "checksum_mismatch" in reason
    assert await skills.count_validated_skills(verify_integrity=True) == 0

    reval = await skills.validate_skill(good_skill["id"])
    assert reval["passed"] is False
    assert reval["status"] == SkillStatus.DEPRECATED.value


@pytest.mark.asyncio
async def test_v2_self_model_capabilities_and_skills_api_endpoints(db: Database, tmp_path: Path):
    read_token = "p1-7-admin-token"
    settings = Settings(
        api=ApiSettings(host="127.0.0.1", auth_enabled=True, admin_key=read_token),
        governance=GovernanceSettings(workspace_root=str(tmp_path)),
        memory=MemorySettings(provider="none"),
        perception=PerceptionSettings(llm_probe=False),
    )
    rt = OrganismRuntime(settings=settings, db=db, auto_load_agents=False)
    app = create_app(settings=settings, runtime=rt, start_scheduler=False)

    async with app.router.lifespan_context(app):
        assert rt.self_model is not None and rt.skill_registry is not None
        sk = await rt.skill_registry.register_skill(
            skill_key="api_test_skill",
            version="1.0.0",
            capability_key="system_diagnosis",
            description="Simple deterministic check",
            artifact_code="def check(): return 42\n",
            test_spec={
                "kind": "python_assertions",
                "code": (
                    "import importlib.util, os\n"
                    "spec = importlib.util.spec_from_file_location('s', os.environ['AOC_SKILL_ARTIFACT'])\n"
                    "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
                    "assert mod.check() == 42\n"
                ),
            },
        )
        await rt.skill_registry.validate_skill(sk["id"])

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Unauthorized rejected
            assert (await client.get("/v2/self-model")).status_code == 401

            headers = {"Authorization": f"Bearer {read_token}"}
            r_sm = await client.get("/v2/self-model", headers=headers)
            assert r_sm.status_code == 200
            assert r_sm.json()["version_number"] >= 2

            r_hist = await client.get("/v2/self-model/history", headers=headers)
            assert r_hist.status_code == 200
            assert len(r_hist.json()) >= 2

            r_caps = await client.get("/v2/capabilities", headers=headers)
            assert r_caps.status_code == 200
            assert any(c["capability_key"] == "system_diagnosis" and c["maturity_status"] == "VALIDATED" for c in r_caps.json())

            r_skills = await client.get("/v2/skills", headers=headers)
            assert r_skills.status_code == 200
            assert len(r_skills.json()) == 1
            assert r_skills.json()[0]["integrity_ok"] is True

            r_detail = await client.get(f"/v2/skills/{sk['id']}", headers=headers)
            assert r_detail.status_code == 200
            body = r_detail.json()
            assert body["status"] == "VALIDATED"
            assert body["integrity_ok"] is True
            assert len(body["validations"]) == 1
            assert body["validations"][0]["passed"] is True


def test_alembic_0007_upgrade_and_downgrade_roundtrip(tmp_path: Path):
    db_file = tmp_path / "alembic_0007.db"
    sync_url = f"sqlite:///{db_file}"
    env = os.environ.copy()
    env["AOC_DATABASE__URL"] = f"sqlite+aiosqlite:///{db_file}"

    def run_alembic(*args: str) -> None:
        res = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=str(PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, f"alembic {args} failed:\n{res.stdout}\n{res.stderr}"

    run_alembic("upgrade", "head")
    eng = create_engine(sync_url)
    insp = inspect(eng)
    tables = set(insp.get_table_names())
    expected_p1_7 = {
        "self_model_versions",
        "capability_profiles",
        "capability_evidence",
        "skill_registry",
        "skill_validation_results",
    }
    assert expected_p1_7.issubset(tables)
    agent_cols = {c["name"] for c in insp.get_columns("agent_registry")}
    assert "stats" in agent_cols

    run_alembic("downgrade", "0006_p1_5_world_sensors")
    insp2 = inspect(eng)
    tables2 = set(insp2.get_table_names())
    assert expected_p1_7.isdisjoint(tables2)
    agent_cols2 = {c["name"] for c in insp2.get_columns("agent_registry")}
    assert "stats" not in agent_cols2

    # Idempotent re-upgrade
    run_alembic("upgrade", "head")
    insp3 = inspect(eng)
    assert expected_p1_7.issubset(set(insp3.get_table_names()))
    eng.dispose()
