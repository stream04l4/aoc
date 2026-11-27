"""Curiosity External Exploration Lifecycle, Interest Portfolio & Fatigue Guard (V4.0 M3 + V5.2 M2).

Reuses `world_states(kind='exploration_topic')` and `world_states(kind='interest_portfolio')`
(0 new tables) to prevent Curiosity from getting trapped in repetitive internal self-diagnostics
or `security_audit` comfort-zone loops, while persistently advancing creator-seeded and
self-seeded cross-domain exploration interests.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from app.observability.logging import get_logger
from app.observability.topic_diversity import extract_topic_bucket
from app.perception.interfaces import utcnow
from app.storage.repository import StateRepository

logger = get_logger("aoc.perception.world.exploration")

_INTERNAL_DIAG_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(curiosity[_\s-]?gap|gap[_\s-]?ledger|provenance[_\s-]?checksum|self[_\s-]?model|"
        r"skill[_\s-]?artifact|aoc[_\s-]?diagnostic|snapshot[_\s-]?diagnostic|agent[_\s-]?redundancy)\b",
        re.I,
    ),
    re.compile(
        r"\b(stdlib[_\s-]?only[_\s-]?l1|reconciliation[_\s-]?diagnostic|internal[_\s-]?audit|inventory[_\s-]?assessment)\b",
        re.I,
    ),
    re.compile(r"(自查|台账|内省诊断|内部审计|自检脚本|好奇心缺口台账)"),
)

_EXTERNAL_SIGNALS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(arxiv|paper|github|release|pgvector|letta|postgres|postgresql|python|rust|mcp|llm|"
        r"agent|benchmark|rag|hnsw|onnx|linux|systemd|docker|kubernetes|security|cve|research|"
        r"psychology|cognition|neuroscience|sport|sports|fitness|biology|physics|history|philosophy|economics)\b",
        re.I,
    ),
    re.compile(r"(外部|论文|开源|框架|前沿|生态|调研|新技术|版本发布|心理学|认知|运动|体育|神经科学|生物|物理|历史|哲学|经济|科普|书评|AI热点)"),
)

EXPLORATION_DOMAIN_SPECTRUM: tuple[str, ...] = (
    "ai_industry",
    "psychology_cognition",
    "sports_health",
    "science_general",
    "humanities_society",
    "engineering_tools",
    "databases",
    "agent_memory",
)

_DOMAIN_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "security_audit",
        re.compile(
            r"(?:"
            r"\b(?:cve|cwe|xss|ssrf|csrf|sqli|rce|owasp|pentest|vulnerability|vulnerabilities|"
            r"exploit|malware|sandbox\s+escape|privilege\s+escalation|prompt\s+injection|"
            r"security\s+audit|checksum|hardening|zero[-\s]?day|firewall|seccomp|apparmor|security)\b"
            r"|安全|漏洞|渗透|攻击|提权|安全审计|校验和|防护|沙箱逃逸|越权|注入防护|风控|加密"
            r")",
            re.I,
        ),
    ),
    (
        "psychology_cognition",
        re.compile(
            r"(?:"
            r"\b(?:psychology|cognitive\s+bias|cognition|neuroscience|behavioral\s+economics?|habit\s+formation|"
            r"motivation|attention\s+span|burnout|sleep\s+deprivation|sleep\s+quality|decision[-\s]?making|"
            r"affective|mindfulness|mental\s+health|metacognition|flow\s+state)\b"
            r"|心理学?|认知偏差|认知科学|神经科学|行为经济|专注力|注意力|情绪管理|睡眠|心流|决策偏差|动机|冥想|焦虑|拖延"
            r")",
            re.I,
        ),
    ),
    (
        "sports_health",
        re.compile(
            r"(?:"
            r"\b(?:sport|sports|fitness|workout|running|marathon|cycling|swimming|"
            r"strength\s+training|hypertrophy|vo2|vo2max|cardio|nutrition|diet|longevity|"
            r"athletic|nba|football|soccer|tennis|badminton|biomechanics|exercise|public\s+health)\b"
            r"|运动|健身|跑步|马拉松|力量训练|有氧|无氧|体能|营养学|运动健康|健康饮食|篮球|足球|羽毛球|网球|骑行|游泳|恢复训练|拉伸|体育"
            r")",
            re.I,
        ),
    ),
    (
        "humanities_society",
        re.compile(
            r"(?:"
            r"\b(?:world\s+history|cultural\s+history|philosophy|sociology|economics|literature|culture|"
            r"art\s+history|music\s+theory|cinema|film|urban\s+architecture|architectural\s+history|ethics|geopolitics|"
            r"education|anthropology|book\s+review)\b"
            r"|历史学|世界历史|哲学|社会学|经济学|宏观经济|微观经济|文学|文化|艺术|音乐|电影|建筑美学|古典建筑|伦理|教育|人类学|书评|人文|传媒"
            r")",
            re.I,
        ),
    ),
    (
        "science_general",
        re.compile(
            r"(?:"
            r"\b(?:physics|quantum|biology|biotech|genetics|crispr|chemistry|astronomy|"
            r"astrophysics|climate\s+change|fusion\s+energy|battery|materials\s+science|ecology|"
            r"medicine|mathematics)\b"
            r"|物理|量子|生物|基因|化学|天文|航天|气候变化|能源|核聚变|电池|材料科学|生态学|生态系统|医学|数学|科普"
            r")",
            re.I,
        ),
    ),
    (
        "databases",
        re.compile(
            r"(?:"
            r"\b(?:pgvector|postgres|postgresql|sqlite|mysql|duckdb|clickhouse|sql|"
            r"hnsw|ivfflat|database|databases|mvcc|wal)\b"
            r"|数据库|向量检索|数据库索引|事务"
            r")",
            re.I,
        ),
    ),
    (
        "agent_memory",
        re.compile(
            r"(?:"
            r"\b(?:letta|memgpt|rag|retrieval|embedding|embeddings|onnx|vector\s+store|"
            r"long[-\s]?term\s+memory|episodic\s+memory|semantic\s+memory|knowledge\s+graph)\b"
            r"|长期记忆|情景记忆|向量嵌入|知识图谱|记忆检索"
            r")",
            re.I,
        ),
    ),
    (
        "ai_industry",
        re.compile(
            r"(?:"
            r"\b(?:arxiv|llm|llms|vlm|transformer|reasoning|deepseek(?:-r1|-v3)?|qwen3?|openai|anthropic|"
            r"claude|gemini|llama|mistral|benchmark|benchmarks|fine[-\s]?tuning|post[-\s]?training|"
            r"rlhf|grpo|inference|vllm|sglang|quantization|fp8|multimodal|ai\s+agent|"
            r"ai\s+industry|open[-\s]?weights|foundation\s+model)\b"
            r"|大模型|人工智能|推理模型|多模态|微调|量化|算力|ai热点|ai动态|ai行业|前沿模型|开源模型|智能体趋势|推理生态"
            r")",
            re.I,
        ),
    ),
    (
        "engineering_tools",
        re.compile(
            r"(?:"
            r"\b(?:mcp|codex|playwright|lightpanda|headless\s+browser|zig|rust|golang|"
            r"python|typescript|docker|kubernetes|systemd|cgroup|linux|kernel|compiler|"
            r"cli|devtools|skill|skills|workflow|git|ci/cd|performance|observability|sandbox|architecture)\b"
            r"|无头浏览器|工程工具|编译器|容器|开发工具|框架对比|架构|性能优化|开源工具|命令行"
            r")",
            re.I,
        ),
    ),
)

_NON_SUBSTANTIVE_ACK_RE = re.compile(
    r"^(?:好的|好|行|收到|ok|yes|no|嗯|嗯嗯|同意|确认|继续|暂停|停止|取消|批准|拒绝|谢谢|thanks)[!！。.，,\s]*$",
    re.I,
)
_PORTFOLIO_STOPWORDS = frozenset(
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
        "study",
        "explore",
        "research",
        "learn",
        "read",
        "check",
        "analyze",
        "report",
        "joel",
        "的研究",
        "调研",
        "了解",
        "研究",
        "探索",
        "学习",
        "看看",
        "整理",
        "简报",
        "关系",
        "最近",
        "近期",
    }
)
_LATIN_TOK_RE = re.compile(r"[a-z0-9_]+")
_CJK_SEG_RE = re.compile(r"[\u4e00-\u9fff]+")


def _parse_iso_dt(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=timezone.utc) if raw.tzinfo is None else raw.astimezone(timezone.utc)
    txt = str(raw or "").strip()
    if not txt:
        return None
    try:
        dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _portfolio_tokens(text: str) -> set[str]:
    low = (text or "").lower()
    toks: set[str] = set()
    for w in _LATIN_TOK_RE.findall(low):
        cw = w.strip("_")
        if len(cw) >= 3 and cw not in _PORTFOLIO_STOPWORDS:
            toks.add(cw)
    for seg in _CJK_SEG_RE.findall(low):
        clean = seg
        for filler in ("关于", "我想", "帮我", "研究一下", "调研一下", "了解一下", "关注", "最近", "近期", "关系", "对比", "整理", "简报"):
            clean = clean.replace(filler, " ")
        for piece in clean.split():
            if len(piece) == 1:
                continue
            if len(piece) == 2:
                toks.add(piece)
            else:
                for i in range(len(piece) - 1):
                    bg = piece[i : i + 2]
                    if bg not in _PORTFOLIO_STOPWORDS:
                        toks.add(bg)
    return toks


def is_substantive_interest_guidance(text: str) -> bool:
    """Filter out short/control acknowledgments before persisting into `interest_portfolio`."""
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(clean) < 6:
        return False
    if _NON_SUBSTANTIVE_ACK_RE.match(clean):
        return False
    return True


def classify_exploration_scope(title: str, description: str = "", related_key: str | None = None) -> str:
    """Deterministically classify a goal/topic as `internal_self_check` vs `external_exploration`."""
    blob = f"{title or ''} {description or ''} {related_key or ''}".strip()
    if any(p.search(blob) for p in _INTERNAL_DIAG_PATTERNS):
        return "internal_self_check"
    if any(p.search(blob) for p in _EXTERNAL_SIGNALS):
        return "external_exploration"
    if str(related_key or "").startswith("world:"):
        return "external_exploration"
    return "external_exploration"


_COMFORT_ZONE_STEMS: tuple[str, ...] = (
    "security",
    "audit",
    "safety",
    "provenance",
    "guardrail",
    "triage",
    "compliance",
)

_TOPIC_SIG_STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "into",
        "via",
        "to",
        "of",
        "in",
        "on",
        "or",
        "as",
        "an",
        "by",
        "close",
        "closed",
        "closure",
        "verify",
        "verification",
        "check",
        "checks",
        "read",
        "readonly",
        "only",
        "public",
        "evidence",
        "bounded",
        "authoritative",
        "fail",
        "failed",
        "final",
        "hop",
        "hops",
        "topic",
        "open",
        "unknown",
        "using",
        "complete",
        "deterministic",
        "drift",
        "reconcile",
        "triage",
        "assess",
        "impact",
        "identify",
        "fetch",
        "fetched",
        "kernel",
        "prefetched",
        "untried",
        "leads",
        "quarantined",
        "trusted",
        "smaller",
        "page",
        "updated",
        "endpoint",
        "range",
        "delta",
        "note",
        "brief",
        "report",
        "pivot",
        "curiosity",
        "raw",
        "snapshot",
        "api",
        "non",
        "truncated",
        "compare",
        "classification",
        "classify",
        "generalize",
        "generalized",
        "skill",
        "external",
        "inv",
        "world",
        "web",
        "rss",
        "git",
        "http",
        "https",
        "公开",
        "只读",
        "证据",
        "关闭",
        "失败",
        "核对",
        "验证",
        "检查",
        "调查",
        "漂移",
        "关于",
        "进行",
        "研究",
        "分析",
        "调研",
        "探索",
        "整理",
    }
)


def _is_comfort_zone_domain(domain: str) -> bool:
    dom = str(domain or "").strip().lower()
    if not dom:
        return False
    if dom == "security_audit":
        return True
    return any(stem in dom for stem in _COMFORT_ZONE_STEMS)


def extract_topic_signature_tokens(title: str, related_key: str | None = None) -> frozenset[str]:
    """Extract normalized entity/topic signature tokens invariant to title & related_key paraphrasing."""
    rel = str(related_key or "").strip().lower()
    rel = re.sub(r"^(?:world:(?:rss-|git-|web-)?|inv:|sec:|tg:|social:|human:)", "", rel)
    raw = f"{title or ''} {rel}".lower()
    # Normalize thousands-separated integers like 5,035 -> 5035 before tokenization
    raw = re.sub(r"(?<=\d),(?=\d)", "", raw)
    raw = re.sub(r"[_:/\\-]+|->|→", " ", raw)

    toks: set[str] = set()
    for w in re.findall(r"[a-z0-9]+", raw):
        if len(w) >= 2 and w not in _TOPIC_SIG_STOPWORDS:
            toks.add(w)
    for seg in _CJK_SEG_RE.findall(raw):
        cur = seg
        for filler in ("公开只读", "失败关闭", "或失败", "关于", "关闭", "失败", "公开", "只读", "证据", "核对", "验证", "检查", "调查", "漂移", "研究", "分析", "调研", "探索"):
            cur = cur.replace(filler, " ")
        for piece in cur.split():
            p = piece.strip()
            if len(p) == 2 and p not in _TOPIC_SIG_STOPWORDS:
                toks.add(p)
            elif len(p) > 2:
                for i in range(len(p) - 1):
                    bg = p[i : i + 2]
                    if bg not in _TOPIC_SIG_STOPWORDS:
                        toks.add(bg)
    return frozenset(toks)


def topic_tokens_match(a: frozenset[str], b: frozenset[str]) -> bool:
    """Return True when two topic signatures clearly refer to the same underlying topic."""
    if not a or not b:
        return False
    inter = len(a & b)
    union = len(a | b)
    jacc = (inter / union) if union else 0.0
    if min(len(a), len(b)) <= 3:
        return (inter >= 3) or (inter >= 2 and jacc >= 0.65)
    return inter >= 3 or jacc >= 0.35


def infer_topic_domain(title: str, description: str = "", explicit_domain: str | None = None) -> str:
    """Deterministically classify text into the 10-domain exploration taxonomy."""
    if explicit_domain and str(explicit_domain).strip():
        slug = re.sub(r"[^a-z0-9_]+", "_", str(explicit_domain).strip().lower()).strip("_")[:32]
        return slug or "general_tech"
    blob = f"{title or ''} {description or ''}".strip()
    for dom, pat in _DOMAIN_RULES:
        if pat.search(blob):
            return dom
    return "general_tech"


def match_interest_portfolio_item(
    items: Sequence[dict[str, Any]],
    *,
    title: str,
    description: str = "",
    related_key: str | None = None,
    portfolio_key: str | None = None,
    domain: str | None = None,
) -> dict[str, Any] | None:
    """Match a goal proposal/record against active `interest_portfolio` entries deterministically."""
    if not items:
        return None
    normalized_items = [dict(it) for it in items if isinstance(it, dict)]
    if not normalized_items:
        return None

    # Sort candidates so human_seeded, higher priority, and least-touched win ties cleanly.
    ordered = sorted(
        normalized_items,
        key=lambda it: (
            0 if str(it.get("origin") or "") == "human_seeded" else 1,
            -float(it.get("priority", 0.5) or 0.5),
            int(it.get("touched_count", 0) or 0),
        ),
    )

    pkey = str(portfolio_key or "").strip().lower()
    if pkey:
        for it in ordered:
            k = str(it.get("key") or it.get("topic_key") or "").strip().lower()
            tk = str(it.get("topic_key") or "").strip().lower()
            if pkey in {k, tk}:
                return it

    rkey = str(related_key or "").strip().lower()
    if rkey:
        for it in ordered:
            k = str(it.get("key") or it.get("topic_key") or "").strip().lower()
            tk = str(it.get("topic_key") or "").strip().lower()
            if rkey in {k, tk}:
                return it

    q_toks = _portfolio_tokens(f"{title} {description}")
    if q_toks:
        best_lex: tuple[int, dict[str, Any]] | None = None
        for it in ordered:
            it_toks = _portfolio_tokens(f"{it.get('prompt') or ''} {it.get('topic_key') or ''}")
            overlap = len(q_toks & it_toks)
            if overlap >= 2 and (best_lex is None or overlap > best_lex[0]):
                best_lex = (overlap, it)
        if best_lex is not None:
            return best_lex[1]

    goal_dom = infer_topic_domain(title, description, explicit_domain=domain)
    if goal_dom not in {"general_tech", "security_audit"}:
        for it in ordered:
            it_dom = infer_topic_domain(
                str(it.get("prompt") or ""),
                explicit_domain=it.get("domain"),
            )
            if it_dom == goal_dom:
                return it

    return None


class ExplorationManager:
    """Tracks external exploration topics, persistent interest portfolio, and domain-fatigue quotas."""

    def __init__(
        self,
        *,
        repo: StateRepository,
        max_internal_curiosity_ratio: float = 0.45,
        lookback_goals: int = 10,
        interest_portfolio_enabled: bool = False,
        max_same_domain_streak: int = 3,
        max_same_topic_streak: int = 3,
        portfolio_starvation_cycles: int = 6,
    ) -> None:
        self.repo = repo
        self.max_internal_curiosity_ratio = max(0.1, min(0.9, float(max_internal_curiosity_ratio)))
        self.lookback_goals = max(3, int(lookback_goals))
        self.interest_portfolio_enabled = bool(interest_portfolio_enabled)
        self.max_same_domain_streak = max(2, int(max_same_domain_streak))
        self.max_same_topic_streak = max(2, int(max_same_topic_streak))
        self.portfolio_starvation_cycles = max(2, int(portfolio_starvation_cycles))

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

    async def upsert_interest_item(
        self,
        *,
        prompt: str,
        topic_key: str | None = None,
        domain: str | None = None,
        origin: str = "human_seeded",
        priority: float | None = None,
        status: str = "active",
        source: str = "human_guidance",
        sender: str | None = None,
    ) -> dict[str, Any]:
        """Persist or refresh an interest item in `world_states(kind='interest_portfolio')`."""
        clean_prompt = re.sub(r"\s+", " ", str(prompt or "")).strip()[:500]
        if not clean_prompt:
            raise ValueError("Interest portfolio prompt must not be empty")

        norm_origin = "human_seeded" if str(origin or "").strip().lower() == "human_seeded" else "self_seeded"
        eff_domain = infer_topic_domain(clean_prompt, explicit_domain=domain)
        eff_priority = (
            max(0.05, min(1.0, float(priority)))
            if priority is not None
            else (0.90 if norm_origin == "human_seeded" else 0.60)
        )
        eff_status = str(status or "active").strip().lower() or "active"
        if eff_status not in {"active", "paused", "explored"}:
            eff_status = "active"

        ascii_hint = re.sub(r"[^a-z0-9_]+", "_", str(topic_key or clean_prompt).lower()).strip("_")[:36]
        digest = hashlib.sha1(clean_prompt.lower().encode("utf-8")).hexdigest()[:10]
        origin_pfx = "human" if norm_origin == "human_seeded" else "self"
        default_slug = (
            f"{origin_pfx}:{eff_domain}:{ascii_hint}:{digest}"
            if ascii_hint
            else f"{origin_pfx}:{eff_domain}:{digest}"
        )[:110]
        if topic_key:
            explicit_slug = re.sub(r"[^a-z0-9_:-]+", "_", str(topic_key).strip().lower()).strip("_")[:110]
            slug = explicit_slug or default_slug
        else:
            slug = default_slug

        existing = await self.repo.list_world_items(kind="interest_portfolio")
        matched_row: dict[str, Any] | None = None
        new_toks = _portfolio_tokens(clean_prompt)
        for row in existing:
            rkey = str(row.get("key") or "")
            rval = row.get("value") if isinstance(row.get("value"), dict) else {}
            rprompt = re.sub(r"\s+", " ", str(rval.get("prompt") or "")).strip()
            if rkey == slug or (rprompt and rprompt.lower() == clean_prompt.lower()):
                matched_row = row
                break
            rtoks = _portfolio_tokens(rprompt)
            if new_toks and rtoks:
                union = new_toks | rtoks
                jacc = len(new_toks & rtoks) / len(union) if union else 0.0
                if jacc >= 0.65:
                    matched_row = row
                    break

        now_iso = utcnow().isoformat()
        if matched_row is not None:
            slug = str(matched_row.get("key") or slug)
            prev_val = dict(matched_row.get("value") or {}) if isinstance(matched_row.get("value"), dict) else {}
        else:
            prev_val = {}

        val = {
            "topic_key": str(prev_val.get("topic_key") or slug),
            "domain": eff_domain if eff_domain != "general_tech" or not prev_val.get("domain") else prev_val["domain"],
            "prompt": clean_prompt,
            "origin": (
                "human_seeded"
                if norm_origin == "human_seeded" or prev_val.get("origin") == "human_seeded"
                else norm_origin
            ),
            "priority": round(max(float(prev_val.get("priority", 0.0) or 0.0), eff_priority), 3),
            "status": eff_status,
            "touched_count": int(prev_val.get("touched_count") or 0),
            "last_explored_at": prev_val.get("last_explored_at"),
            "created_at": prev_val.get("created_at") or now_iso,
            "sender": sender or prev_val.get("sender"),
        }
        await self.repo.upsert_world_item(
            kind="interest_portfolio",
            key=slug,
            value=val,
            status=eff_status,
            source=source,
        )
        return {"key": slug, **val}

    async def list_interest_portfolio(
        self,
        *,
        statuses: Sequence[str] | None = ("active",),
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Return normalized portfolio items sorted by human-seeding, priority, and staleness."""
        allowed_statuses = {str(s).strip().lower() for s in (statuses or ())}
        rows = await self.repo.list_world_items(kind="interest_portfolio")
        normalized: list[dict[str, Any]] = []
        for row in rows:
            val = row.get("value") if isinstance(row.get("value"), dict) else {}
            r_status = str(val.get("status") or row.get("status") or "active").strip().lower()
            if allowed_statuses and r_status not in allowed_statuses:
                continue
            prompt = str(val.get("prompt") or "").strip()
            if not prompt:
                continue
            origin = "human_seeded" if str(val.get("origin") or "") == "human_seeded" else "self_seeded"
            domain = infer_topic_domain(prompt, explicit_domain=val.get("domain"))
            normalized.append(
                {
                    "key": str(row.get("key") or val.get("topic_key") or ""),
                    "topic_key": str(val.get("topic_key") or row.get("key") or ""),
                    "domain": domain,
                    "prompt": prompt,
                    "origin": origin,
                    "priority": round(float(val.get("priority", 0.5) or 0.5), 3),
                    "status": r_status,
                    "touched_count": int(val.get("touched_count") or 0),
                    "last_explored_at": val.get("last_explored_at"),
                    "created_at": val.get("created_at") or str(row.get("created_at") or ""),
                    "sender": val.get("sender"),
                }
            )

        normalized.sort(
            key=lambda it: (
                0 if it["origin"] == "human_seeded" else 1,
                -float(it["priority"]),
                0 if it["touched_count"] == 0 else 1,
                str(it.get("last_explored_at") or ""),
                int(it["touched_count"]),
            )
        )
        return normalized[: max(1, int(limit))]

    async def mark_interest_explored(
        self,
        *,
        goal_title: str,
        goal_description: str = "",
        related_key: str | None = None,
        portfolio_key: str | None = None,
        domain: str | None = None,
    ) -> dict[str, Any] | None:
        """Update `touched_count` & `last_explored_at` when a goal aligned with `interest_portfolio` succeeds."""
        active_items = await self.list_interest_portfolio(statuses=("active",), limit=20)
        matched = match_interest_portfolio_item(
            active_items,
            title=goal_title,
            description=goal_description,
            related_key=related_key,
            portfolio_key=portfolio_key,
            domain=domain,
        )
        if matched is None:
            return None

        key = str(matched["key"])
        rows = await self.repo.list_world_items(kind="interest_portfolio")
        raw_row = next((r for r in rows if str(r.get("key") or "") == key), None)
        val = dict(raw_row.get("value") or {}) if raw_row and isinstance(raw_row.get("value"), dict) else dict(matched)

        now_iso = utcnow().isoformat()
        touched = int(val.get("touched_count") or 0) + 1
        eff_status = "active" if str(val.get("origin") or matched.get("origin")) == "human_seeded" else str(val.get("status") or "active")
        val.update(
            {
                "topic_key": str(val.get("topic_key") or key),
                "domain": str(val.get("domain") or matched.get("domain") or "general_tech"),
                "prompt": str(val.get("prompt") or matched.get("prompt") or ""),
                "origin": str(val.get("origin") or matched.get("origin") or "human_seeded"),
                "priority": round(float(val.get("priority", 0.8) or 0.8), 3),
                "status": eff_status,
                "touched_count": touched,
                "last_explored_at": now_iso,
            }
        )
        await self.repo.upsert_world_item(
            kind="interest_portfolio",
            key=key,
            value=val,
            status=eff_status,
            source=str((raw_row or {}).get("source") or "curiosity:explored"),
        )
        return {"key": key, **val}

    def _detect_stalled_topic_clusters(
        self,
        cur_goals: list[dict[str, Any]],
        *,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Cluster recent Curiosity goals by entity/topic tokens and detect repeated-failure loops."""
        now_dt = now or utcnow()
        cutoff = now_dt - timedelta(hours=12)
        clusters: list[dict[str, Any]] = []

        for g in cur_goals[:30]:
            g_dt = _parse_iso_dt(g.get("created_at"))
            if g_dt is not None and g_dt < cutoff:
                continue
            title = str(g.get("title") or "")
            rel = g.get("related_key")
            toks = extract_topic_signature_tokens(title, related_key=rel)
            if len(toks) < 2:
                continue
            status = str(g.get("status") or "").upper()
            is_fail = status in {"FAILED", "REJECTED", "ABANDONED"}

            matched = next(
                (cl for cl in clusters if topic_tokens_match(toks, cl["rep_tokens"])),
                None,
            )
            if matched is None:
                clusters.append(
                    {
                        "rep_tokens": toks,
                        "token_counts": Counter(toks),
                        "total_count": 1,
                        "failed_count": 1 if is_fail else 0,
                        "sample_title": title[:160],
                    }
                )
            else:
                matched["total_count"] += 1
                if is_fail:
                    matched["failed_count"] += 1
                matched["token_counts"].update(toks)
                common = [t for t, _ in matched["token_counts"].most_common(8)]
                matched["rep_tokens"] = frozenset(common)

        stalled = [
            {
                "tokens": sorted(cl["rep_tokens"])[:12],
                "sample_title": cl["sample_title"],
                "failed_count": int(cl["failed_count"]),
                "total_count": int(cl["total_count"]),
                "reason": "repeated_failure_circuit_breaker",
            }
            for cl in clusters
            if int(cl["failed_count"]) >= 3
            or (int(cl["total_count"]) >= 5 and int(cl["failed_count"]) >= 2)
        ]
        stalled.sort(key=lambda c: (-c["failed_count"], -c["total_count"], c["sample_title"]))
        return stalled[:5]

    def _compute_portfolio_and_fatigue(
        self,
        cur_goals: list[dict[str, Any]],
        portfolio_items: list[dict[str, Any]],
        domains_covered: list[str],
        must_explore_external: bool,
        stalled_topic_clusters: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Compute comfort-zone streak, same-domain/topic streak, human starvation & recommended pivots."""
        goal_domains: list[str] = []
        goal_scopes: list[str] = []
        goal_buckets: list[str] = []
        recent_domain_counts: dict[str, int] = {}

        for g in cur_goals:
            title = str(g.get("title") or "")
            desc = str(g.get("description") or "")
            rel = g.get("related_key")
            exp = g.get("expected_outcome") if isinstance(g.get("expected_outcome"), dict) else {}
            dom = infer_topic_domain(title, desc, explicit_domain=exp.get("exploration_domain"))
            scope = classify_exploration_scope(title, desc, rel)
            topic_exp = {k: v for k, v in exp.items() if k != "exploration_domain"}
            bucket = extract_topic_bucket(title=title, related_key=rel, expected_outcome=topic_exp)
            goal_domains.append(dom)
            goal_scopes.append(scope)
            goal_buckets.append(bucket)
            recent_domain_counts[dom] = recent_domain_counts.get(dom, 0) + 1

        comfort_zone_streak = 0
        comfort_streak_domains: set[str] = set()
        for dom, sc in zip(goal_domains, goal_scopes):
            if _is_comfort_zone_domain(dom) or sc == "internal_self_check":
                comfort_zone_streak += 1
                if dom:
                    comfort_streak_domains.add(dom)
            else:
                break

        latest_domain = goal_domains[0] if goal_domains else None
        same_domain_streak = 0
        if latest_domain is not None:
            for dom in goal_domains:
                if dom == latest_domain:
                    same_domain_streak += 1
                else:
                    break

        latest_topic_bucket = goal_buckets[0] if goal_buckets else None
        same_topic_streak = 0
        if latest_topic_bucket is not None:
            for bk in goal_buckets:
                if bk == latest_topic_bucket:
                    same_topic_streak += 1
                else:
                    break

        starved_keys: list[str] = []
        enriched_portfolio: list[dict[str, Any]] = []
        for item in portfolio_items:
            it_copy = dict(item)
            is_human = it_copy.get("origin") == "human_seeded"
            starved = False
            cycles_since_touch = 0
            if is_human:
                anchor_dt = _parse_iso_dt(it_copy.get("last_explored_at") or it_copy.get("created_at"))
                matched_recent = False
                for idx, g in enumerate(cur_goals):
                    g_dt = _parse_iso_dt(g.get("created_at"))
                    if anchor_dt is not None and g_dt is not None and g_dt < anchor_dt - timedelta(seconds=1):
                        continue
                    exp = g.get("expected_outcome") if isinstance(g.get("expected_outcome"), dict) else {}
                    hit = match_interest_portfolio_item(
                        [it_copy],
                        title=str(g.get("title") or ""),
                        description=str(g.get("description") or ""),
                        related_key=g.get("related_key"),
                        portfolio_key=exp.get("interest_portfolio_key"),
                        domain=goal_domains[idx],
                    )
                    if hit is not None:
                        matched_recent = True
                        break
                    cycles_since_touch += 1
                if not matched_recent and cycles_since_touch >= self.portfolio_starvation_cycles:
                    starved = True
                    starved_keys.append(str(it_copy["key"]))
            it_copy["starved"] = starved
            it_copy["cycles_since_touched"] = cycles_since_touch
            enriched_portfolio.append(it_copy)

        cooldown_domains_set: set[str] = set()
        cooldown_topic: str | None = None
        pivot_reasons: list[str] = []

        if comfort_zone_streak >= self.max_same_domain_streak:
            cooldown_domains_set.add("security_audit")
            cooldown_domains_set.update(d for d in comfort_streak_domains if _is_comfort_zone_domain(d))
            pivot_reasons.append(f"comfort_zone_streak:{comfort_zone_streak}>={self.max_same_domain_streak}")

        if latest_domain and same_domain_streak >= self.max_same_domain_streak:
            cooldown_domains_set.add(latest_domain)
            pivot_reasons.append(f"same_domain_streak:{latest_domain}:{same_domain_streak}>={self.max_same_domain_streak}")

        if latest_topic_bucket and same_topic_streak >= self.max_same_topic_streak:
            cooldown_topic = latest_topic_bucket
            pivot_reasons.append(f"same_topic_streak:{latest_topic_bucket}:{same_topic_streak}>={self.max_same_topic_streak}")

        if starved_keys:
            pivot_reasons.append(f"human_portfolio_starved:{len(starved_keys)}")

        stalled_list = list(stalled_topic_clusters or [])
        for cl in stalled_list:
            pivot_reasons.append(f"stalled_topic_failures:{int(cl.get('failed_count') or 0)}")

        must_pivot_domain = bool(pivot_reasons) or must_explore_external

        # Rank recommended non-cooldown pivot domains: starved/human portfolio domains first, then least-covered spectrum
        rec_domains: list[str] = []
        for it in sorted(
            enriched_portfolio,
            key=lambda x: (
                0 if x.get("starved") else 1,
                0 if x.get("origin") == "human_seeded" else 1,
                -float(x.get("priority", 0.5) or 0.5),
            ),
        ):
            d = str(it.get("domain") or "")
            if d and d not in cooldown_domains_set and d not in rec_domains and not _is_comfort_zone_domain(d):
                rec_domains.append(d)

        covered_set = set(domains_covered)
        spectrum_sorted = sorted(
            [d for d in EXPLORATION_DOMAIN_SPECTRUM if d not in cooldown_domains_set],
            key=lambda d: (recent_domain_counts.get(d, 0), 1 if d in covered_set else 0, EXPLORATION_DOMAIN_SPECTRUM.index(d)),
        )
        for d in spectrum_sorted:
            if d not in rec_domains:
                rec_domains.append(d)

        human_candidates = [
            {
                "key": it["key"],
                "domain": it["domain"],
                "prompt": it["prompt"][:220],
                "priority": it["priority"],
                "touched_count": it["touched_count"],
                "starved": bool(it.get("starved")),
            }
            for it in enriched_portfolio
            if it.get("origin") == "human_seeded" and it.get("domain") not in cooldown_domains_set
        ][:5]

        return {
            "interest_portfolio_enabled": True,
            "comfort_zone_streak": comfort_zone_streak,
            "same_domain_streak": same_domain_streak,
            "latest_domain": latest_domain,
            "same_topic_streak": same_topic_streak,
            "latest_topic_bucket": latest_topic_bucket,
            "must_pivot_domain": must_pivot_domain,
            "cooldown_domains": sorted(cooldown_domains_set),
            "cooldown_topic": cooldown_topic,
            "stalled_topic_clusters": stalled_list,
            "pivot_reasons": pivot_reasons,
            "recent_domain_counts": recent_domain_counts,
            "human_portfolio_candidates": human_candidates,
            "starved_human_portfolio_keys": starved_keys,
            "recommended_pivot_domains": rec_domains[:6],
        }

    async def build_guidance(self, recent_goals: list[dict[str, Any]]) -> dict[str, Any]:
        """Compute Curiosity internal-vs-external balance, fatigue streaks, and portfolio candidates."""
        window_size = (
            max(self.lookback_goals, self.portfolio_starvation_cycles + 2)
            if self.interest_portfolio_enabled
            else self.lookback_goals
        )
        curiosity_goals_stream = [g for g in recent_goals if str(g.get("drive_type") or "").lower() == "curiosity"]
        all_cur_goals = curiosity_goals_stream[:window_size]
        cur_sample_for_ratio = all_cur_goals[: self.lookback_goals]
        stalled_clusters = (
            self._detect_stalled_topic_clusters(curiosity_goals_stream[:30])
            if self.interest_portfolio_enabled
            else []
        )
        stalled_token_sets = [
            frozenset(str(t).lower() for t in (cl.get("tokens") or []) if str(t).strip())
            for cl in stalled_clusters
        ]

        internal_cnt = 0
        external_cnt = 0
        for g in cur_sample_for_ratio:
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
            and not any(
                topic_tokens_match(
                    extract_topic_signature_tokens(
                        str((t.get("value") or {}).get("title") or ""),
                        related_key=str(t["key"]),
                    ),
                    st_set,
                )
                for st_set in stalled_token_sets
            )
        ][:6]

        base = {
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
        if not self.interest_portfolio_enabled:
            return {
                **base,
                "interest_portfolio_enabled": False,
                "must_pivot_domain": False,
                "cooldown_domains": [],
                "stalled_topic_clusters": [],
                "human_portfolio_candidates": [],
            }

        portfolio_items = await self.list_interest_portfolio(statuses=("active",), limit=12)
        m2_meta = self._compute_portfolio_and_fatigue(
            all_cur_goals,
            portfolio_items,
            domains_covered,
            must_explore_external,
            stalled_topic_clusters=stalled_clusters,
        )
        return {**base, **m2_meta}
