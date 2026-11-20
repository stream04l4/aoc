"""P1-2 PostgreSQL integration tests:
  1. Verifies 0003 migration upgrade/downgrade on a disposable PG DB restored from `aoc_pg_dump_20261120.sql`.
  2. Verifies concurrent `SELECT ... FOR UPDATE SKIP LOCKED` outbox claiming across two parallel workers.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from app.storage.database import Database
from app.storage.outbox import OutboxDispatcher, OutboxEventType
from app.storage.repository import StateRepository

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.integration
def test_pg_0003_migration_and_concurrent_skip_locked_outbox():
    """Requires AOC_TEST_PG_URL pointing to a disposable PostgreSQL DB restored from aoc_pg_dump_20261120.sql."""
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
        assert res.returncode == 0, f"alembic {args} failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"

    # 1. Upgrade original snapshot directly from 0001 -> 0002 -> 0003 (and verify idempotency)
    run_alembic("upgrade", "0003_p1_2_durable_tasks")
    run_alembic("upgrade", "0003_p1_2_durable_tasks")

    async def _verify_schema_and_outbox() -> None:
        db = Database(async_url)
        repo = StateRepository(db)
        try:
            async with db.engine.connect() as conn:
                tables = await conn.run_sync(lambda sc: set(inspect(sc).get_table_names()))
                assert {"task_lifecycle_events", "outbox_events", "inbox_events"}.issubset(tables)
                assert (await conn.execute(text("SELECT count(*) FROM agent_tasks"))).scalar_one() == 82
                assert (await conn.execute(text("SELECT count(*) FROM agent_executions"))).scalar_one() == 81

            # Enqueue 12 events and claim concurrently with two independent OutboxDispatchers (SKIP LOCKED!)
            for i in range(12):
                await repo.enqueue_outbox_event(
                    event_type=OutboxEventType.NOTIFICATION_DISPATCH,
                    idempotency_key=f"pg-skip-lock-{i}",
                    payload={"idx": i},
                )

            seen_w1: list[str] = []
            seen_w2: list[str] = []
            d1 = OutboxDispatcher(repo, batch_size=6)
            d2 = OutboxDispatcher(repo, batch_size=6)

            async def h1(ev):
                await asyncio.sleep(0.02)
                seen_w1.append(ev["idempotency_key"])
                return {"worker": 1}

            async def h2(ev):
                await asyncio.sleep(0.02)
                seen_w2.append(ev["idempotency_key"])
                return {"worker": 2}

            d1.register_handler(OutboxEventType.NOTIFICATION_DISPATCH, "pg_consumer", h1)
            d2.register_handler(OutboxEventType.NOTIFICATION_DISPATCH, "pg_consumer", h2)

            await asyncio.gather(d1.dispatch_once(batch_size=6), d2.dispatch_once(batch_size=6))
            # Drain any remaining if one worker finished earlier
            await d1.dispatch_once(batch_size=12)

            # Strictly disjoint sets & all 12 delivered exactly once!
            assert set(seen_w1).isdisjoint(set(seen_w2)), f"SKIP LOCKED overlap: {set(seen_w1) & set(seen_w2)}"
            assert len(seen_w1) + len(seen_w2) == 12
        finally:
            await db.dispose()

    asyncio.run(_verify_schema_and_outbox())

    # 2. Verify clean downgrade to 0002 and re-upgrade to 0003
    run_alembic("downgrade", "0002_p1_1_security_budget")
    run_alembic("upgrade", "0003_p1_2_durable_tasks")
