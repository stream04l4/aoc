"""AOC V2.1 P0-1 & P1-4 Idempotent Data Reconciliation Script.

Safely audits (default: --dry-run) and optionally reconciles (--execute):
  1. Superseded retry attempts in `agent_executions` where `finalized_at IS NULL`
     AND parent `agent_tasks.finalized_at IS NOT NULL` AND a later finalized
     execution exists for the same task.
     - NEVER silently hides unexplained anomalies where the parent task is NOT finalized!
  2. Legacy V1 `Organism Identity: AOC-E2E-*` rows remaining in `memory_index`.
     - Archives full JSON records before removal.
     - Hard-guards `Organism Identity: AOC-001` so production identity is NEVER touched.

Usage:
    # Read-only audit (safe on production DB)
    .venv/bin/python scripts/reconcile_v2_1_data.py --dry-run

    # Execute reconciliation (only after Joel's explicit production approval)
    .venv/bin/python scripts/reconcile_v2_1_data.py --execute \
        --archive-out backups/aoc_v2_1_legacy_e2e_archive.json
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


async def audit_and_reconcile(
    db_url: str,
    *,
    execute: bool = False,
    archive_out: str | None = None,
) -> dict[str, Any]:
    db = Database(db_url)
    report: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": "execute" if execute else "dry_run",
        "superseded_retries": [],
        "unexplained_unfinalized_execs": [],
        "legacy_e2e_identities": [],
        "aoc_001_identity_verified": False,
        "actions_taken": {
            "executions_backfilled": 0,
            "e2e_memories_archived": 0,
            "archive_path": None,
        },
    }

    try:
        async with db.engine.connect() as conn:
            # 1. Verify AOC-001 identity presence (hard safety invariant)
            aoc001_cnt = int(
                (
                    await conn.execute(
                        text("SELECT count(*) FROM organism_identity WHERE name = 'AOC-001'")
                    )
                ).scalar_one()
                or 0
            )
            report["aoc_001_identity_verified"] = aoc001_cnt >= 1

            # 2. Classify all terminal executions with finalized_at IS NULL
            exec_rows = (
                await conn.execute(
                    text(
                        """
                        SELECT e.id AS exec_id,
                               e.task_id,
                               e.attempt_number,
                               e.status AS exec_status,
                               e.completed_at,
                               e.verification,
                               t.status AS task_status,
                               t.finalized_at AS task_finalized_at,
                               (SELECT count(*) FROM agent_executions e2
                                WHERE e2.task_id = e.task_id AND e2.finalized_at IS NOT NULL) AS finalized_siblings,
                               (SELECT max(e3.attempt_number) FROM agent_executions e3
                                WHERE e3.task_id = e.task_id) AS max_attempt
                        FROM agent_executions e
                        JOIN agent_tasks t ON t.id = e.task_id
                        WHERE e.status IN ('SUCCEEDED', 'FAILED', 'TIMEOUT', 'CANCELLED')
                          AND e.finalized_at IS NULL
                        ORDER BY e.started_at ASC
                        """
                    )
                )
            ).mappings().all()

            for r in exec_rows:
                rec = {
                    "execution_id": str(r["exec_id"]),
                    "task_id": str(r["task_id"]),
                    "attempt_number": int(r["attempt_number"] or 1),
                    "max_attempt": int(r["max_attempt"] or 1),
                    "exec_status": r["exec_status"],
                    "task_status": r["task_status"],
                    "task_finalized_at": (
                        r["task_finalized_at"].isoformat()
                        if hasattr(r["task_finalized_at"], "isoformat")
                        else r["task_finalized_at"]
                    ),
                    "finalized_siblings": int(r["finalized_siblings"] or 0),
                }
                is_verified_superseded = (
                    r["task_finalized_at"] is not None
                    and int(r["finalized_siblings"] or 0) >= 1
                    and int(r["attempt_number"] or 1) < int(r["max_attempt"] or 1)
                )
                if is_verified_superseded:
                    report["superseded_retries"].append(rec)
                else:
                    report["unexplained_unfinalized_execs"].append(rec)

            # 3. Identify strictly legacy V1 AOC-E2E-* identity rows in memory_index
            e2e_rows = (
                await conn.execute(
                    text(
                        """
                        SELECT id, memory_type, external_id, title, summary, content_hash,
                               source, importance, tags, cycle_id, sync_status, embedding_status, created_at
                        FROM memory_index
                        WHERE memory_type = 'identity'
                          AND title LIKE 'Organism Identity: AOC-E2E-%'
                          AND title NOT LIKE '%AOC-001%'
                        ORDER BY created_at ASC
                        """
                    )
                )
            ).mappings().all()

            for r in e2e_rows:
                report["legacy_e2e_identities"].append(
                    {
                        "id": str(r["id"]),
                        "memory_type": r["memory_type"],
                        "external_id": r["external_id"],
                        "title": r["title"],
                        "summary": r["summary"],
                        "content_hash": r["content_hash"],
                        "source": r["source"],
                        "importance": float(r["importance"]),
                        "sync_status": r["sync_status"],
                        "embedding_status": r["embedding_status"],
                        "created_at": (
                            r["created_at"].isoformat()
                            if hasattr(r["created_at"], "isoformat")
                            else str(r["created_at"])
                        ),
                    }
                )

        if execute:
            if not report["aoc_001_identity_verified"]:
                raise RuntimeError("Safety abort: AOC-001 identity record not found; refusing to modify DB")

            if report["legacy_e2e_identities"] and archive_out:
                out_path = Path(archive_out)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(
                    json.dumps(
                        {
                            "archived_at": datetime.now(timezone.utc).isoformat(),
                            "reason": "AOC V2.1 P1-4 cleanup of legacy V1 E2E identity rows",
                            "records": report["legacy_e2e_identities"],
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                report["actions_taken"]["archive_path"] = str(out_path)

            async with db.engine.begin() as conn:
                # Backfill ONLY verified superseded retry attempts
                for rec in report["superseded_retries"]:
                    res = await conn.execute(
                        text(
                            """
                            UPDATE agent_executions
                            SET finalized_at = (
                                SELECT t.finalized_at FROM agent_tasks t WHERE t.id = agent_executions.task_id
                            )
                            WHERE id = :eid
                              AND finalized_at IS NULL
                            """
                        ),
                        {"eid": rec["execution_id"]},
                    )
                    report["actions_taken"]["executions_backfilled"] += int(res.rowcount or 0)

                # Delete ONLY archived legacy AOC-E2E-* identity memories
                if report["legacy_e2e_identities"]:
                    if not archive_out:
                        raise RuntimeError("Refusing to remove legacy E2E memories without --archive-out")
                    for rec in report["legacy_e2e_identities"]:
                        res = await conn.execute(
                            text(
                                """
                                DELETE FROM memory_index
                                WHERE id = :mid
                                  AND memory_type = 'identity'
                                  AND title LIKE 'Organism Identity: AOC-E2E-%'
                                  AND title NOT LIKE '%AOC-001%'
                                """
                            ),
                            {"mid": rec["id"]},
                        )
                        report["actions_taken"]["e2e_memories_archived"] += int(res.rowcount or 0)
    finally:
        await db.dispose()

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="AOC V2.1 idempotent data reconciliation tool.")
    parser.add_argument("--db-url", default="", help="Override AOC_DATABASE__URL")
    parser.add_argument("--dry-run", action="store_true", help="Audit only (default)")
    parser.add_argument("--execute", action="store_true", help="Apply verified reconciliations")
    parser.add_argument("--archive-out", default="", help="Archive JSON output path for legacy E2E memories")
    args = parser.parse_args()

    db_url = args.db_url or get_settings().database.url
    execute = bool(args.execute) and not bool(args.dry_run)
    report = asyncio.run(
        audit_and_reconcile(
            db_url,
            execute=execute,
            archive_out=args.archive_out or None,
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not report["unexplained_unfinalized_execs"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
