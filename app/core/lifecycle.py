"""Lifecycle Manager (task book section 10).

Sole owner of organism lifecycle state transitions:
  INITIALIZING, ACTIVE, IDLE, SLEEPING, CRITICAL, PAUSED, STOPPED, ERROR.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime

from app.core.enums import LifecycleState
from app.observability.logging import get_logger
from app.observability.metrics import set_lifecycle
from app.perception.interfaces import utcnow
from app.storage.repository import StateRepository

logger = get_logger("aoc.lifecycle")

ALL_STATES = [s.value for s in LifecycleState]

ALLOWED_TRANSITIONS: dict[LifecycleState, set[LifecycleState]] = {
    LifecycleState.INITIALIZING: {
        LifecycleState.ACTIVE,
        LifecycleState.IDLE,
        LifecycleState.THROTTLED,
        LifecycleState.CRITICAL,
        LifecycleState.PAUSED,
        LifecycleState.ERROR,
        LifecycleState.STOPPED,
    },
    LifecycleState.ACTIVE: {
        LifecycleState.ACTIVE,
        LifecycleState.IDLE,
        LifecycleState.SLEEPING,
        LifecycleState.THROTTLED,
        LifecycleState.CRITICAL,
        LifecycleState.PAUSED,
        LifecycleState.ERROR,
        LifecycleState.STOPPED,
    },
    LifecycleState.IDLE: {
        LifecycleState.ACTIVE,
        LifecycleState.SLEEPING,
        LifecycleState.THROTTLED,
        LifecycleState.CRITICAL,
        LifecycleState.PAUSED,
        LifecycleState.ERROR,
        LifecycleState.STOPPED,
    },
    LifecycleState.SLEEPING: {
        LifecycleState.ACTIVE,
        LifecycleState.IDLE,
        LifecycleState.THROTTLED,
        LifecycleState.CRITICAL,
        LifecycleState.PAUSED,
        LifecycleState.ERROR,
        LifecycleState.STOPPED,
    },
    LifecycleState.THROTTLED: {
        LifecycleState.ACTIVE,
        LifecycleState.IDLE,
        LifecycleState.SLEEPING,
        LifecycleState.THROTTLED,
        LifecycleState.CRITICAL,
        LifecycleState.PAUSED,
        LifecycleState.ERROR,
        LifecycleState.STOPPED,
    },
    LifecycleState.CRITICAL: {
        LifecycleState.ACTIVE,
        LifecycleState.IDLE,
        LifecycleState.SLEEPING,
        LifecycleState.THROTTLED,
        LifecycleState.CRITICAL,
        LifecycleState.PAUSED,
        LifecycleState.ERROR,
        LifecycleState.STOPPED,
    },
    LifecycleState.PAUSED: {
        LifecycleState.ACTIVE,
        LifecycleState.IDLE,
        LifecycleState.SLEEPING,
        LifecycleState.THROTTLED,
        LifecycleState.STOPPED,
    },
    LifecycleState.ERROR: {
        LifecycleState.ACTIVE,
        LifecycleState.IDLE,
        LifecycleState.SLEEPING,
        LifecycleState.THROTTLED,
        LifecycleState.CRITICAL,
        LifecycleState.PAUSED,
        LifecycleState.STOPPED,
    },
    LifecycleState.STOPPED: {LifecycleState.INITIALIZING},
}


class LifecycleManager:
    def __init__(self, organism_id: uuid.UUID, repo: StateRepository) -> None:
        self.organism_id = organism_id
        self.repo = repo
        self._state = LifecycleState.INITIALIZING
        self._wake_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._pause_requested = False

    @property
    def state(self) -> LifecycleState:
        return self._state

    @property
    def is_paused(self) -> bool:
        return self._pause_requested or self._state == LifecycleState.PAUSED

    @property
    def is_stopped(self) -> bool:
        return self._stop_event.is_set() or self._state == LifecycleState.STOPPED

    async def sync_from_db(self) -> LifecycleState:
        st = await self.repo.get_organism_state(self.organism_id)
        if st:
            try:
                self._state = LifecycleState(st["lifecycle_state"])
            except ValueError:
                self._state = LifecycleState.INITIALIZING
            self._pause_requested = bool(st.get("pause_requested", False))
        set_lifecycle(self._state.value, ALL_STATES)
        return self._state

    async def transition(
        self,
        target: LifecycleState,
        reason: str = "",
        next_wake_at: datetime | None = None,
        cycle_id: uuid.UUID | None = None,
    ) -> LifecycleState:
        prev = self._state
        if target != prev and target not in ALLOWED_TRANSITIONS.get(prev, set()):
            logger.warning("lifecycle.invalid_transition", from_state=prev.value, to_state=target.value, reason=reason)
            # Still allow STOP/ERROR unconditionally for safety
            if target not in {LifecycleState.STOPPED, LifecycleState.ERROR, LifecycleState.PAUSED}:
                return prev

        self._state = target
        set_lifecycle(target.value, ALL_STATES)
        updates: dict = {
            "lifecycle_state": target.value,
            "previous_state": prev.value,
            "state_reason": reason,
            "heartbeat_at": utcnow(),
        }
        if next_wake_at is not None:
            updates["next_wake_at"] = next_wake_at
        await self.repo.update_organism_state(self.organism_id, **updates)
        if target != prev:
            await self.repo.log_event(
                "lifecycle.transition",
                message=f"{prev.value} -> {target.value}" + (f": {reason}" if reason else ""),
                payload={"from": prev.value, "to": target.value, "reason": reason},
                cycle_id=cycle_id,
            )
            logger.info("lifecycle.transition", from_state=prev.value, to_state=target.value, reason=reason)
        return target

    async def pause(self, reason: str = "operator requested pause") -> LifecycleState:
        self._pause_requested = True
        await self.repo.update_organism_state(self.organism_id, pause_requested=True)
        return await self.transition(LifecycleState.PAUSED, reason=reason)

    async def resume(self, reason: str = "operator requested resume") -> LifecycleState:
        self._pause_requested = False
        await self.repo.update_organism_state(self.organism_id, pause_requested=False)
        st = await self.transition(LifecycleState.ACTIVE, reason=reason)
        self._wake_event.set()
        return st

    def wake(self, reason: str = "external event") -> None:
        logger.info("lifecycle.wake_signal", reason=reason)
        self._wake_event.set()

    async def stop(self, reason: str = "shutdown") -> LifecycleState:
        self._stop_event.set()
        self._wake_event.set()
        return await self.transition(LifecycleState.STOPPED, reason=reason)

    async def wait_for_wake(self, timeout_seconds: float) -> bool:
        """Sleep until wake_event is triggered or timeout expires. Returns True if woken early."""
        self._wake_event.clear()
        if self._stop_event.is_set():
            return True
        try:
            await asyncio.wait_for(self._wake_event.wait(), timeout=max(0.05, timeout_seconds))
            return True
        except asyncio.TimeoutError:
            return False
