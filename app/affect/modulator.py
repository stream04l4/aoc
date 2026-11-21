"""Deterministic, strictly bounded AffectModulator (V2.0 P1-9, disabled by default).

- Reads live state from `organism_state.emotion_reserved` and logs history to `affect_history`.
- When `enabled=False`: strictly no-op (zero priority delta, zero DB writes).
- On any internal error: `evaluate_safe()` degrades silently to neutral V1.0 policy.
- Strictly forbidden from touching Governance, L0–L3, Budget, Approvals, or Survival CRITICAL.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.affect.interfaces import (
    AffectConfig,
    AffectModulation,
    AffectModulatorInterface,
    AffectState,
)
from app.observability.logging import get_logger
from app.storage.repository import StateRepository

logger = get_logger("aoc.affect.modulator")


class AffectModulator(AffectModulatorInterface):
    def __init__(
        self,
        *,
        organism_id: uuid.UUID | None = None,
        repo: StateRepository | None = None,
        config: AffectConfig | dict[str, Any] | None = None,
    ) -> None:
        self.organism_id = organism_id
        self.repo = repo
        if isinstance(config, AffectConfig):
            self.cfg = config
        elif isinstance(config, dict):
            self.cfg = AffectConfig.model_validate(config)
        else:
            self.cfg = AffectConfig()
        self._state = AffectState()

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled)

    @property
    def state(self) -> AffectState:
        return self._state

    def summary(self) -> dict[str, Any]:
        return {
            "affect_enabled": self.enabled,
            "maximum_influence": self.cfg.maximum_influence,
            "state": self._state.model_dump(mode="json") if self.enabled else None,
        }

    def step_state(
        self,
        prev: AffectState,
        *,
        consecutive_failures: int = 0,
        recent_successes: int = 0,
        last_information_gain: float | None = None,
        idle_cycles: int = 0,
    ) -> tuple[AffectState, str]:
        """Pure deterministic event-driven state transition (strictly bounded)."""
        d = self.cfg.decay_rate
        # Decay smoothly toward neutral baseline (valence=0, arousal=0.2, confidence=0.5, frustration=0, boredom=0)
        valence = prev.valence * (1.0 - d)
        arousal = 0.2 + (prev.arousal - 0.2) * (1.0 - d)
        confidence = 0.5 + (prev.confidence - 0.5) * (1.0 - d)
        frustration = prev.frustration * (1.0 - d)
        boredom = prev.boredom * (1.0 - d)

        reasons: list[str] = []
        if consecutive_failures > 0:
            step = self.cfg.failure_frustration_step * min(consecutive_failures, 4)
            frustration += step
            valence -= 0.5 * step
            confidence -= 0.4 * step
            arousal += 0.3 * step
            reasons.append(f"consecutive_failures={consecutive_failures}")
        elif recent_successes > 0:
            gain_lift = max(0.0, float(last_information_gain or 0.0)) * self.cfg.info_gain_valence_weight
            valence += self.cfg.success_valence_step + gain_lift
            confidence += 0.15
            frustration *= 0.5
            boredom *= 0.4
            reasons.append(f"recent_successes={recent_successes},info_gain={round(float(last_information_gain or 0.0), 3)}")

        if idle_cycles > 0:
            boredom += self.cfg.idle_boredom_step * min(idle_cycles, 5)
            arousal -= 0.05 * min(idle_cycles, 4)
            reasons.append(f"idle_cycles={idle_cycles}")

        new_state = AffectState(
            valence=valence,
            arousal=arousal,
            confidence=confidence,
            frustration=frustration,
            boredom=boredom,
        )
        return new_state, ("; ".join(reasons) or "neutral_decay")

    def compute_modulation(self, state: AffectState | None = None) -> AffectModulation:
        """Map bounded AffectState to bounded behavioral modulation within ±maximum_influence."""
        if not self.enabled:
            return AffectModulation(enabled=False, maximum_influence=self.cfg.maximum_influence, reason="affect disabled")

        st = state or self._state
        max_inf = self.cfg.maximum_influence

        # Boredom gently lifts curiosity exploration; high frustration gently damps exploration & extends retry backoff
        curiosity_raw = (0.65 * st.boredom + 0.25 * max(0.0, st.valence) - 0.60 * st.frustration) * max_inf
        reproduction_raw = (0.45 * st.confidence - 0.25 - 0.50 * st.frustration) * max_inf
        backoff_mult = 1.0 + st.frustration * max_inf
        reflection_factor = 1.0 - 0.5 * st.frustration * max_inf + 0.5 * st.boredom * max_inf

        return AffectModulation(
            enabled=True,
            maximum_influence=max_inf,
            curiosity_priority_delta=curiosity_raw,
            reproduction_priority_delta=reproduction_raw,
            retry_backoff_multiplier=backoff_mult,
            reflection_interval_factor=reflection_factor,
            reason=(
                f"valence={st.valence:.2f},frustration={st.frustration:.2f},"
                f"boredom={st.boredom:.2f},confidence={st.confidence:.2f}"
            ),
        ).clamped(max_inf)

    async def load_from_db(self) -> AffectState:
        if not self.enabled or self.repo is None or self.organism_id is None:
            return self._state
        try:
            st_row = await self.repo.get_organism_state(self.organism_id)
            if st_row and isinstance(st_row.get("emotion_reserved"), dict) and st_row["emotion_reserved"]:
                self._state = AffectState.model_validate(st_row["emotion_reserved"])
        except Exception as exc:
            logger.warning("affect.load_degraded", error=str(exc))
        return self._state

    async def update_and_persist(
        self,
        *,
        recent_execution_statuses: list[str],
        last_information_gain: float | None = None,
        idle_cycles: int = 0,
        cycle_id: uuid.UUID | None = None,
        trigger_reason: str = "cycle",
    ) -> tuple[AffectState, AffectModulation]:
        if not self.enabled:
            return self._state, AffectModulation(
                enabled=False, maximum_influence=self.cfg.maximum_influence, reason="affect disabled"
            )

        # Count trailing consecutive failures from most recent executions
        consec_fail = 0
        for st in recent_execution_statuses:
            if st in {"FAILED", "TIMEOUT", "ORPHANED"}:
                consec_fail += 1
            else:
                break
        recent_ok = sum(1 for st in recent_execution_statuses[:3] if st == "SUCCEEDED")

        new_state, calc_reason = self.step_state(
            self._state,
            consecutive_failures=consec_fail,
            recent_successes=recent_ok,
            last_information_gain=last_information_gain,
            idle_cycles=idle_cycles,
        )
        self._state = new_state
        mod = self.compute_modulation(new_state)

        if self.repo is not None and self.organism_id is not None:
            await self.repo.update_organism_state(
                self.organism_id,
                emotion_reserved=new_state.model_dump(mode="json"),
            )
            await self.repo.record_affect_history(
                organism_id=self.organism_id,
                cycle_id=cycle_id,
                valence=new_state.valence,
                arousal=new_state.arousal,
                confidence=new_state.confidence,
                frustration=new_state.frustration,
                boredom=new_state.boredom,
                modulation_applied=mod.model_dump(mode="json"),
                trigger_reason=f"{trigger_reason}: {calc_reason}",
            )

        return new_state, mod

    async def evaluate_safe(
        self,
        *,
        recent_execution_statuses: list[str],
        last_information_gain: float | None = None,
        idle_cycles: int = 0,
        cycle_id: uuid.UUID | None = None,
        trigger_reason: str = "cycle",
    ) -> AffectModulation:
        """Fault-isolated entrypoint for AutonomousLoop: NEVER raises; falls back to neutral V1.0 policy."""
        if not self.enabled:
            return AffectModulation(enabled=False, maximum_influence=self.cfg.maximum_influence, reason="affect disabled")
        try:
            _st, mod = await self.update_and_persist(
                recent_execution_statuses=recent_execution_statuses,
                last_information_gain=last_information_gain,
                idle_cycles=idle_cycles,
                cycle_id=cycle_id,
                trigger_reason=trigger_reason,
            )
            return mod
        except Exception as exc:
            logger.warning("affect.fault_degraded_to_v1", error=str(exc))
            return AffectModulation(
                enabled=False,
                maximum_influence=self.cfg.maximum_influence,
                reason=f"fault_degraded_v1:{exc}",
            )
