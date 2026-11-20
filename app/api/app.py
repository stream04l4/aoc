"""FastAPI application for AOC observability, world injection, approvals, and lifecycle control."""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Response
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
from app.core.config import Settings, get_settings
from app.core.loop import CycleInProgressError
from app.core.runtime import OrganismRuntime
from app.observability.metrics import REGISTRY
from app.orchestration.orchestrator import (
    ApprovalConflictError,
    ApprovalDeniedError,
    ApprovalNotFoundError,
)
from app.storage.database import Database


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
    model: str = "aoc-local-embed-1024"


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

    @app.get("/agents")
    async def list_agents(_: Principal = Depends(require_scope(SCOPE_READ))) -> list[dict[str, Any]]:
        return await _rt().repo.list_agents()

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

    # ── Lifecycle control ───────────────────────────────────────────────────
    @app.post("/control/pause")
    async def pause(
        body: ControlNote = ControlNote(), _: Principal = Depends(require_scope(SCOPE_CONTROL))
    ) -> dict[str, Any]:
        rt = _rt()
        assert rt.lifecycle is not None
        st = await rt.lifecycle.pause(body.reason)
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

    @app.post("/v1/embeddings")
    async def local_embeddings(
        req: EmbeddingRequest, _: Principal = Depends(require_scope(SCOPE_INTERNAL))
    ) -> dict[str, Any]:
        from app.memory.local_embeddings import embed_batch

        inputs = [req.input] if isinstance(req.input, str) else list(req.input)
        vecs = embed_batch(inputs, dim=cfg.memory.embedding_dim)
        return {
            "object": "list",
            "model": req.model,
            "data": [
                {"object": "embedding", "index": i, "embedding": v}
                for i, v in enumerate(vecs)
            ],
            "usage": {"prompt_tokens": sum(len(s.split()) for s in inputs), "total_tokens": sum(len(s.split()) for s in inputs)},
        }

    @app.get("/metrics")
    async def metrics(_: Principal = Depends(require_scope(SCOPE_INTERNAL))) -> Response:
        return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    return app
