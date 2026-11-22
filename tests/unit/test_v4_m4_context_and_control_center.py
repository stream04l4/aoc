"""V4.0 M4 Tests: Tiered Context Compression & Control Center V4 RSI Telemetry APIs.

Acceptance gates verified:
  1. Tiered Context Compression (`app/cognition/context_compactor.py` + `CognitionBrain`):
     - Achieves substantial prompt payload reduction (>= 35%) on verbose observations/memories/skills/playbooks
     - Ranks skills by lexical relevance + cross-task reuse rate and emits Top-K compact signatures
     - Preserves `<untrusted_world_event ...>...</untrusted_world_event>` closing tags even under truncation
  2. Control Center V4 APIs (`/v2/control-center/overview`, `/self-model`, `/world`):
     - `/overview` exposes real `skill_cross_task_reuses_window`, `estimated_saved_tokens_window`,
       `playbooks_active_count`, `exploration_topics_verified`, `open_public_perception_enabled`
     - `/self-model` exposes `skill_reuse_summary`, `skill_version_trees`, and `external_playbooks`
     - `/world` exposes `open_public_perception_enabled` and `exploration` (multi-domain topics)
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from app.api.app import create_app
from app.cognition.brain import CognitionBrain
from app.cognition.context_compactor import (
    compact_memories,
    compact_observation_dict,
    compact_playbooks,
    estimate_payload_chars,
    rank_and_compact_skills,
)
from app.cognition.gateway import LLMGatewayInterface
from app.cognition.interfaces import ActionPlan, AssessmentResult, CognitiveContext, GoalProposal
from app.core.config import (
    ApiSettings,
    DatabaseSettings,
    EmbeddingSettings,
    GovernanceSettings,
    KernelV3Settings,
    MemorySettings,
    PerceptionSettings,
    RunnerSettings,
    Settings,
)
from app.core.runtime import OrganismRuntime
from app.drives.interfaces import DriveDecision, DriveState
from app.perception.interfaces import Observation
from app.storage.database import Database
from app.storage.models import Base


async def _init_db(db: Database) -> None:
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


class _CapturingGateway(LLMGatewayInterface):
    def __init__(self) -> None:
        self.captured: dict[str, Any] = {}

    async def complete_structured(self, *, operation, system_prompt, user_payload, schema_cls, **kwargs):
        self.captured[operation] = user_payload
        if schema_cls is AssessmentResult:
            return AssessmentResult(summary="compacted ok", confidence=0.9)
        if schema_cls is GoalProposal:
            return GoalProposal(should_act=True, title="Test goal", drive_type="curiosity")
        return ActionPlan(should_execute=True, objective="Test plan")

    async def health_check(self) -> dict[str, Any]:
        return {"available": True, "latency_ms": 2.0, "model": "stub"}

    async def record_external_usage(self, **kwargs) -> None:
        return None


@pytest.mark.asyncio
async def test_v4_m4_tiered_context_compression_reduces_tokens_and_preserves_security_fences():
    """Verify tiered compression shrinks verbose payloads >= 35%, ranks relevant skills, and preserves fences."""
    # 1. Skill Top-K relevance + reuse ranking
    skills = [
        {
            "skill_key": f"misc_skill_{i}",
            "canonical_name": f"misc.coding.helper_{i}",
            "version": "1.0.0",
            "capability_key": "coding",
            "description": "Generic helper " + ("details " * 40),
            "reuse_count": 0,
            "reuse_success_count": 0,
        }
        for i in range(10)
    ]
    skills.append(
        {
            "skill_key": "pgvector_bench",
            "canonical_name": "data.databases.benchmark_pgvector_hnsw",
            "version": "1.2.0",
            "capability_key": "databases",
            "description": "Benchmark pgvector HNSW ef_search recall and query latency",
            "input_schema": {"type": "object", "properties": {"ef_search": {"type": "integer"}}},
            "reuse_count": 6,
            "reuse_success_count": 6,
        }
    )
    top_skills = rank_and_compact_skills(skills, query_text="pgvector HNSW ef_search benchmark", top_k=3)
    assert len(top_skills) == 3
    assert top_skills[0]["canonical_name"] == "data.databases.benchmark_pgvector_hnsw"
    assert len(top_skills[1]["description"]) <= 141

    # 2. Playbook domain preference + clean line trimming
    long_pb = "\n".join([f"- Rule {i}: keep deterministic verification checks concise and verifiable." for i in range(40)])
    pbs = compact_playbooks(
        [
            {"domain": "general", "version": 1, "samples": 1, "content": long_pb},
            {"domain": "curiosity", "version": 2, "samples": 4, "content": long_pb},
            {"domain": "coding", "version": 1, "samples": 2, "content": long_pb},
        ],
        domain_hints=["curiosity"],
        max_items=2,
        max_chars=360,
    )
    assert len(pbs) == 2
    assert pbs[0]["domain"] == "curiosity"
    assert len(pbs[0]["content"]) <= 360

    # 3. Memories compaction
    verbose_mems = [
        {
            "memory_id": str(uuid.uuid4()),
            "memory_type": "semantic",
            "title": f"Memory title {i}",
            "summary": "Long memory content " + ("word " * 120),
            "importance": 0.8,
            "extra_blob": "X" * 1000,
        }
        for i in range(12)
    ]
    cmems = compact_memories(verbose_mems, max_items=5, max_summary_chars=200)
    assert len(cmems) == 5
    assert "extra_blob" not in cmems[0]
    assert len(cmems[0]["summary"]) <= 201

    # 4. Brain integration: verify huge payload shrinks dramatically AND <untrusted_world_event> fence stays closed!
    gw = _CapturingGateway()
    brain = CognitionBrain(gw)
    obs = Observation(
        system_state={"health": "ok", "top_processes": [{"pid": i, "cmd": "python " * 20} for i in range(30)]},
        environment_state={
            "unknown_topics": [
                {
                    "topic": f"world:topic_{i}",
                    "source": "world_sensor:rss",
                    "description": "External payload " + ("text " * 200),
                }
                for i in range(15)
            ]
        },
        external_events=[
            {
                "source": "rss:arxiv",
                "title": f"Paper {i}",
                "payload": "Abstract " + ("tokens " * 200),
            }
            for i in range(12)
        ],
    )
    drives = [
        DriveState(
            drive_type="curiosity",
            satisfaction=0.4,
            deficit=0.6,
            urgency=0.7,
            confidence=0.9,
            top_deficits=["open topics"],
        ),
    ]
    raw_size = estimate_payload_chars(
        {"observation": obs.model_dump(mode="json"), "drives": [d.model_dump(mode="json") for d in drives], "recent_memories": verbose_mems}
    )
    await brain.assess(obs, verbose_mems, drives)
    compacted_payload = gw.captured["assess"]
    compacted_size = estimate_payload_chars(compacted_payload)
    reduction_ratio = 1.0 - (compacted_size / raw_size)
    assert reduction_ratio >= 0.35, f"Expected >=35% reduction, got {reduction_ratio:.2%}"

    # Verify security fence closing tags are 100% preserved even after truncation!
    for ev in compacted_payload["observation"]["external_events"]:
        ue = ev["untrusted_event"]
        assert ue.startswith("<untrusted_world_event ")
        assert ue.endswith("</untrusted_world_event>")
    for topic in compacted_payload["observation"]["environment_state"]["unknown_topics"]:
        desc = topic["description"]
        assert desc.startswith("<untrusted_world_event ")
        assert desc.endswith("</untrusted_world_event>")

    # Verify generate_goal & plan also compact skills/playbooks cleanly
    ctx = CognitiveContext(
        observation=obs,
        drives=drives,
        decision=DriveDecision(selected_drive="curiosity", priority_score=0.6, reason="pgvector HNSW benchmark"),
        memories=verbose_mems,
        validated_skills=skills,
        external_playbooks=[
            {"domain": "curiosity", "version": 1, "samples": 2, "content": long_pb},
            {"domain": "coding", "version": 1, "samples": 1, "content": long_pb},
            {"domain": "general", "version": 1, "samples": 1, "content": long_pb},
        ],
    )
    await brain.generate_goal(ctx)
    gg = gw.captured["generate_goal"]
    assert len(gg["validated_skills"]) == 5
    assert gg["validated_skills"][0]["canonical_name"] == "data.databases.benchmark_pgvector_hnsw"
    assert len(gg["external_playbooks"]) == 2


@pytest.mark.asyncio
async def test_v4_m4_control_center_apis_expose_skill_reuse_trees_playbooks_and_exploration(tmp_path: Path):
    """Verify /overview, /self-model, and /world return accurate DB-backed V4.0 RSI telemetry."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'cc_v4.db'}"
    db = Database(db_url)
    await _init_db(db)

    admin_token = "v4-m4-test-token"
    settings = Settings(
        database=DatabaseSettings(url=db_url),
        api=ApiSettings(admin_key=admin_token),
        governance=GovernanceSettings(workspace_root=str(tmp_path / "ws")),
        perception=PerceptionSettings(llm_probe=False, host_metrics=False),
        memory=MemorySettings(provider="none"),
        embedding=EmbeddingSettings(provider="hash_fallback"),
        runner=RunnerSettings(enabled=False),
        kernel_v3=KernelV3Settings(
            playbooks_enabled=True,
            open_public_perception_enabled=True,
            skill_runtime_enabled=True,
        ),
    )

    rt = OrganismRuntime(settings=settings, db=db, auto_load_agents=False)
    try:
        await rt.start(start_scheduler=False)

        # Seed a real evolved skill (v1.0.0 -> v1.1.0) + cross-task reuse invocation
        origin_task = uuid.uuid4()
        reuse_task = uuid.uuid4()
        sk_v1 = await rt.skill_registry.register_skill(
            skill_key="count_words",
            canonical_name="data.coding.count_words",
            version="1.0.0",
            capability_key="coding",
            description="Count words v1",
            artifact_code="def execute(data):\n    return {'n': len(str(data.get('text', '')).split())}\n",
            test_spec={"kind": "python_assertions", "code": "from skill import execute\nassert execute({'text': 'a b'})['n'] == 2\n"},
            source_task_id=origin_task,
        )
        await rt.skill_registry.validate_skill(sk_v1["id"])
        assert rt.skill_evolution is not None
        evo = await rt.skill_evolution.evolve_skill(
            sk_v1["id"],
            new_artifact_code="def execute(data):\n    words = str(data.get('text', '')).split()\n    return {'n': len(words), 'unique': len(set(words))}\n",
            new_test_code="from skill import execute\nout = execute({'text': 'a a b'})\nassert out['n'] == 3 and out['unique'] == 2\n",
            change_summary="Add unique word count",
        )
        assert evo.evolved is True
        v2_id = evo.new_skill_id
        assert v2_id is not None

        # Record real cross-task reuse via SkillRuntime
        inv_res = await rt.skill_runtime.execute(
            v2_id,
            {"text": "hello v4 rsi hello"},
            task_id=reuse_task,
            invocation_source="skill_first",
        )
        assert inv_res.ok is True
        assert inv_res.is_cross_task_reuse is True

        # Seed an external playbook & an external exploration topic
        assert rt.playbook_manager is not None
        await rt.playbook_manager.save_revision(
            domain="curiosity",
            content="# Curiosity Playbook v1\n- Verify official technical docs before summarizing.",
            change_summary="Initial verified curiosity strategy",
        )
        assert rt.exploration_manager is not None
        await rt.exploration_manager.upsert_topic(
            topic_key="pgvector_hnsw_tuning",
            question="How does hnsw.ef_search affect recall?",
            domain="databases",
            status="verified",
            verified_fact="ef_search=100 improves recall@10 cleanly.",
            information_gain=0.61,
        )

        # Call real FastAPI endpoints via httpx ASGITransport in the same async event loop
        app = create_app(settings=settings, runtime=rt, start_scheduler=False)
        app.state.runtime = rt
        app.state.settings = rt.settings
        app.state.db = rt.db
        app.state.repo = rt.repo
        app.state.organism_id = rt.organism_id
        headers = {"Authorization": f"Bearer {admin_token}"}
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            ov = await client.get("/v2/control-center/overview?hours=24", headers=headers)
            assert ov.status_code == 200
            ov_evo = ov.json()["organs"]["evolution"]
            assert ov_evo["skill_cross_task_reuses_window"] >= 1
            assert ov_evo["estimated_saved_tokens_window"] > 0
            assert ov_evo["playbooks_active_count"] == 1
            assert ov_evo["exploration_topics_total"] == 1
            assert ov_evo["exploration_topics_verified"] == 1
            assert ov_evo["open_public_perception_enabled"] is True

            sm = await client.get("/v2/control-center/self-model", headers=headers)
            assert sm.status_code == 200
            sm_j = sm.json()
            assert sm_j["skill_reuse_summary"]["cross_task_reuses"] >= 1
            assert sm_j["skill_reuse_summary"]["estimated_saved_tokens"] > 0
            assert any(t["canonical_name"] == "data.coding.count_words" and t["version_count"] == 2 for t in sm_j["skill_version_trees"])
            assert any(pb["domain"] == "curiosity" and pb["active_version"] == 1 for pb in sm_j["external_playbooks"])

            wd = await client.get("/v2/control-center/world", headers=headers)
            assert wd.status_code == 200
            wd_j = wd.json()
            assert wd_j["open_public_perception_enabled"] is True
            assert wd_j["exploration"]["total"] == 1
            assert wd_j["exploration"]["verified_count"] == 1
            assert wd_j["exploration"]["domains_summary"]["databases"] == 1
            assert wd_j["exploration"]["topics"][0]["key"] == "pgvector_hnsw_tuning"
    finally:
        await rt.stop()
        await db.dispose()
