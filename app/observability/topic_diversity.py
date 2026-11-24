"""Read-only Long-Term Topic Diversity & Focus Observability Analyzer (V5.1 M3).

Strictly read-only (zero DB mutations, zero LLM calls):
  - Measures topic concentration vs. exploration breadth across 6h / 24h / 7d windows
    (Shannon entropy, normalized diversity index, max consecutive same-topic streak,
    Top-3 concentration ratio, first-appearance novelty rate).
  - Separates self-driven exploration goals vs. human dialogue-adopted goals.
  - Compares technical/research memories vs. `dialogue:telegram` episodic memories
    and skill capability distribution so operators can monitor long-term focus drift.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc, select

from app.perception.interfaces import utcnow
from app.storage.database import Database
from app.storage.models import (
    ActiveGoal,
    DecisionLog,
    MemoryIndex,
    SkillRegistryRow,
    WorldState,
)

_LATIN_RE = re.compile(r"[a-z0-9_]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")

_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "into",
        "about",
        "that",
        "this",
        "check",
        "inspect",
        "analyze",
        "research",
        "explore",
        "review",
        "study",
        "verify",
        "summarize",
        "build",
        "create",
        "update",
        "test",
        "task",
        "goal",
        "agent",
        "aoc",
        "general",
        "topic",
        "分析",
        "探索",
        "调研",
        "检查",
        "总结",
        "关于",
        "研究",
        "进行",
        "问题",
        "系统",
        "实现",
        "查看",
        "任务",
        "目标",
        "目前",
        "一下",
    }
)

_GENERIC_TAGS = frozenset(
    {
        "dialogue",
        "telegram",
        "social",
        "inbox",
        "deferred_due",
        "proactive_impulse",
        "memory",
        "episodic",
        "semantic",
        "procedural",
    }
)

_KEY_PREFIXES = (
    "world:rss-",
    "world:git-",
    "world:web-",
    "world:",
    "inv:",
    "tg:",
    "social:",
    "human:",
)


def _ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    utc_dt = _ensure_utc(dt)
    return utc_dt.isoformat() if utc_dt is not None else None


def _clean_key_fragment(raw: str) -> str:
    txt = (raw or "").strip().lower()
    for pfx in _KEY_PREFIXES:
        if txt.startswith(pfx):
            txt = txt[len(pfx) :]
            break
    txt = re.sub(r"[^a-z0-9\u4e00-\u9fff_:-]+", "_", txt)
    txt = re.sub(r"[_:-]+", "_", txt).strip("_")
    return txt[:48]


def _extract_meaningful_tokens(text: str) -> list[str]:
    low = (text or "").lower()
    tokens: list[str] = []
    seen: set[str] = set()

    for w in _LATIN_RE.findall(low):
        clean_w = w.strip("_")
        if len(clean_w) >= 3 and clean_w not in _STOPWORDS and clean_w not in seen:
            seen.add(clean_w)
            tokens.append(clean_w)

    for seg in _CJK_RE.findall(low):
        cur = seg
        for filler in ("关于", "进行", "研究", "分析", "调研", "探索", "检查", "总结", "问题", "系统", "实现", "查看"):
            cur = cur.replace(filler, " ")
        for piece in cur.split():
            p = piece.strip()
            if len(p) >= 2 and p not in _STOPWORDS and p not in seen:
                chunk = p[:6]
                seen.add(chunk)
                tokens.append(chunk)
    return tokens


def extract_topic_bucket(
    *,
    title: str = "",
    related_key: str | None = None,
    expected_outcome: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    fallback: str = "general",
) -> str:
    """Deterministically normalize a goal/memory into a concise comparable topic bucket."""
    exp = expected_outcome if isinstance(expected_outcome, dict) else {}
    for exp_field in ("exploration_domain", "exploration_topic_key", "skill_key", "new_skill"):
        raw_val = str(exp.get(exp_field) or "").strip()
        if raw_val and raw_val.lower() not in _STOPWORDS:
            cleaned = _clean_key_fragment(raw_val)
            if cleaned and cleaned not in _STOPWORDS:
                return cleaned

    if related_key:
        cleaned_rel = _clean_key_fragment(str(related_key))
        if cleaned_rel and len(cleaned_rel) >= 2 and cleaned_rel not in _STOPWORDS:
            return cleaned_rel

    tag_text = " ".join(str(t) for t in (tags or []) if str(t).strip().lower() not in _GENERIC_TAGS)
    tokens = _extract_meaningful_tokens(f"{title} {tag_text}")
    if tokens:
        return "_".join(tokens[:2])[:48]
    return fallback


def compute_shannon_entropy(counts: dict[str, int]) -> tuple[float, float]:
    """Compute (Shannon entropy in bits, normalized diversity index in [0, 1])."""
    pos = [int(c) for c in counts.values() if int(c) > 0]
    total = sum(pos)
    if total == 0:
        return 0.0, 0.0
    k = len(pos)
    if k == 1:
        return 0.0, (1.0 if total == 1 else 0.0)
    h = -sum((c / total) * math.log2(c / total) for c in pos)
    max_h = math.log2(k)
    norm = max(0.0, min(1.0, h / max_h)) if max_h > 0 else 0.0
    return round(h, 4), round(norm, 4)


def compute_max_streak(topics_chronological: list[str]) -> tuple[int, str | None]:
    """Compute longest consecutive identical-topic run over chronological entries."""
    if not topics_chronological:
        return 0, None
    best_len = 0
    best_topic: str | None = None
    cur_topic: str | None = None
    cur_len = 0
    for tp in topics_chronological:
        if tp == cur_topic:
            cur_len += 1
        else:
            cur_topic = tp
            cur_len = 1
        if cur_len > best_len:
            best_len = cur_len
            best_topic = cur_topic
    return best_len, best_topic


class TopicDiversityAnalyzer:
    """Pure read-only long-term topic diversity & interest-narrowing observer."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def analyze(
        self,
        *,
        now: datetime | None = None,
        windows_hours: tuple[int, ...] = (6, 24, 168),
        custom_hours: int | None = None,
    ) -> dict[str, Any]:
        ref_now = _ensure_utc(now or utcnow()) or datetime.now(timezone.utc)
        window_specs: list[tuple[str, int]] = []
        label_map = {6: "6h", 24: "24h", 168: "7d"}
        for h in windows_hours:
            hh = max(1, int(h))
            window_specs.append((label_map.get(hh, f"{hh}h"), hh))
        if custom_hours is not None and int(custom_hours) > 0:
            ch = max(1, min(720, int(custom_hours)))
            lbl = f"custom_{ch}h"
            if all(h != ch for _, h in window_specs):
                window_specs.append((lbl, ch))

        async with self.db.session() as s:
            goal_rows = (
                await s.execute(select(ActiveGoal).order_by(desc(ActiveGoal.created_at)).limit(1000))
            ).scalars().all()
            mem_rows = (
                await s.execute(select(MemoryIndex).order_by(desc(MemoryIndex.created_at)).limit(1000))
            ).scalars().all()
            skill_rows = (
                await s.execute(select(SkillRegistryRow).order_by(desc(SkillRegistryRow.created_at)).limit(500))
            ).scalars().all()
            social_dec_rows = (
                await s.execute(
                    select(DecisionLog)
                    .where(DecisionLog.decision_type == "social_reflection")
                    .order_by(desc(DecisionLog.created_at))
                    .limit(500)
                )
            ).scalars().all()
            guidance_rows = (
                await s.execute(
                    select(WorldState)
                    .where(WorldState.kind == "human_guidance")
                    .order_by(desc(WorldState.created_at))
                    .limit(200)
                )
            ).scalars().all()

        # Pre-extract adopted dialogue goal hint tokens for matching goal origin cleanly
        adopted_hints: list[tuple[datetime, set[str]]] = []
        for dec in social_dec_rows:
            pl = dec.payload if isinstance(dec.payload, dict) else {}
            if pl.get("adopt_as_goal") and pl.get("adopted_goal_hint"):
                dt = _ensure_utc(dec.created_at)
                if dt is not None:
                    toks = set(_extract_meaningful_tokens(str(pl["adopted_goal_hint"])))
                    if toks:
                        adopted_hints.append((dt, toks))
        for gw in guidance_rows:
            val = gw.value if isinstance(gw.value, dict) else {}
            txt = str(val.get("text") or "")
            dt = _ensure_utc(gw.created_at)
            if dt is not None and txt:
                toks = set(_extract_meaningful_tokens(txt))
                if toks:
                    adopted_hints.append((dt, toks))

        # Sort goals ascending for streak calculations
        goals_norm: list[dict[str, Any]] = []
        for g in goal_rows:
            g_dt = _ensure_utc(g.created_at)
            if g_dt is None:
                continue
            topic = extract_topic_bucket(
                title=g.title or "",
                related_key=g.related_key,
                expected_outcome=g.expected_outcome if isinstance(g.expected_outcome, dict) else {},
            )
            rel_low = str(g.related_key or "").strip().lower()
            exp = g.expected_outcome if isinstance(g.expected_outcome, dict) else {}
            g_toks = set(_extract_meaningful_tokens(f"{g.title or ''} {g.description or ''}"))
            is_dialogue_origin = bool(
                rel_low.startswith(("tg:", "social:", "human:"))
                or exp.get("from_dialogue")
                or exp.get("social_adopted")
                or exp.get("human_guidance")
            )
            if not is_dialogue_origin and g_toks:
                for h_dt, h_toks in adopted_hints:
                    if abs((g_dt - h_dt).total_seconds()) <= 7200 and len(g_toks & h_toks) >= 2:
                        is_dialogue_origin = True
                        break
            goals_norm.append(
                {
                    "created_at": g_dt,
                    "drive_type": g.drive_type,
                    "topic": topic,
                    "dialogue_adopted": is_dialogue_origin,
                }
            )
        goals_norm.sort(key=lambda x: x["created_at"])

        mems_norm: list[dict[str, Any]] = []
        for m in mem_rows:
            m_dt = _ensure_utc(m.created_at)
            if m_dt is None:
                continue
            src = str(m.source or "").strip().lower()
            is_social = src == "dialogue:telegram" or src.startswith("dialogue:")
            m_topic = extract_topic_bucket(
                title=m.title or "",
                tags=list(m.tags) if isinstance(m.tags, list) else [],
                fallback=("social_dialogue" if is_social else "general"),
            )
            mems_norm.append({"created_at": m_dt, "is_social": is_social, "topic": m_topic})

        total_validated_skills = sum(1 for sk in skill_rows if sk.status == "VALIDATED")
        skills_norm: list[dict[str, Any]] = []
        for sk in skill_rows:
            s_dt = _ensure_utc(sk.created_at)
            if s_dt is None:
                continue
            skills_norm.append(
                {
                    "created_at": s_dt,
                    "capability_key": str(sk.capability_key or "general").strip().lower(),
                    "status": sk.status,
                }
            )

        windows_out: dict[str, Any] = {}
        for label, hours in window_specs:
            since = ref_now - timedelta(hours=hours)
            w_goals = [g for g in goals_norm if g["created_at"] >= since]
            topic_seq = [g["topic"] for g in w_goals]
            topic_counts = Counter(topic_seq)
            goal_count = len(w_goals)
            unique_count = len(topic_counts)
            entropy, norm_div = compute_shannon_entropy(dict(topic_counts))
            max_streak, streak_topic = compute_max_streak(topic_seq)
            top3_sum = sum(cnt for _, cnt in topic_counts.most_common(3))
            top3_ratio = round(top3_sum / goal_count, 4) if goal_count > 0 else 0.0
            novel_rate = round(unique_count / goal_count, 4) if goal_count > 0 else 0.0

            top_topics = [
                {
                    "topic": tp,
                    "count": cnt,
                    "share": round(cnt / goal_count, 4) if goal_count > 0 else 0.0,
                }
                for tp, cnt in topic_counts.most_common(8)
            ]
            drive_dist = dict(Counter(g["drive_type"] for g in w_goals))
            adopted_cnt = sum(1 for g in w_goals if g["dialogue_adopted"])
            self_cnt = goal_count - adopted_cnt

            w_mems = [m for m in mems_norm if m["created_at"] >= since]
            m_total = len(w_mems)
            m_social = sum(1 for m in w_mems if m["is_social"])
            m_tech = m_total - m_social
            m_top = [
                {"topic": tp, "count": cnt}
                for tp, cnt in Counter(m["topic"] for m in w_mems if not m["is_social"]).most_common(6)
            ]

            w_skills = [sk for sk in skills_norm if sk["created_at"] >= since]
            s_top = [
                {"capability": cap, "count": cnt}
                for cap, cnt in Counter(sk["capability_key"] for sk in w_skills).most_common(6)
            ]

            windows_out[label] = {
                "hours": hours,
                "since": _iso(since),
                "goal_count": goal_count,
                "unique_topic_count": unique_count,
                "shannon_entropy": entropy,
                "normalized_diversity": norm_div,
                "max_consecutive_same_topic_streak": max_streak,
                "streak_topic": streak_topic,
                "top3_concentration_ratio": top3_ratio,
                "novel_topic_rate": novel_rate,
                "top_topics": top_topics,
                "drive_distribution": drive_dist,
                "origin_breakdown": {
                    "self_driven_count": self_cnt,
                    "dialogue_adopted_count": adopted_cnt,
                    "self_driven_ratio": round(self_cnt / goal_count, 4) if goal_count > 0 else 0.0,
                    "dialogue_adopted_ratio": round(adopted_cnt / goal_count, 4) if goal_count > 0 else 0.0,
                },
                "memory_breakdown": {
                    "total_new": m_total,
                    "technical_or_research": m_tech,
                    "social_dialogue": m_social,
                    "social_ratio": round(m_social / m_total, 4) if m_total > 0 else 0.0,
                    "top_memory_topics": m_top,
                },
                "skill_activity": {
                    "new_skills_count": len(w_skills),
                    "total_validated_skills": total_validated_skills,
                    "top_capabilities": s_top,
                },
            }

        primary = windows_out.get("24h") or windows_out.get("7d") or next(iter(windows_out.values()))
        g_cnt = int(primary["goal_count"])
        streak = int(primary["max_consecutive_same_topic_streak"])
        top3 = float(primary["top3_concentration_ratio"])
        norm_d = float(primary["normalized_diversity"])

        if g_cnt < 3:
            pattern = "insufficient_data"
            note = f"近 {primary['hours']} 小时目标样本仅 {g_cnt} 条，暂不足以评估长期主题集中度。"
        elif streak >= 4 or (g_cnt >= 6 and top3 >= 0.85 and norm_d < 0.55):
            pattern = "narrowing"
            note = (
                f"检测到主题收敛/连续重复倾向：最大同主题连续 {streak} 次"
                + (f"（{primary['streak_topic']}）" if primary["streak_topic"] else "")
                + f"，Top-3 占比 {top3:.0%}，归一化多样性 {norm_d:.2f}。"
            )
        elif norm_d >= 0.75:
            pattern = "diverse"
            note = f"探索主题保持多元分布（归一化多样性 {norm_d:.2f}，共 {primary['unique_topic_count']} 个主题，未见长期卡死单一主题）。"
        else:
            pattern = "balanced"
            note = f"探索主题分布整体均衡（归一化多样性 {norm_d:.2f}，最大连续同主题 {streak} 次）。"

        return {
            "read_only": True,
            "generated_at": _iso(ref_now),
            "summary": {
                "reference_window": ("24h" if "24h" in windows_out else next(iter(windows_out.keys()))),
                "focus_pattern": pattern,
                "note": note,
                "goal_count": g_cnt,
                "unique_topic_count": primary["unique_topic_count"],
                "normalized_diversity": norm_d,
                "max_consecutive_same_topic_streak": streak,
                "top3_concentration_ratio": top3,
            },
            "windows": windows_out,
        }
