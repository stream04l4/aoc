"""V5.3 M2 tests: Deterministic Skill Pre-Selector, 1-Shot Schema Auto-Heal & 2-Fail Auto-Queue."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.cognition.interfaces import GoalProposal
from app.core.config import GovernanceSettings
from app.core.enums import PermissionLevel
from app.governance.policy import GovernanceManager
from app.self_model.skills import SkillRegistryManager
from app.skills.matcher import SkillMatchPreSelector
from app.skills.runtime import SkillRuntime, align_input_args_to_schema
from app.storage.database import Database
from app.storage.models import Base
from app.storage.repository import StateRepository


async def _init_db(db: Database) -> None:
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def test_skill_match_preselector_bilingual_and_threshold():
    selector = SkillMatchPreSelector()
    skills = [
        {
            "skill_id": "s1",
            "skill_key": "web_html_parser",
            "canonical_name": "research.web.html_parser",
            "capability_key": "research",
            "description": "Parse HTML web page metadata, headings, and outbound links",
            "applicable_scenarios": ["网页解析 HTML meta links"],
            "status": "VALIDATED",
            "reuse_count": 3,
            "reuse_success_count": 3,
            "input_schema": {
                "type": "object",
                "required": ["target_url"],
                "properties": {"target_url": {"type": "string"}, "max_links": {"type": "integer", "default": 10}},
                "additionalProperties": False,
            },
        },
        {
            "skill_id": "s2",
            "skill_key": "git_commit_counter",
            "canonical_name": "repository.coding.git_commit_counter",
            "capability_key": "coding",
            "description": "Count git commits and authors in a repository",
            "applicable_scenarios": ["repository git stats"],
            "status": "VALIDATED",
            "reuse_count": 0,
            "reuse_success_count": 0,
            "input_schema": {"type": "object", "required": ["repo_path"], "properties": {"repo_path": {"type": "string"}}},
        },
    ]

    hit_goal = GoalProposal(
        title="解析目标网页 HTML 链接并提取 meta 摘要",
        description="Use HTML web page parser to extract outbound links and metadata.",
        drive_type="curiosity",
    )
    top = selector.select_top_match(hit_goal, skills, threshold=0.55)
    assert top is not None
    assert top.canonical_name == "research.web.html_parser"
    assert top.match_score >= 0.55
    assert top.suggested_args_template["target_url"] == "<string>"
    assert top.suggested_args_template["max_links"] == 10

    unrelated_goal = GoalProposal(
        title="Measure Olympic swimmer stroke frequency",
        description="Analyze biomechanics of freestyle swimming endurance",
        drive_type="curiosity",
    )
    assert selector.select_top_match(unrelated_goal, skills, threshold=0.55) is None


def test_align_input_args_to_schema_aliases_unwrap_coercion():
    schema = {
        "type": "object",
        "required": ["target_url", "limit"],
        "properties": {
            "target_url": {"type": "string"},
            "limit": {"type": "integer"},
            "dry_run": {"type": "boolean"},
            "mode": {"type": "string", "default": "fast"},
        },
        "additionalProperties": False,
    }
    raw = {"payload": {"url": "https://example.org", "limit": "5", "dry_run": "true", "mode": None}}
    healed, fixes = align_input_args_to_schema(raw, schema)
    assert healed == {"target_url": "https://example.org", "limit": 5, "dry_run": True, "mode": "fast"}
    assert "unwrap:payload" in fixes
    assert "alias:url->target_url" in fixes
    assert "coerce_int:limit" in fixes
    assert "coerce_bool:dry_run" in fixes
    assert "default:mode" in fixes


@pytest.mark.asyncio
async def test_skill_runtime_auto_heal_and_consecutive_fail_auto_queue(tmp_path: Path):
    """Auto-heal recovers alias mismatches once; 2 consecutive failures auto-queue skill_refactor_queue."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'm2_rt.db'}")
    await _init_db(db)
    try:
        repo = StateRepository(db)
        gov = GovernanceManager(GovernanceSettings(workspace_root=str(tmp_path / "ws")), repo)
        skills = SkillRegistryManager(
            repo=repo,
            governance=gov,
            config={"skill_storage_dir": str(tmp_path / "skills"), "skill_test_timeout_seconds": 5.0},
        )
        sk = await skills.register_skill(
            skill_key="url_echo_tool",
            canonical_name="web.coding.url_echo_tool",
            version="1.0.0",
            capability_key="coding",
            description="Echo target_url metadata",
            artifact_code=(
                "def execute(data: dict) -> dict:\n"
                "    u = data['target_url']\n"
                "    if 'boom' in u:\n"
                "        raise RuntimeError('simulated runtime error')\n"
                "    return {'echo': u}\n"
            ),
            test_spec={
                "kind": "python_assertions",
                "code": "from skill import execute\nassert execute({'target_url': 'https://a.com'})['echo'] == 'https://a.com'\n",
            },
            input_schema={
                "type": "object",
                "required": ["target_url"],
                "properties": {"target_url": {"type": "string"}},
                "additionalProperties": False,
            },
            permission_level=PermissionLevel.L0.value,
        )
        await skills.validate_skill(sk["id"])

        rt = SkillRuntime(
            repo=repo,
            skill_registry=skills,
            governance=gov,
            auto_heal_enabled=True,
            consecutive_fail_refactor_threshold=2,
        )

        # 1. Passing wrong alias key `url` auto-heals to `target_url`
        res_ok = await rt.execute("web.coding.url_echo_tool", {"url": "https://example.com"})
        assert res_ok.ok is True
        assert res_ok.auto_healed is True
        assert "alias:url->target_url" in res_ok.auto_heal_fixes
        assert res_ok.output == {"echo": "https://example.com"}

        heal_evs = await repo.list_events(limit=10, event_type="skill.auto_healed")
        assert len(heal_evs) == 1

        # 2. First runtime failure should NOT enqueue refactor yet
        res_fail1 = await rt.execute("web.coding.url_echo_tool", {"target_url": "https://boom.invalid"})
        assert res_fail1.ok is False
        assert res_fail1.refactor_queued is False
        assert len(await repo.list_world_items(kind="skill_refactor_queue", status="open")) == 0

        # 3. Second consecutive failure within 24h MUST auto-enqueue refactor
        res_fail2 = await rt.execute("web.coding.url_echo_tool", {"target_url": "https://boom2.invalid"})
        assert res_fail2.ok is False
        assert res_fail2.refactor_queued is True
        q_rows = await repo.list_world_items(kind="skill_refactor_queue", status="open")
        assert len(q_rows) == 1
        assert q_rows[0]["key"] == "refactor:web.coding.url_echo_tool"
        assert q_rows[0]["value"]["priority"] == 2
        assert q_rows[0]["value"]["consecutive_failures"] == 2

        q_evs = await repo.list_events(limit=10, event_type="skill.refactor_auto_queued")
        assert len(q_evs) == 1
    finally:
        await db.dispose()
