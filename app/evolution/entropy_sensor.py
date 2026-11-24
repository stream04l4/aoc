"""Objective Runtime Entropy Sensor for AOC V5.0 (deterministic, read-only perception)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from app.evolution.interfaces import EntropySnapshot
from app.storage.database import Database
from app.storage.models import ActiveGoal, CycleRecord, ResourceUsage, SkillRegistryRow, WorldState


class EntropySensor:
    """Computes objective [0, 1] runtime entropy metrics without LLM inference."""

    def __init__(self, db: Database, *, window_cycles: int = 50) -> None:
        self.db = db
        self.window_cycles = max(5, int(window_cycles))

    async def measure(self) -> EntropySnapshot:
        async with self.db.session() as s:
            # 1 & 2. Skill deprecation & non-reuse ratios
            total_skills = int((await s.execute(select(func.count(SkillRegistryRow.id)))).scalar_one() or 0)
            dep_skills = int(
                (
                    await s.execute(
                        select(func.count(SkillRegistryRow.id)).where(SkillRegistryRow.status == "DEPRECATED")
                    )
                ).scalar_one()
                or 0
            )
            val_skills = int(
                (
                    await s.execute(
                        select(func.count(SkillRegistryRow.id)).where(SkillRegistryRow.status == "VALIDATED")
                    )
                ).scalar_one()
                or 0
            )
            unreused_val = int(
                (
                    await s.execute(
                        select(func.count(SkillRegistryRow.id)).where(
                            SkillRegistryRow.status == "VALIDATED",
                            SkillRegistryRow.reuse_count == 0,
                        )
                    )
                ).scalar_one()
                or 0
            )

            # 3. Hypothesis backlog ratio (`unverified` + `inconclusive` over all hypotheses)
            total_hyp = int(
                (
                    await s.execute(select(func.count(WorldState.id)).where(WorldState.kind == "hypothesis"))
                ).scalar_one()
                or 0
            )
            open_hyp = int(
                (
                    await s.execute(
                        select(func.count(WorldState.id)).where(
                            WorldState.kind == "hypothesis",
                            WorldState.status.in_(("unverified", "inconclusive")),
                        )
                    )
                ).scalar_one()
                or 0
            )

            # 4. Playbook bloat score (`active` playbooks vs target ceiling 60)
            active_playbooks = int(
                (
                    await s.execute(
                        select(func.count(WorldState.id)).where(
                            WorldState.kind == "external_playbook",
                            WorldState.status == "active",
                        )
                    )
                ).scalar_one()
                or 0
            )

            # 5. Recent non-IDLE goal waste ratio
            cycle_rows = (
                await s.execute(
                    select(CycleRecord.outcome)
                    .where(CycleRecord.outcome.is_not(None), CycleRecord.outcome != "IDLE")
                    .order_by(CycleRecord.cycle_number.desc())
                    .limit(self.window_cycles)
                )
            ).scalars().all()
            waste_outcomes = {"GOAL_DEDUP_SKIPPED", "PREFLIGHT_REJECTED", "ACTION_FAILED"}
            waste_count = sum(1 for o in cycle_rows if o in waste_outcomes)

            # 6. IG / 1k Tokens drop relative to earlier baseline window
            ig_goals = (
                await s.execute(
                    select(ActiveGoal.cycle_id, ActiveGoal.verified_ig)
                    .where(ActiveGoal.verified_ig.is_not(None))
                    .order_by(ActiveGoal.created_at.desc())
                    .limit(self.window_cycles * 3)
                )
            ).all()

            async def _window_ig_per_1k(rows: list[Any]) -> float | None:
                if len(rows) < 5:
                    return None
                cids = [r[0] for r in rows if r[0] is not None]
                total_ig = sum(float(r[1] or 0.0) for r in rows)
                if not cids:
                    return total_ig
                toks = float(
                    (
                        await s.execute(
                            select(func.coalesce(func.sum(ResourceUsage.amount), 0.0)).where(
                                ResourceUsage.category == "llm",
                                ResourceUsage.cycle_id.in_(cids),
                            )
                        )
                    ).scalar_one()
                    or 0.0
                )
                return total_ig / max(1.0, toks / 1000.0)

            recent_rows = list(ig_goals[: self.window_cycles])
            base_rows = list(ig_goals[self.window_cycles :])
            recent_eff = await _window_ig_per_1k(recent_rows)
            base_eff = await _window_ig_per_1k(base_rows)

        skill_dep_ratio = round(dep_skills / total_skills, 4) if total_skills > 0 else 0.0
        skill_non_reuse_ratio = round(unreused_val / val_skills, 4) if val_skills > 0 else 0.0
        hyp_backlog_ratio = round(open_hyp / total_hyp, 4) if total_hyp > 0 else 0.0
        playbook_bloat = round(min(1.0, active_playbooks / 60.0), 4)
        goal_waste_ratio = round(waste_count / len(cycle_rows), 4) if cycle_rows else 0.0
        if recent_eff is not None and base_eff is not None and base_eff > 1e-9:
            ig_drop = round(max(0.0, min(1.0, 1.0 - (recent_eff / base_eff))), 4)
        else:
            ig_drop = 0.0

        metrics = {
            "skill_deprecation_ratio": skill_dep_ratio,
            "skill_non_reuse_ratio": skill_non_reuse_ratio,
            "hypothesis_backlog_ratio": hyp_backlog_ratio,
            "playbook_bloat_score": playbook_bloat,
            "goal_waste_ratio": goal_waste_ratio,
            "ig_token_efficiency_drop": ig_drop,
        }
        entropy_index = round(
            0.20 * skill_dep_ratio
            + 0.15 * skill_non_reuse_ratio
            + 0.20 * hyp_backlog_ratio
            + 0.15 * playbook_bloat
            + 0.15 * goal_waste_ratio
            + 0.15 * ig_drop,
            4,
        )
        return EntropySnapshot(
            entropy_index=entropy_index,
            metrics=metrics,
            raw_counts={
                "total_skills": total_skills,
                "deprecated_skills": dep_skills,
                "validated_skills": val_skills,
                "unreused_validated_skills": unreused_val,
                "total_hypotheses": total_hyp,
                "open_hypotheses": open_hyp,
                "active_playbooks": active_playbooks,
                "non_idle_cycles_sampled": len(cycle_rows),
                "wasted_cycles": waste_count,
                "recent_ig_per_1k_tok": round(recent_eff, 6) if recent_eff is not None else None,
                "baseline_ig_per_1k_tok": round(base_eff, 6) if base_eff is not None else None,
            },
        )
