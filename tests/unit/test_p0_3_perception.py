"""P0-3 Perception tests: real SystemSensor, AgentSensor, EnvironmentSensor, fault isolation, persistence."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.config import LLMSettings, PerceptionSettings
from app.perception.agent_sensor import AgentSensor
from app.perception.environment_sensor import EnvironmentSensor
from app.perception.interfaces import Observation, SensorInterface, is_unavailable, utcnow
from app.perception.manager import SensorManager
from app.perception.system_sensor import SystemSensor
from app.storage.database import Database
from app.storage.models import ObservationRecord
from app.storage.repository import StateRepository


class BrokenSensor(SensorInterface):
    name = "broken"

    async def observe(self) -> Observation:
        raise RuntimeError("simulated sensor crash")


@pytest.mark.asyncio
async def test_system_sensor_collects_real_server_metrics():
    async def fake_llm_probe():
        return {"available": True, "latency_ms": 120.0}

    sensor = SystemSensor(
        perception_cfg=PerceptionSettings(disk_paths=["/"], llm_probe=True),
        llm_cfg=LLMSettings(),
        llm_probe_fn=fake_llm_probe,
    )
    obs = await sensor.observe()
    assert obs.confidence == 1.0
    assert obs.system_state["cpu"]["cores"] >= 1
    assert "load_per_core" in obs.system_state["cpu"]
    assert 0.0 < obs.system_state["memory"]["available_ratio"] <= 1.0
    assert 0.0 < obs.system_state["disk"]["free_ratio"] <= 1.0
    assert obs.system_state["llm"]["available"] is True


@pytest.mark.asyncio
async def test_system_sensor_marks_missing_disk_path_unavailable():
    sensor = SystemSensor(
        perception_cfg=PerceptionSettings(disk_paths=["/nonexistent_path_aoc_xyz"], llm_probe=False),
        llm_cfg=LLMSettings(),
    )
    obs = await sensor.observe()
    assert is_unavailable(obs.system_state["disk"])
    assert is_unavailable(obs.system_state["llm"])
    assert obs.confidence == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_system_sensor_applies_simulation_overrides_without_touching_host(repo: StateRepository):
    await repo.upsert_world_item(
        kind="simulation",
        key="disk_pressure",
        value={"free_ratio": 0.03},
        status="active",
        is_simulated=True,
    )
    await repo.upsert_world_item(
        kind="simulation",
        key="llm_outage",
        value={"error": "injected outage"},
        status="active",
        is_simulated=True,
    )
    sensor = SystemSensor(
        perception_cfg=PerceptionSettings(disk_paths=["/"], llm_probe=False),
        llm_cfg=LLMSettings(),
        repo=repo,
    )
    obs = await sensor.observe()
    assert obs.system_state["disk"]["free_ratio"] == 0.03
    assert obs.system_state["disk"]["simulated"] is True
    assert obs.system_state["disk"]["real"]["free_ratio"] > 0.05
    assert obs.system_state["llm"]["available"] is False
    assert obs.system_state["llm"]["simulated"] is True


@pytest.mark.asyncio
async def test_agent_sensor_reports_registered_agents_and_active_tasks(repo: StateRepository):
    await repo.upsert_agent(
        {
            "agent_id": "codex",
            "name": "Codex",
            "agent_type": "cli",
            "capabilities": ["coding", "terminal"],
            "supported_modes": ["workspace-write"],
            "max_concurrency": 1,
            "enabled": True,
        },
        experimental=False,
        probe={"ok": True, "version": "0.147.0"},
    )
    await repo.create_task(
        objective="inspect disk usage",
        description="read-only check",
        assigned_agent="codex",
        expected_output="summary",
        workspace="/tmp/ws",
        timeout_seconds=60,
        resource_budget={},
        permission_level="L0",
        success_criteria=["non-empty output"],
        status="RUNNING",
    )
    sensor = AgentSensor(repo)
    obs = await sensor.observe()
    agents = obs.available_resources["agents"]
    assert agents["enabled_count"] == 1
    assert agents["healthy_count"] == 1
    assert "coding" in agents["capabilities"]
    assert len(obs.active_tasks) == 1
    assert obs.active_tasks[0]["status"] == "RUNNING"


@pytest.mark.asyncio
async def test_environment_sensor_reads_world_and_custom_provider(repo: StateRepository):
    await repo.upsert_world_item(
        kind="knowledge_topic",
        key="raft-consensus",
        value={"description": "Study Raft leader election", "importance": 0.9},
        status="open",
    )
    await repo.upsert_world_item(
        kind="capability",
        key="system_diagnosis",
        value={"verified": True},
        status="acquired",
    )
    await repo.upsert_world_item(
        kind="event",
        key="new-paper",
        value={"title": "Autonomous Agents"},
        status="open",
    )

    async def failing_provider():
        raise ConnectionError("feed down")

    async def ok_provider():
        return {"items": 3}

    sensor = EnvironmentSensor(repo, providers={"ok": ok_provider, "bad": failing_provider})
    obs = await sensor.observe()
    assert len(obs.environment_state["unknown_topics"]) == 1
    assert obs.environment_state["unknown_topics"][0]["topic"] == "raft-consensus"
    assert "system_diagnosis" in obs.environment_state["capabilities_acquired"]
    assert len(obs.external_events) == 1
    assert obs.environment_state["providers"]["ok"] == {"items": 3}
    assert is_unavailable(obs.environment_state["providers"]["bad"])
    assert obs.confidence == pytest.approx(2 / 3)


@pytest.mark.asyncio
async def test_sensor_manager_register_unregister_fault_isolation_and_persistence(
    db: Database, repo: StateRepository
):
    mgr = SensorManager(repo=repo)
    mgr.register(
        SystemSensor(
            perception_cfg=PerceptionSettings(disk_paths=["/"], llm_probe=False),
            llm_cfg=LLMSettings(),
        )
    )
    mgr.register(AgentSensor(repo))
    mgr.register(EnvironmentSensor(repo))
    mgr.register(BrokenSensor())

    assert set(mgr.list_sensors()) == {"system", "agent", "environment", "broken"}

    obs = await mgr.perceive(persist=True)
    # Broken sensor must NOT crash perceive(), must lower confidence, and must be marked failed
    assert obs.sensor_status["broken"]["ok"] is False
    assert obs.sensor_status["system"]["ok"] is True
    assert is_unavailable(obs.system_state["sensor_broken"])
    assert 0.0 < obs.confidence < 0.9

    # Unregister broken sensor -> confidence improves
    assert mgr.unregister("broken") is True
    assert mgr.unregister("broken") is False
    obs2 = await mgr.perceive(persist=True)
    assert "broken" not in obs2.sensor_status
    assert obs2.confidence > obs.confidence

    # Verify both observations were persisted to observation_records
    async with db.session() as s:
        rows = (await s.execute(select(ObservationRecord))).scalars().all()
        assert len(rows) == 2
