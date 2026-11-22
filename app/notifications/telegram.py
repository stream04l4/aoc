"""Telegram Outbox Notifier & Verified Webhook Approval Handler (V2.0 P1-8).

Security guarantees (Task Book Section 5.6.2):
  - Bot token is NEVER logged or persisted in DB/audit payloads.
  - Outbound notifications run asynchronously via PostgreSQL Outbox (notification.dispatch);
    Telegram network outages retry with exponential backoff and NEVER block AutonomousLoop.
  - Inbound Webhook callbacks enforce ALL THREE checks:
      1. X-Telegram-Bot-Api-Secret-Token == AOC_TELEGRAM__WEBHOOK_SECRET (constant-time)
      2. chat_id in AOC_TELEGRAM__ALLOWED_CHAT_IDS (and optional user_id allowlist)
      3. Valid single-use approval nonce + TTL, delegated strictly to ApprovalService -> approve_and_dispatch()
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Any

import httpx

from app.core.config import TelegramSettings
from app.governance.approval import ApprovalService
from app.notifications.human_bridge import HumanBridge
from app.observability.logging import get_logger
from app.storage.repository import StateRepository

logger = get_logger("aoc.notifications.telegram")


class TelegramAuthError(PermissionError):
    """Raised when Telegram Webhook secret or chat/user allowlist check fails."""

    def __init__(self, message: str, status_code: int = 403) -> None:
        super().__init__(message)
        self.status_code = status_code


def format_notification_message(payload: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """Render structured notification payload into concise operator text + optional inline keyboard."""
    kind = str(payload.get("kind") or "info")
    if kind == "approval_requested":
        nonce = str(payload.get("nonce") or "")
        text = (
            f"[AOC L2 Approval Required]\n"
            f"Approval ID: {payload.get('approval_id')}\n"
            f"Task ID: {payload.get('task_id')}\n"
            f"Level: {payload.get('permission_level')} | Agent: {payload.get('assigned_agent')}\n"
            f"Objective: {payload.get('objective', '')}\n"
            f"Expires: {payload.get('expires_at', '')}"
        )
        reply_markup = (
            {
                "inline_keyboard": [
                    [
                        {"text": "Approve", "callback_data": f"approve:{nonce}"},
                        {"text": "Reject", "callback_data": f"reject:{nonce}"},
                    ]
                ]
            }
            if nonce
            else None
        )
        return text, reply_markup

    if kind == "ask_human":
        parts = [
            "[AOC 主动求助 / 能力需求]",
            f"主题：{payload.get('title', '未命名')}",
            f"内容：{payload.get('message', '')}",
        ]
        if payload.get("reason"):
            parts.append(f"原因：{payload['reason']}")
        if payload.get("expected_benefit"):
            parts.append(f"预期收益：{payload['expected_benefit']}")
        parts.append("（直接在此回复文字，我将在下一轮自主思考中读取并优先响应；发送 /status 可查看当前状态）")
        return "\n".join(parts), None
    if kind == "milestone_report":
        parts = [
            "[AOC 自主进化阶段进展汇报]",
            f"成果：{payload.get('title', '能力突破')}",
            f"摘要：{payload.get('message', '')}",
        ]
        if payload.get("expected_benefit"):
            parts.append(f"价值：{payload['expected_benefit']}")
        return "\n".join(parts), None
    if kind == "critical_alert":
        return f"[AOC CRITICAL ALERT]\n{payload.get('message', '')}\nReason: {payload.get('reason', '')}", None
    if kind == "quota_recovered":
        return f"[AOC Quota Recovered]\n{payload.get('message', 'Daily quota refreshed; organism resumed.')}", None
    if kind == "lifecycle_paused":
        return f"[AOC Paused]\nReason: {payload.get('reason', 'operator paused')}", None
    if kind == "task_terminal":
        return (
            f"[AOC Task {payload.get('status', 'DONE')}]\n"
            f"Task: {payload.get('task_id')} | Achieved: {payload.get('goal_achieved')}\n"
            f"Summary: {str(payload.get('summary', ''))[:300]}"
        ), None
    return f"[AOC Notification] {payload.get('message') or payload}", None


class TelegramNotifier:
    """Async Outbox consumer sending notifications via Telegram Bot API."""

    def __init__(
        self,
        settings: TelegramSettings,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._http_client = http_client

    @property
    def configured(self) -> bool:
        tok = self.settings.bot_token.get_secret_value() if self.settings.bot_token else ""
        return bool(tok)

    def _bot_url(self, method: str) -> str:
        tok = self.settings.bot_token.get_secret_value() if self.settings.bot_token else ""
        base = self.settings.api_base_url.rstrip("/")
        return f"{base}/bot{tok}/{method}"

    async def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = self._bot_url(method)
        try:
            if self._http_client is not None:
                resp = await self._http_client.post(url, json=payload, timeout=self.settings.timeout_seconds)
            else:
                async with httpx.AsyncClient(timeout=self.settings.timeout_seconds) as client:
                    resp = await client.post(url, json=payload)
            resp.raise_for_status()
            body = resp.json()
            if not body.get("ok"):
                raise RuntimeError(f"Telegram API {method} returned ok=false: {body.get('description')}")
            return body
        except Exception as exc:
            # Never leak bot token from exception URLs into logs or outbox last_error!
            tok = self.settings.bot_token.get_secret_value() if self.settings.bot_token else ""
            safe_err = str(exc).replace(tok, "<redacted>") if tok else str(exc)
            raise RuntimeError(f"Telegram {method} failed: {safe_err}") from None

    async def get_me(self) -> dict[str, Any]:
        if not self.configured:
            return {"ok": False, "error": "telegram bot_token not configured"}
        return await self._post("getMe", {})

    async def dispatch_notification(self, event: dict[str, Any]) -> dict[str, Any]:
        """Outbox handler for OutboxEventType.NOTIFICATION_DISPATCH."""
        payload = dict(event.get("payload") or {})
        if not self.configured or not self.settings.allowed_chat_ids:
            return {"delivered": 0, "skipped": True, "reason": "telegram token or allowed_chat_ids not configured"}

        text, reply_markup = format_notification_message(payload)
        delivered = 0
        for chat_id in self.settings.allowed_chat_ids:
            req: dict[str, Any] = {"chat_id": chat_id, "text": text[:4000]}
            if reply_markup is not None:
                req["reply_markup"] = reply_markup
            await self._post("sendMessage", req)
            delivered += 1
        return {"delivered": delivered, "kind": payload.get("kind", "info")}

    async def answer_callback_query(self, callback_query_id: str, text: str) -> None:
        if not self.configured or not callback_query_id:
            return
        try:
            await self._post("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text[:180]})
        except Exception as exc:
            logger.warning("telegram.answer_callback_failed", error=str(exc))


class TelegramWebhookHandler:
    """Authenticates Telegram Webhook callbacks and routes approvals through ApprovalService."""

    def __init__(
        self,
        settings: TelegramSettings,
        approval_service: ApprovalService,
        repo: StateRepository,
        notifier: TelegramNotifier | None = None,
    ) -> None:
        self.settings = settings
        self.approval_service = approval_service
        self.repo = repo
        self.notifier = notifier

    async def handle_update(
        self,
        update: dict[str, Any],
        *,
        secret_header: str | None,
    ) -> dict[str, Any]:
        # 1. Verify Webhook Secret Header (fail-closed if webhook_secret is unset)
        expected_secret = self.settings.webhook_secret.get_secret_value() if self.settings.webhook_secret else ""
        if not expected_secret or not secret_header or not secrets.compare_digest(secret_header, expected_secret):
            await self.repo.log_event(
                "telegram.webhook_auth_failed",
                message="Rejected Telegram webhook with missing or invalid secret token",
                level="warning",
            )
            raise TelegramAuthError("Invalid or missing Telegram webhook secret token", status_code=401)

        # 2. Extract chat_id, user_id, and action/nonce from callback_query or command message
        cb = update.get("callback_query")
        msg = update.get("message")
        callback_id = ""
        raw_action = ""

        if isinstance(cb, dict):
            callback_id = str(cb.get("id") or "")
            frm = cb.get("from") or {}
            chat = (cb.get("message") or {}).get("chat") or {}
            chat_id = str(chat.get("id") or "")
            user_id = str(frm.get("id") or "")
            username = str(frm.get("username") or user_id or "telegram_user")
            raw_action = str(cb.get("data") or "").strip()
        elif isinstance(msg, dict):
            frm = msg.get("from") or {}
            chat = msg.get("chat") or {}
            chat_id = str(chat.get("id") or "")
            user_id = str(frm.get("id") or "")
            username = str(frm.get("username") or user_id or "telegram_user")
            text = str(msg.get("text") or "").strip()
            parts = text.split()
            if len(parts) >= 2 and parts[0].lower() in {"/approve", "/reject"}:
                raw_action = f"{parts[0].lower().lstrip('/')}:{parts[1]}"
        else:
            return {"ok": True, "ignored": True, "reason": "unsupported update type"}

        # 3. Enforce strict chat_id & optional user_id allowlist
        allowed_chats = {str(c).strip() for c in self.settings.allowed_chat_ids if str(c).strip()}
        if not allowed_chats or chat_id not in allowed_chats:
            await self.repo.log_event(
                "telegram.unauthorized_chat",
                message=f"Rejected Telegram callback from unauthorized chat_id={chat_id}",
                payload={"chat_id": chat_id, "user_id": user_id},
                level="warning",
            )
            raise TelegramAuthError(f"Chat '{chat_id}' is not in allowed_chat_ids", status_code=403)

        allowed_users = {str(u).strip() for u in self.settings.allowed_user_ids if str(u).strip()}
        if allowed_users and user_id not in allowed_users:
            await self.repo.log_event(
                "telegram.unauthorized_user",
                message=f"Rejected Telegram callback from unauthorized user_id={user_id}",
                payload={"chat_id": chat_id, "user_id": user_id},
                level="warning",
            )
            raise TelegramAuthError(f"User '{user_id}' is not in allowed_user_ids", status_code=403)

        if ":" not in raw_action:
            return {"ok": True, "ignored": True, "reason": "no approval command found"}

        action, nonce = raw_action.split(":", 1)
        action = action.strip().lower()
        nonce = nonce.strip()
        if action not in {"approve", "reject"} or not nonce:
            return {"ok": True, "ignored": True, "reason": "invalid approval callback syntax"}

        # 4. Delegate strictly through ApprovalService -> approve_and_dispatch() (zero Governance bypass!)
        result = await self.approval_service.decide_by_nonce(
            nonce,
            action=action,
            approver=f"telegram:{username}({user_id})",
            reason=f"Telegram {action} by {username}",
            channel="telegram",
        )
        if self.notifier is not None and callback_id:
            await self.notifier.answer_callback_query(callback_id, f"AOC task {result.get('status')}")
        return {"ok": True, "action": action, "result": result}


class TelegramPoller:
    """Long-polling Telegram `getUpdates` loop for bidirectional interaction without public HTTPS Webhook."""

    def __init__(
        self,
        *,
        settings: TelegramSettings,
        notifier: TelegramNotifier,
        webhook_handler: TelegramWebhookHandler,
        human_bridge: HumanBridge,
        repo: StateRepository,
        lifecycle: Any = None,
        status_fn: Any = None,
    ) -> None:
        self.settings = settings
        self.notifier = notifier
        self.webhook_handler = webhook_handler
        self.human_bridge = human_bridge
        self.repo = repo
        self.lifecycle = lifecycle
        self.status_fn = status_fn
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run_forever(), name="aoc-telegram-poller")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def _is_authorized(self, chat_id: str, user_id: str) -> bool:
        allowed_chats = {str(c).strip() for c in self.settings.allowed_chat_ids if str(c).strip()}
        if not allowed_chats or chat_id not in allowed_chats:
            return False
        allowed_users = {str(u).strip() for u in self.settings.allowed_user_ids if str(u).strip()}
        if allowed_users and user_id not in allowed_users:
            return False
        return True

    async def handle_polled_update(self, update: dict[str, Any]) -> dict[str, Any]:
        """Process a single Telegram Update dict from getUpdates under strict allowlist enforcement."""
        update_id = update.get("update_id")
        cb = update.get("callback_query")
        msg = update.get("message")

        if isinstance(cb, dict):
            frm = cb.get("from") or {}
            chat = (cb.get("message") or {}).get("chat") or {}
            chat_id = str(chat.get("id") or "")
            user_id = str(frm.get("id") or "")
            username = str(frm.get("username") or user_id or "telegram_user")
            if not self._is_authorized(chat_id, user_id):
                await self.repo.log_event(
                    "telegram.unauthorized_chat",
                    message=f"Ignored polled callback from unauthorized chat_id={chat_id}",
                    level="warning",
                )
                return {"ok": False, "ignored": True, "reason": "unauthorized"}

            raw_action = str(cb.get("data") or "").strip()
            if ":" in raw_action:
                action, nonce = raw_action.split(":", 1)
                action = action.strip().lower()
                if action in {"approve", "reject"} and nonce.strip():
                    result = await self.webhook_handler.approval_service.decide_by_nonce(
                        nonce.strip(),
                        action=action,
                        approver=f"telegram:{username}({user_id})",
                        reason=f"Telegram polled {action} by {username}",
                        channel="telegram",
                    )
                    cb_id = str(cb.get("id") or "")
                    if cb_id:
                        await self.notifier.answer_callback_query(cb_id, f"AOC task {result.get('status')}")
                    return {"ok": True, "kind": "approval", "action": action, "result": result}
            return {"ok": True, "ignored": True, "reason": "unsupported_callback"}

        if not isinstance(msg, dict):
            return {"ok": True, "ignored": True, "reason": "unsupported_update_type"}

        frm = msg.get("from") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        user_id = str(frm.get("id") or "")
        username = str(frm.get("username") or user_id or "joel")
        if not self._is_authorized(chat_id, user_id):
            await self.repo.log_event(
                "telegram.unauthorized_chat",
                message=f"Ignored polled message from unauthorized chat_id={chat_id}",
                level="warning",
            )
            return {"ok": False, "ignored": True, "reason": "unauthorized"}

        text = str(msg.get("text") or "").strip()
        if not text:
            return {"ok": True, "ignored": True, "reason": "empty_text"}

        parts = text.split()
        cmd = parts[0].lower()
        if len(parts) >= 2 and cmd in {"/approve", "/reject"}:
            action = cmd.lstrip("/")
            result = await self.webhook_handler.approval_service.decide_by_nonce(
                parts[1].strip(),
                action=action,
                approver=f"telegram:{username}({user_id})",
                reason=f"Telegram command {action} by {username}",
                channel="telegram",
            )
            await self.notifier._post(
                "sendMessage",
                {"chat_id": chat_id, "text": f"已执行审批操作 {action}，当前状态：{result.get('status')}"},
            )
            return {"ok": True, "kind": "approval_cmd", "action": action, "result": result}

        if cmd in {"/start", "/status"}:
            status_text = "AOC 在线运行中。"
            if self.status_fn is not None:
                try:
                    info = await self.status_fn()
                    status_text = str(info or status_text)
                except Exception as exc:
                    status_text = f"AOC 在线（获取详细状态异常：{exc}）"
            if cmd == "/start":
                status_text += "\n\n直接发送任何文字，我将作为最高优先级引导写入下一轮自主思考循环。"
            await self.notifier._post("sendMessage", {"chat_id": chat_id, "text": status_text[:4000]})
            return {"ok": True, "kind": "command", "command": cmd}

        # Free-form creator guidance -> persist, wake organism, and acknowledge warmly
        guidance = await self.human_bridge.record_human_guidance(
            key=f"tg:{update_id if update_id is not None else 'msg'}",
            text=text,
            sender=username,
            channel="telegram",
            chat_id=chat_id,
        )
        if self.lifecycle is not None:
            try:
                self.lifecycle.wake(reason=f"telegram guidance from {username}")
            except Exception:
                pass
        try:
            await self.notifier._post(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": "✅ 已收到你的指令并唤醒思考循环，我会在下一轮优先阅读并执行你的建议。",
                },
            )
        except Exception as exc:
            logger.warning("telegram.poller_reply_failed", error=str(exc))
        return {"ok": True, "kind": "guidance", "guidance_key": guidance["key"]}

    async def poll_once(self) -> int:
        """Fetch and process pending Telegram updates once, persisting monotonic update offset."""
        cursors = await self.repo.list_world_items(kind="telegram_cursor")
        cursor_item = next((c for c in cursors if c.get("key") == "offset"), None)
        offset = int(((cursor_item or {}).get("value") or {}).get("next_offset") or 0)

        body = await self.notifier._post(
            "getUpdates",
            {"offset": offset, "timeout": 5, "allowed_updates": ["message", "callback_query"]},
        )
        updates = body.get("result") or []
        if not updates:
            return 0

        next_offset = offset
        processed = 0
        for upd in updates:
            if not isinstance(upd, dict):
                continue
            uid = upd.get("update_id")
            if isinstance(uid, int):
                next_offset = max(next_offset, uid + 1)
            try:
                await self.handle_polled_update(upd)
            except Exception as exc:
                logger.warning("telegram.poller_update_error", update_id=uid, error=str(exc))
            processed += 1

        if next_offset > offset:
            await self.repo.upsert_world_item(
                kind="telegram_cursor",
                key="offset",
                value={"next_offset": next_offset},
                status="active",
                source="telegram_poller",
            )
        return processed

    async def _run_forever(self) -> None:
        logger.info("telegram_poller.started")
        try:
            await self.notifier._post("deleteWebhook", {"drop_pending_updates": False})
        except Exception as exc:
            logger.warning("telegram_poller.delete_webhook_degraded", error=str(exc))

        interval = max(1.0, float(self.settings.polling_interval_seconds))
        err_streak = 0
        while not self._stopping:
            try:
                await self.poll_once()
                err_streak = 0
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                err_streak += 1
                backoff = min(60.0, interval * (2 ** min(err_streak, 4)))
                logger.warning("telegram_poller.poll_error", error=str(exc), backoff_seconds=backoff)
                await asyncio.sleep(backoff)
        logger.info("telegram_poller.stopped")
