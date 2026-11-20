"""Organism Scheduler: runs the AutonomousLoop continuously with sleep, wake, pause, and error recovery."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.core.config import RuntimeSettings
from app.core.enums import LifecycleState
from app.core.lifecycle import LifecycleManager
from app.core.loop import AutonomousLoop
from app.observability.logging import get_logger
from app.perception.interfaces import utcnow

logger = get_logger("aoc.scheduler")


class OrganismScheduler:
    def __init__(
        self,
        loop: AutonomousLoop,
        lifecycle: LifecycleManager,
        settings: RuntimeSettings,
    ) -> None:
        self.loop = loop
        self.lifecycle = lifecycle
        self.settings = settings
        self._task: asyncio.Task | None = None
        self._cycles_executed = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._task = asyncio.create_task(self._run_forever(), name="aoc-autonomous-scheduler")

    async def stop(self) -> None:
        await self.lifecycle.stop("scheduler stopping")
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run_forever(self) -> None:
        logger.info("scheduler.started", max_cycles=self.settings.max_cycles)
        consecutive_errors = 0

        while not self.lifecycle.is_stopped:
            if self.settings.max_cycles > 0 and self._cycles_executed >= self.settings.max_cycles:
                logger.info("scheduler.max_cycles_reached", count=self._cycles_executed)
                break

            if self.lifecycle.is_paused:
                await self.lifecycle.wait_for_wake(timeout_seconds=10.0)
                continue

            try:
                result = await self.loop.run_cycle()
                self._cycles_executed += 1
                consecutive_errors = 0
                outcome = result.get("outcome", "")

                if self.lifecycle.is_stopped or self.lifecycle.is_paused:
                    continue

                if self.lifecycle.state == LifecycleState.CRITICAL:
                    sleep_s = self.settings.critical_interval_seconds
                elif outcome == "BUDGET_EXHAUSTED" or self.lifecycle.state == LifecycleState.THROTTLED:
                    sleep_s = self._compute_throttle_sleep(result)
                    wake_at = utcnow() + timedelta(seconds=sleep_s)
                    await self.lifecycle.transition(
                        LifecycleState.THROTTLED,
                        reason=f"throttled {round(sleep_s, 1)}s awaiting quota reset",
                        next_wake_at=wake_at,
                    )
                elif outcome == "IDLE":
                    sleep_s = self.settings.idle_sleep_seconds
                    await self.lifecycle.transition(
                        LifecycleState.SLEEPING,
                        reason=f"sleeping {sleep_s}s after IDLE cycle",
                        next_wake_at=utcnow() + timedelta(seconds=sleep_s),
                    )
                else:
                    sleep_s = self.settings.cycle_interval_seconds
                    await self.lifecycle.transition(
                        LifecycleState.SLEEPING,
                        reason=f"sleeping {sleep_s}s until next cycle",
                        next_wake_at=utcnow() + timedelta(seconds=sleep_s),
                    )

                await self.lifecycle.wait_for_wake(timeout_seconds=sleep_s)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                consecutive_errors += 1
                backoff = min(300.0, 5.0 * (2 ** min(consecutive_errors, 6)))
                logger.exception("scheduler.cycle_exception", error=str(exc), backoff_seconds=backoff)
                await self.lifecycle.transition(LifecycleState.ERROR, reason=str(exc))
                await self.lifecycle.wait_for_wake(timeout_seconds=backoff)

        logger.info("scheduler.exited", cycles_executed=self._cycles_executed)

    def _compute_throttle_sleep(self, cycle_result: dict) -> float:
        max_slice = max(1.0, float(self.settings.throttled_max_sleep_seconds))
        sc = cycle_result.get("state_changes") or {}
        reset_raw = sc.get("quota_reset_at")
        if isinstance(reset_raw, str):
            try:
                reset_dt = datetime.fromisoformat(reset_raw)
                if reset_dt.tzinfo is None:
                    reset_dt = reset_dt.replace(tzinfo=timezone.utc)
                delta = (reset_dt - utcnow()).total_seconds()
                if delta > 0:
                    return min(max_slice, max(1.0, delta))
                return 1.0
            except ValueError:
                pass
        return min(max_slice, max(float(self.settings.idle_sleep_seconds), 60.0))
