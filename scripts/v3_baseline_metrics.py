"""AOC V3.0 Unified Read-Only Baseline & Comparison Metrics Collector (M0).

Guarantees identical SQL definitions across V2.1 baseline and V3.0 M1-M4 evaluations
so improvements are measured apples-to-apples without changing metric definitions.
Strictly READ-ONLY: executes SELECT statements only, never writes to PostgreSQL.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings
from app.storage.database import Database

_WRITE_INTENT_RE = re.compile(
    r"(write\s+to|save\s+to|create\s+file|output\s+file|result\.md|写入|保存为|输出文件|生成文件)",
    re.IGNORECASE,
)


async def collect_metrics(db_url: str, window_hours: float = 24.0) -> dict[str, Any]:
    db = Database(db_url)
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=window_hours) if window_hours > 0 else datetime(2000, 1, 1, tzinfo=timezone.utc)

    report: dict[str, Any] = {
        "schema_version": "v3_baseline_metrics_v1",
        "collected_at": now.isoformat(),
        "window_hours": window_hours,
        "window_since": since.isoformat() if window_hours > 0 else "all_time",
    }

    try:
        async with db.engine.connect() as conn:
            # 1. Core metadata & schema version
            alembic_ver = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one_or_none()
            exts = {
                r[0]: r[1]
                for r in (await conn.execute(text("SELECT extname, extversion FROM pg_extension ORDER BY extname"))).all()
            }
            org = (
                await conn.execute(
                    text(
                        """
                        SELECT oi.name, oi.version, os.lifecycle_state, os.cycle_count
                        FROM organism_identity oi
                        JOIN organism_state os ON os.organism_id = oi.id
                        WHERE oi.name = 'AOC-001'
                        LIMIT 1
                        """
                    )
                )
            ).mappings().first()

            tables = [
                r[0]
                for r in (
                    await conn.execute(
                        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
                    )
                ).all()
            ]
            table_counts = {
                t: int((await conn.execute(text(f'SELECT count(*) FROM "{t}"'))).scalar_one() or 0) for t in tables
            }
            report["system"] = {
                "alembic_version": alembic_ver,
                "extensions": exts,
                "organism": dict(org) if org else None,
                "tables_total": len(tables),
                "table_counts": table_counts,
            }

            # 2. Cycle outcomes in window
            cycle_rows = (
                await conn.execute(
                    text(
                        """
                        SELECT COALESCE(outcome, 'IN_FLIGHT') AS outcome, count(*) AS cnt
                        FROM cycle_records
                        WHERE started_at >= :since
                        GROUP BY COALESCE(outcome, 'IN_FLIGHT')
                        ORDER BY cnt DESC
                        """
                    ),
                    {"since": since},
                )
            ).mappings().all()
            cycles_by_outcome = {r["outcome"]: int(r["cnt"]) for r in cycle_rows}
            report["cycles"] = {
                "total": sum(cycles_by_outcome.values()),
                "by_outcome": cycles_by_outcome,
            }

            # 3. Tasks & retries & L0 read-only write-intent conflict audit
            task_rows = (
                await conn.execute(
                    text(
                        """
                        SELECT id, objective, permission_level, status, attempt, max_attempts, finalized_at
                        FROM agent_tasks
                        WHERE created_at >= :since
                        """
                    ),
                    {"since": since},
                )
            ).mappings().all()
            tasks_by_status: dict[str, int] = {}
            retried_tasks = 0
            l0_write_conflict_tasks = 0
            l0_write_conflict_failed = 0
            unfinalized_terminal_tasks = 0
            for t in task_rows:
                st = str(t["status"] or "UNKNOWN")
                tasks_by_status[st] = tasks_by_status.get(st, 0) + 1
                if int(t["attempt"] or 1) > 1:
                    retried_tasks += 1
                if st in {"SUCCEEDED", "FAILED", "CANCELLED", "TIMEOUT"} and t["finalized_at"] is None:
                    unfinalized_terminal_tasks += 1
                if str(t["permission_level"] or "") == "L0" and _WRITE_INTENT_RE.search(str(t["objective"] or "")):
                    l0_write_conflict_tasks += 1
                    if st != "SUCCEEDED":
                        l0_write_conflict_failed += 1

            exec_rows = (
                await conn.execute(
                    text(
                        """
                        SELECT status, attempt_number, finalized_at
                        FROM agent_executions
                        WHERE started_at >= :since
                        """
                    ),
                    {"since": since},
                )
            ).mappings().all()
            execs_total = len(exec_rows)
            retry_attempts = sum(1 for e in exec_rows if int(e["attempt_number"] or 1) > 1)
            unfinalized_terminal_execs = sum(
                1
                for e in exec_rows
                if str(e["status"] or "") in {"SUCCEEDED", "FAILED", "CANCELLED", "TIMEOUT"}
                and e["finalized_at"] is None
            )

            report["tasks_and_executions"] = {
                "tasks_total": len(task_rows),
                "tasks_by_status": tasks_by_status,
                "retried_tasks": retried_tasks,
                "retry_rate": round(retried_tasks / len(task_rows), 4) if task_rows else 0.0,
                "executions_total": execs_total,
                "retry_attempts": retry_attempts,
                "l0_write_intent_tasks": l0_write_conflict_tasks,
                "l0_write_intent_failed": l0_write_conflict_failed,
                "unfinalized_terminal_tasks": unfinalized_terminal_tasks,
                "unfinalized_terminal_execs": unfinalized_terminal_execs,
            }

            # 4. Goals & approximate repetition rate (normalized title + related_key)
            goal_rows = (
                await conn.execute(
                    text(
                        """
                        SELECT drive_type, title, related_key, status
                        FROM active_goals
                        WHERE created_at >= :since
                        """
                    ),
                    {"since": since},
                )
            ).mappings().all()
            seen_keys: set[str] = set()
            dup_goals = 0
            for g in goal_rows:
                norm = re.sub(r"\s+", " ", str(g["related_key"] or g["title"] or "").strip().lower())[:120]
                if norm in seen_keys:
                    dup_goals += 1
                else:
                    seen_keys.add(norm)
            report["goals"] = {
                "total": len(goal_rows),
                "unique_keys": len(seen_keys),
                "repeated_key_goals": dup_goals,
                "repetition_rate": round(dup_goals / len(goal_rows), 4) if goal_rows else 0.0,
            }

            # 5. Token consumption & efficiency (Verified IG / 1M Tokens)
            usage_rows = (
                await conn.execute(
                    text(
                        """
                        SELECT amount, seconds, details, task_id
                        FROM resource_usage
                        WHERE category = 'llm' AND created_at >= :since
                        """
                    ),
                    {"since": since},
                )
            ).mappings().all()
            total_tokens = 0.0
            by_source: dict[str, float] = {}
            calls_by_source: dict[str, int] = {}
            failed_task_ids = {
                t["id"] for t in task_rows if str(t["status"] or "") in {"FAILED", "TIMEOUT", "CANCELLED"}
            }
            failed_task_tokens = 0.0
            for u in usage_rows:
                amt = float(u["amount"] or 0.0)
                total_tokens += amt
                det = u["details"] if isinstance(u["details"], dict) else {}
                src = str(det.get("source") or det.get("operation") or det.get("stage") or "unknown")
                by_source[src] = by_source.get(src, 0.0) + amt
                calls_by_source[src] = calls_by_source.get(src, 0) + 1
                if u["task_id"] in failed_task_ids:
                    failed_task_tokens += amt

            mem_rows = (
                await conn.execute(
                    text(
                        """
                        SELECT information_gain, sync_status, embedding_status
                        FROM memory_index
                        WHERE created_at >= :since
                        """
                    ),
                    {"since": since},
                )
            ).mappings().all()
            ig_values = [float(m["information_gain"]) for m in mem_rows if m["information_gain"] is not None]
            total_ig = sum(ig_values)
            avg_ig = (total_ig / len(ig_values)) if ig_values else 0.0
            ig_per_1m_tokens = (total_ig / (total_tokens / 1_000_000.0)) if total_tokens > 0 else 0.0

            report["tokens_and_ig"] = {
                "llm_calls": len(usage_rows),
                "total_tokens": round(total_tokens, 1),
                "tokens_by_source": {k: round(v, 1) for k, v in sorted(by_source.items(), key=lambda kv: -kv[1])},
                "calls_by_source": calls_by_source,
                "failed_task_tokens": round(failed_task_tokens, 1),
                "failed_task_token_ratio": round(failed_task_tokens / total_tokens, 4) if total_tokens > 0 else 0.0,
                "new_memories_window": len(mem_rows),
                "memories_with_ig": len(ig_values),
                "sum_information_gain": round(total_ig, 4),
                "avg_information_gain": round(avg_ig, 4),
                "verified_ig_per_1m_tokens": round(ig_per_1m_tokens, 4),
            }

            # 6. Drives, Self Model, Skills, Memory Sync, World Events, Affect
            drives = (
                await conn.execute(
                    text("SELECT drive_type, satisfaction, deficit, dimensions FROM drive_states ORDER BY drive_type")
                )
            ).mappings().all()
            caps = (
                await conn.execute(
                    text(
                        """
                        SELECT capability_key, maturity_status, sample_count, estimated_success_rate
                        FROM capability_profiles
                        ORDER BY sample_count DESC, capability_key ASC
                        """
                    )
                )
            ).mappings().all()
            skills_by_status = {
                r[0]: int(r[1])
                for r in (
                    await conn.execute(text("SELECT status, count(*) FROM skill_registry GROUP BY status"))
                ).all()
            }
            mem_sync = {
                r[0]: int(r[1])
                for r in (
                    await conn.execute(text("SELECT sync_status, count(*) FROM memory_index GROUP BY sync_status"))
                ).all()
            }
            outbox = {
                r[0]: int(r[1])
                for r in (
                    await conn.execute(text("SELECT status, count(*) FROM outbox_events GROUP BY status"))
                ).all()
            }
            report["state_snapshot"] = {
                "drives": [
                    {
                        "drive_type": d["drive_type"],
                        "satisfaction": round(float(d["satisfaction"]), 4),
                        "deficit": round(float(d["deficit"]), 4),
                        "curiosity_ig_dim": (
                            round(float((d["dimensions"] or {}).get("information_gain", 0.0)), 4)
                            if d["drive_type"] == "curiosity"
                            else None
                        ),
                    }
                    for d in drives
                ],
                "capabilities": [
                    {
                        "capability_key": c["capability_key"],
                        "maturity_status": c["maturity_status"],
                        "sample_count": int(c["sample_count"]),
                        "estimated_success_rate": round(float(c["estimated_success_rate"]), 4),
                    }
                    for c in caps
                ],
                "skills_by_status": skills_by_status,
                "memory_sync_all_time": mem_sync,
                "outbox_all_time": outbox,
                "world_events_total": table_counts.get("world_events", 0),
                "affect_history_total": table_counts.get("affect_history", 0),
            }
    finally:
        await db.dispose()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect unified read-only V3.0 baseline/comparison metrics.")
    parser.add_argument("--db-url", default="", help="PostgreSQL async URL (default: from AOC settings)")
    parser.add_argument("--hours", type=float, default=24.0, help="Lookback window hours (0 = all time)")
    parser.add_argument("--out", default="", help="Optional path to write JSON snapshot")
    args = parser.parse_args()

    db_url = args.db_url or get_settings().database.url
    data = asyncio.run(collect_metrics(db_url, window_hours=args.hours))
    rendered = json.dumps(data, ensure_ascii=False, indent=2)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
