"""Hybrid Drive Arbitration (task book 4.5).

Base score:
    base = deficit * urgency * confidence * drive_weight

Full priority score:
    priority = base * (1 + persistence_gain * persistence) * inhibition(t)
               - cost_penalty * estimated_cost
               - risk_penalty * estimated_risk

Plus:
  - Survival emergency protection when survival satisfaction < survival_critical_threshold
  - Confidence gate (drives below min_confidence cannot win)
  - IDLE selection ("none") when all eligible drives fall below idle_threshold
  - Full audit reason explaining both the selected drive and every rejected alternative.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from app.core.enums import NO_DRIVE, DriveType
from app.drives.interfaces import DriveDecision, DriveState
from app.perception.interfaces import utcnow


class DriveArbitrator:
    def __init__(self, arb_cfg: dict[str, Any], drives_cfg: dict[str, Any]) -> None:
        self.arb = arb_cfg
        self.drives_cfg = drives_cfg

    def arbitrate(
        self,
        states: dict[str, DriveState],
        now: datetime | None = None,
        constraints: dict[str, Any] | None = None,
    ) -> DriveDecision:
        now = now or utcnow()
        out_constraints = dict(constraints or {})

        idle_threshold = float(self.arb.get("idle_threshold", 0.12))
        persistence_gain = float(self.arb.get("persistence_gain", 0.6))
        inhib_strength = float(self.arb.get("inhibition_strength", 0.6))
        inhib_tau = max(1.0, float(self.arb.get("inhibition_tau_seconds", 1800)))
        cost_penalty = float(self.arb.get("cost_penalty", 0.10))
        risk_penalty = float(self.arb.get("risk_penalty", 0.15))
        surv_crit = float(self.arb.get("survival_critical_threshold", 0.30))
        min_conf = float(self.arb.get("min_confidence", 0.20))

        scores: dict[str, dict[str, Any]] = {}
        for dtype, st in states.items():
            d_weight = float(self.drives_cfg.get(dtype, {}).get("weight", 1.0))
            base = st.deficit * st.urgency * st.confidence * d_weight
            p_factor = 1.0 + persistence_gain * st.persistence

            inhib_factor = 1.0
            if st.last_satisfied_at is not None:
                dt = max(0.0, (now - st.last_satisfied_at).total_seconds())
                inhib_factor = 1.0 - inhib_strength * math.exp(-dt / inhib_tau)

            # Survival emergency bypasses inhibition so acute threats are never suppressed
            is_surv_emergency = (
                dtype == DriveType.SURVIVAL.value
                and st.satisfaction < surv_crit
                and st.confidence >= min_conf
            )
            eff_inhib = 1.0 if is_surv_emergency else inhib_factor

            raw = (
                base * p_factor * eff_inhib
                - cost_penalty * st.estimated_cost
                - risk_penalty * st.estimated_risk
            )
            priority = max(0.0, raw)
            eligible = st.confidence >= min_conf

            scores[dtype] = {
                "priority": round(priority, 6),
                "base": round(base, 6),
                "drive_weight": d_weight,
                "persistence_factor": round(p_factor, 4),
                "inhibition_factor": round(eff_inhib, 4),
                "cost_penalty": round(cost_penalty * st.estimated_cost, 4),
                "risk_penalty": round(risk_penalty * st.estimated_risk, 4),
                "satisfaction": round(st.satisfaction, 4),
                "deficit": round(st.deficit, 4),
                "urgency": round(st.urgency, 4),
                "confidence": round(st.confidence, 4),
                "eligible": eligible,
                "emergency": is_surv_emergency,
            }

        # 1. Survival emergency protection
        surv_state = states.get(DriveType.SURVIVAL.value)
        if (
            surv_state is not None
            and surv_state.satisfaction < surv_crit
            and surv_state.confidence >= min_conf
        ):
            alternatives = [d for d in states if d != DriveType.SURVIVAL.value]
            surv_prio = max(scores[DriveType.SURVIVAL.value]["priority"], 1.0)
            scores[DriveType.SURVIVAL.value]["priority"] = round(surv_prio, 6)
            out_constraints.update(
                {
                    "emergency_mode": True,
                    "allow_only_survival": True,
                    "survival_threshold": surv_crit,
                }
            )
            alt_desc = "; ".join(
                f"{d} deferred (priority={scores[d]['priority']:.3f}, overridden by survival emergency)"
                for d in alternatives
            )
            reason = (
                f"EMERGENCY survival protection triggered: survival satisfaction="
                f"{surv_state.satisfaction:.3f} < threshold {surv_crit:.2f} "
                f"(confidence={surv_state.confidence:.2f}). "
                f"Alternatives: {alt_desc or 'none'}."
            )
            return DriveDecision(
                selected_drive=DriveType.SURVIVAL.value,
                priority_score=surv_prio,
                alternative_drives=alternatives,
                reason=reason,
                constraints=out_constraints,
                timestamp=now,
                scores=scores,
            )

        # 2. Rank eligible drives (tie-break deterministically: survival > curiosity > reproduction)
        tie_order = {
            DriveType.SURVIVAL.value: 0,
            DriveType.CURIOSITY.value: 1,
            DriveType.REPRODUCTION.value: 2,
        }
        eligible_drives = [d for d, info in scores.items() if info["eligible"]]
        ranked = sorted(
            eligible_drives,
            key=lambda d: (-scores[d]["priority"], tie_order.get(d, 99), d),
        )

        # 3. Check IDLE condition (no drive eligible or top priority below threshold)
        if not ranked or scores[ranked[0]]["priority"] < idle_threshold:
            all_drives = list(states.keys())
            details = "; ".join(
                f"{d}: priority={scores[d]['priority']:.3f}"
                + (
                    f" (ineligible: confidence={scores[d]['confidence']:.2f} < {min_conf:.2f})"
                    if not scores[d]["eligible"]
                    else f" (< idle_threshold {idle_threshold:.2f}, sat={scores[d]['satisfaction']:.2f})"
                )
                for d in all_drives
            )
            out_constraints["idle_reason"] = "below_threshold_or_low_confidence"
            return DriveDecision(
                selected_drive=NO_DRIVE,
                priority_score=scores[ranked[0]]["priority"] if ranked else 0.0,
                alternative_drives=all_drives,
                reason=f"No drive selected (IDLE): {details}.",
                constraints=out_constraints,
                timestamp=now,
                scores=scores,
            )

        # 4. Normal winner selection
        winner = ranked[0]
        win_info = scores[winner]
        alternatives = [d for d in states if d != winner]
        alt_reasons = []
        for d in alternatives:
            info = scores[d]
            if not info["eligible"]:
                alt_reasons.append(
                    f"{d} rejected (confidence={info['confidence']:.2f} < min_confidence {min_conf:.2f})"
                )
            else:
                alt_reasons.append(
                    f"{d} not selected (priority={info['priority']:.3f} < {win_info['priority']:.3f}, "
                    f"sat={info['satisfaction']:.2f}, inhib={info['inhibition_factor']:.2f})"
                )

        reason = (
            f"Selected '{winner}' with priority={win_info['priority']:.3f} "
            f"(deficit={win_info['deficit']:.2f}, urgency={win_info['urgency']:.2f}, "
            f"conf={win_info['confidence']:.2f}, weight={win_info['drive_weight']:.2f}, "
            f"persist_x={win_info['persistence_factor']:.2f}, inhib_x={win_info['inhibition_factor']:.2f}). "
            f"Rejected alternatives: {'; '.join(alt_reasons) or 'none'}."
        )
        return DriveDecision(
            selected_drive=winner,
            priority_score=win_info["priority"],
            alternative_drives=alternatives,
            reason=reason,
            constraints=out_constraints,
            timestamp=now,
            scores=scores,
        )
