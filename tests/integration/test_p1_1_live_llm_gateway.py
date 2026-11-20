"""P1-1 live integration test: verifies real Qwen/vLLM calls pass through LLMGateway
with accurate PostgreSQL/SQLite token accounting and per-cycle limits enforced.
"""

from __future__ import annotations

import uuid

import pytest

from app.cognition.brain import CognitionBrain
from app.cognition.gateway import LLMBudgetExceededError, LLMGateway
from app.cognition.interfaces import AssessmentResult
from app.core.config import BudgetSettings, Settings
from app.drives.interfaces import DriveState
from app.perception.interfaces import Observation
from app.storage.repository import StateRepository, day_start


@pytest.mark.integration
@pytest.mark.asyncio
async def test_p1_1_live_vllm_through_llm_gateway_with_accounting(repo: StateRepository):
    settings = Settings()
    if not settings.llm.api_key or not settings.llm.api_key.get_secret_value():
        pytest.skip("AOC_LLM__API_KEY not configured")

    gw = LLMGateway(
        llm_settings=settings.llm,
        budget_settings=BudgetSettings(max_llm_calls_per_cycle=2, max_tokens_per_cycle=50_000, max_concurrent_llm_calls=1),
        repo=repo,
    )
    probe = await gw.health_check()
    if not probe.get("available"):
        pytest.skip(f"Live vLLM endpoint unavailable: {probe}")

    brain = CognitionBrain(gw)
    cid = uuid.uuid4()
    obs = Observation(
        system_state={"disk": {"free_ratio": 0.6}, "memory": {"available_ratio": 0.7}},
        environment_state={"unknown_topics": [{"topic": "pgvector-hnsw", "importance": 0.8}]},
        confidence=1.0,
    )
    drives = [DriveState(drive_type="curiosity", satisfaction=0.4, urgency=0.6, confidence=0.9)]

    with gw.scope(cycle_id=cid):
        res = await brain.assess(obs, [], drives)

    assert isinstance(res, AssessmentResult)
    assert res.summary
    assert res.usage.total_tokens > 0

    cycle_usage = await repo.llm_usage_for_cycle(cid)
    assert cycle_usage["calls"] >= 1
    assert cycle_usage["tokens"] == res.usage.total_tokens

    daily = await repo.usage_since("llm", day_start())
    assert daily["count"] >= 1
    assert daily["amount"] >= res.usage.total_tokens

    # Verify per-cycle hard cap cannot be bypassed even against live endpoint
    gw_tight = LLMGateway(
        llm_settings=settings.llm,
        budget_settings=BudgetSettings(max_llm_calls_per_cycle=1),
        repo=repo,
    )
    cid2 = uuid.uuid4()
    await gw_tight.complete_structured("assess", "Return concise JSON assessment", {"status": "ok"}, AssessmentResult, cycle_id=cid2)
    with pytest.raises(LLMBudgetExceededError):
        await gw_tight.complete_structured("assess", "Return concise JSON assessment", {"status": "ok"}, AssessmentResult, cycle_id=cid2)
