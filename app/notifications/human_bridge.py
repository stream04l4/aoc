"""Bidirectional Human Collaboration Bridge (V4.1).

Gives AOC a rate-limited, non-spamming voice (`ask_human` / `milestone_report`)
via the existing PostgreSQL Transactional Outbox (`notification.dispatch`) and stores
incoming creator replies/instructions from Telegram (`digital_world_state(kind='human_guidance')`,
zero new DB tables).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from app.observability.logging import get_logger
from app.perception.interfaces import utcnow
from app.storage.outbox import OutboxEventType
from app.storage.repository import StateRepository

logger = get_logger("aoc.notifications.human_bridge")

_DEFAULT_ASK_OPTIONS: tuple[dict[str, str], ...] = (
    {
        "label": "同意，按推荐方案推进",
        "instruction": "同意按你在求助说明中推荐的方案继续执行，并在合规边界内完成该任务。",
    },
    {
        "label": "先保守探索，暂不改动",
        "instruction": "保持只读与最小影响原则，先做保守验证或查找公开替代方案，暂不做激进变更。",
    },
    {
        "label": "跳过此卡点，切换方向",
        "instruction": "暂时跳过当前阻塞卡点，将该支线标记为搁置并切换到更有信息增益的新探索方向。",
    },
)
_DEFAULT_MILESTONE_OPTIONS: tuple[dict[str, str], ...] = (
    {
        "label": "不错，继续深挖此方向",
        "instruction": "继续沿当前已验证成果方向深入探索，或将经验沉淀为可复用技能。",
    },
    {
        "label": "总结收尾，换新主题",
        "instruction": "总结固化当前里程碑结论，收敛本支线并转向其他更有价值的探索方向。",
    },
)


def _parse_iso_utc(raw: str | None) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class HumanBridge:
    """Coordinates proactive rate-limited human outreach and inbound guidance persistence."""

    def __init__(
        self,
        *,
        repo: StateRepository,
        enabled: bool = True,
        cooldown_minutes: int = 120,
        topic_dedup_hours: float = 24.0,
        l2_bridge: Any = None,
    ) -> None:
        self.repo = repo
        self.enabled = bool(enabled)
        self.cooldown_minutes = max(1, int(cooldown_minutes))
        self.topic_dedup_hours = max(0.25, float(topic_dedup_hours))
        self.l2_bridge = l2_bridge

    @staticmethod
    def _slugify(topic_key: str, title: str) -> str:
        raw = str(topic_key or title or "general").strip().lower()
        slug = re.sub(r"[^a-z0-9_:-]+", "_", raw).strip("_")[:64]
        return slug or "general"

    @staticmethod
    def _normalize_options(kind: str, raw_options: Any) -> list[dict[str, str]]:
        """Normalize 2-4 concrete decision options; filter out duplicates or redundant 'Other' choices."""
        cleaned: list[dict[str, str]] = []
        seen_labels: set[str] = set()
        if isinstance(raw_options, list):
            for opt in raw_options:
                if isinstance(opt, dict):
                    label = str(opt.get("label") or opt.get("text") or opt.get("title") or "").strip()
                    instruction = str(opt.get("instruction") or opt.get("action") or label).strip()
                elif isinstance(opt, str):
                    label = opt.strip()
                    instruction = label
                else:
                    continue
                if not label or "其它" in label or "其他" in label:
                    continue
                norm_label = label[:28].strip()
                norm_inst = instruction[:240].strip() or norm_label
                dedup_key = norm_label.lower()
                if not norm_label or dedup_key in seen_labels:
                    continue
                seen_labels.add(dedup_key)
                cleaned.append({"label": norm_label, "instruction": norm_inst})
                if len(cleaned) >= 4:
                    break

        if len(cleaned) >= 2:
            return cleaned
        defaults = _DEFAULT_MILESTONE_OPTIONS if kind == "milestone_report" else _DEFAULT_ASK_OPTIONS
        return [dict(o) for o in defaults]

    async def maybe_send_outreach(
        self,
        *,
        kind: str,
        topic_key: str,
        title: str,
        message: str,
        reason: str = "",
        expected_benefit: str = "",
        options: list[Any] | None = None,
        cycle_id: uuid.UUID | None = None,
        force: bool = False,
        extra_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Enqueue a rate-limited interactive outreach card to Joel via Outbox."""
        clean_msg = str(message or "").strip()
        if not self.enabled or not clean_msg:
            return {"sent": False, "reason": "disabled_or_empty"}

        norm_kind = str(kind or "ask_human").strip().lower()
        if norm_kind not in {"ask_human", "milestone_report"}:
            norm_kind = "ask_human"

        slug = self._slugify(topic_key, title)
        now = utcnow()
        logs = await self.repo.list_world_items(kind="human_outreach_log")

        if not force:
            for item in logs:
                val = item.get("value") if isinstance(item.get("value"), dict) else {}
                sent_dt = _parse_iso_utc(val.get("sent_at"))
                if sent_dt is None:
                    continue
                age_sec = max(0.0, (now - sent_dt).total_seconds())
                if age_sec < self.cooldown_minutes * 60.0:
                    return {
                        "sent": False,
                        "topic_key": slug,
                        "reason": f"global_cooldown_active ({int(age_sec)}s < {self.cooldown_minutes * 60}s)",
                    }
                if item.get("key") == slug and age_sec < self.topic_dedup_hours * 3600.0:
                    return {
                        "sent": False,
                        "topic_key": slug,
                        "reason": f"topic_dedup_active ({slug})",
                    }

        clean_title = str(title or slug).strip()[:200]
        clean_reason = str(reason or "").strip()[:500]
        clean_benefit = str(expected_benefit or "").strip()[:500]
        card_id = uuid.uuid4().hex[:8]
        norm_options = self._normalize_options(norm_kind, options)

        card_value: dict[str, Any] = {
            "card_id": card_id,
            "kind": norm_kind,
            "topic_key": slug,
            "title": clean_title,
            "message": clean_msg[:1500],
            "reason": clean_reason,
            "expected_benefit": clean_benefit,
            "options": norm_options,
            "created_at": now.isoformat(),
            "answered_at": None,
            "selected_option": None,
        }
        if isinstance(extra_metadata, dict) and extra_metadata:
            card_value["meta"] = dict(extra_metadata)

        await self.repo.upsert_world_item(
            kind="human_outreach_card",
            key=card_id,
            value=card_value,
            status="open",
            source=f"human_bridge:{norm_kind}",
        )

        payload = {
            "kind": norm_kind,
            "card_id": card_id,
            "topic_key": slug,
            "title": clean_title,
            "message": clean_msg[:1500],
            "reason": clean_reason,
            "expected_benefit": clean_benefit,
            "options": norm_options,
            "cycle_id": str(cycle_id) if cycle_id else None,
        }
        await self.repo.enqueue_outbox_event(
            event_type=OutboxEventType.NOTIFICATION_DISPATCH,
            idempotency_key=f"notify:{norm_kind}:{slug}:{card_id}",
            payload=payload,
        )
        await self.repo.upsert_world_item(
            kind="human_outreach_log",
            key=slug,
            value={
                "kind": norm_kind,
                "card_id": card_id,
                "title": clean_title,
                "message": clean_msg[:500],
                "sent_at": now.isoformat(),
                "cycle_id": str(cycle_id) if cycle_id else None,
            },
            status="sent",
            source=f"human_bridge:{norm_kind}",
        )
        await self.repo.log_event(
            "human_bridge.outreach_enqueued",
            message=f"Enqueued proactive {norm_kind} outreach '{clean_title}'",
            payload={"kind": norm_kind, "topic_key": slug, "card_id": card_id},
            cycle_id=cycle_id,
        )
        return {
            "sent": True,
            "kind": norm_kind,
            "topic_key": slug,
            "card_id": card_id,
            "options_count": len(norm_options),
        }

    async def get_outreach_card(self, card_id: str) -> dict[str, Any] | None:
        """Fetch an outreach decision card from `world_states` by card_id."""
        clean_id = str(card_id or "").strip()
        if not clean_id:
            return None
        items = await self.repo.list_world_items(kind="human_outreach_card")
        return next((item for item in items if str(item.get("key") or "") == clean_id), None)

    async def resolve_outreach_choice(
        self,
        card_id: str,
        option_index: int,
        *,
        sender: str = "joel",
        chat_id: str = "",
        update_id: int | None = None,
    ) -> dict[str, Any]:
        """Resolve an inline option button tap on a HumanBridge card and record high-priority guidance."""
        item = await self.get_outreach_card(card_id)
        if item is None:
            raise LookupError(f"Outreach card {card_id} not found")
        val = dict(item.get("value") or {}) if isinstance(item.get("value"), dict) else {}
        options = list(val.get("options") or [])
        if option_index < 0 or option_index >= len(options):
            raise IndexError(f"Option index {option_index} out of range for card {card_id}")

        opt = dict(options[option_index])
        title = str(val.get("title") or "AOC 决策卡片")
        selected = val.get("selected_option") if isinstance(val.get("selected_option"), dict) else None
        if item.get("status") == "answered" and selected and selected.get("index") == option_index:
            return {
                "ok": True,
                "already_selected": True,
                "card_id": card_id,
                "title": title,
                "option": opt,
                "guidance": None,
            }

        now = utcnow()
        guidance_text = (
            f"【创作者 Joel 对「{title}」的选择】\n"
            f"选项：{opt['label']}\n"
            f"执行指示：{opt['instruction']}"
        )
        suffix = str(update_id) if update_id is not None else str(int(now.timestamp()))
        guidance = await self.record_human_guidance(
            key=f"tg_choice:{card_id}:{option_index}:{suffix}",
            text=guidance_text,
            sender=sender,
            channel="telegram",
            chat_id=chat_id,
        )
        val["answered_at"] = now.isoformat()
        val["selected_option"] = {
            "index": option_index,
            "label": opt["label"],
            "instruction": opt["instruction"],
            "sender": sender,
        }
        await self.repo.upsert_world_item(
            kind="human_outreach_card",
            key=card_id,
            value=val,
            status="answered",
            source=str(item.get("source") or "human_bridge:ask_human"),
        )
        bridge_res: dict[str, Any] | None = None
        if self.l2_bridge is not None and getattr(self.l2_bridge, "enabled", False):
            meta = val.get("meta") if isinstance(val.get("meta"), dict) else {}
            b_stage = str(meta.get("bridge_stage") or "").strip().lower()
            try:
                if b_stage == "stage0_candidate" and option_index == 0:
                    bridge_res = await self.l2_bridge.on_proposal_adopted(
                        card_id=card_id, card_val=val, sender=sender
                    )
                elif b_stage == "stage2_confirm":
                    bridge_res = await self.l2_bridge.on_stage2_decision(
                        card_id=card_id, option_index=option_index, card_val=val, sender=sender
                    )
            except Exception as exc:
                logger.warning("human_bridge.l2_bridge_degraded", card_id=card_id, error=str(exc))
                bridge_res = {"ok": False, "error": str(exc)}

        out_res: dict[str, Any] = {
            "ok": True,
            "already_selected": False,
            "card_id": card_id,
            "title": title,
            "option": opt,
            "guidance": guidance,
        }
        if bridge_res is not None:
            out_res["l2_bridge"] = bridge_res
        return out_res

    async def mark_awaiting_custom_reply(
        self,
        card_id: str,
        *,
        chat_id: str,
        prompt_message_id: int | None = None,
    ) -> dict[str, Any]:
        """Bind the next text reply from `chat_id` to the specified outreach card ('Other' option)."""
        item = await self.get_outreach_card(card_id)
        if item is None:
            raise LookupError(f"Outreach card {card_id} not found")
        val = item.get("value") if isinstance(item.get("value"), dict) else {}
        payload = {
            "card_id": card_id,
            "title": str(val.get("title") or "AOC 决策卡片"),
            "prompt_message_id": prompt_message_id,
            "created_at": utcnow().isoformat(),
        }
        await self.repo.upsert_world_item(
            kind="telegram_pending_reply",
            key=str(chat_id).strip(),
            value=payload,
            status="open",
            source="telegram:other_choice",
        )
        return payload

    async def consume_pending_custom_reply(
        self,
        chat_id: str,
        *,
        max_age_seconds: float = 7200.0,
    ) -> dict[str, Any] | None:
        """Pop any active pending 'Other' custom-reply expectation for `chat_id`."""
        clean_chat = str(chat_id or "").strip()
        if not clean_chat:
            return None
        items = await self.repo.list_world_items(kind="telegram_pending_reply", status="open")
        match = next((item for item in items if str(item.get("key") or "") == clean_chat), None)
        if match is None:
            return None
        val = dict(match.get("value") or {}) if isinstance(match.get("value"), dict) else {}
        await self.repo.upsert_world_item(
            kind="telegram_pending_reply",
            key=clean_chat,
            value=val,
            status="consumed",
            source=str(match.get("source") or "telegram:other_choice"),
        )
        created_dt = _parse_iso_utc(val.get("created_at"))
        if created_dt is not None and (utcnow() - created_dt).total_seconds() > max_age_seconds:
            return None
        return val

    async def record_custom_card_reply(
        self,
        card_id: str,
        text: str,
        *,
        sender: str = "joel",
        chat_id: str = "",
        update_id: int | None = None,
    ) -> dict[str, Any]:
        """Persist Joel's custom text reply for an outreach card and close the card as answered."""
        clean_text = str(text or "").strip()
        item = await self.get_outreach_card(card_id)
        val = dict(item.get("value") or {}) if (item and isinstance(item.get("value"), dict)) else {}
        title = str(val.get("title") or "指定话题")
        now = utcnow()
        if item is not None:
            val["answered_at"] = now.isoformat()
            val["selected_option"] = {
                "index": -1,
                "label": "其它答案（自定义）",
                "instruction": clean_text[:500],
                "sender": sender,
            }
            await self.repo.upsert_world_item(
                kind="human_outreach_card",
                key=card_id,
                value=val,
                status="answered",
                source=str(item.get("source") or "human_bridge:ask_human"),
            )

        suffix = str(update_id) if update_id is not None else str(int(now.timestamp()))
        guidance = await self.record_human_guidance(
            key=f"tg_other:{card_id}:{suffix}",
            text=f"【创作者 Joel 对「{title}」的自定义回复】\n{clean_text}",
            sender=sender,
            channel="telegram",
            chat_id=chat_id,
        )
        return {"ok": True, "card_id": card_id, "title": title, "guidance": guidance}

    async def record_human_guidance(
        self,
        *,
        key: str,
        text: str,
        sender: str = "joel",
        channel: str = "telegram",
        chat_id: str = "",
    ) -> dict[str, Any]:
        """Persist an incoming creator instruction/reply as open high-priority guidance."""
        clean_key = str(key or f"{channel}:{int(utcnow().timestamp())}").strip()[:120]
        clean_text = str(text or "").strip()[:2000]
        val = {
            "text": clean_text,
            "sender": str(sender or "joel").strip()[:80],
            "channel": str(channel or "telegram").strip()[:32],
            "chat_id": str(chat_id or "").strip()[:64],
            "received_at": utcnow().isoformat(),
        }
        await self.repo.upsert_world_item(
            kind="human_guidance",
            key=clean_key,
            value=val,
            status="open",
            source=f"{channel}:{val['sender']}",
        )
        await self.repo.log_event(
            "human_bridge.guidance_received",
            message=f"Received human guidance from {val['sender']} via {channel}",
            payload={"key": clean_key, "sender": val["sender"], "channel": channel},
        )
        return {"key": clean_key, "status": "open", **val}

    async def list_pending_guidance(self, limit: int = 5) -> list[dict[str, Any]]:
        """List unacknowledged creator instructions sorted newest-first."""
        items = await self.repo.list_world_items(kind="human_guidance", status="open")
        ordered = sorted(
            items,
            key=lambda x: str((x.get("value") or {}).get("received_at") or x.get("updated_at") or ""),
            reverse=True,
        )
        out: list[dict[str, Any]] = []
        for item in ordered[: max(1, int(limit))]:
            val = item.get("value") if isinstance(item.get("value"), dict) else {}
            text = str(val.get("text") or "").strip()
            if not text:
                continue
            out.append(
                {
                    "key": item.get("key"),
                    "text": text,
                    "sender": val.get("sender", "joel"),
                    "channel": val.get("channel", "telegram"),
                    "received_at": val.get("received_at"),
                }
            )
        return out

    async def acknowledge_guidance(self, keys: list[str], *, cycle_id: uuid.UUID | None = None) -> int:
        """Mark pending human guidance items as acknowledged once injected into a cognitive cycle."""
        clean_keys = {str(k).strip() for k in (keys or []) if k}
        if not clean_keys:
            return 0
        items = await self.repo.list_world_items(kind="human_guidance", status="open")
        count = 0
        for item in items:
            k = str(item.get("key") or "")
            if k not in clean_keys:
                continue
            val = dict(item.get("value") or {}) if isinstance(item.get("value"), dict) else {}
            val["acknowledged_at"] = utcnow().isoformat()
            if cycle_id is not None:
                val["acknowledged_cycle_id"] = str(cycle_id)
            await self.repo.upsert_world_item(
                kind="human_guidance",
                key=k,
                value=val,
                status="acknowledged",
                source=str(item.get("source") or "telegram:joel"),
            )
            count += 1
        return count
