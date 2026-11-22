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
    ) -> None:
        self.repo = repo
        self.enabled = bool(enabled)
        self.cooldown_minutes = max(1, int(cooldown_minutes))
        self.topic_dedup_hours = max(0.25, float(topic_dedup_hours))

    @staticmethod
    def _slugify(topic_key: str, title: str) -> str:
        raw = str(topic_key or title or "general").strip().lower()
        slug = re.sub(r"[^a-z0-9_:-]+", "_", raw).strip("_")[:64]
        return slug or "general"

    async def maybe_send_outreach(
        self,
        *,
        kind: str,
        topic_key: str,
        title: str,
        message: str,
        reason: str = "",
        expected_benefit: str = "",
        cycle_id: uuid.UUID | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Enqueue a rate-limited proactive message to Joel via Outbox."""
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
        payload = {
            "kind": norm_kind,
            "topic_key": slug,
            "title": clean_title,
            "message": clean_msg[:1500],
            "reason": str(reason or "").strip()[:500],
            "expected_benefit": str(expected_benefit or "").strip()[:500],
            "cycle_id": str(cycle_id) if cycle_id else None,
        }
        await self.repo.enqueue_outbox_event(
            event_type=OutboxEventType.NOTIFICATION_DISPATCH,
            idempotency_key=f"notify:{norm_kind}:{slug}:{int(now.timestamp())}",
            payload=payload,
        )
        await self.repo.upsert_world_item(
            kind="human_outreach_log",
            key=slug,
            value={
                "kind": norm_kind,
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
            payload={"kind": norm_kind, "topic_key": slug},
            cycle_id=cycle_id,
        )
        return {"sent": True, "kind": norm_kind, "topic_key": slug}

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
