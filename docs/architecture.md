# Artificial Organism Core (AOC) — Architecture (V1.0)

## Overview

AOC is a Python Modular Monolith implementing a minimum viable autonomous digital organism (Phase 0).
All modules communicate strictly through explicit Python abstract interfaces (`interfaces.py` / `base.py`) and Pydantic v2 schemas.

## Module Boundaries

| Module | Path | Responsibility | External Contract |
|---|---|---|---|
| Core Runtime | `app/core/` | Config, enums (`LifecycleState` incl. `THROTTLED`, `QuotaStatus`), Lifecycle Manager (9 states), Scheduler (quota-reset throttling), Autonomous Loop (16 steps + re-entrancy lock), State Manager | Orchestrates loop via module interfaces only |
| Perception | `app/perception/` | SensorManager, SystemSensor (shares `LLMGateway.health_check`), AgentSensor, EnvironmentSensor | `PerceptionInterface`, `SensorInterface`, `Observation` |
| Drive Engine | `app/drives/` | Independent evaluators (Survival decoupled from Economic Quota, Reproduction, Curiosity), satisfaction/persistence/trend calculation, Arbitration | `DriveEngineInterface`, `DriveEvaluator`, `DriveState`, `DriveDecision` |
| Cognition | `app/cognition/` | Unified `LLMGateway` (per-cycle call/token/concurrency/emergency/hard-cap enforcement, circuit breaker, HTTP-level accounting), OpenAI-compatible LLM Adapter, Brain (`assess/generate_goal/plan/evaluate`) | `BrainInterface`, `LLMGatewayInterface` (read-only advice; zero write/dispatch privileges) |
| Governance | `app/governance/` | Permission levels L0–L3, Policy enforcement, Resource Budget, Workspace isolation | `GovernanceInterface`, `PolicyVerdict` |
| Agent Orchestration | `app/orchestration/`, `app/agents/` | Durable `TaskStateMachine` (optimistic locking + `task_lifecycle_events`), unified idempotent `TaskFinalizer.finalize_execution()` (pure-DB context reconstruction), `ExecutionBackendInterface` (`LocalSubprocessBackend`), `AgentRegistry`, `CodexAdapter`, `HermesAdapter` (experimental), `GenericCLIAdapter`, `ResultVerifier`, `AgentOrchestrator` | `AgentInterface`, `ExecutionBackendInterface`, `AgentCapability`, `AgentTask`, `AgentExecution` |
| Memory | `app/memory/` | Dual-layer memory: PostgreSQL real-time state + Letta long-term cognitive memory (5 types) | `MemoryInterface`, `MemoryItem` |
| Storage | `app/storage/` | Async SQLAlchemy 2.x engine, 18 V1 + 6 V2 ORM tables (`api_credentials`, `approval_records`, `policy_evaluations`, `task_lifecycle_events`, `outbox_events`, `inbox_events`), PostgreSQL Transactional `OutboxDispatcher` (`SKIP LOCKED`), Alembic migrations (`0001`–`0003`), `StateRepository` (Unit-of-Work sessions) | `StateRepository`, `OutboxDispatcher` |
| Observability | `app/observability/` | Structured JSON logging (`structlog`), Decision/Event logs, Prometheus `/metrics` | Metrics & logger helpers |
| API | `app/api/` | FastAPI observability, Bearer token auth + scope RBAC (`app/api/auth.py`), loopback startup guard, control (pause/resume/wake/re-entrancy-guarded cycle), world injection, public `approve_and_dispatch` approvals | HTTP JSON + `/metrics` |

## Hard Invariants

1. **No LLM direct drive override**: Satisfaction changes only via re-perception and deterministic/bounded evaluators. Expected, Actual, and Measured outcomes are always stored separately.
2. **Missing data is never "normal"**: Unavailable sensor metrics carry `{"status": "unavailable", "reason": ...}`, reduce observation/drive confidence, and trigger conservative handling.
3. **Brain has no side-effect privileges**: The Brain cannot write to DB, alter policy/config, approve L2/L3, or directly invoke external agents.
4. **Decoupled async execution**: Agent runs execute asynchronously as tracked subprocesses with DB-persisted PIDs/status; the main loop polls without blocking indefinitely.
5. **Least privilege & isolated workspaces**: Every task runs under `/home/ubuntu/aoc-workspaces/<task_id>` with explicit timeout, budget, and sandbox level (`read-only` for L0, `workspace-write` for L1).
6. **Host systemd Agent Runner & OS ProcessIdentity (P1-3)**: External agents execute via an unprivileged host systemd service (`aoc-agent-runner.service`, `User=ubuntu`, `Delegate=yes`, `KillMode=process`, cgroup v2 subtree isolation, HTTP over Unix Domain Socket `/run/aoc-runner/runner.sock`, zero root / zero Docker socket). Every execution captures full `ProcessIdentity` (`pid`, `pgid`, `host_boot_id`, `/proc/<pid>/stat` `starttime`, `cgroup_path`) and survives Core or Runner `kill -9` with idempotent startup reconciliation (`execution_recovery_records`). Non-systemd dev/CI environments automatically degrade to `fallback_pgroup` mode.
7. **Semantic Memory, Switchable ONNX Embeddings & Async Letta Sync (P1-4)**: `EmbeddingInterface` supports hot-switching between local quantized ONNX (`BAAI/bge-small-zh-v1.5:onnx-qint8` 512d default at 153.8 MB RSS, `intfloat/multilingual-e5-small:onnx-qint8` 384d) and OpenAI-compatible remote endpoints (`BAAI/bge-m3`), enforcing strict per-model dimension isolation via `embedding_model_registry` + `memory_embeddings` (`pgvector` on PG16, JSON on SQLite). `HybridRetrievalEngine` fuses `pgvector` cosine distance, `pg_trgm` bilingual Chinese/English trigram similarity, recency decay, reliability, and importance, degrading cleanly when embeddings or Letta are offline. `InformationGainEvaluator` suppresses duplicate/unverified gains and persists measured `information_gain` across restarts, while Letta synchronization runs asynchronously via PostgreSQL Outbox (`memory.sync_letta`).
