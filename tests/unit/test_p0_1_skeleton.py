"""P0-1 skeleton tests: config, DB schema (18 tables), repository CRUD, FastAPI endpoints."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import inspect

from app.api.app import create_app
from app.core.config import Settings, get_agent_config, get_drive_config, get_policy_config
from app.core.enums import DriveType, LifecycleState
from app.drives.interfaces import DriveState
from app.storage.database import Database
from app.storage.models import Base
from app.storage.repository import StateRepository

REQUIRED_TABLES = {
    "organism_identity",
    "organism_state",
    "drive_states",
    "drive_history",
    "observation_records",
    "world_states",
    "active_goals",
    "goal_history",
    "action_plans",
    "agent_registry",
    "agent_tasks",
    "agent_executions",
    "memory_index",
    "cycle_records",
    "decision_logs",
    "event_logs",
    "resource_usage",
    "system_config",
}


def test_config_loads_all_yaml_files():
    settings = Settings()
    assert settings.organism.name == "AOC-001"
    assert settings.api.port == 9200
    assert settings.governance.budget.max_agent_tasks_per_day >= 1

    drives = get_drive_config()
    assert set(drives["drives"].keys()) == {"survival", "reproduction", "curiosity"}
    assert "arbitration" in drives

    agents = get_agent_config()
    agent_ids = {a["agent_id"] for a in agents["agents"]}
    assert {"codex", "hermes"}.issubset(agent_ids)

    policy = get_policy_config()
    assert set(policy["permission_levels"].keys()) == {"L0", "L1", "L2", "L3"}


def test_all_18_tables_declared_in_metadata():
    tables = set(Base.metadata.tables.keys())
    missing = REQUIRED_TABLES - tables
    assert not missing, f"Missing required tables: {missing}"


@pytest.mark.asyncio
async def test_database_tables_created_and_ping(db: Database):
    assert await db.ping() is True
    async with db.engine.connect() as conn:
        actual_tables = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))
    assert REQUIRED_TABLES.issubset(actual_tables)


@pytest.mark.asyncio
async def test_repository_identity_cycle_and_drive_persistence(repo: StateRepository):
    ident = await repo.get_or_create_identity("AOC-TEST", "1.0.0")
    assert ident["created"] is True
    ident2 = await repo.get_or_create_identity("AOC-TEST", "1.0.0")
    assert ident2["created"] is False
    assert ident2["id"] == ident["id"]

    cycle_id, num = await repo.start_cycle(ident["id"], LifecycleState.ACTIVE.value)
    assert num == 1

    ds = DriveState(
        drive_type=DriveType.SURVIVAL.value,
        satisfaction=0.8,
        urgency=0.25,
        confidence=0.95,
    )
    assert pytest.approx(ds.deficit, abs=1e-6) == 0.2

    await repo.save_drive_states(ident["id"], [ds], cycle_id=cycle_id, phase="pre")
    loaded = await repo.load_drive_states(ident["id"])
    assert len(loaded) == 1
    assert loaded[0]["drive_type"] == "survival"
    assert pytest.approx(loaded[0]["satisfaction"], abs=1e-6) == 0.8
    assert pytest.approx(loaded[0]["deficit"], abs=1e-6) == 0.2


@pytest.mark.asyncio
async def test_api_health_state_metrics_and_world(db: Database):
    app = create_app(settings=Settings(), db=db)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/health")
            assert r.status_code == 200
            assert r.json()["status"] == "ok"
            assert r.json()["database"] == "ok"

            r = await client.get("/state")
            assert r.status_code == 200
            body = r.json()
            assert body["name"] == "AOC-001"
            assert body["state"]["lifecycle_state"] == "INITIALIZING"

            r = await client.post(
                "/world",
                json={"kind": "knowledge_topic", "key": "quantum-error-correction", "value": {"desc": "test"}},
            )
            assert r.status_code == 200
            r = await client.get("/world?kind=knowledge_topic")
            assert r.status_code == 200
            assert len(r.json()) == 1

            r = await client.get("/metrics")
            assert r.status_code == 200
            assert "aoc_lifecycle_state" in r.text
