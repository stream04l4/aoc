"""P0-5 real external agent integration test (launches real codex exec in isolated workspace)."""

from __future__ import annotations

import shutil
import pytest

from app.agents.registry import AgentRegistry
from app.cognition.interfaces import ActionPlan, VerificationSpec
from app.core.config import GovernanceSettings
from app.core.enums import TaskStatus
from app.governance.policy import GovernanceManager
from app.orchestration.orchestrator import AgentOrchestrator
from app.storage.repository import StateRepository


@pytest.mark.real_agent
@pytest.mark.asyncio
async def test_real_codex_exec_bounded_l1_task(repo: StateRepository, tmp_path):
    if not shutil.which("codex"):
        pytest.skip("codex CLI not installed")

    registry = AgentRegistry(repo=repo)
    registry.load_from_config()
    probes = await registry.probe_all()
    assert probes["codex"].ok is True
    # Also verify hermes probe status is truthfully recorded (experimental)
    assert registry.get("hermes").capability.experimental is True

    gov = GovernanceManager(GovernanceSettings(workspace_root=str(tmp_path)), repo)
    orch = AgentOrchestrator(registry, gov, repo, max_retries=0)

    plan = ActionPlan(
        goal_title="P0-5 Codex Verification",
        objective="Create a file named aoc_probe.txt in the workspace containing the exact word AOC_VERIFIED and a 1-sentence summary of what Python asyncio does.",
        description="Bounded L1 verification task inside isolated workspace.",
        assigned_agent="codex",
        required_capability="coding",
        expected_output="aoc_probe.txt",
        timeout_seconds=120,
        permission_level="L1",
        success_criteria=["aoc_probe.txt exists and contains AOC_VERIFIED"],
        verification=[
            VerificationSpec(kind="file_exists", target="aoc_probe.txt"),
            VerificationSpec(kind="contains_keywords", target="aoc_probe.txt", keywords=["AOC_VERIFIED"]),
        ],
    )
    verdict = await gov.validate_plan(plan, "curiosity")
    assert verdict.allowed is True
    assert verdict.sandbox_mode == "workspace-write"

    submitted = await orch.submit_plan(plan, verdict)
    final = await orch.wait_for_task(submitted["task_id"], max_wait_seconds=130.0, poll_interval=1.0)
    assert final["status"] == TaskStatus.SUCCEEDED.value, f"Codex task failed: {final}"
    assert final["outcome"]["verification"]["passed"] is True
