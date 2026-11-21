"""Dynamic Self Model & Skill Registry package (V2.0 P1-7)."""

from app.self_model.interfaces import (
    BetaBinomialPosterior,
    CapabilityEvidenceModel,
    CapabilityMaturity,
    CapabilityProfileModel,
    SelfModelConfig,
    SelfModelInterface,
    SelfModelSnapshot,
    SkillDefinition,
    SkillRegistryInterface,
    SkillStatus,
    SkillValidationOutcome,
    compute_beta_binomial_stats,
)
from app.self_model.manager import SelfModelManager
from app.self_model.skills import SkillRegistryManager

__all__ = [
    "BetaBinomialPosterior",
    "CapabilityEvidenceModel",
    "CapabilityMaturity",
    "CapabilityProfileModel",
    "SelfModelConfig",
    "SelfModelInterface",
    "SelfModelManager",
    "SelfModelSnapshot",
    "SkillDefinition",
    "SkillRegistryInterface",
    "SkillRegistryManager",
    "SkillStatus",
    "SkillValidationOutcome",
    "compute_beta_binomial_stats",
]
