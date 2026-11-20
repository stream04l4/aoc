# Artificial Organism Core (AOC) — Architecture (V1.0)

## Overview

AOC is a Python Modular Monolith implementing a minimum viable autonomous digital organism (Phase 0).
All modules communicate strictly through explicit Python abstract interfaces (`interfaces.py` / `base.py`) and Pydantic v2 schemas.

## Module Boundaries

| Module | Path | Responsibility | External Contract |
|---|---|---|---|
| Core Runtime | `app/core/` | Config, enums, Lifecycle Manager (8 states), Scheduler, Autonomous Loop (16 steps), State Manager | Orchestrates loop via module interfaces only |
| Perception | `app/perception/` | SensorManager, SystemSensor, AgentSensor, EnvironmentSensor | `PerceptionInterface`, `SensorInterface`, `Observation` |
| Drive Engine | `app/drives/` | Independent evaluators (Survival, Reproduction, Curiosity), satisfaction/persistence/trend calculation, Arbitration | `DriveEngineInterface`, `DriveEvaluator`, `DriveState`, `DriveDecision` |
| Cognition | `app/cognition/` | OpenAI-compatible LLM Adapter, Assess, Goal Generation, Action Planning, Outcome Evaluation | `BrainInterface` (read-only advice; zero write/dispatch privileges) |
| Governance | `app/governance/` | Permission levels L0–L3, Policy enforcement, Resource Budget, Workspace isolation | `GovernanceInterface`, `PolicyVerdict` |
| Agent Orchestration | `app/orchestration/`, `app/agents/` | AgentRegistry (probed capabilities), CodexAdapter, HermesAdapter (experimental), GenericCLIAdapter, TaskDispatcher, ExecutionMonitor, ResultCollector | `AgentInterface`, `AgentCapability`, `AgentTask`, `AgentExecution` |
| Memory | `app/memory/` | Dual-layer memory: PostgreSQL real-time state + Letta long-term cognitive memory (5 types) | `MemoryInterface`, `MemoryItem` |
| Storage | `app/storage/` | Async SQLAlchemy 2.x engine, 18 ORM tables, Alembic migrations, StateRepository | `StateRepository` |
| Observability | `app/observability/` | Structured JSON logging (`structlog`), Decision/Event logs, Prometheus `/metrics` | Metrics & logger helpers |
| API | `app/api/` | FastAPI observability, control (pause/resume/wake), world injection, approvals | HTTP JSON + `/metrics` |

## Hard Invariants

1. **No LLM direct drive override**: Satisfaction changes only via re-perception and deterministic/bounded evaluators. Expected, Actual, and Measured outcomes are always stored separately.
2. **Missing data is never "normal"**: Unavailable sensor metrics carry `{"status": "unavailable", "reason": ...}`, reduce observation/drive confidence, and trigger conservative handling.
3. **Brain has no side-effect privileges**: The Brain cannot write to DB, alter policy/config, approve L2/L3, or directly invoke external agents.
4. **Decoupled async execution**: Agent runs execute asynchronously as tracked subprocesses with DB-persisted PIDs/status; the main loop polls without blocking indefinitely.
5. **Least privilege & isolated workspaces**: Every task runs under `/home/ubuntu/aoc-workspaces/<task_id>` with explicit timeout, budget, and sandbox level (`read-only` for L0, `workspace-write` for L1).
