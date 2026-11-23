"""State Repository: the only module that touches ORM rows directly.

Other modules call these methods (or use Pydantic models returned by them);
they never import SQLAlchemy sessions themselves.
"""

from __future__ import annotations

import hashlib
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Iterable

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.database import Database
from app.storage.models import (
    ActionPlanRow,
    ActiveGoal,
    AffectHistoryRow,
    AgentExecutionRow,
    AgentRegistryRow,
    AgentTaskRow,
    ApiCredential,
    ApprovalRecord,
    CapabilityEvidenceRow,
    CapabilityProfileRow,
    CycleRecord,
    DecisionLog,
    DriveHistory,
    DriveStateRow,
    EmbeddingModelRegistry,
    EventLog,
    ExecutionRecoveryRecord,
    GoalHistory,
    InboxEvent,
    MemoryEmbedding,
    MemoryIndex,
    ObservationRecord,
    OrganismIdentity,
    OrganismState,
    OutboxEvent,
    PolicyEvaluation,
    ResourceUsage,
    RunnerRegistryRow,
    SelfModelVersionRow,
    SensorCheckpoint,
    SkillInvocationRow,
    SkillRegistryRow,
    SkillValidationResultRow,
    SystemConfig,
    TaskLifecycleEvent,
    WorldEventRow,
    WorldEventSource,
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

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """Unit-of-work transactional scope shared across multiple repository writes."""
        async with self.db.session() as session:
            yield session

    @asynccontextmanager
    async def _use_session(self, session: AsyncSession | None = None) -> AsyncIterator[AsyncSession]:
        if session is not None:
            yield session
        else:
            async with self.db.session() as own:
                yield own

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
                "emotion_reserved": st.emotion_reserved or {},
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

    async def get_observation_for_cycle(self, cycle_id: uuid.UUID, phase: str = "pre") -> dict | None:
        async with self.db.session() as s:
            row = (
                await s.execute(
                    select(ObservationRecord)
                    .where(ObservationRecord.cycle_id == cycle_id, ObservationRecord.phase == phase)
                    .order_by(ObservationRecord.created_at.desc())
                )
            ).scalars().first()
            if row is None:
                return None
            return {
                "observation_id": str(row.id),
                "timestamp": row.observed_at,
                "system_state": row.system_state,
                "environment_state": row.environment_state,
                "available_resources": row.available_resources,
                "active_tasks": row.active_tasks,
                "external_events": row.external_events,
                "sensor_status": row.sensor_status,
                "confidence": row.confidence,
            }

    async def get_drive_history_for_cycle(self, cycle_id: uuid.UUID, phase: str = "pre") -> list[dict]:
        async with self.db.session() as s:
            rows = (
                await s.execute(
                    select(DriveHistory)
                    .where(DriveHistory.cycle_id == cycle_id, DriveHistory.phase == phase)
                    .order_by(DriveHistory.created_at.asc())
                )
            ).scalars().all()
            return [
                {
                    "drive_type": r.drive_type,
                    "satisfaction": r.satisfaction,
                    "deficit": r.deficit,
                    "urgency": r.urgency,
                    "confidence": r.confidence,
                    "persistence": r.persistence,
                    "trend": r.trend,
                    "dimensions": r.dimensions,
                    "updated_at": r.created_at,
                }
                for r in rows
            ]

    async def get_default_organism_id(self) -> uuid.UUID | None:
        async with self.db.session() as s:
            row = (await s.execute(select(OrganismIdentity).order_by(OrganismIdentity.created_at.asc()))).scalars().first()
            return row.id if row else None

    async def upsert_world_item(
        self,
        kind: str,
        key: str,
        value: dict | None = None,
        status: str | None = None,
        source: str | None = None,
        is_simulated: bool | None = None,
        session: AsyncSession | None = None,
    ) -> dict:
        safe_key = str(key).strip()
        if len(safe_key) > 250:
            digest = hashlib.sha256(safe_key.encode("utf-8")).hexdigest()[:12]
            safe_key = f"{safe_key[:230]}_{digest}"
        async with self._use_session(session) as s:
            row = (
                await s.execute(select(WorldState).where(WorldState.kind == kind, WorldState.key == safe_key))
            ).scalar_one_or_none()
            if row is None:
                row = WorldState(
                    kind=kind,
                    key=safe_key,
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
        preserve = {"duplicate_of", "cooldown_until"}
        fields = {k: (v if k in preserve else _jsonable(v)) for k, v in fields.items()}
        async with self.db.session() as s:
            g = ActiveGoal(organism_id=organism_id, cycle_id=cycle_id, **fields)
            s.add(g)
            await s.flush()
            s.add(GoalHistory(goal_id=g.id, from_status=None, to_status=g.status, note="created",
                              expected_outcome=g.expected_outcome or {}))
            return g.id

    async def update_goal(
        self, goal_id: uuid.UUID, session: AsyncSession | None = None, **values: Any
    ) -> None:
        preserve = {"duplicate_of", "cooldown_until", "updated_at"}
        clean = {k: (v if k in preserve else _jsonable(v)) for k, v in values.items()}
        clean.setdefault("updated_at", utcnow())
        async with self._use_session(session) as s:
            await s.execute(update(ActiveGoal).where(ActiveGoal.id == goal_id).values(**clean))

    async def list_recent_goals(self, since: datetime, limit: int = 80) -> list[dict]:
        async with self.db.session() as s:
            rows = (
                await s.execute(
                    select(ActiveGoal)
                    .where(ActiveGoal.created_at >= since)
                    .order_by(ActiveGoal.created_at.desc())
                    .limit(limit)
                )
            ).scalars().all()
            return [self._goal_dict(g) for g in rows]

    async def transition_goal(
        self,
        goal_id: uuid.UUID,
        to_status: str,
        note: str = "",
        actual_outcome: dict | None = None,
        measured_change: dict | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        async with self._use_session(session) as s:
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
            "goal_fingerprint": g.goal_fingerprint,
            "duplicate_of": str(g.duplicate_of) if g.duplicate_of else None,
            "cooldown_until": g.cooldown_until,
            "predicted_ig": g.predicted_ig,
            "verified_ig": g.verified_ig,
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
        preflight_status: str | None = None,
        preflight_reason_codes: list[str] | None = None,
        preflight_checked_at: datetime | None = None,
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
                preflight_status=preflight_status,
                preflight_reason_codes=_jsonable(preflight_reason_codes) if preflight_reason_codes is not None else None,
                preflight_checked_at=preflight_checked_at,
            )
            s.add(p)
            await s.flush()
            return p.id

    async def list_recent_plan_hashes(self, since: datetime, limit: int = 100) -> list[str]:
        async with self.db.session() as s:
            rows = (
                await s.execute(
                    select(ActionPlanRow.plan_hash)
                    .where(
                        ActionPlanRow.created_at >= since,
                        ActionPlanRow.plan_hash.is_not(None),
                    )
                    .order_by(ActionPlanRow.created_at.desc())
                    .limit(limit)
                )
            ).all()
            return [str(r[0]) for r in rows if r[0]]

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
                "preflight_status": p.preflight_status,
                "preflight_reason_codes": p.preflight_reason_codes,
                "preflight_checked_at": p.preflight_checked_at,
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
                    "stats": r.stats or {},
                }
                for r in rows
            ]

    async def update_agent_stats(
        self, agent_id: str, stats: dict[str, Any], session: AsyncSession | None = None
    ) -> bool:
        async with self._use_session(session) as s:
            row = (
                await s.execute(select(AgentRegistryRow).where(AgentRegistryRow.agent_id == agent_id))
            ).scalar_one_or_none()
            if row is None:
                return False
            row.stats = _jsonable(stats)
            row.updated_at = utcnow()
            await s.flush()
            return True

    async def create_task(self, **fields: Any) -> uuid.UUID:
        fields = {k: (_jsonable(v) if k not in {"id", "plan_id", "goal_id", "cycle_id"} else v) for k, v in fields.items()}
        async with self.db.session() as s:
            t = AgentTaskRow(**fields)
            s.add(t)
            await s.flush()
            return t.id

    async def update_task(
        self, task_id: uuid.UUID, session: AsyncSession | None = None, **values: Any
    ) -> None:
        clean = {k: (v if k in {"finalized_at"} else _jsonable(v)) for k, v in values.items()}
        clean["updated_at"] = utcnow()
        async with self._use_session(session) as s:
            await s.execute(update(AgentTaskRow).where(AgentTaskRow.id == task_id).values(**clean))

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
            "state_version": t.state_version,
            "finalized_at": t.finalized_at,
            "idempotency_key": t.idempotency_key,
            "attempt": t.attempt,
            "max_attempts": t.max_attempts,
            "tried_agents": t.tried_agents,
            "outcome": t.outcome,
            "created_at": t.created_at,
            "updated_at": t.updated_at,
        }

    async def create_execution(
        self,
        task_id: uuid.UUID,
        agent_id: str,
        status: str,
        execution_id: uuid.UUID | None = None,
        attempt_number: int = 1,
        idempotency_key: str | None = None,
        runner_id: str | None = None,
        session: AsyncSession | None = None,
    ) -> uuid.UUID:
        async with self._use_session(session) as s:
            e = AgentExecutionRow(
                id=execution_id or uuid.uuid4(),
                task_id=task_id,
                agent_id=agent_id,
                status=status,
                attempt_number=attempt_number,
                idempotency_key=idempotency_key,
                runner_id=runner_id,
            )
            s.add(e)
            await s.flush()
            return e.id

    async def update_execution(
        self, execution_id: uuid.UUID, session: AsyncSession | None = None, **values: Any
    ) -> None:
        clean = {}
        for k, v in values.items():
            clean[k] = v if k in {"started_at", "completed_at", "finalized_at"} else _jsonable(v)
        clean["updated_at"] = utcnow()
        async with self._use_session(session) as s:
            await s.execute(update(AgentExecutionRow).where(AgentExecutionRow.id == execution_id).values(**clean))

    async def finalize_superseded_executions(
        self,
        task_id: uuid.UUID,
        finalized_at: datetime,
        *,
        exclude_execution_id: uuid.UUID | None = None,
        session: AsyncSession | None = None,
    ) -> int:
        """Mark earlier terminal attempts of a task as finalized (V2.1 P0-1).

        Guarantees that when a retry supersedes an earlier attempt or when the task
        itself reaches finalization, no terminal execution of that task remains with
        `finalized_at IS NULL`.
        """
        terminal_exec_statuses = ["SUCCEEDED", "FAILED", "TIMEOUT", "CANCELLED"]
        stmt = (
            update(AgentExecutionRow)
            .where(
                AgentExecutionRow.task_id == task_id,
                AgentExecutionRow.finalized_at.is_(None),
                AgentExecutionRow.status.in_(terminal_exec_statuses),
            )
            .values(finalized_at=finalized_at, updated_at=finalized_at)
        )
        if exclude_execution_id is not None:
            stmt = stmt.where(AgentExecutionRow.id != exclude_execution_id)
        async with self._use_session(session) as s:
            res = await s.execute(stmt)
            return int(res.rowcount or 0)

    async def list_unfinalized_terminal_executions(self, limit: int = 50) -> list[dict]:
        """Return executions that reached a terminal execution status on a terminal task but are not finalized."""
        terminal_task_statuses = ["SUCCEEDED", "FAILED", "CANCELLED", "ABANDONED"]
        terminal_exec_statuses = ["SUCCEEDED", "FAILED", "TIMEOUT", "CANCELLED"]
        async with self.db.session() as s:
            rows = (
                await s.execute(
                    select(AgentExecutionRow)
                    .join(AgentTaskRow, AgentExecutionRow.task_id == AgentTaskRow.id)
                    .where(
                        AgentExecutionRow.finalized_at.is_(None),
                        AgentTaskRow.finalized_at.is_(None),
                        AgentExecutionRow.status.in_(terminal_exec_statuses),
                        AgentTaskRow.status.in_(terminal_task_statuses),
                    )
                    .order_by(AgentExecutionRow.completed_at.asc())
                    .limit(limit)
                )
            ).scalars().all()
            return [self._exec_dict(e) for e in rows]

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
            "attempt_number": e.attempt_number,
            "idempotency_key": e.idempotency_key,
            "runner_id": e.runner_id,
            "pid": e.pid,
            "process_identity": e.process_identity,
            "thread_id": e.thread_id,
            "started_at": e.started_at,
            "completed_at": e.completed_at,
            "finalized_at": e.finalized_at,
            "result": e.result,
            "stdout": e.stdout,
            "stderr": e.stderr,
            "error": e.error,
            "resource_usage": e.resource_usage,
            "verification": e.verification,
        }

    # ── memory index ────────────────────────────────────────────────────────
    async def add_memory_index(self, session: AsyncSession | None = None, **fields: Any) -> uuid.UUID:
        fields = {k: (_jsonable(v) if k != "cycle_id" else v) for k, v in fields.items()}
        async with self._use_session(session) as s:
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

    async def get_memory_by_id(self, memory_id: uuid.UUID) -> dict | None:
        async with self.db.session() as s:
            m = await s.get(MemoryIndex, memory_id)
            return self._mem_dict(m) if m else None

    async def get_latest_learning_memory(self) -> dict | None:
        """Return most recent semantic/procedural memory with persisted information_gain (replaces hardcoded 0.5)."""
        async with self.db.session() as s:
            m = (
                await s.execute(
                    select(MemoryIndex)
                    .where(MemoryIndex.memory_type.in_(["semantic", "procedural", "episodic"]))
                    .order_by(MemoryIndex.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            return self._mem_dict(m) if m else None

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
            "sync_error": m.sync_error,
            "embedding_status": m.embedding_status,
            "reliability": m.reliability,
            "information_gain": m.information_gain,
            "created_at": m.created_at,
        }

    # ── semantic embeddings & hybrid retrieval (P1-4) ───────────────────────
    async def upsert_embedding_model(
        self,
        *,
        model_key: str,
        provider: str,
        model_name: str,
        model_version: str,
        dimensions: int,
        normalized: bool = True,
        activate: bool = True,
    ) -> dict:
        async with self.db.session() as s:
            if activate:
                await s.execute(update(EmbeddingModelRegistry).values(is_active=False))
            row = (
                await s.execute(
                    select(EmbeddingModelRegistry).where(EmbeddingModelRegistry.model_key == model_key)
                )
            ).scalar_one_or_none()
            if row is None:
                row = EmbeddingModelRegistry(
                    model_key=model_key,
                    provider=provider,
                    model_name=model_name,
                    model_version=model_version,
                    dimensions=dimensions,
                    normalized=normalized,
                    is_active=activate,
                )
                s.add(row)
            else:
                if row.dimensions != dimensions:
                    raise ValueError(
                        f"Model '{model_key}' already registered with dim={row.dimensions}, cannot overwrite with dim={dimensions}"
                    )
                row.provider = provider
                row.model_name = model_name
                row.model_version = model_version
                row.normalized = normalized
                if activate:
                    row.is_active = True
            await s.flush()
            return self._emb_model_dict(row)

    async def get_active_embedding_model(self) -> dict | None:
        async with self.db.session() as s:
            row = (
                await s.execute(
                    select(EmbeddingModelRegistry).where(EmbeddingModelRegistry.is_active.is_(True))
                )
            ).scalars().first()
            return self._emb_model_dict(row) if row else None

    async def list_embedding_models(self) -> list[dict]:
        async with self.db.session() as s:
            rows = (
                await s.execute(select(EmbeddingModelRegistry).order_by(EmbeddingModelRegistry.created_at.desc()))
            ).scalars().all()
            return [self._emb_model_dict(r) for r in rows]

    @staticmethod
    def _emb_model_dict(r: EmbeddingModelRegistry) -> dict:
        return {
            "id": str(r.id),
            "model_key": r.model_key,
            "provider": r.provider,
            "model_name": r.model_name,
            "model_version": r.model_version,
            "dimensions": r.dimensions,
            "normalized": r.normalized,
            "is_active": r.is_active,
            "created_at": r.created_at,
        }

    async def upsert_memory_embedding(
        self,
        *,
        memory_id: uuid.UUID,
        model_id: uuid.UUID,
        dimensions: int,
        embedding: list[float],
        content_hash: str,
    ) -> uuid.UUID:
        if len(embedding) != dimensions:
            raise ValueError(f"Embedding vector length {len(embedding)} != declared dimensions {dimensions}")
        async with self.db.session() as s:
            model = await s.get(EmbeddingModelRegistry, model_id)
            if model is not None and model.dimensions != dimensions:
                raise ValueError(
                    f"Dimension mismatch: model {model.model_key} expects {model.dimensions}, got {dimensions}"
                )
            existing = (
                await s.execute(
                    select(MemoryEmbedding).where(
                        MemoryEmbedding.memory_id == memory_id,
                        MemoryEmbedding.model_id == model_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                row = MemoryEmbedding(
                    memory_id=memory_id,
                    model_id=model_id,
                    dimensions=dimensions,
                    embedding=[float(x) for x in embedding],
                    content_hash=content_hash,
                )
                s.add(row)
                await s.flush()
                return row.id
            existing.dimensions = dimensions
            existing.embedding = [float(x) for x in embedding]
            existing.content_hash = content_hash
            existing.created_at = utcnow()
            await s.flush()
            return existing.id

    async def list_memories_missing_embedding(self, model_id: uuid.UUID, limit: int = 200) -> list[dict]:
        async with self.db.session() as s:
            subq = select(MemoryEmbedding.memory_id).where(MemoryEmbedding.model_id == model_id)
            rows = (
                await s.execute(
                    select(MemoryIndex)
                    .where(~MemoryIndex.id.in_(subq))
                    .order_by(MemoryIndex.created_at.desc())
                    .limit(limit)
                )
            ).scalars().all()
            return [self._mem_dict(m) for m in rows]

    async def fetch_hybrid_memory_candidates(
        self,
        *,
        query: str = "",
        model_id: uuid.UUID | None = None,
        query_vector: list[float] | None = None,
        memory_types: list[str] | None = None,
        limit: int = 120,
    ) -> list[dict]:
        """Fetch memory candidates joined ONLY with embeddings of the active model_id (never mixes dimensions)."""
        async with self.db.session() as s:
            is_pg = s.bind is not None and s.bind.dialect.name == "postgresql"
            if is_pg and model_id is not None and query_vector is not None:
                # Native PostgreSQL pgvector + pg_trgm fast path when extensions are installed
                try:
                    vec_literal = "[" + ",".join(f"{float(x):.6f}" for x in query_vector) + "]"
                    type_clause = "AND m.memory_type = ANY(:mtypes)" if memory_types else ""
                    sql = text(
                        f"""
                        SELECT m.id, m.memory_type, m.external_id, m.title, m.summary, m.content_hash,
                               m.source, m.importance, m.tags, m.cycle_id, m.sync_status, m.sync_error,
                               m.embedding_status, m.reliability, m.information_gain, m.created_at,
                               CASE WHEN e.embedding IS NOT NULL
                                    THEN GREATEST(0.0, 1.0 - (e.embedding <=> CAST(:qvec AS vector)))
                                    ELSE NULL END AS pg_vec_sim,
                               CASE WHEN :qtext <> ''
                                    THEN GREATEST(
                                        similarity(m.title || ' ' || m.summary, :qtext),
                                        word_similarity(:qtext, m.title || ' ' || m.summary)
                                    )
                                    ELSE 0.0 END AS pg_trgm_sim
                        FROM memory_index m
                        LEFT JOIN memory_embeddings e
                               ON e.memory_id = m.id AND e.model_id = :mid
                        WHERE 1=1 {type_clause}
                        ORDER BY COALESCE(
                            CASE WHEN e.embedding IS NOT NULL THEN 1.0 - (e.embedding <=> CAST(:qvec AS vector)) END,
                            0.0
                        ) DESC, m.created_at DESC
                        LIMIT :lim
                        """
                    )
                    params: dict[str, Any] = {
                        "qvec": vec_literal,
                        "qtext": query,
                        "mid": model_id,
                        "lim": limit,
                    }
                    if memory_types:
                        params["mtypes"] = memory_types
                    res = (await s.execute(sql, params)).mappings().all()
                    return [
                        {
                            "memory_id": str(r["id"]),
                            "memory_type": r["memory_type"],
                            "external_id": r["external_id"],
                            "title": r["title"],
                            "summary": r["summary"],
                            "content_hash": r["content_hash"],
                            "source": r["source"],
                            "importance": float(r["importance"]),
                            "tags": list(r["tags"] or []),
                            "cycle_id": str(r["cycle_id"]) if r["cycle_id"] else None,
                            "sync_status": r["sync_status"],
                            "sync_error": r["sync_error"],
                            "embedding_status": r["embedding_status"],
                            "reliability": float(r["reliability"]),
                            "information_gain": r["information_gain"],
                            "created_at": r["created_at"],
                            "pg_vec_sim": float(r["pg_vec_sim"]) if r["pg_vec_sim"] is not None else None,
                            "pg_trgm_sim": float(r["pg_trgm_sim"]) if r["pg_trgm_sim"] is not None else None,
                            "embedding": None,
                        }
                        for r in res
                    ]
                except Exception:
                    # Fallback cleanly if PG instance lacks pgvector/pg_trgm extension
                    await s.rollback()

            q = select(MemoryIndex)
            if memory_types:
                q = q.where(MemoryIndex.memory_type.in_(memory_types))
            mem_rows = (
                await s.execute(q.order_by(MemoryIndex.created_at.desc()).limit(limit))
            ).scalars().all()
            mem_ids = [m.id for m in mem_rows]

            emb_map: dict[uuid.UUID, list[float]] = {}
            if model_id is not None and mem_ids:
                emb_rows = (
                    await s.execute(
                        select(MemoryEmbedding).where(
                            MemoryEmbedding.model_id == model_id,
                            MemoryEmbedding.memory_id.in_(mem_ids),
                        )
                    )
                ).scalars().all()
                for er in emb_rows:
                    emb_map[er.memory_id] = list(er.embedding)

            out: list[dict] = []
            for m in mem_rows:
                d = self._mem_dict(m)
                d["embedding"] = emb_map.get(m.id)
                d["pg_vec_sim"] = None
                d["pg_trgm_sim"] = None
                out.append(d)
            return out

    async def list_embeddings_for_model(self, model_id: uuid.UUID, limit: int = 100) -> list[dict]:
        async with self.db.session() as s:
            rows = (
                await s.execute(
                    select(MemoryEmbedding, MemoryIndex)
                    .join(MemoryIndex, MemoryEmbedding.memory_id == MemoryIndex.id)
                    .where(MemoryEmbedding.model_id == model_id)
                    .order_by(MemoryIndex.created_at.desc())
                    .limit(limit)
                )
            ).all()
            return [
                {
                    "memory_id": str(m.id),
                    "content_hash": m.content_hash,
                    "source": m.source,
                    "title": m.title,
                    "summary": m.summary,
                    "embedding": list(e.embedding),
                    "dimensions": e.dimensions,
                }
                for e, m in rows
            ]

    async def get_memory_embedding_stats(self) -> dict[str, Any]:
        async with self.db.session() as s:
            status_rows = (
                await s.execute(
                    select(MemoryIndex.embedding_status, func.count(MemoryIndex.id)).group_by(
                        MemoryIndex.embedding_status
                    )
                )
            ).all()
            by_status = {str(st): int(cnt) for st, cnt in status_rows}
            models = (
                await s.execute(select(EmbeddingModelRegistry).order_by(EmbeddingModelRegistry.created_at.desc()))
            ).scalars().all()
            model_counts = (
                await s.execute(
                    select(MemoryEmbedding.model_id, func.count(MemoryEmbedding.id)).group_by(
                        MemoryEmbedding.model_id
                    )
                )
            ).all()
            cnt_map = {str(mid): int(cnt) for mid, cnt in model_counts}
            return {
                "by_status": by_status,
                "total_memories": sum(by_status.values()),
                "models": [
                    {**self._emb_model_dict(m), "embedded_count": cnt_map.get(str(m.id), 0)} for m in models
                ],
            }

    async def get_memory_sync_stats(self) -> dict[str, Any]:
        async with self.db.session() as s:
            sync_rows = (
                await s.execute(
                    select(MemoryIndex.sync_status, func.count(MemoryIndex.id)).group_by(MemoryIndex.sync_status)
                )
            ).all()
            outbox_rows = (
                await s.execute(
                    select(OutboxEvent.status, func.count(OutboxEvent.id))
                    .where(OutboxEvent.event_type.in_(["memory.sync_letta", "memory.embed"]))
                    .group_by(OutboxEvent.status)
                )
            ).all()
            return {
                "memory_index_by_sync_status": {str(st): int(cnt) for st, cnt in sync_rows},
                "outbox_by_status": {str(st): int(cnt) for st, cnt in outbox_rows},
            }

    async def replay_memory_sync_outbox(self, event_type: str | None = None, limit: int = 50) -> int:
        """Reset dead_letter / failed memory outbox events back to pending for retry."""
        target_types = [event_type] if event_type else ["memory.sync_letta", "memory.embed"]
        async with self.db.session() as s:
            rows = (
                await s.execute(
                    select(OutboxEvent.id)
                    .where(
                        OutboxEvent.event_type.in_(target_types),
                        OutboxEvent.status.in_(["dead_letter", "pending"]),
                    )
                    .limit(limit)
                )
            ).scalars().all()
            if not rows:
                return 0
            await s.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id.in_(rows))
                .values(status="pending", attempts=0, next_retry_at=utcnow(), locked_at=None, last_error=None)
            )
            return len(rows)

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

    async def oldest_usage_since(self, category: str, since: datetime) -> datetime | None:
        async with self.db.session() as s:
            ts = (
                await s.execute(
                    select(func.min(ResourceUsage.created_at)).where(
                        ResourceUsage.category == category, ResourceUsage.created_at >= since
                    )
                )
            ).scalar_one_or_none()
            if ts is not None and ts.tzinfo is None:
                return ts.replace(tzinfo=timezone.utc)
            return ts

    async def emergency_llm_usage_since(self, since: datetime) -> dict:
        async with self.db.session() as s:
            rows = (
                await s.execute(
                    select(ResourceUsage.amount, ResourceUsage.details).where(
                        ResourceUsage.category == "llm", ResourceUsage.created_at >= since
                    )
                )
            ).all()
            count = 0
            tokens = 0.0
            for amount, details in rows:
                if isinstance(details, dict) and details.get("emergency") is True:
                    count += 1
                    tokens += float(amount or 0.0)
            return {"count": count, "amount": tokens}

    async def llm_usage_for_cycle(self, cycle_id: uuid.UUID) -> dict:
        async with self.db.session() as s:
            row = (
                await s.execute(
                    select(func.count(ResourceUsage.id), func.coalesce(func.sum(ResourceUsage.amount), 0.0)).where(
                        ResourceUsage.category == "llm", ResourceUsage.cycle_id == cycle_id
                    )
                )
            ).one()
            return {"calls": int(row[0]), "tokens": float(row[1])}

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

    async def get_approval_by_nonce(self, nonce: str) -> dict | None:
        async with self.db.session() as s:
            row = (
                await s.execute(select(ApprovalRecord).where(ApprovalRecord.nonce == nonce))
            ).scalar_one_or_none()
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

    # ── durable task lifecycle & outbox/inbox (P1-2) ────────────────────────
    async def list_task_lifecycle_events(self, task_id: uuid.UUID) -> list[dict]:
        async with self.db.session() as s:
            rows = (
                await s.execute(
                    select(TaskLifecycleEvent)
                    .where(TaskLifecycleEvent.task_id == task_id)
                    .order_by(TaskLifecycleEvent.state_version.asc())
                )
            ).scalars().all()
            return [
                {
                    "id": str(r.id),
                    "task_id": str(r.task_id),
                    "execution_id": str(r.execution_id) if r.execution_id else None,
                    "state_version": r.state_version,
                    "from_status": r.from_status,
                    "to_status": r.to_status,
                    "actor": r.actor,
                    "reason": r.reason,
                    "idempotency_key": r.idempotency_key,
                    "payload": r.payload,
                    "created_at": r.created_at,
                }
                for r in rows
            ]

    async def enqueue_outbox_event(
        self,
        event_type: str,
        idempotency_key: str,
        payload: dict[str, Any],
        aggregate_type: str | None = None,
        aggregate_id: uuid.UUID | None = None,
        max_attempts: int = 5,
        next_retry_at: datetime | None = None,
        session: AsyncSession | None = None,
    ) -> uuid.UUID | None:
        """Atomically enqueue an outbox event (idempotent on idempotency_key)."""
        async with self._use_session(session) as s:
            existing = (
                await s.execute(select(OutboxEvent.id).where(OutboxEvent.idempotency_key == idempotency_key))
            ).scalar_one_or_none()
            if existing is not None:
                return existing
            ev = OutboxEvent(
                event_type=event_type,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                idempotency_key=idempotency_key,
                payload=_jsonable(payload),
                status="pending",
                attempts=0,
                max_attempts=max_attempts,
                next_retry_at=next_retry_at or utcnow(),
            )
            s.add(ev)
            await s.flush()
            return ev.id

    async def list_outbox_events(
        self,
        status: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        async with self.db.session() as s:
            q = select(OutboxEvent)
            if status:
                q = q.where(OutboxEvent.status == status)
            if event_type:
                q = q.where(OutboxEvent.event_type == event_type)
            rows = (await s.execute(q.order_by(OutboxEvent.created_at.desc()).limit(limit))).scalars().all()
            return [
                {
                    "id": str(r.id),
                    "event_type": r.event_type,
                    "aggregate_type": r.aggregate_type,
                    "aggregate_id": str(r.aggregate_id) if r.aggregate_id else None,
                    "idempotency_key": r.idempotency_key,
                    "payload": r.payload,
                    "status": r.status,
                    "attempts": r.attempts,
                    "max_attempts": r.max_attempts,
                    "next_retry_at": r.next_retry_at,
                    "dispatched_at": r.dispatched_at,
                    "last_error": r.last_error,
                    "created_at": r.created_at,
                }
                for r in rows
            ]

    async def has_inbox_event(
        self, consumer_name: str, idempotency_key: str, session: AsyncSession | None = None
    ) -> bool:
        async with self._use_session(session) as s:
            found = (
                await s.execute(
                    select(InboxEvent.id).where(
                        InboxEvent.consumer_name == consumer_name,
                        InboxEvent.idempotency_key == idempotency_key,
                    )
                )
            ).scalar_one_or_none()
            return found is not None

    async def mark_inbox_processed(
        self,
        consumer_name: str,
        idempotency_key: str,
        event_type: str | None = None,
        result_summary: dict[str, Any] | None = None,
        session: AsyncSession | None = None,
    ) -> bool:
        """Record consumption in inbox_events. Returns True if newly recorded, False if duplicate."""
        async with self._use_session(session) as s:
            existing = (
                await s.execute(
                    select(InboxEvent.id).where(
                        InboxEvent.consumer_name == consumer_name,
                        InboxEvent.idempotency_key == idempotency_key,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return False
            s.add(
                InboxEvent(
                    consumer_name=consumer_name,
                    idempotency_key=idempotency_key,
                    event_type=event_type,
                    processed_at=utcnow(),
                    result_summary=_jsonable(result_summary or {}),
                )
            )
            try:
                await s.flush()
                return True
            except IntegrityError:
                return False

    async def replay_outbox_event(self, event_id: uuid.UUID) -> bool:
        async with self.db.session() as s:
            res = await s.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id == event_id)
                .values(status="pending", attempts=0, next_retry_at=utcnow(), locked_at=None, last_error=None)
            )
            return (res.rowcount or 0) == 1

    # ── host agent runner registry & crash recovery audit (P1-3) ────────────
    async def upsert_runner(
        self,
        *,
        runner_id: str,
        socket_path: str,
        host_boot_id: str | None = None,
        systemd_unit: str | None = None,
        status: str = "online",
        capabilities: dict[str, Any] | None = None,
        last_heartbeat_at: datetime | None = None,
    ) -> dict:
        now = utcnow()
        async with self.db.session() as s:
            row = (
                await s.execute(select(RunnerRegistryRow).where(RunnerRegistryRow.runner_id == runner_id))
            ).scalar_one_or_none()
            if row is None:
                row = RunnerRegistryRow(
                    runner_id=runner_id,
                    socket_path=socket_path,
                    host_boot_id=host_boot_id,
                    systemd_unit=systemd_unit,
                    status=status,
                    capabilities=_jsonable(capabilities or {}),
                    last_heartbeat_at=last_heartbeat_at or now,
                    updated_at=now,
                )
                s.add(row)
            else:
                row.socket_path = socket_path
                if host_boot_id is not None:
                    row.host_boot_id = host_boot_id
                if systemd_unit is not None:
                    row.systemd_unit = systemd_unit
                row.status = status
                if capabilities is not None:
                    row.capabilities = _jsonable(capabilities)
                row.last_heartbeat_at = last_heartbeat_at or now
                row.updated_at = now
            await s.flush()
            return self._runner_dict(row)

    async def get_runner(self, runner_id: str) -> dict | None:
        async with self.db.session() as s:
            row = (
                await s.execute(select(RunnerRegistryRow).where(RunnerRegistryRow.runner_id == runner_id))
            ).scalar_one_or_none()
            return self._runner_dict(row) if row else None

    async def list_runners(self) -> list[dict]:
        async with self.db.session() as s:
            rows = (
                await s.execute(select(RunnerRegistryRow).order_by(RunnerRegistryRow.runner_id.asc()))
            ).scalars().all()
            return [self._runner_dict(r) for r in rows]

    @staticmethod
    def _runner_dict(r: RunnerRegistryRow) -> dict:
        return {
            "id": str(r.id),
            "runner_id": r.runner_id,
            "socket_path": r.socket_path,
            "host_boot_id": r.host_boot_id,
            "systemd_unit": r.systemd_unit,
            "status": r.status,
            "capabilities": r.capabilities,
            "last_heartbeat_at": r.last_heartbeat_at,
            "updated_at": r.updated_at,
        }

    async def create_recovery_record(
        self,
        *,
        execution_id: uuid.UUID,
        task_id: uuid.UUID,
        action_taken: str,
        runner_id: str | None = None,
        trigger: str = "startup",
        observed_identity: dict[str, Any] | None = None,
        previous_status: str | None = None,
        resolved_status: str | None = None,
        notes: str = "",
    ) -> uuid.UUID:
        async with self.db.session() as s:
            row = ExecutionRecoveryRecord(
                execution_id=execution_id,
                task_id=task_id,
                runner_id=runner_id,
                trigger=trigger,
                observed_identity=_jsonable(observed_identity or {}),
                previous_status=previous_status,
                resolved_status=resolved_status,
                action_taken=action_taken,
                notes=notes,
            )
            s.add(row)
            await s.flush()
            return row.id

    async def list_recovery_records(
        self, execution_id: uuid.UUID | None = None, task_id: uuid.UUID | None = None, limit: int = 50
    ) -> list[dict]:
        async with self.db.session() as s:
            q = select(ExecutionRecoveryRecord)
            if execution_id is not None:
                q = q.where(ExecutionRecoveryRecord.execution_id == execution_id)
            if task_id is not None:
                q = q.where(ExecutionRecoveryRecord.task_id == task_id)
            rows = (
                await s.execute(q.order_by(ExecutionRecoveryRecord.created_at.desc()).limit(limit))
            ).scalars().all()
            return [
                {
                    "id": str(r.id),
                    "execution_id": str(r.execution_id),
                    "task_id": str(r.task_id),
                    "runner_id": r.runner_id,
                    "trigger": r.trigger,
                    "observed_identity": r.observed_identity,
                    "previous_status": r.previous_status,
                    "resolved_status": r.resolved_status,
                    "action_taken": r.action_taken,
                    "notes": r.notes,
                    "created_at": r.created_at,
                }
                for r in rows
            ]

    async def get_execution_by_idempotency_key(self, idempotency_key: str) -> dict | None:
        async with self.db.session() as s:
            row = (
                await s.execute(
                    select(AgentExecutionRow).where(AgentExecutionRow.idempotency_key == idempotency_key)
                )
            ).scalar_one_or_none()
            return self._exec_dict(row) if row else None

    async def count_recent_error_events(self, minutes: int = 15) -> int:
        since = utcnow() - timedelta(minutes=max(1, int(minutes)))
        async with self.db.session() as s:
            return int(
                (
                    await s.execute(
                        select(func.count(EventLog.id)).where(
                            EventLog.level.in_(["error", "critical"]),
                            EventLog.created_at >= since,
                        )
                    )
                ).scalar_one()
            )

    # ── digital world perception sensors & events (P1-5) ────────────────────
    async def upsert_world_source(
        self,
        *,
        source_id: str,
        sensor_type: str,
        name: str = "",
        config: dict[str, Any] | None = None,
        reliability_score: float = 0.7,
        poll_interval_seconds: int = 900,
        enabled: bool = True,
    ) -> dict:
        now = utcnow()
        async with self.db.session() as s:
            row = (
                await s.execute(select(WorldEventSource).where(WorldEventSource.source_id == source_id))
            ).scalar_one_or_none()
            if row is None:
                row = WorldEventSource(
                    source_id=source_id,
                    sensor_type=sensor_type,
                    name=name,
                    config=_jsonable(config or {}),
                    reliability_score=float(reliability_score),
                    poll_interval_seconds=int(poll_interval_seconds),
                    enabled=bool(enabled),
                    updated_at=now,
                )
                s.add(row)
            else:
                row.sensor_type = sensor_type
                row.name = name or row.name
                if config is not None:
                    row.config = _jsonable(config)
                row.reliability_score = float(reliability_score)
                row.poll_interval_seconds = int(poll_interval_seconds)
                row.enabled = bool(enabled)
                row.updated_at = now
            await s.flush()
            return self._world_source_dict(row)

    async def get_world_source(self, source_id: str) -> dict | None:
        async with self.db.session() as s:
            row = (
                await s.execute(select(WorldEventSource).where(WorldEventSource.source_id == source_id))
            ).scalar_one_or_none()
            if row is None:
                return None
            cp = (
                await s.execute(select(SensorCheckpoint).where(SensorCheckpoint.source_id == source_id))
            ).scalar_one_or_none()
            return self._world_source_dict(row, cp)

    async def list_world_sources(self, enabled_only: bool = False) -> list[dict]:
        async with self.db.session() as s:
            q = select(WorldEventSource)
            if enabled_only:
                q = q.where(WorldEventSource.enabled.is_(True))
            rows = (await s.execute(q.order_by(WorldEventSource.source_id.asc()))).scalars().all()
            cps = (await s.execute(select(SensorCheckpoint))).scalars().all()
            cp_map = {c.source_id: c for c in cps}
            return [self._world_source_dict(r, cp_map.get(r.source_id)) for r in rows]

    async def set_world_source_enabled(self, source_id: str, enabled: bool) -> dict | None:
        async with self.db.session() as s:
            row = (
                await s.execute(select(WorldEventSource).where(WorldEventSource.source_id == source_id))
            ).scalar_one_or_none()
            if row is None:
                return None
            row.enabled = bool(enabled)
            row.updated_at = utcnow()
            await s.flush()
            return self._world_source_dict(row)

    async def update_world_source_health(
        self, source_id: str, *, health_status: str, last_error: str | None = None
    ) -> None:
        async with self.db.session() as s:
            await s.execute(
                update(WorldEventSource)
                .where(WorldEventSource.source_id == source_id)
                .values(health_status=health_status, last_error=last_error, updated_at=utcnow())
            )

    @staticmethod
    def _world_source_dict(r: WorldEventSource, cp: SensorCheckpoint | None = None) -> dict:
        d = {
            "id": str(r.id),
            "source_id": r.source_id,
            "sensor_type": r.sensor_type,
            "name": r.name,
            "config": r.config,
            "reliability_score": float(r.reliability_score),
            "poll_interval_seconds": int(r.poll_interval_seconds),
            "enabled": bool(r.enabled),
            "health_status": r.health_status,
            "last_error": r.last_error,
            "updated_at": r.updated_at,
        }
        if cp is not None:
            d["checkpoint"] = {
                "last_polled_at": cp.last_polled_at,
                "last_success_at": cp.last_success_at,
                "consecutive_failures": cp.consecutive_failures,
            }
        return d

    async def get_sensor_checkpoint(self, source_id: str) -> dict | None:
        async with self.db.session() as s:
            cp = (
                await s.execute(select(SensorCheckpoint).where(SensorCheckpoint.source_id == source_id))
            ).scalar_one_or_none()
            if cp is None:
                return None
            return {
                "source_id": cp.source_id,
                "cursor": cp.cursor,
                "last_polled_at": cp.last_polled_at,
                "last_success_at": cp.last_success_at,
                "consecutive_failures": cp.consecutive_failures,
                "updated_at": cp.updated_at,
            }

    async def upsert_sensor_checkpoint(
        self,
        *,
        source_id: str,
        cursor: dict[str, Any],
        last_polled_at: datetime | None = None,
        last_success_at: datetime | None = None,
        consecutive_failures: int = 0,
    ) -> None:
        now = utcnow()
        async with self.db.session() as s:
            cp = (
                await s.execute(select(SensorCheckpoint).where(SensorCheckpoint.source_id == source_id))
            ).scalar_one_or_none()
            if cp is None:
                s.add(
                    SensorCheckpoint(
                        source_id=source_id,
                        cursor=_jsonable(cursor),
                        last_polled_at=last_polled_at or now,
                        last_success_at=last_success_at,
                        consecutive_failures=int(consecutive_failures),
                        updated_at=now,
                    )
                )
            else:
                cp.cursor = _jsonable(cursor)
                if last_polled_at is not None:
                    cp.last_polled_at = last_polled_at
                if last_success_at is not None:
                    cp.last_success_at = last_success_at
                cp.consecutive_failures = int(consecutive_failures)
                cp.updated_at = now

    async def world_event_exists(self, source_id: str, content_hash: str) -> bool:
        async with self.db.session() as s:
            found = (
                await s.execute(
                    select(WorldEventRow.id).where(
                        WorldEventRow.source_id == source_id,
                        WorldEventRow.content_hash == content_hash,
                    )
                )
            ).scalar_one_or_none()
            return found is not None

    async def create_world_event(
        self,
        *,
        source_id: str,
        event_type: str,
        title: str,
        content: str,
        content_hash: str,
        source_reliability: float,
        importance_score: float,
        status: str,
        metadata: dict[str, Any],
        observed_at: datetime,
    ) -> uuid.UUID | None:
        async with self.db.session() as s:
            row = WorldEventRow(
                source_id=source_id,
                event_type=event_type,
                title=title[:512],
                content=content,
                content_hash=content_hash,
                source_reliability=float(source_reliability),
                importance_score=float(importance_score),
                status=status,
                meta=_jsonable(metadata),
                observed_at=observed_at,
            )
            s.add(row)
            try:
                await s.flush()
                return row.id
            except IntegrityError:
                await s.rollback()
                return None

    async def list_world_events(
        self,
        *,
        source_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        async with self.db.session() as s:
            q = select(WorldEventRow)
            if source_id:
                q = q.where(WorldEventRow.source_id == source_id)
            if status:
                q = q.where(WorldEventRow.status == status)
            rows = (
                await s.execute(q.order_by(WorldEventRow.observed_at.desc()).limit(limit))
            ).scalars().all()
            return [
                {
                    "id": str(r.id),
                    "source_id": r.source_id,
                    "event_type": r.event_type,
                    "title": r.title,
                    "content": r.content,
                    "content_hash": r.content_hash,
                    "source_reliability": float(r.source_reliability),
                    "importance_score": float(r.importance_score),
                    "status": r.status,
                    "metadata": r.meta,
                    "observed_at": r.observed_at,
                    "created_at": r.created_at,
                }
                for r in rows
            ]

    # ── dynamic self model, capability profiles & skill registry (P1-7) ─────
    @staticmethod
    def _cap_profile_dict(r: CapabilityProfileRow) -> dict[str, Any]:
        return {
            "id": str(r.id),
            "organism_id": str(r.organism_id),
            "capability_key": r.capability_key,
            "maturity_status": r.maturity_status,
            "alpha": float(r.alpha),
            "beta": float(r.beta),
            "sample_count": int(r.sample_count),
            "estimated_success_rate": float(r.estimated_success_rate),
            "confidence_low": float(r.confidence_low),
            "confidence_high": float(r.confidence_high),
            "updated_at": r.updated_at,
        }

    async def get_capability_profile(
        self, organism_id: uuid.UUID, capability_key: str, session: AsyncSession | None = None
    ) -> dict[str, Any] | None:
        async with self._use_session(session) as s:
            row = (
                await s.execute(
                    select(CapabilityProfileRow).where(
                        CapabilityProfileRow.organism_id == organism_id,
                        CapabilityProfileRow.capability_key == capability_key,
                    )
                )
            ).scalar_one_or_none()
            return self._cap_profile_dict(row) if row is not None else None

    async def upsert_capability_profile(
        self,
        *,
        organism_id: uuid.UUID,
        capability_key: str,
        maturity_status: str,
        alpha: float = 1.0,
        beta: float = 1.0,
        sample_count: int = 0,
        estimated_success_rate: float = 0.5,
        confidence_low: float = 0.0,
        confidence_high: float = 1.0,
        session: AsyncSession | None = None,
    ) -> dict[str, Any]:
        now = utcnow()
        async with self._use_session(session) as s:
            row = (
                await s.execute(
                    select(CapabilityProfileRow).where(
                        CapabilityProfileRow.organism_id == organism_id,
                        CapabilityProfileRow.capability_key == capability_key,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = CapabilityProfileRow(
                    organism_id=organism_id,
                    capability_key=capability_key,
                    maturity_status=maturity_status,
                    alpha=float(alpha),
                    beta=float(beta),
                    sample_count=int(sample_count),
                    estimated_success_rate=float(estimated_success_rate),
                    confidence_low=float(confidence_low),
                    confidence_high=float(confidence_high),
                    updated_at=now,
                )
                s.add(row)
            else:
                row.maturity_status = maturity_status
                row.alpha = float(alpha)
                row.beta = float(beta)
                row.sample_count = int(sample_count)
                row.estimated_success_rate = float(estimated_success_rate)
                row.confidence_low = float(confidence_low)
                row.confidence_high = float(confidence_high)
                row.updated_at = now
            await s.flush()
            return self._cap_profile_dict(row)

    async def list_capability_profiles(
        self,
        organism_id: uuid.UUID,
        *,
        statuses: list[str] | None = None,
        session: AsyncSession | None = None,
    ) -> list[dict[str, Any]]:
        async with self._use_session(session) as s:
            q = select(CapabilityProfileRow).where(CapabilityProfileRow.organism_id == organism_id)
            if statuses:
                q = q.where(CapabilityProfileRow.maturity_status.in_(statuses))
            rows = (await s.execute(q.order_by(CapabilityProfileRow.capability_key.asc()))).scalars().all()
            return [self._cap_profile_dict(r) for r in rows]

    async def add_capability_evidence(
        self,
        *,
        profile_id: uuid.UUID,
        execution_id: uuid.UUID,
        task_id: uuid.UUID,
        outcome: str,
        weight: float = 1.0,
        verification_summary: str = "",
        session: AsyncSession | None = None,
    ) -> tuple[bool, uuid.UUID | None]:
        """Insert capability evidence idempotently on (profile_id, execution_id)."""
        async with self._use_session(session) as s:
            existing = (
                await s.execute(
                    select(CapabilityEvidenceRow.id).where(
                        CapabilityEvidenceRow.profile_id == profile_id,
                        CapabilityEvidenceRow.execution_id == execution_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return False, existing
            row = CapabilityEvidenceRow(
                profile_id=profile_id,
                execution_id=execution_id,
                task_id=task_id,
                outcome=outcome,
                weight=float(weight),
                verification_summary=verification_summary,
                created_at=utcnow(),
            )
            try:
                async with s.begin_nested():
                    s.add(row)
                    await s.flush()
                return True, row.id
            except IntegrityError:
                existing2 = (
                    await s.execute(
                        select(CapabilityEvidenceRow.id).where(
                            CapabilityEvidenceRow.profile_id == profile_id,
                            CapabilityEvidenceRow.execution_id == execution_id,
                        )
                    )
                ).scalar_one_or_none()
                return False, existing2

    async def list_capability_evidence(
        self,
        profile_id: uuid.UUID,
        *,
        limit: int = 200,
        session: AsyncSession | None = None,
    ) -> list[dict[str, Any]]:
        async with self._use_session(session) as s:
            rows = (
                await s.execute(
                    select(CapabilityEvidenceRow)
                    .where(CapabilityEvidenceRow.profile_id == profile_id)
                    .order_by(CapabilityEvidenceRow.created_at.desc())
                    .limit(limit)
                )
            ).scalars().all()
            return [
                {
                    "id": str(r.id),
                    "profile_id": str(r.profile_id),
                    "execution_id": str(r.execution_id),
                    "task_id": str(r.task_id),
                    "outcome": r.outcome,
                    "weight": float(r.weight),
                    "verification_summary": r.verification_summary,
                    "created_at": r.created_at,
                }
                for r in rows
            ]

    @staticmethod
    def _self_model_version_dict(r: SelfModelVersionRow) -> dict[str, Any]:
        return {
            "id": str(r.id),
            "organism_id": str(r.organism_id),
            "version_number": int(r.version_number),
            "summary_text": r.summary_text,
            "profile_snapshot": r.profile_snapshot,
            "trigger_execution_id": str(r.trigger_execution_id) if r.trigger_execution_id else None,
            "letta_synced_at": r.letta_synced_at,
            "created_at": r.created_at,
        }

    async def create_self_model_version(
        self,
        *,
        organism_id: uuid.UUID,
        summary_text: str,
        profile_snapshot: dict[str, Any],
        trigger_execution_id: uuid.UUID | None = None,
        session: AsyncSession | None = None,
    ) -> dict[str, Any]:
        async with self._use_session(session) as s:
            max_ver = (
                await s.execute(
                    select(func.max(SelfModelVersionRow.version_number)).where(
                        SelfModelVersionRow.organism_id == organism_id
                    )
                )
            ).scalar_one_or_none()
            next_ver = int(max_ver or 0) + 1
            row = SelfModelVersionRow(
                organism_id=organism_id,
                version_number=next_ver,
                summary_text=summary_text,
                profile_snapshot=_jsonable(profile_snapshot),
                trigger_execution_id=trigger_execution_id,
                created_at=utcnow(),
            )
            s.add(row)
            await s.flush()
            return self._self_model_version_dict(row)

    async def get_self_model_version(
        self, version_id: uuid.UUID, session: AsyncSession | None = None
    ) -> dict[str, Any] | None:
        async with self._use_session(session) as s:
            row = (
                await s.execute(select(SelfModelVersionRow).where(SelfModelVersionRow.id == version_id))
            ).scalar_one_or_none()
            return self._self_model_version_dict(row) if row is not None else None

    async def get_latest_self_model_version(
        self, organism_id: uuid.UUID, session: AsyncSession | None = None
    ) -> dict[str, Any] | None:
        async with self._use_session(session) as s:
            row = (
                await s.execute(
                    select(SelfModelVersionRow)
                    .where(SelfModelVersionRow.organism_id == organism_id)
                    .order_by(SelfModelVersionRow.version_number.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            return self._self_model_version_dict(row) if row is not None else None

    async def list_self_model_versions(
        self, organism_id: uuid.UUID, limit: int = 20
    ) -> list[dict[str, Any]]:
        async with self.db.session() as s:
            rows = (
                await s.execute(
                    select(SelfModelVersionRow)
                    .where(SelfModelVersionRow.organism_id == organism_id)
                    .order_by(SelfModelVersionRow.version_number.desc())
                    .limit(limit)
                )
            ).scalars().all()
            return [self._self_model_version_dict(r) for r in rows]

    async def mark_self_model_letta_synced(
        self, version_id: uuid.UUID, synced_at: datetime | None = None
    ) -> bool:
        async with self.db.session() as s:
            res = await s.execute(
                update(SelfModelVersionRow)
                .where(SelfModelVersionRow.id == version_id)
                .values(letta_synced_at=synced_at or utcnow())
            )
            return (res.rowcount or 0) == 1

    @staticmethod
    def _skill_dict(r: SkillRegistryRow) -> dict[str, Any]:
        return {
            "id": str(r.id),
            "skill_key": r.skill_key,
            "canonical_name": r.canonical_name,
            "version": r.version,
            "parent_version": r.parent_version,
            "superseded_by": str(r.superseded_by) if r.superseded_by else None,
            "capability_key": r.capability_key,
            "source_task_id": str(r.source_task_id) if r.source_task_id else None,
            "source_execution_id": str(r.source_execution_id) if r.source_execution_id else None,
            "description": r.description,
            "applicable_scenarios": r.applicable_scenarios or [],
            "steps": r.steps or [],
            "artifact_path": r.artifact_path,
            "artifact_checksum": r.artifact_checksum,
            "test_spec": r.test_spec or {},
            "input_schema": r.input_schema or {},
            "output_schema": r.output_schema or {},
            "permission_level": r.permission_level,
            "status": r.status,
            "invocation_count": int(r.invocation_count or 0),
            "reuse_count": int(r.reuse_count or 0),
            "reuse_success_count": int(r.reuse_success_count or 0),
            "last_invoked_at": r.last_invoked_at,
            "evolution_meta": r.evolution_meta or {},
            "created_at": r.created_at,
            "updated_at": r.updated_at,
        }

    async def create_or_update_skill(
        self,
        *,
        skill_key: str,
        version: str,
        capability_key: str,
        description: str = "",
        applicable_scenarios: list[str] | None = None,
        steps: list[str] | None = None,
        artifact_path: str | None = None,
        artifact_checksum: str | None = None,
        test_spec: dict[str, Any] | None = None,
        permission_level: str = "L1",
        status: str = "DRAFT",
        source_task_id: uuid.UUID | None = None,
        source_execution_id: uuid.UUID | None = None,
        canonical_name: str | None = None,
        parent_version: str | None = None,
        superseded_by: uuid.UUID | None = None,
        input_schema: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
        evolution_meta: dict[str, Any] | None = None,
        session: AsyncSession | None = None,
    ) -> dict[str, Any]:
        now = utcnow()
        async with self._use_session(session) as s:
            row = (
                await s.execute(
                    select(SkillRegistryRow).where(
                        SkillRegistryRow.skill_key == skill_key,
                        SkillRegistryRow.version == version,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = SkillRegistryRow(
                    skill_key=skill_key,
                    version=version,
                    capability_key=capability_key,
                    source_task_id=source_task_id,
                    source_execution_id=source_execution_id,
                    description=description,
                    applicable_scenarios=_jsonable(applicable_scenarios or []),
                    steps=_jsonable(steps or []),
                    artifact_path=artifact_path,
                    artifact_checksum=artifact_checksum,
                    test_spec=_jsonable(test_spec or {}),
                    permission_level=permission_level,
                    status=status,
                    canonical_name=canonical_name,
                    parent_version=parent_version,
                    superseded_by=superseded_by,
                    input_schema=_jsonable(input_schema or {}),
                    output_schema=_jsonable(output_schema or {}),
                    evolution_meta=_jsonable(evolution_meta or {}),
                    created_at=now,
                    updated_at=now,
                )
                s.add(row)
            else:
                row.capability_key = capability_key
                row.description = description
                row.applicable_scenarios = _jsonable(applicable_scenarios or [])
                row.steps = _jsonable(steps or [])
                row.artifact_path = artifact_path
                row.artifact_checksum = artifact_checksum
                row.test_spec = _jsonable(test_spec or {})
                row.permission_level = permission_level
                row.status = status
                if canonical_name is not None:
                    row.canonical_name = canonical_name
                if parent_version is not None:
                    row.parent_version = parent_version
                if superseded_by is not None:
                    row.superseded_by = superseded_by
                if input_schema is not None:
                    row.input_schema = _jsonable(input_schema)
                if output_schema is not None:
                    row.output_schema = _jsonable(output_schema)
                if evolution_meta is not None:
                    row.evolution_meta = _jsonable(evolution_meta)
                if source_task_id is not None:
                    row.source_task_id = source_task_id
                if source_execution_id is not None:
                    row.source_execution_id = source_execution_id
                row.updated_at = now
            await s.flush()
            return self._skill_dict(row)

    async def get_skill(
        self, skill_id: uuid.UUID, session: AsyncSession | None = None
    ) -> dict[str, Any] | None:
        async with self._use_session(session) as s:
            row = (
                await s.execute(select(SkillRegistryRow).where(SkillRegistryRow.id == skill_id))
            ).scalar_one_or_none()
            return self._skill_dict(row) if row is not None else None

    async def get_skill_by_name(
        self,
        name: str,
        *,
        version: str | None = None,
        validated_only: bool = False,
        session: AsyncSession | None = None,
    ) -> dict[str, Any] | None:
        key = str(name or "").strip()
        if not key:
            return None
        async with self._use_session(session) as s:
            q = select(SkillRegistryRow).where(
                or_(SkillRegistryRow.canonical_name == key, SkillRegistryRow.skill_key == key)
            )
            if version:
                q = q.where(SkillRegistryRow.version == version)
            if validated_only:
                q = q.where(SkillRegistryRow.status == "VALIDATED")
            rows = (await s.execute(q.order_by(SkillRegistryRow.updated_at.desc()).limit(10))).scalars().all()
            if not rows:
                return None
            # Prefer VALIDATED when validated_only is False
            rows.sort(key=lambda r: (1 if r.status == "VALIDATED" else 0, r.updated_at or utcnow()), reverse=True)
            return self._skill_dict(rows[0])

    async def record_skill_invocation(
        self,
        *,
        skill_id: uuid.UUID,
        skill_version: str,
        invocation_source: str = "skill_first",
        task_id: uuid.UUID | None = None,
        cycle_id: uuid.UUID | None = None,
        passed: bool = False,
        latency_ms: float = 0.0,
        estimated_saved_tokens: int = 0,
        input_digest: str = "",
        output_excerpt: str = "",
        error: str | None = None,
        session: AsyncSession | None = None,
    ) -> dict[str, Any]:
        """Record a real SkillRuntime/MCP invocation and deterministically compute cross-task reuse."""
        now = utcnow()
        async with self._use_session(session) as s:
            skill_row = (
                await s.execute(select(SkillRegistryRow).where(SkillRegistryRow.id == skill_id))
            ).scalar_one_or_none()
            if skill_row is None:
                raise ValueError(f"Skill {skill_id} not found")

            # Strict cross-task reuse rule:
            # 1. Same task as originating source_task_id is NEVER cross-task reuse.
            # 2. Different task from originating source_task_id IS cross-task reuse.
            # 3. Otherwise, check prior PASSED invocations on a different task_id or cycle_id.
            is_reuse = False
            if task_id is not None and skill_row.source_task_id is not None:
                is_reuse = task_id != skill_row.source_task_id
            else:
                prior_rows = (
                    await s.execute(
                        select(SkillInvocationRow.task_id, SkillInvocationRow.cycle_id)
                        .where(
                            SkillInvocationRow.skill_id == skill_id,
                            SkillInvocationRow.passed.is_(True),
                        )
                        .limit(50)
                    )
                ).all()
                for prev_tid, prev_cid in prior_rows:
                    if task_id is not None and prev_tid is not None and prev_tid != task_id:
                        is_reuse = True
                        break
                    if task_id is None and cycle_id is not None and prev_cid is not None and prev_cid != cycle_id:
                        is_reuse = True
                        break
                    if task_id is not None and prev_tid is None:
                        is_reuse = True
                        break

            inv = SkillInvocationRow(
                skill_id=skill_id,
                skill_version=skill_version,
                task_id=task_id,
                cycle_id=cycle_id,
                invocation_source=invocation_source[:24],
                is_cross_task_reuse=is_reuse,
                passed=bool(passed),
                latency_ms=round(float(latency_ms or 0.0), 2),
                estimated_saved_tokens=max(0, int(estimated_saved_tokens or 0)) if passed else 0,
                input_digest=input_digest[:64],
                output_excerpt=(output_excerpt or "")[:2000],
                error=(error[:1000] if error else None),
                created_at=now,
            )
            s.add(inv)
            skill_row.invocation_count = int(skill_row.invocation_count or 0) + 1
            if is_reuse:
                skill_row.reuse_count = int(skill_row.reuse_count or 0) + 1
                if passed:
                    skill_row.reuse_success_count = int(skill_row.reuse_success_count or 0) + 1
            skill_row.last_invoked_at = now
            skill_row.updated_at = now
            await s.flush()
            return {
                "id": str(inv.id),
                "skill_id": str(inv.skill_id),
                "skill_version": inv.skill_version,
                "task_id": str(inv.task_id) if inv.task_id else None,
                "cycle_id": str(inv.cycle_id) if inv.cycle_id else None,
                "invocation_source": inv.invocation_source,
                "is_cross_task_reuse": bool(inv.is_cross_task_reuse),
                "passed": bool(inv.passed),
                "latency_ms": float(inv.latency_ms),
                "estimated_saved_tokens": int(inv.estimated_saved_tokens),
                "input_digest": inv.input_digest,
                "output_excerpt": inv.output_excerpt,
                "error": inv.error,
                "created_at": inv.created_at,
            }

    async def list_skill_invocations(
        self,
        *,
        skill_id: uuid.UUID | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        async with self.db.session() as s:
            q = select(SkillInvocationRow)
            if skill_id is not None:
                q = q.where(SkillInvocationRow.skill_id == skill_id)
            rows = (
                await s.execute(q.order_by(SkillInvocationRow.created_at.desc()).limit(limit))
            ).scalars().all()
            return [
                {
                    "id": str(r.id),
                    "skill_id": str(r.skill_id),
                    "skill_version": r.skill_version,
                    "task_id": str(r.task_id) if r.task_id else None,
                    "cycle_id": str(r.cycle_id) if r.cycle_id else None,
                    "invocation_source": r.invocation_source,
                    "is_cross_task_reuse": bool(r.is_cross_task_reuse),
                    "passed": bool(r.passed),
                    "latency_ms": float(r.latency_ms),
                    "estimated_saved_tokens": int(r.estimated_saved_tokens),
                    "input_digest": r.input_digest,
                    "output_excerpt": r.output_excerpt,
                    "error": r.error,
                    "created_at": r.created_at,
                }
                for r in rows
            ]

    async def list_skills(
        self,
        *,
        status: str | None = None,
        capability_key: str | None = None,
        limit: int = 100,
        session: AsyncSession | None = None,
    ) -> list[dict[str, Any]]:
        async with self._use_session(session) as s:
            q = select(SkillRegistryRow)
            if status:
                q = q.where(SkillRegistryRow.status == status)
            if capability_key:
                q = q.where(SkillRegistryRow.capability_key == capability_key)
            rows = (
                await s.execute(q.order_by(SkillRegistryRow.updated_at.desc()).limit(limit))
            ).scalars().all()
            return [self._skill_dict(r) for r in rows]

    async def update_skill_status(
        self, skill_id: uuid.UUID, status: str, session: AsyncSession | None = None
    ) -> bool:
        async with self._use_session(session) as s:
            res = await s.execute(
                update(SkillRegistryRow)
                .where(SkillRegistryRow.id == skill_id)
                .values(status=status, updated_at=utcnow())
            )
            return (res.rowcount or 0) == 1

    async def update_skill_metadata(
        self,
        skill_id: uuid.UUID,
        *,
        status: str | None = None,
        canonical_name: str | None = None,
        parent_version: str | None = None,
        superseded_by: uuid.UUID | None = None,
        clear_superseded_by: bool = False,
        evolution_meta: dict[str, Any] | None = None,
        session: AsyncSession | None = None,
    ) -> dict[str, Any] | None:
        async with self._use_session(session) as s:
            row = (
                await s.execute(select(SkillRegistryRow).where(SkillRegistryRow.id == skill_id))
            ).scalar_one_or_none()
            if row is None:
                return None
            if status is not None:
                row.status = status
            if canonical_name is not None:
                row.canonical_name = canonical_name
            if parent_version is not None:
                row.parent_version = parent_version
            if clear_superseded_by:
                row.superseded_by = None
            elif superseded_by is not None:
                row.superseded_by = superseded_by
            if evolution_meta is not None:
                merged = dict(row.evolution_meta or {})
                merged.update(evolution_meta)
                row.evolution_meta = _jsonable(merged)
            row.updated_at = utcnow()
            await s.flush()
            return self._skill_dict(row)

    async def list_skill_versions(
        self, name_or_key: str, *, limit: int = 30
    ) -> list[dict[str, Any]]:
        key = str(name_or_key or "").strip()
        if not key:
            return []
        async with self.db.session() as s:
            rows = (
                await s.execute(
                    select(SkillRegistryRow)
                    .where(or_(SkillRegistryRow.canonical_name == key, SkillRegistryRow.skill_key == key))
                    .order_by(SkillRegistryRow.created_at.desc())
                    .limit(limit)
                )
            ).scalars().all()
            return [self._skill_dict(r) for r in rows]

    async def record_skill_validation(
        self,
        *,
        skill_id: uuid.UUID,
        passed: bool,
        checks: list[dict[str, Any]] | None = None,
        stdout_excerpt: str = "",
        session: AsyncSession | None = None,
    ) -> dict[str, Any]:
        now = utcnow()
        async with self._use_session(session) as s:
            row = SkillValidationResultRow(
                skill_id=skill_id,
                passed=bool(passed),
                checks=_jsonable(checks or []),
                stdout_excerpt=stdout_excerpt[:4000],
                executed_at=now,
            )
            s.add(row)
            await s.flush()
            return {
                "id": str(row.id),
                "skill_id": str(row.skill_id),
                "passed": bool(row.passed),
                "checks": row.checks,
                "stdout_excerpt": row.stdout_excerpt,
                "executed_at": row.executed_at,
            }

    async def list_skill_validations(
        self, skill_id: uuid.UUID, limit: int = 20
    ) -> list[dict[str, Any]]:
        async with self.db.session() as s:
            rows = (
                await s.execute(
                    select(SkillValidationResultRow)
                    .where(SkillValidationResultRow.skill_id == skill_id)
                    .order_by(SkillValidationResultRow.executed_at.desc())
                    .limit(limit)
                )
            ).scalars().all()
            return [
                {
                    "id": str(r.id),
                    "skill_id": str(r.skill_id),
                    "passed": bool(r.passed),
                    "checks": r.checks,
                    "stdout_excerpt": r.stdout_excerpt,
                    "executed_at": r.executed_at,
                }
                for r in rows
            ]

    # ── optional affect history (P1-9 / V3.0 M2) ────────────────────────────
    async def has_affect_trigger_event(self, trigger_event_id: str) -> bool:
        if not trigger_event_id:
            return False
        async with self.db.session() as s:
            row = (
                await s.execute(
                    select(AffectHistoryRow.id)
                    .where(AffectHistoryRow.trigger_event_id == trigger_event_id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            return row is not None

    async def record_affect_history(
        self,
        *,
        organism_id: uuid.UUID,
        valence: float,
        arousal: float,
        confidence: float,
        frustration: float,
        boredom: float,
        modulation_applied: dict[str, Any] | None = None,
        trigger_reason: str = "",
        cycle_id: uuid.UUID | None = None,
        trigger_event_id: str | None = None,
        decay_applied: float | None = None,
    ) -> uuid.UUID:
        async with self.db.session() as s:
            row = AffectHistoryRow(
                organism_id=organism_id,
                cycle_id=cycle_id,
                valence=float(valence),
                arousal=float(arousal),
                confidence=float(confidence),
                frustration=float(frustration),
                boredom=float(boredom),
                modulation_applied=_jsonable(modulation_applied or {}),
                trigger_reason=trigger_reason[:1000],
                trigger_event_id=trigger_event_id[:128] if trigger_event_id else None,
                decay_applied=float(decay_applied) if decay_applied is not None else None,
                created_at=utcnow(),
            )
            s.add(row)
            await s.flush()
            return row.id

    async def list_affect_history(
        self, organism_id: uuid.UUID, limit: int = 50
    ) -> list[dict[str, Any]]:
        async with self.db.session() as s:
            rows = (
                await s.execute(
                    select(AffectHistoryRow)
                    .where(AffectHistoryRow.organism_id == organism_id)
                    .order_by(AffectHistoryRow.created_at.desc())
                    .limit(limit)
                )
            ).scalars().all()
            return [
                {
                    "id": str(r.id),
                    "organism_id": str(r.organism_id),
                    "cycle_id": str(r.cycle_id) if r.cycle_id else None,
                    "valence": float(r.valence),
                    "arousal": float(r.arousal),
                    "confidence": float(r.confidence),
                    "frustration": float(r.frustration),
                    "boredom": float(r.boredom),
                    "modulation_applied": r.modulation_applied or {},
                    "trigger_reason": r.trigger_reason,
                    "trigger_event_id": r.trigger_event_id,
                    "decay_applied": float(r.decay_applied) if r.decay_applied is not None else None,
                    "created_at": r.created_at,
                }
                for r in rows
            ]


def day_start(now: datetime | None = None) -> datetime:
    now = now or utcnow()
    return now - timedelta(hours=24)
