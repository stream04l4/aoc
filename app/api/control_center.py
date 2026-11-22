"""AOC Control Center Read-Only Observability, Analytics & Configuration Introspection API (V2.1).

Strictly read-only endpoints (all authenticated via existing Bearer Token + SCOPE_READ):
  - Never returns raw secrets, API keys, bot tokens, or database passwords.
  - Never fabricates missing data: unrecorded DB fields are explicitly returned as `null`.
  - All long lists are paginated and support time-range / status / entity filters.
  - Reuses PostgreSQL authoritative tables directly via SQLAlchemy AsyncSession.
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, or_, select, text

from app.api.auth import SCOPE_READ, Principal, require_scope
from app.core.config import (
    PROJECT_ROOT,
    get_affect_config,
    get_agent_config,
    get_drive_config,
    get_embedding_config,
    get_lifecycle_config,
    get_memory_config,
    get_policy_config,
    get_runner_config,
    get_self_model_config,
    get_sensors_config,
    load_yaml,
)
from app.perception.interfaces import utcnow
from app.storage.models import (
    ActionPlanRow,
    ActiveGoal,
    AgentExecutionRow,
    AgentRegistryRow,
    AgentTaskRow,
    ApprovalRecord,
    CapabilityEvidenceRow as CapabilityEvidence,
    CapabilityProfileRow as CapabilityProfile,
    CycleRecord,
    DecisionLog,
    DriveHistory,
    EmbeddingModelRegistry,
    EventLog,
    ExecutionRecoveryRecord,
    GoalHistory,
    MemoryEmbedding,
    MemoryIndex,
    ObservationRecord,
    OrganismIdentity,
    OrganismState,
    OutboxEvent,
    PolicyEvaluation,
    ResourceUsage,
    RunnerRegistryRow as RunnerRegistry,
    SelfModelVersionRow as SelfModelVersion,
    SensorCheckpoint,
    SkillRegistryRow as SkillRegistry,
    SkillValidationResultRow as SkillValidationResult,
    TaskLifecycleEvent,
    WorldEventRow,
    WorldEventSource,
    WorldState,
)

REDACTED = "***REDACTED***"


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc).isoformat()
    return dt.isoformat()


def _redact_db_url(url: str) -> str:
    """Mask password inside database URL while keeping dialect/host/port/db readable."""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        if parts.password:
            netloc = f"{parts.username or ''}:{REDACTED}@{parts.hostname or ''}"
            if parts.port:
                netloc += f":{parts.port}"
            return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        pass
    return re.sub(r"://([^:/@]+):([^@]+)@", rf"://\1:{REDACTED}@", url)


def _read_dotenv_keys() -> set[str]:
    env_path = PROJECT_ROOT / ".env"
    keys: set[str] = set()
    if not env_path.exists():
        return keys
    try:
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k = s.split("=", 1)[0].strip()
            if k:
                keys.add(k)
    except OSError:
        pass
    return keys


def _read_process_rss_mb() -> float | None:
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return round(float(line.split()[1]) / 1024.0, 2)
    except OSError:
        return None
    return None


def _parse_since(hours: float | None) -> datetime | None:
    if hours is None or hours <= 0:
        return None
    return utcnow() - timedelta(hours=min(hours, 24.0 * 365.0))


def _cfg_item(
    *,
    key: str,
    label: str,
    description: str,
    effective_value: Any,
    yaml_value: Any = None,
    default_value: Any = None,
    env_var: str | None = None,
    dotenv_keys: set[str] | None = None,
    requires_restart: bool = True,
    sensitive: bool = False,
    risk_note: str | None = None,
) -> dict[str, Any]:
    dotenv_set = dotenv_keys or set()
    in_env = bool(env_var and env_var in os.environ)
    in_dotenv = bool(env_var and env_var in dotenv_set)
    has_yaml = yaml_value is not None

    if in_env:
        source = "env"
        overridden = has_yaml or in_dotenv or (default_value is not None and effective_value != default_value)
    elif in_dotenv:
        source = ".env"
        overridden = has_yaml or (default_value is not None and effective_value != default_value)
    elif has_yaml:
        source = "yaml"
        overridden = default_value is not None and effective_value != default_value
    else:
        source = "default"
        overridden = False

    if sensitive:
        if effective_value in (None, "", []):
            display_val: Any = None
        elif isinstance(effective_value, str) and "://" in effective_value and "@" in effective_value:
            display_val = _redact_db_url(effective_value)
        else:
            display_val = REDACTED
    else:
        display_val = effective_value

    return {
        "key": key,
        "label": label,
        "description": description,
        "effective_value": display_val,
        "source": source,
        "env_var": env_var,
        "overridden": overridden,
        "requires_restart": requires_restart,
        "sensitive": sensitive,
        "risk_note": risk_note,
    }


def _nested_get(d: dict[str, Any], *path: str) -> Any:
    cur: Any = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


def create_control_center_router(get_runtime) -> APIRouter:
    router = APIRouter(tags=["control-center"])

    @router.get("/v2/auth/me")
    async def auth_me(principal: Principal = Depends(require_scope(SCOPE_READ))) -> dict[str, Any]:
        return {
            "name": principal.name,
            "scopes": sorted(principal.scopes),
            "source": principal.source,
        }

    # ── A. Overview ──────────────────────────────────────────────────────────
    @router.get("/v2/control-center/overview")
    async def cc_overview(
        hours: float = Query(default=24.0, ge=0.0, le=8760.0),
        _: Principal = Depends(require_scope(SCOPE_READ)),
    ) -> dict[str, Any]:
        rt = get_runtime()
        since = _parse_since(hours)
        org_id = rt.organism_id

        db_ok = await rt.db.ping()
        drives = await rt.repo.load_drive_states(org_id) if org_id else []
        state = await rt.repo.get_organism_state(org_id) if org_id else None
        budget = await rt.governance.check_budget()

        async with rt.db.session() as s:
            identity = await s.get(OrganismIdentity, org_id) if org_id else None
            latest_cycle = (
                await s.execute(select(CycleRecord).order_by(desc(CycleRecord.started_at)).limit(1))
            ).scalar_one_or_none()
            latest_obs = (
                await s.execute(select(ObservationRecord).order_by(desc(ObservationRecord.created_at)).limit(1))
            ).scalar_one_or_none()

            # Drive history trend
            dh_q = select(DriveHistory).order_by(desc(DriveHistory.created_at)).limit(180)
            if since is not None:
                dh_q = dh_q.where(DriveHistory.created_at >= since)
            dh_rows = list(reversed((await s.execute(dh_q)).scalars().all()))

            # Active goals & active/recent tasks
            active_goals = (
                await s.execute(
                    select(ActiveGoal).where(ActiveGoal.status == "ACTIVE").order_by(desc(ActiveGoal.created_at)).limit(5)
                )
            ).scalars().all()
            recent_tasks = (
                await s.execute(select(AgentTaskRow).order_by(desc(AgentTaskRow.created_at)).limit(6))
            ).scalars().all()

            # Recent decisions & events
            recent_decisions = (
                await s.execute(select(DecisionLog).order_by(desc(DecisionLog.created_at)).limit(10))
            ).scalars().all()
            recent_events = (
                await s.execute(select(EventLog).order_by(desc(EventLog.created_at)).limit(10))
            ).scalars().all()

            # Runners & agents
            runners = (await s.execute(select(RunnerRegistry).order_by(RunnerRegistry.runner_id))).scalars().all()
            agents = (await s.execute(select(AgentRegistryRow).order_by(AgentRegistryRow.agent_id))).scalars().all()

            # Windowed task & cycle & LLM usage stats
            c_q = select(CycleRecord.outcome, func.count(CycleRecord.id)).group_by(CycleRecord.outcome)
            t_q = select(AgentTaskRow.status, func.count(AgentTaskRow.id)).group_by(AgentTaskRow.status)
            u_q = select(
                ResourceUsage.category,
                func.count(ResourceUsage.id),
                func.coalesce(func.sum(ResourceUsage.amount), 0.0),
                func.coalesce(func.avg(ResourceUsage.seconds), 0.0),
            ).group_by(ResourceUsage.category)
            if since is not None:
                c_q = c_q.where(CycleRecord.started_at >= since)
                t_q = t_q.where(AgentTaskRow.created_at >= since)
                u_q = u_q.where(ResourceUsage.created_at >= since)

            cycle_outcomes = {str(r[0] or "IN_PROGRESS"): int(r[1]) for r in (await s.execute(c_q)).all()}
            task_statuses = {str(r[0]): int(r[1]) for r in (await s.execute(t_q)).all()}
            usage_by_cat = {
                str(r[0]): {
                    "calls": int(r[1]),
                    "total_amount": round(float(r[2] or 0.0), 2),
                    "avg_seconds": round(float(r[3] or 0.0), 3),
                }
                for r in (await s.execute(u_q)).all()
            }

            # Memory & Outbox health
            outbox_counts = {
                str(r[0]): int(r[1])
                for r in (
                    await s.execute(select(OutboxEvent.status, func.count(OutboxEvent.id)).group_by(OutboxEvent.status))
                ).all()
            }
            mem_total = int((await s.execute(select(func.count(MemoryIndex.id)))).scalar_one() or 0)
            mem_synced = int(
                (
                    await s.execute(select(func.count(MemoryIndex.id)).where(MemoryIndex.sync_status == "synced"))
                ).scalar_one()
                or 0
            )
            emb_total = int((await s.execute(select(func.count(MemoryEmbedding.id)))).scalar_one() or 0)
            active_model = (
                await s.execute(select(EmbeddingModelRegistry).where(EmbeddingModelRegistry.is_active.is_(True)).limit(1))
            ).scalar_one_or_none()

            # Check PG vector extension status (safe on SQLite)
            pg_vector_enabled = False
            is_pg = s.bind is not None and s.bind.dialect.name == "postgresql"
            if is_pg:
                try:
                    ext = (
                        await s.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))
                    ).scalar_one_or_none()
                    pg_vector_enabled = ext is not None
                except Exception:
                    pg_vector_enabled = False

            # V3.2 Organism Organs & RSI Evolution real-time telemetry (0 fabricated metrics)
            world_sources = (await s.execute(select(WorldEventSource).order_by(WorldEventSource.source_id))).scalars().all()
            world_events_total = int((await s.execute(select(func.count(WorldEventRow.id)))).scalar_one() or 0)
            hyp_rows = (
                await s.execute(select(WorldState).where(WorldState.kind == "hypothesis"))
            ).scalars().all()
            cap_rows = (
                await s.execute(select(CapabilityProfile).where(CapabilityProfile.organism_id == org_id))
                if org_id
                else None
            )
            cap_list = cap_rows.scalars().all() if cap_rows is not None else []
            skill_rows = (await s.execute(select(SkillRegistry))).scalars().all()

            pf_q = select(func.count(ActionPlanRow.id)).where(ActionPlanRow.preflight_status == "REJECT")
            dedup_q = select(func.count(CycleRecord.id)).where(CycleRecord.outcome == "GOAL_DEDUP_SKIPPED")
            ig_q = select(func.coalesce(func.sum(MemoryIndex.information_gain), 0.0)).where(
                MemoryIndex.information_gain.is_not(None)
            )
            if since is not None:
                pf_q = pf_q.where(ActionPlanRow.created_at >= since)
                dedup_q = dedup_q.where(CycleRecord.started_at >= since)
                ig_q = ig_q.where(MemoryIndex.created_at >= since)
            preflight_rejected_win = int((await s.execute(pf_q)).scalar_one() or 0)
            dedup_skipped_win = int((await s.execute(dedup_q)).scalar_one() or 0)
            sum_ig_win = float((await s.execute(ig_q)).scalar_one() or 0.0)

        sys_obs = (latest_obs.system_state if latest_obs else {}) or {}
        llm_obs = sys_obs.get("llm") if isinstance(sys_obs, dict) else None
        resources_obs = (latest_obs.available_resources if latest_obs else {}) or (
            sys_obs.get("resources") if isinstance(sys_obs, dict) else {}
        ) or {}

        affect_enabled_now = bool(rt.affect and rt.affect.enabled)
        affect_state_now = rt.affect.state.model_dump(mode="json") if affect_enabled_now else None
        affect_mod_now = rt.affect.compute_modulation().model_dump(mode="json") if affect_enabled_now else {"enabled": False}
        mcp_registered_ov = rt.mcp_manager.summary_for_brain() if rt.mcp_manager is not None else []
        sensors_cfg_ov = get_sensors_config()
        eff_poll_ov = (
            bool(rt.settings.kernel_v3.world_poll_on_perceive)
            if rt.settings.kernel_v3.world_poll_on_perceive is not None
            else bool(sensors_cfg_ov.get("poll_on_perceive", False))
        )
        llm_tok_win = float((usage_by_cat.get("llm") or {}).get("total_amount") or 0.0)
        ig_per_1m_win = round((sum_ig_win / (llm_tok_win / 1_000_000.0)), 4) if llm_tok_win > 0 else 0.0
        agent_list_ov = [
            {
                "agent_id": a.agent_id,
                "name": a.name,
                "enabled": a.enabled,
                "healthy": bool((a.probe or {}).get("ok", False)),
                "version": (a.probe or {}).get("version"),
            }
            for a in agents
        ]

        return {
            "timestamp": _iso(utcnow()),
            "window_hours": hours,
            "organism": {
                "id": str(org_id) if org_id else None,
                "name": identity.name if identity else rt.settings.organism.name,
                "version": identity.version if identity else rt.settings.organism.version,
                "created_at": _iso(identity.created_at) if identity else None,
                "lifecycle_state": state.get("lifecycle_state", "UNKNOWN") if state else "UNKNOWN",
                "cycle_count": state.get("cycle_count", 0) if state else 0,
                "last_cycle_at": _iso(state.get("last_cycle_at") or state.get("updated_at")) if state else None,
                "next_wake_at": _iso(state.get("next_wake_at")) if state else None,
                "quota_reset_at": _iso(state.get("quota_reset_at")) if state else None,
                "throttled_reason": state.get("throttled_reason") if state else None,
                "scheduler_running": bool(rt.scheduler and rt.scheduler.running),
                "affect_enabled": bool(rt.affect and rt.affect.enabled),
            },
            "latest_cycle": (
                {
                    "cycle_id": str(latest_cycle.id),
                    "cycle_number": latest_cycle.cycle_number,
                    "phase": latest_cycle.phase,
                    "outcome": latest_cycle.outcome,
                    "started_at": _iso(latest_cycle.started_at),
                    "completed_at": _iso(latest_cycle.ended_at),
                    "selected_drive": (latest_cycle.decision or {}).get("selected_drive"),
                    "arbitration_reason": (latest_cycle.decision or {}).get("reason"),
                    "goal_title": ((latest_cycle.brain_output or {}).get("goal") or {}).get("title"),
                }
                if latest_cycle
                else None
            ),
            "drives": drives,
            "drive_trend": [
                {
                    "timestamp": _iso(d.created_at),
                    "drive_type": d.drive_type,
                    "phase": d.phase,
                    "satisfaction": round(float(d.satisfaction), 4),
                    "priority": round(float(d.deficit) * (0.5 + 0.5 * float(d.urgency)), 4),
                }
                for d in dh_rows
            ],
            "active_goals": [
                {
                    "goal_id": str(g.id),
                    "drive_type": g.drive_type,
                    "title": g.title,
                    "status": g.status,
                    "cycle_id": str(g.cycle_id) if g.cycle_id else None,
                    "created_at": _iso(g.created_at),
                }
                for g in active_goals
            ],
            "recent_tasks": [
                {
                    "task_id": str(t.id),
                    "cycle_id": str(t.cycle_id) if t.cycle_id else None,
                    "goal_id": str(t.goal_id) if t.goal_id else None,
                    "objective": t.objective,
                    "assigned_agent": t.assigned_agent,
                    "permission_level": t.permission_level,
                    "status": t.status,
                    "attempt": t.attempt,
                    "max_attempts": t.max_attempts,
                    "created_at": _iso(t.created_at),
                    "finalized_at": _iso(t.finalized_at),
                }
                for t in recent_tasks
            ],
            "recent_decisions": [
                {
                    "id": str(d.id),
                    "cycle_id": str(d.cycle_id) if d.cycle_id else None,
                    "decision_type": d.decision_type,
                    "selected": d.selected,
                    "reason": d.reason,
                    "created_at": _iso(d.created_at),
                }
                for d in recent_decisions
            ],
            "recent_events": [
                {
                    "id": str(e.id),
                    "cycle_id": str(e.cycle_id) if e.cycle_id else None,
                    "level": e.level,
                    "event_type": e.event_type,
                    "message": e.message,
                    "created_at": _iso(e.created_at),
                }
                for e in recent_events
            ],
            "components": {
                "core": {
                    "status": "ok",
                    "rss_mb": _read_process_rss_mb(),
                    "cpu_percent": resources_obs.get("cpu_percent") if isinstance(resources_obs, dict) else None,
                    "mem_available_pct": resources_obs.get("memory_available_pct") if isinstance(resources_obs, dict) else None,
                    "disk_free_pct": resources_obs.get("disk_free_pct") if isinstance(resources_obs, dict) else None,
                },
                "postgres": {
                    "status": "ok" if db_ok else "down",
                    "pgvector_enabled": pg_vector_enabled,
                },
                "runner": {
                    "enabled": rt.settings.runner.enabled,
                    "runners": [
                        {
                            "runner_id": r.runner_id,
                            "status": r.status,
                            "systemd_unit": r.systemd_unit,
                            "last_heartbeat_at": _iso(r.last_heartbeat_at),
                            "capabilities": r.capabilities,
                        }
                        for r in runners
                    ],
                },
                "llm": {
                    "model": rt.settings.llm.model,
                    "available": llm_obs.get("available") if isinstance(llm_obs, dict) else None,
                    "latency_ms": llm_obs.get("latency_ms") if isinstance(llm_obs, dict) else None,
                    "circuit_open": bool(getattr(rt.llm_gateway, "circuit_open", False)),
                },
                "letta": {
                    "provider": rt.settings.memory.provider,
                    "available": bool(rt.memory.backend_available),
                    "memories_synced": mem_synced,
                    "memories_total": mem_total,
                    "outbox": outbox_counts,
                },
                "embedding": {
                    "provider": rt.settings.embedding.provider,
                    "model_key": active_model.model_key if active_model else rt.settings.embedding.model_key,
                    "dimensions": active_model.dimensions if active_model else rt.settings.embedding.dimensions,
                    "embedded_count": emb_total,
                },
                "agents": agent_list_ov,
            },
            "affect": {
                "enabled": affect_enabled_now,
                "current_state": affect_state_now,
                "current_modulation": affect_mod_now,
            },
            "organs": {
                "brain": {
                    "model": rt.settings.llm.model,
                    "available": llm_obs.get("available") if isinstance(llm_obs, dict) else None,
                    "latency_ms": llm_obs.get("latency_ms") if isinstance(llm_obs, dict) else None,
                    "circuit_open": bool(getattr(rt.llm_gateway, "circuit_open", False)),
                    "calls_window": int((usage_by_cat.get("llm") or {}).get("calls") or 0),
                    "tokens_window": int(round(llm_tok_win)),
                    "avg_seconds": float((usage_by_cat.get("llm") or {}).get("avg_seconds") or 0.0),
                },
                "perception": {
                    "poll_on_perceive": eff_poll_ov,
                    "sources_total": len(world_sources),
                    "sources_enabled": sum(1 for ws in world_sources if ws.enabled),
                    "active_source_names": [ws.source_id for ws in world_sources if ws.enabled][:6],
                    "events_total": world_events_total,
                    "hypotheses_total": len(hyp_rows),
                    "hypotheses_confirmed": sum(1 for h in hyp_rows if h.status == "confirmed"),
                    "mcp_enabled": bool(rt.settings.kernel_v3.mcp_in_loop_enabled),
                    "mcp_tools_count": len(mcp_registered_ov),
                    "mcp_tools": [t.get("tool_name") for t in mcp_registered_ov][:6],
                },
                "memory": {
                    "provider": rt.settings.memory.provider,
                    "letta_available": bool(rt.memory.backend_available),
                    "memories_synced": mem_synced,
                    "memories_total": mem_total,
                    "pgvector_enabled": pg_vector_enabled,
                    "embedding_provider": rt.settings.embedding.provider,
                    "embedding_dim": active_model.dimensions if active_model else rt.settings.embedding.dimensions,
                    "embedded_count": emb_total,
                    "outbox_pending": int(outbox_counts.get("pending", 0)) + int(outbox_counts.get("failed", 0)),
                },
                "action": {
                    "runner_enabled": rt.settings.runner.enabled,
                    "runner_online": bool(runners and runners[0].status == "online"),
                    "runner_id": runners[0].runner_id if runners else "local-fallback",
                    "agents": agent_list_ov,
                    "agents_total": len(agent_list_ov),
                    "agents_healthy": sum(1 for a in agent_list_ov if a["healthy"]),
                    "tasks_window": sum(task_statuses.values()),
                    "tasks_succeeded": int(task_statuses.get("SUCCEEDED", 0)),
                    "tasks_failed": int(task_statuses.get("FAILED", 0)),
                },
                "evolution": {
                    "capabilities_total": len(cap_list),
                    "capabilities_validated": sum(1 for c in cap_list if c.maturity_status == "VALIDATED"),
                    "capabilities_acquired": sum(1 for c in cap_list if c.maturity_status == "ACQUIRED"),
                    "skills_total": len(skill_rows),
                    "skills_validated": sum(1 for sk in skill_rows if sk.status == "VALIDATED"),
                    "verified_ig_per_1m_tokens": ig_per_1m_win,
                    "preflight_rejected_window": preflight_rejected_win,
                    "goal_dedup_skipped_window": dedup_skipped_win,
                },
            },
            "stats": {
                "cycles_by_outcome": cycle_outcomes,
                "cycles_total_window": sum(cycle_outcomes.values()),
                "tasks_by_status": task_statuses,
                "tasks_total_window": sum(task_statuses.values()),
                "usage_by_category": usage_by_cat,
                "budget": budget.model_dump(mode="json"),
            },
        }

    # ── B. Mind (Cycles & Full 11-Step Trace) ────────────────────────────────
    @router.get("/v2/control-center/cycles")
    async def cc_cycles(
        limit: int = Query(default=25, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        outcome: str | None = None,
        drive: str | None = None,
        hours: float | None = Query(default=None, ge=0.0, le=8760.0),
        _: Principal = Depends(require_scope(SCOPE_READ)),
    ) -> dict[str, Any]:
        rt = get_runtime()
        since = _parse_since(hours)
        async with rt.db.session() as s:
            q = select(CycleRecord)
            if outcome:
                q = q.where(CycleRecord.outcome == outcome)
            if since is not None:
                q = q.where(CycleRecord.started_at >= since)
            rows = (await s.execute(q.order_by(desc(CycleRecord.started_at)))).scalars().all()
            if drive:
                rows = [r for r in rows if (r.decision or {}).get("selected_drive") == drive]
            total = len(rows)
            page = rows[offset : offset + limit]
            cycle_ids = [r.id for r in page]

            # Fetch per-cycle LLM token totals and primary task status
            tok_map: dict[uuid.UUID, float] = {}
            task_map: dict[uuid.UUID, dict[str, Any]] = {}
            if cycle_ids:
                tok_rows = (
                    await s.execute(
                        select(ResourceUsage.cycle_id, func.coalesce(func.sum(ResourceUsage.amount), 0.0))
                        .where(ResourceUsage.category == "llm", ResourceUsage.cycle_id.in_(cycle_ids))
                        .group_by(ResourceUsage.cycle_id)
                    )
                ).all()
                tok_map = {r[0]: float(r[1] or 0.0) for r in tok_rows if r[0] is not None}

                t_rows = (
                    await s.execute(
                        select(AgentTaskRow)
                        .where(AgentTaskRow.cycle_id.in_(cycle_ids))
                        .order_by(desc(AgentTaskRow.created_at))
                    )
                ).scalars().all()
                for t in t_rows:
                    if t.cycle_id and t.cycle_id not in task_map:
                        task_map[t.cycle_id] = {
                            "task_id": str(t.id),
                            "status": t.status,
                            "assigned_agent": t.assigned_agent,
                            "permission_level": t.permission_level,
                        }

        items = []
        for r in page:
            dur = None
            if r.started_at and r.ended_at:
                dur = round((r.ended_at - r.started_at).total_seconds(), 2)
            dec = r.decision or {}
            bo = r.brain_output or {}
            items.append(
                {
                    "cycle_id": str(r.id),
                    "cycle_number": r.cycle_number,
                    "lifecycle_state": r.lifecycle_state,
                    "phase": r.phase,
                    "outcome": r.outcome,
                    "started_at": _iso(r.started_at),
                    "completed_at": _iso(r.ended_at),
                    "duration_seconds": dur,
                    "selected_drive": dec.get("selected_drive"),
                    "priority_score": dec.get("priority_score"),
                    "goal_title": (bo.get("goal") or {}).get("title"),
                    "task": task_map.get(r.id),
                    "llm_tokens": int(tok_map.get(r.id, 0.0)),
                    "error": r.error,
                }
            )
        return {"total": total, "limit": limit, "offset": offset, "items": items}

    @router.get("/v2/control-center/cycles/{cycle_id}/trace")
    async def cc_cycle_trace(
        cycle_id: str,
        _: Principal = Depends(require_scope(SCOPE_READ)),
    ) -> dict[str, Any]:
        rt = get_runtime()
        try:
            cid = uuid.UUID(cycle_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid cycle_id UUID") from exc

        async with rt.db.session() as s:
            cycle = await s.get(CycleRecord, cid)
            if cycle is None:
                raise HTTPException(status_code=404, detail="Cycle not found")

            obs_rows = (
                await s.execute(
                    select(ObservationRecord)
                    .where(ObservationRecord.cycle_id == cid)
                    .order_by(ObservationRecord.created_at.asc())
                )
            ).scalars().all()
            pre_obs = next((o for o in obs_rows if o.phase == "pre"), None)
            post_obs = next((o for o in obs_rows if o.phase == "post"), None)

            goals = (
                await s.execute(select(ActiveGoal).where(ActiveGoal.cycle_id == cid).order_by(ActiveGoal.created_at.asc()))
            ).scalars().all()
            plans = (
                await s.execute(
                    select(ActionPlanRow).where(ActionPlanRow.cycle_id == cid).order_by(ActionPlanRow.created_at.asc())
                )
            ).scalars().all()
            tasks = (
                await s.execute(
                    select(AgentTaskRow).where(AgentTaskRow.cycle_id == cid).order_by(AgentTaskRow.created_at.asc())
                )
            ).scalars().all()
            task_ids = [t.id for t in tasks]
            execs = (
                (
                    await s.execute(
                        select(AgentExecutionRow)
                        .where(AgentExecutionRow.task_id.in_(task_ids))
                        .order_by(AgentExecutionRow.attempt_number.asc(), AgentExecutionRow.started_at.asc())
                    )
                )
                .scalars()
                .all()
                if task_ids
                else []
            )
            policy_evals = (
                await s.execute(
                    select(PolicyEvaluation)
                    .where(PolicyEvaluation.cycle_id == cid)
                    .order_by(PolicyEvaluation.created_at.asc())
                )
            ).scalars().all()
            approvals = (
                (
                    await s.execute(
                        select(ApprovalRecord)
                        .where(ApprovalRecord.task_id.in_(task_ids))
                        .order_by(ApprovalRecord.created_at.asc())
                    )
                )
                .scalars()
                .all()
                if task_ids
                else []
            )
            decisions = (
                await s.execute(
                    select(DecisionLog).where(DecisionLog.cycle_id == cid).order_by(DecisionLog.created_at.asc())
                )
            ).scalars().all()
            memories = (
                await s.execute(
                    select(MemoryIndex).where(MemoryIndex.cycle_id == cid).order_by(MemoryIndex.created_at.asc())
                )
            ).scalars().all()
            usage_rows = (
                await s.execute(
                    select(ResourceUsage).where(ResourceUsage.cycle_id == cid).order_by(ResourceUsage.created_at.asc())
                )
            ).scalars().all()

        dur = None
        if cycle.started_at and cycle.ended_at:
            dur = round((cycle.ended_at - cycle.started_at).total_seconds(), 2)
        bo = cycle.brain_output or {}
        eval_data = cycle.evaluation or None

        return {
            "cycle": {
                "cycle_id": str(cycle.id),
                "cycle_number": cycle.cycle_number,
                "lifecycle_state": cycle.lifecycle_state,
                "phase": cycle.phase,
                "outcome": cycle.outcome,
                "started_at": _iso(cycle.started_at),
                "completed_at": _iso(cycle.ended_at),
                "duration_seconds": dur,
                "error": cycle.error,
            },
            # Strict 11-step structured trace (missing data is explicitly None, NEVER fabricated)
            "steps": {
                "1_perception": (
                    {
                        "observation_id": str(pre_obs.id),
                        "created_at": _iso(pre_obs.created_at),
                        "system_state": pre_obs.system_state,
                        "environment_state": pre_obs.environment_state,
                        "available_resources": pre_obs.available_resources,
                        "active_tasks": pre_obs.active_tasks,
                        "external_events": pre_obs.external_events,
                        "sensor_status": pre_obs.sensor_status,
                        "confidence": pre_obs.confidence,
                    }
                    if pre_obs
                    else None
                ),
                "2_drive_assessment": cycle.drives_pre if cycle.drives_pre else None,
                "3_arbitration": (
                    {
                        "decision": cycle.decision,
                        "affect_snapshot": bo.get("affect_snapshot"),
                        "audit_logs": [
                            {
                                "id": str(d.id),
                                "selected": d.selected,
                                "alternatives": d.alternatives,
                                "reason": d.reason,
                                "payload": d.payload,
                                "created_at": _iso(d.created_at),
                            }
                            for d in decisions
                            if d.decision_type == "drive_arbitration"
                        ],
                    }
                    if cycle.decision
                    else None
                ),
                "4_brain_assessment": bo.get("assessment") if bo.get("assessment") else None,
                "5_goal_generation": (
                    {
                        "proposal": bo.get("goal"),
                        "goal_candidates": bo.get("goal_candidates") or [],
                        "goal_dedup": bo.get("goal_dedup"),
                        "ranking_logs": [
                            {
                                "id": str(d.id),
                                "selected": d.selected,
                                "reason": d.reason,
                                "payload": d.payload,
                                "created_at": _iso(d.created_at),
                            }
                            for d in decisions
                            if d.decision_type in {"goal_ranking", "goal_dedup", "active_perception_source"}
                        ],
                        "persisted_goals": [
                            {
                                "goal_id": str(g.id),
                                "drive_type": g.drive_type,
                                "title": g.title,
                                "description": g.description,
                                "status": g.status,
                                "success_criteria": g.success_criteria,
                                "expected_outcome": g.expected_outcome,
                                "related_key": g.related_key,
                                "predicted_ig": g.predicted_ig,
                                "verified_ig": g.verified_ig,
                                "cooldown_until": _iso(g.cooldown_until),
                                "created_at": _iso(g.created_at),
                            }
                            for g in goals
                        ],
                    }
                    if (bo.get("goal") or goals)
                    else None
                ),
                "6_planning": (
                    {
                        "plan": cycle.plan,
                        "persisted_plans": [
                            {
                                "plan_id": str(p.id),
                                "permission_level": p.permission_level,
                                "status": p.status,
                                "plan_hash": p.plan_hash,
                                "policy_verdict": p.policy_verdict,
                                "created_at": _iso(p.created_at),
                            }
                            for p in plans
                        ],
                    }
                    if (cycle.plan or plans)
                    else None
                ),
                "7_governance": (
                    {
                        "policy_evaluations": [
                            {
                                "id": str(pe.id),
                                "subject_type": pe.subject_type,
                                "requested_level": pe.requested_level,
                                "deterministic_level": pe.deterministic_level,
                                "semantic_level": pe.semantic_level,
                                "effective_level": pe.effective_level,
                                "verdict": pe.verdict,
                                "violations": pe.violations,
                                "reason": pe.reason,
                                "created_at": _iso(pe.created_at),
                            }
                            for pe in policy_evals
                        ],
                        "approvals": [
                            {
                                "approval_id": str(a.id),
                                "task_id": str(a.task_id),
                                "status": a.status,
                                "permission_level": a.permission_level,
                                "approver": a.approver,
                                "channel": a.channel,
                                "decision_note": a.decision_note,
                                "created_at": _iso(a.created_at),
                                "decided_at": _iso(a.decided_at),
                            }
                            for a in approvals
                        ],
                    }
                    if (policy_evals or approvals)
                    else None
                ),
                "8_agent_dispatch": (
                    {
                        "routing_decisions": [
                            {
                                "id": str(d.id),
                                "decision_type": d.decision_type,
                                "selected": d.selected,
                                "alternatives": d.alternatives,
                                "reason": d.reason,
                                "payload": d.payload,
                                "created_at": _iso(d.created_at),
                            }
                            for d in decisions
                            if d.decision_type in {"execution_routing", "agent_fallback_retry"}
                        ],
                        "tasks": [
                            {
                                "task_id": str(t.id),
                                "objective": t.objective,
                                "assigned_agent": t.assigned_agent,
                                "required_capability": t.required_capability,
                                "permission_level": t.permission_level,
                                "status": t.status,
                                "attempt": t.attempt,
                                "max_attempts": t.max_attempts,
                                "workspace": t.workspace,
                                "created_at": _iso(t.created_at),
                                "finalized_at": _iso(t.finalized_at),
                            }
                            for t in tasks
                        ],
                        "executions": [
                            {
                                "execution_id": str(e.id),
                                "task_id": str(e.task_id),
                                "agent_id": e.agent_id,
                                "attempt_number": e.attempt_number,
                                "status": e.status,
                                "pid": e.pid,
                                "runner_id": e.runner_id,
                                "thread_id": e.thread_id,
                                "started_at": _iso(e.started_at),
                                "completed_at": _iso(e.completed_at),
                                "finalized_at": _iso(e.finalized_at),
                                "error": e.error,
                                "resource_usage": e.resource_usage,
                            }
                            for e in execs
                        ],
                    }
                    if (tasks or execs)
                    else None
                ),
                "9_result_verification": (
                    [
                        {
                            "execution_id": str(e.id),
                            "attempt_number": e.attempt_number,
                            "status": e.status,
                            "verification": e.verification,
                            "stdout_excerpt": (e.stdout or "")[:1200] if e.stdout else None,
                            "stderr_excerpt": (e.stderr or "")[:600] if e.stderr else None,
                            "result": e.result,
                        }
                        for e in execs
                    ]
                    if execs
                    else None
                ),
                "10_outcome_evaluation": (
                    {
                        "evaluation": eval_data,
                        "memories_created": [
                            {
                                "memory_id": str(m.id),
                                "memory_type": m.memory_type,
                                "title": m.title,
                                "summary": m.summary,
                                "importance": m.importance,
                                "information_gain": m.information_gain,
                                "sync_status": m.sync_status,
                            }
                            for m in memories
                        ],
                    }
                    if (eval_data or memories)
                    else None
                ),
                "11_post_perception": (
                    {
                        "observation": (
                            {
                                "observation_id": str(post_obs.id),
                                "created_at": _iso(post_obs.created_at),
                                "system_state": post_obs.system_state,
                                "environment_state": post_obs.environment_state,
                                "external_events": post_obs.external_events,
                                "confidence": post_obs.confidence,
                            }
                            if post_obs
                            else None
                        ),
                        "drives_post": cycle.drives_post if cycle.drives_post else None,
                        "state_changes": cycle.state_changes if cycle.state_changes else None,
                    }
                    if (post_obs or cycle.drives_post or cycle.state_changes)
                    else None
                ),
            },
            "resource_usage": [
                {
                    "category": u.category,
                    "amount": u.amount,
                    "seconds": u.seconds,
                    "details": u.details,
                    "created_at": _iso(u.created_at),
                }
                for u in usage_rows
            ],
        }

    # ── C. Drives ────────────────────────────────────────────────────────────
    @router.get("/v2/control-center/drives")
    async def cc_drives(
        hours: float = Query(default=24.0, ge=0.0, le=8760.0),
        limit: int = Query(default=300, ge=10, le=1000),
        _: Principal = Depends(require_scope(SCOPE_READ)),
    ) -> dict[str, Any]:
        rt = get_runtime()
        since = _parse_since(hours)
        org_id = rt.organism_id
        current = await rt.repo.load_drive_states(org_id) if org_id else []

        async with rt.db.session() as s:
            dh_q = select(DriveHistory).order_by(desc(DriveHistory.created_at)).limit(limit)
            arb_q = (
                select(DecisionLog)
                .where(DecisionLog.decision_type == "drive_arbitration")
                .order_by(desc(DecisionLog.created_at))
                .limit(50)
            )
            gh_q = select(GoalHistory).order_by(desc(GoalHistory.created_at)).limit(40)
            if since is not None:
                dh_q = dh_q.where(DriveHistory.created_at >= since)
                arb_q = arb_q.where(DecisionLog.created_at >= since)
                gh_q = gh_q.where(GoalHistory.created_at >= since)

            history_rows = list(reversed((await s.execute(dh_q)).scalars().all()))
            arb_rows = (await s.execute(arb_q)).scalars().all()
            gh_rows = (await s.execute(gh_q)).scalars().all()

            # Compare predicted vs measured satisfaction changes from recent cycles
            c_q = select(CycleRecord).order_by(desc(CycleRecord.started_at)).limit(40)
            ig_g_q = (
                select(ActiveGoal)
                .where(or_(ActiveGoal.predicted_ig.is_not(None), ActiveGoal.verified_ig.is_not(None)))
                .order_by(desc(ActiveGoal.created_at))
                .limit(30)
            )
            if since is not None:
                c_q = c_q.where(CycleRecord.started_at >= since)
                ig_g_q = ig_g_q.where(ActiveGoal.created_at >= since)
            c_rows = (await s.execute(c_q)).scalars().all()
            ig_goal_rows = (await s.execute(ig_g_q)).scalars().all()

        affect_hist = (await rt.repo.list_affect_history(org_id, limit=80)) if org_id else []
        affect_hist_asc = list(reversed(affect_hist))
        if since is not None:
            affect_hist_asc = [
                h
                for h in affect_hist_asc
                if h.get("created_at") is None
                or (
                    h["created_at"].replace(tzinfo=since.tzinfo)
                    if h["created_at"].tzinfo is None
                    else h["created_at"]
                )
                >= since
            ]
        affect_mod = rt.affect.compute_modulation().model_dump(mode="json") if rt.affect else {"enabled": False}
        affect_state = rt.affect.state.model_dump(mode="json") if (rt.affect and rt.affect.enabled) else None

        predicted_vs_measured = []
        for c in c_rows:
            sc = c.state_changes or {}
            ev = c.evaluation or {}
            measured = sc.get("measured_satisfaction_change") or ev.get("measured_satisfaction_change")
            expected = (c.plan or {}).get("expected_improvement")
            if measured is not None or expected is not None:
                sel_drive = (c.decision or {}).get("selected_drive")
                predicted_vs_measured.append(
                    {
                        "cycle_id": str(c.id),
                        "cycle_number": c.cycle_number,
                        "selected_drive": sel_drive,
                        "outcome": c.outcome,
                        "expected_improvement": expected,
                        "measured_change": measured.get(sel_drive) if isinstance(measured, dict) and sel_drive else None,
                        "measured_all": measured,
                        "started_at": _iso(c.started_at),
                    }
                )

        return {
            "window_hours": hours,
            "current": current,
            "history": [
                {
                    "timestamp": _iso(d.created_at),
                    "cycle_id": str(d.cycle_id) if d.cycle_id else None,
                    "drive_type": d.drive_type,
                    "phase": d.phase,
                    "satisfaction": round(float(d.satisfaction), 4),
                    "priority": round(float(d.deficit) * (0.5 + 0.5 * float(d.urgency)), 4),
                    "dimensions": d.dimensions,
                }
                for d in history_rows
            ],
            "arbitration_history": [
                {
                    "id": str(a.id),
                    "cycle_id": str(a.cycle_id) if a.cycle_id else None,
                    "selected": a.selected,
                    "alternatives": a.alternatives,
                    "reason": a.reason,
                    "payload": a.payload,
                    "created_at": _iso(a.created_at),
                }
                for a in arb_rows
            ],
            "predicted_vs_measured": predicted_vs_measured,
            "ig_calibration": [
                {
                    "goal_id": str(g.id),
                    "cycle_id": str(g.cycle_id) if g.cycle_id else None,
                    "drive_type": g.drive_type,
                    "title": g.title,
                    "status": g.status,
                    "predicted_ig": round(float(g.predicted_ig), 4) if g.predicted_ig is not None else None,
                    "verified_ig": round(float(g.verified_ig), 4) if g.verified_ig is not None else None,
                    "prediction_error": (
                        round(float(g.verified_ig) - float(g.predicted_ig), 4)
                        if (g.predicted_ig is not None and g.verified_ig is not None)
                        else None
                    ),
                    "created_at": _iso(g.created_at),
                }
                for g in ig_goal_rows
            ],
            "affect": {
                "enabled": bool(rt.affect and rt.affect.enabled),
                "maximum_influence": float(rt.affect.cfg.maximum_influence) if rt.affect else 0.15,
                "brain_read_only_injected": True,
                "confidence_evidence_anchored": True,
                "runtime_hooks": {
                    "retry_backoff_active": True,
                    "scheduler_interval_active": True,
                },
                "current_state": affect_state,
                "current_modulation": affect_mod,
                "history": [
                    {
                        "id": h["id"],
                        "cycle_id": h["cycle_id"],
                        "timestamp": _iso(h["created_at"]),
                        "valence": round(float(h["valence"]), 4),
                        "arousal": round(float(h["arousal"]), 4),
                        "confidence": round(float(h["confidence"]), 4),
                        "frustration": round(float(h["frustration"]), 4),
                        "boredom": round(float(h["boredom"]), 4),
                        "decay_applied": h.get("decay_applied"),
                        "trigger_event_id": h.get("trigger_event_id"),
                        "trigger_reason": h.get("trigger_reason", ""),
                        "modulation_applied": h.get("modulation_applied", {}),
                    }
                    for h in affect_hist_asc
                ],
            },
            "goal_transitions": [
                {
                    "id": str(g.id),
                    "goal_id": str(g.goal_id),
                    "from_status": g.from_status,
                    "to_status": g.to_status,
                    "note": g.note,
                    "measured_change": g.measured_change,
                    "created_at": _iso(g.created_at),
                }
                for g in gh_rows
            ],
        }

    # ── D. Tasks & Agents ────────────────────────────────────────────────────
    @router.get("/v2/control-center/tasks")
    async def cc_tasks(
        limit: int = Query(default=25, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        status: str | None = None,
        agent: str | None = None,
        search: str | None = None,
        _: Principal = Depends(require_scope(SCOPE_READ)),
    ) -> dict[str, Any]:
        rt = get_runtime()
        async with rt.db.session() as s:
            q = select(AgentTaskRow)
            cnt_q = select(func.count(AgentTaskRow.id))
            if status:
                q = q.where(AgentTaskRow.status == status)
                cnt_q = cnt_q.where(AgentTaskRow.status == status)
            if agent:
                q = q.where(AgentTaskRow.assigned_agent == agent)
                cnt_q = cnt_q.where(AgentTaskRow.assigned_agent == agent)
            if search:
                like = f"%{search.strip()}%"
                cond = or_(AgentTaskRow.objective.ilike(like), AgentTaskRow.description.ilike(like))
                q = q.where(cond)
                cnt_q = cnt_q.where(cond)

            total = int((await s.execute(cnt_q)).scalar_one() or 0)
            tasks = (await s.execute(q.order_by(desc(AgentTaskRow.created_at)).offset(offset).limit(limit))).scalars().all()
            t_ids = [t.id for t in tasks]

            exec_by_task: dict[uuid.UUID, list[AgentExecutionRow]] = {}
            if t_ids:
                e_rows = (
                    await s.execute(
                        select(AgentExecutionRow)
                        .where(AgentExecutionRow.task_id.in_(t_ids))
                        .order_by(AgentExecutionRow.attempt_number.asc())
                    )
                ).scalars().all()
                for e in e_rows:
                    exec_by_task.setdefault(e.task_id, []).append(e)

            agents = (await s.execute(select(AgentRegistryRow).order_by(AgentRegistryRow.agent_id))).scalars().all()
            runners = (await s.execute(select(RunnerRegistry).order_by(RunnerRegistry.runner_id))).scalars().all()

        items = []
        for t in tasks:
            exs = exec_by_task.get(t.id, [])
            last_ex = exs[-1] if exs else None
            dur_s = None
            if t.created_at and t.finalized_at:
                dur_s = round((t.finalized_at - t.created_at).total_seconds(), 2)
            items.append(
                {
                    "task_id": str(t.id),
                    "cycle_id": str(t.cycle_id) if t.cycle_id else None,
                    "goal_id": str(t.goal_id) if t.goal_id else None,
                    "plan_id": str(t.plan_id) if t.plan_id else None,
                    "objective": t.objective,
                    "assigned_agent": t.assigned_agent,
                    "required_capability": t.required_capability,
                    "permission_level": t.permission_level,
                    "status": t.status,
                    "state_version": t.state_version,
                    "attempt": t.attempt,
                    "max_attempts": t.max_attempts,
                    "executions_count": len(exs),
                    "duration_seconds": dur_s,
                    "created_at": _iso(t.created_at),
                    "finalized_at": _iso(t.finalized_at),
                    "latest_execution": (
                        {
                            "execution_id": str(last_ex.id),
                            "status": last_ex.status,
                            "pid": last_ex.pid,
                            "runner_id": last_ex.runner_id,
                            "thread_id": last_ex.thread_id,
                            "verification_passed": (last_ex.verification or {}).get("passed"),
                            "error": last_ex.error,
                        }
                        if last_ex
                        else None
                    ),
                }
            )

        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": items,
            "agents": [
                {
                    "agent_id": a.agent_id,
                    "name": a.name,
                    "agent_type": a.agent_type,
                    "enabled": a.enabled,
                    "healthy": bool((a.probe or {}).get("ok", False)),
                    "experimental": a.experimental,
                    "version": (a.probe or {}).get("version"),
                    "capabilities": a.capabilities,
                    "supported_modes": a.supported_modes,
                    "stats": a.stats,
                    "last_probe_at": _iso(a.updated_at),
                }
                for a in agents
            ],
            "runners": [
                {
                    "runner_id": r.runner_id,
                    "socket_path": r.socket_path,
                    "host_boot_id": r.host_boot_id,
                    "systemd_unit": r.systemd_unit,
                    "status": r.status,
                    "capabilities": r.capabilities,
                    "last_heartbeat_at": _iso(r.last_heartbeat_at),
                }
                for r in runners
            ],
        }

    @router.get("/v2/control-center/tasks/{task_id}/detail")
    async def cc_task_detail(
        task_id: str,
        _: Principal = Depends(require_scope(SCOPE_READ)),
    ) -> dict[str, Any]:
        rt = get_runtime()
        try:
            tid = uuid.UUID(task_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid task_id UUID") from exc

        async with rt.db.session() as s:
            task = await s.get(AgentTaskRow, tid)
            if task is None:
                raise HTTPException(status_code=404, detail="Task not found")
            goal = await s.get(ActiveGoal, task.goal_id) if task.goal_id else None
            plan = await s.get(ActionPlanRow, task.plan_id) if task.plan_id else None
            lifecycle = (
                await s.execute(
                    select(TaskLifecycleEvent)
                    .where(TaskLifecycleEvent.task_id == tid)
                    .order_by(TaskLifecycleEvent.state_version.asc(), TaskLifecycleEvent.created_at.asc())
                )
            ).scalars().all()
            execs = (
                await s.execute(
                    select(AgentExecutionRow)
                    .where(AgentExecutionRow.task_id == tid)
                    .order_by(AgentExecutionRow.attempt_number.asc(), AgentExecutionRow.started_at.asc())
                )
            ).scalars().all()
            recoveries = (
                await s.execute(
                    select(ExecutionRecoveryRecord)
                    .where(ExecutionRecoveryRecord.task_id == tid)
                    .order_by(ExecutionRecoveryRecord.created_at.asc())
                )
            ).scalars().all()
            approvals = (
                await s.execute(
                    select(ApprovalRecord).where(ApprovalRecord.task_id == tid).order_by(ApprovalRecord.created_at.asc())
                )
            ).scalars().all()
            routing = (
                (
                    await s.execute(
                        select(DecisionLog)
                        .where(
                            DecisionLog.cycle_id == task.cycle_id,
                            DecisionLog.decision_type.in_(["execution_routing", "agent_fallback_retry"]),
                        )
                        .order_by(DecisionLog.created_at.asc())
                    )
                )
                .scalars()
                .all()
                if task.cycle_id
                else []
            )

        return {
            "task": {
                "task_id": str(task.id),
                "cycle_id": str(task.cycle_id) if task.cycle_id else None,
                "goal_id": str(task.goal_id) if task.goal_id else None,
                "plan_id": str(task.plan_id) if task.plan_id else None,
                "objective": task.objective,
                "description": task.description,
                "assigned_agent": task.assigned_agent,
                "required_capability": task.required_capability,
                "expected_output": task.expected_output,
                "workspace": task.workspace,
                "timeout_seconds": task.timeout_seconds,
                "permission_level": task.permission_level,
                "resource_budget": task.resource_budget,
                "success_criteria": task.success_criteria,
                "verification_specs": task.verification,
                "status": task.status,
                "state_version": task.state_version,
                "attempt": task.attempt,
                "max_attempts": task.max_attempts,
                "tried_agents": task.tried_agents,
                "outcome": task.outcome,
                "created_at": _iso(task.created_at),
                "finalized_at": _iso(task.finalized_at),
            },
            "goal": (
                {
                    "goal_id": str(goal.id),
                    "drive_type": goal.drive_type,
                    "title": goal.title,
                    "status": goal.status,
                }
                if goal
                else None
            ),
            "plan": (
                {
                    "plan_id": str(plan.id),
                    "status": plan.status,
                    "permission_level": plan.permission_level,
                    "plan_hash": plan.plan_hash,
                    "policy_verdict": plan.policy_verdict,
                }
                if plan
                else None
            ),
            "lifecycle_events": [
                {
                    "id": str(ev.id),
                    "execution_id": str(ev.execution_id) if ev.execution_id else None,
                    "state_version": ev.state_version,
                    "from_status": ev.from_status,
                    "to_status": ev.to_status,
                    "actor": ev.actor,
                    "reason": ev.reason,
                    "created_at": _iso(ev.created_at),
                }
                for ev in lifecycle
            ],
            "executions": [
                {
                    "execution_id": str(e.id),
                    "agent_id": e.agent_id,
                    "attempt_number": e.attempt_number,
                    "status": e.status,
                    "pid": e.pid,
                    "runner_id": e.runner_id,
                    "thread_id": e.thread_id,
                    "process_identity": e.process_identity,
                    "started_at": _iso(e.started_at),
                    "completed_at": _iso(e.completed_at),
                    "finalized_at": _iso(e.finalized_at),
                    "duration_seconds": (
                        round((e.completed_at - e.started_at).total_seconds(), 2)
                        if (e.started_at and e.completed_at)
                        else None
                    ),
                    "verification": e.verification,
                    "resource_usage": e.resource_usage,
                    "result": e.result,
                    "stdout_excerpt": (e.stdout or "")[:3000] if e.stdout else None,
                    "stderr_excerpt": (e.stderr or "")[:1500] if e.stderr else None,
                    "error": e.error,
                }
                for e in execs
            ],
            "recovery_records": [
                {
                    "id": str(r.id),
                    "execution_id": str(r.execution_id),
                    "runner_id": r.runner_id,
                    "trigger": r.trigger,
                    "previous_status": r.previous_status,
                    "resolved_status": r.resolved_status,
                    "action_taken": r.action_taken,
                    "notes": r.notes,
                    "observed_identity": r.observed_identity,
                    "created_at": _iso(r.created_at),
                }
                for r in recoveries
            ],
            "approvals": [
                {
                    "approval_id": str(a.id),
                    "status": a.status,
                    "permission_level": a.permission_level,
                    "approver": a.approver,
                    "channel": a.channel,
                    "decision_note": a.decision_note,
                    "created_at": _iso(a.created_at),
                    "decided_at": _iso(a.decided_at),
                }
                for a in approvals
            ],
            "routing_decisions": [
                {
                    "id": str(d.id),
                    "decision_type": d.decision_type,
                    "selected": d.selected,
                    "reason": d.reason,
                    "payload": d.payload,
                    "created_at": _iso(d.created_at),
                }
                for d in routing
            ],
        }

    # ── E. Memory ────────────────────────────────────────────────────────────
    @router.get("/v2/control-center/memory")
    async def cc_memory(
        limit: int = Query(default=25, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        memory_type: str | None = None,
        sync_status: str | None = None,
        search: str | None = None,
        _: Principal = Depends(require_scope(SCOPE_READ)),
    ) -> dict[str, Any]:
        rt = get_runtime()
        async with rt.db.session() as s:
            q = select(MemoryIndex)
            cnt_q = select(func.count(MemoryIndex.id))
            if memory_type:
                q = q.where(MemoryIndex.memory_type == memory_type)
                cnt_q = cnt_q.where(MemoryIndex.memory_type == memory_type)
            if sync_status:
                q = q.where(MemoryIndex.sync_status == sync_status)
                cnt_q = cnt_q.where(MemoryIndex.sync_status == sync_status)
            if search:
                like = f"%{search.strip()}%"
                cond = or_(MemoryIndex.title.ilike(like), MemoryIndex.summary.ilike(like))
                q = q.where(cond)
                cnt_q = cnt_q.where(cond)

            total = int((await s.execute(cnt_q)).scalar_one() or 0)
            rows = (await s.execute(q.order_by(desc(MemoryIndex.created_at)).offset(offset).limit(limit))).scalars().all()

            by_type = {
                str(r[0]): int(r[1])
                for r in (
                    await s.execute(select(MemoryIndex.memory_type, func.count(MemoryIndex.id)).group_by(MemoryIndex.memory_type))
                ).all()
            }
            by_sync = {
                str(r[0]): int(r[1])
                for r in (
                    await s.execute(select(MemoryIndex.sync_status, func.count(MemoryIndex.id)).group_by(MemoryIndex.sync_status))
                ).all()
            }
            by_emb = {
                str(r[0]): int(r[1])
                for r in (
                    await s.execute(
                        select(MemoryIndex.embedding_status, func.count(MemoryIndex.id)).group_by(MemoryIndex.embedding_status)
                    )
                ).all()
            }
            outbox_stats = [
                {"event_type": str(r[0]), "status": str(r[1]), "count": int(r[2])}
                for r in (
                    await s.execute(
                        select(OutboxEvent.event_type, OutboxEvent.status, func.count(OutboxEvent.id)).group_by(
                            OutboxEvent.event_type, OutboxEvent.status
                        )
                    )
                ).all()
            ]
            models = (
                await s.execute(select(EmbeddingModelRegistry).order_by(desc(EmbeddingModelRegistry.created_at)))
            ).scalars().all()
            sm_versions = (
                await s.execute(select(SelfModelVersion).order_by(desc(SelfModelVersion.version_number)).limit(15))
            ).scalars().all()

            pgvector_active = False
            embedding_col_type = "json"
            if s.bind is not None and s.bind.dialect.name == "postgresql":
                try:
                    ext = (
                        await s.execute(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'"))
                    ).scalar_one_or_none()
                    pgvector_active = ext is not None
                    col_t = (
                        await s.execute(
                            text(
                                "SELECT udt_name FROM information_schema.columns "
                                "WHERE table_name = 'memory_embeddings' AND column_name = 'embedding' LIMIT 1"
                            )
                        )
                    ).scalar_one_or_none()
                    if col_t:
                        embedding_col_type = str(col_t)
                except Exception:
                    pass

        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": [
                {
                    "memory_id": str(m.id),
                    "external_id": m.external_id,
                    "memory_type": m.memory_type,
                    "title": m.title,
                    "summary": m.summary,
                    "source": m.source,
                    "importance": float(m.importance),
                    "reliability": float(m.reliability),
                    "information_gain": m.information_gain,
                    "tags": list(m.tags or []),
                    "cycle_id": str(m.cycle_id) if m.cycle_id else None,
                    "sync_status": m.sync_status,
                    "sync_error": m.sync_error,
                    "embedding_status": m.embedding_status,
                    "created_at": _iso(m.created_at),
                }
                for m in rows
            ],
            "summary": {
                "by_type": by_type,
                "by_sync_status": by_sync,
                "by_embedding_status": by_emb,
                "letta_available": bool(rt.memory.backend_available),
                "pgvector_active": pgvector_active,
                "embedding_column_type": embedding_col_type,
                "outbox": outbox_stats,
                "models": [
                    {
                        "id": str(em.id),
                        "model_key": em.model_key,
                        "provider": em.provider,
                        "model_name": em.model_name,
                        "model_version": em.model_version,
                        "dimensions": em.dimensions,
                        "is_active": em.is_active,
                    }
                    for em in models
                ],
            },
            "self_model_versions": [
                {
                    "version_id": str(v.id),
                    "version_number": v.version_number,
                    "summary_text": v.summary_text,
                    "letta_synced_at": _iso(v.letta_synced_at),
                    "created_at": _iso(v.created_at),
                }
                for v in sm_versions
            ],
        }

    # ── F. World Perception ──────────────────────────────────────────────────
    @router.get("/v2/control-center/world")
    async def cc_world(
        limit: int = Query(default=30, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        source_id: str | None = None,
        status: str | None = None,
        _: Principal = Depends(require_scope(SCOPE_READ)),
    ) -> dict[str, Any]:
        rt = get_runtime()
        async with rt.db.session() as s:
            sources = (await s.execute(select(WorldEventSource).order_by(WorldEventSource.source_id))).scalars().all()
            cps = {
                c.source_id: c
                for c in (await s.execute(select(SensorCheckpoint))).scalars().all()
            }

            ev_q = select(WorldEventRow)
            cnt_q = select(func.count(WorldEventRow.id))
            if source_id:
                ev_q = ev_q.where(WorldEventRow.source_id == source_id)
                cnt_q = cnt_q.where(WorldEventRow.source_id == source_id)
            if status:
                ev_q = ev_q.where(WorldEventRow.status == status)
                cnt_q = cnt_q.where(WorldEventRow.status == status)

            total_events = int((await s.execute(cnt_q)).scalar_one() or 0)
            events = (
                await s.execute(ev_q.order_by(desc(WorldEventRow.observed_at)).offset(offset).limit(limit))
            ).scalars().all()

            ws_rows = (await s.execute(select(WorldState).order_by(desc(WorldState.updated_at)).limit(100))).scalars().all()
            latest_obs = (
                await s.execute(select(ObservationRecord).order_by(desc(ObservationRecord.created_at)).limit(1))
            ).scalar_one_or_none()
            mcp_usage_rows = (
                await s.execute(
                    select(ResourceUsage)
                    .where(ResourceUsage.category == "mcp_tool")
                    .order_by(desc(ResourceUsage.created_at))
                    .limit(30)
                )
            ).scalars().all()
            mcp_policy_cnt = int(
                (
                    await s.execute(
                        select(func.count(PolicyEvaluation.id)).where(PolicyEvaluation.subject_type == "mcp_tool")
                    )
                ).scalar_one()
                or 0
            )

        quarantined = [
            {
                "event_id": str(e.id),
                "source_id": e.source_id,
                "title": e.title,
                "status": e.status,
                "observed_at": _iso(e.observed_at),
                "metadata": e.meta,
            }
            for e in events
            if (e.meta or {}).get("quarantined_injection") or e.status == "quarantined"
        ]

        sensors_cfg = get_sensors_config()
        mcp_registered = rt.mcp_manager.summary_for_brain() if rt.mcp_manager is not None else []
        eff_poll_on_perceive = (
            bool(rt.settings.kernel_v3.world_poll_on_perceive)
            if rt.settings.kernel_v3.world_poll_on_perceive is not None
            else bool(sensors_cfg.get("poll_on_perceive", False))
        )
        hypotheses = []
        for w in ws_rows:
            if w.kind != "hypothesis":
                continue
            val = w.value if isinstance(w.value, dict) else {}
            ev_list = val.get("evidence") if isinstance(val.get("evidence"), list) else []
            hypotheses.append(
                {
                    "id": str(w.id),
                    "key": w.key,
                    "status": w.status,
                    "claim": val.get("claim", ""),
                    "verification_hint": val.get("verification_hint", ""),
                    "confidence": val.get("confidence", 0.5),
                    "evidence_count": len(ev_list),
                    "evidence": ev_list[-5:],
                    "last_verdict_summary": val.get("last_verdict_summary"),
                    "source": w.source,
                    "updated_at": _iso(w.updated_at),
                }
            )

        return {
            "poll_on_perceive": eff_poll_on_perceive,
            "importance_threshold": float(sensors_cfg.get("importance_threshold", 0.45)),
            "allowed_domains": list(sensors_cfg.get("allowed_domains") or []),
            "hypotheses": hypotheses,
            "mcp_tools": {
                "in_loop_enabled": bool(rt.settings.kernel_v3.mcp_in_loop_enabled),
                "max_calls_per_cycle": int(rt.settings.kernel_v3.max_mcp_calls_per_cycle),
                "registered": mcp_registered,
                "policy_evals_count": mcp_policy_cnt,
                "recent_invocations": [
                    {
                        "id": str(u.id),
                        "cycle_id": str(u.cycle_id) if u.cycle_id else None,
                        "tool_name": (u.details or {}).get("tool_name", "unknown"),
                        "server_name": (u.details or {}).get("server_name", ""),
                        "ok": (u.details or {}).get("ok", True),
                        "seconds": round(float(u.seconds or 0.0), 3),
                        "created_at": _iso(u.created_at),
                    }
                    for u in mcp_usage_rows
                ],
            },
            "sources": [
                {
                    "source_id": src.source_id,
                    "sensor_type": src.sensor_type,
                    "name": src.name,
                    "enabled": src.enabled,
                    "reliability_score": src.reliability_score,
                    "poll_interval_seconds": src.poll_interval_seconds,
                    "health_status": src.health_status,
                    "last_error": src.last_error,
                    "config": src.config,
                    "checkpoint": (
                        {
                            "last_polled_at": _iso(cps[src.source_id].last_polled_at),
                            "last_success_at": _iso(cps[src.source_id].last_success_at),
                            "consecutive_failures": cps[src.source_id].consecutive_failures,
                            "cursor": cps[src.source_id].cursor,
                        }
                        if src.source_id in cps
                        else None
                    ),
                }
                for src in sources
            ],
            "events": {
                "total": total_events,
                "limit": limit,
                "offset": offset,
                "items": [
                    {
                        "event_id": str(e.id),
                        "source_id": e.source_id,
                        "event_type": e.event_type,
                        "title": e.title,
                        "content": e.content[:800],
                        "source_reliability": e.source_reliability,
                        "importance_score": e.importance_score,
                        "status": e.status,
                        "quarantined_injection": bool((e.meta or {}).get("quarantined_injection")),
                        "metadata": e.meta,
                        "observed_at": _iso(e.observed_at),
                    }
                    for e in events
                ],
            },
            "quarantined_injections": quarantined,
            "world_states": [
                {
                    "id": str(w.id),
                    "kind": w.kind,
                    "key": w.key,
                    "status": w.status,
                    "source": w.source,
                    "is_simulated": w.is_simulated,
                    "value": w.value,
                    "updated_at": _iso(w.updated_at),
                }
                for w in ws_rows
            ],
            "system_sensor_latest": latest_obs.system_state if latest_obs else None,
        }

    # ── G. Self Model ────────────────────────────────────────────────────────
    @router.get("/v2/control-center/self-model")
    async def cc_self_model(_: Principal = Depends(require_scope(SCOPE_READ))) -> dict[str, Any]:
        rt = get_runtime()
        org_id = rt.organism_id
        async with rt.db.session() as s:
            identity = await s.get(OrganismIdentity, org_id) if org_id else None
            latest_ver = (
                await s.execute(select(SelfModelVersion).order_by(desc(SelfModelVersion.version_number)).limit(1))
            ).scalar_one_or_none()
            versions = (
                await s.execute(select(SelfModelVersion).order_by(desc(SelfModelVersion.version_number)).limit(25))
            ).scalars().all()
            profiles = (
                await s.execute(
                    select(CapabilityProfile).order_by(
                        desc(CapabilityProfile.sample_count), CapabilityProfile.capability_key.asc()
                    )
                )
            ).scalars().all()
            ev_rows = (
                await s.execute(select(CapabilityEvidence).order_by(desc(CapabilityEvidence.created_at)).limit(60))
            ).scalars().all()
            agents = (await s.execute(select(AgentRegistryRow).order_by(AgentRegistryRow.agent_id))).scalars().all()
            skills = (await s.execute(select(SkillRegistry).order_by(desc(SkillRegistry.updated_at)))).scalars().all()
            skill_vals = (
                await s.execute(select(SkillValidationResult).order_by(desc(SkillValidationResult.executed_at)).limit(50))
            ).scalars().all()

        vals_by_skill: dict[uuid.UUID, list[dict[str, Any]]] = {}
        for sv in skill_vals:
            vals_by_skill.setdefault(sv.skill_id, []).append(
                {
                    "id": str(sv.id),
                    "passed": sv.passed,
                    "checks": sv.checks,
                    "executed_at": _iso(sv.executed_at),
                }
            )

        prof_key_by_id = {p.id: p.capability_key for p in profiles}
        return {
            "identity": (
                {
                    "id": str(identity.id),
                    "name": identity.name,
                    "version": identity.version,
                    "traits": identity.traits,
                    "memory_agent_id": identity.memory_agent_id,
                    "created_at": _iso(identity.created_at),
                }
                if identity
                else None
            ),
            "latest_snapshot": (
                {
                    "version_id": str(latest_ver.id),
                    "version_number": latest_ver.version_number,
                    "summary_text": latest_ver.summary_text,
                    "profile_snapshot": latest_ver.profile_snapshot,
                    "letta_synced_at": _iso(latest_ver.letta_synced_at),
                    "created_at": _iso(latest_ver.created_at),
                }
                if latest_ver
                else None
            ),
            "capabilities": [
                {
                    "id": str(p.id),
                    "capability_key": p.capability_key,
                    "maturity_status": p.maturity_status,
                    "alpha": round(float(p.alpha), 3),
                    "beta": round(float(p.beta), 3),
                    "sample_count": int(p.sample_count),
                    "success_samples": max(0, int(round(float(p.alpha) - 1.0))),
                    "failure_samples": max(0, int(round(float(p.beta) - 1.0))),
                    "estimated_success_rate": round(float(p.estimated_success_rate), 4),
                    "confidence_low": round(float(p.confidence_low), 4),
                    "confidence_high": round(float(p.confidence_high), 4),
                    "updated_at": _iso(p.updated_at),
                }
                for p in profiles
            ],
            "recent_evidence": [
                {
                    "id": str(ev.id),
                    "capability_key": prof_key_by_id.get(ev.profile_id, "unknown"),
                    "execution_id": str(ev.execution_id),
                    "task_id": str(ev.task_id),
                    "outcome": ev.outcome,
                    "weight": ev.weight,
                    "verification_summary": ev.verification_summary,
                    "created_at": _iso(ev.created_at),
                }
                for ev in ev_rows
            ],
            "agent_reliability": [
                {
                    "agent_id": a.agent_id,
                    "name": a.name,
                    "enabled": a.enabled,
                    "healthy": bool((a.probe or {}).get("ok", False)),
                    "stats": a.stats,
                }
                for a in agents
            ],
            "skills": [
                {
                    "id": str(sk.id),
                    "skill_key": sk.skill_key,
                    "version": sk.version,
                    "capability_key": sk.capability_key,
                    "description": sk.description,
                    "permission_level": sk.permission_level,
                    "status": sk.status,
                    "artifact_path": sk.artifact_path,
                    "artifact_checksum": sk.artifact_checksum,
                    "validations": vals_by_skill.get(sk.id, []),
                    "updated_at": _iso(sk.updated_at),
                }
                for sk in skills
            ],
            "versions": [
                {
                    "version_id": str(v.id),
                    "version_number": v.version_number,
                    "summary_text": v.summary_text,
                    "letta_synced_at": _iso(v.letta_synced_at),
                    "created_at": _iso(v.created_at),
                }
                for v in versions
            ],
        }

    # ── H. Governance & Token Analytics ──────────────────────────────────────
    @router.get("/v2/control-center/governance")
    async def cc_governance(
        hours: float = Query(default=24.0, ge=0.0, le=8760.0),
        limit: int = Query(default=40, ge=1, le=200),
        _: Principal = Depends(require_scope(SCOPE_READ)),
    ) -> dict[str, Any]:
        rt = get_runtime()
        since = _parse_since(hours)
        policy_cfg = get_policy_config()
        budget = await rt.governance.check_budget()

        async with rt.db.session() as s:
            pe_q = select(PolicyEvaluation).order_by(desc(PolicyEvaluation.created_at)).limit(limit)
            ap_q = select(ApprovalRecord).order_by(desc(ApprovalRecord.created_at)).limit(limit)
            ru_q = select(ResourceUsage).where(ResourceUsage.category == "llm").order_by(desc(ResourceUsage.created_at))
            if since is not None:
                pe_q = pe_q.where(PolicyEvaluation.created_at >= since)
                ru_q = ru_q.where(ResourceUsage.created_at >= since)

            policy_evals = (await s.execute(pe_q)).scalars().all()
            approvals = (await s.execute(ap_q)).scalars().all()
            llm_rows = (await s.execute(ru_q)).scalars().all()

            # V3.0 M1 kernel guard & cognitive efficiency metrics
            pf_q = select(ActionPlanRow)
            gd_q = select(ActiveGoal)
            mi_q = select(MemoryIndex)
            if since is not None:
                pf_q = pf_q.where(ActionPlanRow.created_at >= since)
                gd_q = gd_q.where(ActiveGoal.created_at >= since)
                mi_q = mi_q.where(MemoryIndex.created_at >= since)
            plan_rows = (await s.execute(pf_q)).scalars().all()
            goal_rows = (await s.execute(gd_q)).scalars().all()
            mem_rows_win = (await s.execute(mi_q)).scalars().all()

            preflight_rejected = 0
            preflight_reasons: dict[str, int] = {}
            for pr in plan_rows:
                if pr.preflight_status == "REJECT":
                    preflight_rejected += 1
                    for rc in (pr.preflight_reason_codes or []):
                        preflight_reasons[str(rc)] = preflight_reasons.get(str(rc), 0) + 1

            goal_dedup_skipped = sum(1 for g in goal_rows if g.duplicate_of is not None)
            now_utc = utcnow()
            goals_in_cooldown = sum(
                1
                for g in goal_rows
                if g.cooldown_until is not None
                and (
                    g.cooldown_until.replace(tzinfo=now_utc.tzinfo)
                    if g.cooldown_until.tzinfo is None
                    else g.cooldown_until
                )
                > now_utc
            )
            sum_ig_win = sum(float(m.information_gain or 0.0) for m in mem_rows_win if m.information_gain is not None)

            # Aggregate LLM usage by source/operation and top tasks
            breakdown: dict[str, dict[str, Any]] = {}
            tokens_by_task: dict[uuid.UUID, dict[str, Any]] = {}
            total_tokens = 0
            total_calls = 0
            failed_calls = 0
            lat_sum = 0.0
            for r in llm_rows:
                det = r.details or {}
                src = str(det.get("source") or "unknown")
                op = str(det.get("operation") or ("codex_exec" if src == "codex" else "other"))
                bkey = f"{src}:{op}"
                entry = breakdown.setdefault(
                    bkey, {"source": src, "operation": op, "calls": 0, "tokens": 0, "max_tokens": 0, "lat_sum": 0.0}
                )
                amt = int(r.amount or 0)
                sec = float(r.seconds or 0.0)
                entry["calls"] += 1
                entry["tokens"] += amt
                entry["max_tokens"] = max(entry["max_tokens"], amt)
                entry["lat_sum"] += sec
                total_tokens += amt
                total_calls += 1
                lat_sum += sec
                if det.get("error") or det.get("ok") is False or det.get("success") is False:
                    failed_calls += 1
                if r.task_id is not None:
                    te = tokens_by_task.setdefault(r.task_id, {"calls": 0, "tokens": 0})
                    te["calls"] += 1
                    te["tokens"] += amt

            top_tids = [
                tid for tid, _ in sorted(tokens_by_task.items(), key=lambda kv: kv[1]["tokens"], reverse=True)[:10]
            ]
            top_tasks_data = []
            if top_tids:
                t_map = {
                    t.id: t
                    for t in (await s.execute(select(AgentTaskRow).where(AgentTaskRow.id.in_(top_tids)))).scalars().all()
                }
                for tid in top_tids:
                    t = t_map.get(tid)
                    if t:
                        top_tasks_data.append(
                            {
                                "task_id": str(tid),
                                "cycle_id": str(t.cycle_id) if t.cycle_id else None,
                                "status": t.status,
                                "objective": t.objective[:140],
                                "llm_calls": tokens_by_task[tid]["calls"],
                                "tokens": tokens_by_task[tid]["tokens"],
                            }
                        )

        sem_enabled = bool((policy_cfg.get("semantic_policy") or {}).get("enabled", False))
        auto_max = rt.settings.governance.auto_approve_max_level
        risk_advisories: list[dict[str, str]] = []
        if auto_max in {"L2", "L3"}:
            risk_advisories.append(
                {
                    "severity": "warning",
                    "code": "AUTO_APPROVE_L2_OPEN",
                    "title": f"自动审批级别当前为 {auto_max}",
                    "detail": "L2 高风险任务与 MCP 工具将自动放行（workspace-write），仅 L3 核心禁令保持硬拒绝。此为已批准配置，请结合预算与沙箱关注执行轨迹。",
                }
            )
        if not sem_enabled:
            risk_advisories.append(
                {
                    "severity": "info",
                    "code": "SEMANTIC_POLICY_DISABLED",
                    "title": "LLM 双语语义风控层当前关闭（semantic_policy.enabled = false）",
                    "detail": "当前仅由确定性中英双语正则规则执行拦截，隐蔽语义伪装请求不会经 LLM 二次风险分类。",
                }
            )
        if rt.settings.governance.budget.max_agent_tasks_per_day >= 9000:
            risk_advisories.append(
                {
                    "severity": "info",
                    "code": "HIGH_LOCAL_QUOTA",
                    "title": f"本地 Qwen 配额已放宽（日任务上限 {rt.settings.governance.budget.max_agent_tasks_per_day}）",
                    "detail": "适用于本地部署模型的高吞吐探索，但单个多轮 Codex 任务仍可能消耗大量 Token，建议通过此页面持续观测单任务峰值。",
                }
            )

        return {
            "window_hours": hours,
            "effective_policy": {
                "auto_approve_max_level": auto_max,
                "semantic_policy_enabled": sem_enabled,
                "semantic_fail_closed_level": (policy_cfg.get("semantic_policy") or {}).get("fail_closed_level", "L2"),
                "approval_default_ttl_hours": (policy_cfg.get("approval") or {}).get("default_ttl_hours", 24),
                "permission_levels": policy_cfg.get("permission_levels", {}),
                "level_sandbox": policy_cfg.get("level_sandbox", {}),
                "escalation_rules_count": len(policy_cfg.get("escalation_rules") or []),
                "escalation_rules": [
                    {
                        "level": r.get("level"),
                        "reason": r.get("reason"),
                        "patterns_count": len(r.get("patterns") or []),
                    }
                    for r in (policy_cfg.get("escalation_rules") or [])
                ],
            },
            "risk_advisories": risk_advisories,
            "kernel_v3_metrics": {
                "plan_preflight_enabled": rt.settings.kernel_v3.plan_preflight_enabled,
                "goal_dedup_enabled": rt.settings.kernel_v3.goal_dedup_enabled,
                "preflight_rejected_count": preflight_rejected,
                "preflight_reason_breakdown": preflight_reasons,
                "goal_dedup_skipped_count": goal_dedup_skipped,
                "goals_in_cooldown": goals_in_cooldown,
                "sum_verified_ig": round(sum_ig_win, 4),
                "verified_ig_per_1m_tokens": (
                    round(sum_ig_win / (total_tokens / 1_000_000.0), 4) if total_tokens > 0 else 0.0
                ),
            },
            "budget": budget.model_dump(mode="json"),
            "token_analytics": {
                "total_tokens": total_tokens,
                "total_calls": total_calls,
                "failed_calls": failed_calls,
                "error_rate": round(failed_calls / total_calls, 4) if total_calls else 0.0,
                "avg_latency_seconds": round(lat_sum / total_calls, 2) if total_calls else 0.0,
                "by_source": [
                    {
                        "source": v["source"],
                        "operation": v["operation"],
                        "calls": v["calls"],
                        "tokens": v["tokens"],
                        "avg_tokens": int(v["tokens"] / v["calls"]) if v["calls"] else 0,
                        "max_tokens": v["max_tokens"],
                        "avg_seconds": round(v["lat_sum"] / v["calls"], 2) if v["calls"] else 0.0,
                    }
                    for v in sorted(breakdown.values(), key=lambda x: x["tokens"], reverse=True)
                ],
                "top_tasks": top_tasks_data,
            },
            "policy_evaluations": [
                {
                    "id": str(pe.id),
                    "cycle_id": str(pe.cycle_id) if pe.cycle_id else None,
                    "subject_type": pe.subject_type,
                    "requested_level": pe.requested_level,
                    "deterministic_level": pe.deterministic_level,
                    "semantic_level": pe.semantic_level,
                    "effective_level": pe.effective_level,
                    "verdict": pe.verdict,
                    "violations": pe.violations,
                    "reason": pe.reason,
                    "created_at": _iso(pe.created_at),
                }
                for pe in policy_evals
            ],
            "approvals": [
                {
                    "approval_id": str(a.id),
                    "task_id": str(a.task_id),
                    "plan_id": str(a.plan_id) if a.plan_id else None,
                    "permission_level": a.permission_level,
                    "status": a.status,
                    "approver": a.approver,
                    "channel": a.channel,
                    "decision_note": a.decision_note,
                    "plan_hash": a.plan_hash[:16] + "…" if a.plan_hash else None,
                    "expires_at": _iso(a.expires_at),
                    "decided_at": _iso(a.decided_at),
                    "created_at": _iso(a.created_at),
                }
                for a in approvals
            ],
            "mcp_tools": [t.model_dump(mode="json") for t in rt.mcp_manager.list_tools()],
        }

    # ── I. Configuration Center (12 Domains, Strictly Redacted & Read-Only) ──
    @router.get("/v2/control-center/config")
    async def cc_config(_: Principal = Depends(require_scope(SCOPE_READ))) -> dict[str, Any]:
        rt = get_runtime()
        s = rt.settings
        settings_yaml = load_yaml("settings.yaml")
        drives_yaml = get_drive_config()
        agents_yaml = get_agent_config()
        policy_yaml = get_policy_config()
        lifecycle_yaml = get_lifecycle_config()
        runner_yaml = get_runner_config()
        embedding_yaml = get_embedding_config()
        memory_yaml = get_memory_config()
        sensors_yaml = get_sensors_config()
        self_model_yaml = get_self_model_config()
        affect_yaml = get_affect_config()
        dotenv_keys = _read_dotenv_keys()

        def item(**kw: Any) -> dict[str, Any]:
            return _cfg_item(dotenv_keys=dotenv_keys, **kw)

        domains = [
            {
                "id": "core_lifecycle",
                "name": "Core 与 Lifecycle",
                "description": "生命体核心身份、数据库与持久化任务生命周期参数",
                "items": [
                    item(key="organism.name", label="生命体名称", description="AOC 实例唯一身份名称", effective_value=s.organism.name, yaml_value=_nested_get(settings_yaml, "organism", "name"), default_value="AOC-001", env_var="AOC_ORGANISM__NAME"),
                    item(key="organism.version", label="版本号", description="当前核心声明版本", effective_value=s.organism.version, yaml_value=_nested_get(settings_yaml, "organism", "version"), default_value="1.0.0", env_var="AOC_ORGANISM__VERSION"),
                    item(key="api.host", label="API 监听地址", description="默认仅限 127.0.0.1 回环绑定", effective_value=s.api.host, yaml_value=_nested_get(settings_yaml, "api", "host"), default_value="127.0.0.1", env_var="AOC_API__HOST", risk_note="非回环地址必须启用鉴权" if s.api.host not in {"127.0.0.1", "localhost", "::1"} else None),
                    item(key="api.port", label="API 端口", description="FastAPI 与 Control Center 监听端口", effective_value=s.api.port, yaml_value=_nested_get(settings_yaml, "api", "port"), default_value=9200, env_var="AOC_API__PORT"),
                    item(key="api.auth_enabled", label="API Bearer 鉴权", description="控制非 /health 端点是否强制 Bearer Token 鉴权", effective_value=s.api.auth_enabled, yaml_value=_nested_get(settings_yaml, "api", "auth_enabled"), default_value=True, env_var="AOC_API__AUTH_ENABLED"),
                    item(key="api.admin_key", label="管理员 API Key", description="环境变量管理员引导密钥（严格脱敏）", effective_value=bool(s.api.admin_key and s.api.admin_key.get_secret_value()), env_var="AOC_API__ADMIN_KEY", sensitive=True),
                    item(key="database.url", label="PostgreSQL 连接地址", description="权威状态数据库连接串（密码已脱敏）", effective_value=s.database.url, yaml_value=_nested_get(settings_yaml, "database", "url"), env_var="AOC_DATABASE__URL", sensitive=True),
                    item(key="lifecycle.outbox_poll_interval", label="Outbox 轮询间隔 (s)", description="PostgreSQL Transactional Outbox 后台投递周期", effective_value=s.lifecycle.outbox_poll_interval, yaml_value=lifecycle_yaml.get("outbox_poll_interval"), default_value=2.0),
                    item(key="lifecycle.outbox_max_attempts", label="Outbox 最大重试次数", description="超过次数转入 dead_letter 等待重放", effective_value=s.lifecycle.outbox_max_attempts, yaml_value=lifecycle_yaml.get("outbox_max_attempts"), default_value=5),
                    item(key="lifecycle.max_inflight_tasks", label="最大并行在途任务", description="Durable FSM 同时允许的在途任务上限", effective_value=s.lifecycle.max_inflight_tasks, yaml_value=lifecycle_yaml.get("max_inflight_tasks"), default_value=1),
                ],
            },
            {
                "id": "drives",
                "name": "Drives（欲望系统）",
                "description": "Survival / Reproduction / Curiosity 权重与仲裁阈值（config/drives.yaml）",
                "items": [
                    item(key="drives.survival.emergency_threshold", label="Survival 紧急阈值", description="低于该值触发 CRITICAL 物理生存保护（已与经济配额解耦）", effective_value=_nested_get(drives_yaml, "survival", "emergency_threshold") or 0.30, yaml_value=_nested_get(drives_yaml, "survival", "emergency_threshold"), default_value=0.30),
                    item(key="drives.arbitration.idle_threshold", label="合法 IDLE 优先级阈值", description="最高驱动优先级低于该阈值时合法休眠", effective_value=_nested_get(drives_yaml, "arbitration", "idle_threshold") or 0.15, yaml_value=_nested_get(drives_yaml, "arbitration", "idle_threshold"), default_value=0.15),
                    item(key="drives.weights", label="三大欲望基础权重", description="Survival / Reproduction / Curiosity 基础仲裁权重", effective_value=_nested_get(drives_yaml, "arbitration", "weights") or drives_yaml.get("weights"), yaml_value=_nested_get(drives_yaml, "arbitration", "weights") or drives_yaml.get("weights")),
                    item(key="self_model.minimum_samples", label="能力 VALIDATED 最小样本量", description="Beta-Binomial 能力晋升 VALIDATED 的最小验证样本数", effective_value=self_model_yaml.get("minimum_samples", 3), yaml_value=self_model_yaml.get("minimum_samples"), default_value=3),
                    item(key="self_model.confidence_threshold", label="能力置信下界阈值", description="90% Beta 置信下界达标阈值", effective_value=self_model_yaml.get("confidence_threshold", 0.55), yaml_value=self_model_yaml.get("confidence_threshold"), default_value=0.55),
                ],
            },
            {
                "id": "brain_llm",
                "name": "Brain 与 LLM",
                "description": "统一经 LLMGateway 访问的推理模型配置（严格与 AOC 专属通道隔离）",
                "items": [
                    item(key="llm.base_url", label="推理网关地址", description="OpenAI 兼容 vLLM 推理端点", effective_value=s.llm.base_url, yaml_value=_nested_get(settings_yaml, "llm", "base_url"), default_value="http://127.0.0.1:8000/v1", env_var="AOC_LLM__BASE_URL"),
                    item(key="llm.model", label="主推理模型", description="CognitionBrain 与 Codex 共享的本地模型", effective_value=s.llm.model, yaml_value=_nested_get(settings_yaml, "llm", "model"), default_value="default", env_var="AOC_LLM__MODEL"),
                    item(key="llm.api_key", label="LLM API Key", description="推理服务鉴权密钥（严格脱敏）", effective_value=bool(s.llm.api_key and s.llm.api_key.get_secret_value()), env_var="AOC_LLM__API_KEY", sensitive=True),
                    item(key="llm.temperature", label="采样温度", description="结构化输出默认温度", effective_value=s.llm.temperature, yaml_value=_nested_get(settings_yaml, "llm", "temperature"), default_value=0.3, env_var="AOC_LLM__TEMPERATURE"),
                    item(key="llm.max_tokens", label="单次最大生成 Token", description="单次 LLM 响应 Token 上限", effective_value=s.llm.max_tokens, yaml_value=_nested_get(settings_yaml, "llm", "max_tokens"), default_value=9999, env_var="AOC_LLM__MAX_TOKENS"),
                    item(key="llm.timeout_seconds", label="单次推理超时 (s)", description="HTTP 请求超时阈值", effective_value=s.llm.timeout_seconds, yaml_value=_nested_get(settings_yaml, "llm", "timeout_seconds"), default_value=180.0, env_var="AOC_LLM__TIMEOUT_SECONDS"),
                    item(key="llm.max_schema_retries", label="Schema 重试次数", description="JSON/Pydantic 校验失败后的最大重试次数", effective_value=s.llm.max_schema_retries, yaml_value=_nested_get(settings_yaml, "llm", "max_schema_retries"), default_value=2, env_var="AOC_LLM__MAX_SCHEMA_RETRIES"),
                ],
            },
            {
                "id": "agents_runner",
                "name": "Agent 与 Runner",
                "description": "宿主机非特权 systemd Runner、UDS 通信及 AgentProvider 注册",
                "items": [
                    item(key="runner.enabled", label="宿主机 Runner 启用", description="通过 Unix Domain Socket 调用宿主机 cgroup v2 Runner", effective_value=s.runner.enabled, yaml_value=runner_yaml.get("enabled"), default_value=False, env_var="AOC_RUNNER__ENABLED"),
                    item(key="runner.socket_path", label="UDS Socket 路径", description="Core 与 Runner 的本地通信套接字（最小权限 0660）", effective_value=s.runner.socket_path, yaml_value=runner_yaml.get("socket_path"), default_value="/run/aoc-runner/runner.sock", env_var="AOC_RUNNER__SOCKET_PATH"),
                    item(key="runner.auth_token", label="Runner UDS 认证令牌", description="X-AOC-Runner-Token 共享密钥（严格脱敏）", effective_value=bool(s.runner.auth_token and s.runner.auth_token.get_secret_value()), env_var="AOC_RUNNER__AUTH_TOKEN", sensitive=True),
                    item(key="runner.cgroup_memory_max", label="单任务内存硬限", description="cgroup v2 memory.max 隔离上限", effective_value=s.runner.cgroup_memory_max, yaml_value=runner_yaml.get("cgroup_memory_max"), default_value="2G"),
                    item(key="runner.cgroup_pids_max", label="单任务进程数硬限", description="cgroup v2 pids.max 防 fork bomb 上限", effective_value=s.runner.cgroup_pids_max, yaml_value=runner_yaml.get("cgroup_pids_max"), default_value=128),
                    item(key="agents.enabled_list", label="已启用 AgentProvider", description="当前配置启用的自主执行 Agent 列表（严格区别于 MCP 工具）", effective_value=[a.get("agent_id") for a in (agents_yaml.get("agents") or []) if a.get("enabled")], yaml_value=[a.get("agent_id") for a in (agents_yaml.get("agents") or []) if a.get("enabled")]),
                ],
            },
            {
                "id": "governance_approval",
                "name": "Governance 与 Approval",
                "description": "权限分级、审批策略、语义风控与 Telegram 协同通道",
                "items": [
                    item(key="governance.auto_approve_max_level", label="自动放行最高权限等级", description="不高于此等级的任务将自动放行，无需人工点击审批", effective_value=s.governance.auto_approve_max_level, yaml_value=_nested_get(settings_yaml, "governance", "auto_approve_max_level"), default_value="L1", env_var="AOC_GOVERNANCE__AUTO_APPROVE_MAX_LEVEL", risk_note="当前已放开至 L2，仅 L3 核心禁令硬拒绝" if s.governance.auto_approve_max_level == "L2" else None),
                    item(key="governance.workspace_root", label="隔离工作空间根目录", description="每个任务在独立子目录内运行，禁止路径逃逸", effective_value=s.governance.workspace_root, yaml_value=_nested_get(settings_yaml, "governance", "workspace_root"), default_value="/home/ubuntu/aoc-workspaces", env_var="AOC_GOVERNANCE__WORKSPACE_ROOT"),
                    item(key="policy.semantic_policy.enabled", label="双语语义风控开关", description="是否经 LLMGateway 执行隐蔽意图语义分类", effective_value=bool(_nested_get(policy_yaml, "semantic_policy", "enabled")), yaml_value=_nested_get(policy_yaml, "semantic_policy", "enabled"), default_value=False, risk_note="当前未启用语义层，仅依靠确定性中英文规则" if not _nested_get(policy_yaml, "semantic_policy", "enabled") else None),
                    item(key="policy.approval.default_ttl_hours", label="审批单默认有效期 (h)", description="超时自动置 EXPIRED 防止陈旧审批被误执行", effective_value=_nested_get(policy_yaml, "approval", "default_ttl_hours") or 24, yaml_value=_nested_get(policy_yaml, "approval", "default_ttl_hours"), default_value=24),
                    item(key="telegram.enabled", label="Telegram 通知/审批开关", description="是否启用 @Joel_aoc_bot 异步通知与受控 Webhook", effective_value=s.telegram.enabled, default_value=False, env_var="AOC_TELEGRAM__ENABLED"),
                    item(key="telegram.bot_token", label="Telegram Bot Token", description="机器人 API 密钥（严格脱敏）", effective_value=bool(s.telegram.bot_token and s.telegram.bot_token.get_secret_value()), env_var="AOC_TELEGRAM__BOT_TOKEN", sensitive=True),
                    item(key="telegram.webhook_secret", label="Telegram Webhook Secret", description="回调验签密钥（严格脱敏）", effective_value=bool(s.telegram.webhook_secret and s.telegram.webhook_secret.get_secret_value()), env_var="AOC_TELEGRAM__WEBHOOK_SECRET", sensitive=True),
                ],
            },
            {
                "id": "budget",
                "name": "Budget（预算治理）",
                "description": "统一由 LLMGateway 与 GovernanceManager 强制执行的调用与 Token 限额",
                "items": [
                    item(key="governance.budget.max_agent_tasks_per_day", label="每日最大 Agent 任务数", description="达到上限后非生存危机转入 THROTTLED", effective_value=s.governance.budget.max_agent_tasks_per_day, yaml_value=_nested_get(settings_yaml, "governance", "budget", "max_agent_tasks_per_day"), default_value=9999, env_var="AOC_GOVERNANCE__BUDGET__MAX_AGENT_TASKS_PER_DAY", risk_note="已为本地 Qwen 模型放宽至 9999" if s.governance.budget.max_agent_tasks_per_day >= 9000 else None),
                    item(key="governance.budget.max_llm_calls_per_cycle", label="单周期最大 LLM 调用数", description="由 LLMGateway 原子预占强制执行", effective_value=s.governance.budget.max_llm_calls_per_cycle, yaml_value=_nested_get(settings_yaml, "governance", "budget", "max_llm_calls_per_cycle"), default_value=9999),
                    item(key="governance.budget.max_tokens_per_cycle", label="单周期最大 Token 数", description="单周期内 Brain + 重试累计 Token 硬顶", effective_value=s.governance.budget.max_tokens_per_cycle, yaml_value=_nested_get(settings_yaml, "governance", "budget", "max_tokens_per_cycle"), default_value=99_990_000),
                    item(key="governance.budget.max_llm_tokens_per_day", label="每日常规 Token 上限", description="覆盖 Brain 与同源 Codex 归集 Token", effective_value=s.governance.budget.max_llm_tokens_per_day, yaml_value=_nested_get(settings_yaml, "governance", "budget", "max_llm_tokens_per_day"), default_value=999_900_000),
                    item(key="governance.budget.max_concurrent_llm_calls", label="最大并发 LLM 调用数", description="LLMGateway asyncio.Semaphore 并发上限", effective_value=s.governance.budget.max_concurrent_llm_calls, yaml_value=_nested_get(settings_yaml, "governance", "budget", "max_concurrent_llm_calls"), default_value=16),
                    item(key="governance.budget.max_concurrent_tasks", label="最大并发 Agent 任务数", description="同一时间仅允许运行的外部 Agent 任务槽位", effective_value=s.governance.budget.max_concurrent_tasks, yaml_value=_nested_get(settings_yaml, "governance", "budget", "max_concurrent_tasks"), default_value=1),
                    item(key="governance.budget.max_task_timeout_seconds", label="单任务最大超时 (s)", description="超过后由 Runner 强杀进程组", effective_value=s.governance.budget.max_task_timeout_seconds, yaml_value=_nested_get(settings_yaml, "governance", "budget", "max_task_timeout_seconds"), default_value=600),
                    item(key="governance.budget.max_task_retries", label="任务最大重试次数", description="验证失败或超时后的有限重试上限", effective_value=s.governance.budget.max_task_retries, yaml_value=_nested_get(settings_yaml, "governance", "budget", "max_task_retries"), default_value=2),
                ],
            },
            {
                "id": "memory_embedding",
                "name": "Memory 与 Embedding",
                "description": "真实神经 Embedding、混合检索与 Letta 0.16.4 异步投影边界",
                "items": [
                    item(key="embedding.provider", label="Embedding 提供商", description="local_onnx（真实神经模型）/ remote_openai / hash_fallback", effective_value=s.embedding.provider, yaml_value=embedding_yaml.get("provider"), default_value="hash_fallback", env_var="AOC_EMBEDDING__PROVIDER"),
                    item(key="embedding.model_key", label="Embedding 模型版本", description="向量表严格按 model_id 隔离，禁止跨维度混查", effective_value=s.embedding.model_key, yaml_value=embedding_yaml.get("model_key"), default_value="BAAI/bge-small-zh-v1.5:onnx-qint8"),
                    item(key="embedding.dimensions", label="向量维度", description="统一 512 维语义向量", effective_value=s.embedding.dimensions, yaml_value=embedding_yaml.get("dimensions"), default_value=512),
                    item(key="memory.provider", label="长期记忆投影后端", description="letta（仅 REST 投影，禁用 /messages 推理）或 none", effective_value=s.memory.provider, yaml_value=_nested_get(settings_yaml, "memory", "provider"), default_value="letta"),
                    item(key="memory.letta_base_url", label="Letta 服务地址", description="Letta 0.16.4 容器 REST 地址", effective_value=s.memory.letta_base_url, yaml_value=_nested_get(settings_yaml, "memory", "letta_base_url"), default_value="http://127.0.0.1:8283", env_var="AOC_MEMORY__LETTA_BASE_URL"),
                    item(key="memory.embedding_dim", label="Letta 统一嵌入维度", description="V2.1 已移除 512d→1024d 补零兼容，统一 512 维", effective_value=s.memory.embedding_dim, default_value=512, env_var="AOC_MEMORY__EMBEDDING_DIM"),
                    item(key="memory.async_outbox_sync", label="Outbox 异步同步 Letta", description="写入 PG 后通过 Outbox 异步推送，不阻塞主循环", effective_value=memory_yaml.get("async_outbox_sync", True), yaml_value=memory_yaml.get("async_outbox_sync"), default_value=True),
                    item(key="memory.hybrid_retrieval", label="混合检索五维权重", description="向量 / 文本(pg_trgm) / 时效 / 可靠度 / 重要度权重", effective_value=memory_yaml.get("hybrid_retrieval"), yaml_value=memory_yaml.get("hybrid_retrieval")),
                ],
            },
            {
                "id": "sensors",
                "name": "Sensors（环境感知）",
                "description": "RSS / Git / Web / System 传感器与 SSRF / Prompt Injection 防护",
                "items": [
                    item(key="sensors.enabled", label="数字世界感知总开关", description="启用 WorldEventPipeline", effective_value=sensors_yaml.get("enabled", True), yaml_value=sensors_yaml.get("enabled"), default_value=True),
                    item(key="sensors.poll_on_perceive", label="每周期自动拉取外源", description="是否在 PERCEPTION 阶段按 poll_interval 自动抓取外部源", effective_value=sensors_yaml.get("poll_on_perceive", False), yaml_value=sensors_yaml.get("poll_on_perceive"), default_value=False, risk_note="当前为 false，需手动调 /v2/world/poll 或在收口后开启" if not sensors_yaml.get("poll_on_perceive") else None),
                    item(key="sensors.allowed_domains", label="外网域名白名单", description="SSRF 硬门禁，仅允许白名单域名且阻断私网 IP", effective_value=sensors_yaml.get("allowed_domains", []), yaml_value=sensors_yaml.get("allowed_domains")),
                    item(key="sensors.allowed_git_roots", label="只读 Git 仓库白名单", description="GitWorldSensor 允许访问的本地路径", effective_value=sensors_yaml.get("allowed_git_roots", []), yaml_value=sensors_yaml.get("allowed_git_roots")),
                    item(key="sensors.importance_threshold", label="事件入库重要性阈值", description="低于该阈值的新事件不自动晋升为探索话题", effective_value=sensors_yaml.get("importance_threshold", 0.45), yaml_value=sensors_yaml.get("importance_threshold"), default_value=0.45),
                ],
            },
            {
                "id": "mcp",
                "name": "MCP（ToolProvider）",
                "description": "独立于 AgentProvider 的受控原子工具执行体系（stdio JSON-RPC 2.0）",
                "items": [
                    item(key="mcp.registered_tools", label="已注册受控 MCP 工具", description="每个工具独立声明 Schema、L0-L3 权限与超时", effective_value=[{"tool": t.tool_name, "server": t.server_name, "level": t.permission_level, "timeout_s": t.timeout_seconds} for t in rt.mcp_manager.list_tools()], yaml_value=agents_yaml.get("mcp_servers")),
                    item(key="mcp.unregistered_policy", label="未登记工具默认策略", description="任何未在白名单登记的工具一律硬拒绝（L3）", effective_value="DENY_L3", default_value="DENY_L3"),
                ],
            },
            {
                "id": "affect",
                "name": "Affect（可选情绪调制）",
                "description": "默认关闭的有界功能调制层（严禁触碰 Governance 与预算）",
                "items": [
                    item(key="affect.enabled", label="Affect 调制层开关", description="默认 false（零影响），开启后有界调制优先级与退避", effective_value=bool(affect_yaml.get("enabled", False)), yaml_value=affect_yaml.get("enabled"), default_value=False),
                    item(key="affect.maximum_influence", label="最大调制幅度硬上限", description="硬限幅防止覆盖主欲望与安全边界", effective_value=affect_yaml.get("maximum_influence", 0.15), yaml_value=affect_yaml.get("maximum_influence"), default_value=0.15),
                ],
            },
            {
                "id": "scheduler",
                "name": "Scheduler（调度与休眠）",
                "description": "活跃周期、IDLE 休眠、CRITICAL 频率与 THROTTLED 配额休眠参数",
                "items": [
                    item(key="runtime.cycle_interval_seconds", label="活跃周期间隔 (s)", description="普通 ACTIVE 周期结束后的休眠间隔", effective_value=s.runtime.cycle_interval_seconds, yaml_value=_nested_get(settings_yaml, "runtime", "cycle_interval_seconds"), default_value=60.0),
                    item(key="runtime.idle_sleep_seconds", label="IDLE 休眠时长 (s)", description="无高赤字欲望时的合法休眠时长", effective_value=s.runtime.idle_sleep_seconds, yaml_value=_nested_get(settings_yaml, "runtime", "idle_sleep_seconds"), default_value=300.0),
                    item(key="runtime.critical_interval_seconds", label="CRITICAL 周期频率 (s)", description="物理生存危机时的快速响应间隔", effective_value=s.runtime.critical_interval_seconds, yaml_value=_nested_get(settings_yaml, "runtime", "critical_interval_seconds"), default_value=30.0),
                    item(key="runtime.throttled_max_sleep_seconds", label="THROTTLED 切片休眠 (s)", description="等待 quota_reset_at 时的单次最大休眠切片", effective_value=s.runtime.throttled_max_sleep_seconds, yaml_value=_nested_get(settings_yaml, "runtime", "throttled_max_sleep_seconds"), default_value=900.0),
                    item(key="runtime.max_wait_per_cycle_seconds", label="单周期最大等待任务 (s)", description="超出后任务转跨周期在途运行，不阻塞主循环", effective_value=s.runtime.max_wait_per_cycle_seconds, yaml_value=_nested_get(settings_yaml, "runtime", "max_wait_per_cycle_seconds"), default_value=120.0),
                    item(key="runtime.max_cycles", label="最大运行周期数", description="0 表示无限制持续自主运行", effective_value=s.runtime.max_cycles, yaml_value=_nested_get(settings_yaml, "runtime", "max_cycles"), default_value=0),
                ],
            },
            {
                "id": "logging_monitoring",
                "name": "Logging 与 Monitoring",
                "description": "结构化日志与 Prometheus 指标采集配置",
                "items": [
                    item(key="logging.level", label="日志级别", description="structlog 全局输出级别", effective_value=s.logging.level, yaml_value=_nested_get(settings_yaml, "logging", "level"), default_value="INFO", env_var="AOC_LOGGING__LEVEL"),
                    item(key="logging.json", label="JSON 结构化日志", description="是否以 JSON 格式输出便于采集审计", effective_value=s.logging.json_output, yaml_value=_nested_get(settings_yaml, "logging", "json"), default_value=True),
                    item(key="api.internal_token", label="Prometheus / 内部端点 Token", description="保护 /metrics 与 /v1/embeddings（严格脱敏）", effective_value=bool(s.api.internal_token and s.api.internal_token.get_secret_value()), env_var="AOC_API__INTERNAL_TOKEN", sensitive=True),
                    item(key="monitoring.metrics_endpoint", label="Prometheus 采集端点", description="需 SCOPE_INTERNAL 或 ADMIN Bearer Token", effective_value="/metrics", default_value="/metrics", requires_restart=False),
                ],
            },
        ]

        return {
            "read_only": True,
            "generated_at": _iso(utcnow()),
            "domains": domains,
        }

    # ── J. Unified Logs & Events ─────────────────────────────────────────────
    @router.get("/v2/control-center/logs")
    async def cc_logs(
        kind: str = Query(default="all", pattern="^(all|events|decisions|policy|recovery|cycles)$"),
        level: str | None = None,
        module: str | None = None,
        cycle_id: str | None = None,
        task_id: str | None = None,
        search: str | None = None,
        hours: float | None = Query(default=24.0, ge=0.0, le=8760.0),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        _: Principal = Depends(require_scope(SCOPE_READ)),
    ) -> dict[str, Any]:
        rt = get_runtime()
        since = _parse_since(hours)
        cid_uuid = None
        tid_uuid = None
        if cycle_id:
            try:
                cid_uuid = uuid.UUID(cycle_id)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid cycle_id UUID") from exc
        if task_id:
            try:
                tid_uuid = uuid.UUID(task_id)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid task_id UUID") from exc

        entries: list[dict[str, Any]] = []
        fetch_cap = min(500, offset + limit + 100)

        async with rt.db.session() as s:
            if kind in {"all", "events"} and tid_uuid is None:
                q = select(EventLog).order_by(desc(EventLog.created_at)).limit(fetch_cap)
                if since is not None:
                    q = q.where(EventLog.created_at >= since)
                if level:
                    q = q.where(EventLog.level == level.lower())
                if cid_uuid is not None:
                    q = q.where(EventLog.cycle_id == cid_uuid)
                for e in (await s.execute(q)).scalars().all():
                    entries.append(
                        {
                            "id": str(e.id),
                            "kind": "events",
                            "level": e.level,
                            "module": e.event_type.split(".")[0] if "." in e.event_type else "system",
                            "title": e.event_type,
                            "message": e.message,
                            "cycle_id": str(e.cycle_id) if e.cycle_id else None,
                            "task_id": None,
                            "payload": e.payload,
                            "created_at": _iso(e.created_at),
                        }
                    )

            if kind in {"all", "decisions"} and tid_uuid is None:
                q = select(DecisionLog).order_by(desc(DecisionLog.created_at)).limit(fetch_cap)
                if since is not None:
                    q = q.where(DecisionLog.created_at >= since)
                if cid_uuid is not None:
                    q = q.where(DecisionLog.cycle_id == cid_uuid)
                for d in (await s.execute(q)).scalars().all():
                    entries.append(
                        {
                            "id": str(d.id),
                            "kind": "decisions",
                            "level": "info",
                            "module": d.decision_type,
                            "title": f"{d.decision_type} → {d.selected}",
                            "message": d.reason,
                            "cycle_id": str(d.cycle_id) if d.cycle_id else None,
                            "task_id": (d.payload or {}).get("task_id") if isinstance(d.payload, dict) else None,
                            "payload": d.payload,
                            "created_at": _iso(d.created_at),
                        }
                    )

            if kind in {"all", "policy"} and tid_uuid is None:
                q = select(PolicyEvaluation).order_by(desc(PolicyEvaluation.created_at)).limit(fetch_cap)
                if since is not None:
                    q = q.where(PolicyEvaluation.created_at >= since)
                if cid_uuid is not None:
                    q = q.where(PolicyEvaluation.cycle_id == cid_uuid)
                for p in (await s.execute(q)).scalars().all():
                    lvl = "error" if p.verdict == "denied" else ("warning" if p.verdict == "requires_approval" else "info")
                    entries.append(
                        {
                            "id": str(p.id),
                            "kind": "policy",
                            "level": lvl,
                            "module": f"governance.{p.subject_type}",
                            "title": f"{p.subject_type} [{p.requested_level}→{p.effective_level}] {p.verdict}",
                            "message": p.reason,
                            "cycle_id": str(p.cycle_id) if p.cycle_id else None,
                            "task_id": None,
                            "payload": {
                                "violations": p.violations,
                                "deterministic_level": p.deterministic_level,
                                "semantic_level": p.semantic_level,
                            },
                            "created_at": _iso(p.created_at),
                        }
                    )

            if kind in {"all", "recovery"}:
                q = select(ExecutionRecoveryRecord).order_by(desc(ExecutionRecoveryRecord.created_at)).limit(fetch_cap)
                if since is not None:
                    q = q.where(ExecutionRecoveryRecord.created_at >= since)
                if tid_uuid is not None:
                    q = q.where(ExecutionRecoveryRecord.task_id == tid_uuid)
                for r in (await s.execute(q)).scalars().all():
                    entries.append(
                        {
                            "id": str(r.id),
                            "kind": "recovery",
                            "level": "warning",
                            "module": "runner.recovery",
                            "title": f"{r.trigger}: {r.previous_status} → {r.resolved_status} ({r.action_taken})",
                            "message": r.notes,
                            "cycle_id": None,
                            "task_id": str(r.task_id),
                            "payload": r.observed_identity,
                            "created_at": _iso(r.created_at),
                        }
                    )

            if kind in {"all", "cycles"} and tid_uuid is None:
                q = select(CycleRecord).order_by(desc(CycleRecord.started_at)).limit(fetch_cap)
                if since is not None:
                    q = q.where(CycleRecord.started_at >= since)
                if cid_uuid is not None:
                    q = q.where(CycleRecord.id == cid_uuid)
                for c in (await s.execute(q)).scalars().all():
                    lvl = "error" if c.outcome == "ERROR" else ("warning" if c.outcome in {"POLICY_DENIED", "ACTION_FAILED"} else "info")
                    entries.append(
                        {
                            "id": str(c.id),
                            "kind": "cycles",
                            "level": lvl,
                            "module": "loop.cycle",
                            "title": f"Cycle #{c.cycle_number} [{c.outcome or 'RUNNING'}]",
                            "message": c.error or (c.decision or {}).get("reason") or f"phase={c.phase}",
                            "cycle_id": str(c.id),
                            "task_id": None,
                            "payload": {"phase": c.phase, "lifecycle_state": c.lifecycle_state, "outcome": c.outcome},
                            "created_at": _iso(c.started_at),
                        }
                    )

        # In-memory filters for unified cross-table view
        if level:
            lvl_l = level.lower()
            entries = [e for e in entries if (e.get("level") or "").lower() == lvl_l]
        if module:
            mod_l = module.lower()
            entries = [e for e in entries if mod_l in (e.get("module") or "").lower()]
        if tid_uuid is not None:
            t_str = str(tid_uuid)
            entries = [e for e in entries if e.get("task_id") == t_str]
        if search:
            q_l = search.strip().lower()
            entries = [
                e
                for e in entries
                if q_l in (e.get("title") or "").lower()
                or q_l in (e.get("message") or "").lower()
                or q_l in (e.get("module") or "").lower()
            ]

        entries.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        total = len(entries)
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": entries[offset : offset + limit],
        }

    return router
