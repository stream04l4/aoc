"""V5.1 M3 tests: Long-Term Topic Diversity Observability, Control Center Social Panel,
Config Social Introspection (preserving 12 domains), and Extended Soak Hygiene.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.api.app import create_app
from app.core.config import (
    ApiSettings,
    DatabaseSettings,
    EmbeddingSettings,
    GovernanceSettings,
    LLMSettings,
    MemorySettings,
    PerceptionSettings,
    RunnerSettings,
    Settings,
    SocialSettings,
    TelegramSettings,
)
from app.core.enums import DriveType, GoalStatus
from app.core.runtime import OrganismRuntime
from app.observability.topic_diversity import (
    TopicDiversityAnalyzer,
    compute_max_streak,
    compute_shannon_entropy,
    extract_topic_bucket,
)
from app.perception.interfaces import utcnow
from app.storage.database import Database
from app.storage.models import Base, DecisionLog, SkillRegistryRow
from app.storage.repository import StateRepository
from scripts.v5_soak_check import run_v5_soak_check


async def _init_db(db: Database) -> None:
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _healthy_llm_probe() -> dict[str, Any]:
    return {"available": True, "latency_ms": 3.0, "model": "stub-m3"}


def test_v5_1_m3_topic_diversity_math_helpers():
    # 1. Deterministic topic bucketing
    assert (
        extract_topic_bucket(
            title="Investigate RSS world",
            related_key="world:rss-postgres_notes",
            expected_outcome={"exploration_domain": "database_internals"},
        )
        == "database_internals"
    )
    assert extract_topic_bucket(title="Check hook", related_key="inv:vllm_kv_cache") == "vllm_kv_cache"
    bilingual_bucket = extract_topic_bucket(title="调研 SearXNG 代理超时问题", tags=["searxng", "telegram"])
    assert "searxng" in bilingual_bucket

    # 2. Shannon entropy & normalization
    assert compute_shannon_entropy({}) == (0.0, 0.0)
    assert compute_shannon_entropy({"only": 1}) == (0.0, 1.0)
    assert compute_shannon_entropy({"only": 5}) == (0.0, 0.0)
    ent, norm = compute_shannon_entropy({"a": 2, "b": 2, "c": 2, "d": 2})
    assert ent == pytest.approx(2.0)
    assert norm == pytest.approx(1.0)

    # 3. Streak calculation
    assert compute_max_streak([]) == (0, None)
    assert compute_max_streak(["a", "b", "b", "b", "b", "c", "b"]) == (4, "b")


@pytest.mark.asyncio
async def test_v5_1_m3_topic_diversity_analyzer_narrowing_vs_diverse(tmp_path: Path):
    # 1. Narrowing pattern DB
    db_n = Database(f"sqlite+aiosqlite:///{tmp_path / 'td_narrow.db'}")
    await _init_db(db_n)
    try:
        repo = StateRepository(db_n)
        ident = await repo.get_or_create_identity("AOC-TD-NARROW", "5.1.0", {})
        cid, _ = await repo.start_cycle(ident["id"], "ACTIVE")

        # 5 identical topics + 1 dialogue-adopted topic inside 6h
        for i in range(5):
            await repo.create_goal(
                ident["id"],
                cid,
                drive_type=DriveType.CURIOSITY.value,
                title=f"Analyze postgres outbox lock #{i}",
                description="postgres outbox check",
                success_criteria=[],
                expected_outcome={},
                related_key="inv:postgres_outbox",
                status=GoalStatus.ACHIEVED.value,
            )
        await repo.create_goal(
            ident["id"],
            cid,
            drive_type=DriveType.REPRODUCTION.value,
            title="Study vllm prefix caching",
            description="dialogue sparked goal",
            success_criteria=[],
            expected_outcome={"from_dialogue": True},
            related_key="tg:vllm_prefix_cache",
            status=GoalStatus.ACHIEVED.value,
        )
        await repo.add_memory_index(
            memory_type="semantic",
            title="Outbox lock note",
            summary="PostgreSQL skip locked notes",
            content_hash="td-h1",
            source="system:research",
            tags=["postgres"],
        )
        await repo.add_memory_index(
            memory_type="episodic",
            title="Joel dialogue",
            summary="Joel prefers concise plain Chinese updates",
            content_hash="td-h2",
            source="dialogue:telegram",
            tags=["dialogue", "telegram"],
        )
        async with db_n.session() as s:
            s.add(
                SkillRegistryRow(
                    skill_key="outbox_inspector",
                    version="1.0.0",
                    capability_key="postgres_ops",
                    description="inspect outbox",
                    status="VALIDATED",
                )
            )

        rep = await TopicDiversityAnalyzer(db_n).analyze()
        assert rep["read_only"] is True
        assert {"6h", "24h", "7d"} <= set(rep["windows"].keys())
        assert rep["summary"]["focus_pattern"] == "narrowing"
        assert rep["summary"]["max_consecutive_same_topic_streak"] == 5

        w6 = rep["windows"]["6h"]
        assert w6["goal_count"] == 6
        assert w6["unique_topic_count"] == 2
        assert w6["top3_concentration_ratio"] == pytest.approx(1.0)
        assert w6["origin_breakdown"]["dialogue_adopted_count"] == 1
        assert w6["origin_breakdown"]["self_driven_count"] == 5
        assert w6["memory_breakdown"]["social_dialogue"] == 1
        assert w6["memory_breakdown"]["technical_or_research"] == 1
        assert w6["skill_activity"]["total_validated_skills"] == 1
    finally:
        await db_n.dispose()

    # 2. Diverse pattern DB
    db_d = Database(f"sqlite+aiosqlite:///{tmp_path / 'td_diverse.db'}")
    await _init_db(db_d)
    try:
        repo = StateRepository(db_d)
        ident = await repo.get_or_create_identity("AOC-TD-DIV", "5.1.0", {})
        cid, _ = await repo.start_cycle(ident["id"], "ACTIVE")
        for topic in ("cuda_kernels", "rss_ranking", "sqlite_wal", "httpx_tls", "onnx_quant", "skill_sandbox"):
            await repo.create_goal(
                ident["id"],
                cid,
                drive_type=DriveType.CURIOSITY.value,
                title=f"Explore {topic}",
                description="broad curiosity",
                success_criteria=[],
                expected_outcome={},
                related_key=topic,
                status=GoalStatus.ACHIEVED.value,
            )
        rep_d = await TopicDiversityAnalyzer(db_d).analyze()
        assert rep_d["summary"]["focus_pattern"] == "diverse"
        assert rep_d["summary"]["normalized_diversity"] == pytest.approx(1.0)
        assert rep_d["windows"]["24h"]["novel_topic_rate"] == pytest.approx(1.0)
    finally:
        await db_d.dispose()


@pytest.mark.asyncio
async def test_v5_1_m3_api_social_panel_topic_diversity_config_and_soak(tmp_path: Path):
    admin_secret = "m3-super-admin-token-999"
    llm_secret = "m3-llm-secret-token-888"
    bot_secret = "999888:BOT_SECRET_M3_777"
    wh_secret = "m3-webhook-secret-666"
    db_file = tmp_path / "m3_api_soak.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"

    db = Database(db_url)
    await _init_db(db)

    settings = Settings(
        api=ApiSettings(host="127.0.0.1", auth_enabled=True, admin_key=SecretStr(admin_secret)),
        database=DatabaseSettings(url=db_url),
        llm=LLMSettings(base_url="http://127.0.0.1:8000/v1", api_key=SecretStr(llm_secret)),
        memory=MemorySettings(provider="none", embedding_dim=512),
        embedding=EmbeddingSettings(provider="hash_fallback", dimensions=512),
        governance=GovernanceSettings(workspace_root=str(tmp_path / "ws")),
        perception=PerceptionSettings(llm_probe=False),
        runner=RunnerSettings(enabled=False),
        telegram=TelegramSettings(
            enabled=True,
            bot_token=SecretStr(bot_secret),
            webhook_secret=SecretStr(wh_secret),
            allowed_chat_ids=["777"],
        ),
        social=SocialSettings(enabled=True, auto_enable_affect=True, min_proactive_interval_minutes=30),
    )

    rt = OrganismRuntime(settings=settings, db=db, auto_load_agents=False, llm_probe_fn=_healthy_llm_probe)
    app = create_app(settings=settings, db=db, runtime=rt, start_scheduler=False)

    try:
        async with app.router.lifespan_context(app):
            cid, _ = await rt.repo.start_cycle(rt.organism_id, "ACTIVE")
            for tp in ("pg_outbox", "skill_sandbox", "vector_recall", "arxiv_feeds"):
                await rt.repo.create_goal(
                    rt.organism_id,
                    cid,
                    drive_type=DriveType.CURIOSITY.value,
                    title=f"Investigate {tp}",
                    description=tp,
                    success_criteria=[],
                    expected_outcome={},
                    related_key=tp,
                    status=GoalStatus.ACHIEVED.value,
                )

            await rt.repo.upsert_world_item(
                kind="human_inbox",
                key="tg:777:901",
                value={"chat_id": "777", "sender": "joel", "text": "今晚在测啥？", "message_id": 901},
                status="open",
            )
            await rt.repo.upsert_world_item(
                kind="social_deferred_reply",
                key="def:900",
                value={
                    "chat_id": "777",
                    "sender": "joel",
                    "original_text": "那个权重改完咋样",
                    "reason": "在跑回归验证",
                    "deferred_activity": "跑测试中",
                    "defer_count": 1,
                    "due_at": (utcnow() + timedelta(minutes=12)).isoformat(),
                },
                status="open",
            )
            await rt.repo.upsert_world_item(
                kind="dialogue_turn",
                key="turn:1",
                value={"role": "human", "sender": "joel", "text": "最近很稳", "timestamp": utcnow().isoformat()},
                status="recorded",
            )
            await rt.repo.upsert_world_item(
                kind="social_rhythm_state",
                key="primary",
                value={
                    "consecutive_ignored_proactive": 2,
                    "current_backoff_minutes": 120.0,
                    "pending_milestone": {"kind": "skill_crystallized", "event_id": "ex-1", "summary": "新技能验证通过"},
                    "pending_goal_hints": [{"hint": "研究 prefix cache", "sender": "joel"}],
                },
                status="active",
            )
            await rt.repo.add_memory_index(
                memory_type="episodic",
                title="对话记忆",
                summary="Joel 喜欢简明表达",
                content_hash="m3-social-mem-1",
                source="dialogue:telegram",
            )
            async with rt.db.session() as s:
                s.add(
                    DecisionLog(
                        cycle_id=cid,
                        decision_type="social_reflection",
                        selected="REACT_ONLY",
                        alternatives=["REPLY_NOW", "REPLY_LATER", "REACT_ONLY", "IGNORE"],
                        reason="PRIV_MONO_PANEL_ONLY: 刚卡住，点个表情就好。",
                        payload={
                            "trigger_source": "inbox",
                            "internal_monologue": "PRIV_MONO_PANEL_ONLY: 刚卡住，点个表情就好。",
                            "action": "REACT_ONLY",
                            "reaction_emoji": "👀",
                            "affect_snapshot": {"valence": -0.1, "arousal": 0.4, "confidence": 0.6, "frustration": 0.5, "boredom": 0.1},
                            "busy_focus_level": 0.85,
                            "memory_promoted": False,
                            "adopt_as_goal": False,
                            "delivery": {"ok": True},
                        },
                    )
                )

            headers = {"Authorization": f"Bearer {admin_secret}"}
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                # 1. Auth enforcement
                assert (await client.get("/v5/observability/topic-diversity")).status_code == 401
                assert (await client.get("/v2/control-center/social")).status_code == 401

                # 2. Topic diversity endpoint (default + custom hours=48)
                td_resp = await client.get("/v5/observability/topic-diversity?hours=48", headers=headers)
                assert td_resp.status_code == 200
                td_json = td_resp.json()
                assert td_json["read_only"] is True
                assert "custom_48h" in td_json["windows"]
                assert td_json["windows"]["24h"]["goal_count"] == 4

                # 3. Control Center social panel endpoint (with private internal_monologue)
                soc_resp = await client.get("/v2/control-center/social?limit=20", headers=headers)
                assert soc_resp.status_code == 200
                soc_json = soc_resp.json()
                assert soc_json["read_only"] is True
                assert soc_json["status"]["social_enabled"] is True
                assert soc_json["status"]["affect_enabled"] is True
                assert soc_json["status"]["current_affect"] is not None
                assert soc_json["rhythm"]["consecutive_ignored_proactive"] == 2
                assert soc_json["rhythm"]["current_backoff_minutes"] == pytest.approx(120.0)
                assert soc_json["rhythm"]["pending_milestone"]["kind"] == "skill_crystallized"
                assert soc_json["rhythm"]["pending_goal_hints_count"] == 1
                assert soc_json["queues"]["open_inbox_count"] == 1
                assert len(soc_json["queues"]["deferred_replies"]) == 1
                assert len(soc_json["queues"]["recent_dialogue_turns"]) == 1
                assert len(soc_json["recent_decisions"]) == 1
                assert "PRIV_MONO_PANEL_ONLY" in soc_json["recent_decisions"][0]["internal_monologue"]
                assert soc_json["stats"]["action_counts"]["REACT_ONLY"] == 1
                assert soc_json["stats"]["social_episodic_memory_total"] == 1

                # 4. Config Center preserves 12 domains, updates affect.enabled live, includes social.* & leaks zero secrets
                cfg_resp = await client.get("/v2/control-center/config", headers=headers)
                assert cfg_resp.status_code == 200
                cfg_json = cfg_resp.json()
                assert len(cfg_json["domains"]) == 12
                aff_dom = next(d for d in cfg_json["domains"] if d["id"] == "affect")
                aff_items = {it["key"]: it["effective_value"] for it in aff_dom["items"]}
                assert aff_items["affect.enabled"] is True
                assert aff_items["social.enabled"] is True

                for resp_txt in (td_resp.text, soc_resp.text, cfg_resp.text):
                    for sec in (admin_secret, llm_secret, bot_secret, wh_secret):
                        assert sec not in resp_txt

        # 5. Extended soak check populates v5_1_observability cleanly & flags bloated dialogue_turn windows
        soak_ok = await run_v5_soak_check(
            instances_root=str(tmp_path / "inst"),
            min_free_disk_ratio=0.01,
            db_url=db_url,
        )
        assert soak_ok["steady_state_ok"] is True
        assert soak_ok["checks"]["v5_1_observability"]["skipped"] is False
        assert soak_ok["checks"]["v5_1_observability"]["ok"] is True
        assert soak_ok["checks"]["v5_1_observability"]["dialogue_turn_rows"] == 1
        assert soak_ok["checks"]["v5_1_observability"]["topic_diversity_24h"]["goal_count"] == 4

        # Inject > 200 dialogue_turn rows -> soak hygiene guard must fail cleanly
        repo2 = StateRepository(db)
        for idx in range(205):
            await repo2.upsert_world_item(
                kind="dialogue_turn",
                key=f"leaked_turn:{idx}",
                value={"role": "human", "text": "spam"},
                status="recorded",
            )
        soak_bloated = await run_v5_soak_check(
            instances_root=str(tmp_path / "inst"),
            min_free_disk_ratio=0.01,
            db_url=db_url,
        )
        assert soak_bloated["steady_state_ok"] is False
        assert soak_bloated["checks"]["v5_1_observability"]["ok"] is False
    finally:
        await db.dispose()
