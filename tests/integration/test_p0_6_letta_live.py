"""P0-6 live Letta integration test (runs when aoc-letta is reachable at 127.0.0.1:8283)."""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.core.enums import MemoryType
from app.memory.experience_manager import ExperienceManager
from app.memory.interfaces import MemoryItem
from app.memory.letta_adapter import LettaAdapter
from app.storage.repository import StateRepository


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_letta_store_and_restart_recall(repo: StateRepository):
    settings = Settings()
    adapter = LettaAdapter(
        settings.memory,
        llm_model=settings.llm.model,
        llm_endpoint=settings.llm.base_url,
    )
    if not await adapter.health():
        pytest.skip(f"Letta not reachable at {settings.memory.letta_base_url}")

    ident = await repo.get_or_create_identity("AOC-LETTA-LIVE", "1.0.0")
    mgr = ExperienceManager(repo, backend=adapter)
    info = await mgr.initialize(ident["id"], "AOC-LETTA-LIVE", "Live Letta integration organism")
    assert info["letta_available"] is True

    res = await mgr.remember(
        MemoryItem(
            memory_type=MemoryType.SEMANTIC.value,
            title="Live Letta Verification",
            content="AOC verified persistent semantic recall across restart.",
            source="integration_test",
            importance=0.9,
            tags=["verification"],
        )
    )
    assert res.stored_index_id is not None

    # Re-instantiate manager (simulating restart)
    mgr2 = ExperienceManager(
        repo,
        backend=LettaAdapter(settings.memory, agent_id=info.get("agent_id")),
    )
    await mgr2.initialize(ident["id"], "AOC-LETTA-LIVE", "Live Letta integration organism")
    items = await mgr2.recall(query="persistent semantic recall", limit=5)
    assert any("persistent semantic recall" in it.content for it in items)
