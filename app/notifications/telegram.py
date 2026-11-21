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

import secrets
from typing import Any

import httpx

from app.core.config import TelegramSettings
from app.governance.approval import ApprovalService
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
