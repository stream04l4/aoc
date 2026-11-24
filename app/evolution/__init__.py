"""AOC V5.0 Evolutionary Reproduction package (M1-M5)."""

from app.evolution.arena import FitnessArena
from app.evolution.benchmarks import (
    ArenaBudgetTracker,
    BenchmarkRunnerInterface,
    HttpInstanceBenchmarkRunner,
    LocalSandboxBenchmarkRunner,
    build_standard_benchmark_suite,
)
from app.evolution.blank_slate import BlankSlateVerifier
from app.evolution.constitution_guard import CONSTITUTIONAL_FILES, EVOLVABLE_ALLOWLIST, ConstitutionGuard
from app.evolution.entropy_sensor import EntropySensor
from app.evolution.gc import CullingGC
from app.evolution.interfaces import (
    MAX_LESSON_SUMMARY_BYTES,
    ArenaComparisonVerdict,
    ArenaReport,
    ArenaTelemetry,
    BenchmarkTaskOutcome,
    BenchmarkTaskSpec,
    BlankSlateCheckItem,
    BlankSlateReport,
    ConstitutionLayerResult,
    ConstitutionVerdict,
    CullingReport,
    EntropySnapshot,
    FitnessScoreBreakdown,
    InstanceRole,
    InstanceStatus,
    LineageOutcome,
    ProposalStage,
    SpawnRequest,
    SpawnResult,
    SuccessionResult,
    build_instance_schema_name,
    enforce_lesson_summary_budget,
    is_droppable_instance_schema,
    validate_instance_key,
)
from app.evolution.proposal_manager import ProposalManager
from app.evolution.repository import EvolutionRepository
from app.evolution.scoring import compare_parent_vs_offspring, compute_fitness_score
from app.evolution.spawner import InstanceSpawner
from app.evolution.succession import SuccessionManager

__all__ = [
    "ArenaBudgetTracker",
    "ArenaComparisonVerdict",
    "ArenaReport",
    "ArenaTelemetry",
    "BenchmarkRunnerInterface",
    "BenchmarkTaskOutcome",
    "BenchmarkTaskSpec",
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
    "FitnessArena",
    "FitnessScoreBreakdown",
    "HttpInstanceBenchmarkRunner",
    "InstanceRole",
    "InstanceSpawner",
    "InstanceStatus",
    "LineageOutcome",
    "LocalSandboxBenchmarkRunner",
    "MAX_LESSON_SUMMARY_BYTES",
    "ProposalManager",
    "ProposalStage",
    "SpawnRequest",
    "SpawnResult",
    "SuccessionManager",
    "SuccessionResult",
    "build_instance_schema_name",
    "build_standard_benchmark_suite",
    "compare_parent_vs_offspring",
    "compute_fitness_score",
    "enforce_lesson_summary_budget",
    "is_droppable_instance_schema",
    "validate_instance_key",
]
