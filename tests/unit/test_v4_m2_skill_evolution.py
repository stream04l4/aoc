"""V4.0 M2 Tests: Bounded RSI Skill Evolution Engine, Canonical Naming, Semantic Dedup,
Dual Regression/Boundary Verification, Automatic & Manual Rollback, and Pruning/Merging.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

import pytest

from app.core.config import GovernanceSettings
from app.core.enums import PermissionLevel
from app.governance.policy import GovernanceManager
from app.perception.interfaces import utcnow
from app.self_model.interfaces import SkillStatus
from app.self_model.skills import SkillRegistryManager
from app.skills.evolution import (
    SkillEvolutionEngine,
    bump_semver,
    canonicalize_skill_name,
    is_canonical_skill_name,
)
from app.skills.runtime import SkillRuntime
from app.storage.database import Database
from app.storage.models import Base
from app.storage.repository import StateRepository
from app.tools.mcp_manager import MCPClientManager


async def _init_db(db: Database) -> None:
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def test_v4_m2_canonical_naming_and_semver():
    """Verify truncated goal sentences normalize cleanly into `<domain>.<capability>.<operation>`."""
    c1 = canonicalize_skill_name(
        "create_and_run_a_stdlib_only_l1_curiosity_gap_ledger_diagnostic_sna", "coding", "reconcile curiosity gap ledger"
    )
    assert c1 == "cognition.coding.curiosity_gap_reconcile"
    assert is_canonical_skill_name(c1)

    c2 = canonicalize_skill_name(
        "create_a_bounded_l1_stdlib_only_checksummed_diagnostic_embed_a_t", "coding", "verify sha256 provenance"
    )
    assert c2 == "audit.coding.provenance_checksum_check"
    assert is_canonical_skill_name(c2)

    # Already canonical name is preserved verbatim
    assert canonicalize_skill_name("data.coding.count_items", "coding") == "data.coding.count_items"
    assert bump_semver("1.0.0", "patch") == "1.0.1"
    assert bump_semver("1.0.1", "minor") == "1.1.0"
    assert bump_semver("1.1.0", "major") == "2.0.0"


@pytest.mark.asyncio
async def test_v4_m2_skill_evolution_promote_regression_rollback_and_safety_guards(tmp_path: Path):
    """Verify full v1 -> vNext evolution, regression auto-rollback, L2 gate, unsafe block & parent restore."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'v4_m2_evo.db'}")
    await _init_db(db)
    try:
        repo = StateRepository(db)
        gov = GovernanceManager(GovernanceSettings(workspace_root=str(tmp_path / "ws")), repo)
        sm_cfg = {"skill_storage_dir": str(tmp_path / "skills"), "skill_test_timeout_seconds": 5.0}
        skills = SkillRegistryManager(repo=repo, governance=gov, config=sm_cfg)
        rt = SkillRuntime(repo=repo, skill_registry=skills, governance=gov)
        mcp = MCPClientManager(governance=gov, repo=repo, workspace_root=str(tmp_path / "ws"))
        mcp.attach_skill_runtime(rt)
        skills.set_on_skills_changed(mcp.sync_validated_skills_as_tools)

        evo = SkillEvolutionEngine(repo=repo, skill_registry=skills, max_versions_per_canonical=6)
        skills.evolution_engine = evo

        # Seed v1.0.0 VALIDATED skill
        v1 = await skills.register_skill(
            skill_key="data_coding_count_items",
            canonical_name="data.coding.count_items",
            version="1.0.0",
            capability_key="coding",
            description="Count item frequencies",
            artifact_code=(
                "from collections import Counter\n"
                "def execute(data: dict) -> dict:\n"
                "    items = data.get('items') or []\n"
                "    c = dict(Counter(str(x).lower() for x in items))\n"
                "    return {'counts': c, 'total': sum(c.values())}\n"
            ),
            test_spec={
                "kind": "python_assertions",
                "code": (
                    "from skill import execute\n"
                    "assert execute({'items': ['A', 'a', 'b']}) == {'counts': {'a': 2, 'b': 1}, 'total': 3}\n"
                ),
            },
            permission_level=PermissionLevel.L1.value,
        )
        await skills.validate_skill(v1["id"])
        assert mcp.get_tool("skill:data.coding.count_items") is not None

        # 1. Permission escalation L1 -> L2 is blocked immediately
        esc = await evo.evolve_skill(
            v1["id"],
            new_artifact_code="def execute(data): return {'counts': {}, 'total': 0}\n",
            new_test_code="assert True\n",
            new_permission_level="L2",
        )
        assert esc.promoted is False and esc.rolled_back is True and "escalation" in esc.reason.lower()

        # 2. Unsafe static scan code blocked before DB/disk mutation
        unsafe = await evo.evolve_skill(
            v1["id"],
            new_artifact_code="import os\ndef execute(data): os.system('rm -rf /tmp/x')\n",
            new_test_code="assert True\n",
        )
        assert unsafe.promoted is False and unsafe.rolled_back is True and "static safety" in unsafe.reason.lower()

        # 3. Regression-breaking vNext -> auto-rolled back, v1.0.0 remains active VALIDATED!
        broken = await evo.evolve_skill(
            v1["id"],
            new_artifact_code="def execute(data): return {'counts': {}, 'total': 0}\n",
            new_test_code="from skill import execute\nassert execute({})['total'] == 0\n",
            change_type="minor",
            change_summary="Broke case-insensitive counting",
        )
        assert broken.promoted is False and broken.rolled_back is True and broken.regression_passed is False
        v1_still = await repo.get_skill(uuid.UUID(v1["id"]))
        assert v1_still["status"] == SkillStatus.VALIDATED.value

        # 4. Genuine improved vNext (strips whitespace + ignores empty strings + adds boundary assertions + schema) -> PROMOTED!
        improved = await evo.evolve_skill(
            v1["id"],
            new_artifact_code=(
                "from collections import Counter\n"
                "def execute(data: dict) -> dict:\n"
                "    items = data.get('items') or []\n"
                "    cleaned = [str(x).strip().lower() for x in items if str(x).strip()]\n"
                "    c = dict(Counter(cleaned))\n"
                "    return {'counts': c, 'total': sum(c.values())}\n"
            ),
            new_test_code=(
                "from skill import execute\n"
                "assert execute({'items': ['A', 'a', 'b']}) == {'counts': {'a': 2, 'b': 1}, 'total': 3}\n"
                "assert execute({'items': [' A ', '', '   ', 'a']}) == {'counts': {'a': 2}, 'total': 2}\n"
                "assert execute({'items': []}) == {'counts': {}, 'total': 0}\n"
            ),
            change_type="minor",
            change_summary="Strip whitespace and ignore empty entries with boundary tests",
            new_input_schema={"type": "object", "required": ["items"], "properties": {"items": {"type": "array"}}},
        )
        assert improved.promoted is True
        assert improved.rolled_back is False
        assert improved.regression_passed is True
        assert improved.new_tests_passed is True
        assert improved.new_version == "1.1.1" or improved.new_version == "1.2.0" or improved.new_version.startswith("1.")

        # Base v1.0.0 is now DEPRECATED with superseded_by pointing to vNext; vNext is active VALIDATED!
        v1_after = await repo.get_skill(uuid.UUID(v1["id"]))
        vnext_row = await repo.get_skill(uuid.UUID(improved.new_skill_id))
        assert v1_after["status"] == SkillStatus.DEPRECATED.value
        assert v1_after["superseded_by"] == improved.new_skill_id
        assert vnext_row["status"] == SkillStatus.VALIDATED.value
        assert vnext_row["parent_version"] == "1.0.0"

        # Executing canonical name runs the newly promoted vNext behavior (strips whitespace!)
        exec_res = await rt.execute("data.coding.count_items", {"items": [" X ", "", "x"]})
        assert exec_res.ok is True and exec_res.output == {"counts": {"x": 2}, "total": 2}

        # 5. Rollback cleanly restores v1.0.0 back to VALIDATED and deprecates vNext!
        rb = await evo.rollback_skill_version(improved.new_skill_id, reason="post-release anomaly")
        assert rb["rolled_back"] is True
        assert rb["restored_skill_id"] == v1["id"]
        assert (await repo.get_skill(uuid.UUID(v1["id"])))["status"] == SkillStatus.VALIDATED.value
        assert (await repo.get_skill(uuid.UUID(improved.new_skill_id)))["status"] == SkillStatus.DEPRECATED.value
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_v4_m2_prune_deduplicate_and_workspace_auto_evolution_routing(tmp_path: Path):
    """Verify legacy duplicate cluster merging, low-reuse deprecation, and duplicate workspace -> vNext routing."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'v4_m2_prune.db'}")
    await _init_db(db)
    try:
        repo = StateRepository(db)
        gov = GovernanceManager(GovernanceSettings(workspace_root=str(tmp_path / "ws")), repo)
        sm_cfg = {"skill_storage_dir": str(tmp_path / "skills"), "skill_test_timeout_seconds": 5.0}
        skills = SkillRegistryManager(repo=repo, governance=gov, config=sm_cfg)
        evo = SkillEvolutionEngine(repo=repo, skill_registry=skills)
        skills.evolution_engine = evo

        # Seed 3 homogeneous truncated-sentence VALIDATED skills + 2 DRAFTs in same cluster
        for idx in range(3):
            sk = await skills.register_skill(
                skill_key=f"create_a_stdlib_only_l1_curiosity_gap_ledger_diagnostic_variant_{idx}",
                version="1.0.0",
                capability_key="coding",
                description="Reconcile curiosity gap ledger snapshot",
                artifact_code=f"def execute(d): return {{'variant': {idx}}}\n",
                test_spec={"kind": "python_assertions", "code": f"from skill import execute\nassert execute({{}})['variant'] == {idx}\n"},
            )
            await skills.validate_skill(sk["id"])
        for idx in range(2):
            await skills.register_skill(
                skill_key=f"create_a_stdlib_only_l1_curiosity_gap_ledger_broken_draft_{idx}",
                version="1.0.0",
                capability_key="coding",
                description="Broken curiosity gap ledger draft",
                artifact_code="def execute(d): return {}\n",
                test_spec={"kind": "python_assertions", "code": "assert False\n"},
            )

        # Dry run makes 0 mutations
        dry = await evo.prune_and_deduplicate_skills(dry_run=True)
        assert dry["kept_seeds_count"] == 1
        assert dry["merged_duplicates_count"] == 2
        assert dry["archived_drafts_count"] == 2
        assert len(await repo.list_skills(status=SkillStatus.VALIDATED.value)) == 3

        # Apply merges 2 duplicate VALIDATED -> DEPRECATED (with superseded_by) and archives 2 DRAFTs, preserving all 5 rows!
        applied = await evo.prune_and_deduplicate_skills(dry_run=False)
        assert applied["kept_seeds_count"] == 1
        assert applied["merged_duplicates_count"] == 2
        assert applied["archived_drafts_count"] == 2
        assert len(await repo.list_skills(limit=50)) == 5
        active_after = await repo.list_skills(status=SkillStatus.VALIDATED.value)
        assert len(active_after) == 1
        assert active_after[0]["canonical_name"] == "cognition.coding.curiosity_gap_reconcile"

        # Workspace crystallization of another curiosity-gap-ledger script evolves seed v1.0.0 -> vNext instead of duplicating!
        ws = tmp_path / "task_ws"
        ws.mkdir()
        (ws / "skill.py").write_text(
            "def execute(d):\n    return {'variant': 2, 'reconciled': True, 'count': len(d.get('gaps') or [])}\n",
            encoding="utf-8",
        )
        (ws / "test_skill.py").write_text(
            "from skill import execute\n"
            "assert execute({})['variant'] == 2\n"
            "assert execute({'gaps': ['a', 'b']})['count'] == 2\n",
            encoding="utf-8",
        )
        cryst = await skills.crystallize_from_workspace(
            workspace=str(ws),
            skill_key="create_another_curiosity_gap_ledger_reconciliation_script_long_title",
            capability_key="coding",
            description="Improved curiosity gap ledger reconciliation",
        )
        assert cryst is not None
        assert cryst["evolved"] is True
        assert cryst["promoted"] is True
        # Still ONLY 1 active VALIDATED skill (upgraded cleanly, zero duplication!)
        assert len(await repo.list_skills(status=SkillStatus.VALIDATED.value)) == 1
        assert (await repo.list_skills(status=SkillStatus.VALIDATED.value))[0]["version"] == "1.1.0"
    finally:
        await db.dispose()
