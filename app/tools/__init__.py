"""Guarded MCP ToolProvider subsystem (V2.0 P1-6).

Architectural distinction (Task Book Section 5.7):
  - AgentProvider (app.agents): autonomous multi-step agents (e.g., Codex CLI).
  - ToolProvider (app.tools): atomic guarded tools exposed via MCPClientManager.
  Adding 3 MCP tools establishes a second controlled execution path (tool path),
  NOT a second autonomous general Agent and NOT multi-agent collaboration.
"""

from app.tools.interfaces import ToolCallRequest, ToolCallResult, ToolProviderInterface, ToolSpec
from app.tools.mcp_manager import MCPClientManager

__all__ = [
    "ToolSpec",
    "ToolCallRequest",
    "ToolCallResult",
    "ToolProviderInterface",
    "MCPClientManager",
]
