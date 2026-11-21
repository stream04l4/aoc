"""V2.1 P0 Reliability Fix Tests:
  - P0-1: TaskFinalizer & Orchestrator retry superseded-attempt finalization semantics
  - P0-1: Idempotent reconciliation script (`reconcile_v2_1_data.py`) dry-run & execute
  - P0-3: Host Runner UDS socket strictly enforced at mode 0o660 (never 0o666)
  - P0-3: `/v1/embeddings` returns native 512-dim vectors without 1024-dim zero-padding
"""

from __future__ import annotations

import asyncio
import os
import stat
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

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
from app.cognition.interfaces import ActionPlan, EvaluationResult
from app.core.config import ApiSettings, EmbeddingSettings, GovernanceSettings, MemorySettings, Settings
from app.core.enums import ExecutionStatus, MemoryType, TaskStatus
from app.core.runtime import OrganismRuntime
from app.drives.engine import DriveEngine
from app.governance.policy import GovernanceManager
from app.memory.experience_manager import ExperienceManager
from app.orchestration.finalizer import TaskFinalizer
from app.orchestration.orchestrator import AgentOrchestrator
from app.perception.interfaces import Observation, utcnow
from app.perception.manager import SensorManager
from app.runner.service import RunnerManager, bind_uds_socket_0660, create_runner_app
from app.storage.database import Database
from app.storage.models import Base
from app.storage.repository import StateRepository
from scripts.reconcile_v2_1_data import audit_and_reconcile


async def _init_db(db: Database) -> None:
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


class _FlakyThenSuccessAgent(AgentInterface):
    """Fails on attempt 1, succeeds on attempt 2."""

    def __init__(self) -> None:
        self._cap = AgentCapability(
            agent_id="flaky-agent",
            name="Flaky Agent",
            agent_type="cli",
            capabilities=["general_task"],
            supported_modes=["read-only", "workspace-write"],
        )
        self.calls = 0

    @property
    def capability(self) -> AgentCapability:
        return self._cap

    async def probe(self) -> AgentProbeResult:
        return AgentProbeResult(ok=True, binary="/bin/true", version="1.0")

    async def execute(self, task: AgentTask) -> AgentExecution:
        self.calls += 1
        now = utcnow()
        if self.calls == 1:
            return AgentExecution(
                execution_id=task.execution_id or str(uuid.uuid4()),
                task_id=task.task_id,
                agent_id=self._cap.agent_id,
                status=ExecutionStatus.FAILED.value,
                started_at=now,
                completed_at=now,
                error="transient failure on attempt 1",
            )
        return AgentExecution(
            execution_id=task.execution_id or str(uuid.uuid4()),
            task_id=task.task_id,
            agent_id=self._cap.agent_id,
            status=ExecutionStatus.SUCCEEDED.value,
            started_at=now,
            completed_at=now,
            stdout="verified output on attempt 2",
            result={"summary": "succeeded on retry"},
        )

    async def get_status(self, execution_id: str) -> AgentStatus:
        return AgentStatus(execution_id=execution_id, agent_id=self._cap.agent_id, status="SUCCEEDED")

    async def cancel(self, execution_id: str) -> bool:
        return True


class _StubBrain:
    async def assess(self, *args, **kwargs):
        raise NotImplementedError

    async def generate_goal(self, *args, **kwargs):
        raise NotImplementedError

    async def plan(self, *args, **kwargs):
        raise NotImplementedError

    async def evaluate(self, ctx) -> EvaluationResult:
        return EvaluationResult(
            goal_achieved=bool(ctx.verification_passed),
            summary="Evaluated retry completion",
            information_gain=0.25,
            learned_facts=["retry succeeded cleanly"],
        )


@pytest.mark.asyncio
async def test_v2_1_retry_superseded_execution_is_finalized(tmp_path: Path):
    """V2.1 P0-1: When attempt 1 fails and attempt 2 succeeds, BOTH executions MUST have finalized_at set."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'retry_fin.db'}")
    await _init_db(db)
    repo = StateRepository(db)
    try:
        org = await repo.get_or_create_identity("AOC-TEST", "2.1.0")
        org_id = org["id"]
        cycle_id, _ = await repo.start_cycle(org_id, "ACTIVE")
        goal_id = await repo.create_goal(org_id, cycle_id, drive_type="curiosity", title="Retry test", status="ACTIVE")

        gov = GovernanceManager(GovernanceSettings(workspace_root=str(tmp_path / "ws")), repo)
        reg = AgentRegistry(repo=repo)
        agent = _FlakyThenSuccessAgent()
        reg.register(agent)
        await reg.probe_all()

        plan = ActionPlan(
            should_execute=True,
            goal_title="Retry test",
            objective="Test retry attempt finalization",
            assigned_agent="flaky-agent",
            permission_level="L0",
        )
        verdict = await gov.validate_plan(plan, "curiosity")
        plan_id = await repo.create_plan(goal_id, cycle_id, plan, "L0", verdict, "APPROVED")

        orch = AgentOrchestrator(registry=reg, governance=gov, repo=repo, max_retries=2)
        submitted = await orch.submit_plan(plan=plan, verdict=verdict, goal_id=goal_id, plan_id=plan_id, cycle_id=cycle_id)
        task_id = uuid.UUID(submitted["task_id"])

        # Poll through attempt 1 -> retry -> attempt 2 completion
        await orch.wait_for_task(submitted["task_id"], max_wait_seconds=5.0, poll_interval=0.05)

        execs = await repo.list_executions(task_id=task_id, limit=10)
        assert len(execs) == 2
        attempt1 = next(e for e in execs if e["attempt_number"] == 1)
        attempt2 = next(e for e in execs if e["attempt_number"] == 2)

        # Superseded attempt 1 MUST already be finalized with superseded_by_retry marker!
        assert attempt1["finalized_at"] is not None
        assert (attempt1.get("verification") or {}).get("superseded_by_retry") is True

        # Finalize attempt 2 via TaskFinalizer
        finalizer = TaskFinalizer(
            organism_id=org_id,
            repo=repo,
            perception=SensorManager(repo=repo),
            drives=DriveEngine(),
            brain=_StubBrain(),  # type: ignore[arg-type]
            governance=gov,
            memory=ExperienceManager(repo, backend=None, outbox_enabled=False, embed_on_write=False),
        )
        fin = await finalizer.finalize_execution(
            uuid.UUID(attempt2["execution_id"]),
            pre_obs_override=Observation(),
            pre_drives_override={},
            llm_available_override=True,
        )
        assert fin.goal_achieved is True

        execs_after = await repo.list_executions(task_id=task_id, limit=10)
        assert all(e["finalized_at"] is not None for e in execs_after)
        task_after = await repo.get_task(task_id)
        assert task_after is not None and task_after["finalized_at"] is not None
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_v2_1_reconcile_script_idempotent_and_protects_aoc_001(tmp_path: Path):
    """V2.1 P0-1 & P1-4: Reconcile script backfills superseded retries, preserves unexplained anomalies, and archives only AOC-E2E-*."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'reconcile.db'}"
    db = Database(db_url)
    await _init_db(db)
    repo = StateRepository(db)
    try:
        org = await repo.get_or_create_identity("AOC-001", "2.1.0")
        org_id = org["id"]
        cid, _ = await repo.start_cycle(org_id, "ACTIVE")
        gid = await repo.create_goal(org_id, cid, drive_type="curiosity", title="G", status="ACHIEVED")

        # Task 1: finalized task with attempt 1 unfinalized (superseded retry) and attempt 2 finalized
        tid1 = await repo.create_task(
            plan_id=None,
            goal_id=gid,
            cycle_id=cid,
            objective="Task 1",
            assigned_agent="codex",
            workspace=str(tmp_path),
            timeout_seconds=60,
            permission_level="L0",
            status="SUCCEEDED",
        )
        now = utcnow()
        await repo.update_task(tid1, finalized_at=now)
        e1_id = await repo.create_execution(tid1, "codex", status="FAILED", attempt_number=1)
        await repo.update_execution(e1_id, completed_at=now)  # finalized_at stays NULL
        e2_id = await repo.create_execution(tid1, "codex", status="SUCCEEDED", attempt_number=2)
        await repo.update_execution(e2_id, completed_at=now, finalized_at=now)

        # Task 2: genuine anomaly — parent task NOT finalized, attempt 1 terminal & unfinalized
        tid2 = await repo.create_task(
            plan_id=None,
            goal_id=gid,
            cycle_id=cid,
            objective="Task 2 anomaly",
            assigned_agent="codex",
            workspace=str(tmp_path),
            timeout_seconds=60,
            permission_level="L0",
            status="SUCCEEDED",
        )
        e3_id = await repo.create_execution(tid2, "codex", status="SUCCEEDED", attempt_number=1)
        await repo.update_execution(e3_id, completed_at=now)

        # Identity memories: 1 AOC-001 (MUST KEEP) + 2 AOC-E2E-* (MUST ARCHIVE)
        await repo.add_memory_index(
            memory_type=MemoryType.IDENTITY.value,
            title="Organism Identity: AOC-001",
            summary="Production identity",
            content_hash="h-aoc-001",
            source="runtime",
            sync_status="synced",
        )
        await repo.add_memory_index(
            memory_type=MemoryType.IDENTITY.value,
            title="Organism Identity: AOC-E2E-abc123",
            summary="Legacy E2E identity",
            content_hash="h-e2e-1",
            source="runtime",
            sync_status="synced",
        )

        # Dry run changes nothing
        dry = await audit_and_reconcile(db_url, execute=False)
        assert len(dry["superseded_retries"]) == 1
        assert len(dry["unexplained_unfinalized_execs"]) == 1
        assert len(dry["legacy_e2e_identities"]) == 1

        # Execute reconciles ONLY superseded retry & archives ONLY AOC-E2E-*
        arch_path = tmp_path / "e2e_archive.json"
        res = await audit_and_reconcile(db_url, execute=True, archive_out=str(arch_path))
        assert res["actions_taken"]["executions_backfilled"] == 1
        assert res["actions_taken"]["e2e_memories_archived"] == 1
        assert arch_path.exists()

        # Verify e1 is now finalized, e3 (anomaly) is STILL unfinalized, AOC-001 identity untouched!
        e1_after = await repo.get_execution(e1_id)
        e3_after = await repo.get_execution(e3_id)
        assert e1_after is not None and e1_after["finalized_at"] is not None
        assert e3_after is not None and e3_after["finalized_at"] is None

        mems = await repo.list_memory_index(memory_type=MemoryType.IDENTITY.value, limit=10)
        assert [m["title"] for m in mems] == ["Organism Identity: AOC-001"]

        # Second execute is strictly idempotent (0 changes)
        res2 = await audit_and_reconcile(db_url, execute=True, archive_out=str(arch_path))
        assert res2["actions_taken"]["executions_backfilled"] == 0
        assert res2["actions_taken"]["e2e_memories_archived"] == 0
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_v2_1_runner_uds_socket_mode_is_0660(tmp_path: Path):
    """V2.1 P0-3: Host Runner UDS socket file mode MUST be 0o660 (least privilege), never 0o666."""
    sock_path = tmp_path / "runner.sock"
    mgr = RunnerManager(
        runner_id="test-runner-0660",
        socket_path=str(sock_path),
        state_dir=str(tmp_path / "state"),
        workspace_root=str(tmp_path / "ws"),
        auth_token="runner-secret",
    )
    app = create_runner_app(mgr)
    bound_sock = bind_uds_socket_0660(sock_path)
    config = uvicorn.Config(app, log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[bound_sock]))
    try:
        for _ in range(50):
            if sock_path.exists() and server.started:
                break
            await asyncio.sleep(0.05)
        assert sock_path.exists()
        mode = stat.S_IMODE(os.stat(sock_path).st_mode)
        assert mode == 0o660, f"Expected UDS socket mode 0o660, got {oct(mode)}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5.0)
        bound_sock.close()


@pytest.mark.asyncio
async def test_v2_1_embeddings_endpoint_returns_native_512d_no_zero_padding(tmp_path: Path):
    """V2.1 P0-3: `/v1/embeddings` must return native 512d vectors without legacy 1024d zero-padding."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'emb512.db'}")
    await _init_db(db)
    try:
        settings = Settings(
            api=ApiSettings(host="127.0.0.1", auth_enabled=True, admin_key="adm-k", internal_token="int-k"),
            embedding=EmbeddingSettings(provider="hash_fallback", dimensions=512),
            memory=MemorySettings(provider="none", embedding_dim=512, embedding_model="aoc-local-embed-512"),
            governance=GovernanceSettings(workspace_root=str(tmp_path / "ws")),
        )
        rt = OrganismRuntime(settings=settings, db=db, auto_load_agents=False, llm_probe_fn=lambda: None)
        app = create_app(settings=settings, db=db, runtime=rt, start_scheduler=False)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.post(
                    "/v1/embeddings",
                    json={"input": "AOC native 512d embedding test", "model": "aoc-local-embed-512"},
                    headers={"Authorization": "Bearer int-k"},
                )
                assert r.status_code == 200
                data = r.json()["data"]
                assert len(data) == 1
                assert len(data[0]["embedding"]) == 512
    finally:
        await db.dispose()
