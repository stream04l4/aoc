"""Hermes Adapter (EXPERIMENTAL — task book 7.2).

Uses Hermes Agent's verified single-query CLI mode:
    hermes chat --query-file - --oneshot -Q --format stream-json --in <workspace> ...

Marked experimental=True because Hermes v0.23.2 does not expose a native
status/cancel machine API for oneshot runs; status and cancellation are managed
at the subprocess process-group level.
"""

from __future__ import annotations

import asyncio
import json
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


class HermesAdapter(AgentInterface):
    def __init__(
        self,
        cap: AgentCapability,
        binary: str = "hermes",
        options: dict[str, Any] | None = None,
    ) -> None:
        # Force experimental flag per task book 7.2
        self._cap = cap.model_copy(update={"experimental": True})
        self.binary = binary
        self.options = dict(options or {})
        self._running: dict[str, _RunningProc] = {}
        self._finished: dict[str, AgentStatus] = {}

    @property
    def capability(self) -> AgentCapability:
        return self._cap

    async def probe(self) -> AgentProbeResult:
        resolved = shutil.which(self.binary)
        if not resolved:
            return AgentProbeResult(ok=False, binary=self.binary, error=f"binary '{self.binary}' not found")
        try:
            proc = await asyncio.create_subprocess_exec(
                resolved,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=15)
            lines = (out.decode(errors="replace") or err.decode(errors="replace")).strip().splitlines()
            if proc.returncode == 0:
                return AgentProbeResult(ok=True, binary=resolved, version=lines[0] if lines else "unknown")
            return AgentProbeResult(ok=False, binary=resolved, error=f"exit {proc.returncode}: {lines[:1]}")
        except Exception as exc:
            return AgentProbeResult(ok=False, binary=resolved, error=str(exc))

    async def execute(self, task: AgentTask) -> AgentExecution:
        exec_id = task.execution_id or str(uuid.uuid4())
        started = utcnow()
        t0 = time.perf_counter()

        resolved = shutil.which(self.binary)
        if not resolved:
            return AgentExecution(
                execution_id=exec_id,
                task_id=task.task_id,
                agent_id=self._cap.agent_id,
                status=ExecutionStatus.FAILED.value,
                started_at=started,
                completed_at=utcnow(),
                error=f"Hermes binary '{self.binary}' not found",
            )

        os.makedirs(task.workspace, exist_ok=True)
        max_turns = int(self.options.get("max_turns", 20))
        toolsets = self.options.get("toolsets")
        safe_mode = bool(self.options.get("safe_mode", True))
        ignore_user_cfg = bool(self.options.get("ignore_user_config", False))

        cmd = [
            resolved,
            "chat",
            "--query-file",
            "-",
            "--oneshot",
            "-Q",
            "--format",
            "stream-json",
            "--in",
            task.workspace,
            "--max-turns",
            str(max_turns),
        ]
        if safe_mode:
            cmd.append("--safe-mode")
            from app.core.config import get_settings

            llm_cfg = get_settings().llm
            cmd.extend(
                [
                    "--provider",
                    "custom",
                    "--base-url",
                    str(self.options.get("base_url") or llm_cfg.base_url),
                    "-m",
                    str(self.options.get("model") or llm_cfg.model),
                ]
            )
            if llm_cfg.api_key:
                cmd.extend(["--api-key", llm_cfg.api_key.get_secret_value()])
        if ignore_user_cfg:
            cmd.append("--ignore-user-config")
        if toolsets:
            cmd.extend(["-t", str(toolsets)])

        prompt = build_agent_prompt(task)
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
                error=f"Failed to start hermes: {exc}",
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
                timeout=max(10, int(task.timeout_seconds) + 10),
            )
            stdout_str = out_b.decode("utf-8", errors="replace")
            stderr_str = err_b.decode("utf-8", errors="replace")
            if rp.cancelled:
                status = ExecutionStatus.CANCELLED.value
                err_msg = "Cancelled by AOC"
            elif proc.returncode != 0:
                status = ExecutionStatus.FAILED.value
                err_msg = f"hermes exited with code {proc.returncode}"
        except asyncio.TimeoutError:
            await terminate_process_group(proc)
            status = ExecutionStatus.TIMEOUT.value
            err_msg = f"hermes timed out after {task.timeout_seconds}s"
        except asyncio.CancelledError:
            rp.cancelled = True
            await terminate_process_group(proc)
            status = ExecutionStatus.CANCELLED.value
            err_msg = "Task cancelled"
        finally:
            self._running.pop(exec_id, None)

        elapsed = round(time.perf_counter() - t0, 3)
        completed = utcnow()
        parsed_result, usage = self._parse_stream_json(stdout_str, task.workspace)
        usage["wall_seconds"] = elapsed

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
            result=parsed_result,
            stdout=stdout_str[-20000:] if stdout_str else "",
            stderr=stderr_str[-8000:] if stderr_str else "",
            error=err_msg,
            resource_usage=usage,
            pid=proc.pid,
        )

    def _parse_stream_json(self, stdout: str, workspace: str) -> tuple[dict[str, Any], dict[str, Any]]:
        events = 0
        final_text = ""
        usage: dict[str, Any] = {}
        plain_lines: list[str] = []

        for line in (stdout or "").splitlines():
            s = line.strip()
            if not s:
                continue
            if s.startswith("{"):
                try:
                    ev = json.loads(s)
                    events += 1
                    if isinstance(ev, dict):
                        for key in ("content", "text", "response", "message", "final_response"):
                            val = ev.get(key)
                            if isinstance(val, str) and val.strip():
                                final_text = val.strip()
                        if isinstance(ev.get("usage"), dict):
                            usage.update(ev["usage"])
                    continue
                except json.JSONDecodeError:
                    pass
            plain_lines.append(s)

        if not final_text and plain_lines:
            final_text = "\n".join(plain_lines).strip()

        return (
            {
                "summary": final_text,
                "events_count": events,
                "experimental": True,
                "workspace_files": list_workspace_files(workspace),
            },
            usage,
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
