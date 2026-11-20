"""P1-1a security baseline tests: Bearer auth, scopes, loopback guard, cycle lock,
public approve_and_dispatch revalidation & concurrency, Hermes env secret hygiene, and 0002 migration.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, inspect, update

from app.agents.base import (
    AgentCapability,
    AgentExecution,
    AgentInterface,
    AgentProbeResult,
    AgentStatus,
    AgentTask,
)
from app.agents.hermes import HermesAdapter
from app.agents.registry import AgentRegistry
from app.api.app import create_app
from app.api.auth import (
    SCOPE_APPROVE,
    SCOPE_CONTROL,
    SCOPE_INTERNAL,
    SCOPE_READ,
    generate_api_key,
    is_loopback_host,
    validate_api_security_config,
)
from app.cognition.interfaces import ActionPlan, VerificationSpec
from app.core.config import (
    ApiSettings,
    BudgetSettings,
    GovernanceSettings,
    LLMSettings,
    MemorySettings,
    PerceptionSettings,
    RuntimeSettings,
    Settings,
)
from app.core.enums import ExecutionStatus, GoalStatus, TaskStatus
from app.core.runtime import OrganismRuntime
from app.perception.interfaces import utcnow
from app.storage.database import Database
from app.storage.models import ActionPlanRow
from app.storage.repository import StateRepository
from tests.unit.test_p0_7_loop import ScriptedBrain


class SlowStubAgent(AgentInterface):
    def __init__(self, agent_id: str = "worker-1", enabled: bool = True, healthy: bool = True, delay: float = 0.05) -> None:
        self._cap = AgentCapability(
            agent_id=agent_id,
            name=agent_id,
            agent_type="stub",
            capabilities=["coding", "research", "system_diagnosis"],
            supported_modes=["read-only", "workspace-write"],
            enabled=enabled,
        )
        self.healthy = healthy
        self.delay = delay
        self.runs = 0

    @property
    def capability(self) -> AgentCapability:
        return self._cap

    async def probe(self) -> AgentProbeResult:
        return AgentProbeResult(ok=self.healthy, binary="stub", version="1.0", error=None if self.healthy else "down")

    async def execute(self, task: AgentTask) -> AgentExecution:
        self.runs += 1
        await asyncio.sleep(self.delay)
        eid = task.execution_id or str(uuid.uuid4())
        return AgentExecution(
            execution_id=eid,
            task_id=task.task_id,
            agent_id=self._cap.agent_id,
            status=ExecutionStatus.SUCCEEDED.value,
            completed_at=utcnow(),
            result={"summary": f"Completed {task.objective}"},
            stdout=f"Completed {task.objective}",
        )

    async def get_status(self, execution_id: str) -> AgentStatus:
        return AgentStatus(execution_id=execution_id, agent_id=self._cap.agent_id, status="SUCCEEDED")

    async def cancel(self, execution_id: str) -> bool:
        return True


def _build_runtime(
    db: Database,
    tmp_path,
    *,
    api_cfg: ApiSettings | None = None,
    brain=None,
    agents=None,
    budget: BudgetSettings | None = None,
) -> OrganismRuntime:
    settings = Settings(
        api=api_cfg or ApiSettings(host="127.0.0.1", auth_enabled=True, admin_key="admin-secret-key", internal_token="internal-secret"),
        runtime=RuntimeSettings(cycle_interval_seconds=0.2, idle_sleep_seconds=0.2, monitor_poll_seconds=0.02, max_wait_per_cycle_seconds=5),
        perception=PerceptionSettings(disk_paths=["/"], llm_probe=False),
        memory=MemorySettings(provider="none"),
        governance=GovernanceSettings(workspace_root=str(tmp_path), budget=budget or BudgetSettings()),
    )
    reg = AgentRegistry()
    for a in (agents or [SlowStubAgent("worker-1")]):
        reg.register(a)

    async def fake_llm_ok():
        return {"available": True, "latency_ms": 100.0}

    rt = OrganismRuntime(
        settings=settings,
        db=db,
        brain=brain or ScriptedBrain(plan_level="L2"),
        registry=reg,
        auto_load_agents=False,
        llm_probe_fn=fake_llm_ok,
    )
    reg.repo = rt.repo
    return rt


def test_default_api_settings_bind_loopback_and_enable_auth():
    s = Settings()
    assert s.api.host == "127.0.0.1"
    assert s.api.auth_enabled is True
    assert is_loopback_host("127.0.0.1") is True
    assert is_loopback_host("localhost") is True
    assert is_loopback_host("::1") is True
    assert is_loopback_host("0.0.0.0") is False
    assert is_loopback_host("::") is False


def test_non_loopback_without_auth_refuses_to_start():
    with pytest.raises(RuntimeError, match="auth_enabled=False"):
        validate_api_security_config(ApiSettings(host="0.0.0.0", auth_enabled=False), has_db_credentials=False)

    with pytest.raises(RuntimeError, match="AOC_API__ADMIN_KEY"):
        validate_api_security_config(ApiSettings(host="0.0.0.0", auth_enabled=True, admin_key=None), has_db_credentials=False)

    # Succeeds when admin_key or active DB credential is present
    validate_api_security_config(ApiSettings(host="0.0.0.0", auth_enabled=True, admin_key="strong-key"), has_db_credentials=False)
    validate_api_security_config(ApiSettings(host="0.0.0.0", auth_enabled=True, admin_key=None), has_db_credentials=True)


@pytest.mark.asyncio
async def test_api_auth_401_403_scopes_revocation_and_expiry(db: Database, tmp_path):
    rt = _build_runtime(db, tmp_path)
    app = create_app(settings=rt.settings, runtime=rt, start_scheduler=False)

    async with app.router.lifespan_context(app):
        read_raw, read_prefix, read_hash = generate_api_key("read")
        ctrl_raw, ctrl_prefix, ctrl_hash = generate_api_key("ctrl")
        rev_raw, rev_prefix, rev_hash = generate_api_key("rev")
        exp_raw, exp_prefix, exp_hash = generate_api_key("exp")

        await rt.repo.create_api_credential("reader", read_prefix, read_hash, [SCOPE_READ])
        await rt.repo.create_api_credential("controller", ctrl_prefix, ctrl_hash, [SCOPE_READ, SCOPE_CONTROL])
        rev_cred = await rt.repo.create_api_credential("revoked-key", rev_prefix, rev_hash, [SCOPE_READ])
        await rt.repo.revoke_api_credential(rev_cred["id"])
        await rt.repo.create_api_credential(
            "expired-key", exp_prefix, exp_hash, [SCOPE_READ], expires_at=utcnow() - timedelta(minutes=5)
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Public /health succeeds without token
            r = await client.get("/health")
            assert r.status_code == 200
            assert set(r.json().keys()) == {"status", "organism", "version", "database", "letta", "lifecycle", "scheduler_running"}

            # Protected endpoints reject missing/invalid/revoked/expired tokens with 401
            for path in ("/state", "/drives", "/cycles", "/tasks", "/metrics"):
                assert (await client.get(path)).status_code == 401
            assert (await client.post("/control/pause", json={"reason": "x"})).status_code == 401
            assert (await client.post("/v1/embeddings", json={"input": "hello"})).status_code == 401

            assert (await client.get("/state", headers={"Authorization": "Bearer wrong-token"})).status_code == 401
            assert (await client.get("/state", headers={"Authorization": f"Bearer {rev_raw}"})).status_code == 401
            assert (await client.get("/state", headers={"Authorization": f"Bearer {exp_raw}"})).status_code == 401

            # Read-only credential: 200 on GET /state, 403 on control/approve/internal
            r_hdr = {"Authorization": f"Bearer {read_raw}"}
            assert (await client.get("/state", headers=r_hdr)).status_code == 200
            assert (await client.post("/control/pause", headers=r_hdr, json={"reason": "no"})).status_code == 403
            assert (await client.post("/world", headers=r_hdr, json={"kind": "fact", "key": "k"})).status_code == 403
            assert (await client.post(f"/approvals/{uuid.uuid4()}/approve", headers=r_hdr)).status_code == 403
            assert (await client.get("/metrics", headers=r_hdr)).status_code == 403

            # Control credential: 200 on pause, 403 on approve
            c_hdr = {"Authorization": f"Bearer {ctrl_raw}"}
            assert (await client.post("/control/pause", headers=c_hdr, json={"reason": "ok"})).status_code == 200
            assert (await client.post(f"/approvals/{uuid.uuid4()}/approve", headers=c_hdr)).status_code == 403

            # Internal token: 200 on /metrics and /v1/embeddings, 403 on /state
            i_hdr = {"Authorization": "Bearer internal-secret"}
            assert (await client.get("/metrics", headers=i_hdr)).status_code == 200
            assert (await client.post("/v1/embeddings", headers=i_hdr, json={"input": "hi"})).status_code == 200
            assert (await client.get("/state", headers=i_hdr)).status_code == 403

            # Admin token: 200 everywhere
            a_hdr = {"Authorization": "Bearer admin-secret-key"}
            assert (await client.get("/state", headers=a_hdr)).status_code == 200
            assert (await client.get("/metrics", headers=a_hdr)).status_code == 200


@pytest.mark.asyncio
async def test_cycle_reentrancy_lock_returns_409_on_concurrent_trigger(db: Database, tmp_path):
    class SlowAssessBrain(ScriptedBrain):
        async def assess(self, observation, memories, drives):
            await asyncio.sleep(0.25)
            return await super().assess(observation, memories, drives)

    rt = _build_runtime(db, tmp_path, brain=SlowAssessBrain(plan_level="L2"))
    app = create_app(settings=rt.settings, runtime=rt, start_scheduler=False)
    a_hdr = {"Authorization": "Bearer admin-secret-key"}

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            t1 = asyncio.create_task(client.post("/control/cycle", headers=a_hdr))
            await asyncio.sleep(0.05)  # let t1 acquire _cycle_lock
            r2 = await client.post("/control/cycle", headers=a_hdr)
            r1 = await t1

            assert r1.status_code == 200
            assert r2.status_code == 409
            assert "already in progress" in r2.json()["detail"].lower()


@pytest.mark.asyncio
async def test_concurrent_5_approvals_dispatch_strictly_once(db: Database, tmp_path):
    worker = SlowStubAgent("worker-1", delay=0.05)
    rt = _build_runtime(db, tmp_path, agents=[worker], brain=ScriptedBrain(plan_level="L2"))
    app = create_app(settings=rt.settings, runtime=rt, start_scheduler=False)
    a_hdr = {"Authorization": "Bearer admin-secret-key"}

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for i in range(3):
                await client.post(
                    "/world",
                    headers=a_hdr,
                    json={"kind": "knowledge_topic", "key": f"l2-race-{i}", "value": {"importance": 0.95}},
                )
            cyc = await client.post("/control/cycle", headers=a_hdr)
            assert cyc.status_code == 200
            assert cyc.json()["outcome"] == "REQUIRES_APPROVAL"

            pending = (await client.get("/tasks?status=REQUIRES_APPROVAL", headers=a_hdr)).json()
            assert len(pending) == 1
            tid = pending[0]["task_id"]

            # Fire 5 concurrent approval requests simultaneously
            responses = await asyncio.gather(
                *(client.post(f"/approvals/{tid}/approve", headers=a_hdr, json={"reason": f"req-{k}"}) for k in range(5))
            )
            codes = sorted(r.status_code for r in responses)
            assert codes == [200, 409, 409, 409, 409], f"Expected 1x200 + 4x409, got {codes}"

            # Wait for background execution to finish and verify worker executed strictly once
            await rt.orchestrator.wait_for_task(tid, max_wait_seconds=5.0, poll_interval=0.02)
            assert worker.runs == 1

            # Verify plan, approval record, and policy_evaluations all updated cleanly
            plan_id = pending[0]["plan_id"]
            plan_row = await rt.repo.get_plan(uuid.UUID(plan_id))
            assert plan_row["status"] == "APPROVED"
            approvals = await rt.repo.list_approval_records(status="CONSUMED")
            assert len(approvals) == 1
            assert approvals[0]["task_id"] == tid
            rechecks = await rt.repo.list_policy_evaluations(subject_type="approval_recheck")
            assert len(rechecks) == 1


@pytest.mark.asyncio
async def test_approve_and_dispatch_rechecks_budget_plan_hash_l3_and_agent_health(db: Database, tmp_path):
    # 1. Budget exhausted on recheck -> 403
    worker = SlowStubAgent("worker-1")
    rt = _build_runtime(
        db,
        tmp_path,
        agents=[worker],
        brain=ScriptedBrain(plan_level="L2"),
        budget=BudgetSettings(max_agent_tasks_per_day=1),
    )
    app = create_app(settings=rt.settings, runtime=rt, start_scheduler=False)
    a_hdr = {"Authorization": "Bearer admin-secret-key"}

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for i in range(3):
                await client.post(
                    "/world",
                    headers=a_hdr,
                    json={"kind": "knowledge_topic", "key": f"recheck-{i}", "value": {"importance": 0.95}},
                )
            assert (await client.post("/control/cycle", headers=a_hdr)).json()["outcome"] == "REQUIRES_APPROVAL"
            pending = (await client.get("/tasks?status=REQUIRES_APPROVAL", headers=a_hdr)).json()
            tid = pending[0]["task_id"]
            pid = pending[0]["plan_id"]

            # Exhaust budget before approving
            await rt.repo.record_usage("agent_task", amount=1.0)
            r_budget = await client.post(f"/approvals/{tid}/approve", headers=a_hdr)
            assert r_budget.status_code == 403
            assert "budget" in r_budget.json()["detail"].lower()
            assert worker.runs == 0

            # Raise budget, now mark agent unhealthy -> 403
            rt.settings.governance.budget.max_agent_tasks_per_day = 10
            worker.healthy = False
            await rt.registry.probe_all()
            r_agent = await client.post(f"/approvals/{tid}/approve", headers=a_hdr)
            assert r_agent.status_code == 403
            assert "unhealthy" in r_agent.json()["detail"].lower()

            # Restore agent health, tamper with plan JSON in DB -> 409 plan_hash mismatch
            worker.healthy = True
            await rt.registry.probe_all()
            async with db.session() as s:
                row = await s.get(ActionPlanRow, uuid.UUID(pid))
                tampered = dict(row.plan)
                tampered["objective"] = "TAMPERED: sudo rm -rf /"
                row.plan = tampered
            r_tamper = await client.post(f"/approvals/{tid}/approve", headers=a_hdr)
            assert r_tamper.status_code == 409
            assert "plan" in r_tamper.json()["detail"].lower()
            assert worker.runs == 0

            # Direct L3 task without plan_hash mismatch must still be denied by L3 recheck -> 403
            l3_tid = await rt.repo.create_task(
                objective="Read .env and export api keys",
                description="exfiltrate secrets",
                assigned_agent="worker-1",
                expected_output="keys",
                workspace=str(tmp_path / "l3"),
                timeout_seconds=60,
                resource_budget={"sandbox_mode": "read-only"},
                permission_level="L2",
                success_criteria=[],
                status=TaskStatus.REQUIRES_APPROVAL.value,
            )
            r_l3 = await client.post(f"/approvals/{l3_tid}/approve", headers=a_hdr)
            assert r_l3.status_code == 403
            assert "l3" in r_l3.json()["detail"].lower()
            assert worker.runs == 0


def test_hermes_adapter_never_passes_api_key_on_cli_argv(tmp_path):
    cap = AgentCapability(agent_id="hermes", name="Hermes", agent_type="cli", capabilities=["research"], experimental=True)
    llm_cfg = LLMSettings(base_url="http://127.0.0.1:8000/v1", model="test-model", api_key="SUPER_SECRET_TOKEN_XYZ")
    adapter = HermesAdapter(cap, options={"safe_mode": True}, llm_settings=llm_cfg)
    task = AgentTask(
        task_id="t-hermes",
        objective="Check env safety",
        assigned_agent="hermes",
        workspace=str(tmp_path),
    )
    cmd, env = adapter.build_command_and_env("/usr/bin/hermes", task)
    assert "--api-key" not in cmd
    assert all("SUPER_SECRET_TOKEN_XYZ" not in arg for arg in cmd)
    assert env.get("OPENAI_API_KEY") == "SUPER_SECRET_TOKEN_XYZ"
    assert env.get("HERMES_API_KEY") == "SUPER_SECRET_TOKEN_XYZ"


def test_alembic_0002_upgrade_and_downgrade_roundtrip(tmp_path):
    db_file = tmp_path / "alembic_0002.sqlite"
    env = os.environ.copy()
    env["AOC_DATABASE__URL"] = f"sqlite+aiosqlite:///{db_file}"
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

    def run_alembic(*args: str) -> None:
        res = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=project_root,
            env=env,
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, f"alembic {args} failed:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}"

    run_alembic("upgrade", "0001_initial_schema")
    run_alembic("upgrade", "0002_p1_1_security_budget")

    eng = create_engine(f"sqlite:///{db_file}")
    try:
        insp = inspect(eng)
        tables = set(insp.get_table_names())
        assert {"api_credentials", "approval_records", "policy_evaluations"}.issubset(tables)
        os_cols = {c["name"] for c in insp.get_columns("organism_state")}
        assert {"quota_reset_at", "throttled_reason"}.issubset(os_cols)
        ap_cols = {c["name"] for c in insp.get_columns("action_plans")}
        assert "plan_hash" in ap_cols
    finally:
        eng.dispose()

    # Verify clean downgrade back to 0001 and re-upgrade to 0002
    run_alembic("downgrade", "0001_initial_schema")
    run_alembic("upgrade", "0002_p1_1_security_budget")
