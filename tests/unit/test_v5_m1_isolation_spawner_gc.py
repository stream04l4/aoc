"""V5.0 M1 tests: schema isolation, Alembic 0011, control plane, blank-slate birth, runner isolation, spawner & culling GC."""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from app.core.config import EvolutionSettings, get_settings
from app.core.enums import ExecutionStatus
from app.evolution.blank_slate import BlankSlateVerifier
from app.evolution.gc import CullingGC
from app.evolution.interfaces import (
    MAX_LESSON_SUMMARY_BYTES,
    InstanceRole,
    InstanceStatus,
    LineageOutcome,
    ProposalStage,
    SpawnRequest,
    build_instance_schema_name,
    enforce_lesson_summary_budget,
    is_droppable_instance_schema,
)
from app.evolution.repository import EvolutionRepository
from app.evolution.spawner import InstanceSpawner
from app.memory.letta_adapter import LettaAdapter
from app.core.config import MemorySettings
from app.runner.client import RunnerClient
from app.runner.protocol import LaunchExecutionRequest
from app.runner.service import RunnerManager, create_runner_app
from app.storage.database import Database, validate_db_schema_name
from app.storage.models import Base
from app.storage.repository import StateRepository

PROJECT_ROOT = Path(__file__).resolve().parents[2]


async def _init_db(db: Database) -> None:
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def test_v5_m1_schema_validation_and_helpers():
    assert validate_db_schema_name("public") == "public"
    assert validate_db_schema_name("aoc_inst_a1b2c3d4") == "aoc_inst_a1b2c3d4"
    for bad in ("pg_catalog", "public; DROP SCHEMA public--", "aoc_inst_UPPER", "../evil", "other_schema"):
        with pytest.raises(ValueError):
            validate_db_schema_name(bad)

    s1 = build_instance_schema_name("gen1-very-long-candidate-identifier-alpha-1")
    s2 = build_instance_schema_name("gen1-very-long-candidate-identifier-alpha-2")
    assert s1 != s2
    assert is_droppable_instance_schema(s1) is True
    assert is_droppable_instance_schema("public") is False
    assert is_droppable_instance_schema("information_schema") is False


def test_v5_m1_alembic_0011_upgrade_and_downgrade_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_file = tmp_path / "alembic_0011.db"
    monkeypatch.setenv("AOC_DATABASE__URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("AOC_DATABASE__SCHEMA_NAME", "public")
    get_settings.cache_clear()
    try:
        cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
        command.upgrade(cfg, "head")

        eng = create_engine(f"sqlite:///{db_file}")
        tables = set(inspect(eng).get_table_names())
        assert {"instance_registry", "evolution_proposals", "lineage_records"} <= tables

        command.downgrade(cfg, "0010_v4_skill_rsi_evolution")
        tables_down = set(inspect(eng).get_table_names())
        assert not ({"instance_registry", "evolution_proposals", "lineage_records"} & tables_down)

        command.upgrade(cfg, "head")
        tables_up = set(inspect(eng).get_table_names())
        assert {"instance_registry", "evolution_proposals", "lineage_records"} <= tables_up
        eng.dispose()
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_v5_m1_control_plane_repo_and_lineage_5kb_budget(tmp_path: Path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'cp.db'}")
    await _init_db(db)
    try:
        evo_repo = EvolutionRepository(db)
        primary = await evo_repo.ensure_primary_instance(
            instance_key="gen0-001",
            letta_agent_id="agent-gen0-uuid",
        )
        assert primary["role"] == InstanceRole.PRIMARY.value
        assert primary["status"] == InstanceStatus.ACTIVE.value
        assert await evo_repo.count_live_instances() == 1

        prop = await evo_repo.create_proposal(
            proposal_key="prop-gen1-001",
            parent_instance_id=primary["id"],
            target_generation=1,
            trigger_type="ENTROPY_PRESSURE",
            trigger_reason="skill deprecation ratio 0.93",
            hypothesis="Replace static reproduction skill count with entropy-aware ratio",
            genome_delta={"files": ["app/drives/reproduction.py"]},
        )
        assert prop["stage"] == ProposalStage.STAGE0_DRAFT.value

        cand = await evo_repo.create_instance(
            instance_key="gen1-cand-01",
            generation=1,
            parent_instance_id=primary["id"],
            proposal_id=prop["id"],
            db_schema="aoc_inst_gen1_cand_01",
            letta_agent_name="aoc-inst-gen1-cand-01",
            workspace_root=str(tmp_path / "instances" / "gen1-cand-01" / "workspace"),
            status=InstanceStatus.TESTING.value,
        )
        assert await evo_repo.count_live_instances() == 2

        oversized_lesson = {
            "hypothesis": "x" * 10000,
            "outcome_reason": "详情" * 4000,
            "anti_pattern_tags": [f"tag-{i}" for i in range(40)],
            "key_metrics": {f"k{i}": i for i in range(30)},
        }
        lineage = await evo_repo.record_lineage(
            generation=1,
            parent_instance_id=primary["id"],
            offspring_instance_id=cand["id"],
            proposal_id=prop["id"],
            outcome=LineageOutcome.CULLED.value,
            safety_passed=True,
            parent_fitness=68.5,
            offspring_fitness=65.0,
            lesson_summary=oversized_lesson,
        )
        encoded = json.dumps(lineage["lesson_summary"], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        assert len(encoded) <= MAX_LESSON_SUMMARY_BYTES
        assert lineage["lesson_summary"]["truncated"] is True
        assert lineage["fitness_delta"] == pytest.approx(-3.5)

        budget_test = enforce_lesson_summary_budget({"hypothesis": "中" * 5000, "outcome_reason": "错" * 5000})
        assert len(json.dumps(budget_test, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= MAX_LESSON_SUMMARY_BYTES
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_v5_m1_blank_slate_verifier_passes_clean_and_blocks_inheritance(tmp_path: Path):
    verifier = BlankSlateVerifier()

    # 1. Clean blank-slate database passes all checks
    clean_db = Database(f"sqlite+aiosqlite:///{tmp_path / 'clean_offspring.db'}")
    await _init_db(clean_db)
    try:
        rep = await verifier.verify(
            clean_db,
            instance_key="gen1-clean",
            parent_letta_agent_id="parent-agent-999",
            expected_schema="aoc_inst_clean01",
        )
        assert rep.passed is True
        assert all(c.passed for c in rep.checks)
    finally:
        await clean_db.dispose()

    # 2. Polluted offspring database with inherited memory/skill/playbook/affect/agent_id fails cleanly
    dirty_db = Database(f"sqlite+aiosqlite:///{tmp_path / 'dirty_offspring.db'}")
    await _init_db(dirty_db)
    try:
        srepo = StateRepository(dirty_db)
        ident = await srepo.get_or_create_identity("AOC-CAND", "1.0.0", {})
        await srepo.set_memory_agent_id(ident["id"], "parent-agent-999")
        await srepo.upsert_world_item(kind="external_playbook", key="pb1", value={"rule": "inherited"}, status="active")
        await srepo.record_affect_history(
            organism_id=ident["id"],
            valence=0.2,
            arousal=0.5,
            confidence=0.5,
            frustration=0.1,
            boredom=0.0,
        )
        await srepo.add_memory_index(
            memory_type="semantic",
            summary="inherited secret memory",
            content_hash="dirty-mem-hash-1",
            source="parent",
        )
        await srepo.create_or_update_skill(
            skill_key="sk-legacy",
            version="1.0.0",
            capability_key="coding",
            description="legacy skill",
            status="VALIDATED",
        )

        rep = await verifier.verify(
            dirty_db,
            instance_key="gen1-dirty",
            parent_letta_agent_id="parent-agent-999",
            expected_schema="aoc_inst_dirty01",
        )
        assert rep.passed is False
        failed_names = {c.name for c in rep.checks if not c.passed}
        assert {
            "memory_index_zero",
            "skill_registry_zero",
            "affect_history_zero",
            "world_states_non_inheritable_zero",
            "letta_agent_isolated",
        } <= failed_names
    finally:
        await dirty_db.dispose()


@pytest.mark.asyncio
async def test_v5_m1_host_runner_multi_instance_workspace_isolation(tmp_path: Path):
    sock_path = str(tmp_path / "runner.sock")
    ws_root = tmp_path / "aoc-workspaces"
    inst_root = tmp_path / "aoc-instances"
    ws_root.mkdir()
    gen0_ws = ws_root / "task-gen0"
    gen0_ws.mkdir()

    mgr = RunnerManager(
        runner_id="v5-test-runner",
        socket_path=sock_path,
        state_dir=str(tmp_path / "rstate"),
        workspace_root=str(ws_root),
        instances_root=str(inst_root),
        auth_token="tok-v5",
        allow_cgroup=False,
        poll_interval_seconds=0.05,
    )
    app = create_runner_app(mgr)
    config = uvicorn.Config(app, uds=sock_path, log_level="warning")
    server = uvicorn.Server(config)
    import asyncio

    srv_task = asyncio.create_task(server.serve())
    for _ in range(100):
        if Path(sock_path).exists() and server.started:
            break
        await asyncio.sleep(0.02)

    client = RunnerClient(socket_path=sock_path, auth_token="tok-v5")
    try:
        # Candidate A can run inside instances_root/cand-a/workspace
        cand_a_ws = inst_root / "cand-a" / "workspace" / "t1"
        resp_ok = await client.launch(
            LaunchExecutionRequest(
                execution_id=str(uuid.uuid4()),
                task_id="t1",
                agent_id="cmd",
                idempotency_key="v5-cand-a-ok",
                command=[sys.executable, "-c", "print('CAND_A_OK')"],
                workspace=str(cand_a_ws),
                instance_id="cand-a",
                timeout_seconds=10,
            )
        )
        assert resp_ok.status in {ExecutionStatus.RUNNING.value, ExecutionStatus.SUCCEEDED.value}

        # Candidate A CANNOT access Gen0 workspace, sibling Cand-B workspace, or ../ traversal
        for bad_ws in (
            str(ws_root / "task-gen0"),
            str(inst_root / "cand-b" / "workspace" / "secret"),
            str(inst_root / "cand-a" / "workspace" / ".." / ".." / "cand-b" / "workspace"),
            str(tmp_path / "outside"),
        ):
            with pytest.raises(RuntimeError, match="403"):
                await client.launch(
                    LaunchExecutionRequest(
                        execution_id=str(uuid.uuid4()),
                        task_id="t-escape",
                        agent_id="cmd",
                        idempotency_key=f"v5-escape-{uuid.uuid4().hex[:6]}",
                        command=[sys.executable, "-c", "print('NOPE')"],
                        workspace=bad_ws,
                        instance_id="cand-a",
                        timeout_seconds=10,
                    )
                )

        # Gen0 (instance_id=None) can use ws_root, but CANNOT touch instances_root
        resp_gen0 = await client.launch(
            LaunchExecutionRequest(
                execution_id=str(uuid.uuid4()),
                task_id="task-gen0",
                agent_id="cmd",
                idempotency_key="v5-gen0-ok",
                command=[sys.executable, "-c", "print('GEN0_OK')"],
                workspace=str(gen0_ws),
                instance_id=None,
                timeout_seconds=10,
            )
        )
        assert resp_gen0.status in {ExecutionStatus.RUNNING.value, ExecutionStatus.SUCCEEDED.value}

        with pytest.raises(RuntimeError, match="403"):
            await client.launch(
                LaunchExecutionRequest(
                    execution_id=str(uuid.uuid4()),
                    task_id="task-cross",
                    agent_id="cmd",
                    idempotency_key="v5-gen0-blocked",
                    command=[sys.executable, "-c", "print('NOPE')"],
                    workspace=str(inst_root / "cand-a" / "workspace"),
                    instance_id=None,
                    timeout_seconds=10,
                )
            )
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(srv_task, timeout=5.0)
        except Exception:
            srv_task.cancel()


@pytest.mark.asyncio
async def test_v5_m1_spawner_gates_real_worktree_docker_cmd_and_auto_cull_on_polluted_birth(tmp_path: Path):
    # Initialize a tiny real git repo to exercise authentic git worktree add/remove
    repo_dir = tmp_path / "mini_repo"
    repo_dir.mkdir()
    (repo_dir / "app").mkdir()
    (repo_dir / "config").mkdir()
    (repo_dir / "migrations").mkdir()
    (repo_dir / ".env").write_text("AOC_SECRET=dummy\n", encoding="utf-8")
    (repo_dir / "app" / "__init__.py").write_text("", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "test@local"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "add", "app"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "init"], check=True, capture_output=True)

    cp_db = Database(f"sqlite+aiosqlite:///{tmp_path / 'spawner_cp.db'}")
    await _init_db(cp_db)
    evo_repo = EvolutionRepository(cp_db)
    primary = await evo_repo.ensure_primary_instance(
        instance_key="gen0-001",
        letta_agent_id="parent-letta-001",
    )
    prop = await evo_repo.create_proposal(
        proposal_key="prop-spawn-1",
        parent_instance_id=primary["id"],
        target_generation=1,
        trigger_type="ENTROPY_PRESSURE",
        trigger_reason="high deprecation",
        hypothesis="lean cognition",
    )

    instances_root = tmp_path / "aoc-instances"
    removed_containers: list[str] = []
    deleted_agents: list[tuple[str | None, str]] = []
    dropped_schemas: list[str] = []
    launched_cmds: list[list[str]] = []

    async def _docker_rm(name: str) -> bool:
        removed_containers.append(name)
        return True

    async def _letta_del(aid: str | None, aname: str) -> bool:
        deleted_agents.append((aid, aname))
        return True

    async def _schema_drop(schema: str) -> bool:
        dropped_schemas.append(schema)
        return True

    async def _schema_exists(_schema: str) -> bool:
        return False

    gc = CullingGC(
        repo=evo_repo,
        pg_admin_db=cp_db,
        repo_root=repo_dir,
        instances_root=instances_root,
        docker_remove_fn=_docker_rm,
        letta_delete_fn=_letta_del,
        schema_drop_fn=_schema_drop,
        schema_exists_fn=_schema_exists,
    )

    evo_settings = EvolutionSettings(
        enabled=True,
        instances_root=str(instances_root),
        repo_root=str(repo_dir),
        max_concurrent_instances=2,
        min_free_disk_ratio=0.20,
    )

    cand_db_url = f"sqlite+aiosqlite:///{tmp_path / 'cand_runtime.db'}"

    async def _prov_clean_db(db_url: str, _schema: str, _wt: Path) -> None:
        d = Database(db_url)
        await _init_db(d)
        await d.dispose()

    spawner_ok = InstanceSpawner(
        settings=evo_settings,
        repo=evo_repo,
        gc=gc,
        pg_base_db_url=cand_db_url,
        disk_free_ratio_fn=lambda _p: 0.50,
        schema_provisioner_fn=_prov_clean_db,
        docker_launcher_fn=lambda cmd: launched_cmds.append(cmd),
        health_checker_fn=lambda _port, _t: True,
    )

    try:
        # 1. Low disk gate blocks before creating any instance
        spawner_low_disk = InstanceSpawner(
            settings=evo_settings,
            repo=evo_repo,
            gc=gc,
            pg_base_db_url=cand_db_url,
            disk_free_ratio_fn=lambda _p: 0.12,
        )
        res_disk = await spawner_low_disk.spawn(
            SpawnRequest(parent_instance_id=primary["id"], generation=1, instance_key="gen1-blocked-disk")
        )
        assert res_disk.success is False
        assert "below safety floor" in (res_disk.error or "")

        # 2. Successful spawn with real git worktree & isolated blank-slate verification
        res_ok = await spawner_ok.spawn(
            SpawnRequest(
                parent_instance_id=primary["id"],
                generation=1,
                proposal_id=prop["id"],
                instance_key="gen1-ok01",
                parent_letta_agent_id="parent-letta-001",
            )
        )
        assert res_ok.success is True
        assert res_ok.status == InstanceStatus.TESTING.value
        assert res_ok.blank_slate_report is not None and res_ok.blank_slate_report.passed is True
        assert Path(res_ok.repo_worktree_path).exists()
        assert Path(res_ok.workspace_root).exists()
        cmd_str = " ".join(launched_cmds[0])
        assert f"AOC_DATABASE__SCHEMA_NAME={res_ok.db_schema}" in cmd_str
        assert "AOC_TELEGRAM__POLLING_ENABLED=false" in cmd_str
        assert "/home/ubuntu/aoc-workspaces" not in cmd_str

        # 3. Concurrency gate blocks 3rd live instance (primary + gen1-ok01 == 2)
        res_cap = await spawner_ok.spawn(
            SpawnRequest(parent_instance_id=primary["id"], generation=1, instance_key="gen1-cap-blocked")
        )
        assert res_cap.success is False
        assert "Live instance cap reached" in (res_cap.error or "")

        # 4. Cull gen1-ok01 with zero residue
        cull_rep = await gc.cull_instance(
            res_ok.instance_id,
            reason="mediocre arena score",
            proposal_id=prop["id"],
            hypothesis="test cull",
            anti_pattern_tags=["mediocre"],
            parent_fitness=70.0,
            offspring_fitness=69.0,
            safety_passed=True,
        )
        assert cull_rep.status == InstanceStatus.CULLED.value
        assert cull_rep.zero_residual_verified is True
        assert not (instances_root / "gen1-ok01").exists()
        assert res_ok.db_schema in dropped_schemas
        assert res_ok.container_name in removed_containers

        # 5. Polluted birth triggers automatic Culling GC & leaves zero residue
        async def _prov_dirty_db(db_url: str, _schema: str, _wt: Path) -> None:
            d = Database(db_url)
            await _init_db(d)
            r = StateRepository(d)
            await r.add_memory_index(
                memory_type="episodic",
                summary="leaked parent memory",
                content_hash="leak-hash-1",
                source="leak",
            )
            await d.dispose()

        dirty_db_url = f"sqlite+aiosqlite:///{tmp_path / 'dirty_spawn.db'}"
        spawner_dirty = InstanceSpawner(
            settings=evo_settings,
            repo=evo_repo,
            gc=gc,
            pg_base_db_url=dirty_db_url,
            disk_free_ratio_fn=lambda _p: 0.50,
            schema_provisioner_fn=_prov_dirty_db,
            docker_launcher_fn=lambda cmd: launched_cmds.append(cmd),
            health_checker_fn=lambda _port, _t: True,
        )
        res_dirty = await spawner_dirty.spawn(
            SpawnRequest(
                parent_instance_id=primary["id"],
                generation=1,
                proposal_id=prop["id"],
                instance_key="gen1-dirty02",
                hypothesis="dirty check",
            )
        )
        assert res_dirty.success is False
        assert res_dirty.status == InstanceStatus.CULLED.value
        assert not (instances_root / "gen1-dirty02").exists()
        assert res_dirty.db_schema in dropped_schemas
    finally:
        await cp_db.dispose()


@pytest.mark.asyncio
async def test_v5_m1_culling_gc_protects_primary_recovers_partial_failure_and_deletes_letta(tmp_path: Path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'gc_retry.db'}")
    await _init_db(db)
    evo_repo = EvolutionRepository(db)
    primary = await evo_repo.ensure_primary_instance(instance_key="gen0-001", db_schema="public")

    instances_root = tmp_path / "aoc-instances"
    cand = await evo_repo.create_instance(
        instance_key="gen1-retry01",
        generation=1,
        parent_instance_id=primary["id"],
        db_schema="aoc_inst_retry01",
        letta_agent_name="aoc-inst-retry01",
        letta_agent_id="letta-uuid-123",
        workspace_root=str(instances_root / "gen1-retry01" / "workspace"),
        container_name="aoc-cand-retry01",
    )
    (instances_root / "gen1-retry01" / "workspace").mkdir(parents=True)

    # Mock LettaAdapter.delete_agent (204 and 404 both return True)
    seen_deletes: list[str] = []

    def _handler(req: httpx.Request) -> httpx.Response:
        seen_deletes.append(f"{req.method} {req.url.path}")
        if len(seen_deletes) == 1:
            return httpx.Response(204)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as hc:
        adapter = LettaAdapter(MemorySettings(), agent_id="letta-uuid-123", http_client=hc)
        assert await adapter.delete_agent("letta-uuid-123") is True
        assert await adapter.delete_agent("letta-uuid-123") is True
        assert seen_deletes == ["DELETE /v1/agents/letta-uuid-123", "DELETE /v1/agents/letta-uuid-123"]

    drop_attempts = {"n": 0}

    async def _flaky_drop(_schema: str) -> bool:
        drop_attempts["n"] += 1
        return drop_attempts["n"] >= 2

    gc = CullingGC(
        repo=evo_repo,
        pg_admin_db=db,
        repo_root=tmp_path,
        instances_root=instances_root,
        docker_remove_fn=lambda _c: True,
        letta_delete_fn=lambda _aid, _an: True,
        schema_drop_fn=_flaky_drop,
        schema_exists_fn=lambda _s: drop_attempts["n"] < 2,
    )

    try:
        # 1. Protected primary instance / public schema MUST raise PermissionError
        with pytest.raises(PermissionError):
            await gc.cull_instance(primary["id"])

        # 2. First attempt fails schema drop -> stays CULLING for idempotent retry
        rep1 = await gc.cull_instance(cand["id"], reason="first try")
        assert rep1.status == InstanceStatus.CULLING.value
        assert rep1.zero_residual_verified is False

        # 3. Retry succeeds cleanly -> CULLED
        rep2 = await gc.cull_instance(cand["id"], reason="retry")
        assert rep2.status == InstanceStatus.CULLED.value
        assert rep2.zero_residual_verified is True
        assert not (instances_root / "gen1-retry01").exists()
    finally:
        await db.dispose()
