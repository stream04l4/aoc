"""P0-4 Cognition unit tests: structured output parsing, retries, model swap, safety guards."""

from __future__ import annotations

import json
import httpx
import pytest
from pydantic import SecretStr

from app.cognition.brain import CognitionBrain
from app.cognition.interfaces import (
    ActionPlan,
    AssessmentResult,
    BrainSchemaError,
    CognitiveContext,
    EvaluationContext,
)
from app.cognition.llm_adapter import OpenAICompatibleLLMAdapter, extract_json_object
from app.core.config import LLMSettings
from app.drives.interfaces import DriveDecision, DriveState
from app.perception.interfaces import Observation


def _mock_client(responses: list[tuple[int, dict]]):
    idx = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        i = min(idx["i"], len(responses) - 1)
        idx["i"] += 1
        code, body = responses[i]
        return httpx.Response(code, json=body, request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _chat_resp(content: str, model: str = "test-model", tokens: int = 50) -> tuple[int, dict]:
    return (
        200,
        {
            "model": model,
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 20, "total_tokens": tokens},
        },
    )


def test_extract_json_handles_think_tags_and_fences():
    raw = '<think>let me reason about {"nested": 1}</think>\n```json\n{"summary": "ok", "confidence": 0.8}\n```'
    extracted = extract_json_object(raw)
    assert json.loads(extracted) == {"summary": "ok", "confidence": 0.8}


@pytest.mark.asyncio
async def test_adapter_retries_on_schema_error_and_succeeds():
    bad = _chat_resp("not valid json at all")
    good = _chat_resp(json.dumps({"summary": "healthy", "confidence": 0.9}))
    async with _mock_client([bad, good]) as client:
        adapter = OpenAICompatibleLLMAdapter(
            LLMSettings(base_url="http://mock/v1", model="m1", max_schema_retries=2),
            http_client=client,
        )
        res = await adapter.complete_structured("assess", "sys", {"k": 1}, AssessmentResult)
        assert res.summary == "healthy"
        assert res.confidence == 0.9
        assert res.usage.total_tokens == 100  # summed across both attempts


@pytest.mark.asyncio
async def test_adapter_raises_schema_error_after_exhausting_retries():
    bad = _chat_resp("{broken")
    async with _mock_client([bad, bad, bad]) as client:
        adapter = OpenAICompatibleLLMAdapter(
            LLMSettings(base_url="http://mock/v1", model="m1", max_schema_retries=1),
            http_client=client,
        )
        with pytest.raises(BrainSchemaError):
            await adapter.complete_structured("assess", "sys", {}, AssessmentResult)


@pytest.mark.asyncio
async def test_model_can_be_swapped_via_config_only():
    seen_models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        seen_models.append(body["model"])
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "choices": [{"message": {"content": json.dumps({"summary": "ok", "confidence": 0.8})}}],
                "usage": {"total_tokens": 10},
            },
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for model_name in ("model-alpha", "model-beta"):
            brain = CognitionBrain(
                OpenAICompatibleLLMAdapter(
                    LLMSettings(base_url="http://mock/v1", model=model_name, api_key=SecretStr("k")),
                    http_client=client,
                )
            )
            res = await brain.assess(Observation(), [], [])
            assert res.usage.model == model_name
    assert seen_models == ["model-alpha", "model-beta"]


@pytest.mark.asyncio
async def test_brain_cannot_declare_success_when_verification_failed():
    # LLM tries to claim goal_achieved=True even though execution/verification failed
    dishonest = _chat_resp(
        json.dumps(
            {
                "goal_achieved": True,
                "summary": "looks good",
                "resolved_topic": "raft",
                "acquired_skill": "bash",
                "information_gain": 0.9,
            }
        )
    )
    async with _mock_client([dishonest]) as client:
        brain = CognitionBrain(
            OpenAICompatibleLLMAdapter(LLMSettings(base_url="http://mock/v1", model="m"), http_client=client)
        )
        ev = await brain.evaluate(
            EvaluationContext(
                goal={"title": "study raft"},
                plan={"objective": "study raft"},
                execution={"status": "FAILED", "error": "timeout"},
                verification_passed=False,
                pre_observation=Observation(),
                post_observation=Observation(),
            )
        )
        assert ev.goal_achieved is False
        assert ev.resolved_topic is None
        assert ev.acquired_skill is None
        assert "Overridden" in ev.notes


@pytest.mark.asyncio
async def test_brain_full_cognitive_flow_and_privilege_isolation():
    responses = [
        _chat_resp(
            json.dumps(
                {
                    "summary": "Open topic detected: duckdb",
                    "knowledge_gaps": [{"topic": "duckdb", "importance": 0.8}],
                    "confidence": 0.85,
                }
            )
        ),
        _chat_resp(
            json.dumps(
                {
                    "should_act": True,
                    "title": "Research DuckDB architecture",
                    "description": "Summarize vectorized execution",
                    "drive_type": "curiosity",
                    "related_key": "duckdb",
                    "success_criteria": ["produce summary.md"],
                    "expected_outcome": {"curiosity_delta": 0.15},
                }
            )
        ),
        _chat_resp(
            json.dumps(
                {
                    "should_execute": True,
                    "goal_title": "Research DuckDB architecture",
                    "objective": "Write concise notes on DuckDB vectorized engine to summary.md",
                    "assigned_agent": "codex",
                    "required_capability": "research",
                    "expected_output": "summary.md",
                    "timeout_seconds": 120,
                    "permission_level": "L1",
                    "success_criteria": ["summary.md exists"],
                    "verification": [{"kind": "file_exists", "target": "summary.md"}],
                    "expected_improvement": 0.15,
                }
            )
        ),
    ]
    async with _mock_client(responses) as client:
        brain = CognitionBrain(
            OpenAICompatibleLLMAdapter(LLMSettings(base_url="http://mock/v1", model="m"), http_client=client)
        )
        # Privilege isolation check: brain holds no db/repo/agent/policy handles
        assert not hasattr(brain, "repo")
        assert not hasattr(brain, "db")
        assert not hasattr(brain, "registry")

        obs = Observation(environment_state={"unknown_topics": [{"topic": "duckdb"}]})
        drives = [DriveState(drive_type="curiosity", satisfaction=0.3, urgency=0.7, confidence=0.9)]
        assess = await brain.assess(obs, [], drives)
        assert assess.knowledge_gaps[0].topic == "duckdb"

        ctx = CognitiveContext(
            observation=obs,
            drives=drives,
            decision=DriveDecision(selected_drive="curiosity", priority_score=0.5, reason="gap"),
            assessment=assess,
            available_agents=[{"agent_id": "codex", "capabilities": ["coding", "research"], "enabled": True}],
        )
        goal = await brain.generate_goal(ctx)
        assert goal.should_act is True
        plan = await brain.plan(ctx.model_copy(update={"goal": goal}))
        assert isinstance(plan, ActionPlan)
        assert plan.assigned_agent == "codex"
        assert plan.permission_level == "L1"
