"""ToolProvider interfaces strictly separated from AgentProvider (V2.0 P1-6)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field


class ToolSpec(BaseModel):
    tool_name: str
    server_name: str
    provider_kind: str = "tool_provider_mcp"  # Explicitly NOT an AgentProvider
    description: str = ""
    permission_level: str = "L0"  # Default baseline level (may escalate per-action, e.g. write -> L1)
    action_levels: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = 15.0
    max_output_bytes: int = 16384
    enabled: bool = True
    input_schema: dict[str, Any] = Field(default_factory=dict)
    command: list[str] = Field(default_factory=list)


class ToolCallRequest(BaseModel):
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    workspace: str | None = None
    task_id: str | None = None
    cycle_id: str | None = None
    requested_by: str = "orchestrator"
    approved_l2: bool = False


class ToolCallResult(BaseModel):
    tool_name: str
    server_name: str = ""
    provider_kind: str = "tool_provider_mcp"
    ok: bool = False
    output: Any = None
    truncated: bool = False
    requested_level: str = "L0"
    effective_level: str = "L0"
    requires_approval: bool = False
    denied: bool = False
    error: str | None = None
    latency_ms: float = 0.0
    policy_evaluation_id: str | None = None


class ToolProviderInterface(ABC):
    @abstractmethod
    def list_tools(self, *, enabled_only: bool = False) -> list[ToolSpec]: ...

    @abstractmethod
    def get_tool(self, tool_name: str) -> ToolSpec | None: ...

    @abstractmethod
    async def invoke(self, req: ToolCallRequest) -> ToolCallResult: ...
