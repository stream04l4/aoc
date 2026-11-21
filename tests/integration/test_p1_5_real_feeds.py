"""P1-5 Real Integration Tests:
  1. Real live RSS feed (PostgreSQL official RSS) + Real local Git repo (/home/ubuntu/aoc)
     + Real live Web Change page (Python 3.12 docs) + Real enhanced SystemSensor.
     Verifies >= 3 real sensor types ingest traceable events, 0% duplicates on re-poll,
     and Curiosity-consumable open knowledge topics are queued.
  2. Real PostgreSQL 16 + pgvector original snapshot (`aoc_pg_dump_20261120.sql`)
     `0001 -> 0006` migration upgrade/downgrade verification.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from app.core.config import Settings, get_sensors_config
from app.perception.system_sensor import SystemSensor
from app.perception.world.git_sensor import GitWorldSensor
from app.perception.world.pipeline import WorldEventPipeline
from app.perception.world.rss_sensor import RSSWorldSensor
from app.perception.world.web_sensor import WebChangeWorldSensor
from app.storage.database import Database
from app.storage.models import Base
from app.storage.repository import StateRepository

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.integration
def test_real_rss_git_web_and_system_sensors_e2e(tmp_path: Path):
    """Connects to real live allowlisted RSS/Web feeds, real /home/ubuntu/aoc Git repo, and host SystemSensor."""

    async def _run():
        db = Database(f"sqlite+aiosqlite:///{tmp_path / 'real_feeds.db'}")
        async with db.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        repo = StateRepository(db)
        try:
            scfg = get_sensors_config()
            allowed_domains = list(scfg.get("allowed_domains") or [])
            allowed_git_roots = list(scfg.get("allowed_git_roots") or [])

            pipeline = WorldEventPipeline(
                repo,
                sensors=[
                    RSSWorldSensor(allowed_domains=allowed_domains, timeout_seconds=15.0),
                    GitWorldSensor(allowed_git_roots=allowed_git_roots),
                    WebChangeWorldSensor(allowed_domains=allowed_domains, timeout_seconds=15.0),
                ],
                interest_keywords=list(scfg.get("interest_keywords") or []),
                importance_threshold=0.35,
            )

            await repo.upsert_world_source(
                source_id="real-git-aoc",
                sensor_type="git",
                name="AOC Repo",
                config={"repo_path": str(PROJECT_ROOT), "max_commits": 3},
                reliability_score=0.95,
                enabled=True,
            )
            await repo.upsert_world_source(
                source_id="real-rss-pg",
                sensor_type="rss",
                name="PostgreSQL Official News",
                config={"url": "https://www.postgresql.org/news.rss", "max_items": 5},
                reliability_score=0.90,
                enabled=True,
            )
            await repo.upsert_world_source(
                source_id="real-web-py",
                sensor_type="web",
                name="Python 3.12 Release Notes",
                config={"url": "https://docs.python.org/3.12/whatsnew/3.12.html"},
                reliability_score=0.90,
                enabled=True,
            )

            # 1. Poll all 3 real external/world sensors
            res1 = await pipeline.poll_all(force=True)
            print("\n=== P1-5 Real Sensor Poll #1 ===", res1)
            by_id = {r["source_id"]: r for r in res1}
            assert by_id["real-git-aoc"]["ok"] is True and by_id["real-git-aoc"]["inserted"] >= 1
            assert by_id["real-rss-pg"]["ok"] is True and by_id["real-rss-pg"]["inserted"] >= 1
            assert by_id["real-web-py"]["ok"] is True and by_id["real-web-py"]["inserted"] == 1

            events_r1 = await repo.list_world_events(limit=100)
            assert len(events_r1) >= 3
            # Every event must be 100% provenance-traceable
            assert all(e["source_id"] and e["content_hash"] and e["observed_at"] for e in events_r1)

            # At least 1 high-value clean event promoted into open Curiosity knowledge_topics
            topics = await repo.list_world_items(kind="knowledge_topic", status="open")
            assert len(topics) >= 1

            # 2. Re-poll immediately: 0% duplicate ingestion!
            res2 = await pipeline.poll_all(force=True)
            print("=== P1-5 Real Sensor Poll #2 (Dedup Check) ===", res2)
            assert all(r.get("inserted", 0) == 0 for r in res2)
            assert len(await repo.list_world_events(limit=100)) == len(events_r1)

            # 3. Verify 4th sensor class: enhanced SystemSensor (CPU/mem/disk + DB ping + error rate)
            settings = Settings()
            sys_sensor = SystemSensor(
                settings.perception.model_copy(update={"llm_probe": False}),
                settings.llm,
                repo=repo,
                db_ping_fn=db.ping,
            )
            obs = await sys_sensor.observe()
            assert obs.system_state["database"]["ok"] is True
            assert "error_rate_15m" in obs.system_state
            assert obs.system_state["disk"]["free_ratio"] > 0.0
        finally:
            await db.dispose()

    asyncio.run(_run())


@pytest.mark.integration
def test_pg_0006_world_sensors_snapshot_migration():
    """Requires AOC_TEST_PG_URL pointing to a pgvector PG16 disposable DB restored from aoc_pg_dump_20261120.sql."""
    async_url = os.environ.get("AOC_TEST_PG_URL", "").strip()
    if not async_url:
        pytest.skip("AOC_TEST_PG_URL not set")

    env = os.environ.copy()
    env["AOC_DATABASE__URL"] = async_url

    def run_alembic(*args: str) -> None:
        res = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=str(PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, f"alembic {args} failed:\n{res.stdout}\n{res.stderr}"

    run_alembic("upgrade", "0006_p1_5_world_sensors")
    run_alembic("upgrade", "0006_p1_5_world_sensors")

    async def _check():
        db = Database(async_url)
        try:
            async with db.engine.connect() as conn:
                tables = await conn.run_sync(lambda sc: set(inspect(sc).get_table_names()))
                assert {"world_event_sources", "sensor_checkpoints", "world_events"}.issubset(tables)
                # Original V1.0 rows preserved
                assert (await conn.execute(text("SELECT count(*) FROM memory_index"))).scalar_one() == 58
                assert (await conn.execute(text("SELECT count(*) FROM agent_tasks"))).scalar_one() == 82
        finally:
            await db.dispose()

    asyncio.run(_check())
    run_alembic("downgrade", "0005_p1_4_semantic_memory")
    run_alembic("upgrade", "0006_p1_5_world_sensors")
