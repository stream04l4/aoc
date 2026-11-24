"""Shared `public` control-plane repository for V5.0 instance registry, proposals, and lineage."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select

from app.evolution.interfaces import (
    LIVE_INSTANCE_STATUSES,
    InstanceRole,
    InstanceStatus,
    ProposalStage,
    enforce_lesson_summary_budget,
)
from app.perception.interfaces import utcnow
from app.storage.database import Database, validate_db_schema_name
from app.storage.models import EvolutionProposalRow, InstanceRegistryRow, LineageRecordRow
from app.storage.repository import _jsonable


def _as_uuid(val: uuid.UUID | str | None) -> uuid.UUID | None:
    if val is None:
        return None
    return val if isinstance(val, uuid.UUID) else uuid.UUID(str(val))


def _instance_to_dict(row: InstanceRegistryRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "instance_key": row.instance_key,
        "generation": row.generation,
        "parent_instance_id": row.parent_instance_id,
        "proposal_id": row.proposal_id,
        "role": row.role,
        "status": row.status,
        "db_schema": row.db_schema,
        "letta_agent_name": row.letta_agent_name,
        "letta_agent_id": row.letta_agent_id,
        "git_branch": row.git_branch,
        "git_commit": row.git_commit,
        "repo_worktree_path": row.repo_worktree_path,
        "workspace_root": row.workspace_root,
        "container_name": row.container_name,
        "api_port": row.api_port,
        "blank_slate_verified": row.blank_slate_verified,
        "resource_snapshot": dict(row.resource_snapshot or {}),
        "created_at": row.created_at,
        "retired_at": row.retired_at,
        "cleaned_at": row.cleaned_at,
        "updated_at": row.updated_at,
    }


def _proposal_to_dict(row: EvolutionProposalRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "proposal_key": row.proposal_key,
        "parent_instance_id": row.parent_instance_id,
        "target_generation": row.target_generation,
        "trigger_type": row.trigger_type,
        "trigger_reason": row.trigger_reason,
        "entropy_snapshot": dict(row.entropy_snapshot or {}),
        "hypothesis": row.hypothesis,
        "genome_delta": dict(row.genome_delta or {}),
        "stage": row.stage,
        "stage1_report": dict(row.stage1_report or {}),
        "approval_id": row.approval_id,
        "approved_by": row.approved_by,
        "approved_at": row.approved_at,
        "arena_budget": dict(row.arena_budget or {}),
        "arena_result": dict(row.arena_result or {}),
        "candidate_instance_id": row.candidate_instance_id,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _lineage_to_dict(row: LineageRecordRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "generation": row.generation,
        "parent_instance_id": row.parent_instance_id,
        "offspring_instance_id": row.offspring_instance_id,
        "proposal_id": row.proposal_id,
        "outcome": row.outcome,
        "safety_passed": row.safety_passed,
        "parent_fitness": row.parent_fitness,
        "offspring_fitness": row.offspring_fitness,
        "fitness_delta": row.fitness_delta,
        "fitness_breakdown": dict(row.fitness_breakdown or {}),
        "drive_metric_changes": dict(row.drive_metric_changes or {}),
        "lesson_summary": dict(row.lesson_summary or {}),
        "created_at": row.created_at,
    }


class EvolutionRepository:
    """Async repository for V5.0 shared `public` control-plane tables."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def ensure_primary_instance(
        self,
        *,
        instance_key: str = "gen0-001",
        generation: int = 0,
        db_schema: str = "public",
        letta_agent_name: str = "aoc-organism-memory",
        letta_agent_id: str | None = None,
        workspace_root: str = "/home/ubuntu/aoc-workspaces",
        container_name: str | None = "aoc-core",
        api_port: int | None = 9200,
        git_branch: str | None = "main",
        git_commit: str | None = None,
    ) -> dict[str, Any]:
        """Idempotently register or fetch an ACTIVE primary instance row."""
        valid_schema = validate_db_schema_name(db_schema)
        async with self.db.session() as s:
            row = (
                await s.execute(select(InstanceRegistryRow).where(InstanceRegistryRow.instance_key == instance_key))
            ).scalar_one_or_none()
            if row is not None:
                if letta_agent_id and not row.letta_agent_id:
                    row.letta_agent_id = letta_agent_id
                if git_commit and not row.git_commit:
                    row.git_commit = git_commit
                await s.flush()
                return _instance_to_dict(row)
            row = InstanceRegistryRow(
                instance_key=instance_key,
                generation=generation,
                role=InstanceRole.PRIMARY.value,
                status=InstanceStatus.ACTIVE.value,
                db_schema=valid_schema,
                letta_agent_name=letta_agent_name,
                letta_agent_id=letta_agent_id,
                workspace_root=workspace_root,
                container_name=container_name,
                api_port=api_port,
                git_branch=git_branch,
                git_commit=git_commit,
                blank_slate_verified=True,
            )
            s.add(row)
            await s.flush()
            return _instance_to_dict(row)

    async def create_instance(
        self,
        *,
        instance_key: str,
        generation: int,
        db_schema: str,
        letta_agent_name: str,
        workspace_root: str,
        parent_instance_id: uuid.UUID | str | None = None,
        proposal_id: uuid.UUID | str | None = None,
        role: str = InstanceRole.CANDIDATE.value,
        status: str = InstanceStatus.INITIALIZING.value,
        letta_agent_id: str | None = None,
        git_branch: str | None = None,
        git_commit: str | None = None,
        repo_worktree_path: str | None = None,
        container_name: str | None = None,
        api_port: int | None = None,
        blank_slate_verified: bool = False,
        resource_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        valid_schema = validate_db_schema_name(db_schema)
        async with self.db.session() as s:
            row = InstanceRegistryRow(
                instance_key=instance_key,
                generation=generation,
                parent_instance_id=_as_uuid(parent_instance_id),
                proposal_id=_as_uuid(proposal_id),
                role=role,
                status=status,
                db_schema=valid_schema,
                letta_agent_name=letta_agent_name,
                letta_agent_id=letta_agent_id,
                git_branch=git_branch,
                git_commit=git_commit,
                repo_worktree_path=repo_worktree_path,
                workspace_root=workspace_root,
                container_name=container_name,
                api_port=api_port,
                blank_slate_verified=blank_slate_verified,
                resource_snapshot=_jsonable(resource_snapshot or {}),
            )
            s.add(row)
            await s.flush()
            return _instance_to_dict(row)

    async def get_instance(self, instance_id: uuid.UUID | str) -> dict[str, Any] | None:
        uid = _as_uuid(instance_id)
        async with self.db.session() as s:
            row = (
                await s.execute(select(InstanceRegistryRow).where(InstanceRegistryRow.id == uid))
            ).scalar_one_or_none()
            return _instance_to_dict(row) if row is not None else None

    async def get_instance_by_key(self, instance_key: str) -> dict[str, Any] | None:
        async with self.db.session() as s:
            row = (
                await s.execute(select(InstanceRegistryRow).where(InstanceRegistryRow.instance_key == instance_key))
            ).scalar_one_or_none()
            return _instance_to_dict(row) if row is not None else None

    async def update_instance(self, instance_id: uuid.UUID | str, **fields: Any) -> dict[str, Any] | None:
        uid = _as_uuid(instance_id)
        async with self.db.session() as s:
            row = (
                await s.execute(select(InstanceRegistryRow).where(InstanceRegistryRow.id == uid))
            ).scalar_one_or_none()
            if row is None:
                return None
            for k, v in fields.items():
                if hasattr(row, k):
                    if k == "resource_snapshot":
                        v = _jsonable(v or {})
                    setattr(row, k, v)
            row.updated_at = utcnow()
            await s.flush()
            return _instance_to_dict(row)

    async def list_instances(
        self,
        *,
        status: str | None = None,
        role: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        async with self.db.session() as s:
            stmt = select(InstanceRegistryRow).order_by(InstanceRegistryRow.created_at.desc()).limit(limit)
            if status:
                stmt = stmt.where(InstanceRegistryRow.status == status)
            if role:
                stmt = stmt.where(InstanceRegistryRow.role == role)
            rows = (await s.execute(stmt)).scalars().all()
            return [_instance_to_dict(r) for r in rows]

    async def count_live_instances(self) -> int:
        async with self.db.session() as s:
            count = (
                await s.execute(
                    select(func.count(InstanceRegistryRow.id)).where(
                        InstanceRegistryRow.status.in_(tuple(LIVE_INSTANCE_STATUSES))
                    )
                )
            ).scalar_one()
            return int(count or 0)

    async def create_proposal(
        self,
        *,
        proposal_key: str,
        parent_instance_id: uuid.UUID | str,
        target_generation: int,
        trigger_type: str,
        trigger_reason: str,
        hypothesis: str,
        entropy_snapshot: dict[str, Any] | None = None,
        genome_delta: dict[str, Any] | None = None,
        stage: str = ProposalStage.STAGE0_DRAFT.value,
        arena_budget: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self.db.session() as s:
            row = EvolutionProposalRow(
                proposal_key=proposal_key,
                parent_instance_id=_as_uuid(parent_instance_id),
                target_generation=target_generation,
                trigger_type=trigger_type,
                trigger_reason=trigger_reason,
                entropy_snapshot=_jsonable(entropy_snapshot or {}),
                hypothesis=hypothesis,
                genome_delta=_jsonable(genome_delta or {}),
                stage=stage,
                arena_budget=_jsonable(arena_budget or {"max_tokens": 500000, "max_seconds": 1200}),
            )
            s.add(row)
            await s.flush()
            return _proposal_to_dict(row)

    async def get_proposal(self, proposal_id: uuid.UUID | str) -> dict[str, Any] | None:
        uid = _as_uuid(proposal_id)
        async with self.db.session() as s:
            row = (
                await s.execute(select(EvolutionProposalRow).where(EvolutionProposalRow.id == uid))
            ).scalar_one_or_none()
            return _proposal_to_dict(row) if row is not None else None

    async def update_proposal(self, proposal_id: uuid.UUID | str, **fields: Any) -> dict[str, Any] | None:
        uid = _as_uuid(proposal_id)
        async with self.db.session() as s:
            row = (
                await s.execute(select(EvolutionProposalRow).where(EvolutionProposalRow.id == uid))
            ).scalar_one_or_none()
            if row is None:
                return None
            json_fields = {"entropy_snapshot", "genome_delta", "stage1_report", "arena_budget", "arena_result"}
            for k, v in fields.items():
                if hasattr(row, k):
                    if k in json_fields:
                        v = _jsonable(v or {})
                    setattr(row, k, v)
            row.updated_at = utcnow()
            await s.flush()
            return _proposal_to_dict(row)

    async def list_proposals(self, *, stage: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        async with self.db.session() as s:
            stmt = select(EvolutionProposalRow).order_by(EvolutionProposalRow.created_at.desc()).limit(limit)
            if stage:
                stmt = stmt.where(EvolutionProposalRow.stage == stage)
            rows = (await s.execute(stmt)).scalars().all()
            return [_proposal_to_dict(r) for r in rows]

    async def record_lineage(
        self,
        *,
        generation: int,
        offspring_instance_id: uuid.UUID | str,
        proposal_id: uuid.UUID | str,
        outcome: str,
        safety_passed: bool,
        parent_instance_id: uuid.UUID | str | None = None,
        parent_fitness: float | None = None,
        offspring_fitness: float | None = None,
        fitness_breakdown: dict[str, Any] | None = None,
        drive_metric_changes: dict[str, Any] | None = None,
        lesson_summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        delta = (
            round(float(offspring_fitness) - float(parent_fitness), 4)
            if (offspring_fitness is not None and parent_fitness is not None)
            else None
        )
        bounded_lesson = enforce_lesson_summary_budget(_jsonable(lesson_summary or {}))
        async with self.db.session() as s:
            row = LineageRecordRow(
                generation=generation,
                parent_instance_id=_as_uuid(parent_instance_id),
                offspring_instance_id=_as_uuid(offspring_instance_id),
                proposal_id=_as_uuid(proposal_id),
                outcome=outcome,
                safety_passed=safety_passed,
                parent_fitness=parent_fitness,
                offspring_fitness=offspring_fitness,
                fitness_delta=delta,
                fitness_breakdown=_jsonable(fitness_breakdown or {}),
                drive_metric_changes=_jsonable(drive_metric_changes or {}),
                lesson_summary=bounded_lesson,
            )
            s.add(row)
            await s.flush()
            return _lineage_to_dict(row)

    async def list_lineage(
        self,
        *,
        proposal_id: uuid.UUID | str | None = None,
        outcome: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        async with self.db.session() as s:
            stmt = select(LineageRecordRow).order_by(LineageRecordRow.created_at.desc()).limit(limit)
            if proposal_id is not None:
                stmt = stmt.where(LineageRecordRow.proposal_id == _as_uuid(proposal_id))
            if outcome:
                stmt = stmt.where(LineageRecordRow.outcome == outcome)
            rows = (await s.execute(stmt)).scalars().all()
            return [_lineage_to_dict(r) for r in rows]
