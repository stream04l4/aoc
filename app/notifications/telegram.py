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


def _format_human_card_parts(
    *,
    options: Any,
    card_id: str,
    prompt_line: str,
    legacy_hint: str | None = None,
) -> tuple[list[str], dict[str, Any] | None]:
    """Format readable A/B/C/D options text + inline keyboard with automatic 'Other' choice."""
    letters = ("A", "B", "C", "D")
    parsed: list[tuple[str, str]] = []
    if isinstance(options, list):
        for raw in options[:4]:
            if isinstance(raw, dict):
                lbl = str(raw.get("label") or raw.get("text") or "").strip()
                inst = str(raw.get("instruction") or raw.get("action") or lbl).strip()
            elif isinstance(raw, str):
                lbl = raw.strip()
                inst = lbl
            else:
                continue
            if lbl:
                parsed.append((lbl[:28], inst[:240]))

    extra_lines: list[str] = []
    if parsed:
        extra_lines.append(f"\n{prompt_line}")
        for idx, (lbl, inst) in enumerate(parsed):
            letter = letters[idx]
            if inst and inst != lbl:
                extra_lines.append(f"{letter}. {lbl} — {inst[:60]}")
            else:
                extra_lines.append(f"{letter}. {lbl}")
    elif legacy_hint:
        extra_lines.append(legacy_hint)

    reply_markup: dict[str, Any] | None = None
    if card_id and parsed:
        rows = [
            [{"text": f"{letters[idx]}. {lbl}", "callback_data": f"hb:{card_id}:{idx}"}]
            for idx, (lbl, _inst) in enumerate(parsed)
        ]
        rows.append([{"text": "✏️ 其它答案（自定义输入）", "callback_data": f"hb:{card_id}:other"}])
        reply_markup = {"inline_keyboard": rows}
    return extra_lines, reply_markup


def format_notification_message(payload: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """Render structured notification payload into concise Chinese operator text + optional inline keyboard."""
    kind = str(payload.get("kind") or "info")
    if kind == "approval_requested":
        nonce = str(payload.get("nonce") or "")
        text = (
            f"[AOC L2 审批请求]\n"
            f"审批 ID：{payload.get('approval_id')}\n"
            f"任务 ID：{payload.get('task_id')}\n"
            f"权限级别：{payload.get('permission_level')} | 执行体：{payload.get('assigned_agent')}\n"
            f"任务目标：{payload.get('objective', '')}\n"
            f"过期时间：{payload.get('expires_at', '')}"
        )
        reply_markup = (
            {
                "inline_keyboard": [
                    [
                        {"text": "同意", "callback_data": f"approve:{nonce}"},
                        {"text": "拒绝", "callback_data": f"reject:{nonce}"},
                    ]
                ]
            }
            if nonce
            else None
        )
        return text, reply_markup

    if kind == "ask_human":
        parts = [
            f"💬 【需要你拍板】{payload.get('title', '请确认下一步')}",
            f"📌 情况说明\n{payload.get('message', '')}",
        ]
        if payload.get("reason"):
            parts.append(f"💡 原因说明\n{payload['reason']}")
        if payload.get("expected_benefit"):
            parts.append(f"🎯 解决后收益\n{payload['expected_benefit']}")
        opt_lines, reply_markup = _format_human_card_parts(
            options=payload.get("options"),
            card_id=str(payload.get("card_id") or "").strip(),
            prompt_line="👇 请直接点击下方选项（或点「其它答案」输入你的想法）：",
            legacy_hint="（直接在此回复文字，我将在下一轮自主思考中读取并优先响应；发送 /status 可查看当前状态）",
        )
        parts.extend(opt_lines)
        return "\n".join(parts), reply_markup
    if kind == "milestone_report":
        parts = [
            f"✨ 【阶段进展】{payload.get('title', '阶段成果')}",
            f"📌 核心成果\n{payload.get('message', '')}",
        ]
        if payload.get("expected_benefit"):
            parts.append(f"🎯 实际价值\n{payload['expected_benefit']}")
        opt_lines, reply_markup = _format_human_card_parts(
            options=payload.get("options"),
            card_id=str(payload.get("card_id") or "").strip(),
            prompt_line="👇 接下来希望我怎么做？（可点击选项，也可忽略）：",
        )
        parts.extend(opt_lines)
        return "\n".join(parts), reply_markup
    if kind == "critical_alert":
        return f"[AOC 紧急告警]\n{payload.get('message', '')}\n原因：{payload.get('reason', '')}", None
    if kind == "quota_recovered":
        msg = payload.get("message")
        body = "每日额度已刷新，系统已恢复运行。" if (not msg or msg == "Daily quota refreshed; organism resumed.") else msg
        return f"[AOC 额度已恢复]\n{body}", None
    if kind == "lifecycle_paused":
        return f"[AOC 已暂停]\n原因：{payload.get('reason', '管理员暂停')}", None
    if kind == "task_terminal":
        return (
            f"[AOC 任务结束：{payload.get('status', 'DONE')}]\n"
            f"任务：{payload.get('task_id')} | 达成目标：{payload.get('goal_achieved')}\n"
            f"摘要：{str(payload.get('summary', ''))[:300]}"
        ), None
    if kind == "evolution_proposal_approval":
        pkey = str(payload.get("proposal_key") or "")
        files = list(payload.get("changed_files") or [])
        mutates_drives = bool(payload.get("mutates_drive_metrics"))
        budget = payload.get("arena_budget") or {}
        entropy_idx = payload.get("entropy_index")
        entropy_line = f"当前熵增指数：{entropy_idx:.3f} | " if isinstance(entropy_idx, (int, float)) else ""
        text = (
            f"🧬 [AOC V5.0 演化繁衍提案审批]\n"
            f"提案编号：{pkey} (目标代数 Gen {payload.get('target_generation')})\n"
            f"触发类型：{payload.get('trigger_type')} | {entropy_line}"
            f"是否调整欲望评估指标：{'是' if mutates_drives else '否'}\n"
            f"触发诊断：{str(payload.get('trigger_reason', ''))[:240]}\n"
            f"演化假设：{str(payload.get('hypothesis', ''))[:360]}\n"
            f"改动文件({len(files)})：{', '.join(files[:6]) or '无'}\n"
            f"Stage 1 宪法四层检验：✅ 已通过\n"
            f"沙箱竞技预算上限：{budget.get('max_tokens', 500000)} Tokens / {budget.get('max_seconds', 1200)}s\n"
            f"批准命令：/evo_approve {pkey} | 拒绝：/evo_reject {pkey}"
        )
        reply_markup = (
            {
                "inline_keyboard": [
                    [
                        {"text": "✅ 同意孵化竞技", "callback_data": f"evo_approve:{pkey}"},
                        {"text": "❌ 拒绝提案", "callback_data": f"evo_reject:{pkey}"},
                    ]
                ]
            }
            if pkey
            else None
        )
        return text, reply_markup
    if kind == "evolution_arena_victory":
        pkey = str(payload.get("proposal_key") or "")
        mutates = bool(payload.get("mutates_drive_metrics"))
        text = (
            f"🏆 [AOC V5.0 沙箱竞技胜出 — 待确认晋升]\n"
            f"提案：{pkey} → 候选 {payload.get('candidate_key')} (目标 Gen {payload.get('target_generation')})\n"
            f"适应度对比：父代 {payload.get('parent_fitness')} vs 子代 {payload.get('offspring_fitness')} "
            f"(+{payload.get('fitness_delta')})\n"
            f"安全红线：✅ 100% 合规 | 欲望指标调整：{'是' if mutates else '否'}\n"
            f"假设：{str(payload.get('hypothesis', ''))[:260]}\n"
            f"晋升命令：/evo_promote {pkey}"
        )
        reply_markup = (
            {
                "inline_keyboard": [
                    [{"text": "🚀 确认晋升新一代（Gen N 休眠）", "callback_data": f"evo_promote:{pkey}"}]
                ]
            }
            if pkey
            else None
        )
        return text, reply_markup
    if kind == "evolution_promoted":
        return (
            f"🌱 [AOC V5.0 世代交替完成]\n"
            f"提案：{payload.get('proposal_key')} → Gen {payload.get('generation')} "
            f"({payload.get('new_primary_key')}) 已以白板身份上线主线！\n"
            f"前代 {payload.get('previous_primary_key')} 已平滑休眠退役。\n"
            f"适应度：{payload.get('parent_fitness')} → {payload.get('offspring_fitness')} | "
            f"欲望指标演化：{'是' if payload.get('mutates_drive_metrics') else '否'}\n"
            f"一键回滚快照标签：{payload.get('rollback_tag') or '无'}"
        ), None
    if kind == "evolution_rolled_back":
        return (
            f"⏪ [AOC V5.0 一键回滚完成]\n"
            f"已从 {payload.get('previous_primary_key')} 回滚恢复至 Gen {payload.get('generation')} "
            f"({payload.get('new_primary_key')})。\n"
            f"原因：{payload.get('reason', '管理员触发')}\n"
            f"快照标签：{payload.get('rollback_tag') or '无'}"
        ), None
    if kind == "evolution_arena_culled":
        reasons = "; ".join(str(r) for r in (payload.get("reasons") or []))[:260]
        text = (
            f"🗑️ [AOC V5.0 沙箱竞技淘汰并已清理]\n"
            f"提案：{payload.get('proposal_key')} | 候选：{payload.get('candidate_key')}\n"
            f"适应度对比：父代 {payload.get('parent_fitness')} vs 子代 {payload.get('offspring_fitness')} "
            f"({payload.get('fitness_delta')})\n"
            f"淘汰原因：{reasons}\n"
            f"零残留 Culling GC：{'✅ 已清空容器/Schema/工作区' if payload.get('zero_residual') else '⚠️ 待重试恢复'}"
        )
        return text, None
    return f"[AOC 通知] {payload.get('message') or payload}", None


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

    async def edit_message_reply_markup(
        self,
        chat_id: str,
        message_id: int,
        reply_markup: dict[str, Any] | None,
    ) -> None:
        """Best-effort inline keyboard update after operator selects an outreach option."""
        if not self.configured or not chat_id or not message_id:
            return
        try:
            await self._post(
                "editMessageReplyMarkup",
                {
                    "chat_id": chat_id,
                    "message_id": int(message_id),
                    "reply_markup": reply_markup or {"inline_keyboard": []},
                },
            )
        except Exception as exc:
            logger.warning("telegram.edit_reply_markup_failed", error=str(exc))


async def _dispatch_human_bridge_callback(
    *,
    human_bridge: HumanBridge | None,
    notifier: TelegramNotifier | None,
    lifecycle: Any,
    raw_action: str,
    chat_id: str,
    username: str,
    callback_id: str,
    message_id: int | None,
    update_id: int | None,
) -> dict[str, Any] | None:
    """Shared handler for `hb:<card_id>:<index|other>` inline card callbacks across Webhook & Poller."""
    parts = raw_action.split(":", 2)
    if len(parts) != 3 or parts[0].strip().lower() != "hb":
        return None
    card_id = parts[1].strip()
    choice = parts[2].strip().lower()
    if not card_id:
        return {"ok": False, "kind": "human_card_invalid", "reason": "missing card_id"}
    if human_bridge is None:
        if notifier is not None and callback_id:
            await notifier.answer_callback_query(callback_id, "AOC 人机协作桥接未启用")
        return {"ok": False, "kind": "human_card_unavailable", "card_id": card_id}

    if choice == "other":
        card = await human_bridge.get_outreach_card(card_id)
        if card is None:
            if notifier is not None and callback_id:
                await notifier.answer_callback_query(callback_id, "该选项卡已过期，请直接发送文字指令")
            return {"ok": True, "kind": "human_card_expired", "card_id": card_id}
        val = card.get("value") if isinstance(card.get("value"), dict) else {}
        title = str(val.get("title") or "指定话题")
        prompt_msg_id: int | None = None
        if notifier is not None and chat_id:
            sent = await notifier._post(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": f"✏️ 请直接回复你对「{title}」的想法或指令（发送文字即可，我会自动关联此话题并唤醒 AOC）：",
                    "reply_markup": {
                        "force_reply": True,
                        "selective": True,
                        "input_field_placeholder": "请直接输入你的建议或决定...",
                    },
                },
            )
            res_obj = sent.get("result") if isinstance(sent, dict) else None
            if isinstance(res_obj, dict) and isinstance(res_obj.get("message_id"), int):
                prompt_msg_id = res_obj["message_id"]
        await human_bridge.mark_awaiting_custom_reply(
            card_id, chat_id=chat_id, prompt_message_id=prompt_msg_id
        )
        if notifier is not None and callback_id:
            await notifier.answer_callback_query(callback_id, "请直接输入你的自定义回复")
        return {"ok": True, "kind": "human_card_other", "card_id": card_id}

    try:
        idx = int(choice)
    except ValueError:
        if notifier is not None and callback_id:
            await notifier.answer_callback_query(callback_id, "无效的选项编号")
        return {"ok": False, "kind": "human_card_invalid_choice", "card_id": card_id}

    try:
        res = await human_bridge.resolve_outreach_choice(
            card_id, idx, sender=username, chat_id=chat_id, update_id=update_id
        )
    except (LookupError, IndexError):
        if notifier is not None and callback_id:
            await notifier.answer_callback_query(callback_id, "该选项卡或选项已失效，请直接发送文字指令")
        return {"ok": True, "kind": "human_card_expired", "card_id": card_id}

    already = bool(res.get("already_selected"))
    opt = res["option"]
    if not already and lifecycle is not None:
        try:
            lifecycle.wake(reason=f"telegram card choice from {username}")
        except Exception:
            pass

    if notifier is not None and callback_id:
        toast = "你已选择过该选项" if already else f"已选择：{opt['label'][:60]}"
        await notifier.answer_callback_query(callback_id, toast)

    if not already and notifier is not None:
        if message_id is not None and chat_id:
            await notifier.edit_message_reply_markup(
                chat_id,
                message_id,
                {
                    "inline_keyboard": [
                        [{"text": f"✅ 已选：{opt['label'][:24]}", "callback_data": f"hb:{card_id}:{idx}"}],
                        [{"text": "✏️ 补充/修改（其它答案）", "callback_data": f"hb:{card_id}:other"}],
                    ]
                },
            )
        if chat_id:
            await notifier._post(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": (
                        f"✅ 已收到你的选择\n"
                        f"主题：{res['title']}\n"
                        f"决定：{opt['label']}\n\n"
                        f"已唤醒 AOC，将在下一轮思考中优先按此执行。"
                    ),
                },
            )
    return {"ok": True, "kind": "human_card_choice", "card_id": card_id, "option_index": idx, "result": res}


class TelegramWebhookHandler:
    """Authenticates Telegram Webhook callbacks and routes approvals through ApprovalService."""

    def __init__(
        self,
        settings: TelegramSettings,
        approval_service: ApprovalService,
        repo: StateRepository,
        notifier: TelegramNotifier | None = None,
        proposal_manager: Any = None,
        succession_manager: Any = None,
        human_bridge: HumanBridge | None = None,
        lifecycle: Any = None,
    ) -> None:
        self.settings = settings
        self.approval_service = approval_service
        self.repo = repo
        self.notifier = notifier
        self.proposal_manager = proposal_manager
        self.succession_manager = succession_manager
        self.human_bridge = human_bridge
        self.lifecycle = lifecycle

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

        cb_message_id: int | None = None
        if isinstance(cb, dict):
            callback_id = str(cb.get("id") or "")
            frm = cb.get("from") or {}
            cb_msg = cb.get("message") or {}
            chat = cb_msg.get("chat") or {}
            cb_message_id = cb_msg.get("message_id") if isinstance(cb_msg.get("message_id"), int) else None
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
            if len(parts) >= 2 and parts[0].lower() in {
                "/approve",
                "/reject",
                "/evo_approve",
                "/evo_reject",
                "/evo_promote",
            }:
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

        if isinstance(cb, dict) and raw_action.lower().startswith("hb:"):
            hb_res = await _dispatch_human_bridge_callback(
                human_bridge=self.human_bridge,
                notifier=self.notifier,
                lifecycle=self.lifecycle,
                raw_action=raw_action,
                chat_id=chat_id,
                username=username,
                callback_id=callback_id,
                message_id=cb_message_id,
                update_id=update.get("update_id") if isinstance(update.get("update_id"), int) else None,
            )
            if hb_res is not None:
                return hb_res

        if ":" not in raw_action:
            return {"ok": True, "ignored": True, "reason": "no approval command found"}

        action, nonce = raw_action.split(":", 1)
        action = action.strip().lower()
        nonce = nonce.strip()
        if action in {"evo_approve", "evo_reject"} and nonce and self.proposal_manager is not None:
            approver = f"telegram:{username}({user_id})"
            if action == "evo_approve":
                evo_res = await self.proposal_manager.approve_proposal(
                    nonce, approver=approver, note="Telegram 审批通过"
                )
            else:
                evo_res = await self.proposal_manager.reject_proposal(
                    nonce, approver=approver, note="Telegram 拒绝"
                )
            if self.notifier is not None and callback_id:
                await self.notifier.answer_callback_query(
                    callback_id, f"AOC 演化提案已处理，状态：{evo_res.get('stage')}"
                )
            return {"ok": True, "kind": "evolution_proposal", "action": action, "result": evo_res}

        if action == "evo_promote" and nonce and self.succession_manager is not None:
            approver = f"telegram:{username}({user_id})"
            succ_res = await self.succession_manager.promote_candidate(
                nonce, promoter=approver, note="Telegram 二次确认晋升"
            )
            if self.notifier is not None and callback_id:
                await self.notifier.answer_callback_query(
                    callback_id,
                    f"AOC 已晋升 Gen {succ_res.generation} ({succ_res.new_primary_key})",
                )
            return {
                "ok": True,
                "kind": "evolution_promote",
                "action": action,
                "result": succ_res.model_dump(mode="json"),
            }

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
            await self.notifier.answer_callback_query(callback_id, f"AOC 审批已处理，当前状态：{result.get('status')}")
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
        proposal_manager: Any = None,
        succession_manager: Any = None,
    ) -> None:
        self.settings = settings
        self.notifier = notifier
        self.webhook_handler = webhook_handler
        self.human_bridge = human_bridge
        self.repo = repo
        self.lifecycle = lifecycle
        self.status_fn = status_fn
        self.proposal_manager = proposal_manager or getattr(webhook_handler, "proposal_manager", None)
        self.succession_manager = succession_manager or getattr(webhook_handler, "succession_manager", None)
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

            cb_msg = cb.get("message") or {}
            cb_message_id = cb_msg.get("message_id") if isinstance(cb_msg.get("message_id"), int) else None
            raw_action = str(cb.get("data") or "").strip()
            if raw_action.lower().startswith("hb:"):
                hb_res = await _dispatch_human_bridge_callback(
                    human_bridge=self.human_bridge,
                    notifier=self.notifier,
                    lifecycle=self.lifecycle,
                    raw_action=raw_action,
                    chat_id=chat_id,
                    username=username,
                    callback_id=str(cb.get("id") or ""),
                    message_id=cb_message_id,
                    update_id=update_id if isinstance(update_id, int) else None,
                )
                if hb_res is not None:
                    return hb_res
            if ":" in raw_action:
                action, nonce = raw_action.split(":", 1)
                action = action.strip().lower()
                if action in {"evo_approve", "evo_reject"} and nonce.strip() and self.proposal_manager is not None:
                    approver = f"telegram:{username}({user_id})"
                    try:
                        evo_res = (
                            await self.proposal_manager.approve_proposal(
                                nonce.strip(), approver=approver, note="Telegram 按钮审批通过"
                            )
                            if action == "evo_approve"
                            else await self.proposal_manager.reject_proposal(
                                nonce.strip(), approver=approver, note="Telegram 按钮拒绝"
                            )
                        )
                        msg_reply = f"AOC 演化提案已处理，状态：{evo_res.get('stage')}"
                    except Exception as exc:
                        evo_res = {"error": str(exc)}
                        msg_reply = f"演化提案处理失败：{exc}"
                    cb_id = str(cb.get("id") or "")
                    if cb_id:
                        await self.notifier.answer_callback_query(cb_id, msg_reply)
                    return {"ok": True, "kind": "evolution_proposal", "action": action, "result": evo_res}
                if action == "evo_promote" and nonce.strip() and self.succession_manager is not None:
                    approver = f"telegram:{username}({user_id})"
                    try:
                        succ_res = await self.succession_manager.promote_candidate(
                            nonce.strip(), promoter=approver, note="Telegram 按钮晋升"
                        )
                        res_d = succ_res.model_dump(mode="json")
                        msg_reply = f"🌱 已晋升 Gen {succ_res.generation} ({succ_res.new_primary_key})"
                    except Exception as exc:
                        res_d = {"error": str(exc)}
                        msg_reply = f"晋升操作失败：{exc}"
                    cb_id = str(cb.get("id") or "")
                    if cb_id:
                        await self.notifier.answer_callback_query(cb_id, msg_reply)
                    return {"ok": True, "kind": "evolution_promote", "action": action, "result": res_d}
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
                        await self.notifier.answer_callback_query(cb_id, f"AOC 审批已处理，当前状态：{result.get('status')}")
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
        if len(parts) >= 2 and cmd == "/evo_promote" and self.succession_manager is not None:
            pkey = parts[1].strip()
            note = " ".join(parts[2:]).strip()
            approver = f"telegram:{username}({user_id})"
            try:
                succ_res = await self.succession_manager.promote_candidate(pkey, promoter=approver, note=note)
                res_d = succ_res.model_dump(mode="json")
                reply_txt = (
                    f"🌱 世代交替完成：Gen {succ_res.generation} ({succ_res.new_primary_key}) 已上线，"
                    f"旧代 {succ_res.previous_primary_key} 已退役（快照：{succ_res.rollback_tag}）。"
                )
            except Exception as exc:
                res_d = {"error": str(exc)}
                reply_txt = f"❌ 晋升操作失败：{exc}"
            await self.notifier._post("sendMessage", {"chat_id": chat_id, "text": reply_txt})
            return {"ok": True, "kind": "evolution_promote_cmd", "action": "evo_promote", "result": res_d}

        if len(parts) >= 2 and cmd in {"/evo_approve", "/evo_reject"} and self.proposal_manager is not None:
            action = cmd.lstrip("/")
            pkey = parts[1].strip()
            note = " ".join(parts[2:]).strip()
            approver = f"telegram:{username}({user_id})"
            try:
                evo_res = (
                    await self.proposal_manager.approve_proposal(pkey, approver=approver, note=note)
                    if action == "evo_approve"
                    else await self.proposal_manager.reject_proposal(pkey, approver=approver, note=note)
                )
                reply_txt = f"🧬 已执行演化提案操作 {action}（{pkey}），当前状态：{evo_res.get('stage')}"
            except Exception as exc:
                evo_res = {"error": str(exc)}
                reply_txt = f"❌ 演化提案操作失败：{exc}"
            await self.notifier._post("sendMessage", {"chat_id": chat_id, "text": reply_txt})
            return {"ok": True, "kind": "evolution_proposal_cmd", "action": action, "result": evo_res}

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

        # Check if user is replying to a pending "Other (custom reply)" outreach prompt first
        pending_custom = await self.human_bridge.consume_pending_custom_reply(chat_id)
        if pending_custom is not None:
            res_custom = await self.human_bridge.record_custom_card_reply(
                pending_custom["card_id"],
                text,
                sender=username,
                chat_id=chat_id,
                update_id=update_id if isinstance(update_id, int) else None,
            )
            if self.lifecycle is not None:
                try:
                    self.lifecycle.wake(reason=f"telegram custom card reply from {username}")
                except Exception:
                    pass
            try:
                await self.notifier._post(
                    "sendMessage",
                    {
                        "chat_id": chat_id,
                        "text": f"✅ 已收到你的指令（已关联「{res_custom['title']}」）并唤醒思考循环，我会在下一轮优先阅读并执行你的建议。",
                    },
                )
            except Exception as exc:
                logger.warning("telegram.poller_reply_failed", error=str(exc))
            return {
                "ok": True,
                "kind": "guidance_custom_card",
                "card_id": pending_custom["card_id"],
                "guidance_key": res_custom["guidance"]["key"],
            }

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
