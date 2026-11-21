"""P1-4 unit tests: switchable embeddings, cross-dim guard, model upgrade coexistence,
4-factor info gain, outage degradation/recovery, restart info_gain hydration, V2 memory APIs & Alembic 0005.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.api.app import create_app
from app.cognition.interfaces import ActionPlan, AssessmentResult, BrainInterface, EvaluationResult, GoalProposal
from app.core.config import ApiSettings, EmbeddingSettings, Settings
from app.core.enums import MemoryType
from app.core.runtime import OrganismRuntime
from app.memory.embedding import (
    DeterministicHashEmbeddingProvider,
    EmbeddingBatchResult,
    EmbeddingDimensionMismatchError,
    EmbeddingError,
    EmbeddingInterface,
    EmbeddingModelSpec,
    EmbeddingService,
    cosine_similarity,
)
from app.memory.experience_manager import ExperienceManager, compute_content_hash
from app.memory.info_gain import ExistingMemorySample, InformationGainEvaluator
from app.memory.interfaces import MemoryInterface, MemoryItem, MemoryWriteResult
from app.memory.retrieval import HybridRetrievalEngine, bilingual_trigram_similarity, build_contextual_query
from app.perception.interfaces import Observation, utcnow
from app.storage.database import Database
from app.storage.models import Base
from app.storage.outbox import OutboxDispatcher, OutboxEventType
from app.storage.repository import StateRepository

PROJECT_ROOT = Path(__file__).resolve().parents[2]


async def _init_db(db: Database) -> None:
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


class _FlakyEmbeddingProvider(EmbeddingInterface):
    def __init__(self, *, fail: bool = True, dim: int = 64) -> None:
        self.fail = fail
        self._delegate = DeterministicHashEmbeddingProvider(model_key=f"flaky-embed-{dim}:v1", dimensions=dim)

    @property
    def spec(self) -> EmbeddingModelSpec:
        return self._delegate.spec

    async def embed_texts(self, texts: list[str], *, is_query: bool = False) -> EmbeddingBatchResult:
        if self.fail:
            raise EmbeddingError("Simulated embedding service outage")
        return await self._delegate.embed_texts(texts, is_query=is_query)

    async def health(self) -> bool:
        return not self.fail


class _ToggleLettaBackend(MemoryInterface):
    def __init__(self, *, online: bool = True) -> None:
        self.online = online
        self.agent_id: str | None = "letta-agent-1"
        self.stored: list[MemoryItem] = []

    async def health(self) -> bool:
        return self.online

    async def ensure_agent(self, organism_name: str, identity_summary: str) -> str | None:
        return self.agent_id if self.online else None

    async def store(self, item: MemoryItem) -> MemoryWriteResult:
        if not self.online:
            return MemoryWriteResult(synced_to_letta=False, error="Simulated Letta outage")
        self.stored.append(item)
        return MemoryWriteResult(external_id=f"pass-{len(self.stored)}", synced_to_letta=True)

    async def search(self, query: str = "", memory_types: list[str] | None = None, limit: int = 10) -> list[MemoryItem]:
        if not self.online:
            raise RuntimeError("Letta offline")
        return self.stored[:limit]

    async def update_core_block(self, label: str, value: str) -> bool:
        return self.online

    async def get_core_blocks(self) -> dict[str, str]:
        return {"identity": "AOC"} if self.online else {}


class _IdleBrain(BrainInterface):
    async def assess(self, obs, memories, drives) -> AssessmentResult:
        return AssessmentResult(summary="healthy", confidence=0.95)

    async def generate_goal(self, ctx) -> GoalProposal:
        return GoalProposal(should_act=False, rationale="idle")

    async def plan(self, ctx) -> ActionPlan:
        return ActionPlan(should_execute=False)

    async def evaluate(self, ctx) -> EvaluationResult:
        return EvaluationResult(goal_achieved=True, summary="ok")


async def test_cross_dimension_embedding_mixing_strictly_rejected(tmp_path: Path):
    # 1. Direct cosine similarity guard
    with pytest.raises(EmbeddingDimensionMismatchError):
        cosine_similarity([1.0, 0.0, 0.0], [1.0, 0.0])

    # 2. DB-level guard rejects storing wrong-dimension vector under registered model
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'dim_guard.db'}")
    await _init_db(db)
    repo = StateRepository(db)
    try:
        m_row = await repo.upsert_embedding_model(
            model_key="test-384:v1",
            provider="local_onnx",
            model_name="test-384",
            model_version="v1",
            dimensions=384,
            activate=True,
        )
        mem_id = await repo.add_memory_index(
            memory_type="semantic",
            title="PG Outbox",
            summary="PostgreSQL transactional outbox uses SKIP LOCKED",
            content_hash=compute_content_hash("semantic", "PG Outbox", "PostgreSQL transactional outbox uses SKIP LOCKED"),
            source="unit",
            importance=0.8,
            tags=["pg"],
        )
        with pytest.raises(ValueError, match="Dimension mismatch"):
            await repo.upsert_memory_embedding(
                memory_id=mem_id,
                model_id=uuid.UUID(m_row["id"]),
                dimensions=512,
                embedding=[0.01] * 512,
                content_hash="abc",
            )
    finally:
        await db.dispose()


async def test_model_version_switch_coexistence_and_reindexing(tmp_path: Path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'model_switch.db'}")
    await _init_db(db)
    repo = StateRepository(db)
    try:
        prov_a = DeterministicHashEmbeddingProvider(model_key="model-a-128:v1", dimensions=128)
        svc = EmbeddingService(repo, prov_a)
        mid_a = await svc.ensure_registered()

        m1 = await repo.add_memory_index(
            memory_type="semantic",
            title="PostgreSQL 并发锁",
            summary="使用 SELECT FOR UPDATE SKIP LOCKED 避免多 Worker 重复消费 Outbox。",
            content_hash=compute_content_hash("semantic", "PostgreSQL 并发锁", "skip locked"),
            source="unit",
            importance=0.9,
            tags=["postgres", "outbox"],
        )
        m2 = await repo.add_memory_index(
            memory_type="semantic",
            title="Linux cgroup v2 isolation",
            summary="Systemd Delegate=yes delegates memory.max and pids.max without root.",
            content_hash=compute_content_hash("semantic", "Linux cgroup v2 isolation", "delegate"),
            source="unit",
            importance=0.85,
            tags=["linux", "cgroup"],
        )
        await svc.embed_memory_record(m1)
        await svc.embed_memory_record(m2)

        # Switch to incompatible-dimension model B (64d): old 128d vectors stay isolated & untouched
        prov_b = DeterministicHashEmbeddingProvider(model_key="model-b-64:v2", dimensions=64)
        mid_b = await svc.switch_provider(prov_b)
        assert mid_b != mid_a

        # Before reindex, search under model B must NOT mix with model A's 128d vectors (zero crash!)
        engine = HybridRetrievalEngine(repo, svc)
        res_before = await engine.search(query="PostgreSQL SKIP LOCKED", limit=5)
        assert len(res_before) == 2

        # Reindex missing under model B -> now both models coexist cleanly
        reidx = await svc.reindex_missing_for_active_model(limit=50)
        assert reidx["embedded"] == 2
        assert reidx["failed"] == 0

        stats = await repo.get_memory_embedding_stats()
        counts = {m["model_key"]: m["embedded_count"] for m in stats["models"]}
        assert counts["model-a-128:v1"] == 2
        assert counts["model-b-64:v2"] == 2

        res_after = await engine.search(query="PostgreSQL 并发锁 SKIP LOCKED", limit=2)
        assert res_after[0].item.title == "PostgreSQL 并发锁"
        assert res_after[0].vec_score > 0.0
    finally:
        await db.dispose()


def test_four_factor_information_gain_suppresses_duplicates_and_low_reliability_preserves_corroboration():
    ev = InformationGainEvaluator()
    known = [
        ExistingMemorySample(
            memory_id="m1",
            content_hash="hash-known-1",
            source="source_alpha",
            text="PostgreSQL transactional outbox guarantees reliable event delivery with SKIP LOCKED",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
    ]

    # 1. Truly novel, verified, high-reliability knowledge -> high gain
    novel = ev.evaluate(
        new_text="Systemd Delegate=yes enables unprivileged cgroup v2 subtree management",
        new_facts=["Systemd Delegate=yes delegates cgroup v2 subtree to non-root ubuntu user"],
        new_source="source_beta",
        new_embedding=[0.0, 1.0, 0.0, 0.0],
        existing_memories=known,
        source_reliability=0.9,
        goal_relevance=1.0,
        verification_confidence=1.0,
    )
    assert novel.information_gain >= 0.55
    assert novel.is_duplicate is False

    # 2. Same-source near-duplicate paraphrase (cosine_sim = 0.99) -> suppressed to 0!
    dup = ev.evaluate(
        new_text="PostgreSQL outbox guarantees reliable delivery via SKIP LOCKED",
        new_facts=["PostgreSQL transactional outbox guarantees reliable event delivery"],
        new_source="source_alpha",
        new_embedding=[0.995, 0.099, 0.0, 0.0],
        existing_memories=known,
        source_reliability=0.9,
        goal_relevance=1.0,
        verification_confidence=1.0,
    )
    assert dup.is_duplicate is True
    assert dup.information_gain == 0.0

    # 3. Independent source corroboration -> retains positive Evidence_Gain (0.35 * 0.3 * factors)
    corrob = ev.evaluate(
        new_text="PostgreSQL transactional outbox uses SKIP LOCKED for concurrency",
        new_facts=["PostgreSQL transactional outbox guarantees reliable event delivery"],
        new_source="independent_audit_source",
        new_embedding=[0.98, 0.19, 0.0, 0.0],
        existing_memories=known,
        source_reliability=0.9,
        goal_relevance=1.0,
        verification_confidence=1.0,
    )
    assert corrob.corroborated_new_source is True
    assert corrob.evidence_gain == pytest.approx(0.35)
    assert corrob.information_gain > 0.05

    # 4. Unverified or low-reliability output -> strictly suppressed
    unverified = ev.evaluate(
        new_text="Brand new unverified rumor",
        new_facts=["Unverified claim"],
        new_source="untrusted",
        new_embedding=[0.0, 0.0, 1.0, 0.0],
        existing_memories=known,
        source_reliability=0.2,
        goal_relevance=1.0,
        verification_confidence=0.0,
    )
    assert unverified.information_gain == 0.0


async def test_embedding_and_letta_outage_10_cycles_no_crash_and_full_recovery_reconciliation(tmp_path: Path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'outage_10_cycles.db'}")
    await _init_db(db)
    try:
        flaky_emb = _FlakyEmbeddingProvider(fail=True, dim=64)
        flaky_letta = _ToggleLettaBackend(online=False)
        settings = Settings(
            embedding=EmbeddingSettings(provider="hash_fallback", dimensions=64),
        )
        rt = OrganismRuntime(
            settings=settings,
            db=db,
            brain=_IdleBrain(),
            memory_backend=flaky_letta,
            auto_load_agents=False,
            llm_probe_fn=lambda: None,
            embedding_provider=flaky_emb,
        )
        await rt.start(start_scheduler=False)

        # Write memories & run 10 consecutive cycles while BOTH Embedding and Letta are DOWN!
        for i in range(10):
            await rt.memory.remember(
                MemoryItem(
                    memory_type=MemoryType.SEMANTIC.value,
                    title=f"故障韧性记录 {i}",
                    content=f"在 Embedding 与 Letta 均宕机期间记录第 {i} 条可靠知识，测试 PostgreSQL 降级。",
                    source=f"cycle:{i}",
                    importance=0.8,
                    tags=["resilience", "outage"],
                )
            )
            cyc = await rt.loop.run_cycle()
            assert cyc["outcome"] != "ERROR"

        # Verify graceful degradation on search while Embedding is still down
        degraded_hits = await rt.memory.search_hybrid(query="故障韧性记录 PostgreSQL 降级", limit=5)
        assert len(degraded_hits) >= 1
        assert degraded_hits[0].degraded_no_vector is True
        assert degraded_hits[0].txt_score > 0.0

        # Restore BOTH Embedding and Letta -> replay Outbox -> 100% embedded & synced!
        flaky_emb.fail = False
        flaky_letta.online = True
        await rt.repo.replay_memory_sync_outbox(limit=200)
        for _ in range(5):
            processed = await rt.outbox.dispatch_once(batch_size=50)
            if processed == 0:
                break

        emb_stats = await rt.repo.get_memory_embedding_stats()
        sync_stats = await rt.repo.get_memory_sync_stats()
        total_mems = emb_stats["total_memories"]
        assert total_mems == 11  # 1 bootstrap identity + 10 outage memories
        assert emb_stats["by_status"].get("embedded", 0) == total_mems
        assert sync_stats["memory_index_by_sync_status"].get("synced", 0) == total_mems

        # Duplicate replay MUST produce zero duplicate Letta stores
        stored_count_before = len(flaky_letta.stored)
        await rt.repo.replay_memory_sync_outbox(limit=200)
        await rt.outbox.dispatch_once(batch_size=50)
        assert len(flaky_letta.stored) == stored_count_before

        await rt.stop()
    finally:
        await db.dispose()


async def test_restart_restores_persisted_info_gain_not_hardcoded_0_5(tmp_path: Path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'ig_restart.db'}")
    await _init_db(db)
    repo = StateRepository(db)
    try:
        await repo.add_memory_index(
            memory_type=MemoryType.SEMANTIC.value,
            title="Verified finding",
            summary="Measured deterministic gain",
            content_hash=compute_content_hash("semantic", "Verified finding", "Measured deterministic gain"),
            source="execution:1",
            importance=0.9,
            information_gain=0.73,
        )
        rt = OrganismRuntime(
            settings=Settings(embedding=EmbeddingSettings(provider="hash_fallback", dimensions=64)),
            db=db,
            brain=_IdleBrain(),
            memory_backend=None,
            auto_load_agents=False,
            llm_probe_fn=lambda: None,
        )
        await rt.start(start_scheduler=False)
        assert rt.loop is not None
        assert rt.loop._last_info_gain == pytest.approx(0.73)
        await rt.stop()
    finally:
        await db.dispose()


async def test_v2_memory_search_embeddings_sync_and_replay_api(tmp_path: Path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'mem_api.db'}")
    await _init_db(db)
    try:
        settings = Settings(
            api=ApiSettings(host="127.0.0.1", auth_enabled=True, admin_key="admin-mem-key"),
            embedding=EmbeddingSettings(provider="hash_fallback", dimensions=64),
        )
        letta = _ToggleLettaBackend(online=True)
        rt = OrganismRuntime(
            settings=settings,
            db=db,
            brain=_IdleBrain(),
            memory_backend=letta,
            auto_load_agents=False,
            llm_probe_fn=lambda: None,
        )
        app = create_app(settings=settings, db=db, runtime=rt, start_scheduler=False)
        headers = {"Authorization": "Bearer admin-mem-key"}

        async with app.router.lifespan_context(app):
            await rt.memory.remember(
                MemoryItem(
                    memory_type=MemoryType.SEMANTIC.value,
                    title="Redis vs PostgreSQL Outbox",
                    content="AOC uses PostgreSQL SKIP LOCKED outbox without Redis.",
                    source="test",
                    importance=0.9,
                )
            )
            await rt.outbox.dispatch_once(batch_size=20)

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                # 401 without Bearer token
                assert (await client.post("/v2/memories/search", json={"query": "Outbox"})).status_code == 401

                # Hybrid search
                sr = await client.post("/v2/memories/search", json={"query": "PostgreSQL Outbox", "limit": 5}, headers=headers)
                assert sr.status_code == 200
                hits = sr.json()
                assert len(hits) >= 1
                assert hits[0]["item"]["title"] == "Redis vs PostgreSQL Outbox"

                # Embeddings status
                er = await client.get("/v2/memories/embeddings/status", headers=headers)
                assert er.status_code == 200
                assert er.json()["by_status"].get("embedded", 0) >= 2

                # Sync status & replay
                sy = await client.get("/v2/memories/sync/status", headers=headers)
                assert sy.status_code == 200
                assert sy.json()["memory_index_by_sync_status"].get("synced", 0) >= 2

                rp = await client.post("/v2/memories/sync/replay", json={"limit": 10}, headers=headers)
                assert rp.status_code == 200
                assert "requeued" in rp.json()
    finally:
        await db.dispose()


def test_alembic_0005_upgrade_and_downgrade_roundtrip(tmp_path: Path):
    db_file = tmp_path / "alembic_0005.db"
    env = os.environ.copy()
    env["AOC_DATABASE__URL"] = f"sqlite+aiosqlite:///{db_file}"

    def run_alembic(*args: str) -> None:
        res = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=str(PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, f"alembic {args} failed:\n{res.stdout}\n{res.stderr}"

    run_alembic("upgrade", "0005_p1_4_semantic_memory")
    run_alembic("downgrade", "0004_p1_3_runner_recovery")
    run_alembic("upgrade", "0005_p1_4_semantic_memory")
