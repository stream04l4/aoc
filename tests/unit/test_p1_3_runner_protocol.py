"""P1-3 unit tests: ProcessIdentity, PID reuse defense, UDS runner protocol, fallback mode, API & Alembic 0004."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from app.agents.base import AgentCapability, AgentExecution, AgentInterface, AgentProbeResult, AgentStatus, AgentTask
from app.agents.registry import AgentRegistry
from app.api.app import create_app
from app.core.config import ApiSettings, RunnerSettings, Settings
from app.core.enums import ExecutionStatus, PermissionLevel, TaskStatus
from app.core.runtime import OrganismRuntime
from app.governance.policy import GovernanceManager
from app.orchestration.orchestrator import AgentOrchestrator
from app.runner.cgroup import CgroupV2Manager
from app.runner.client import RunnerClient, UnixSocketRunnerBackend
from app.runner.process_identity import (
    capture_process_identity,
    parse_proc_stat,
    read_host_boot_id,
    verify_process_identity,
)
from app.runner.protocol import LaunchExecutionRequest
from app.runner.service import RunnerManager, create_runner_app
from app.storage.database import Database
from app.storage.models import Base
from app.storage.repository import StateRepository


async def _init_db(db: Database) -> None:
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


async def _start_uds_runner_in_loop(
    manager: RunnerManager, socket_path: str
) -> tuple[uvicorn.Server, asyncio.Task[None]]:
    app = create_runner_app(manager)
    config = uvicorn.Config(app, uds=socket_path, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if Path(socket_path).exists() and server.started:
            break
        await asyncio.sleep(0.02)
    return server, task


async def _stop_uds_runner_in_loop(server: uvicorn.Server, task: asyncio.Task[None]) -> None:
    server.should_exit = True
    try:
        await asyncio.wait_for(task, timeout=5.0)
    except Exception:
        task.cancel()


def test_parse_proc_stat_handles_spaces_and_parens():
    sample = "4242 (my (weird) cmd) S 1 4242 4242 0 -1 4194304 100 0 0 0 1 2 0 0 20 0 1 0 987654321 1000 50"
    parsed = parse_proc_stat(sample)
    assert parsed == ("S", 987654321)


def test_process_identity_verification_and_pid_reuse_rejection(tmp_path: Path):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"])
    try:
        ident = capture_process_identity(
            execution_id="exec-1",
            idempotency_key="idem-1",
            pid=proc.pid,
            runner_id="test-runner",
        )
        assert ident.pid == proc.pid
        assert ident.process_start_ticks > 0
        assert ident.host_boot_id == read_host_boot_id()

        ok, reason = verify_process_identity(ident)
        assert ok is True and reason == "alive_verified"

        # Simulate PID reuse: same PID exists, but starttime ticks differ -> MUST reject!
        reused_ident = ident.model_copy(update={"process_start_ticks": ident.process_start_ticks + 42})
        ok_reuse, reason_reuse = verify_process_identity(reused_ident)
        assert ok_reuse is False
        assert "start_ticks_mismatch" in reason_reuse

        # Simulate host reboot: boot_id differs -> MUST reject!
        reboot_ident = ident.model_copy(update={"host_boot_id": "00000000-0000-0000-0000-000000000000"})
        ok_boot, reason_boot = verify_process_identity(reboot_ident)
        assert ok_boot is False
        assert reason_boot == "host_boot_id_mismatch"

        # Simulate cgroup mismatch: pid not in expected cgroup.procs -> MUST reject!
        fake_cg = tmp_path / "exec-fake"
        fake_cg.mkdir()
        (fake_cg / "cgroup.procs").write_text("999999\n", encoding="utf-8")
        cg_ident = ident.model_copy(update={"cgroup_path": str(fake_cg)})
        ok_cg, reason_cg = verify_process_identity(cg_ident)
        assert ok_cg is False
        assert reason_cg == "pid_not_in_expected_cgroup"
    finally:
        proc.kill()
        proc.wait(timeout=5)

    ok_dead, _ = verify_process_identity(ident)
    assert ok_dead is False


def test_cgroup_manager_degrades_cleanly_without_delegation(tmp_path: Path):
    fake_cgroup = tmp_path / "self_cgroup"
    fake_cgroup.write_text("0::/nonexistent/unwritable/path\n", encoding="utf-8")
    mgr = CgroupV2Manager(proc_self_cgroup=fake_cgroup, cgroup_fs_root=tmp_path / "cgfs")
    assert mgr.delegated is False
    assert mgr.mode == "fallback_pgroup"
    assert mgr.create_execution_cgroup("exec-1") is None


async def test_runner_fallback_mode_idempotent_launch_token_auth_and_cancel(tmp_path: Path):
    sock_path = str(tmp_path / "runner.sock")
    state_dir = str(tmp_path / "state")
    ws_root = tmp_path / "workspaces"
    ws_root.mkdir()
    task_ws = ws_root / "task-1"
    task_ws.mkdir()

    mgr = RunnerManager(
        runner_id="unit-runner-1",
        socket_path=sock_path,
        state_dir=state_dir,
        workspace_root=str(ws_root),
        auth_token="runner-secret-xyz",
        allow_cgroup=False,
        poll_interval_seconds=0.05,
    )
    server, srv_task = await _start_uds_runner_in_loop(mgr, sock_path)
    try:
        # 1. Missing/invalid auth token rejected with 401
        bad_client = RunnerClient(socket_path=sock_path, auth_token="wrong")
        with pytest.raises(RuntimeError, match="401"):
            await bad_client.launch(
                LaunchExecutionRequest(
                    execution_id=str(uuid.uuid4()),
                    task_id="task-1",
                    agent_id="cmd",
                    idempotency_key="bad-auth",
                    command=[sys.executable, "-c", "print('hi')"],
                    workspace=str(task_ws),
                )
            )

        client = RunnerClient(socket_path=sock_path, auth_token="runner-secret-xyz")
        h = await client.health()
        assert h.status == "ok"
        assert h.runner_id == "unit-runner-1"
        assert h.cgroup_delegated is False

        # 2. Idempotent launch: same idempotency_key MUST NOT spawn duplicate process
        exec_id = str(uuid.uuid4())
        req = LaunchExecutionRequest(
            execution_id=exec_id,
            task_id="task-1",
            agent_id="cmd",
            idempotency_key="idem-sleep-1",
            command=[sys.executable, "-c", "import time; print('started', flush=True); time.sleep(20)"],
            workspace=str(task_ws),
            timeout_seconds=30,
        )
        r1 = await client.launch(req)
        assert r1.deduplicated is False
        assert r1.process_identity is not None
        assert r1.isolation_mode == "fallback_pgroup"
        orig_pid = r1.process_identity.pid

        r2 = await client.launch(req.model_copy(update={"execution_id": str(uuid.uuid4())}))
        assert r2.deduplicated is True
        assert r2.execution_id == exec_id
        assert r2.process_identity is not None
        assert r2.process_identity.pid == orig_pid

        # 3. Cancel cleanly kills process group (0 orphans)
        cresp = await client.cancel(exec_id, reason="unit test cancel")
        assert cresp.cancelled is True
        assert cresp.status == ExecutionStatus.CANCELLED.value
        await asyncio.sleep(0.2)
        alive, _ = verify_process_identity(r1.process_identity)
        assert alive is False

        # 4. Stdin prompt delivery works cleanly under POSIX sh async backgrounding
        stdin_exec = str(uuid.uuid4())
        await client.launch(
            LaunchExecutionRequest(
                execution_id=stdin_exec,
                task_id="task-stdin",
                agent_id="cmd",
                idempotency_key="idem-stdin-1",
                command=[sys.executable, "-c", "import sys; print('GOT:' + sys.stdin.read().strip())"],
                stdin_text="HELLO_AOC_STDIN",
                workspace=str(task_ws),
                timeout_seconds=10,
            )
        )
        for _ in range(50):
            rec = await client.get_execution(stdin_exec)
            if rec and rec.status == ExecutionStatus.SUCCEEDED.value:
                break
            await asyncio.sleep(0.05)
        assert rec is not None and rec.status == ExecutionStatus.SUCCEEDED.value
        assert "GOT:HELLO_AOC_STDIN" in rec.stdout
    finally:
        await _stop_uds_runner_in_loop(server, srv_task)


async def test_orchestrator_reconcile_marks_recovery_required_when_runner_offline(tmp_path: Path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'reconcile_offline.db'}")
    await _init_db(db)
    repo = StateRepository(db)
    settings = Settings()
    gov = GovernanceManager(settings.governance.model_copy(update={"workspace_root": str(tmp_path / "ws")}), repo)
    reg = AgentRegistry(repo=repo)

    # Point backend at a non-existent UDS socket -> offline runner
    offline_backend = UnixSocketRunnerBackend(
        RunnerClient(socket_path=str(tmp_path / "missing.sock")),
        runner_id="offline-runner",
    )
    orch = AgentOrchestrator(registry=reg, governance=gov, repo=repo, backend=offline_backend)

    task_id = await repo.create_task(
        objective="L1 write task interrupted before runner confirm",
        assigned_agent="codex",
        workspace=str(tmp_path / "ws" / "t1"),
        timeout_seconds=60,
        permission_level=PermissionLevel.L1.value,
        status=TaskStatus.DISPATCHING.value,
        attempt=1,
        max_attempts=1,
    )
    exec_id = await repo.create_execution(
        task_id,
        "codex",
        ExecutionStatus.DISPATCHING.value,
        attempt_number=1,
        idempotency_key=f"exec:{task_id}:1",
    )

    res = await orch.reconcile_task(str(task_id), trigger="startup")
    assert res["status"] == TaskStatus.RECOVERY_REQUIRED.value
    assert res["action"] == "marked_recovery_required_runner_offline"

    t_row = await repo.get_task(task_id)
    assert t_row is not None
    assert t_row["status"] == TaskStatus.RECOVERY_REQUIRED.value

    recs = await repo.list_recovery_records(execution_id=exec_id)
    assert len(recs) == 1
    assert recs[0]["action_taken"] == "marked_recovery_required_runner_offline"
    assert recs[0]["resolved_status"] == TaskStatus.RECOVERY_REQUIRED.value
    await db.dispose()


async def test_v2_runner_and_reconcile_api_endpoints(tmp_path: Path):
    sock_path = str(tmp_path / "api_runner.sock")
    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    mgr = RunnerManager(
        runner_id="api-runner-1",
        socket_path=sock_path,
        state_dir=str(tmp_path / "rstate"),
        workspace_root=str(ws_root),
        allow_cgroup=False,
    )
    server, srv_task = await _start_uds_runner_in_loop(mgr, sock_path)
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'api_runner.db'}")
    await _init_db(db)
    try:
        settings = Settings(
            api=ApiSettings(host="127.0.0.1", auth_enabled=True, admin_key="admin-runner-key"),
            runner=RunnerSettings(
                enabled=True,
                runner_id="api-runner-1",
                socket_path=sock_path,
                workspace_root=str(ws_root),
            ),
        )
        rt = OrganismRuntime(settings=settings, db=db, auto_load_agents=False, llm_probe_fn=lambda: None)
        app = create_app(settings=settings, db=db, runtime=rt, start_scheduler=False)
        headers = {"Authorization": "Bearer admin-runner-key"}

        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                # Unauthenticated -> 401
                assert (await client.get("/v2/runners")).status_code == 401

                # Authenticated -> lists online host runner
                r = await client.get("/v2/runners", headers=headers)
                assert r.status_code == 200
                runners = r.json()
                assert len(runners) == 1
                assert runners[0]["runner_id"] == "api-runner-1"
                assert runners[0]["status"] == "online"

                # Executions list
                ex_r = await client.get("/v2/runners/api-runner-1/executions", headers=headers)
                assert ex_r.status_code == 200
                assert ex_r.json() == []

                # Manual reconcile on terminal task returns already_terminal
                tid = await rt.repo.create_task(
                    objective="already done",
                    assigned_agent="codex",
                    workspace=str(ws_root / "done"),
                    timeout_seconds=30,
                    permission_level="L0",
                    status=TaskStatus.SUCCEEDED.value,
                )
                rec_r = await client.post(f"/v2/tasks/{tid}/reconcile", headers=headers)
                assert rec_r.status_code == 200
                assert rec_r.json()["action"] == "already_terminal"
    finally:
        await db.dispose()
        await _stop_uds_runner_in_loop(server, srv_task)


def test_alembic_0004_upgrade_and_downgrade_roundtrip(tmp_path: Path):
    db_file = tmp_path / "alembic_0004.db"
    env = os.environ.copy()
    env["AOC_DATABASE__URL"] = f"sqlite+aiosqlite:///{db_file}"

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
    run_alembic("downgrade", "0003_p1_2_durable_tasks")
    run_alembic("upgrade", "0004_p1_3_runner_recovery")
