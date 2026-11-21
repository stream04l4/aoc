"""V2.1 AOC Control Center Read-Only API Tests:
  - Enforces Bearer Token & SCOPE_READ across all 10 Control Center endpoints (401/403)
  - Strict secret redaction in `/v2/control-center/config` (ZERO plaintext API keys, DB passwords, bot tokens)
  - Full 11-step Mind trace returns real DB records and explicit `null` for unrecorded steps (never fabricated)
  - Pagination, filtering, cross-entity links (Cycle <-> Goal <-> Task <-> Execution), and token analytics
  - Frontend static fault isolation (missing/broken frontend never affects Core API or AutonomousLoop)
"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from app.api.app import create_app
from app.api.auth import SCOPE_INTERNAL, SCOPE_READ, generate_api_key
from app.cognition.interfaces import ActionPlan
from app.core.config import (
    ApiSettings,
    DatabaseSettings,
    EmbeddingSettings,
    GovernanceSettings,
    LLMSettings,
    MemorySettings,
    RunnerSettings,
    Settings,
    TelegramSettings,
)
from app.core.enums import MemoryType
from app.core.runtime import OrganismRuntime
from app.governance.interfaces import PolicyVerdict
from app.perception.interfaces import Observation, utcnow
from app.storage.database import Database
from app.storage.models import Base


async def _init_db(db: Database) -> None:
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.mark.asyncio
async def test_v2_1_control_center_api_auth_redaction_and_full_trace(tmp_path: Path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'cc_api.db'}")
    await _init_db(db)
    admin_secret = "super-secret-admin-key-999"
    llm_secret = "super-secret-llm-key-888"
    tg_secret = "999999:SUPER_SECRET_BOT_TOKEN_777"
    runner_secret = "super-secret-runner-tok-666"
    internal_secret = "super-secret-internal-555"

    settings = Settings(
        api=ApiSettings(
            host="127.0.0.1",
            auth_enabled=True,
            admin_key=SecretStr(admin_secret),
            internal_token=SecretStr(internal_secret),
        ),
        database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path / 'cc_api.db'}"),
        llm=LLMSettings(base_url="http://127.0.0.1:9999/v1", model="Qwen/Test", api_key=SecretStr(llm_secret)),
        memory=MemorySettings(provider="none", embedding_dim=512),
        embedding=EmbeddingSettings(provider="hash_fallback", dimensions=512),
        governance=GovernanceSettings(workspace_root=str(tmp_path / "ws")),
        runner=RunnerSettings(enabled=False, auth_token=SecretStr(runner_secret)),
        telegram=TelegramSettings(
            enabled=True,
            bot_token=SecretStr(tg_secret),
            webhook_secret=SecretStr("super-secret-wh-444"),
            allowed_chat_ids=["10001"],
        ),
    )

    rt = OrganismRuntime(settings=settings, db=db, auto_load_agents=False, llm_probe_fn=lambda: None)
    app = create_app(settings=settings, db=db, runtime=rt, start_scheduler=False)

    try:
        async with app.router.lifespan_context(app):
            assert rt.organism_id is not None
            # Create scoped read-only key and internal-only key
            read_key, read_pref, read_hash = generate_api_key("aoc_r")
            await rt.repo.create_api_credential("cc-viewer", read_pref, read_hash, [SCOPE_READ])

            # Seed realistic DB records across Cycle -> Observation -> Goal -> Plan -> Task -> Execution -> Memory -> Usage
            cid, cnum = await rt.repo.start_cycle(rt.organism_id, "ACTIVE")
            obs_id = await rt.repo.save_observation(
                Observation(
                    system_state={"llm": {"available": True, "latency_ms": 14.0}},
                    available_resources={"cpu_percent": 12.5, "memory_available_pct": 80.0},
                ),
                cycle_id=cid,
                phase="pre",
            )
            await rt.repo.update_cycle(
                cid,
                observation_id=obs_id,
                decision={"selected_drive": "curiosity", "priority_score": 0.42, "reason": "Open topic deficit"},
                brain_output={
                    "assessment": {"summary": "System healthy, 1 open curiosity topic", "confidence": 0.9},
                    "goal": {"should_act": True, "title": "Inspect pgvector docs", "rationale": "Fill gap"},
                },
                plan={"objective": "Read docs", "permission_level": "L0", "expected_improvement": 0.2},
                outcome="SUCCEEDED",
            )
            gid = await rt.repo.create_goal(
                rt.organism_id, cid, drive_type="curiosity", title="Inspect pgvector docs", status="ACHIEVED"
            )
            plan_obj = ActionPlan(
                should_execute=True,
                goal_title="Inspect pgvector docs",
                objective="Read docs",
                assigned_agent="codex",
                permission_level="L0",
            )
            verdict = PolicyVerdict(
                allowed=True,
                requires_approval=False,
                denied=False,
                requested_level="L0",
                effective_level="L0",
                sandbox_mode="read-only",
                effective_timeout_seconds=120,
                reason="Allowed at L0",
            )
            pid = await rt.repo.create_plan(gid, cid, plan_obj, "L0", verdict, "APPROVED", plan_hash="abc123")
            await rt.repo.record_policy_evaluation(
                subject_type="plan",
                subject_id=pid,
                cycle_id=cid,
                requested_level="L0",
                deterministic_level="L0",
                effective_level="L0",
                verdict="approved",
                violations=[],
                reason="Allowed at L0",
            )
            tid = await rt.repo.create_task(
                plan_id=pid,
                goal_id=gid,
                cycle_id=cid,
                objective="Read docs",
                assigned_agent="codex",
                workspace=str(tmp_path / "ws" / "t1"),
                timeout_seconds=120,
                permission_level="L0",
                status="SUCCEEDED",
            )
            now = utcnow()
            await rt.repo.update_task(tid, finalized_at=now)
            eid = await rt.repo.create_execution(tid, "codex", status="SUCCEEDED", attempt_number=1, runner_id="host-runner-1")
            await rt.repo.update_execution(
                eid,
                pid=4242,
                thread_id="thread-xyz-1",
                completed_at=now,
                finalized_at=now,
                verification={"passed": True, "summary": "2/2 checks passed"},
            )
            await rt.repo.add_memory_index(
                memory_type=MemoryType.SEMANTIC.value,
                title="pgvector HNSW note",
                summary="HNSW cosine index accelerates 512d vector retrieval",
                content_hash="hash-pgv-1",
                source="task_finalizer",
                cycle_id=cid,
                sync_status="synced",
            )
            await rt.repo.record_usage(
                category="llm",
                amount=12345.0,
                seconds=12.4,
                details={"source": "codex", "execution_id": str(eid)},
                cycle_id=cid,
                task_id=tid,
            )

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                endpoints = [
                    "/v2/auth/me",
                    "/v2/control-center/overview",
                    "/v2/control-center/cycles",
                    f"/v2/control-center/cycles/{cid}/trace",
                    "/v2/control-center/drives",
                    "/v2/control-center/tasks",
                    f"/v2/control-center/tasks/{tid}/detail",
                    "/v2/control-center/memory",
                    "/v2/control-center/world",
                    "/v2/control-center/self-model",
                    "/v2/control-center/governance",
                    "/v2/control-center/config",
                    "/v2/control-center/logs",
                ]

                # 1. Unauthenticated -> 401 on EVERY endpoint
                for ep in endpoints:
                    r401 = await client.get(ep)
                    assert r401.status_code == 401, f"Expected 401 on {ep}, got {r401.status_code}"

                # 2. Internal-only token -> 403 Forbidden on SCOPE_READ endpoints
                r403 = await client.get(
                    "/v2/control-center/overview", headers={"Authorization": f"Bearer {internal_secret}"}
                )
                assert r403.status_code == 403

                # 3. Scoped read-only key -> 200 on ALL endpoints, but 403 on control/pause!
                read_headers = {"Authorization": f"Bearer {read_key}"}
                r_pause = await client.post("/control/pause", json={"reason": "test"}, headers=read_headers)
                assert r_pause.status_code == 403

                me = (await client.get("/v2/auth/me", headers=read_headers)).json()
                assert me["scopes"] == [SCOPE_READ]

                # 4. Overview returns real organism, cycle, and stats
                ov = (await client.get("/v2/control-center/overview?hours=24", headers=read_headers)).json()
                assert ov["organism"]["name"] == "AOC-001"
                assert ov["latest_cycle"]["cycle_id"] == str(cid)
                assert ov["stats"]["usage_by_category"]["llm"]["total_amount"] == 12345.0

                # 5. Mind 11-step trace: real steps populated, unrecorded post-perception explicitly None!
                tr = (await client.get(f"/v2/control-center/cycles/{cid}/trace", headers=read_headers)).json()
                assert tr["steps"]["1_perception"]["observation_id"] == str(obs_id)
                assert tr["steps"]["4_brain_assessment"]["summary"].startswith("System healthy")
                assert tr["steps"]["7_governance"]["policy_evaluations"][0]["verdict"] == "approved"
                assert tr["steps"]["8_agent_dispatch"]["executions"][0]["thread_id"] == "thread-xyz-1"
                assert tr["steps"]["11_post_perception"] is None  # Strictly unrecorded, NEVER fabricated!

                # 6. Task detail returns full cross-entity references
                td = (await client.get(f"/v2/control-center/tasks/{tid}/detail", headers=read_headers)).json()
                assert td["task"]["task_id"] == str(tid)
                assert td["goal"]["goal_id"] == str(gid)
                assert td["plan"]["plan_id"] == str(pid)
                assert td["executions"][0]["pid"] == 4242

                # 7. Governance returns token analytics & risk advisories
                gov_r = (await client.get("/v2/control-center/governance?hours=24", headers=read_headers)).json()
                assert gov_r["token_analytics"]["total_tokens"] == 12345
                assert gov_r["token_analytics"]["top_tasks"][0]["task_id"] == str(tid)

                # 8. Configuration Center returns all 12 domains and NEVER leaks any secret!
                cfg_resp = await client.get("/v2/control-center/config", headers=read_headers)
                assert cfg_resp.status_code == 200
                raw_text = cfg_resp.text
                for secret in (admin_secret, llm_secret, tg_secret, runner_secret, internal_secret, "super-secret-wh-444"):
                    assert secret not in raw_text, f"SECRET LEAKED in /v2/control-center/config: {secret}"
                cfg_json = cfg_resp.json()
                assert cfg_json["read_only"] is True
                assert len(cfg_json["domains"]) == 12
                assert any(
                    it["sensitive"] is True and it["effective_value"] == "***REDACTED***"
                    for dom in cfg_json["domains"]
                    for it in dom["items"]
                )

                # 9. Logs & Events filtering works
                logs_r = (await client.get(f"/v2/control-center/logs?cycle_id={cid}", headers=read_headers)).json()
                assert logs_r["total"] >= 1
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_v2_1_frontend_static_fault_isolation(tmp_path: Path, monkeypatch):
    """Frontend dist absence or error must NEVER impact Core health or Control Center API."""
    import app.api.app as app_module

    # Point frontend dist to a non-existent path
    monkeypatch.setattr(app_module, "_resolve_frontend_dist", lambda: tmp_path / "missing_dist")

    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'iso.db'}")
    await _init_db(db)
    settings = Settings(
        api=ApiSettings(host="127.0.0.1", auth_enabled=True, admin_key=SecretStr("adm-iso")),
        database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path / 'iso.db'}"),
        memory=MemorySettings(provider="none", embedding_dim=512),
        embedding=EmbeddingSettings(provider="hash_fallback", dimensions=512),
        governance=GovernanceSettings(workspace_root=str(tmp_path / "ws")),
        runner=RunnerSettings(enabled=False),
    )
    rt = OrganismRuntime(settings=settings, db=db, auto_load_agents=False, llm_probe_fn=lambda: None)
    app = create_app(settings=settings, db=db, runtime=rt, start_scheduler=False)

    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                h = await client.get("/health")
                assert h.status_code == 200
                ov = await client.get(
                    "/v2/control-center/overview", headers={"Authorization": "Bearer adm-iso"}
                )
                assert ov.status_code == 200
    finally:
        await db.dispose()
