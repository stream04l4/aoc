"""Reproduction Drive Evaluator (task book 4.2).

V1.0 does NOT create or clone new organism instances. Reproduction is modeled
via virtual capability-building targets:
  - capability_coverage: acquired capabilities vs desired capability set
  - reusable_skills: procedural skills stored in long-term memory
  - capability_goals: completed capability-building goals in rolling window
  - resource_reserve: spare compute/budget headroom needed for future growth
  - expansion_readiness: persisted identity + healthy memory + healthy agents
"""

from __future__ import annotations

from typing import Any

from app.core.enums import DriveType
from app.drives.interfaces import DriveContext, DriveEvaluation, DriveEvaluator, clamp01
from app.drives.scoring import extract_number, linear_band, weighted_dimensions
from app.perception.interfaces import is_unavailable


class ReproductionDriveEvaluator(DriveEvaluator):
    drive_type = DriveType.REPRODUCTION.value

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg

    def evaluate(self, ctx: DriveContext) -> DriveEvaluation:
        targets = self.cfg.get("targets", {})
        desired = list(self.cfg.get("desired_capabilities", []))
        obs = ctx.observation
        sys_state = obs.system_state or {}
        avail_res = obs.available_resources or {}
        notes: list[str] = []

        # 1. Capability coverage (V2.0 P1-7 / T10 fix):
        # STRICTLY evidence-driven from capability_profiles(ACQUIRED|VALIDATED).
        # Static external Agent capabilities in avail_res["agents"]["capabilities"]
        # contribute ZERO to AOC's own capability coverage.
        cap_score: float | None = None
        if desired:
            acquired = set(ctx.capabilities_acquired) | set(ctx.validated_capabilities)
            validated = set(ctx.validated_capabilities)
            acquired_weight = float(self.cfg.get("acquired_capability_credit", 1.0))
            matched_credit = 0.0
            missing: list[str] = []
            for cap in desired:
                if cap in validated:
                    matched_credit += 1.0
                elif cap in acquired:
                    matched_credit += acquired_weight
                else:
                    missing.append(cap)
            cap_score = clamp01(matched_credit / len(desired))
            if missing:
                notes.append(f"missing desired capabilities: {', '.join(missing[:5])}")

        # 2. Reusable skills count (V2.0 P1-7: prefer checksum-intact VALIDATED skills from SkillRegistry)
        skill_target = max(1, int(targets.get("skills", 10)))
        skills_score: float | None = None
        effective_skill_count = (
            ctx.validated_skill_count
            if ctx.validated_skill_count is not None
            else ctx.procedural_skill_count
        )
        if effective_skill_count is not None:
            skills_score = clamp01(effective_skill_count / skill_target)
            if effective_skill_count < skill_target:
                notes.append(f"validated reusable skills {effective_skill_count}/{skill_target}")

        # 3. Completed capability-building goals
        goal_target = max(1, int(targets.get("capability_goals_window", 5)))
        cap_goals_score = clamp01(ctx.capability_goals_completed_recent / goal_target)

        # 4. Resource reserve
        reserve_score = self._score_reserve(sys_state, ctx.budget, avail_res)

        # 5. Expansion readiness (identity + long-term memory + healthy agents)
        readiness_score = self._score_readiness(ctx, avail_res)

        dimensions: dict[str, float | None] = {
            "capability_coverage": cap_score,
            "reusable_skills": skills_score,
            "capability_goals": cap_goals_score,
            "resource_reserve": reserve_score,
            "expansion_readiness": readiness_score,
        }

        weights = self.cfg.get(
            "dimension_weights",
            {
                "capability_coverage": 0.30,
                "reusable_skills": 0.25,
                "capability_goals": 0.15,
                "resource_reserve": 0.15,
                "expansion_readiness": 0.15,
            },
        )
        base_u = float(self.cfg.get("base_urgency", 0.1))
        u_gain = float(self.cfg.get("urgency_deficit_gain", 0.5))
        urgency_boost = 0.0

        if ctx.reproduction_quality_scoring_enabled:
            eff_cnt = int(effective_skill_count or 0)
            reuse_q = (
                clamp01(ctx.skill_reuse_ratio_recent)
                if ctx.skill_reuse_ratio_recent is not None
                else (0.0 if eff_cnt >= 3 else 0.5)
            )
            rel_q = (
                clamp01((float(ctx.execution_reliability_recent) - 0.60) / 0.40)
                if ctx.execution_reliability_recent is not None
                else 1.0
            )
            pb_q = (
                clamp01(ctx.playbook_freshness_score)
                if ctx.playbook_freshness_score is not None
                else 0.25
            )
            dimensions["skill_reuse_quality"] = reuse_q
            dimensions["execution_reliability"] = rel_q
            dimensions["playbook_freshness"] = pb_q
            weights = self.cfg.get(
                "quality_dimension_weights",
                {
                    "capability_coverage": 0.03,
                    "reusable_skills": 0.06,
                    "capability_goals": 0.04,
                    "resource_reserve": 0.03,
                    "expansion_readiness": 0.02,
                    "skill_reuse_quality": 0.45,
                    "execution_reliability": 0.25,
                    "playbook_freshness": 0.12,
                },
            )
            if eff_cnt >= 3 and reuse_q < 0.20:
                urgency_boost += 0.30 * clamp01((0.20 - reuse_q) / 0.20)
                notes.append(f"low skill reuse quality ({reuse_q:.2f}) across {eff_cnt} validated skills")
            if int(ctx.pending_skill_refactor_count or 0) > 0:
                urgency_boost += 0.18
                notes.append(f"pending skill refactors: {int(ctx.pending_skill_refactor_count)}")
            if ctx.execution_reliability_recent is not None and float(ctx.execution_reliability_recent) < 0.92:
                urgency_boost += 0.14
                notes.append(f"execution reliability below target ({float(ctx.execution_reliability_recent):.2f})")

        satisfaction, data_conf, w_notes = weighted_dimensions(
            dimensions, weights, default_when_all_missing=0.40
        )
        notes.extend(w_notes)
        confidence = clamp01(data_conf * max(0.2, obs.confidence)) if data_conf > 0 else 0.0

        deficit = 1.0 - satisfaction
        urgency = clamp01(base_u + u_gain * deficit + urgency_boost)

        return DriveEvaluation(
            drive_type=self.drive_type,
            satisfaction=satisfaction,
            urgency=urgency,
            confidence=confidence,
            estimated_cost=clamp01(float(self.cfg.get("estimated_cost", 0.4))),
            estimated_risk=clamp01(float(self.cfg.get("estimated_risk", 0.2))),
            dimensions=dimensions,
            notes=notes,
            critical=False,
        )

    def _score_reserve(
        self, sys_state: dict[str, Any], budget: dict[str, Any], avail_res: dict[str, Any]
    ) -> float | None:
        parts: list[float] = []
        disk = sys_state.get("disk")
        if not is_unavailable(disk) and isinstance(disk, dict):
            ratio = extract_number(disk, "free_ratio")
            if ratio is not None:
                parts.append(linear_band(ratio, 0.15, 0.50))
        mem = sys_state.get("memory")
        if not is_unavailable(mem) and isinstance(mem, dict):
            ratio = extract_number(mem, "available_ratio")
            if ratio is not None:
                parts.append(linear_band(ratio, 0.20, 0.60))
        b = budget or avail_res.get("budget")
        if not is_unavailable(b) and isinstance(b, dict):
            used = extract_number(b, "tasks_used")
            limit = extract_number(b, "tasks_limit")
            if used is not None and limit is not None and limit > 0:
                parts.append(clamp01(1.0 - used / limit))
        return (sum(parts) / len(parts)) if parts else None

    def _score_readiness(self, ctx: DriveContext, avail_res: dict[str, Any]) -> float | None:
        checks: list[float] = []
        if ctx.identity_persisted is not None:
            checks.append(1.0 if ctx.identity_persisted else 0.0)
        if ctx.memory_available is not None:
            checks.append(1.0 if ctx.memory_available else 0.0)
        if ctx.knowledge_transfer_readiness is not None:
            checks.append(clamp01(ctx.knowledge_transfer_readiness))
        agents = avail_res.get("agents")
        if not is_unavailable(agents) and isinstance(agents, dict):
            healthy = extract_number(agents, "healthy_count")
            if healthy is not None:
                checks.append(1.0 if healthy >= 1 else 0.0)
        return (sum(checks) / len(checks)) if checks else None
