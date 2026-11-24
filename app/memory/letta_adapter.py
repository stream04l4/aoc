"""Letta Adapter: connects AOC to a Letta server over its REST API.

Handles Letta API differences gracefully across v0.6–v0.16+:
  - Health probe: GET /v1/health/ (fallback /health)
  - Agent discovery/creation: GET/POST /v1/agents/
  - Core memory blocks (Identity Memory & Self Model): GET/PATCH /v1/agents/{id}/core-memory/blocks/{label}
  - Archival passages (Episodic, Semantic, Procedural): POST/GET /v1/agents/{id}/archival-memory

Never raises fatal errors into the main loop on transient outages — failures are
returned in MemoryWriteResult or logged cleanly (task book 8.2).
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from app.core.config import MemorySettings
from app.core.enums import MemoryType
from app.memory.interfaces import MemoryInterface, MemoryItem, MemoryWriteResult
from app.observability.logging import get_logger
from app.observability.metrics import MEMORY_WRITES
from app.perception.interfaces import utcnow

logger = get_logger("aoc.memory.letta")

_META_PREFIX_RE = re.compile(r"^\[AOC_META:(\{.*?\})\]\n?", re.DOTALL)


def format_passage(item: MemoryItem) -> str:
    meta = {
        "type": item.memory_type,
        "title": item.title,
        "source": item.source,
        "importance": round(item.normalized_importance(), 3),
        "tags": item.tags,
        "at": item.timestamp.isoformat(),
    }
    return f"[AOC_META:{json.dumps(meta, ensure_ascii=False)}]\n{item.content}"


def parse_passage(text: str, external_id: str | None = None) -> MemoryItem:
    m = _META_PREFIX_RE.match(text or "")
    if m:
        try:
            meta = json.loads(m.group(1))
            body = text[m.end() :].strip()
            return MemoryItem(
                external_id=external_id,
                memory_type=str(meta.get("type", MemoryType.EPISODIC.value)),
                title=str(meta.get("title", "")),
                content=body,
                source=str(meta.get("source", "letta")),
                importance=float(meta.get("importance", 0.5)),
                tags=list(meta.get("tags") or []),
                metadata={"raw_at": meta.get("at")},
            )
        except Exception:
            pass
    return MemoryItem(
        external_id=external_id,
        memory_type=MemoryType.EPISODIC.value,
        content=(text or "").strip(),
        source="letta",
        importance=0.5,
    )


class LettaAdapter(MemoryInterface):
    def __init__(
        self,
        settings: MemorySettings,
        agent_id: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        llm_model: str | None = None,
        llm_endpoint: str | None = None,
    ) -> None:
        self.settings = settings
        self.base_url = settings.letta_base_url.rstrip("/")
        self.agent_id = agent_id
        self._http_client = http_client
        self.llm_model = llm_model
        self.llm_endpoint = llm_endpoint

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.letta_token:
            tok = self.settings.letta_token.get_secret_value()
            if tok:
                headers["Authorization"] = f"Bearer {tok}"
        return headers

    async def _req(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = f"{self.base_url}{path}"
        timeout = kwargs.pop("timeout", self.settings.timeout_seconds)
        if self._http_client is not None:
            return await self._http_client.request(method, url, headers=self._headers(), timeout=timeout, **kwargs)
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.request(method, url, headers=self._headers(), **kwargs)

    async def health(self) -> bool:
        for path in ("/v1/health/", "/v1/health", "/health"):
            try:
                r = await self._req("GET", path, timeout=5.0)
                if r.status_code == 200:
                    return True
            except Exception:
                continue
        return False

    async def ensure_agent(self, organism_name: str, identity_summary: str) -> str | None:
        """Find or create the Letta memory agent representing this organism."""
        target_name = self.settings.agent_name or f"aoc-{organism_name.lower()}"
        try:
            if self.agent_id:
                r = await self._req("GET", f"/v1/agents/{self.agent_id}")
                if r.status_code == 200:
                    return self.agent_id

            # Search existing agents by name
            r = await self._req("GET", "/v1/agents/", params={"name": target_name})
            if r.status_code == 200:
                agents = r.json()
                if isinstance(agents, list) and agents:
                    for a in agents:
                        if a.get("name") == target_name:
                            self.agent_id = str(a["id"])
                            return self.agent_id

            # Create new agent with Identity and Self Model core memory blocks
            payload: dict[str, Any] = {
                "name": target_name,
                "memory_blocks": [
                    {"label": "identity", "value": identity_summary[:4000]},
                    {"label": "self_model", "value": "Initial AOC V1.0 self model: learning capabilities and limits."},
                ],
                "include_base_tools": False,
            }
            if self.llm_model:
                payload["llm_config"] = {
                    "model": self.llm_model,
                    "model_endpoint_type": "openai",
                    "model_endpoint": self.llm_endpoint or "http://host.docker.internal:8888/v1",
                    "context_window": 32768,
                }
            # Use configured OpenAI-compatible embedding endpoint (defaults to AOC's local /v1/embeddings)
            payload["embedding_config"] = {
                "embedding_endpoint_type": "openai",
                "embedding_endpoint": self.settings.embedding_endpoint,
                "embedding_model": self.settings.embedding_model,
                "embedding_dim": self.settings.embedding_dim,
                "embedding_chunk_size": 300,
            }
            r = await self._req("POST", "/v1/agents/", json=payload)
            if r.status_code >= 400:
                # Retry minimal payload (let Letta server pick its own configured defaults)
                minimal = {
                    "name": target_name,
                    "memory_blocks": payload["memory_blocks"],
                }
                r = await self._req("POST", "/v1/agents/", json=minimal)
            r.raise_for_status()
            created = r.json()
            self.agent_id = str(created["id"])
            logger.info("letta.agent_created", agent_id=self.agent_id, name=target_name)
            return self.agent_id
        except Exception as exc:
            logger.warning("letta.ensure_agent_failed", error=str(exc))
            return None

    async def store(self, item: MemoryItem) -> MemoryWriteResult:
        if not self.agent_id:
            return MemoryWriteResult(synced_to_letta=False, error="letta agent_id not initialized")

        # Identity & Self Model also update core memory blocks
        if item.memory_type in {MemoryType.IDENTITY.value, MemoryType.SELF_MODEL.value}:
            ok = await self.update_core_block(item.memory_type, item.content)
            MEMORY_WRITES.labels(type=item.memory_type, result="ok" if ok else "failed").inc()
            return MemoryWriteResult(
                external_id=f"block:{item.memory_type}" if ok else None,
                synced_to_letta=ok,
                error=None if ok else "failed to update Letta core memory block",
            )

        text = format_passage(item)
        try:
            r = await self._req(
                "POST",
                f"/v1/agents/{self.agent_id}/archival-memory",
                json={"text": text},
            )
            r.raise_for_status()
            body = r.json()
            ext_id: str | None = None
            if isinstance(body, list) and body:
                ext_id = str(body[0].get("id"))
            elif isinstance(body, dict):
                ext_id = str(body.get("id") or "")
            MEMORY_WRITES.labels(type=item.memory_type, result="ok").inc()
            return MemoryWriteResult(external_id=ext_id or None, synced_to_letta=True)
        except Exception as exc:
            MEMORY_WRITES.labels(type=item.memory_type, result="failed").inc()
            logger.warning("letta.store_failed", memory_type=item.memory_type, error=str(exc))
            return MemoryWriteResult(synced_to_letta=False, error=str(exc))

    async def search(
        self,
        query: str = "",
        memory_types: list[str] | None = None,
        limit: int = 10,
    ) -> list[MemoryItem]:
        if not self.agent_id:
            return []
        try:
            params: dict[str, Any] = {"limit": max(limit * 2, 20)}
            if query:
                params["search"] = query
            r = await self._req("GET", f"/v1/agents/{self.agent_id}/archival-memory", params=params)
            r.raise_for_status()
            passages = r.json()
            if not isinstance(passages, list):
                return []
            items = [parse_passage(p.get("text", ""), external_id=str(p.get("id", ""))) for p in passages if isinstance(p, dict)]
            if memory_types:
                allowed = set(memory_types)
                items = [it for it in items if it.memory_type in allowed]
            items.sort(key=lambda it: it.importance, reverse=True)
            return items[:limit]
        except Exception as exc:
            logger.warning("letta.search_failed", query=query[:80], error=str(exc))
            return []

    async def update_core_block(self, label: str, value: str) -> bool:
        if not self.agent_id:
            return False
        try:
            r = await self._req(
                "PATCH",
                f"/v1/agents/{self.agent_id}/core-memory/blocks/{label}",
                json={"value": value[:4000]},
            )
            return r.status_code < 400
        except Exception as exc:
            logger.warning("letta.update_block_failed", label=label, error=str(exc))
            return False

    async def get_core_blocks(self) -> dict[str, str]:
        if not self.agent_id:
            return {}
        try:
            r = await self._req("GET", f"/v1/agents/{self.agent_id}/core-memory/blocks")
            r.raise_for_status()
            blocks = r.json()
            if isinstance(blocks, list):
                return {str(b.get("label")): str(b.get("value", "")) for b in blocks if isinstance(b, dict)}
            return {}
        except Exception as exc:
            logger.warning("letta.get_blocks_failed", error=str(exc))
            return {}

    async def delete_agent(self, agent_id: str) -> bool:
        """Idempotently delete a temporary/culled Letta agent and its passages."""
        target = (agent_id or "").strip()
        if not target:
            return True
        try:
            r = await self._req("DELETE", f"/v1/agents/{target}")
            if r.status_code in {200, 202, 204, 404}:
                if self.agent_id == target:
                    self.agent_id = None
                return True
            logger.warning("letta.delete_agent_unexpected_status", agent_id=target, status=r.status_code)
            return False
        except Exception as exc:
            logger.warning("letta.delete_agent_failed", agent_id=target, error=str(exc))
            return False
