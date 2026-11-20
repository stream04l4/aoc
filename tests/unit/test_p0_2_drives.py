"""P0-2 Drive Engine tests: 40+ boundary, monotonicity, arbitration, and persistence tests."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import get_drive_config
from app.core.enums import NO_DRIVE, DriveType, ExecutionStatus
from app.drives.engine import DriveEngine
from app.drives.interfaces import DriveContext, DriveState, KnowledgeGap
from app.perception.interfaces import Observation, unavailable
from app.storage.repository import StateRepository

T0 = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)


def healthy_obs(**overrides) -> Observation:
    base = dict(
        timestamp=T0,
        system_state={
            "disk": {"free_ratio": 0.65, "paths": {"/": {"free_ratio": 0.65}}},
            "memory": {"available_ratio": 0.75},
            "cpu": {"load_per_core": 0.3, "percent": 95.0},  # high raw % to verify it's NOT used
            "llm": {"available": True, "latency_ms": 800},
            "services": {"postgres": {"ok": True}},
        },
        environment_state={"unknown_topics": []},
        available_resources={
            "agents": {
                "enabled_count": 2,
                "healthy_count": 2,
                "capabilities": [
                    "coding",
                    "research",
                    "repository_analysis",
                    "system_diagnosis",
                    "knowledge_summarization",
                ],
            },
            "budget": {"tasks_used": 0, "tasks_limit": 20, "tokens_used": 0, "tokens_limit": 2_000_000},
        },
        confidence=1.0,
    )
    base.update(overrides)
    return Observation(**base)


def healthy_ctx(**overrides) -> DriveContext:
    obs = overrides.pop("observation", healthy_obs())
    base = dict(
        now=T0,
        observation=obs,
        recent_execution_statuses=[ExecutionStatus.SUCCEEDED.value] * 5,
        budget={"tasks_used": 0, "tasks_limit": 20, "tokens_used": 0, "tokens_limit": 2_000_000},
        capabilities_acquired=[
            "coding",
            "research",
            "repository_analysis",
            "system_diagnosis",
            "knowledge_summarization",
        ],
        procedural_skill_count=10,
        capability_goals_completed_recent=5,
        memory_available=True,
        identity_persisted=True,
        open_knowledge_gaps=[],
        unknown_topics=[],
        exploration_attempted_recent=3,
        exploration_succeeded_recent=3,
        new_knowledge_recent=5,
        last_learning_at=T0 - timedelta(minutes=10),
        last_information_gain=0.9,
        last_information_gain_at=T0 - timedelta(minutes=10),
    )
    base.update(overrides)
    return DriveContext(**base)


# ── 1. Data model invariants (tests 1-5) ────────────────────────────────────
def test_01_drive_state_deficit_always_equals_one_minus_satisfaction():
    ds = DriveState(drive_type="survival", satisfaction=0.73, deficit=0.99, urgency=0.5, confidence=0.8)
    assert pytest.approx(ds.deficit, abs=1e-6) == 0.27


def test_02_drive_state_clamps_out_of_range_values():
    ds = DriveState(
        drive_type="curiosity",
        satisfaction=1.7,
        urgency=-0.5,
        confidence=2.0,
        persistence=5.0,
        trend=-3.0,
        estimated_cost=-1.0,
        estimated_risk=9.0,
    )
    assert ds.satisfaction == 1.0
    assert ds.deficit == 0.0
    assert ds.urgency == 0.0
    assert ds.confidence == 1.0
    assert ds.persistence == 1.0
    assert ds.trend == -1.0
    assert ds.estimated_cost == 0.0
    assert ds.estimated_risk == 1.0


def test_03_drive_state_nan_guard():
    nan = float("nan")
    ds = DriveState(drive_type="survival", satisfaction=nan, urgency=nan, confidence=nan, trend=nan)
    assert ds.satisfaction == 0.0
    assert ds.deficit == 1.0
    assert ds.urgency == 0.0
    assert ds.confidence == 0.0
    assert ds.trend == 0.0


def test_04_engine_has_three_independent_evaluators():
    engine = DriveEngine()
    assert set(engine.evaluators.keys()) == {"survival", "reproduction", "curiosity"}
    assert len({type(e) for e in engine.evaluators.values()}) == 3


def test_05_healthy_baseline_all_drives_high_satisfaction():
    engine = DriveEngine()
    states = engine.compute_states(healthy_ctx(), {})
    assert states["survival"].satisfaction > 0.90
    assert states["reproduction"].satisfaction > 0.90
    assert states["curiosity"].satisfaction > 0.85


# ── 2. Survival Drive boundary & scenario tests (tests 6-17) ────────────────
def test_06_survival_disk_critical_triggers_emergency():
    engine = DriveEngine()
    obs = healthy_obs()
    obs.system_state["disk"] = {"free_ratio": 0.03}
    states = engine.compute_states(healthy_ctx(observation=obs), {})
    assert states["survival"].satisfaction < 0.25
    assert states["survival"].urgency == 1.0


def test_07_survival_disk_monotonicity():
    engine = DriveEngine()
    sats = []
    for free_ratio in (0.02, 0.08, 0.14, 0.25):
        obs = healthy_obs()
        obs.system_state["disk"] = {"free_ratio": free_ratio}
        st = engine.compute_states(healthy_ctx(observation=obs), {})["survival"]
        sats.append(st.satisfaction)
    assert sats[0] < sats[1] < sats[2] <= sats[3]


def test_08_survival_multi_path_disk_uses_worst_path():
    engine = DriveEngine()
    obs = healthy_obs()
    obs.system_state["disk"] = {"paths": {"/": {"free_ratio": 0.80}, "/var": {"free_ratio": 0.04}}}
    st = engine.compute_states(healthy_ctx(observation=obs), {})["survival"]
    assert st.satisfaction < 0.30
    assert st.urgency == 1.0


def test_09_survival_memory_exhaustion_drops_satisfaction():
    engine = DriveEngine()
    obs = healthy_obs()
    obs.system_state["memory"] = {"available_ratio": 0.04}
    st = engine.compute_states(healthy_ctx(observation=obs), {})["survival"]
    assert st.satisfaction < 0.45
    assert any("memory" in n for n in st.notes)


def test_10_survival_does_not_use_raw_cpu_percent():
    engine = DriveEngine()
    obs_low = healthy_obs()
    obs_low.system_state["cpu"] = {"percent": 5.0, "load_per_core": 0.4}
    obs_high = healthy_obs()
    obs_high.system_state["cpu"] = {"percent": 99.0, "load_per_core": 0.4}
    s1 = engine.compute_states(healthy_ctx(observation=obs_low), {})["survival"].satisfaction
    s2 = engine.compute_states(healthy_ctx(observation=obs_high), {})["survival"].satisfaction
    assert s1 == s2


def test_11_survival_high_sustained_load_reduces_satisfaction():
    engine = DriveEngine()
    obs = healthy_obs()
    obs.system_state["cpu"] = {"load_per_core": 4.0}
    s_high = engine.compute_states(healthy_ctx(observation=obs), {})["survival"].satisfaction
    s_norm = engine.compute_states(healthy_ctx(), {})["survival"].satisfaction
    assert s_high < s_norm


def test_12_survival_llm_unavailable_triggers_critical():
    engine = DriveEngine()
    obs = healthy_obs()
    obs.system_state["llm"] = {"available": False, "error": "connection refused"}
    st = engine.compute_states(healthy_ctx(observation=obs), {})["survival"]
    assert st.dimensions["llm_health"] == 0.0
    assert st.satisfaction <= 0.15
    assert st.urgency == 1.0


def test_13_survival_llm_high_latency_degrades_llm_dimension():
    engine = DriveEngine()
    obs = healthy_obs()
    obs.system_state["llm"] = {"available": True, "latency_ms": 45000}
    st = engine.compute_states(healthy_ctx(observation=obs), {})["survival"]
    assert 0.2 <= st.dimensions["llm_health"] < 0.6


def test_14_survival_zero_healthy_agents_lowers_score():
    engine = DriveEngine()
    obs = healthy_obs()
    obs.available_resources["agents"] = {"enabled_count": 2, "healthy_count": 0}
    st = engine.compute_states(healthy_ctx(observation=obs), {})["survival"]
    assert st.dimensions["agent_availability"] == 0.0
    assert st.satisfaction < 0.90


def test_15_survival_budget_exhausted_does_not_trigger_critical():
    # V2.0 P1-1 (T1): Economic Quota exhaustion is decoupled from Survival CRITICAL
    engine = DriveEngine()
    ctx = healthy_ctx(budget={"tasks_used": 20, "tasks_limit": 20, "tokens_used": 100, "tokens_limit": 100000})
    st = engine.compute_states(ctx, {})["survival"]
    assert st.dimensions["budget"] == 0.0
    assert st.satisfaction > 0.80
    assert st.urgency < 0.60
    decision = engine.arbitrate({"survival": st}, now=T0)
    assert not decision.constraints.get("emergency_mode")


def test_16_survival_recent_failures_lower_reliability():
    engine = DriveEngine()
    ctx = healthy_ctx(recent_execution_statuses=[ExecutionStatus.FAILED.value, ExecutionStatus.TIMEOUT.value] * 3)
    st = engine.compute_states(ctx, {})["survival"]
    assert st.dimensions["reliability"] < 0.6


def test_17_survival_unavailable_metrics_lower_confidence_never_normal():
    engine = DriveEngine()
    obs = Observation(
        timestamp=T0,
        system_state={
            "disk": unavailable("permission denied"),
            "memory": unavailable("proc missing"),
            "llm": unavailable("not probed"),
        },
        available_resources={"agents": unavailable("registry down")},
        confidence=0.8,
    )
    ctx = DriveContext(now=T0, observation=obs)
    st = engine.compute_states(ctx, {})["survival"]
    assert st.dimensions["resources"] is None
    assert st.dimensions["llm_health"] is None
    assert st.dimensions["agent_availability"] is None
    assert st.confidence < 0.30


# ── 3. Reproduction Drive tests (tests 18-22) ───────────────────────────────
def test_18_reproduction_missing_capabilities_drops_satisfaction():
    engine = DriveEngine()
    obs = healthy_obs()
    obs.available_resources["agents"]["capabilities"] = []
    ctx = healthy_ctx(
        observation=obs,
        capabilities_acquired=[],
        procedural_skill_count=0,
        capability_goals_completed_recent=0,
    )
    st = engine.compute_states(ctx, {})["reproduction"]
    assert st.dimensions["capability_coverage"] == 0.0
    assert st.dimensions["reusable_skills"] == 0.0
    assert st.satisfaction < 0.35


def test_19_reproduction_acquiring_skills_improves_satisfaction():
    engine = DriveEngine()
    obs = healthy_obs()
    obs.available_resources["agents"]["capabilities"] = ["coding"]
    s0 = engine.compute_states(
        healthy_ctx(observation=obs, capabilities_acquired=["coding"], procedural_skill_count=1, capability_goals_completed_recent=0),
        {},
    )["reproduction"].satisfaction
    s1 = engine.compute_states(
        healthy_ctx(
            observation=obs,
            capabilities_acquired=["coding", "research", "system_diagnosis"],
            procedural_skill_count=6,
            capability_goals_completed_recent=3,
        ),
        {},
    )["reproduction"].satisfaction
    assert s1 > s0


def test_20_reproduction_unavailable_skill_count_reduces_confidence():
    engine = DriveEngine()
    full_conf = engine.compute_states(healthy_ctx(procedural_skill_count=5), {})["reproduction"].confidence
    miss_conf = engine.compute_states(healthy_ctx(procedural_skill_count=None), {})["reproduction"].confidence
    assert miss_conf < full_conf


def test_21_reproduction_low_reserve_reduces_reserve_dimension():
    engine = DriveEngine()
    obs = healthy_obs()
    obs.system_state["disk"] = {"free_ratio": 0.10}
    obs.system_state["memory"] = {"available_ratio": 0.15}
    ctx = healthy_ctx(observation=obs, budget={"tasks_used": 18, "tasks_limit": 20})
    st = engine.compute_states(ctx, {})["reproduction"]
    assert st.dimensions["resource_reserve"] < 0.20


def test_22_reproduction_readiness_reflects_memory_and_identity():
    engine = DriveEngine()
    st_ready = engine.compute_states(healthy_ctx(memory_available=True, identity_persisted=True), {})["reproduction"]
    st_not = engine.compute_states(healthy_ctx(memory_available=False, identity_persisted=False), {})["reproduction"]
    assert st_not.dimensions["expansion_readiness"] < st_ready.dimensions["expansion_readiness"]


# ── 4. Curiosity Drive tests (tests 23-28) ──────────────────────────────────
def test_23_curiosity_open_gaps_drop_satisfaction():
    engine = DriveEngine()
    gaps = [
        KnowledgeGap(topic="rust-async", importance=0.9),
        KnowledgeGap(topic="ebpf-tracing", importance=0.8),
        KnowledgeGap(topic="duckdb-internals", importance=0.7),
    ]
    st = engine.compute_states(healthy_ctx(open_knowledge_gaps=gaps), {})["curiosity"]
    assert st.satisfaction < 0.45
    assert st.deficit > 0.55


def test_24_curiosity_environment_unknown_topics_detected():
    engine = DriveEngine()
    obs = healthy_obs(
        environment_state={
            "unknown_topics": [
                {"topic": "topic-a"},
                {"topic": "topic-b"},
                {"topic": "topic-c"},
            ]
        }
    )
    st = engine.compute_states(healthy_ctx(observation=obs), {})["curiosity"]
    assert st.dimensions["unknown_information"] < 0.5


def test_25_curiosity_information_gain_decays_over_time():
    engine = DriveEngine()
    recent = engine.compute_states(
        healthy_ctx(last_information_gain=1.0, last_information_gain_at=T0 - timedelta(hours=1)), {}
    )["curiosity"]
    stale = engine.compute_states(
        healthy_ctx(last_information_gain=1.0, last_information_gain_at=T0 - timedelta(hours=48)), {}
    )["curiosity"]
    assert stale.dimensions["information_gain"] < recent.dimensions["information_gain"]


def test_26_curiosity_learning_progress_decays_without_recent_learning():
    engine = DriveEngine()
    fresh = engine.compute_states(healthy_ctx(last_learning_at=T0), {})["curiosity"]
    old = engine.compute_states(healthy_ctx(last_learning_at=T0 - timedelta(hours=36)), {})["curiosity"]
    assert old.dimensions["learning_progress"] < 0.2 < fresh.dimensions["learning_progress"]


def test_27_curiosity_gap_assessment_confidence_modulates_confidence():
    engine = DriveEngine()
    hi = engine.compute_states(healthy_ctx(gap_assessment_confidence=0.95), {})["curiosity"].confidence
    lo = engine.compute_states(healthy_ctx(gap_assessment_confidence=0.10), {})["curiosity"].confidence
    assert lo < hi


def test_28_curiosity_exploration_failures_lower_completion_score():
    engine = DriveEngine()
    st = engine.compute_states(
        healthy_ctx(exploration_attempted_recent=4, exploration_succeeded_recent=1), {}
    )["curiosity"]
    assert pytest.approx(st.dimensions["exploration_completion"], abs=1e-6) == 0.25


# ── 5. Dynamics: persistence, trend, satiation (tests 29-33) ────────────────
def test_29_persistence_grows_across_repeated_unmet_cycles():
    engine = DriveEngine()
    gaps = [KnowledgeGap(topic=f"t{i}", importance=0.9) for i in range(4)]
    prev: dict[str, DriveState] = {}
    pers = []
    for i in range(5):
        ctx = healthy_ctx(now=T0 + timedelta(minutes=i * 5), open_knowledge_gaps=gaps)
        prev = engine.compute_states(ctx, prev)
        pers.append(prev["curiosity"].persistence)
    assert pers[0] < pers[1] < pers[2] < pers[3] < pers[4]
    assert pers[-1] <= 1.0


def test_30_persistence_decays_once_satisfied():
    engine = DriveEngine()
    gaps = [KnowledgeGap(topic=f"t{i}", importance=0.9) for i in range(4)]
    prev: dict[str, DriveState] = {}
    for i in range(4):
        prev = engine.compute_states(healthy_ctx(now=T0 + timedelta(minutes=i), open_knowledge_gaps=gaps), prev)
    high_p = prev["curiosity"].persistence
    assert high_p > 0.2
    after = engine.compute_states(healthy_ctx(now=T0 + timedelta(minutes=10), open_knowledge_gaps=[]), prev)
    assert after["curiosity"].persistence < high_p


def test_31_trend_negative_when_satisfaction_drops_and_boosts_urgency():
    engine = DriveEngine()
    st1 = engine.compute_states(healthy_ctx(), {})
    obs_drop = healthy_obs()
    obs_drop.system_state["memory"] = {"available_ratio": 0.12}
    st2 = engine.compute_states(healthy_ctx(now=T0 + timedelta(minutes=1), observation=obs_drop), st1)
    assert st2["survival"].trend < 0
    # Compare urgency with falling trend vs fresh evaluation with no prior trend
    st2_fresh = engine.compute_states(healthy_ctx(now=T0 + timedelta(minutes=1), observation=obs_drop), {})
    assert st2["survival"].urgency > st2_fresh["survival"].urgency


def test_32_trend_positive_and_last_satisfied_at_updated_on_improvement():
    engine = DriveEngine()
    gaps = [KnowledgeGap(topic=f"t{i}", importance=0.9) for i in range(4)]
    st1 = engine.compute_states(healthy_ctx(now=T0, open_knowledge_gaps=gaps), {})
    t1 = T0 + timedelta(minutes=10)
    st2 = engine.compute_states(healthy_ctx(now=t1, open_knowledge_gaps=[]), st1)
    assert st2["curiosity"].trend > 0
    assert st2["curiosity"].last_satisfied_at == t1


def test_33_zero_observations_all_unavailable_gives_zero_confidence():
    engine = DriveEngine()
    obs = Observation(timestamp=T0, confidence=0.0)
    ctx = DriveContext(now=T0, observation=obs, procedural_skill_count=None, new_knowledge_recent=None)
    states = engine.compute_states(ctx, {})
    assert states["survival"].confidence == 0.0


# ── 6. Arbitration tests (tests 34-43) ──────────────────────────────────────
def test_34_arbitration_selects_idle_when_all_drives_satisfied():
    engine = DriveEngine()
    states = engine.compute_states(healthy_ctx(), {})
    decision = engine.arbitrate(states, now=T0)
    assert decision.selected_drive == NO_DRIVE
    assert "IDLE" in decision.reason


def test_35_arbitration_selects_curiosity_when_knowledge_gaps_present():
    engine = DriveEngine()
    gaps = [KnowledgeGap(topic=f"quantum-{i}", importance=0.9) for i in range(4)]
    ctx = healthy_ctx(
        open_knowledge_gaps=gaps,
        new_knowledge_recent=0,
        last_learning_at=T0 - timedelta(hours=48),
        last_information_gain=0.0,
    )
    states = engine.compute_states(ctx, {})
    decision = engine.arbitrate(states, now=T0)
    assert decision.selected_drive == DriveType.CURIOSITY.value
    assert "survival" in decision.alternative_drives
    assert "reproduction" in decision.alternative_drives
    assert "Rejected alternatives" in decision.reason


def test_36_arbitration_selects_reproduction_when_capability_deficient():
    engine = DriveEngine()
    obs = healthy_obs()
    obs.available_resources["agents"]["capabilities"] = []
    ctx = healthy_ctx(
        observation=obs,
        capabilities_acquired=[],
        procedural_skill_count=0,
        capability_goals_completed_recent=0,
    )
    states = engine.compute_states(ctx, {})
    decision = engine.arbitrate(states, now=T0)
    assert decision.selected_drive == DriveType.REPRODUCTION.value


def test_37_arbitration_survival_emergency_overrides_all_others():
    engine = DriveEngine()
    obs = healthy_obs()
    obs.system_state["disk"] = {"free_ratio": 0.02}
    obs.available_resources["agents"]["capabilities"] = []
    gaps = [KnowledgeGap(topic=f"g{i}", importance=1.0) for i in range(5)]
    ctx = healthy_ctx(
        observation=obs,
        capabilities_acquired=[],
        procedural_skill_count=0,
        open_knowledge_gaps=gaps,
        new_knowledge_recent=0,
    )
    states = engine.compute_states(ctx, {})
    # Even if survival was just satisfied 1 second ago, emergency bypasses inhibition
    states["survival"] = states["survival"].model_copy(update={"last_satisfied_at": T0})
    decision = engine.arbitrate(states, now=T0 + timedelta(seconds=1))
    assert decision.selected_drive == DriveType.SURVIVAL.value
    assert decision.constraints.get("emergency_mode") is True
    assert decision.scores["survival"]["inhibition_factor"] == 1.0


def test_38_arbitration_recent_satisfaction_inhibits_drive_and_decays():
    engine = DriveEngine()
    base_state = DriveState(
        drive_type="curiosity",
        satisfaction=0.35,
        urgency=0.65,
        confidence=0.95,
        persistence=0.2,
        estimated_cost=0.1,
        estimated_risk=0.05,
        last_satisfied_at=T0,
    )
    d_immediate = engine.arbitrate({"curiosity": base_state}, now=T0 + timedelta(seconds=5))
    d_later = engine.arbitrate({"curiosity": base_state}, now=T0 + timedelta(hours=3))
    assert d_immediate.scores["curiosity"]["inhibition_factor"] < 0.5
    assert d_later.scores["curiosity"]["inhibition_factor"] > 0.95
    assert d_later.scores["curiosity"]["priority"] > d_immediate.scores["curiosity"]["priority"]


def test_39_arbitration_persistence_breaks_tie_for_long_unmet_drive():
    engine = DriveEngine()
    fresh = DriveState(
        drive_type="curiosity", satisfaction=0.40, urgency=0.60, confidence=0.9, persistence=0.0,
        estimated_cost=0.2, estimated_risk=0.1,
    )
    long_unmet = DriveState(
        drive_type="reproduction", satisfaction=0.40, urgency=0.60, confidence=0.9, persistence=0.95,
        estimated_cost=0.2, estimated_risk=0.1,
    )
    decision = engine.arbitrate({"curiosity": fresh, "reproduction": long_unmet}, now=T0)
    assert decision.selected_drive == "reproduction"


def test_40_arbitration_cost_and_risk_penalties_affect_choice():
    engine = DriveEngine()
    risky = DriveState(
        drive_type="curiosity", satisfaction=0.45, urgency=0.60, confidence=0.9,
        estimated_cost=1.0, estimated_risk=1.0,
    )
    cheap = DriveState(
        drive_type="reproduction", satisfaction=0.45, urgency=0.60, confidence=0.9,
        estimated_cost=0.0, estimated_risk=0.0,
    )
    decision = engine.arbitrate({"curiosity": risky, "reproduction": cheap}, now=T0)
    assert decision.selected_drive == "reproduction"


def test_41_arbitration_filters_low_confidence_drives():
    engine = DriveEngine()
    low_conf = DriveState(
        drive_type="curiosity", satisfaction=0.05, urgency=0.95, confidence=0.05,
        estimated_cost=0.0, estimated_risk=0.0,
    )
    decision = engine.arbitrate({"curiosity": low_conf}, now=T0)
    assert decision.selected_drive == NO_DRIVE
    assert decision.scores["curiosity"]["eligible"] is False


def test_42_arbitration_respects_external_config_weights():
    cfg = copy.deepcopy(get_drive_config())
    cfg["drives"]["reproduction"]["weight"] = 3.0
    cfg["drives"]["curiosity"]["weight"] = 0.2
    engine = DriveEngine(config=cfg)
    states = {
        "curiosity": DriveState(drive_type="curiosity", satisfaction=0.4, urgency=0.6, confidence=0.9),
        "reproduction": DriveState(drive_type="reproduction", satisfaction=0.4, urgency=0.6, confidence=0.9),
    }
    decision = engine.arbitrate(states, now=T0)
    assert decision.selected_drive == "reproduction"


@pytest.mark.asyncio
async def test_43_drive_state_roundtrip_persistence_across_cycles(repo: StateRepository):
    engine = DriveEngine()
    ident = await repo.get_or_create_identity("AOC-DRIVE-TEST", "1.0.0")
    org_id = ident["id"]

    # Cycle 1: gaps open -> curiosity unmet
    c1, _ = await repo.start_cycle(org_id, "ACTIVE")
    gaps = [KnowledgeGap(topic=f"gap-{i}", importance=0.85) for i in range(3)]
    s1 = engine.compute_states(healthy_ctx(now=T0, open_knowledge_gaps=gaps), {})
    await repo.save_drive_states(org_id, s1.values(), cycle_id=c1, phase="pre")

    # Reload from DB (simulating process restart)
    loaded_rows = await repo.load_drive_states(org_id)
    reloaded = {r["drive_type"]: DriveState(**r) for r in loaded_rows}
    assert set(reloaded.keys()) == {"survival", "reproduction", "curiosity"}
    assert pytest.approx(reloaded["curiosity"].satisfaction, abs=1e-6) == s1["curiosity"].satisfaction

    # Cycle 2: still unmet -> persistence increases using reloaded state
    c2, _ = await repo.start_cycle(org_id, "ACTIVE")
    s2 = engine.compute_states(healthy_ctx(now=T0 + timedelta(minutes=5), open_knowledge_gaps=gaps), reloaded)
    await repo.save_drive_states(org_id, s2.values(), cycle_id=c2, phase="pre")
    assert s2["curiosity"].persistence > reloaded["curiosity"].persistence
