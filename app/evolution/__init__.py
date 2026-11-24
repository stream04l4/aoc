"""AOC V5.0 Evolutionary Reproduction package (M1-M2)."""

from app.evolution.blank_slate import BlankSlateVerifier
from app.evolution.constitution_guard import CONSTITUTIONAL_FILES, EVOLVABLE_ALLOWLIST, ConstitutionGuard
from app.evolution.entropy_sensor import EntropySensor
from app.evolution.gc import CullingGC
from app.evolution.interfaces import (
    MAX_LESSON_SUMMARY_BYTES,
    BlankSlateCheckItem,
    BlankSlateReport,
    ConstitutionLayerResult,
    ConstitutionVerdict,
    CullingReport,
    EntropySnapshot,
    InstanceRole,
    InstanceStatus,
    LineageOutcome,
    ProposalStage,
    SpawnRequest,
    SpawnResult,
    build_instance_schema_name,
    enforce_lesson_summary_budget,
    is_droppable_instance_schema,
    validate_instance_key,
)
from app.evolution.proposal_manager import ProposalManager
from app.evolution.repository import EvolutionRepository
from app.evolution.spawner import InstanceSpawner

__all__ = [
    "BlankSlateCheckItem",
    "BlankSlateReport",
    "BlankSlateVerifier",
    "CONSTITUTIONAL_FILES",
    "ConstitutionGuard",
    "ConstitutionLayerResult",
    "ConstitutionVerdict",
    "CullingGC",
    "CullingReport",
    "EVOLVABLE_ALLOWLIST",
    "EntropySensor",
    "EntropySnapshot",
    "EvolutionRepository",
    "InstanceRole",
    "InstanceSpawner",
    "InstanceStatus",
    "LineageOutcome",
    "MAX_LESSON_SUMMARY_BYTES",
    "ProposalManager",
    "ProposalStage",
    "SpawnRequest",
    "SpawnResult",
    "build_instance_schema_name",
    "enforce_lesson_summary_budget",
    "is_droppable_instance_schema",
    "validate_instance_key",
]
