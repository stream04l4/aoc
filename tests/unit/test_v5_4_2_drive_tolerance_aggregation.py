"""V5.4.2 tests: Non-compensatory series tolerance gate + Softmin drive aggregation (strict 0% lethal floor)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core.enums import DriveType, ExecutionStatus
from app.drives.engine import DriveEngine
from app.drives.interfaces import DriveContext
from app.drives.scoring import aggregate_dimensions, tolerance_gate
from app.evolution.constitution_guard import ConstitutionGuard
from app.perception.interfaces import Observation

PROJECT_ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 11, 28, 13, 20, 0, tzinfo=timezone.utc)


def _healthy_obs(*, disk_free: float = 0.65, mem_avail: float = 0.75, llm_ok: bool = True, db_ok: bool = True) -> Observation:
    return Observation(
        timestamp=T0,
        system_state={
            "disk": {"free_ratio": disk_free, "paths": {"/": {"free_ratio": disk_free}}},
            "memory": {"available_ratio": mem_avail},
            "cpu": {"load_per_core": 0.25},
            "llm": {"available": llm_ok, "latency_ms": 120.0} if llm_ok else {"available": False, "error": "down"},
            "database": {"ok": db_ok},
            "services": {"postgres": {"ok": db_ok}},
        },
        environment_state={"unknown_topics": []},
        available_resources={
            "agents": {"enabled_count": 2, "healthy_count": 2, "capabilities": ["coding", "research"]},
            "budget": {"tasks_used": 1, "tasks_limit": 100, "tokens_used": 1000, "tokens_limit": 1_000_000},
        },
        confidence=1.0,
    )


def _healthy_ctx(obs: Observation, **overrides) -> DriveContext:
    caps = ["coding", "research", "repository_analysis", "system_diagnosis", "knowledge_summarization"]
    base = dict(
        now=T0,
        observation=obs,
        recent_execution_statuses=[ExecutionStatus.SUCCEEDED.value] * 5,
        budget={"tasks_used": 1, "tasks_limit": 100, "tokens_used": 1000, "tokens_limit": 1_000_000},
        capabilities_acquired=caps,
        validated_capabilities=caps,
        validated_skill_count=12,
        procedural_skill_count=12,
        capability_goals_completed_recent=5,
        memory_available=True,
        identity_persisted=True,
        knowledge_transfer_readiness=1.0,
        open_knowledge_gaps=[],
        unknown_topics=[],
        exploration_attempted_recent=4,
        exploration_succeeded_recent=4,
        new_knowledge_recent=5,
        last_learning_at=T0 - timedelta(minutes=5),
        last_information_gain=0.9,
        last_information_gain_at=T0 - timedelta(minutes=5),
    )
    base.update(overrides)
    return DriveContext(**base)


def test_v5_4_2_tolerance_gate_and_aggregate_dimensions_math():
    """Verify safe-zone weighted compensation, bottleneck nonlinear drop, 0.0 lethal floor, and None exclusion."""
    # Safe zone: gate is strictly 1.0
    assert tolerance_gate(0.85, threshold=0.80, lethal=0.0, gamma=1.3, floor=0.0) == 1.0
    # Warning zone: nonlinear drop between lethal and threshold
    g_warn = tolerance_gate(0.40, threshold=0.80, lethal=0.0, gamma=1.3, floor=0.0)
    assert pytest.approx(g_warn, abs=1e-6) == 0.5**1.3
    # Lethal floor (both zero and non-zero floor)
    assert tolerance_gate(0.0, threshold=0.80, lethal=0.0, gamma=1.3, floor=0.0) == 0.0
    assert tolerance_gate(0.0, threshold=0.50, lethal=0.0, gamma=1.2, floor=0.25) == 0.25

    gates = {"a": {"threshold": 0.6, "lethal": 0.0, "gamma": 1.5, "floor": 0.0}}
    weights = {"a": 0.5, "b": 0.5, "missing": 0.5}

    # Above gate threshold + missing dim -> pure weighted mean of available dims, lower confidence
    sat_safe, conf_safe, notes_safe = aggregate_dimensions(
        {"a": 0.8, "b": 1.0, "missing": None},
        weights,
        softmin_beta=0.0,
        gates=gates,
    )
    assert pytest.approx(sat_safe, abs=1e-6) == 0.90
    assert pytest.approx(conf_safe, abs=1e-6) == pytest.approx(2.0 / 3.0, abs=1e-6)
    assert any("dimension 'missing' unavailable" in n for n in notes_safe)

    # Bottleneck dim drops nonlinearly below gate
    sat_warn, _, notes_warn = aggregate_dimensions(
        {"a": 0.3, "b": 1.0, "missing": 1.0},
        {"a": 0.33, "b": 0.33, "missing": 0.34},
        softmin_beta=1.0,
        gates=gates,
    )
    assert 0.15 < sat_warn < 0.35
    assert any("bottleneck gate 'a'" in n for n in notes_warn)

    # Strict 0.0 lethal collapses composite score to 0.0 even when other dims are 1.0
    sat_dead, _, _ = aggregate_dimensions(
        {"a": 0.0, "b": 1.0},
        {"a": 0.5, "b": 0.5},
        softmin_beta=1.2,
        gates=gates,
    )
    assert sat_dead == 0.0


def test_v5_4_2_survival_zero_percent_lethal_and_warning_gradient():
    """Survival flags 25%/20% free disk clearly without false death, and hits 0.0 ONLY at 0% / core outage."""
    engine = DriveEngine()

    st_ok = engine.compute_states(_healthy_ctx(_healthy_obs(disk_free=0.40)), {})["survival"]
    assert st_ok.satisfaction == 1.0

    # 25% free (75% used): noticeable concern without false death/emergency
    st_75 = engine.compute_states(_healthy_ctx(_healthy_obs(disk_free=0.25)), {})["survival"]
    assert 0.55 < st_75.satisfaction < 0.90
    assert st_75.urgency < 1.0
    assert any("disk free ratio low" in n for n in st_75.notes)

    # 20% free (80% used): heavy bottleneck drag, still alive (> 0.0)
    st_80 = engine.compute_states(_healthy_ctx(_healthy_obs(disk_free=0.20)), {})["survival"]
    assert 0.30 < st_80.satisfaction < 0.65
    assert st_80.satisfaction < st_75.satisfaction

    # 0% free disk / 0% free memory / LLM outage / DB outage: strict 0.0 death & urgency=1.0
    for obs in (
        _healthy_obs(disk_free=0.00),
        _healthy_obs(mem_avail=0.00),
        _healthy_obs(llm_ok=False),
        _healthy_obs(db_ok=False),
    ):
        st_dead = engine.compute_states(_healthy_ctx(obs), {})["survival"]
        assert st_dead.satisfaction == 0.0
        assert st_dead.urgency == 1.0

    # Economic budget exhaustion remains decoupled from survival lethality/crisis
    st_no_budget = engine.compute_states(
        _healthy_ctx(
            _healthy_obs(disk_free=0.65),
            budget={"tasks_used": 100, "tasks_limit": 100, "tokens_used": 1_000_000, "tokens_limit": 1_000_000},
        ),
        {},
    )["survival"]
    assert st_no_budget.dimensions["budget"] == 0.0
    assert st_no_budget.satisfaction > 0.85
    assert st_no_budget.urgency < 0.60


def test_v5_4_2_reproduction_bottleneck_prevents_inflation():
    """Quality mode reflects low skill reuse (0.33) honestly instead of inflating to ~0.69."""
    engine = DriveEngine()
    obs = _healthy_obs()

    poor_ctx = _healthy_ctx(
        obs,
        reproduction_quality_scoring_enabled=True,
        skill_reuse_ratio_recent=0.3333,
        execution_reliability_recent=0.78,
        playbook_freshness_score=0.95,
        pending_skill_refactor_count=6,
    )
    st_poor = engine.compute_states(poor_ctx, {})[DriveType.REPRODUCTION.value]
    assert st_poor.satisfaction < 0.55

    good_ctx = poor_ctx.model_copy(
        update={
            "skill_reuse_ratio_recent": 0.75,
            "execution_reliability_recent": 0.98,
            "pending_skill_refactor_count": 0,
        }
    )
    st_good = engine.compute_states(good_ctx, {})[DriveType.REPRODUCTION.value]
    assert st_good.satisfaction >= 0.85


@pytest.mark.asyncio
async def test_v5_4_2_strict_constitution_guard_passes_with_new_drive_math():
    """Strict ConstitutionGuard (AST + YAML bounds + behavioral probe) must pass all 4 layers on V5.4.2 math."""
    guard = ConstitutionGuard(repo_root=PROJECT_ROOT, policy_mode="strict")
    targets = (
        "app/drives/scoring.py",
        "app/drives/survival.py",
        "app/drives/reproduction.py",
        "app/drives/curiosity.py",
        "config/drives.yaml",
    )
    verdict = await guard.evaluate(
        {
            "files": list(targets),
            "file_overrides": {
                rel: (PROJECT_ROOT / rel).read_text(encoding="utf-8")
                for rel in targets
            },
        }
    )
    assert verdict.passed is True, verdict.violations
    assert verdict.mutates_drive_metrics is True
    assert all(layer.passed for layer in verdict.layers)
