"""P0-4 live LLM integration test using the configured OpenAI-compatible endpoint."""

from __future__ import annotations

import pytest

from app.cognition.brain import CognitionBrain
from app.cognition.interfaces import CognitiveContext, EvaluationContext
from app.cognition.llm_adapter import OpenAICompatibleLLMAdapter
from app.core.config import Settings
from app.drives.interfaces import DriveDecision, DriveState
from app.perception.interfaces import Observation


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_llm_assess_goal_plan_evaluate():
    settings = Settings()
    if not settings.llm.api_key or not settings.llm.api_key.get_secret_value():
        pytest.skip("AOC_LLM__API_KEY not configured")

    adapter = OpenAICompatibleLLMAdapter(settings.llm)
    brain = CognitionBrain(adapter)

    obs = Observation(
        system_state={"disk": {"free_ratio": 0.52}, "memory": {"available_ratio": 0.80}},
        environment_state={
            "unknown_topics": [
                {"topic": "sqlite-wal-mode", "description": "How SQLite WAL journaling works", "importance": 0.8}
            ]
        },
        confidence=1.0,
    )
    drives = [
        DriveState(drive_type="survival", satisfaction=0.95, urgency=0.1, confidence=1.0),
        DriveState(drive_type="reproduction", satisfaction=0.80, urgency=0.2, confidence=0.9),
        DriveState(drive_type="curiosity", satisfaction=0.35, urgency=0.65, confidence=0.95),
    ]

    assess = await brain.assess(obs, [], drives)
    assert assess.summary
    assert assess.usage.total_tokens > 0

    ctx = CognitiveContext(
        observation=obs,
        drives=drives,
        decision=DriveDecision(selected_drive="curiosity", priority_score=0.45, reason="open topic sqlite-wal-mode"),
        assessment=assess,
        available_agents=[
            {"agent_id": "codex", "capabilities": ["coding", "research", "terminal"], "enabled": True, "healthy": True}
        ],
        budget_remaining={"tasks_remaining": 15, "max_timeout_seconds": 300},
    )
    goal = await brain.generate_goal(ctx)
    assert goal.should_act is True
    assert goal.title

    plan = await brain.plan(ctx.model_copy(update={"goal": goal}))
    assert plan.objective
    assert plan.permission_level in {"L0", "L1", "L2"}

    ev = await brain.evaluate(
        EvaluationContext(
            goal=goal.model_dump(mode="json"),
            plan=plan.model_dump(mode="json"),
            execution={
                "status": "SUCCEEDED",
                "stdout": "SQLite WAL writes changes to a separate -wal file before checkpointing to the main DB.",
            },
            verification_passed=True,
            pre_observation=obs,
            post_observation=obs,
        )
    )
    assert ev.summary
    assert ev.goal_achieved is True
