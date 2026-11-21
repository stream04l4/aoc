"""Deterministic Goal Deduplication & Cooldown Engine (V3.0 M1, P0-2).

Stops low-information-gain paraphrased goal loops inside the thin kernel without
requiring extra LLM calls:
  1. Canonical structural fingerprint (drive_type + normalized related_key / title tokens)
  2. Active cooldown gate (`cooldown_until > now`)
  3. Recent duplicate check (exact fingerprint, same related_key, or high lexical/semantic similarity)
  4. Outcome-driven cooldown backoff for failed or low-IG completed goals
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from typing import Any, Sequence

from pydantic import BaseModel, Field

from app.cognition.interfaces import GoalProposal
from app.memory.embedding import cosine_similarity
from app.perception.interfaces import utcnow

_WORD_RE = re.compile(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]")
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "for",
        "on",
        "with",
        "by",
        "at",
        "from",
        "into",
        "about",
        "check",
        "inspect",
        "analyze",
        "investigate",
        "explore",
        "review",
        "study",
        "research",
        "verify",
        "summarize",
        "的",
        "了",
        "和",
        "与",
        "对",
        "进行",
        "分析",
        "检查",
        "调研",
        "探索",
        "总结",
    }
)


def _canonical_tokens(text: str) -> set[str]:
    raw = [t.lower() for t in _WORD_RE.findall(text or "")]
    filtered = {t for t in raw if t not in _STOPWORDS and (len(t) > 1 or "\u4e00" <= t <= "\u9fff")}
    return filtered or set(raw)


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def compute_goal_fingerprint(
    *,
    drive_type: str,
    title: str,
    related_key: str | None = None,
    description: str = "",
) -> str:
    """Stable SHA-256 prefix fingerprint invariant to minor phrasing/whitespace differences."""
    drive_norm = (drive_type or "").strip().lower()
    rel_norm = re.sub(r"\s+", " ", (related_key or "").strip().lower())
    if rel_norm:
        canonical = f"{drive_norm}|key:{rel_norm}"
    else:
        toks = sorted(_canonical_tokens(f"{title} {description[:120]}"))[:16]
        canonical = f"{drive_norm}|tok:{' '.join(toks)}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def compute_cooldown_until(
    *,
    now: datetime,
    goal_achieved: bool,
    verified_ig: float,
    failure_streak: int = 0,
    failed_cooldown_minutes: int = 60,
    low_ig_cooldown_minutes: int = 120,
    low_ig_threshold: float = 0.15,
) -> datetime | None:
    """Compute deterministic cooldown horizon after a goal finishes or is rejected."""
    if not goal_achieved:
        mult = max(1, min(int(failure_streak or 1), 4))
        return now + timedelta(minutes=failed_cooldown_minutes * mult)
    if float(verified_ig) < float(low_ig_threshold):
        return now + timedelta(minutes=low_ig_cooldown_minutes)
    # High-IG achievement still gets a light 15-minute debounce to prevent immediate echo
    return now + timedelta(minutes=15)


class GoalDedupDecision(BaseModel):
    decision: str  # ACCEPT | DEFER | REJECT
    fingerprint: str
    reason_code: str | None = None
    duplicate_of: str | None = None
    similarity: float = 0.0
    cooldown_until: datetime | None = None
    notes: list[str] = Field(default_factory=list)


class GoalDedupEngine:
    """Deterministic goal deduplication & cooldown gate."""

    def __init__(
        self,
        *,
        dedup_window_hours: float = 6.0,
        lexical_threshold: float = 0.72,
        semantic_threshold: float = 0.86,
        failed_cooldown_minutes: int = 60,
        low_ig_cooldown_minutes: int = 120,
        low_ig_threshold: float = 0.15,
    ) -> None:
        self.dedup_window_hours = float(dedup_window_hours)
        self.lexical_threshold = float(lexical_threshold)
        self.semantic_threshold = float(semantic_threshold)
        self.failed_cooldown_minutes = int(failed_cooldown_minutes)
        self.low_ig_cooldown_minutes = int(low_ig_cooldown_minutes)
        self.low_ig_threshold = float(low_ig_threshold)

    def evaluate(
        self,
        *,
        proposal: GoalProposal,
        drive_type: str,
        recent_goals: Sequence[dict[str, Any]],
        now: datetime | None = None,
        candidate_embedding: Sequence[float] | None = None,
        recent_embeddings: dict[str, Sequence[float]] | None = None,
    ) -> GoalDedupDecision:
        now = now or utcnow()
        fp = compute_goal_fingerprint(
            drive_type=drive_type,
            title=proposal.title,
            related_key=proposal.related_key,
            description=proposal.description,
        )
        cand_rel = (proposal.related_key or "").strip().lower()
        cand_toks = _canonical_tokens(f"{proposal.title} {proposal.description[:160]}")
        window_cutoff = now - timedelta(hours=self.dedup_window_hours)
        emb_map = recent_embeddings or {}

        # Count recent failures for same fingerprint/key to scale backoff if needed
        best_dup: dict[str, Any] | None = None
        best_sim = 0.0
        best_code: str | None = None

        for g in recent_goals:
            gid = str(g.get("goal_id") or g.get("id") or "")
            g_drive = str(g.get("drive_type") or "").strip().lower()
            g_fp = str(g.get("goal_fingerprint") or "") or compute_goal_fingerprint(
                drive_type=g_drive,
                title=str(g.get("title") or ""),
                related_key=g.get("related_key"),
                description=str(g.get("description") or ""),
            )
            g_rel = str(g.get("related_key") or "").strip().lower()
            g_status = str(g.get("status") or "").upper()
            g_created = g.get("created_at")
            g_cooldown = g.get("cooldown_until")
            g_ver_ig = g.get("verified_ig")

            # Normalize naive datetimes from SQLite tests cleanly
            if isinstance(g_cooldown, datetime) and g_cooldown.tzinfo is None and now.tzinfo is not None:
                g_cooldown = g_cooldown.replace(tzinfo=now.tzinfo)
            if isinstance(g_created, datetime) and g_created.tzinfo is None and now.tzinfo is not None:
                g_created = g_created.replace(tzinfo=now.tzinfo)

            same_fp = (g_fp == fp)
            same_rel = bool(cand_rel and g_rel and cand_rel == g_rel and g_drive == drive_type.lower())

            # 1. Active cooldown gate (highest priority)
            if (same_fp or same_rel) and isinstance(g_cooldown, datetime) and g_cooldown > now:
                return GoalDedupDecision(
                    decision="DEFER",
                    fingerprint=fp,
                    reason_code="GOAL_IN_COOLDOWN",
                    duplicate_of=gid or None,
                    similarity=1.0,
                    cooldown_until=g_cooldown,
                    notes=[f"Goal matches {gid[:8]} which is in cooldown until {g_cooldown.isoformat()}"],
                )

            # Only compare against recent window for non-cooldown dedup
            in_window = (not isinstance(g_created, datetime)) or (g_created >= window_cutoff)
            if not in_window:
                continue

            # 2. Exact fingerprint or related_key repetition inside window
            if same_fp or same_rel:
                code = "DUPLICATE_GOAL_FINGERPRINT" if same_fp else "DUPLICATE_RELATED_KEY"
                # If already active/in-progress, or already failed/rejected, or achieved with low IG -> block!
                is_low_ig = (g_ver_ig is not None) and (float(g_ver_ig) < self.low_ig_threshold)
                if g_status in {"ACTIVE", "FAILED", "REJECTED", "ABANDONED"} or is_low_ig or g_status == "ACHIEVED":
                    cd = (
                        now + timedelta(minutes=self.low_ig_cooldown_minutes)
                        if is_low_ig
                        else now + timedelta(minutes=self.failed_cooldown_minutes)
                    )
                    return GoalDedupDecision(
                        decision="REJECT",
                        fingerprint=fp,
                        reason_code=code,
                        duplicate_of=gid or None,
                        similarity=1.0,
                        cooldown_until=cd,
                        notes=[
                            f"Repeated goal ({code}) within {self.dedup_window_hours}h window "
                            f"(prior status={g_status}, verified_ig={g_ver_ig})"
                        ],
                    )

            # 3. Lexical / Semantic paraphrase detection (same drive)
            # If BOTH goals specify explicit, distinct related_keys (e.g., "t-0" vs "t-1"),
            # they target different concrete entities/topics — never false-positive on templated titles!
            if g_drive != drive_type.lower():
                continue
            if cand_rel and g_rel and cand_rel != g_rel:
                continue

            sim = 0.0
            code = "DUPLICATE_GOAL_LEXICAL"
            if candidate_embedding is not None and gid in emb_map:
                sim = cosine_similarity(candidate_embedding, emb_map[gid])
                if sim >= self.semantic_threshold:
                    code = "DUPLICATE_GOAL_SEMANTIC"
                else:
                    sim = 0.0
            if sim == 0.0:
                g_toks = _canonical_tokens(f"{g.get('title', '')} {str(g.get('description', ''))[:160]}")
                lex_sim = _jaccard(cand_toks, g_toks)
                if lex_sim >= self.lexical_threshold:
                    sim = lex_sim
                    code = "DUPLICATE_GOAL_LEXICAL"

            if sim > best_sim:
                best_sim = sim
                best_dup = g
                best_code = code

        if best_dup is not None and best_code is not None:
            gid = str(best_dup.get("goal_id") or best_dup.get("id") or "")
            cd = now + timedelta(minutes=self.low_ig_cooldown_minutes)
            return GoalDedupDecision(
                decision="REJECT",
                fingerprint=fp,
                reason_code=best_code,
                duplicate_of=gid or None,
                similarity=round(best_sim, 4),
                cooldown_until=cd,
                notes=[
                    f"Paraphrase duplicate detected ({best_code}, sim={best_sim:.3f}) "
                    f"matching prior goal '{str(best_dup.get('title', ''))[:60]}'"
                ],
            )

        return GoalDedupDecision(
            decision="ACCEPT",
            fingerprint=fp,
            reason_code=None,
            duplicate_of=None,
            similarity=0.0,
            cooldown_until=None,
            notes=["Goal is novel and outside cooldown windows"],
        )
