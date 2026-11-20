"""State Repository: the only module that touches ORM rows directly.

Other modules call these methods (or use Pydantic models returned by them);
they never import SQLAlchemy sessions themselves.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import func, select, update

from app.storage.database import Database
from app.storage.models import (
    ActionPlanRow,
    ActiveGoal,
    AgentExecutionRow,
    AgentRegistryRow,
    AgentTaskRow,
    ApiCredential,
    ApprovalRecord,
    CycleRecord,
    DecisionLog,
    DriveHistory,
    DriveStateRow,
    EventLog,
    GoalHistory,
    MemoryIndex,
    ObservationRecord,
    OrganismIdentity,
    OrganismState,
    PolicyEvaluation,
    ResourceUsage,
    SystemConfig,
    WorldState,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _jsonable(obj: Any) -> Any:
    """Make Pydantic/datetime/uuid values JSON-serialisable for JSON columns."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, uuid.UUID):
        return str(obj)
    return obj


class StateRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ── identity / organism state ───────────────────────────────────────────
    async def get_or_create_identity(self, name: str, version: str, traits: dict | None = None) -> dict:
        async with self.db.session() as s:
            row = (await s.execute(select(OrganismIdentity).where(OrganismIdentity.name == name))).scalar_one_or_none()
            created = False
            if row is None:
                row = OrganismIdentity(name=name, version=version, traits=traits or {})
                s.add(row)
                await s.flush()
                created = True
            st = (await s.execute(select(OrganismState).where(OrganismState.organism_id == row.id))).scalar_one_or_none()
            if st is None:
                st = OrganismState(organism_id=row.id, lifecycle_state="INITIALIZING", cycle_count=0)
                s.add(st)
            return {
                "id": row.id,
                "name": row.name,
                "version": row.version,
                "traits": row.traits,
                "memory_agent_id": row.memory_agent_id,
                "created_at": row.created_at,
                "created": created,
            }

    async def set_memory_agent_id(self, organism_id: uuid.UUID, agent_id: str) -> None:
        async with self.db.session() as s:
            await s.execute(
                update(OrganismIdentity).where(OrganismIdentity.id == organism_id).values(memory_agent_id=agent_id)
            )

    async def get_organism_state(self, organism_id: uuid.UUID) -> dict | None:
        async with self.db.session() as s:
            st = (await s.execute(select(OrganismState).where(OrganismState.organism_id == organism_id))).scalar_one_or_none()
            if st is None:
                return None
            return {
                "lifecycle_state": st.lifecycle_state,
                "previous_state": st.previous_state,
                "state_reason": st.state_reason,
                "current_cycle_id": st.current_cycle_id,
                "cycle_count": st.cycle_count,
                "pause_requested": st.pause_requested,
                "quota_reset_at": st.quota_reset_at,
                "throttled_reason": st.throttled_reason,
                "last_wake_at": st.last_wake_at,
                "next_wake_at": st.next_wake_at,
                "heartbeat_at": st.heartbeat_at,
                "updated_at": st.updated_at,
            }

    async def update_organism_state(self, organism_id: uuid.UUID, **values: Any) -> None:
        values["updated_at"] = utcnow()
        async with self.db.session() as s:
            await s.execute(update(OrganismState).where(OrganismState.organism_id == organism_id).values(**values))

    # ── drives ──────────────────────────────────────────────────────────────
    async def load_drive_states(self, organism_id: uuid.UUID) -> list[dict]:
        async with self.db.session() as s:
            rows = (await s.execute(select(DriveStateRow).where(DriveStateRow.organism_id == organism_id))).scalars().all()
            return [
                {
                    "drive_type": r.drive_type,
                    "satisfaction": r.satisfaction,
                    "deficit": r.deficit,
                    "urgency": r.urgency,
                    "confidence": r.confidence,
                    "persistence": r.persistence,
                    "trend": r.trend,
                    "estimated_cost": r.estimated_cost,
                    "estimated_risk": r.estimated_risk,
                    "last_satisfied_at": r.last_satisfied_at,
                    "dimensions": r.dimensions,
                    "notes": r.notes,
                    "updated_at": r.updated_at,
                }
                for r in rows
            ]

    async def save_drive_states(
        self, organism_id: uuid.UUID, states: Iterable[dict], cycle_id: uuid.UUID | None, phase: str
    ) -> None:
        """Upsert current drive states and append history rows in ONE transaction."""
        async with self.db.session() as s:
            for st in states:
                st = _jsonable(st)
                row = (
                    await s.execute(
                        select(DriveStateRow).where(
                            DriveStateRow.organism_id == organism_id, DriveStateRow.drive_type == st["drive_type"]
                        )
                    )
                ).scalar_one_or_none()
                fields = dict(
                    satisfaction=st["satisfaction"],
                    deficit=st["deficit"],
                    urgency=st["urgency"],
                    confidence=st["confidence"],
                    persistence=st["persistence"],
                    trend=st["trend"],
                    estimated_cost=st["estimated_cost"],
                    estimated_risk=st["estimated_risk"],
                    last_satisfied_at=datetime.fromisoformat(st["last_satisfied_at"]) if st.get("last_satisfied_at") else None,
                    dimensions=st.get("dimensions", {}),
                    notes=st.get("notes", []),
                    updated_at=utcnow(),
                )
                if row is None:
                    s.add(DriveStateRow(organism_id=organism_id, drive_type=st["drive_type"], **fields))
                else:
                    for k, v in fields.items():
                        setattr(row, k, v)
                s.add(
                    DriveHistory(
                        organism_id=organism_id,
                        cycle_id=cycle_id,
                        phase=phase,
                        drive_type=st["drive_type"],
                        satisfaction=st["satisfaction"],
                        deficit=st["deficit"],
                        urgency=st["urgency"],
                        confidence=st["confidence"],
                        persistence=st["persistence"],
                        trend=st["trend"],
                        dimensions=st.get("dimensions", {}),
                    )
                )

    # ── observations / world ────────────────────────────────────────────────
    async def save_observation(self, obs: Any, cycle_id: uuid.UUID | None, phase: str = "pre") -> uuid.UUID:
        data = _jsonable(obs)
        async with self.db.session() as s:
            row = ObservationRecord(
                cycle_id=cycle_id,
                phase=phase,
                observed_at=datetime.fromisoformat(data["timestamp"]),
                system_state=data.get("system_state", {}),
                environment_state=data.get("environment_state", {}),
                available_resources=data.get("available_resources", {}),
                active_tasks=data.get("active_tasks", []),
                external_events=data.get("external_events", []),
                sensor_status=data.get("sensor_status", {}),
                confidence=data.get("confidence", 0.0),
            )
            s.add(row)
            await s.flush()
            return row.id

    async def upsert_world_item(
        self,
        kind: str,
        key: str,
        value: dict | None = None,
        status: str | None = None,
        source: str | None = None,
        is_simulated: bool | None = None,
    ) -> dict:
        async with self.db.session() as s:
            row = (
                await s.execute(select(WorldState).where(WorldState.kind == kind, WorldState.key == key))
            ).scalar_one_or_none()
            if row is None:
                row = WorldState(
                    kind=kind,
                    key=key,
                    value=_jsonable(value or {}),
                    status=status or "open",
                    source=source or "environment",
                    is_simulated=bool(is_simulated),
                )
                s.add(row)
            else:
                if value is not None:
                    merged = dict(row.value or {})
                    merged.update(_jsonable(value))
                    row.value = merged
                if status is not None:
                    row.status = status
                if source is not None:
                    row.source = source
                if is_simulated is not None:
                    row.is_simulated = is_simulated
                row.updated_at = utcnow()
            await s.flush()
            return self._world_dict(row)

    async def list_world_items(self, kind: str | None = None, status: str | None = None) -> list[dict]:
        async with self.db.session() as s:
            q = select(WorldState)
            if kind:
                q = q.where(WorldState.kind == kind)
            if status:
                q = q.where(WorldState.status == status)
            rows = (await s.execute(q.order_by(WorldState.created_at))).scalars().all()
            return [self._world_dict(r) for r in rows]

    async def delete_world_item(self, kind: str, key: str) -> bool:
        async with self.db.session() as s:
            row = (
                await s.execute(select(WorldState).where(WorldState.kind == kind, WorldState.key == key))
            ).scalar_one_or_none()
            if row is None:
                return False
            await s.delete(row)
            return True

    @staticmethod
    def _world_dict(r: WorldState) -> dict:
        return {
            "kind": r.kind,
            "key": r.key,
            "value": r.value,
            "status": r.status,
            "source": r.source,
            "is_simulated": r.is_simulated,
            "created_at": r.created_at,
            "updated_at": r.updated_at,
        }

    # ── cycles ──────────────────────────────────────────────────────────────
    async def start_cycle(self, organism_id: uuid.UUID, lifecycle_state: str) -> tuple[uuid.UUID, int]:
        async with self.db.session() as s:
            st = (
                await s.execute(select(OrganismState).where(OrganismState.organism_id == organism_id).with_for_update())
            ).scalar_one()
            st.cycle_count += 1
            number = st.cycle_count
            rec = CycleRecord(organism_id=organism_id, cycle_number=number, phase="WAKE_UP", lifecycle_state=lifecycle_state)
            s.add(rec)
            await s.flush()
            st.current_cycle_id = rec.id
            st.last_wake_at = utcnow()
            return rec.id, number

    async def update_cycle(self, cycle_id: uuid.UUID, **values: Any) -> None:
        values = {k: (_jsonable(v) if k not in {"ended_at", "observation_id"} else v) for k, v in values.items()}
        async with self.db.session() as s:
            await s.execute(update(CycleRecord).where(CycleRecord.id == cycle_id).values(**values))

    async def get_cycle(self, cycle_id: uuid.UUID) -> dict | None:
        async with self.db.session() as s:
            r = await s.get(CycleRecord, cycle_id)
            return self._cycle_dict(r) if r else None

    async def list_cycles(self, limit: int = 20) -> list[dict]:
        async with self.db.session() as s:
            rows = (await s.execute(select(CycleRecord).order_by(CycleRecord.cycle_number.desc()).limit(limit))).scalars().all()
            return [self._cycle_dict(r) for r in rows]

    async def close_unfinished_cycles(self, organism_id: uuid.UUID, note: str) -> int:
        async with self.db.session() as s:
            res = await s.execute(
                update(CycleRecord)
                .where(CycleRecord.organism_id == organism_id, CycleRecord.ended_at.is_(None))
                .values(ended_at=utcnow(), outcome="INTERRUPTED", error=note)
            )
            return res.rowcount or 0

    @staticmethod
    def _cycle_dict(r: CycleRecord) -> dict:
        return {
            "cycle_id": str(r.id),
            "cycle_number": r.cycle_number,
            "started_at": r.started_at,
            "ended_at": r.ended_at,
            "phase": r.phase,
            "outcome": r.outcome,
            "lifecycle_state": r.lifecycle_state,
            "drives_pre": r.drives_pre,
            "drives_post": r.drives_post,
            "decision": r.decision,
            "brain_output": r.brain_output,
            "plan": r.plan,
            "execution_summary": r.execution_summary,
            "evaluation": r.evaluation,
            "state_changes": r.state_changes,
            "error": r.error,
        }

    # ── goals & plans ───────────────────────────────────────────────────────
    async def create_goal(self, organism_id: uuid.UUID, cycle_id: uuid.UUID, **fields: Any) -> uuid.UUID:
        fields = {k: _jsonable(v) for k, v in fields.items()}
        async with self.db.session() as s:
            g = ActiveGoal(organism_id=organism_id, cycle_id=cycle_id, **fields)
            s.add(g)
            await s.flush()
            s.add(GoalHistory(goal_id=g.id, from_status=None, to_status=g.status, note="created",
                              expected_outcome=g.expected_outcome or {}))
            return g.id

    async def transition_goal(
        self,
        goal_id: uuid.UUID,
        to_status: str,
        note: str = "",
        actual_outcome: dict | None = None,
        measured_change: dict | None = None,
    ) -> None:
        async with self.db.session() as s:
            g = await s.get(ActiveGoal, goal_id)
            if g is None:
                return
            s.add(
                GoalHistory(
                    goal_id=goal_id,
                    from_status=g.status,
                    to_status=to_status,
                    note=note,
                    expected_outcome=g.expected_outcome or {},
                    actual_outcome=_jsonable(actual_outcome or {}),
                    measured_change=_jsonable(measured_change or {}),
                )
            )
            g.status = to_status
            g.updated_at = utcnow()

    async def get_goal(self, goal_id: uuid.UUID) -> dict | None:
        async with self.db.session() as s:
            g = await s.get(ActiveGoal, goal_id)
            return self._goal_dict(g) if g else None

    async def list_goals(self, statuses: list[str] | None = None, limit: int = 50) -> list[dict]:
        async with self.db.session() as s:
            q = select(ActiveGoal)
            if statuses:
                q = q.where(ActiveGoal.status.in_(statuses))
            rows = (await s.execute(q.order_by(ActiveGoal.created_at.desc()).limit(limit))).scalars().all()
            return [self._goal_dict(g) for g in rows]

    async def count_goals_since(self, drive_type: str, status: str, since: datetime) -> int:
        async with self.db.session() as s:
            return (
                await s.execute(
                    select(func.count(ActiveGoal.id)).where(
                        ActiveGoal.drive_type == drive_type, ActiveGoal.status == status, ActiveGoal.updated_at >= since
                    )
                )
            ).scalar_one()

    async def goal_history(self, goal_id: uuid.UUID) -> list[dict]:
        async with self.db.session() as s:
            rows = (
                await s.execute(select(GoalHistory).where(GoalHistory.goal_id == goal_id).order_by(GoalHistory.created_at))
            ).scalars().all()
            return [
                {
                    "from": r.from_status,
                    "to": r.to_status,
                    "note": r.note,
                    "expected_outcome": r.expected_outcome,
                    "actual_outcome": r.actual_outcome,
                    "measured_change": r.measured_change,
                    "at": r.created_at,
                }
                for r in rows
            ]

    @staticmethod
    def _goal_dict(g: ActiveGoal) -> dict:
        return {
            "goal_id": str(g.id),
            "cycle_id": str(g.cycle_id),
            "drive_type": g.drive_type,
            "title": g.title,
            "description": g.description,
            "success_criteria": g.success_criteria,
            "expected_outcome": g.expected_outcome,
            "related_key": g.related_key,
            "status": g.status,
            "created_at": g.created_at,
            "updated_at": g.updated_at,
        }

    async def create_plan(
        self,
        goal_id: uuid.UUID,
        cycle_id: uuid.UUID,
        plan: Any,
        permission_level: str,
        verdict: Any,
        status: str,
        plan_hash: str | None = None,
    ) -> uuid.UUID:
        async with self.db.session() as s:
            p = ActionPlanRow(
                goal_id=goal_id,
                cycle_id=cycle_id,
                plan=_jsonable(plan),
                plan_hash=plan_hash,
                permission_level=permission_level,
                policy_verdict=_jsonable(verdict),
                status=status,
            )
            s.add(p)
            await s.flush()
            return p.id

    async def get_plan(self, plan_id: uuid.UUID) -> dict | None:
        async with self.db.session() as s:
            p = await s.get(ActionPlanRow, plan_id)
            if p is None:
                return None
            return {
                "plan_id": str(p.id),
                "goal_id": str(p.goal_id),
                "cycle_id": str(p.cycle_id),
                "plan": p.plan,
                "plan_hash": p.plan_hash,
                "permission_level": p.permission_level,
                "policy_verdict": p.policy_verdict,
                "status": p.status,
                "created_at": p.created_at,
                "updated_at": p.updated_at,
            }

    async def update_plan_status(self, plan_id: uuid.UUID, status: str) -> None:
        async with self.db.session() as s:
            await s.execute(update(ActionPlanRow).where(ActionPlanRow.id == plan_id).values(status=status, updated_at=utcnow()))

    # ── agents ──────────────────────────────────────────────────────────────
    async def upsert_agent(self, cap: dict, experimental: bool, probe: dict) -> None:
        async with self.db.session() as s:
            row = (await s.execute(select(AgentRegistryRow).where(AgentRegistryRow.agent_id == cap["agent_id"]))).scalar_one_or_none()
            fields = dict(
                name=cap["name"],
                agent_type=cap["agent_type"],
                capabilities=cap["capabilities"],
                supported_modes=cap["supported_modes"],
                max_concurrency=cap["max_concurrency"],
                enabled=cap["enabled"],
                experimental=experimental,
                probe=_jsonable(probe),
                updated_at=utcnow(),
            )
            if row is None:
                s.add(AgentRegistryRow(agent_id=cap["agent_id"], **fields))
            else:
                for k, v in fields.items():
                    setattr(row, k, v)

    async def list_agents(self) -> list[dict]:
        async with self.db.session() as s:
            rows = (await s.execute(select(AgentRegistryRow))).scalars().all()
            return [
                {
                    "agent_id": r.agent_id,
                    "name": r.name,
                    "agent_type": r.agent_type,
                    "capabilities": r.capabilities,
                    "supported_modes": r.supported_modes,
                    "max_concurrency": r.max_concurrency,
                    "enabled": r.enabled,
                    "experimental": r.experimental,
                    "probe": r.probe,
                }
                for r in rows
            ]

    async def create_task(self, **fields: Any) -> uuid.UUID:
        fields = {k: (_jsonable(v) if k not in {"id", "plan_id", "goal_id", "cycle_id"} else v) for k, v in fields.items()}
        async with self.db.session() as s:
            t = AgentTaskRow(**fields)
            s.add(t)
            await s.flush()
            return t.id

    async def update_task(self, task_id: uuid.UUID, **values: Any) -> None:
        values = {k: _jsonable(v) for k, v in values.items()}
        values["updated_at"] = utcnow()
        async with self.db.session() as s:
            await s.execute(update(AgentTaskRow).where(AgentTaskRow.id == task_id).values(**values))

    async def claim_task_for_approval(
        self,
        task_id: uuid.UUID,
        allowed_statuses: list[str],
        new_status: str,
        outcome: dict | None = None,
    ) -> bool:
        """Atomically transition a task awaiting approval to prevent concurrent double-dispatch."""
        values: dict[str, Any] = {"status": new_status, "updated_at": utcnow()}
        if outcome is not None:
            values["outcome"] = _jsonable(outcome)
        async with self.db.session() as s:
            res = await s.execute(
                update(AgentTaskRow)
                .where(AgentTaskRow.id == task_id, AgentTaskRow.status.in_(allowed_statuses))
                .values(**values)
            )
            return (res.rowcount or 0) == 1

    async def get_task(self, task_id: uuid.UUID) -> dict | None:
        async with self.db.session() as s:
            t = await s.get(AgentTaskRow, task_id)
            return self._task_dict(t) if t else None

    async def list_tasks(self, statuses: list[str] | None = None, limit: int = 100) -> list[dict]:
        async with self.db.session() as s:
            q = select(AgentTaskRow)
            if statuses:
                q = q.where(AgentTaskRow.status.in_(statuses))
            rows = (await s.execute(q.order_by(AgentTaskRow.created_at.desc()).limit(limit))).scalars().all()
            return [self._task_dict(t) for t in rows]

    @staticmethod
    def _task_dict(t: AgentTaskRow) -> dict:
        return {
            "task_id": str(t.id),
            "plan_id": str(t.plan_id) if t.plan_id else None,
            "goal_id": str(t.goal_id) if t.goal_id else None,
            "cycle_id": str(t.cycle_id) if t.cycle_id else None,
            "objective": t.objective,
            "description": t.description,
            "assigned_agent": t.assigned_agent,
            "expected_output": t.expected_output,
            "workspace": t.workspace,
            "timeout_seconds": t.timeout_seconds,
            "resource_budget": t.resource_budget,
            "permission_level": t.permission_level,
            "success_criteria": t.success_criteria,
            "verification": t.verification,
            "required_capability": t.required_capability,
            "status": t.status,
            "attempt": t.attempt,
            "max_attempts": t.max_attempts,
            "tried_agents": t.tried_agents,
            "outcome": t.outcome,
            "created_at": t.created_at,
            "updated_at": t.updated_at,
        }

    async def create_execution(self, task_id: uuid.UUID, agent_id: str, status: str, execution_id: uuid.UUID | None = None) -> uuid.UUID:
        async with self.db.session() as s:
            e = AgentExecutionRow(id=execution_id or uuid.uuid4(), task_id=task_id, agent_id=agent_id, status=status)
            s.add(e)
            await s.flush()
            return e.id

    async def update_execution(self, execution_id: uuid.UUID, **values: Any) -> None:
        clean = {}
        for k, v in values.items():
            clean[k] = v if k in {"started_at", "completed_at"} else _jsonable(v)
        clean["updated_at"] = utcnow()
        async with self.db.session() as s:
            await s.execute(update(AgentExecutionRow).where(AgentExecutionRow.id == execution_id).values(**clean))

    async def get_execution(self, execution_id: uuid.UUID) -> dict | None:
        async with self.db.session() as s:
            e = await s.get(AgentExecutionRow, execution_id)
            return self._exec_dict(e) if e else None

    async def list_executions(
        self, task_id: uuid.UUID | None = None, statuses: list[str] | None = None, limit: int = 50
    ) -> list[dict]:
        async with self.db.session() as s:
            q = select(AgentExecutionRow)
            if task_id:
                q = q.where(AgentExecutionRow.task_id == task_id)
            if statuses:
                q = q.where(AgentExecutionRow.status.in_(statuses))
            rows = (await s.execute(q.order_by(AgentExecutionRow.started_at.desc()).limit(limit))).scalars().all()
            return [self._exec_dict(e) for e in rows]

    @staticmethod
    def _exec_dict(e: AgentExecutionRow) -> dict:
        return {
            "execution_id": str(e.id),
            "task_id": str(e.task_id),
            "agent_id": e.agent_id,
            "status": e.status,
            "pid": e.pid,
            "started_at": e.started_at,
            "completed_at": e.completed_at,
            "result": e.result,
            "stdout": e.stdout,
            "stderr": e.stderr,
            "error": e.error,
            "resource_usage": e.resource_usage,
            "verification": e.verification,
        }

    # ── memory index ────────────────────────────────────────────────────────
    async def add_memory_index(self, **fields: Any) -> uuid.UUID:
        fields = {k: (_jsonable(v) if k != "cycle_id" else v) for k, v in fields.items()}
        async with self.db.session() as s:
            m = MemoryIndex(**fields)
            s.add(m)
            await s.flush()
            return m.id

    async def update_memory_index(self, memory_id: uuid.UUID, **values: Any) -> None:
        values["updated_at"] = utcnow()
        async with self.db.session() as s:
            await s.execute(update(MemoryIndex).where(MemoryIndex.id == memory_id).values(**values))

    async def find_memory_by_hash(self, content_hash: str) -> dict | None:
        async with self.db.session() as s:
            m = (await s.execute(select(MemoryIndex).where(MemoryIndex.content_hash == content_hash))).scalars().first()
            return self._mem_dict(m) if m else None

    async def list_memory_index(
        self, memory_type: str | None = None, sync_status: str | None = None, limit: int = 100
    ) -> list[dict]:
        async with self.db.session() as s:
            q = select(MemoryIndex)
            if memory_type:
                q = q.where(MemoryIndex.memory_type == memory_type)
            if sync_status:
                q = q.where(MemoryIndex.sync_status == sync_status)
            rows = (await s.execute(q.order_by(MemoryIndex.importance.desc(), MemoryIndex.created_at.desc()).limit(limit))).scalars().all()
            return [self._mem_dict(m) for m in rows]

    async def count_memories(self, memory_type: str, since: datetime | None = None) -> int:
        async with self.db.session() as s:
            q = select(func.count(MemoryIndex.id)).where(MemoryIndex.memory_type == memory_type)
            if since:
                q = q.where(MemoryIndex.created_at >= since)
            return (await s.execute(q)).scalar_one()

    @staticmethod
    def _mem_dict(m: MemoryIndex) -> dict:
        return {
            "memory_id": str(m.id),
            "memory_type": m.memory_type,
            "external_id": m.external_id,
            "title": m.title,
            "summary": m.summary,
            "content_hash": m.content_hash,
            "source": m.source,
            "importance": m.importance,
            "tags": m.tags,
            "cycle_id": str(m.cycle_id) if m.cycle_id else None,
            "sync_status": m.sync_status,
            "created_at": m.created_at,
        }

    # ── audit / logs ────────────────────────────────────────────────────────
    async def log_event(
        self, event_type: str, message: str = "", payload: dict | None = None, level: str = "info",
        cycle_id: uuid.UUID | None = None,
    ) -> None:
        async with self.db.session() as s:
            s.add(EventLog(event_type=event_type, message=message, payload=_jsonable(payload or {}), level=level, cycle_id=cycle_id))

    async def log_decision(
        self, decision_type: str, selected: str | None, alternatives: list, reason: str, payload: Any = None,
        cycle_id: uuid.UUID | None = None,
    ) -> None:
        async with self.db.session() as s:
            s.add(
                DecisionLog(
                    cycle_id=cycle_id,
                    decision_type=decision_type,
                    selected=selected,
                    alternatives=_jsonable(alternatives),
                    reason=reason,
                    payload=_jsonable(payload or {}),
                )
            )

    async def list_events(self, limit: int = 100, event_type: str | None = None) -> list[dict]:
        async with self.db.session() as s:
            q = select(EventLog)
            if event_type:
                q = q.where(EventLog.event_type == event_type)
            rows = (await s.execute(q.order_by(EventLog.created_at.desc()).limit(limit))).scalars().all()
            return [
                {"event_type": r.event_type, "level": r.level, "message": r.message, "payload": r.payload,
                 "cycle_id": str(r.cycle_id) if r.cycle_id else None, "at": r.created_at}
                for r in rows
            ]

    async def list_decisions(self, limit: int = 50) -> list[dict]:
        async with self.db.session() as s:
            rows = (await s.execute(select(DecisionLog).order_by(DecisionLog.created_at.desc()).limit(limit))).scalars().all()
            return [
                {"decision_type": r.decision_type, "selected": r.selected, "alternatives": r.alternatives,
                 "reason": r.reason, "payload": r.payload, "cycle_id": str(r.cycle_id) if r.cycle_id else None,
                 "at": r.created_at}
                for r in rows
            ]

    # ── resources / budget ──────────────────────────────────────────────────
    async def record_usage(
        self, category: str, amount: float = 0.0, seconds: float = 0.0, details: dict | None = None,
        cycle_id: uuid.UUID | None = None, task_id: uuid.UUID | None = None,
    ) -> None:
        async with self.db.session() as s:
            s.add(ResourceUsage(category=category, amount=amount, seconds=seconds, details=_jsonable(details or {}),
                                cycle_id=cycle_id, task_id=task_id))

    async def usage_since(self, category: str, since: datetime) -> dict:
        async with self.db.session() as s:
            row = (
                await s.execute(
                    select(func.count(ResourceUsage.id), func.coalesce(func.sum(ResourceUsage.amount), 0.0),
                           func.coalesce(func.sum(ResourceUsage.seconds), 0.0)).where(
                        ResourceUsage.category == category, ResourceUsage.created_at >= since
                    )
                )
            ).one()
            return {"count": int(row[0]), "amount": float(row[1]), "seconds": float(row[2])}

    # ── system config (operator-owned) ──────────────────────────────────────
    async def get_config(self, key: str) -> dict | None:
        async with self.db.session() as s:
            r = await s.get(SystemConfig, key)
            return r.value if r else None

    async def set_config(self, key: str, value: dict, updated_by: str = "operator") -> None:
        async with self.db.session() as s:
            r = await s.get(SystemConfig, key)
            if r is None:
                s.add(SystemConfig(key=key, value=_jsonable(value), updated_by=updated_by))
            else:
                r.value = _jsonable(value)
                r.updated_by = updated_by
                r.updated_at = utcnow()

    # ── api credentials (P1-1a) ─────────────────────────────────────────────
    async def create_api_credential(
        self,
        name: str,
        key_prefix: str,
        key_hash: str,
        scopes: list[str],
        expires_at: datetime | None = None,
    ) -> dict:
        async with self.db.session() as s:
            row = ApiCredential(
                name=name,
                key_prefix=key_prefix,
                key_hash=key_hash,
                scopes=_jsonable(scopes),
                expires_at=expires_at,
            )
            s.add(row)
            await s.flush()
            return self._cred_dict(row)

    async def get_api_credential_by_hash(self, key_hash: str) -> dict | None:
        async with self.db.session() as s:
            row = (
                await s.execute(select(ApiCredential).where(ApiCredential.key_hash == key_hash))
            ).scalar_one_or_none()
            return self._cred_dict(row) if row else None

    async def touch_api_credential(self, cred_id: uuid.UUID) -> None:
        async with self.db.session() as s:
            await s.execute(
                update(ApiCredential).where(ApiCredential.id == cred_id).values(last_used_at=utcnow())
            )

    async def revoke_api_credential(self, cred_id: uuid.UUID) -> bool:
        async with self.db.session() as s:
            res = await s.execute(
                update(ApiCredential).where(ApiCredential.id == cred_id).values(revoked=True)
            )
            return (res.rowcount or 0) > 0

    async def has_active_api_credentials(self) -> bool:
        now = utcnow()
        async with self.db.session() as s:
            rows = (
                await s.execute(select(ApiCredential).where(ApiCredential.revoked.is_(False)))
            ).scalars().all()
            for r in rows:
                if r.expires_at is None:
                    return True
                exp = r.expires_at.replace(tzinfo=timezone.utc) if r.expires_at.tzinfo is None else r.expires_at
                if exp > now:
                    return True
            return False

    @staticmethod
    def _cred_dict(r: ApiCredential) -> dict:
        return {
            "id": r.id,
            "name": r.name,
            "key_prefix": r.key_prefix,
            "scopes": r.scopes,
            "revoked": r.revoked,
            "expires_at": r.expires_at,
            "last_used_at": r.last_used_at,
            "created_at": r.created_at,
        }

    # ── approval records (P1-1a / P1-8) ─────────────────────────────────────
    async def create_approval_record(
        self,
        task_id: uuid.UUID,
        plan_id: uuid.UUID | None,
        plan_hash: str,
        params_digest: str,
        permission_level: str,
        nonce: str,
        expires_at: datetime,
        status: str = "PENDING",
    ) -> dict:
        async with self.db.session() as s:
            row = ApprovalRecord(
                task_id=task_id,
                plan_id=plan_id,
                plan_hash=plan_hash,
                params_digest=params_digest,
                permission_level=permission_level,
                nonce=nonce,
                status=status,
                expires_at=expires_at,
            )
            s.add(row)
            await s.flush()
            return self._approval_dict(row)

    async def get_pending_approval_for_task(self, task_id: uuid.UUID) -> dict | None:
        async with self.db.session() as s:
            row = (
                await s.execute(
                    select(ApprovalRecord)
                    .where(ApprovalRecord.task_id == task_id, ApprovalRecord.status == "PENDING")
                    .order_by(ApprovalRecord.created_at.desc())
                )
            ).scalars().first()
            return self._approval_dict(row) if row else None

    async def get_approval_record(self, approval_id: uuid.UUID) -> dict | None:
        async with self.db.session() as s:
            row = await s.get(ApprovalRecord, approval_id)
            return self._approval_dict(row) if row else None

    async def update_approval_record(self, approval_id: uuid.UUID, **values: Any) -> None:
        clean = {}
        for k, v in values.items():
            clean[k] = v if k in {"decided_at", "consumed_at", "expires_at"} else _jsonable(v)
        async with self.db.session() as s:
            await s.execute(update(ApprovalRecord).where(ApprovalRecord.id == approval_id).values(**clean))

    async def list_approval_records(self, status: str | None = None, limit: int = 50) -> list[dict]:
        async with self.db.session() as s:
            q = select(ApprovalRecord)
            if status:
                q = q.where(ApprovalRecord.status == status)
            rows = (await s.execute(q.order_by(ApprovalRecord.created_at.desc()).limit(limit))).scalars().all()
            return [self._approval_dict(r) for r in rows]

    @staticmethod
    def _approval_dict(r: ApprovalRecord) -> dict:
        return {
            "approval_id": str(r.id),
            "task_id": str(r.task_id),
            "plan_id": str(r.plan_id) if r.plan_id else None,
            "plan_hash": r.plan_hash,
            "params_digest": r.params_digest,
            "permission_level": r.permission_level,
            "nonce": r.nonce,
            "status": r.status,
            "approver": r.approver,
            "channel": r.channel,
            "decision_note": r.decision_note,
            "expires_at": r.expires_at,
            "decided_at": r.decided_at,
            "consumed_at": r.consumed_at,
            "created_at": r.created_at,
        }

    # ── policy evaluations (P1-1a / P1-8) ───────────────────────────────────
    async def record_policy_evaluation(
        self,
        subject_type: str,
        requested_level: str,
        deterministic_level: str,
        effective_level: str,
        verdict: str,
        violations: list[str] | None = None,
        reason: str = "",
        cycle_id: uuid.UUID | None = None,
        subject_id: uuid.UUID | None = None,
        semantic_level: str | None = None,
    ) -> uuid.UUID:
        async with self.db.session() as s:
            row = PolicyEvaluation(
                cycle_id=cycle_id,
                subject_type=subject_type,
                subject_id=subject_id,
                requested_level=requested_level,
                deterministic_level=deterministic_level,
                semantic_level=semantic_level,
                effective_level=effective_level,
                verdict=verdict,
                violations=_jsonable(violations or []),
                reason=reason,
            )
            s.add(row)
            await s.flush()
            return row.id

    async def list_policy_evaluations(self, subject_type: str | None = None, limit: int = 50) -> list[dict]:
        async with self.db.session() as s:
            q = select(PolicyEvaluation)
            if subject_type:
                q = q.where(PolicyEvaluation.subject_type == subject_type)
            rows = (await s.execute(q.order_by(PolicyEvaluation.created_at.desc()).limit(limit))).scalars().all()
            return [
                {
                    "id": str(r.id),
                    "cycle_id": str(r.cycle_id) if r.cycle_id else None,
                    "subject_type": r.subject_type,
                    "subject_id": str(r.subject_id) if r.subject_id else None,
                    "requested_level": r.requested_level,
                    "deterministic_level": r.deterministic_level,
                    "semantic_level": r.semantic_level,
                    "effective_level": r.effective_level,
                    "verdict": r.verdict,
                    "violations": r.violations,
                    "reason": r.reason,
                    "created_at": r.created_at,
                }
                for r in rows
            ]


def day_start(now: datetime | None = None) -> datetime:
    now = now or utcnow()
    return now - timedelta(hours=24)
