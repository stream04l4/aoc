"""Host Agent Runner Service: HTTP over Unix Domain Socket with cgroup v2 & crash recovery (V2.0 P1-3).

Runs as an unprivileged host systemd service (User=ubuntu, Delegate=yes, KillMode=process)
or in automatic fallback_pgroup mode during development/unit tests.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app.agents.base import sanitize_env
from app.core.enums import ExecutionStatus
from app.observability.logging import configure_logging, get_logger
from app.perception.interfaces import utcnow
from app.runner.cgroup import CgroupV2Manager
from app.runner.process_identity import (
    capture_process_identity,
    read_host_boot_id,
    verify_process_identity,
)
from app.runner.protocol import (
    CancelExecutionResponse,
    ExecutionRecordResponse,
    LaunchExecutionRequest,
    LaunchExecutionResponse,
    ProcessIdentity,
    RunnerHealthResponse,
)

logger = get_logger("aoc.runner.service")

# Lightweight POSIX sh supervisor shim:
#   - Redirects stdio to regular files (prevents SIGPIPE when Runner restarts/crashes)
#   - Spawns target command directly as child and atomically persists its exact PID
#   - Waits for child and atomically persists exact exit code (128+sig on signal death)
_SHIM_SCRIPT = r"""
"$@" < "$AOC_SHIM_STDIN" > "$AOC_SHIM_STDOUT" 2> "$AOC_SHIM_STDERR" &
child=$!
printf '%d\n' "$child" > "$AOC_SHIM_PID_TMP" && mv "$AOC_SHIM_PID_TMP" "$AOC_SHIM_PID_FILE"
wait "$child"
rc=$?
printf '%d\n' "$rc" > "$AOC_SHIM_RC_TMP" && mv "$AOC_SHIM_RC_TMP" "$AOC_SHIM_RC_FILE"
exit "$rc"
""".strip()

_TERMINAL_STATUSES = {
    ExecutionStatus.SUCCEEDED.value,
    ExecutionStatus.FAILED.value,
    ExecutionStatus.TIMEOUT.value,
    ExecutionStatus.CANCELLED.value,
}


class _PersistedExecution(BaseModel):
    execution_id: str
    task_id: str
    agent_id: str
    idempotency_key: str
    runner_id: str
    status: str = ExecutionStatus.STARTING.value
    isolation_mode: str = "fallback_pgroup"
    process_identity: ProcessIdentity | None = None
    exit_code: int | None = None
    started_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime | None = None
    error: str | None = None
    workspace: str = ""
    timeout_seconds: int = 300
    command: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    raw = json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    with open(tmp, "wb") as fh:
        fh.write(raw)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _read_tail_text(path: Path, max_bytes: int) -> str:
    if not path.exists():
        return ""
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


class RunnerManager:
    """Manages durable, cgroup-isolated subprocess executions with crash re-adoption."""

    def __init__(
        self,
        *,
        runner_id: str = "host-runner-1",
        socket_path: str = "/run/aoc-runner/runner.sock",
        state_dir: str = "/var/lib/aoc-runner",
        workspace_root: str = "/home/ubuntu/aoc-workspaces",
        cgroup_memory_max: str = "2G",
        cgroup_pids_max: int = 128,
        systemd_unit: str | None = None,
        auth_token: str | None = None,
        allow_cgroup: bool = True,
        poll_interval_seconds: float = 0.15,
    ) -> None:
        self.runner_id = runner_id
        self.socket_path = socket_path
        self.state_dir = Path(state_dir)
        self.executions_dir = self.state_dir / "executions"
        self.executions_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_root = Path(workspace_root).resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.systemd_unit = systemd_unit
        self.auth_token = auth_token or ""
        self.poll_interval = max(0.05, float(poll_interval_seconds))
        self.host_boot_id = read_host_boot_id()

        self.cgroup = CgroupV2Manager(
            memory_max=cgroup_memory_max,
            pids_max=cgroup_pids_max,
            allow_cgroup=allow_cgroup,
        )

        self._records: dict[str, _PersistedExecution] = {}
        self._by_idem_key: dict[str, str] = {}
        self._monitors: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()

    def _state_path(self, execution_id: str) -> Path:
        return self.executions_dir / f"{execution_id}.json"

    def _stdout_path(self, execution_id: str) -> Path:
        return self.executions_dir / f"{execution_id}.stdout"

    def _stderr_path(self, execution_id: str) -> Path:
        return self.executions_dir / f"{execution_id}.stderr"

    def _stdin_path(self, execution_id: str) -> Path:
        return self.executions_dir / f"{execution_id}.stdin"

    def _pid_path(self, execution_id: str) -> Path:
        return self.executions_dir / f"{execution_id}.child.pid"

    def _rc_path(self, execution_id: str) -> Path:
        return self.executions_dir / f"{execution_id}.exitcode"

    def _persist(self, rec: _PersistedExecution) -> None:
        _atomic_write_json(self._state_path(rec.execution_id), rec.model_dump(mode="json"))

    def _validate_workspace(self, workspace: str, task_id: str) -> Path:
        raw = workspace.strip() if workspace else ""
        if raw:
            candidate = Path(raw).resolve()
            if candidate == self.workspace_root or self.workspace_root in candidate.parents:
                candidate.mkdir(parents=True, exist_ok=True)
                return candidate
        safe_tid = "".join(ch for ch in task_id if ch.isalnum() or ch in {"-", "_"})[:64] or "task"
        fallback = (self.workspace_root / safe_tid).resolve()
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback

    async def recover_from_disk(self) -> dict[str, int]:
        """Reload persisted execution state files and re-adopt any live processes."""
        loaded = 0
        reattached = 0
        finalized = 0
        for fp in sorted(self.executions_dir.glob("*.json")):
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                rec = _PersistedExecution.model_validate(data)
            except Exception as exc:
                logger.warning("runner.recover_corrupt_file", path=str(fp), error=str(exc))
                continue
            self._records[rec.execution_id] = rec
            if rec.idempotency_key:
                self._by_idem_key[rec.idempotency_key] = rec.execution_id
            loaded += 1

            if rec.status in _TERMINAL_STATUSES:
                continue

            # If child PID file appeared while Runner crashed during STARTING, capture identity
            if rec.process_identity is None and self._pid_path(rec.execution_id).exists():
                try:
                    cpid = int(self._pid_path(rec.execution_id).read_text(encoding="utf-8").strip())
                    rec.process_identity = capture_process_identity(
                        execution_id=rec.execution_id,
                        idempotency_key=rec.idempotency_key,
                        pid=cpid,
                        runner_id=self.runner_id,
                        systemd_unit=self.systemd_unit,
                    )
                except Exception:
                    pass

            # Check if exitcode was already written while Runner was down
            rc = self._read_exit_code(rec.execution_id)
            if rc is not None:
                self._apply_exit_code(rec, rc)
                self._persist(rec)
                finalized += 1
                continue

            if rec.process_identity is not None:
                alive, reason = verify_process_identity(rec.process_identity)
                if alive:
                    rec.status = ExecutionStatus.RUNNING.value
                    self._persist(rec)
                    self._start_monitor(rec.execution_id)
                    reattached += 1
                    logger.info(
                        "runner.reattached_process",
                        execution_id=rec.execution_id,
                        pid=rec.process_identity.pid,
                        reason=reason,
                    )
                    continue

            # Process not alive and no exitcode yet — wait briefly for shim flush
            await asyncio.sleep(0.2)
            rc = self._read_exit_code(rec.execution_id)
            if rc is not None:
                self._apply_exit_code(rec, rc)
            else:
                rec.status = ExecutionStatus.FAILED.value
                rec.completed_at = utcnow()
                rec.error = "Process disappeared without exit code during runner recovery (likely SIGKILL or reboot)"
            if rec.process_identity and rec.process_identity.cgroup_path:
                self.cgroup.cleanup_cgroup(rec.process_identity.cgroup_path)
            self._persist(rec)
            finalized += 1

        logger.info("runner.recovered", loaded=loaded, reattached=reattached, finalized=finalized)
        return {"loaded": loaded, "reattached": reattached, "finalized": finalized}

    async def shutdown(self) -> None:
        """Stop monitoring tasks cleanly WITHOUT killing detached running child executions."""
        for task in list(self._monitors.values()):
            task.cancel()
        for task in list(self._monitors.values()):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._monitors.clear()

    async def launch(self, req: LaunchExecutionRequest) -> LaunchExecutionResponse:
        async with self._lock:
            # Step 3: Strict idempotency check prevents duplicate spawns on retry
            existing_id = self._by_idem_key.get(req.idempotency_key)
            if existing_id and existing_id in self._records:
                rec = self._records[existing_id]
                return LaunchExecutionResponse(
                    execution_id=rec.execution_id,
                    idempotency_key=rec.idempotency_key,
                    status=rec.status,
                    deduplicated=True,
                    process_identity=rec.process_identity,
                    runner_id=self.runner_id,
                    isolation_mode=rec.isolation_mode,
                )
            if req.execution_id in self._records:
                rec = self._records[req.execution_id]
                return LaunchExecutionResponse(
                    execution_id=rec.execution_id,
                    idempotency_key=rec.idempotency_key,
                    status=rec.status,
                    deduplicated=True,
                    process_identity=rec.process_identity,
                    runner_id=self.runner_id,
                    isolation_mode=rec.isolation_mode,
                )

            if not req.command:
                raise HTTPException(status_code=400, detail="command must be non-empty")

            workspace = self._validate_workspace(req.workspace, req.task_id)
            rec = _PersistedExecution(
                execution_id=req.execution_id,
                task_id=req.task_id,
                agent_id=req.agent_id,
                idempotency_key=req.idempotency_key,
                runner_id=self.runner_id,
                status=ExecutionStatus.STARTING.value,
                isolation_mode=self.cgroup.mode,
                workspace=str(workspace),
                timeout_seconds=max(1, int(req.timeout_seconds)),
                command=list(req.command),
                metadata=dict(req.metadata),
            )
            self._records[rec.execution_id] = rec
            self._by_idem_key[rec.idempotency_key] = rec.execution_id
            self._persist(rec)

        # Prepare stdio files & cgroup outside global lock
        stdin_path = self._stdin_path(rec.execution_id)
        stdout_path = self._stdout_path(rec.execution_id)
        stderr_path = self._stderr_path(rec.execution_id)
        pid_path = self._pid_path(rec.execution_id)
        rc_path = self._rc_path(rec.execution_id)

        stdin_path.write_text(req.stdin_text or "", encoding="utf-8")
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        for stale in (pid_path, rc_path):
            if stale.exists():
                stale.unlink(missing_ok=True)

        cg_dir = self.cgroup.create_execution_cgroup(rec.execution_id)
        sh_bin = shutil.which("sh") or "/bin/sh"

        shim_env = sanitize_env(
            {
                **req.extra_env,
                "AOC_SHIM_STDIN": str(stdin_path),
                "AOC_SHIM_STDOUT": str(stdout_path),
                "AOC_SHIM_STDERR": str(stderr_path),
                "AOC_SHIM_PID_TMP": str(pid_path) + ".tmp",
                "AOC_SHIM_PID_FILE": str(pid_path),
                "AOC_SHIM_RC_TMP": str(rc_path) + ".tmp",
                "AOC_SHIM_RC_FILE": str(rc_path),
            }
        )

        try:
            proc = subprocess.Popen(
                [sh_bin, "-c", _SHIM_SCRIPT, "aoc-runner-shim", *req.command],
                cwd=str(workspace),
                env=shim_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except Exception as exc:
            rec.status = ExecutionStatus.FAILED.value
            rec.completed_at = utcnow()
            rec.error = f"Failed to spawn subprocess: {exc}"
            self._persist(rec)
            return LaunchExecutionResponse(
                execution_id=rec.execution_id,
                idempotency_key=rec.idempotency_key,
                status=rec.status,
                deduplicated=False,
                process_identity=None,
                runner_id=self.runner_id,
                isolation_mode=rec.isolation_mode,
            )

        if cg_dir is not None:
            self.cgroup.attach_pid(cg_dir, proc.pid)

        # Wait briefly for shim to atomically write child's exact PID (typically <5ms)
        child_pid: int | None = None
        wait_deadline = time.monotonic() + 3.0
        while time.monotonic() < wait_deadline:
            if pid_path.exists():
                try:
                    child_pid = int(pid_path.read_text(encoding="utf-8").strip())
                    break
                except (ValueError, OSError):
                    pass
            if proc.poll() is not None:
                break
            await asyncio.sleep(0.01)

        target_pid = child_pid or proc.pid
        if cg_dir is not None and child_pid and child_pid != proc.pid:
            self.cgroup.attach_pid(cg_dir, child_pid)

        identity = capture_process_identity(
            execution_id=rec.execution_id,
            idempotency_key=rec.idempotency_key,
            pid=target_pid,
            shim_pid=proc.pid,
            runner_id=self.runner_id,
            cgroup_path=str(cg_dir) if cg_dir is not None else None,
            systemd_unit=self.systemd_unit,
        )
        rec.process_identity = identity
        rec.status = ExecutionStatus.RUNNING.value

        # Handle ultra-fast commands that already exited before handshake finished
        rc = self._read_exit_code(rec.execution_id)
        if rc is not None:
            self._apply_exit_code(rec, rc)
            if identity.cgroup_path:
                self.cgroup.cleanup_cgroup(identity.cgroup_path)
            self._persist(rec)
        else:
            self._persist(rec)
            self._start_monitor(rec.execution_id, popen=proc)

        return LaunchExecutionResponse(
            execution_id=rec.execution_id,
            idempotency_key=rec.idempotency_key,
            status=rec.status,
            deduplicated=False,
            process_identity=rec.process_identity,
            runner_id=self.runner_id,
            isolation_mode=rec.isolation_mode,
        )

    def _start_monitor(self, execution_id: str, popen: subprocess.Popen[Any] | None = None) -> None:
        if execution_id in self._monitors and not self._monitors[execution_id].done():
            return
        self._monitors[execution_id] = asyncio.create_task(
            self._monitor_loop(execution_id, popen=popen),
            name=f"runner-mon-{execution_id[:8]}",
        )

    async def _monitor_loop(self, execution_id: str, popen: subprocess.Popen[Any] | None = None) -> None:
        try:
            while True:
                rec = self._records.get(execution_id)
                if rec is None or rec.status in _TERMINAL_STATUSES:
                    return

                # Reap shim if it's our direct child (prevents zombie shims)
                if popen is not None:
                    popen.poll()

                rc = self._read_exit_code(execution_id)
                if rc is not None:
                    self._apply_exit_code(rec, rc)
                    if rec.process_identity and rec.process_identity.cgroup_path:
                        self.cgroup.cleanup_cgroup(rec.process_identity.cgroup_path)
                    self._persist(rec)
                    return

                # Check timeout
                elapsed = (utcnow() - rec.started_at).total_seconds()
                if elapsed >= rec.timeout_seconds:
                    logger.warning("runner.execution_timeout", execution_id=execution_id, timeout_seconds=rec.timeout_seconds)
                    ident = rec.process_identity
                    self.cgroup.kill_execution(
                        ident.cgroup_path if ident else None,
                        pgid=ident.process_group_id if ident else None,
                        pid=ident.pid if ident else None,
                    )
                    if popen is not None:
                        try:
                            popen.wait(timeout=1.0)
                        except subprocess.TimeoutExpired:
                            popen.kill()
                    rec.status = ExecutionStatus.TIMEOUT.value
                    rec.completed_at = utcnow()
                    rec.error = f"Execution timed out after {rec.timeout_seconds}s"
                    if ident and ident.cgroup_path:
                        self.cgroup.cleanup_cgroup(ident.cgroup_path)
                    self._persist(rec)
                    return

                # Check whether child process is still alive via ProcessIdentity
                if rec.process_identity is not None:
                    alive, reason = verify_process_identity(rec.process_identity)
                    if not alive:
                        # Give shim a brief grace window to flush .exitcode after child exits/dies
                        await asyncio.sleep(0.15)
                        if popen is not None:
                            popen.poll()
                        rc = self._read_exit_code(execution_id)
                        if rc is not None:
                            self._apply_exit_code(rec, rc)
                        else:
                            rec.status = ExecutionStatus.FAILED.value
                            rec.completed_at = utcnow()
                            rec.error = f"Process terminated without exit code ({reason})"
                        if rec.process_identity.cgroup_path:
                            self.cgroup.cleanup_cgroup(rec.process_identity.cgroup_path)
                        self._persist(rec)
                        return

                await asyncio.sleep(self.poll_interval)
        except asyncio.CancelledError:
            # Runner shutting down — leave detached child running!
            return
        finally:
            self._monitors.pop(execution_id, None)

    def _read_exit_code(self, execution_id: str) -> int | None:
        rc_path = self._rc_path(execution_id)
        if not rc_path.exists():
            return None
        try:
            return int(rc_path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            return None

    def _apply_exit_code(self, rec: _PersistedExecution, rc: int) -> None:
        rec.exit_code = rc
        rec.completed_at = rec.completed_at or utcnow()
        if rec.status in {ExecutionStatus.CANCELLED.value, ExecutionStatus.TIMEOUT.value}:
            return
        if rc == 0:
            rec.status = ExecutionStatus.SUCCEEDED.value
            rec.error = None
        else:
            rec.status = ExecutionStatus.FAILED.value
            if rc > 128:
                sig = rc - 128
                rec.error = f"Process killed by signal {sig} (exit code {rc})"
            else:
                rec.error = f"Process exited with non-zero code {rc}"

    def _to_response(self, rec: _PersistedExecution) -> ExecutionRecordResponse:
        alive = False
        check_reason = "terminal"
        if rec.status not in _TERMINAL_STATUSES and rec.process_identity is not None:
            alive, check_reason = verify_process_identity(rec.process_identity)
        return ExecutionRecordResponse(
            execution_id=rec.execution_id,
            task_id=rec.task_id,
            agent_id=rec.agent_id,
            idempotency_key=rec.idempotency_key,
            runner_id=rec.runner_id,
            status=rec.status,
            isolation_mode=rec.isolation_mode,
            process_identity=rec.process_identity,
            exit_code=rec.exit_code,
            started_at=rec.started_at,
            completed_at=rec.completed_at,
            stdout=_read_tail_text(self._stdout_path(rec.execution_id), 65536),
            stderr=_read_tail_text(self._stderr_path(rec.execution_id), 16384),
            error=rec.error,
            workspace=rec.workspace,
            alive=alive,
            identity_check=check_reason,
            metadata=rec.metadata,
        )

    async def get_execution(self, execution_id: str) -> ExecutionRecordResponse | None:
        rec = self._records.get(execution_id)
        if rec is None:
            return None
        # Opportunistic refresh if exitcode landed just now
        if rec.status not in _TERMINAL_STATUSES:
            rc = self._read_exit_code(execution_id)
            if rc is not None:
                self._apply_exit_code(rec, rc)
                self._persist(rec)
        return self._to_response(rec)

    async def get_execution_by_key(self, idempotency_key: str) -> ExecutionRecordResponse | None:
        eid = self._by_idem_key.get(idempotency_key)
        if not eid:
            return None
        return await self.get_execution(eid)

    async def list_executions(self, status: str | None = None, limit: int = 100) -> list[ExecutionRecordResponse]:
        items = list(self._records.values())
        items.sort(key=lambda r: r.started_at, reverse=True)
        if status:
            items = [r for r in items if r.status == status]
        return [self._to_response(r) for r in items[:limit]]

    async def cancel(self, execution_id: str, reason: str = "cancelled by AOC") -> CancelExecutionResponse:
        rec = self._records.get(execution_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"Execution {execution_id} not found")
        if rec.status in _TERMINAL_STATUSES:
            return CancelExecutionResponse(
                execution_id=execution_id,
                cancelled=False,
                status=rec.status,
                reason="already terminal",
            )
        rec.status = ExecutionStatus.CANCELLED.value
        rec.completed_at = utcnow()
        rec.error = reason
        ident = rec.process_identity
        if ident is not None:
            self.cgroup.kill_execution(ident.cgroup_path, pgid=ident.process_group_id, pid=ident.pid)
            if ident.cgroup_path:
                self.cgroup.cleanup_cgroup(ident.cgroup_path)
        self._persist(rec)
        return CancelExecutionResponse(
            execution_id=execution_id,
            cancelled=True,
            status=rec.status,
            reason=reason,
        )

    def health(self) -> RunnerHealthResponse:
        active = sum(1 for r in self._records.values() if r.status not in _TERMINAL_STATUSES)
        return RunnerHealthResponse(
            status="ok",
            runner_id=self.runner_id,
            host_boot_id=self.host_boot_id,
            systemd_unit=self.systemd_unit,
            cgroup_delegated=self.cgroup.delegated,
            cgroup_root=str(self.cgroup.root_dir) if self.cgroup.root_dir else None,
            socket_path=self.socket_path,
            state_dir=str(self.state_dir),
            active_executions=active,
            total_tracked_executions=len(self._records),
        )


def bind_uds_socket_0660(socket_path: str | Path) -> socket.socket:
    """Create and bind a Unix Domain Socket with strict 0o660 permissions (never 0o666)."""
    sock_p = Path(socket_path)
    sock_p.parent.mkdir(parents=True, exist_ok=True)
    if sock_p.exists():
        try:
            sock_p.unlink()
        except OSError:
            pass
    prev_umask = os.umask(0o117)
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(sock_p))
        os.chmod(sock_p, 0o660)
        sock.listen(128)
        sock.setblocking(False)
        return sock
    finally:
        os.umask(prev_umask)


def create_runner_app(manager: RunnerManager) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Defense-in-depth: ensure least-privilege 0o660 on the UDS socket if already present
        sock_p = Path(manager.socket_path)
        if sock_p.exists():
            try:
                os.chmod(sock_p, 0o660)
            except OSError as exc:
                logger.warning("runner.socket_chmod_failed", socket_path=manager.socket_path, error=str(exc))
        await manager.recover_from_disk()
        try:
            yield
        finally:
            await manager.shutdown()

    app = FastAPI(title="AOC Host Agent Runner", version="2.0.0", lifespan=lifespan)

    def _require_token(x_aoc_runner_token: str | None = Header(default=None)) -> None:
        if not manager.auth_token:
            return
        if not x_aoc_runner_token or not secrets.compare_digest(x_aoc_runner_token, manager.auth_token):
            raise HTTPException(status_code=401, detail="Invalid or missing X-AOC-Runner-Token")

    @app.get("/v1/health", response_model=RunnerHealthResponse)
    async def health() -> RunnerHealthResponse:
        return manager.health()

    @app.get("/v1/probe", dependencies=[Depends(_require_token)])
    async def probe_binary(binary: str) -> dict[str, Any]:
        """Probe an agent CLI binary on the host where Runner executes it (supports containerized Core)."""
        b = (binary or "").strip()
        if not b or any(ch in b for ch in (";", "&", "|", "$", "`", "\n", "\r", " ")):
            raise HTTPException(status_code=400, detail="Invalid binary name")
        resolved = shutil.which(b)
        if not resolved:
            return {"ok": False, "binary": b, "version": "", "error": f"binary '{b}' not found on host runner"}
        try:
            proc = await asyncio.create_subprocess_exec(
                resolved,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=10)
            lines = (out.decode(errors="replace") or err.decode(errors="replace")).strip().splitlines()
            if proc.returncode == 0:
                return {"ok": True, "binary": resolved, "version": lines[0] if lines else "unknown", "error": None}
            return {"ok": False, "binary": resolved, "version": "", "error": f"exit {proc.returncode}: {lines[:1]}"}
        except Exception as exc:
            return {"ok": False, "binary": resolved, "version": "", "error": str(exc)}

    @app.post("/v1/executions", response_model=LaunchExecutionResponse, dependencies=[Depends(_require_token)])
    async def launch_execution(req: LaunchExecutionRequest) -> LaunchExecutionResponse:
        return await manager.launch(req)

    @app.get("/v1/executions", response_model=list[ExecutionRecordResponse], dependencies=[Depends(_require_token)])
    async def list_executions(status: str | None = None, limit: int = 100) -> list[ExecutionRecordResponse]:
        return await manager.list_executions(status=status, limit=min(max(limit, 1), 500))

    @app.get(
        "/v1/executions/by-key/{idempotency_key:path}",
        response_model=ExecutionRecordResponse,
        dependencies=[Depends(_require_token)],
    )
    async def get_execution_by_key(idempotency_key: str) -> ExecutionRecordResponse:
        res = await manager.get_execution_by_key(idempotency_key)
        if res is None:
            raise HTTPException(status_code=404, detail="Execution not found for idempotency_key")
        return res

    @app.get(
        "/v1/executions/{execution_id}",
        response_model=ExecutionRecordResponse,
        dependencies=[Depends(_require_token)],
    )
    async def get_execution(execution_id: str) -> ExecutionRecordResponse:
        res = await manager.get_execution(execution_id)
        if res is None:
            raise HTTPException(status_code=404, detail="Execution not found")
        return res

    @app.post(
        "/v1/executions/{execution_id}/cancel",
        response_model=CancelExecutionResponse,
        dependencies=[Depends(_require_token)],
    )
    async def cancel_execution(execution_id: str, reason: str = "cancelled by AOC") -> CancelExecutionResponse:
        return await manager.cancel(execution_id, reason=reason)

    return app


def main() -> None:
    import uvicorn
    from app.core.config import get_runner_config, get_settings

    settings = get_settings()
    rcfg = get_runner_config()
    configure_logging(settings.logging.level, settings.logging.json_output)

    runner_id = os.environ.get("AOC_RUNNER__RUNNER_ID") or str(rcfg.get("runner_id", "host-runner-1"))
    socket_path = os.environ.get("AOC_RUNNER__SOCKET_PATH") or str(rcfg.get("socket_path", "/run/aoc-runner/runner.sock"))
    state_dir = os.environ.get("AOC_RUNNER__STATE_DIR") or str(rcfg.get("state_dir", "/var/lib/aoc-runner"))
    workspace_root = os.environ.get("AOC_RUNNER__WORKSPACE_ROOT") or str(
        rcfg.get("workspace_root", "/home/ubuntu/aoc-workspaces")
    )
    mem_max = os.environ.get("AOC_RUNNER__CGROUP_MEMORY_MAX") or str(rcfg.get("cgroup_memory_max", "2G"))
    pids_max = int(os.environ.get("AOC_RUNNER__CGROUP_PIDS_MAX") or rcfg.get("cgroup_pids_max", 128))
    systemd_unit = os.environ.get("AOC_RUNNER__SYSTEMD_UNIT") or None
    auth_token = os.environ.get("AOC_RUNNER__AUTH_TOKEN") or (
        settings.runner.auth_token.get_secret_value() if settings.runner.auth_token else ""
    )

    sock_p = Path(socket_path)
    sock_p.parent.mkdir(parents=True, exist_ok=True)
    if sock_p.exists():
        try:
            sock_p.unlink()
        except OSError:
            pass

    # Ensure socket created with 0660 so group ubuntu (and UID 1000 container) can connect
    os.umask(0o007)

    manager = RunnerManager(
        runner_id=runner_id,
        socket_path=socket_path,
        state_dir=state_dir,
        workspace_root=workspace_root,
        cgroup_memory_max=mem_max,
        cgroup_pids_max=pids_max,
        systemd_unit=systemd_unit,
        auth_token=auth_token,
    )
    app = create_runner_app(manager)
    bound_sock = bind_uds_socket_0660(socket_path)
    config = uvicorn.Config(app, log_level=settings.logging.level.lower())
    server = uvicorn.Server(config)
    try:
        asyncio.run(server.serve(sockets=[bound_sock]))
    finally:
        try:
            bound_sock.close()
        except OSError:
            pass
        if sock_p.exists():
            try:
                sock_p.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    main()
