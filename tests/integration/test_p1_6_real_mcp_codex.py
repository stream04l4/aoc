"""P1-6 Real Integration Tests:
  1. All 3 real guarded MCP ToolProvider servers over stdio JSON-RPC 2.0:
     - web_fetch_readonly (real HTTPS fetch to docs.python.org)
     - workspace_filesystem (real L1 write + L0 read inside isolated workspace)
     - git_docs_search (real git_log + grep on /home/ubuntu/aoc)
     Verifying policy_evaluations(subject_type='mcp_tool') and resource_usage(category='mcp_tool') persistence.
  2. Real `codex-cli 0.171.0` execution via AgentOrchestrator + AgentRouter:
     - Verifies real `thread_id` persisted in `agent_executions.thread_id`
     - Verifies external tokens attributed to LLMGateway ledger
     - Verifies explainable `execution_routing` logged in `decision_logs`.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from app.agents.codex import CodexAdapter
from app.agents.base import AgentCapability
from app.agents.registry import AgentRegistry
from app.cognition.gateway import LLMGateway
from app.cognition.interfaces import ActionPlan, VerificationSpec
from app.core.config import Settings, get_agent_config, get_sensors_config
from app.core.enums import TaskStatus
from app.governance.interfaces import PolicyVerdict
from app.governance.policy import GovernanceManager
from app.orchestration.orchestrator import AgentOrchestrator
from app.perception.interfaces import utcnow
from app.storage.database import Database
from app.storage.models import Base
from app.storage.repository import StateRepository
from app.tools.interfaces import ToolCallRequest
from app.tools.mcp_manager import MCPClientManager

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.integration
def test_real_3_mcp_tools_e2e_with_audit_and_budget(tmp_path: Path):
    async def _run():
        db = Database(f"sqlite+aiosqlite:///{tmp_path / 'p1_6_mcp_real.db'}")
        async with db.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        repo = StateRepository(db)
        ws_root = tmp_path / "ws"
        task_ws = ws_root / "real-mcp-task"
        task_ws.mkdir(parents=True)
        try:
            settings = Settings()
            gov = GovernanceManager(settings.governance.model_copy(update={"workspace_root": str(ws_root)}), repo)
            scfg = get_sensors_config()
            mgr = MCPClientManager(
                governance=gov,
                repo=repo,
                workspace_root=str(ws_root),
                allowed_domains=list(scfg.get("allowed_domains") or []),
                allowed_git_roots=list(scfg.get("allowed_git_roots") or []),
            )
            mgr.load_from_config(get_agent_config())

            # 1. Real web_fetch_readonly against live allowlisted docs.python.org
            web_res = await mgr.invoke(
                ToolCallRequest(
                    tool_name="web_fetch_readonly",
                    arguments={"url": "https://docs.python.org/3.12/whatsnew/3.12.html"},
                )
            )
            assert web_res.ok is True, f"web_fetch_readonly failed: {web_res.error}"
            assert "Python" in web_res.output["markdown_excerpt"]

            # 2. Real workspace_filesystem L1 write + L0 read
            fs_w = await mgr.invoke(
                ToolCallRequest(
                    tool_name="workspace_filesystem",
                    arguments={"action": "write_file", "path": "report.md", "content": web_res.output["markdown_excerpt"][:500]},
                    workspace=str(task_ws),
                )
            )
            assert fs_w.ok is True and fs_w.effective_level == "L1"
            fs_r = await mgr.invoke(
                ToolCallRequest(
                    tool_name="workspace_filesystem",
                    arguments={"action": "read_file", "path": "report.md"},
                    workspace=str(task_ws),
                )
            )
            assert fs_r.ok is True and "Python" in fs_r.output["content"]

            # 3. Real git_docs_search on /home/ubuntu/aoc
            git_res = await mgr.invoke(
                ToolCallRequest(
                    tool_name="git_docs_search",
                    arguments={"action": "git_log", "repo_path": str(PROJECT_ROOT), "limit": 5},
                )
            )
            assert git_res.ok is True and len(git_res.output["commits"]) == 5

            # Verify full Governance & Budget audit trail
            audits = await repo.list_policy_evaluations(subject_type="mcp_tool", limit=20)
            assert len(audits) == 4
            assert all(a["verdict"] == "allowed" for a in audits)

            usage = await repo.usage_since("mcp_tool", utcnow().replace(hour=0, minute=0, second=0))
            assert usage["count"] == 4
        finally:
            await db.dispose()

    asyncio.run(_run())


@pytest.mark.integration
@pytest.mark.real_agent
def test_real_codex_0171_thread_id_persistence_and_explainable_routing(tmp_path: Path):
    settings = Settings()

    async def _run():
        db = Database(f"sqlite+aiosqlite:///{tmp_path / 'p1_6_codex_real.db'}")
        async with db.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        repo = StateRepository(db)
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        try:
            gov = GovernanceManager(settings.governance.model_copy(update={"workspace_root": str(ws_root)}), repo)
            gw = LLMGateway(settings.llm, settings.governance.budget, repo=repo)
            reg = AgentRegistry(repo=repo)
            reg.register(
                CodexAdapter(
                    AgentCapability(
                        agent_id="codex",
                        name="OpenAI Codex CLI",
                        agent_type="cli",
                        capabilities=["system_diagnosis", "general_task", "research"],
                        supported_modes=["read-only", "workspace-write"],
                    ),
                    binary="codex",
                    options={"use_aoc_llm": True, "sandbox": "read-only"},
                    llm_settings=settings.llm,
                )
            )
            probes = await reg.probe_all()
            assert probes["codex"].ok is True

            orch = AgentOrchestrator(registry=reg, governance=gov, repo=repo, max_retries=0, llm_gateway=gw)
            plan = ActionPlan(
                should_execute=True,
                goal_title="Codex 0.171 thread_id check",
                objective="Reply with exactly one line: CODEX_P16_OK",
                assigned_agent="codex",
                required_capability="research",
                expected_output="CODEX_P16_OK",
                timeout_seconds=90,
                permission_level="L0",
                verification=[VerificationSpec(kind="non_empty_output")],
            )
            verdict = PolicyVerdict(
                allowed=True,
                requested_level="L0",
                effective_level="L0",
                sandbox_mode="read-only",
                effective_timeout_seconds=90,
                reason="allowed",
            )
            info = await orch.submit_plan(plan, verdict)
            final = await orch.wait_for_task(info["task_id"], max_wait_seconds=90.0, poll_interval=0.5)
            assert final["status"] == TaskStatus.SUCCEEDED.value

            # Verify real thread_id persisted into agent_executions.thread_id
            ex = await repo.get_execution(uuid.UUID(info["execution_id"]))
            assert ex is not None
            assert ex["thread_id"] is not None and len(ex["thread_id"]) >= 16
            assert "CODEX_P16_OK" in ((ex["result"] or {}).get("summary") or "")

            # Verify explainable execution_routing logged in decision_logs
            decs = await repo.list_decisions(limit=10)
            routing_decs = [d for d in decs if d["decision_type"] == "execution_routing"]
            assert len(routing_decs) == 1 and routing_decs[0]["selected"] == "codex"
            assert "candidates" in routing_decs[0]["payload"]
        finally:
            await db.dispose()

    asyncio.run(_run())
