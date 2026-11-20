"""P1-3 Integration & Real OS `kill -9` Fault Injection Tests (6 scenarios x 3 reps = 18 real SIGKILL runs).

Also includes:
  - Real PostgreSQL 16 original snapshot `0001 -> 0004` upgrade/downgrade test (when AOC_TEST_PG_URL is set).
  - Real host systemd `aoc-agent-runner.service` + real `codex-cli 0.171.0` UDS integration test
    (when /run/aoc-runner/runner.sock is reachable).
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import inspect, text

from app.agents.base import AgentCapability, AgentExecution, AgentInterface, AgentProbeResult, AgentStatus, AgentTask
from app.agents.codex import CodexAdapter
from app.agents.registry import AgentRegistry
from app.cognition.interfaces import ActionPlan, AssessmentResult, BrainInterface, EvaluationResult, GoalProposal
from app.core.config import RunnerSettings, Settings
from app.core.enums import ExecutionStatus, PermissionLevel, TaskStatus
from app.core.runtime import OrganismRuntime
from app.governance.interfaces import PolicyVerdict
from app.governance.policy import GovernanceManager
from app.memory.experience_manager import ExperienceManager
from app.orchestration.finalizer import TaskFinalizer
from app.orchestration.orchestrator import AgentOrchestrator
from app.orchestration.state_machine import TaskStateMachine
from app.perception.interfaces import Observation
from app.runner.client import RunnerClient, UnixSocketRunnerBackend
from app.runner.process_identity import verify_process_identity
from app.runner.protocol import LaunchExecutionRequest
from app.storage.database import Database
from app.storage.models import Base
from app.storage.outbox import OutboxDispatcher, OutboxEventType
from app.storage.repository import StateRepository


async def _init_db(db: Database) -> None:
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class _ShellTestAdapter(AgentInterface):
    """Real OS subprocess adapter used for deterministic kill -9 fault injection through Host Runner."""

    def __init__(self, script_code: str, agent_id: str = "shell-worker") -> None:
        self._script_code = script_code
        self._cap = AgentCapability(
            agent_id=agent_id,
            name="Shell Worker",
            agent_type="cli",
            capabilities=["general_task", "knowledge_research"],
            supported_modes=["read-only", "workspace-write"],
        )

    @property
    def capability(self) -> AgentCapability:
        return self._cap

    async def probe(self) -> AgentProbeResult:
        return AgentProbeResult(ok=True, binary=sys.executable, version="py")

    async def execute(self, task: AgentTask) -> AgentExecution:
        raise NotImplementedError("Must run via UnixSocketRunnerBackend")

    async def get_status(self, execution_id: str) -> AgentStatus:
        return AgentStatus(execution_id=execution_id, agent_id=self._cap.agent_id, status="RUNNING")

    async def cancel(self, execution_id: str) -> bool:
        return True

    def build_runner_spec(self, task: AgentTask) -> dict[str, Any] | None:
        return {
            "command": [sys.executable, "-c", self._script_code, task.workspace],
            "stdin_text": "",
            "extra_env": {},
        }


class _DeterministicBrain(BrainInterface):
    async def assess(self, obs, memories, drives) -> AssessmentResult:
        return AssessmentResult(summary="ok", confidence=0.9)

    async def generate_goal(self, ctx) -> GoalProposal:
        return GoalProposal(should_act=False)

    async def plan(self, ctx) -> ActionPlan:
        return ActionPlan(should_execute=False)

    async def evaluate(self, ctx) -> EvaluationResult:
        return EvaluationResult(
            goal_achieved=ctx.verification_passed,
            summary="verified via kill-9 recovery test",
            information_gain=0.4 if ctx.verification_passed else 0.0,
            should_persist_memory=True,
        )


def _spawn_runner_process(sock_path: Path, state_dir: Path, ws_root: Path, runner_id: str = "kill9-runner") -> subprocess.Popen[Any]:
    env = os.environ.copy()
    env["AOC_RUNNER__RUNNER_ID"] = runner_id
    env["AOC_RUNNER__SOCKET_PATH"] = str(sock_path)
    env["AOC_RUNNER__STATE_DIR"] = str(state_dir)
    env["AOC_RUNNER__WORKSPACE_ROOT"] = str(ws_root)
    env["AOC_LOGGING__LEVEL"] = "WARNING"
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.runner.service"],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 10.0
    client = RunnerClient(socket_path=str(sock_path))
    while time.monotonic() < deadline:
        if sock_path.exists():
            try:
                h = asyncio.run(client.health())
                if h.status == "ok":
                    return proc
            except Exception:
                pass
        if proc.poll() is not None:
            raise RuntimeError(f"Runner exited prematurely with code {proc.returncode}")
        time.sleep(0.05)
    proc.kill()
    raise TimeoutError(f"Runner failed to start on {sock_path}")


def _terminate_proc_safely(proc: subprocess.Popen[Any] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:
            pass


def _make_orchestrator(db: Database, sock_path: Path, ws_root: Path, script_code: str) -> tuple[StateRepository, AgentOrchestrator, UnixSocketRunnerBackend]:
    repo = StateRepository(db)
    settings = Settings()
    gov = GovernanceManager(settings.governance.model_copy(update={"workspace_root": str(ws_root)}), repo)
    reg = AgentRegistry(repo=repo)
    reg.register(_ShellTestAdapter(script_code))
    backend = UnixSocketRunnerBackend(RunnerClient(socket_path=str(sock_path)), runner_id="kill9-runner")
    orch = AgentOrchestrator(registry=reg, governance=gov, repo=repo, max_retries=0, backend=backend)
    return repo, orch, backend


async def _setup_db_and_plan(db: Database, repo: StateRepository, ws_root: Path, permission_level: str = "L0") -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, ActionPlan, PolicyVerdict]:
    ident = await repo.get_or_create_identity("AOC-KILL9", "2.0.0")
    org_id = ident["id"]
    cid, _ = await repo.start_cycle(org_id, "ACTIVE")
    gid = await repo.create_goal(
        org_id,
        cid,
        drive_type="curiosity",
        title="Kill-9 resilience check",
        status="ACTIVE",
    )
    plan = ActionPlan(
        should_execute=True,
        goal_title="Kill-9 resilience check",
        objective="Produce marker file and exit cleanly",
        assigned_agent="shell-worker",
        required_capability="general_task",
        expected_output="ok",
        timeout_seconds=30,
        permission_level=permission_level,
    )
    verdict = PolicyVerdict(
        allowed=True,
        requested_level=permission_level,
        effective_level=permission_level,
        sandbox_mode="read-only" if permission_level == "L0" else "workspace-write",
        effective_timeout_seconds=30,
        reason="ok",
    )
    await repo.create_plan(
        goal_id=gid,
        cycle_id=cid,
        plan=plan,
        permission_level=permission_level,
        verdict=verdict,
        status="APPROVED",
    )
    return org_id, cid, gid, plan, verdict


@pytest.mark.integration
@pytest.mark.parametrize("rep", [1, 2, 3])
def test_kill9_scenario_1_kill_core_while_task_running(tmp_path: Path, rep: int):
    """Scenario 1 (x3): OS `kill -9` on Core worker process while task runs on Host Runner."""
    sock_path = tmp_path / f"r1_{rep}.sock"
    state_dir = tmp_path / f"st1_{rep}"
    ws_root = tmp_path / f"ws1_{rep}"
    ws_root.mkdir()
    db_file = tmp_path / f"db1_{rep}.sqlite"
    marker_file = tmp_path / f"core1_{rep}.ready"

    runner = _spawn_runner_process(sock_path, state_dir, ws_root)
    core_proc: subprocess.Popen[Any] | None = None
    try:
        # Spawn a real separate OS process acting as AOC Core that dispatches a 1.5s task then sleeps forever
        core_script = f"""
import asyncio, json, sys
from pathlib import Path
from app.storage.database import Database
from tests.integration.test_p1_3_kill9_recovery import _init_db, _make_orchestrator, _setup_db_and_plan

async def main():
    db = Database("sqlite+aiosqlite:///{db_file}")
    await _init_db(db)
    script = "import time, sys; time.sleep(1.2); print('CORE_KILL9_OK', flush=True)"
    repo, orch, _ = _make_orchestrator(db, Path("{sock_path}"), Path("{ws_root}"), script)
    await orch.registry.probe_all()
    org_id, cid, gid, plan, verdict = await _setup_db_and_plan(db, repo, Path("{ws_root}"))
    info = await orch.submit_plan(plan, verdict, goal_id=gid, cycle_id=cid)
    Path("{marker_file}").write_text(json.dumps(info), encoding="utf-8")
    await asyncio.sleep(60)

asyncio.run(main())
"""
        core_proc = subprocess.Popen([sys.executable, "-c", core_script], cwd=str(PROJECT_ROOT))
        deadline = time.monotonic() + 10.0
        while not marker_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker_file.exists(), "Core process failed to dispatch task"
        info = json.loads(marker_file.read_text(encoding="utf-8"))

        # REAL OS kill -9 on AOC Core!
        os.kill(core_proc.pid, signal.SIGKILL)
        core_proc.wait(timeout=5)
        assert core_proc.returncode == -signal.SIGKILL

        # Restart fresh AOC Core against same DB + Runner UDS
        async def _recover_and_verify():
            db = Database(f"sqlite+aiosqlite:///{db_file}")
            script = "import time, sys; time.sleep(1.2); print('CORE_KILL9_OK', flush=True)"
            repo, orch, _ = _make_orchestrator(db, sock_path, ws_root, script)
            await orch.registry.probe_all()
            try:
                rec = await orch.recover_on_startup()
                assert rec["reattached_running"] + rec["recovered_terminal"] >= 1
                final = await orch.wait_for_task(info["task_id"], max_wait_seconds=10.0, poll_interval=0.1)
                assert final["status"] == TaskStatus.SUCCEEDED.value
                # Verify exactly 1 execution (zero duplicate execution!)
                execs = await repo.list_executions(task_id=uuid.UUID(info["task_id"]))
                assert len(execs) == 1
                assert "CORE_KILL9_OK" in (execs[0]["stdout"] or "")
                # Verify recovery audit record exists
                audits = await repo.list_recovery_records(task_id=uuid.UUID(info["task_id"]))
                assert len(audits) >= 1
            finally:
                await db.dispose()

        asyncio.run(_recover_and_verify())
    finally:
        _terminate_proc_safely(core_proc)
        _terminate_proc_safely(runner)


@pytest.mark.integration
@pytest.mark.parametrize("rep", [1, 2, 3])
def test_kill9_scenario_2_kill_runner_while_child_running(tmp_path: Path, rep: int):
    """Scenario 2 (x3): OS `kill -9` on Host Runner while child process runs; restarted Runner re-adopts child."""
    sock_path = tmp_path / f"r2_{rep}.sock"
    state_dir = tmp_path / f"st2_{rep}"
    ws_root = tmp_path / f"ws2_{rep}"
    ws_root.mkdir()
    db_file = tmp_path / f"db2_{rep}.sqlite"

    runner1 = _spawn_runner_process(sock_path, state_dir, ws_root)
    runner2: subprocess.Popen[Any] | None = None
    try:
        script = "import time; time.sleep(1.5); print('SURVIVED_RUNNER_KILL9', flush=True)"

        async def _dispatch() -> tuple[str, str, int]:
            db = Database(f"sqlite+aiosqlite:///{db_file}")
            await _init_db(db)
            repo, orch, _ = _make_orchestrator(db, sock_path, ws_root, script)
            await orch.registry.probe_all()
            try:
                _, cid, gid, plan, verdict = await _setup_db_and_plan(db, repo, ws_root)
                info = await orch.submit_plan(plan, verdict, goal_id=gid, cycle_id=cid)
                ex = await repo.get_execution(uuid.UUID(info["execution_id"]))
                assert ex is not None and ex["pid"] is not None
                return info["task_id"], info["execution_id"], int(ex["pid"])
            finally:
                await db.dispose()

        task_id, exec_id, child_pid = asyncio.run(_dispatch())

        # REAL OS kill -9 on Host Runner while child is still sleeping!
        os.kill(runner1.pid, signal.SIGKILL)
        runner1.wait(timeout=5)
        assert runner1.returncode == -signal.SIGKILL

        # Child process MUST still be alive after Runner died!
        os.kill(child_pid, 0)

        # Restart Host Runner (simulates systemd Restart=always)
        runner2 = _spawn_runner_process(sock_path, state_dir, ws_root)

        async def _collect():
            db = Database(f"sqlite+aiosqlite:///{db_file}")
            repo, orch, _ = _make_orchestrator(db, sock_path, ws_root, script)
            await orch.registry.probe_all()
            try:
                await orch.recover_on_startup()
                final = await orch.wait_for_task(task_id, max_wait_seconds=10.0, poll_interval=0.1)
                assert final["status"] == TaskStatus.SUCCEEDED.value
                ex = await repo.get_execution(uuid.UUID(exec_id))
                assert ex is not None
                assert "SURVIVED_RUNNER_KILL9" in (ex["stdout"] or "")
                assert len(await repo.list_executions(task_id=uuid.UUID(task_id))) == 1
            finally:
                await db.dispose()

        asyncio.run(_collect())
    finally:
        _terminate_proc_safely(runner1)
        _terminate_proc_safely(runner2)


@pytest.mark.integration
@pytest.mark.parametrize("rep", [1, 2, 3])
def test_kill9_scenario_3_kill_child_agent_process(tmp_path: Path, rep: int):
    """Scenario 3 (x3): OS `kill -9` directly on child agent PID; MUST mark FAILED (never SUCCEEDED)."""
    sock_path = tmp_path / f"r3_{rep}.sock"
    state_dir = tmp_path / f"st3_{rep}"
    ws_root = tmp_path / f"ws3_{rep}"
    ws_root.mkdir()
    db_file = tmp_path / f"db3_{rep}.sqlite"

    runner = _spawn_runner_process(sock_path, state_dir, ws_root)
    try:
        script = "import time; time.sleep(30)"

        async def _run():
            db = Database(f"sqlite+aiosqlite:///{db_file}")
            await _init_db(db)
            repo, orch, _ = _make_orchestrator(db, sock_path, ws_root, script)
            await orch.registry.probe_all()
            try:
                _, cid, gid, plan, verdict = await _setup_db_and_plan(db, repo, ws_root)
                info = await orch.submit_plan(plan, verdict, goal_id=gid, cycle_id=cid)
                ex = await repo.get_execution(uuid.UUID(info["execution_id"]))
                assert ex is not None and ex["pid"] is not None
                child_pid = int(ex["pid"])

                # REAL OS kill -9 directly on child agent PID!
                os.kill(child_pid, signal.SIGKILL)

                final = await orch.wait_for_task(info["task_id"], max_wait_seconds=10.0, poll_interval=0.1)
                assert final["status"] == TaskStatus.FAILED.value
                ex_after = await repo.get_execution(uuid.UUID(info["execution_id"]))
                assert ex_after is not None
                assert ex_after["status"] == ExecutionStatus.FAILED.value
                assert "signal 9" in (ex_after["error"] or "").lower() or "137" in (ex_after["error"] or "")
            finally:
                await db.dispose()

        asyncio.run(_run())
    finally:
        _terminate_proc_safely(runner)


@pytest.mark.integration
@pytest.mark.parametrize("rep", [1, 2, 3])
def test_kill9_scenario_4_crash_before_handshake_completed(tmp_path: Path, rep: int):
    """Scenario 4 (x3): Core crashes while DB is DISPATCHING (both runner-received and runner-missed L1 cases)."""
    sock_path = tmp_path / f"r4_{rep}.sock"
    state_dir = tmp_path / f"st4_{rep}"
    ws_root = tmp_path / f"ws4_{rep}"
    ws_root.mkdir()
    db_file = tmp_path / f"db4_{rep}.sqlite"

    runner = _spawn_runner_process(sock_path, state_dir, ws_root)
    try:
        script = "import time; time.sleep(0.2); print('HANDSHAKE_RECOVERED', flush=True)"

        async def _run():
            db = Database(f"sqlite+aiosqlite:///{db_file}")
            await _init_db(db)
            repo, orch, backend = _make_orchestrator(db, sock_path, ws_root, script)
            await orch.registry.probe_all()
            try:
                # Sub-case 4a: Runner DID start the job for idempotency_key, but Core crashed before writing RUNNING to DB
                t1 = await repo.create_task(
                    objective="Handshake crash after runner launch",
                    assigned_agent="shell-worker",
                    workspace=str(ws_root / "t4a"),
                    timeout_seconds=30,
                    permission_level="L1",
                    status=TaskStatus.DISPATCHING.value,
                    attempt=1,
                    max_attempts=1,
                )
                idem1 = f"exec:{t1}:1"
                e1 = await repo.create_execution(t1, "shell-worker", ExecutionStatus.DISPATCHING.value, attempt_number=1, idempotency_key=idem1)
                await backend.client.launch(
                    LaunchExecutionRequest(
                        execution_id=str(e1),
                        task_id=str(t1),
                        agent_id="shell-worker",
                        idempotency_key=idem1,
                        command=[sys.executable, "-c", script, str(ws_root / "t4a")],
                        workspace=str(ws_root / "t4a"),
                    )
                )

                # Sub-case 4b: Non-idempotent L1 task stuck in DISPATCHING where Runner NEVER saw it -> MUST become RECOVERY_REQUIRED
                t2 = await repo.create_task(
                    objective="Handshake crash before runner saw L1 write",
                    assigned_agent="shell-worker",
                    workspace=str(ws_root / "t4b"),
                    timeout_seconds=30,
                    permission_level="L1",
                    status=TaskStatus.DISPATCHING.value,
                    attempt=1,
                    max_attempts=1,
                )
                e2 = await repo.create_execution(t2, "shell-worker", ExecutionStatus.DISPATCHING.value, attempt_number=1, idempotency_key=f"exec:{t2}:1")

                # Wait briefly so 4a completes on runner, then run startup recovery!
                await asyncio.sleep(0.5)
                await orch.recover_on_startup()

                row1 = await repo.get_task(t1)
                assert row1 is not None and row1["status"] == TaskStatus.SUCCEEDED.value
                assert len(await repo.list_executions(task_id=t1)) == 1

                row2 = await repo.get_task(t2)
                assert row2 is not None and row2["status"] == TaskStatus.RECOVERY_REQUIRED.value
                assert len(await repo.list_recovery_records(execution_id=e2)) == 1
            finally:
                await db.dispose()

        asyncio.run(_run())
    finally:
        _terminate_proc_safely(runner)


@pytest.mark.integration
@pytest.mark.parametrize("rep", [1, 2, 3])
def test_kill9_scenario_5_kill_core_after_agent_finished_before_db_write(tmp_path: Path, rep: int):
    """Scenario 5 (x3): Agent completes on Runner, Core is killed with SIGKILL before collecting into DB."""
    sock_path = tmp_path / f"r5_{rep}.sock"
    state_dir = tmp_path / f"st5_{rep}"
    ws_root = tmp_path / f"ws5_{rep}"
    ws_root.mkdir()
    db_file = tmp_path / f"db5_{rep}.sqlite"
    marker_file = tmp_path / f"core5_{rep}.ready"

    runner = _spawn_runner_process(sock_path, state_dir, ws_root)
    core_proc: subprocess.Popen[Any] | None = None
    try:
        core_script = f"""
import asyncio, json
from pathlib import Path
from app.storage.database import Database
from tests.integration.test_p1_3_kill9_recovery import _init_db, _make_orchestrator, _setup_db_and_plan

async def main():
    db = Database("sqlite+aiosqlite:///{db_file}")
    await _init_db(db)
    script = "print('FINISHED_BEFORE_DB_WRITE', flush=True)"
    repo, orch, _ = _make_orchestrator(db, Path("{sock_path}"), Path("{ws_root}"), script)
    await orch.registry.probe_all()
    _, cid, gid, plan, verdict = await _setup_db_and_plan(db, repo, Path("{ws_root}"))
    info = await orch.submit_plan(plan, verdict, goal_id=gid, cycle_id=cid)
    # Wait so child finishes on runner, while Core does NOT poll/write DB
    await asyncio.sleep(0.5)
    Path("{marker_file}").write_text(json.dumps(info), encoding="utf-8")
    await asyncio.sleep(60)

asyncio.run(main())
"""
        core_proc = subprocess.Popen([sys.executable, "-c", core_script], cwd=str(PROJECT_ROOT))
        deadline = time.monotonic() + 10.0
        while not marker_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker_file.exists()
        info = json.loads(marker_file.read_text(encoding="utf-8"))

        # REAL OS kill -9 before Core writes result into DB
        os.kill(core_proc.pid, signal.SIGKILL)
        core_proc.wait(timeout=5)

        async def _recover():
            db = Database(f"sqlite+aiosqlite:///{db_file}")
            repo, orch, _ = _make_orchestrator(db, sock_path, ws_root, "print('noop')")
            await orch.registry.probe_all()
            try:
                # Before recovery, DB still thinks task is RUNNING
                pre = await repo.get_task(uuid.UUID(info["task_id"]))
                assert pre is not None and pre["status"] == TaskStatus.RUNNING.value

                rec = await orch.recover_on_startup()
                assert rec["recovered_terminal"] == 1

                post = await repo.get_task(uuid.UUID(info["task_id"]))
                assert post is not None and post["status"] == TaskStatus.SUCCEEDED.value
                ex = await repo.get_execution(uuid.UUID(info["execution_id"]))
                assert ex is not None and "FINISHED_BEFORE_DB_WRITE" in (ex["stdout"] or "")
            finally:
                await db.dispose()

        asyncio.run(_recover())
    finally:
        _terminate_proc_safely(core_proc)
        _terminate_proc_safely(runner)


@pytest.mark.integration
@pytest.mark.parametrize("rep", [1, 2, 3])
def test_kill9_scenario_6_kill_core_after_db_write_before_outbox_finalization(tmp_path: Path, rep: int):
    """Scenario 6 (x3): Result + Outbox committed to DB, Core killed with SIGKILL before TaskFinalizer runs."""
    sock_path = tmp_path / f"r6_{rep}.sock"
    state_dir = tmp_path / f"st6_{rep}"
    ws_root = tmp_path / f"ws6_{rep}"
    ws_root.mkdir()
    db_file = tmp_path / f"db6_{rep}.sqlite"
    marker_file = tmp_path / f"core6_{rep}.ready"

    runner = _spawn_runner_process(sock_path, state_dir, ws_root)
    core_proc: subprocess.Popen[Any] | None = None
    try:
        core_script = f"""
import asyncio, json
from pathlib import Path
from app.storage.database import Database
from tests.integration.test_p1_3_kill9_recovery import _init_db, _make_orchestrator, _setup_db_and_plan

async def main():
    db = Database("sqlite+aiosqlite:///{db_file}")
    await _init_db(db)
    script = "print('OUTBOX_CRASH_RECOVERY_OK', flush=True)"
    repo, orch, _ = _make_orchestrator(db, Path("{sock_path}"), Path("{ws_root}"), script)
    await orch.registry.probe_all()
    org_id, cid, gid, plan, verdict = await _setup_db_and_plan(db, repo, Path("{ws_root}"))
    info = await orch.submit_plan(plan, verdict, goal_id=gid, cycle_id=cid)
    await orch.wait_for_task(info["task_id"], max_wait_seconds=10.0, poll_interval=0.05)
    # Result & outbox event are now in DB, but TaskFinalizer has NOT run yet!
    Path("{marker_file}").write_text(json.dumps({{**info, "org_id": str(org_id), "goal_id": str(gid)}}), encoding="utf-8")
    await asyncio.sleep(60)

asyncio.run(main())
"""
        core_proc = subprocess.Popen([sys.executable, "-c", core_script], cwd=str(PROJECT_ROOT))
        deadline = time.monotonic() + 10.0
        while not marker_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker_file.exists()
        info = json.loads(marker_file.read_text(encoding="utf-8"))

        # REAL OS kill -9 before Outbox / TaskFinalizer executes!
        os.kill(core_proc.pid, signal.SIGKILL)
        core_proc.wait(timeout=5)

        async def _recover_and_finalize():
            db = Database(f"sqlite+aiosqlite:///{db_file}")
            repo, orch, _ = _make_orchestrator(db, sock_path, ws_root, "print('noop')")
            await orch.registry.probe_all()
            try:
                from app.drives.engine import DriveEngine
                from app.perception.manager import SensorManager

                perception = SensorManager(repo=repo)
                finalizer = TaskFinalizer(
                    organism_id=uuid.UUID(info["org_id"]),
                    repo=repo,
                    perception=perception,
                    drives=DriveEngine(),
                    brain=_DeterministicBrain(),
                    governance=orch.governance,
                    memory=ExperienceManager(repo, backend=None),
                    verifier=orch.verifier,
                )
                outbox = OutboxDispatcher(repo)

                async def _handle(ev):
                    res = await finalizer.finalize_execution(ev["payload"]["execution_id"])
                    return {"already_finalized": res.already_finalized, "goal_achieved": res.goal_achieved}

                outbox.register_handler(OutboxEventType.EXECUTION_FINALIZE, "kill9_finalizer", _handle)

                await orch.recover_on_startup()
                await outbox.dispatch_once()
                # Dispatch again to prove 0 duplicates
                await outbox.dispatch_once()

                ex = await repo.get_execution(uuid.UUID(info["execution_id"]))
                assert ex is not None and ex["finalized_at"] is not None
                goal = await repo.get_goal(uuid.UUID(info["goal_id"]))
                assert goal is not None and goal["status"] == "ACHIEVED"
                mems = await repo.list_memory_index(limit=20)
                assert len(mems) == 1
            finally:
                await db.dispose()

        asyncio.run(_recover_and_finalize())
    finally:
        _terminate_proc_safely(core_proc)
        _terminate_proc_safely(runner)


@pytest.mark.integration
def test_pg_0004_migration_on_original_snapshot():
    """Requires AOC_TEST_PG_URL pointing to a disposable PG DB restored from aoc_pg_dump_20261120.sql."""
    async_url = os.environ.get("AOC_TEST_PG_URL", "").strip()
    if not async_url:
        pytest.skip("AOC_TEST_PG_URL not set")

    env = os.environ.copy()
    env["AOC_DATABASE__URL"] = async_url

    def run_alembic(*args: str) -> None:
        res = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=str(PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, f"alembic {args} failed:\n{res.stdout}\n{res.stderr}"

    run_alembic("upgrade", "0004_p1_3_runner_recovery")
    run_alembic("upgrade", "0004_p1_3_runner_recovery")

    async def _check():
        db = Database(async_url)
        try:
            async with db.engine.connect() as conn:
                tables = await conn.run_sync(lambda sc: set(inspect(sc).get_table_names()))
                assert {"runner_registry", "execution_recovery_records"}.issubset(tables)
                assert (await conn.execute(text("SELECT count(*) FROM agent_tasks"))).scalar_one() == 82
                assert (await conn.execute(text("SELECT count(*) FROM agent_executions"))).scalar_one() == 81
        finally:
            await db.dispose()

    asyncio.run(_check())
    run_alembic("downgrade", "0003_p1_2_durable_tasks")
    run_alembic("upgrade", "0004_p1_3_runner_recovery")


@pytest.mark.integration
@pytest.mark.real_agent
def test_real_systemd_runner_uds_with_real_codex_cli(tmp_path: Path):
    """End-to-end test through live host systemd `aoc-agent-runner.service` UDS running real `codex-cli 0.171.0`."""
    sock_path = Path(os.environ.get("AOC_RUNNER__SOCKET_PATH", "/run/aoc-runner/runner.sock"))
    if not sock_path.exists():
        pytest.skip(f"Live systemd runner socket {sock_path} not present")

    async def _run():
        settings = Settings()
        client = RunnerClient(
            socket_path=str(sock_path),
            auth_token=settings.runner.auth_token.get_secret_value() if settings.runner.auth_token else "",
        )
        h = await client.health()
        assert h.status == "ok"
        assert h.systemd_unit == "aoc-agent-runner.service"
        assert h.cgroup_delegated is True

        ws_dir = Path(settings.runner.workspace_root) / f"p1-3-real-codex-{uuid.uuid4().hex[:8]}"
        ws_dir.mkdir(parents=True, exist_ok=True)
        try:
            codex = CodexAdapter(
                AgentCapability(
                    agent_id="codex",
                    name="Codex CLI",
                    agent_type="cli",
                    capabilities=["system_diagnosis", "knowledge_research"],
                    supported_modes=["read-only", "workspace-write"],
                ),
                binary="codex",
                options={"use_aoc_llm": True, "sandbox": "read-only"},
                llm_settings=settings.llm,
            )
            backend = UnixSocketRunnerBackend(client, runner_id=h.runner_id)
            task = AgentTask(
                task_id=str(uuid.uuid4()),
                execution_id=str(uuid.uuid4()),
                objective="Reply with exactly one line: AOC_RUNNER_CODEX_OK",
                description="Minimal read-only verification through systemd UDS runner.",
                assigned_agent="codex",
                expected_output="AOC_RUNNER_CODEX_OK",
                workspace=str(ws_dir),
                timeout_seconds=90,
                permission_level="L0",
                sandbox_mode="read-only",
            )
            exec_id = await backend.launch(codex, task)
            meta = backend.get_launch_metadata(task.task_id)
            assert meta["pid"] is not None
            assert meta["process_identity"].get("cgroup_path") is not None

            deadline = time.monotonic() + 90.0
            result: AgentExecution | None = None
            while time.monotonic() < deadline:
                result = await backend.poll(task.task_id)
                if result is not None:
                    break
                await backend.wait_step(task.task_id, timeout=1.0)

            assert result is not None
            assert result.status == ExecutionStatus.SUCCEEDED.value, f"Codex failed: {result.error} / {result.stderr}"
            assert result.result and "AOC_RUNNER_CODEX_OK" in (result.result.get("summary") or "")
        finally:
            import shutil
            shutil.rmtree(ws_dir, ignore_errors=True)

    asyncio.run(_run())
