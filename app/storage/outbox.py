"""PostgreSQL Transactional Outbox / Inbox Dispatcher (V2.0 P1-2).

Delivers strictly bounded cross-boundary events without introducing Redis/NATS/Kafka:
  - execution.finalize
  - memory.embed
  - memory.sync_letta
  - notification.dispatch

Guarantees:
  - PostgreSQL: atomic claim via `SELECT ... FOR UPDATE SKIP LOCKED`
  - SQLite (unit tests): process-local asyncio.Lock fallback
  - Consumer-side idempotency via `inbox_events(consumer_name, idempotency_key)`
  - Exponential backoff on failure + `dead_letter` transition + manual replay
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any, Awaitable, Callable

from sqlalchemy import select, update

from app.observability.logging import get_logger
from app.perception.interfaces import utcnow
from app.storage.models import OutboxEvent
from app.storage.repository import StateRepository

logger = get_logger("aoc.outbox")


class OutboxEventType:
    EXECUTION_FINALIZE = "execution.finalize"
    MEMORY_EMBED = "memory.embed"
    MEMORY_SYNC_LETTA = "memory.sync_letta"
    NOTIFICATION_DISPATCH = "notification.dispatch"

    ALLOWED = {
        EXECUTION_FINALIZE,
        MEMORY_EMBED,
        MEMORY_SYNC_LETTA,
        NOTIFICATION_DISPATCH,
    }


OutboxHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]


class OutboxDispatcher:
    def __init__(
        self,
        repo: StateRepository,
        *,
        poll_interval: float = 2.0,
        batch_size: int = 20,
        base_backoff_seconds: float = 10.0,
        max_backoff_seconds: float = 3600.0,
    ) -> None:
        self.repo = repo
        self.poll_interval = max(0.05, float(poll_interval))
        self.batch_size = max(1, int(batch_size))
        self.base_backoff_seconds = max(0.01, float(base_backoff_seconds))
        self.max_backoff_seconds = max(self.base_backoff_seconds, float(max_backoff_seconds))
        self._handlers: dict[str, tuple[str, OutboxHandler]] = {}
        self._sqlite_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    def register_handler(self, event_type: str, consumer_name: str, handler: OutboxHandler) -> None:
        self._handlers[event_type] = (consumer_name, handler)

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name="aoc-outbox-dispatcher")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.dispatch_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("outbox.loop_error", error=str(exc))
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass

    async def claim_batch(self, batch_size: int | None = None) -> list[dict[str, Any]]:
        """Claim due pending outbox events atomically (SKIP LOCKED on PostgreSQL)."""
        limit = batch_size or self.batch_size
        now = utcnow()

        async def _claim() -> list[dict[str, Any]]:
            async with self.repo.transaction() as s:
                dialect = s.bind.dialect.name if s.bind is not None else "sqlite"
                q = (
                    select(OutboxEvent)
                    .where(OutboxEvent.status == "pending", OutboxEvent.next_retry_at <= now)
                    .order_by(OutboxEvent.next_retry_at.asc(), OutboxEvent.created_at.asc())
                    .limit(limit)
                )
                if dialect == "postgresql":
                    q = q.with_for_update(skip_locked=True)
                rows = (await s.execute(q)).scalars().all()
                claimed: list[dict[str, Any]] = []
                for r in rows:
                    r.status = "processing"
                    r.locked_at = now
                    claimed.append(
                        {
                            "id": r.id,
                            "event_type": r.event_type,
                            "aggregate_type": r.aggregate_type,
                            "aggregate_id": r.aggregate_id,
                            "idempotency_key": r.idempotency_key,
                            "payload": r.payload,
                            "attempts": r.attempts,
                            "max_attempts": r.max_attempts,
                        }
                    )
                return claimed

        if self.repo.db.url.startswith("postgresql"):
            return await _claim()
        async with self._sqlite_lock:
            return await _claim()

    async def dispatch_once(self, batch_size: int | None = None) -> dict[str, int]:
        """Claim and dispatch one batch of due outbox events."""
        stats = {"claimed": 0, "dispatched": 0, "deduplicated": 0, "retried": 0, "dead_letter": 0, "unhandled": 0}
        events = await self.claim_batch(batch_size=batch_size)
        stats["claimed"] = len(events)

        for ev in events:
            entry = self._handlers.get(ev["event_type"])
            if entry is None:
                # No handler registered yet (e.g. memory.embed before P1-4): release cleanly back to pending
                await self._release_unhandled(ev["id"])
                stats["unhandled"] += 1
                continue

            consumer_name, handler = entry
            idem_key = ev["idempotency_key"]

            # Inbox idempotency check BEFORE executing handler
            if await self.repo.has_inbox_event(consumer_name, idem_key):
                await self._mark_dispatched(ev["id"], consumer_name=None, idem_key=None, event_type=ev["event_type"], summary=None)
                stats["deduplicated"] += 1
                continue

            try:
                result = await handler(ev)
                await self._mark_dispatched(
                    ev["id"],
                    consumer_name=consumer_name,
                    idem_key=idem_key,
                    event_type=ev["event_type"],
                    summary=result or {"ok": True},
                )
                stats["dispatched"] += 1
            except Exception as exc:
                err_text = str(exc)
                attempts = int(ev["attempts"]) + 1
                max_attempts = int(ev["max_attempts"])
                now = utcnow()
                if attempts >= max_attempts:
                    async with self.repo.transaction() as s:
                        await s.execute(
                            update(OutboxEvent)
                            .where(OutboxEvent.id == ev["id"])
                            .values(
                                status="dead_letter",
                                attempts=attempts,
                                locked_at=None,
                                last_error=err_text[:1000],
                            )
                        )
                    await self.repo.log_event(
                        "outbox.dead_letter",
                        message=f"Outbox event {ev['event_type']} ({idem_key}) exhausted {attempts} attempts: {err_text}",
                        payload={"event_id": str(ev["id"]), "event_type": ev["event_type"], "idempotency_key": idem_key},
                        level="error",
                    )
                    stats["dead_letter"] += 1
                else:
                    delay = min(self.max_backoff_seconds, self.base_backoff_seconds * (2 ** min(attempts, 10)))
                    async with self.repo.transaction() as s:
                        await s.execute(
                            update(OutboxEvent)
                            .where(OutboxEvent.id == ev["id"])
                            .values(
                                status="pending",
                                attempts=attempts,
                                next_retry_at=now + timedelta(seconds=delay),
                                locked_at=None,
                                last_error=err_text[:1000],
                            )
                        )
                    stats["retried"] += 1
                    logger.warning(
                        "outbox.retry_scheduled",
                        event_type=ev["event_type"],
                        idempotency_key=idem_key,
                        attempt=attempts,
                        delay_seconds=round(delay, 2),
                        error=err_text[:200],
                    )
        return stats

    async def _mark_dispatched(
        self,
        event_id: uuid.UUID,
        *,
        consumer_name: str | None,
        idem_key: str | None,
        event_type: str,
        summary: dict[str, Any] | None,
    ) -> None:
        now = utcnow()
        async with self.repo.transaction() as s:
            if consumer_name and idem_key:
                await self.repo.mark_inbox_processed(
                    consumer_name=consumer_name,
                    idempotency_key=idem_key,
                    event_type=event_type,
                    result_summary=summary,
                    session=s,
                )
            await s.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id == event_id)
                .values(status="dispatched", dispatched_at=now, locked_at=None, last_error=None)
            )

    async def _release_unhandled(self, event_id: uuid.UUID) -> None:
        async with self.repo.transaction() as s:
            await s.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id == event_id)
                .values(
                    status="pending",
                    locked_at=None,
                    next_retry_at=utcnow() + timedelta(seconds=max(self.poll_interval, 5.0)),
                )
            )
