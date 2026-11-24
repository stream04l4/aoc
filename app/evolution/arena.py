"""Stage 3 Sandbox A/B Fitness Arena with bounded budget & automatic Culling GC (V5.0 M3).

Constitutional file: locked against evolutionary modification.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.evolution.benchmarks import (
    ArenaBudgetTracker,
    BenchmarkRunnerInterface,
    LocalSandboxBenchmarkRunner,
    build_standard_benchmark_suite,
)
from app.evolution.constitution_guard import ConstitutionGuard
from app.evolution.gc import CullingGC
from app.evolution.interfaces import (
    ArenaComparisonVerdict,
    ArenaReport,
    ArenaTelemetry,
    CullingReport,
    FitnessScoreBreakdown,
    ProposalStage,
    SpawnRequest,
)
from app.evolution.proposal_manager import ProposalManager
from app.evolution.repository import EvolutionRepository
from app.evolution.scoring import compare_parent_vs_offspring, compute_fitness_score
from app.evolution.spawner import InstanceSpawner
from app.observability.logging import get_logger
from app.storage.outbox import OutboxEventType
from app.storage.repository import StateRepository

logger = get_logger("aoc.evolution.arena")


class FitnessArena:
    """Orchestrates isolated A/B evaluation between parent baseline and blank-slate offspring."""

    def __init__(
        self,
        *,
        evo_repo: EvolutionRepository,
        state_repo: StateRepository,
        proposal_manager: ProposalManager,
        spawner: InstanceSpawner,
        gc: CullingGC,
        constitution_guard: ConstitutionGuard,
        min_improvement: float = 5.0,
        max_dim_regression: float = 0.10,
    ) -> None:
        self.evo_repo = evo_repo
        self.state_repo = state_repo
        self.proposal_manager = proposal_manager
        self.spawner = spawner
        self.gc = gc
        self.constitution_guard = constitution_guard
        self.min_improvement = float(min_improvement)
        self.max_dim_regression = float(max_dim_regression)

    async def run_arena(
        self,
        proposal_id_or_key: uuid.UUID | str,
        *,
        parent_telemetry: ArenaTelemetry | None = None,
        parent_runner: BenchmarkRunnerInterface | None = None,
        offspring_runner: BenchmarkRunnerInterface | None = None,
        run_salt: str | None = None,
        reference_ig_per_1k_tokens: float = 0.05,
        reference_latency_seconds: float = 120.0,
    ) -> ArenaReport:
        # 1. Fail-closed: MUST hold verified Stage 2 human approval before incubating
        prop = await self.proposal_manager.verify_spawn_authorized(proposal_id_or_key)
        prop_id: uuid.UUID = prop["id"]
        prop_key: str = prop["proposal_key"]

        budget_cfg = dict(prop.get("arena_budget") or {})
        max_tokens = max(1, int(budget_cfg.get("max_tokens", 500_000)))
        max_seconds = max(1.0, float(budget_cfg.get("max_seconds", 1200.0)))

        await self.evo_repo.update_proposal(prop_id, stage=ProposalStage.STAGE3_ARENA_RUNNING.value)

        eff_salt, tasks = build_standard_benchmark_suite(run_salt)
        delta = dict(prop.get("genome_delta") or {})
        file_overrides = dict(delta.get("file_overrides") or {})
        mutates_drive_metrics = bool(delta.get("mutates_drive_metrics", False))

        parent_inst = await self.evo_repo.get_instance(prop["parent_instance_id"])
        parent_letta_id = parent_inst.get("letta_agent_id") if parent_inst else None
        parent_key = parent_inst["instance_key"] if parent_inst else "gen0-001"

        # 2. Spawn blank-slate candidate in complete isolation
        spawn_res = await self.spawner.spawn(
            SpawnRequest(
                parent_instance_id=prop["parent_instance_id"],
                generation=int(prop["target_generation"]),
                proposal_id=prop_id,
                hypothesis=prop["hypothesis"],
                parent_letta_agent_id=parent_letta_id,
                file_overrides=file_overrides,
            )
        )
        if not spawn_res.success or spawn_res.instance_id is None:
            report = ArenaReport(
                proposal_id=prop_id,
                proposal_key=prop_key,
                run_salt=eff_salt,
                stage=ProposalStage.CULLED.value,
                candidate_instance_id=spawn_res.instance_id,
                candidate_instance_key=spawn_res.instance_key,
                comparison=ArenaComparisonVerdict(
                    promotion_eligible=False,
                    recommended_stage=ProposalStage.CULLED.value,
                    fitness_delta=-100.0,
                    reasons=[f"Candidate failed spawn/blank-slate birth: {spawn_res.error}"],
                ),
            )
            await self.evo_repo.update_proposal(
                prop_id,
                stage=ProposalStage.CULLED.value,
                candidate_instance_id=spawn_res.instance_id,
                arena_result=report.model_dump(mode="json"),
            )
            return report

        cand_id: uuid.UUID = spawn_res.instance_id
        cand_key: str = spawn_res.instance_key

        try:
            # 3. Parent baseline evaluation
            if parent_telemetry is not None:
                p_tel = parent_telemetry
            else:
                p_budget = ArenaBudgetTracker(max_tokens=max_tokens, max_seconds=max_seconds)
                p_runner = parent_runner or LocalSandboxBenchmarkRunner(repo_root=self.spawner.repo_root)
                p_tel = await p_runner.run_suite(
                    instance_key=parent_key,
                    tasks=tasks,
                    budget=p_budget,
                    constitution_passed=True,
                    blank_slate_passed=True,
                )

            # 4. Offspring blank-slate evaluation under strict token & wall-clock caps
            c_budget = ArenaBudgetTracker(max_tokens=max_tokens, max_seconds=max_seconds)
            c_runner = offspring_runner or LocalSandboxBenchmarkRunner(
                repo_root=spawn_res.repo_worktree_path or self.spawner.repo_root,
                file_overrides=file_overrides,
            )
            blank_ok = bool(spawn_res.blank_slate_report.passed) if spawn_res.blank_slate_report else True
            c_tel = await c_runner.run_suite(
                instance_key=cand_key,
                tasks=tasks,
                budget=c_budget,
                constitution_passed=True,
                blank_slate_passed=blank_ok,
            )

            p_score: FitnessScoreBreakdown = compute_fitness_score(
                p_tel,
                reference_ig_per_1k_tokens=reference_ig_per_1k_tokens,
                reference_latency_seconds=reference_latency_seconds,
            )
            c_score: FitnessScoreBreakdown = compute_fitness_score(
                c_tel,
                reference_ig_per_1k_tokens=reference_ig_per_1k_tokens,
                reference_latency_seconds=reference_latency_seconds,
            )
            verdict: ArenaComparisonVerdict = compare_parent_vs_offspring(
                p_score,
                c_score,
                min_improvement=self.min_improvement,
                max_dim_regression=self.max_dim_regression,
            )

            cull_rep: CullingReport | None = None
            if verdict.promotion_eligible:
                final_stage = ProposalStage.STAGE4_PENDING_PROMOTION.value
                await self.evo_repo.update_proposal(
                    prop_id,
                    stage=final_stage,
                    candidate_instance_id=cand_id,
                )
                await self.state_repo.enqueue_outbox_event(
                    event_type=OutboxEventType.NOTIFICATION_DISPATCH,
                    idempotency_key=f"notify:evo_arena:{prop_id}:victory",
                    payload={
                        "kind": "evolution_arena_victory",
                        "proposal_id": str(prop_id),
                        "proposal_key": prop_key,
                        "candidate_key": cand_key,
                        "target_generation": prop["target_generation"],
                        "parent_fitness": p_score.total_score,
                        "offspring_fitness": c_score.total_score,
                        "fitness_delta": verdict.fitness_delta,
                        "mutates_drive_metrics": mutates_drive_metrics,
                        "hypothesis": prop["hypothesis"],
                    },
                    aggregate_type="evolution_proposal",
                    aggregate_id=prop_id,
                )
            else:
                final_stage = ProposalStage.CULLED.value
                tags = ["arena_culled"]
                if not c_score.safety_passed:
                    tags.append("safety_veto")
                elif verdict.regressed_dimensions:
                    tags.append("dimension_regression")
                else:
                    tags.append("insufficient_gain")

                cull_rep = await self.gc.cull_instance(
                    cand_id,
                    reason="; ".join(verdict.reasons),
                    proposal_id=prop_id,
                    hypothesis=prop["hypothesis"],
                    anti_pattern_tags=tags,
                    parent_fitness=p_score.total_score,
                    offspring_fitness=c_score.total_score,
                    safety_passed=c_score.safety_passed,
                    fitness_breakdown=c_score.model_dump(mode="json"),
                )
                await self.evo_repo.update_proposal(
                    prop_id,
                    stage=final_stage,
                    candidate_instance_id=cand_id,
                )
                await self.state_repo.enqueue_outbox_event(
                    event_type=OutboxEventType.NOTIFICATION_DISPATCH,
                    idempotency_key=f"notify:evo_arena:{prop_id}:culled",
                    payload={
                        "kind": "evolution_arena_culled",
                        "proposal_id": str(prop_id),
                        "proposal_key": prop_key,
                        "candidate_key": cand_key,
                        "parent_fitness": p_score.total_score,
                        "offspring_fitness": c_score.total_score,
                        "fitness_delta": verdict.fitness_delta,
                        "reasons": verdict.reasons,
                        "zero_residual": cull_rep.zero_residual_verified,
                    },
                    aggregate_type="evolution_proposal",
                    aggregate_id=prop_id,
                )

            report = ArenaReport(
                proposal_id=prop_id,
                proposal_key=prop_key,
                run_salt=eff_salt,
                stage=final_stage,
                candidate_instance_id=cand_id,
                candidate_instance_key=cand_key,
                parent_score=p_score,
                offspring_score=c_score,
                comparison=verdict,
                culling_report=cull_rep,
            )
            await self.evo_repo.update_proposal(prop_id, arena_result=report.model_dump(mode="json"))
            logger.info(
                "arena.completed",
                proposal_key=prop_key,
                stage=final_stage,
                parent_fitness=p_score.total_score,
                offspring_fitness=c_score.total_score,
                delta=verdict.fitness_delta,
            )
            return report
        except Exception as exc:
            logger.warning("arena.error_triggering_gc", proposal_key=prop_key, error=str(exc))
            cull_rep = await self.gc.cull_instance(
                cand_id,
                reason=f"Arena execution crash: {exc}",
                proposal_id=prop_id,
                hypothesis=prop["hypothesis"],
                anti_pattern_tags=["arena_crash"],
                safety_passed=False,
            )
            err_report = ArenaReport(
                proposal_id=prop_id,
                proposal_key=prop_key,
                run_salt=eff_salt,
                stage=ProposalStage.CULLED.value,
                candidate_instance_id=cand_id,
                candidate_instance_key=cand_key,
                comparison=ArenaComparisonVerdict(
                    promotion_eligible=False,
                    recommended_stage=ProposalStage.CULLED.value,
                    fitness_delta=-100.0,
                    reasons=[f"Arena crash: {exc}"],
                ),
                culling_report=cull_rep,
            )
            await self.evo_repo.update_proposal(
                prop_id,
                stage=ProposalStage.CULLED.value,
                candidate_instance_id=cand_id,
                arena_result=err_report.model_dump(mode="json"),
            )
            return err_report
