"""FastAPI application for AOC observability, world injection, approvals, and lifecycle control."""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from app.core.config import Settings, get_settings
from app.core.enums import TaskStatus
from app.core.runtime import OrganismRuntime
from app.observability.metrics import REGISTRY
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
        app.state.runtime = rt
        app.state.settings = rt.settings
        app.state.db = rt.db
        app.state.repo = rt.repo
        await rt.start(start_scheduler=bool(start_scheduler))
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
    async def get_state() -> dict[str, Any]:
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
    async def get_drives() -> list[dict[str, Any]]:
        rt = _rt()
        assert rt.organism_id is not None
        return await rt.repo.load_drive_states(rt.organism_id)

    @app.get("/cycles")
    async def list_cycles(limit: int = 20) -> list[dict[str, Any]]:
        return await _rt().repo.list_cycles(limit=min(max(limit, 1), 200))

    @app.get("/goals")
    async def list_goals(status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        statuses = [status] if status else None
        return await _rt().repo.list_goals(statuses=statuses, limit=min(max(limit, 1), 200))

    @app.get("/tasks")
    async def list_tasks(status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        statuses = [status] if status else None
        return await _rt().repo.list_tasks(statuses=statuses, limit=min(max(limit, 1), 200))

    @app.get("/agents")
    async def list_agents() -> list[dict[str, Any]]:
        return await _rt().repo.list_agents()

    @app.get("/memories")
    async def list_memories(memory_type: str | None = None, query: str = "", limit: int = 20) -> list[dict[str, Any]]:
        types = [memory_type] if memory_type else None
        items = await _rt().memory.recall(query=query, memory_types=types, limit=min(max(limit, 1), 100))
        return [it.model_dump(mode="json") for it in items]

    @app.get("/events")
    async def list_events(limit: int = 50, event_type: str | None = None) -> list[dict[str, Any]]:
        return await _rt().repo.list_events(limit=min(max(limit, 1), 500), event_type=event_type)

    @app.get("/decisions")
    async def list_decisions(limit: int = 50) -> list[dict[str, Any]]:
        return await _rt().repo.list_decisions(limit=min(max(limit, 1), 200))

    @app.get("/world")
    async def list_world(kind: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        return await _rt().repo.list_world_items(kind=kind, status=status)

    @app.post("/world")
    async def upsert_world(req: WorldInputRequest) -> dict[str, Any]:
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
    async def delete_world(kind: str, key: str) -> dict[str, Any]:
        deleted = await _rt().repo.delete_world_item(kind=kind, key=key)
        if not deleted:
            raise HTTPException(status_code=404, detail="world item not found")
        return {"deleted": True, "kind": kind, "key": key}

    # ── Lifecycle control ───────────────────────────────────────────────────
    @app.post("/control/pause")
    async def pause(body: ControlNote = ControlNote()) -> dict[str, Any]:
        rt = _rt()
        assert rt.lifecycle is not None
        st = await rt.lifecycle.pause(body.reason)
        return {"lifecycle_state": st.value}

    @app.post("/control/resume")
    async def resume(body: ControlNote = ControlNote()) -> dict[str, Any]:
        rt = _rt()
        assert rt.lifecycle is not None
        st = await rt.lifecycle.resume(body.reason)
        return {"lifecycle_state": st.value}

    @app.post("/control/wake")
    async def wake(body: ControlNote = ControlNote()) -> dict[str, Any]:
        rt = _rt()
        assert rt.lifecycle is not None
        rt.lifecycle.wake(body.reason)
        return {"woken": True, "lifecycle_state": rt.lifecycle.state.value}

    @app.post("/control/cycle")
    async def trigger_cycle() -> dict[str, Any]:
        rt = _rt()
        assert rt.loop is not None
        return await rt.loop.run_cycle()

    # ── Human-in-the-loop L2 approvals ──────────────────────────────────────
    @app.post("/approvals/{task_id}/approve")
    async def approve_task(task_id: str) -> dict[str, Any]:
        rt = _rt()
        t_uuid = uuid.UUID(task_id)
        task = await rt.repo.get_task(t_uuid)
        if task is None or task["status"] != TaskStatus.REQUIRES_APPROVAL.value:
            raise HTTPException(status_code=404, detail="Pending approval task not found")
        await rt.repo.log_decision("human_approval", selected="APPROVED", alternatives=["REJECTED"], reason=f"Approved task {task_id}")
        exec_id = await rt.orchestrator._launch_attempt(
            t_uuid, task["assigned_agent"], sandbox_mode="workspace-write"
        )
        return {"task_id": task_id, "execution_id": exec_id, "status": TaskStatus.RUNNING.value}

    @app.post("/approvals/{task_id}/reject")
    async def reject_task(task_id: str, body: ControlNote = ControlNote()) -> dict[str, Any]:
        rt = _rt()
        t_uuid = uuid.UUID(task_id)
        task = await rt.repo.get_task(t_uuid)
        if task is None or task["status"] != TaskStatus.REQUIRES_APPROVAL.value:
            raise HTTPException(status_code=404, detail="Pending approval task not found")
        await rt.repo.update_task(t_uuid, status=TaskStatus.REJECTED.value, outcome={"rejected_reason": body.reason})
        await rt.repo.log_decision("human_approval", selected="REJECTED", alternatives=["APPROVED"], reason=body.reason)
        return {"task_id": task_id, "status": TaskStatus.REJECTED.value}

    @app.post("/v1/embeddings")
    async def local_embeddings(req: EmbeddingRequest) -> dict[str, Any]:
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
    async def metrics() -> Response:
        return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    return app
