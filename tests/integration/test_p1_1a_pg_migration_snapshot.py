"""P1-1a PostgreSQL integration test: verifies 0002 migration upgrade/downgrade
against a temporary PostgreSQL database restored from the unmodified production snapshot
`backups/aoc_pg_dump_20261120.sql`.
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

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_PATH = PROJECT_ROOT / "backups" / "aoc_pg_dump_20261120.sql"


@pytest.mark.integration
def test_pg_0002_migration_on_real_production_snapshot_copy():
    """Requires AOC_TEST_PG_URL pointing to a disposable PostgreSQL DB loaded with aoc_pg_dump_20261120.sql."""
    async_url = os.environ.get("AOC_TEST_PG_URL", "").strip()
    if not async_url:
        pytest.skip("AOC_TEST_PG_URL not set (disposable PostgreSQL test DB required)")
    assert SNAPSHOT_PATH.exists(), f"Missing snapshot: {SNAPSHOT_PATH}"

    env = os.environ.copy()
    env["AOC_DATABASE__URL"] = async_url

    def run_alembic(*args: str) -> subprocess.CompletedProcess[str]:
        res = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=str(PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, f"alembic {args} failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        return res

    # 1. Verify current revision is 0001_initial_schema on restored snapshot
    cur = run_alembic("current")
    assert "0001_initial_schema" in cur.stdout or "0002_p1_1_security_budget" in cur.stdout

    # 2. Upgrade to 0002_p1_1_security_budget (idempotent even if re-run)
    run_alembic("upgrade", "0002_p1_1_security_budget")
    run_alembic("upgrade", "0002_p1_1_security_budget")

    # 3. Verify schema + zero data loss on original snapshot rows using asyncpg
    async def _verify_upgraded() -> None:
        db = Database(async_url)
        try:
            async with db.engine.connect() as conn:
                tables = await conn.run_sync(lambda sc: set(inspect(sc).get_table_names()))
                assert {"api_credentials", "approval_records", "policy_evaluations"}.issubset(tables)

                os_cols = await conn.run_sync(lambda sc: {c["name"] for c in inspect(sc).get_columns("organism_state")})
                assert {"quota_reset_at", "throttled_reason"}.issubset(os_cols)

                ap_cols = await conn.run_sync(lambda sc: {c["name"] for c in inspect(sc).get_columns("action_plans")})
                assert "plan_hash" in ap_cols

                # Verify unmodified production snapshot counts remain intact
                assert (await conn.execute(text("SELECT count(*) FROM organism_identity"))).scalar_one() == 8
                assert (await conn.execute(text("SELECT count(*) FROM cycle_records"))).scalar_one() == 432
                assert (await conn.execute(text("SELECT count(*) FROM agent_tasks"))).scalar_one() == 82
                assert (await conn.execute(text("SELECT count(*) FROM memory_index"))).scalar_one() == 58
                assert (
                    await conn.execute(
                        text("SELECT lifecycle_state FROM organism_state os JOIN organism_identity oi ON os.organism_id = oi.id WHERE oi.name = 'AOC-001'")
                    )
                ).scalar_one() == "PAUSED"
        finally:
            await db.dispose()

    asyncio.run(_verify_upgraded())

    # 4. Verify clean downgrade to 0001 and re-upgrade to 0002 without losing snapshot rows
    run_alembic("downgrade", "0001_initial_schema")
    run_alembic("upgrade", "0002_p1_1_security_budget")
    asyncio.run(_verify_upgraded())
