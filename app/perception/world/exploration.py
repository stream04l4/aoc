"""Curiosity External Exploration Lifecycle & Diversity Guard (V4.0 M3).

Reuses `digital_world_state(kind='exploration_topic')` (0 new tables) to prevent Curiosity
from getting trapped in repetitive internal self-diagnostics, pushing exploration outward
toward diverse external technical/scientific domains with real verifiable outcomes.
"""

from __future__ import annotations

import re
from typing import Any

from app.observability.logging import get_logger
from app.perception.interfaces import utcnow
from app.storage.repository import StateRepository

logger = get_logger("aoc.perception.world.exploration")

_INTERNAL_DIAG_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(curiosity[_\s-]?gap|gap[_\s-]?ledger|provenance[_\s-]?checksum|self[_\s-]?model|skill[_\s-]?artifact|aoc[_\s-]?diagnostic|snapshot[_\s-]?diagnostic|agent[_\s-]?redundancy)\b", re.I),
    re.compile(r"\b(stdlib[_\s-]?only[_\s-]?l1|reconciliation[_\s-]?diagnostic|internal[_\s-]?audit|inventory[_\s-]?assessment)\b", re.I),
    re.compile(r"(自查|台账|内省诊断|内部审计|自检脚本|好奇心缺口台账)"),
)

_EXTERNAL_SIGNALS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(arxiv|paper|github|release|pgvector|letta|postgres|postgresql|python|rust|mcp|llm|agent|benchmark|rag|hnsw|onnx|linux|systemd|docker|kubernetes|security|cve|research)\b", re.I),
    re.compile(r"(外部|论文|开源|框架|前沿|生态|调研|新技术|版本发布)"),
)


def classify_exploration_scope(title: str, description: str = "", related_key: str | None = None) -> str:
    """Deterministically classify a goal/topic as `internal_self_check` vs `external_exploration`."""
    blob = f"{title or ''} {description or ''} {related_key or ''}".strip()
    if any(p.search(blob) for p in _INTERNAL_DIAG_PATTERNS):
        return "internal_self_check"
    if any(p.search(blob) for p in _EXTERNAL_SIGNALS):
        return "external_exploration"
    # Default unknown topics from world sensors (`world:...`) as external
    if str(related_key or "").startswith("world:"):
        return "external_exploration"
    return "external_exploration"


def infer_topic_domain(title: str, description: str = "", explicit_domain: str | None = None) -> str:
    if explicit_domain and str(explicit_domain).strip():
        return re.sub(r"[^a-z0-9_]+", "_", str(explicit_domain).strip().lower()).strip("_")[:32] or "general"
    blob = f"{title} {description}".lower()
    if any(k in blob for k in ("arxiv", "paper", "llm", "transformer", "reasoning", "rl")):
        return "ai_research"
    if any(k in blob for k in ("pgvector", "postgres", "sql", "hnsw", "database", "index")):
        return "databases"
    if any(k in blob for k in ("letta", "memory", "rag", "embedding", "onnx")):
        return "agent_memory"
    if any(k in blob for k in ("mcp", "tool", "agent", "codex", "sandbox", "skill")):
        return "agent_engineering"
    if any(k in blob for k in ("linux", "systemd", "cgroup", "docker", "security", "kernel")):
        return "systems_infra"
    return "general_tech"


class ExplorationManager:
    """Tracks external exploration topics and enforces anti-internal-loop diversity guidance."""

    def __init__(
        self,
        *,
        repo: StateRepository,
        max_internal_curiosity_ratio: float = 0.45,
        lookback_goals: int = 10,
    ) -> None:
        self.repo = repo
        self.max_internal_curiosity_ratio = max(0.1, min(0.9, float(max_internal_curiosity_ratio)))
        self.lookback_goals = max(3, int(lookback_goals))

    async def upsert_topic(
        self,
        *,
        topic_key: str,
        question: str,
        domain: str | None = None,
        status: str = "exploring",
        source: str = "curiosity",
        verified_fact: str | None = None,
        information_gain: float | None = None,
    ) -> dict[str, Any]:
        slug = re.sub(r"[^a-z0-9_:-]+", "_", str(topic_key or "").strip().lower()).strip("_")[:100] or "external_topic"
        items = await self.repo.list_world_items(kind="exploration_topic")
        prev = next((x for x in items if x.get("key") == slug), None)
        val = dict(prev.get("value") or {}) if prev and isinstance(prev.get("value"), dict) else {}

        dom = infer_topic_domain(slug, question, explicit_domain=domain or val.get("domain"))
        scope = classify_exploration_scope(slug, question, slug)
        facts = list(val.get("verified_facts") or [])
        if verified_fact and verified_fact not in facts:
            facts.append(verified_fact[:400])

        val.update(
            {
                "topic_key": slug,
                "domain": dom,
                "scope": scope,
                "question": (question or val.get("question") or slug)[:400],
                "attempts": int(val.get("attempts") or 0) + (1 if status == "exploring" else 0),
                "verified_facts": facts[-10:],
                "information_gain": (
                    round(float(information_gain), 4)
                    if information_gain is not None
                    else val.get("information_gain")
                ),
                "last_explored_at": utcnow().isoformat(),
            }
        )
        await self.repo.upsert_world_item(
            kind="exploration_topic",
            key=slug,
            value=val,
            status=status,
            source=source,
        )
        return {"key": slug, "status": status, **val}

    async def build_guidance(self, recent_goals: list[dict[str, Any]]) -> dict[str, Any]:
        """Compute Curiosity internal-vs-external balance and surface fresh external targets."""
        cur_goals = [g for g in recent_goals if str(g.get("drive_type") or "").lower() == "curiosity"][: self.lookback_goals]
        internal_cnt = 0
        external_cnt = 0
        for g in cur_goals:
            sc = classify_exploration_scope(
                str(g.get("title") or ""),
                str(g.get("description") or ""),
                g.get("related_key"),
            )
            if sc == "internal_self_check":
                internal_cnt += 1
            else:
                external_cnt += 1

        total = internal_cnt + external_cnt
        internal_ratio = round(internal_cnt / total, 4) if total > 0 else 0.0
        must_explore_external = total >= 2 and internal_ratio >= self.max_internal_curiosity_ratio

        topics = await self.repo.list_world_items(kind="exploration_topic")
        domains_covered = sorted(
            {
                str((t.get("value") or {}).get("domain") or "general_tech")
                for t in topics
                if isinstance(t.get("value"), dict)
            }
        )
        open_world_topics = await self.repo.list_world_items(kind="knowledge_topic", status="open")
        external_suggestions = [
            {
                "key": t["key"],
                "title": (t.get("value") or {}).get("title") or t["key"],
                "source_id": (t.get("value") or {}).get("source_id") or t.get("source"),
            }
            for t in open_world_topics
            if classify_exploration_scope(
                str((t.get("value") or {}).get("title") or t["key"]),
                str((t.get("value") or {}).get("description") or ""),
                t["key"],
            )
            == "external_exploration"
        ][:6]

        return {
            "recent_curiosity_sample": total,
            "internal_self_check_count": internal_cnt,
            "external_exploration_count": external_cnt,
            "internal_ratio": internal_ratio,
            "max_internal_ratio": self.max_internal_curiosity_ratio,
            "must_explore_external": must_explore_external,
            "domains_covered": domains_covered,
            "active_exploration_topics": [
                {
                    "key": t["key"],
                    "status": t["status"],
                    "domain": (t.get("value") or {}).get("domain", "general_tech"),
                    "question": (t.get("value") or {}).get("question", ""),
                }
                for t in topics[:8]
            ],
            "suggested_external_topics": external_suggestions,
        }
