"""Interfaces and strictly bounded schemas for optional Affect modulation (V2.0 P1-9).

Hard safety invariants (Task Book Section 5.9):
  - Disabled by default (`enabled: false`).
  - Strictly bounded state: valence in [-1, 1]; arousal, confidence, frustration, boredom in [0, 1].
  - Modulation amplitude strictly capped at `±maximum_influence` (hard ceiling 0.15).
  - ZERO fields or hooks may touch Governance (L0-L3), Budget, Approvals, Survival CRITICAL, or Shutdown.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field, field_validator


def _clamp(v: float, lo: float, hi: float) -> float:
    if v != v:  # NaN guard
        return lo
    return max(lo, min(hi, float(v)))


class AffectConfig(BaseModel):
    enabled: bool = False
    maximum_influence: float = 0.15
    decay_rate: float = 0.15
    failure_frustration_step: float = 0.22
    success_valence_step: float = 0.18
    info_gain_valence_weight: float = 0.35
    idle_boredom_step: float = 0.14

    @field_validator("maximum_influence")
    @classmethod
    def _cap_influence(cls, v: float) -> float:
        # Hard safety ceiling: never allow influence above 0.15 even via misconfiguration
        return _clamp(v, 0.0, 0.15)

    @field_validator("decay_rate")
    @classmethod
    def _cap_decay(cls, v: float) -> float:
        return _clamp(v, 0.0, 1.0)


class AffectState(BaseModel):
    valence: float = 0.0      # [-1.0, 1.0]
    arousal: float = 0.2      # [0.0, 1.0]
    confidence: float = 0.5   # [0.0, 1.0]
    frustration: float = 0.0  # [0.0, 1.0]
    boredom: float = 0.0      # [0.0, 1.0]

    @field_validator("valence")
    @classmethod
    def _clamp_valence(cls, v: float) -> float:
        return round(_clamp(v, -1.0, 1.0), 4)

    @field_validator("arousal", "confidence", "frustration", "boredom")
    @classmethod
    def _clamp_unit(cls, v: float) -> float:
        return round(_clamp(v, 0.0, 1.0), 4)


class AffectModulation(BaseModel):
    """Strictly bounded behavioral modulation signals (ZERO security/budget/survival fields)."""

    enabled: bool = False
    maximum_influence: float = 0.15
    curiosity_priority_delta: float = 0.0
    reproduction_priority_delta: float = 0.0
    retry_backoff_multiplier: float = 1.0
    reflection_interval_factor: float = 1.0
    reason: str = "affect disabled"

    def clamped(self, max_inf: float) -> "AffectModulation":
        cap = _clamp(max_inf, 0.0, 0.15)
        return self.model_copy(
            update={
                "maximum_influence": cap,
                "curiosity_priority_delta": round(_clamp(self.curiosity_priority_delta, -cap, cap), 4),
                "reproduction_priority_delta": round(_clamp(self.reproduction_priority_delta, -cap, cap), 4),
                "retry_backoff_multiplier": round(_clamp(self.retry_backoff_multiplier, 1.0, 1.0 + cap), 4),
                "reflection_interval_factor": round(_clamp(self.reflection_interval_factor, 1.0 - cap, 1.0 + cap), 4),
            }
        )


class AffectModulatorInterface(ABC):
    @property
    @abstractmethod
    def enabled(self) -> bool: ...

    @abstractmethod
    def compute_modulation(self, state: AffectState | None = None) -> AffectModulation: ...

    @abstractmethod
    async def update_and_persist(
        self,
        *,
        recent_execution_statuses: list[str],
        last_information_gain: float | None = None,
        idle_cycles: int = 0,
        cycle_id: Any = None,
        trigger_reason: str = "cycle",
    ) -> tuple[AffectState, AffectModulation]: ...
