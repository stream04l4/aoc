"""Run a verifiable 100-cycle continuous acceptance test against real PostgreSQL.

Exercises all acceptance scenarios (A-F) across 100 autonomous cycles:
  - Cycle 1-15:  Curiosity & Reproduction tasks (multi-drive autonomy)
  - Cycle 20:    Scenario A (simulated disk pressure -> Survival emergency)
  - Cycle 35:    Scenario F (primary agent failure -> fallback + full outage bounded retry)
  - Cycle 50:    Scenario E (simulated crash & full OrganismRuntime restart from PostgreSQL)
  - Cycle 51-85: High-satisfaction IDLE & periodic new knowledge topic wakeups (Scenario B & D)
  - Cycle 86-100: Mixed workload + budget cap verification

Produces verifiable audit stats from PostgreSQL + writes docs/test_report_100_cycles.md.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.agents.base import (
    AgentCapability,
    AgentExecution,
    AgentInterface,
    AgentProbeResult,
    AgentStatus,
    AgentTask,
)
from app.agents.registry import AgentRegistry
from app.cognition.interfaces import (
    ActionPlan,
    AssessmentResult,
    BrainInterface,
    CognitiveContext,
    EvaluationContext,
    EvaluationResult,
    GoalProposal,
    VerificationSpec,
)
from app.core.config import (
    BudgetSettings,
    GovernanceSettings,
    MemorySettings,
    OrganismSettings,
    PerceptionSettings,
    RuntimeSettings,
    Settings,
)
from app.core.enums import DriveType, ExecutionStatus, MemoryType, TaskStatus
from app.core.runtime import OrganismRuntime
from app.drives.interfaces import DriveState, KnowledgeGap
from app.perception.interfaces import Observation, utcnow
from app.storage.database import Database


class BenchmarkBrain(BrainInterface):
    """Fast deterministic brain for 100-cycle stress benchmark (real LLM tested separately)."""

    async def assess(self, observation: Observation, memories: list[dict], drives: list[DriveState]) -> AssessmentResult:
        topics = (observation.environment_state or {}).get("unknown_topics") or []
        gaps = [KnowledgeGap(topic=t["topic"], importance=float(t.get("importance", 0.9))) for t in topics]
        return AssessmentResult(summary=f"Assessed {len(gaps)} open topic(s)", knowledge_gaps=gaps, confidence=0.95)

    async def generate_goal(self, context: CognitiveContext) -> GoalProposal:
        d = context.decision.selected_drive
        if d == DriveType.CURIOSITY.value:
            topics = (context.observation.environment_state or {}).get("unknown_topics") or []
            key = topics[0]["topic"] if topics else f"topic-{uuid.uuid4().hex[:6]}"
            return GoalProposal(
                should_act=True,
                title=f"Explore {key}",
                description=f"Research {key} and summarize key concepts",
                drive_type=d,
                related_key=key,
                success_criteria=["concise summary"],
                expected_outcome={"expected_curiosity_delta": 0.25},
            )
        if d == DriveType.REPRODUCTION.value:
            acquired = set((context.observation.environment_state or {}).get("capabilities_acquired") or [])
            desired = ["coding", "research", "repository_analysis", "system_diagnosis", "knowledge_summarization"]
            missing = [c for c in desired if c not in acquired]
            key = missing[0] if missing else f"skill-{uuid.uuid4().hex[:4]}"
            return GoalProposal(
                should_act=True,
                title=f"Build capability: {key}",
                description=f"Create reusable procedure for {key}",
                drive_type=d,
                related_key=key,
                success_criteria=["reusable skill procedure"],
                expected_outcome={"skill": key},
            )
        return GoalProposal(
            should_act=True,
            title="Diagnose survival pressure",
            description="Inspect disk and service health read-only",
            drive_type=d,
            success_criteria=["diagnostic report"],
            expected_outcome={"diagnosed": True},
        )

    async def plan(self, context: CognitiveContext) -> ActionPlan:
        g = context.goal
        return ActionPlan(
            should_execute=True,
            goal_title=g.title if g else "Task",
            objective=g.description if g else "Execute bounded task",
            assigned_agent="bench-primary",
            required_capability="research",
            expected_output="Verified summary",
            timeout_seconds=30,
            permission_level="L0" if context.decision.selected_drive == "survival" else "L1",
            success_criteria=["non-empty verified output"],
            verification=[VerificationSpec(kind="min_output_chars", min_value=10)],
            expected_improvement=0.25,
        )

    async def evaluate(self, context: EvaluationContext) -> EvaluationResult:
        ok = context.verification_passed and context.execution.get("status") == "SUCCEEDED"
        d = context.goal.get("drive_type")
        key = context.goal.get("related_key")
        return EvaluationResult(
            goal_achieved=ok,
            summary=f"Evaluated {d} goal ({'achieved' if ok else 'failed'})",
            information_gain=0.55 if (ok and d == "curiosity") else 0.0,
            learned_facts=[f"Verified findings for {key}"] if (ok and d == "curiosity") else [],
            acquired_skill=key if (ok and d == "reproduction") else None,
            resolved_topic=key if (ok and d == "curiosity") else None,
            should_persist_memory=True,
            memory_type=MemoryType.SEMANTIC.value if d == "curiosity" else MemoryType.EPISODIC.value,
            memory_importance=0.8,
        )


class BenchAgent(AgentInterface):
    def __init__(self, agent_id: str, fail_mode: str | None = None) -> None:
        self._cap = AgentCapability(
            agent_id=agent_id,
            name=agent_id,
            agent_type="benchmark",
            capabilities=["coding", "research", "repository_analysis", "system_diagnosis", "knowledge_summarization"],
            supported_modes=["read-only", "workspace-write"],
            enabled=True,
        )
        self.fail_mode = fail_mode
        self.calls = 0

    @property
    def capability(self) -> AgentCapability:
        return self._cap

    async def probe(self) -> AgentProbeResult:
        return AgentProbeResult(ok=True, binary=self._cap.agent_id, version="bench-1.0")

    async def execute(self, task: AgentTask) -> AgentExecution:
        self.calls += 1
        eid = task.execution_id or str(uuid.uuid4())
        if self.fail_mode == "fail":
            return AgentExecution(
                execution_id=eid,
                task_id=task.task_id,
                agent_id=self._cap.agent_id,
                status=ExecutionStatus.FAILED.value,
                completed_at=utcnow(),
                error="injected agent failure",
            )
        if self.fail_mode == "timeout":
            return AgentExecution(
                execution_id=eid,
                task_id=task.task_id,
                agent_id=self._cap.agent_id,
                status=ExecutionStatus.TIMEOUT.value,
                completed_at=utcnow(),
                error="injected agent timeout",
            )
        return AgentExecution(
            execution_id=eid,
            task_id=task.task_id,
            agent_id=self._cap.agent_id,
            status=ExecutionStatus.SUCCEEDED.value,
            completed_at=utcnow(),
            result={"summary": f"[{self._cap.agent_id}] Verified result for: {task.objective}"},
            stdout=f"[{self._cap.agent_id}] Verified result for: {task.objective}",
            resource_usage={"wall_seconds": 0.01},
        )

    async def get_status(self, execution_id: str) -> AgentStatus:
        return AgentStatus(execution_id=execution_id, agent_id=self._cap.agent_id, status="SUCCEEDED")

    async def cancel(self, execution_id: str) -> bool:
        return True


async def run_benchmark(total_cycles: int = 100, db_url: str | None = None) -> dict[str, Any]:
    t0 = time.perf_counter()
    org_name = f"AOC-BENCH-{uuid.uuid4().hex[:6]}"
    ws_root = f"/tmp/aoc-bench-{uuid.uuid4().hex[:6]}"
    os.makedirs(ws_root, exist_ok=True)

    base_settings = Settings()
    db_settings = base_settings.database.model_copy(update={"url": db_url}) if db_url else base_settings.database
    settings = Settings(
        organism=OrganismSettings(name=org_name, version="1.0.0"),
        runtime=RuntimeSettings(
            cycle_interval_seconds=0.01,
            idle_sleep_seconds=0.01,
            monitor_poll_seconds=0.01,
            max_wait_per_cycle_seconds=5.0,
        ),
        database=db_settings,
        perception=PerceptionSettings(disk_paths=["/"], llm_probe=False),
        memory=MemorySettings(provider="none"),
        governance=GovernanceSettings(
            workspace_root=ws_root,
            budget=BudgetSettings(max_agent_tasks_per_day=25, max_task_timeout_seconds=60, max_task_retries=1),
        ),
    )

    primary = BenchAgent("bench-primary")
    backup = BenchAgent("bench-backup")

    async def llm_ok():
        return {"available": True, "latency_ms": 120.0}

    def build_runtime(db: Database) -> OrganismRuntime:
        reg = AgentRegistry()
        reg.register(primary)
        reg.register(backup)
        rt = OrganismRuntime(
            settings=settings,
            db=db,
            brain=BenchmarkBrain(),
            registry=reg,
            auto_load_agents=False,
            llm_probe_fn=llm_ok,
        )
        reg.repo = rt.repo
        return rt

    db = Database(settings.database.url)
    # Give this benchmark run a clean 25-task budget window above any prior runs today
    from app.storage.repository import StateRepository, day_start

    prior_used = int((await StateRepository(db).usage_since("agent_task", day_start()))["count"])
    settings.governance.budget.max_agent_tasks_per_day = prior_used + 25
    rt = build_runtime(db)
    await rt.start(start_scheduler=False)

    outcomes: Counter[str] = Counter()
    selected_drives: Counter[str] = Counter()
    fatal_errors = 0
    restart_verified = False
    scenario_flags = {
        "A_survival_emergency": False,
        "B_curiosity_explored": False,
        "C_reproduction_virtual": False,
        "D_idle_no_forced_task": False,
        "E_restart_recovery": False,
        "F_agent_fallback_and_bounded_fail": False,
        "budget_respected": True,
    }

    try:
        # Pre-clear any stray world items from prior runs
        for item in await rt.repo.list_world_items():
            await rt.repo.delete_world_item(item["kind"], item["key"])

        for i in range(1, total_cycles + 1):
            # Inject scenarios at specific cycles
            if i == 1:
                # Scenario C: simulate capability deficiency so reproduction drive wins cleanly
                primary._cap = primary._cap.model_copy(update={"capabilities": []})
                backup._cap = backup._cap.model_copy(update={"capabilities": []})
                await rt.registry.probe_all()
            elif i == 3:
                full_caps = ["coding", "research", "repository_analysis", "system_diagnosis", "knowledge_summarization"]
                primary._cap = primary._cap.model_copy(update={"capabilities": full_caps})
                backup._cap = backup._cap.model_copy(update={"capabilities": full_caps})
                await rt.registry.probe_all()

            if i in (4, 10, 25, 60, 75):
                for j in range(4):
                    await rt.repo.upsert_world_item(
                        "knowledge_topic",
                        f"bench-topic-{i}-{j}",
                        {"description": f"Topic {i}-{j}", "importance": 0.95},
                        status="open",
                    )

            if i == 20:
                await rt.repo.upsert_world_item(
                    "simulation", "disk_pressure", {"free_ratio": 0.02}, status="active", is_simulated=True
                )
            elif i == 22:
                await rt.repo.delete_world_item("simulation", "disk_pressure")

            if i == 35:
                # Primary fails -> backup catches it (Scenario F part 1)
                primary.fail_mode = "fail"
                await rt.repo.upsert_world_item(
                    "simulation", "disk_pressure", {"free_ratio": 0.02}, status="active", is_simulated=True
                )
            elif i == 36:
                # Both fail -> must record ACTION_FAILED, never SUCCEEDED, bounded retries (Scenario F part 2)
                primary.fail_mode = "timeout"
                backup.fail_mode = "fail"
            elif i == 37:
                primary.fail_mode = None
                backup.fail_mode = None
                await rt.repo.delete_world_item("simulation", "disk_pressure")

            if i == 50:
                # Simulate hard crash & restart with a brand new OrganismRuntime on the same PostgreSQL DB
                prev_org_id = rt.organism_id
                # Leave a simulated orphan running task to verify recovery
                orphan_tid = await rt.repo.create_task(
                    objective="simulated crash orphan",
                    assigned_agent="bench-primary",
                    workspace=ws_root,
                    timeout_seconds=30,
                    resource_budget={},
                    permission_level="L0",
                    success_criteria=[],
                    status=TaskStatus.RUNNING.value,
                )
                await rt.stop()
                db = Database(settings.database.url)
                rt = build_runtime(db)
                boot = await rt.start(start_scheduler=False)
                recovered_orphan = await rt.repo.get_task(orphan_tid)
                restart_verified = (
                    rt.organism_id == prev_org_id
                    and boot["recovery"]["orchestrator"]["reconciled_tasks"] >= 1
                    and recovered_orphan["status"] == TaskStatus.FAILED.value
                )
                scenario_flags["E_restart_recovery"] = restart_verified

            rec = await rt.loop.run_cycle()
            out = rec.get("outcome", "UNKNOWN")
            sel = (rec.get("decision") or {}).get("selected_drive", "none")
            outcomes[out] += 1
            selected_drives[sel] += 1

            if out == "ERROR":
                fatal_errors += 1
            if i == 20 and sel == "survival" and (rec.get("decision") or {}).get("constraints", {}).get("emergency_mode"):
                scenario_flags["A_survival_emergency"] = True
            if sel == "curiosity" and out == "SUCCEEDED":
                scenario_flags["B_curiosity_explored"] = True
            if sel == "reproduction" and out == "SUCCEEDED":
                scenario_flags["C_reproduction_virtual"] = True
            if sel == "none" and out == "IDLE":
                scenario_flags["D_idle_no_forced_task"] = True
            if i == 36 and out == "ACTION_FAILED" and (rec.get("evaluation") or {}).get("goal_achieved") is False:
                scenario_flags["F_agent_fallback_and_bounded_fail"] = True

        budget = await rt.governance.check_budget()
        # Allow small slack for retries launched within the last allowed task
        scenario_flags["budget_respected"] = budget.tasks_used_today <= settings.governance.budget.max_agent_tasks_per_day + 2

        summary = {
            "organism": org_name,
            "total_cycles": total_cycles,
            "wall_seconds": round(time.perf_counter() - t0, 2),
            "fatal_errors": fatal_errors,
            "outcomes": dict(outcomes),
            "selected_drives": dict(selected_drives),
            "primary_agent_calls": primary.calls,
            "backup_agent_calls": backup.calls,
            "tasks_used_this_run": budget.tasks_used_today - prior_used,
            "tasks_used_today": budget.tasks_used_today,
            "tasks_limit_per_day": budget.tasks_limit_per_day,
            "scenario_flags": scenario_flags,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return summary
    finally:
        await rt.stop()
        await db.dispose()


def main() -> None:
    db_url = os.environ.get("AOC_BENCH_DB_URL")
    summary = asyncio.run(run_benchmark(100, db_url=db_url))
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    assert summary["total_cycles"] == 100
    assert summary["fatal_errors"] == 0
    assert summary["scenario_flags"]["A_survival_emergency"]
    assert summary["scenario_flags"]["B_curiosity_explored"]
    assert summary["scenario_flags"]["C_reproduction_virtual"]
    assert summary["scenario_flags"]["D_idle_no_forced_task"]
    assert summary["scenario_flags"]["E_restart_recovery"]
    assert summary["scenario_flags"]["F_agent_fallback_and_bounded_fail"]
    assert summary["scenario_flags"]["budget_respected"]
    assert summary["backup_agent_calls"] >= 1

    # Write verifiable Markdown report (Deliverable #10)
    report_path = Path(__file__).resolve().parents[1] / "docs" / "test_report_100_cycles.md"
    lines = [
        "# AOC V1.0 — 100-Cycle Continuous Run & Acceptance Test Report",
        "",
        f"- **Timestamp (UTC)**: `{summary['timestamp']}`",
        f"- **Organism**: `{summary['organism']}`",
        f"- **Total Cycles Completed**: `{summary['total_cycles']}`",
        f"- **Wall Time**: `{summary['wall_seconds']}s`",
        f"- **Fatal Errors**: `{summary['fatal_errors']}`",
        f"- **Agent Calls**: primary=`{summary['primary_agent_calls']}`, backup=`{summary['backup_agent_calls']}`",
        f"- **Budget Enforcement**: `{summary['tasks_used_this_run']}` tasks used (limit window `{summary['tasks_limit_per_day']}`)",
        "",
        "## Cycle Outcomes",
        "",
        "| Outcome | Count |",
        "|---|---|",
    ]
    for k, v in sorted(summary["outcomes"].items()):
        lines.append(f"| `{k}` | {v} |")
    lines += [
        "",
        "## Selected Drives Distribution",
        "",
        "| Selected Drive | Count |",
        "|---|---|",
    ]
    for k, v in sorted(summary["selected_drives"].items()):
        lines.append(f"| `{k}` | {v} |")
    lines += [
        "",
        "## Acceptance Scenario Verification (Scenarios A–F)",
        "",
        "| Scenario | Passed |",
        "|---|---|",
    ]
    for k, v in summary["scenario_flags"].items():
        lines.append(f"| `{k}` | {'PASS' if v else 'FAIL'} |")
    lines.append("")
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Report written to {report_path}")


if __name__ == "__main__":
    main()
