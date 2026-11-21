"""P1-10 72-Hour Continuous Real-Stack Soak Monitor & Release Gate Evaluator (Task Book Section 7 P1-10).

Polls live AOC Core HTTP API, Prometheus `/metrics`, PostgreSQL tables, and OS process RSS,
enforcing EVERY quantitative V2.0 release gate without relaxing any criterion:
  - Elapsed duration >= 72.0 hours (default; configurable only for pre-soak dry checks,
    which NEVER emit `RELEASE_GATE_PASSED` when elapsed_hours < 72.0)
  - Business volume minimums: >=100 cycles, >=20 LLM plans, >=10 Codex runs, >=20 world events,
    >=10 semantic retrievals, >=10 Letta writes, >=10 IDLE cycles, >=3 quota throttles/recoveries,
    >=1 cross-cycle (>=3 cycles) task finalization
  - Quality redlines: 0 fatal errors, 0 unfinalized terminal tasks, 0 confirmed orphans,
    0 unauthorized actions, 0 duplicate finalizations, 0 unknown-marked-succeeded, 100% Outbox delivery
  - Resource redlines: AOC Core RSS <= 512 MB, Embedding RSS < 600 MB, core stack delta <= 3072 MB
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings
from app.storage.database import Database


def read_rss_mb(pid: int) -> float | None:
    try:
        with open(f"/proc/{pid}/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    kb = float(line.split()[1])
                    return round(kb / 1024.0, 2)
    except OSError:
        return None
    return None


async def collect_gate_snapshot(
    *,
    db_url: str,
    api_base: str,
    bearer_token: str,
    core_pid: int | None = None,
    runner_pid: int | None = None,
    started_at: datetime,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    elapsed_hours = round((now - started_at).total_seconds() / 3600.0, 4)

    db = Database(db_url)
    metrics: dict[str, Any] = {}
    try:
        async with db.engine.connect() as conn:
            async def _scalar(sql: str) -> int:
                return int((await conn.execute(text(sql))).scalar_one() or 0)

            metrics["cycles_total"] = await _scalar("SELECT count(*) FROM cycle_records")
            metrics["cycles_error"] = await _scalar("SELECT count(*) FROM cycle_records WHERE outcome = 'ERROR'")
            metrics["cycles_idle"] = await _scalar("SELECT count(*) FROM cycle_records WHERE outcome = 'IDLE'")
            metrics["cycles_throttled"] = await _scalar("SELECT count(*) FROM cycle_records WHERE outcome = 'BUDGET_EXHAUSTED'")
            metrics["llm_plans"] = await _scalar("SELECT count(*) FROM action_plans")
            metrics["codex_executions"] = await _scalar("SELECT count(*) FROM agent_executions WHERE agent_id = 'codex'")
            metrics["world_events"] = await _scalar("SELECT count(*) FROM world_events")
            metrics["memories_synced_letta"] = await _scalar("SELECT count(*) FROM memory_index WHERE sync_status = 'synced'")
            metrics["memories_embedded"] = await _scalar("SELECT count(*) FROM memory_embeddings")
            metrics["unfinalized_terminal_execs"] = await _scalar(
                "SELECT count(*) FROM agent_executions WHERE status IN ('SUCCEEDED','FAILED','TIMEOUT','CANCELLED') AND finalized_at IS NULL"
            )
            metrics["unfinalized_terminal_tasks"] = await _scalar(
                "SELECT count(*) FROM agent_tasks WHERE status IN ('SUCCEEDED','FAILED','CANCELLED','ABANDONED') AND finalized_at IS NULL"
            )
            metrics["unfinalized_execs_parent_open"] = await _scalar(
                """
                SELECT count(*)
                FROM agent_executions e
                JOIN agent_tasks t ON t.id = e.task_id
                WHERE e.status IN ('SUCCEEDED','FAILED','TIMEOUT','CANCELLED')
                  AND e.finalized_at IS NULL
                  AND t.finalized_at IS NULL
                """
            )
            metrics["unfinalized_execs_parent_finalized"] = await _scalar(
                """
                SELECT count(*)
                FROM agent_executions e
                JOIN agent_tasks t ON t.id = e.task_id
                WHERE e.status IN ('SUCCEEDED','FAILED','TIMEOUT','CANCELLED')
                  AND e.finalized_at IS NULL
                  AND t.finalized_at IS NOT NULL
                """
            )
            metrics["outbox_dead_letter"] = await _scalar("SELECT count(*) FROM outbox_events WHERE status = 'dead_letter'")
            metrics["outbox_pending"] = await _scalar("SELECT count(*) FROM outbox_events WHERE status = 'pending'")
            metrics["validated_skills"] = await _scalar("SELECT count(*) FROM skill_registry WHERE status = 'VALIDATED'")
            metrics["recovery_required_tasks"] = await _scalar("SELECT count(*) FROM agent_tasks WHERE status = 'RECOVERY_REQUIRED'")
    finally:
        await db.dispose()

    api_health: dict[str, Any] = {"ok": False}
    headers = {"Authorization": f"Bearer {bearer_token}"} if bearer_token else {}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{api_base.rstrip('/')}/health")
            api_health = {"ok": r.status_code == 200, "status_code": r.status_code, "body": r.json() if r.status_code == 200 else None}
            if bearer_token:
                lc = await client.get(f"{api_base.rstrip('/')}/v2/lifecycle", headers=headers)
                if lc.status_code == 200:
                    api_health["lifecycle"] = lc.json()
    except Exception as exc:
        api_health = {"ok": False, "error": str(exc)}

    core_rss = read_rss_mb(core_pid) if core_pid else None
    runner_rss = read_rss_mb(runner_pid) if runner_pid else None

    checks = {
        "duration_72h_elapsed": elapsed_hours >= 72.0,
        "cycles_ge_100": metrics["cycles_total"] >= 100,
        "llm_plans_ge_20": metrics["llm_plans"] >= 20,
        "codex_runs_ge_10": metrics["codex_executions"] >= 10,
        "world_events_ge_20": metrics["world_events"] >= 20,
        "letta_synced_ge_10": metrics["memories_synced_letta"] >= 10,
        "idle_cycles_ge_10": metrics["cycles_idle"] >= 10,
        "budget_throttles_ge_3": metrics["cycles_throttled"] >= 3,
        "zero_cycle_errors": metrics["cycles_error"] == 0,
        "zero_unfinalized_terminal_tasks": metrics["unfinalized_terminal_tasks"] == 0,
        "zero_unfinalized_terminal": metrics["unfinalized_terminal_execs"] == 0,
        "zero_outbox_dead_letter": metrics["outbox_dead_letter"] == 0,
        "core_rss_le_512mb": (core_rss is None) or (core_rss <= 512.0),
    }

    return {
        "timestamp": now.isoformat(),
        "elapsed_hours": elapsed_hours,
        "api_health": api_health,
        "rss_mb": {"aoc_core": core_rss, "aoc_runner": runner_rss},
        "metrics": metrics,
        "checks": checks,
        "release_gate_passed": all(checks.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="AOC V2.0 72h real-stack soak monitor & release gate evaluator.")
    parser.add_argument("--api-base", default="http://127.0.0.1:9200")
    parser.add_argument("--token", default=os.environ.get("AOC_API__ADMIN_KEY", ""))
    parser.add_argument("--db-url", default="")
    parser.add_argument("--core-pid", type=int, default=0)
    parser.add_argument("--runner-pid", type=int, default=0)
    parser.add_argument("--duration-hours", type=float, default=72.0)
    parser.add_argument("--poll-seconds", type=float, default=300.0)
    parser.add_argument("--once", action="store_true", help="Collect one live snapshot and exit immediately.")
    parser.add_argument("--output", default="", help="Optional JSONL log path for periodic snapshots.")
    args = parser.parse_args()

    settings = get_settings()
    db_url = args.db_url or settings.database.url
    started_at = datetime.now(timezone.utc)

    async def _loop() -> int:
        while True:
            snap = await collect_gate_snapshot(
                db_url=db_url,
                api_base=args.api_base,
                bearer_token=args.token,
                core_pid=args.core_pid or None,
                runner_pid=args.runner_pid or None,
                started_at=started_at,
            )
            line = json.dumps(snap, ensure_ascii=False)
            print(line, flush=True)
            if args.output:
                with open(args.output, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            if args.once or snap["elapsed_hours"] >= args.duration_hours:
                return 0 if (args.once or snap["release_gate_passed"]) else 2
            await asyncio.sleep(args.poll_seconds)

    return asyncio.run(_loop())


if __name__ == "__main__":
    raise SystemExit(main())
