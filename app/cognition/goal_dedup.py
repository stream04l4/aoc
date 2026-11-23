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
from app.perception.world.exploration import classify_exploration_scope

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
    if rel_norm.startswith("inv:"):
        toks = sorted(_canonical_tokens(f"{title} {description[:120]}"))[:12]
        canonical = f"{drive_norm}|inv:{rel_norm}|tok:{' '.join(toks)}"
    elif rel_norm:
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
        has_human_guidance: bool = False,
    ) -> GoalDedupDecision:
        now = now or utcnow()
        fp = compute_goal_fingerprint(
            drive_type=drive_type,
            title=proposal.title,
            related_key=proposal.related_key,
            description=proposal.description,
        )
        cand_rel = (proposal.related_key or "").strip().lower()
        is_inv = cand_rel.startswith("inv:")
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
            # Multi-hop investigation threads (`inv:...`) reuse one thread key across distinct hops;
            # never let one hop's related_key or cooldown block a genuinely distinct next hop.
            same_rel_blocks = same_rel and not is_inv

            # Always block concurrent overlap on an already-ACTIVE goal
            if (same_fp or same_rel) and g_status == "ACTIVE":
                code = "DUPLICATE_GOAL_FINGERPRINT" if same_fp else "DUPLICATE_RELATED_KEY"
                return GoalDedupDecision(
                    decision="REJECT",
                    fingerprint=fp,
                    reason_code=code,
                    duplicate_of=gid or None,
                    similarity=1.0,
                    cooldown_until=now + timedelta(minutes=self.failed_cooldown_minutes),
                    notes=[f"Goal overlaps already-ACTIVE goal {gid[:8]} ({code})"],
                )

            # 1. Active cooldown gate (bypassed when fresh human_guidance directs the organism)
            if not has_human_guidance and (same_fp or same_rel_blocks) and isinstance(g_cooldown, datetime) and g_cooldown > now:
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
            if not has_human_guidance and (same_fp or same_rel_blocks):
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
            # Also skip paraphrase blocking when fresh human_guidance is actively guiding retries.
            if has_human_guidance or g_drive != drive_type.lower():
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


class GoalCandidateScore(BaseModel):
    index: int
    source: str  # "primary" | "alternative"
    title: str
    related_key: str | None = None
    predicted_ig: float = 0.0
    dedup_decision: str = "ACCEPT"
    dedup_reason: str | None = None
    score: float = 0.0
    selected: bool = False


class GoalSelectionResult(BaseModel):
    selected_proposal: GoalProposal | None = None
    selected_dedup: GoalDedupDecision
    used_alternative: bool = False
    candidates: list[GoalCandidateScore] = Field(default_factory=list)


def select_best_goal_candidate(
    *,
    primary: GoalProposal,
    drive_type: str,
    recent_goals: list[dict[str, Any]],
    dedup_engine: GoalDedupEngine,
    now: datetime | None = None,
    affect_state: dict[str, Any] | None = None,
    prefer_external: bool = False,
    has_human_guidance: bool = False,
) -> GoalSelectionResult:
    """Evaluate primary + up to 2 alternative_goals through GoalDedupEngine and deterministically pick best.

    Eliminates unnecessary IDLE cycles when the primary goal hits dedup/cooldown by transparently
    scoring and selecting the highest-value novel backup candidate (all within ONE LLM call).
    """
    cand_proposals: list[tuple[str, GoalProposal]] = [("primary", primary)]
    for raw_alt in (primary.alternative_goals or [])[:2]:
        if not isinstance(raw_alt, dict):
            continue
        alt_title = str(raw_alt.get("title") or "").strip()
        if not alt_title:
            continue
        try:
            alt_pred_ig = float(raw_alt.get("predicted_ig", 0.35) or 0.35)
        except (TypeError, ValueError):
            alt_pred_ig = 0.35
        cand_proposals.append(
            (
                "alternative",
                GoalProposal(
                    should_act=True,
                    title=alt_title[:500],
                    description=str(raw_alt.get("description") or primary.description or "")[:1000],
                    drive_type=drive_type,
                    related_key=(str(raw_alt["related_key"]).strip() if raw_alt.get("related_key") else None),
                    success_criteria=list(raw_alt.get("success_criteria") or primary.success_criteria or []),
                    expected_outcome=dict(raw_alt.get("expected_outcome") or {}),
                    rationale=str(raw_alt.get("rationale") or "Alternative candidate fallback"),
                    predicted_ig=max(0.0, min(1.0, alt_pred_ig)),
                    hypothesis_to_test=raw_alt.get("hypothesis_to_test") if isinstance(raw_alt.get("hypothesis_to_test"), dict) else None,
                    discover_world_source=raw_alt.get("discover_world_source") if isinstance(raw_alt.get("discover_world_source"), dict) else None,
                    exploration_topic=raw_alt.get("exploration_topic") if isinstance(raw_alt.get("exploration_topic"), dict) else None,
                ),
            )
        )

    boredom = float((affect_state or {}).get("boredom", 0.0) or 0.0)
    frustration = float((affect_state or {}).get("frustration", 0.0) or 0.0)

    scored: list[tuple[float, int, str, GoalProposal, GoalDedupDecision]] = []
    audit_rows: list[GoalCandidateScore] = []
    first_dedup: GoalDedupDecision | None = None

    for idx, (src, prop) in enumerate(cand_proposals):
        dec = dedup_engine.evaluate(
            proposal=prop,
            drive_type=drive_type,
            recent_goals=recent_goals,
            now=now,
            has_human_guidance=has_human_guidance,
        )
        if first_dedup is None:
            first_dedup = dec

        pred_ig = max(0.0, min(1.0, float(prop.predicted_ig or 0.0)))
        if dec.decision == "ACCEPT":
            # Explainable deterministic utility score:
            # predicted_ig (0.50) + novelty (0.25) + specificity/hypothesis bonus (0.15) + primary tie-break (0.10)
            novelty_term = max(0.0, 1.0 - float(dec.similarity or 0.0))
            specificity = 0.10 if prop.related_key else 0.0
            if prop.hypothesis_to_test:
                specificity += 0.05
            # When bored, reward higher expected IG slightly more; when frustrated, slightly favor focused related_key goals
            affect_fit = 0.05 * boredom * pred_ig + (0.04 * frustration if prop.related_key else 0.0)
            primary_bonus = 0.10 if src == "primary" else 0.0
            external_bias = 0.0
            if prefer_external:
                scope = classify_exploration_scope(prop.title, prop.description, prop.related_key)
                external_bias = 0.25 if scope == "external_exploration" else -0.30
            score = max(
                0.0,
                round(0.50 * pred_ig + 0.25 * novelty_term + specificity + affect_fit + primary_bonus + external_bias, 4),
            )
            scored.append((score, -idx, src, prop, dec))
        else:
            score = 0.0

        audit_rows.append(
            GoalCandidateScore(
                index=idx,
                source=src,
                title=prop.title[:160],
                related_key=prop.related_key,
                predicted_ig=round(pred_ig, 4),
                dedup_decision=dec.decision,
                dedup_reason=dec.reason_code,
                score=score,
                selected=False,
            )
        )

    if scored:
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        _, neg_idx, best_src, best_prop, best_dec = scored[0]
        best_idx = -neg_idx
        audit_rows[best_idx].selected = True
        return GoalSelectionResult(
            selected_proposal=best_prop,
            selected_dedup=best_dec,
            used_alternative=(best_src == "alternative"),
            candidates=audit_rows,
        )

    assert first_dedup is not None
    return GoalSelectionResult(
        selected_proposal=None,
        selected_dedup=first_dedup,
        used_alternative=False,
        candidates=audit_rows,
    )
