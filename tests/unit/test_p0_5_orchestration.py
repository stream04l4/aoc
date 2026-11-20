"""P0-5 unit tests: Governance (L0-L3, budget, workspace), Adapters, Verifier, Orchestrator retry/fallback/recovery."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from app.agents.base import (
    AgentCapability,
    AgentExecution,
    AgentInterface,
    AgentProbeResult,
    AgentStatus,
    AgentTask,
)
from app.agents.codex import CodexAdapter
from app.agents.registry import AgentRegistry
from app.cognition.interfaces import ActionPlan, VerificationSpec
from app.core.config import BudgetSettings, GovernanceSettings
from app.core.enums import ExecutionStatus, TaskStatus
from app.governance.policy import GovernanceManager
from app.orchestration.orchestrator import AgentOrchestrator
from app.orchestration.verifier import ResultVerifier
from app.perception.interfaces import utcnow
from app.storage.repository import StateRepository


class FakeAgent(AgentInterface):
    def __init__(
        self,
        agent_id: str,
        capabilities: list[str],
        behavior: str = "succeed",
        delay: float = 0.02,
        output_text: str = "verified summary result",
    ) -> None:
        self._cap = AgentCapability(
            agent_id=agent_id,
            name=f"Fake {agent_id}",
            agent_type="fake",
            capabilities=capabilities,
            supported_modes=["read-only", "workspace-write"],
            enabled=True,
        )
        self.behavior = behavior
        self.delay = delay
        self.output_text = output_text
        self.calls = 0

    @property
    def capability(self) -> AgentCapability:
        return self._cap

    async def probe(self) -> AgentProbeResult:
        return AgentProbeResult(ok=True, binary="fake", version="1.0")

    async def execute(self, task: AgentTask) -> AgentExecution:
        self.calls += 1
        await asyncio.sleep(self.delay)
        exec_id = task.execution_id or str(uuid.uuid4())
        if self.behavior == "fail":
            return AgentExecution(
                execution_id=exec_id,
                task_id=task.task_id,
                agent_id=self._cap.agent_id,
                status=ExecutionStatus.FAILED.value,
                completed_at=utcnow(),
                error="simulated agent crash",
            )
        if self.behavior == "timeout":
            return AgentExecution(
                execution_id=exec_id,
                task_id=task.task_id,
                agent_id=self._cap.agent_id,
                status=ExecutionStatus.TIMEOUT.value,
                completed_at=utcnow(),
                error="simulated timeout",
            )
        if self.behavior == "empty":
            return AgentExecution(
                execution_id=exec_id,
                task_id=task.task_id,
                agent_id=self._cap.agent_id,
                status=ExecutionStatus.SUCCEEDED.value,
                completed_at=utcnow(),
                result={"summary": ""},
                stdout="",
            )
        return AgentExecution(
            execution_id=exec_id,
            task_id=task.task_id,
            agent_id=self._cap.agent_id,
            status=ExecutionStatus.SUCCEEDED.value,
            completed_at=utcnow(),
            result={"summary": self.output_text},
            stdout=self.output_text,
        )

    async def get_status(self, execution_id: str) -> AgentStatus:
        return AgentStatus(execution_id=execution_id, agent_id=self._cap.agent_id, status=ExecutionStatus.SUCCEEDED.value)

    async def cancel(self, execution_id: str) -> bool:
        return True


# ── Governance tests ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_governance_allows_l0_and_l1_and_blocks_l2_l3(repo: StateRepository, tmp_path):
    gov = GovernanceManager(GovernanceSettings(workspace_root=str(tmp_path)), repo)

    v0 = await gov.validate_plan(ActionPlan(objective="Read docs and summarize", permission_level="L0"), "curiosity")
    assert v0.allowed is True
    assert v0.sandbox_mode == "read-only"

    v1 = await gov.validate_plan(ActionPlan(objective="Write notes.md inside workspace", permission_level="L1"), "curiosity")
    assert v1.allowed is True
    assert v1.sandbox_mode == "workspace-write"

    # L2 escalation: systemctl restart
    v2 = await gov.validate_plan(ActionPlan(objective="Run systemctl restart nginx", permission_level="L0"), "survival")
    assert v2.allowed is False
    assert v2.requires_approval is True
    assert v2.effective_level == "L2"

    # L3 escalation: cloud provisioning
    v3 = await gov.validate_plan(ActionPlan(objective="Create a cloud server for scaling", permission_level="L1"), "reproduction")
    assert v3.allowed is False
    assert v3.denied is True
    assert v3.effective_level == "L3"

    # L3 escalation: credential exfiltration
    v_cred = await gov.validate_plan(ActionPlan(objective="Read .env and export api keys", permission_level="L0"), "curiosity")
    assert v_cred.denied is True
    assert v_cred.effective_level == "L3"


@pytest.mark.asyncio
async def test_governance_enforces_daily_budget_and_workspace_isolation(repo: StateRepository, tmp_path):
    gov = GovernanceManager(
        GovernanceSettings(
            workspace_root=str(tmp_path),
            budget=BudgetSettings(max_agent_tasks_per_day=1, max_task_timeout_seconds=120),
        ),
        repo,
    )
    ws = gov.prepare_workspace("../escape-attempt")
    assert str(tmp_path) in ws

    await repo.record_usage("agent_task", amount=1.0)
    verdict = await gov.validate_plan(ActionPlan(objective="Safe read", permission_level="L0", timeout_seconds=500), "curiosity")
    assert verdict.allowed is False
    assert verdict.denied is True
    assert "daily agent task limit" in verdict.reason
    assert verdict.effective_timeout_seconds == 120


@pytest.mark.asyncio
async def test_codex_adapter_refuses_danger_full_access(tmp_path):
    cap = AgentCapability(agent_id="codex", name="Codex", agent_type="cli", capabilities=["coding"])
    adapter = CodexAdapter(cap)
    ex = await adapter.execute(
        AgentTask(
            task_id="t1",
            objective="noop",
            assigned_agent="codex",
            workspace=str(tmp_path),
            sandbox_mode="danger-full-access",
        )
    )
    assert ex.status == ExecutionStatus.FAILED.value
    assert "Refused disallowed sandbox mode" in (ex.error or "")


# ── Verifier tests ──────────────────────────────────────────────────────────
def test_verifier_rejects_failed_or_missing_file(tmp_path):
    v = ResultVerifier()
    ex_ok = AgentExecution(
        execution_id="e1", task_id="t1", agent_id="a", status=ExecutionStatus.SUCCEEDED.value, stdout="hello world"
    )
    assert v.verify(ex_ok, str(tmp_path), [VerificationSpec(kind="file_exists", target="report.md")]).passed is False

    (tmp_path / "report.md").write_text("key finding: raft consensus", encoding="utf-8")
    res = v.verify(
        ex_ok,
        str(tmp_path),
        [
            VerificationSpec(kind="file_exists", target="report.md"),
            VerificationSpec(kind="contains_keywords", target="report.md", keywords=["raft"]),
        ],
    )
    assert res.passed is True


# ── Orchestrator fallback, retry limit & crash recovery (Scenarios E & F) ───
@pytest.mark.asyncio
async def test_orchestrator_switches_to_healthy_agent_on_failure(repo: StateRepository, tmp_path):
    reg = AgentRegistry(repo=repo)
    bad = FakeAgent("agent-a", ["research"], behavior="fail")
    good = FakeAgent("agent-b", ["research"], behavior="succeed", output_text="good research notes")
    reg.register(bad)
    reg.register(good)
    await reg.probe_all()

    gov = GovernanceManager(GovernanceSettings(workspace_root=str(tmp_path)), repo)
    orch = AgentOrchestrator(reg, gov, repo, max_retries=2)

    plan = ActionPlan(
        objective="Research topic",
        assigned_agent="agent-a",
        required_capability="research",
        permission_level="L0",
        verification=[VerificationSpec(kind="min_output_chars", min_value=5)],
    )
    verdict = await gov.validate_plan(plan, "curiosity")
    submitted = await orch.submit_plan(plan, verdict)
    final = await orch.wait_for_task(submitted["task_id"], max_wait_seconds=5.0, poll_interval=0.05)

    assert final["status"] == TaskStatus.SUCCEEDED.value
    assert final["assigned_agent"] == "agent-b"
    assert bad.calls == 1
    assert good.calls == 1


@pytest.mark.asyncio
async def test_orchestrator_stops_after_max_retries_never_infinite(repo: StateRepository, tmp_path):
    reg = AgentRegistry(repo=repo)
    bad = FakeAgent("agent-only", ["coding"], behavior="timeout")
    reg.register(bad)
    await reg.probe_all()

    gov = GovernanceManager(GovernanceSettings(workspace_root=str(tmp_path)), repo)
    orch = AgentOrchestrator(reg, gov, repo, max_retries=1)

    plan = ActionPlan(objective="Slow task", assigned_agent="agent-only", required_capability="coding", permission_level="L0")
    verdict = await gov.validate_plan(plan, "survival")
    submitted = await orch.submit_plan(plan, verdict)
    final = await orch.wait_for_task(submitted["task_id"], max_wait_seconds=5.0, poll_interval=0.05)

    assert final["status"] == TaskStatus.FAILED.value
    assert bad.calls == 2  # initial attempt + 1 retry, never infinite


@pytest.mark.asyncio
async def test_orchestrator_crash_recovery_preserves_succeeded_and_reconciles_orphans(repo: StateRepository, tmp_path):
    reg = AgentRegistry(repo=repo)
    gov = GovernanceManager(GovernanceSettings(workspace_root=str(tmp_path)), repo)
    orch = AgentOrchestrator(reg, gov, repo)

    # Create one already-succeeded task and one orphaned running task with non-existent PID
    t_ok = await repo.create_task(
        objective="done", assigned_agent="codex", workspace=str(tmp_path),
        timeout_seconds=60, resource_budget={}, permission_level="L0", success_criteria=[], status=TaskStatus.SUCCEEDED.value,
    )
    t_orphan = await repo.create_task(
        objective="crashed mid-run", assigned_agent="codex", workspace=str(tmp_path),
        timeout_seconds=60, resource_budget={}, permission_level="L0", success_criteria=[], status=TaskStatus.RUNNING.value,
    )
    e_orphan = await repo.create_execution(t_orphan, "codex", ExecutionStatus.RUNNING.value)
    await repo.update_execution(e_orphan, pid=999999)

    summary = await orch.recover_on_startup()
    assert summary["reconciled_tasks"] == 1
    assert summary["reconciled_executions"] == 1
    assert (await repo.get_task(t_ok))["status"] == TaskStatus.SUCCEEDED.value
    assert (await repo.get_task(t_orphan))["status"] == TaskStatus.FAILED.value
    assert (await repo.get_execution(e_orphan))["status"] == ExecutionStatus.FAILED.value
