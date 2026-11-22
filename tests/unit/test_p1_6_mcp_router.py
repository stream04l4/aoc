"""P1-6 unit & fault-injection tests:
  - MCPClientManager: 3 guarded stdio JSON-RPC tools (web_fetch_readonly, workspace_filesystem, git_docs_search)
  - Schema validation, unregistered tool denial, path/symlink/.aoc_* escape blocks, SSRF blocks, L2 approval gate
  - MCP server crash & timeout fault isolation
  - CodexAdapter 0.171.0 JSONL thread_id / tool_calls parsing & guarded resume policy
  - AgentRouter explainable 5-factor scoring & decision_logs audit
  - V2 /v2/tools API & Hermes remains disabled
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.agents.base import AgentCapability, AgentExecution, AgentInterface, AgentProbeResult, AgentStatus, AgentTask
from app.agents.codex import CodexAdapter
from app.agents.registry import AgentRegistry
from app.agents.router import AgentRouter
from app.api.app import create_app
from app.core.config import ApiSettings, EmbeddingSettings, Settings, get_agent_config
from app.core.enums import ExecutionStatus
from app.core.runtime import OrganismRuntime
from app.governance.policy import GovernanceManager
from app.perception.interfaces import utcnow
from app.storage.database import Database
from app.storage.models import Base
from app.storage.repository import StateRepository
from app.tools.interfaces import ToolCallRequest, ToolSpec
from app.tools.mcp_manager import MCPClientManager

PROJECT_ROOT = Path(__file__).resolve().parents[2]


async def _init_db(db: Database) -> None:
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


class _DummyAgent(AgentInterface):
    def __init__(self, agent_id: str, caps: list[str], modes: list[str], experimental: bool = False) -> None:
        self._cap = AgentCapability(
            agent_id=agent_id,
            name=agent_id,
            agent_type="cli",
            capabilities=caps,
            supported_modes=modes,
            experimental=experimental,
        )

    @property
    def capability(self) -> AgentCapability:
        return self._cap

    async def probe(self) -> AgentProbeResult:
        return AgentProbeResult(ok=True, binary="/bin/true", version="1.0")

    async def execute(self, task: AgentTask) -> AgentExecution:
        return AgentExecution(
            execution_id=task.execution_id or str(uuid.uuid4()),
            task_id=task.task_id,
            agent_id=self._cap.agent_id,
            status=ExecutionStatus.SUCCEEDED.value,
            completed_at=utcnow(),
            result={"summary": "ok"},
        )

    async def get_status(self, execution_id: str) -> AgentStatus:
        return AgentStatus(execution_id=execution_id, agent_id=self._cap.agent_id, status="SUCCEEDED")

    async def cancel(self, execution_id: str) -> bool:
        return True


def _make_mcp_manager(repo: StateRepository, ws_root: Path, git_roots: list[str]) -> MCPClientManager:
    settings = Settings()
    gov = GovernanceManager(
        settings.governance.model_copy(update={"workspace_root": str(ws_root), "auto_approve_max_level": "L1"}),
        repo,
    )
    mgr = MCPClientManager(
        governance=gov,
        repo=repo,
        workspace_root=str(ws_root),
        allowed_domains=["docs.python.org", "www.postgresql.org"],
        allowed_git_roots=git_roots,
    )
    mgr.load_from_config(get_agent_config())
    return mgr


async def test_mcp_unregistered_and_schema_invalid_tools_rejected(tmp_path: Path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'mcp_schema.db'}")
    await _init_db(db)
    repo = StateRepository(db)
    try:
        mgr = _make_mcp_manager(repo, tmp_path / "ws", [str(PROJECT_ROOT)])

        # 1. Unregistered tool strictly denied by default + audited in policy_evaluations
        unreg = await mgr.invoke(ToolCallRequest(tool_name="unknown_shell_exec", arguments={"cmd": "whoami"}))
        assert unreg.ok is False and unreg.denied is True
        audits = await repo.list_policy_evaluations(subject_type="mcp_tool")
        assert len(audits) == 1 and audits[0]["verdict"] == "denied"

        # 2. Missing required parameter rejected
        missing = await mgr.invoke(ToolCallRequest(tool_name="web_fetch_readonly", arguments={}))
        assert missing.ok is False and missing.denied is True
        assert "Missing required parameter" in (missing.error or "")

        # 3. Unknown extra parameter & invalid enum rejected
        extra = await mgr.invoke(
            ToolCallRequest(tool_name="workspace_filesystem", arguments={"action": "list_dir", "evil": 1})
        )
        assert extra.ok is False and extra.denied is True
        bad_enum = await mgr.invoke(
            ToolCallRequest(tool_name="git_docs_search", arguments={"action": "git_push"})
        )
        assert bad_enum.ok is False and bad_enum.denied is True
    finally:
        await db.dispose()


async def test_mcp_workspace_filesystem_isolation_and_escape_blocks(tmp_path: Path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'mcp_fs.db'}")
    await _init_db(db)
    repo = StateRepository(db)
    ws_root = tmp_path / "ws"
    task_ws = ws_root / "task-fs-1"
    task_ws.mkdir(parents=True)
    try:
        mgr = _make_mcp_manager(repo, ws_root, [str(PROJECT_ROOT)])

        # 1. Legitimate L1 write + L0 read + L0 list_dir over real stdio JSON-RPC server
        w = await mgr.invoke(
            ToolCallRequest(
                tool_name="workspace_filesystem",
                arguments={"action": "write_file", "path": "notes/summary.md", "content": "# AOC MCP Test\nOK"},
                workspace=str(task_ws),
            )
        )
        assert w.ok is True and w.effective_level == "L1"
        assert w.output["bytes_written"] > 0

        r = await mgr.invoke(
            ToolCallRequest(
                tool_name="workspace_filesystem",
                arguments={"action": "read_file", "path": "notes/summary.md"},
                workspace=str(task_ws),
            )
        )
        assert r.ok is True and r.effective_level == "L0"
        assert "AOC MCP Test" in r.output["content"]

        ls = await mgr.invoke(
            ToolCallRequest(
                tool_name="workspace_filesystem",
                arguments={"action": "list_dir", "path": "notes"},
                workspace=str(task_ws),
            )
        )
        assert ls.ok is True and any(e["name"] == "summary.md" for e in ls.output["entries"])

        # 2. Path traversal `..` blocked
        esc = await mgr.invoke(
            ToolCallRequest(
                tool_name="workspace_filesystem",
                arguments={"action": "read_file", "path": "../../etc/passwd"},
                workspace=str(task_ws),
            )
        )
        assert esc.ok is False and "traversal" in (esc.error or "").lower()

        # 3. Symlink escape blocked
        outside = tmp_path / "secret.txt"
        outside.write_text("TOP_SECRET", encoding="utf-8")
        sym = task_ws / "escape_link.txt"
        sym.symlink_to(outside)
        sym_res = await mgr.invoke(
            ToolCallRequest(
                tool_name="workspace_filesystem",
                arguments={"action": "read_file", "path": "escape_link.txt"},
                workspace=str(task_ws),
            )
        )
        assert sym_res.ok is False and "symlink escape" in (sym_res.error or "").lower()

        # 4. Internal `.aoc_*` metadata access blocked
        meta_res = await mgr.invoke(
            ToolCallRequest(
                tool_name="workspace_filesystem",
                arguments={"action": "write_file", "path": ".aoc_last_message.txt", "content": "spoof"},
                workspace=str(task_ws),
            )
        )
        assert meta_res.ok is False and "metadata" in (meta_res.error or "").lower()

        # Verify budget & policy audit recorded
        usage = await repo.usage_since("mcp_tool", utcnow().replace(hour=0, minute=0, second=0))
        assert usage["count"] >= 6
        audits = await repo.list_policy_evaluations(subject_type="mcp_tool")
        assert len(audits) >= 6
    finally:
        await db.dispose()


async def test_mcp_git_docs_search_and_web_fetch_ssrf_blocks(tmp_path: Path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'mcp_git_web.db'}")
    await _init_db(db)
    repo = StateRepository(db)
    try:
        mgr = _make_mcp_manager(repo, tmp_path / "ws", [str(PROJECT_ROOT)])

        # 1. Real git_docs_search on allowlisted AOC repo
        g_log = await mgr.invoke(
            ToolCallRequest(
                tool_name="git_docs_search",
                arguments={"action": "git_log", "repo_path": str(PROJECT_ROOT), "limit": 3},
            )
        )
        assert g_log.ok is True and len(g_log.output["commits"]) >= 1

        g_grep = await mgr.invoke(
            ToolCallRequest(
                tool_name="git_docs_search",
                arguments={"action": "grep", "repo_path": str(PROJECT_ROOT), "query": "MCPClientManager"},
            )
        )
        assert g_grep.ok is True and len(g_grep.output["matches"]) >= 1

        # 2. Reading outside allowed_git_roots or sensitive .env blocked
        g_out = await mgr.invoke(
            ToolCallRequest(
                tool_name="git_docs_search",
                arguments={"action": "git_log", "repo_path": "/etc"},
            )
        )
        assert g_out.ok is False and "outside allowed_git_roots" in (g_out.error or "")

        g_env = await mgr.invoke(
            ToolCallRequest(
                tool_name="git_docs_search",
                arguments={"action": "read_doc", "repo_path": str(PROJECT_ROOT), "doc_path": ".env"},
            )
        )
        assert g_env.ok is False

        # 3. web_fetch_readonly blocks SSRF loopback, metadata, and disallowed domains
        for bad_url in (
            "https://127.0.0.1/admin",
            "https://169.254.169.254/latest/meta-data/",
            "https://evil.unlisted.example/data",
        ):
            res = await mgr.invoke(
                ToolCallRequest(tool_name="web_fetch_readonly", arguments={"url": bad_url})
            )
            assert res.ok is False
    finally:
        await db.dispose()


async def test_mcp_l2_tool_requires_approval_and_server_crash_fault_isolation(tmp_path: Path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'mcp_fault.db'}")
    await _init_db(db)
    repo = StateRepository(db)
    try:
        mgr = _make_mcp_manager(repo, tmp_path / "ws", [str(PROJECT_ROOT)])

        # 1. Explicit L2 tool requires approval (blocked unless approved_l2=True)
        mgr.register_tool(
            ToolSpec(
                tool_name="guarded_l2_op",
                server_name="aoc-mcp-l2",
                permission_level="L2",
                command=[sys.executable, "-c", "pass"],
            )
        )
        l2_res = await mgr.invoke(ToolCallRequest(tool_name="guarded_l2_op", arguments={}))
        assert l2_res.ok is False and l2_res.requires_approval is True

        # 2. Crashing MCP server process is cleanly isolated (never raises into AutonomousLoop)
        mgr.register_tool(
            ToolSpec(
                tool_name="crashing_tool",
                server_name="aoc-mcp-crash",
                permission_level="L0",
                timeout_seconds=5.0,
                command=[sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(2)"],
            )
        )
        crash_res = await mgr.invoke(ToolCallRequest(tool_name="crashing_tool", arguments={}))
        assert crash_res.ok is False and "exited (2)" in (crash_res.error or "")

        # 3. Hanging MCP server is cleanly killed on timeout
        mgr.register_tool(
            ToolSpec(
                tool_name="hanging_tool",
                server_name="aoc-mcp-hang",
                permission_level="L0",
                timeout_seconds=0.3,
                command=[sys.executable, "-c", "import time; time.sleep(30)"],
            )
        )
        hang_res = await mgr.invoke(ToolCallRequest(tool_name="hanging_tool", arguments={}))
        assert hang_res.ok is False and "timed out" in (hang_res.error or "").lower()
    finally:
        await db.dispose()


def test_codex_0171_jsonl_thread_id_parsing_resume_guard_and_hermes_disabled(tmp_path: Path):
    # Verify Hermes remains DISABLED in config/agents.yaml (Decision 4)
    cfg = get_agent_config()
    agents_by_id = {a["agent_id"]: a for a in cfg["agents"]}
    assert agents_by_id["hermes"]["enabled"] is False
    assert agents_by_id["codex"]["enabled"] is True

    # Verify Codex 0.171.0 JSONL thread_id & tool_calls extraction
    sample_jsonl = "\n".join(
        [
            '{"type":"thread.started","thread_id":"01a1fb89-92a4-7d11-9e23-3dc642435e5a"}',
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"id":"item_1","type":"command_execution","command":"ls -la","status":"completed"}}',
            '{"type":"item.completed","item":{"id":"item_2","type":"agent_message","text":"ALL_GOOD"}}',
            '{"type":"turn.completed","usage":{"input_tokens":1200,"output_tokens":45}}',
        ]
    )
    cap = AgentCapability(
        agent_id="codex", name="Codex", agent_type="cli", capabilities=["coding"], supported_modes=["read-only", "workspace-write"]
    )
    adapter = CodexAdapter(cap, binary="codex", options={"use_aoc_llm": False})
    parsed, usage, thread_id = adapter._parse_output(sample_jsonl, str(tmp_path / "missing.txt"), str(tmp_path))
    assert thread_id == "01a1fb89-92a4-7d11-9e23-3dc642435e5a"
    assert parsed["thread_id"] == thread_id
    assert parsed["summary"] == "ALL_GOOD"
    assert len(parsed["tool_calls"]) == 1 and parsed["tool_calls"][0]["command"] == "ls -la"
    assert usage["input_tokens"] == 1200

    # Controlled resume guard: refuses unless idempotent_or_confirmed=True
    task = AgentTask(
        task_id="t1", objective="continue check", assigned_agent="codex", workspace=str(tmp_path)
    )
    with pytest.raises(PermissionError, match="Refused codex exec resume"):
        adapter.build_resume_spec(task, thread_id, idempotent_or_confirmed=False)

    spec = adapter.build_resume_spec(task, thread_id, idempotent_or_confirmed=True)
    assert "resume" in spec["command"] and thread_id in spec["command"]


async def test_explainable_agent_router_scoring_and_decision_logs(tmp_path: Path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'router.db'}")
    await _init_db(db)
    repo = StateRepository(db)
    try:
        reg = AgentRegistry(repo=repo)
        reg.register(_DummyAgent("agent-reliable", ["coding", "research"], ["read-only", "workspace-write"]))
        reg.register(_DummyAgent("agent-flaky", ["coding", "research"], ["read-only", "workspace-write"]))
        await reg.probe_all()

        # Seed empirical history: agent-reliable succeeded 5x, agent-flaky failed 4x
        tid = await repo.create_task(
            objective="hist", assigned_agent="agent-reliable", workspace=str(tmp_path), timeout_seconds=30, permission_level="L0", status="SUCCEEDED"
        )
        for _ in range(5):
            await repo.create_execution(tid, "agent-reliable", ExecutionStatus.SUCCEEDED.value)
        for _ in range(4):
            await repo.create_execution(tid, "agent-flaky", ExecutionStatus.FAILED.value)

        router = AgentRouter(reg, repo)
        winner, dec = await router.select(required_capability="coding", sandbox_mode="read-only")
        assert winner is not None and winner.capability.agent_id == "agent-reliable"
        assert dec.selected_agent_id == "agent-reliable"
        assert len(dec.candidates) == 2
        assert dec.candidates[0].empirical_reliability > dec.candidates[1].empirical_reliability

        # Verify explainable routing decision logged in decision_logs
        logs = await repo.list_decisions(limit=10)
        assert any(d["decision_type"] == "execution_routing" and d["selected"] == "agent-reliable" for d in logs)

        # Excluding winner cleanly switches to backup
        backup, dec2 = await router.select(required_capability="coding", exclude={"agent-reliable"})
        assert backup is not None and backup.capability.agent_id == "agent-flaky"
        assert dec2.selected_agent_id == "agent-flaky"
    finally:
        await db.dispose()


async def test_v2_tools_api_endpoints(tmp_path: Path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'tools_api.db'}")
    await _init_db(db)
    try:
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        settings = Settings(
            api=ApiSettings(host="127.0.0.1", auth_enabled=True, admin_key="admin-tools-key"),
            embedding=EmbeddingSettings(provider="hash_fallback", dimensions=64),
            governance=Settings().governance.model_copy(update={"workspace_root": str(ws_root)}),
        )
        rt = OrganismRuntime(settings=settings, db=db, auto_load_agents=False, llm_probe_fn=lambda: None)
        app = create_app(settings=settings, db=db, runtime=rt, start_scheduler=False)
        headers = {"Authorization": "Bearer admin-tools-key"}

        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                assert (await client.get("/v2/tools")).status_code == 401

                tr = await client.get("/v2/tools", headers=headers)
                assert tr.status_code == 200
                body = tr.json()
                assert body["provider_kind"] == "tool_provider_mcp"
                names = {t["tool_name"] for t in body["tools"]}
                assert names == {"web_fetch_readonly", "workspace_filesystem", "git_docs_search", "arxiv_search"}

                inv = await client.post(
                    "/v2/tools/workspace_filesystem/invoke",
                    json={"arguments": {"action": "write_file", "path": "hello.txt", "content": "mcp api ok"}, "task_id": "t-api"},
                    headers=headers,
                )
                assert inv.status_code == 200 and inv.json()["ok"] is True

                # Unregistered tool -> 403
                bad = await client.post("/v2/tools/no_such_tool/invoke", json={"arguments": {}}, headers=headers)
                assert bad.status_code == 403
    finally:
        await db.dispose()
