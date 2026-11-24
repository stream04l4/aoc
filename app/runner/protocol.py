"""Protocol schemas shared between AOC Core and Host Agent Runner over UDS (V2.0 P1-3)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.perception.interfaces import utcnow


class ProcessIdentity(BaseModel):
    """Complete OS-level process identity resistant to PID reuse and host reboots."""

    execution_id: str
    idempotency_key: str
    pid: int
    process_group_id: int
    shim_pid: int | None = None
    host_boot_id: str
    process_start_ticks: int
    cgroup_path: str | None = None
    systemd_unit: str | None = None
    runner_id: str
    started_at: datetime = Field(default_factory=utcnow)


class LaunchExecutionRequest(BaseModel):
    execution_id: str
    task_id: str
    agent_id: str
    idempotency_key: str
    command: list[str]
    stdin_text: str = ""
    workspace: str
    sandbox_mode: str = "read-only"
    timeout_seconds: int = 300
    instance_id: str | None = None
    extra_env: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class LaunchExecutionResponse(BaseModel):
    execution_id: str
    idempotency_key: str
    status: str
    deduplicated: bool = False
    process_identity: ProcessIdentity | None = None
    runner_id: str
    isolation_mode: str = "fallback_pgroup"


class ExecutionRecordResponse(BaseModel):
    execution_id: str
    task_id: str
    agent_id: str
    idempotency_key: str
    runner_id: str
    status: str  # STARTING | RUNNING | SUCCEEDED | FAILED | TIMEOUT | CANCELLED
    isolation_mode: str = "fallback_pgroup"
    process_identity: ProcessIdentity | None = None
    exit_code: int | None = None
    started_at: datetime
    completed_at: datetime | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    workspace: str = ""
    alive: bool = False
    identity_check: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class CancelExecutionResponse(BaseModel):
    execution_id: str
    cancelled: bool
    status: str
    reason: str = ""


class RunnerHealthResponse(BaseModel):
    status: str = "ok"
    runner_id: str
    host_boot_id: str
    systemd_unit: str | None = None
    cgroup_delegated: bool = False
    cgroup_root: str | None = None
    socket_path: str
    state_dir: str
    active_executions: int = 0
    total_tracked_executions: int = 0
    timestamp: datetime = Field(default_factory=utcnow)
