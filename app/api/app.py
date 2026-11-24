"""FastAPI application for AOC observability, world injection, approvals, and lifecycle control."""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from app.api.auth import (
    SCOPE_APPROVE,
    SCOPE_CONTROL,
    SCOPE_INTERNAL,
    SCOPE_READ,
    Principal,
    require_scope,
    validate_api_security_config,
)
from app.api.control_center import create_control_center_router
from app.core.config import PROJECT_ROOT, Settings, get_settings
from app.core.loop import CycleInProgressError
from app.core.runtime import OrganismRuntime
from app.notifications.telegram import TelegramAuthError
from app.observability.metrics import REGISTRY
from app.orchestration.orchestrator import (
    ApprovalConflictError,
    ApprovalDeniedError,
    ApprovalNotFoundError,
)
from app.storage.database import Database
from app.storage.outbox import OutboxEventType


class WorldInputRequest(BaseModel):
    kind: str = "knowledge_topic"
    key: str
    value: dict[str, Any] = {}
    status: str = "open"
    source: str = "operator"
    is_simulated: bool = False
    wake: bool = True


class ControlNote(BaseModel):
    reason: str = "operator request"


class EmbeddingRequest(BaseModel):
    input: str | list[str]
    model: str = "aoc-local-embed-512"


class MemorySearchRequest(BaseModel):
    query: str = ""
    memory_types: list[str] | None = None
    limit: int = 10


class MemorySyncReplayRequest(BaseModel):
    event_type: str | None = None
    limit: int = 50
    dispatch_now: bool = True


class WorldSourceUpsertRequest(BaseModel):
    source_id: str
    sensor_type: str  # rss | git | web | system
    name: str = ""
    config: dict[str, Any] = {}
    reliability_score: float = 0.7
    poll_interval_seconds: int = 900
    enabled: bool = True


class ToolInvokeRequest(BaseModel):
    arguments: dict[str, Any] = {}
    workspace: str | None = None
    task_id: str | None = None
    approved_l2: bool = False


class EvolutionProposalCreateRequest(BaseModel):
    proposal_key: str | None = None
    parent_instance_id: str | None = None
    target_generation: int = 1
    trigger_type: str = "OPERATOR_INITIATED"
    trigger_reason: str
    hypothesis: str
    genome_delta: dict[str, Any]
    auto_screen: bool = True


class EvolutionDecisionRequest(BaseModel):
    reason: str = ""


class EvolutionRollbackRequest(BaseModel):
    target_instance_id: str | None = None
    reason: str = "operator initiated rollback"


def create_app(
    settings: Settings | None = None,
    db: Database | None = None,
    runtime: OrganismRuntime | None = None,
    start_scheduler: bool | None = None,
) -> FastAPI:
    cfg = settings or get_settings()
    if start_scheduler is None:
        start_scheduler = os.environ.get("AOC_ENABLE_SCHEDULER", "true").lower() in {"1", "true", "yes"}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        rt = runtime or OrganismRuntime(settings=cfg, db=db)
        # Fail fast if bound to non-loopback with auth_enabled=False before initializing runtime
        if not rt.settings.api.auth_enabled:
            validate_api_security_config(rt.settings.api, has_db_credentials=False)
        app.state.runtime = rt
        app.state.settings = rt.settings
        app.state.db = rt.db
        app.state.repo = rt.repo
        await rt.start(start_scheduler=bool(start_scheduler))
        try:
            has_db_creds = await rt.repo.has_active_api_credentials()
            validate_api_security_config(rt.settings.api, has_db_credentials=has_db_creds)
        except Exception:
            await rt.stop()
            raise
        app.state.organism_id = rt.organism_id
        try:
            yield
        finally:
            await rt.stop()

    app = FastAPI(title="Artificial Organism Core (AOC)", version=cfg.organism.version, lifespan=lifespan)

    def _rt() -> OrganismRuntime:
        return app.state.runtime

    @app.get("/health")
    async def health() -> dict[str, Any]:
        rt = _rt()
        db_ok = await rt.db.ping()
        return {
            "status": "ok" if db_ok else "degraded",
            "organism": rt.settings.organism.name,
            "version": rt.settings.organism.version,
            "database": "ok" if db_ok else "unavailable",
            "letta": "ok" if rt.memory.backend_available else "degraded",
            "lifecycle": rt.lifecycle.state.value if rt.lifecycle else "UNKNOWN",
            "scheduler_running": bool(rt.scheduler and rt.scheduler.running),
        }

    @app.get("/state")
    async def get_state(_: Principal = Depends(require_scope(SCOPE_READ))) -> dict[str, Any]:
        rt = _rt()
        assert rt.organism_id is not None
        state = await rt.repo.get_organism_state(rt.organism_id)
        drives = await rt.repo.load_drive_states(rt.organism_id)
        budget = await rt.governance.check_budget()
        return {
            "organism_id": str(rt.organism_id),
            "name": rt.settings.organism.name,
            "version": rt.settings.organism.version,
            "state": state,
            "drives": drives,
            "budget": budget.model_dump(mode="json"),
        }

    @app.get("/drives")
    async def get_drives(_: Principal = Depends(require_scope(SCOPE_READ))) -> list[dict[str, Any]]:
        rt = _rt()
        assert rt.organism_id is not None
        return await rt.repo.load_drive_states(rt.organism_id)

    @app.get("/cycles")
    async def list_cycles(limit: int = 20, _: Principal = Depends(require_scope(SCOPE_READ))) -> list[dict[str, Any]]:
        return await _rt().repo.list_cycles(limit=min(max(limit, 1), 200))

    @app.get("/goals")
    async def list_goals(
        status: str | None = None, limit: int = 50, _: Principal = Depends(require_scope(SCOPE_READ))
    ) -> list[dict[str, Any]]:
        statuses = [status] if status else None
        return await _rt().repo.list_goals(statuses=statuses, limit=min(max(limit, 1), 200))

    @app.get("/tasks")
    async def list_tasks(
        status: str | None = None, limit: int = 50, _: Principal = Depends(require_scope(SCOPE_READ))
    ) -> list[dict[str, Any]]:
        statuses = [status] if status else None
        return await _rt().repo.list_tasks(statuses=statuses, limit=min(max(limit, 1), 200))

    @app.get("/v2/tasks/{task_id}/lifecycle")
    async def get_task_lifecycle(
        task_id: str, _: Principal = Depends(require_scope(SCOPE_READ))
    ) -> list[dict[str, Any]]:
        try:
            t_uuid = uuid.UUID(task_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid task_id UUID") from exc
        if await _rt().repo.get_task(t_uuid) is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return await _rt().repo.list_task_lifecycle_events(t_uuid)

    @app.get("/v2/tasks/{task_id}/attempts")
    async def get_task_attempts(
        task_id: str, _: Principal = Depends(require_scope(SCOPE_READ))
    ) -> list[dict[str, Any]]:
        try:
            t_uuid = uuid.UUID(task_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid task_id UUID") from exc
        if await _rt().repo.get_task(t_uuid) is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return await _rt().repo.list_executions(task_id=t_uuid, limit=50)

    @app.post("/v2/tasks/{task_id}/cancel")
    async def cancel_task_v2(
        task_id: str,
        body: ControlNote = ControlNote(reason="operator cancelled"),
        principal: Principal = Depends(require_scope(SCOPE_CONTROL)),
    ) -> dict[str, Any]:
        rt = _rt()
        try:
            t_uuid = uuid.UUID(task_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid task_id UUID") from exc
        if await rt.repo.get_task(t_uuid) is None:
            raise HTTPException(status_code=404, detail="Task not found")
        cancelled = await rt.orchestrator.cancel_task(task_id, actor=principal.name, reason=body.reason)
        if not cancelled:
            raise HTTPException(status_code=409, detail="Task cannot be cancelled in its current state")
        if rt.outbox is not None:
            await rt.outbox.dispatch_once()
        return {"task_id": task_id, "status": "CANCELLED"}

    @app.post("/v2/tasks/{task_id}/reconcile")
    async def reconcile_task_v2(
        task_id: str, _: Principal = Depends(require_scope(SCOPE_CONTROL))
    ) -> dict[str, Any]:
        rt = _rt()
        try:
            t_uuid = uuid.UUID(task_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid task_id UUID") from exc
        if await rt.repo.get_task(t_uuid) is None:
            raise HTTPException(status_code=404, detail="Task not found")
        res = await rt.orchestrator.reconcile_task(task_id, trigger="manual_reconcile")
        if rt.outbox is not None:
            await rt.outbox.dispatch_once()
        return res

    @app.get("/v2/runners")
    async def list_runners_v2(_: Principal = Depends(require_scope(SCOPE_READ))) -> list[dict[str, Any]]:
        rt = _rt()
        await rt.orchestrator.sync_runner_heartbeat()
        return await rt.repo.list_runners()

    @app.get("/v2/runners/{runner_id}/executions")
    async def list_runner_executions_v2(
        runner_id: str, limit: int = 50, _: Principal = Depends(require_scope(SCOPE_READ))
    ) -> list[dict[str, Any]]:
        rt = _rt()
        from app.runner.client import RunnerUnavailableError, UnixSocketRunnerBackend

        if isinstance(rt.execution_backend, UnixSocketRunnerBackend) and rt.execution_backend.runner_id == runner_id:
            try:
                recs = await rt.execution_backend.client.list_executions(limit=min(max(limit, 1), 200))
                return [r.model_dump(mode="json") for r in recs]
            except RunnerUnavailableError:
                pass
        rows = await rt.repo.list_executions(limit=min(max(limit, 1), 200))
        return [r for r in rows if r.get("runner_id") == runner_id]

    @app.get("/agents")
    async def list_agents(_: Principal = Depends(require_scope(SCOPE_READ))) -> list[dict[str, Any]]:
        return await _rt().repo.list_agents()

    @app.get("/v2/tools")
    async def list_tools_v2(_: Principal = Depends(require_scope(SCOPE_READ))) -> dict[str, Any]:
        rt = _rt()
        return {
            "provider_kind": "tool_provider_mcp",
            "note": "MCP ToolProvider establishes a second controlled execution path (atomic tools), not a second autonomous AgentProvider.",
            "tools": [t.model_dump(mode="json") for t in rt.mcp_manager.list_tools()],
        }

    @app.post("/v2/tools/{tool_name}/invoke")
    async def invoke_tool_v2(
        tool_name: str,
        req: ToolInvokeRequest = ToolInvokeRequest(),
        principal: Principal = Depends(require_scope(SCOPE_CONTROL)),
    ) -> dict[str, Any]:
        from app.tools.interfaces import ToolCallRequest

        rt = _rt()
        res = await rt.mcp_manager.invoke(
            ToolCallRequest(
                tool_name=tool_name,
                arguments=req.arguments,
                workspace=req.workspace,
                task_id=req.task_id,
                requested_by=principal.name,
                approved_l2=req.approved_l2,
            )
        )
        if res.denied:
            raise HTTPException(status_code=403, detail=res.error or "Tool invocation denied")
        if res.requires_approval:
            raise HTTPException(status_code=202, detail=res.error or "Tool invocation requires L2 approval")
        return res.model_dump(mode="json")

    @app.get("/memories")
    async def list_memories(
        memory_type: str | None = None,
        query: str = "",
        limit: int = 20,
        _: Principal = Depends(require_scope(SCOPE_READ)),
    ) -> list[dict[str, Any]]:
        types = [memory_type] if memory_type else None
        items = await _rt().memory.recall(query=query, memory_types=types, limit=min(max(limit, 1), 100))
        return [it.model_dump(mode="json") for it in items]

    @app.post("/v2/memories/search")
    async def search_memories_v2(
        req: MemorySearchRequest, _: Principal = Depends(require_scope(SCOPE_READ))
    ) -> list[dict[str, Any]]:
        scored = await _rt().memory.search_hybrid(
            query=req.query,
            memory_types=req.memory_types,
            limit=min(max(req.limit, 1), 100),
        )
        return [s.model_dump(mode="json") for s in scored]

    @app.get("/v2/memories/embeddings/status")
    async def memory_embeddings_status_v2(
        _: Principal = Depends(require_scope(SCOPE_READ)),
    ) -> dict[str, Any]:
        rt = _rt()
        stats = await rt.repo.get_memory_embedding_stats()
        stats["active_provider"] = rt.embedding_service.spec.model_dump(mode="json")
        stats["embedding_healthy"] = rt.embedding_service.healthy
        return stats

    @app.get("/v2/memories/sync/status")
    async def memory_sync_status_v2(
        _: Principal = Depends(require_scope(SCOPE_READ)),
    ) -> dict[str, Any]:
        rt = _rt()
        stats = await rt.repo.get_memory_sync_stats()
        stats["letta_available"] = rt.memory.backend_available
        return stats

    @app.post("/v2/memories/sync/replay")
    async def memory_sync_replay_v2(
        req: MemorySyncReplayRequest = MemorySyncReplayRequest(),
        _: Principal = Depends(require_scope(SCOPE_CONTROL)),
    ) -> dict[str, Any]:
        rt = _rt()
        requeued = await rt.repo.replay_memory_sync_outbox(
            event_type=req.event_type, limit=min(max(req.limit, 1), 200)
        )
        dispatched = 0
        if req.dispatch_now and rt.outbox is not None:
            dispatched = await rt.outbox.dispatch_once(batch_size=min(max(req.limit, 1), 50))
        return {"requeued": requeued, "dispatched": dispatched}

    @app.get("/events")
    async def list_events(
        limit: int = 50, event_type: str | None = None, _: Principal = Depends(require_scope(SCOPE_READ))
    ) -> list[dict[str, Any]]:
        return await _rt().repo.list_events(limit=min(max(limit, 1), 500), event_type=event_type)

    @app.get("/decisions")
    async def list_decisions(limit: int = 50, _: Principal = Depends(require_scope(SCOPE_READ))) -> list[dict[str, Any]]:
        return await _rt().repo.list_decisions(limit=min(max(limit, 1), 200))

    @app.get("/world")
    async def list_world(
        kind: str | None = None, status: str | None = None, _: Principal = Depends(require_scope(SCOPE_READ))
    ) -> list[dict[str, Any]]:
        return await _rt().repo.list_world_items(kind=kind, status=status)

    @app.post("/world")
    async def upsert_world(
        req: WorldInputRequest, _: Principal = Depends(require_scope(SCOPE_CONTROL))
    ) -> dict[str, Any]:
        rt = _rt()
        item = await rt.repo.upsert_world_item(
            kind=req.kind,
            key=req.key,
            value=req.value,
            status=req.status,
            source=req.source,
            is_simulated=req.is_simulated,
        )
        await rt.repo.log_event("world.updated", message=f"{req.kind}:{req.key}", payload=item)
        if req.wake and rt.lifecycle is not None:
            rt.lifecycle.wake(f"world update {req.kind}:{req.key}")
        return item

    @app.delete("/world/{kind}/{key}")
    async def delete_world(
        kind: str, key: str, _: Principal = Depends(require_scope(SCOPE_CONTROL))
    ) -> dict[str, Any]:
        deleted = await _rt().repo.delete_world_item(kind=kind, key=key)
        if not deleted:
            raise HTTPException(status_code=404, detail="world item not found")
        return {"deleted": True, "kind": kind, "key": key}

    # ── Digital World Perception Sensors (V2.0 P1-5) ────────────────────────
    @app.get("/v2/world/events")
    async def list_world_events_v2(
        source_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        _: Principal = Depends(require_scope(SCOPE_READ)),
    ) -> list[dict[str, Any]]:
        return await _rt().repo.list_world_events(
            source_id=source_id, status=status, limit=min(max(limit, 1), 200)
        )

    @app.get("/v2/world/sources")
    async def list_world_sources_v2(
        enabled_only: bool = False, _: Principal = Depends(require_scope(SCOPE_READ))
    ) -> list[dict[str, Any]]:
        return await _rt().repo.list_world_sources(enabled_only=enabled_only)

    @app.post("/v2/world/sources")
    async def upsert_world_source_v2(
        req: WorldSourceUpsertRequest, _: Principal = Depends(require_scope(SCOPE_CONTROL))
    ) -> dict[str, Any]:
        rt = _rt()
        if req.sensor_type not in {"rss", "git", "web", "system"}:
            raise HTTPException(status_code=400, detail=f"Unsupported sensor_type '{req.sensor_type}'")
        if req.sensor_type in {"rss", "web"} and rt.world_pipeline is not None:
            from app.perception.world.security import SSRFBlockedError, is_domain_allowed
            from urllib.parse import urlparse

            raw_url = str(req.config.get("url") or "").strip()
            parsed = urlparse(raw_url)
            if not raw_url or not is_domain_allowed(parsed.hostname or "", rt.world_pipeline.allowed_domains):
                raise HTTPException(
                    status_code=403,
                    detail=f"Source URL domain '{parsed.hostname}' is not in sensors allowed_domains allowlist",
                )
        return await rt.repo.upsert_world_source(
            source_id=req.source_id,
            sensor_type=req.sensor_type,
            name=req.name,
            config=req.config,
            reliability_score=req.reliability_score,
            poll_interval_seconds=req.poll_interval_seconds,
            enabled=req.enabled,
        )

    @app.post("/v2/world/sources/{source_id}/enable")
    async def enable_world_source_v2(
        source_id: str, _: Principal = Depends(require_scope(SCOPE_CONTROL))
    ) -> dict[str, Any]:
        updated = await _rt().repo.set_world_source_enabled(source_id, True)
        if updated is None:
            raise HTTPException(status_code=404, detail="World source not found")
        return updated

    @app.post("/v2/world/sources/{source_id}/disable")
    async def disable_world_source_v2(
        source_id: str, _: Principal = Depends(require_scope(SCOPE_CONTROL))
    ) -> dict[str, Any]:
        updated = await _rt().repo.set_world_source_enabled(source_id, False)
        if updated is None:
            raise HTTPException(status_code=404, detail="World source not found")
        return updated

    @app.post("/v2/world/poll")
    async def poll_world_sources_v2(
        source_id: str | None = None,
        force: bool = True,
        _: Principal = Depends(require_scope(SCOPE_CONTROL)),
    ) -> dict[str, Any]:
        rt = _rt()
        if rt.world_pipeline is None:
            raise HTTPException(status_code=503, detail="WorldEventPipeline not initialized")
        if source_id:
            return {"results": [await rt.world_pipeline.poll_source(source_id, force=force)]}
        return {"results": await rt.world_pipeline.poll_all(force=force)}

    # ── Lifecycle control ───────────────────────────────────────────────────
    @app.post("/control/pause")
    async def pause(
        body: ControlNote = ControlNote(), _: Principal = Depends(require_scope(SCOPE_CONTROL))
    ) -> dict[str, Any]:
        rt = _rt()
        assert rt.lifecycle is not None
        st = await rt.lifecycle.pause(body.reason)
        await rt.repo.enqueue_outbox_event(
            event_type=OutboxEventType.NOTIFICATION_DISPATCH,
            idempotency_key=f"notify:paused:{uuid.uuid4()}",
            payload={"kind": "lifecycle_paused", "reason": body.reason},
        )
        return {"lifecycle_state": st.value}

    @app.post("/control/resume")
    async def resume(
        body: ControlNote = ControlNote(), _: Principal = Depends(require_scope(SCOPE_CONTROL))
    ) -> dict[str, Any]:
        rt = _rt()
        assert rt.lifecycle is not None
        st = await rt.lifecycle.resume(body.reason)
        return {"lifecycle_state": st.value}

    @app.post("/control/wake")
    async def wake(
        body: ControlNote = ControlNote(), _: Principal = Depends(require_scope(SCOPE_CONTROL))
    ) -> dict[str, Any]:
        rt = _rt()
        assert rt.lifecycle is not None
        rt.lifecycle.wake(body.reason)
        return {"woken": True, "lifecycle_state": rt.lifecycle.state.value}

    @app.post("/control/cycle")
    async def trigger_cycle(_: Principal = Depends(require_scope(SCOPE_CONTROL))) -> dict[str, Any]:
        rt = _rt()
        assert rt.loop is not None
        try:
            return await rt.loop.run_cycle(fail_if_busy=True)
        except CycleInProgressError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    # ── Human-in-the-loop L2 approvals (via public approve_and_dispatch) ────
    @app.post("/approvals/{task_id}/approve")
    async def approve_task(
        task_id: str,
        body: ControlNote = ControlNote(reason="operator approved"),
        principal: Principal = Depends(require_scope(SCOPE_APPROVE)),
    ) -> dict[str, Any]:
        rt = _rt()
        try:
            t_uuid = uuid.UUID(task_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid task_id UUID") from exc
        try:
            return await rt.orchestrator.approve_and_dispatch(
                task_id=t_uuid,
                approver=principal.name,
                reason=body.reason,
                channel="api",
            )
        except ApprovalNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ApprovalConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ApprovalDeniedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.post("/approvals/{task_id}/reject")
    async def reject_task(
        task_id: str,
        body: ControlNote = ControlNote(reason="operator rejected"),
        principal: Principal = Depends(require_scope(SCOPE_APPROVE)),
    ) -> dict[str, Any]:
        rt = _rt()
        try:
            t_uuid = uuid.UUID(task_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid task_id UUID") from exc
        try:
            return await rt.orchestrator.reject_approval(
                task_id=t_uuid,
                approver=principal.name,
                reason=body.reason,
                channel="api",
            )
        except ApprovalNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ApprovalConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    # ── V2 Governance, Bound Approvals & Telegram Webhook (P1-8) ────────────
    @app.get("/v2/governance/policies")
    async def get_governance_policies_v2(_: Principal = Depends(require_scope(SCOPE_READ))) -> dict[str, Any]:
        rt = _rt()
        return {
            "auto_approve_max_level": rt.settings.governance.auto_approve_max_level,
            "semantic_classifier_enabled": rt.semantic_classifier is not None,
            "policy": getattr(rt.governance, "policy", {}),
        }

    @app.get("/v2/governance/audit")
    async def get_governance_audit_v2(
        subject_type: str | None = None,
        limit: int = 50,
        _: Principal = Depends(require_scope(SCOPE_READ)),
    ) -> list[dict[str, Any]]:
        return await _rt().repo.list_policy_evaluations(
            subject_type=subject_type, limit=min(max(limit, 1), 200)
        )

    @app.get("/v2/approvals")
    async def list_approvals_v2(
        status: str | None = None,
        limit: int = 50,
        _: Principal = Depends(require_scope(SCOPE_READ)),
    ) -> list[dict[str, Any]]:
        return await _rt().approval_service.list_approvals(status=status, limit=min(max(limit, 1), 200))

    @app.post("/v2/approvals/{approval_id}/approve")
    async def approve_by_id_v2(
        approval_id: str,
        body: ControlNote = ControlNote(reason="operator approved"),
        principal: Principal = Depends(require_scope(SCOPE_APPROVE)),
    ) -> dict[str, Any]:
        rt = _rt()
        try:
            a_uuid = uuid.UUID(approval_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid approval_id UUID") from exc
        try:
            return await rt.approval_service.approve_by_id(
                a_uuid,
                approver=principal.name,
                reason=body.reason,
                channel="api",
            )
        except ApprovalNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ApprovalConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ApprovalDeniedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.post("/v2/approvals/{approval_id}/reject")
    async def reject_by_id_v2(
        approval_id: str,
        body: ControlNote = ControlNote(reason="operator rejected"),
        principal: Principal = Depends(require_scope(SCOPE_APPROVE)),
    ) -> dict[str, Any]:
        rt = _rt()
        try:
            a_uuid = uuid.UUID(approval_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid approval_id UUID") from exc
        try:
            return await rt.approval_service.reject_by_id(
                a_uuid,
                approver=principal.name,
                reason=body.reason,
                channel="api",
            )
        except ApprovalNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ApprovalConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ApprovalDeniedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.post("/v2/webhooks/telegram")
    async def telegram_webhook_v2(
        request: Request,
        x_telegram_bot_api_secret_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        rt = _rt()
        try:
            update_payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
        if not isinstance(update_payload, dict):
            raise HTTPException(status_code=400, detail="Telegram update must be a JSON object")
        try:
            return await rt.telegram_webhook.handle_update(
                update_payload,
                secret_header=x_telegram_bot_api_secret_token,
            )
        except TelegramAuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ApprovalNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ApprovalConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ApprovalDeniedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/v2/lifecycle")
    async def get_lifecycle_v2(_: Principal = Depends(require_scope(SCOPE_READ))) -> dict[str, Any]:
        rt = _rt()
        state = await rt.repo.get_organism_state(rt.organism_id) if rt.organism_id else None
        affect_summary = (
            rt.affect.summary()
            if getattr(rt, "affect", None) is not None
            else {"affect_enabled": False, "maximum_influence": 0.15, "state": None}
        )
        return {
            "organism_id": str(rt.organism_id) if rt.organism_id else None,
            "lifecycle_state": (state or {}).get("lifecycle_state"),
            "previous_state": (state or {}).get("previous_state"),
            "state_reason": (state or {}).get("state_reason"),
            "cycle_count": (state or {}).get("cycle_count", 0),
            "pause_requested": (state or {}).get("pause_requested", False),
            "quota_reset_at": (state or {}).get("quota_reset_at"),
            "throttled_reason": (state or {}).get("throttled_reason"),
            "next_wake_at": (state or {}).get("next_wake_at"),
            "affect_enabled": affect_summary["affect_enabled"],
            "affect": affect_summary,
        }

    @app.get("/v2/self-model")
    async def get_self_model_v2(_: Principal = Depends(require_scope(SCOPE_READ))) -> dict[str, Any]:
        rt = _rt()
        if rt.self_model is None:
            raise HTTPException(status_code=503, detail="SelfModelManager not initialized")
        return await rt.self_model.get_latest_snapshot()

    @app.get("/v2/self-model/history")
    async def get_self_model_history_v2(
        limit: int = 20, _: Principal = Depends(require_scope(SCOPE_READ))
    ) -> list[dict[str, Any]]:
        rt = _rt()
        if rt.self_model is None:
            raise HTTPException(status_code=503, detail="SelfModelManager not initialized")
        return await rt.self_model.list_history(limit=min(max(limit, 1), 100))

    @app.get("/v2/capabilities")
    async def list_capabilities_v2(_: Principal = Depends(require_scope(SCOPE_READ))) -> list[dict[str, Any]]:
        rt = _rt()
        if rt.self_model is None:
            raise HTTPException(status_code=503, detail="SelfModelManager not initialized")
        return await rt.self_model.list_capabilities()

    @app.get("/v2/skills")
    async def list_skills_v2(
        status: str | None = None,
        capability_key: str | None = None,
        limit: int = 100,
        _: Principal = Depends(require_scope(SCOPE_READ)),
    ) -> list[dict[str, Any]]:
        rt = _rt()
        if rt.skill_registry is None:
            raise HTTPException(status_code=503, detail="SkillRegistryManager not initialized")
        return await rt.skill_registry.list_skills_with_integrity(
            status=status, capability_key=capability_key, limit=min(max(limit, 1), 200)
        )

    @app.get("/v2/skills/{skill_id}")
    async def get_skill_v2(
        skill_id: str, _: Principal = Depends(require_scope(SCOPE_READ))
    ) -> dict[str, Any]:
        rt = _rt()
        if rt.skill_registry is None:
            raise HTTPException(status_code=503, detail="SkillRegistryManager not initialized")
        try:
            sid = uuid.UUID(skill_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid skill_id UUID") from exc
        detail = await rt.skill_registry.get_skill_detail(sid)
        if detail is None:
            raise HTTPException(status_code=404, detail="Skill not found")
        return detail

    @app.post("/v1/embeddings")
    async def local_embeddings(
        req: EmbeddingRequest,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        from app.memory.local_embeddings import embed_batch

        # Enforce Bearer auth: accept SCOPE_INTERNAL token OR configured LLM API key (sent by Dockerized aoc-letta)
        if cfg.api.auth_enabled:
            llm_key = cfg.llm.api_key.get_secret_value() if cfg.llm.api_key else ""
            raw_tok = (authorization or "").removeprefix("Bearer ").removeprefix("bearer ").strip()
            if not (llm_key and raw_tok and raw_tok == llm_key):
                checker = require_scope(SCOPE_INTERNAL)
                await checker(request)

        inputs = [req.input] if isinstance(req.input, str) else list(req.input)
        rt = _rt()
        model_used = req.model
        target_dim = int(cfg.memory.embedding_dim or 512)
        vecs: list[list[float]] | None = None
        if rt.embedding_service is not None:
            try:
                res = await rt.embedding_service.provider.embed_texts(inputs, is_query=False)
                vecs = res.vectors
                model_used = res.model_key
            except Exception:
                vecs = None
        if vecs is None:
            vecs = embed_batch(inputs, dim=target_dim)
        return {
            "object": "list",
            "model": model_used,
            "data": [
                {"object": "embedding", "index": i, "embedding": v}
                for i, v in enumerate(vecs)
            ],
            "usage": {"prompt_tokens": sum(len(s.split()) for s in inputs), "total_tokens": sum(len(s.split()) for s in inputs)},
        }

    @app.get("/metrics")
    async def metrics(_: Principal = Depends(require_scope(SCOPE_INTERNAL))) -> Response:
        return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    # ── V5.0 Evolutionary Reproduction Control Plane (M2) ────────────────────
    @app.get("/v5/evolution/entropy")
    async def get_evolution_entropy_v5(_: Principal = Depends(require_scope(SCOPE_READ))) -> dict[str, Any]:
        snap = await _rt().entropy_sensor.measure()
        return snap.model_dump(mode="json")

    @app.get("/v5/evolution/constitution")
    async def get_evolution_constitution_v5(_: Principal = Depends(require_scope(SCOPE_READ))) -> dict[str, Any]:
        return _rt().constitution_guard.constitutional_manifest()

    @app.get("/v5/evolution/proposals")
    async def list_evolution_proposals_v5(
        stage: str | None = None,
        limit: int = 50,
        _: Principal = Depends(require_scope(SCOPE_READ)),
    ) -> list[dict[str, Any]]:
        return await _rt().evolution_repo.list_proposals(stage=stage, limit=min(max(limit, 1), 200))

    @app.post("/v5/evolution/proposals")
    async def create_evolution_proposal_v5(
        req: EvolutionProposalCreateRequest,
        _: Principal = Depends(require_scope(SCOPE_CONTROL)),
    ) -> dict[str, Any]:
        rt = _rt()
        parent_id: str | uuid.UUID | None = req.parent_instance_id
        if not parent_id:
            prim = await rt.evolution_repo.ensure_primary_instance(
                db_schema=rt.settings.database.schema_name,
                workspace_root=rt.settings.governance.workspace_root,
                letta_agent_name=rt.settings.memory.agent_name,
            )
            parent_id = prim["id"]
        try:
            prop = await rt.proposal_manager.create_proposal(
                parent_instance_id=parent_id,
                target_generation=req.target_generation,
                trigger_type=req.trigger_type,
                trigger_reason=req.trigger_reason,
                hypothesis=req.hypothesis,
                genome_delta=req.genome_delta,
                proposal_key=req.proposal_key,
            )
            if req.auto_screen:
                prop = await rt.proposal_manager.screen_proposal(prop["id"])
            return prop
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v5/evolution/proposals/{proposal_id}/screen")
    async def screen_evolution_proposal_v5(
        proposal_id: str,
        _: Principal = Depends(require_scope(SCOPE_CONTROL)),
    ) -> dict[str, Any]:
        try:
            return await _rt().proposal_manager.screen_proposal(proposal_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v5/evolution/proposals/{proposal_id}/approve")
    async def approve_evolution_proposal_v5(
        proposal_id: str,
        req: EvolutionDecisionRequest = EvolutionDecisionRequest(),
        principal: Principal = Depends(require_scope(SCOPE_APPROVE)),
    ) -> dict[str, Any]:
        try:
            return await _rt().proposal_manager.approve_proposal(
                proposal_id,
                approver=principal.name,
                note=req.reason,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v5/evolution/proposals/{proposal_id}/reject")
    async def reject_evolution_proposal_v5(
        proposal_id: str,
        req: EvolutionDecisionRequest = EvolutionDecisionRequest(),
        principal: Principal = Depends(require_scope(SCOPE_APPROVE)),
    ) -> dict[str, Any]:
        try:
            return await _rt().proposal_manager.reject_proposal(
                proposal_id,
                approver=principal.name,
                note=req.reason,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v5/evolution/proposals/{proposal_id}/arena")
    async def run_evolution_arena_v5(
        proposal_id: str,
        _: Principal = Depends(require_scope(SCOPE_CONTROL)),
    ) -> dict[str, Any]:
        try:
            rep = await _rt().fitness_arena.run_arena(proposal_id)
            return rep.model_dump(mode="json")
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v5/evolution/proposals/{proposal_id}/promote")
    async def promote_evolution_proposal_v5(
        proposal_id: str,
        req: EvolutionDecisionRequest = EvolutionDecisionRequest(),
        principal: Principal = Depends(require_scope(SCOPE_APPROVE)),
    ) -> dict[str, Any]:
        try:
            res = await _rt().succession_manager.promote_candidate(
                proposal_id,
                promoter=principal.name,
                note=req.reason,
            )
            return res.model_dump(mode="json")
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v5/evolution/rollback")
    async def rollback_evolution_v5(
        req: EvolutionRollbackRequest = EvolutionRollbackRequest(),
        principal: Principal = Depends(require_scope(SCOPE_APPROVE)),
    ) -> dict[str, Any]:
        try:
            res = await _rt().succession_manager.rollback_generation(
                operator=principal.name,
                reason=req.reason,
                target_instance_id_or_key=req.target_instance_id,
            )
            return res.model_dump(mode="json")
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/v5/evolution/lineage")
    async def list_evolution_lineage_v5(
        outcome: str | None = None,
        limit: int = 50,
        _: Principal = Depends(require_scope(SCOPE_READ)),
    ) -> list[dict[str, Any]]:
        return await _rt().evolution_repo.list_lineage(outcome=outcome, limit=min(max(limit, 1), 200))

    @app.get("/v5/evolution/instances")
    async def list_evolution_instances_v5(
        status: str | None = None,
        role: str | None = None,
        limit: int = 50,
        _: Principal = Depends(require_scope(SCOPE_READ)),
    ) -> list[dict[str, Any]]:
        return await _rt().evolution_repo.list_instances(
            status=status, role=role, limit=min(max(limit, 1), 200)
        )

    # V2.1 AOC Control Center read-only API router (enforces Bearer Token + SCOPE_READ)
    app.include_router(create_control_center_router(_rt))

    # Optional isolated static serving for built AOC Control Center SPA (`frontend/dist`).
    # Strictly wrapped so missing/broken frontend assets NEVER affect API or AutonomousLoop.
    dist_dir = Path(os.environ.get("AOC_FRONTEND_DIST", str(PROJECT_ROOT / "frontend" / "dist")))
    if dist_dir.is_dir() and (dist_dir / "index.html").is_file():
        assets_dir = dist_dir / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="control-center-assets")

        @app.get("/", include_in_schema=False)
        async def serve_control_center_root() -> Response:
            try:
                return FileResponse(str(dist_dir / "index.html"))
            except Exception as exc:
                raise HTTPException(status_code=503, detail=f"Control Center UI unavailable: {exc}") from exc

        @app.get("/ui/{full_path:path}", include_in_schema=False)
        async def serve_control_center_spa(full_path: str) -> Response:
            try:
                candidate = (dist_dir / full_path).resolve()
                if dist_dir.resolve() in candidate.parents and candidate.is_file():
                    return FileResponse(str(candidate))
                return FileResponse(str(dist_dir / "index.html"))
            except Exception as exc:
                raise HTTPException(status_code=503, detail=f"Control Center UI unavailable: {exc}") from exc

    return app
