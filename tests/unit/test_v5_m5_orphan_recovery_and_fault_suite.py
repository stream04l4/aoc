"""V5.0 M5 tests: Startup orphan candidate recovery, Loop/Runtime wiring, /v5/evolution/gc/recover & soak check."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import update

from app.api.app import create_app
from app.core.config import (
    ApiSettings,
    EvolutionSettings,
    GovernanceSettings,
    KernelV3Settings,
    MemorySettings,
    PerceptionSettings,
    Settings,
)
from app.core.runtime import OrganismRuntime
from app.evolution.gc import CullingGC
from app.evolution.interfaces import InstanceStatus, ProposalStage
from app.evolution.repository import EvolutionRepository
from app.storage.database import Database
from app.storage.models import Base, InstanceRegistryRow
from scripts.v5_soak_check import run_v5_soak_check

PROJECT_ROOT = Path(__file__).resolve().parents[2]


async def _init_db(db: Database) -> None:
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _healthy_llm_probe() -> dict[str, Any]:
    return {"available": True, "latency_ms": 50}


@pytest.mark.asyncio
async def test_v5_m5_recover_orphan_candidates_and_loop_integration(tmp_path: Path):
    db_path = tmp_path / "m5_orphan.db"
    db = Database(f"sqlite+aiosqlite:///{db_path}")
    await _init_db(db)
    inst_root = tmp_path / "aoc-instances"
    evo_repo = EvolutionRepository(db)
    gc = CullingGC(
        repo=evo_repo,
        pg_admin_db=db,
        repo_root=PROJECT_ROOT,
        instances_root=inst_root,
        docker_remove_fn=lambda _c: True,
        letta_delete_fn=lambda _a, _n: True,
        schema_drop_fn=lambda _s: True,
        schema_exists_fn=lambda _s: False,
    )

    try:
        prim = await evo_repo.ensure_primary_instance(instance_key="gen0-001", db_schema="public")
        p_cull = await evo_repo.create_proposal(
            proposal_key="prop-m5-cull",
            parent_instance_id=prim["id"],
            target_generation=1,
            trigger_type="OPERATOR_INITIATED",
            trigger_reason="culling resume",
            hypothesis="h1",
            genome_delta={"files": ["config/drives.yaml"]},
        )
        p_stale = await evo_repo.create_proposal(
            proposal_key="prop-m5-stale",
            parent_instance_id=prim["id"],
            target_generation=1,
            trigger_type="OPERATOR_INITIATED",
            trigger_reason="stale orphan",
            hypothesis="h2",
            genome_delta={"files": ["config/drives.yaml"]},
            stage=ProposalStage.STAGE3_ARENA_RUNNING.value,
            arena_budget={"max_tokens": 500000, "max_seconds": 1200},
        )
        p_fresh = await evo_repo.create_proposal(
            proposal_key="prop-m5-fresh",
            parent_instance_id=prim["id"],
            target_generation=1,
            trigger_type="OPERATOR_INITIATED",
            trigger_reason="fresh active",
            hypothesis="h3",
            genome_delta={"files": ["config/drives.yaml"]},
            stage=ProposalStage.STAGE3_ARENA_RUNNING.value,
            arena_budget={"max_tokens": 500000, "max_seconds": 1200},
        )
        p_win = await evo_repo.create_proposal(
            proposal_key="prop-m5-win",
            parent_instance_id=prim["id"],
            target_generation=1,
            trigger_type="OPERATOR_INITIATED",
            trigger_reason="winner",
            hypothesis="h4",
            genome_delta={"files": ["config/drives.yaml"]},
            stage=ProposalStage.STAGE4_PENDING_PROMOTION.value,
            arena_budget={"max_tokens": 500000, "max_seconds": 1200},
        )

        for key in ("gen1-culling", "gen1-stale", "gen1-fresh", "gen1-winner"):
            (inst_root / key / "workspace").mkdir(parents=True, exist_ok=True)
            (inst_root / key / "repo").mkdir(parents=True, exist_ok=True)

        c_culling = await evo_repo.create_instance(
            instance_key="gen1-culling",
            generation=1,
            parent_instance_id=prim["id"],
            proposal_id=p_cull["id"],
            db_schema="aoc_inst_m5cull1",
            letta_agent_name="aoc-inst-m5cull1",
            repo_worktree_path=str(inst_root / "gen1-culling" / "repo"),
            workspace_root=str(inst_root / "gen1-culling" / "workspace"),
            status=InstanceStatus.CULLING.value,
        )
        # Pre-seed an existing lineage record simulating a crash right before final DB status update
        await evo_repo.record_lineage(
            generation=1,
            parent_instance_id=prim["id"],
            offspring_instance_id=c_culling["id"],
            proposal_id=p_cull["id"],
            outcome="CULLED",
            safety_passed=False,
            lesson_summary={"hypothesis": "h1", "outcome_reason": "pre-crash"},
        )

        c_stale = await evo_repo.create_instance(
            instance_key="gen1-stale",
            generation=1,
            parent_instance_id=prim["id"],
            proposal_id=p_stale["id"],
            db_schema="aoc_inst_m5stale",
            letta_agent_name="aoc-inst-m5stale",
            repo_worktree_path=str(inst_root / "gen1-stale" / "repo"),
            workspace_root=str(inst_root / "gen1-stale" / "workspace"),
            status=InstanceStatus.TESTING.value,
        )
        c_fresh = await evo_repo.create_instance(
            instance_key="gen1-fresh",
            generation=1,
            parent_instance_id=prim["id"],
            proposal_id=p_fresh["id"],
            db_schema="aoc_inst_m5fresh",
            letta_agent_name="aoc-inst-m5fresh",
            repo_worktree_path=str(inst_root / "gen1-fresh" / "repo"),
            workspace_root=str(inst_root / "gen1-fresh" / "workspace"),
            status=InstanceStatus.TESTING.value,
        )
        c_win = await evo_repo.create_instance(
            instance_key="gen1-winner",
            generation=1,
            parent_instance_id=prim["id"],
            proposal_id=p_win["id"],
            db_schema="aoc_inst_m5win01",
            letta_agent_name="aoc-inst-m5win01",
            repo_worktree_path=str(inst_root / "gen1-winner" / "repo"),
            workspace_root=str(inst_root / "gen1-winner" / "workspace"),
            status=InstanceStatus.TESTING.value,
        )

        ref_now = datetime.now(timezone.utc)
        async with db.engine.begin() as conn:
            await conn.execute(
                update(InstanceRegistryRow).where(InstanceRegistryRow.id == c_stale["id"]).values(
                    updated_at=ref_now - timedelta(minutes=30)
                )
            )
            await conn.execute(
                update(InstanceRegistryRow).where(InstanceRegistryRow.id == c_fresh["id"]).values(
                    updated_at=ref_now - timedelta(seconds=60)
                )
            )
            await conn.execute(
                update(InstanceRegistryRow).where(InstanceRegistryRow.id == c_win["id"]).values(
                    updated_at=ref_now - timedelta(hours=3)
                )
            )

        summary = await gc.recover_orphan_candidates(now=ref_now)
        assert summary["resumed_culling"] == ["gen1-culling"]
        assert summary["culled_stale"] == ["gen1-stale"]
        assert summary["skipped_pending_promotion"] == ["gen1-winner"]
        assert summary["failed"] == []

        assert not (inst_root / "gen1-culling").exists()
        assert not (inst_root / "gen1-stale").exists()
        assert (inst_root / "gen1-fresh").exists()
        assert (inst_root / "gen1-winner").exists()

        # No duplicate lineage row for pre-recorded CULLING instance
        assert len(await evo_repo.list_lineage(proposal_id=p_cull["id"])) == 1
        assert (await evo_repo.get_proposal(p_stale["id"]))["stage"] == ProposalStage.CULLED.value
        assert (await evo_repo.get_proposal(p_win["id"]))["stage"] == ProposalStage.STAGE4_PENDING_PROMOTION.value
        assert (await evo_repo.get_instance(prim["id"]))["status"] == InstanceStatus.ACTIVE.value

        # Also verify OrganismRuntime.start() wires culling_gc into AutonomousLoop.recover()
        c_cull_loop = await evo_repo.create_instance(
            instance_key="gen1-loop-gc",
            generation=1,
            parent_instance_id=prim["id"],
            db_schema="aoc_inst_m5loop",
            letta_agent_name="aoc-inst-m5loop",
            repo_worktree_path=str(inst_root / "gen1-loop-gc" / "repo"),
            workspace_root=str(inst_root / "gen1-loop-gc" / "workspace"),
            status=InstanceStatus.CULLING.value,
        )
        (inst_root / "gen1-loop-gc" / "workspace").mkdir(parents=True, exist_ok=True)
    finally:
        await db.dispose()

    db2 = Database(f"sqlite+aiosqlite:///{db_path}")
    settings = Settings(
        governance=GovernanceSettings(workspace_root=str(tmp_path / "ws_rt")),
        memory=MemorySettings(provider="none"),
        perception=PerceptionSettings(llm_probe=False),
        kernel_v3=KernelV3Settings(world_poll_on_perceive=False),
        evolution=EvolutionSettings(instances_root=str(inst_root), repo_root=str(PROJECT_ROOT)),
    )
    rt = OrganismRuntime(
        settings=settings,
        db=db2,
        auto_load_agents=False,
        llm_probe_fn=_healthy_llm_probe,
    )
    rt.culling_gc = CullingGC(
        repo=rt.evolution_repo,
        pg_admin_db=db2,
        repo_root=PROJECT_ROOT,
        instances_root=inst_root,
        docker_remove_fn=lambda _c: True,
        letta_delete_fn=lambda _a, _n: True,
        schema_drop_fn=lambda _s: True,
        schema_exists_fn=lambda _s: False,
    )
    try:
        started = await rt.start(start_scheduler=False)
        assert "gen1-loop-gc" in started["recovery"]["evolution_orphan_gc"]["resumed_culling"]
        row_after = await rt.evolution_repo.get_instance(c_cull_loop["id"])
        assert row_after is not None and row_after["status"] == InstanceStatus.CULLED.value
    finally:
        await rt.stop()
        await db2.dispose()


@pytest.mark.asyncio
async def test_v5_m5_api_gc_recover_and_soak_check(tmp_path: Path):
    db_path = tmp_path / "m5_api_soak.db"
    db = Database(f"sqlite+aiosqlite:///{db_path}")
    await _init_db(db)
    inst_root = tmp_path / "aoc-instances"
    settings = Settings(
        api=ApiSettings(host="127.0.0.1", auth_enabled=True, admin_key="v5-m5-admin"),
        memory=MemorySettings(provider="none"),
        perception=PerceptionSettings(llm_probe=False),
        kernel_v3=KernelV3Settings(world_poll_on_perceive=False),
        evolution=EvolutionSettings(instances_root=str(inst_root), repo_root=str(PROJECT_ROOT)),
    )
    rt = OrganismRuntime(
        settings=settings,
        db=db,
        auto_load_agents=False,
        llm_probe_fn=_healthy_llm_probe,
    )
    rt.culling_gc = CullingGC(
        repo=rt.evolution_repo,
        pg_admin_db=db,
        repo_root=PROJECT_ROOT,
        instances_root=inst_root,
        docker_remove_fn=lambda _c: True,
        letta_delete_fn=lambda _a, _n: True,
        schema_drop_fn=lambda _s: True,
        schema_exists_fn=lambda _s: False,
    )
    app = create_app(settings=settings, db=db, runtime=rt, start_scheduler=False)
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            hdr = {"Authorization": "Bearer v5-m5-admin"}
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                prim = await rt.evolution_repo.ensure_primary_instance(instance_key="gen0-001")
                (inst_root / "gen1-api-cull" / "workspace").mkdir(parents=True, exist_ok=True)
                await rt.evolution_repo.create_instance(
                    instance_key="gen1-api-cull",
                    generation=1,
                    parent_instance_id=prim["id"],
                    db_schema="aoc_inst_m5apic",
                    letta_agent_name="aoc-inst-m5apic",
                    workspace_root=str(inst_root / "gen1-api-cull" / "workspace"),
                    status=InstanceStatus.CULLING.value,
                )

                unauth = await client.post("/v5/evolution/gc/recover")
                assert unauth.status_code == 401

                resp = await client.post("/v5/evolution/gc/recover", headers=hdr)
                assert resp.status_code == 200
                body = resp.json()
                assert body["resumed_culling"] == ["gen1-api-cull"]
                assert not (inst_root / "gen1-api-cull").exists()

                # Soak check passes cleanly when instances_root has zero orphan dirs & snapshots/ is ignored
                (inst_root / "snapshots").mkdir(parents=True, exist_ok=True)
                clean_soak = await run_v5_soak_check(
                    repo_root=PROJECT_ROOT,
                    instances_root=inst_root,
                    db_url=f"sqlite+aiosqlite:///{db_path}",
                )
                assert clean_soak["steady_state_ok"] is True
                assert clean_soak["checks"]["constitution"]["ok"] is True
                assert clean_soak["checks"]["database"]["live_instances"] == 1

                # Inject an orphan candidate directory -> soak check flags steady_state_ok=False
                (inst_root / "gen1-orphan-leftover").mkdir(parents=True, exist_ok=True)
                dirty_soak = await run_v5_soak_check(
                    repo_root=PROJECT_ROOT,
                    instances_root=inst_root,
                    db_url=f"sqlite+aiosqlite:///{db_path}",
                )
                assert dirty_soak["steady_state_ok"] is False
                assert any("Orphan candidate directories" in f for f in dirty_soak["failures"])
    finally:
        await rt.stop()
        await db.dispose()
