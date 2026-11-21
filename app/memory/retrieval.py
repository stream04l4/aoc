"""Hybrid Semantic + Trigram Text + Recency + Reliability + Importance Retrieval Engine (V2.0 P1-4).

Score(m) = w_vec * Sim_cos(q, m)
         + w_txt * Sim_trgm(q, m)
         + w_rec * exp(-dt_hours / tau_hours)
         + w_rel * reliability(m)
         + w_imp * importance(m)

Hard guarantees:
  - Vector comparisons STRICTLY filter by active ``model_id`` and validate dimensions (zero cross-dim mixing).
  - Supports bilingual Chinese + English matching via PostgreSQL ``pg_trgm`` (or CJK char-trigram fallback on SQLite).
  - If the Embedding service is unavailable, automatically degrades to trigram + recency + importance without crashing.
"""

from __future__ import annotations

import math
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Sequence

from pydantic import BaseModel, Field

from app.drives.interfaces import DriveState, clamp01
from app.memory.embedding import EmbeddingError, EmbeddingService, cosine_similarity
from app.memory.interfaces import MemoryItem
from app.observability.logging import get_logger
from app.perception.interfaces import Observation, utcnow
from app.storage.repository import StateRepository

logger = get_logger("aoc.memory.retrieval")

_LATIN_WORD_RE = re.compile(r"[a-z0-9_]+")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")


def _ensure_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _extract_trigrams(text: str) -> set[str]:
    """Bilingual char-trigram + CJK bigram extractor approximating PostgreSQL pg_trgm for SQLite/Python fallback."""
    norm = (text or "").lower().strip()
    if not norm:
        return set()
    grams: set[str] = set()
    for word in _LATIN_WORD_RE.findall(norm):
        padded = f"  {word} "
        for i in range(max(0, len(padded) - 2)):
            grams.add(padded[i : i + 3])
    for run in _CJK_RUN_RE.findall(norm):
        if len(run) == 1:
            grams.add(run)
        else:
            for i in range(len(run) - 1):
                grams.add(run[i : i + 2])
            for i in range(max(0, len(run) - 2)):
                grams.add(run[i : i + 3])
    return grams


def bilingual_trigram_similarity(query: str, document: str) -> float:
    """Compute asymmetric word/trigram containment similarity in [0, 1] for Chinese + English queries."""
    q_grams = _extract_trigrams(query)
    if not q_grams:
        return 0.0
    d_grams = _extract_trigrams(document)
    if not d_grams:
        return 0.0
    inter = len(q_grams & d_grams)
    if inter == 0:
        return 0.0
    # Blend Dice similarity with query coverage so short queries against longer docs score cleanly
    coverage = inter / len(q_grams)
    dice = (2.0 * inter) / (len(q_grams) + len(d_grams))
    return clamp01(0.65 * coverage + 0.35 * dice)


def build_contextual_query(
    *,
    observation: Observation | None = None,
    drives: Sequence[DriveState] = (),
    active_goals: Sequence[dict[str, Any]] = (),
) -> str:
    """Construct a rich contextual recall query for SELF_ASSESSMENT (replaces empty query='' in V1.0)."""
    parts: list[str] = []

    if drives:
        worst = max(drives, key=lambda d: d.deficit)
        if worst.deficit > 0.15:
            parts.append(f"{worst.drive_type} drive deficit")
            if worst.notes:
                parts.append(" ".join(worst.notes[:2]))

    if observation is not None:
        env = observation.environment_state or {}
        topics = env.get("unknown_topics") or []
        for t in topics[:3]:
            if isinstance(t, dict):
                parts.append(str(t.get("topic") or t.get("key") or ""))
                if t.get("description"):
                    parts.append(str(t["description"])[:120])
            else:
                parts.append(str(t))
        sys_st = observation.system_state or {}
        if isinstance(sys_st.get("disk"), dict):
            for path, dinfo in list(sys_st["disk"].items())[:2]:
                if isinstance(dinfo, dict) and float(dinfo.get("free_pct", 100)) < 25:
                    parts.append(f"disk pressure {path}")
        if isinstance(sys_st.get("llm"), dict) and sys_st["llm"].get("available") is False:
            parts.append("llm unavailable outage")

    for g in list(active_goals)[:2]:
        if g.get("title"):
            parts.append(str(g["title"]))

    query = " ".join(p.strip() for p in parts if p and p.strip())
    return query[:512]


class ScoredMemory(BaseModel):
    item: MemoryItem
    total_score: float
    vec_score: float = 0.0
    txt_score: float = 0.0
    recency_score: float = 0.0
    reliability_score: float = 0.8
    importance_score: float = 0.5
    degraded_no_vector: bool = False


class HybridRetrievalEngine:
    def __init__(
        self,
        repo: StateRepository,
        embedding_service: EmbeddingService | None = None,
        *,
        w_vec: float = 0.45,
        w_txt: float = 0.20,
        w_rec: float = 0.15,
        w_rel: float = 0.10,
        w_imp: float = 0.10,
        recency_half_life_hours: float = 168.0,
        candidate_pool_size: int = 120,
    ) -> None:
        self.repo = repo
        self.embedding_service = embedding_service
        self.w_vec = w_vec
        self.w_txt = w_txt
        self.w_rec = w_rec
        self.w_rel = w_rel
        self.w_imp = w_imp
        self.recency_tau_hours = max(1.0, recency_half_life_hours / math.log(2.0))
        self.candidate_pool_size = max(20, int(candidate_pool_size))

    async def search(
        self,
        *,
        query: str = "",
        memory_types: list[str] | None = None,
        limit: int = 10,
        now: datetime | None = None,
    ) -> list[ScoredMemory]:
        now_dt = _ensure_utc(now or utcnow())
        q_clean = (query or "").strip()

        q_vec: list[float] | None = None
        active_model_id: uuid.UUID | None = None
        degraded = False

        if q_clean and self.embedding_service is not None:
            try:
                active_model_id, q_vec = await self.embedding_service.embed_query(q_clean)
            except EmbeddingError as exc:
                degraded = True
                logger.warning("retrieval.embedding_degraded", error=str(exc))

        # Fetch candidate pool with same-model embeddings ONLY (strictly prevents cross-dimension mixing!)
        candidates = await self.repo.fetch_hybrid_memory_candidates(
            query=q_clean,
            model_id=active_model_id,
            query_vector=q_vec,
            memory_types=memory_types,
            limit=max(self.candidate_pool_size, limit * 4),
        )

        # Re-normalize weights cleanly when vector search is unavailable or query is empty
        if q_vec is None:
            w_vec = 0.0
            if q_clean:
                w_txt, w_rec, w_rel, w_imp = 0.50, 0.20, 0.15, 0.15
            else:
                w_txt, w_rec, w_rel, w_imp = 0.0, 0.35, 0.20, 0.45
        else:
            w_vec, w_txt, w_rec, w_rel, w_imp = self.w_vec, self.w_txt, self.w_rec, self.w_rel, self.w_imp

        scored: list[ScoredMemory] = []
        for row in candidates:
            vec_sim = 0.0
            if q_vec is not None:
                if row.get("pg_vec_sim") is not None:
                    vec_sim = clamp01(float(row["pg_vec_sim"]))
                elif row.get("embedding") is not None:
                    # Strictly verify dimension match (defense-in-depth beyond SQL model_id filter)
                    raw_sim = cosine_similarity(q_vec, row["embedding"])
                    vec_sim = clamp01((raw_sim + 1.0) / 2.0 if raw_sim < 0 else raw_sim)

            if q_clean:
                if row.get("pg_trgm_sim") is not None:
                    txt_sim = clamp01(float(row["pg_trgm_sim"]))
                else:
                    haystack = f"{row['title']} {row['summary']} {' '.join(row.get('tags') or [])}"
                    txt_sim = bilingual_trigram_similarity(q_clean, haystack)
            else:
                txt_sim = 0.0

            created = _ensure_utc(row["created_at"])
            dt_hours = max(0.0, (now_dt - created).total_seconds() / 3600.0)
            recency = math.exp(-dt_hours / self.recency_tau_hours)
            reliability = clamp01(float(row.get("reliability", 0.8)))
            importance = clamp01(float(row.get("importance", 0.5)))

            total = (
                w_vec * vec_sim
                + w_txt * txt_sim
                + w_rec * recency
                + w_rel * reliability
                + w_imp * importance
            )

            item = MemoryItem(
                memory_id=row["memory_id"],
                external_id=row.get("external_id"),
                memory_type=row["memory_type"],
                title=row["title"],
                content=row["summary"],
                source=row["source"],
                importance=importance,
                tags=list(row.get("tags") or []),
                timestamp=row["created_at"],
                metadata={
                    "reliability": reliability,
                    "information_gain": row.get("information_gain"),
                    "embedding_status": row.get("embedding_status"),
                    "hybrid_score": round(total, 4),
                    "vec_score": round(vec_sim, 4),
                    "txt_score": round(txt_sim, 4),
                },
            )
            scored.append(
                ScoredMemory(
                    item=item,
                    total_score=round(total, 4),
                    vec_score=round(vec_sim, 4),
                    txt_score=round(txt_sim, 4),
                    recency_score=round(recency, 4),
                    reliability_score=round(reliability, 4),
                    importance_score=round(importance, 4),
                    degraded_no_vector=degraded,
                )
            )

        scored.sort(key=lambda s: s.total_score, reverse=True)
        return scored[:limit]
