"""AOC V5.0 Evolutionary Reproduction package (M1: isolation, blank-slate spawn & culling GC)."""

from app.evolution.blank_slate import BlankSlateVerifier
from app.evolution.gc import CullingGC
from app.evolution.interfaces import (
    MAX_LESSON_SUMMARY_BYTES,
    BlankSlateCheckItem,
    BlankSlateReport,
    CullingReport,
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
from app.evolution.repository import EvolutionRepository
from app.evolution.spawner import InstanceSpawner

__all__ = [
    "BlankSlateCheckItem",
    "BlankSlateReport",
    "BlankSlateVerifier",
    "CullingGC",
    "CullingReport",
    "EvolutionRepository",
    "InstanceRole",
    "InstanceSpawner",
    "InstanceStatus",
    "LineageOutcome",
    "MAX_LESSON_SUMMARY_BYTES",
    "ProposalStage",
    "SpawnRequest",
    "SpawnResult",
    "build_instance_schema_name",
    "enforce_lesson_summary_budget",
    "is_droppable_instance_schema",
    "validate_instance_key",
]
