"""Generic CLI Adapter extension point (for future CLI agents such as DSH)."""

from __future__ import annotations

import asyncio
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.agents.base import (
    AgentCapability,
    AgentExecution,
    AgentInterface,
    AgentProbeResult,
    AgentStatus,
    AgentTask,
    build_agent_prompt,
    list_workspace_files,
    sanitize_env,
    terminate_process_group,
)
from app.core.enums import ExecutionStatus
from app.perception.interfaces import utcnow


@dataclass
class _RunningProc:
    proc: asyncio.subprocess.Process
    started_at: datetime
    cancelled: bool = False


class GenericCLIAdapter(AgentInterface):
    """Template-driven CLI adapter; disabled until a valid command_template and binary are verified."""

    def __init__(
        self,
        cap: AgentCapability,
        binary: str,
        options: dict[str, Any] | None = None,
    ) -> None:
        self._cap = cap
        self.binary = binary
        self.options = dict(options or {})
        self.command_template: list[str] = list(self.options.get("command_template") or [])
        self._running: dict[str, _RunningProc] = {}
        self._finished: dict[str, AgentStatus] = {}

    @property
    def capability(self) -> AgentCapability:
        return self._cap

    async def probe(self) -> AgentProbeResult:
        if not self.command_template:
            return AgentProbeResult(ok=False, binary=self.binary, error="command_template not configured")
        resolved = shutil.which(self.binary)
        if not resolved:
            return AgentProbeResult(ok=False, binary=self.binary, error=f"binary '{self.binary}' not found")
        return AgentProbeResult(ok=True, binary=resolved, version="configured-template")

    async def execute(self, task: AgentTask) -> AgentExecution:
        exec_id = task.execution_id or str(uuid.uuid4())
        started = utcnow()
        t0 = time.perf_counter()

        if not self.command_template:
            return AgentExecution(
                execution_id=exec_id,
                task_id=task.task_id,
                agent_id=self._cap.agent_id,
                status=ExecutionStatus.FAILED.value,
                started_at=started,
                completed_at=utcnow(),
                error="GenericCLIAdapter has no configured command_template",
            )

        os.makedirs(task.workspace, exist_ok=True)
        prompt = build_agent_prompt(task)
        cmd = [
            part.format(
                binary=shutil.which(self.binary) or self.binary,
                workspace=task.workspace,
                prompt=prompt,
                timeout=task.timeout_seconds,
            )
            for part in self.command_template
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=task.workspace,
                env=sanitize_env(),
                start_new_session=True,
            )
        except Exception as exc:
            return AgentExecution(
                execution_id=exec_id,
                task_id=task.task_id,
                agent_id=self._cap.agent_id,
                status=ExecutionStatus.FAILED.value,
                started_at=started,
                completed_at=utcnow(),
                error=f"Failed to start generic CLI agent: {exc}",
            )

        rp = _RunningProc(proc=proc, started_at=started)
        self._running[exec_id] = rp

        status = ExecutionStatus.SUCCEEDED.value
        err_msg: str | None = None
        stdout_str = ""
        stderr_str = ""

        try:
            out_b, err_b = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")),
                timeout=max(5, int(task.timeout_seconds)),
            )
            stdout_str = out_b.decode("utf-8", errors="replace")
            stderr_str = err_b.decode("utf-8", errors="replace")
            if rp.cancelled:
                status = ExecutionStatus.CANCELLED.value
                err_msg = "Cancelled by AOC"
            elif proc.returncode != 0:
                status = ExecutionStatus.FAILED.value
                err_msg = f"generic CLI exited with code {proc.returncode}"
        except asyncio.TimeoutError:
            await terminate_process_group(proc)
            status = ExecutionStatus.TIMEOUT.value
            err_msg = f"generic CLI timed out after {task.timeout_seconds}s"
        finally:
            self._running.pop(exec_id, None)

        elapsed = round(time.perf_counter() - t0, 3)
        completed = utcnow()
        self._finished[exec_id] = AgentStatus(
            execution_id=exec_id,
            agent_id=self._cap.agent_id,
            status=status,
            pid=proc.pid,
            started_at=started,
            elapsed_seconds=elapsed,
            error=err_msg,
        )
        return AgentExecution(
            execution_id=exec_id,
            task_id=task.task_id,
            agent_id=self._cap.agent_id,
            status=status,
            started_at=started,
            completed_at=completed,
            result={"summary": stdout_str.strip(), "workspace_files": list_workspace_files(task.workspace)},
            stdout=stdout_str[-20000:],
            stderr=stderr_str[-8000:],
            error=err_msg,
            resource_usage={"wall_seconds": elapsed},
            pid=proc.pid,
        )

    async def get_status(self, execution_id: str) -> AgentStatus:
        rp = self._running.get(execution_id)
        if rp is not None:
            return AgentStatus(
                execution_id=execution_id,
                agent_id=self._cap.agent_id,
                status=ExecutionStatus.RUNNING.value,
                pid=rp.proc.pid,
                started_at=rp.started_at,
                elapsed_seconds=round((utcnow() - rp.started_at).total_seconds(), 2),
            )
        if execution_id in self._finished:
            return self._finished[execution_id]
        return AgentStatus(
            execution_id=execution_id,
            agent_id=self._cap.agent_id,
            status=ExecutionStatus.FAILED.value,
            error="unknown execution_id",
        )

    async def cancel(self, execution_id: str) -> bool:
        rp = self._running.get(execution_id)
        if rp is None:
            return False
        rp.cancelled = True
        await terminate_process_group(rp.proc)
        return True
