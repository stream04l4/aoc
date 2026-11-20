"""P0-7 Closed-Loop tests covering Scenarios A, B, C, D, E, F and API controls."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.agents.base import (
    AgentCapability,
    AgentExecution,
    AgentInterface,
    AgentProbeResult,
    AgentStatus,
    AgentTask,
)
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
    VerificationSpec,
)
from app.core.config import ApiSettings, GovernanceSettings, MemorySettings, PerceptionSettings, RuntimeSettings, Settings
from app.core.enums import DriveType, ExecutionStatus, LifecycleState, MemoryType, TaskStatus
from app.core.runtime import OrganismRuntime
from app.drives.interfaces import DriveState, KnowledgeGap
from app.perception.interfaces import Observation, utcnow
from app.storage.database import Database


class ScriptedBrain(BrainInterface):
    """Deterministic brain for unit/simulation testing of the autonomous loop."""

    def __init__(self, plan_level: str = "L1", expected_imp: float = 0.50) -> None:
        self.plan_level = plan_level
        self.expected_imp = expected_imp
        self.calls: list[str] = []

    async def assess(self, observation: Observation, memories: list[dict], drives: list[DriveState]) -> AssessmentResult:
        self.calls.append("assess")
        topics = (observation.environment_state or {}).get("unknown_topics") or []
        gaps = [KnowledgeGap(topic=t["topic"], importance=float(t.get("importance", 0.9))) for t in topics]
        return AssessmentResult(summary="Assessed state", knowledge_gaps=gaps, confidence=0.95)

    async def generate_goal(self, context: CognitiveContext) -> GoalProposal:
        self.calls.append("generate_goal")
        d = context.decision.selected_drive
        if d == DriveType.CURIOSITY.value:
            topics = (context.observation.environment_state or {}).get("unknown_topics") or []
            key = topics[0]["topic"] if topics else "general-topic"
            return GoalProposal(
                should_act=True,
                title=f"Research {key}",
                description=f"Investigate {key} and summarize findings",
                drive_type=d,
                related_key=key,
                success_criteria=["non-empty research summary"],
                expected_outcome={"expected_curiosity_gain": self.expected_imp},
            )
        if d == DriveType.REPRODUCTION.value:
            return GoalProposal(
                should_act=True,
                title="Build system_diagnosis skill",
                description="Document reusable system diagnosis procedure",
                drive_type=d,
                related_key="system_diagnosis",
                success_criteria=["skill documented"],
                expected_outcome={"new_skill": "system_diagnosis"},
            )
        return GoalProposal(
            should_act=True,
            title="Diagnose survival pressure",
            description="Inspect disk/service health read-only",
            drive_type=d,
            success_criteria=["diagnostic summary"],
            expected_outcome={"diagnosed": True},
        )

    async def plan(self, context: CognitiveContext) -> ActionPlan:
        self.calls.append("plan")
        goal = context.goal
        return ActionPlan(
            should_execute=True,
            goal_title=goal.title if goal else "Task",
            objective=goal.description if goal else "Do task",
            assigned_agent="worker-1",
            required_capability="research",
            expected_output="summary",
            timeout_seconds=60,
            permission_level=self.plan_level,
            success_criteria=["non-empty output"],
            verification=[VerificationSpec(kind="min_output_chars", min_value=5)],
            expected_improvement=self.expected_imp,
        )

    async def evaluate(self, context: EvaluationContext) -> EvaluationResult:
        self.calls.append("evaluate")
        ok = context.verification_passed and context.execution.get("status") == "SUCCEEDED"
        drive = context.goal.get("drive_type")
        key = context.goal.get("related_key")
        return EvaluationResult(
            goal_achieved=ok,
            summary="Verified output produced" if ok else "Execution failed",
            information_gain=0.6 if (ok and drive == "curiosity") else 0.0,
            learned_facts=[f"Learned core concepts of {key}"] if (ok and drive == "curiosity") else [],
            acquired_skill=key if (ok and drive == "reproduction") else None,
            resolved_topic=key if (ok and drive == "curiosity") else None,
            should_persist_memory=True,
            memory_type=MemoryType.SEMANTIC.value if drive == "curiosity" else MemoryType.EPISODIC.value,
            memory_importance=0.85,
        )


class StubAgent(AgentInterface):
    def __init__(self, agent_id: str, succeed: bool = True) -> None:
        self._cap = AgentCapability(
            agent_id=agent_id,
            name=agent_id,
            agent_type="stub",
            capabilities=["coding", "research", "system_diagnosis"],
            supported_modes=["read-only", "workspace-write"],
            enabled=True,
        )
        self.succeed = succeed
        self.runs = 0

    @property
    def capability(self) -> AgentCapability:
        return self._cap

    async def probe(self) -> AgentProbeResult:
        return AgentProbeResult(ok=True, binary="stub", version="1.0")

    async def execute(self, task: AgentTask) -> AgentExecution:
        self.runs += 1
        eid = task.execution_id or str(uuid.uuid4())
        if not self.succeed:
            return AgentExecution(
                execution_id=eid,
                task_id=task.task_id,
                agent_id=self._cap.agent_id,
                status=ExecutionStatus.FAILED.value,
                completed_at=utcnow(),
                error="stub failure",
            )
        return AgentExecution(
            execution_id=eid,
            task_id=task.task_id,
            agent_id=self._cap.agent_id,
            status=ExecutionStatus.SUCCEEDED.value,
            completed_at=utcnow(),
            result={"summary": f"Completed: {task.objective}"},
            stdout=f"Completed: {task.objective}",
        )

    async def get_status(self, execution_id: str) -> AgentStatus:
        return AgentStatus(execution_id=execution_id, agent_id=self._cap.agent_id, status="SUCCEEDED")

    async def cancel(self, execution_id: str) -> bool:
        return True


def _make_runtime(db: Database, tmp_path, brain=None, agents=None) -> OrganismRuntime:
    settings = Settings(
        runtime=RuntimeSettings(cycle_interval_seconds=0.2, idle_sleep_seconds=0.2, monitor_poll_seconds=0.05, max_wait_per_cycle_seconds=5),
        perception=PerceptionSettings(disk_paths=["/"], llm_probe=False),
        memory=MemorySettings(provider="none"),
        governance=GovernanceSettings(workspace_root=str(tmp_path)),
    )
    reg = AgentRegistry()
    for a in (agents or [StubAgent("worker-1", succeed=True)]):
        reg.register(a)

    async def fake_llm_ok():
        return {"available": True, "latency_ms": 150.0}

    rt = OrganismRuntime(
        settings=settings,
        db=db,
        brain=brain or ScriptedBrain(),
        registry=reg,
        auto_load_agents=False,
        llm_probe_fn=fake_llm_ok,
    )
    reg.repo = rt.repo
    return rt


@pytest.mark.asyncio
async def test_scenario_a_survival_disk_pressure(db: Database, tmp_path):
    rt = _make_runtime(db, tmp_path)
    await rt.start(start_scheduler=False)
    try:
        await rt.repo.upsert_world_item("simulation", "disk_pressure", {"free_ratio": 0.02}, status="active", is_simulated=True)
        rec = await rt.loop.run_cycle()
        assert rec["decision"]["selected_drive"] == "survival"
        assert rec["decision"]["constraints"].get("emergency_mode") is True
        assert rec["outcome"] == "SUCCEEDED"
        assert rt.lifecycle.state == LifecycleState.CRITICAL
    finally:
        await rt.stop()


@pytest.mark.asyncio
async def test_scenario_b_curiosity_resolves_topic_and_recalculates_from_perception(db: Database, tmp_path):
    # Brain claims huge expected_improvement=0.95, but satisfaction MUST come from re-perception!
    brain = ScriptedBrain(expected_imp=0.95)
    rt = _make_runtime(db, tmp_path, brain=brain)
    await rt.start(start_scheduler=False)
    try:
        # Make reproduction already satisfied so curiosity cleanly wins
        for cap in ("coding", "research", "repository_analysis", "system_diagnosis", "knowledge_summarization"):
            await rt.repo.upsert_world_item("capability", cap, {"verified": True}, status="acquired")
        for i in range(4):
            await rt.repo.upsert_world_item(
                "knowledge_topic", f"ebpf-topic-{i}", {"description": "Learn eBPF", "importance": 0.95}, status="open"
            )
        rec = await rt.loop.run_cycle()
        assert rec["decision"]["selected_drive"] == "curiosity"
        assert rec["outcome"] == "SUCCEEDED"

        # Verify Expected vs Actual vs Measured are all recorded separately (task book 9.1)
        ev = rec["evaluation"]
        assert ev["expected_outcome"]["expected_improvement"] == 0.95
        assert ev["actual_outcome"]["goal_achieved"] is True
        measured = ev["measured_satisfaction_change"]["curiosity"]
        # Re-perceived change is real & bounded (definitely NOT blindly +0.95)
        assert measured != 0.95
        assert measured > 0.0

        # Verify semantic memory saved
        mems = await rt.memory.recall(query="ebpf", memory_types=[MemoryType.SEMANTIC.value])
        assert len(mems) >= 1
    finally:
        await rt.stop()


@pytest.mark.asyncio
async def test_scenario_c_reproduction_virtual_capability_only(db: Database, tmp_path):
    # Agent has no capabilities listed, no skills yet, no unknown topics -> reproduction wins
    worker = StubAgent("worker-1", succeed=True)
    worker._cap = worker._cap.model_copy(update={"capabilities": []})
    rt = _make_runtime(db, tmp_path, agents=[worker])
    await rt.start(start_scheduler=False)
    try:
        # Pre-seed recent learning so curiosity stays satisfied
        rt.loop._last_learning_at = utcnow()
        rt.loop._last_info_gain = 0.9
        rt.loop._last_info_gain_at = utcnow()
        for i in range(5):
            await rt.repo.add_memory_index(
                memory_type=MemoryType.SEMANTIC.value, title=f"k{i}", summary=f"fact {i}",
                content_hash=f"h{i}", source="seed", importance=0.8, tags=[], sync_status="synced"
            )
        rec = await rt.loop.run_cycle()
        assert rec["decision"]["selected_drive"] == "reproduction"
        assert rec["outcome"] == "SUCCEEDED"
        caps = await rt.repo.list_world_items(kind="capability", status="acquired")
        assert any(c["key"] == "system_diagnosis" for c in caps)
        assert await rt.repo.count_memories(MemoryType.PROCEDURAL.value) >= 1
    finally:
        await rt.stop()


@pytest.mark.asyncio
async def test_scenario_d_idle_when_all_drives_satisfied(db: Database, tmp_path):
    worker = StubAgent("worker-1", succeed=True)
    rt = _make_runtime(db, tmp_path, agents=[worker])
    await rt.start(start_scheduler=False)
    try:
        for cap in ("coding", "research", "repository_analysis", "system_diagnosis", "knowledge_summarization"):
            await rt.repo.upsert_world_item("capability", cap, {}, status="acquired")
        for i in range(10):
            await rt.repo.add_memory_index(
                memory_type=MemoryType.PROCEDURAL.value, title=f"p{i}", summary=f"skill {i}",
                content_hash=f"ph{i}", source="seed", importance=0.8, tags=[], sync_status="synced"
            )
        for i in range(5):
            await rt.repo.add_memory_index(
                memory_type=MemoryType.SEMANTIC.value, title=f"s{i}", summary=f"fact {i}",
                content_hash=f"sh{i}", source="seed", importance=0.8, tags=[], sync_status="synced"
            )
        rt.loop._last_learning_at = utcnow()
        rt.loop._last_info_gain = 0.95
        rt.loop._last_info_gain_at = utcnow()
        rt.memory._available = True

        rec = await rt.loop.run_cycle()
        assert rec["decision"]["selected_drive"] == "none"
        assert rec["outcome"] == "IDLE"
        assert worker.runs == 0  # NO forced task!
        assert rt.lifecycle.state == LifecycleState.IDLE
    finally:
        await rt.stop()


@pytest.mark.asyncio
async def test_scenario_f_agent_failure_never_marked_achieved_and_switches_agent(db: Database, tmp_path):
    bad = StubAgent("worker-1", succeed=False)
    good = StubAgent("worker-2", succeed=True)
    rt = _make_runtime(db, tmp_path, agents=[bad, good])
    await rt.start(start_scheduler=False)
    try:
        for i in range(3):
            await rt.repo.upsert_world_item("knowledge_topic", f"t-{i}", {"importance": 0.95}, status="open")
        rec = await rt.loop.run_cycle()
        assert rec["outcome"] == "SUCCEEDED"
        assert bad.runs == 1
        assert good.runs == 1

        # Now make ALL agents fail -> must fail cleanly, never declare success
        good.succeed = False
        rec2 = await rt.loop.run_cycle()
        assert rec2["outcome"] == "ACTION_FAILED"
        assert rec2["evaluation"]["goal_achieved"] is False
    finally:
        await rt.stop()


@pytest.mark.asyncio
async def test_api_pause_resume_and_l2_human_approval(db: Database, tmp_path):
    admin_token = "test-admin-token-p0-7"
    headers = {"Authorization": f"Bearer {admin_token}"}
    rt = _make_runtime(db, tmp_path, brain=ScriptedBrain(plan_level="L2"))
    rt.settings = rt.settings.model_copy(
        update={"api": ApiSettings(host="127.0.0.1", auth_enabled=True, admin_key=admin_token)}
    )
    app = create_app(settings=rt.settings, runtime=rt, start_scheduler=False)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post("/control/pause", headers=headers, json={"reason": "maintenance"})
            assert r.status_code == 200
            assert r.json()["lifecycle_state"] == "PAUSED"

            r = await client.post("/control/resume", headers=headers, json={"reason": "done"})
            assert r.status_code == 200
            assert r.json()["lifecycle_state"] == "ACTIVE"

            # Inject topics -> trigger cycle -> L2 plan must pause at REQUIRES_APPROVAL
            for i in range(3):
                await client.post(
                    "/world",
                    headers=headers,
                    json={"kind": "knowledge_topic", "key": f"l2-topic-{i}", "value": {"importance": 0.95}},
                )
            r = await client.post("/control/cycle", headers=headers)
            assert r.status_code == 200
            assert r.json()["outcome"] == "REQUIRES_APPROVAL"

            pending = (await client.get("/tasks?status=REQUIRES_APPROVAL", headers=headers)).json()
            assert len(pending) == 1
            tid = pending[0]["task_id"]

            r = await client.post(f"/approvals/{tid}/reject", headers=headers, json={"reason": "not needed"})
            assert r.status_code == 200
            assert r.json()["status"] == "REJECTED"
