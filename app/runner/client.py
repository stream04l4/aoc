"""Unix Domain Socket client and ExecutionBackendInterface implementation (V2.0 P1-3)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from app.agents.base import AgentExecution, AgentInterface, AgentTask
from app.core.enums import ExecutionStatus
from app.observability.logging import get_logger
from app.orchestration.backend import ExecutionBackendInterface
from app.runner.protocol import (
    CancelExecutionResponse,
    ExecutionRecordResponse,
    LaunchExecutionRequest,
    LaunchExecutionResponse,
    RunnerHealthResponse,
)

logger = get_logger("aoc.runner.client")

_TERMINAL_EXEC_STATUSES = {
    ExecutionStatus.SUCCEEDED.value,
    ExecutionStatus.FAILED.value,
    ExecutionStatus.TIMEOUT.value,
    ExecutionStatus.CANCELLED.value,
}


class RunnerUnavailableError(RuntimeError):
    """Raised when the host Agent Runner UDS endpoint cannot be reached."""


class RunnerClient:
    """Async HTTP-over-Unix-Domain-Socket client for Host Agent Runner."""

    def __init__(
        self,
        socket_path: str = "/run/aoc-runner/runner.sock",
        auth_token: str = "",
        timeout_seconds: float = 30.0,
    ) -> None:
        self.socket_path = socket_path
        self.auth_token = auth_token
        self.timeout_seconds = timeout_seconds

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.auth_token:
            headers["X-AOC-Runner-Token"] = self.auth_token
        return headers

    def _make_client(self) -> httpx.AsyncClient:
        transport = httpx.AsyncHTTPTransport(uds=self.socket_path)
        return httpx.AsyncClient(
            transport=transport,
            base_url="http://aoc-runner",
            headers=self._headers(),
            timeout=self.timeout_seconds,
        )

    async def health(self) -> RunnerHealthResponse:
        try:
            async with self._make_client() as client:
                r = await client.get("/v1/health")
                r.raise_for_status()
                return RunnerHealthResponse.model_validate(r.json())
        except Exception as exc:
            raise RunnerUnavailableError(f"Runner health check failed on {self.socket_path}: {exc}") from exc

    async def probe_binary(self, binary: str) -> dict[str, Any]:
        try:
            async with self._make_client() as client:
                r = await client.get("/v1/probe", params={"binary": binary})
                r.raise_for_status()
                return dict(r.json())
        except Exception as exc:
            raise RunnerUnavailableError(f"Runner probe failed on {self.socket_path}: {exc}") from exc

    async def launch(self, req: LaunchExecutionRequest) -> LaunchExecutionResponse:
        try:
            async with self._make_client() as client:
                r = await client.post("/v1/executions", json=req.model_dump(mode="json"))
                r.raise_for_status()
                return LaunchExecutionResponse.model_validate(r.json())
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"Runner launch rejected ({exc.response.status_code}): {exc.response.text}") from exc
        except Exception as exc:
            raise RunnerUnavailableError(f"Runner launch failed on {self.socket_path}: {exc}") from exc

    async def get_execution(self, execution_id: str) -> ExecutionRecordResponse | None:
        try:
            async with self._make_client() as client:
                r = await client.get(f"/v1/executions/{execution_id}")
                if r.status_code == 404:
                    return None
                r.raise_for_status()
                return ExecutionRecordResponse.model_validate(r.json())
        except Exception as exc:
            raise RunnerUnavailableError(f"Runner get_execution failed: {exc}") from exc

    async def get_execution_by_key(self, idempotency_key: str) -> ExecutionRecordResponse | None:
        try:
            async with self._make_client() as client:
                r = await client.get(f"/v1/executions/by-key/{idempotency_key}")
                if r.status_code == 404:
                    return None
                r.raise_for_status()
                return ExecutionRecordResponse.model_validate(r.json())
        except Exception as exc:
            raise RunnerUnavailableError(f"Runner get_execution_by_key failed: {exc}") from exc

    async def list_executions(self, status: str | None = None, limit: int = 100) -> list[ExecutionRecordResponse]:
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        try:
            async with self._make_client() as client:
                r = await client.get("/v1/executions", params=params)
                r.raise_for_status()
                return [ExecutionRecordResponse.model_validate(item) for item in r.json()]
        except Exception as exc:
            raise RunnerUnavailableError(f"Runner list_executions failed: {exc}") from exc

    async def cancel(self, execution_id: str, reason: str = "cancelled by AOC") -> CancelExecutionResponse:
        try:
            async with self._make_client() as client:
                r = await client.post(f"/v1/executions/{execution_id}/cancel", params={"reason": reason})
                r.raise_for_status()
                return CancelExecutionResponse.model_validate(r.json())
        except Exception as exc:
            raise RunnerUnavailableError(f"Runner cancel failed: {exc}") from exc


@dataclass
class _TrackedRunnerTask:
    execution_id: str
    adapter: AgentInterface
    task: AgentTask
    runner_id: str
    process_identity: dict[str, Any]


class UnixSocketRunnerBackend(ExecutionBackendInterface):
    """ExecutionBackendInterface delegating process lifecycle to Host Agent Runner over UDS."""

    def __init__(
        self,
        client: RunnerClient,
        runner_id: str = "host-runner-1",
        instance_id: str | None = None,
    ) -> None:
        self.client = client
        self.runner_id = runner_id
        self.instance_id = instance_id
        self._tracked: dict[str, _TrackedRunnerTask] = {}

    async def launch(self, adapter: AgentInterface, task: AgentTask) -> str:
        exec_id = task.execution_id or ""
        spec = adapter.build_runner_spec(task)
        if spec is None:
            raise NotImplementedError(
                f"Adapter '{adapter.capability.agent_id}' does not support UnixSocketRunnerBackend (build_runner_spec returned None)"
            )
        idem_key = str(task.resource_budget.get("execution_idempotency_key") or f"exec:{task.task_id}:{exec_id}")
        req = LaunchExecutionRequest(
            execution_id=exec_id,
            task_id=task.task_id,
            agent_id=adapter.capability.agent_id,
            idempotency_key=idem_key,
            command=spec["command"],
            stdin_text=spec.get("stdin_text", ""),
            workspace=task.workspace,
            sandbox_mode=task.sandbox_mode,
            timeout_seconds=int(task.timeout_seconds),
            instance_id=self.instance_id,
            extra_env=spec.get("extra_env", {}),
            metadata={"objective": task.objective[:200]},
        )
        resp = await self.client.launch(req)
        pi_dict = resp.process_identity.model_dump(mode="json") if resp.process_identity else {}
        self._tracked[task.task_id] = _TrackedRunnerTask(
            execution_id=resp.execution_id,
            adapter=adapter,
            task=task,
            runner_id=resp.runner_id,
            process_identity=pi_dict,
        )
        return resp.execution_id

    def get_launch_metadata(self, task_id: str) -> dict[str, Any]:
        tr = self._tracked.get(task_id)
        if tr is None:
            return {}
        return {
            "runner_id": tr.runner_id,
            "pid": tr.process_identity.get("pid"),
            "process_identity": tr.process_identity,
        }

    def reattach(
        self,
        *,
        task_id: str,
        execution_id: str,
        adapter: AgentInterface,
        task: AgentTask,
        runner_id: str,
        process_identity: dict[str, Any] | None = None,
    ) -> None:
        """Re-attach monitoring for an already running execution after AOC Core restart."""
        self._tracked[task_id] = _TrackedRunnerTask(
            execution_id=execution_id,
            adapter=adapter,
            task=task,
            runner_id=runner_id,
            process_identity=dict(process_identity or {}),
        )

    def is_running(self, task_id: str) -> bool:
        return task_id in self._tracked

    def list_running_task_ids(self) -> list[str]:
        return list(self._tracked.keys())

    def convert_record(
        self,
        adapter: AgentInterface | None,
        task: AgentTask,
        rec: ExecutionRecordResponse,
    ) -> AgentExecution:
        if adapter is not None:
            return adapter.parse_runner_result(
                task=task,
                status=rec.status,
                stdout=rec.stdout,
                stderr=rec.stderr,
                exit_code=rec.exit_code,
                error=rec.error,
                started_at=rec.started_at,
                completed_at=rec.completed_at,
                pid=rec.process_identity.pid if rec.process_identity else None,
            )
        return AgentExecution(
            execution_id=rec.execution_id,
            task_id=rec.task_id,
            agent_id=rec.agent_id,
            status=rec.status,
            started_at=rec.started_at,
            completed_at=rec.completed_at,
            result={"summary": (rec.stdout or "").strip()[-2000:], "exit_code": rec.exit_code},
            stdout=rec.stdout[-20000:],
            stderr=rec.stderr[-8000:],
            error=rec.error,
            pid=rec.process_identity.pid if rec.process_identity else None,
        )

    async def poll(self, task_id: str) -> AgentExecution | None:
        tr = self._tracked.get(task_id)
        if tr is None:
            return None
        try:
            rec = await self.client.get_execution(tr.execution_id)
        except RunnerUnavailableError as exc:
            logger.warning("runner_backend.poll_unavailable", task_id=task_id, error=str(exc))
            return None
        if rec is None:
            self._tracked.pop(task_id, None)
            return AgentExecution(
                execution_id=tr.execution_id,
                task_id=task_id,
                agent_id=tr.adapter.capability.agent_id,
                status=ExecutionStatus.FAILED.value,
                error="Execution record missing on host runner",
            )
        if rec.status not in _TERMINAL_EXEC_STATUSES:
            return None
        self._tracked.pop(task_id, None)
        return self.convert_record(tr.adapter, tr.task, rec)

    async def wait_step(self, task_id: str, timeout: float) -> bool:
        tr = self._tracked.get(task_id)
        if tr is None:
            return True
        deadline = asyncio.get_running_loop().time() + max(0.05, timeout)
        while True:
            try:
                rec = await self.client.get_execution(tr.execution_id)
                if rec is None or rec.status in _TERMINAL_EXEC_STATUSES:
                    return True
            except RunnerUnavailableError:
                pass
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.15, remaining))

    async def cancel(self, task_id: str, adapter: AgentInterface | None = None) -> tuple[bool, str | None]:
        tr = self._tracked.get(task_id)
        if tr is None:
            return False, None
        try:
            resp = await self.client.cancel(tr.execution_id)
            self._tracked.pop(task_id, None)
            return resp.cancelled, tr.execution_id
        except RunnerUnavailableError as exc:
            logger.warning("runner_backend.cancel_failed", task_id=task_id, error=str(exc))
            return False, tr.execution_id
