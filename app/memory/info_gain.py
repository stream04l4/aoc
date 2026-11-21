"""Four-Factor Information Gain Evaluator (V2.0 P1-4, Task Book Section 5.4.3).

Prevents naive cosine-distance-as-knowledge-gain inflation:
  Semantic_Novelty = calibrated(1 - max_cosine_sim(new_item, top_k_known_same_model))
  Factual_Novelty  = dedup_and_contradiction_factor(new_facts vs existing_verified_facts)
  Evidence_Gain    = 0.35 if corroborates_existing_with_new_source else 0.0
  Information_Gain = clamp01(
      (0.7 * Semantic_Novelty * Factual_Novelty + 0.3 * Evidence_Gain)
      * Source_Reliability
      * Goal_Relevance
      * Verification_Confidence
      * Learning_Progress_Factor
  )
"""

from __future__ import annotations

import re
from typing import Sequence

from pydantic import BaseModel, Field

from app.drives.interfaces import clamp01
from app.memory.embedding import cosine_similarity

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]")


def _fact_tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall((text or "").lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


class ExistingMemorySample(BaseModel):
    memory_id: str
    content_hash: str = ""
    source: str = ""
    text: str = ""
    embedding: list[float] | None = None


class InfoGainBreakdown(BaseModel):
    information_gain: float
    semantic_novelty: float
    factual_novelty: float
    evidence_gain: float
    max_cosine_sim: float
    source_reliability: float
    goal_relevance: float
    verification_confidence: float
    learning_progress_factor: float
    is_duplicate: bool = False
    corroborated_new_source: bool = False
    notes: list[str] = Field(default_factory=list)


class InformationGainEvaluator:
    def __init__(
        self,
        *,
        sim_floor: float = 0.25,
        sim_ceil: float = 0.92,
        duplicate_sim_threshold: float = 0.92,
        corroboration_sim_min: float = 0.72,
        corroboration_bonus: float = 0.35,
    ) -> None:
        self.sim_floor = sim_floor
        self.sim_ceil = sim_ceil
        self.duplicate_sim_threshold = duplicate_sim_threshold
        self.corroboration_sim_min = corroboration_sim_min
        self.corroboration_bonus = corroboration_bonus

    def evaluate(
        self,
        *,
        new_text: str,
        new_facts: Sequence[str],
        new_source: str,
        new_content_hash: str = "",
        new_embedding: Sequence[float] | None = None,
        existing_memories: Sequence[ExistingMemorySample] = (),
        source_reliability: float = 0.8,
        goal_relevance: float = 1.0,
        verification_confidence: float = 1.0,
        learning_progress_factor: float = 1.0,
        topic_resolved: bool = False,
        new_capability_evidence: bool = False,
        verified_code_artifact: bool = False,
    ) -> InfoGainBreakdown:
        notes: list[str] = []
        src_rel = clamp01(source_reliability)
        goal_rel = clamp01(goal_relevance)
        ver_conf = clamp01(verification_confidence)
        learn_fac = clamp01(learning_progress_factor)

        # 1. Exact hash duplicate check
        if new_content_hash:
            for ex in existing_memories:
                if ex.content_hash and ex.content_hash == new_content_hash:
                    return InfoGainBreakdown(
                        information_gain=0.0,
                        semantic_novelty=0.0,
                        factual_novelty=0.0,
                        evidence_gain=0.0,
                        max_cosine_sim=1.0,
                        source_reliability=src_rel,
                        goal_relevance=goal_rel,
                        verification_confidence=ver_conf,
                        learning_progress_factor=learn_fac,
                        is_duplicate=True,
                        notes=["Exact content_hash duplicate suppressed"],
                    )

        # 2. Semantic novelty against known memories (same model/dimension strictly enforced by cosine_similarity)
        max_sim = 0.0
        best_match: ExistingMemorySample | None = None
        new_toks = _fact_tokens(new_text)

        for ex in existing_memories:
            if new_embedding is not None and ex.embedding is not None:
                sim = cosine_similarity(new_embedding, ex.embedding)
            else:
                sim = _jaccard(new_toks, _fact_tokens(ex.text))
            if sim > max_sim:
                max_sim = sim
                best_match = ex

        if max_sim >= self.sim_ceil:
            sem_novelty = 0.0
        elif max_sim <= self.sim_floor:
            sem_novelty = 1.0
        else:
            sem_novelty = clamp01((self.sim_ceil - max_sim) / max(1e-6, self.sim_ceil - self.sim_floor))

        # 3. Factual novelty across learned_facts vs existing verified texts
        facts = [f.strip() for f in new_facts if f and f.strip()]
        if not facts:
            facts = [new_text.strip()] if new_text.strip() else []
        existing_tok_sets = [_fact_tokens(ex.text) for ex in existing_memories if ex.text]

        if not facts:
            factual_novelty = 0.0
        elif not existing_tok_sets:
            factual_novelty = 1.0
        else:
            novel_scores: list[float] = []
            for f in facts:
                ft = _fact_tokens(f)
                best_overlap = max((_jaccard(ft, et) for et in existing_tok_sets), default=0.0)
                novel_scores.append(clamp01(1.0 - best_overlap))
            factual_novelty = clamp01(sum(novel_scores) / len(novel_scores))

        # 4. Corroboration evidence gain (distinct independent source corroborating an existing finding)
        corroborated = False
        evidence_gain = 0.0
        if (
            best_match is not None
            and max_sim >= self.corroboration_sim_min
            and new_source
            and best_match.source
            and new_source.strip().lower() != best_match.source.strip().lower()
        ):
            corroborated = True
            evidence_gain = self.corroboration_bonus
            notes.append(f"Corroborated existing memory from independent source '{new_source}' vs '{best_match.source}'")

        is_dup = max_sim >= self.duplicate_sim_threshold and not corroborated
        if is_dup:
            notes.append(f"Near-duplicate paraphrase suppressed (max_sim={max_sim:.3f})")

        raw_core = 0.7 * sem_novelty * factual_novelty + 0.3 * evidence_gain
        total = clamp01(raw_core * src_rel * goal_rel * ver_conf * learn_fac)

        # V3.0 M1 deterministic IG v2: concrete verifiable state deltas (strictly gated by ver_conf & non-duplicate)
        if not is_dup and ver_conf > 0.0:
            v2_bonus = 0.0
            if topic_resolved:
                v2_bonus += 0.15 * ver_conf
                notes.append("IG v2: verified open world topic resolution (+0.15)")
            if new_capability_evidence:
                v2_bonus += 0.12 * ver_conf
                notes.append("IG v2: new verified capability evidence (+0.12)")
            if verified_code_artifact:
                v2_bonus += 0.10 * ver_conf
                notes.append("IG v2: verified workspace artifact (+0.10)")
            if v2_bonus > 0.0:
                total = clamp01(total + v2_bonus)

        return InfoGainBreakdown(
            information_gain=round(total, 4),
            semantic_novelty=round(sem_novelty, 4),
            factual_novelty=round(factual_novelty, 4),
            evidence_gain=round(evidence_gain, 4),
            max_cosine_sim=round(max_sim, 4),
            source_reliability=round(src_rel, 4),
            goal_relevance=round(goal_rel, 4),
            verification_confidence=round(ver_conf, 4),
            learning_progress_factor=round(learn_fac, 4),
            is_duplicate=is_dup,
            corroborated_new_source=corroborated,
            notes=notes,
        )
