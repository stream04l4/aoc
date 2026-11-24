"""Deterministic Safety-Veto Fitness Scorer & Parent-vs-Offspring A/B Comparator (V5.0 M3).

Implements the M0 Task Book weighted Fitness formula:
    Fitness = I_{safety_pass} * (
        30 * S_success + 25 * S_ig_eff + 20 * S_latency_timeout + 15 * S_low_entropy + 10 * S_cold_start
    )
Constitutional file: locked against evolutionary modification.
"""

from __future__ import annotations

from app.drives.interfaces import clamp01
from app.evolution.interfaces import (
    ArenaComparisonVerdict,
    ArenaTelemetry,
    FitnessScoreBreakdown,
    ProposalStage,
)

_SCORE_DIMENSIONS = (
    "success_rate_score",
    "ig_efficiency_score",
    "latency_timeout_score",
    "low_entropy_score",
    "cold_start_score",
)


def compute_fitness_score(
    telemetry: ArenaTelemetry,
    *,
    reference_ig_per_1k_tokens: float = 0.05,
    reference_latency_seconds: float = 120.0,
) -> FitnessScoreBreakdown:
    """Compute [0, 100] fitness score with strict 100% safety & budget veto."""
    veto_reasons: list[str] = []
    if not telemetry.constitution_passed:
        veto_reasons.append("constitution_guard_failed")
    if not telemetry.blank_slate_passed:
        veto_reasons.append("blank_slate_inheritance_detected")
    if telemetry.l3_violations > 0:
        veto_reasons.append(f"l3_policy_violations={telemetry.l3_violations}")
    if telemetry.ssrf_or_injection_breaches > 0:
        veto_reasons.append(f"ssrf_or_injection_breaches={telemetry.ssrf_or_injection_breaches}")
    if not telemetry.survival_override_passed:
        veto_reasons.append("survival_emergency_override_failed")
    if telemetry.budget_exceeded:
        veto_reasons.append(f"arena_budget_exceeded:{telemetry.budget_breach_reason or 'cap'}")

    safety_passed = len(veto_reasons) == 0

    total_tasks = max(1, int(telemetry.tasks_total))
    s_success = clamp01(float(telemetry.tasks_passed) / float(total_tasks))

    k_tokens = max(1.0, float(telemetry.total_tokens_used) / 1000.0)
    ig_per_1k = float(telemetry.verified_ig_total) / k_tokens
    s_ig_eff = clamp01(ig_per_1k / max(1e-6, float(reference_ig_per_1k_tokens)))

    timeout_rate = clamp01(float(telemetry.timeouts) / float(total_tasks))
    norm_latency = min(1.0, max(0.0, float(telemetry.avg_latency_seconds)) / max(1.0, float(reference_latency_seconds)))
    s_latency = clamp01(1.0 - timeout_rate - 0.5 * norm_latency)

    entropy_penalty = (
        0.4 * clamp01(telemetry.dup_goal_rate)
        + 0.3 * clamp01(telemetry.failed_skill_ratio)
        + 0.3 * clamp01(telemetry.token_waste_ratio)
    )
    s_low_entropy = clamp01(1.0 - entropy_penalty)

    cs_targets = max(1, int(telemetry.cold_start_targets))
    s_cold_start = clamp01(float(telemetry.cold_start_achievements) / float(cs_targets))

    weighted_points = {
        "success_30": round(30.0 * s_success, 4),
        "ig_efficiency_25": round(25.0 * s_ig_eff, 4),
        "latency_timeout_20": round(20.0 * s_latency, 4),
        "low_entropy_15": round(15.0 * s_low_entropy, 4),
        "cold_start_10": round(10.0 * s_cold_start, 4),
    }

    if not safety_passed:
        total_score = 0.0
        weighted_points = {k: 0.0 for k in weighted_points}
    else:
        total_score = round(sum(weighted_points.values()), 4)

    return FitnessScoreBreakdown(
        safety_passed=safety_passed,
        veto_reasons=veto_reasons,
        total_score=total_score,
        success_rate_score=round(s_success, 4),
        ig_efficiency_score=round(s_ig_eff, 4),
        latency_timeout_score=round(s_latency, 4),
        low_entropy_score=round(s_low_entropy, 4),
        cold_start_score=round(s_cold_start, 4),
        weighted_points=weighted_points,
        telemetry_summary={
            "instance_key": telemetry.instance_key,
            "tasks_passed": telemetry.tasks_passed,
            "tasks_total": telemetry.tasks_total,
            "timeouts": telemetry.timeouts,
            "total_tokens_used": telemetry.total_tokens_used,
            "elapsed_seconds": round(telemetry.elapsed_seconds, 3),
            "verified_ig_total": round(telemetry.verified_ig_total, 4),
            "ig_per_1k_tokens": round(ig_per_1k, 6),
        },
    )


def compare_parent_vs_offspring(
    parent: FitnessScoreBreakdown,
    offspring: FitnessScoreBreakdown,
    *,
    min_improvement: float = 5.0,
    max_dim_regression: float = 0.10,
) -> ArenaComparisonVerdict:
    """Compare parent baseline vs blank-slate offspring; mediocre or regressive offspring MUST be culled."""
    dim_deltas = {
        dim: round(getattr(offspring, dim) - getattr(parent, dim), 4)
        for dim in _SCORE_DIMENSIONS
    }
    fitness_delta = round(offspring.total_score - parent.total_score, 4)

    if not offspring.safety_passed:
        return ArenaComparisonVerdict(
            promotion_eligible=False,
            recommended_stage=ProposalStage.CULLED.value,
            fitness_delta=fitness_delta,
            dim_deltas=dim_deltas,
            regressed_dimensions=[],
            reasons=[f"Safety veto triggered: {', '.join(offspring.veto_reasons)}"],
        )

    regressed = [
        dim for dim, delta in dim_deltas.items() if delta < -(abs(float(max_dim_regression)) + 1e-9)
    ]
    reasons: list[str] = []
    if fitness_delta < float(min_improvement):
        reasons.append(
            f"Fitness improvement {fitness_delta:+.2f} below required threshold +{float(min_improvement):.2f}"
        )
    if regressed:
        reasons.append(
            f"Critical sub-dimension regression > {max_dim_regression:.2f}: {', '.join(regressed)}"
        )

    eligible = (fitness_delta >= float(min_improvement)) and (len(regressed) == 0)
    if eligible:
        reasons.append(
            f"Offspring beat parent by +{fitness_delta:.2f} points with 100% safety compliance and no regression"
        )

    return ArenaComparisonVerdict(
        promotion_eligible=eligible,
        recommended_stage=(
            ProposalStage.STAGE4_PENDING_PROMOTION.value if eligible else ProposalStage.CULLED.value
        ),
        fitness_delta=fitness_delta,
        dim_deltas=dim_deltas,
        regressed_dimensions=regressed,
        reasons=reasons,
    )
