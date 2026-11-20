"""ExecutionBackendInterface (V2.0 P1-2 -> P1-3 seam).

Decouples AgentOrchestrator from how external agent processes are spawned and tracked:
  - LocalSubprocessBackend (P1-2 default, preserves full unit-test & local runtime compatibility)
  - UnixSocketRunnerBackend (P1-3 host systemd Agent Runner over UDS + cgroup v2)
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

from app.agents.base import AgentExecution, AgentInterface, AgentTask
from app.core.enums import ExecutionStatus
from app.perception.interfaces import utcnow


class ExecutionBackendInterface(ABC):
    @abstractmethod
    async def launch(self, adapter: AgentInterface, task: AgentTask) -> str: ...

    @abstractmethod
    def is_running(self, task_id: str) -> bool: ...

    @abstractmethod
    def list_running_task_ids(self) -> list[str]: ...

    @abstractmethod
    async def poll(self, task_id: str) -> AgentExecution | None:
        """Non-blocking check: returns AgentExecution once finished, else None if still running/absent."""

    @abstractmethod
    async def wait_step(self, task_id: str, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds for completion. Returns True if finished."""

    @abstractmethod
    async def cancel(self, task_id: str, adapter: AgentInterface | None = None) -> tuple[bool, str | None]:
        """Cancel a running task. Returns (cancelled, execution_id)."""


class LocalSubprocessBackend(ExecutionBackendInterface):
    """In-process asyncio.Task backend wrapping AgentInterface.execute()."""

    def __init__(self) -> None:
        self._inflight: dict[str, tuple[str, asyncio.Task[AgentExecution]]] = {}

    async def launch(self, adapter: AgentInterface, task: AgentTask) -> str:
        exec_id = task.execution_id or ""

        async def _run() -> AgentExecution:
            try:
                return await adapter.execute(task)
            except Exception as exc:
                return AgentExecution(
                    execution_id=exec_id,
                    task_id=task.task_id,
                    agent_id=adapter.capability.agent_id,
                    status=ExecutionStatus.FAILED.value,
                    completed_at=utcnow(),
                    error=f"Unhandled adapter exception: {exc}",
                )

        bg = asyncio.create_task(_run(), name=f"aoc-exec-{exec_id[:8]}")
        self._inflight[task.task_id] = (exec_id, bg)
        return exec_id

    def is_running(self, task_id: str) -> bool:
        return task_id in self._inflight

    def list_running_task_ids(self) -> list[str]:
        return list(self._inflight.keys())

    async def poll(self, task_id: str) -> AgentExecution | None:
        entry = self._inflight.get(task_id)
        if entry is None:
            return None
        _, bg = entry
        if not bg.done():
            return None
        self._inflight.pop(task_id, None)
        if bg.cancelled():
            return None
        return bg.result()

    async def wait_step(self, task_id: str, timeout: float) -> bool:
        entry = self._inflight.get(task_id)
        if entry is None:
            return True
        _, bg = entry
        if bg.done():
            return True
        try:
            await asyncio.wait_for(asyncio.shield(bg), timeout=max(0.01, timeout))
            return True
        except asyncio.TimeoutError:
            return False
        except asyncio.CancelledError:
            if bg.cancelled():
                return True
            raise

    async def cancel(self, task_id: str, adapter: AgentInterface | None = None) -> tuple[bool, str | None]:
        entry = self._inflight.get(task_id)
        if entry is None:
            return False, None
        exec_id, bg = entry
        if adapter is not None:
            await adapter.cancel(exec_id)
        bg.cancel()
        try:
            await bg
        except (asyncio.CancelledError, Exception):
            pass
        self._inflight.pop(task_id, None)
        return True, exec_id
