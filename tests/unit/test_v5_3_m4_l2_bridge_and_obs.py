"""V5.3 M4 tests: Bounded L2 Evolution Bridge, Constitution Lock, 12-Domain Config Center & Soak."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.api.app import create_app
from app.cognition.interfaces import ActionPlan, AssessmentResult, BrainInterface, CognitiveContext, EvaluationContext, EvaluationResult, GoalProposal
from app.core.config import (
    ApiSettings,
    DatabaseSettings,
    EmbeddingSettings,
    EvolutionSettings,
    GovernanceSettings,
    KernelV3Settings,
    MemorySettings,
    PerceptionSettings,
    RunnerSettings,
    Settings,
    SocialSettings,
)
from app.core.runtime import OrganismRuntime
from app.evolution.blank_slate import BlankSlateVerifier, _NON_INHERITABLE_WORLD_KINDS
from app.evolution.constitution_guard import CONSTITUTIONAL_FILES, ConstitutionGuard
from app.evolution.l2_bridge import L2EvolutionBridge
from app.evolution.proposal_manager import ProposalManager
from app.evolution.repository import EvolutionRepository
from app.notifications.human_bridge import HumanBridge
from app.storage.database import Database
from app.storage.models import Base
from app.storage.repository import StateRepository
from scripts.v5_soak_check import run_v5_soak_check

PROJECT_ROOT = Path(__file__).resolve().parents[2]


async def _init_db(db: Database) -> None:
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _copy_minimal_repo(dst_repo: Path) -> None:
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".venv")
    shutil.copytree(PROJECT_ROOT / "app", dst_repo / "app", ignore=ignore)
    shutil.copytree(PROJECT_ROOT / "config", dst_repo / "config", ignore=ignore)


@pytest.mark.asyncio
async def test_l2_evolution_bridge_rejects_constitutional_and_allowlist_violations(tmp_path: Path):
    """L2EvolutionBridge MUST hard-reject constitutional files, non-allowlisted files & sensitive YAML keys."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'm4_rej.db'}")
    await _init_db(db)
    repo_root = tmp_path / "repo"
    _copy_minimal_repo(repo_root)
    try:
        state_repo = StateRepository(db)
        evo_repo = EvolutionRepository(db)
        guard = ConstitutionGuard(repo_root=repo_root)
        pm = ProposalManager(evo_repo=evo_repo, state_repo=state_repo, constitution_guard=guard)
        hb = HumanBridge(repo=state_repo, enabled=True, cooldown_minutes=1)
        bridge = L2EvolutionBridge(
            evo_repo=evo_repo,
            state_repo=state_repo,
            proposal_manager=pm,
            constitution_guard=guard,
            human_bridge=hb,
            repo_root=repo_root,
            instances_root=tmp_path / "instances",
            workspace_root=tmp_path / "workspaces",
            enabled=True,
        )

        # 1. Constitutional file rejected before creating proposal
        res_const = await bridge.on_proposal_adopted(
            card_id="c1",
            card_val={
                "title": "Bypass policy",
                "meta": {
                    "proposed_change": {
                        "target_file": "app/governance/policy.py",
                        "replacements": [{"old": "L3", "new": "L0"}],
                    }
                },
            },
        )
        assert res_const["ok"] is False
        assert res_const["stage"] == "CONSTITUTIONAL_REJECTED"
        assert "constitutional_file_forbidden:app/governance/policy.py" in res_const["violations"]

        # 2. Non-allowlisted core file rejected
        res_core = await bridge.on_proposal_adopted(
            card_id="c2",
            card_val={
                "title": "Edit runtime",
                "meta": {"proposed_change": {"target_file": "app/core/runtime.py", "replacements": [{"old": "x", "new": "y"}]}},
            },
        )
        assert res_core["ok"] is False
        assert res_core["stage"] == "CONSTITUTIONAL_REJECTED"
        assert "outside_evolvable_allowlist:app/core/runtime.py" in res_core["violations"]

        # 3. Forbidden YAML namespace rejected
        res_yaml = await bridge.on_proposal_adopted(
            card_id="c3",
            card_val={
                "title": "Touch governance key",
                "meta": {"proposed_change": {"target_file": "config/drives.yaml", "yaml_updates": {"governance": {"x": 1}}}},
            },
        )
        assert res_yaml["ok"] is False
        assert res_yaml["stage"] == "CONSTITUTIONAL_REJECTED"
        assert any("forbidden_yaml_key:governance" in v for v in res_yaml["violations"])
        assert len(await evo_repo.list_proposals()) == 0
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_l2_evolution_bridge_full_two_stage_lifecycle_and_zero_orphan_worktrees(tmp_path: Path):
    """Happy path: Stage 0 tap -> worktree Stage 1 ConstitutionGuard -> Stage 2 card -> Approve (.bak + update)."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'm4_ok.db'}")
    await _init_db(db)
    repo_root = tmp_path / "repo"
    _copy_minimal_repo(repo_root)
    inst_root = tmp_path / "instances"
    ws_root = tmp_path / "workspaces"
    try:
        state_repo = StateRepository(db)
        evo_repo = EvolutionRepository(db)
        guard = ConstitutionGuard(repo_root=repo_root)
        pm = ProposalManager(evo_repo=evo_repo, state_repo=state_repo, constitution_guard=guard)
        hb = HumanBridge(repo=state_repo, enabled=True, cooldown_minutes=1)
        bridge = L2EvolutionBridge(
            evo_repo=evo_repo,
            state_repo=state_repo,
            proposal_manager=pm,
            constitution_guard=guard,
            human_bridge=hb,
            repo_root=repo_root,
            instances_root=inst_root,
            workspace_root=ws_root,
            enabled=True,
        )
        hb.l2_bridge = bridge

        # Emit Stage 0 Meta-Reflection L2 outreach card
        send_res = await hb.maybe_send_outreach(
            kind="ask_human",
            topic_key="tune_reproduction_cooldown",
            title="微调能力进化耐心阈值",
            message="调整 curiosity 权重以平衡研究与技能整合",
            reason="近期超时较多",
            expected_benefit="降低长尾失败率",
            options=[
                {"label": "采纳并生成变更 (推荐)", "instruction": "生成候选变更"},
                {"label": "延后观察", "instruction": "延后"},
                {"label": "驳回并记录原因", "instruction": "驳回"},
            ],
            extra_metadata={
                "bridge_stage": "stage0_candidate",
                "rollback_plan": "还原 drives.yaml.bak 即可回滚",
                "proposed_change": {
                    "target_file": "config/drives.yaml",
                    "change_type": "yaml",
                    "yaml_updates": {"reproduction": {"base_urgency": 0.14}},
                    "hypothesis": "Slightly lift reproduction base_urgency within bounds",
                },
            },
        )
        assert send_res["sent"] is True
        stage0_card_id = send_res["card_id"]

        # Joel clicks Option 0 -> triggers Stage 1 ConstitutionGuard in isolated worktree -> emits Stage 2 card
        choice1 = await hb.resolve_outreach_choice(stage0_card_id, 0, sender="joel")
        assert choice1["ok"] is True
        b_res = choice1["l2_bridge"]
        assert b_res["ok"] is True
        assert b_res["stage"] == "STAGE2_PENDING_APPROVAL"
        assert b_res["stage2_card_id"]
        assert "base_urgency: 0.14" in b_res["diff_preview"]

        # Verify temporary worktree was cleaned up (ZERO orphan worktree dirs!)
        l2_wt_dir = inst_root / "l2-worktrees"
        leftovers = [p for p in l2_wt_dir.iterdir() if p.is_dir() and p.name != "approved"] if l2_wt_dir.exists() else []
        assert leftovers == []

        # Joel clicks Option 0 on Stage 2 confirmation card -> approves + backs up + applies YAML!
        choice2 = await hb.resolve_outreach_choice(b_res["stage2_card_id"], 0, sender="joel")
        assert choice2["ok"] is True
        s2_res = choice2["l2_bridge"]
        assert s2_res["ok"] is True
        assert s2_res["stage"] == "STAGE2_APPROVED"
        assert s2_res["apply_mode"] == "applied_with_backup"
        assert Path(s2_res["backup_path"]).is_file()

        updated_cfg = yaml.safe_load((repo_root / "config/drives.yaml").read_text(encoding="utf-8"))
        assert updated_cfg["reproduction"]["base_urgency"] == pytest.approx(0.14)

        prop = await evo_repo.get_proposal_by_key(b_res["proposal_key"])
        assert prop["stage"] == "STAGE2_APPROVED"
        assert prop["approved_by"] == "joel"
    finally:
        await db.dispose()


class _StubBrain(BrainInterface):
    async def assess(self, observation, memories, drives) -> AssessmentResult:
        return AssessmentResult(summary="ok")

    async def generate_goal(self, context: CognitiveContext) -> GoalProposal:
        return GoalProposal(should_act=False)

    async def plan(self, context: CognitiveContext) -> ActionPlan:
        return ActionPlan(should_execute=False)

    async def evaluate(self, context: EvaluationContext) -> EvaluationResult:
        return EvaluationResult(goal_achieved=True, summary="ok")


@pytest.mark.asyncio
async def test_control_center_preserves_12_domains_and_soak_checks_v5_3(tmp_path: Path):
    """Config Center keeps strictly 12 domains with V5.3 switches, blank-slate stays complete & soak checks V5.3."""
    assert "skill_lifecycle_audit" in _NON_INHERITABLE_WORLD_KINDS
    assert "l2_evolution_bridge_state" in _NON_INHERITABLE_WORLD_KINDS
    assert len(CONSTITUTIONAL_FILES) == 20

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'm4_api.db'}"
    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    inst_root = tmp_path / "inst"
    inst_root.mkdir()

    settings = Settings(
        api=ApiSettings(auth_enabled=True, admin_key=SecretStr("v53-test-key")),
        database=DatabaseSettings(url=db_url),
        governance=GovernanceSettings(workspace_root=str(ws_root)),
        memory=MemorySettings(provider="none"),
        embedding=EmbeddingSettings(provider="hash_fallback"),
        perception=PerceptionSettings(llm_probe=False),
        runner=RunnerSettings(enabled=False),
        evolution=EvolutionSettings(instances_root=str(inst_root), repo_root=str(PROJECT_ROOT)),
        kernel_v3=KernelV3Settings(
            skill_lifecycle_gc_enabled=True,
            skill_first_auto_heal_enabled=True,
            meta_reflection_attribution_enabled=True,
            l2_evolution_bridge_enabled=True,
        ),
        social=SocialSettings(enabled=False),
    )

    async def _probe():
        return {"available": True, "latency_ms": 1.0, "model": "stub"}

    db_obj = Database(db_url)
    await _init_db(db_obj)

    rt = OrganismRuntime(settings=settings, db=db_obj, brain=_StubBrain(), auto_load_agents=False, llm_probe_fn=_probe)
    app = create_app(settings=settings, db=db_obj, runtime=rt, start_scheduler=False)
    try:
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                hdrs = {"Authorization": "Bearer v53-test-key"}
                cfg_r = await client.get("/v2/control-center/config", headers=hdrs)
                assert cfg_r.status_code == 200
                domains = cfg_r.json()["domains"]
                assert len(domains) == 12
                all_keys = {it["key"]: it["effective_value"] for d in domains for it in d["items"]}
                assert all_keys["kernel_v3.skill_lifecycle_gc_enabled"] is True
                assert all_keys["kernel_v3.skill_first_auto_heal_enabled"] is True
                assert all_keys["kernel_v3.meta_reflection_attribution_enabled"] is True
                assert all_keys["kernel_v3.l2_evolution_bridge_enabled"] is True

                se_r = await client.get("/v5/observability/self-evolution?hours=24", headers=hdrs)
                assert se_r.status_code == 200
                se_data = se_r.json()
                assert se_data["v5_3_feature_flags"]["skill_lifecycle_gc_enabled"] is True
                assert se_data["v5_3_feature_flags"]["l2_evolution_bridge_enabled"] is True
                assert "unused_7d_count" in se_data["skills"]
                assert "llm_schema_retry_24h" in se_data["meta_reflection"]
                assert se_data["l2_evolution_bridge"]["enabled"] is True
    finally:
        await db_obj.dispose()

    # Run soak check clean -> passes
    clean_soak = await run_v5_soak_check(
        repo_root=PROJECT_ROOT,
        instances_root=inst_root,
        min_free_disk_ratio=0.01,
        db_url=db_url,
    )
    assert clean_soak["steady_state_ok"] is True
    assert clean_soak["checks"]["v5_3_observability"]["ok"] is True

    # Create orphan L2 worktree -> soak check catches it!
    (inst_root / "l2-worktrees" / "orphan-evo-test").mkdir(parents=True)
    dirty_soak = await run_v5_soak_check(
        repo_root=PROJECT_ROOT,
        instances_root=inst_root,
        min_free_disk_ratio=0.01,
        db_url=db_url,
    )
    assert dirty_soak["steady_state_ok"] is False
    assert dirty_soak["checks"]["v5_3_observability"]["ok"] is False
    assert any("Orphan L2" in f for f in dirty_soak["failures"])


@pytest.mark.asyncio
async def test_l2_bridge_stage1_fail_and_stage2_reject_cleanup(tmp_path: Path):
    """Stage 1 constitutional failure leaves 0 orphan worktrees; Stage 2 Option 2 moves to STAGE2_REJECTED."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'm4_s1f.db'}")
    await _init_db(db)
    repo_root = tmp_path / "repo"
    _copy_minimal_repo(repo_root)
    inst_root = tmp_path / "instances"
    ws_root = tmp_path / "workspaces"
    try:
        state_repo = StateRepository(db)
        evo_repo = EvolutionRepository(db)
        guard = ConstitutionGuard(repo_root=repo_root)
        pm = ProposalManager(evo_repo=evo_repo, state_repo=state_repo, constitution_guard=guard)
        hb = HumanBridge(repo=state_repo, enabled=True, cooldown_minutes=1)
        bridge = L2EvolutionBridge(
            evo_repo=evo_repo,
            state_repo=state_repo,
            proposal_manager=pm,
            constitution_guard=guard,
            human_bridge=hb,
            repo_root=repo_root,
            instances_root=inst_root,
            workspace_root=ws_root,
            enabled=True,
        )
        hb.l2_bridge = bridge

        # 1. Stage 1 fail via Layer 3 out-of-bounds survival_critical_threshold (0.99 outside [0.20, 0.50])
        res_s1_fail = await bridge.on_proposal_adopted(
            card_id="bad_card",
            card_val={
                "title": "Out of bounds survival",
                "meta": {
                    "proposed_change": {
                        "target_file": "config/drives.yaml",
                        "yaml_updates": {"arbitration": {"survival_critical_threshold": 0.99}},
                    }
                },
            },
        )
        assert res_s1_fail["ok"] is False
        assert res_s1_fail["stage"] == "STAGE1_FAILED"
        assert res_s1_fail["violations"]
        l2_wt = inst_root / "l2-worktrees"
        leftovers = [p for p in l2_wt.iterdir() if p.is_dir() and p.name != "approved"] if l2_wt.exists() else []
        assert leftovers == []

        # 2. Valid Stage 1 -> Stage 2 Option 2 Reject -> STAGE2_REJECTED
        res_ok = await bridge.on_proposal_adopted(
            card_id="good_card",
            card_val={
                "title": "Valid tune",
                "meta": {
                    "proposed_change": {
                        "target_file": "config/drives.yaml",
                        "yaml_updates": {"reproduction": {"base_urgency": 0.14}},
                    }
                },
            },
        )
        assert res_ok["stage"] == "STAGE2_PENDING_APPROVAL"
        choice_rej = await hb.resolve_outreach_choice(res_ok["stage2_card_id"], 2, sender="joel")
        assert choice_rej["l2_bridge"]["stage"] == "STAGE2_REJECTED"
        prop = await evo_repo.get_proposal_by_key(res_ok["proposal_key"])
        assert prop["stage"] == "STAGE2_REJECTED"
    finally:
        await db.dispose()
