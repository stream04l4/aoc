"""V5.3 M1 tests: Failure-Case Injection, Unused VALIDATED Consolidation/GC & Source Truncation."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import update

from app.core.config import GovernanceSettings
from app.core.enums import PermissionLevel
from app.governance.policy import GovernanceManager
from app.orchestration.finalizer import TaskFinalizer
from app.orchestration.orchestrator import AgentOrchestrator
from app.perception.interfaces import Observation, utcnow
from app.perception.world.investigation import InvestigationManager
from app.self_model.interfaces import SkillStatus
from app.self_model.skills import SkillRegistryManager
from app.skills.evolution import SkillEvolutionEngine
from app.storage.database import Database
from app.storage.models import Base, SkillRegistryRow
from app.storage.repository import StateRepository


async def _init_db(db: Database) -> None:
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _dummy_skill_code(tag: str = "v1") -> str:
    return (
        "def execute(data: dict) -> dict:\n"
        f"    return {{'tag': {tag!r}, 'count': len(data.get('items') or [])}}\n"
    )


def _dummy_skill_test(tag: str = "v1") -> str:
    return (
        "from skill import execute\n"
        f"assert execute({{'items': [1, 2]}})['tag'] == {tag!r}\n"
    )


@pytest.mark.asyncio
async def test_failure_cases_injected_and_sanitized_in_workspace(tmp_path: Path):
    """stage_base_skill_in_workspace MUST write failure_cases.json with sanitized failure history."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'm1_fc.db'}")
    await _init_db(db)
    try:
        repo = StateRepository(db)
        gov = GovernanceManager(GovernanceSettings(workspace_root=str(tmp_path / "ws")), repo)
        skills = SkillRegistryManager(
            repo=repo,
            governance=gov,
            config={"skill_storage_dir": str(tmp_path / "skills"), "skill_test_timeout_seconds": 5.0},
            explicit_skill_refactor_enabled=True,
        )
        sk = await skills.register_skill(
            skill_key="mcp_meff_resolution",
            canonical_name="repository.coding.mcp_meff_resolution",
            version="1.0.0",
            capability_key="coding",
            description="Resolve MCP tool paths",
            artifact_code=_dummy_skill_code("meff"),
            test_spec={"kind": "python_assertions", "code": _dummy_skill_test("meff")},
            permission_level=PermissionLevel.L1.value,
        )
        await skills.validate_skill(sk["id"])

        # Record 2 failed invocations (one containing sensitive URL) and 1 passed invocation
        await repo.record_skill_invocation(
            skill_id=uuid.UUID(sk["id"]),
            skill_version="1.0.0",
            invocation_source="skill_first",
            passed=True,
            output_excerpt="ok",
        )
        await repo.record_skill_invocation(
            skill_id=uuid.UUID(sk["id"]),
            skill_version="1.0.0",
            invocation_source="skill_first",
            passed=False,
            error="Input schema validation failed: Missing required parameter 'target_url'",
            output_excerpt="postgresql+asyncpg://user:secret@localhost/aoc failed",
        )
        await repo.upsert_world_item(
            kind="skill_refactor_queue",
            key="refactor:repository.coding.mcp_meff_resolution",
            value={
                "skill_key": "mcp_meff_resolution",
                "canonical_name": "repository.coding.mcp_meff_resolution",
                "reason": "Missing target_url alias",
                "target_generalization": "Accept both url and target_url",
            },
            status="open",
            source="meta_reflection",
        )

        ws = tmp_path / "ws" / "task_refactor"
        ws.mkdir(parents=True)
        staged = await skills.stage_base_skill_in_workspace(
            str(ws),
            {"canonical_name": "repository.coding.mcp_meff_resolution"},
        )
        assert staged is not None
        assert staged["failure_cases_count"] == 1
        fc_path = ws / ".aoc_base_skill" / "failure_cases.json"
        assert fc_path.is_file()
        fc = json.loads(fc_path.read_text(encoding="utf-8"))
        assert fc["refactor_reason"] == "Missing target_url alias"
        assert len(fc["failure_cases"]) == 1
        assert "secret" not in fc["failure_cases"][0]["output_excerpt"]
        assert "[REDACTED_DB_URL]" in fc["failure_cases"][0]["output_excerpt"]
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_audit_and_tidy_validated_skills_protects_and_archives_safely(tmp_path: Path):
    """Verify >=7d consolidation queueing, >=14d archival GC, and strict protection of anchors/parents/reused/queued."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'm1_gc.db'}")
    await _init_db(db)
    try:
        repo = StateRepository(db)
        gov = GovernanceManager(GovernanceSettings(workspace_root=str(tmp_path / "ws")), repo)
        skills = SkillRegistryManager(
            repo=repo,
            governance=gov,
            config={"skill_storage_dir": str(tmp_path / "skills"), "skill_test_timeout_seconds": 5.0},
            explicit_skill_refactor_enabled=True,
        )
        now = utcnow()
        d10 = now - timedelta(days=10)
        d16 = now - timedelta(days=16)

        async def _make_val(
            skey: str,
            cname: str,
            cap: str,
            desc: str,
            created: Any,
            *,
            version: str = "1.0.0",
            parent_ver: str | None = None,
            reuse_cnt: int = 0,
            inv_cnt: int = 0,
        ) -> dict[str, Any]:
            row = await skills.register_skill(
                skill_key=skey,
                canonical_name=cname,
                version=version,
                capability_key=cap,
                description=desc,
                parent_version=parent_ver,
                artifact_code=_dummy_skill_code(skey),
                test_spec={"kind": "python_assertions", "code": _dummy_skill_test(skey)},
                permission_level=PermissionLevel.L1.value,
            )
            await skills.validate_skill(row["id"])
            async with db.session() as s:
                await s.execute(
                    update(SkillRegistryRow)
                    .where(SkillRegistryRow.id == uuid.UUID(row["id"]))
                    .values(
                        created_at=created,
                        updated_at=created,
                        reuse_count=reuse_cnt,
                        invocation_count=inv_cnt,
                    )
                )
            return row

        # 1. Two overlapping >=10d zero-reuse skills -> should enqueue consolidate, NOT be archived
        ov1 = await _make_val(
            "json_norm_alpha",
            "data.coding.json_norm_alpha",
            "coding",
            "Normalize JSON config schema fields",
            d10,
        )
        ov2 = await _make_val(
            "json_norm_beta",
            "data.coding.json_norm_beta",
            "coding",
            "Normalize JSON config schema fields helper",
            d10,
        )
        # 2. Old >=16d zero-reuse unrelated skill (with coding cap already having other VALIDATED skills) -> should archive!
        old_frag = await _make_val(
            "obsolete_scratch_parser",
            "data.coding.obsolete_scratch_parser",
            "coding",
            "One-off scratch markdown helper xyzzy",
            d16,
        )
        # 3. Old >=16d zero-reuse BUT sole VALIDATED skill for capability 'security_audit' -> protected!
        sole_anchor = await _make_val(
            "sole_sec_anchor",
            "audit.security.sole_anchor",
            "security_audit",
            "Sole capability anchor",
            d16,
        )
        # 4. Old >=16d parent referenced by active v1.1.0 -> protected!
        parent_sk = await _make_val(
            "chain_tool",
            "repo.coding.chain_tool",
            "coding",
            "Alpha parser utility",
            d16,
            version="1.0.0",
        )
        child_sk = await _make_val(
            "chain_tool",
            "repo.coding.chain_tool",
            "coding",
            "Alpha parser utility extended",
            now - timedelta(days=2),
            version="1.1.0",
            parent_ver="1.0.0",
        )
        # 5. Old >=16d BUT reused once -> protected!
        reused_sk = await _make_val(
            "reused_old_tool",
            "repo.coding.reused_old_tool",
            "coding",
            "Legacy CSV exporter",
            d16,
            reuse_cnt=1,
            inv_cnt=1,
        )

        summary = await skills.audit_and_tidy_validated_skills(consolidate_days=7, expiry_days=14, now=now)
        assert summary["consolidated_enqueued_count"] == 1
        assert summary["archived_count"] == 1
        assert summary["archived_skill_keys"] == ["obsolete_scratch_parser"]

        archived_row = await repo.get_skill(uuid.UUID(old_frag["id"]))
        assert archived_row["status"] == SkillStatus.DEPRECATED.value
        assert archived_row["evolution_meta"]["deprecated_reason"] == "unused_validated_gc"
        assert archived_row["evolution_meta"]["restorable_snapshot"]["skill_key"] == "obsolete_scratch_parser"

        for protected_id in (ov1["id"], ov2["id"], sole_anchor["id"], parent_sk["id"], child_sk["id"], reused_sk["id"]):
            row = await repo.get_skill(uuid.UUID(protected_id))
            assert row["status"] == SkillStatus.VALIDATED.value

        q_items = await repo.list_world_items(kind="skill_refactor_queue", status="open")
        consolidate_q = [q for q in q_items if str(q["key"]).startswith("consolidate:")]
        assert len(consolidate_q) == 1
        assert set([consolidate_q[0]["value"]["primary_skill_key"], *consolidate_q[0]["value"]["merge_candidates"]]) == {
            "json_norm_alpha",
            "json_norm_beta",
        }

        audit_ws = [r for r in await repo.list_world_items(kind="skill_lifecycle_audit") if r["key"] == "latest"]
        assert len(audit_ws) == 1
        assert audit_ws[0]["value"]["archived_count"] == 1
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_world_states_long_source_truncation_in_investigation_and_repo(tmp_path: Path):
    """Ensure >64 char investigation thread keys no longer fail DB VARCHAR(64) on world_states.source."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'm1_src.db'}")
    await _init_db(db)
    try:
        repo = StateRepository(db)
        inv = InvestigationManager(repo=repo)
        long_tkey = "inv:" + "engineering_tools_very_long_topic_name_" * 2
        res = await inv.record_discovered_channel(
            {"domain": "example.org", "entry_url": "https://example.org/docs"},
            source=f"investigation:{long_tkey}",
        )
        assert res is not None
        rows = await repo.list_world_items(kind="discovered_channel")
        assert len(rows) == 1
        assert len(rows[0]["source"]) <= 64
    finally:
        await db.dispose()
