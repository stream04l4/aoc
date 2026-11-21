"""Experience Manager: coordinates PostgreSQL memory_index, Hybrid Semantic Retrieval, and Async Letta Sync.

Guarantees (task book 8.2 & V2.0 P1-4 5.4):
  - Memory write failures NEVER crash the autonomous loop.
  - Every memory carries source + timestamp + memory_type + importance + reliability.
  - Deduplicated via content_hash; supports Outbox-driven asynchronous Letta sync & embedding.
  - Hybrid recall combines pgvector semantic similarity + pg_trgm bilingual text + recency + reliability + importance,
    degrading gracefully when Embedding or Letta services are unavailable.
  - Preserves V1.0 flush_pending() and recall() signatures for 100% backward compatibility.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from app.core.enums import MemoryType
from app.memory.embedding import EmbeddingService
from app.memory.interfaces import MemoryInterface, MemoryItem, MemoryWriteResult
from app.memory.retrieval import HybridRetrievalEngine, ScoredMemory
from app.observability.logging import get_logger
from app.storage.outbox import OutboxEventType
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
        *,
        embedding_service: EmbeddingService | None = None,
        retrieval_engine: HybridRetrievalEngine | None = None,
        outbox_enabled: bool = False,
        embed_on_write: bool = True,
    ) -> None:
        self.repo = repo
        self.backend = backend
        self.embedding_service = embedding_service
        self.retrieval_engine = retrieval_engine
        self.outbox_enabled = outbox_enabled
        self.embed_on_write = embed_on_write
        self._available: bool = False
        self._organism_name: str = "AOC-001"
        self._identity_summary: str = ""
        self._organism_id: uuid.UUID | None = None

    @property
    def backend_available(self) -> bool:
        return self._available

    async def initialize(self, organism_id: uuid.UUID, organism_name: str, identity_summary: str) -> dict[str, Any]:
        """Restore identity from PostgreSQL + Letta (if available)."""
        self._organism_id = organism_id
        self._organism_name = organism_name
        self._identity_summary = identity_summary
        restored: dict[str, Any] = {"letta_available": False, "core_blocks": {}, "indexed_count": 0}
        if self.embedding_service is not None:
            try:
                await self.embedding_service.ensure_registered()
            except Exception as exc:
                logger.warning("memory.embedding_register_failed", error=str(exc))

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
        *,
        reliability: float = 0.8,
        information_gain: float | None = None,
    ) -> MemoryWriteResult:
        """Persist memory to PostgreSQL index first; sync to Letta & embed via Outbox (V2.0) or sync fallback (V1.0)."""
        chash = compute_content_hash(item.memory_type, item.title, item.content)
        existing = await self.repo.find_memory_by_hash(chash)
        if existing is not None:
            return MemoryWriteResult(
                stored_index_id=existing["memory_id"],
                external_id=existing.get("external_id"),
                synced_to_letta=existing.get("sync_status") == "synced",
                deduplicated=True,
            )

        if self.outbox_enabled:
            async with self.repo.transaction() as s:
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
                    embedding_status="pending",
                    reliability=reliability,
                    information_gain=information_gain,
                    session=s,
                )
                mid = str(idx_id)
                if self.embed_on_write and self.embedding_service is not None:
                    await self.repo.enqueue_outbox_event(
                        event_type=OutboxEventType.MEMORY_EMBED,
                        idempotency_key=f"embed:{mid}",
                        payload={"memory_id": mid},
                        aggregate_type="memory",
                        aggregate_id=idx_id,
                        session=s,
                    )
                if self.backend is not None:
                    await self.repo.enqueue_outbox_event(
                        event_type=OutboxEventType.MEMORY_SYNC_LETTA,
                        idempotency_key=f"sync_letta:{mid}",
                        payload={"memory_id": mid, "memory_type": item.memory_type},
                        aggregate_type="memory",
                        aggregate_id=idx_id,
                        session=s,
                    )
            return MemoryWriteResult(stored_index_id=str(idx_id), synced_to_letta=False)

        # V1.0 synchronous best-effort mode (used by direct ExperienceManager unit tests without OutboxDispatcher)
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
            embedding_status="pending",
            reliability=reliability,
            information_gain=information_gain,
        )

        if self.embed_on_write and self.embedding_service is not None:
            try:
                await self.embedding_service.embed_memory_record(idx_id)
            except Exception as exc:
                logger.warning("memory.inline_embed_degraded", memory_id=str(idx_id), error=str(exc))

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

    async def sync_memory_to_letta(self, memory_id: uuid.UUID) -> dict[str, Any]:
        """Idempotent Outbox consumer for ``memory.sync_letta`` (V2.0 P1-4).

        Raises RuntimeError on transient Letta outages so OutboxDispatcher applies exponential backoff.
        """
        row = await self.repo.get_memory_by_id(memory_id)
        if row is None:
            return {"memory_id": str(memory_id), "status": "not_found"}
        if row.get("sync_status") == "synced":
            return {"memory_id": str(memory_id), "status": "already_synced", "external_id": row.get("external_id")}
        if self.backend is None:
            return {"memory_id": str(memory_id), "status": "no_backend"}

        if not await self.backend.health():
            self._available = False
            await self.repo.update_memory_index(memory_id, sync_status="failed", sync_error="Letta backend unhealthy")
            raise RuntimeError("Letta backend unavailable; deferring via Outbox backoff")

        # Ensure Letta agent_id is initialized if Letta came up after AOC startup
        if hasattr(self.backend, "agent_id") and not getattr(self.backend, "agent_id"):
            aid = await self.backend.ensure_agent(
                self._organism_name, self._identity_summary or f"Organism {self._organism_name}"
            )
            if aid and self._organism_id is not None:
                await self.repo.set_memory_agent_id(self._organism_id, aid)

        item = MemoryItem(
            memory_id=row["memory_id"],
            memory_type=row["memory_type"],
            title=row["title"],
            content=row["summary"],
            source=row["source"],
            importance=float(row["importance"]),
            tags=list(row.get("tags") or []),
            timestamp=row["created_at"],
        )
        res = await self.backend.store(item)
        if not res.synced_to_letta:
            await self.repo.update_memory_index(
                memory_id, sync_status="failed", sync_error=res.error or "Letta store failed"
            )
            raise RuntimeError(res.error or "Letta store failed")

        self._available = True
        await self.repo.update_memory_index(
            memory_id,
            external_id=res.external_id,
            sync_status="synced",
            sync_error=None,
        )
        return {"memory_id": str(memory_id), "status": "synced", "external_id": res.external_id}

    async def search_hybrid(
        self,
        *,
        query: str = "",
        memory_types: list[str] | None = None,
        limit: int = 10,
    ) -> list[ScoredMemory]:
        """Execute hybrid semantic + trigram + recency + reliability + importance search."""
        if self.retrieval_engine is not None:
            return await self.retrieval_engine.search(query=query, memory_types=memory_types, limit=limit)
        items = await self.recall(query=query, memory_types=memory_types, limit=limit)
        return [
            ScoredMemory(
                item=it,
                total_score=round(it.importance, 4),
                importance_score=round(it.importance, 4),
                degraded_no_vector=True,
            )
            for it in items
        ]

    async def recall(
        self,
        query: str = "",
        memory_types: list[str] | None = None,
        limit: int = 10,
    ) -> list[MemoryItem]:
        """Retrieve relevant memories: uses HybridRetrievalEngine when configured, merged with Letta."""
        results: list[MemoryItem] = []
        seen_hashes: set[str] = set()

        # 1. Local PostgreSQL Hybrid Retrieval (pgvector + pg_trgm + recency + reliability + importance)
        if self.retrieval_engine is not None:
            try:
                scored = await self.retrieval_engine.search(query=query, memory_types=memory_types, limit=limit)
                for sc in scored:
                    h = compute_content_hash(sc.item.memory_type, sc.item.title, sc.item.content)
                    if h not in seen_hashes:
                        seen_hashes.add(h)
                        results.append(sc.item)
            except Exception as exc:
                logger.warning("memory.hybrid_recall_error", error=str(exc))

        # 2. Query Letta (if online and we still want additional recall candidates)
        if self.backend is not None and self._available and len(results) < limit:
            try:
                letta_items = await self.backend.search(query=query, memory_types=memory_types, limit=limit)
                for it in letta_items:
                    h = compute_content_hash(it.memory_type, it.title, it.content)
                    if h not in seen_hashes:
                        seen_hashes.add(h)
                        results.append(it)
            except Exception as exc:
                logger.warning("memory.recall_letta_error", error=str(exc))

        # 3. Fallback directly to PostgreSQL memory_index when no retrieval_engine is configured
        if self.retrieval_engine is None and len(results) < limit:
            rows = await self.repo.list_memory_index(limit=200)
            q_terms = [t.lower() for t in query.split() if len(t) >= 2]
            allowed_types = set(memory_types) if memory_types else None
            scored_fb: list[tuple[float, MemoryItem]] = []
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
                scored_fb.append((score, item))

            scored_fb.sort(key=lambda pair: pair[0], reverse=True)
            for _, it in scored_fb[: max(0, limit - len(results))]:
                results.append(it)

        return results[:limit]

    async def flush_pending(self, max_items: int = 20) -> int:
        """Retry syncing previously failed/pending memories to Letta (V1.0 compatible wrapper)."""
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
                try:
                    res = await self.sync_memory_to_letta(uuid.UUID(r["memory_id"]))
                    if res.get("status") == "synced":
                        synced += 1
                except Exception:
                    continue
        return synced
