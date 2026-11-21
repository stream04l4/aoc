"""V3.1 Kernel Completion & Control Center Tests:
  1. Affect loop completion: SelfModel confidence anchoring, prediction-error surprise,
     read-only affect_state injected into Brain CognitiveContext, Scheduler reflection_interval_factor,
     and Orchestrator retry_backoff_multiplier.
  2. Multi-candidate goal deterministic scoring & fallback when primary hits dedup/cooldown + predicted_ig calibration.
  3. Lightweight World Hypotheses (digital_world_state kind='hypothesis', strict verification guard) +
     Curiosity-driven allowlist-governed dynamic world source registration + Control Center API adaptation.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.affect.interfaces import AffectConfig, AffectState
from app.affect.modulator import AffectModulator
from app.agents.base import (
    AgentCapability,
    AgentExecution,
    AgentInterface,
    AgentProbeResult,
    AgentStatus,
    AgentTask,
)
from app.agents.registry import AgentRegistry
from app.api.app import create_app
from app.cognition.goal_dedup import GoalDedupEngine, compute_goal_fingerprint, select_best_goal_candidate
from app.cognition.interfaces import (
    ActionPlan,
    AssessmentResult,
    BrainInterface,
    CognitiveContext,
    EvaluationContext,
    EvaluationResult,
    GoalProposal,
    VerificationSpec,
)
from app.core.config import (
    ApiSettings,
    DatabaseSettings,
    EmbeddingSettings,
    GovernanceSettings,
    KernelV3Settings,
    MemorySettings,
    PerceptionSettings,
    RunnerSettings,
    RuntimeSettings,
    Settings,
)
from app.core.lifecycle import LifecycleManager
from app.core.runtime import OrganismRuntime
from app.core.scheduler import OrganismScheduler
from app.governance.interfaces import PolicyVerdict
from app.governance.policy import GovernanceManager
from app.orchestration.orchestrator import AgentOrchestrator
from app.perception.interfaces import Observation, utcnow
from app.storage.database import Database
from app.storage.models import Base
from app.storage.repository import StateRepository


async def _init_db(db: Database) -> None:
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _llm_ok() -> dict[str, Any]:
    return {"available": True, "latency_ms": 4.0, "model": "stub-v3-1"}


def test_v3_1_affect_confidence_anchor_prediction_surprise_and_scheduler_factor():
    """Verify SelfModel confidence anchor, prediction surprise modulation, deterministic replay & scheduler factor."""
    mod = AffectModulator(config=AffectConfig(enabled=True, maximum_influence=0.15))

    # 1. SelfModel confidence anchoring pulls confidence smoothly toward real evidence
    st_low, r1 = mod.step_state(AffectState(confidence=0.8), self_model_confidence=0.2)
    st_high, r2 = mod.step_state(AffectState(confidence=0.2), self_model_confidence=0.95)
    assert st_low.confidence < 0.70 and "self_model_conf=0.20" in r1
    assert st_high.confidence > 0.40 and "self_model_conf=0.95" in r2

    # 2. Positive prediction surprise boosts valence/arousal; negative surprise nudges frustration/boredom
    st_pos, rp = mod.step_state(AffectState(), prediction_surprise=0.6)
    st_neg, rn = mod.step_state(AffectState(), prediction_surprise=-0.5)
    assert st_pos.valence > 0.05 and st_pos.arousal > 0.20 and "positive_surprise" in rp
    assert st_neg.frustration > 0.0 and st_neg.boredom > 0.0 and "negative_surprise" in rn

    # 3. Deterministic replay produces identical state
    evs = [
        {"trigger_event_id": "e1", "recent_successes": 1, "last_information_gain": 0.5, "self_model_confidence": 0.85, "prediction_surprise": 0.3},
        {"trigger_event_id": "e1", "recent_successes": 1, "last_information_gain": 0.5, "self_model_confidence": 0.85, "prediction_surprise": 0.3},
        {"trigger_event_id": "e2", "consecutive_failures": 1, "prediction_surprise": -0.4},
    ]
    assert mod.replay_events(evs) == mod.replay_events(evs)

    # 4. Scheduler reads bounded reflection_interval_factor in [0.85, 1.15]
    sched = OrganismScheduler(loop=None, lifecycle=None, settings=RuntimeSettings())  # type: ignore[arg-type]
    assert sched._extract_reflection_factor({"decision": {"constraints": {"affect_modulation": {"enabled": True, "reflection_interval_factor": 1.12}}}}) == 1.12
    assert sched._extract_reflection_factor({"decision": {"constraints": {"affect_modulation": {"enabled": True, "reflection_interval_factor": 2.5}}}}) == 1.15
    assert sched._extract_reflection_factor({"decision": {"constraints": {"affect_modulation": {"enabled": False, "reflection_interval_factor": 1.12}}}}) == 1.0


def test_v3_1_multi_candidate_goal_fallback_and_scoring():
    """When primary goal hits dedup/cooldown, kernel selects highest-scoring novel alternative instead of IDLE."""
    eng = GoalDedupEngine(dedup_window_hours=6.0)
    now = utcnow()
    dup_fp = compute_goal_fingerprint(drive_type="curiosity", title="Check pgvector status", related_key="pgvector_status")
    recent = [
        {
            "goal_id": str(uuid.uuid4()),
            "drive_type": "curiosity",
            "title": "Check pgvector status",
            "related_key": "pgvector_status",
            "goal_fingerprint": dup_fp,
            "status": "ACHIEVED",
            "verified_ig": 0.1,
            "created_at": now - timedelta(minutes=20),
            "cooldown_until": now + timedelta(minutes=60),
        }
    ]
    primary = GoalProposal(
        should_act=True,
        title="Check pgvector status",
        related_key="pgvector_status",
        predicted_ig=0.4,
        alternative_goals=[
            {"title": "Survey Letta archival block indexing", "related_key": "letta_archival", "predicted_ig": 0.72},
            {"title": "Inspect MCP tool audit latency", "related_key": "mcp_latency", "predicted_ig": 0.35},
        ],
    )
    sel = select_best_goal_candidate(
        primary=primary,
        drive_type="curiosity",
        recent_goals=recent,
        dedup_engine=eng,
        now=now,
        affect_state={"boredom": 0.6, "frustration": 0.0},
    )
    assert sel.selected_proposal is not None
    assert sel.used_alternative is True
    assert sel.selected_proposal.related_key == "letta_archival"
    assert len(sel.candidates) == 3
    assert sel.candidates[0].dedup_decision == "DEFER"
    assert sel.candidates[1].selected is True

    # If ALL candidates duplicate -> cleanly returns None
    all_dup = GoalProposal(
        should_act=True,
        title="Check pgvector status",
        related_key="pgvector_status",
        alternative_goals=[{"title": "Check pgvector status", "related_key": "pgvector_status"}],
    )
    sel_none = select_best_goal_candidate(
        primary=all_dup, drive_type="curiosity", recent_goals=recent, dedup_engine=eng, now=now
    )
    assert sel_none.selected_proposal is None


class _FlakyOnceAgent(AgentInterface):
    """Fails attempt 1, succeeds attempt 2 so we verify retry_backoff_multiplier in Orchestrator."""

    def __init__(self) -> None:
        self._cap = AgentCapability(
            agent_id="codex",
            name="FlakyOnce",
            agent_type="cli",
            capabilities=["research", "general_task", "coding"],
            supported_modes=["read-only", "workspace-write"],
            enabled=True,
        )
        self.calls = 0

    @property
    def capability(self) -> AgentCapability:
        return self._cap

    async def probe(self) -> AgentProbeResult:
        return AgentProbeResult(ok=True, binary="codex", version="v3.1")

    async def execute(self, task: AgentTask) -> AgentExecution:
        self.calls += 1
        if self.calls == 1:
            return AgentExecution(
                execution_id=task.execution_id or str(uuid.uuid4()),
                task_id=task.task_id,
                agent_id="codex",
                status="FAILED",
                started_at=utcnow(),
                completed_at=utcnow(),
                error="transient failure",
            )
        return AgentExecution(
            execution_id=task.execution_id or str(uuid.uuid4()),
            task_id=task.task_id,
            agent_id="codex",
            status="SUCCEEDED",
            started_at=utcnow(),
            completed_at=utcnow(),
            result={"summary": "Verified on attempt 2"},
            stdout="verified output",
        )

    async def get_status(self, execution_id: str) -> AgentStatus:
        return AgentStatus(execution_id=execution_id, status="SUCCEEDED")

    async def cancel(self, execution_id: str) -> bool:
        return True


@pytest.mark.asyncio
async def test_v3_1_orchestrator_applies_affect_retry_backoff_multiplier(tmp_path: Path):
    """Verify retry_backoff_multiplier is persisted and logged with non-zero backoff on retry."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'backoff.db'}")
    await _init_db(db)
    try:
        repo = StateRepository(db)
        gov = GovernanceManager(GovernanceSettings(workspace_root=str(tmp_path / "ws")), repo)
        reg = AgentRegistry()
        agent = _FlakyOnceAgent()
        reg.register(agent, probe=AgentProbeResult(ok=True, binary="codex", version="v3.1"))
        orch = AgentOrchestrator(registry=reg, governance=gov, repo=repo, max_retries=2)
        plan = ActionPlan(
            should_execute=True,
            goal_title="Retry backoff check",
            objective="Inspect system state read-only",
            assigned_agent="codex",
            permission_level="L0",
            verification=[VerificationSpec(kind="non_empty_output")],
        )
        verdict = PolicyVerdict(
            allowed=True,
            requested_level="L0",
            effective_level="L0",
            sandbox_mode="read-only",
            effective_timeout_seconds=60,
            reason="ok",
        )
        info = await orch.submit_plan(plan=plan, verdict=verdict, retry_backoff_multiplier=1.02)
        final = await orch.wait_for_task(info["task_id"], max_wait_seconds=10.0, poll_interval=0.05)
        assert final["status"] == "SUCCEEDED"
        assert agent.calls == 2
        decs = await repo.list_decisions(limit=20)
        retry_dec = next((d for d in decs if d["decision_type"] == "agent_fallback_retry"), None)
        assert retry_dec is not None
        assert retry_dec["payload"]["retry_backoff_multiplier"] == 1.02
        assert retry_dec["payload"]["backoff_seconds"] > 0.0
    finally:
        await db.dispose()


class _V31StubAgent(AgentInterface):
    def __init__(self) -> None:
        self._cap = AgentCapability(
            agent_id="codex",
            name="Stub Codex",
            agent_type="cli",
            capabilities=["research", "coding", "general_task"],
            supported_modes=["read-only", "workspace-write"],
            enabled=True,
        )

    @property
    def capability(self) -> AgentCapability:
        return self._cap

    async def probe(self) -> AgentProbeResult:
        return AgentProbeResult(ok=True, binary="codex", version="v3.1-e2e")

    async def execute(self, task: AgentTask) -> AgentExecution:
        return AgentExecution(
            execution_id=task.execution_id or str(uuid.uuid4()),
            task_id=task.task_id,
            agent_id="codex",
            status="SUCCEEDED",
            started_at=utcnow(),
            completed_at=utcnow(),
            result={"summary": "Observed pgvector HNSW p95 latency under 5ms."},
            stdout="HNSW p95 latency measured at 3.2ms across 512d vectors.",
        )

    async def get_status(self, execution_id: str) -> AgentStatus:
        return AgentStatus(execution_id=execution_id, status="SUCCEEDED")

    async def cancel(self, execution_id: str) -> bool:
        return True


class _V31KernelBrain(BrainInterface):
    def __init__(self) -> None:
        self.calls = 0
        self.seen_affect_states: list[dict[str, Any] | None] = []
        self.seen_hypotheses: list[list[dict[str, Any]]] = []

    async def assess(self, observation: Observation, memories: list[dict[str, Any]], drives: list[Any]) -> AssessmentResult:
        return AssessmentResult(summary="Curiosity exploration ready", confidence=0.9)

    async def generate_goal(self, ctx: CognitiveContext) -> GoalProposal:
        self.calls += 1
        self.seen_affect_states.append(ctx.affect_state)
        self.seen_hypotheses.append(list(ctx.active_hypotheses))
        if self.calls == 1:
            return GoalProposal(
                should_act=True,
                title="Verify pgvector HNSW query latency hypothesis",
                description="Inspect pgvector HNSW retrieval latency on local embeddings.",
                related_key="pgvector_hnsw_latency",
                predicted_ig=0.35,
                alternative_goals=[
                    {"title": "Inspect Letta memory sync health", "related_key": "letta_sync_check", "predicted_ig": 0.25}
                ],
                hypothesis_to_test={
                    "key": "pgvector_hnsw_latency",
                    "claim": "pgvector 512d HNSW cosine query p95 stays below 10ms",
                    "verification_hint": "Check retrieval query latency",
                },
                discover_world_source={
                    "source_id": "rss-github-openai-python",
                    "sensor_type": "rss",
                    "url": "https://github.com/openai/openai-python/releases.atom",
                    "name": "OpenAI Python SDK Releases",
                },
            )
        return GoalProposal(should_act=False, title="", rationale="Observed confirmed hypothesis in Cycle 2")

    async def plan(self, ctx: CognitiveContext) -> ActionPlan:
        return ActionPlan(
            should_execute=True,
            goal_title=ctx.goal.title if ctx.goal else "Verify hypothesis",
            objective="Read-only inspection of pgvector HNSW latency metrics without modifying files",
            assigned_agent="codex",
            required_capability="research",
            permission_level="L0",
            verification=[VerificationSpec(kind="non_empty_output")],
            expected_improvement=0.2,
        )

    async def evaluate(self, ctx: EvaluationContext) -> EvaluationResult:
        return EvaluationResult(
            goal_achieved=ctx.verification_passed,
            summary="Verified pgvector HNSW latency p95 is 3.2ms (< 10ms).",
            learned_facts=["pgvector HNSW 512d cosine search p95 is 3.2ms."],
            resolved_topic="pgvector_hnsw_latency",
            hypothesis_key="pgvector_hnsw_latency",
            hypothesis_verdict="confirmed",
            information_gain=0.75,
            should_persist_memory=True,
        )


@pytest.mark.asyncio
async def test_v3_1_end_to_end_affect_hypotheses_prediction_error_and_control_center(tmp_path: Path):
    """End-to-end V3.1 loop + Control Center API verification."""
    db_path = tmp_path / "v3_1_e2e.db"
    db = Database(f"sqlite+aiosqlite:///{db_path}")
    await _init_db(db)

    reg = AgentRegistry()
    reg.register(_V31StubAgent(), probe=AgentProbeResult(ok=True, binary="codex", version="v3.1-e2e"))
    brain = _V31KernelBrain()
    token = "v31-test-admin"

    settings = Settings(
        api=ApiSettings(host="127.0.0.1", auth_enabled=True, admin_key=token),
        database=DatabaseSettings(url=f"sqlite+aiosqlite:///{db_path}"),
        governance=GovernanceSettings(workspace_root=str(tmp_path / "ws")),
        memory=MemorySettings(provider="none"),
        embedding=EmbeddingSettings(provider="hash_fallback"),
        perception=PerceptionSettings(llm_probe=True),
        runner=RunnerSettings(enabled=False),
        kernel_v3=KernelV3Settings(affect_enabled=True, world_poll_on_perceive=False),
    )
    rt = OrganismRuntime(
        settings=settings,
        db=db,
        brain=brain,
        registry=reg,
        auto_load_agents=False,
        llm_probe_fn=_llm_ok,
    )
    await rt.start(start_scheduler=False)
    try:
        # Seed open knowledge gaps so Curiosity wins Cycle 1
        for k in ("pgvector_hnsw_latency", "mcp_prefetch_design", "letta_recall_speed", "affect_calibration"):
            await rt.repo.upsert_world_item(
                kind="knowledge_topic",
                key=k,
                value={"description": f"Research {k}", "importance": 0.9},
                status="open",
                source="test_v3_1",
            )

        # Cycle 1: Brain receives read-only affect_state, proposes hypothesis + allowlisted dynamic source + predicted_ig
        c1 = await rt.loop.run_cycle()
        assert c1["outcome"] == "SUCCEEDED"
        assert brain.seen_affect_states[0] is not None
        assert {"valence", "arousal", "confidence", "frustration", "boredom"} <= set(brain.seen_affect_states[0].keys())

        # Prediction error calibration recorded in evaluation
        cal = (c1.get("evaluation") or {}).get("prediction_calibration")
        assert cal is not None
        assert cal["predicted_ig"] == 0.35
        assert cal["verified_ig"] > 0.0
        assert cal["prediction_error"] == round(cal["verified_ig"] - 0.35, 4)

        # Hypothesis persisted and deterministically updated to confirmed with execution evidence
        hyps = await rt.repo.list_world_items(kind="hypothesis")
        h = next((x for x in hyps if x["key"] == "pgvector_hnsw_latency"), None)
        assert h is not None
        assert h["status"] == "confirmed"
        assert len((h["value"] or {}).get("evidence") or []) == 1

        # Curiosity-driven allowlisted dynamic world source registered cleanly
        srcs = await rt.repo.list_world_sources()
        assert any(s["source_id"] == "rss-github-openai-python" for s in srcs)

        # Cycle 2: simulate elapsed time (clear inhibition window) so Curiosity enters GOAL_GENERATION
        rt.loop._last_learning_at = None
        rt.loop._last_info_gain = None
        ds_rows = await rt.repo.load_drive_states(rt.organism_id)
        for row in ds_rows:
            row["last_satisfied_at"] = None
        from app.drives.interfaces import DriveState
        await rt.repo.save_drive_states(rt.organism_id, [DriveState(**r) for r in ds_rows], cycle_id=None, phase="pre")
        c2 = await rt.loop.run_cycle()
        assert c2["outcome"] == "IDLE"
        assert len(brain.seen_hypotheses) == 2
        assert any(x["key"] == "pgvector_hnsw_latency" and x["status"] == "confirmed" for x in brain.seen_hypotheses[1])

        # Control Center APIs expose all V3.1 fields cleanly
        app = create_app(settings=settings, runtime=rt, start_scheduler=False)
        app.state.runtime = rt
        app.state.settings = rt.settings
        app.state.db = rt.db
        app.state.repo = rt.repo
        app.state.organism_id = rt.organism_id
        headers = {"Authorization": f"Bearer {token}"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r_dr = await client.get("/v2/control-center/drives?hours=24", headers=headers)
            assert r_dr.status_code == 200
            dr = r_dr.json()
            assert dr["affect"]["brain_read_only_injected"] is True
            assert dr["affect"]["confidence_evidence_anchored"] is True
            assert dr["affect"]["runtime_hooks"]["retry_backoff_active"] is True
            assert len(dr["ig_calibration"]) >= 1
            assert dr["ig_calibration"][0]["predicted_ig"] == 0.35

            r_wd = await client.get("/v2/control-center/world", headers=headers)
            assert r_wd.status_code == 200
            wd = r_wd.json()
            assert isinstance(wd["allowed_domains"], list) and len(wd["allowed_domains"]) >= 1
            assert any(x["key"] == "pgvector_hnsw_latency" and x["status"] == "confirmed" for x in wd["hypotheses"])

            r_tr = await client.get(f"/v2/control-center/cycles/{c1['cycle_id']}/trace", headers=headers)
            assert r_tr.status_code == 200
            tr = r_tr.json()
            assert tr["steps"]["3_arbitration"]["affect_snapshot"] is not None
            assert len(tr["steps"]["5_goal_generation"]["goal_candidates"]) == 2
            assert tr["steps"]["10_outcome_evaluation"]["evaluation"]["prediction_calibration"] is not None
    finally:
        await rt.stop()
        await db.dispose()
