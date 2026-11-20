"""P0-6 Memory unit tests: 5 memory types, Letta REST adapter, fault tolerance, restart recovery."""

from __future__ import annotations

import json
import uuid
import httpx
import pytest

from app.core.config import MemorySettings
from app.core.enums import MemoryType
from app.memory.experience_manager import ExperienceManager
from app.memory.interfaces import MemoryItem
from app.memory.letta_adapter import LettaAdapter, format_passage, parse_passage
from app.storage.repository import StateRepository


class FakeLettaServer:
    """In-memory mock of Letta v1 REST API."""

    def __init__(self, fail_writes: bool = False, healthy: bool = True) -> None:
        self.healthy = healthy
        self.fail_writes = fail_writes
        self.agents: dict[str, dict] = {}
        self.blocks: dict[str, dict[str, str]] = {}
        self.passages: dict[str, list[dict]] = {}

    def client(self) -> httpx.AsyncClient:
        def handler(req: httpx.Request) -> httpx.Response:
            path = req.url.path
            if path in ("/v1/health/", "/v1/health", "/health"):
                return httpx.Response(200 if self.healthy else 503, json={"status": "ok" if self.healthy else "down"}, request=req)

            if path == "/v1/agents/" and req.method == "GET":
                name = req.url.params.get("name")
                matched = [a for a in self.agents.values() if not name or a["name"] == name]
                return httpx.Response(200, json=matched, request=req)

            if path == "/v1/agents/" and req.method == "POST":
                body = json.loads(req.content.decode())
                aid = f"agent-{uuid.uuid4().hex[:8]}"
                self.agents[aid] = {"id": aid, "name": body["name"]}
                self.blocks[aid] = {b["label"]: b["value"] for b in body.get("memory_blocks", [])}
                self.passages[aid] = []
                return httpx.Response(200, json=self.agents[aid], request=req)

            parts = [p for p in path.split("/") if p]
            # /v1/agents/{aid}
            if len(parts) == 3 and parts[:2] == ["v1", "agents"] and req.method == "GET":
                aid = parts[2]
                if aid in self.agents:
                    return httpx.Response(200, json=self.agents[aid], request=req)
                return httpx.Response(404, request=req)

            # /v1/agents/{aid}/core-memory/blocks
            if len(parts) == 5 and parts[3:] == ["core-memory", "blocks"] and req.method == "GET":
                aid = parts[2]
                blks = [{"label": k, "value": v} for k, v in self.blocks.get(aid, {}).items()]
                return httpx.Response(200, json=blks, request=req)

            # /v1/agents/{aid}/core-memory/blocks/{label}
            if len(parts) == 6 and parts[3:5] == ["core-memory", "blocks"] and req.method == "PATCH":
                if self.fail_writes:
                    return httpx.Response(500, text="letta down", request=req)
                aid, label = parts[2], parts[5]
                body = json.loads(req.content.decode())
                self.blocks.setdefault(aid, {})[label] = body["value"]
                return httpx.Response(200, json={"label": label, "value": body["value"]}, request=req)

            # /v1/agents/{aid}/archival-memory
            if len(parts) == 4 and parts[3] == "archival-memory":
                aid = parts[2]
                if req.method == "POST":
                    if self.fail_writes:
                        return httpx.Response(500, text="letta write error", request=req)
                    body = json.loads(req.content.decode())
                    pid = f"passage-{uuid.uuid4().hex[:8]}"
                    rec = {"id": pid, "text": body["text"]}
                    self.passages.setdefault(aid, []).append(rec)
                    return httpx.Response(200, json=[rec], request=req)
                if req.method == "GET":
                    q = (req.url.params.get("search") or "").lower()
                    items = self.passages.get(aid, [])
                    if q:
                        items = [p for p in items if q in p["text"].lower()]
                    return httpx.Response(200, json=items, request=req)

            return httpx.Response(404, request=req)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_passage_format_and_parse_roundtrip():
    item = MemoryItem(
        memory_type=MemoryType.SEMANTIC.value,
        title="SQLite WAL",
        content="WAL improves concurrent reads.",
        source="cycle:1",
        importance=0.85,
        tags=["sqlite", "storage"],
    )
    raw = format_passage(item)
    parsed = parse_passage(raw, external_id="p-1")
    assert parsed.memory_type == "semantic"
    assert parsed.title == "SQLite WAL"
    assert parsed.content == "WAL improves concurrent reads."
    assert parsed.source == "cycle:1"
    assert parsed.importance == pytest.approx(0.85)
    assert parsed.external_id == "p-1"


@pytest.mark.asyncio
async def test_experience_manager_all_5_types_and_restart_recovery(repo: StateRepository):
    server = FakeLettaServer()
    ident = await repo.get_or_create_identity("AOC-MEM", "1.0.0")

    async with server.client() as client:
        adapter = LettaAdapter(MemorySettings(letta_base_url="http://letta-mock"), http_client=client)
        mgr = ExperienceManager(repo, backend=adapter)
        init_info = await mgr.initialize(ident["id"], "AOC-MEM", "AOC-MEM digital organism identity")
        assert init_info["letta_available"] is True
        saved_agent_id = init_info["agent_id"]

        for mtype, title, body, imp in [
            (MemoryType.EPISODIC.value, "Disk incident", "Handled simulated disk pressure", 0.9),
            (MemoryType.SEMANTIC.value, "Raft election", "Raft uses randomized election timeouts", 0.8),
            (MemoryType.PROCEDURAL.value, "Disk check skill", "Run df -h and du -sh to locate large dirs", 0.85),
            (MemoryType.SELF_MODEL.value, "Self limits", "Cannot execute L3 operations", 0.95),
        ]:
            res = await mgr.remember(
                MemoryItem(memory_type=mtype, title=title, content=body, source="test", importance=imp)
            )
            assert res.synced_to_letta is True

        # Deduplication check
        dup = await mgr.remember(
            MemoryItem(
                memory_type=MemoryType.SEMANTIC.value,
                title="Raft election",
                content="Raft uses randomized election timeouts",
                source="test",
                importance=0.8,
            )
        )
        assert dup.deduplicated is True

    # Simulate process restart: new LettaAdapter + new ExperienceManager reading same DB & Letta server
    reloaded_ident = await repo.get_or_create_identity("AOC-MEM", "1.0.0")
    assert reloaded_ident["memory_agent_id"] == saved_agent_id

    async with server.client() as client2:
        adapter2 = LettaAdapter(
            MemorySettings(letta_base_url="http://letta-mock"),
            agent_id=reloaded_ident["memory_agent_id"],
            http_client=client2,
        )
        mgr2 = ExperienceManager(repo, backend=adapter2)
        await mgr2.initialize(reloaded_ident["id"], "AOC-MEM", "AOC-MEM digital organism identity")

        found = await mgr2.recall(query="Raft", memory_types=[MemoryType.SEMANTIC.value], limit=5)
        assert len(found) >= 1
        assert "randomized election timeouts" in found[0].content


@pytest.mark.asyncio
async def test_memory_write_failure_does_not_crash_and_flushes_later(repo: StateRepository):
    server = FakeLettaServer(fail_writes=True, healthy=True)
    ident = await repo.get_or_create_identity("AOC-FAULT", "1.0.0")

    async with server.client() as client:
        adapter = LettaAdapter(MemorySettings(letta_base_url="http://letta-mock"), http_client=client)
        mgr = ExperienceManager(repo, backend=adapter)
        await mgr.initialize(ident["id"], "AOC-FAULT", "Identity summary")

        # Write fails in Letta, must NOT raise, and must still be locally searchable!
        res = await mgr.remember(
            MemoryItem(
                memory_type=MemoryType.EPISODIC.value,
                title="Transient outage event",
                content="Survived Letta 500 error without crashing loop",
                source="cycle:7",
                importance=0.9,
            )
        )
        assert res.synced_to_letta is False
        assert res.stored_index_id is not None

        recalled = await mgr.recall(query="outage", limit=5)
        assert len(recalled) == 1
        assert "Survived Letta 500" in recalled[0].content

        # Now Letta recovers -> flush_pending syncs the queued item
        server.fail_writes = False
        flushed = await mgr.flush_pending()
        assert flushed >= 1
