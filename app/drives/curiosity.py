"""Curiosity Drive Evaluator (task book 4.3).

Evaluates knowledge exploration and learning progress across six dimensions:
  - knowledge_gaps: open knowledge gaps (environment + LLM assessment)
  - unknown_information: unexamined topics/events in digital environment
  - exploration_completion: ratio of recent exploration attempts that succeeded
  - new_knowledge: recently acquired semantic memory count
  - information_gain: time-decayed measured information gain
  - learning_progress: recency of last verified learning event
"""

from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any


def _ensure_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

from app.core.enums import DriveType
from app.drives.interfaces import DriveContext, DriveEvaluation, DriveEvaluator, clamp01
from app.drives.scoring import aggregate_dimensions


class CuriosityDriveEvaluator(DriveEvaluator):
    drive_type = DriveType.CURIOSITY.value

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg

    def evaluate(self, ctx: DriveContext) -> DriveEvaluation:
        targets = self.cfg.get("targets", {})
        notes: list[str] = []

        # 1. Knowledge gaps (importance-weighted count)
        max_gaps = max(1.0, float(targets.get("max_gaps", 5)))
        weighted_gaps = sum(clamp01(g.importance) for g in ctx.open_knowledge_gaps)
        # Every gap carries at least 0.4 weight so low-importance gaps still register
        effective_gaps = sum(max(0.4, clamp01(g.importance)) for g in ctx.open_knowledge_gaps)
        gap_score = clamp01(1.0 - effective_gaps / max_gaps)
        if ctx.open_knowledge_gaps:
            notes.append(f"{len(ctx.open_knowledge_gaps)} open knowledge gap(s) (eff={effective_gaps:.1f})")

        # 2. Unknown information count (from context + environment_state open topics)
        max_unknown = max(1.0, float(targets.get("max_unknown", 5)))
        env_unknown = []
        env_state = ctx.observation.environment_state or {}
        if isinstance(env_state.get("unknown_topics"), list):
            env_unknown = env_state["unknown_topics"]
        unknown_keys = {u.topic for u in ctx.unknown_topics} | {
            (t.get("topic") or t.get("key") or str(t)) if isinstance(t, dict) else str(t)
            for t in env_unknown
        }
        unknown_score = clamp01(1.0 - len(unknown_keys) / max_unknown)
        if unknown_keys:
            notes.append(f"{len(unknown_keys)} unknown topic(s) in environment")

        # 3. Exploration task completion
        if ctx.exploration_attempted_recent > 0:
            exp_score: float | None = clamp01(
                ctx.exploration_succeeded_recent / ctx.exploration_attempted_recent
            )
        else:
            # If no gaps exist and nothing needed exploration, exploration completion is high;
            # if gaps exist but nothing has been attempted yet, it's lower.
            exp_score = 0.85 if not (ctx.open_knowledge_gaps or unknown_keys) else 0.35

        # 4. New knowledge acquired in rolling window
        new_k_target = max(1.0, float(targets.get("new_knowledge_window", 5)))
        new_knowledge_score: float | None = None
        if ctx.new_knowledge_recent is not None:
            new_knowledge_score = clamp01(ctx.new_knowledge_recent / new_k_target)

        # 5. Information gain (time-decayed)
        ig_half_life = max(0.5, float(targets.get("information_gain_half_life_hours", 24)))
        ig_score: float | None = None
        if ctx.last_information_gain is not None:
            decay = 1.0
            if ctx.last_information_gain_at is not None:
                dt_hours = max(0.0, (_ensure_utc(ctx.now) - _ensure_utc(ctx.last_information_gain_at)).total_seconds() / 3600.0)
                decay = math.pow(0.5, dt_hours / ig_half_life)
            ig_score = clamp01(clamp01(ctx.last_information_gain) * decay)

        # 6. Learning progress (recency of last learning event)
        learn_half_life = max(0.5, float(targets.get("learning_half_life_hours", 12)))
        learning_score: float | None = None
        if ctx.last_learning_at is not None:
            dt_hours = max(0.0, (_ensure_utc(ctx.now) - _ensure_utc(ctx.last_learning_at)).total_seconds() / 3600.0)
            learning_score = clamp01(math.pow(0.5, dt_hours / learn_half_life))

        dimensions: dict[str, float | None] = {
            "knowledge_gaps": gap_score,
            "unknown_information": unknown_score,
            "exploration_completion": exp_score,
            "new_knowledge": new_knowledge_score,
            "information_gain": ig_score,
            "learning_progress": learning_score,
        }

        weights = self.cfg.get(
            "dimension_weights",
            {
                "knowledge_gaps": 0.25,
                "unknown_information": 0.20,
                "exploration_completion": 0.15,
                "new_knowledge": 0.15,
                "information_gain": 0.10,
                "learning_progress": 0.15,
            },
        )
        cur_agg = self.cfg.get("aggregation", {}) if isinstance(self.cfg.get("aggregation"), dict) else {}
        cur_beta = float(cur_agg.get("softmin_beta", 0.8))
        default_cur_gates: dict[str, dict[str, Any]] = {
            "knowledge_gaps": {"threshold": 0.50, "lethal": 0.0, "gamma": 1.2, "floor": 0.35},
            "unknown_information": {"threshold": 0.50, "lethal": 0.0, "gamma": 1.2, "floor": 0.55},
        }
        cfg_cur_gates = cur_agg.get("gates") if isinstance(cur_agg.get("gates"), dict) else {}
        cur_gates = {**default_cur_gates, **cfg_cur_gates}
        satisfaction, data_conf, w_notes = aggregate_dimensions(
            dimensions,
            weights,
            default_when_all_missing=0.50,
            softmin_beta=cur_beta,
            gates=cur_gates,
        )
        notes.extend(w_notes)

        # If gaps or unknown topics are present, ensure curiosity deficit responds visibly
        if (ctx.open_knowledge_gaps or unknown_keys) and weighted_gaps > 0:
            # Cap satisfaction when significant open gaps exist
            gap_cap = clamp01(1.0 - min(0.65, 0.20 * len(ctx.open_knowledge_gaps) + 0.15 * len(unknown_keys)))
            satisfaction = min(satisfaction, max(gap_cap, 0.10))

        conf_factor = max(0.2, ctx.observation.confidence)
        if ctx.gap_assessment_confidence is not None:
            conf_factor = 0.5 * conf_factor + 0.5 * clamp01(ctx.gap_assessment_confidence)
        confidence = clamp01(data_conf * conf_factor) if data_conf > 0 else 0.0

        deficit = 1.0 - satisfaction
        base_u = float(self.cfg.get("base_urgency", 0.15))
        u_gain = float(self.cfg.get("urgency_deficit_gain", 0.6))
        urgency = clamp01(base_u + u_gain * deficit)

        return DriveEvaluation(
            drive_type=self.drive_type,
            satisfaction=satisfaction,
            urgency=urgency,
            confidence=confidence,
            estimated_cost=clamp01(float(self.cfg.get("estimated_cost", 0.3))),
            estimated_risk=clamp01(float(self.cfg.get("estimated_risk", 0.1))),
            dimensions=dimensions,
            notes=notes,
            critical=False,
        )
