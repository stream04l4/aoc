"""P1-2 Durable Task State Machine, TaskFinalizer, Outbox/Inbox, and V2 Task API tests."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, inspect

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
from app.core.config import (
    ApiSettings,
    GovernanceSettings,
    MemorySettings,
    PerceptionSettings,
    RuntimeSettings,
    Settings,
)
from app.core.enums import ExecutionStatus, GoalStatus, MemoryType, TaskStatus
from app.core.runtime import OrganismRuntime
from app.orchestration.state_machine import (
    InvalidStateTransitionError,
    StateVersionConflictError,
    TaskStateMachine,
)
from app.perception.interfaces import utcnow
from app.storage.database import Database
from app.storage.outbox import OutboxDispatcher, OutboxEventType
from app.storage.repository import StateRepository
from tests.unit.test_p0_7_loop import ScriptedBrain


class MultiCycleAgent(AgentInterface):
    """Agent that completes only when explicitly released via an asyncio.Event (spans 3+ cycles)."""

    def __init__(self, agent_id: str = "worker-1") -> None:
        self._cap = AgentCapability(
            agent_id=agent_id,
            name=agent_id,
            agent_type="stub",
            capabilities=["coding", "research", "system_diagnosis"],
            supported_modes=["read-only", "workspace-write"],
            enabled=True,
        )
        self.release_event = asyncio.Event()
        self.started_event = asyncio.Event()
        self.runs = 0

    @property
    def capability(self) -> AgentCapability:
        return self._cap

    async def probe(self) -> AgentProbeResult:
        return AgentProbeResult(ok=True, binary="stub", version="1.0")

    async def execute(self, task: AgentTask) -> AgentExecution:
        self.runs += 1
        self.started_event.set()
        await self.release_event.wait()
        eid = task.execution_id or str(uuid.uuid4())
        return AgentExecution(
            execution_id=eid,
            task_id=task.task_id,
            agent_id=self._cap.agent_id,
            status=ExecutionStatus.SUCCEEDED.value,
            completed_at=utcnow(),
            result={"summary": f"Cross-cycle verified findings for: {task.objective}"},
            stdout=f"Cross-cycle verified findings for: {task.objective}",
        )

    async def get_status(self, execution_id: str) -> AgentStatus:
        return AgentStatus(execution_id=execution_id, agent_id=self._cap.agent_id, status="RUNNING")

    async def cancel(self, execution_id: str) -> bool:
        self.release_event.set()
        return True


def _make_runtime(db: Database, tmp_path, agent: AgentInterface, max_wait: float = 0.08, brain=None) -> OrganismRuntime:
    settings = Settings(
        api=ApiSettings(host="127.0.0.1", auth_enabled=True, admin_key="p1-2-admin"),
        runtime=RuntimeSettings(
            cycle_interval_seconds=0.05,
            idle_sleep_seconds=0.05,
            monitor_poll_seconds=0.02,
            max_wait_per_cycle_seconds=max_wait,
        ),
        perception=PerceptionSettings(disk_paths=["/"], llm_probe=False),
        memory=MemorySettings(provider="none"),
        governance=GovernanceSettings(workspace_root=str(tmp_path)),
    )
    reg = AgentRegistry()
    reg.register(agent)

    async def llm_ok():
        return {"available": True, "latency_ms": 50.0}

    rt = OrganismRuntime(
        settings=settings,
        db=db,
        brain=brain or ScriptedBrain(plan_level="L1"),
        registry=reg,
        auto_load_agents=False,
        llm_probe_fn=llm_ok,
    )
    reg.repo = rt.repo
    return rt


@pytest.mark.asyncio
async def test_state_machine_legal_edges_idempotent_replay_and_illegal_rejection(repo: StateRepository, tmp_path):
    sm = TaskStateMachine(repo)
    tid = await repo.create_task(
        objective="Test SM",
        assigned_agent="worker-1",
        workspace=str(tmp_path),
        timeout_seconds=60,
        resource_budget={},
        permission_level="L0",
        success_criteria=[],
        status=TaskStatus.CREATED.value,
    )

    r1 = await sm.transition(tid, TaskStatus.QUEUED, idempotency_key="sm-q-1", reason="queued")
    assert r1.to_status == TaskStatus.QUEUED.value
    assert r1.state_version == 2
    assert r1.noop is False

    # Replaying the exact same idempotency_key is a safe no-op without bumping state_version
    r1_dup = await sm.transition(tid, TaskStatus.QUEUED, idempotency_key="sm-q-1", reason="queued")
    assert r1_dup.noop is True
    assert r1_dup.state_version == 2

    # Optimistic locking detects stale expected_version
    with pytest.raises(StateVersionConflictError):
        await sm.transition(tid, TaskStatus.DISPATCHING, expected_version=1)

    await sm.transition(tid, TaskStatus.DISPATCHING, expected_version=2)
    await sm.transition(tid, TaskStatus.RUNNING)
    await sm.transition(tid, TaskStatus.RESULT_RECEIVED)
    await sm.transition(tid, TaskStatus.VERIFYING)
    r_done = await sm.transition(tid, TaskStatus.SUCCEEDED)
    assert r_done.state_version == 7

    # Illegal transition out of terminal SUCCEEDED must raise & write error audit event without mutating DB
    with pytest.raises(InvalidStateTransitionError):
        await sm.transition(tid, TaskStatus.RUNNING)

    task_after = await repo.get_task(tid)
    assert task_after["status"] == TaskStatus.SUCCEEDED.value
    assert task_after["state_version"] == 7

    events = await repo.list_task_lifecycle_events(tid)
    assert [e["to_status"] for e in events] == [
        "QUEUED",
        "DISPATCHING",
        "RUNNING",
        "RESULT_RECEIVED",
        "VERIFYING",
        "SUCCEEDED",
    ]
    err_logs = await repo.list_events(event_type="task.invalid_transition")
    assert len(err_logs) == 1


@pytest.mark.asyncio
async def test_cross_cycle_inflight_task_spans_3_cycles_and_finalizes_completely(db: Database, tmp_path):
    """Task spans Cycle 1 (IN_FLIGHT) -> Cycle 2 (still running) -> Cycle 3 (still running) -> Cycle 4 (finalized)."""
    agent = MultiCycleAgent("worker-1")
    rt = _make_runtime(db, tmp_path, agent, max_wait=0.05)
    await rt.start(start_scheduler=False)

    try:
        for cap in ("coding", "research", "repository_analysis", "system_diagnosis", "knowledge_summarization"):
            await rt.repo.upsert_world_item("capability", cap, {}, status="acquired")
        await rt.repo.upsert_world_item(
            "knowledge_topic", "raft-snapshot-compaction", {"description": "Study Raft compaction", "importance": 0.98}, status="open"
        )
        await rt.repo.upsert_world_item(
            "knowledge_topic", "mvcc-vacuum", {"description": "Study MVCC vacuum", "importance": 0.90}, status="open"
        )
        await rt.repo.upsert_world_item(
            "knowledge_topic", "ebpf-kprobes", {"description": "Study eBPF", "importance": 0.90}, status="open"
        )

        # Cycle 1: dispatches task, hits max_wait_per_cycle_seconds -> ends cleanly as IN_FLIGHT
        c1 = await rt.loop.run_cycle()
        assert c1["outcome"] == "IN_FLIGHT"
        task_id = c1["state_changes"]["task_id"]
        assert rt.orchestrator.has_inflight() is True

        # Cycle 2 & Cycle 3: task is STILL running in background; cycles run without blocking or double-dispatching
        c2 = await rt.loop.run_cycle()
        c3 = await rt.loop.run_cycle()
        assert c2["outcome"] == "BUDGET_DEFERRED"  # concurrency=1 prevents double-dispatch
        assert c3["outcome"] == "BUDGET_DEFERRED"
        assert agent.runs == 1

        # Now release the long-running task so it finishes cleanly
        agent.release_event.set()
        await asyncio.sleep(0.05)

        # Cycle 4: _poll_existing_inflight() collects result and invokes unified TaskFinalizer!
        await rt.loop.run_cycle()

        # Verify complete end-to-end closure for the cross-cycle task:
        # 1. Task & Execution both finalized
        task_row = await rt.repo.get_task(uuid.UUID(task_id))
        assert task_row["status"] == TaskStatus.SUCCEEDED.value
        assert task_row["finalized_at"] is not None
        exec_id = task_row["outcome"]["execution_id"]
        exec_row = await rt.repo.get_execution(uuid.UUID(exec_id))
        assert exec_row["finalized_at"] is not None

        # 2. Originating Goal transitioned to ACHIEVED (not stuck ACTIVE)
        goal_row = await rt.repo.get_goal(uuid.UUID(task_row["goal_id"]))
        assert goal_row["status"] == GoalStatus.ACHIEVED.value

        # 3. World State updated (topic resolved)
        resolved = await rt.repo.list_world_items(kind="knowledge_topic", status="resolved")
        assert len(resolved) >= 1

        # 4. Memories written (episodic + semantic) and Letta outbox enqueued
        mems = await rt.repo.list_memory_index(limit=20)
        mtypes = {m["memory_type"] for m in mems}
        assert MemoryType.EPISODIC.value in mtypes
        assert MemoryType.SEMANTIC.value in mtypes

        # 5. Originating Cycle 1 backfilled with evaluation, post_drives, and SUCCEEDED outcome
        c1_after = await rt.repo.get_cycle(uuid.UUID(c1["cycle_id"]))
        assert c1_after["outcome"] == "SUCCEEDED"
        assert "measured_satisfaction_change" in c1_after["evaluation"]
        assert c1_after["drives_post"]

        # 6. Lifecycle events audit trail persisted
        lc_events = await rt.repo.list_task_lifecycle_events(uuid.UUID(task_id))
        assert lc_events[-1]["to_status"] == TaskStatus.SUCCEEDED.value
    finally:
        agent.release_event.set()
        await rt.stop()


@pytest.mark.asyncio
async def test_finalizer_reconstructs_context_after_loop_destroy_and_is_idempotent_across_10_replays(
    db: Database, tmp_path
):
    """Fault injection: destroy AutonomousLoop memory while task is IN_FLIGHT, rebuild from scratch,
    finalize purely from DB, then replay finalize_execution() 10 times -> strictly zero duplicates!
    """
    agent = MultiCycleAgent("worker-1")
    rt1 = _make_runtime(db, tmp_path, agent, max_wait=0.05)
    await rt1.start(start_scheduler=False)

    for cap in ("coding", "research", "repository_analysis", "system_diagnosis", "knowledge_summarization"):
        await rt1.repo.upsert_world_item("capability", cap, {}, status="acquired")
    for i in range(3):
        await rt1.repo.upsert_world_item("knowledge_topic", f"crash-topic-{i}", {"importance": 0.95}, status="open")

    c1 = await rt1.loop.run_cycle()
    assert c1["outcome"] == "IN_FLIGHT"
    task_id = c1["state_changes"]["task_id"]

    # Let execution finish and transition to SUCCEEDED inside orchestrator, BEFORE finalizing
    agent.release_event.set()
    final_task = await rt1.orchestrator.wait_for_task(task_id, max_wait_seconds=5.0, poll_interval=0.02)
    assert final_task["status"] == TaskStatus.SUCCEEDED.value
    exec_id = final_task["outcome"]["execution_id"]

    # Destroy rt1 completely (all in-memory cycle variables lost!)
    await rt1.stop()

    # Build a brand-new OrganismRuntime reading the same DB
    rt2 = _make_runtime(db, tmp_path, MultiCycleAgent("worker-1"), max_wait=0.05)
    await rt2.start(start_scheduler=False)
    try:
        # 1st call: reconstructs context purely from DB and executes full finalization
        r_first = await rt2.finalizer.finalize_execution(exec_id)
        assert r_first.already_finalized is False
        assert r_first.goal_achieved is True
        assert len(r_first.memory_ids) >= 2

        mem_count_after_1 = len(await rt2.repo.list_memory_index(limit=100))
        goal_hist_after_1 = len(await rt2.repo.goal_history(uuid.UUID(r_first.goal_id)))

        # Replay finalize_execution 10 times (simulating duplicate Outbox/Runner delivery)
        for _ in range(10):
            r_dup = await rt2.finalizer.finalize_execution(exec_id)
            assert r_dup.already_finalized is True

        # Strictly zero duplicate memories or goal transitions
        assert len(await rt2.repo.list_memory_index(limit=100)) == mem_count_after_1
        assert len(await rt2.repo.goal_history(uuid.UUID(r_first.goal_id))) == goal_hist_after_1
    finally:
        await rt2.stop()


@pytest.mark.asyncio
async def test_outbox_inbox_retry_backoff_dead_letter_and_replay(repo: StateRepository):
    dispatcher = OutboxDispatcher(repo, poll_interval=0.05, base_backoff_seconds=0.01)
    attempts_seen = {"n": 0}
    should_fail = {"v": True}

    async def flaky_handler(ev: dict) -> dict:
        attempts_seen["n"] += 1
        if should_fail["v"]:
            raise RuntimeError("transient downstream failure")
        return {"processed": ev["idempotency_key"]}

    dispatcher.register_handler(OutboxEventType.NOTIFICATION_DISPATCH, "test_notifier", flaky_handler)

    ev_id = await repo.enqueue_outbox_event(
        event_type=OutboxEventType.NOTIFICATION_DISPATCH,
        idempotency_key="notif:test-1",
        payload={"msg": "hello"},
        max_attempts=2,
    )
    # Duplicate enqueue with same idempotency_key returns existing id without creating a 2nd row
    ev_id2 = await repo.enqueue_outbox_event(
        event_type=OutboxEventType.NOTIFICATION_DISPATCH,
        idempotency_key="notif:test-1",
        payload={"msg": "hello"},
        max_attempts=2,
    )
    assert ev_id2 == ev_id

    # Attempt 1 fails -> retried with backoff
    s1 = await dispatcher.dispatch_once()
    assert s1["retried"] == 1
    await asyncio.sleep(0.03)

    # Attempt 2 fails -> dead_letter
    s2 = await dispatcher.dispatch_once()
    assert s2["dead_letter"] == 1
    dead = await repo.list_outbox_events(status="dead_letter")
    assert len(dead) == 1

    # Replay dead letter after fixing downstream -> succeeds and records inbox
    should_fail["v"] = False
    assert await repo.replay_outbox_event(ev_id) is True
    s3 = await dispatcher.dispatch_once()
    assert s3["dispatched"] == 1
    assert attempts_seen["n"] == 3

    # Manual replay again -> Inbox deduplicates without invoking handler a 4th time!
    assert await repo.replay_outbox_event(ev_id) is True
    s4 = await dispatcher.dispatch_once()
    assert s4["deduplicated"] == 1
    assert attempts_seen["n"] == 3


@pytest.mark.asyncio
async def test_v2_task_lifecycle_attempts_and_cancel_api(db: Database, tmp_path):
    agent = MultiCycleAgent("worker-1")
    rt = _make_runtime(db, tmp_path, agent, max_wait=0.05)
    app = create_app(settings=rt.settings, runtime=rt, start_scheduler=False)
    hdr = {"Authorization": "Bearer p1-2-admin"}

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for i in range(3):
                await client.post(
                    "/world",
                    headers=hdr,
                    json={"kind": "knowledge_topic", "key": f"cancel-t-{i}", "value": {"importance": 0.95}},
                )
            cyc = await client.post("/control/cycle", headers=hdr)
            assert cyc.status_code == 200
            assert cyc.json()["outcome"] == "IN_FLIGHT"
            tid = cyc.json()["state_changes"]["task_id"]

            r_lc = await client.get(f"/v2/tasks/{tid}/lifecycle", headers=hdr)
            assert r_lc.status_code == 200
            assert len(r_lc.json()) >= 3

            r_att = await client.get(f"/v2/tasks/{tid}/attempts", headers=hdr)
            assert r_att.status_code == 200
            assert len(r_att.json()) == 1
            assert r_att.json()[0]["attempt_number"] == 1

            r_can = await client.post(f"/v2/tasks/{tid}/cancel", headers=hdr, json={"reason": "user abort"})
            assert r_can.status_code == 200
            assert r_can.json()["status"] == "CANCELLED"

            # Second cancel returns 409 Conflict
            r_can2 = await client.post(f"/v2/tasks/{tid}/cancel", headers=hdr)
            assert r_can2.status_code == 409


def test_alembic_0003_upgrade_and_downgrade_roundtrip(tmp_path):
    db_file = tmp_path / "alembic_0003.sqlite"
    env = os.environ.copy()
    env["AOC_DATABASE__URL"] = f"sqlite+aiosqlite:///{db_file}"
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

    def run_alembic(*args: str) -> None:
        res = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=project_root,
            env=env,
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, f"alembic {args} failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"

    run_alembic("upgrade", "head")
    eng = create_engine(f"sqlite:///{db_file}")
    try:
        insp = inspect(eng)
        tables = set(insp.get_table_names())
        assert {"task_lifecycle_events", "outbox_events", "inbox_events"}.issubset(tables)
        t_cols = {c["name"] for c in insp.get_columns("agent_tasks")}
        assert {"state_version", "finalized_at", "idempotency_key"}.issubset(t_cols)
        e_cols = {c["name"] for c in insp.get_columns("agent_executions")}
        assert {"attempt_number", "idempotency_key", "runner_id", "process_identity", "thread_id", "finalized_at"}.issubset(e_cols)
    finally:
        eng.dispose()

    run_alembic("downgrade", "0002_p1_1_security_budget")
    run_alembic("upgrade", "0003_p1_2_durable_tasks")
