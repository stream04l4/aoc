"""Multi-Hop Epistemic Investigation & Discovered Channel Map Manager (V4.2, 0 new DB tables).

Reuses `digital_world_state`:
  - kind='investigation_thread': tracks cross-cycle curiosity/survival/reproduction/human threads
    (origin trigger -> multi-hop leads -> verified findings -> deterministic stall/hop bounds -> artifacts).
  - kind='discovered_channel': tracks external high-value knowledge channels discovered organically
    by the organism (domain, entry URL, RSS/Atom feed URL, registered dynamic source id, utility score).
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any
from urllib.parse import urlparse

from app.drives.interfaces import KnowledgeGap
from app.observability.logging import get_logger
from app.perception.interfaces import utcnow
from app.perception.world.security import is_internal_or_blocked_host, sanitize_external_text
from app.storage.repository import StateRepository

logger = get_logger("aoc.perception.world.investigation")

ACTIVE_THREAD_STAGES = {"seeding", "foraging", "deepening", "internalizing", "stalled"}
TERMINAL_THREAD_STAGES = {"resolved", "closed"}


def _slugify(text: str, *, prefix: str = "inv:", max_len: int = 100) -> str:
    raw = str(text or "").strip().lower()
    if prefix and raw.startswith(prefix):
        raw = raw[len(prefix) :]
    slug = re.sub(r"[^a-z0-9_:-]+", "_", raw).strip("_")
    slug = slug[: max(8, max_len - len(prefix))] if slug else "thread"
    return f"{prefix}{slug}" if prefix else slug


def _valid_public_https_url(url: str | None) -> str | None:
    cand = str(url or "").strip()
    if not cand:
        return None
    p = urlparse(cand)
    if p.scheme.lower() != "https" or p.username or p.password:
        return None
    host = (p.hostname or "").strip().lower().rstrip(".")
    if is_internal_or_blocked_host(host):
        return None
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    return cand[:500]


def _normalize_lead(raw: Any, fallback_idx: int) -> dict[str, Any] | None:
    if isinstance(raw, str):
        tgt = raw.strip()
        if not tgt:
            return None
        is_url = tgt.lower().startswith("https://")
        if is_url and not _valid_public_https_url(tgt):
            return None
        ltype = "url" if is_url else "search_query"
        return {
            "lead_id": _slugify(tgt, prefix="", max_len=36) + f"_{fallback_idx}",
            "kind": ltype,
            "target": tgt[:400],
            "note": "",
            "status": "pending",
        }
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "search_query").strip().lower()
    if kind not in {"search_query", "url", "ask_human"}:
        kind = "search_query"
    target = sanitize_external_text(str(raw.get("target") or raw.get("url") or raw.get("query") or ""), max_chars=400)
    if not target:
        return None
    if kind == "url":
        safe_url = _valid_public_https_url(target)
        if not safe_url:
            return None
        target = safe_url
    lid = _slugify(str(raw.get("lead_id") or target[:32]), prefix="", max_len=40)
    note = sanitize_external_text(str(raw.get("note") or raw.get("reason") or ""), max_chars=240)
    return {
        "lead_id": lid,
        "kind": kind,
        "target": target,
        "note": note,
        "status": "pending",
    }


class InvestigationManager:
    """Manages cross-cycle multi-hop epistemic threads and discovered channel maps."""

    def __init__(
        self,
        *,
        repo: StateRepository,
        max_active: int = 2,
        max_hops: int = 6,
        low_ig_stall_streak: int = 2,
        low_ig_threshold: float = 0.15,
    ) -> None:
        self.repo = repo
        self.max_active = max(1, int(max_active))
        self.max_hops = max(2, int(max_hops))
        self.low_ig_stall_streak = max(1, int(low_ig_stall_streak))
        self.low_ig_threshold = max(0.01, min(0.8, float(low_ig_threshold)))

    async def list_threads(self, *, active_only: bool = False, limit: int = 20) -> list[dict[str, Any]]:
        rows = await self.repo.list_world_items(kind="investigation_thread")
        out: list[dict[str, Any]] = []
        for r in rows:
            val = dict(r.get("value") or {}) if isinstance(r.get("value"), dict) else {}
            stage = str(r.get("status") or val.get("stage") or "seeding")
            if active_only and stage not in ACTIVE_THREAD_STAGES:
                continue
            item = {"thread_key": r["key"], "stage": stage, **val, "updated_at": r.get("updated_at")}
            out.append(item)
        out.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)
        return out[: max(1, int(limit))]

    async def list_discovered_channels(self, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = await self.repo.list_world_items(kind="discovered_channel")
        out: list[dict[str, Any]] = []
        for r in rows:
            val = dict(r.get("value") or {}) if isinstance(r.get("value"), dict) else {}
            out.append({"domain": r["key"], "status": r.get("status", "active"), **val, "updated_at": r.get("updated_at")})
        out.sort(
            key=lambda x: (float(x.get("utility_score") or 0.0), str(x.get("updated_at") or "")),
            reverse=True,
        )
        return out[: max(1, int(limit))]

    async def record_discovered_channel(
        self,
        channel: dict[str, Any],
        *,
        source: str = "investigation",
    ) -> dict[str, Any] | None:
        if not isinstance(channel, dict):
            return None
        entry_url = _valid_public_https_url(channel.get("entry_url"))
        feed_url = _valid_public_https_url(channel.get("feed_url"))
        raw_dom = str(channel.get("domain") or "").strip().lower().rstrip(".")
        if not raw_dom and entry_url:
            raw_dom = (urlparse(entry_url).hostname or "").strip().lower().rstrip(".")
        if not raw_dom and feed_url:
            raw_dom = (urlparse(feed_url).hostname or "").strip().lower().rstrip(".")
        if is_internal_or_blocked_host(raw_dom):
            return None
        try:
            ipaddress.ip_address(raw_dom)
            return None
        except ValueError:
            pass

        existing_rows = await self.repo.list_world_items(kind="discovered_channel")
        prev = next((x for x in existing_rows if x.get("key") == raw_dom), None)
        val = dict(prev.get("value") or {}) if prev and isinstance(prev.get("value"), dict) else {}

        prev_hits = int(val.get("discoveries") or 0)
        notes = sanitize_external_text(str(channel.get("notes") or val.get("notes") or ""), max_chars=300)
        ctype = str(channel.get("channel_type") or val.get("channel_type") or "website").strip().lower()[:40]
        reg_sid = str(channel.get("registered_source_id") or val.get("registered_source_id") or "").strip()[:120] or None
        utility = min(1.0, round(float(val.get("utility_score") or 0.5) + (0.15 if prev else 0.0) + (0.1 if feed_url else 0.0), 4))

        new_val = {
            "domain": raw_dom,
            "channel_type": ctype,
            "entry_url": entry_url or val.get("entry_url"),
            "feed_url": feed_url or val.get("feed_url"),
            "registered_source_id": reg_sid,
            "notes": notes,
            "discoveries": prev_hits + 1,
            "utility_score": utility,
            "last_seen_at": utcnow().isoformat(),
        }
        await self.repo.upsert_world_item(
            kind="discovered_channel",
            key=raw_dom,
            value=new_val,
            status="active",
            source=source,
        )
        return new_val

    async def apply_goal_action(
        self,
        action_spec: dict[str, Any] | None,
        *,
        origin_drive: str,
        origin_trigger: str,
        cycle_id: Any = None,
    ) -> dict[str, Any] | None:
        """Apply GoalProposal.investigation_action (open | continue | resolve | abandon)."""
        if not isinstance(action_spec, dict):
            return None
        action = str(action_spec.get("action") or "open").strip().lower()
        if action not in {"open", "continue", "resolve", "abandon"}:
            action = "open"

        raw_key = str(action_spec.get("thread_key") or "").strip()
        question = sanitize_external_text(
            str(action_spec.get("core_question") or origin_trigger or ""), max_chars=360
        )
        threads = await self.list_threads(active_only=False, limit=40)
        by_key = {t["thread_key"]: t for t in threads}

        if raw_key:
            tkey = _slugify(raw_key, prefix="inv:")
        elif action in {"continue", "resolve", "abandon"}:
            active_first = next((t for t in threads if t["stage"] in ACTIVE_THREAD_STAGES), None)
            if active_first is None:
                if action != "open":
                    return None
                tkey = _slugify(question[:48] or "thread", prefix="inv:")
            else:
                tkey = active_first["thread_key"]
        else:
            tkey = _slugify(question[:48] or "thread", prefix="inv:")

        existing = by_key.get(tkey)
        if action == "open" and existing is None:
            active_non_stalled = [t for t in threads if t["stage"] in (ACTIVE_THREAD_STAGES - {"stalled"})]
            if len(active_non_stalled) >= self.max_active:
                # Enforce active investigation cap deterministically: attach/continue highest-priority active thread
                fallback = active_non_stalled[0]
                return fallback

        now_iso = utcnow().isoformat()
        if existing is not None:
            stage = existing.get("stage", "seeding")
            if action == "abandon":
                stage = "closed"
            elif action == "resolve":
                stage = "resolved"
            elif action == "continue" and stage == "stalled":
                stage = "foraging"

            leads = list(existing.get("leads") or [])
            for idx, nl in enumerate(list(action_spec.get("initial_leads") or [])[:3]):
                norm = _normalize_lead(nl, idx)
                if norm and not any(x.get("target") == norm["target"] for x in leads) and len(leads) < 10:
                    leads.append(norm)

            val = {
                **{k: v for k, v in existing.items() if k not in {"thread_key", "stage", "updated_at"}},
                "core_question": question or existing.get("core_question", ""),
                "stage": stage,
                "leads": leads[:10],
                "last_cycle_id": str(cycle_id) if cycle_id else existing.get("last_cycle_id"),
            }
            if action == "continue" and existing.get("stage") == "stalled":
                val["low_ig_streak"] = 0
            await self.repo.upsert_world_item(
                kind="investigation_thread",
                key=tkey,
                value=val,
                status=stage,
                source=f"cycle:{cycle_id}" if cycle_id else "investigation",
            )
            return {"thread_key": tkey, **val}

        if action in {"resolve", "abandon"}:
            return None

        init_leads: list[dict[str, Any]] = []
        for idx, nl in enumerate(list(action_spec.get("initial_leads") or [])[:4]):
            norm = _normalize_lead(nl, idx)
            if norm and not any(x["target"] == norm["target"] for x in init_leads):
                init_leads.append(norm)
        if not init_leads and question:
            init_leads.append(_normalize_lead(question, 0) or {"lead_id": "seed_0", "kind": "search_query", "target": question[:160], "note": "seed question", "status": "pending"})

        val = {
            "origin_drive": str(origin_drive or "curiosity"),
            "origin_trigger": sanitize_external_text(origin_trigger, max_chars=260),
            "core_question": question or sanitize_external_text(origin_trigger, max_chars=260),
            "stage": "seeding",
            "hops_taken": 0,
            "max_hops": self.max_hops,
            "low_ig_streak": 0,
            "leads": init_leads[:10],
            "findings": [],
            "resulting_artifacts": [],
            "created_at": now_iso,
            "last_cycle_id": str(cycle_id) if cycle_id else None,
        }
        await self.repo.upsert_world_item(
            kind="investigation_thread",
            key=tkey,
            value=val,
            status="seeding",
            source=f"cycle:{cycle_id}" if cycle_id else "investigation",
        )
        return {"thread_key": tkey, **val}

    async def record_hop_outcome(
        self,
        *,
        thread_key: str,
        verification_passed: bool,
        goal_achieved: bool,
        measured_ig: float,
        update_spec: dict[str, Any] | None = None,
        registered_source_id: str | None = None,
        skill_key: str | None = None,
        playbook_domain: str | None = None,
        cycle_id: Any = None,
    ) -> dict[str, Any] | None:
        tkey = _slugify(thread_key, prefix="inv:")
        threads = await self.list_threads(active_only=False, limit=60)
        cur = next((t for t in threads if t["thread_key"] == tkey), None)
        if cur is None:
            return None

        verified_ok = bool(verification_passed and goal_achieved)
        hops_taken = int(cur.get("hops_taken") or 0) + 1
        max_hops = max(2, int(cur.get("max_hops") or self.max_hops))
        low_streak = int(cur.get("low_ig_streak") or 0)
        if not verified_ok or float(measured_ig) < self.low_ig_threshold:
            low_streak += 1
        else:
            low_streak = 0

        leads = [dict(l) for l in (cur.get("leads") or []) if isinstance(l, dict)]
        findings = list(cur.get("findings") or [])
        artifacts = list(cur.get("resulting_artifacts") or [])
        stage = str(cur.get("stage") or "foraging")

        upd = dict(update_spec) if isinstance(update_spec, dict) else {}
        if verified_ok:
            consumed_id = str(upd.get("consumed_lead_id") or "").strip()
            if consumed_id:
                for ld in leads:
                    if ld.get("lead_id") == consumed_id:
                        ld["status"] = "explored"
                        break
            else:
                for ld in leads:
                    if ld.get("status") == "pending":
                        ld["status"] = "explored"
                        break

            for idx, raw_nl in enumerate(list(upd.get("new_leads") or [])[:4]):
                norm = _normalize_lead(raw_nl, hops_taken * 10 + idx)
                if norm and not any(x.get("target") == norm["target"] for x in leads) and len(leads) < 10:
                    leads.append(norm)

            finding_txt = sanitize_external_text(str(upd.get("finding") or ""), max_chars=320)
            if finding_txt and finding_txt not in findings:
                findings.append(finding_txt)
                findings = findings[-8:]

            if isinstance(upd.get("discovered_channel"), dict):
                ch_payload = dict(upd["discovered_channel"])
                if registered_source_id:
                    ch_payload["registered_source_id"] = registered_source_id
                ch_res = await self.record_discovered_channel(
                    ch_payload,
                    source=f"investigation:{tkey}"[:64],
                )
                if ch_res is not None:
                    artifacts.append(f"channel:{ch_res['domain']}")

            if registered_source_id:
                artifacts.append(f"source:{registered_source_id}")
            if skill_key:
                artifacts.append(f"skill:{skill_key}")
            if playbook_domain:
                artifacts.append(f"playbook:{playbook_domain}")

            suggested = str(upd.get("stage_suggestion") or "").strip().lower()
            if suggested in (ACTIVE_THREAD_STAGES | TERMINAL_THREAD_STAGES):
                stage = suggested
            elif stage == "seeding":
                stage = "foraging"

        # Kernel deterministic safety & anti-spin convergence caps override LLM suggestion
        stop_reason: str | None = None
        if hops_taken >= max_hops and stage != "resolved":
            stage = "closed"
            stop_reason = f"max_hops_reached ({hops_taken}/{max_hops})"
        elif low_streak >= self.low_ig_stall_streak and stage not in TERMINAL_THREAD_STAGES:
            stage = "stalled"
            stop_reason = f"low_information_gain_stalled (streak={low_streak})"

        dedup_artifacts = list(dict.fromkeys(str(a) for a in artifacts if a))[-12:]
        new_val = {
            **{k: v for k, v in cur.items() if k not in {"thread_key", "stage", "updated_at"}},
            "stage": stage,
            "hops_taken": hops_taken,
            "max_hops": max_hops,
            "low_ig_streak": low_streak,
            "leads": leads[:10],
            "findings": findings[-8:],
            "resulting_artifacts": dedup_artifacts,
            "last_hop_verified": verified_ok,
            "last_hop_ig": round(float(measured_ig), 4),
            "stop_reason": stop_reason,
            "last_cycle_id": str(cycle_id) if cycle_id else cur.get("last_cycle_id"),
        }
        await self.repo.upsert_world_item(
            kind="investigation_thread",
            key=tkey,
            value=new_val,
            status=stage,
            source=f"cycle:{cycle_id}" if cycle_id else "investigation",
        )
        return {"thread_key": tkey, **new_val}

    async def pending_leads_as_knowledge_gaps(self, *, limit: int = 3) -> list[KnowledgeGap]:
        """Project active unresolved leads into KnowledgeGaps so Curiosity stays self-driven without preset RSS."""
        active = await self.list_threads(active_only=True, limit=self.max_active * 2)
        gaps: list[KnowledgeGap] = []
        for t in active:
            if t.get("stage") == "stalled":
                continue
            pending = [ld for ld in (t.get("leads") or []) if isinstance(ld, dict) and ld.get("status") == "pending"]
            for ld in pending[:2]:
                gaps.append(
                    KnowledgeGap(
                        topic=f"{t['thread_key']}:{ld.get('lead_id') or 'lead'}",
                        description=f"[{ld.get('kind', 'lead')}] {ld.get('target', '')}",
                        importance=0.82,
                        confidence=1.0,
                        source="investigation",
                    )
                )
                if len(gaps) >= limit:
                    return gaps
            if t.get("core_question") and len(gaps) < limit:
                gaps.append(
                    KnowledgeGap(
                        topic=t["thread_key"],
                        description=str(t["core_question"])[:220],
                        importance=0.80,
                        confidence=1.0,
                        source="investigation",
                    )
                )
                if len(gaps) >= limit:
                    return gaps
        return gaps

    async def build_context_snapshot(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return compact active threads and top discovered channels for CognitiveContext."""
        active = await self.list_threads(active_only=True, limit=self.max_active + 2)
        channels = await self.list_discovered_channels(limit=6)
        return active, channels
