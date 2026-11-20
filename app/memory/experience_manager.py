"""Experience Manager: coordinates PostgreSQL memory_index + Letta long-term memory.

Guarantees (task book 8.2):
  - Memory write failures NEVER crash the autonomous loop.
  - Every memory carries source + timestamp + memory_type + importance.
  - Important experiences are prioritized and deduplicated via content_hash.
  - Identity and recent memories survive process restarts (PostgreSQL index + Letta).
  - Unsynced memories are retried automatically when Letta comes back online.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from app.core.enums import MemoryType
from app.memory.interfaces import MemoryInterface, MemoryItem, MemoryWriteResult
from app.observability.logging import get_logger
from app.storage.repository import StateRepository

logger = get_logger("aoc.memory.experience")


def compute_content_hash(memory_type: str, title: str, content: str) -> str:
    raw = f"{memory_type.strip().lower()}|{title.strip().lower()}|{content.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ExperienceManager:
    def __init__(
        self,
        repo: StateRepository,
        backend: MemoryInterface | None = None,
    ) -> None:
        self.repo = repo
        self.backend = backend
        self._available: bool = False

    @property
    def backend_available(self) -> bool:
        return self._available

    async def initialize(self, organism_id: uuid.UUID, organism_name: str, identity_summary: str) -> dict[str, Any]:
        """Restore identity from PostgreSQL + Letta (if available)."""
        restored: dict[str, Any] = {"letta_available": False, "core_blocks": {}, "indexed_count": 0}
        if self.backend is not None:
            try:
                healthy = await self.backend.health()
                if healthy:
                    agent_id = await self.backend.ensure_agent(organism_name, identity_summary)
                    if agent_id:
                        await self.repo.set_memory_agent_id(organism_id, agent_id)
                        self._available = True
                        restored["letta_available"] = True
                        restored["agent_id"] = agent_id
                        restored["core_blocks"] = await self.backend.get_core_blocks()
            except Exception as exc:
                logger.warning("memory.init_degraded", error=str(exc))
                self._available = False

        # Ensure baseline Identity Memory is indexed locally even if Letta is down
        await self.remember(
            MemoryItem(
                memory_type=MemoryType.IDENTITY.value,
                title=f"Organism Identity: {organism_name}",
                content=identity_summary,
                source="system.bootstrap",
                importance=1.0,
                tags=["identity", "bootstrap"],
            )
        )
        indexed = await self.repo.list_memory_index(limit=200)
        restored["indexed_count"] = len(indexed)
        return restored

    async def remember(
        self,
        item: MemoryItem,
        cycle_id: uuid.UUID | None = None,
    ) -> MemoryWriteResult:
        """Persist memory to PostgreSQL index first, then best-effort sync to Letta."""
        chash = compute_content_hash(item.memory_type, item.title, item.content)
        existing = await self.repo.find_memory_by_hash(chash)
        if existing is not None:
            return MemoryWriteResult(
                stored_index_id=existing["memory_id"],
                external_id=existing.get("external_id"),
                synced_to_letta=existing.get("sync_status") == "synced",
                deduplicated=True,
            )

        idx_id = await self.repo.add_memory_index(
            memory_type=item.memory_type,
            title=item.title[:500],
            summary=item.content,
            content_hash=chash,
            source=item.source,
            importance=item.normalized_importance(),
            tags=item.tags,
            cycle_id=cycle_id,
            sync_status="pending",
        )

        if self.backend is None:
            return MemoryWriteResult(stored_index_id=str(idx_id), synced_to_letta=False)

        try:
            res = await self.backend.store(item)
            if res.synced_to_letta:
                self._available = True
                await self.repo.update_memory_index(
                    idx_id,
                    external_id=res.external_id,
                    sync_status="synced",
                    sync_error=None,
                )
            else:
                await self.repo.update_memory_index(
                    idx_id,
                    sync_status="failed",
                    sync_error=res.error or "sync failed",
                )
            return MemoryWriteResult(
                stored_index_id=str(idx_id),
                external_id=res.external_id,
                synced_to_letta=res.synced_to_letta,
                error=res.error,
            )
        except Exception as exc:
            logger.warning("memory.remember_backend_error", error=str(exc))
            await self.repo.update_memory_index(idx_id, sync_status="failed", sync_error=str(exc))
            return MemoryWriteResult(stored_index_id=str(idx_id), synced_to_letta=False, error=str(exc))

    async def recall(
        self,
        query: str = "",
        memory_types: list[str] | None = None,
        limit: int = 10,
    ) -> list[MemoryItem]:
        """Retrieve relevant memories: queries Letta first, merges/falls back to PostgreSQL index."""
        results: list[MemoryItem] = []
        seen_hashes: set[str] = set()

        if self.backend is not None and self._available:
            try:
                letta_items = await self.backend.search(query=query, memory_types=memory_types, limit=limit)
                for it in letta_items:
                    h = compute_content_hash(it.memory_type, it.title, it.content)
                    if h not in seen_hashes:
                        seen_hashes.add(h)
                        results.append(it)
            except Exception as exc:
                logger.warning("memory.recall_letta_error", error=str(exc))

        # Supplement or fallback using PostgreSQL memory_index (survives restarts even if Letta is down)
        if len(results) < limit:
            rows = await self.repo.list_memory_index(limit=200)
            q_terms = [t.lower() for t in query.split() if len(t) >= 2]
            allowed_types = set(memory_types) if memory_types else None
            scored: list[tuple[float, MemoryItem]] = []
            for r in rows:
                if allowed_types and r["memory_type"] not in allowed_types:
                    continue
                item = MemoryItem(
                    memory_id=r["memory_id"],
                    external_id=r.get("external_id"),
                    memory_type=r["memory_type"],
                    title=r["title"],
                    content=r["summary"],
                    source=r["source"],
                    importance=float(r["importance"]),
                    tags=list(r.get("tags") or []),
                    timestamp=r["created_at"],
                )
                h = compute_content_hash(item.memory_type, item.title, item.content)
                if h in seen_hashes:
                    continue
                haystack = f"{item.title} {item.content} {' '.join(item.tags)}".lower()
                if q_terms:
                    hits = sum(1 for t in q_terms if t in haystack)
                    if hits == 0:
                        continue
                    score = item.importance + hits * 0.5
                else:
                    score = item.importance
                scored.append((score, item))

            scored.sort(key=lambda pair: pair[0], reverse=True)
            for _, it in scored[: max(0, limit - len(results))]:
                results.append(it)

        return results[:limit]

    async def flush_pending(self, max_items: int = 20) -> int:
        """Retry syncing previously failed/pending memories to Letta."""
        if self.backend is None:
            return 0
        if not await self.backend.health():
            self._available = False
            return 0
        self._available = True
        synced = 0
        for status in ("pending", "failed"):
            rows = await self.repo.list_memory_index(sync_status=status, limit=max_items)
            for r in rows:
                item = MemoryItem(
                    memory_id=r["memory_id"],
                    memory_type=r["memory_type"],
                    title=r["title"],
                    content=r["summary"],
                    source=r["source"],
                    importance=float(r["importance"]),
                    tags=list(r.get("tags") or []),
                    timestamp=r["created_at"],
                )
                res = await self.backend.store(item)
                if res.synced_to_letta:
                    await self.repo.update_memory_index(
                        uuid.UUID(r["memory_id"]),
                        external_id=res.external_id,
                        sync_status="synced",
                        sync_error=None,
                    )
                    synced += 1
        return synced
