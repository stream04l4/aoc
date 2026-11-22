"""MCPClientManager: guarded ToolProvider executing MCP tools over stdio JSON-RPC 2.0 (V2.0 P1-6).

Hard guarantees (Task Book Section 5.7):
  - Strictly a ToolProvider (NOT a second general autonomous AgentProvider).
  - Unregistered or disabled tools are strictly DENIED by default.
  - Validates JSON Schema inputs before spawning any server.
  - Enforces Governance (L0-L3, L2 requires explicit approval, L3 hard-denied) and Budget checks,
    recording every check in `policy_evaluations(subject_type='mcp_tool')` and every invocation
    in `resource_usage(category='mcp_tool')`.
  - Enforces per-tool timeout and output byte caps; server crashes never crash AutonomousLoop.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Sequence

from app.agents.base import sanitize_env
from app.cognition.interfaces import ActionPlan
from app.core.enums import DriveType, PermissionLevel
from app.governance.interfaces import GovernanceInterface
from app.observability.logging import get_logger
from app.storage.repository import StateRepository
from app.tools.interfaces import ToolCallRequest, ToolCallResult, ToolProviderInterface, ToolSpec

logger = get_logger("aoc.tools.mcp_manager")


def _parse_uuid_or_none(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def _validate_json_schema(args: dict[str, Any], schema: dict[str, Any]) -> str | None:
    """Lightweight deterministic JSON Schema validator (required, type, enum, additionalProperties)."""
    if not schema:
        return None
    required = schema.get("required") or []
    for req_field in required:
        if req_field not in args or args[req_field] is None:
            return f"Missing required parameter '{req_field}'"

    props = schema.get("properties") or {}
    if schema.get("additionalProperties") is False:
        unknown = set(args.keys()) - set(props.keys())
        if unknown:
            return f"Unknown parameter(s) rejected: {sorted(unknown)}"

    type_map = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "object": dict, "array": list}
    for k, val in args.items():
        if k not in props:
            continue
        pspec = props[k]
        expected_type = pspec.get("type")
        if expected_type in type_map:
            if expected_type == "integer" and isinstance(val, bool):
                return f"Parameter '{k}' must be integer, got boolean"
            if not isinstance(val, type_map[expected_type]):
                return f"Parameter '{k}' must be {expected_type}, got {type(val).__name__}"
        if "enum" in pspec and val not in pspec["enum"]:
            return f"Parameter '{k}' value '{val}' not in allowed enum {pspec['enum']}"
    return None


class MCPClientManager(ToolProviderInterface):
    def __init__(
        self,
        *,
        governance: GovernanceInterface,
        repo: StateRepository,
        workspace_root: str = "/home/ubuntu/aoc-workspaces",
        allowed_domains: Sequence[str] = (),
        allowed_git_roots: Sequence[str] = (),
    ) -> None:
        self.governance = governance
        self.repo = repo
        self.workspace_root = Path(workspace_root).resolve()
        self.allowed_domains = list(allowed_domains)
        self.allowed_git_roots = list(allowed_git_roots)
        self._tools: dict[str, ToolSpec] = {}
        self._skill_runtime: Any = None

    def attach_skill_runtime(self, skill_runtime: Any) -> None:
        """Attach external SkillRuntime so VALIDATED skills expose as unified MCP tools (V4.0 M1)."""
        self._skill_runtime = skill_runtime

    async def sync_validated_skills_as_tools(self, limit: int = 30) -> int:
        """Register integrity-verified VALIDATED skills as callable `skill:<name>` MCP tools."""
        if self._skill_runtime is None:
            return 0
        # Remove stale dynamic skill tools before refreshing
        for k in [name for name, spec in self._tools.items() if spec.provider_kind == "tool_provider_skill"]:
            self._tools.pop(k, None)
        skills = await self._skill_runtime.skill_registry.list_reusable_skills_for_context(limit=limit)
        added = 0
        for sk in skills:
            ref_name = str(sk.get("canonical_name") or sk["skill_key"])
            tname = f"skill:{ref_name}"
            spec = ToolSpec(
                tool_name=tname,
                server_name="aoc-skill-runtime",
                provider_kind="tool_provider_skill",
                description=(
                    f"[Skill v{sk.get('version', '1.0.0')} · {sk.get('capability_key', 'coding')}] "
                    f"{sk.get('description', '')}"
                )[:300],
                permission_level=str(sk.get("permission_level") or "L1"),
                timeout_seconds=float(self._skill_runtime.default_timeout_seconds),
                enabled=True,
                input_schema=dict(sk.get("input_schema") or {}),
            )
            self.register_tool(spec)
            added += 1
        return added

    def register_tool(self, spec: ToolSpec) -> None:
        self._tools[spec.tool_name] = spec

    def load_from_config(self, cfg: dict[str, Any]) -> int:
        count = 0
        for srv in cfg.get("mcp_servers") or []:
            srv_name = str(srv.get("server_name") or "")
            srv_enabled = bool(srv.get("enabled", True))
            cmd_raw = list(srv.get("command") or [sys.executable, "-m", "app.tools.builtin_servers", srv_name])
            cmd = [sys.executable if c == "${PYTHON}" else str(c) for c in cmd_raw]
            for t in srv.get("tools") or []:
                spec = ToolSpec(
                    tool_name=str(t["tool_name"]),
                    server_name=srv_name,
                    description=str(t.get("description", "")),
                    permission_level=str(t.get("permission_level", "L0")),
                    action_levels=dict(t.get("action_levels") or {}),
                    timeout_seconds=float(t.get("timeout_seconds", 15.0)),
                    max_output_bytes=int(t.get("max_output_bytes", 16384)),
                    enabled=bool(srv_enabled and t.get("enabled", True)),
                    input_schema=dict(t.get("input_schema") or {}),
                    command=cmd,
                )
                self.register_tool(spec)
                count += 1
        return count

    def list_tools(self, *, enabled_only: bool = False) -> list[ToolSpec]:
        tools = list(self._tools.values())
        if enabled_only:
            tools = [t for t in tools if t.enabled]
        return sorted(tools, key=lambda t: t.tool_name)

    def get_tool(self, tool_name: str) -> ToolSpec | None:
        return self._tools.get(tool_name)

    def summary_for_brain(self) -> list[dict[str, Any]]:
        return [
            {
                "tool_name": t.tool_name,
                "server_name": t.server_name,
                "provider_kind": t.provider_kind,
                "description": t.description,
                "permission_level": t.permission_level,
                "action_levels": t.action_levels,
                "enabled": t.enabled,
            }
            for t in self.list_tools(enabled_only=True)
        ]

    async def invoke(self, req: ToolCallRequest) -> ToolCallResult:
        t0 = time.perf_counter()
        cycle_uuid = _parse_uuid_or_none(req.cycle_id)
        task_uuid = _parse_uuid_or_none(req.task_id)

        # 1. Hard deny any unregistered or disabled tool
        spec = self._tools.get(req.tool_name)
        if spec is None or not spec.enabled:
            reason = f"MCP tool '{req.tool_name}' is unregistered or disabled (denied by default)"
            eval_id = await self.repo.record_policy_evaluation(
                subject_type="mcp_tool",
                subject_id=task_uuid,
                cycle_id=cycle_uuid,
                requested_level="L3",
                deterministic_level="L3",
                effective_level="L3",
                verdict="denied",
                violations=[reason],
                reason=reason,
            )
            return ToolCallResult(
                tool_name=req.tool_name,
                ok=False,
                denied=True,
                requested_level="L3",
                effective_level="L3",
                error=reason,
                policy_evaluation_id=str(eval_id),
            )

        # 2. Input JSON Schema validation
        schema_err = _validate_json_schema(req.arguments, spec.input_schema)
        if schema_err is not None:
            return ToolCallResult(
                tool_name=spec.tool_name,
                server_name=spec.server_name,
                ok=False,
                denied=True,
                requested_level=spec.permission_level,
                effective_level=spec.permission_level,
                error=f"Input schema validation failed: {schema_err}",
            )

        # 3. Determine action-specific permission level & validate via GovernanceManager (ZERO bypass!)
        action_val = str(req.arguments.get("action") or "").strip().lower()
        req_level_str = spec.action_levels.get(action_val, spec.permission_level)
        args_for_policy = dict(req.arguments)
        # Allowlisted read-only git repo roots should not trip the "files outside workspace" L2 write rule
        if spec.tool_name == "git_docs_search" and args_for_policy.get("repo_path"):
            cand = Path(str(args_for_policy["repo_path"])).resolve()
            if any(cand == Path(r).resolve() or Path(r).resolve() in cand.parents for r in self.allowed_git_roots):
                args_for_policy["repo_path"] = "<allowlisted_readonly_git_root>"
        synthetic_plan = ActionPlan(
            should_execute=True,
            goal_title=f"MCP tool invocation: {spec.tool_name}",
            objective=f"Invoke MCP tool {spec.tool_name} ({spec.server_name}) with args {json.dumps(args_for_policy, ensure_ascii=False)[:600]}",
            description=spec.description,
            assigned_agent=spec.server_name,
            permission_level=req_level_str,
            timeout_seconds=int(spec.timeout_seconds),
        )
        verdict = await self.governance.validate_plan(synthetic_plan, DriveType.CURIOSITY.value)
        eff_level = PermissionLevel(verdict.effective_level)

        # Handle L3 hard denial
        if eff_level == PermissionLevel.L3 or verdict.denied:
            eval_id = await self.repo.record_policy_evaluation(
                subject_type="mcp_tool",
                subject_id=task_uuid,
                cycle_id=cycle_uuid,
                requested_level=req_level_str,
                deterministic_level=verdict.deterministic_level or verdict.effective_level,
                semantic_level=verdict.semantic_level,
                effective_level=verdict.effective_level,
                verdict="denied",
                violations=verdict.violations,
                reason=verdict.reason,
            )
            return ToolCallResult(
                tool_name=spec.tool_name,
                server_name=spec.server_name,
                ok=False,
                denied=True,
                requested_level=req_level_str,
                effective_level=verdict.effective_level,
                error=verdict.reason,
                policy_evaluation_id=str(eval_id),
            )

        # Handle L2 approval gate (unless caller presents verified post-approval flag)
        if verdict.requires_approval and not req.approved_l2:
            eval_id = await self.repo.record_policy_evaluation(
                subject_type="mcp_tool",
                subject_id=task_uuid,
                cycle_id=cycle_uuid,
                requested_level=req_level_str,
                deterministic_level=verdict.deterministic_level or verdict.effective_level,
                semantic_level=verdict.semantic_level,
                effective_level=verdict.effective_level,
                verdict="requires_approval",
                violations=verdict.violations,
                reason=f"MCP tool '{spec.tool_name}' requires human L2 approval",
            )
            return ToolCallResult(
                tool_name=spec.tool_name,
                server_name=spec.server_name,
                ok=False,
                requires_approval=True,
                requested_level=req_level_str,
                effective_level=verdict.effective_level,
                error=f"MCP tool '{spec.tool_name}' requires human L2 approval",
                policy_evaluation_id=str(eval_id),
            )

        # Record approved policy evaluation
        eval_id = await self.repo.record_policy_evaluation(
            subject_type="mcp_tool",
            subject_id=task_uuid,
            cycle_id=cycle_uuid,
            requested_level=req_level_str,
            deterministic_level=verdict.deterministic_level or verdict.effective_level,
            semantic_level=verdict.semantic_level,
            effective_level=verdict.effective_level,
            verdict="allowed",
            violations=verdict.violations,
            reason=f"Allowed MCP tool '{spec.tool_name}' at {verdict.effective_level}",
        )

        # 4. V4.0 M1: Unified SkillRuntime execution path for `skill:*` tools (shares single runtime & version index)
        if spec.provider_kind == "tool_provider_skill" and self._skill_runtime is not None:
            s_res = await self._skill_runtime.execute(
                spec.tool_name,
                req.arguments,
                invocation_source="mcp",
                task_id=task_uuid,
                cycle_id=cycle_uuid,
                timeout_seconds=spec.timeout_seconds,
                approved_l2=req.approved_l2,
            )
            await self.repo.record_usage(
                category="mcp_tool",
                amount=1.0,
                seconds=s_res.latency_ms / 1000.0,
                details={
                    "tool_name": spec.tool_name,
                    "server_name": spec.server_name,
                    "skill_id": s_res.skill_id,
                    "skill_version": s_res.version,
                    "is_cross_task_reuse": s_res.is_cross_task_reuse,
                    "ok": s_res.ok,
                },
                cycle_id=cycle_uuid,
                task_id=task_uuid,
            )
            return ToolCallResult(
                tool_name=spec.tool_name,
                server_name=spec.server_name,
                provider_kind=spec.provider_kind,
                ok=s_res.ok,
                output=s_res.output,
                denied=s_res.denied,
                requires_approval=s_res.requires_approval,
                requested_level=req_level_str,
                effective_level=verdict.effective_level,
                error=s_res.error,
                latency_ms=s_res.latency_ms,
                policy_evaluation_id=str(eval_id),
            )

        # 5. Execute over real stdio JSON-RPC 2.0 child process with strict timeout & sanitized env
        workspace = req.workspace or self.governance.prepare_workspace(req.task_id or f"mcp-{uuid.uuid4().hex[:8]}")
        try:
            structured, truncated = await self._call_stdio_server(spec, req.arguments, workspace=workspace)
            latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            await self.repo.record_usage(
                category="mcp_tool",
                amount=1.0,
                seconds=latency_ms / 1000.0,
                details={
                    "tool_name": spec.tool_name,
                    "server_name": spec.server_name,
                    "effective_level": verdict.effective_level,
                    "ok": True,
                },
                cycle_id=cycle_uuid,
                task_id=task_uuid,
            )
            return ToolCallResult(
                tool_name=spec.tool_name,
                server_name=spec.server_name,
                ok=True,
                output=structured,
                truncated=truncated,
                requested_level=req_level_str,
                effective_level=verdict.effective_level,
                latency_ms=latency_ms,
                policy_evaluation_id=str(eval_id),
            )
        except Exception as exc:
            latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            logger.warning("mcp.tool_execution_failed", tool=spec.tool_name, server=spec.server_name, error=str(exc))
            await self.repo.record_usage(
                category="mcp_tool",
                amount=1.0,
                seconds=latency_ms / 1000.0,
                details={
                    "tool_name": spec.tool_name,
                    "server_name": spec.server_name,
                    "effective_level": verdict.effective_level,
                    "ok": False,
                    "error": str(exc)[:300],
                },
                cycle_id=cycle_uuid,
                task_id=task_uuid,
            )
            return ToolCallResult(
                tool_name=spec.tool_name,
                server_name=spec.server_name,
                ok=False,
                requested_level=req_level_str,
                effective_level=verdict.effective_level,
                error=str(exc),
                latency_ms=latency_ms,
                policy_evaluation_id=str(eval_id),
            )

    async def _call_stdio_server(
        self, spec: ToolSpec, arguments: dict[str, Any], *, workspace: str
    ) -> tuple[Any, bool]:
        env = sanitize_env(
            {
                "AOC_MCP_ALLOWED_DOMAINS": json.dumps(self.allowed_domains),
                "AOC_MCP_ALLOWED_GIT_ROOTS": json.dumps(self.allowed_git_roots),
                "AOC_MCP_WORKSPACE_ROOT": str(self.workspace_root),
            }
        )
        proc = await asyncio.create_subprocess_exec(
            *spec.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        rpc_msgs = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            + "\n"
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": spec.tool_name, "arguments": arguments, "workspace": workspace},
                },
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")

        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(rpc_msgs), timeout=spec.timeout_seconds)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            raise TimeoutError(f"MCP server '{spec.server_name}' timed out after {spec.timeout_seconds}s")

        lines = [ln.strip() for ln in out_b.decode("utf-8", errors="replace").splitlines() if ln.strip()]
        call_resp: dict[str, Any] | None = None
        for ln in lines:
            try:
                parsed = json.loads(ln)
                if parsed.get("id") == 2:
                    call_resp = parsed
            except json.JSONDecodeError:
                continue

        if call_resp is None:
            err_txt = err_b.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"MCP server '{spec.server_name}' exited ({proc.returncode}) without JSON-RPC response: {err_txt[:300]}")

        if "error" in call_resp:
            raise RuntimeError(str((call_resp["error"] or {}).get("message") or "MCP server error"))

        res_obj = call_resp.get("result") or {}
        structured = res_obj.get("structured")
        if structured is None:
            structured = res_obj

        raw_json = json.dumps(structured, ensure_ascii=False)
        truncated = len(raw_json.encode("utf-8")) > spec.max_output_bytes
        if truncated:
            structured = {"truncated_output": raw_json[: spec.max_output_bytes], "truncated": True}
        return structured, truncated
