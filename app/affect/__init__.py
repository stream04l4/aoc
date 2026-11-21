"""Optional bounded Affect modulation layer (V2.0 P1-9, disabled by default)."""

from app.affect.interfaces import (
    AffectConfig,
    AffectModulation,
    AffectModulatorInterface,
    AffectState,
)
from app.affect.modulator import AffectModulator

__all__ = [
    "AffectConfig",
    "AffectModulation",
    "AffectModulator",
    "AffectModulatorInterface",
    "AffectState",
]
