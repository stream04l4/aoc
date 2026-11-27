"""Blank-slate birth verification for newly spawned V5.0 offspring instances."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from app.evolution.interfaces import BlankSlateCheckItem, BlankSlateReport
from app.storage.database import Database, validate_db_schema_name

_NON_INHERITABLE_WORLD_KINDS = (
    "external_playbook",
    "hypothesis",
    "human_guidance",
    "knowledge_topic",
    "exploration_topic",
    "investigation_thread",
    "human_inbox",
    "dialogue_turn",
    "social_deferred_reply",
    "social_rhythm_state",
    "human_outreach_card",
    "telegram_pending_reply",
    "interest_portfolio",
    "meta_reflection_state",
    "skill_refactor_queue",
    "skill_lifecycle_audit",
    "l2_evolution_bridge_state",
    "self_edit_queue",
    "self_edit_state",
)
_REQUIRED_BLANK_TABLES = (
    "memory_index",
    "memory_embeddings",
    "skill_registry",
    "skill_invocations",
    "affect_history",
    "self_model_versions",
    "world_states",
    "organism_identity",
)


class BlankSlateVerifier:
    """Verifies an offspring instance has ZERO inherited memories, skills, playbooks, or affect state."""

    async def verify(
        self,
        target_db: Database,
        *,
        instance_key: str,
        parent_letta_agent_id: str | None = None,
        expected_schema: str | None = None,
    ) -> BlankSlateReport:
        schema = validate_db_schema_name(expected_schema or target_db.schema_name)
        checks: list[BlankSlateCheckItem] = []

        async with target_db.engine.connect() as conn:
            dialect = conn.dialect.name
            is_pg = dialect == "postgresql"

            if is_pg:
                present_rows = (
                    await conn.execute(
                        text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = :schema"
                        ),
                        {"schema": schema},
                    )
                ).scalars().all()
                present = set(present_rows)
                missing = sorted(set(_REQUIRED_BLANK_TABLES) - present)
                checks.append(
                    BlankSlateCheckItem(
                        name="schema_tables_present",
                        passed=len(missing) == 0,
                        observed=sorted(present & set(_REQUIRED_BLANK_TABLES)),
                        expected=list(_REQUIRED_BLANK_TABLES),
                        detail=f"missing tables in schema {schema}: {missing}" if missing else "all tables present in isolated schema",
                    )
                )
                if missing:
                    return BlankSlateReport(
                        instance_key=instance_key,
                        db_schema=schema,
                        passed=False,
                        checks=checks,
                    )

            def _tbl(name: str) -> str:
                return f'"{schema}"."{name}"' if is_pg else f'"{name}"'

            zero_tables = (
                ("memory_index_zero", "memory_index"),
                ("memory_embeddings_zero", "memory_embeddings"),
                ("skill_registry_zero", "skill_registry"),
                ("skill_invocations_zero", "skill_invocations"),
                ("affect_history_zero", "affect_history"),
            )
            for check_name, tbl in zero_tables:
                cnt = int((await conn.execute(text(f"SELECT count(*) FROM {_tbl(tbl)}"))).scalar_one())
                checks.append(
                    BlankSlateCheckItem(
                        name=check_name,
                        passed=cnt == 0,
                        observed=cnt,
                        expected=0,
                        detail="" if cnt == 0 else f"found {cnt} inherited rows in {tbl}",
                    )
                )

            sm_cnt = int(
                (await conn.execute(text(f"SELECT count(*) FROM {_tbl('self_model_versions')}"))).scalar_one()
            )
            checks.append(
                BlankSlateCheckItem(
                    name="self_model_versions_blank",
                    passed=sm_cnt <= 1,
                    observed=sm_cnt,
                    expected="<= 1",
                    detail="" if sm_cnt <= 1 else f"found {sm_cnt} self_model versions (>1 implies inheritance)",
                )
            )

            kinds_sql = ", ".join(f"'{k}'" for k in _NON_INHERITABLE_WORLD_KINDS)
            ws_cnt = int(
                (
                    await conn.execute(
                        text(f"SELECT count(*) FROM {_tbl('world_states')} WHERE kind IN ({kinds_sql})")
                    )
                ).scalar_one()
            )
            checks.append(
                BlankSlateCheckItem(
                    name="world_states_non_inheritable_zero",
                    passed=ws_cnt == 0,
                    observed=ws_cnt,
                    expected=0,
                    detail="" if ws_cnt == 0 else f"found {ws_cnt} inherited world_states artifacts",
                )
            )

            social_mem_cnt = int(
                (
                    await conn.execute(
                        text(
                            f"SELECT count(*) FROM {_tbl('memory_index')} "
                            "WHERE source = 'dialogue:telegram' OR source LIKE 'dialogue:%'"
                        )
                    )
                ).scalar_one()
            )
            checks.append(
                BlankSlateCheckItem(
                    name="social_dialogue_memories_zero",
                    passed=social_mem_cnt == 0,
                    observed=social_mem_cnt,
                    expected=0,
                    detail=""
                    if social_mem_cnt == 0
                    else f"found {social_mem_cnt} inherited personal dialogue memories",
                )
            )

            agent_ids = (
                await conn.execute(text(f"SELECT memory_agent_id FROM {_tbl('organism_identity')}"))
            ).scalars().all()
            parent_norm = (parent_letta_agent_id or "").strip()
            inherited_agent = bool(parent_norm and any((aid or "").strip() == parent_norm for aid in agent_ids))
            checks.append(
                BlankSlateCheckItem(
                    name="letta_agent_isolated",
                    passed=not inherited_agent,
                    observed=[aid for aid in agent_ids if aid],
                    expected=f"!= {parent_norm}" if parent_norm else "distinct",
                    detail="offspring reused parent memory_agent_id" if inherited_agent else "",
                )
            )

        passed = all(c.passed for c in checks)
        return BlankSlateReport(
            instance_key=instance_key,
            db_schema=schema,
            passed=passed,
            checks=checks,
        )
