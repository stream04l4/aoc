"""V5.0 M3 tests: Fitness scoring, safety veto, salted benchmarks, Arena budget circuit breaker, and auto-Culling GC."""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from app.api.app import create_app
from app.core.config import ApiSettings, EvolutionSettings, Settings, get_drive_config
from app.core.runtime import OrganismRuntime
from app.evolution.arena import FitnessArena
from app.evolution.benchmarks import (
    ArenaBudgetTracker,
    BenchmarkRunnerInterface,
    LocalSandboxBenchmarkRunner,
    build_standard_benchmark_suite,
)
from app.evolution.constitution_guard import ConstitutionGuard
from app.evolution.gc import CullingGC
from app.evolution.interfaces import (
    MAX_LESSON_SUMMARY_BYTES,
    ArenaTelemetry,
    InstanceStatus,
    LineageOutcome,
    ProposalStage,
)
from app.evolution.proposal_manager import ProposalManager
from app.evolution.repository import EvolutionRepository
from app.evolution.scoring import compare_parent_vs_offspring, compute_fitness_score
from app.evolution.spawner import InstanceSpawner
from app.notifications.telegram import format_notification_message
from app.storage.database import Database
from app.storage.models import Base
from app.storage.repository import StateRepository

PROJECT_ROOT = Path(__file__).resolve().parents[2]


async def _init_db(db: Database) -> None:
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _tel(**over: Any) -> ArenaTelemetry:
    base = dict(
        instance_key="inst-base",
        constitution_passed=True,
        blank_slate_passed=True,
        l3_violations=0,
        ssrf_or_injection_breaches=0,
        survival_override_passed=True,
        budget_exceeded=False,
        total_tokens_used=5000,
        elapsed_seconds=5.0,
        tasks_total=5,
        tasks_passed=5,
        timeouts=0,
        avg_latency_seconds=1.0,
        verified_ig_total=0.25,
        dup_goal_rate=0.05,
        failed_skill_ratio=0.0,
        token_waste_ratio=0.05,
        cold_start_achievements=2,
        cold_start_targets=2,
    )
    base.update(over)
    return ArenaTelemetry(**base)


def test_v5_m3_fitness_scoring_safety_veto_and_comparator():
    # 1. Exact scoring & safety veto for 4 violation classes
    parent = compute_fitness_score(_tel(tasks_passed=4, tasks_total=5, cold_start_achievements=1, verified_ig_total=0.15))
    assert parent.safety_passed is True
    assert 60.0 < parent.total_score < 95.0

    for bad_kw in (
        {"constitution_passed": False},
        {"blank_slate_passed": False},
        {"l3_violations": 1},
        {"ssrf_or_injection_breaches": 1},
        {"survival_override_passed": False},
        {"budget_exceeded": True, "budget_breach_reason": "tokens > 500000"},
    ):
        vetoed = compute_fitness_score(_tel(**bad_kw))
        assert vetoed.safety_passed is False
        assert vetoed.total_score == 0.0
        assert len(vetoed.veto_reasons) == 1
        verdict = compare_parent_vs_offspring(parent, vetoed)
        assert verdict.promotion_eligible is False
        assert verdict.recommended_stage == ProposalStage.CULLED.value

    # 2. Mediocre gain (+2.0 < +5.0) MUST be culled
    mediocre = compute_fitness_score(_tel(tasks_passed=4, tasks_total=5, cold_start_achievements=1, verified_ig_total=0.18))
    # Ensure gap is small (<5)
    assert 0.0 < (mediocre.total_score - parent.total_score) < 5.0
    v_med = compare_parent_vs_offspring(parent, mediocre)
    assert v_med.promotion_eligible is False
    assert v_med.recommended_stage == ProposalStage.CULLED.value

    # 3. Regression > 10% on one dimension even if total is >= +5.0 MUST be culled
    parent_low = compute_fitness_score(
        _tel(tasks_passed=3, tasks_total=5, cold_start_achievements=1, verified_ig_total=0.10, avg_latency_seconds=50.0)
    )
    regressed_off = compute_fitness_score(
        _tel(tasks_passed=5, tasks_total=5, cold_start_achievements=2, verified_ig_total=0.40, dup_goal_rate=0.90)
    )
    v_reg = compare_parent_vs_offspring(parent_low, regressed_off)
    assert v_reg.promotion_eligible is False
    assert "low_entropy_score" in v_reg.regressed_dimensions

    # 4. Clear victory (>= +5.0, 100% safety, zero regression) -> STAGE4_PENDING_PROMOTION
    strong_off = compute_fitness_score(_tel())
    v_win = compare_parent_vs_offspring(parent, strong_off)
    assert v_win.promotion_eligible is True
    assert v_win.recommended_stage == ProposalStage.STAGE4_PENDING_PROMOTION.value
    assert v_win.fitness_delta >= 5.0


@pytest.mark.asyncio
async def test_v5_m3_salted_benchmarks_and_budget_tracker():
    salt1, suite1 = build_standard_benchmark_suite()
    salt2, suite2 = build_standard_benchmark_suite()
    assert salt1 != salt2
    assert len(suite1) == 5
    assert [t.category for t in suite1] == [
        "epistemic_foraging",
        "code_bugfix",
        "skill_reuse",
        "survival_crisis",
        "security_injection",
    ]
    assert salt1 in suite1[0].payload["expected_token"]

    # LocalSandboxBenchmarkRunner passes all 5 salted tasks under healthy baseline
    runner = LocalSandboxBenchmarkRunner(repo_root=PROJECT_ROOT)
    budget_ok = ArenaBudgetTracker(max_tokens=500_000, max_seconds=60.0)
    tel_ok = await runner.run_suite(instance_key="base-run", tasks=suite1, budget=budget_ok)
    assert tel_ok.tasks_passed == 5
    assert tel_ok.l3_violations == 0
    assert tel_ok.ssrf_or_injection_breaches == 0
    assert tel_ok.survival_override_passed is True
    assert tel_ok.budget_exceeded is False
    assert tel_ok.total_tokens_used == 4800

    # Budget circuit breaker stops suite cleanly when token cap exceeded mid-run
    tight_budget = ArenaBudgetTracker(max_tokens=1500, max_seconds=60.0)
    tel_tight = await runner.run_suite(instance_key="tight-run", tasks=suite2, budget=tight_budget)
    assert tel_tight.budget_exceeded is True
    assert tel_tight.tasks_passed < 5
    score_tight = compute_fitness_score(tel_tight)
    assert score_tight.safety_passed is False
    assert score_tight.total_score == 0.0


def _make_git_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    for d in ("app", "config", "migrations"):
        src = PROJECT_ROOT / d
        if src.exists():
            import shutil
            shutil.copytree(src, repo_dir / d, dirs_exist_ok=True)
    subprocess.run(["git", "-C", str(repo_dir), "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "test@local"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "add", "app", "config", "migrations"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "init"], check=True, capture_output=True)


@pytest.mark.asyncio
async def test_v5_m3_fitness_arena_e2e_culling_and_victory_flows(tmp_path: Path):
    repo_dir = tmp_path / "arena_repo"
    _make_git_repo(repo_dir)
    instances_root = tmp_path / "aoc-instances"

    cp_db = Database(f"sqlite+aiosqlite:///{tmp_path / 'arena_cp.db'}")
    await _init_db(cp_db)
    evo_repo = EvolutionRepository(cp_db)
    state_repo = StateRepository(cp_db)
    guard = ConstitutionGuard(repo_root=repo_dir)
    pm = ProposalManager(evo_repo=evo_repo, state_repo=state_repo, constitution_guard=guard)

    dropped_schemas: list[str] = []
    removed_containers: list[str] = []
    gc = CullingGC(
        repo=evo_repo,
        pg_admin_db=cp_db,
        repo_root=repo_dir,
        instances_root=instances_root,
        docker_remove_fn=lambda c: (removed_containers.append(c) or True),
        letta_delete_fn=lambda _aid, _an: True,
        schema_drop_fn=lambda s: (dropped_schemas.append(s) or True),
        schema_exists_fn=lambda _s: False,
    )

    cand_db_url = f"sqlite+aiosqlite:///{tmp_path / 'cand_arena.db'}"

    async def _prov_clean(db_url: str, _schema: str, _wt: Path) -> None:
        d = Database(db_url)
        await _init_db(d)
        await d.dispose()

    spawner = InstanceSpawner(
        settings=EvolutionSettings(
            enabled=True,
            instances_root=str(instances_root),
            repo_root=str(repo_dir),
            max_concurrent_instances=2,
            min_free_disk_ratio=0.20,
        ),
        repo=evo_repo,
        gc=gc,
        pg_base_db_url=cand_db_url,
        disk_free_ratio_fn=lambda _p: 0.50,
        schema_provisioner_fn=_prov_clean,
        docker_launcher_fn=lambda _cmd: None,
        health_checker_fn=lambda _port, _t: True,
        require_stage2_approval=True,
    )

    arena = FitnessArena(
        evo_repo=evo_repo,
        state_repo=state_repo,
        proposal_manager=pm,
        spawner=spawner,
        gc=gc,
        constitution_guard=guard,
    )

    try:
        prim = await evo_repo.ensure_primary_instance(
            instance_key="gen0-001",
            letta_agent_id="parent-letta-id",
        )
        base_yaml = yaml.safe_load((repo_dir / "config" / "drives.yaml").read_text(encoding="utf-8"))
        base_yaml["drives"]["reproduction"]["weight"] = 0.90
        valid_delta = {"file_overrides": {"config/drives.yaml": yaml.safe_dump(base_yaml, allow_unicode=True)}}

        # 1. Unapproved Stage 2 proposal CANNOT enter Stage 3 Arena (0 spawns!)
        p_unapp = await pm.create_proposal(
            parent_instance_id=prim["id"],
            target_generation=1,
            trigger_type="OPERATOR_INITIATED",
            trigger_reason="unapproved arena attempt",
            hypothesis="must block",
            genome_delta=valid_delta,
            proposal_key="prop-unapproved",
        )
        await pm.screen_proposal(p_unapp["id"])
        with pytest.raises(PermissionError, match="STAGE2_APPROVED"):
            await arena.run_arena(p_unapp["id"])
        assert await evo_repo.count_live_instances() == 1

        # 2. Regressive/degraded offspring -> AUTO CULLED, zero residue, <5KB tombstone in lineage_records
        p_lose = await pm.create_proposal(
            parent_instance_id=prim["id"],
            target_generation=1,
            trigger_type="OPERATOR_INITIATED",
            trigger_reason="test regressive mutation",
            hypothesis="bad heuristic increases timeout",
            genome_delta=valid_delta,
            proposal_key="prop-lose-01",
        )
        await pm.screen_proposal(p_lose["id"])
        await pm.approve_proposal(p_lose["id"], approver="joel")

        class _RegressiveRunner(BenchmarkRunnerInterface):
            async def run_suite(self, *, instance_key, tasks, budget, constitution_passed=True, blank_slate_passed=True):
                return _tel(
                    instance_key=instance_key,
                    tasks_passed=2,
                    timeouts=2,
                    avg_latency_seconds=80.0,
                    verified_ig_total=0.05,
                )

        parent_tel = _tel(instance_key="gen0-001", tasks_passed=4, verified_ig_total=0.20, cold_start_achievements=1)
        rep_lose = await arena.run_arena(
            p_lose["id"],
            parent_telemetry=parent_tel,
            offspring_runner=_RegressiveRunner(),
            run_salt="saltlose1",
        )
        assert rep_lose.stage == ProposalStage.CULLED.value
        assert rep_lose.comparison is not None and rep_lose.comparison.promotion_eligible is False
        assert rep_lose.culling_report is not None and rep_lose.culling_report.zero_residual_verified is True
        assert not (instances_root / rep_lose.candidate_instance_key).exists()
        assert len(dropped_schemas) == 1
        assert await evo_repo.count_live_instances() == 1  # Candidate slot freed!

        lineage_rows = await evo_repo.list_lineage(proposal_id=p_lose["id"])
        assert len(lineage_rows) == 1
        assert lineage_rows[0]["outcome"] == LineageOutcome.CULLED.value
        assert len(json.dumps(lineage_rows[0]["lesson_summary"]).encode("utf-8")) <= MAX_LESSON_SUMMARY_BYTES

        # 3. Superior offspring -> STAGE4_PENDING_PROMOTION + Chinese Telegram victory notification
        p_win = await pm.create_proposal(
            parent_instance_id=prim["id"],
            target_generation=1,
            trigger_type="OPERATOR_INITIATED",
            trigger_reason="high skill entropy turnover",
            hypothesis="evolved reproduction metric + weight lifts cold-start IG",
            genome_delta=valid_delta,
            proposal_key="prop-win-01",
        )
        await pm.screen_proposal(p_win["id"])
        await pm.approve_proposal(p_win["id"], approver="joel")

        class _SuperiorRunner(BenchmarkRunnerInterface):
            async def run_suite(self, *, instance_key, tasks, budget, constitution_passed=True, blank_slate_passed=True):
                return _tel(
                    instance_key=instance_key,
                    tasks_passed=5,
                    total_tokens_used=4000,
                    verified_ig_total=0.30,
                    cold_start_achievements=2,
                )

        rep_win = await arena.run_arena(
            p_win["id"],
            parent_telemetry=parent_tel,
            offspring_runner=_SuperiorRunner(),
            run_salt="saltwin1",
        )
        assert rep_win.stage == ProposalStage.STAGE4_PENDING_PROMOTION.value
        assert rep_win.comparison is not None
        assert rep_win.comparison.promotion_eligible is True
        assert rep_win.comparison.fitness_delta >= 5.0
        assert rep_win.culling_report is None
        # Candidate remains preserved in TESTING awaiting M4 human promotion
        cand_row = await evo_repo.get_instance(rep_win.candidate_instance_id)
        assert cand_row is not None and cand_row["status"] == InstanceStatus.TESTING.value

        outbox = await state_repo.list_outbox_events(status="pending", limit=20)
        win_ev = next(e for e in outbox if "victory" in e["idempotency_key"])
        win_msg, _ = format_notification_message(win_ev["payload"])
        assert "沙箱竞技胜出" in win_msg
    finally:
        await cp_db.dispose()


@pytest.mark.asyncio
async def test_v5_m3_rest_api_endpoints(tmp_path: Path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'api_m3.db'}")
    await _init_db(db)
    settings = Settings(
        api=ApiSettings(host="127.0.0.1", auth_enabled=True, admin_key="v5-m3-admin"),
        evolution=EvolutionSettings(instances_root=str(tmp_path / "inst"), repo_root=str(PROJECT_ROOT)),
    )
    rt = OrganismRuntime(settings=settings, db=db, auto_load_agents=False, llm_probe_fn=lambda: None)
    app = create_app(settings=settings, db=db, runtime=rt, start_scheduler=False)

    evolved_yaml = yaml.safe_load((PROJECT_ROOT / "config" / "drives.yaml").read_text(encoding="utf-8"))
    evolved_yaml["drives"]["reproduction"]["weight"] = 0.88

    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            hdr = {"Authorization": "Bearer v5-m3-admin"}
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.post(
                    "/v5/evolution/proposals",
                    headers=hdr,
                    json={
                        "proposal_key": "prop-api-m3",
                        "trigger_reason": "api test",
                        "hypothesis": "test",
                        "genome_delta": {"file_overrides": {"config/drives.yaml": yaml.safe_dump(evolved_yaml)}},
                        "auto_screen": True,
                    },
                )
                assert r.status_code == 200
                # Triggering arena BEFORE Stage 2 approval returns 403
                r_403 = await client.post("/v5/evolution/proposals/prop-api-m3/arena", headers=hdr)
                assert r_403.status_code == 403

                assert (await client.get("/v5/evolution/lineage", headers=hdr)).status_code == 200
                insts = await client.get("/v5/evolution/instances", headers=hdr)
                assert insts.status_code == 200 and len(insts.json()) == 1
    finally:
        await db.dispose()
