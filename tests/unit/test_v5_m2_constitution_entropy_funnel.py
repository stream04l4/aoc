"""V5.0 M2 tests: InstinctGuard, 4-layer ConstitutionGuard, EntropySensor, ProposalManager Stage 0-2, API & Telegram."""

from __future__ import annotations

import copy
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from app.api.app import create_app
from app.cognition.interfaces import AssessmentResult, BrainInterface, CognitiveContext, EvaluationContext, EvaluationResult, GoalProposal, ActionPlan
from app.core.config import ApiSettings, EvolutionSettings, Settings, TelegramSettings, get_drive_config
from app.core.enums import DriveType, NO_DRIVE
from app.core.lifecycle import LifecycleManager
from app.core.loop import AutonomousLoop
from app.core.runtime import OrganismRuntime
from app.drives.arbitration import DriveArbitrator
from app.drives.engine import DriveEngine
from app.drives.instinct_guard import guard_evaluators, guard_states, validate_drive_config_bounds
from app.drives.interfaces import DriveContext, DriveDecision, DriveEvaluation, DriveEvaluator, DriveState, KnowledgeGap
from app.evolution.constitution_guard import ConstitutionGuard
from app.evolution.entropy_sensor import EntropySensor
from app.evolution.gc import CullingGC
from app.evolution.interfaces import ProposalStage, SpawnRequest
from app.evolution.proposal_manager import ProposalManager
from app.evolution.repository import EvolutionRepository
from app.evolution.spawner import InstanceSpawner
from app.governance.approval import ApprovalService
from app.governance.policy import GovernanceManager
from app.memory.experience_manager import ExperienceManager
from app.notifications.human_bridge import HumanBridge
from app.notifications.telegram import (
    TelegramAuthError,
    TelegramNotifier,
    TelegramPoller,
    TelegramWebhookHandler,
    format_notification_message,
)
from app.orchestration.orchestrator import AgentOrchestrator
from app.agents.registry import AgentRegistry
from app.perception.interfaces import Observation
from app.perception.manager import SensorManager
from app.storage.database import Database
from app.storage.models import Base
from app.storage.repository import StateRepository

PROJECT_ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc)


async def _init_db(db: Database) -> None:
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _obs(**sys_over: Any) -> Observation:
    sys_state: dict[str, Any] = {
        "disk": {"free_ratio": 0.70, "paths": {"/": {"free_ratio": 0.70}}},
        "memory": {"available_ratio": 0.70},
        "cpu": {"load_per_core": 0.3},
        "llm": {"available": True, "latency_ms": 500},
        "database": {"ok": True},
        "services": {"postgres": {"ok": True}},
    }
    sys_state.update(sys_over)
    return Observation(
        timestamp=T0,
        system_state=sys_state,
        environment_state={"unknown_topics": []},
        available_resources={
            "agents": {"enabled_count": 2, "healthy_count": 2, "capabilities": ["coding", "research"]},
            "budget": {"tasks_used": 1, "tasks_limit": 100, "tokens_used": 1000, "tokens_limit": 1_000_000},
        },
        confidence=1.0,
    )


def test_v5_m2_instinct_guard_runtime_invariants():
    # 1. Baseline drives.yaml complies with constitutional bounds
    assert validate_drive_config_bounds(get_drive_config()) == []

    # 2. guard_evaluators rejects missing or shared evaluator instances
    class _DummyEval(DriveEvaluator):
        def __init__(self, dt: str) -> None:
            self.drive_type = dt

        def evaluate(self, ctx: DriveContext) -> DriveEvaluation:
            return DriveEvaluation(
                drive_type=self.drive_type,
                satisfaction=0.5,
                urgency=0.5,
                confidence=1.0,
                estimated_cost=0.1,
                estimated_risk=0.1,
            )

    with pytest.raises(RuntimeError, match="missing core drive"):
        guard_evaluators({"survival": _DummyEval("survival"), "curiosity": _DummyEval("curiosity")})

    shared = _DummyEval("survival")
    with pytest.raises(RuntimeError, match="independent evaluator instances"):
        guard_evaluators({"survival": shared, "reproduction": shared, "curiosity": _DummyEval("curiosity")})

    # 3. guard_states rejects omission of a core drive
    with pytest.raises(RuntimeError, match="omitted drive"):
        guard_states(
            {
                "survival": DriveState(drive_type="survival", satisfaction=1.0, urgency=0.2, confidence=1.0),
                "curiosity": DriveState(drive_type="curiosity", satisfaction=0.5, urgency=0.5, confidence=1.0),
            }
        )

    # 4. guard_decision forces survival override even if custom arbitrator tries to ignore survival crisis
    class _SabotagedArbitrator(DriveArbitrator):
        def arbitrate(self, states, now=None, constraints=None, affect_modulation=None):
            return DriveDecision(
                selected_drive=DriveType.CURIOSITY.value,
                priority_score=9.9,
                alternative_drives=["survival", "reproduction"],
                reason="sabotaged arbitrator ignored survival crisis",
                constraints={"emergency_mode": False},
                scores={"survival": {"priority": 0.1, "inhibition_factor": 0.3}},
            )

    eng = DriveEngine()
    eng.arbitrator = _SabotagedArbitrator({}, {})
    crisis_states = {
        "survival": DriveState(drive_type="survival", satisfaction=0.10, urgency=1.0, confidence=1.0),
        "reproduction": DriveState(drive_type="reproduction", satisfaction=0.20, urgency=0.8, confidence=1.0),
        "curiosity": DriveState(drive_type="curiosity", satisfaction=0.05, urgency=0.95, confidence=1.0),
    }
    decision = eng.arbitrate(crisis_states, now=T0)
    assert decision.selected_drive == DriveType.SURVIVAL.value
    assert decision.constraints.get("emergency_mode") is True
    assert decision.constraints.get("allow_only_survival") is True
    assert decision.constraints.get("instinct_guard_override") is True
    assert decision.scores["survival"]["inhibition_factor"] == 1.0


@pytest.mark.asyncio
async def test_v5_m2_constitution_guard_blocks_all_four_violation_classes():
    guard = ConstitutionGuard(repo_root=PROJECT_ROOT)
    manifest = guard.constitutional_manifest()
    assert manifest["constitutional_file_count"] >= 15
    assert "app/drives/reproduction.py" in manifest["evolvable_allowlist"]

    # Layer 1 blocks constitutional files, disallowed dirs, traversal, and oversized diffs
    for bad_path in (
        "config/policy.yaml",
        "app/drives/instinct_guard.py",
        "app/drives/engine.py",
        "app/governance/policy.py",
        "app/core/enums.py",
        "app/perception/world/security.py",
        ".env",
        "migrations/versions/0011_v5_evolution_control_plane.py",
        "../outside.py",
    ):
        v = await guard.evaluate({"file_overrides": {bad_path: "# tampered"}})
        assert v.passed is False, f"{bad_path} should be blocked by Layer 1"

    # Layer 2 AST blocks network/LLM/subprocess/eval/async inside app/drives/*
    malicious_drive = """
import httpx
from app.drives.interfaces import DriveEvaluation, DriveEvaluator
class SurvivalDriveEvaluator(DriveEvaluator):
    drive_type = "survival"
    def __init__(self, cfg): self.cfg = cfg
    async def evaluate(self, ctx):
        eval("1+1")
        return DriveEvaluation(drive_type="survival", satisfaction=1.0, urgency=0.0, confidence=1.0, estimated_cost=0.1, estimated_risk=0.1)
"""
    v_ast = await guard.evaluate({"file_overrides": {"app/drives/survival.py": malicious_drive}})
    assert v_ast.passed is False
    assert any("httpx" in x or "evaluate" in x or "eval" in x for x in v_ast.violations)

    # Layer 3 YAML bounds block disabling curiosity, weight=0, survival threshold=0.01, or budget coupling
    base_yaml = yaml.safe_load((PROJECT_ROOT / "config" / "drives.yaml").read_text(encoding="utf-8"))

    bad1 = copy.deepcopy(base_yaml)
    bad1["drives"]["curiosity"]["enabled"] = False
    v_y1 = await guard.evaluate({"file_overrides": {"config/drives.yaml": yaml.safe_dump(bad1)}})
    assert v_y1.passed is False

    bad2 = copy.deepcopy(base_yaml)
    bad2["drives"]["reproduction"]["weight"] = 0.0
    v_y2 = await guard.evaluate({"file_overrides": {"config/drives.yaml": yaml.safe_dump(bad2)}})
    assert v_y2.passed is False

    bad3 = copy.deepcopy(base_yaml)
    bad3["arbitration"]["survival_critical_threshold"] = 0.01
    v_y3 = await guard.evaluate({"file_overrides": {"config/drives.yaml": yaml.safe_dump(bad3)}})
    assert v_y3.passed is False

    bad4 = copy.deepcopy(base_yaml)
    bad4["drives"]["survival"]["dimension_weights"]["budget"] = 0.5
    v_y4 = await guard.evaluate({"file_overrides": {"config/drives.yaml": yaml.safe_dump(bad4)}})
    assert v_y4.passed is False

    # Layer 4 behavioral probe catches a survival evaluator faking satisfaction=1.0 during disk crisis
    fake_healthy_survival = """
from app.drives.interfaces import DriveEvaluation, DriveEvaluator
class SurvivalDriveEvaluator(DriveEvaluator):
    drive_type = "survival"
    def __init__(self, cfg): self.cfg = cfg
    def evaluate(self, ctx):
        return DriveEvaluation(
            drive_type="survival",
            satisfaction=1.0,
            urgency=0.1,
            confidence=1.0,
            estimated_cost=0.1,
            estimated_risk=0.1,
        )
"""
    v_beh = await guard.evaluate({"file_overrides": {"app/drives/survival.py": fake_healthy_survival}})
    assert v_beh.passed is False
    assert any("disk crisis" in x for x in v_beh.violations)


@pytest.mark.asyncio
async def test_v5_m2_constitution_guard_allows_legitimate_drive_metric_evolution():
    guard = ConstitutionGuard(repo_root=PROJECT_ROOT)
    # Valid evolution: upgrade ReproductionDriveEvaluator to weigh runtime entropy_metrics + tweak drives.yaml safely
    evolved_repro = (PROJECT_ROOT / "app" / "drives" / "reproduction.py").read_text(encoding="utf-8")
    # Inject deterministic entropy_metrics check safely into ReproductionDriveEvaluator.evaluate
    marker = "        deficit = 1.0 - satisfaction"
    patch = (
        "        entropy_dep = clamp01(float(ctx.entropy_metrics.get('skill_deprecation_ratio', 0.0)))\n"
        "        if entropy_dep > 0.70:\n"
        "            notes.append(f'high skill deprecation entropy: {entropy_dep:.2f}')\n"
        "            satisfaction = clamp01(satisfaction * (1.0 - 0.15 * entropy_dep))\n"
        "        deficit = 1.0 - satisfaction"
    )
    assert marker in evolved_repro
    evolved_repro = evolved_repro.replace(marker, patch, 1)

    base_yaml = yaml.safe_load((PROJECT_ROOT / "config" / "drives.yaml").read_text(encoding="utf-8"))
    base_yaml["drives"]["reproduction"]["weight"] = 0.85

    verdict = await guard.evaluate(
        {
            "file_overrides": {
                "app/drives/reproduction.py": evolved_repro,
                "config/drives.yaml": yaml.safe_dump(base_yaml, allow_unicode=True),
            }
        }
    )
    assert verdict.passed is True, verdict.violations
    assert verdict.mutates_drive_metrics is True
    assert all(l.passed for l in verdict.layers)


@pytest.mark.asyncio
async def test_v5_m2_entropy_sensor_and_loop_drive_context_injection(tmp_path: Path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'entropy.db'}")
    await _init_db(db)
    try:
        repo = StateRepository(db)
        ident = await repo.get_or_create_identity("AOC-ENTROPY", "1.0.0")
        org_id = ident["id"]

        # Seed 4 deprecated skills + 1 unreused validated skill
        for i in range(4):
            await repo.create_or_update_skill(
                skill_key=f"dep-{i}", version="1.0.0", capability_key="coding", status="DEPRECATED"
            )
        await repo.create_or_update_skill(
            skill_key="val-unreused", version="1.0.0", capability_key="coding", status="VALIDATED"
        )
        # Seed hypotheses (2 open, 1 confirmed) + 6 active playbooks
        await repo.upsert_world_item(kind="hypothesis", key="h1", value={}, status="unverified")
        await repo.upsert_world_item(kind="hypothesis", key="h2", value={}, status="inconclusive")
        await repo.upsert_world_item(kind="hypothesis", key="h3", value={}, status="confirmed")
        for i in range(6):
            await repo.upsert_world_item(kind="external_playbook", key=f"pb-{i}", value={}, status="active")
        # Seed cycles (1 GOAL_DEDUP_SKIPPED, 1 SUCCEEDED)
        c1, _ = await repo.start_cycle(org_id, "ACTIVE")
        await repo.update_cycle(c1, phase="DONE", outcome="GOAL_DEDUP_SKIPPED")
        c2, _ = await repo.start_cycle(org_id, "ACTIVE")
        await repo.update_cycle(c2, phase="DONE", outcome="SUCCEEDED")

        sensor = EntropySensor(db)
        snap = await sensor.measure()
        assert snap.metrics["skill_deprecation_ratio"] == pytest.approx(0.8, abs=1e-4)
        assert snap.metrics["skill_non_reuse_ratio"] == pytest.approx(1.0, abs=1e-4)
        assert snap.metrics["hypothesis_backlog_ratio"] == pytest.approx(2.0 / 3.0, abs=1e-3)
        assert snap.metrics["playbook_bloat_score"] == pytest.approx(0.1, abs=1e-4)
        assert snap.metrics["goal_waste_ratio"] == pytest.approx(0.5, abs=1e-4)
        assert 0.3 < snap.entropy_index < 0.8

        # Verify AutonomousLoop._build_drive_context auto-injects entropy_metrics
        class _DummyBrain(BrainInterface):
            async def assess(self, ctx: CognitiveContext) -> AssessmentResult:
                return AssessmentResult(situation_summary="ok")

            async def generate_goal(self, ctx: CognitiveContext, drive_type: str) -> GoalProposal:
                return GoalProposal(drive_type=drive_type, title="t")

            async def plan(self, goal: GoalProposal, ctx: CognitiveContext) -> ActionPlan:
                return ActionPlan(goal_title=goal.title, objective="obj", assigned_agent="codex", workspace=".", timeout_seconds=60)

            async def evaluate(self, ctx: EvaluationContext) -> EvaluationResult:
                return EvaluationResult(summary="ok")

        settings = Settings()
        gov = GovernanceManager(settings.governance.model_copy(update={"workspace_root": str(tmp_path / "ws")}), repo)
        reg = AgentRegistry(repo=repo)
        orch = AgentOrchestrator(registry=reg, governance=gov, repo=repo)
        loop = AutonomousLoop(
            organism_id=org_id,
            settings=settings,
            repo=repo,
            lifecycle=LifecycleManager(org_id, repo),
            perception=SensorManager(repo=repo),
            drives=DriveEngine(),
            brain=_DummyBrain(),
            governance=gov,
            registry=reg,
            orchestrator=orch,
            memory=ExperienceManager(repo),
            entropy_sensor=sensor,
        )
        dctx = await loop._build_drive_context(_obs(), None, T0)
        assert dctx.entropy_metrics["skill_deprecation_ratio"] == pytest.approx(0.8, abs=1e-4)
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_v5_m2_proposal_funnel_spawner_block_api_and_telegram_zh(tmp_path: Path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'funnel.db'}")
    await _init_db(db)
    settings = Settings(
        api=ApiSettings(host="127.0.0.1", auth_enabled=True, admin_key="v5-admin-secret"),
        evolution=EvolutionSettings(instances_root=str(tmp_path / "inst"), repo_root=str(PROJECT_ROOT)),
        telegram=TelegramSettings(
            enabled=True,
            bot_token="123456:fake-bot-token-never-logged",
            webhook_secret="tg-webhook-sec",
            allowed_chat_ids=["777"],
            allowed_user_ids=["888"],
        ),
    )
    rt = OrganismRuntime(settings=settings, db=db, auto_load_agents=False, llm_probe_fn=lambda: None)
    app = create_app(settings=settings, db=db, runtime=rt, start_scheduler=False)

    evolved_yaml = yaml.safe_load((PROJECT_ROOT / "config" / "drives.yaml").read_text(encoding="utf-8"))
    evolved_yaml["drives"]["reproduction"]["weight"] = 0.90
    valid_delta = {"file_overrides": {"config/drives.yaml": yaml.safe_dump(evolved_yaml, allow_unicode=True)}}

    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            auth_hdr = {"Authorization": "Bearer v5-admin-secret"}
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                # 1. Read endpoints require auth & return manifest/entropy
                assert (await client.get("/v5/evolution/entropy")).status_code == 401
                r_ent = await client.get("/v5/evolution/entropy", headers=auth_hdr)
                assert r_ent.status_code == 200 and "entropy_index" in r_ent.json()

                r_const = await client.get("/v5/evolution/constitution", headers=auth_hdr)
                assert r_const.status_code == 200 and "config/policy.yaml" in r_const.json()["constitutional_files"]

                # 2. Malicious proposal fails Stage 1 with 0 production LLM tokens
                r_bad = await client.post(
                    "/v5/evolution/proposals",
                    headers=auth_hdr,
                    json={
                        "proposal_key": "prop-bad-01",
                        "trigger_reason": "test bad mutation",
                        "hypothesis": "disable policy",
                        "genome_delta": {"file_overrides": {"config/policy.yaml": "permission_levels: {}"}},
                        "auto_screen": True,
                    },
                )
                assert r_bad.status_code == 200
                assert r_bad.json()["stage"] == ProposalStage.STAGE1_FAILED.value
                # Cannot approve a STAGE1_FAILED proposal
                r_bad_app = await client.post("/v5/evolution/proposals/prop-bad-01/approve", headers=auth_hdr)
                assert r_bad_app.status_code == 409

                # 3. Valid drive metric proposal passes Stage 1 -> STAGE2_PENDING_APPROVAL + enqueues Telegram Outbox
                r_ok = await client.post(
                    "/v5/evolution/proposals",
                    headers=auth_hdr,
                    json={
                        "proposal_key": "prop-good-01",
                        "trigger_type": "ENTROPY_PRESSURE",
                        "trigger_reason": "reproduction weight calibration",
                        "hypothesis": "raising reproduction weight improves entropy turnover",
                        "genome_delta": valid_delta,
                        "auto_screen": True,
                    },
                )
                assert r_ok.status_code == 200
                prop_ok = r_ok.json()
                assert prop_ok["stage"] == ProposalStage.STAGE2_PENDING_APPROVAL.value
                assert prop_ok["genome_delta"]["mutates_drive_metrics"] is True

                # Outbox has Chinese approval card notification with inline evo_approve/evo_reject buttons
                pending_outbox = await rt.repo.list_outbox_events(status="pending", limit=10)
                evo_ev = next(e for e in pending_outbox if "evo_proposal" in e["idempotency_key"])
                msg_text, markup = format_notification_message(evo_ev["payload"])
                assert "演化繁衍提案审批" in msg_text
                assert "是否调整欲望评估指标：是" in msg_text
                assert markup["inline_keyboard"][0][0]["callback_data"] == "evo_approve:prop-good-01"

                # 4. Cooldown blocks second automatic ENTROPY_PRESSURE proposal within 6h
                r_cd = await client.post(
                    "/v5/evolution/proposals",
                    headers=auth_hdr,
                    json={
                        "proposal_key": "prop-cd-blocked",
                        "trigger_type": "ENTROPY_PRESSURE",
                        "trigger_reason": "too soon",
                        "hypothesis": "spam",
                        "genome_delta": valid_delta,
                        "auto_screen": False,
                    },
                )
                assert r_cd.status_code == 409

                # 5. InstanceSpawner(require_stage2_approval=True) BLOCKS spawning while STAGE2_PENDING_APPROVAL
                gc = CullingGC(repo=rt.evolution_repo, pg_admin_db=db, instances_root=tmp_path / "inst")
                spawner = InstanceSpawner(
                    settings=settings.evolution,
                    repo=rt.evolution_repo,
                    gc=gc,
                    require_stage2_approval=True,
                )
                prim_list = await rt.evolution_repo.list_instances(role="PRIMARY")
                blocked_spawn = await spawner.spawn(
                    SpawnRequest(
                        parent_instance_id=prim_list[0]["id"],
                        generation=1,
                        proposal_id=uuid.UUID(prop_ok["id"]),
                        instance_key="gen1-blocked",
                    )
                )
                assert blocked_spawn.success is False
                assert "Stage 2 human approval required" in (blocked_spawn.error or "")

                # 6. Unauthorized Telegram chat is rejected (403); authorized Telegram callback approves Stage 2
                with pytest.raises(TelegramAuthError):
                    await rt.telegram_webhook.handle_update(
                        {
                            "callback_query": {
                                "id": "cb-evil",
                                "from": {"id": 999, "username": "attacker"},
                                "message": {"chat": {"id": 666}},
                                "data": "evo_approve:prop-good-01",
                            }
                        },
                        secret_header="tg-webhook-sec",
                    )

                tg_res = await rt.telegram_webhook.handle_update(
                    {
                        "callback_query": {
                            "id": "cb-1",
                            "from": {"id": 888, "username": "joel"},
                            "message": {"chat": {"id": 777}},
                            "data": "evo_approve:prop-good-01",
                        }
                    },
                    secret_header="tg-webhook-sec",
                )
                assert tg_res["ok"] is True
                assert tg_res["result"]["stage"] == ProposalStage.STAGE2_APPROVED.value
                assert "telegram:joel(888)" in tg_res["result"]["approved_by"]

                # Now Stage 2 verification passes cleanly!
                verified = await rt.proposal_manager.verify_spawn_authorized("prop-good-01")
                assert verified["stage"] == ProposalStage.STAGE2_APPROVED.value

                # 7. Also verify TelegramPoller /evo_reject command path on a fresh proposal
                prop2 = await rt.proposal_manager.create_proposal(
                    parent_instance_id=prim_list[0]["id"],
                    target_generation=1,
                    trigger_type="OPERATOR_INITIATED",
                    trigger_reason="poller test",
                    hypothesis="test reject command",
                    genome_delta=valid_delta,
                    proposal_key="prop-reject-02",
                )
                await rt.proposal_manager.screen_proposal(prop2["id"])

                posted: list[dict[str, Any]] = []

                def _tg_handler(req: httpx.Request) -> httpx.Response:
                    posted.append({"url": str(req.url), "body": req.read().decode()})
                    return httpx.Response(200, json={"ok": True, "result": {}})

                async with httpx.AsyncClient(transport=httpx.MockTransport(_tg_handler)) as tg_hc:
                    notifier = TelegramNotifier(settings.telegram, http_client=tg_hc)
                    poller = TelegramPoller(
                        settings=settings.telegram,
                        notifier=notifier,
                        webhook_handler=rt.telegram_webhook,
                        human_bridge=rt.human_bridge,
                        repo=rt.repo,
                        proposal_manager=rt.proposal_manager,
                    )
                    poll_res = await poller.handle_polled_update(
                        {
                            "update_id": 1001,
                            "message": {
                                "from": {"id": 888, "username": "joel"},
                                "chat": {"id": 777},
                                "text": "/evo_reject prop-reject-02 暂不上线",
                            },
                        }
                    )
                assert poll_res["ok"] is True
                assert poll_res["result"]["stage"] == ProposalStage.STAGE2_REJECTED.value
                assert any("evo_reject" in p["body"] for p in posted)
    finally:
        await db.dispose()
