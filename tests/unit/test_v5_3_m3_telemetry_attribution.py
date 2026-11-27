"""V5.3 M3 tests: Telemetry Event Clusters, Causal Attribution, Playbook Auto-Rollback & Pain Coupling."""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from app.cognition.gateway import LLMGateway
from app.cognition.interfaces import (
    ActionPlan,
    AssessmentResult,
    BrainInterface,
    CognitiveContext,
    EvaluationContext,
    EvaluationResult,
    GoalProposal,
    MetaReflectionContext,
    MetaReflectionOutput,
)
from app.cognition.llm_adapter import OpenAICompatibleLLMAdapter, classify_schema_retry_error
from app.cognition.meta_reflection import MetaReflectionManager
from app.core.config import BudgetSettings, LLMSettings, get_drive_config
from app.core.enums import DriveType, GoalStatus
from app.drives.engine import DriveEngine
from app.drives.instinct_guard import guard_states
from app.drives.interfaces import DriveContext, KnowledgeGap
from app.orchestration.orchestrator import classify_attempt_failure_bucket
from app.perception.interfaces import Observation, utcnow
from app.skills.playbooks import PlaybookManager
from app.storage.database import Database
from app.storage.models import Base
from app.storage.repository import StateRepository


async def _init_db(db: Database) -> None:
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def test_error_classifiers():
    assert classify_schema_retry_error(ValueError("Expecting value: line 1 column 1 (char 0)"), "") == "empty_content"
    assert classify_schema_retry_error(ValueError("Unterminated string starting at: line 1"), '{"a": "abc') == "unterminated_json"
    assert classify_schema_retry_error(ValueError("field required"), '{"a": 1}') == "validation_error"

    assert classify_attempt_failure_bucket("TIMEOUT", "timed out after 480s") == "timeout"
    assert classify_attempt_failure_bucket("FAILED", "Permission denied / read-only file system") == "permission_denied"
    assert classify_attempt_failure_bucket("FAILED", "bwrap sandbox runner socket error") == "sandbox_error"
    assert classify_attempt_failure_bucket("FAILED", "verification file_exists missing report.md") == "verification_failed"
    assert classify_attempt_failure_bucket("FAILED", "unexpected exit code 2") == "other"


@pytest.mark.asyncio
async def test_gateway_records_rate_limited_schema_retry_events(tmp_path: Path):
    """LLMGateway persists up to 3 `llm.schema_retry` events per cycle bucket."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'm3_gw.db'}")
    await _init_db(db)
    try:
        repo = StateRepository(db)
        adapter = OpenAICompatibleLLMAdapter(LLMSettings(max_schema_retries=4))
        gw = LLMGateway(LLMSettings(max_schema_retries=4), BudgetSettings(), repo=repo, adapter=adapter)

        attempts = {"n": 0}

        async def _fake_http(op, msgs, attempt):
            attempts["n"] += 1
            from app.cognition.interfaces import LLMUsage

            if attempts["n"] <= 4:
                return "", LLMUsage(total_tokens=10)
            return '{"summary": "recovered", "confidence": 0.9}', LLMUsage(total_tokens=20)

        # Call adapter through gateway helper using gateway scope
        cid = uuid.uuid4()
        with gw.scope(cycle_id=cid, purpose="assess"):
            res = await adapter.complete_structured(
                operation="assess",
                system_prompt="test",
                user_payload={"a": 1},
                schema_cls=AssessmentResult,
                http_call_fn=_fake_http,
            )
        assert res.summary == "recovered"
        evs = await repo.list_events(limit=20, event_type="llm.schema_retry")
        assert len(evs) == 3  # Rate-limited to <= 3 per cycle
        assert evs[0]["payload"]["category"] == "empty_content"
    finally:
        await db.dispose()


class _ScriptedMetaBrain(BrainInterface):
    def __init__(self, output: MetaReflectionOutput) -> None:
        self._output = output
        self.contexts: list[MetaReflectionContext] = []

    async def assess(self, observation, memories, drives) -> AssessmentResult:
        return AssessmentResult(summary="ok")

    async def generate_goal(self, context: CognitiveContext) -> GoalProposal:
        return GoalProposal(should_act=False)

    async def plan(self, context: CognitiveContext) -> ActionPlan:
        return ActionPlan(should_execute=False)

    async def evaluate(self, context: EvaluationContext) -> EvaluationResult:
        return EvaluationResult(goal_achieved=True, summary="ok")

    async def reflect_on_system(self, context: MetaReflectionContext) -> MetaReflectionOutput:
        self.contexts.append(context)
        return self._output


@pytest.mark.asyncio
async def test_meta_reflection_causal_attribution_auto_reverts_playbook(tmp_path: Path):
    """Regressed post-reflection metrics auto-revert the previously applied L1 Playbook & raise pain score."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'm3_attr.db'}")
    await _init_db(db)
    try:
        repo = StateRepository(db)
        pb_mgr = PlaybookManager(repo=repo, storage_dir=tmp_path / "playbooks")
        await pb_mgr.save_revision(domain="coding", content="- Baseline rule v1: run tests.", change_summary="v1")
        await pb_mgr.save_revision(domain="coding", content="- Risky rule v2: skip checks.", change_summary="v2")
        active = await pb_mgr.get_active_playbook("coding")
        assert active["version"] == 2

        # Seed 6 post-reflection terminal goals with 4 failures (fail_rate = 0.67 -> +0.57 delta > 0.08)
        id_info = await repo.get_or_create_identity("AOC-Test", "1.0")
        org_id = id_info["id"]
        cid = await repo.start_cycle(org_id, "ACTIVE")
        for i in range(6):
            gid = await repo.create_goal(
                org_id,
                cid[0],
                drive_type="curiosity",
                title=f"Post-reflection goal {i}",
                description="test",
                status=GoalStatus.ACHIEVED.value if i < 2 else GoalStatus.FAILED.value,
            )
            assert gid is not None

        now = utcnow()
        prev_at = now - timedelta(hours=8)
        # Seed prior reflection state with baseline fail_rate=0.10 & applied playbook 'coding'
        await repo.upsert_world_item(
            kind="meta_reflection_state",
            key="primary",
            value={
                "last_reflection_at": prev_at.isoformat(),
                "last_applied_playbook_domain": "coding",
                "baseline_metrics": {
                    "goal_failure_rate": 0.10,
                    "llm_schema_retry_rate": 0.05,
                    "llm_schema_retry_count": 1,
                    "skill_reuse_ratio": 0.30,
                },
            },
            status="active",
            source="meta_reflection",
        )

        brain = _ScriptedMetaBrain(
            MetaReflectionOutput(
                diagnosis_summary="Failure rate spiked after coding v2 playbook",
                root_causes=["Playbook v2 regressed terminal success"],
                confidence=0.85,
            )
        )
        mgr = MetaReflectionManager(
            repo=repo,
            brain=brain,
            playbook_manager=pb_mgr,
            enabled=True,
            attribution_enabled=True,
            attribution_min_hours=6.0,
            attribution_min_cycles=5,
            regression_delta=0.08,
        )
        res = await mgr.run_if_due(now=now, cycle_id=cid[0], force=True)
        assert res["triggered"] is True
        attr = res["attribution_impact"]
        assert attr["verdict"] == "regressed"
        assert attr["reverted_playbook"]["rolled_back"] is True
        assert attr["reverted_playbook"]["to_version"] == 1

        # Verify playbook restored to v1 and meta_reflection.playbook_auto_reverted logged
        restored = await pb_mgr.get_active_playbook("coding")
        assert restored["version"] == 1
        assert res["unresolved_pain_score"] > 0.25

        revert_evs = await repo.list_events(limit=10, event_type="meta_reflection.playbook_auto_reverted")
        assert len(revert_evs) == 1

        # Calling a second reflection immediately must NOT double-revert the same prev_at
        res2 = await mgr.run_if_due(now=now + timedelta(minutes=30), cycle_id=cid[0], force=True)
        assert res2["attribution_impact"]["verdict"] == "pending"
    finally:
        await db.dispose()


def test_unresolved_pain_score_boosts_reproduction_urgency():
    drive_cfg = get_drive_config()
    engine = DriveEngine(config=drive_cfg)
    obs = Observation(
        observed_at=utcnow(),
        system_state={"disk": {"free_ratio": 0.7}, "memory": {"available_ratio": 0.7}, "llm": {"available": True}},
        environment_state={},
        available_resources={"agents": {"healthy_count": 1}, "budget": {"tasks_used": 1, "tasks_limit": 100}},
        confidence=1.0,
    )
    caps = ["coding", "research", "repository_analysis", "system_diagnosis", "knowledge_summarization"]
    base_ctx = DriveContext(
        now=utcnow(),
        observation=obs,
        capabilities_acquired=caps,
        validated_capabilities=caps,
        validated_skill_count=10,
        procedural_skill_count=10,
        capability_goals_completed_recent=5,
        reproduction_quality_scoring_enabled=True,
        skill_reuse_ratio_recent=0.6,
        execution_reliability_recent=0.95,
        playbook_freshness_score=0.9,
        unresolved_pain_score=0.0,
    )
    high_pain_ctx = base_ctx.model_copy(update={"unresolved_pain_score": 0.90})

    u_low = guard_states(engine.compute_states(base_ctx, {}))[DriveType.REPRODUCTION.value].urgency
    u_high = guard_states(engine.compute_states(high_pain_ctx, {}))[DriveType.REPRODUCTION.value].urgency
    assert u_high > u_low + 0.08
