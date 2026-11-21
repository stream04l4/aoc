"""V3.0 M2 Tests: Upgraded AffectModulator (Idempotency, Decay Replay, Low-IG Boredom,
World Novelty, Safety Isolation & Control Center Drives Fusion).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.affect.interfaces import AffectState
from app.affect.modulator import AffectModulator
from app.api.app import create_app
from app.cognition.interfaces import ActionPlan, VerificationSpec
from app.cognition.preflight import ActionPreflight
from app.core.config import (
    ApiSettings,
    DatabaseSettings,
    EmbeddingSettings,
    GovernanceSettings,
    KernelV3Settings,
    MemorySettings,
    RunnerSettings,
    Settings,
)
from app.core.enums import DriveType, PermissionLevel
from app.core.runtime import OrganismRuntime
from app.drives.engine import DriveEngine
from app.drives.interfaces import DriveState
from app.governance.interfaces import BudgetStatus
from app.governance.policy import GovernanceManager
from app.perception.interfaces import utcnow
from app.storage.database import Database
from app.storage.models import Base
from app.storage.repository import StateRepository


async def _init_db(db: Database) -> None:
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.mark.asyncio
async def test_v3_m2_affect_event_idempotency_and_decay_persistence(tmp_path: Path):
    """Delivering the exact same trigger_event_id multiple times must NEVER double-mutate state or DB rows."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'aff_idem.db'}")
    await _init_db(db)
    repo = StateRepository(db)
    ident = await repo.get_or_create_identity("AOC-V3-M2-IDEM", "3.0.0")

    mod = AffectModulator(
        organism_id=ident["id"],
        repo=repo,
        config={"enabled": True, "maximum_influence": 0.15, "decay_rate": 0.15},
    )

    event_key = f"cycle:{uuid.uuid4()}:drive_arbitration"
    st1, m1 = await mod.update_and_persist(
        recent_execution_statuses=["FAILED", "FAILED"],
        idle_cycles=0,
        trigger_reason="drive_arbitration",
        trigger_event_id=event_key,
    )
    assert st1.frustration > 0.3

    # Replay same trigger_event_id 4 more times (including from a fresh AffectModulator instance simulating restart!)
    for _ in range(2):
        st_dup, m_dup = await mod.update_and_persist(
            recent_execution_statuses=["FAILED", "FAILED"],
            trigger_reason="drive_arbitration",
            trigger_event_id=event_key,
        )
        assert st_dup == st1
        assert m_dup == m1

    mod_restarted = AffectModulator(
        organism_id=ident["id"],
        repo=repo,
        config={"enabled": True, "maximum_influence": 0.15, "decay_rate": 0.15},
    )
    await mod_restarted.load_from_db()
    st_after_restart, _ = await mod_restarted.update_and_persist(
        recent_execution_statuses=["FAILED", "FAILED"],
        trigger_reason="drive_arbitration",
        trigger_event_id=event_key,
    )
    assert st_after_restart == st1

    hist = await repo.list_affect_history(ident["id"])
    assert len(hist) == 1
    assert hist[0]["trigger_event_id"] == event_key
    assert hist[0]["decay_applied"] == pytest.approx(0.15)
    await db.dispose()


def test_v3_m2_affect_deterministic_replay_low_ig_boredom_and_world_novelty():
    """Replay produces identical state; low-IG streak raises boredom -> Curiosity lift; world novelty lifts arousal."""
    mod = AffectModulator(config={"enabled": True, "maximum_influence": 0.15, "decay_rate": 0.15})

    events = [
        {"trigger_event_id": "e1", "low_ig_streak": 2, "idle_cycles": 0},
        {"trigger_event_id": "e1", "low_ig_streak": 2, "idle_cycles": 0},  # duplicate must be ignored in replay!
        {"trigger_event_id": "e2", "low_ig_streak": 3, "idle_cycles": 1},
        {"trigger_event_id": "e3", "world_event_novelty": 0.8},
    ]
    s_a = mod.replay_events(events)
    s_b = mod.replay_events(events)
    assert s_a == s_b

    # Verify low-IG streak drives boredom upward and lifts Curiosity priority within ±0.15
    st_bored, reason = mod.step_state(AffectState(), low_ig_streak=3)
    assert st_bored.boredom >= 0.35
    assert "low_ig_streak=3" in reason
    m_bored = mod.compute_modulation(st_bored)
    assert 0.0 < m_bored.curiosity_priority_delta <= 0.15

    # Verify fresh world novelty lifts arousal & valence and reduces boredom
    st_novel, reason2 = mod.step_state(st_bored, world_event_novelty=1.0)
    assert st_novel.arousal > st_bored.arousal
    assert st_novel.valence > st_bored.valence
    assert st_novel.boredom < st_bored.boredom
    assert "world_novelty=1.00" in reason2


@pytest.mark.asyncio
async def test_v3_m2_affect_never_alters_governance_preflight_or_survival_critical(tmp_path: Path):
    """Hard safety invariant: enabling Affect MUST NEVER change L0-L3 Governance, Preflight, or Survival CRITICAL."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'aff_safety.db'}")
    await _init_db(db)
    repo = StateRepository(db)
    gov = GovernanceManager(GovernanceSettings(workspace_root=str(tmp_path / "ws"), auto_approve_max_level="L1"), repo)
    pf = ActionPreflight()

    # Extreme affect state (max arousal, max confidence, max boredom)
    extreme = AffectModulator(config={"enabled": True, "maximum_influence": 0.15})
    extreme_mod = extreme.compute_modulation(
        AffectState(valence=1.0, arousal=1.0, confidence=1.0, frustration=0.0, boredom=1.0)
    )

    # 1. L3 dangerous plan MUST remain DENIED
    l3_plan = ActionPlan(
        should_execute=True,
        goal_title="Dangerous op",
        objective="Run sudo rm -rf /etc/passwd",
        assigned_agent="codex",
        permission_level=PermissionLevel.L3.value,
    )
    verdict = await gov.validate_plan(l3_plan, DriveType.CURIOSITY.value)
    assert verdict.denied is True
    pf_res = pf.validate(plan=l3_plan, verdict=verdict, budget=BudgetStatus(allowed=True, reason="ok"))
    assert pf_res.decision == "REJECT"

    # 2. L0 read-only write-intent MUST still be REJECTED by ActionPreflight
    l0_write = ActionPlan(
        should_execute=True,
        goal_title="Write in L0",
        objective="Inspect and write to result.md",
        assigned_agent="codex",
        permission_level=PermissionLevel.L0.value,
        verification=[VerificationSpec(kind="file_exists", target="result.md")],
    )
    v_l0 = await gov.validate_plan(l0_write, DriveType.CURIOSITY.value)
    pf_l0 = pf.validate(plan=l0_write, verdict=v_l0, budget=BudgetStatus(allowed=True, reason="ok"))
    assert pf_l0.decision == "REJECT" and "PERMISSION_MISMATCH" in pf_l0.reason_codes

    # 3. Survival CRITICAL emergency MUST ALWAYS override extreme affect
    engine = DriveEngine()
    crit_states = {
        DriveType.SURVIVAL.value: DriveState(drive_type="survival", satisfaction=0.12, urgency=1.0, confidence=1.0),
        DriveType.CURIOSITY.value: DriveState(drive_type="curiosity", satisfaction=0.05, urgency=1.0, confidence=1.0),
        DriveType.REPRODUCTION.value: DriveState(drive_type="reproduction", satisfaction=0.05, urgency=1.0, confidence=1.0),
    }
    dec = engine.arbitrate(crit_states, now=utcnow(), affect_modulation=extreme_mod.model_dump(mode="json"))
    assert dec.selected_drive == DriveType.SURVIVAL.value
    assert dec.constraints.get("emergency_mode") is True
    assert dec.scores["survival"]["affect_delta"] == 0.0
    await db.dispose()


@pytest.mark.asyncio
async def test_v3_m2_runtime_feature_flag_and_control_center_drives_affect_payload(tmp_path: Path):
    """Enabling kernel_v3.affect_enabled runs affect in AutonomousLoop and exposes real history via /v2/control-center/drives."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'aff_cc.db'}")
    await _init_db(db)
    settings = Settings(
        api=ApiSettings(host="127.0.0.1", auth_enabled=True, admin_key=SecretStr("m2-adm")),
        database=DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path / 'aff_cc.db'}"),
        memory=MemorySettings(provider="none", embedding_dim=512),
        embedding=EmbeddingSettings(provider="hash_fallback", dimensions=512),
        governance=GovernanceSettings(workspace_root=str(tmp_path / "ws")),
        runner=RunnerSettings(enabled=False),
        kernel_v3=KernelV3Settings(affect_enabled=True),
    )

    async def _healthy_probe() -> dict[str, Any]:
        return {"available": True, "latency_ms": 4.0}

    rt = OrganismRuntime(settings=settings, db=db, auto_load_agents=False, llm_probe_fn=_healthy_probe)
    app = create_app(settings=settings, db=db, runtime=rt, start_scheduler=False)

    try:
        async with app.router.lifespan_context(app):
            assert rt.affect is not None and rt.affect.enabled is True
            # Run 2 cycles cleanly -> should persist 2 distinct idempotent affect_history rows
            c1 = await rt.loop.run_cycle()
            c2 = await rt.loop.run_cycle()
            assert c1["outcome"] != "ERROR"
            assert c2["outcome"] != "ERROR"

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.get(
                    "/v2/control-center/drives?hours=24",
                    headers={"Authorization": "Bearer m2-adm"},
                )
                assert r.status_code == 200
                body = r.json()
                assert body["affect"]["enabled"] is True
                assert body["affect"]["current_state"] is not None
                assert len(body["affect"]["history"]) == 2
                assert body["affect"]["history"][0]["trigger_event_id"].startswith("cycle:")
                assert body["affect"]["history"][0]["decay_applied"] is not None
    finally:
        await db.dispose()
