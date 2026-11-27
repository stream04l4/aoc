"""V5.4 tests: Autonomous Self-Editing (wide_open policy), Self-Approval onto evo/auto, Dual Channels & Telegram."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.agents.base import AgentCapability, AgentExecution, AgentInterface, AgentProbeResult, AgentStatus, AgentTask
from app.agents.registry import AgentRegistry
from app.api.app import create_app
from app.cognition.interfaces import (
    ActionPlan,
    AssessmentResult,
    BrainInterface,
    CognitiveContext,
    EvaluationContext,
    EvaluationResult,
    GoalProposal,
    MetaReflectionContext,
    MetaReflectionOutput,
    SelfEditReviewContext,
    SelfEditReviewDecision,
    VerificationSpec,
)
from app.cognition.meta_reflection import MetaReflectionManager
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
    TelegramSettings,
)
from app.core.enums import ExecutionStatus, LifecycleState, PermissionLevel
from app.core.lifecycle import LifecycleManager
from app.core.loop import AutonomousLoop
from app.core.runtime import OrganismRuntime
from app.drives.engine import DriveEngine
from app.evolution.constitution_guard import CONSTITUTIONAL_FILES, ConstitutionGuard
from app.evolution.proposal_manager import ProposalManager
from app.evolution.repository import EvolutionRepository
from app.evolution.self_edit import SelfEditManager
from app.governance.approval import ApprovalService
from app.governance.policy import GovernanceManager
from app.memory.experience_manager import ExperienceManager
from app.notifications.human_bridge import HumanBridge
from app.notifications.telegram import TelegramNotifier, TelegramWebhookHandler, format_notification_message
from app.orchestration.backend import LocalSubprocessBackend
from app.orchestration.finalizer import TaskFinalizer
from app.orchestration.orchestrator import AgentOrchestrator
from app.perception.interfaces import Observation
from app.perception.manager import SensorManager
from app.storage.database import Database
from app.storage.models import Base, OutboxEvent
from app.storage.repository import StateRepository
from sqlalchemy import select
from scripts.v5_soak_check import run_v5_soak_check

PROJECT_ROOT = Path(__file__).resolve().parents[2]


async def _init_db(db: Database) -> None:
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _create_git_repo(repo_dir: Path, *, include_app_and_config: bool = True) -> str:
    repo_dir.mkdir(parents=True, exist_ok=True)
    _git(repo_dir, "init", "-b", "main")
    _git(repo_dir, "config", "user.name", "Test")
    _git(repo_dir, "config", "user.email", "test@aoc.local")

    if include_app_and_config:
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".venv", ".pytest_cache")
        shutil.copytree(PROJECT_ROOT / "app", repo_dir / "app", ignore=ignore)
        shutil.copytree(PROJECT_ROOT / "config", repo_dir / "config", ignore=ignore)

    (repo_dir / "app").mkdir(parents=True, exist_ok=True)
    (repo_dir / "app" / "__init__.py").write_text("", encoding="utf-8") if not (repo_dir / "app" / "__init__.py").exists() else None
    (repo_dir / "app" / "math_helper.py").write_text(
        "def add_numbers(a: int, b: int) -> int:\n    return a + b\n",
        encoding="utf-8",
    )
    (repo_dir / "tests" / "unit").mkdir(parents=True, exist_ok=True)
    (repo_dir / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (repo_dir / "tests" / "unit" / "__init__.py").write_text("", encoding="utf-8")
    (repo_dir / "tests" / "unit" / "test_math_helper.py").write_text(
        "from app.math_helper import add_numbers\n\ndef test_add():\n    assert add_numbers(2, 3) == 5\n",
        encoding="utf-8",
    )
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-m", "init repo")
    return _git(repo_dir, "rev-parse", "HEAD").stdout.strip()


@pytest.mark.asyncio
async def test_v5_4_wide_open_policy_allows_general_code_and_blocks_hard_locks(tmp_path: Path):
    """wide_open mode unlocks repo code/tests/configs while locking auth.py, lifecycle.py & secrets."""
    repo_root = tmp_path / "repo"
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".venv")
    shutil.copytree(PROJECT_ROOT / "app", repo_root / "app", ignore=ignore)
    shutil.copytree(PROJECT_ROOT / "config", repo_root / "config", ignore=ignore)

    strict_guard = ConstitutionGuard(repo_root=repo_root, policy_mode="strict")
    wide_guard = ConstitutionGuard(repo_root=repo_root, policy_mode="wide_open")
    assert len(CONSTITUTIONAL_FILES) == 20
    assert strict_guard.constitutional_manifest()["constitutional_file_count"] == 20
    assert wide_guard.constitutional_manifest()["policy_mode"] == "wide_open"

    # Strict rejects app/core/runtime.py, wide_open permits it
    rt_src = (repo_root / "app/core/runtime.py").read_text(encoding="utf-8") + "\n# harmless comment\n"
    strict_res = await strict_guard.evaluate({"files": ["app/core/runtime.py"], "file_overrides": {"app/core/runtime.py": rt_src}})
    assert strict_res.passed is False
    wide_res = await wide_guard.evaluate({"files": ["app/core/runtime.py"], "file_overrides": {"app/core/runtime.py": rt_src}})
    assert wide_res.passed is True, wide_res.violations

    # wide_open permits sensors.yaml & new unit test
    sensors_src = (repo_root / "config/sensors.yaml").read_text(encoding="utf-8")
    wide_test = await wide_guard.evaluate(
        {
            "files": ["config/sensors.yaml", "tests/unit/test_custom.py"],
            "file_overrides": {
                "config/sensors.yaml": sensors_src,
                "tests/unit/test_custom.py": "def test_ok():\n    assert True\n",
            },
        }
    )
    assert wide_test.passed is True, wide_test.violations

    # wide_open HARD-REJECTS auth.py, lifecycle.py, .env, .pem, and secret YAML keys
    for locked_path, override in [
        ("app/api/auth.py", "def noop(): pass\n"),
        ("app/core/lifecycle.py", "def noop(): pass\n"),
        (".env", "AOC_API__ADMIN_KEY=hack\n"),
        (".env.prod", "SECRET=1\n"),
        ("certs/server.pem", "CERT"),
    ]:
        bad = await wide_guard.evaluate({"files": [locked_path], "file_overrides": {locked_path: override}})
        assert bad.passed is False
        assert any(
            k in v
            for v in bad.violations
            for k in ("hard_locked_file_forbidden", "secret_credential_file_forbidden")
        ), (locked_path, bad.violations)

    bad_yaml = await wide_guard.evaluate(
        {
            "files": ["config/settings.yaml"],
            "file_overrides": {"config/settings.yaml": "api:\n  auth_enabled: false\n  admin_key: leaked\n"},
        }
    )
    assert bad_yaml.passed is False
    assert any("forbidden_secret_yaml_key" in v for v in bad_yaml.violations)


@pytest.mark.asyncio
async def test_v5_4_self_edit_end_to_end_worktree_pytest_critic_commit_evo_auto(tmp_path: Path):
    """Happy path: detached worktree -> real pytest -> Brain review -> self-approve commit onto evo/auto (main untouched)."""
    repo_root = tmp_path / "repo"
    main_sha = _create_git_repo(repo_root, include_app_and_config=True)
    ws_root = tmp_path / "workspaces"
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'se_e2e.db'}")
    await _init_db(db)

    review_calls: list[SelfEditReviewContext] = []

    class _CriticBrain(BrainInterface):
        async def assess(self, observation, memories, drives):
            return AssessmentResult(summary="ok")

        async def generate_goal(self, context):
            return GoalProposal(should_act=False)

        async def plan(self, context):
            return ActionPlan(should_execute=False)

        async def evaluate(self, context):
            return EvaluationResult(goal_achieved=True, summary="ok")

        async def review_self_edit(self, context: SelfEditReviewContext) -> SelfEditReviewDecision:
            review_calls.append(context)
            return SelfEditReviewDecision(
                approved=True,
                risk_level="low",
                summary_zh="新增 multiply_numbers 函数与单测验证通过，范围干净可控。",
                concerns=[],
                restart_required=True,
            )

    try:
        state_repo = StateRepository(db)
        evo_repo = EvolutionRepository(db)
        guard = ConstitutionGuard(repo_root=repo_root, policy_mode="wide_open")
        pm = ProposalManager(evo_repo=evo_repo, state_repo=state_repo, constitution_guard=guard)
        mgr = SelfEditManager(
            evo_repo=evo_repo,
            state_repo=state_repo,
            proposal_manager=pm,
            constitution_guard=guard,
            brain=_CriticBrain(),
            repo_root=repo_root,
            workspace_root=ws_root,
            enabled=True,
            auto_approve_enabled=True,
            policy_mode="wide_open",
            target_branch="evo/auto",
            cooldown_minutes=0,
        )

        meta = await mgr.prepare_codex_worktree(
            title="新增 multiply_numbers 能力与测试",
            hypothesis="Add bounded math helper function + unit test coverage",
            target_files=["app/math_helper.py", "tests/unit/test_math_helper.py"],
        )
        wt = Path(meta["worktree_path"])
        assert wt.is_dir()

        # Simulate Codex editing inside the isolated worktree (and leaving harmless .aoc_last_message.txt)
        (wt / ".aoc_last_message.txt").write_text("Done", encoding="utf-8")
        (wt / "app" / "math_helper.py").write_text(
            "def add_numbers(a: int, b: int) -> int:\n    return a + b\n\n"
            "def multiply_numbers(a: int, b: int) -> int:\n    return a * b\n",
            encoding="utf-8",
        )
        (wt / "tests" / "unit" / "test_math_helper.py").write_text(
            "from app.math_helper import add_numbers, multiply_numbers\n\n"
            "def test_add():\n    assert add_numbers(2, 3) == 5\n\n"
            "def test_multiply():\n    assert multiply_numbers(4, 5) == 20\n",
            encoding="utf-8",
        )

        res = await mgr.review_and_finalize_worktree(self_edit_meta=meta, execution_succeeded=True)
        assert res["ok"] is True
        assert res["committed"] is True
        assert res["branch"] == "evo/auto"
        assert res["restart_required"] is True
        assert len(review_calls) == 1
        assert "app/math_helper.py" in res["changed_files"]

        # Verify main untouched & clean, evo/auto advanced, rollback tag points to main_sha
        assert _git(repo_root, "rev-parse", "refs/heads/main").stdout.strip() == main_sha
        assert _git(repo_root, "rev-parse", "refs/heads/evo/auto").stdout.strip() == res["commit_sha"]
        assert _git(repo_root, "rev-parse", res["rollback_tag"]).stdout.strip() == main_sha
        assert _git(repo_root, "status", "--porcelain").stdout.strip() == ""

        # Zero orphan worktrees remain
        se_dir = ws_root / "self-edit"
        leftovers = [p for p in se_dir.iterdir() if p.is_dir()] if se_dir.exists() else []
        assert leftovers == []

        # Proposal & Outbox report card check
        prop = await evo_repo.get_proposal_by_key(res["proposal_key"])
        assert prop["stage"] == "STAGE2_APPROVED"
        assert prop["approved_by"] == "aoc:self_approver"

        async with db.session() as s:
            evs = (await s.execute(select(OutboxEvent))).scalars().all()
        se_evs = [e for e in evs if (e.payload or {}).get("kind") == "self_edit_report"]
        assert len(se_evs) == 1
        txt, markup = format_notification_message(se_evs[0].payload)
        assert "自主代码进化汇报" in txt
        assert "需重启生效" in txt
        assert markup["inline_keyboard"][0][0]["callback_data"] == f"self_edit_revert:{res['proposal_key']}"
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_v5_4_self_edit_fails_closed_on_pytest_failure_and_critic_veto(tmp_path: Path):
    """Failing pytest OR Critic veto blocks commit, keeps evo/auto untouched, and cleans worktrees."""
    repo_root = tmp_path / "repo"
    _create_git_repo(repo_root, include_app_and_config=True)
    ws_root = tmp_path / "workspaces"
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'se_fail.db'}")
    await _init_db(db)

    class _VetoBrain(BrainInterface):
        async def assess(self, observation, memories, drives):
            return AssessmentResult(summary="ok")

        async def generate_goal(self, context):
            return GoalProposal(should_act=False)

        async def plan(self, context):
            return ActionPlan(should_execute=False)

        async def evaluate(self, context):
            return EvaluationResult(goal_achieved=True, summary="ok")

        async def review_self_edit(self, context: SelfEditReviewContext) -> SelfEditReviewDecision:
            return SelfEditReviewDecision(
                approved=False,
                risk_level="high",
                summary_zh="语义偏离假设，拦截自审批",
                concerns=["change contradicts stated hypothesis"],
            )

    try:
        state_repo = StateRepository(db)
        evo_repo = EvolutionRepository(db)
        guard = ConstitutionGuard(repo_root=repo_root, policy_mode="wide_open")
        pm = ProposalManager(evo_repo=evo_repo, state_repo=state_repo, constitution_guard=guard)
        mgr = SelfEditManager(
            evo_repo=evo_repo,
            state_repo=state_repo,
            proposal_manager=pm,
            constitution_guard=guard,
            brain=_VetoBrain(),
            repo_root=repo_root,
            workspace_root=ws_root,
            enabled=True,
            auto_approve_enabled=True,
            policy_mode="wide_open",
            cooldown_minutes=0,
        )

        # 1. Failing pytest blocks at STAGE1_FAILED
        meta1 = await mgr.prepare_codex_worktree(
            title="Break math helper",
            hypothesis="Break add_numbers",
            target_files=["app/math_helper.py"],
            test_targets=["tests/unit/test_math_helper.py"],
        )
        (Path(meta1["worktree_path"]) / "app" / "math_helper.py").write_text(
            "def add_numbers(a: int, b: int) -> int:\n    return a - b\n",
            encoding="utf-8",
        )
        r1 = await mgr.review_and_finalize_worktree(self_edit_meta=meta1, execution_succeeded=True)
        assert r1["ok"] is False
        assert r1["committed"] is False
        assert r1["stage"] == "STAGE1_FAILED"
        assert subprocess.run(["git", "-C", str(repo_root), "rev-parse", "--verify", "refs/heads/evo/auto"], capture_output=True).returncode != 0

        # 2. Passing pytest + Critic veto blocks at STAGE2_REJECTED
        meta2 = await mgr.prepare_codex_worktree(
            title="Valid syntax vetoed by Critic",
            hypothesis="Veto test",
            target_files=["app/math_helper.py"],
            test_targets=["tests/unit/test_math_helper.py"],
        )
        (Path(meta2["worktree_path"]) / "app" / "math_helper.py").write_text(
            "def add_numbers(a: int, b: int) -> int:\n    return int(a) + int(b)\n",
            encoding="utf-8",
        )
        r2 = await mgr.review_and_finalize_worktree(self_edit_meta=meta2, execution_succeeded=True)
        assert r2["ok"] is False
        assert r2["committed"] is False
        assert r2["stage"] == "STAGE2_REJECTED"
        assert r2["concerns"] == ["change contradicts stated hypothesis"]

        se_dir = ws_root / "self-edit"
        leftovers = [p for p in se_dir.iterdir() if p.is_dir()] if se_dir.exists() else []
        assert leftovers == []
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_v5_4_channel1_meta_reflection_direct_and_queue_without_human_card(tmp_path: Path):
    """Meta-Reflection L2 routes direct patches onto evo/auto & broader tasks into self_edit_queue without ask_human cards."""
    repo_root = tmp_path / "repo"
    _create_git_repo(repo_root, include_app_and_config=True)
    ws_root = tmp_path / "workspaces"
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'se_ch1.db'}")
    await _init_db(db)

    class _StubCritic(BrainInterface):
        def __init__(self, l2_payload: dict[str, Any]) -> None:
            self._l2 = l2_payload

        async def assess(self, observation, memories, drives):
            return AssessmentResult(summary="ok")

        async def generate_goal(self, context):
            return GoalProposal(should_act=False)

        async def plan(self, context):
            return ActionPlan(should_execute=False)

        async def evaluate(self, context):
            return EvaluationResult(goal_achieved=True, summary="ok")

        async def reflect_on_system(self, context: MetaReflectionContext) -> MetaReflectionOutput:
            assert context.self_edit_auto_approve_enabled is True
            assert context.self_edit_policy_mode == "wide_open"
            return MetaReflectionOutput(
                diagnosis_summary="Adjust reproduction base_urgency slightly & enqueue complex orchestrator optimization",
                root_causes=["test root cause"],
                l2_upgrade_proposal=self._l2,
                confidence=0.85,
            )

    try:
        state_repo = StateRepository(db)
        evo_repo = EvolutionRepository(db)
        guard = ConstitutionGuard(repo_root=repo_root, policy_mode="wide_open")
        pm = ProposalManager(evo_repo=evo_repo, state_repo=state_repo, constitution_guard=guard)
        hb = HumanBridge(repo=state_repo, enabled=True, cooldown_minutes=1)

        async def _fast_pytest(_wt: Path) -> tuple[bool, str]:
            return True, "2 passed in 0.05s"

        se_mgr = SelfEditManager(
            evo_repo=evo_repo,
            state_repo=state_repo,
            proposal_manager=pm,
            constitution_guard=guard,
            brain=_StubCritic({}),
            repo_root=repo_root,
            workspace_root=ws_root,
            enabled=True,
            auto_approve_enabled=True,
            policy_mode="wide_open",
            cooldown_minutes=0,
            pytest_runner_fn=_fast_pytest,
        )

        # Case A: structured YAML change auto-commits cleanly without ask_human card
        brain_a = _StubCritic(
            {
                "title": "微调繁殖基础紧迫度",
                "reason": "提升繁殖演进活跃度",
                "expected_benefit": "加快系统自我优化",
                "rollback_plan": "还原 evo-rollback tag",
                "proposed_change": {
                    "target_file": "config/drives.yaml",
                    "change_type": "yaml",
                    "yaml_updates": {"reproduction": {"base_urgency": 0.14}},
                    "hypothesis": "Increase reproduction base_urgency within constitutional bounds",
                },
            }
        )
        mr_a = MetaReflectionManager(
            repo=state_repo,
            brain=brain_a,
            human_bridge=hb,
            enabled=True,
            l2_cooldown_hours=1,
            self_edit_manager=se_mgr,
            self_edit_enabled=True,
            self_edit_auto_approve_enabled=True,
            self_edit_policy_mode="wide_open",
        )
        res_a = await mr_a.run_if_due(force=True)
        assert res_a["l2_outcome"]["self_edit"] is True
        assert res_a["l2_outcome"]["committed"] is True
        assert res_a["l2_outcome"]["branch"] == "evo/auto"
        assert await state_repo.list_world_items(kind="human_outreach_card") == []

        # Case B: broader code improvement (no exact replacement) -> enqueued into self_edit_queue
        await state_repo.upsert_world_item(
            kind="meta_reflection_state",
            key="primary",
            value={"last_l2_proposal_at": None},
            status="active",
            source="test",
        )
        brain_b = _StubCritic(
            {
                "title": "优化 math_helper 边界处理",
                "reason": "需要 Codex 全量分析与补充测试",
                "desired_change": "为 app/math_helper.py 增加安全乘法函数与边界测试",
                "target_files": ["app/math_helper.py", "tests/unit/test_math_helper.py"],
                "test_plan": "tests/unit/test_math_helper.py",
                "rollback_plan": "撤销 evo/auto 对应提交",
            }
        )
        mr_b = MetaReflectionManager(
            repo=state_repo,
            brain=brain_b,
            human_bridge=hb,
            enabled=True,
            l2_cooldown_hours=1,
            self_edit_manager=se_mgr,
            self_edit_enabled=True,
            self_edit_auto_approve_enabled=True,
            self_edit_policy_mode="wide_open",
        )
        res_b = await mr_b.run_if_due(force=True)
        assert res_b["l2_outcome"]["mode"] == "queued_for_codex"
        assert res_b["l2_outcome"]["enqueued"] is True
        pending = await se_mgr.list_pending_self_edits(limit=5)
        assert len(pending) == 1
        assert pending[0]["title"] == "优化 math_helper 边界处理"
        assert await state_repo.list_world_items(kind="human_outreach_card") == []
    finally:
        await db.dispose()


class _FakeSelfEditAgent(AgentInterface):
    def __init__(self) -> None:
        self._cap = AgentCapability(
            agent_id="codex",
            name="Fake Codex",
            agent_type="cli",
            capabilities=["coding", "general_task", "system_diagnosis"],
            supported_modes=["read-only", "workspace-write"],
            enabled=True,
            healthy=True,
        )

    @property
    def capability(self) -> AgentCapability:
        return self._cap

    async def probe(self) -> AgentProbeResult:
        return AgentProbeResult(ok=True, binary="codex", version="fake")

    async def execute(self, task: AgentTask) -> AgentExecution:
        ws = Path(task.workspace)
        (ws / "app" / "math_helper.py").write_text(
            "def add_numbers(a: int, b: int) -> int:\n    return a + b\n\n"
            "def subtract_numbers(a: int, b: int) -> int:\n    return a - b\n",
            encoding="utf-8",
        )
        (ws / "tests" / "unit" / "test_math_helper.py").write_text(
            "from app.math_helper import add_numbers, subtract_numbers\n\n"
            "def test_add():\n    assert add_numbers(2, 3) == 5\n\n"
            "def test_sub():\n    assert subtract_numbers(5, 2) == 3\n",
            encoding="utf-8",
        )
        now = datetime.now(timezone.utc)
        return AgentExecution(
            execution_id=task.execution_id,
            task_id=task.task_id,
            agent_id="codex",
            status=ExecutionStatus.SUCCEEDED.value,
            started_at=now,
            completed_at=now,
            result={"summary": "Implemented subtract_numbers + unit tests cleanly", "workspace_files": ["app/math_helper.py"]},
            stdout="Implemented subtract_numbers cleanly",
        )

    async def get_status(self, execution_id: str) -> AgentStatus:
        return AgentStatus(execution_id=execution_id, agent_id="codex", status=ExecutionStatus.SUCCEEDED.value)

    async def cancel(self, execution_id: str) -> bool:
        return False


@pytest.mark.asyncio
async def test_v5_4_channel2_loop_end_to_end_and_telegram_revert(tmp_path: Path):
    """Channel 2: AutonomousLoop -> Orchestrator (worktree) -> Finalizer (self-approve onto evo/auto) -> Telegram revert."""
    repo_root = tmp_path / "repo"
    main_sha = _create_git_repo(repo_root, include_app_and_config=True)
    ws_root = tmp_path / "workspaces"
    ws_root.mkdir(parents=True, exist_ok=True)
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'se_ch2.db'}")
    await _init_db(db)

    seen_contexts: list[CognitiveContext] = []

    class _SelfEditBrain(BrainInterface):
        async def assess(self, observation, memories, drives):
            return AssessmentResult(summary="Ready to evolve codebase")

        async def generate_goal(self, context: CognitiveContext) -> GoalProposal:
            seen_contexts.append(context)
            q_item = context.pending_self_edits[0]
            return GoalProposal(
                should_act=True,
                title=f"自改实现：{q_item['title']}",
                description=q_item["desired_change"],
                drive_type="reproduction",
                related_key=q_item["queue_key"],
                success_criteria=["All unit tests pass"],
                predicted_ig=0.8,
            )

        async def plan(self, context: CognitiveContext) -> ActionPlan:
            q_item = context.pending_self_edits[0]
            return ActionPlan(
                should_execute=True,
                goal_title=context.goal.title,
                objective="Add subtract_numbers helper and tests in isolated self-edit worktree",
                description="Edit app/math_helper.py and tests/unit/test_math_helper.py",
                execution_mode="self_edit",
                assigned_agent="codex",
                required_capability="coding",
                expected_output="Updated math helper + tests",
                timeout_seconds=120,
                permission_level=PermissionLevel.L1.value,
                success_criteria=["pytest passes"],
                verification=[VerificationSpec(kind="non_empty_output")],
                self_edit_spec={
                    "target_files": q_item["target_files"],
                    "hypothesis": "Provide subtract_numbers helper",
                    "change_summary": "Add subtract_numbers function and unit test",
                    "test_targets": ["tests/unit/test_math_helper.py"],
                    "queue_key": q_item["queue_key"],
                },
            )

        async def evaluate(self, context: EvaluationContext) -> EvaluationResult:
            return EvaluationResult(goal_achieved=True, summary="Verified self-edit completed", information_gain=0.8)

        async def review_self_edit(self, context: SelfEditReviewContext) -> SelfEditReviewDecision:
            return SelfEditReviewDecision(
                approved=True,
                risk_level="low",
                summary_zh="已增加 subtract_numbers 函数并通过单测",
                concerns=[],
                restart_required=True,
            )

    try:
        settings = Settings(
            api=ApiSettings(auth_enabled=True, admin_key=SecretStr("k")),
            database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path / 'se_ch2.db'}"),
            governance=GovernanceSettings(auto_approve_max_level="L2", workspace_root=str(ws_root)),
            evolution=EvolutionSettings(repo_root=str(repo_root), instances_root=str(tmp_path / "inst")),
            perception=PerceptionSettings(llm_probe=False),
            kernel_v3=KernelV3Settings(
                self_edit_enabled=True,
                self_edit_auto_approve_enabled=True,
                self_edit_policy_mode="wide_open",
                self_edit_branch="evo/auto",
                self_edit_cooldown_minutes=0,
                plan_preflight_enabled=True,
                goal_dedup_enabled=False,
            ),
            telegram=TelegramSettings(
                enabled=True,
                bot_token=SecretStr("123456:FAKE"),
                webhook_secret=SecretStr("sec"),
                allowed_chat_ids=["42"],
            ),
            social=SocialSettings(enabled=False),
        )
        state_repo = StateRepository(db)
        evo_repo = EvolutionRepository(db)
        identity = await state_repo.get_or_create_identity(name="AOC-001", version="1.0.0")
        lifecycle = LifecycleManager(identity["id"], state_repo)
        await lifecycle.transition(LifecycleState.ACTIVE, reason="test startup")

        guard = ConstitutionGuard(repo_root=repo_root, policy_mode="wide_open")
        pm = ProposalManager(evo_repo=evo_repo, state_repo=state_repo, constitution_guard=guard)
        brain = _SelfEditBrain()
        se_mgr = SelfEditManager(
            evo_repo=evo_repo,
            state_repo=state_repo,
            proposal_manager=pm,
            constitution_guard=guard,
            brain=brain,
            repo_root=repo_root,
            workspace_root=ws_root,
            enabled=True,
            auto_approve_enabled=True,
            policy_mode="wide_open",
            target_branch="evo/auto",
            cooldown_minutes=0,
        )
        enq = await se_mgr.enqueue_self_edit(
            title="增加减法辅助函数",
            target_files=["app/math_helper.py", "tests/unit/test_math_helper.py"],
            desired_change="Add subtract_numbers with unit test",
            priority=3,
        )

        # Perception & Drives configured so reproduction wins cleanly
        class _DummySensor:
            name = "dummy"

            async def observe(self) -> Observation:
                return Observation(
                    timestamp=datetime.now(timezone.utc),
                    system_state={
                        "disk": {"free_ratio": 0.70, "paths": {"/": {"free_ratio": 0.70}}},
                        "memory": {"available_ratio": 0.70},
                        "cpu": {"load_per_core": 0.2},
                        "llm": {"available": True},
                        "database": {"ok": True},
                    },
                    environment_state={"unknown_topics": []},
                    available_resources={
                        "agents": {"enabled_count": 1, "healthy_count": 1, "capabilities": ["coding", "general_task"]},
                        "budget": {"tasks_used": 0, "tasks_limit": 100, "tokens_used": 0, "tokens_limit": 1_000_000},
                    },
                    confidence=1.0,
                )

        perception = SensorManager(repo=state_repo)
        perception.register(_DummySensor())
        drives = DriveEngine(config={
            "arbitration": {"weights": {"survival": 1.0, "reproduction": 0.9, "curiosity": 0.1}, "idle_threshold": 0.05},
            "reproduction": {"base_urgency": 0.45},
        })
        governance = GovernanceManager(settings.governance, state_repo)
        reg = AgentRegistry(repo=state_repo)
        reg.register(_FakeSelfEditAgent(), probe=AgentProbeResult(ok=True, binary="codex", version="fake"))
        orch = AgentOrchestrator(registry=reg, governance=governance, repo=state_repo, backend=LocalSubprocessBackend())
        mem = ExperienceManager(state_repo, backend=None, outbox_enabled=False, embed_on_write=False)
        fin = TaskFinalizer(
            organism_id=identity["id"],
            repo=state_repo,
            perception=perception,
            drives=drives,
            brain=brain,
            governance=governance,
            memory=mem,
            verifier=orch.verifier,
            self_edit_manager=se_mgr,
            skill_auto_crystallize=False,
        )
        loop = AutonomousLoop(
            organism_id=identity["id"],
            settings=settings,
            repo=state_repo,
            lifecycle=lifecycle,
            perception=perception,
            drives=drives,
            brain=brain,
            governance=governance,
            registry=reg,
            orchestrator=orch,
            memory=mem,
            finalizer=fin,
            self_edit_manager=se_mgr,
        )

        cycle = await loop.run_cycle()
        assert cycle["outcome"] == "SUCCEEDED"
        assert len(seen_contexts) >= 1 and seen_contexts[0].self_edit_enabled is True
        assert await se_mgr.list_pending_self_edits(limit=5) == []  # queue consumed!

        se_state = await se_mgr.get_state()
        assert se_state["last_stage"] == "SELF_APPROVED_COMMITTED"
        assert se_state["pending_restart"] is True
        pkey = se_state["last_proposal_key"]
        assert _git(repo_root, "rev-parse", "refs/heads/main").stdout.strip() == main_sha
        assert _git(repo_root, "rev-parse", "refs/heads/evo/auto").stdout.strip() == se_state["last_commit_sha"]

        # Zero orphan worktrees remain
        se_dir = ws_root / "self-edit"
        leftovers = [p for p in se_dir.iterdir() if p.is_dir()] if se_dir.exists() else []
        assert leftovers == []

        # Telegram /evo_auto_status + inline self_edit_revert:<pkey> reverts evo/auto cleanly
        posted_msgs: list[tuple[str, dict[str, Any]]] = []

        class _StubNotifier(TelegramNotifier):
            async def _post(self, method: str, payload: dict[str, Any], *, timeout_seconds: float | None = None):
                posted_msgs.append((method, payload))
                return {"ok": True, "result": {"message_id": len(posted_msgs)}}

        notifier = _StubNotifier(settings.telegram)
        approval_svc = ApprovalService(state_repo, orch)
        wh = TelegramWebhookHandler(
            settings.telegram,
            approval_svc,
            state_repo,
            notifier=notifier,
            self_edit_manager=se_mgr,
        )

        st_res = await wh.handle_update(
            {"message": {"from": {"id": 9, "username": "joel"}, "chat": {"id": 42}, "text": "/evo_auto_status"}},
            secret_header="sec",
        )
        assert st_res["kind"] == "self_edit_status"
        assert any("evo/auto" in m[1].get("text", "") for m in posted_msgs)

        rev_res = await wh.handle_update(
            {
                "callback_query": {
                    "id": "cb1",
                    "from": {"id": 9, "username": "joel"},
                    "message": {"chat": {"id": 42}, "message_id": 10},
                    "data": f"self_edit_revert:{pkey}",
                }
            },
            secret_header="sec",
        )
        assert rev_res["ok"] is True
        assert _git(repo_root, "rev-parse", "refs/heads/evo/auto").stdout.strip() == main_sha
        assert _git(repo_root, "rev-parse", "refs/heads/main").stdout.strip() == main_sha
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_v5_4_control_center_and_soak_check_hygiene(tmp_path: Path):
    """Config Center preserves 12 domains + V5.4 flags; /self-evolution exposes V5.4 state; soak check detects orphan self-edit worktree."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'v54_api.db'}"
    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    inst_root = tmp_path / "inst"
    inst_root.mkdir()

    settings = Settings(
        api=ApiSettings(auth_enabled=True, admin_key=SecretStr("v54-test-key")),
        database=DatabaseSettings(url=db_url),
        governance=GovernanceSettings(workspace_root=str(ws_root)),
        memory=MemorySettings(provider="none"),
        embedding=EmbeddingSettings(provider="hash_fallback"),
        perception=PerceptionSettings(llm_probe=False),
        runner=RunnerSettings(enabled=False),
        evolution=EvolutionSettings(instances_root=str(inst_root), repo_root=str(PROJECT_ROOT)),
        kernel_v3=KernelV3Settings(
            self_edit_enabled=True,
            self_edit_auto_approve_enabled=True,
            self_edit_policy_mode="wide_open",
            self_edit_branch="evo/auto",
        ),
        social=SocialSettings(enabled=False),
    )

    class _MinBrain(BrainInterface):
        async def assess(self, observation, memories, drives):
            return AssessmentResult(summary="ok")

        async def generate_goal(self, context):
            return GoalProposal(should_act=False)

        async def plan(self, context):
            return ActionPlan(should_execute=False)

        async def evaluate(self, context):
            return EvaluationResult(goal_achieved=True, summary="ok")

    async def _probe():
        return {"available": True, "latency_ms": 1.0, "model": "stub"}

    db_obj = Database(db_url)
    await _init_db(db_obj)
    rt = OrganismRuntime(settings=settings, db=db_obj, brain=_MinBrain(), auto_load_agents=False, llm_probe_fn=_probe)
    app = create_app(settings=settings, db=db_obj, runtime=rt, start_scheduler=False)
    try:
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                hdrs = {"Authorization": "Bearer v54-test-key"}
                cfg_r = await client.get("/v2/control-center/config", headers=hdrs)
                assert cfg_r.status_code == 200
                domains = cfg_r.json()["domains"]
                assert len(domains) == 12
                all_keys = {it["key"]: it["effective_value"] for d in domains for it in d["items"]}
                assert all_keys["kernel_v3.self_edit_enabled"] is True
                assert all_keys["kernel_v3.self_edit_auto_approve_enabled"] is True
                assert all_keys["kernel_v3.self_edit_policy_mode"] == "wide_open"
                assert all_keys["kernel_v3.self_edit_branch"] == "evo/auto"

                se_r = await client.get("/v5/observability/self-evolution?hours=24", headers=hdrs)
                assert se_r.status_code == 200
                se_data = se_r.json()
                assert set(se_data["feature_flags"].keys()) == {
                    "brain_research_enabled",
                    "interest_portfolio_enabled",
                    "explicit_skill_refactor_enabled",
                    "reproduction_quality_scoring_enabled",
                    "meta_reflection_enabled",
                }
                assert se_data["v5_4_feature_flags"]["self_edit_enabled"] is True
                assert se_data["self_edit"]["enabled"] is True
                assert se_data["self_edit"]["policy_mode"] == "wide_open"
    finally:
        await db_obj.dispose()

    clean_soak = await run_v5_soak_check(
        repo_root=PROJECT_ROOT,
        instances_root=inst_root,
        min_free_disk_ratio=0.01,
        db_url=db_url,
    )
    assert clean_soak["steady_state_ok"] is True
    assert clean_soak["checks"]["v5_4_self_edit"]["ok"] is True

    (inst_root / "self-edit" / "orphan-se-worktree").mkdir(parents=True)
    dirty_soak = await run_v5_soak_check(
        repo_root=PROJECT_ROOT,
        instances_root=inst_root,
        min_free_disk_ratio=0.01,
        db_url=db_url,
    )
    assert dirty_soak["steady_state_ok"] is False
    assert dirty_soak["checks"]["v5_4_self_edit"]["ok"] is False
    assert any("Orphan self-edit worktrees" in f for f in dirty_soak["failures"])
