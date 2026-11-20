"""FastAPI application for AOC observability and control."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from app.core.config import Settings, get_settings
from app.core.enums import LifecycleState
from app.observability.logging import configure_logging, get_logger
from app.observability.metrics import REGISTRY, set_lifecycle
from app.storage.database import Database
from app.storage.repository import StateRepository

logger = get_logger("aoc.api")


class WorldInputRequest(BaseModel):
    kind: str = "knowledge_topic"
    key: str
    value: dict[str, Any] = {}
    status: str = "open"
    source: str = "operator"
    is_simulated: bool = False


def create_app(settings: Settings | None = None, db: Database | None = None) -> FastAPI:
    cfg = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging(cfg.logging.level, cfg.logging.json_output)
        database = db or Database(cfg.database.url, echo=cfg.database.echo)
        repo = StateRepository(database)
        app.state.settings = cfg
        app.state.db = database
        app.state.repo = repo

        identity = await repo.get_or_create_identity(
            name=cfg.organism.name,
            version=cfg.organism.version,
            traits={"phase": "Phase 0 - Minimum Viable Organism"},
        )
        app.state.organism_id = identity["id"]
        set_lifecycle(LifecycleState.INITIALIZING.value, [s.value for s in LifecycleState])
        await repo.log_event(
            "system.startup",
            message=f"AOC {cfg.organism.name} v{cfg.organism.version} initialized",
            payload={"created_identity": identity["created"]},
        )
        logger.info("aoc.started", organism=cfg.organism.name, organism_id=str(identity["id"]))
        try:
            yield
        finally:
            await repo.log_event("system.shutdown", message="AOC shutting down")
            if db is None:
                await database.dispose()

    app = FastAPI(title="Artificial Organism Core (AOC)", version=cfg.organism.version, lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        db_ok = await app.state.db.ping()
        status = "ok" if db_ok else "degraded"
        return {
            "status": status,
            "organism": cfg.organism.name,
            "version": cfg.organism.version,
            "database": "ok" if db_ok else "unavailable",
        }

    @app.get("/state")
    async def get_state() -> dict[str, Any]:
        repo: StateRepository = app.state.repo
        org_id = app.state.organism_id
        state = await repo.get_organism_state(org_id)
        drives = await repo.load_drive_states(org_id)
        return {
            "organism_id": str(org_id),
            "name": cfg.organism.name,
            "version": cfg.organism.version,
            "state": state,
            "drives": drives,
        }

    @app.get("/drives")
    async def get_drives() -> list[dict[str, Any]]:
        return await app.state.repo.load_drive_states(app.state.organism_id)

    @app.get("/cycles")
    async def list_cycles(limit: int = 20) -> list[dict[str, Any]]:
        return await app.state.repo.list_cycles(limit=min(max(limit, 1), 200))

    @app.get("/events")
    async def list_events(limit: int = 50, event_type: str | None = None) -> list[dict[str, Any]]:
        return await app.state.repo.list_events(limit=min(max(limit, 1), 500), event_type=event_type)

    @app.get("/decisions")
    async def list_decisions(limit: int = 50) -> list[dict[str, Any]]:
        return await app.state.repo.list_decisions(limit=min(max(limit, 1), 200))

    @app.get("/world")
    async def list_world(kind: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        return await app.state.repo.list_world_items(kind=kind, status=status)

    @app.post("/world")
    async def upsert_world(req: WorldInputRequest) -> dict[str, Any]:
        item = await app.state.repo.upsert_world_item(
            kind=req.kind,
            key=req.key,
            value=req.value,
            status=req.status,
            source=req.source,
            is_simulated=req.is_simulated,
        )
        await app.state.repo.log_event("world.updated", message=f"{req.kind}:{req.key}", payload=item)
        return item

    @app.delete("/world/{kind}/{key}")
    async def delete_world(kind: str, key: str) -> dict[str, Any]:
        deleted = await app.state.repo.delete_world_item(kind=kind, key=key)
        if not deleted:
            raise HTTPException(status_code=404, detail="world item not found")
        return {"deleted": True, "kind": kind, "key": key}

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    return app
