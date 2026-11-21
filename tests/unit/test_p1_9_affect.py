"""P1-9 unit, integration comparison, fault-injection, and Alembic 0008 tests for optional Affect layer."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, inspect

from app.affect.interfaces import AffectConfig, AffectModulation, AffectState
from app.affect.modulator import AffectModulator
from app.api.app import create_app
from app.core.config import (
    ApiSettings,
    GovernanceSettings,
    MemorySettings,
    PerceptionSettings,
    Settings,
    get_affect_config,
)
from app.core.enums import DriveType
from app.core.runtime import OrganismRuntime
from app.drives.engine import DriveEngine
from app.drives.interfaces import DriveState
from app.perception.interfaces import utcnow
from app.storage.database import Database
from app.storage.repository import StateRepository

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_affect_default_config_is_disabled_and_zero_impact():
    yaml_cfg = get_affect_config()
    assert yaml_cfg.get("enabled") is False
    assert float(yaml_cfg.get("maximum_influence", 0.15)) <= 0.15

    # Even if someone misconfigures maximum_influence=0.99, hard ceiling clamps to 0.15
    cfg_clamped = AffectConfig(enabled=True, maximum_influence=0.99)
    assert cfg_clamped.maximum_influence == 0.15

    mod_off = AffectModulator(config=yaml_cfg)
    assert mod_off.enabled is False
    out = mod_off.compute_modulation()
    assert out.enabled is False
    assert out.curiosity_priority_delta == 0.0
    assert out.reproduction_priority_delta == 0.0
    assert out.retry_backoff_multiplier == 1.0
    assert out.reflection_interval_factor == 1.0


@pytest.mark.asyncio
async def test_affect_disabled_writes_zero_history_and_identical_arbitration(db: Database):
    repo = StateRepository(db)
    ident = await repo.get_or_create_identity("AOC-P1-9-OFF", "2.0.0")
    mod = AffectModulator(organism_id=ident["id"], repo=repo, config={"enabled": False})

    res = await mod.evaluate_safe(
        recent_execution_statuses=["FAILED", "FAILED", "FAILED"],
        idle_cycles=5,
    )
    assert res.enabled is False
    assert await repo.list_affect_history(ident["id"]) == []

    engine = DriveEngine()
    states = {
        DriveType.SURVIVAL.value: DriveState(drive_type="survival", satisfaction=0.95, urgency=0.1, confidence=1.0),
        DriveType.CURIOSITY.value: DriveState(drive_type="curiosity", satisfaction=0.45, urgency=0.6, confidence=0.9),
        DriveType.REPRODUCTION.value: DriveState(drive_type="reproduction", satisfaction=0.55, urgency=0.5, confidence=0.9),
    }
    d_plain = engine.arbitrate(states, now=utcnow())
    d_off = engine.arbitrate(states, now=utcnow(), affect_modulation=res.model_dump(mode="json"))
    assert d_plain.selected_drive == d_off.selected_drive
    assert d_plain.priority_score == d_off.priority_score
    assert "affect_modulation" not in d_off.constraints


@pytest.mark.asyncio
async def test_affect_enabled_strict_bounds_frustration_backoff_and_safety_isolation(db: Database):
    repo = StateRepository(db)
    ident = await repo.get_or_create_identity("AOC-P1-9-ON", "2.0.0")
    mod = AffectModulator(
        organism_id=ident["id"],
        repo=repo,
        config={"enabled": True, "maximum_influence": 0.15, "decay_rate": 0.10},
    )

    # 1. Consecutive failures raise frustration and extend retry backoff strictly within <= 1.15
    prev_frust = 0.0
    for i in range(4):
        st, m = await mod.update_and_persist(
            recent_execution_statuses=["FAILED", "FAILED", "FAILED"],
            idle_cycles=0,
            trigger_reason=f"fail_step_{i}",
        )
        assert -1.0 <= st.valence <= 1.0
        for dim in (st.arousal, st.confidence, st.frustration, st.boredom):
            assert 0.0 <= dim <= 1.0
        assert st.frustration >= prev_frust
        prev_frust = st.frustration
        assert abs(m.curiosity_priority_delta) <= 0.15
        assert abs(m.reproduction_priority_delta) <= 0.15
        assert 1.0 <= m.retry_backoff_multiplier <= 1.15
        assert 0.85 <= m.reflection_interval_factor <= 1.15

    assert prev_frust > 0.5
    assert m.retry_backoff_multiplier > 1.05

    # History & organism_state.emotion_reserved persisted
    hist = await repo.list_affect_history(ident["id"])
    assert len(hist) == 4
    org_st = await repo.get_organism_state(ident["id"])
    assert org_st is not None and org_st["emotion_reserved"]["frustration"] == st.frustration

    # 2. Hard safety isolation: Affect NEVER overrides Survival CRITICAL emergency!
    engine = DriveEngine()
    critical_states = {
        DriveType.SURVIVAL.value: DriveState(drive_type="survival", satisfaction=0.10, urgency=1.0, confidence=1.0),
        DriveType.CURIOSITY.value: DriveState(drive_type="curiosity", satisfaction=0.20, urgency=0.9, confidence=1.0),
        DriveType.REPRODUCTION.value: DriveState(drive_type="reproduction", satisfaction=0.20, urgency=0.9, confidence=1.0),
    }
    d_crit = engine.arbitrate(critical_states, now=utcnow(), affect_modulation=m.model_dump(mode="json"))
    assert d_crit.selected_drive == DriveType.SURVIVAL.value
    assert d_crit.constraints.get("emergency_mode") is True
    assert d_crit.scores["survival"]["affect_delta"] == 0.0

    # 3. Contrast enabled vs disabled arbitration logs in normal exploration scenario
    norm_states = {
        DriveType.SURVIVAL.value: DriveState(drive_type="survival", satisfaction=0.95, urgency=0.1, confidence=1.0),
        DriveType.CURIOSITY.value: DriveState(drive_type="curiosity", satisfaction=0.40, urgency=0.65, confidence=0.95),
        DriveType.REPRODUCTION.value: DriveState(drive_type="reproduction", satisfaction=0.60, urgency=0.40, confidence=0.95),
    }
    d_no_affect = engine.arbitrate(norm_states, now=utcnow(), affect_modulation=None)
    d_with_affect = engine.arbitrate(norm_states, now=utcnow(), affect_modulation=m.model_dump(mode="json"))
    assert "affect_modulation" in d_with_affect.constraints
    delta_actual = d_with_affect.scores["curiosity"]["priority"] - d_no_affect.scores["curiosity"]["priority"]
    assert abs(delta_actual) <= 0.15 + 1e-6


@pytest.mark.asyncio
async def test_affect_fault_injection_degrades_silently_to_v1_and_lifecycle_api(db: Database, tmp_path: Path):
    admin_token = "p1-9-admin-token"
    settings = Settings(
        api=ApiSettings(host="127.0.0.1", auth_enabled=True, admin_key=admin_token),
        governance=GovernanceSettings(workspace_root=str(tmp_path)),
        memory=MemorySettings(provider="none"),
        perception=PerceptionSettings(llm_probe=False),
    )
    rt = OrganismRuntime(settings=settings, db=db, auto_load_agents=False)
    app = create_app(settings=settings, runtime=rt, start_scheduler=False)

    async with app.router.lifespan_context(app):
        # Default runtime has Affect disabled
        assert rt.affect is not None and rt.affect.enabled is False

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/v2/lifecycle")).status_code == 401
            r = await client.get("/v2/lifecycle", headers={"Authorization": f"Bearer {admin_token}"})
            assert r.status_code == 200
            body = r.json()
            assert body["affect_enabled"] is False
            assert body["affect"]["affect_enabled"] is False

        # Fault injection: enable Affect & inject an internal crash inside update_and_persist ->
        # evaluate_safe() and AutonomousLoop MUST degrade silently to V1.0 policy with zero failure!
        broken = AffectModulator(organism_id=rt.organism_id, repo=rt.repo, config={"enabled": True})

        async def _boom(**_kwargs):
            raise RuntimeError("injected affect internal crash")

        broken.update_and_persist = _boom  # type: ignore[method-assign]
        rt.affect = broken
        rt.loop.affect = broken

        degraded = await broken.evaluate_safe(recent_execution_statuses=["FAILED"])
        assert degraded.enabled is False
        assert "fault_degraded_v1" in degraded.reason

        # Run a full cycle with the crashing AffectModulator attached -> MUST succeed cleanly without raising!
        rec = await rt.loop.run_cycle()
        assert rec["outcome"] != "ERROR"


def test_alembic_0008_upgrade_and_downgrade_roundtrip(tmp_path: Path):
    db_file = tmp_path / "alembic_0008.db"
    sync_url = f"sqlite:///{db_file}"
    env = os.environ.copy()
    env["AOC_DATABASE__URL"] = f"sqlite+aiosqlite:///{db_file}"

    def run_alembic(*args: str) -> None:
        res = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=str(PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, f"alembic {args} failed:\n{res.stdout}\n{res.stderr}"

    run_alembic("upgrade", "head")
    eng = create_engine(sync_url)
    assert "affect_history" in set(inspect(eng).get_table_names())

    run_alembic("downgrade", "0007_p1_7_self_model")
    assert "affect_history" not in set(inspect(eng).get_table_names())

    run_alembic("upgrade", "head")
    assert "affect_history" in set(inspect(eng).get_table_names())
    eng.dispose()
