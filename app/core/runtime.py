"""OrganismRuntime: wires all AOC modules through their explicit interfaces."""

from __future__ import annotations

import uuid
from typing import Any

from app.agents.registry import AgentRegistry
from app.cognition.brain import CognitionBrain
from app.cognition.gateway import LLMGateway, LLMGatewayInterface
from app.cognition.interfaces import BrainInterface
from app.core.config import Settings, get_agent_config, get_drive_config, get_policy_config, get_settings
from app.core.lifecycle import LifecycleManager
from app.core.loop import AutonomousLoop
from app.core.scheduler import OrganismScheduler
from app.drives.engine import DriveEngine
from app.governance.policy import GovernanceManager
from app.memory.experience_manager import ExperienceManager
from app.memory.interfaces import MemoryInterface
from app.memory.letta_adapter import LettaAdapter
from app.observability.logging import configure_logging, get_logger
from app.orchestration.orchestrator import AgentOrchestrator
from app.perception.agent_sensor import AgentSensor
from app.perception.environment_sensor import EnvironmentSensor
from app.perception.manager import SensorManager
from app.perception.system_sensor import SystemSensor
from app.storage.database import Database
from app.storage.repository import StateRepository

logger = get_logger("aoc.runtime")


class OrganismRuntime:
    def __init__(
        self,
        settings: Settings | None = None,
        db: Database | None = None,
        brain: BrainInterface | None = None,
        memory_backend: MemoryInterface | None = None,
        registry: AgentRegistry | None = None,
        drive_config: dict[str, Any] | None = None,
        policy_config: dict[str, Any] | None = None,
        auto_load_agents: bool = True,
        llm_probe_fn=None,
        llm_gateway: LLMGatewayInterface | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._owns_db = db is None
        self.db = db or Database(self.settings.database.url, echo=self.settings.database.echo)
        self.repo = StateRepository(self.db)

        self.organism_id: uuid.UUID | None = None
        self.lifecycle: LifecycleManager | None = None

        # Unified LLM Gateway (P1-1: all LLM calls, health probes, and external token accounting)
        self.llm_gateway: LLMGatewayInterface = llm_gateway or LLMGateway(
            llm_settings=self.settings.llm,
            budget_settings=self.settings.governance.budget,
            repo=self.repo,
        )

        # Perception (shares LLMGateway.health_check when llm_probe is enabled)
        effective_llm_probe = llm_probe_fn
        if effective_llm_probe is None and self.settings.perception.llm_probe:
            effective_llm_probe = self.llm_gateway.health_check
        self.perception = SensorManager(repo=self.repo)
        self.system_sensor = SystemSensor(
            self.settings.perception,
            self.settings.llm,
            repo=self.repo,
            llm_probe_fn=effective_llm_probe,
        )
        self.agent_sensor = AgentSensor(self.repo)
        self.env_sensor = EnvironmentSensor(self.repo)
        self.perception.register(self.system_sensor)
        self.perception.register(self.agent_sensor)
        self.perception.register(self.env_sensor)

        # Drives
        self.drives = DriveEngine(config=drive_config if drive_config is not None else get_drive_config())

        # Brain
        self.brain: BrainInterface = brain or CognitionBrain(self.llm_gateway)

        # Governance
        self.governance = GovernanceManager(
            self.settings.governance,
            self.repo,
            policy_cfg=policy_config if policy_config is not None else get_policy_config(),
        )

        # Agents & Orchestration (inject llm_settings explicitly — no hidden global get_settings())
        self.registry = registry or AgentRegistry(repo=self.repo)
        if registry is None and auto_load_agents:
            self.registry.load_from_config(get_agent_config(), llm_settings=self.settings.llm)
        self.orchestrator = AgentOrchestrator(
            registry=self.registry,
            governance=self.governance,
            repo=self.repo,
            max_retries=self.settings.governance.budget.max_task_retries,
            llm_gateway=self.llm_gateway,
        )

        # Memory
        if memory_backend is not None:
            backend: MemoryInterface | None = memory_backend
        elif self.settings.memory.provider.lower() == "letta":
            backend = LettaAdapter(
                self.settings.memory,
                llm_model=self.settings.llm.model,
                llm_endpoint=self.settings.llm.base_url,
            )
        else:
            backend = None
        self.memory = ExperienceManager(self.repo, backend=backend)

        self.loop: AutonomousLoop | None = None
        self.scheduler: OrganismScheduler | None = None

    async def start(self, start_scheduler: bool = True) -> dict[str, Any]:
        configure_logging(self.settings.logging.level, self.settings.logging.json_output)
        identity = await self.repo.get_or_create_identity(
            name=self.settings.organism.name,
            version=self.settings.organism.version,
            traits={"phase": "Phase 0 - Minimum Viable Organism"},
        )
        self.organism_id = identity["id"]
        if isinstance(self.memory.backend, LettaAdapter) and identity.get("memory_agent_id"):
            self.memory.backend.agent_id = identity["memory_agent_id"]

        self.lifecycle = LifecycleManager(self.organism_id, self.repo)
        self.loop = AutonomousLoop(
            organism_id=self.organism_id,
            settings=self.settings,
            repo=self.repo,
            lifecycle=self.lifecycle,
            perception=self.perception,
            drives=self.drives,
            brain=self.brain,
            governance=self.governance,
            registry=self.registry,
            orchestrator=self.orchestrator,
            memory=self.memory,
            llm_gateway=self.llm_gateway,
        )
        recovery = await self.loop.recover()
        self.scheduler = OrganismScheduler(self.loop, self.lifecycle, self.settings.runtime)
        if start_scheduler:
            self.scheduler.start()
        logger.info("runtime.started", organism=self.settings.organism.name, organism_id=str(self.organism_id))
        return {"organism_id": str(self.organism_id), "recovery": recovery}

    async def stop(self) -> None:
        if self.scheduler is not None:
            await self.scheduler.stop()
        elif self.lifecycle is not None:
            await self.lifecycle.stop("runtime stop")
        if self._owns_db:
            await self.db.dispose()
        logger.info("runtime.stopped")
