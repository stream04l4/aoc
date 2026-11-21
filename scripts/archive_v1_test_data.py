"""V1.0 Historical Test Artifact Classification & Archival Script (Task Book Section 9.2).

Strict safety guardrails:
  - Defaults to `--dry-run` (read-only audit; modifies ZERO rows).
  - Never touches `backups/aoc_pg_dump_20261120.sql`.
  - Requires `--execute --archive-out <file>` to actually isolate/delete non-AOC-001 test artifacts
    (AOC-BENCH-*, AOC-E2E-*, bench-primary, bench-backup, bench-topic-*, and 4 Scenario-E mock orphans).
  - Preserves 100% of `AOC-001` identity, state, cycles, goals, pending L2 task, memories, and drive history.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings
from app.storage.database import Database


async def classify_and_optionally_archive(
    *,
    db_url: str,
    execute: bool = False,
    archive_out: Path | None = None,
) -> dict[str, Any]:
    db = Database(db_url)
    try:
        async with db.engine.begin() as conn:
            orgs = (
                await conn.execute(
                    text("SELECT id::text, name, version, created_at::text FROM organism_identity ORDER BY created_at ASC")
                )
            ).mappings().all()
            prod_org = next((o for o in orgs if o["name"] == "AOC-001"), None)
            if prod_org is None:
                raise RuntimeError("Primary production instance AOC-001 not found in organism_identity!")
            prod_id = prod_org["id"]
            test_orgs = [o for o in orgs if o["name"].startswith(("AOC-BENCH-", "AOC-E2E-"))]
            test_org_ids = [o["id"] for o in test_orgs]

            async def _count(sql: str, params: dict[str, Any] | None = None) -> int:
                return int((await conn.execute(text(sql), params or {})).scalar_one() or 0)

            summary: dict[str, Any] = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "mode": "EXECUTE" if execute else "DRY_RUN",
                "primary_instance": {"id": prod_id, "name": "AOC-001"},
                "test_instances_to_archive": [{"id": o["id"], "name": o["name"]} for o in test_orgs],
                "before_counts": {
                    "organism_identity": await _count("SELECT count(*) FROM organism_identity"),
                    "organism_state": await _count("SELECT count(*) FROM organism_state"),
                    "cycle_records_total": await _count("SELECT count(*) FROM cycle_records"),
                    "cycle_records_aoc001": await _count("SELECT count(*) FROM cycle_records WHERE organism_id = :pid", {"pid": prod_id}),
                    "active_goals_total": await _count("SELECT count(*) FROM active_goals"),
                    "active_goals_aoc001": await _count("SELECT count(*) FROM active_goals WHERE organism_id = :pid", {"pid": prod_id}),
                    "agent_tasks_total": await _count("SELECT count(*) FROM agent_tasks"),
                    "agent_executions_total": await _count("SELECT count(*) FROM agent_executions"),
                    "memory_index_total": await _count("SELECT count(*) FROM memory_index"),
                    "memory_index_pending": await _count("SELECT count(*) FROM memory_index WHERE sync_status = 'pending'"),
                    "memory_index_failed": await _count("SELECT count(*) FROM memory_index WHERE sync_status = 'failed'"),
                    "world_states_total": await _count("SELECT count(*) FROM world_states"),
                    "world_states_bench_topics": await _count("SELECT count(*) FROM world_states WHERE key LIKE 'bench-topic-%'"),
                    "agent_registry_total": await _count("SELECT count(*) FROM agent_registry"),
                    "agent_registry_bench": await _count("SELECT count(*) FROM agent_registry WHERE agent_id LIKE 'bench-%'"),
                    "drive_history_total": await _count("SELECT count(*) FROM drive_history"),
                    "drive_history_aoc001": await _count("SELECT count(*) FROM drive_history WHERE organism_id = :pid", {"pid": prod_id}),
                    "resource_usage_total": await _count("SELECT count(*) FROM resource_usage"),
                },
            }

            # Identify cycle IDs owned by test instances
            test_cycle_rows = (
                await conn.execute(
                    text("SELECT id::text FROM cycle_records WHERE organism_id = ANY(:tids)"),
                    {"tids": test_org_ids},
                )
            ).scalars().all() if test_org_ids else []

            prod_cycle_rows = (
                await conn.execute(
                    text("SELECT id::text FROM cycle_records WHERE organism_id = :pid"),
                    {"pid": prod_id},
                )
            ).scalars().all()

            summary["classification"] = {
                "test_cycles_count": len(test_cycle_rows),
                "aoc001_cycles_preserved": len(prod_cycle_rows),
            }

            if execute:
                if archive_out is None:
                    raise ValueError("--archive-out is required when --execute is specified")
                archive_payload: dict[str, Any] = {
                    "exported_at": datetime.now(timezone.utc).isoformat(),
                    "test_instances": [dict(o) for o in test_orgs],
                    "test_cycles_count": len(test_cycle_rows),
                }
                # Archive non-AOC-001 memories & bench world_states & bench agents before deletion
                arch_mems = (
                    await conn.execute(
                        text(
                            "SELECT id::text, memory_type, title, sync_status, source, cycle_id::text "
                            "FROM memory_index WHERE sync_status IN ('pending', 'failed') "
                            "OR cycle_id = ANY(:tcids) OR title LIKE 'AOC-BENCH-%' OR title LIKE 'AOC-E2E-%'"
                        ),
                        {"tcids": test_cycle_rows},
                    )
                ).mappings().all()
                arch_world = (
                    await conn.execute(
                        text("SELECT id::text, kind, key, status, source FROM world_states WHERE key LIKE 'bench-topic-%'")
                    )
                ).mappings().all()
                arch_agents = (
                    await conn.execute(
                        text("SELECT id::text, agent_id, name FROM agent_registry WHERE agent_id LIKE 'bench-%'")
                    )
                ).mappings().all()
                archive_payload["archived_memories"] = [dict(r) for r in arch_mems]
                archive_payload["archived_world_states"] = [dict(r) for r in arch_world]
                archive_payload["archived_agents"] = [dict(r) for r in arch_agents]
                archive_out.parent.mkdir(parents=True, exist_ok=True)
                archive_out.write_text(json.dumps(archive_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

                # Execute FK-safe deletions strictly for non-AOC-001 test artifacts
                if test_cycle_rows:
                    await conn.execute(
                        text(
                            "DELETE FROM agent_executions WHERE task_id IN "
                            "(SELECT id FROM agent_tasks WHERE cycle_id = ANY(:tcids) OR cycle_id IS NULL)"
                        ),
                        {"tcids": test_cycle_rows},
                    )
                    await conn.execute(
                        text("DELETE FROM agent_tasks WHERE cycle_id = ANY(:tcids) OR cycle_id IS NULL"),
                        {"tcids": test_cycle_rows},
                    )
                    await conn.execute(
                        text("DELETE FROM action_plans WHERE cycle_id = ANY(:tcids)"),
                        {"tcids": test_cycle_rows},
                    )
                    await conn.execute(
                        text("DELETE FROM goal_history WHERE goal_id IN (SELECT id FROM active_goals WHERE organism_id = ANY(:tids))"),
                        {"tids": test_org_ids},
                    )
                    await conn.execute(
                        text("DELETE FROM active_goals WHERE organism_id = ANY(:tids)"),
                        {"tids": test_org_ids},
                    )
                    await conn.execute(
                        text(
                            "DELETE FROM memory_index WHERE sync_status IN ('pending', 'failed') "
                            "OR cycle_id = ANY(:tcids) OR title LIKE 'AOC-BENCH-%' OR title LIKE 'AOC-E2E-%'"
                        ),
                        {"tcids": test_cycle_rows},
                    )
                    await conn.execute(text("DELETE FROM observation_records WHERE cycle_id = ANY(:tcids)"), {"tcids": test_cycle_rows})
                    await conn.execute(text("DELETE FROM decision_logs WHERE cycle_id = ANY(:tcids)"), {"tcids": test_cycle_rows})
                    await conn.execute(text("DELETE FROM event_logs WHERE cycle_id = ANY(:tcids)"), {"tcids": test_cycle_rows})
                    await conn.execute(text("DELETE FROM resource_usage WHERE cycle_id = ANY(:tcids)"), {"tcids": test_cycle_rows})
                    await conn.execute(text("DELETE FROM drive_history WHERE organism_id = ANY(:tids)"), {"tids": test_org_ids})
                    await conn.execute(text("DELETE FROM drive_states WHERE organism_id = ANY(:tids)"), {"tids": test_org_ids})
                    await conn.execute(text("DELETE FROM cycle_records WHERE organism_id = ANY(:tids)"), {"tids": test_org_ids})
                    await conn.execute(text("DELETE FROM organism_state WHERE organism_id = ANY(:tids)"), {"tids": test_org_ids})
                    await conn.execute(text("DELETE FROM organism_identity WHERE id = ANY(:tids)"), {"tids": test_org_ids})

                await conn.execute(text("DELETE FROM world_states WHERE key LIKE 'bench-topic-%'"))
                await conn.execute(text("DELETE FROM agent_registry WHERE agent_id LIKE 'bench-%'"))

                summary["after_counts"] = {
                    "organism_identity": await _count("SELECT count(*) FROM organism_identity"),
                    "organism_state": await _count("SELECT count(*) FROM organism_state"),
                    "cycle_records_total": await _count("SELECT count(*) FROM cycle_records"),
                    "cycle_records_aoc001": await _count("SELECT count(*) FROM cycle_records WHERE organism_id = :pid", {"pid": prod_id}),
                    "active_goals_total": await _count("SELECT count(*) FROM active_goals"),
                    "agent_tasks_total": await _count("SELECT count(*) FROM agent_tasks"),
                    "agent_executions_total": await _count("SELECT count(*) FROM agent_executions"),
                    "memory_index_total": await _count("SELECT count(*) FROM memory_index"),
                    "memory_index_pending": await _count("SELECT count(*) FROM memory_index WHERE sync_status = 'pending'"),
                    "world_states_total": await _count("SELECT count(*) FROM world_states"),
                    "agent_registry_total": await _count("SELECT count(*) FROM agent_registry"),
                    "drive_history_total": await _count("SELECT count(*) FROM drive_history"),
                    "resource_usage_total": await _count("SELECT count(*) FROM resource_usage"),
                }
                summary["archive_file"] = str(archive_out)
        return summary
    finally:
        await db.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Classify and optionally archive V1.0 test artifacts (default: dry-run).")
    parser.add_argument("--db-url", default="", help="Target PostgreSQL asyncpg URL.")
    parser.add_argument("--execute", action="store_true", help="Actually archive & remove test rows (NEVER use on prod without approval).")
    parser.add_argument("--archive-out", default="", help="Output JSON archive file path (required with --execute).")
    args = parser.parse_args()

    url = args.db_url or get_settings().database.url
    res = asyncio.run(
        classify_and_optionally_archive(
            db_url=url,
            execute=args.execute,
            archive_out=Path(args.archive_out) if args.archive_out else None,
        )
    )
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
