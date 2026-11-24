"""Idempotent, zero-residual Culling Garbage Collector for eliminated or failed V5.0 offspring."""

from __future__ import annotations

import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from sqlalchemy import text

from app.evolution.interfaces import (
    CullingReport,
    InstanceRole,
    InstanceStatus,
    LineageOutcome,
    ProposalStage,
    is_droppable_instance_schema,
)
from app.evolution.repository import EvolutionRepository
from app.memory.letta_adapter import LettaAdapter
from app.observability.logging import get_logger
from app.perception.interfaces import utcnow
from app.storage.database import Database

logger = get_logger("aoc.evolution.gc")


def _ensure_utc(dt: datetime) -> datetime:
    """Normalize naive or offset datetimes to UTC for safe age arithmetic."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


class CullingGC:
    """Destroys candidate containers, Letta agents, PG schemas, worktrees, and workspaces with zero residue."""

    def __init__(
        self,
        *,
        repo: EvolutionRepository,
        pg_admin_db: Database | None = None,
        letta_adapter: LettaAdapter | None = None,
        repo_root: str | Path = ".",
        instances_root: str | Path = "/home/ubuntu/aoc-instances",
        docker_remove_fn: Callable[[str], Awaitable[bool] | bool] | None = None,
        letta_delete_fn: Callable[[str | None, str], Awaitable[bool] | bool] | None = None,
        schema_drop_fn: Callable[[str], Awaitable[bool] | bool] | None = None,
        schema_exists_fn: Callable[[str], Awaitable[bool] | bool] | None = None,
        git_worktree_remove_fn: Callable[[str, str | None], Awaitable[bool] | bool] | None = None,
    ) -> None:
        self.repo = repo
        self.pg_admin_db = pg_admin_db or repo.db
        self.letta_adapter = letta_adapter
        self.repo_root = Path(repo_root).resolve()
        self.instances_root = Path(instances_root).resolve()
        self._docker_remove_fn = docker_remove_fn or self._default_docker_remove
        self._letta_delete_fn = letta_delete_fn or self._default_letta_delete
        self._schema_drop_fn = schema_drop_fn or self._default_schema_drop
        self._schema_exists_fn = schema_exists_fn or self._default_schema_exists
        self._git_worktree_remove_fn = git_worktree_remove_fn or self._default_git_worktree_remove

    async def _maybe_await(self, res: Any) -> Any:
        if hasattr(res, "__await__"):
            return await res
        return res

    def _is_under_instances_root(self, target: Path) -> bool:
        try:
            resolved = target.resolve()
        except OSError:
            return False
        return self.instances_root in resolved.parents

    async def _default_docker_remove(self, container_name: str) -> bool:
        if not container_name:
            return True
        res = subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0 or "No such container" in (res.stderr or ""):
            return True
        logger.warning("gc.docker_rm_failed", container=container_name, stderr=(res.stderr or "").strip())
        return False

    async def _default_letta_delete(self, agent_id: str | None, agent_name: str) -> bool:
        if self.letta_adapter is None:
            return True
        target_id = (agent_id or "").strip()
        if not target_id and agent_name:
            try:
                r = await self.letta_adapter._req("GET", "/v1/agents/", params={"name": agent_name})
                if r.status_code == 200 and isinstance(r.json(), list):
                    for a in r.json():
                        if isinstance(a, dict) and a.get("name") == agent_name:
                            target_id = str(a.get("id") or "")
                            break
            except Exception as exc:
                logger.warning("gc.letta_lookup_failed", agent_name=agent_name, error=str(exc))
                return False
        if not target_id:
            return True
        return await self.letta_adapter.delete_agent(target_id)

    async def _default_schema_drop(self, schema_name: str) -> bool:
        if not is_droppable_instance_schema(schema_name):
            raise PermissionError(f"Refusing to drop protected/invalid schema: {schema_name}")
        if self.pg_admin_db.engine.dialect.name != "postgresql":
            return True
        async with self.pg_admin_db.engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        return True

    async def _default_schema_exists(self, schema_name: str) -> bool:
        if self.pg_admin_db.engine.dialect.name != "postgresql":
            return False
        async with self.pg_admin_db.engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT 1 FROM information_schema.schemata WHERE schema_name = :schema"),
                    {"schema": schema_name},
                )
            ).scalar_one_or_none()
            return row is not None

    async def _default_git_worktree_remove(self, worktree_path: str, branch_name: str | None) -> bool:
        if worktree_path:
            wt = Path(worktree_path)
            if not self._is_under_instances_root(wt):
                raise PermissionError(f"Refusing to remove worktree outside instances_root: {worktree_path}")
            if wt.exists() and (self.repo_root / ".git").exists():
                subprocess.run(
                    ["git", "-C", str(self.repo_root), "worktree", "remove", "--force", str(wt)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                subprocess.run(
                    ["git", "-C", str(self.repo_root), "worktree", "prune"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            if wt.exists():
                shutil.rmtree(wt, ignore_errors=True)
        if branch_name and branch_name.startswith("evo-cand-") and (self.repo_root / ".git").exists():
            subprocess.run(
                ["git", "-C", str(self.repo_root), "branch", "-D", branch_name],
                capture_output=True,
                text=True,
                check=False,
            )
        return not worktree_path or not Path(worktree_path).exists()

    async def cull_instance(
        self,
        instance_id: uuid.UUID | str,
        *,
        reason: str = "culled by V5 evolution funnel",
        proposal_id: uuid.UUID | str | None = None,
        hypothesis: str = "",
        anti_pattern_tags: list[str] | None = None,
        parent_fitness: float | None = None,
        offspring_fitness: float | None = None,
        safety_passed: bool = False,
        fitness_breakdown: dict[str, Any] | None = None,
    ) -> CullingReport:
        inst = await self.repo.get_instance(instance_id)
        if inst is None:
            raise LookupError(f"Instance {instance_id} not found in public.instance_registry")

        inst_key = inst["instance_key"]
        schema_name = inst["db_schema"]
        if inst["role"] == InstanceRole.PRIMARY.value or schema_name == "public" or not is_droppable_instance_schema(schema_name):
            raise PermissionError(
                f"Refusing to cull protected instance '{inst_key}' (role={inst['role']}, schema={schema_name})"
            )

        await self.repo.update_instance(inst["id"], status=InstanceStatus.CULLING.value)
        errors: list[str] = []

        # 1. Remove candidate container
        container_removed = False
        try:
            container_removed = bool(await self._maybe_await(self._docker_remove_fn(inst.get("container_name") or "")))
            if not container_removed:
                errors.append(f"container removal failed: {inst.get('container_name')}")
        except Exception as exc:
            errors.append(f"container removal error: {exc}")

        # 2. Delete isolated Letta agent
        letta_deleted = False
        try:
            letta_deleted = bool(
                await self._maybe_await(
                    self._letta_delete_fn(inst.get("letta_agent_id"), inst.get("letta_agent_name") or "")
                )
            )
            if not letta_deleted:
                errors.append(f"letta agent deletion failed: {inst.get('letta_agent_name')}")
        except Exception as exc:
            errors.append(f"letta agent deletion error: {exc}")

        # 3. Drop isolated PostgreSQL schema (`DROP SCHEMA ... CASCADE`)
        schema_dropped = False
        try:
            schema_dropped = bool(await self._maybe_await(self._schema_drop_fn(schema_name)))
            if not schema_dropped:
                errors.append(f"schema drop returned False: {schema_name}")
        except Exception as exc:
            errors.append(f"schema drop error: {exc}")

        # 4. Remove Git worktree, temp branch, and isolated workspace directory
        worktree_removed = False
        wt_path = inst.get("repo_worktree_path") or ""
        try:
            worktree_removed = bool(
                await self._maybe_await(self._git_worktree_remove_fn(wt_path, inst.get("git_branch")))
            )
            if not worktree_removed:
                errors.append(f"worktree removal incomplete: {wt_path}")
        except Exception as exc:
            errors.append(f"worktree removal error: {exc}")

        workspace_removed = False
        ws_path_str = inst.get("workspace_root") or ""
        try:
            inst_base = self.instances_root / inst_key
            if ws_path_str:
                ws_path = Path(ws_path_str)
                if not self._is_under_instances_root(ws_path):
                    raise PermissionError(f"Refusing to delete workspace outside instances_root: {ws_path_str}")
                if ws_path.exists():
                    shutil.rmtree(ws_path)
            if inst_base.exists() and self._is_under_instances_root(inst_base):
                shutil.rmtree(inst_base)
            workspace_removed = (not ws_path_str or not Path(ws_path_str).exists()) and (not inst_base.exists())
            if not workspace_removed:
                errors.append(f"workspace directory still present: {ws_path_str}")
        except Exception as exc:
            errors.append(f"workspace removal error: {exc}")

        # 5. Zero-residual verification
        schema_still_exists = bool(await self._maybe_await(self._schema_exists_fn(schema_name)))
        if schema_still_exists:
            errors.append(f"residual PostgreSQL schema still exists: {schema_name}")
            schema_dropped = False

        zero_residual = (
            container_removed
            and letta_deleted
            and schema_dropped
            and worktree_removed
            and workspace_removed
            and not schema_still_exists
            and not errors
        )

        final_status = InstanceStatus.CULLED.value if zero_residual else InstanceStatus.CULLING.value
        snap = dict(inst.get("resource_snapshot") or {})
        snap["gc_errors"] = errors
        await self.repo.update_instance(
            inst["id"],
            status=final_status,
            cleaned_at=utcnow() if zero_residual else None,
            resource_snapshot=snap,
        )

        # 6. Record <5KB lineage tombstone on associated proposal (idempotent across retries)
        eff_proposal_id = proposal_id or inst.get("proposal_id")
        lineage_recorded = False
        if eff_proposal_id is not None:
            existing = await self.repo.list_lineage(proposal_id=eff_proposal_id, limit=5)
            already = any(str(row["offspring_instance_id"]) == str(inst["id"]) for row in existing)
            if not already:
                await self.repo.record_lineage(
                    generation=inst["generation"],
                    parent_instance_id=inst.get("parent_instance_id"),
                    offspring_instance_id=inst["id"],
                    proposal_id=eff_proposal_id,
                    outcome=LineageOutcome.CULLED.value,
                    safety_passed=safety_passed,
                    parent_fitness=parent_fitness,
                    offspring_fitness=offspring_fitness,
                    fitness_breakdown=fitness_breakdown or {},
                    lesson_summary={
                        "hypothesis": hypothesis or reason,
                        "outcome_reason": reason,
                        "anti_pattern_tags": list(anti_pattern_tags or ["culled_m1"]),
                        "key_metrics": {"zero_residual": zero_residual},
                    },
                )
            lineage_recorded = True

        logger.info(
            "gc.cull_completed",
            instance_key=inst_key,
            status=final_status,
            zero_residual=zero_residual,
            errors=errors,
        )
        return CullingReport(
            instance_id=inst["id"],
            instance_key=inst_key,
            db_schema=schema_name,
            status=final_status,
            container_removed=container_removed,
            letta_deleted=letta_deleted,
            schema_dropped=schema_dropped,
            worktree_removed=worktree_removed,
            workspace_removed=workspace_removed,
            zero_residual_verified=zero_residual,
            lineage_recorded=lineage_recorded,
            errors=errors,
        )

    async def recover_orphan_candidates(
        self,
        *,
        stale_grace_seconds: float = 300.0,
        default_max_seconds: float = 1200.0,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Resume interrupted CULLING runs and reclaim stale orphan candidate instances on startup."""
        ref_now = _ensure_utc(now) if now is not None else utcnow()
        instances = await self.repo.list_instances(limit=200)
        summary: dict[str, Any] = {
            "scanned": len(instances),
            "resumed_culling": [],
            "culled_stale": [],
            "skipped_pending_promotion": [],
            "failed": [],
        }

        for inst in instances:
            if (
                inst.get("role") != InstanceRole.CANDIDATE.value
                or inst.get("db_schema") == "public"
                or not is_droppable_instance_schema(inst.get("db_schema"))
            ):
                continue

            status = inst.get("status")
            inst_key = inst["instance_key"]

            if status == InstanceStatus.CULLING.value:
                rep = await self.cull_instance(
                    inst["id"],
                    reason="Resumed interrupted CullingGC on startup recovery",
                    proposal_id=inst.get("proposal_id"),
                    anti_pattern_tags=["orphan_recovery", "gc_resume"],
                    safety_passed=False,
                )
                if rep.zero_residual_verified:
                    summary["resumed_culling"].append(inst_key)
                else:
                    summary["failed"].append({"instance_key": inst_key, "errors": rep.errors})
                continue

            if status in {
                InstanceStatus.INITIALIZING.value,
                InstanceStatus.INCUBATING.value,
                InstanceStatus.TESTING.value,
            }:
                prop = (
                    await self.repo.get_proposal(inst["proposal_id"])
                    if inst.get("proposal_id") is not None
                    else None
                )
                if prop is not None and prop.get("stage") in {
                    ProposalStage.STAGE4_PENDING_PROMOTION.value,
                    ProposalStage.PROMOTED.value,
                }:
                    summary["skipped_pending_promotion"].append(inst_key)
                    continue

                budget_sec = (
                    float((prop.get("arena_budget") or {}).get("max_seconds", default_max_seconds))
                    if prop is not None
                    else float(default_max_seconds)
                )
                threshold = max(0.0, budget_sec) + max(0.0, float(stale_grace_seconds))
                ts = inst.get("updated_at") or inst.get("created_at")
                age = (ref_now - _ensure_utc(ts)).total_seconds() if isinstance(ts, datetime) else 0.0
                if age < threshold:
                    continue

                rep = await self.cull_instance(
                    inst["id"],
                    reason=f"Orphan candidate exceeded stale threshold ({age:.0f}s >= {threshold:.0f}s)",
                    proposal_id=inst.get("proposal_id"),
                    hypothesis=(prop or {}).get("hypothesis", ""),
                    anti_pattern_tags=["orphan_recovery", "stale_timeout"],
                    safety_passed=False,
                )
                if rep.zero_residual_verified:
                    summary["culled_stale"].append(inst_key)
                    if prop is not None and prop.get("stage") == ProposalStage.STAGE3_ARENA_RUNNING.value:
                        await self.repo.update_proposal(prop["id"], stage=ProposalStage.CULLED.value)
                else:
                    summary["failed"].append({"instance_key": inst_key, "errors": rep.errors})

        if (
            summary["resumed_culling"]
            or summary["culled_stale"]
            or summary["skipped_pending_promotion"]
            or summary["failed"]
        ):
            logger.info("gc.orphan_recovery_completed", **summary)
        return summary
