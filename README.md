# Artificial Organism Core (AOC) V1.0

Minimum Viable Autonomous Organism (Phase 0) implemented as a Python 3.12+ Modular Monolith.

## Architecture & Modules

- **Core Runtime** (`app/core/`): 8-state Lifecycle Manager, 16-stage Autonomous Loop, Scheduler, State Recovery
- **Perception** (`app/perception/`): `SystemSensor`, `AgentSensor`, `EnvironmentSensor`, fault-isolated `SensorManager`
- **Drive Engine** (`app/drives/`): Independent `Survival`, `Reproduction` (virtual), and `Curiosity` evaluators + hybrid arbitrator (`config/drives.yaml`)
- **Cognition** (`app/cognition/`): `BrainInterface` + OpenAI-compatible LLM adapter with Pydantic schema validation and bounded retries
- **Governance** (`app/governance/`): Least-privilege L0–L3 policy (`config/policy.yaml`), daily/concurrency budgets, isolated workspaces
- **Agent Orchestration** (`app/agents/`, `app/orchestration/`): `AgentRegistry` (probed capabilities), `CodexAdapter` (`codex exec`), `HermesAdapter` (experimental), `GenericCLIAdapter`, `ResultVerifier`, `AgentOrchestrator`
- **Memory** (`app/memory/`): Dual-layer PostgreSQL real-time state + Letta long-term cognitive memory (5 memory types) + local `/v1/embeddings`
- **Storage** (`app/storage/`, `migrations/`): Async SQLAlchemy 2.x, 18 core tables, Alembic migrations
- **Observability & API** (`app/observability/`, `app/api/`): `structlog` JSON logs, Prometheus `/metrics`, FastAPI control/observability API

## Quick Start (Docker Compose)

```bash
cp .env.example .env
# Edit .env and fill in AOC_LLM__API_KEY and POSTGRES_PASSWORD
docker compose up -d --build
curl http://127.0.0.1:9200/health
```

## Development & Tests

```bash
uv sync --python 3.12
uv run pytest tests/unit/                        # Unit tests (fast, SQLite)
uv run pytest tests/integration/                 # Live LLM, Codex CLI, Letta tests
uv run pytest tests/acceptance/                  # Real end-to-end closed-loop test
uv run python scripts/run_100_cycles.py          # 100-cycle continuous PostgreSQL benchmark
```

## Documentation

- [System Architecture](docs/architecture.md)
- [Core Drive & Arbitration Algorithms](docs/algorithms.md)
- [External Agent Adapters](docs/agent_adapters.md)
- [HTTP API Reference](docs/api.md)
- [Continuous Run & Acceptance Test Report](docs/test_report_100_cycles.md)
- [Known Issues & Next-Stage Roadmap](docs/known_issues_and_roadmap.md)
