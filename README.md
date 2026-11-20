# Artificial Organism Core (AOC) V1.0

Minimum Viable Autonomous Organism (Phase 0) implemented as a Python 3.12+ Modular Monolith.

## Quick Start (Docker Compose)

```bash
cp .env.example .env
# Edit .env and set AOC_LLM__API_KEY and POSTGRES_PASSWORD
docker compose up -d --build
curl http://127.0.0.1:9200/health
```

## Development (uv)

```bash
uv sync --python 3.12
uv run pytest
```

## Documentation

- [Architecture](docs/architecture.md)
- [Core Algorithms](docs/algorithms.md) (added in P0-2)
- [Agent Adapters](docs/agent_adapters.md) (added in P0-5)
- [API Reference](docs/api.md) (added in P0-7)
