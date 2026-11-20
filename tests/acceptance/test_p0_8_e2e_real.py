"""P0-8 End-to-End Real Acceptance Test:
Real PostgreSQL + Real Sensors + Real Brain LLM (Qwen) + Real Codex CLI + Real Letta Memory!
"""

from __future__ import annotations

import shutil
import uuid

import pytest

from app.core.config import (
    BudgetSettings,
    GovernanceSettings,
    OrganismSettings,
    RuntimeSettings,
    Settings,
)
from app.core.enums import MemoryType
from app.core.runtime import OrganismRuntime


@pytest.mark.acceptance
@pytest.mark.real_agent
@pytest.mark.asyncio
async def test_e2e_real_curiosity_closed_loop_with_postgres_llm_codex_and_letta(tmp_path):
    if not shutil.which("codex"):
        pytest.skip("codex CLI not installed")

    base = Settings()
    if not base.llm.api_key or not base.llm.api_key.get_secret_value():
        pytest.skip("AOC_LLM__API_KEY not configured")

    org_name = f"AOC-E2E-{uuid.uuid4().hex[:6]}"
    settings = Settings(
        organism=OrganismSettings(name=org_name, version="1.0.0"),
        runtime=RuntimeSettings(
            cycle_interval_seconds=1,
            idle_sleep_seconds=5,
            monitor_poll_seconds=1.0,
            max_wait_per_cycle_seconds=150.0,
        ),
        database=base.database,
        llm=base.llm,
        memory=base.memory,
        perception=base.perception,
        governance=GovernanceSettings(
            workspace_root=str(tmp_path),
            budget=BudgetSettings(max_agent_tasks_per_day=500, max_task_timeout_seconds=120, max_task_retries=1),
        ),
    )

    rt = OrganismRuntime(settings=settings)
    if not await rt.db.ping():
        pytest.skip("PostgreSQL not reachable")

    await rt.start(start_scheduler=False)
    try:
        # Ensure reproduction capabilities are satisfied so curiosity cleanly wins Scenario B
        for cap in ("coding", "research", "repository_analysis", "system_diagnosis", "knowledge_summarization"):
            await rt.repo.upsert_world_item("capability", cap, {"verified": True}, status="acquired")

        topic_key = f"postgres-mvcc-{uuid.uuid4().hex[:5]}"
        await rt.repo.upsert_world_item(
            kind="knowledge_topic",
            key=topic_key,
            value={
                "description": "Summarize in 3 concise bullet points how PostgreSQL MVCC (xmin/xmax) enables non-blocking reads.",
                "importance": 0.98,
            },
            status="open",
            source="acceptance_test_B",
        )

        # Run ONE real end-to-end autonomous cycle
        rec = await rt.loop.run_cycle()

        assert rec["outcome"] in {"SUCCEEDED", "ACTION_FAILED", "REQUIRES_APPROVAL"}
        assert rec["decision"]["selected_drive"] in {"curiosity", "reproduction", "survival"}
        assert rec["drives_pre"]
        assert rec["drives_post"]

        # If curiosity ran and succeeded via real Codex, verify semantic memory + measured change!
        if rec["outcome"] == "SUCCEEDED":
            ev = rec["evaluation"]
            assert ev["actual_outcome"]["goal_achieved"] is True
            assert "measured_satisfaction_change" in ev
            mems = await rt.memory.recall(query="MVCC", memory_types=[MemoryType.SEMANTIC.value, MemoryType.EPISODIC.value], limit=5)
            assert len(mems) >= 1
    finally:
        await rt.repo.delete_world_item("knowledge_topic", topic_key)
        await rt.stop()
