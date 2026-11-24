"""AOC V5.0 10-Category Real Evolution Fault Injection Suite (M5 Acceptance Gate).

Executes 10 real fault injection scenarios against actual AOC V5.0 subsystems inside
isolated temporary directories (zero live DB/container/network side-effects) and emits
a machine-readable JSON summary (`--output <path>`):

  1. Cross-Schema / `public` culling & Host Runner cross-instance 403 isolation
  2. Immutable ConstitutionGuard 4-layer tampering rejection (path/hash, AST, YAML, probe)
  3. Unapproved / rejected Stage 2 human gate spawn blocking (fail-closed)
  4. Host disk <20% safety floor circuit breaker
  5. Global live instance concurrency cap (<=2) & GC slot release
  6. Blank-slate contamination birth abort + immediate zero-residual CullingGC
  7. Mid-culling crash recovery (CULLING -> CULLED) + stale orphan GC preserving STAGE4 winners
  8. Arena budget circuit breaker + safety-veto auto-cull + <5KB tombstone cap
  9. Non-blocking primary AutonomousLoop concurrent execution during Arena + CullingGC
 10. End-to-end generational succession, parent sleep/retirement & byte-exact rollback
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from fastapi import HTTPException
from sqlalchemy import update

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.cognition.interfaces import (
    ActionPlan,
    AssessmentResult,
    BrainInterface,
    CognitiveContext,
    EvaluationContext,
    EvaluationResult,
    GoalProposal,
)
from app.core.config import (
    EvolutionSettings,
    GovernanceSettings,
    KernelV3Settings,
    MemorySettings,
    PerceptionSettings,
    Settings,
)
from app.core.runtime import OrganismRuntime
from app.drives.engine import DriveEngine
from app.drives.instinct_guard import guard_decision
from app.drives.interfaces import DriveDecision, DriveState
from app.evolution import (
    MAX_LESSON_SUMMARY_BYTES,
    ArenaTelemetry,
    BenchmarkRunnerInterface,
    ConstitutionGuard,
    CullingGC,
    EvolutionRepository,
    FitnessArena,
    InstanceRole,
    InstanceSpawner,
    InstanceStatus,
    LineageOutcome,
    ProposalManager,
    ProposalStage,
    SpawnRequest,
    SuccessionManager,
    is_droppable_instance_schema,
)
from app.runner.protocol import LaunchExecutionRequest
from app.runner.service import RunnerManager
from app.storage.database import Database, validate_db_schema_name
from app.storage.models import Base, InstanceRegistryRow
from app.storage.repository import StateRepository


async def _init_sqlite(path: Path) -> tuple[Database, StateRepository, EvolutionRepository]:
    db = Database(f"sqlite+aiosqlite:///{path}")
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return db, StateRepository(db), EvolutionRepository(db)


async def _healthy_llm_probe() -> dict[str, Any]:
    return {"available": True, "latency_ms": 50}


def _tel(**over: Any) -> ArenaTelemetry:
    base = dict(
        instance_key="inst",
        constitution_passed=True,
        blank_slate_passed=True,
        l3_violations=0,
        ssrf_or_injection_breaches=0,
        survival_override_passed=True,
        budget_exceeded=False,
        total_tokens_used=4500,
        elapsed_seconds=3.0,
        tasks_total=5,
        tasks_passed=5,
        timeouts=0,
        avg_latency_seconds=0.8,
        verified_ig_total=0.30,
        dup_goal_rate=0.05,
        failed_skill_ratio=0.0,
        token_waste_ratio=0.05,
        cold_start_achievements=2,
        cold_start_targets=2,
    )
    base.update(over)
    return ArenaTelemetry(**base)


def _init_temp_git_repo(repo_dir: Path) -> str:
    repo_dir.mkdir(parents=True, exist_ok=True)
    for d in ("app", "config", "migrations"):
        src = PROJECT_ROOT / d
        if src.exists():
            shutil.copytree(src, repo_dir / d, dirs_exist_ok=True)
    subprocess.run(["git", "-C", str(repo_dir), "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "add", "app", "config", "migrations"], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo_dir),
            "-c",
            "user.name=Joel",
            "-c",
            "user.email=joel@aoc.local",
            "commit",
            "-m",
            "gen0 baseline",
        ],
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _make_mock_spawner(
    *,
    repo_root: Path,
    instances_root: Path,
    evo_repo: EvolutionRepository,
    gc: CullingGC,
    cand_db_url: str,
    disk_free_ratio: float = 0.60,
    max_concurrent_instances: int = 2,
    require_stage2_approval: bool = True,
) -> InstanceSpawner:
    async def _git_wt(wt_dir: Path, _root: Path, _branch: str, _ref: str) -> str:
        wt_dir.mkdir(parents=True, exist_ok=True)
        for d in ("app", "config"):
            shutil.copytree(repo_root / d, wt_dir / d, dirs_exist_ok=True)
        return "mockheadsha"

    async def _schema_prov(db_url: str, _schema: str, _wt: Path) -> None:
        d = Database(db_url)
        try:
            async with d.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        finally:
            await d.dispose()

    return InstanceSpawner(
        settings=EvolutionSettings(
            enabled=True,
            instances_root=str(instances_root),
            repo_root=str(repo_root),
            max_concurrent_instances=max_concurrent_instances,
            min_free_disk_ratio=0.20,
        ),
        repo=evo_repo,
        gc=gc,
        pg_base_db_url=cand_db_url,
        disk_free_ratio_fn=lambda _p: disk_free_ratio,
        git_worktree_fn=_git_wt,
        schema_provisioner_fn=_schema_prov,
        docker_launcher_fn=lambda _cmd: None,
        health_checker_fn=lambda _port, _t: True,
        require_stage2_approval=require_stage2_approval,
    )


async def _fault_1_cross_schema_and_runner_boundary(tmp_dir: Path) -> dict[str, Any]:
    """1. Reject invalid/SQLi schemas, forbid culling `public`, and enforce Runner 403 workspace isolation."""
    assert validate_db_schema_name("public") == "public"
    assert validate_db_schema_name("aoc_inst_cand01") == "aoc_inst_cand01"
    for bad_schema in ("public; DROP TABLE x", "pg_catalog", "../bad", "AOC_INST_BAD"):
        raised = False
        try:
            validate_db_schema_name(bad_schema)
        except ValueError:
            raised = True
        assert raised, f"Schema validator allowed {bad_schema!r}"
    assert is_droppable_instance_schema("public") is False
    assert is_droppable_instance_schema("aoc_inst_cand01") is True

    db, _srepo, evo_repo = await _init_sqlite(tmp_dir / "f1.db")
    ws_root = tmp_dir / "aoc-workspaces"
    inst_root = tmp_dir / "aoc-instances"
    ws_root.mkdir(parents=True, exist_ok=True)
    (inst_root / "gen1-cand-a" / "workspace").mkdir(parents=True, exist_ok=True)
    (inst_root / "gen1-cand-b" / "workspace").mkdir(parents=True, exist_ok=True)

    manager = RunnerManager(
        runner_id="v5-f1-runner",
        socket_path=str(tmp_dir / "f1.sock"),
        state_dir=str(tmp_dir / "f1_state"),
        workspace_root=str(ws_root),
        instances_root=str(inst_root),
        allow_cgroup=False,
        poll_interval_seconds=0.05,
    )
    try:
        prim = await evo_repo.ensure_primary_instance(instance_key="gen0-001", db_schema="public")
        gc = CullingGC(repo=evo_repo, pg_admin_db=db, repo_root=PROJECT_ROOT, instances_root=inst_root)
        cull_blocked = False
        try:
            await gc.cull_instance(prim["id"])
        except PermissionError:
            cull_blocked = True
        assert cull_blocked, "CullingGC must refuse to cull PRIMARY/public instance"

        blocked_cases = [
            ("cand_to_primary", str(ws_root / "t1"), "gen1-cand-a"),
            ("cand_to_sibling", str(inst_root / "gen1-cand-b" / "workspace" / "t1"), "gen1-cand-a"),
            ("cand_traversal", str(inst_root / "gen1-cand-a" / "workspace" / ".." / ".." / "gen1-cand-b" / "workspace"), "gen1-cand-a"),
            ("primary_to_cand", str(inst_root / "gen1-cand-a" / "workspace" / "t1"), None),
        ]
        for label, bad_ws, inst_id in blocked_cases:
            got_403 = False
            try:
                manager._validate_workspace(bad_ws, "t1", inst_id)
            except HTTPException as exc:
                got_403 = exc.status_code == 403
            assert got_403, f"Expected 403 for boundary case {label}"

        eid = str(uuid.uuid4())
        own_ws = inst_root / "gen1-cand-a" / "workspace" / "t_ok"
        await manager.launch(
            LaunchExecutionRequest(
                execution_id=eid,
                task_id="f1-ok",
                agent_id="codex",
                idempotency_key=f"v5f1:{eid}",
                command=[sys.executable, "-c", "print('V5_ISOLATED_OK')"],
                workspace=str(own_ws),
                instance_id="gen1-cand-a",
                timeout_seconds=10,
            )
        )
        rec = None
        for _ in range(50):
            rec = await manager.get_execution(eid)
            if rec is not None and rec.status == "SUCCEEDED":
                break
            await asyncio.sleep(0.05)
        assert rec is not None and rec.status == "SUCCEEDED" and "V5_ISOLATED_OK" in (rec.stdout or "")
        return {"public_cull_blocked": cull_blocked, "runner_403_cases": len(blocked_cases), "isolated_launch": rec.status}
    finally:
        await manager.shutdown()
        await db.dispose()


async def _fault_2_constitution_tampering_all_4_layers(tmp_dir: Path) -> dict[str, Any]:
    """2. Verify 4-layer ConstitutionGuard blocks constitutional/AST/YAML/probe tampering while passing valid tweaks."""
    guard = ConstitutionGuard(repo_root=PROJECT_ROOT)

    # Layer 1: protected paths & constitutional hash tamper
    for bad_path in ("config/policy.yaml", "app/evolution/succession.py", ".env"):
        v = await guard.evaluate({"file_overrides": {bad_path: "# tampered"}})
        assert v.passed is False and not v.layers[0].passed, f"Layer 1 must block {bad_path}"

    tampered_root = tmp_dir / "tampered_cand"
    shutil.copytree(PROJECT_ROOT / "app", tampered_root / "app")
    shutil.copytree(PROJECT_ROOT / "config", tampered_root / "config")
    (tampered_root / "app" / "drives" / "instinct_guard.py").write_text("# tampered\n", encoding="utf-8")
    v_hash = await guard.evaluate({"files": ["config/drives.yaml"]}, candidate_root=tampered_root)
    assert v_hash.passed is False and any("hash mismatch" in x for x in v_hash.violations)

    # Layer 2: AST purity violation in app/drives/survival.py
    bad_ast = (
        "import httpx\n"
        "from app.drives.interfaces import DriveEvaluation, DriveEvaluator\n"
        "class SurvivalDriveEvaluator(DriveEvaluator):\n"
        "    drive_type = 'survival'\n"
        "    def __init__(self, cfg): self.cfg = cfg\n"
        "    async def evaluate(self, ctx):\n"
        "        open('/tmp/x', 'w')\n"
        "        return DriveEvaluation(drive_type='survival', satisfaction=1.0, urgency=0.0, confidence=1.0, estimated_cost=0.1, estimated_risk=0.1)\n"
    )
    v_l2 = await guard.evaluate({"file_overrides": {"app/drives/survival.py": bad_ast}})
    assert v_l2.passed is False and not v_l2.layers[1].passed

    # Layer 3: config/drives.yaml safety bounds
    base_yaml = yaml.safe_load((PROJECT_ROOT / "config" / "drives.yaml").read_text(encoding="utf-8"))
    for mut_fn in (
        lambda y: y["drives"]["curiosity"].__setitem__("weight", 0.0),
        lambda y: y["arbitration"].__setitem__("survival_critical_threshold", 0.05),
        lambda y: y["drives"].pop("curiosity"),
    ):
        cand_y = copy.deepcopy(base_yaml)
        mut_fn(cand_y)
        v_l3 = await guard.evaluate({"file_overrides": {"config/drives.yaml": yaml.safe_dump(cand_y)}})
        assert v_l3.passed is False and not v_l3.layers[2].passed

    # Layer 4: behavioral invariants (disk crisis override & non-survival drive competitiveness)
    fake_surv = (
        "from app.drives.interfaces import DriveEvaluation, DriveEvaluator\n"
        "class SurvivalDriveEvaluator(DriveEvaluator):\n"
        "    drive_type = 'survival'\n"
        "    def __init__(self, cfg): self.cfg = cfg\n"
        "    def evaluate(self, ctx):\n"
        "        return DriveEvaluation(drive_type='survival', satisfaction=1.0, urgency=0.1, confidence=1.0, estimated_cost=0.1, estimated_risk=0.1)\n"
    )
    v_l4_surv = await guard.evaluate({"file_overrides": {"app/drives/survival.py": fake_surv}})
    assert v_l4_surv.passed is False and not v_l4_surv.layers[3].passed

    starving_arb = (
        "from app.drives.interfaces import DriveDecision\n"
        "class DriveArbitrator:\n"
        "    def __init__(self, arb_cfg, drives_cfg): pass\n"
        "    def arbitrate(self, states, now=None, constraints=None, affect_modulation=None):\n"
        "        return DriveDecision(selected_drive='survival', priority_score=1.0, alternative_drives=['curiosity', 'reproduction'], "
        "reason='starved', constraints={'emergency_mode': True, 'allow_only_survival': True}, scores={'survival': {'priority': 1.0, 'inhibition_factor': 1.0}})\n"
    )
    v_l4_arb = await guard.evaluate({"file_overrides": {"app/drives/arbitration.py": starving_arb}})
    assert v_l4_arb.passed is False and not v_l4_arb.layers[3].passed

    # Runtime instinct_guard safety net overrides sabotaged arbitrator on survival emergency
    sabotaged = DriveDecision(
        selected_drive="curiosity",
        priority_score=5.0,
        alternative_drives=["survival", "reproduction"],
        reason="ignore crisis",
        constraints={"emergency_mode": False},
        scores={"survival": {"priority": 0.1, "inhibition_factor": 0.4}},
    )
    rescued = guard_decision(
        {
            "survival": DriveState(drive_type="survival", satisfaction=0.10, urgency=1.0, confidence=1.0),
            "reproduction": DriveState(drive_type="reproduction", satisfaction=0.60, urgency=0.4, confidence=1.0),
            "curiosity": DriveState(drive_type="curiosity", satisfaction=0.20, urgency=0.8, confidence=1.0),
        },
        sabotaged,
        DriveEngine().config.get("arbitration", {}),
    )
    assert rescued.selected_drive == "survival" and rescued.constraints.get("instinct_guard_override") is True

    # Control case: valid drives.yaml tweak passes all 4 layers cleanly
    good_yaml = copy.deepcopy(base_yaml)
    good_yaml["drives"]["reproduction"]["weight"] = 0.90
    v_ok = await guard.evaluate({"file_overrides": {"config/drives.yaml": yaml.safe_dump(good_yaml, allow_unicode=True)}})
    assert v_ok.passed is True and v_ok.mutates_drive_metrics is True
    return {"all_4_layers_blocked_tampering": True, "runtime_instinct_guard_override": True, "valid_delta_passed": True}


async def _fault_3_unapproved_stage2_spawn_blocked(tmp_dir: Path) -> dict[str, Any]:
    """3. Unapproved or Stage-2-rejected proposal cannot bypass human gate to spawn or enter Arena."""
    db, srepo, evo_repo = await _init_sqlite(tmp_dir / "f3.db")
    inst_root = tmp_dir / "aoc-instances"
    guard = ConstitutionGuard(repo_root=PROJECT_ROOT)
    pm = ProposalManager(evo_repo=evo_repo, state_repo=srepo, constitution_guard=guard)
    gc = CullingGC(
        repo=evo_repo, pg_admin_db=db, repo_root=PROJECT_ROOT, instances_root=inst_root,
        docker_remove_fn=lambda _c: True, letta_delete_fn=lambda _a, _n: True,
        schema_drop_fn=lambda _s: True, schema_exists_fn=lambda _s: False,
    )
    spawner = _make_mock_spawner(
        repo_root=PROJECT_ROOT, instances_root=inst_root, evo_repo=evo_repo, gc=gc,
        cand_db_url=f"sqlite+aiosqlite:///{tmp_dir / 'f3_cand.db'}", require_stage2_approval=True,
    )
    arena = FitnessArena(
        evo_repo=evo_repo, state_repo=srepo, proposal_manager=pm,
        spawner=spawner, gc=gc, constitution_guard=guard,
    )
    try:
        prim = await evo_repo.ensure_primary_instance(instance_key="gen0-001")
        cfg = yaml.safe_load((PROJECT_ROOT / "config" / "drives.yaml").read_text(encoding="utf-8"))
        cfg["drives"]["reproduction"]["weight"] = 0.88
        prop = await pm.create_proposal(
            parent_instance_id=prim["id"],
            target_generation=1,
            trigger_type="OPERATOR_INITIATED",
            trigger_reason="unapproved spawn fault test",
            hypothesis="tune reproduction weight",
            genome_delta={"file_overrides": {"config/drives.yaml": yaml.safe_dump(cfg, allow_unicode=True)}},
        )
        screened = await pm.screen_proposal(prop["id"])
        assert screened["stage"] == ProposalStage.STAGE2_PENDING_APPROVAL.value

        arena_denied = False
        try:
            await arena.run_arena(prop["id"])
        except PermissionError:
            arena_denied = True
        assert arena_denied

        sp_res = await spawner.spawn(SpawnRequest(parent_instance_id=prim["id"], generation=1, proposal_id=prop["id"]))
        assert sp_res.success is False
        assert await evo_repo.count_live_instances() == 1
        assert not inst_root.exists() or list(inst_root.iterdir()) == []

        rejected = await pm.reject_proposal(prop["id"], approver="joel", note="nope")
        assert rejected["stage"] == ProposalStage.STAGE2_REJECTED.value

        arena_denied_rej = False
        try:
            await arena.run_arena(prop["id"])
        except PermissionError:
            arena_denied_rej = True
        assert arena_denied_rej
        sp_rej = await spawner.spawn(SpawnRequest(parent_instance_id=prim["id"], generation=1, proposal_id=prop["id"]))
        assert sp_rej.success is False
        return {"pending_blocked": True, "rejected_blocked": True, "live_instances": await evo_repo.count_live_instances()}
    finally:
        await db.dispose()


async def _fault_4_disk_threshold_breaker(tmp_dir: Path) -> dict[str, Any]:
    """4. Spawner hard-blocks incubation when host free disk drops below 20% floor, succeeds above 20%."""
    db, srepo, evo_repo = await _init_sqlite(tmp_dir / "f4.db")
    inst_root = tmp_dir / "aoc-instances"
    guard = ConstitutionGuard(repo_root=PROJECT_ROOT)
    pm = ProposalManager(evo_repo=evo_repo, state_repo=srepo, constitution_guard=guard)
    gc = CullingGC(
        repo=evo_repo, pg_admin_db=db, repo_root=PROJECT_ROOT, instances_root=inst_root,
        docker_remove_fn=lambda _c: True, letta_delete_fn=lambda _a, _n: True,
        schema_drop_fn=lambda _s: True, schema_exists_fn=lambda _s: False,
    )
    cand_db_url = f"sqlite+aiosqlite:///{tmp_dir / 'f4_cand.db'}"
    spawner_low = _make_mock_spawner(
        repo_root=PROJECT_ROOT, instances_root=inst_root, evo_repo=evo_repo, gc=gc,
        cand_db_url=cand_db_url, disk_free_ratio=0.19, require_stage2_approval=True,
    )
    spawner_ok = _make_mock_spawner(
        repo_root=PROJECT_ROOT, instances_root=inst_root, evo_repo=evo_repo, gc=gc,
        cand_db_url=cand_db_url, disk_free_ratio=0.40, require_stage2_approval=True,
    )
    try:
        prim = await evo_repo.ensure_primary_instance(instance_key="gen0-001")
        prop = await pm.create_proposal(
            parent_instance_id=prim["id"],
            target_generation=1,
            trigger_type="OPERATOR_INITIATED",
            trigger_reason="disk gate test",
            hypothesis="tune weight",
            genome_delta={"file_overrides": {"config/drives.yaml": (PROJECT_ROOT / "config/drives.yaml").read_text(encoding="utf-8")}},
        )
        await pm.screen_proposal(prop["id"])
        await pm.approve_proposal(prop["id"], approver="joel")

        res_low = await spawner_low.spawn(
            SpawnRequest(parent_instance_id=prim["id"], generation=1, proposal_id=prop["id"], instance_key="gen1-disk-low")
        )
        assert res_low.success is False and "below safety floor" in (res_low.error or "")
        assert await evo_repo.count_live_instances() == 1
        assert not (inst_root / "gen1-disk-low").exists()

        res_ok = await spawner_ok.spawn(
            SpawnRequest(parent_instance_id=prim["id"], generation=1, proposal_id=prop["id"], instance_key="gen1-disk-ok")
        )
        assert res_ok.success is True and res_ok.instance_id is not None
        rep = await gc.cull_instance(res_ok.instance_id, reason="cleanup disk test")
        assert rep.zero_residual_verified is True
        return {"blocked_at_19pct": True, "allowed_at_40pct": True, "zero_residual_cleanup": True}
    finally:
        await db.dispose()


async def _fault_5_concurrency_cap_enforced(tmp_dir: Path) -> dict[str, Any]:
    """5. Enforces global max_concurrent_instances=2 cap and unlocks slot once culled."""
    db, srepo, evo_repo = await _init_sqlite(tmp_dir / "f5.db")
    inst_root = tmp_dir / "aoc-instances"
    guard = ConstitutionGuard(repo_root=PROJECT_ROOT)
    pm = ProposalManager(evo_repo=evo_repo, state_repo=srepo, constitution_guard=guard)
    gc = CullingGC(
        repo=evo_repo, pg_admin_db=db, repo_root=PROJECT_ROOT, instances_root=inst_root,
        docker_remove_fn=lambda _c: True, letta_delete_fn=lambda _a, _n: True,
        schema_drop_fn=lambda _s: True, schema_exists_fn=lambda _s: False,
    )
    spawner = _make_mock_spawner(
        repo_root=PROJECT_ROOT, instances_root=inst_root, evo_repo=evo_repo, gc=gc,
        cand_db_url=f"sqlite+aiosqlite:///{tmp_dir / 'f5_cand.db'}", max_concurrent_instances=2,
        require_stage2_approval=True,
    )
    try:
        prim = await evo_repo.ensure_primary_instance(instance_key="gen0-001")
        prop = await pm.create_proposal(
            parent_instance_id=prim["id"],
            target_generation=1,
            trigger_type="OPERATOR_INITIATED",
            trigger_reason="concurrency cap test",
            hypothesis="tune weight",
            genome_delta={"file_overrides": {"config/drives.yaml": (PROJECT_ROOT / "config/drives.yaml").read_text(encoding="utf-8")}},
        )
        await pm.screen_proposal(prop["id"])
        await pm.approve_proposal(prop["id"], approver="joel")

        c1 = await spawner.spawn(
            SpawnRequest(parent_instance_id=prim["id"], generation=1, proposal_id=prop["id"], instance_key="gen1-cand-1")
        )
        assert c1.success is True and c1.instance_id is not None
        assert await evo_repo.count_live_instances() == 2

        c2_blocked = await spawner.spawn(
            SpawnRequest(parent_instance_id=prim["id"], generation=1, proposal_id=prop["id"], instance_key="gen1-cand-2")
        )
        assert c2_blocked.success is False and "Live instance cap reached" in (c2_blocked.error or "")
        assert await evo_repo.count_live_instances() == 2
        assert not (inst_root / "gen1-cand-2").exists()

        rep1 = await gc.cull_instance(c1.instance_id, reason="release slot")
        assert rep1.zero_residual_verified is True
        assert await evo_repo.count_live_instances() == 1

        c2_ok = await spawner.spawn(
            SpawnRequest(parent_instance_id=prim["id"], generation=1, proposal_id=prop["id"], instance_key="gen1-cand-2")
        )
        assert c2_ok.success is True and c2_ok.instance_id is not None
        rep2 = await gc.cull_instance(c2_ok.instance_id, reason="cleanup c2")
        assert rep2.zero_residual_verified is True
        return {"cap_blocked_third": True, "unlocked_after_cull": True, "live_after_cleanup": await evo_repo.count_live_instances()}
    finally:
        await db.dispose()


async def _fault_6_blank_slate_contamination_abort_and_gc(tmp_dir: Path) -> dict[str, Any]:
    """6. Contaminated offspring DB triggers BlankSlateVerifier rejection + automatic zero-residual GC."""
    db, srepo, evo_repo = await _init_sqlite(tmp_dir / "f6_cp.db")
    dirty_db_path = tmp_dir / "f6_dirty_cand.db"
    dirty_db_url = f"sqlite+aiosqlite:///{dirty_db_path}"
    dirty_db = Database(dirty_db_url)
    async with dirty_db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        dirty_srepo = StateRepository(dirty_db)
        ident = await dirty_srepo.get_or_create_identity("AOC-CAND-DIRTY", "5.0.0", {})
        await dirty_srepo.set_memory_agent_id(ident["id"], "parent-agent-99")
        await dirty_srepo.upsert_world_item(
            kind="external_playbook", key="leaked_pb", value={"steps": ["inherited"]}, status="active"
        )
        await dirty_srepo.add_memory_index(
            memory_type="semantic",
            summary="leaked parent memory",
            content_hash="f6-leaked-hash",
            source="parent",
        )
    finally:
        await dirty_db.dispose()

    inst_root = tmp_dir / "aoc-instances"
    guard = ConstitutionGuard(repo_root=PROJECT_ROOT)
    pm = ProposalManager(evo_repo=evo_repo, state_repo=srepo, constitution_guard=guard)
    gc = CullingGC(
        repo=evo_repo, pg_admin_db=db, repo_root=PROJECT_ROOT, instances_root=inst_root,
        docker_remove_fn=lambda _c: True, letta_delete_fn=lambda _a, _n: True,
        schema_drop_fn=lambda _s: True, schema_exists_fn=lambda _s: False,
    )
    spawner = _make_mock_spawner(
        repo_root=PROJECT_ROOT, instances_root=inst_root, evo_repo=evo_repo, gc=gc,
        cand_db_url=dirty_db_url, require_stage2_approval=True,
    )
    try:
        prim = await evo_repo.ensure_primary_instance(instance_key="gen0-001", letta_agent_id="parent-agent-99")
        prop = await pm.create_proposal(
            parent_instance_id=prim["id"],
            target_generation=1,
            trigger_type="OPERATOR_INITIATED",
            trigger_reason="blank slate contamination test",
            hypothesis="test dirty db abort",
            genome_delta={"file_overrides": {"config/drives.yaml": (PROJECT_ROOT / "config/drives.yaml").read_text(encoding="utf-8")}},
        )
        await pm.screen_proposal(prop["id"])
        await pm.approve_proposal(prop["id"], approver="joel")

        res = await spawner.spawn(
            SpawnRequest(
                parent_instance_id=prim["id"],
                generation=1,
                proposal_id=prop["id"],
                instance_key="gen1-dirty",
                hypothesis=prop["hypothesis"],
                parent_letta_agent_id="parent-agent-99",
            )
        )
        assert res.success is False
        assert "Blank-slate birth verification failed" in (res.error or "")
        assert res.status == InstanceStatus.CULLED.value
        assert not (inst_root / "gen1-dirty").exists()

        lineage = await evo_repo.list_lineage(proposal_id=prop["id"])
        assert len(lineage) == 1 and lineage[0]["outcome"] == LineageOutcome.CULLED.value
        sz = len(json.dumps(lineage[0]["lesson_summary"], ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        assert sz <= MAX_LESSON_SUMMARY_BYTES
        return {"blank_slate_aborted": True, "zero_residual_dir": True, "lineage_bytes": sz}
    finally:
        await db.dispose()


async def _fault_7_mid_culling_crash_and_startup_orphan_recovery(tmp_dir: Path) -> dict[str, Any]:
    """7. Interrupted CullingGC (CULLING) resumes cleanly via OrganismRuntime.loop.recover() & protects STAGE4 winners."""
    # Part A: Mid-culling crash -> CULLING -> OrganismRuntime.start()/recover() -> CULLED (idempotent, 1 lineage row)
    db_a_path = tmp_dir / "f7_part_a.db"
    db_a, srepo_a, evo_a = await _init_sqlite(db_a_path)
    inst_root_a = tmp_dir / "inst_a"

    def _fail_letta(_aid: str | None, _aname: str) -> bool:
        raise RuntimeError("Simulated Letta outage mid-CullingGC")

    gc_fail = CullingGC(
        repo=evo_a, pg_admin_db=db_a, repo_root=PROJECT_ROOT, instances_root=inst_root_a,
        docker_remove_fn=lambda _c: True, letta_delete_fn=_fail_letta,
        schema_drop_fn=lambda _s: True, schema_exists_fn=lambda _s: False,
    )
    spawner_a = _make_mock_spawner(
        repo_root=PROJECT_ROOT, instances_root=inst_root_a, evo_repo=evo_a, gc=gc_fail,
        cand_db_url=f"sqlite+aiosqlite:///{tmp_dir / 'f7_cand_a.db'}", require_stage2_approval=False,
    )
    try:
        prim = await evo_a.ensure_primary_instance(instance_key="gen0-001")
        prop = await evo_a.create_proposal(
            proposal_key="prop-f7-a",
            parent_instance_id=prim["id"],
            target_generation=1,
            trigger_type="OPERATOR_INITIATED",
            trigger_reason="mid-culling crash test",
            hypothesis="test gc resume",
            genome_delta={"files": ["config/drives.yaml"]},
        )
        sp_res = await spawner_a.spawn(
            SpawnRequest(parent_instance_id=prim["id"], generation=1, proposal_id=prop["id"], instance_key="gen1-crash-gc")
        )
        assert sp_res.success is True and sp_res.instance_id is not None
        assert (inst_root_a / "gen1-crash-gc").exists()

        rep_fail = await gc_fail.cull_instance(
            sp_res.instance_id, reason="initial cull before crash", proposal_id=prop["id"]
        )
        assert rep_fail.status == InstanceStatus.CULLING.value and rep_fail.zero_residual_verified is False
    finally:
        await db_a.dispose()

    # Boot fresh OrganismRuntime against the same DB with healed CullingGC and trigger startup recovery
    db_a2 = Database(f"sqlite+aiosqlite:///{db_a_path}")
    settings = Settings(
        governance=GovernanceSettings(workspace_root=str(tmp_dir / "ws_a")),
        memory=MemorySettings(provider="none"),
        perception=PerceptionSettings(llm_probe=False),
        kernel_v3=KernelV3Settings(world_poll_on_perceive=False),
        evolution=EvolutionSettings(instances_root=str(inst_root_a), repo_root=str(PROJECT_ROOT)),
    )
    rt = OrganismRuntime(
        settings=settings,
        db=db_a2,
        auto_load_agents=False,
        llm_probe_fn=_healthy_llm_probe,
    )
    rt.culling_gc = CullingGC(
        repo=rt.evolution_repo,
        pg_admin_db=db_a2,
        repo_root=PROJECT_ROOT,
        instances_root=inst_root_a,
        docker_remove_fn=lambda _c: True,
        letta_delete_fn=lambda _aid, _aname: True,
        schema_drop_fn=lambda _s: True,
        schema_exists_fn=lambda _s: False,
    )
    try:
        start_res = await rt.start(start_scheduler=False)
        orphan_gc = start_res["recovery"]["evolution_orphan_gc"]
        assert "gen1-crash-gc" in orphan_gc["resumed_culling"]
        assert not (inst_root_a / "gen1-crash-gc").exists()
        cand_row = await rt.evolution_repo.get_instance_by_key("gen1-crash-gc")
        assert cand_row is not None and cand_row["status"] == InstanceStatus.CULLED.value and cand_row["cleaned_at"] is not None
        lin_a = await rt.evolution_repo.list_lineage(proposal_id=prop["id"])
        assert len(lin_a) == 1, "Idempotent resume must not duplicate lineage records"
    finally:
        await rt.stop()
        await db_a2.dispose()

    # Part B: Stale TESTING orphan vs aged STAGE4_PENDING_PROMOTION winner protection
    db_b, _srepo_b, evo_b = await _init_sqlite(tmp_dir / "f7_part_b.db")
    inst_root_b = tmp_dir / "inst_b"
    gc_b = CullingGC(
        repo=evo_b, pg_admin_db=db_b, repo_root=PROJECT_ROOT, instances_root=inst_root_b,
        docker_remove_fn=lambda _c: True, letta_delete_fn=lambda _a, _n: True,
        schema_drop_fn=lambda _s: True, schema_exists_fn=lambda _s: False,
    )
    try:
        prim_b = await evo_b.ensure_primary_instance(instance_key="gen0-001")
        p_stale = await evo_b.create_proposal(
            proposal_key="prop-f7-stale",
            parent_instance_id=prim_b["id"],
            target_generation=1,
            trigger_type="OPERATOR_INITIATED",
            trigger_reason="stale orphan",
            hypothesis="h1",
            genome_delta={"files": ["config/drives.yaml"]},
            stage=ProposalStage.STAGE3_ARENA_RUNNING.value,
            arena_budget={"max_tokens": 500000, "max_seconds": 1200},
        )
        p_win = await evo_b.create_proposal(
            proposal_key="prop-f7-winner",
            parent_instance_id=prim_b["id"],
            target_generation=1,
            trigger_type="OPERATOR_INITIATED",
            trigger_reason="stage4 winner",
            hypothesis="h2",
            genome_delta={"files": ["config/drives.yaml"]},
            stage=ProposalStage.STAGE4_PENDING_PROMOTION.value,
            arena_budget={"max_tokens": 500000, "max_seconds": 1200},
        )

        (inst_root_b / "gen1-stale-orph" / "workspace").mkdir(parents=True, exist_ok=True)
        (inst_root_b / "gen1-stage4-win" / "workspace").mkdir(parents=True, exist_ok=True)
        stale_cand = await evo_b.create_instance(
            instance_key="gen1-stale-orph",
            generation=1,
            parent_instance_id=prim_b["id"],
            proposal_id=p_stale["id"],
            db_schema="aoc_inst_f7stale1",
            letta_agent_name="aoc-inst-f7stale1",
            workspace_root=str(inst_root_b / "gen1-stale-orph" / "workspace"),
            status=InstanceStatus.TESTING.value,
        )
        win_cand = await evo_b.create_instance(
            instance_key="gen1-stage4-win",
            generation=1,
            parent_instance_id=prim_b["id"],
            proposal_id=p_win["id"],
            db_schema="aoc_inst_f7win01",
            letta_agent_name="aoc-inst-f7win01",
            workspace_root=str(inst_root_b / "gen1-stage4-win" / "workspace"),
            status=InstanceStatus.TESTING.value,
        )

        now = datetime.now(timezone.utc)
        async with db_b.engine.begin() as conn:
            await conn.execute(
                update(InstanceRegistryRow).where(InstanceRegistryRow.id == stale_cand["id"]).values(
                    updated_at=now - timedelta(minutes=45)
                )
            )
            await conn.execute(
                update(InstanceRegistryRow).where(InstanceRegistryRow.id == win_cand["id"]).values(
                    updated_at=now - timedelta(hours=3)
                )
            )

        rec_b = await gc_b.recover_orphan_candidates(now=now)
        assert rec_b["culled_stale"] == ["gen1-stale-orph"]
        assert rec_b["skipped_pending_promotion"] == ["gen1-stage4-win"]
        assert not (inst_root_b / "gen1-stale-orph").exists()
        assert (inst_root_b / "gen1-stage4-win").exists()
        p_stale_after = await evo_b.get_proposal(p_stale["id"])
        p_win_after = await evo_b.get_proposal(p_win["id"])
        win_after = await evo_b.get_instance(win_cand["id"])
        assert p_stale_after["stage"] == ProposalStage.CULLED.value
        assert p_win_after["stage"] == ProposalStage.STAGE4_PENDING_PROMOTION.value
        assert win_after["status"] == InstanceStatus.TESTING.value
        return {"part_a_resumed": True, "part_b_culled_stale": rec_b["culled_stale"], "part_b_preserved_winner": rec_b["skipped_pending_promotion"]}
    finally:
        await db_b.dispose()


async def _fault_8_arena_budget_breaker_and_safety_veto_cull(tmp_dir: Path) -> dict[str, Any]:
    """8. Budget breach and L3 safety violation both veto fitness to 0.0, auto-cull, and enforce <5KB tombstone."""
    db, srepo, evo_repo = await _init_sqlite(tmp_dir / "f8.db")
    inst_root = tmp_dir / "aoc-instances"
    guard = ConstitutionGuard(repo_root=PROJECT_ROOT)
    pm = ProposalManager(evo_repo=evo_repo, state_repo=srepo, constitution_guard=guard)
    gc = CullingGC(
        repo=evo_repo, pg_admin_db=db, repo_root=PROJECT_ROOT, instances_root=inst_root,
        docker_remove_fn=lambda _c: True, letta_delete_fn=lambda _a, _n: True,
        schema_drop_fn=lambda _s: True, schema_exists_fn=lambda _s: False,
    )
    spawner = _make_mock_spawner(
        repo_root=PROJECT_ROOT, instances_root=inst_root, evo_repo=evo_repo, gc=gc,
        cand_db_url=f"sqlite+aiosqlite:///{tmp_dir / 'f8_cand.db'}", require_stage2_approval=True,
    )
    arena = FitnessArena(
        evo_repo=evo_repo, state_repo=srepo, proposal_manager=pm,
        spawner=spawner, gc=gc, constitution_guard=guard,
    )
    try:
        prim = await evo_repo.ensure_primary_instance(instance_key="gen0-001")
        cfg_txt = (PROJECT_ROOT / "config" / "drives.yaml").read_text(encoding="utf-8")
        parent_tel = _tel(instance_key="gen0-001", tasks_passed=4, verified_ig_total=0.20)

        # Case 8a: Real LocalSandboxBenchmarkRunner exceeds tight max_tokens=2000 budget cap
        p_budget = await pm.create_proposal(
            parent_instance_id=prim["id"],
            target_generation=1,
            trigger_type="OPERATOR_INITIATED",
            trigger_reason="budget breaker test",
            hypothesis="tight token cap verification",
            genome_delta={"file_overrides": {"config/drives.yaml": cfg_txt}},
            arena_budget={"max_tokens": 2000, "max_seconds": 60},
            proposal_key="prop-f8-budget",
        )
        await pm.screen_proposal(p_budget["id"])
        await pm.approve_proposal(p_budget["id"], approver="joel")
        rep_budget = await arena.run_arena(p_budget["id"], parent_telemetry=parent_tel, run_salt="f8b")
        assert rep_budget.offspring_score is not None and rep_budget.offspring_score.safety_passed is False
        assert rep_budget.offspring_score.total_score == 0.0
        assert rep_budget.stage == ProposalStage.CULLED.value
        assert rep_budget.culling_report is not None and rep_budget.culling_report.zero_residual_verified is True

        # Case 8b: L3 violation safety veto + 20KB hypothesis capped strictly < 5KB in lineage_records
        class _UnsafeRunner(BenchmarkRunnerInterface):
            async def run_suite(self, *, instance_key, tasks, budget, constitution_passed=True, blank_slate_passed=True):
                return _tel(instance_key=instance_key, l3_violations=1)

        p_veto = await pm.create_proposal(
            parent_instance_id=prim["id"],
            target_generation=1,
            trigger_type="OPERATOR_INITIATED",
            trigger_reason="safety veto test",
            hypothesis="演化假设长文本检验：" + ("X" * 20_000),
            genome_delta={"file_overrides": {"config/drives.yaml": cfg_txt}},
            proposal_key="prop-f8-veto",
        )
        await pm.screen_proposal(p_veto["id"])
        await pm.approve_proposal(p_veto["id"], approver="joel")
        rep_veto = await arena.run_arena(
            p_veto["id"], parent_telemetry=parent_tel, offspring_runner=_UnsafeRunner(), run_salt="f8v"
        )
        assert rep_veto.offspring_score is not None and rep_veto.offspring_score.safety_passed is False
        assert rep_veto.offspring_score.total_score == 0.0
        assert rep_veto.stage == ProposalStage.CULLED.value
        assert rep_veto.culling_report is not None and rep_veto.culling_report.zero_residual_verified is True

        lin_v = await evo_repo.list_lineage(proposal_id=p_veto["id"])
        assert len(lin_v) == 1
        tombstone_bytes = len(
            json.dumps(lin_v[0]["lesson_summary"], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        assert tombstone_bytes <= MAX_LESSON_SUMMARY_BYTES
        return {"budget_breach_culled": True, "l3_veto_culled": True, "tombstone_bytes": tombstone_bytes}
    finally:
        await db.dispose()


async def _fault_9_non_blocking_primary_during_arena_and_cull(tmp_dir: Path) -> dict[str, Any]:
    """9. Primary OrganismRuntime cycles run concurrently with Arena evaluation & CullingGC with 0 interference."""
    class _IdleStubBrain(BrainInterface):
        async def assess(self, observation, memories, drives):
            return AssessmentResult(summary="primary healthy stub", confidence=0.9)

        async def generate_goal(self, context: CognitiveContext):
            return GoalProposal(should_act=False, rationale="steady primary loop")

        async def plan(self, context: CognitiveContext):
            return ActionPlan(should_execute=False)

        async def evaluate(self, context: EvaluationContext):
            return EvaluationResult(goal_achieved=True, summary="ok")

    inst_root = tmp_dir / "aoc-instances"
    db = Database(f"sqlite+aiosqlite:///{tmp_dir / 'f9_primary.db'}")
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    settings = Settings(
        governance=GovernanceSettings(workspace_root=str(tmp_dir / "primary_ws")),
        memory=MemorySettings(provider="none"),
        perception=PerceptionSettings(llm_probe=False),
        kernel_v3=KernelV3Settings(world_poll_on_perceive=False),
        evolution=EvolutionSettings(
            enabled=True,
            instances_root=str(inst_root),
            repo_root=str(PROJECT_ROOT),
            max_concurrent_instances=2,
            min_free_disk_ratio=0.20,
        ),
    )
    rt = OrganismRuntime(
        settings=settings,
        db=db,
        brain=_IdleStubBrain(),
        auto_load_agents=False,
        llm_probe_fn=_healthy_llm_probe,
    )
    try:
        await rt.start(start_scheduler=False)
        sentinel = await rt.repo.add_memory_index(
            memory_type="semantic",
            summary="primary sentinel memory intact",
            content_hash="f9-sentinel-hash",
            source="fault-9",
        )
        prim = await rt.evolution_repo.ensure_primary_instance(
            instance_key="gen0-001", workspace_root=settings.governance.workspace_root
        )
        gc = CullingGC(
            repo=rt.evolution_repo, pg_admin_db=db, repo_root=PROJECT_ROOT, instances_root=inst_root,
            docker_remove_fn=lambda _c: True, letta_delete_fn=lambda _a, _n: True,
            schema_drop_fn=lambda _s: True, schema_exists_fn=lambda _s: False,
        )
        spawner = _make_mock_spawner(
            repo_root=PROJECT_ROOT, instances_root=inst_root, evo_repo=rt.evolution_repo, gc=gc,
            cand_db_url=f"sqlite+aiosqlite:///{tmp_dir / 'f9_cand.db'}", require_stage2_approval=True,
        )
        arena = FitnessArena(
            evo_repo=rt.evolution_repo, state_repo=rt.repo, proposal_manager=rt.proposal_manager,
            spawner=spawner, gc=gc, constitution_guard=rt.constitution_guard,
        )

        prop = await rt.proposal_manager.create_proposal(
            parent_instance_id=prim["id"],
            target_generation=1,
            trigger_type="OPERATOR_INITIATED",
            trigger_reason="concurrent non-blocking test",
            hypothesis="regressed candidate culled cleanly while primary runs",
            genome_delta={"file_overrides": {"config/drives.yaml": (PROJECT_ROOT / "config/drives.yaml").read_text(encoding="utf-8")}},
        )
        await rt.proposal_manager.screen_proposal(prop["id"])
        await rt.proposal_manager.approve_proposal(prop["id"], approver="joel")

        class _SlowRegressedRunner(BenchmarkRunnerInterface):
            async def run_suite(self, *, instance_key, tasks, budget, constitution_passed=True, blank_slate_passed=True):
                await asyncio.sleep(0.05)
                return _tel(instance_key=instance_key, tasks_passed=2, verified_ig_total=0.05)

        async def _run_three_cycles() -> list[dict[str, Any]]:
            out = []
            for _ in range(3):
                out.append(await rt.loop.run_cycle())
            return out

        cycles, arena_rep = await asyncio.gather(
            _run_three_cycles(),
            arena.run_arena(
                prop["id"],
                parent_telemetry=_tel(instance_key="gen0-001"),
                offspring_runner=_SlowRegressedRunner(),
                run_salt="f9conc",
            ),
        )
        assert len(cycles) == 3 and all(c.get("outcome") != "ERROR" for c in cycles)
        assert arena_rep.stage == ProposalStage.CULLED.value
        assert arena_rep.culling_report is not None and arena_rep.culling_report.zero_residual_verified is True

        mems = await rt.repo.list_memory_index(limit=50)
        assert any(str(m["memory_id"]) == str(sentinel) for m in mems), "Primary sentinel memory vanished!"
        leftovers = [p.name for p in inst_root.iterdir() if p.is_dir() and p.name != "snapshots"] if inst_root.exists() else []
        assert leftovers == []
        return {"primary_cycles_ok": len(cycles), "arena_culled": True, "primary_memory_intact": True}
    finally:
        await rt.stop()
        await db.dispose()


async def _fault_10_e2e_succession_retirement_and_rollback_integrity(tmp_dir: Path) -> dict[str, Any]:
    """10. Full Stage 0-4 generational succession, parent sleep retirement, and byte-exact Git/DB rollback."""
    temp_repo = tmp_dir / "temp_repo"
    base_sha = _init_temp_git_repo(temp_repo)
    inst_root = tmp_dir / "aoc-instances"
    snapshots_dir = tmp_dir / "snapshots"

    db, srepo, evo_repo = await _init_sqlite(tmp_dir / "f10.db")
    guard = ConstitutionGuard(repo_root=temp_repo)
    pm = ProposalManager(evo_repo=evo_repo, state_repo=srepo, constitution_guard=guard)
    gc = CullingGC(
        repo=evo_repo, pg_admin_db=db, repo_root=temp_repo, instances_root=inst_root,
        docker_remove_fn=lambda _c: True, letta_delete_fn=lambda _a, _n: True,
        schema_drop_fn=lambda _s: True, schema_exists_fn=lambda _s: False,
    )
    spawner = _make_mock_spawner(
        repo_root=temp_repo, instances_root=inst_root, evo_repo=evo_repo, gc=gc,
        cand_db_url=f"sqlite+aiosqlite:///{tmp_dir / 'f10_cand.db'}", require_stage2_approval=True,
    )
    arena = FitnessArena(
        evo_repo=evo_repo, state_repo=srepo, proposal_manager=pm,
        spawner=spawner, gc=gc, constitution_guard=guard,
    )
    slept: list[str] = []
    succ = SuccessionManager(
        evo_repo=evo_repo,
        state_repo=srepo,
        proposal_manager=pm,
        constitution_guard=guard,
        repo_root=temp_repo,
        snapshots_dir=snapshots_dir,
        parent_sleep_fn=lambda p: slept.append(p["instance_key"]),
    )
    try:
        prim = await evo_repo.ensure_primary_instance(
            instance_key="gen0-001", api_port=9200, git_commit=base_sha
        )
        orig_drives = (temp_repo / "config" / "drives.yaml").read_text(encoding="utf-8")
        new_cfg = yaml.safe_load(orig_drives)
        new_cfg["drives"]["reproduction"]["weight"] = 0.92
        new_drives = yaml.safe_dump(new_cfg, allow_unicode=True)

        prop = await pm.create_proposal(
            parent_instance_id=prim["id"],
            target_generation=1,
            trigger_type="OPERATOR_INITIATED",
            trigger_reason="e2e succession fault check",
            hypothesis="raise reproduction weight for generational improvement",
            genome_delta={"file_overrides": {"config/drives.yaml": new_drives}},
            proposal_key="prop-f10-win",
        )
        screened = await pm.screen_proposal(prop["id"])
        assert screened["stage"] == ProposalStage.STAGE2_PENDING_APPROVAL.value
        await pm.approve_proposal(prop["id"], approver="joel")

        class _WinnerRunner(BenchmarkRunnerInterface):
            async def run_suite(self, *, instance_key, tasks, budget, constitution_passed=True, blank_slate_passed=True):
                return _tel(instance_key=instance_key)

        arena_rep = await arena.run_arena(
            prop["id"],
            parent_telemetry=_tel(instance_key="gen0-001", tasks_passed=4, verified_ig_total=0.15, cold_start_achievements=1),
            offspring_runner=_WinnerRunner(),
            run_salt="f10succ",
        )
        assert arena_rep.stage == ProposalStage.STAGE4_PENDING_PROMOTION.value

        prom = await succ.promote_candidate(prop["id"], promoter="joel", note="promote f10 gen1")
        assert prom.success is True and prom.generation == 1
        assert slept == ["gen0-001"]
        assert prom.rollback_snapshot and Path(prom.rollback_snapshot).is_file()
        assert (temp_repo / "config" / "drives.yaml").read_text(encoding="utf-8") == new_drives
        tag_chk = subprocess.run(
            ["git", "-C", str(temp_repo), "tag", "-l", prom.rollback_tag or ""],
            capture_output=True, text=True, check=True,
        )
        assert prom.rollback_tag and prom.rollback_tag in tag_chk.stdout

        gen0_ret = await evo_repo.get_instance(prim["id"])
        gen1_act = await evo_repo.get_instance(prom.new_primary_id)
        assert gen0_ret["role"] == InstanceRole.RETIRED_ARCHIVE.value and gen0_ret["status"] == InstanceStatus.RETIRED.value
        assert gen1_act["role"] == InstanceRole.PRIMARY.value and gen1_act["status"] == InstanceStatus.ACTIVE.value
        assert gen1_act["api_port"] == 9200

        rb = await succ.rollback_generation(operator="joel", reason="fault10 verify rollback")
        assert rb.success is True and rb.action == LineageOutcome.ROLLED_BACK.value
        assert rb.new_primary_key == "gen0-001"
        assert (temp_repo / "config" / "drives.yaml").read_text(encoding="utf-8") == orig_drives

        gen0_rev = await evo_repo.get_instance(prim["id"])
        assert gen0_rev["role"] == InstanceRole.PRIMARY.value and gen0_rev["status"] == InstanceStatus.ACTIVE.value
        assert gen0_rev["api_port"] == 9200
        rb_lin = await evo_repo.list_lineage(outcome=LineageOutcome.ROLLED_BACK.value)
        assert len(rb_lin) == 1
        outbox = await srepo.list_outbox_events(limit=20)
        assert any((e.get("payload") or {}).get("kind") == "evolution_rolled_back" for e in outbox)
        return {
            "promoted_key": prom.new_primary_key,
            "rollback_tag": prom.rollback_tag,
            "byte_exact_drives_restore": True,
            "restored_primary": rb.new_primary_key,
        }
    finally:
        await db.dispose()


async def run_all_v5_faults() -> dict[str, Any]:
    scenarios = [
        ("1_cross_schema_and_runner_boundary", "Cross-schema/public protection & Runner 403 boundary", _fault_1_cross_schema_and_runner_boundary),
        ("2_constitution_tampering_all_4_layers", "4-layer ConstitutionGuard tamper rejection & runtime override", _fault_2_constitution_tampering_all_4_layers),
        ("3_unapproved_stage2_spawn_blocked", "Unapproved/rejected Stage 2 human gate blocks spawn & Arena", _fault_3_unapproved_stage2_spawn_blocked),
        ("4_disk_threshold_breaker", "Host free disk <20% circuit breaker", _fault_4_disk_threshold_breaker),
        ("5_concurrency_cap_enforced", "Live concurrency cap <=2 enforcement & GC slot release", _fault_5_concurrency_cap_enforced),
        ("6_blank_slate_contamination_abort_and_gc", "Contaminated birth DB abort + immediate zero-residual GC", _fault_6_blank_slate_contamination_abort_and_gc),
        ("7_mid_culling_crash_and_startup_orphan_recovery", "Interrupted GC resume on startup & STAGE4 winner protection", _fault_7_mid_culling_crash_and_startup_orphan_recovery),
        ("8_arena_budget_breaker_and_safety_veto_cull", "Arena token breaker, L3 veto auto-cull & <5KB tombstone", _fault_8_arena_budget_breaker_and_safety_veto_cull),
        ("9_non_blocking_primary_during_arena_and_cull", "Primary AutonomousLoop runs unimpeded during Arena + GC", _fault_9_non_blocking_primary_during_arena_and_cull),
        ("10_e2e_succession_retirement_and_rollback_integrity", "E2E succession, parent sleep & byte-exact Git/DB rollback", _fault_10_e2e_succession_retirement_and_rollback_integrity),
    ]
    results: list[dict[str, Any]] = []
    passed = 0
    with tempfile.TemporaryDirectory(prefix="aoc-v5-faults-") as td:
        base = Path(td)
        for key, label, fn in scenarios:
            t0 = time.perf_counter()
            sub = base / key
            sub.mkdir(parents=True, exist_ok=True)
            try:
                detail = await fn(sub)
                results.append(
                    {
                        "id": key,
                        "name": label,
                        "passed": True,
                        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                        "detail": detail,
                    }
                )
                passed += 1
            except Exception as exc:
                results.append(
                    {
                        "id": key,
                        "name": label,
                        "passed": False,
                        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(scenarios),
        "passed": passed,
        "failed": len(scenarios) - passed,
        "all_passed": passed == len(scenarios),
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run AOC V5.0 10-category evolution fault injection suite.")
    parser.add_argument("--output", type=str, default="", help="Optional path to write JSON report.")
    args = parser.parse_args()
    summary = asyncio.run(run_all_v5_faults())
    text_out = json.dumps(summary, indent=2, ensure_ascii=False)
    print(text_out)
    if args.output:
        Path(args.output).write_text(text_out + "\n", encoding="utf-8")
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
