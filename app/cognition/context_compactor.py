"""Tiered Context Compression & Relevance Pruning (V4.0 M4).

Reduces redundant LLM prompt tokens across `assess`, `generate_goal`, and `plan`
without losing critical security fences (`<untrusted_world_event>`), health signals,
or high-value reusable Skill / Playbook context:
  - Tier 1 (Always kept compact): drive states, core system health, budget remaining, affect snapshot.
  - Tier 2 (Relevance-ranked Top-K): validated skill signatures ranked by lexical relevance + empirical reuse rate;
    domain-matched external playbooks truncated cleanly on line boundaries.
  - Tier 3 (Bounded previews): memories, active hypotheses, external events, and unknown topics trimmed to
    deterministic character/item budgets while keeping security fences intact.
"""

from __future__ import annotations

import json
import re
from typing import Any

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]")


def _tok_set(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text or "") if len(t) > 1 or "\u4e00" <= t <= "\u9fff"}


def _trim_text(text: Any, max_chars: int) -> str:
    s = str(text or "").strip()
    if len(s) <= max_chars:
        return s
    return s[: max(0, max_chars - 1)].rstrip() + "…"


def compact_memories(
    memories: list[dict[str, Any]],
    *,
    max_items: int = 6,
    max_summary_chars: int = 240,
) -> list[dict[str, Any]]:
    """Strip verbose fields and bound memory summaries to save prompt tokens."""
    out: list[dict[str, Any]] = []
    for m in (memories or [])[: max(1, int(max_items))]:
        if not isinstance(m, dict):
            continue
        item: dict[str, Any] = {
            "type": m.get("memory_type") or m.get("type") or "episodic",
            "title": _trim_text(m.get("title"), 100),
            "summary": _trim_text(m.get("summary") or m.get("content"), max_summary_chars),
        }
        if m.get("importance") is not None:
            item["importance"] = round(float(m["importance"]), 2)
        if m.get("score") is not None:
            item["score"] = round(float(m["score"]), 3)
        out.append(item)
    return out


def rank_and_compact_skills(
    skills: list[dict[str, Any]],
    *,
    query_text: str = "",
    top_k: int = 5,
    max_desc_chars: int = 140,
) -> list[dict[str, Any]]:
    """Select Top-K most relevant & proven validated skills and emit compact signatures."""
    if not skills:
        return []
    q_toks = _tok_set(query_text)

    scored: list[tuple[float, int, dict[str, Any]]] = []
    for idx, sk in enumerate(skills):
        if not isinstance(sk, dict):
            continue
        blob = " ".join(
            [
                str(sk.get("canonical_name") or ""),
                str(sk.get("skill_key") or ""),
                str(sk.get("capability_key") or ""),
                str(sk.get("description") or ""),
            ]
        )
        s_toks = _tok_set(blob)
        overlap = len(q_toks & s_toks) / max(1, len(q_toks)) if q_toks else 0.0
        reuse_cnt = int(sk.get("reuse_count") or 0)
        reuse_ok = int(sk.get("reuse_success_count") or 0)
        reuse_bonus = min(0.25, 0.05 * reuse_cnt) + (0.15 * (reuse_ok / reuse_cnt) if reuse_cnt > 0 else 0.0)
        score = overlap + reuse_bonus
        scored.append((score, -idx, sk))

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    out: list[dict[str, Any]] = []
    for _, _, sk in scored[: max(1, int(top_k))]:
        in_schema = sk.get("input_schema") if isinstance(sk.get("input_schema"), dict) else {}
        out_schema = sk.get("output_schema") if isinstance(sk.get("output_schema"), dict) else {}
        compact: dict[str, Any] = {
            "skill_key": sk.get("skill_key"),
            "canonical_name": sk.get("canonical_name") or sk.get("skill_key"),
            "version": sk.get("version") or "1.0.0",
            "capability_key": sk.get("capability_key") or "coding",
            "permission_level": sk.get("permission_level") or "L1",
            "description": _trim_text(sk.get("description"), max_desc_chars),
            "reuse_count": int(sk.get("reuse_count") or 0),
        }
        if in_schema:
            compact["input_schema"] = in_schema
        if out_schema:
            compact["output_schema"] = out_schema
        target_ref = str(compact["canonical_name"] or compact["skill_key"] or "").strip()
        props = in_schema.get("properties") if isinstance(in_schema.get("properties"), dict) else {}
        if props:
            param_preview = ", ".join(f"{k!r}: ..." for k in list(props.keys())[:4])
            hint_raw = (
                f"execution_mode='skill_first', skill_invocation={{'skill_key': '{target_ref}', "
                f"'input_data': {{{param_preview}}}}}"
            )
        else:
            hint_raw = (
                f"execution_mode='skill_first', skill_invocation={{'skill_key': '{target_ref}', "
                "'input_data': {}}}"
            )
        compact["invocation_hint"] = _trim_text(hint_raw, 180)
        out.append(compact)
    return out


def compact_playbooks(
    playbooks: list[dict[str, Any]],
    *,
    domain_hints: list[str] | None = None,
    max_items: int = 2,
    max_chars: int = 480,
) -> list[dict[str, Any]]:
    """Keep only domain-relevant external playbooks trimmed cleanly on line boundaries."""
    if not playbooks:
        return []
    hints = {str(h or "").strip().lower() for h in (domain_hints or []) if h}
    ordered = sorted(
        [p for p in playbooks if isinstance(p, dict)],
        key=lambda p: (0 if str(p.get("domain") or "").lower() in hints else 1, -int(p.get("samples") or 0)),
    )
    out: list[dict[str, Any]] = []
    for pb in ordered[: max(1, int(max_items))]:
        raw = str(pb.get("content") or "").strip()
        if len(raw) > max_chars:
            lines = raw.splitlines()
            kept: list[str] = []
            acc = 0
            for ln in lines:
                if acc + len(ln) + 1 > max_chars:
                    break
                kept.append(ln)
                acc += len(ln) + 1
            trimmed = "\n".join(kept).strip() or _trim_text(raw, max_chars)
        else:
            trimmed = raw
        out.append(
            {
                "domain": pb.get("domain"),
                "version": pb.get("version"),
                "success_rate": pb.get("success_rate"),
                "content": trimmed,
            }
        )
    return out


def compact_observation_dict(
    fenced_obs: dict[str, Any],
    *,
    max_events: int = 5,
    max_topics: int = 8,
    max_event_chars: int = 420,
) -> dict[str, Any]:
    """Compact an already-fenced observation dict while preserving security fences and health metrics."""
    out: dict[str, Any] = {}
    if fenced_obs.get("timestamp"):
        out["timestamp"] = fenced_obs["timestamp"]

    # System state: retain health, llm, db, runner, resources (drop bulky process lists if any)
    sys_state = fenced_obs.get("system_state")
    if isinstance(sys_state, dict):
        out["system_state"] = {
            k: v
            for k, v in sys_state.items()
            if k not in {"top_processes", "raw_metrics", "env_vars"}
        }

    if isinstance(fenced_obs.get("agent_status"), dict):
        out["agent_status"] = fenced_obs["agent_status"]

    # Environment state: bound unknown_topics while preserving <untrusted_world_event> closing tag!
    # Also exclude raw V5.1 `social` chat streams from technical prompts to prevent pollution/injection.
    env = fenced_obs.get("environment_state")
    if isinstance(env, dict):
        env_c = {k: v for k, v in env.items() if k not in {"unknown_topics", "social"}}
        social_data = env.get("social")
        if isinstance(social_data, dict):
            inbox = social_data.get("inbox")
            if isinstance(inbox, list) and inbox:
                env_c["social_summary"] = {"unread_messages": len(inbox)}
        topics = env.get("unknown_topics")
        if isinstance(topics, list):
            trimmed_topics: list[dict[str, Any]] = []
            for t in topics[: max(1, int(max_topics))]:
                if not isinstance(t, dict):
                    continue
                tc = dict(t)
                desc = str(tc.get("description") or "")
                if len(desc) > max_event_chars:
                    if desc.startswith("<untrusted_world_event ") and desc.endswith("</untrusted_world_event>"):
                        end_tag = "</untrusted_world_event>"
                        tc["description"] = desc[: max(80, max_event_chars - len(end_tag) - 1)] + "…" + end_tag
                    else:
                        tc["description"] = _trim_text(desc, max_event_chars)
                trimmed_topics.append(tc)
            env_c["unknown_topics"] = trimmed_topics
        out["environment_state"] = env_c

    # External events: cap count & preserve <untrusted_world_event> closing tag!
    ext_evs = fenced_obs.get("external_events")
    if isinstance(ext_evs, list):
        trimmed_evs: list[dict[str, Any]] = []
        for ev in ext_evs[: max(1, int(max_events))]:
            if not isinstance(ev, dict):
                continue
            ev_c = dict(ev)
            ue = str(ev_c.get("untrusted_event") or "")
            if len(ue) > max_event_chars and ue.startswith("<untrusted_world_event ") and ue.endswith("</untrusted_world_event>"):
                end_tag = "</untrusted_world_event>"
                ev_c["untrusted_event"] = ue[: max(80, max_event_chars - len(end_tag) - 1)] + "…" + end_tag
            trimmed_evs.append(ev_c)
        out["external_events"] = trimmed_evs

    if fenced_obs.get("available_resources"):
        out["available_resources"] = fenced_obs["available_resources"]
    return out


def compact_hypotheses(hypotheses: list[dict[str, Any]], *, max_items: int = 5) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for h in (hypotheses or [])[: max(1, int(max_items))]:
        if not isinstance(h, dict):
            continue
        out.append(
            {
                "key": h.get("key"),
                "status": h.get("status"),
                "claim": _trim_text(h.get("claim"), 180),
                "confidence": h.get("confidence", 0.5),
            }
        )
    return out


def compact_drives(drives: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep essential drive satisfaction/deficit/urgency/top_deficits fields."""
    out: list[dict[str, Any]] = []
    for d in drives or []:
        if not isinstance(d, dict):
            continue
        out.append(
            {
                "drive_type": d.get("drive_type"),
                "satisfaction": d.get("satisfaction"),
                "deficit": d.get("deficit"),
                "urgency": d.get("urgency"),
                "top_deficits": list(d.get("top_deficits") or [])[:3],
            }
        )
    return out


def compact_investigations(threads: list[dict[str, Any]], *, max_items: int = 2) -> list[dict[str, Any]]:
    """Keep active multi-hop investigation threads compact with pending leads & recent findings."""
    out: list[dict[str, Any]] = []
    for t in (threads or [])[: max(1, int(max_items))]:
        if not isinstance(t, dict):
            continue
        pending = [
            {
                "lead_id": ld.get("lead_id"),
                "kind": ld.get("kind"),
                "target": _trim_text(ld.get("target"), 160),
                "note": _trim_text(ld.get("note"), 100),
            }
            for ld in (t.get("leads") or [])
            if isinstance(ld, dict) and ld.get("status") == "pending"
        ][:4]
        out.append(
            {
                "thread_key": t.get("thread_key"),
                "stage": t.get("stage"),
                "origin_drive": t.get("origin_drive"),
                "core_question": _trim_text(t.get("core_question"), 200),
                "hops": f"{int(t.get('hops_taken') or 0)}/{int(t.get('max_hops') or 6)}",
                "low_ig_streak": int(t.get("low_ig_streak") or 0),
                "pending_leads": pending,
                "recent_findings": [_trim_text(f, 160) for f in (t.get("findings") or [])[-3:]],
            }
        )
    return out


def compact_channels(channels: list[dict[str, Any]], *, max_items: int = 6) -> list[dict[str, Any]]:
    """Keep organically discovered external knowledge channels compact for CognitiveContext."""
    out: list[dict[str, Any]] = []
    for ch in (channels or [])[: max(1, int(max_items))]:
        if not isinstance(ch, dict):
            continue
        out.append(
            {
                "domain": ch.get("domain"),
                "channel_type": ch.get("channel_type"),
                "entry_url": _trim_text(ch.get("entry_url"), 160),
                "feed_url": _trim_text(ch.get("feed_url"), 160),
                "registered_source_id": ch.get("registered_source_id"),
                "utility_score": ch.get("utility_score"),
                "notes": _trim_text(ch.get("notes"), 140),
            }
        )
    return out


def compact_interest_portfolio(items: list[dict[str, Any]], *, max_items: int = 6) -> list[dict[str, Any]]:
    """Keep active cross-cycle human/self-seeded interest portfolio entries compact for `generate_goal()`."""
    out: list[dict[str, Any]] = []
    for it in (items or [])[: max(1, int(max_items))]:
        if not isinstance(it, dict):
            continue
        out.append(
            {
                "topic_key": it.get("topic_key") or it.get("key"),
                "domain": it.get("domain") or "general_tech",
                "prompt": _trim_text(it.get("prompt"), 180),
                "origin": it.get("origin") or "human_seeded",
                "priority": round(float(it.get("priority", 0.5) or 0.5), 2),
                "touched_count": int(it.get("touched_count") or 0),
                "starved": bool(it.get("starved", False)),
            }
        )
    return out


def estimate_payload_chars(payload: Any) -> int:
    return len(json.dumps(payload, ensure_ascii=False, default=str))
