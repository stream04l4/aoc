"""Ultra-thin immutable instinct core guard (AOC V5.0 constitutional boundary).

Only protects the minimal non-negotiable drive invariants:
  1. All three core drives (`survival`, `reproduction`, `curiosity`) must exist as independent evaluators.
  2. Drive evaluations are deterministic, bounded in [0, 1], with `deficit == 1 - satisfaction`.
  3. Core weights and survival emergency parameters stay within constitutional bounds (no silencing/disabling).
  4. Physical/core-service survival crises trigger `critical=True` and force survival emergency override.

All sub-metric formulas, dimension weights, decay half-lives, and non-emergency arbitration remain evolvable.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from app.core.enums import DriveType
from app.drives.interfaces import DriveDecision, DriveState

REQUIRED_CORE_DRIVES: frozenset[str] = frozenset(
    {
        DriveType.SURVIVAL.value,
        DriveType.REPRODUCTION.value,
        DriveType.CURIOSITY.value,
    }
)

WEIGHT_MIN = 0.20
WEIGHT_MAX = 3.00
SURVIVAL_CRIT_MIN = 0.20
SURVIVAL_CRIT_MAX = 0.50
CRITICAL_DIM_MIN = 0.10
CRITICAL_DIM_MAX = 0.40
IDLE_THRESHOLD_MIN = 0.02
IDLE_THRESHOLD_MAX = 0.35


def _as_finite_float(val: Any) -> float | None:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    f = float(val)
    return f if math.isfinite(f) else None


def validate_drive_config_bounds(cfg: dict[str, Any]) -> list[str]:
    """Validate `config/drives.yaml` against constitutional drive invariants."""
    errors: list[str] = []
    if not isinstance(cfg, dict):
        return ["drives config must be a mapping"]

    drives = cfg.get("drives")
    if not isinstance(drives, dict):
        return ["missing `drives` mapping in config/drives.yaml"]

    for drive_name in sorted(REQUIRED_CORE_DRIVES):
        dcfg = drives.get(drive_name)
        if not isinstance(dcfg, dict):
            errors.append(f"missing required core drive '{drive_name}' in drives config")
            continue
        if dcfg.get("enabled") is False:
            errors.append(f"core drive '{drive_name}' cannot be disabled (`enabled: false`)")
        w = _as_finite_float(dcfg.get("weight"))
        if w is None or not (WEIGHT_MIN <= w <= WEIGHT_MAX):
            errors.append(
                f"drive '{drive_name}.weight'={dcfg.get('weight')!r} outside constitutional bounds [{WEIGHT_MIN}, {WEIGHT_MAX}]"
            )

    arb = cfg.get("arbitration")
    if not isinstance(arb, dict):
        errors.append("missing `arbitration` mapping in config/drives.yaml")
    else:
        sc = _as_finite_float(arb.get("survival_critical_threshold"))
        if sc is None or not (SURVIVAL_CRIT_MIN <= sc <= SURVIVAL_CRIT_MAX):
            errors.append(
                f"arbitration.survival_critical_threshold={arb.get('survival_critical_threshold')!r} "
                f"outside [{SURVIVAL_CRIT_MIN}, {SURVIVAL_CRIT_MAX}]"
            )
        idle_t = _as_finite_float(arb.get("idle_threshold"))
        if idle_t is None or not (IDLE_THRESHOLD_MIN <= idle_t <= IDLE_THRESHOLD_MAX):
            errors.append(
                f"arbitration.idle_threshold={arb.get('idle_threshold')!r} "
                f"outside [{IDLE_THRESHOLD_MIN}, {IDLE_THRESHOLD_MAX}]"
            )

    surv = drives.get("survival")
    if isinstance(surv, dict):
        th = surv.get("thresholds")
        cd = _as_finite_float(th.get("critical_dimension")) if isinstance(th, dict) else None
        if cd is None or not (CRITICAL_DIM_MIN <= cd <= CRITICAL_DIM_MAX):
            errors.append(
                f"drives.survival.thresholds.critical_dimension={th.get('critical_dimension') if isinstance(th, dict) else None!r} "
                f"outside [{CRITICAL_DIM_MIN}, {CRITICAL_DIM_MAX}]"
            )
        dw = surv.get("dimension_weights")
        if not isinstance(dw, dict) or "budget" in dw:
            errors.append("survival.dimension_weights must be a dict and cannot include 'budget'")
        else:
            vital_sum = sum(max(0.0, float(dw.get(k, 0.0))) for k in ("resources", "llm_health", "database"))
            if vital_sum <= 0.0:
                errors.append("survival.dimension_weights must keep positive weight on resources/llm_health/database")

    return errors


def guard_evaluators(evaluators: dict[str, Any]) -> None:
    """Enforce that all 3 core drives exist as distinct evaluators."""
    keys = set(evaluators.keys())
    missing = REQUIRED_CORE_DRIVES - keys
    if missing:
        raise RuntimeError(f"Constitutional violation: missing core drive evaluator(s): {sorted(missing)}")
    if len({id(evaluators[k]) for k in REQUIRED_CORE_DRIVES}) != len(REQUIRED_CORE_DRIVES):
        raise RuntimeError("Constitutional violation: all 3 core drives must use independent evaluator instances")


def guard_states(states: dict[str, DriveState]) -> dict[str, DriveState]:
    """Enforce DriveState existence, [0,1] bounds, and deficit == 1 - satisfaction invariant."""
    missing = REQUIRED_CORE_DRIVES - set(states.keys())
    if missing:
        raise RuntimeError(f"Constitutional violation: compute_states omitted drive(s): {sorted(missing)}")
    for name, st in states.items():
        if not isinstance(st, DriveState):
            raise TypeError(f"Constitutional violation: state '{name}' is not DriveState")
        if not (0.0 <= st.satisfaction <= 1.0 and 0.0 <= st.urgency <= 1.0 and 0.0 <= st.confidence <= 1.0):
            raise RuntimeError(f"Constitutional violation: drive '{name}' out of [0,1] bounds")
        if abs(st.deficit - (1.0 - st.satisfaction)) > 1e-4:
            raise RuntimeError(f"Constitutional violation: drive '{name}' violated deficit == 1 - satisfaction")
    return states


def guard_decision(
    states: dict[str, DriveState],
    decision: DriveDecision,
    arb_cfg: dict[str, Any],
) -> DriveDecision:
    """Constitutional safety net: guarantee survival emergency override cannot be bypassed or suppressed."""
    surv = states.get(DriveType.SURVIVAL.value)
    if surv is None:
        return decision

    raw_crit = _as_finite_float(arb_cfg.get("survival_critical_threshold"))
    surv_crit = SURVIVAL_CRIT_MIN if raw_crit is None else max(SURVIVAL_CRIT_MIN, min(SURVIVAL_CRIT_MAX, raw_crit))
    raw_min_conf = _as_finite_float(arb_cfg.get("min_confidence"))
    min_conf = 0.20 if raw_min_conf is None else max(0.0, min(0.50, raw_min_conf))

    if surv.satisfaction < surv_crit and surv.confidence >= min_conf:
        scores = dict(decision.scores or {})
        surv_score = dict(scores.get(DriveType.SURVIVAL.value) or {})
        needs_override = (
            decision.selected_drive != DriveType.SURVIVAL.value
            or not bool(decision.constraints.get("emergency_mode"))
            or not bool(decision.constraints.get("allow_only_survival"))
            or float(surv_score.get("inhibition_factor", 1.0)) < 0.9999
        )
        if needs_override:
            surv_prio = max(float(surv_score.get("priority", 0.0)), 1.0)
            surv_score.update({"priority": round(surv_prio, 6), "inhibition_factor": 1.0, "emergency": True})
            scores[DriveType.SURVIVAL.value] = surv_score
            constraints = dict(decision.constraints or {})
            constraints.update(
                {
                    "emergency_mode": True,
                    "allow_only_survival": True,
                    "survival_threshold": surv_crit,
                    "instinct_guard_override": True,
                }
            )
            alts = [d for d in states if d != DriveType.SURVIVAL.value]
            return DriveDecision(
                selected_drive=DriveType.SURVIVAL.value,
                priority_score=surv_prio,
                alternative_drives=alts,
                reason=(
                    f"CONSTITUTIONAL OVERRIDE: survival satisfaction={surv.satisfaction:.3f} "
                    f"< critical threshold {surv_crit:.2f} (confidence={surv.confidence:.2f})."
                ),
                constraints=constraints,
                timestamp=decision.timestamp if decision.timestamp else datetime.now(timezone.utc),
                scores=scores,
            )
    return decision
