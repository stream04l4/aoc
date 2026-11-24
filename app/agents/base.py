"""Unified External Agent Interfaces (task book section 7)."""

from __future__ import annotations

import asyncio
import os
import signal
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.core.enums import ExecutionStatus
from app.perception.interfaces import utcnow


class AgentCapability(BaseModel):
    agent_id: str
    name: str
    agent_type: str
    capabilities: list[str] = Field(default_factory=list)
    supported_modes: list[str] = Field(default_factory=list)
    max_concurrency: int = 1
    enabled: bool = True
    experimental: bool = False


class AgentProbeResult(BaseModel):
    ok: bool
    binary: str = ""
    version: str = ""
    error: str | None = None
    checked_at: datetime = Field(default_factory=utcnow)


class AgentTask(BaseModel):
    task_id: str
    objective: str
    description: str = ""
    assigned_agent: str
    expected_output: str = ""
    workspace: str
    timeout_seconds: int = 480
    resource_budget: dict[str, Any] = Field(default_factory=dict)
    permission_level: str = "L0"
    success_criteria: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    # extra runtime hints
    sandbox_mode: str = "read-only"
    execution_id: str | None = None


class AgentStatus(BaseModel):
    execution_id: str
    agent_id: str
    status: str
    pid: int | None = None
    started_at: datetime | None = None
    elapsed_seconds: float = 0.0
    error: str | None = None


class AgentExecution(BaseModel):
    execution_id: str
    task_id: str
    agent_id: str
    status: str
    started_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime | None = None
    result: dict[str, Any] | None = None
    stdout: str | None = None
    stderr: str | None = None
    error: str | None = None
    resource_usage: dict[str, Any] = Field(default_factory=dict)
    pid: int | None = None
    thread_id: str | None = None


class AgentInterface(ABC):
    """All external AI agents MUST implement this interface (task book 7.1 & 17.8)."""

    @property
    @abstractmethod
    def capability(self) -> AgentCapability: ...

    @abstractmethod
    async def probe(self) -> AgentProbeResult: ...

    @abstractmethod
    async def execute(self, task: AgentTask) -> AgentExecution: ...

    @abstractmethod
    async def get_status(self, execution_id: str) -> AgentStatus: ...

    @abstractmethod
    async def cancel(self, execution_id: str) -> bool: ...

    def build_runner_spec(self, task: AgentTask) -> dict[str, Any] | None:
        """Optional spec for host Agent Runner execution: {command, stdin_text, extra_env}."""
        return None

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
        return AgentExecution(
            execution_id=task.execution_id or "",
            task_id=task.task_id,
            agent_id=self.capability.agent_id,
            status=status,
            started_at=started_at,
            completed_at=completed_at or utcnow(),
            result={"summary": (stdout or "").strip()[-4000:], "exit_code": exit_code},
            stdout=(stdout or "")[-20000:],
            stderr=(stderr or "")[-8000:],
            error=error,
            pid=pid,
        )


async def terminate_process_group(proc: asyncio.subprocess.Process, grace_seconds: float = 3.0) -> None:
    """Safely terminate a subprocess and its entire process group."""
    if proc.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass

    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
        return
    except asyncio.TimeoutError:
        pass

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        return
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except Exception:
        pass


def build_agent_prompt(task: AgentTask) -> str:
    criteria = "\n".join(f"- {c}" for c in task.success_criteria) or "- Provide a concise, verifiable answer."
    return (
        f"Objective:\n{task.objective}\n\n"
        f"Details:\n{task.description or 'None'}\n\n"
        f"Expected Output:\n{task.expected_output or 'Concise summary'}\n\n"
        f"Success Criteria:\n{criteria}\n\n"
        f"Constraints:\n"
        f"- Work ONLY inside the current workspace directory ({task.workspace}).\n"
        f"- Permission level: {task.permission_level} (sandbox: {task.sandbox_mode}).\n"
        f"- Do NOT access or modify files outside this workspace, system configs, credentials, or databases.\n"
        f"- Work concisely and directly: avoid redundant exploratory shell commands, write required deliverables in 1-2 clean steps, and finish well within the timeout.\n"
        f"- Complete within {task.timeout_seconds} seconds."
    )


def sanitize_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Strip AOC internal DB/secret env vars before spawning an external agent."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("AOC_") and not k.startswith("POSTGRES_")
    }
    if extra:
        env.update(extra)
    return env


def list_workspace_files(workspace: str) -> list[str]:
    files: list[str] = []
    for root, dirs, filenames in os.walk(workspace):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fn in filenames:
            if fn.startswith(".aoc_"):
                continue
            rel = os.path.relpath(os.path.join(root, fn), workspace)
            files.append(rel)
    return sorted(files)
