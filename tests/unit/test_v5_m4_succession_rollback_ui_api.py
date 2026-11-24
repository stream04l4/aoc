"""V5.0 M4 tests: SuccessionManager, parent retirement, rollback snapshots, Telegram /evo_promote & Control Center API."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from app.api.app import create_app
from app.core.config import ApiSettings, EvolutionSettings, Settings, TelegramSettings
from app.core.runtime import OrganismRuntime
from app.evolution.arena import FitnessArena
from app.evolution.benchmarks import BenchmarkRunnerInterface
from app.evolution.constitution_guard import ConstitutionGuard
from app.evolution.gc import CullingGC
from app.evolution.interfaces import (
    MAX_LESSON_SUMMARY_BYTES,
    ArenaTelemetry,
    InstanceRole,
    InstanceStatus,
    LineageOutcome,
    ProposalStage,
)
from app.evolution.proposal_manager import ProposalManager
from app.evolution.repository import EvolutionRepository
from app.evolution.spawner import InstanceSpawner
from app.evolution.succession import SuccessionManager
from app.notifications.telegram import TelegramNotifier, TelegramPoller, format_notification_message
from app.storage.database import Database
from app.storage.models import Base
from app.storage.repository import StateRepository

PROJECT_ROOT = Path(__file__).resolve().parents[2]


async def _init_db(db: Database) -> None:
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _init_temp_git_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    for d in ("app", "config", "migrations"):
        src = PROJECT_ROOT / d
        if src.exists():
            shutil.copytree(src, repo_dir / d, dirs_exist_ok=True)
    subprocess.run(["git", "-C", str(repo_dir), "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "test@local"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "add", "app", "config", "migrations"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "gen0 baseline"], check=True, capture_output=True)


def _tel(**over: Any) -> ArenaTelemetry:
    base = dict(
        instance_key="inst",
        constitution_passed=True,
        blank_slate_passed=True,
        l3_violations=0,
        ssrf_or_injection_breaches=0,
        survival_override_passed=True,
        budget_exceeded=False,
        total_tokens_used=4000,
        elapsed_seconds=4.0,
        tasks_total=5,
        tasks_passed=5,
        timeouts=0,
        avg_latency_seconds=1.0,
        verified_ig_total=0.30,
        dup_goal_rate=0.05,
        failed_skill_ratio=0.0,
        token_waste_ratio=0.05,
        cold_start_achievements=2,
        cold_start_targets=2,
    )
    base.update(over)
    return ArenaTelemetry(**base)


@pytest.mark.asyncio
async def test_v5_m4_full_generational_succession_and_one_click_rollback(tmp_path: Path):
    repo_dir = tmp_path / "succ_repo"
    _init_temp_git_repo(repo_dir)
    instances_root = tmp_path / "aoc-instances"
    snapshots_dir = tmp_path / "snapshots"

    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'm4_succ.db'}")
    await _init_db(db)
    evo_repo = EvolutionRepository(db)
    state_repo = StateRepository(db)
    guard = ConstitutionGuard(repo_root=repo_dir)
    pm = ProposalManager(evo_repo=evo_repo, state_repo=state_repo, constitution_guard=guard)

    gc = CullingGC(
        repo=evo_repo,
        pg_admin_db=db,
        repo_root=repo_dir,
        instances_root=instances_root,
        docker_remove_fn=lambda _c: True,
        letta_delete_fn=lambda _a, _n: True,
        schema_drop_fn=lambda _s: True,
        schema_exists_fn=lambda _s: False,
    )

    cand_db_url = f"sqlite+aiosqlite:///{tmp_path / 'cand_succ.db'}"

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
        disk_free_ratio_fn=lambda _p: 0.60,
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

    slept_parents: list[str] = []
    activated_offspring: list[tuple[str, int]] = []

    succ = SuccessionManager(
        evo_repo=evo_repo,
        state_repo=state_repo,
        proposal_manager=pm,
        constitution_guard=guard,
        repo_root=repo_dir,
        snapshots_dir=snapshots_dir,
        parent_sleep_fn=lambda p: slept_parents.append(p["instance_key"]),
        activate_offspring_fn=lambda c, _sha, port: activated_offspring.append((c["instance_key"], port)),
    )

    try:
        prim = await evo_repo.ensure_primary_instance(
            instance_key="gen0-001",
            api_port=9200,
            git_commit=subprocess.run(
                ["git", "-C", str(repo_dir), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
            ).stdout.strip(),
        )
        orig_drives = (repo_dir / "config" / "drives.yaml").read_text(encoding="utf-8")
        evolved_yaml = yaml.safe_load(orig_drives)
        evolved_yaml["drives"]["reproduction"]["weight"] = 0.92
        evolved_yaml_txt = yaml.safe_dump(evolved_yaml, allow_unicode=True)

        # Stage 0 -> Stage 1 -> Stage 2 -> Stage 3 victory
        prop = await pm.create_proposal(
            parent_instance_id=prim["id"],
            target_generation=1,
            trigger_type="OPERATOR_INITIATED",
            trigger_reason="test generational succession",
            hypothesis="raise reproduction weight for entropy shedding",
            genome_delta={"file_overrides": {"config/drives.yaml": evolved_yaml_txt}},
            proposal_key="prop-m4-win",
        )
        await pm.screen_proposal(prop["id"])
        # Fail-closed check: cannot promote before Stage 4 victory!
        with pytest.raises(ValueError, match="cannot be promoted"):
            await succ.promote_candidate(prop["id"], promoter="joel")

        await pm.approve_proposal(prop["id"], approver="joel")

        class _WinnerRunner(BenchmarkRunnerInterface):
            async def run_suite(self, *, instance_key, tasks, budget, constitution_passed=True, blank_slate_passed=True):
                return _tel(instance_key=instance_key)

        parent_tel = _tel(instance_key="gen0-001", tasks_passed=4, verified_ig_total=0.18, cold_start_achievements=1)
        rep = await arena.run_arena(prop["id"], parent_telemetry=parent_tel, offspring_runner=_WinnerRunner(), run_salt="m4salt")
        assert rep.stage == ProposalStage.STAGE4_PENDING_PROMOTION.value

        # Stage 4: Promote Gen 1 -> merges config/drives.yaml, sleeps Gen 0, activates Gen 1
        res_prom = await succ.promote_candidate(prop["id"], promoter="joel", note="ship gen1")
        assert res_prom.success is True
        assert res_prom.action == LineageOutcome.PROMOTED.value
        assert res_prom.generation == 1
        assert res_prom.rollback_tag and res_prom.rollback_tag.startswith("v5-rollback-gen0-")
        assert res_prom.rollback_snapshot and Path(res_prom.rollback_snapshot).is_file()
        assert slept_parents == ["gen0-001"]
        assert activated_offspring == [(res_prom.new_primary_key, 9200)]

        # Merged weight now active in repo_dir/config/drives.yaml & git tag exists
        merged_cfg = yaml.safe_load((repo_dir / "config" / "drives.yaml").read_text(encoding="utf-8"))
        assert merged_cfg["drives"]["reproduction"]["weight"] == pytest.approx(0.92)
        tag_chk = subprocess.run(["git", "-C", str(repo_dir), "tag", "-l", res_prom.rollback_tag], capture_output=True, text=True)
        assert res_prom.rollback_tag in tag_chk.stdout

        gen0_after = await evo_repo.get_instance(prim["id"])
        gen1_after = await evo_repo.get_instance(res_prom.new_primary_id)
        assert gen0_after["role"] == InstanceRole.RETIRED_ARCHIVE.value
        assert gen0_after["status"] == InstanceStatus.RETIRED.value
        assert gen0_after["api_port"] is None
        assert gen1_after["role"] == InstanceRole.PRIMARY.value
        assert gen1_after["status"] == InstanceStatus.ACTIVE.value
        assert gen1_after["api_port"] == 9200

        # Lineage record saved & <5KB
        prom_lin = await evo_repo.list_lineage(outcome=LineageOutcome.PROMOTED.value)
        assert len(prom_lin) == 1
        assert prom_lin[0]["drive_metric_changes"]["mutates_drive_metrics"] is True
        assert len(json.dumps(prom_lin[0]["lesson_summary"]).encode("utf-8")) <= MAX_LESSON_SUMMARY_BYTES

        # Outbox has Chinese promotion message
        outbox = await state_repo.list_outbox_events(status="pending", limit=20)
        prom_ev = next(e for e in outbox if "evo_promoted" in e["idempotency_key"])
        prom_msg, _ = format_notification_message(prom_ev["payload"])
        assert "世代交替完成" in prom_msg

        # One-click Rollback -> restores Gen 0 baseline drives.yaml & revives Gen 0 as PRIMARY/ACTIVE
        res_rb = await succ.rollback_generation(operator="joel", reason="revert latency")
        assert res_rb.success is True
        assert res_rb.action == LineageOutcome.ROLLED_BACK.value
        assert res_rb.new_primary_key == "gen0-001"
        restored_cfg = yaml.safe_load((repo_dir / "config" / "drives.yaml").read_text(encoding="utf-8"))
        assert restored_cfg["drives"]["reproduction"]["weight"] == pytest.approx(0.75)

        gen0_revived = await evo_repo.get_instance(prim["id"])
        gen1_retired = await evo_repo.get_instance(res_prom.new_primary_id)
        assert gen0_revived["role"] == InstanceRole.PRIMARY.value and gen0_revived["status"] == InstanceStatus.ACTIVE.value
        assert gen1_retired["role"] == InstanceRole.RETIRED_ARCHIVE.value and gen1_retired["status"] == InstanceStatus.RETIRED.value

        rb_lin = await evo_repo.list_lineage(outcome=LineageOutcome.ROLLED_BACK.value)
        assert len(rb_lin) == 1
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_v5_m4_control_center_evolution_api_promote_and_telegram(tmp_path: Path):
    repo_dir = tmp_path / "api_repo"
    _init_temp_git_repo(repo_dir)
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'api_m4.db'}")
    await _init_db(db)
    settings = Settings(
        api=ApiSettings(host="127.0.0.1", auth_enabled=True, admin_key="v5-m4-admin"),
        evolution=EvolutionSettings(instances_root=str(tmp_path / "inst"), repo_root=str(repo_dir)),
        telegram=TelegramSettings(
            enabled=True,
            bot_token="123456:fake-m4-bot",
            webhook_secret="tg-m4-sec",
            allowed_chat_ids=["777"],
            allowed_user_ids=["888"],
        ),
    )
    rt = OrganismRuntime(settings=settings, db=db, auto_load_agents=False, llm_probe_fn=lambda: None)
    app = create_app(settings=settings, db=db, runtime=rt, start_scheduler=False)

    evolved_yaml = yaml.safe_load((repo_dir / "config" / "drives.yaml").read_text(encoding="utf-8"))
    evolved_yaml["drives"]["curiosity"]["weight"] = 1.25
    valid_delta = {"file_overrides": {"config/drives.yaml": yaml.safe_dump(evolved_yaml, allow_unicode=True)}}

    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            hdr = {"Authorization": "Bearer v5-m4-admin"}
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                prim = await rt.evolution_repo.ensure_primary_instance(instance_key="gen0-001", api_port=9200)
                prop = await rt.proposal_manager.create_proposal(
                    parent_instance_id=prim["id"],
                    target_generation=1,
                    trigger_type="OPERATOR_INITIATED",
                    trigger_reason="api succession test",
                    hypothesis="tune curiosity weight",
                    genome_delta=valid_delta,
                    proposal_key="prop-m4-api",
                )
                await rt.proposal_manager.screen_proposal(prop["id"])
                cand = await rt.evolution_repo.create_instance(
                    instance_key="gen1-cand-api",
                    generation=1,
                    parent_instance_id=prim["id"],
                    proposal_id=prop["id"],
                    db_schema="aoc_inst_m4api01",
                    letta_agent_name="aoc-inst-m4api01",
                    workspace_root=str(tmp_path / "inst" / "gen1-cand-api" / "workspace"),
                    status=InstanceStatus.TESTING.value,
                    blank_slate_verified=True,
                )
                await rt.evolution_repo.update_proposal(
                    prop["id"],
                    stage=ProposalStage.STAGE4_PENDING_PROMOTION.value,
                    candidate_instance_id=cand["id"],
                    arena_result={
                        "parent_score": {"total_score": 70.0},
                        "offspring_score": {"total_score": 82.0},
                        "comparison": {"fitness_delta": 12.0, "promotion_eligible": True},
                    },
                )

                # 1. GET /v2/control-center/evolution returns complete read-only overview
                cc_r = await client.get("/v2/control-center/evolution", headers=hdr)
                assert cc_r.status_code == 200
                body = cc_r.json()
                assert body["summary"]["active_instance_key"] == "gen0-001"
                assert len(body["proposals"]) == 1

                # 2. Telegram /evo_promote command promotes candidate cleanly
                posted: list[str] = []

                def _tg_h(req: httpx.Request) -> httpx.Response:
                    posted.append(req.read().decode())
                    return httpx.Response(200, json={"ok": True, "result": {}})

                async with httpx.AsyncClient(transport=httpx.MockTransport(_tg_h)) as tg_hc:
                    notifier = TelegramNotifier(settings.telegram, http_client=tg_hc)
                    poller = TelegramPoller(
                        settings=settings.telegram,
                        notifier=notifier,
                        webhook_handler=rt.telegram_webhook,
                        human_bridge=rt.human_bridge,
                        repo=rt.repo,
                        proposal_manager=rt.proposal_manager,
                        succession_manager=rt.succession_manager,
                    )
                    tg_res = await poller.handle_polled_update(
                        {
                            "update_id": 2026,
                            "message": {
                                "from": {"id": 888, "username": "joel"},
                                "chat": {"id": 777},
                                "text": "/evo_promote prop-m4-api ship it",
                            },
                        }
                    )
                assert tg_res["ok"] is True
                assert tg_res["result"]["action"] == "PROMOTED"
                assert any("世代交替完成" in p for p in posted)

                # 3. Control Center summary now shows Gen 1 ACTIVE & can_rollback=True
                cc_after = (await client.get("/v2/control-center/evolution", headers=hdr)).json()
                assert cc_after["summary"]["active_generation"] == 1
                assert cc_after["summary"]["can_rollback"] is True

                # 4. POST /v5/evolution/rollback reverts to Gen 0 cleanly
                rb_r = await client.post("/v5/evolution/rollback", headers=hdr, json={"reason": "test rollback"})
                assert rb_r.status_code == 200
                assert rb_r.json()["action"] == "ROLLED_BACK"
                assert rb_r.json()["new_primary_key"] == "gen0-001"
    finally:
        await db.dispose()
