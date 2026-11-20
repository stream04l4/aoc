"""Durable Task State Machine (V2.0 P1-2, Task Book Section 5.2).

Enforces legal state transitions with optimistic locking (`state_version`),
idempotent replays (`idempotency_key`), immutable `task_lifecycle_events`,
and optional atomic `outbox_events` enqueue within the same DB transaction.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import TaskStatus
from app.observability.logging import get_logger
from app.perception.interfaces import utcnow
from app.storage.models import AgentTaskRow, TaskLifecycleEvent
from app.storage.repository import StateRepository

logger = get_logger("aoc.state_machine")


class InvalidStateTransitionError(RuntimeError):
    """Raised when an illegal task state transition is attempted."""


class StateVersionConflictError(RuntimeError):
    """Raised when optimistic locking detects a concurrent state_version update."""


class TransitionResult(BaseModel):
    task_id: str
    from_status: str | None
    to_status: str
    state_version: int
    noop: bool = False


ALLOWED_TASK_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.CREATED: {
        TaskStatus.VALIDATING,
        TaskStatus.QUEUED,
        TaskStatus.PENDING,
        TaskStatus.AWAITING_APPROVAL,
        TaskStatus.REQUIRES_APPROVAL,
        TaskStatus.BUDGET_DEFERRED,
        TaskStatus.REJECTED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.PENDING: {
        TaskStatus.VALIDATING,
        TaskStatus.QUEUED,
        TaskStatus.DISPATCHING,
        TaskStatus.DISPATCHED,
        TaskStatus.RUNNING,
        TaskStatus.AWAITING_APPROVAL,
        TaskStatus.REQUIRES_APPROVAL,
        TaskStatus.REJECTED,
        TaskStatus.CANCELLED,
        TaskStatus.FAILED,
    },
    TaskStatus.VALIDATING: {
        TaskStatus.REJECTED,
        TaskStatus.BUDGET_DEFERRED,
        TaskStatus.AWAITING_APPROVAL,
        TaskStatus.REQUIRES_APPROVAL,
        TaskStatus.QUEUED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.AWAITING_APPROVAL: {
        TaskStatus.QUEUED,
        TaskStatus.DISPATCHING,
        TaskStatus.DISPATCHED,
        TaskStatus.REJECTED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.REQUIRES_APPROVAL: {
        TaskStatus.AWAITING_APPROVAL,
        TaskStatus.QUEUED,
        TaskStatus.DISPATCHING,
        TaskStatus.DISPATCHED,
        TaskStatus.REJECTED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.QUEUED: {
        TaskStatus.DISPATCHING,
        TaskStatus.DISPATCHED,
        TaskStatus.RUNNING,
        TaskStatus.CANCELLED,
        TaskStatus.FAILED,
        TaskStatus.BUDGET_DEFERRED,
    },
    TaskStatus.DISPATCHING: {
        TaskStatus.RUNNING,
        TaskStatus.RECOVERY_REQUIRED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.DISPATCHED: {
        TaskStatus.RUNNING,
        TaskStatus.RECOVERY_REQUIRED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.RUNNING: {
        TaskStatus.RESULT_RECEIVED,
        TaskStatus.VERIFYING,
        TaskStatus.TIMEOUT,
        TaskStatus.CANCELLED,
        TaskStatus.ABANDONED,
        TaskStatus.ORPHANED,
        TaskStatus.RECOVERY_REQUIRED,
        TaskStatus.FAILED,
    },
    TaskStatus.RESULT_RECEIVED: {
        TaskStatus.VERIFYING,
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.RETRY_WAIT,
    },
    TaskStatus.VERIFYING: {
        TaskStatus.SUCCEEDED,
        TaskStatus.RETRY_WAIT,
        TaskStatus.FAILED,
    },
    TaskStatus.TIMEOUT: {
        TaskStatus.RETRY_WAIT,
        TaskStatus.FAILED,
        TaskStatus.RECOVERY_REQUIRED,
    },
    TaskStatus.ORPHANED: {
        TaskStatus.RECOVERY_REQUIRED,
        TaskStatus.FAILED,
    },
    TaskStatus.RETRY_WAIT: {
        TaskStatus.QUEUED,
        TaskStatus.DISPATCHING,
        TaskStatus.CANCELLED,
        TaskStatus.FAILED,
    },
    TaskStatus.RECOVERY_REQUIRED: {
        TaskStatus.RUNNING,
        TaskStatus.RESULT_RECEIVED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    # Terminal states have NO outgoing transitions (illegal transitions strictly rejected!)
    TaskStatus.SUCCEEDED: set(),
    TaskStatus.FAILED: set(),
    TaskStatus.REJECTED: set(),
    TaskStatus.ABANDONED: set(),
    TaskStatus.CANCELLED: set(),
    TaskStatus.BUDGET_DEFERRED: set(),
}


class TaskStateMachine:
    def __init__(self, repo: StateRepository) -> None:
        self.repo = repo

    async def transition(
        self,
        task_id: uuid.UUID,
        to_status: TaskStatus | str,
        *,
        expected_version: int | None = None,
        execution_id: uuid.UUID | None = None,
        actor: str = "orchestrator",
        reason: str = "",
        idempotency_key: str | None = None,
        payload: dict[str, Any] | None = None,
        task_updates: dict[str, Any] | None = None,
        outbox_events: list[dict[str, Any]] | None = None,
        session: AsyncSession | None = None,
    ) -> TransitionResult:
        target = TaskStatus(to_status) if isinstance(to_status, str) else to_status

        async def _apply(s: AsyncSession) -> TransitionResult:
            # 1. Idempotent replay check on idempotency_key
            if idempotency_key:
                prev_ev = (
                    await s.execute(
                        select(TaskLifecycleEvent).where(TaskLifecycleEvent.idempotency_key == idempotency_key)
                    )
                ).scalar_one_or_none()
                if prev_ev is not None:
                    return TransitionResult(
                        task_id=str(task_id),
                        from_status=prev_ev.from_status,
                        to_status=prev_ev.to_status,
                        state_version=prev_ev.state_version,
                        noop=True,
                    )

            row = await s.get(AgentTaskRow, task_id)
            if row is None:
                raise InvalidStateTransitionError(f"Task {task_id} not found")

            current = TaskStatus(row.status)
            cur_ver = int(row.state_version or 1)

            if expected_version is not None and cur_ver != expected_version:
                raise StateVersionConflictError(
                    f"Task {task_id} version mismatch: expected {expected_version}, actual {cur_ver}"
                )

            # Idempotent same-state transition with matching key or explicit same state
            if current == target and idempotency_key:
                return TransitionResult(
                    task_id=str(task_id),
                    from_status=current.value,
                    to_status=target.value,
                    state_version=cur_ver,
                    noop=True,
                )

            allowed = ALLOWED_TASK_TRANSITIONS.get(current, set())
            if target not in allowed:
                raise InvalidStateTransitionError(
                    f"Illegal task state transition for {task_id}: {current.value} -> {target.value}"
                )

            next_ver = cur_ver + 1
            updates: dict[str, Any] = {
                "status": target.value,
                "state_version": next_ver,
                "updated_at": utcnow(),
            }
            if task_updates:
                for k, v in task_updates.items():
                    if k not in {"status", "state_version"}:
                        updates[k] = v

            res = await s.execute(
                update(AgentTaskRow)
                .where(AgentTaskRow.id == task_id, AgentTaskRow.state_version == cur_ver)
                .values(**updates)
            )
            if (res.rowcount or 0) != 1:
                raise StateVersionConflictError(
                    f"Concurrent state_version modification on task {task_id} (expected {cur_ver})"
                )

            s.add(
                TaskLifecycleEvent(
                    task_id=task_id,
                    execution_id=execution_id,
                    state_version=next_ver,
                    from_status=current.value,
                    to_status=target.value,
                    actor=actor,
                    reason=reason,
                    idempotency_key=idempotency_key,
                    payload=payload or {},
                    created_at=utcnow(),
                )
            )

            for ob in outbox_events or []:
                await self.repo.enqueue_outbox_event(
                    event_type=ob["event_type"],
                    idempotency_key=ob["idempotency_key"],
                    payload=ob["payload"],
                    aggregate_type=ob.get("aggregate_type", "task"),
                    aggregate_id=ob.get("aggregate_id", task_id),
                    max_attempts=int(ob.get("max_attempts", 5)),
                    session=s,
                )

            return TransitionResult(
                task_id=str(task_id),
                from_status=current.value,
                to_status=target.value,
                state_version=next_ver,
                noop=False,
            )

        try:
            if session is not None:
                return await _apply(session)
            async with self.repo.transaction() as s:
                return await _apply(s)
        except InvalidStateTransitionError as exc:
            # Audit illegal transition attempts without altering DB state
            await self.repo.log_event(
                "task.invalid_transition",
                message=str(exc),
                payload={"task_id": str(task_id), "target": target.value, "actor": actor},
                level="error",
            )
            logger.error("state_machine.invalid_transition", task_id=str(task_id), target=target.value, error=str(exc))
            raise
