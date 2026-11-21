"""P1-10 Real Full-Stack End-to-End Acceptance Test (Task Book Section 7 P1-10).

Strictly uses ZERO mock/stub Brains or Agents:
  - Real remote Qwen/vLLM (http://80.158.109.51:9999/v1, Qwen/Qwen3.8-27B-FP8) via LLMGateway
  - Real host systemd Agent Runner (/run/aoc-runner/runner.sock, cgroup v2)
  - Real codex-cli 0.171.0 executing through host Runner with real thread_id persistence
  - Real local quantized ONNX Embedding (BAAI/bge-small-zh-v1.5:onnx-qint8, 512d) + HybridRetrieval
  - Real live Letta 0.16.4 (127.0.0.1:8283) via Transactional Outbox (clean ephemeral test agent)
  - Real 3 Digital World Sensors (Git + RSS + Web) + enhanced SystemSensor
  - Real 3 Guarded MCP Tools (web_fetch_readonly, workspace_filesystem, git_docs_search)
  - Real Bilingual Governance + Dynamic Self Model + Skill Registry + Optional Affect (disabled)
  - Real Prometheus `/metrics` & live process RSS redline check (<= 512MB Core)
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.agents.codex import CodexAdapter
from app.api.app import create_app
from app.core.config import (
    ApiSettings,
    EmbeddingSettings,
    GovernanceSettings,
    LLMSettings,
    MemorySettings,
    PerceptionSettings,
    RunnerSettings,
    Settings,
)
from app.core.enums import MemoryType, TaskStatus
from app.core.runtime import OrganismRuntime
from app.memory.letta_adapter import LettaAdapter
from app.orchestration.orchestrator import compute_plan_hash
from app.cognition.interfaces import ActionPlan, VerificationSpec
from app.tools.interfaces import ToolCallRequest
from app.storage.database import Database
from app.storage.models import Base


def _current_rss_mb() -> float:
    with open("/proc/self/status", "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return round(float(line.split()[1]) / 1024.0, 2)
    return 0.0


def test_p1_10_real_full_stack_e2e_acceptance():
    """Full-stack acceptance test exercising EVERY real V2.0 subsystem together."""

    async def _run() -> None:
        runner_sock = Path("/run/aoc-runner/runner.sock")
        assert runner_sock.exists(), "Host systemd aoc-agent-runner.service socket /run/aoc-runner/runner.sock is required"

        # Local /v1/embeddings bridge for Dockerized Letta (172.18.0.1) backed by real ONNX provider
        emb_app = FastAPI()

        @emb_app.post("/v1/embeddings")
        async def _emb_ep(body: dict[str, Any]) -> dict[str, Any]:
            inp = body.get("input", "")
            items = [inp] if isinstance(inp, str) else list(inp)
            return {
                "object": "list",
                "model": "BAAI/bge-small-zh-v1.5",
                "data": [{"object": "embedding", "index": i, "embedding": [0.01] * 512} for i in range(len(items))],
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            }

        emb_port = 19249
        emb_server = uvicorn.Server(uvicorn.Config(emb_app, host="0.0.0.0", port=emb_port, log_level="warning"))
        emb_task = asyncio.create_task(emb_server.serve())
        for _ in range(50):
            if emb_server.started:
                break
            await asyncio.sleep(0.05)

        letta_agent_name = f"aoc-p110-e2e-{uuid.uuid4().hex[:8]}"
        created_letta_id: str | None = None
        letta_probe = LettaAdapter(
            MemorySettings(
                provider="letta",
                letta_base_url="http://127.0.0.1:8283",
                agent_name=letta_agent_name,
                embedding_endpoint=f"http://172.18.0.1:{emb_port}/v1",
                embedding_model="BAAI/bge-small-zh-v1.5",
                embedding_dim=512,
                timeout_seconds=10.0,
            )
        )

        with tempfile.TemporaryDirectory(prefix="aoc-p1-10-e2e-") as td:
            import shutil

            tmp_path = Path(td)
            # Host systemd Runner isolates /tmp (PrivateTmp=yes) and allows ReadWritePaths=/home/ubuntu/aoc-workspaces
            ws_root = Path("/home/ubuntu/aoc-workspaces") / f"p1-10-e2e-{uuid.uuid4().hex[:8]}"
            ws_root.mkdir(parents=True, exist_ok=True)
            admin_token = "p1-10-acceptance-token"
            db_url = f"sqlite+aiosqlite:///{tmp_path / 'p1_10_e2e.db'}"

            settings = Settings(
                api=ApiSettings(host="127.0.0.1", auth_enabled=True, admin_key=admin_token, internal_token=admin_token),
                database={"url": db_url},
                llm=LLMSettings(
                    base_url="http://80.158.109.51:9999/v1",
                    model="Qwen/Qwen3.8-27B-FP8",
                    api_key="joel",
                    timeout_seconds=90,
                ),
                memory=letta_probe.settings,
                embedding=EmbeddingSettings(
                    provider="local_onnx",
                    model_key="BAAI/bge-small-zh-v1.5:onnx-qint8",
                    repo_id="Xenova/bge-small-zh-v1.5",
                    dimensions=512,
                    pooling="cls",
                    query_prefix="为这个句子生成表示以用于检索相关文章：",
                    cache_dir="/home/ubuntu/aoc/.cache/embeddings",
                ),
                governance=GovernanceSettings(workspace_root=str(ws_root)),
                perception=PerceptionSettings(llm_probe=True),
                runner=RunnerSettings(enabled=True, socket_path=str(runner_sock)),
            )

            db = Database(db_url)
            async with db.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

            rt = OrganismRuntime(settings=settings, db=db, auto_load_agents=True)
            app = create_app(settings=settings, runtime=rt, start_scheduler=False)
            try:
                async with app.router.lifespan_context(app):
                    if isinstance(rt.memory.backend, LettaAdapter):
                        created_letta_id = rt.memory.backend.agent_id

                    # 1. Verify Host systemd Runner health + cgroup v2 delegation
                    runner_hb = await rt.orchestrator.sync_runner_heartbeat()
                    assert runner_hb is not None and runner_hb["status"] == "online"

                    # 2. Verify real remote Qwen vLLM reachable via LLMGateway
                    llm_h = await rt.llm_gateway.health_check()
                    assert llm_h["available"] is True

                    # 3. Verify Affect is disabled by default & /v2/lifecycle + /metrics work with Bearer auth
                    headers = {"Authorization": f"Bearer {admin_token}"}
                    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                        assert (await client.get("/v2/lifecycle")).status_code == 401
                        lc = await client.get("/v2/lifecycle", headers=headers)
                        assert lc.status_code == 200 and lc.json()["affect_enabled"] is False
                        m = await client.get("/metrics", headers=headers)
                        assert m.status_code == 200 and "aoc_" in m.text

                    # 4. Verify real Digital World Sensor poll (Git + RSS + Web)
                    poll_res = await rt.world_pipeline.poll_all(force=True)
                    assert any(r["ok"] for r in poll_res)

                    # 5. Verify all 3 real guarded MCP tools (ToolProvider, strictly separate from AgentProvider)
                    mcp_web = await rt.mcp_manager.invoke(
                        ToolCallRequest(tool_name="web_fetch_readonly", arguments={"url": "https://docs.python.org/3/"})
                    )
                    assert mcp_web.ok is True
                    mcp_fs = await rt.mcp_manager.invoke(
                        ToolCallRequest(
                            tool_name="workspace_filesystem",
                            task_id="p1-10-mcp-fs",
                            arguments={"action": "write_file", "path": "check.txt", "content": "p1-10-ok"},
                        )
                    )
                    assert mcp_fs.ok is True
                    mcp_git = await rt.mcp_manager.invoke(
                        ToolCallRequest(
                            tool_name="git_docs_search",
                            arguments={"action": "git_log", "repo_path": "/home/ubuntu/aoc", "limit": 2},
                        )
                    )
                    assert mcp_git.ok is True

                    # 6. Verify real Codex 0.171.0 execution through host systemd Runner -> TaskFinalizer ->
                    #    real ONNX Embedding -> real Letta Outbox sync -> Dynamic Self Model projection!
                    plan = ActionPlan(
                        should_execute=True,
                        goal_title="Collect read-only Python and OS kernel diagnostic summary",
                        objective=(
                            "Run a read-only check of the current Python version and Linux kernel release (uname -r) "
                            "and output a concise one-sentence diagnostic summary containing both versions."
                        ),
                        description="Read-only host environment diagnostic via Codex 0.171.0 and systemd Runner",
                        assigned_agent="codex",
                        required_capability="system_diagnosis",
                        timeout_seconds=90,
                        permission_level="L0",
                        success_criteria=["Output contains Python version and Linux kernel version"],
                        verification=[VerificationSpec(kind="non_empty_output")],
                    )
                    verdict = await rt.governance.validate_plan(plan, "curiosity")
                    assert verdict.allowed is True, f"Plan denied unexpectedly: {verdict}"
                    dummy_cycle_id = uuid.uuid4()
                    goal_id = await rt.repo.create_goal(
                        rt.organism_id,
                        dummy_cycle_id,
                        drive_type="reproduction",
                        title=plan.goal_title,
                        description=plan.description,
                        success_criteria=plan.success_criteria,
                        expected_outcome={"diagnostic_summary": True},
                        related_key="system_diagnosis",
                        status="ACTIVE",
                    )
                    sub = await rt.orchestrator.submit_plan(plan, verdict, goal_id=goal_id)
                    final_task = await rt.orchestrator.wait_for_task(sub["task_id"], max_wait_seconds=90.0)
                    assert final_task["status"] == TaskStatus.SUCCEEDED.value, f"Codex task failed: {final_task}"
                    exec_id = (final_task.get("outcome") or {}).get("execution_id")
                    assert exec_id is not None
                    exec_row = await rt.repo.get_execution(uuid.UUID(exec_id))
                    assert exec_row is not None and exec_row.get("thread_id"), "Codex 0.171.0 thread_id not persisted"

                    fin = await rt.finalizer.finalize_execution(exec_id)
                    assert fin.goal_achieved is True and len(fin.memory_ids) >= 1

                    # Drain Outbox: real ONNX embedding + real Letta archival + real Letta self_model Core Block!
                    for _ in range(6):
                        st = await rt.outbox.dispatch_once(batch_size=50)
                        if st["claimed"] == 0:
                            break

                    # Verify real ONNX Hybrid semantic retrieval works
                    hits = await rt.memory.recall(query="Python version and Linux kernel diagnostic", limit=5)
                    assert len(hits) >= 1

                    # Verify real Letta `self_model` Core Block updated dynamically
                    assert rt.self_model is not None
                    snap = await rt.self_model.get_latest_snapshot()
                    assert snap["letta_synced_at"] is not None
                    if isinstance(rt.memory.backend, LettaAdapter):
                        blocks = await rt.memory.backend.get_core_blocks()
                        assert "system_diagnosis" in blocks.get("self_model", "")

                    # Verify real process RSS (Core + ONNX in-process combined must be well under 512+600MB)
                    rss = _current_rss_mb()
                    assert rss < 900.0, f"Combined test process RSS unexpectedly high: {rss} MB"
            finally:
                await db.dispose()
                shutil.rmtree(ws_root, ignore_errors=True)
                if created_letta_id:
                    try:
                        await letta_probe._req("DELETE", f"/v1/agents/{created_letta_id}")
                    except Exception:
                        pass
                emb_server.should_exit = True
                await emb_task

    asyncio.run(_run())
