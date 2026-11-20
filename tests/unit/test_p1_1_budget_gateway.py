"""P1-1 unit & fault-injection tests:
  - T1: Budget exhaustion decoupled from Survival CRITICAL, 100 throttled cycles with 0 spin/0 LLM calls,
        automatic recovery when rolling 24h quota refreshes.
  - T9: Unified LLMGateway enforcing max_llm_calls_per_cycle (incl. retries), max_tokens_per_cycle,
        max_concurrent_llm_calls, daily/emergency/hard-cap budgets, failure/retry accounting,
        circuit breaker open + half-open recovery, external Codex token attribution, and static bypass audit.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import update

from app.agents.base import (
    AgentCapability,
    AgentExecution,
    AgentInterface,
    AgentProbeResult,
    AgentStatus,
    AgentTask,
)
from app.agents.registry import AgentRegistry
from app.cognition.brain import CognitionBrain
from app.cognition.gateway import LLMBudgetExceededError, LLMCircuitOpenError, LLMGateway
from app.cognition.interfaces import ActionPlan, AssessmentResult, BrainSchemaError
from app.core.config import (
    BudgetSettings,
    GovernanceSettings,
    LLMSettings,
    MemorySettings,
    PerceptionSettings,
    RuntimeSettings,
    Settings,
)
from app.core.enums import ExecutionStatus, LifecycleState, QuotaStatus
from app.core.runtime import OrganismRuntime
from app.governance.policy import GovernanceManager
from app.orchestration.orchestrator import AgentOrchestrator
from app.perception.interfaces import utcnow
from app.storage.database import Database
from app.storage.models import ResourceUsage
from app.storage.repository import StateRepository, day_start
from tests.unit.test_p0_7_loop import ScriptedBrain, StubAgent


def _chat_ok(content: str, prompt_tokens: int = 30, completion_tokens: int = 20) -> dict:
    return {
        "model": "test-model",
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


@pytest.mark.asyncio
async def test_100_budget_exhausted_cycles_no_critical_no_llm_no_alarm_spin(db: Database, tmp_path):
    """Acceptance gate: 100 consecutive budget-exhausted cycles produce 0 CRITICAL, 0 LLM calls, 0 spin transitions."""
    brain = ScriptedBrain()
    settings = Settings(
        runtime=RuntimeSettings(cycle_interval_seconds=0.1, idle_sleep_seconds=0.1, throttled_max_sleep_seconds=60),
        perception=PerceptionSettings(disk_paths=["/"], llm_probe=False),
        memory=MemorySettings(provider="none"),
        governance=GovernanceSettings(
            workspace_root=str(tmp_path),
            budget=BudgetSettings(max_agent_tasks_per_day=2, max_llm_tokens_per_day=100_000),
        ),
    )
    reg = AgentRegistry()
    reg.register(StubAgent("worker-1"))

    async def llm_ok():
        return {"available": True, "latency_ms": 80.0}

    rt = OrganismRuntime(
        settings=settings,
        db=db,
        brain=brain,
        registry=reg,
        auto_load_agents=False,
        llm_probe_fn=llm_ok,
    )
    reg.repo = rt.repo
    await rt.start(start_scheduler=False)

    try:
        # Inject open curiosity topics AND exhaust the daily task budget (2/2)
        for i in range(3):
            await rt.repo.upsert_world_item("knowledge_topic", f"topic-{i}", {"importance": 0.95}, status="open")
        await rt.repo.record_usage("agent_task", amount=1.0)
        await rt.repo.record_usage("agent_task", amount=1.0)

        critical_hits = 0
        outcomes: list[str] = []
        for _ in range(100):
            rec = await rt.loop.run_cycle()
            outcomes.append(rec["outcome"])
            if rt.lifecycle.state == LifecycleState.CRITICAL:
                critical_hits += 1

        assert critical_hits == 0, "Budget exhaustion must NEVER trigger Survival CRITICAL"
        assert all(o == "BUDGET_EXHAUSTED" for o in outcomes)
        assert rt.lifecycle.state == LifecycleState.THROTTLED
        assert len(brain.calls) == 0, f"Must not invoke LLM while throttled, got calls={brain.calls}"

        # Verify organism_state persisted quota_reset_at and throttled_reason
        ost = await rt.repo.get_organism_state(rt.organism_id)
        assert ost["lifecycle_state"] == LifecycleState.THROTTLED.value
        assert ost["quota_reset_at"] is not None
        assert "daily agent task limit" in (ost["throttled_reason"] or "")

        # Only ONE transition event into THROTTLED across all 100 cycles (no repeated alarm spam)
        transitions = await rt.repo.list_events(limit=200, event_type="lifecycle.transition")
        to_throttled = [e for e in transitions if (e.get("payload") or {}).get("to") == LifecycleState.THROTTLED.value]
        to_critical = [e for e in transitions if (e.get("payload") or {}).get("to") == LifecycleState.CRITICAL.value]
        assert len(to_throttled) == 1
        assert len(to_critical) == 0
    finally:
        await rt.stop()


@pytest.mark.asyncio
async def test_quota_refresh_auto_recovers_from_throttled(db: Database, tmp_path):
    """When rolling 24h usage expires past the window, organism automatically clears throttle and resumes."""
    brain = ScriptedBrain(plan_level="L1")
    settings = Settings(
        runtime=RuntimeSettings(monitor_poll_seconds=0.02, max_wait_per_cycle_seconds=5),
        perception=PerceptionSettings(disk_paths=["/"], llm_probe=False),
        memory=MemorySettings(provider="none"),
        governance=GovernanceSettings(
            workspace_root=str(tmp_path),
            budget=BudgetSettings(max_agent_tasks_per_day=1),
        ),
    )
    reg = AgentRegistry()
    reg.register(StubAgent("worker-1"))

    async def llm_ok():
        return {"available": True, "latency_ms": 80.0}

    rt = OrganismRuntime(
        settings=settings,
        db=db,
        brain=brain,
        registry=reg,
        auto_load_agents=False,
        llm_probe_fn=llm_ok,
    )
    reg.repo = rt.repo
    await rt.start(start_scheduler=False)

    try:
        for cap in ("coding", "research", "repository_analysis", "system_diagnosis", "knowledge_summarization"):
            await rt.repo.upsert_world_item("capability", cap, {}, status="acquired")
        for i in range(3):
            await rt.repo.upsert_world_item("knowledge_topic", f"refresh-topic-{i}", {"importance": 0.95}, status="open")

        await rt.repo.record_usage("agent_task", amount=1.0)
        rec1 = await rt.loop.run_cycle()
        assert rec1["outcome"] == "BUDGET_EXHAUSTED"
        assert rt.lifecycle.state == LifecycleState.THROTTLED

        # Age the usage record by 25 hours so the rolling 24h quota refreshes
        old_ts = utcnow() - timedelta(hours=25)
        async with db.session() as s:
            await s.execute(update(ResourceUsage).values(created_at=old_ts))

        rec2 = await rt.loop.run_cycle()
        assert rec2["outcome"] == "SUCCEEDED"
        ost = await rt.repo.get_organism_state(rt.organism_id)
        assert ost["quota_reset_at"] is None
        assert ost["throttled_reason"] is None
        assert len(brain.calls) >= 3
    finally:
        await rt.stop()


@pytest.mark.asyncio
async def test_gateway_enforces_max_calls_per_cycle_including_retries_and_accounts_failures(repo: StateRepository):
    """Per-cycle call limit counts every HTTP attempt (including schema retries) and records failed attempts."""
    calls_made = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls_made["n"] += 1
        # Always return invalid JSON to force schema retries
        return httpx.Response(200, json=_chat_ok("not json", prompt_tokens=40, completion_tokens=10), request=req)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gw = LLMGateway(
            llm_settings=LLMSettings(base_url="http://mock/v1", model="m1", max_schema_retries=5),
            budget_settings=BudgetSettings(max_llm_calls_per_cycle=3, max_tokens_per_cycle=10_000),
            repo=repo,
            http_client=client,
        )
        cid = uuid.uuid4()
        # With max_schema_retries=5 (6 attempts), gateway MUST hard-stop at attempt 4 (after 3 real HTTP calls)
        with pytest.raises(LLMBudgetExceededError, match="Per-cycle LLM call limit"):
            await gw.complete_structured("assess", "sys", {}, AssessmentResult, cycle_id=cid)

        assert calls_made["n"] == 3
        assert gw.get_cycle_stats(cid)["calls"] == 3
        assert gw.get_cycle_stats(cid)["tokens"] == 150

        # DB ledger recorded all 3 failed-schema HTTP attempts with exact tokens
        db_cycle = await repo.llm_usage_for_cycle(cid)
        assert db_cycle["calls"] == 3
        assert db_cycle["tokens"] == 150.0


@pytest.mark.asyncio
async def test_gateway_enforces_max_tokens_per_cycle_and_concurrency_cap(repo: StateRepository):
    async def slow_handler(req: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.06)
        return httpx.Response(
            200,
            json=_chat_ok(json.dumps({"summary": "ok", "confidence": 0.9}), prompt_tokens=60, completion_tokens=40),
            request=req,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(slow_handler)) as client:
        gw = LLMGateway(
            llm_settings=LLMSettings(base_url="http://mock/v1", model="m1", max_schema_retries=0),
            budget_settings=BudgetSettings(
                max_concurrent_llm_calls=2,
                max_llm_calls_per_cycle=20,
                max_tokens_per_cycle=250,
            ),
            repo=repo,
            http_client=client,
        )

        # Concurrency cap: 6 simultaneous requests must never exceed peak concurrency 2
        results = await asyncio.gather(
            *(gw.complete_structured("assess", "sys", {"i": i}, AssessmentResult) for i in range(6))
        )
        assert len(results) == 6
        assert gw.peak_concurrent_calls <= 2

        # Per-cycle token cap (250): 3 calls x 100 tokens -> 4th call blocked
        cid = uuid.uuid4()
        for _ in range(3):
            await gw.complete_structured("assess", "sys", {}, AssessmentResult, cycle_id=cid)
        with pytest.raises(LLMBudgetExceededError, match="Per-cycle LLM token limit"):
            await gw.complete_structured("assess", "sys", {}, AssessmentResult, cycle_id=cid)


@pytest.mark.asyncio
async def test_gateway_emergency_budget_and_hard_daily_cap(repo: StateRepository):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_chat_ok(json.dumps({"summary": "em", "confidence": 0.8}), prompt_tokens=30, completion_tokens=20),
            request=req,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gw = LLMGateway(
            llm_settings=LLMSettings(base_url="http://mock/v1", model="m1", max_schema_retries=0),
            budget_settings=BudgetSettings(
                max_llm_tokens_per_day=100,
                emergency_llm_calls_per_day=5,
                emergency_llm_tokens_per_day=500,
                hard_daily_llm_tokens_cap=160,
            ),
            repo=repo,
            http_client=client,
        )
        # Seed 100 tokens used today -> normal quota exhausted
        await repo.record_usage("llm", amount=100.0, details={"source": "seed"})

        # Normal call rejected
        with pytest.raises(LLMBudgetExceededError, match="Daily LLM token budget"):
            await gw.complete_structured("assess", "sys", {}, AssessmentResult, emergency=False)

        # Emergency call allowed while below hard_daily_llm_tokens_cap (100 -> 150 -> 200)
        r1 = await gw.complete_structured("assess", "sys", {}, AssessmentResult, emergency=True)
        assert r1.summary == "em"
        r2 = await gw.complete_structured("assess", "sys", {}, AssessmentResult, emergency=True)
        assert r2.summary == "em"  # now total daily = 200 >= hard_cap 160

        # Once hard cap is reached, even emergency calls are hard-blocked
        with pytest.raises(LLMBudgetExceededError, match="Hard daily LLM token cap"):
            await gw.complete_structured("assess", "sys", {}, AssessmentResult, emergency=True)


@pytest.mark.asyncio
async def test_gateway_circuit_breaker_opens_and_recovers(repo: StateRepository):
    mode = {"fail": True}
    http_hits = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        http_hits["n"] += 1
        if mode["fail"]:
            return httpx.Response(500, json={"error": "upstream down"}, request=req)
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []}, request=req)
        return httpx.Response(
            200, json=_chat_ok(json.dumps({"summary": "recovered", "confidence": 0.9})), request=req
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gw = LLMGateway(
            llm_settings=LLMSettings(base_url="http://mock/v1", model="m1", max_schema_retries=0),
            budget_settings=BudgetSettings(circuit_breaker_threshold=2, circuit_breaker_cooldown_seconds=0.15),
            repo=repo,
            http_client=client,
        )
        # 2 consecutive HTTP 500 errors open the circuit and are both recorded in DB
        for _ in range(2):
            with pytest.raises(Exception):
                await gw.complete_structured("assess", "sys", {}, AssessmentResult)

        assert gw.circuit_open is True
        hits_before_open = http_hits["n"]
        assert hits_before_open == 2

        # While circuit is open, calls fail fast without touching upstream HTTP & health_check reflects open state
        with pytest.raises(LLMCircuitOpenError):
            await gw.complete_structured("assess", "sys", {}, AssessmentResult)
        assert http_hits["n"] == hits_before_open

        probe = await gw.health_check()
        assert probe["available"] is False
        assert probe.get("circuit_open") is True

        # Failed attempts were recorded in DB usage table
        usage = await repo.usage_since("llm", day_start())
        assert usage["count"] == 2

        # After cooldown expires and upstream recovers, half-open call succeeds and closes circuit
        await asyncio.sleep(0.18)
        mode["fail"] = False
        res = await gw.complete_structured("assess", "sys", {}, AssessmentResult)
        assert res.summary == "recovered"
        assert gw.circuit_open is False


@pytest.mark.asyncio
async def test_orchestrator_attributes_external_codex_tokens_to_llm_gateway(repo: StateRepository, tmp_path):
    class TokenReportingAgent(AgentInterface):
        def __init__(self) -> None:
            self._cap = AgentCapability(
                agent_id="codex", name="Codex", agent_type="cli", capabilities=["coding"], enabled=True
            )

        @property
        def capability(self) -> AgentCapability:
            return self._cap

        async def probe(self) -> AgentProbeResult:
            return AgentProbeResult(ok=True, binary="codex", version="0.171.0")

        async def execute(self, task: AgentTask) -> AgentExecution:
            return AgentExecution(
                execution_id=task.execution_id or str(uuid.uuid4()),
                task_id=task.task_id,
                agent_id="codex",
                status=ExecutionStatus.SUCCEEDED.value,
                completed_at=utcnow(),
                result={"summary": "Done"},
                stdout="Done",
                resource_usage={"input_tokens": 26393, "output_tokens": 2048, "wall_seconds": 12.5},
            )

        async def get_status(self, execution_id: str) -> AgentStatus:
            return AgentStatus(execution_id=execution_id, agent_id="codex", status="SUCCEEDED")

        async def cancel(self, execution_id: str) -> bool:
            return True

    reg = AgentRegistry(repo=repo)
    reg.register(TokenReportingAgent())
    await reg.probe_all()

    gov = GovernanceManager(GovernanceSettings(workspace_root=str(tmp_path)), repo)
    gw = LLMGateway(LLMSettings(base_url="http://mock/v1", model="m1"), BudgetSettings(), repo=repo)
    orch = AgentOrchestrator(reg, gov, repo, max_retries=0, llm_gateway=gw)

    plan = ActionPlan(objective="Write code", assigned_agent="codex", required_capability="coding", permission_level="L1")
    verdict = await gov.validate_plan(plan, "curiosity")
    sub = await orch.submit_plan(plan, verdict)
    final = await orch.wait_for_task(sub["task_id"], max_wait_seconds=5.0, poll_interval=0.02)
    assert final["status"] == "SUCCEEDED"

    # Codex's 26393 + 2048 = 28441 tokens are now tracked in the unified LLM ledger
    llm_usage = await repo.usage_since("llm", day_start())
    assert llm_usage["count"] == 1
    assert int(llm_usage["amount"]) == 28441


def test_static_no_gateway_bypass_and_no_global_settings_in_adapters():
    """Verify no business module bypasses LLMGateway or calls get_settings() inside agent adapters."""
    app_dir = Path(__file__).resolve().parents[2] / "app"

    chat_callers: list[str] = []
    for py in app_dir.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        if "/chat/completions" in text:
            chat_callers.append(str(py.relative_to(app_dir)))
    assert chat_callers == ["cognition/llm_adapter.py"], f"Unexpected direct /chat/completions callers: {chat_callers}"

    for rel in ("agents/codex.py", "agents/hermes.py"):
        text = (app_dir / rel).read_text(encoding="utf-8")
        assert "get_settings(" not in text, f"Global get_settings() still present in app/{rel}"
