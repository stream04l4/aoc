"""Codex Adapter: non-interactive `codex exec` with strict sandbox & workspace isolation."""

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
from app.core.config import LLMSettings
from app.core.enums import ExecutionStatus
from app.perception.interfaces import utcnow

ALLOWED_SANDBOXES = {"read-only", "workspace-write"}


@dataclass
class _RunningProc:
    proc: asyncio.subprocess.Process
    started_at: datetime
    status: str = ExecutionStatus.RUNNING.value
    cancelled: bool = False


class CodexAdapter(AgentInterface):
    def __init__(
        self,
        cap: AgentCapability,
        binary: str = "codex",
        options: dict[str, Any] | None = None,
        llm_settings: LLMSettings | None = None,
    ) -> None:
        self._cap = cap
        self.binary = binary
        self.options = dict(options or {})
        self._llm_settings = llm_settings
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
            out, err = await asyncio.wait_for(proc.communicate(), timeout=10)
            ver = (out.decode(errors="replace") or err.decode(errors="replace")).strip().splitlines()
            if proc.returncode == 0:
                return AgentProbeResult(ok=True, binary=resolved, version=ver[0] if ver else "unknown")
            return AgentProbeResult(ok=False, binary=resolved, error=f"exit {proc.returncode}: {ver[:1]}")
        except Exception as exc:
            return AgentProbeResult(ok=False, binary=resolved, error=str(exc))

    async def execute(self, task: AgentTask) -> AgentExecution:
        exec_id = task.execution_id or str(uuid.uuid4())
        started = utcnow()
        t0 = time.perf_counter()

        # Hard security invariant (task book 7.2): NEVER allow danger-full-access or bypass flags
        sandbox = task.sandbox_mode or str(self.options.get("sandbox", "read-only"))
        if sandbox not in ALLOWED_SANDBOXES:
            return AgentExecution(
                execution_id=exec_id,
                task_id=task.task_id,
                agent_id=self._cap.agent_id,
                status=ExecutionStatus.FAILED.value,
                started_at=started,
                completed_at=utcnow(),
                error=f"Refused disallowed sandbox mode '{sandbox}' (allowed: {sorted(ALLOWED_SANDBOXES)})",
            )

        resolved = shutil.which(self.binary)
        if not resolved:
            return AgentExecution(
                execution_id=exec_id,
                task_id=task.task_id,
                agent_id=self._cap.agent_id,
                status=ExecutionStatus.FAILED.value,
                started_at=started,
                completed_at=utcnow(),
                error=f"Codex binary '{self.binary}' not found",
            )

        os.makedirs(task.workspace, exist_ok=True)
        last_msg_path = os.path.join(task.workspace, ".aoc_last_message.txt")
        if os.path.exists(last_msg_path):
            try:
                os.remove(last_msg_path)
            except OSError:
                pass

        cmd = [
            resolved,
            "exec",
            "--sandbox",
            sandbox,
            "-C",
            task.workspace,
            "--skip-git-repo-check",
            "--json",
            "-o",
            last_msg_path,
        ]
        if not bool(self.options.get("persist_session", False)):
            cmd.append("--ephemeral")
        if bool(self.options.get("worktree", False)):
            cmd.append("--worktree")
        extra_env: dict[str, str] = {}
        if self.options.get("use_aoc_llm"):
            llm_cfg = self._llm_settings
            base_url = str(self.options.get("base_url") or (llm_cfg.base_url if llm_cfg else ""))
            model_name = str(self.options.get("model") or (llm_cfg.model if llm_cfg else ""))
            if not base_url or not model_name:
                return AgentExecution(
                    execution_id=exec_id,
                    task_id=task.task_id,
                    agent_id=self._cap.agent_id,
                    status=ExecutionStatus.FAILED.value,
                    started_at=started,
                    completed_at=utcnow(),
                    error="CodexAdapter use_aoc_llm=true requires injected llm_settings or options.base_url/model",
                )
            cmd.extend(
                [
                    "-c",
                    'model_provider="aoc_llm"',
                    "-c",
                    'model_providers.aoc_llm.name="AOC_LLM"',
                    "-c",
                    f'model_providers.aoc_llm.base_url="{base_url}"',
                    "-c",
                    'model_providers.aoc_llm.env_key="CODEX_CUSTOM_API_KEY"',
                    "-c",
                    'model_providers.aoc_llm.wire_api="responses"',
                    "-m",
                    model_name,
                ]
            )
            if llm_cfg and llm_cfg.api_key:
                extra_env["CODEX_CUSTOM_API_KEY"] = llm_cfg.api_key.get_secret_value()
        else:
            model = self.options.get("model")
            if model:
                cmd.extend(["-m", str(model)])
        cmd.append("-")  # prompt read safely from stdin

        prompt = build_agent_prompt(task)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=task.workspace,
                env=sanitize_env(extra_env),
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
                error=f"Failed to start codex exec: {exc}",
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
                err_msg = f"codex exec exited with code {proc.returncode}"
        except asyncio.TimeoutError:
            await terminate_process_group(proc)
            status = ExecutionStatus.TIMEOUT.value
            err_msg = f"codex exec timed out after {task.timeout_seconds}s"
        except asyncio.CancelledError:
            rp.cancelled = True
            await terminate_process_group(proc)
            status = ExecutionStatus.CANCELLED.value
            err_msg = "Task cancelled"
        finally:
            self._running.pop(exec_id, None)

        elapsed = round(time.perf_counter() - t0, 3)
        completed = utcnow()
        parsed_result, usage, thread_id = self._parse_output(stdout_str, last_msg_path, task.workspace)
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
            thread_id=thread_id,
        )

    def _parse_output(
        self, stdout: str, last_msg_path: str, workspace: str
    ) -> tuple[dict[str, Any], dict[str, Any], str | None]:
        events = 0
        usage: dict[str, Any] = {}
        last_agent_text = ""
        thread_id: str | None = None
        tool_calls: list[dict[str, Any]] = []

        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
                events += 1
                if isinstance(ev, dict):
                    if ev.get("type") == "thread.started" and ev.get("thread_id"):
                        thread_id = str(ev["thread_id"])
                    if ev.get("type") == "turn.completed" and isinstance(ev.get("usage"), dict):
                        usage.update(ev["usage"])
                    item = ev.get("item")
                    if isinstance(item, dict):
                        itype = str(item.get("type") or "")
                        if itype == "agent_message":
                            txt = item.get("text") or ""
                            if txt:
                                last_agent_text = txt
                        elif itype in {"command_execution", "mcp_tool_call", "file_change"}:
                            tool_calls.append(
                                {
                                    "id": item.get("id"),
                                    "type": itype,
                                    "command": item.get("command") or item.get("tool") or item.get("name"),
                                    "status": item.get("status"),
                                }
                            )
            except json.JSONDecodeError:
                continue

        if os.path.exists(last_msg_path):
            try:
                with open(last_msg_path, "r", encoding="utf-8", errors="replace") as fh:
                    file_msg = fh.read().strip()
                if file_msg:
                    last_agent_text = file_msg
            except OSError:
                pass

        return (
            {
                "summary": last_agent_text,
                "events_count": events,
                "thread_id": thread_id,
                "tool_calls": tool_calls[:50],
                "workspace_files": list_workspace_files(workspace),
            },
            usage,
            thread_id,
        )

    async def get_status(self, execution_id: str) -> AgentStatus:
        rp = self._running.get(execution_id)
        if rp is not None:
            elapsed = (utcnow() - rp.started_at).total_seconds()
            return AgentStatus(
                execution_id=execution_id,
                agent_id=self._cap.agent_id,
                status=ExecutionStatus.RUNNING.value,
                pid=rp.proc.pid,
                started_at=rp.started_at,
                elapsed_seconds=round(elapsed, 2),
            )
        if execution_id in self._finished:
            return self._finished[execution_id]
        return AgentStatus(
            execution_id=execution_id,
            agent_id=self._cap.agent_id,
            status=ExecutionStatus.FAILED.value,
            error="unknown execution_id",
        )

    def build_runner_spec(self, task: AgentTask) -> dict[str, Any] | None:
        sandbox = task.sandbox_mode or str(self.options.get("sandbox", "read-only"))
        if sandbox not in ALLOWED_SANDBOXES:
            raise ValueError(f"Refused disallowed sandbox mode '{sandbox}' (allowed: {sorted(ALLOWED_SANDBOXES)})")
        resolved = shutil.which(self.binary) or getattr(self, "_runner_binary_path", None)
        if not resolved:
            raise FileNotFoundError(f"Codex binary '{self.binary}' not found")

        os.makedirs(task.workspace, exist_ok=True)
        last_msg_path = os.path.join(task.workspace, ".aoc_last_message.txt")
        if os.path.exists(last_msg_path):
            try:
                os.remove(last_msg_path)
            except OSError:
                pass

        cmd = [
            resolved,
            "exec",
            "--sandbox",
            sandbox,
            "-C",
            task.workspace,
            "--skip-git-repo-check",
            "--json",
            "-o",
            last_msg_path,
        ]
        if not bool(self.options.get("persist_session", False)):
            cmd.append("--ephemeral")
        if bool(self.options.get("worktree", False)):
            cmd.append("--worktree")
        extra_env: dict[str, str] = {}
        if self.options.get("use_aoc_llm"):
            llm_cfg = self._llm_settings
            base_url = str(self.options.get("base_url") or (llm_cfg.base_url if llm_cfg else ""))
            model_name = str(self.options.get("model") or (llm_cfg.model if llm_cfg else ""))
            if not base_url or not model_name:
                raise ValueError("CodexAdapter use_aoc_llm=true requires injected llm_settings or options.base_url/model")
            cmd.extend(
                [
                    "-c",
                    'model_provider="aoc_llm"',
                    "-c",
                    'model_providers.aoc_llm.name="AOC_LLM"',
                    "-c",
                    f'model_providers.aoc_llm.base_url="{base_url}"',
                    "-c",
                    'model_providers.aoc_llm.env_key="CODEX_CUSTOM_API_KEY"',
                    "-c",
                    'model_providers.aoc_llm.wire_api="responses"',
                    "-m",
                    model_name,
                ]
            )
            if llm_cfg and llm_cfg.api_key:
                extra_env["CODEX_CUSTOM_API_KEY"] = llm_cfg.api_key.get_secret_value()
        else:
            model = self.options.get("model")
            if model:
                cmd.extend(["-m", str(model)])
        cmd.append("-")
        return {
            "command": cmd,
            "stdin_text": build_agent_prompt(task),
            "extra_env": extra_env,
        }

    def build_resume_spec(
        self,
        task: AgentTask,
        thread_id: str,
        *,
        idempotent_or_confirmed: bool = False,
    ) -> dict[str, Any]:
        """Controlled `codex exec resume <thread_id>` spec (Task Book 5.7.2).

        Hard guard: refuses automatic resume unless the caller explicitly certifies
        the task is idempotent or operator-confirmed.
        """
        if not idempotent_or_confirmed:
            raise PermissionError(
                "Refused codex exec resume: only idempotent or operator-confirmed tasks may be resumed"
            )
        if not thread_id or not thread_id.strip():
            raise ValueError("thread_id is required for codex exec resume")
        resolved = shutil.which(self.binary) or getattr(self, "_runner_binary_path", None)
        if not resolved:
            raise FileNotFoundError(f"Codex binary '{self.binary}' not found")

        last_msg_path = os.path.join(task.workspace, ".aoc_last_message.txt")
        cmd = [
            resolved,
            "exec",
            "resume",
            "--skip-git-repo-check",
            "--json",
            "-o",
            last_msg_path,
        ]
        extra_env: dict[str, str] = {}
        if self.options.get("use_aoc_llm"):
            llm_cfg = self._llm_settings
            base_url = str(self.options.get("base_url") or (llm_cfg.base_url if llm_cfg else ""))
            model_name = str(self.options.get("model") or (llm_cfg.model if llm_cfg else ""))
            if base_url and model_name:
                cmd.extend(
                    [
                        "-c",
                        'model_provider="aoc_llm"',
                        "-c",
                        'model_providers.aoc_llm.name="AOC_LLM"',
                        "-c",
                        f'model_providers.aoc_llm.base_url="{base_url}"',
                        "-c",
                        'model_providers.aoc_llm.env_key="CODEX_CUSTOM_API_KEY"',
                        "-c",
                        'model_providers.aoc_llm.wire_api="responses"',
                        "-m",
                        model_name,
                    ]
                )
                if llm_cfg and llm_cfg.api_key:
                    extra_env["CODEX_CUSTOM_API_KEY"] = llm_cfg.api_key.get_secret_value()
        cmd.extend([thread_id.strip(), "-"])
        return {"command": cmd, "stdin_text": build_agent_prompt(task), "extra_env": extra_env}

    def parse_runner_result(
        self,
        *,
        task: AgentTask,
        status: str,
        stdout: str,
        stderr: str,
        exit_code: int | None,
        error: str | None,
        started_at: datetime,
        completed_at: datetime | None,
        pid: int | None,
    ) -> AgentExecution:
        last_msg_path = os.path.join(task.workspace, ".aoc_last_message.txt")
        parsed_result, usage, thread_id = self._parse_output(stdout or "", last_msg_path, task.workspace)
        done_at = completed_at or utcnow()
        try:
            usage["wall_seconds"] = round(max(0.0, (done_at - started_at).total_seconds()), 3)
        except Exception:
            usage["wall_seconds"] = 0.0
        return AgentExecution(
            execution_id=task.execution_id or "",
            task_id=task.task_id,
            agent_id=self._cap.agent_id,
            status=status,
            started_at=started_at,
            completed_at=done_at,
            result=parsed_result,
            stdout=(stdout or "")[-20000:],
            stderr=(stderr or "")[-8000:],
            error=error,
            resource_usage=usage,
            pid=pid,
            thread_id=thread_id,
        )

    async def cancel(self, execution_id: str) -> bool:
        rp = self._running.get(execution_id)
        if rp is None:
            return False
        rp.cancelled = True
        await terminate_process_group(rp.proc)
        return True
