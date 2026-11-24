"""V5.0.1 tests: Plain-Chinese Telegram decision cards, inline options, and guided 'Other' reply."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.agents.registry import AgentRegistry
from app.cognition.brain import CognitionBrain
from app.cognition.interfaces import EvaluationContext, EvaluationResult, GoalProposal
from app.core.config import GovernanceSettings, TelegramSettings
from app.core.enums import DriveType
from app.drives.interfaces import DriveDecision, DriveState
from app.governance.approval import ApprovalService
from app.governance.policy import GovernanceManager
from app.notifications.human_bridge import HumanBridge
from app.notifications.telegram import (
    TelegramNotifier,
    TelegramPoller,
    TelegramWebhookHandler,
    format_notification_message,
)
from app.orchestration.orchestrator import AgentOrchestrator
from app.perception.interfaces import Observation, utcnow
from app.storage.database import Database
from app.storage.models import Base
from app.storage.outbox import OutboxEventType
from app.storage.repository import StateRepository


async def _init_db(db: Database) -> None:
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def test_v5_telegram_option_card_formatting_and_defaults():
    # 1. Custom ask_human decision card renders plain headings, option summaries, and canonical 'Other' button
    text, markup = format_notification_message(
        {
            "kind": "ask_human",
            "card_id": "abc12345",
            "title": "无法抓取某配置文档正文",
            "message": "刚才尝试读取配置页时只拿到目录列表，缺核心参数说明。",
            "reason": "目标站点只开放了索引页。",
            "expected_benefit": "补全后能自动核对安全隔离规则。",
            "options": [
                {"label": "允许抓官网全文 (推荐)", "instruction": "临时放行 docs.example.org 只读抓取"},
                {"label": "直接读本地配置文件", "instruction": "改为分析仓库内已有配置文件"},
            ],
        }
    )
    assert text.startswith("💬 【需要你拍板】无法抓取某配置文档正文")
    assert "📌 情况说明" in text
    assert "💡 原因说明" in text
    assert "🎯 解决后收益" in text
    assert "A. 允许抓官网全文 (推荐)" in text
    assert "B. 直接读本地配置文件" in text
    assert markup is not None
    rows = markup["inline_keyboard"]
    assert len(rows) == 3
    assert rows[0][0]["callback_data"] == "hb:abc12345:0"
    assert rows[1][0]["callback_data"] == "hb:abc12345:1"
    assert rows[2][0]["callback_data"] == "hb:abc12345:other"
    assert "其它答案" in rows[2][0]["text"]
    for row in rows:
        for btn in row:
            assert len(btn["callback_data"].encode("utf-8")) <= 64

    # 2. Filtering LLM-injected '其它'/'其他' and falling back to safe defaults when <2 valid options remain
    fallback_ask = HumanBridge._normalize_options(
        "ask_human",
        [{"label": "其它答案", "instruction": "x"}, {"label": "仅此一项", "instruction": "y"}],
    )
    assert len(fallback_ask) == 3
    assert all("其它" not in o["label"] and "其他" not in o["label"] for o in fallback_ask)

    # 3. Milestone card formatting
    ms_text, ms_markup = format_notification_message(
        {
            "kind": "milestone_report",
            "card_id": "ms112233",
            "title": "已完成某框架版本排查",
            "message": "确认当前版本不受该更新影响，无需你手动调整。",
            "expected_benefit": "可关闭旧追踪项并继续探索。",
            "options": HumanBridge._normalize_options("milestone_report", None),
        }
    )
    assert ms_text.startswith("✨ 【阶段进展】已完成某框架版本排查")
    assert "📌 核心成果" in ms_text
    assert "🎯 实际价值" in ms_text
    assert ms_markup is not None
    assert ms_markup["inline_keyboard"][-1][0]["callback_data"] == "hb:ms112233:other"

    # 4. Legacy payload without card_id / options still renders gracefully
    leg_text, leg_markup = format_notification_message(
        {"kind": "ask_human", "title": "旧版求助", "message": "纯文本求助内容"}
    )
    assert "旧版求助" in leg_text and "纯文本求助内容" in leg_text
    assert leg_markup is None


@pytest.mark.asyncio
async def test_v5_telegram_poller_option_click_and_custom_other_reply_flow(tmp_path: Path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'v5_tg_cards.db'}")
    await _init_db(db)
    try:
        repo = StateRepository(db)
        gov = GovernanceManager(GovernanceSettings(workspace_root=str(tmp_path / "ws")), repo)
        reg = AgentRegistry()
        orch = AgentOrchestrator(registry=reg, governance=gov, repo=repo)
        app_svc = ApprovalService(repo, orch)
        hb = HumanBridge(repo=repo, enabled=True, cooldown_minutes=1, topic_dedup_hours=1.0)

        woke: list[str] = []

        class _StubLifecycle:
            def wake(self, reason: str = "") -> None:
                woke.append(reason)

        # Card 1: ask_human with 2 concrete options
        r1 = await hb.maybe_send_outreach(
            kind="ask_human",
            topic_key="searxng_doc",
            title="无法读取配置文档正文",
            message="目前只能看到目录，看不到具体配置参数。",
            reason="页面被折叠为索引",
            expected_benefit="补全后可自动检查风险配置",
            options=[
                {"label": "允许抓官网全文 (推荐)", "instruction": "临时开放 docs.searxng.org 只读抓取"},
                {"label": "改读本地现有配置", "instruction": "直接检查仓库里的 config 文件"},
            ],
        )
        assert r1["sent"] is True
        card1_id = r1["card_id"]
        assert r1["options_count"] == 2

        outbox_rows = await repo.list_outbox_events(event_type=OutboxEventType.NOTIFICATION_DISPATCH)
        assert len(outbox_rows) == 1
        card_text, card_kb = format_notification_message(outbox_rows[0]["payload"])
        assert "【需要你拍板】" in card_text
        assert card_kb["inline_keyboard"][0][0]["callback_data"] == f"hb:{card1_id}:0"

        calls: list[tuple[str, dict[str, Any]]] = []

        def _mock_tg(request: httpx.Request) -> httpx.Response:
            method = request.url.path.rsplit("/", 1)[-1]
            body = json.loads(request.content.decode("utf-8")) if request.content else {}
            calls.append((method, body))
            if method == "sendMessage":
                return httpx.Response(200, json={"ok": True, "result": {"message_id": 900 + len(calls)}})
            return httpx.Response(200, json={"ok": True, "result": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(_mock_tg)) as client:
            tg_cfg = TelegramSettings(
                enabled=True,
                polling_enabled=True,
                bot_token=SecretStr("123:TEST"),
                allowed_chat_ids=["777"],
                allowed_user_ids=["888"],
            )
            notifier = TelegramNotifier(tg_cfg, http_client=client)
            wh = TelegramWebhookHandler(
                tg_cfg, app_svc, repo, notifier=notifier, human_bridge=hb, lifecycle=_StubLifecycle()
            )
            poller = TelegramPoller(
                settings=tg_cfg,
                notifier=notifier,
                webhook_handler=wh,
                human_bridge=hb,
                repo=repo,
                lifecycle=_StubLifecycle(),
            )

            # A. User taps Option A (`hb:<card1_id>:0`)
            cb_res = await poller.handle_polled_update(
                {
                    "update_id": 501,
                    "callback_query": {
                        "id": "cbq-1",
                        "from": {"id": 888, "username": "joel"},
                        "message": {"message_id": 42, "chat": {"id": 777}},
                        "data": f"hb:{card1_id}:0",
                    },
                }
            )
            assert cb_res["ok"] is True
            assert cb_res["kind"] == "human_card_choice"
            assert cb_res["option_index"] == 0
            assert len(woke) == 1

            # Verify answerCallbackQuery + editMessageReplyMarkup + sendMessage confirmation
            assert any(m == "answerCallbackQuery" and "允许抓官网全文" in b["text"] for m, b in calls)
            assert any(
                m == "editMessageReplyMarkup"
                and b["message_id"] == 42
                and "✅ 已选：允许抓官网全文" in b["reply_markup"]["inline_keyboard"][0][0]["text"]
                for m, b in calls
            )
            assert any(m == "sendMessage" and "✅ 已收到你的选择" in b["text"] for m, b in calls)

            pending_g = await hb.list_pending_guidance(limit=5)
            assert len(pending_g) == 1
            assert "无法读取配置文档正文" in pending_g[0]["text"]
            assert "临时开放 docs.searxng.org 只读抓取" in pending_g[0]["text"]

            # B. Duplicate tap on same option is idempotent (no duplicate guidance, wakes=1)
            dup_res = await poller.handle_polled_update(
                {
                    "update_id": 502,
                    "callback_query": {
                        "id": "cbq-2",
                        "from": {"id": 888, "username": "joel"},
                        "message": {"message_id": 42, "chat": {"id": 777}},
                        "data": f"hb:{card1_id}:0",
                    },
                }
            )
            assert dup_res["result"]["already_selected"] is True
            assert len(await hb.list_pending_guidance(limit=5)) == 1
            assert len(woke) == 1

            # C. Card 2 -> tap '✏️ 其它答案（自定义输入）' -> ForceReply -> Joel types custom instruction
            r2 = await hb.maybe_send_outreach(
                kind="ask_human",
                topic_key="cache_strategy",
                title="请确认研究方向优先级",
                message="接下来有两条路，不确定你想让我先做哪条。",
                force=True,
            )
            card2_id = r2["card_id"]
            calls.clear()

            other_res = await poller.handle_polled_update(
                {
                    "update_id": 503,
                    "callback_query": {
                        "id": "cbq-3",
                        "from": {"id": 888, "username": "joel"},
                        "message": {"message_id": 43, "chat": {"id": 777}},
                        "data": f"hb:{card2_id}:other",
                    },
                }
            )
            assert other_res["ok"] is True
            assert other_res["kind"] == "human_card_other"
            force_msgs = [b for m, b in calls if m == "sendMessage" and (b.get("reply_markup") or {}).get("force_reply")]
            assert len(force_msgs) == 1
            assert "请确认研究方向优先级" in force_msgs[0]["text"]

            # User types custom answer
            calls.clear()
            custom_reply = await poller.handle_polled_update(
                {
                    "update_id": 504,
                    "message": {
                        "from": {"id": 888, "username": "joel"},
                        "chat": {"id": 777},
                        "text": "两条都暂缓，先去检查今天 Codex 为什么运行变慢了。",
                    },
                }
            )
            assert custom_reply["ok"] is True
            assert custom_reply["kind"] == "guidance_custom_card"
            assert custom_reply["card_id"] == card2_id
            assert any(m == "sendMessage" and "已关联「请确认研究方向优先级」" in b["text"] for m, b in calls)

            g_all = await hb.list_pending_guidance(limit=5)
            assert len(g_all) == 2
            assert any("【创作者 Joel 对「请确认研究方向优先级」的自定义回复】" in g["text"] for g in g_all)

            card2_row = await hb.get_outreach_card(card2_id)
            assert card2_row is not None and card2_row["status"] == "answered"

            # D. Next unrelated text message does NOT re-bind to card2 (pending state was consumed)
            followup = await poller.handle_polled_update(
                {
                    "update_id": 505,
                    "message": {
                        "from": {"id": 888, "username": "joel"},
                        "chat": {"id": 777},
                        "text": "顺便多留意一下 vLLM 的最新发布。",
                    },
                }
            )
            assert followup["ok"] is True
            assert followup["kind"] == "guidance"
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_v5_brain_outreach_prompt_mandates_plain_chinese_and_options():
    from app.cognition.interfaces import CognitiveContext

    seen_prompts: list[tuple[str, str]] = []

    class _SpyGateway:
        async def complete_structured(self, *, operation: str, system_prompt: str, user_payload: Any, schema_cls: Any):
            seen_prompts.append((operation, system_prompt))
            if schema_cls is GoalProposal:
                return GoalProposal(should_act=False)
            return EvaluationResult(goal_achieved=True, summary="ok")

    brain = CognitionBrain(_SpyGateway())
    now = utcnow()
    obs = Observation(timestamp=now, confidence=1.0)
    dec = DriveDecision(
        selected_drive=DriveType.CURIOSITY.value,
        priority_score=0.5,
        alternative_drives=[],
        reason="curiosity",
        constraints={},
        timestamp=now,
        scores={},
    )
    await brain.generate_goal(
        CognitiveContext(
            observation=obs,
            drives=[DriveState(drive_type="curiosity", satisfaction=0.5, urgency=0.5, confidence=1.0)],
            decision=dec,
            human_outreach_available=True,
        )
    )
    await brain.evaluate(
        EvaluationContext(
            goal={"title": "test"},
            plan={"objective": "test"},
            execution={"status": "SUCCEEDED"},
            verification_passed=True,
            pre_observation=obs,
            post_observation=obs,
        )
    )
    for op in ("generate_goal", "evaluate"):
        sp = next(s for o, s in seen_prompts if o == op)
        assert "Simplified Chinese" in sp and "简体中文" in sp
        assert "options:[{label, instruction}]" in sp
        assert "jargon" in sp
