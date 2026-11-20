"""Satisfaction calculator & temporal dynamics (persistence, trend, satiation)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.drives.interfaces import DriveEvaluation, DriveState, clamp01, clamp_signed


class SatisfactionCalculator:
    """Turns a DriveEvaluation + previous DriveState into a new DriveState."""

    def __init__(self, dynamics_cfg: dict[str, Any], drives_cfg: dict[str, Any]) -> None:
        self.dyn = dynamics_cfg
        self.drives_cfg = drives_cfg

    def compute(
        self,
        evaluation: DriveEvaluation,
        previous: DriveState | None,
        now: datetime,
    ) -> DriveState:
        unmet_threshold = float(self.dyn.get("unmet_threshold", 0.35))
        growth = float(self.dyn.get("persistence_growth", 0.15))
        decay = float(self.dyn.get("persistence_decay", 0.6))
        alpha = clamp01(float(self.dyn.get("trend_smoothing", 0.5)))

        sat = clamp01(evaluation.satisfaction)
        deficit = clamp01(1.0 - sat)

        if previous is None:
            persistence = clamp01(growth * deficit) if deficit > unmet_threshold else 0.0
            trend = 0.0
            last_satisfied_at = now if deficit <= unmet_threshold else None
        else:
            delta = sat - previous.satisfaction
            trend = clamp_signed(alpha * delta + (1.0 - alpha) * previous.trend)

            if deficit > unmet_threshold:
                persistence = clamp01(previous.persistence + growth * deficit)
            else:
                persistence = clamp01(previous.persistence * decay)

            # Record satiation timestamp when satisfaction meaningfully improved
            # or crossed into the satisfied zone from an unmet state.
            was_unmet = previous.deficit > unmet_threshold
            now_met = deficit <= unmet_threshold
            if delta >= 0.05 or (was_unmet and now_met):
                last_satisfied_at = now
            else:
                last_satisfied_at = previous.last_satisfied_at

        # Falling trend boosts urgency (configured per drive)
        drive_cfg = self.drives_cfg.get(evaluation.drive_type, {})
        trend_gain = float(drive_cfg.get("urgency_trend_gain", 0.0))
        urgency = evaluation.urgency
        if not evaluation.critical and trend < 0 and trend_gain > 0:
            urgency = clamp01(urgency + trend_gain * (-trend))

        return DriveState(
            drive_type=evaluation.drive_type,
            satisfaction=sat,
            deficit=deficit,
            urgency=urgency,
            confidence=evaluation.confidence,
            persistence=persistence,
            trend=trend,
            estimated_cost=evaluation.estimated_cost,
            estimated_risk=evaluation.estimated_risk,
            updated_at=now,
            last_satisfied_at=last_satisfied_at,
            dimensions=evaluation.dimensions,
            notes=evaluation.notes,
        )
