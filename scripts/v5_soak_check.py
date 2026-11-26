"""Read-only V5.0 steady-state & resource-conservation monitor (M5).

Performs zero LLM calls and emits machine-readable JSON (`--output <path>`) checking:
  1. Host disk headroom >= configured safety floor (default 20%)
  2. Immutable ConstitutionGuard manifest integrity (all constitutional files present & hashed)
  3. Zero orphan candidate directories under `instances_root` (excluding `snapshots/`)
  4. Optional PostgreSQL/SQLite V5 control-plane invariants (live cap <= 2, 0 stuck CULLING, <5KB tombstones)
  5. Optional HTTP `/health` endpoint check
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, inspect, select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.evolution.constitution_guard import CONSTITUTIONAL_FILES, ConstitutionGuard
from app.evolution.interfaces import LIVE_INSTANCE_STATUSES, InstanceRole, InstanceStatus
from app.observability.topic_diversity import TopicDiversityAnalyzer
from app.storage.database import Database
from app.storage.models import (
    ActiveGoal,
    AgentExecutionRow,
    InstanceRegistryRow,
    LineageRecordRow,
    SkillRegistryRow,
    WorldState,
)


def _norm_utc(dt: Any) -> datetime | None:
    if isinstance(dt, datetime):
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    if isinstance(dt, str) and dt.strip():
        try:
            parsed = datetime.fromisoformat(dt.strip().replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        except ValueError:
            return None
    return None

_ACTIVE_CANDIDATE_STATUSES = (
    InstanceStatus.INITIALIZING.value,
    InstanceStatus.INCUBATING.value,
    InstanceStatus.TESTING.value,
)


def _disk_free_ratio(path: Path) -> tuple[str, float, int, int]:
    probe = path.resolve()
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    ratio = float(usage.free) / float(max(1, usage.total))
    return str(probe), round(ratio, 4), int(usage.free), int(usage.total)


async def run_v5_soak_check(
    *,
    repo_root: str | Path = PROJECT_ROOT,
    instances_root: str | Path = "/home/ubuntu/aoc-instances",
    min_free_disk_ratio: float = 0.20,
    max_concurrent_instances: int = 2,
    db_url: str = "",
    api_base: str = "",
    api_token: str = "",
) -> dict[str, Any]:
    """Run read-only V5 resource, constitution, orphan hygiene, and optional DB/API health checks."""
    failures: list[str] = []
    checks: dict[str, Any] = {}

    repo_path = Path(repo_root).resolve()
    inst_path = Path(instances_root).resolve()

    # 1. Disk headroom
    probe_path, free_ratio, free_bytes, total_bytes = _disk_free_ratio(inst_path)
    disk_ok = free_ratio >= float(min_free_disk_ratio)
    checks["disk"] = {
        "ok": disk_ok,
        "probe_path": probe_path,
        "free_ratio": free_ratio,
        "min_required_ratio": float(min_free_disk_ratio),
        "free_gb": round(free_bytes / (1024**3), 2),
        "total_gb": round(total_bytes / (1024**3), 2),
    }
    if not disk_ok:
        failures.append(
            f"Free disk ratio {free_ratio:.1%} below minimum {float(min_free_disk_ratio):.0%}"
        )

    # 2. Constitutional manifest integrity
    guard = ConstitutionGuard(repo_root=repo_path)
    manifest = guard.constitutional_manifest()
    present_const = manifest.get("constitutional_files") or {}
    missing_const = [rel for rel in CONSTITUTIONAL_FILES if rel not in present_const]
    const_ok = (len(missing_const) == 0) and (
        int(manifest.get("constitutional_file_count", 0)) == len(CONSTITUTIONAL_FILES)
    )
    checks["constitution"] = {
        "ok": const_ok,
        "expected_count": len(CONSTITUTIONAL_FILES),
        "present_count": int(manifest.get("constitutional_file_count", 0)),
        "missing_files": missing_const,
    }
    if not const_ok:
        failures.append(f"Constitutional files missing: {missing_const}")

    # 3. Optional DB control-plane check
    active_candidate_keys: set[str] = set()
    db_tables_present = False
    if (db_url or "").strip():
        db = Database(db_url.strip())
        try:
            async with db.engine.connect() as conn:
                schema = "public" if conn.dialect.name == "postgresql" else None
                tbls = set(await conn.run_sync(lambda sc: inspect(sc).get_table_names(schema=schema)))
            db_tables_present = {"instance_registry", "lineage_records"}.issubset(tbls)
            if not db_tables_present:
                checks["database"] = {
                    "ok": True,
                    "v5_tables_present": False,
                    "detail": "V5 evolution tables not present on target DB; skipped control-plane checks",
                }
            else:
                async with db.session() as s:
                    inst_rows = (await s.execute(select(InstanceRegistryRow))).scalars().all()
                    lin_rows = (await s.execute(select(LineageRecordRow))).scalars().all()

                live_count = sum(1 for r in inst_rows if r.status in LIVE_INSTANCE_STATUSES)
                stuck_culling = [r.instance_key for r in inst_rows if r.status == InstanceStatus.CULLING.value]
                active_candidate_keys = {
                    r.instance_key
                    for r in inst_rows
                    if r.role == InstanceRole.CANDIDATE.value and r.status in _ACTIVE_CANDIDATE_STATUSES
                }
                max_lesson_bytes = 0
                for lr in lin_rows:
                    sz = len(
                        json.dumps(lr.lesson_summary or {}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    )
                    if sz > max_lesson_bytes:
                        max_lesson_bytes = sz

                live_ok = live_count <= int(max_concurrent_instances)
                cull_ok = len(stuck_culling) == 0
                lesson_ok = max_lesson_bytes < 5120
                db_ok = live_ok and cull_ok and lesson_ok
                checks["database"] = {
                    "ok": db_ok,
                    "v5_tables_present": True,
                    "live_instances": live_count,
                    "max_concurrent_instances": int(max_concurrent_instances),
                    "active_candidate_keys": sorted(active_candidate_keys),
                    "stuck_culling_keys": stuck_culling,
                    "lineage_records_count": len(lin_rows),
                    "max_lesson_summary_bytes": max_lesson_bytes,
                }
                if not live_ok:
                    failures.append(
                        f"Live instance count {live_count} exceeds max {max_concurrent_instances}"
                    )
                if not cull_ok:
                    failures.append(f"Stuck CULLING instances detected: {stuck_culling}")
                if not lesson_ok:
                    failures.append(f"Lineage lesson_summary exceeded 5KB: {max_lesson_bytes} bytes")

            v5_1_obs: dict[str, Any] = {"ok": True, "skipped": True}
            if {"active_goals", "world_states", "memory_index", "decision_logs"}.issubset(tbls):
                td_report = await TopicDiversityAnalyzer(db).analyze()
                async with db.session() as s:
                    turn_cnt = int(
                        (
                            await s.execute(
                                select(func.count(WorldState.id)).where(WorldState.kind == "dialogue_turn")
                            )
                        ).scalar_one()
                    )
                    inbox_open_cnt = int(
                        (
                            await s.execute(
                                select(func.count(WorldState.id)).where(
                                    WorldState.kind == "human_inbox",
                                    WorldState.status == "open",
                                )
                            )
                        ).scalar_one()
                    )
                turns_bounded = turn_cnt <= 200
                v5_1_obs = {
                    "ok": turns_bounded,
                    "skipped": False,
                    "dialogue_turn_rows": turn_cnt,
                    "open_human_inbox_rows": inbox_open_cnt,
                    "topic_diversity_24h": td_report.get("summary") or {},
                }
                if not turns_bounded:
                    failures.append(f"V5.1 dialogue_turn window exceeded hygiene cap ({turn_cnt} > 200)")
            checks["v5_1_observability"] = v5_1_obs

            v5_2_obs: dict[str, Any] = {"ok": True, "skipped": True}
            if {"active_goals", "world_states", "skill_registry", "agent_executions"}.issubset(tbls):
                now_utc = datetime.now(timezone.utc)
                since_24h = now_utc - timedelta(hours=24)
                stale_draft_cutoff = now_utc - timedelta(hours=48)
                stale_goal_cutoff = now_utc - timedelta(hours=12)
                async with db.session() as s:
                    exec_rows = (
                        await s.execute(
                            select(AgentExecutionRow).order_by(AgentExecutionRow.started_at.desc()).limit(500)
                        )
                    ).scalars().all()
                    skill_rows = (await s.execute(select(SkillRegistryRow))).scalars().all()
                    active_goal_rows = (
                        await s.execute(select(ActiveGoal).where(ActiveGoal.status == "ACTIVE"))
                    ).scalars().all()
                    meta_ws_rows = (
                        await s.execute(
                            select(WorldState).where(
                                WorldState.kind.in_(["meta_reflection_state", "skill_refactor_queue"])
                            )
                        )
                    ).scalars().all()

                br_24h = [
                    e
                    for e in exec_rows
                    if e.agent_id == "brain_research" and (_norm_utc(e.started_at) or since_24h) >= since_24h
                ]
                br_failed = sum(1 for e in br_24h if e.status in {"FAILED", "TIMEOUT", "CANCELLED"})
                val_cnt = sum(1 for sk in skill_rows if sk.status == "VALIDATED")
                draft_rows = [sk for sk in skill_rows if sk.status == "DRAFT"]
                stale_draft_cnt = sum(
                    1 for sk in draft_rows if (_norm_utc(sk.updated_at or sk.created_at) or now_utc) < stale_draft_cutoff
                )
                stale_active_goals_cnt = sum(
                    1 for g in active_goal_rows if (_norm_utc(g.updated_at or g.created_at) or now_utc) < stale_goal_cutoff
                )
                meta_state_row = next(
                    (w for w in meta_ws_rows if w.kind == "meta_reflection_state" and w.key == "primary"), None
                )
                open_refactors_cnt = sum(
                    1 for w in meta_ws_rows if w.kind == "skill_refactor_queue" and w.status == "open"
                )
                v5_2_ok = stale_draft_cnt <= 5 and stale_active_goals_cnt <= 2
                v5_2_obs = {
                    "ok": v5_2_ok,
                    "skipped": False,
                    "brain_research_total_24h": len(br_24h),
                    "brain_research_failed_24h": br_failed,
                    "validated_skills": val_cnt,
                    "draft_skills": len(draft_rows),
                    "stale_draft_skills": stale_draft_cnt,
                    "stale_active_goals": stale_active_goals_cnt,
                    "open_skill_refactors": open_refactors_cnt,
                    "meta_reflection_recorded": meta_state_row is not None,
                    "last_reflection_at": (
                        (meta_state_row.value or {}).get("last_reflection_at")
                        if (meta_state_row and isinstance(meta_state_row.value, dict))
                        else None
                    ),
                }
                if stale_draft_cnt > 5:
                    failures.append(f"V5.2 stale DRAFT skill backlog exceeded hygiene cap ({stale_draft_cnt} > 5)")
                if stale_active_goals_cnt > 2:
                    failures.append(f"V5.2 stale ACTIVE goals exceeded hygiene cap ({stale_active_goals_cnt} > 2)")
            checks["v5_2_observability"] = v5_2_obs
        finally:
            await db.dispose()
    else:
        checks["database"] = {"ok": True, "skipped": True, "v5_tables_present": False}
        checks["v5_1_observability"] = {"ok": True, "skipped": True}
        checks["v5_2_observability"] = {"ok": True, "skipped": True}

    # 4. Orphan directory hygiene under instances_root (excluding `snapshots`)
    dirs_on_disk: list[str] = []
    if inst_path.exists():
        dirs_on_disk = sorted(
            p.name for p in inst_path.iterdir() if p.is_dir() and p.name != "snapshots"
        )
    if db_tables_present and active_candidate_keys:
        unexpected_dirs = [d for d in dirs_on_disk if d not in active_candidate_keys]
    else:
        unexpected_dirs = list(dirs_on_disk)
    orphan_ok = len(unexpected_dirs) == 0
    checks["orphan_directories"] = {
        "ok": orphan_ok,
        "instances_root": str(inst_path),
        "instances_root_exists": inst_path.exists(),
        "candidate_dirs_on_disk": dirs_on_disk,
        "unexpected_orphan_dirs": unexpected_dirs,
    }
    if not orphan_ok:
        failures.append(f"Orphan candidate directories on disk: {unexpected_dirs}")

    # 5. Optional HTTP /health check
    if (api_base or "").strip():
        url = f"{api_base.strip().rstrip('/')}/health"
        api_ok = False
        status_code: int | None = None
        err_msg: str | None = None
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url)
                status_code = resp.status_code
                api_ok = resp.status_code == 200
        except Exception as exc:
            err_msg = str(exc)
        checks["api_health"] = {
            "ok": api_ok,
            "url": url,
            "status_code": status_code,
            "error": err_msg,
        }
        if not api_ok:
            failures.append(f"API health check failed at {url}: status={status_code}, error={err_msg}")
        if (api_token or "").strip():
            base = api_base.strip().rstrip("/")
            headers = {"Authorization": f"Bearer {api_token.strip()}"}
            api_endpoints = [
                ("topic_diversity", f"{base}/v5/observability/topic-diversity"),
                ("control_center_social", f"{base}/v2/control-center/social?limit=5"),
            ]
            v5_1_api: dict[str, Any] = {"ok": True, "skipped": False, "endpoints": {}}
            v5_2_api: dict[str, Any] = {"ok": True, "skipped": False, "endpoints": {}}
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    for ep_name, ep_url in api_endpoints:
                        r = await client.get(ep_url, headers=headers)
                        ep_ok = r.status_code == 200
                        v5_1_api["endpoints"][ep_name] = {"ok": ep_ok, "status_code": r.status_code}
                        if not ep_ok:
                            v5_1_api["ok"] = False
                            failures.append(f"V5.1 API check failed at {ep_url}: status={r.status_code}")

                    se_url = f"{base}/v5/observability/self-evolution"
                    se_resp = await client.get(se_url, headers=headers)
                    se_ok = se_resp.status_code == 200
                    if se_ok:
                        try:
                            se_ok = bool(se_resp.json().get("read_only") is True)
                        except Exception:
                            se_ok = False
                    v5_2_api["endpoints"]["self_evolution"] = {"ok": se_ok, "status_code": se_resp.status_code}
                    if not se_ok:
                        v5_2_api["ok"] = False
                        failures.append(f"V5.2 API check failed at {se_url}: status={se_resp.status_code}")
            except Exception as exc:
                v5_1_api["ok"] = False
                v5_1_api["error"] = str(exc)
                v5_2_api["ok"] = False
                v5_2_api["error"] = str(exc)
                failures.append(f"V5.1/V5.2 API check error: {exc}")
            checks["v5_1_api"] = v5_1_api
            checks["v5_2_api"] = v5_2_api
        else:
            checks["v5_1_api"] = {"ok": True, "skipped": True}
            checks["v5_2_api"] = {"ok": True, "skipped": True}
    else:
        checks["api_health"] = {"ok": True, "skipped": True}
        checks["v5_1_api"] = {"ok": True, "skipped": True}
        checks["v5_2_api"] = {"ok": True, "skipped": True}

    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "steady_state_ok": len(failures) == 0,
        "checks": checks,
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run AOC V5.0 read-only steady-state soak check.")
    parser.add_argument("--repo-root", type=str, default=str(PROJECT_ROOT), help="Path to AOC repository.")
    parser.add_argument(
        "--instances-root", type=str, default="/home/ubuntu/aoc-instances", help="Candidate instances directory."
    )
    parser.add_argument("--min-free-disk-ratio", type=float, default=0.20, help="Minimum required free disk ratio.")
    parser.add_argument("--max-concurrent-instances", type=int, default=2, help="Maximum concurrent live instances.")
    parser.add_argument("--db-url", type=str, default="", help="Optional database URL for control-plane inspection.")
    parser.add_argument("--api-base", type=str, default="", help="Optional API base URL for /health check.")
    parser.add_argument("--api-token", type=str, default="", help="Optional Bearer token for V5.1 read-only API checks.")
    parser.add_argument("--output", type=str, default="", help="Optional path to write JSON report.")
    args = parser.parse_args()

    report = asyncio.run(
        run_v5_soak_check(
            repo_root=args.repo_root,
            instances_root=args.instances_root,
            min_free_disk_ratio=args.min_free_disk_ratio,
            max_concurrent_instances=args.max_concurrent_instances,
            db_url=args.db_url,
            api_base=args.api_base,
            api_token=args.api_token,
        )
    )
    text_out = json.dumps(report, indent=2, ensure_ascii=False)
    print(text_out)
    if args.output:
        Path(args.output).write_text(text_out + "\n", encoding="utf-8")
    return 0 if report["steady_state_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
