"""Deterministic Skill-First Pre-Selector (V5.3 M2).

Pure zero-LLM scoring engine that scores integrity-verified `VALIDATED` skills
against a proposed `GoalProposal` using:
  - 0.50 Bilingual (English + Chinese n-gram) lexical / scenario overlap
  - 0.30 Capability match + empirical reuse reliability
  - 0.20 Input JSON Schema bindability & parameter ergonomics

Injected into `CognitiveContext.top_skill_match` before `CognitionBrain.plan()` so
high-confidence matching skills cleanly steer execution toward `skill_first`.
"""

from __future__ import annotations

import re
from typing import Any, Sequence

from pydantic import BaseModel, Field

from app.drives.interfaces import clamp01

_STOPWORDS = frozenset(
    {
        "a", "an", "the", "and", "or", "of", "to", "in", "for", "with", "that", "by", "on", "at", "as",
        "from", "into", "via", "using", "use", "is", "are", "be", "this", "it", "its", "can", "will",
        "skill", "skills", "task", "goal", "validated", "execute", "run", "build", "create", "make",
        "report", "check", "tool", "utility", "script", "python",
    }
)
_EN_TOK_RE = re.compile(r"[a-z0-9]+")
_ZH_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")


def tokenize_bilingual(text: Any) -> set[str]:
    """Extract normalized English alnum tokens and Chinese 2-char bigrams for deterministic similarity."""
    raw = str(text or "").strip().lower()
    if not raw:
        return set()
    # Split camelCase / snake_case / dotted names cleanly before extracting English tokens
    norm = re.sub(r"([a-z])([A-Z])", r"\1 \2", raw).replace("_", " ").replace(".", " ").replace("-", " ")
    tokens: set[str] = set()
    for tok in _EN_TOK_RE.findall(norm):
        if len(tok) >= 2 and tok not in _STOPWORDS:
            tokens.add(tok)
    for run in _ZH_RUN_RE.findall(raw):
        if len(run) == 2:
            tokens.add(run)
        else:
            for i in range(len(run) - 1):
                tokens.add(run[i : i + 2])
    return tokens


def build_suggested_args_template(input_schema: dict[str, Any] | None) -> dict[str, Any]:
    """Generate a compact argument skeleton from a skill's JSON Schema."""
    if not isinstance(input_schema, dict):
        return {}
    props = input_schema.get("properties")
    if not isinstance(props, dict) or not props:
        return {}
    template: dict[str, Any] = {}
    for pname, pspec in list(props.items())[:8]:
        spec = pspec if isinstance(pspec, dict) else {}
        if "default" in spec:
            template[pname] = spec["default"]
        elif isinstance(spec.get("enum"), list) and spec["enum"]:
            template[pname] = spec["enum"][0]
        else:
            ptype = str(spec.get("type") or "any").strip().lower()
            template[pname] = f"<{ptype}>"
    return template


class SkillMatchCandidate(BaseModel):
    skill_id: str = ""
    skill_key: str
    canonical_name: str
    version: str = "1.0.0"
    match_score: float = 0.0
    lexical_score: float = 0.0
    capability_bonus: float = 0.0
    schema_score: float = 0.0
    permission_level: str = "L1"
    input_schema: dict[str, Any] = Field(default_factory=dict)
    suggested_args_template: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""


class SkillMatchPreSelector:
    """Deterministic scoring selector matching a GoalProposal against VALIDATED skills."""

    def score_skill(self, goal: Any, skill: dict[str, Any]) -> SkillMatchCandidate:
        if isinstance(goal, dict):
            g_title = str(goal.get("title") or "")
            g_desc = str(goal.get("description") or "")
            g_rel = str(goal.get("related_key") or "")
            g_crit = " ".join(str(x) for x in (goal.get("success_criteria") or []))
            g_drive = str(goal.get("drive_type") or "")
        else:
            g_title = str(getattr(goal, "title", "") or "")
            g_desc = str(getattr(goal, "description", "") or "")
            g_rel = str(getattr(goal, "related_key", "") or "")
            g_crit = " ".join(str(x) for x in (getattr(goal, "success_criteria", None) or []))
            g_drive = str(getattr(goal, "drive_type", "") or "")

        goal_blob = f"{g_title} {g_desc} {g_rel} {g_crit}".strip()
        g_toks = tokenize_bilingual(goal_blob)

        skey = str(skill.get("skill_key") or "").strip()
        cname = str(skill.get("canonical_name") or skey).strip()
        cap_key = str(skill.get("capability_key") or "").strip()
        scenarios = " ".join(str(x) for x in (skill.get("applicable_scenarios") or []))
        in_schema = skill.get("input_schema") if isinstance(skill.get("input_schema"), dict) else {}
        in_props = " ".join(str(k) for k in (in_schema.get("properties") or {}).keys())
        skill_blob = " ".join(
            [
                cname,
                skey,
                cap_key,
                str(skill.get("description") or ""),
                scenarios,
                in_props,
            ]
        )
        s_toks = tokenize_bilingual(skill_blob)

        # 1. Lexical & scenario overlap (weight 0.50)
        inter = g_toks & s_toks
        if inter:
            recall = len(inter) / max(1, min(len(g_toks), len(s_toks)))
            union = len(g_toks | s_toks)
            jacc = len(inter) / max(1, union)
            lex_score = clamp01(0.65 * recall + 0.35 * jacc)
        else:
            lex_score = 0.0

        low_goal = goal_blob.lower()
        if (skey and skey.lower() in low_goal) or (cname and cname.lower() in low_goal):
            lex_score = clamp01(lex_score + 0.15)

        # 2. Capability + empirical reuse reliability bonus (weight 0.30)
        cap_toks = tokenize_bilingual(cap_key)
        cap_hit = bool(cap_toks and (cap_toks & g_toks)) or (
            cap_key and g_drive and cap_key.lower() == g_drive.lower()
        )
        cap_component = 0.45 if cap_hit else 0.20

        reuse_cnt = int(skill.get("reuse_count") or 0)
        reuse_ok = int(skill.get("reuse_success_count") or 0)
        if reuse_cnt > 0:
            rel_rate = clamp01(reuse_ok / reuse_cnt)
            reliability_component = 0.55 * rel_rate
        else:
            # Neutral score for untested VALIDATED skills so reuse gets bootstrapped
            reliability_component = 0.35
        cap_bonus = clamp01(cap_component + reliability_component)

        # 3. Input schema bindability (weight 0.20)
        req_fields = list(in_schema.get("required") or [])
        props = in_schema.get("properties") if isinstance(in_schema.get("properties"), dict) else {}
        if len(req_fields) <= 3 and len(props) <= 6:
            schema_score = 1.0 if (in_schema or not req_fields) else 0.9
        elif len(req_fields) <= 6:
            schema_score = 0.7
        else:
            schema_score = 0.4

        # Zero lexical signal should not trigger a false positive from capability/schema priors alone
        raw_score = 0.50 * lex_score + 0.30 * cap_bonus + 0.20 * schema_score
        if lex_score <= 0.0:
            raw_score = min(raw_score, 0.25)
        match_score = round(clamp01(raw_score), 4)

        matched_preview = ", ".join(sorted(inter)[:5]) if inter else "none"
        reason = (
            f"tokens=[{matched_preview}] lex={lex_score:.2f} "
            f"cap_rel={cap_bonus:.2f} schema={schema_score:.2f}"
        )
        return SkillMatchCandidate(
            skill_id=str(skill.get("skill_id") or skill.get("id") or ""),
            skill_key=skey,
            canonical_name=cname,
            version=str(skill.get("version") or "1.0.0"),
            match_score=match_score,
            lexical_score=round(lex_score, 4),
            capability_bonus=round(cap_bonus, 4),
            schema_score=round(schema_score, 4),
            permission_level=str(skill.get("permission_level") or "L1"),
            input_schema=in_schema,
            suggested_args_template=build_suggested_args_template(in_schema),
            reason=reason,
        )

    def select_top_match(
        self,
        goal: Any,
        validated_skills: Sequence[dict[str, Any]],
        *,
        threshold: float = 0.55,
    ) -> SkillMatchCandidate | None:
        """Return highest-scoring VALIDATED skill candidate if score >= threshold, else None."""
        if not validated_skills:
            return None
        eff_thresh = clamp01(float(threshold))
        best: tuple[float, int, SkillMatchCandidate] | None = None
        for sk in validated_skills:
            if not isinstance(sk, dict):
                continue
            status = sk.get("status")
            if status is not None and str(status).upper() != "VALIDATED":
                continue
            cand = self.score_skill(goal, sk)
            if cand.match_score < eff_thresh:
                continue
            ok_cnt = int(sk.get("reuse_success_count") or 0)
            if best is None or (cand.match_score, ok_cnt) > (best[0], best[1]):
                best = (cand.match_score, ok_cnt, cand)
        return best[2] if best is not None else None
