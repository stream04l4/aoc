"""OrganismRuntime: wires all AOC modules through their explicit interfaces."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.affect.modulator import AffectModulator
from app.agents.registry import AgentRegistry
from app.cognition.brain import CognitionBrain
from app.cognition.gateway import LLMGateway, LLMGatewayInterface
from app.cognition.interfaces import BrainInterface
from app.core.config import (
    EmbeddingSettings,
    LifecycleSettings,
    RunnerSettings,
    Settings,
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
    get_settings,
)
from app.core.lifecycle import LifecycleManager
from app.core.loop import AutonomousLoop
from app.core.scheduler import OrganismScheduler
from app.drives.engine import DriveEngine
from app.evolution.arena import FitnessArena
from app.evolution.constitution_guard import ConstitutionGuard
from app.evolution.entropy_sensor import EntropySensor
from app.evolution.gc import CullingGC
from app.evolution.proposal_manager import ProposalManager
from app.evolution.repository import EvolutionRepository
from app.evolution.spawner import InstanceSpawner
from app.evolution.succession import SuccessionManager
from app.governance.approval import ApprovalService
from app.governance.policy import GovernanceManager
from app.governance.semantic_policy import SemanticRiskClassifier
from app.memory.embedding import EmbeddingInterface, EmbeddingService, build_embedding_provider
from app.memory.experience_manager import ExperienceManager
from app.memory.info_gain import InformationGainEvaluator
from app.memory.interfaces import MemoryInterface
from app.memory.letta_adapter import LettaAdapter
from app.memory.retrieval import HybridRetrievalEngine
from app.notifications.human_bridge import HumanBridge
from app.notifications.telegram import TelegramNotifier, TelegramPoller, TelegramWebhookHandler
from app.observability.logging import configure_logging, get_logger
from app.orchestration.backend import ExecutionBackendInterface, LocalSubprocessBackend
from app.orchestration.finalizer import TaskFinalizer
from app.orchestration.orchestrator import AgentOrchestrator
from app.orchestration.state_machine import TaskStateMachine
from app.perception.agent_sensor import AgentSensor
from app.perception.environment_sensor import EnvironmentSensor
from app.perception.manager import SensorManager
from app.perception.system_sensor import SystemSensor
from app.perception.world.exploration import ExplorationManager
from app.perception.world.git_sensor import GitWorldSensor
from app.perception.world.investigation import InvestigationManager
from app.perception.world.pipeline import WorldEventPipeline
from app.perception.world.rss_sensor import RSSWorldSensor
from app.perception.world.web_sensor import WebChangeWorldSensor
from app.self_model.manager import SelfModelManager
from app.self_model.skills import SkillRegistryManager
from app.skills.evolution import SkillEvolutionEngine
from app.skills.playbooks import PlaybookManager
from app.skills.runtime import SkillRuntime
from app.storage.database import Database
from app.storage.outbox import OutboxDispatcher, OutboxEventType
from app.storage.repository import StateRepository
from app.tools.mcp_manager import MCPClientManager

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
        execution_backend: ExecutionBackendInterface | None = None,
        embedding_provider: EmbeddingInterface | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        lc_yaml = get_lifecycle_config()
        if lc_yaml and self.settings.lifecycle == LifecycleSettings():
            self.settings = self.settings.model_copy(update={"lifecycle": LifecycleSettings.model_validate(lc_yaml)})
        rn_yaml = get_runner_config()
        if rn_yaml and self.settings.runner == RunnerSettings():
            self.settings = self.settings.model_copy(update={"runner": RunnerSettings.model_validate(rn_yaml)})
        mem_yaml = get_memory_config()
        # Only load embedding.yaml automatically if caller didn't override Settings.embedding and cache/config is intended
        emb_yaml = get_embedding_config()
        if embedding_provider is None and self.settings.embedding == EmbeddingSettings() and emb_yaml:
            # Keep unit tests offline-safe: only auto-enable local_onnx if model cache exists or explicitly configured
            emb_cfg = EmbeddingSettings.model_validate(emb_yaml)
            if emb_cfg.provider == "local_onnx" and not Path(emb_cfg.cache_dir).exists():
                emb_cfg = emb_cfg.model_copy(update={"provider": "hash_fallback"})
            self.settings = self.settings.model_copy(update={"embedding": emb_cfg})
        self._owns_db = db is None
        self.db = db or Database(
            self.settings.database.url,
            echo=self.settings.database.echo,
            schema_name=self.settings.database.schema_name,
        )
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
            db_ping_fn=self.db.ping,
        )
        self.agent_sensor = AgentSensor(self.repo)
        self.env_sensor = EnvironmentSensor(self.repo)
        self.perception.register(self.system_sensor)
        self.perception.register(self.agent_sensor)
        self.perception.register(self.env_sensor)
        self._sensors_cfg: dict[str, Any] = get_sensors_config()
        self.world_pipeline: WorldEventPipeline | None = None

        # Drives
        self.drives = DriveEngine(config=drive_config if drive_config is not None else get_drive_config())

        # Brain
        self.brain: BrainInterface = brain or CognitionBrain(self.llm_gateway)

        # Governance (with optional bilingual SemanticRiskClassifier via LLMGateway, P1-8)
        eff_policy_cfg = policy_config if policy_config is not None else get_policy_config()
        sem_cfg = (eff_policy_cfg or {}).get("semantic_policy") or {}
        self.semantic_classifier: SemanticRiskClassifier | None = (
            SemanticRiskClassifier(
                self.llm_gateway,
                timeout_seconds=float(sem_cfg.get("timeout_seconds", 12.0)),
                fail_closed_level=str(sem_cfg.get("fail_closed_level", "L2")),
            )
            if bool(sem_cfg.get("enabled", False))
            else None
        )
        self.governance = GovernanceManager(
            self.settings.governance,
            self.repo,
            policy_cfg=eff_policy_cfg,
            semantic_classifier=self.semantic_classifier,
        )

        # Agents & Orchestration (inject llm_settings explicitly — no hidden global get_settings())
        self.registry = registry or AgentRegistry(repo=self.repo)
        if registry is None and auto_load_agents:
            self.registry.load_from_config(get_agent_config(), llm_settings=self.settings.llm)
        self.state_machine = TaskStateMachine(self.repo)
        if execution_backend is not None:
            self.execution_backend: ExecutionBackendInterface = execution_backend
        elif self.settings.runner.enabled:
            from app.runner.client import RunnerClient, UnixSocketRunnerBackend

            rc = RunnerClient(
                socket_path=self.settings.runner.socket_path,
                auth_token=(
                    self.settings.runner.auth_token.get_secret_value() if self.settings.runner.auth_token else ""
                ),
                timeout_seconds=self.settings.runner.request_timeout_seconds,
            )
            self.execution_backend = UnixSocketRunnerBackend(
                rc,
                runner_id=self.settings.runner.runner_id,
                instance_id=self.settings.runner.instance_id,
            )
            self.registry.runner_client = rc
        else:
            self.execution_backend = LocalSubprocessBackend()
        self.orchestrator = AgentOrchestrator(
            registry=self.registry,
            governance=self.governance,
            repo=self.repo,
            max_retries=self.settings.governance.budget.max_task_retries,
            llm_gateway=self.llm_gateway,
            backend=self.execution_backend,
            state_machine=self.state_machine,
        )
        app_ttl = float(((eff_policy_cfg or {}).get("approval") or {}).get("default_ttl_hours", 24.0))
        self.approval_service = ApprovalService(self.repo, self.orchestrator, default_ttl_hours=app_ttl)
        self.telegram_notifier = TelegramNotifier(self.settings.telegram)
        self.human_bridge = HumanBridge(
            repo=self.repo,
            enabled=self.settings.kernel_v3.human_outreach_enabled,
            cooldown_minutes=self.settings.kernel_v3.human_outreach_cooldown_minutes,
            topic_dedup_hours=self.settings.kernel_v3.human_outreach_topic_dedup_hours,
        )
        self.evolution_repo = EvolutionRepository(self.repo.db)
        self.entropy_sensor = EntropySensor(self.repo.db)
        self.constitution_guard = ConstitutionGuard(repo_root=self.settings.evolution.repo_root)
        self.proposal_manager = ProposalManager(
            evo_repo=self.evolution_repo,
            state_repo=self.repo,
            constitution_guard=self.constitution_guard,
            entropy_sensor=self.entropy_sensor,
        )
        self.culling_gc = CullingGC(
            repo=self.evolution_repo,
            pg_admin_db=self.repo.db,
            repo_root=self.settings.evolution.repo_root,
            instances_root=self.settings.evolution.instances_root,
        )
        self.instance_spawner = InstanceSpawner(
            settings=self.settings.evolution,
            repo=self.evolution_repo,
            gc=self.culling_gc,
            require_stage2_approval=True,
        )
        self.fitness_arena = FitnessArena(
            evo_repo=self.evolution_repo,
            state_repo=self.repo,
            proposal_manager=self.proposal_manager,
            spawner=self.instance_spawner,
            gc=self.culling_gc,
            constitution_guard=self.constitution_guard,
        )

        async def _retire_active_parent(_parent: dict[str, Any]) -> None:
            if self.lifecycle is not None:
                try:
                    await self.lifecycle.pause(reason="V5 generational succession retirement")
                except Exception:
                    pass

        self.succession_manager = SuccessionManager(
            evo_repo=self.evolution_repo,
            state_repo=self.repo,
            proposal_manager=self.proposal_manager,
            constitution_guard=self.constitution_guard,
            repo_root=self.settings.evolution.repo_root,
            snapshots_dir=Path(self.settings.evolution.instances_root) / "snapshots",
            parent_sleep_fn=_retire_active_parent,
        )
        self.telegram_webhook = TelegramWebhookHandler(
            self.settings.telegram,
            self.approval_service,
            self.repo,
            notifier=self.telegram_notifier,
            proposal_manager=self.proposal_manager,
            succession_manager=self.succession_manager,
            human_bridge=self.human_bridge,
            social_settings=self.settings.social,
        )
        self.telegram_poller: TelegramPoller | None = None
        self.outbox = OutboxDispatcher(
            self.repo,
            poll_interval=self.settings.lifecycle.outbox_poll_interval,
            batch_size=self.settings.lifecycle.outbox_batch_size,
            base_backoff_seconds=self.settings.lifecycle.outbox_base_backoff_seconds,
        )
        self.finalizer: TaskFinalizer | None = None

        # Semantic Embedding, Hybrid Retrieval & Async Outbox Memory (P1-4)
        eff_emb_provider = embedding_provider or build_embedding_provider(self.settings.embedding)
        self.embedding_service = EmbeddingService(self.repo, eff_emb_provider)
        ret_cfg = (mem_yaml or {}).get("hybrid_retrieval") or {}
        self.retrieval_engine = HybridRetrievalEngine(
            self.repo,
            self.embedding_service,
            w_vec=float(ret_cfg.get("w_vec", 0.45)),
            w_txt=float(ret_cfg.get("w_txt", 0.20)),
            w_rec=float(ret_cfg.get("w_rec", 0.15)),
            w_rel=float(ret_cfg.get("w_rel", 0.10)),
            w_imp=float(ret_cfg.get("w_imp", 0.10)),
            recency_half_life_hours=float(ret_cfg.get("recency_half_life_hours", 168.0)),
            candidate_pool_size=int(ret_cfg.get("candidate_pool_size", 120)),
        )
        ig_cfg = (mem_yaml or {}).get("info_gain") or {}
        self.info_gain_evaluator = InformationGainEvaluator(
            sim_floor=float(ig_cfg.get("semantic_novelty_sim_floor", 0.25)),
            sim_ceil=float(ig_cfg.get("semantic_novelty_sim_ceil", 0.92)),
            duplicate_sim_threshold=float(ig_cfg.get("duplicate_sim_threshold", 0.92)),
            corroboration_sim_min=float(ig_cfg.get("corroboration_sim_min", 0.72)),
            corroboration_bonus=float(ig_cfg.get("corroboration_bonus", 0.35)),
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
        if isinstance(backend, LettaAdapter) and self.culling_gc is not None:
            self.culling_gc.letta_adapter = backend
        async_sync = bool((mem_yaml or {}).get("async_outbox_sync", True))
        embed_on_write = bool((mem_yaml or {}).get("embed_on_write", True))
        self.memory = ExperienceManager(
            self.repo,
            backend=backend,
            embedding_service=self.embedding_service,
            retrieval_engine=self.retrieval_engine,
            outbox_enabled=async_sync,
            embed_on_write=embed_on_write,
        )

        # Digital World Perception Pipeline (V2.0 P1-5: RSS, Git, Web + SSRF/PI guards)
        scfg = self._sensors_cfg or {}
        allowed_domains = list(scfg.get("allowed_domains") or [])
        allowed_git_roots = list(scfg.get("allowed_git_roots") or [])
        max_bytes = int(scfg.get("max_response_bytes", 2 * 1024 * 1024))
        s_timeout = float(scfg.get("timeout_seconds", 15.0))
        max_redir = int(scfg.get("max_redirects", 3))
        self.world_pipeline = WorldEventPipeline(
            self.repo,
            sensors=[
                RSSWorldSensor(
                    allowed_domains=allowed_domains,
                    max_response_bytes=max_bytes,
                    timeout_seconds=s_timeout,
                    max_redirects=max_redir,
                ),
                GitWorldSensor(allowed_git_roots=allowed_git_roots),
                WebChangeWorldSensor(
                    allowed_domains=allowed_domains,
                    max_response_bytes=max_bytes,
                    timeout_seconds=s_timeout,
                    max_redirects=max_redir,
                ),
            ],
            interest_keywords=list(scfg.get("interest_keywords") or []),
            importance_threshold=float(scfg.get("importance_threshold", 0.45)),
            embedding_service=self.embedding_service,
            allowed_domains=allowed_domains,
            allowed_git_roots=allowed_git_roots,
            open_public_https=self.settings.kernel_v3.open_public_perception_enabled,
        )
        self.env_sensor.world_pipeline = self.world_pipeline
        if self.settings.kernel_v3.world_poll_on_perceive is not None:
            self.env_sensor.poll_on_observe = bool(self.settings.kernel_v3.world_poll_on_perceive)
        else:
            self.env_sensor.poll_on_observe = bool(scfg.get("poll_on_perceive", False))
        self.system_sensor._runner_health_fn = self.orchestrator.sync_runner_heartbeat

        # Guarded MCP ToolProvider (V2.0 P1-6: second controlled tool path, strictly distinct from AgentProvider)
        llm_host = (urlparse(self.settings.llm.base_url).hostname or "").strip().lower()
        blocked_hosts = [llm_host] if llm_host else []
        self.mcp_manager = MCPClientManager(
            governance=self.governance,
            repo=self.repo,
            workspace_root=self.settings.governance.workspace_root,
            allowed_domains=allowed_domains,
            allowed_git_roots=allowed_git_roots,
            open_public_https=bool(
                self.settings.kernel_v3.open_public_perception_enabled
                or self.settings.kernel_v3.brain_research_enabled
            ),
            blocked_hosts=blocked_hosts,
            searxng_url=self.settings.kernel_v3.searxng_url,
        )
        self.mcp_manager.load_from_config(get_agent_config())

        self._self_model_cfg: dict[str, Any] = get_self_model_config()
        self._affect_cfg: dict[str, Any] = get_affect_config()
        self.self_model: SelfModelManager | None = None
        self.skill_registry: SkillRegistryManager | None = None
        self.skill_runtime: SkillRuntime | None = None
        self.skill_evolution: SkillEvolutionEngine | None = None
        self.playbook_manager: PlaybookManager | None = None
        self.exploration_manager: ExplorationManager | None = None
        self.investigation_manager: InvestigationManager | None = None
        self.affect: AffectModulator | None = None

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
        if self.telegram_webhook.lifecycle is None:
            self.telegram_webhook.lifecycle = self.lifecycle

        # Dynamic Self Model & Skill Registry (V2.0 P1-7)
        sm_cfg = dict(self._self_model_cfg or {})
        ws_root = str(self.settings.governance.workspace_root)
        if not sm_cfg.get("skill_storage_dir") or ws_root != "/home/ubuntu/aoc-workspaces":
            sm_cfg["skill_storage_dir"] = str(Path(ws_root) / "skills")
        self.skill_registry = SkillRegistryManager(
            repo=self.repo,
            governance=self.governance,
            config=sm_cfg,
        )
        self.self_model = SelfModelManager(
            organism_id=self.organism_id,
            repo=self.repo,
            config=sm_cfg,
            memory_backend=self.memory.backend,
            skill_registry=self.skill_registry,
        )
        self.skill_registry.self_model = self.self_model
        # V4.0 M1/M2: External isolated SkillRuntime, Bounded RSI Evolution Engine & MCP bridge
        if self.settings.kernel_v3.skill_evolution_enabled:
            self.skill_evolution = SkillEvolutionEngine(
                repo=self.repo,
                skill_registry=self.skill_registry,
                max_versions_per_canonical=self.settings.kernel_v3.max_skill_versions_per_canonical,
                dedup_threshold=self.settings.kernel_v3.skill_semantic_dedup_threshold,
            )
            self.skill_registry.evolution_engine = self.skill_evolution
        if self.settings.kernel_v3.skill_runtime_enabled:
            self.skill_runtime = SkillRuntime(
                repo=self.repo,
                skill_registry=self.skill_registry,
                governance=self.governance,
                default_timeout_seconds=self.settings.kernel_v3.skill_exec_timeout_seconds,
            )
            if self.settings.kernel_v3.skill_mcp_bridge_enabled and self.mcp_manager is not None:
                self.mcp_manager.attach_skill_runtime(self.skill_runtime)
                self.skill_registry.set_on_skills_changed(self.mcp_manager.sync_validated_skills_as_tools)
                await self.mcp_manager.sync_validated_skills_as_tools()

        # V4.0 M3: External Self-Evolving Playbooks & Curiosity Anti-Internal-Loop Manager
        if self.settings.kernel_v3.playbooks_enabled:
            self.playbook_manager = PlaybookManager(
                repo=self.repo,
                storage_dir=Path(ws_root) / "playbooks",
            )
        self.exploration_manager = ExplorationManager(
            repo=self.repo,
            max_internal_curiosity_ratio=self.settings.kernel_v3.max_internal_curiosity_ratio,
            interest_portfolio_enabled=self.settings.kernel_v3.interest_portfolio_enabled,
            max_same_domain_streak=self.settings.kernel_v3.max_same_domain_streak,
            max_same_topic_streak=self.settings.kernel_v3.max_same_topic_streak,
            portfolio_starvation_cycles=self.settings.kernel_v3.portfolio_starvation_cycles,
        )
        if self.settings.kernel_v3.investigations_enabled:
            self.investigation_manager = InvestigationManager(
                repo=self.repo,
                max_active=self.settings.kernel_v3.max_active_investigations,
                max_hops=self.settings.kernel_v3.investigation_max_hops,
                low_ig_stall_streak=self.settings.kernel_v3.investigation_low_ig_stall_streak,
                low_ig_threshold=self.settings.kernel_v3.goal_low_ig_threshold,
            )

        # Optional bounded Affect modulation layer (V2.0 P1-9 / V3.0 M2 / V5.1 live social affect)
        eff_affect_cfg = dict(self._affect_cfg or {})
        if self.settings.kernel_v3.affect_enabled is not None:
            eff_affect_cfg["enabled"] = bool(self.settings.kernel_v3.affect_enabled)
        elif self.settings.social.enabled and self.settings.social.auto_enable_affect:
            eff_affect_cfg["enabled"] = True
        self.affect = AffectModulator(
            organism_id=self.organism_id,
            repo=self.repo,
            config=eff_affect_cfg,
        )

        self.finalizer = TaskFinalizer(
            organism_id=self.organism_id,
            repo=self.repo,
            perception=self.perception,
            drives=self.drives,
            brain=self.brain,
            governance=self.governance,
            memory=self.memory,
            verifier=self.orchestrator.verifier,
            llm_gateway=self.llm_gateway,
            embedding_service=self.embedding_service,
            info_gain_evaluator=self.info_gain_evaluator,
            self_model=self.self_model,
            skill_registry=self.skill_registry,
            skill_auto_crystallize=self.settings.kernel_v3.skill_auto_crystallize_enabled,
            playbook_manager=self.playbook_manager,
            exploration_manager=self.exploration_manager,
            human_bridge=self.human_bridge,
            investigation_manager=self.investigation_manager,
            world_pipeline=self.world_pipeline,
        )

        async def _handle_finalize_event(ev: dict[str, Any]) -> dict[str, Any]:
            eid = (ev.get("payload") or {}).get("execution_id")
            if not eid:
                return {"skipped": True}
            res = await self.finalizer.finalize_execution(eid)
            return {"execution_id": res.execution_id, "already_finalized": res.already_finalized, "goal_achieved": res.goal_achieved}

        async def _handle_memory_embed(ev: dict[str, Any]) -> dict[str, Any]:
            mid = (ev.get("payload") or {}).get("memory_id")
            if not mid:
                return {"skipped": True}
            return await self.embedding_service.embed_memory_record(uuid.UUID(mid))

        async def _handle_memory_sync_letta(ev: dict[str, Any]) -> dict[str, Any]:
            mid = (ev.get("payload") or {}).get("memory_id")
            if not mid:
                return {"skipped": True}
            return await self.memory.sync_memory_to_letta(uuid.UUID(mid))

        async def _handle_self_model_sync_letta(ev: dict[str, Any]) -> dict[str, Any]:
            vid = (ev.get("payload") or {}).get("version_id")
            if not vid or self.self_model is None:
                return {"skipped": True}
            return await self.self_model.sync_version_to_letta(vid)

        self.outbox.register_handler(OutboxEventType.EXECUTION_FINALIZE, "outbox_finalizer", _handle_finalize_event)
        self.outbox.register_handler(OutboxEventType.MEMORY_EMBED, "outbox_memory_embed", _handle_memory_embed)
        self.outbox.register_handler(OutboxEventType.MEMORY_SYNC_LETTA, "outbox_memory_letta", _handle_memory_sync_letta)
        self.outbox.register_handler(
            OutboxEventType.SELF_MODEL_SYNC_LETTA,
            "outbox_self_model_letta",
            _handle_self_model_sync_letta,
        )
        self.outbox.register_handler(
            OutboxEventType.NOTIFICATION_DISPATCH,
            "outbox_telegram_notifier",
            self.telegram_notifier.dispatch_notification,
        )

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
            finalizer=self.finalizer,
            outbox=self.outbox,
            approval_service=self.approval_service,
            self_model=self.self_model,
            skill_registry=self.skill_registry,
            skill_runtime=self.skill_runtime,
            affect=self.affect,
            mcp_manager=self.mcp_manager,
            world_pipeline=self.world_pipeline,
            playbook_manager=self.playbook_manager,
            exploration_manager=self.exploration_manager,
            human_bridge=self.human_bridge,
            investigation_manager=self.investigation_manager,
            entropy_sensor=self.entropy_sensor,
            culling_gc=self.culling_gc,
            telegram_notifier=self.telegram_notifier,
        )
        self.finalizer._on_learning = self.loop._record_learning_signal
        self.finalizer._on_prediction_error = self.loop._record_prediction_error
        if self.world_pipeline is not None and (self._sensors_cfg or {}).get("sources"):
            await self.world_pipeline.sync_configured_sources(self._sensors_cfg["sources"])
        recovery = await self.loop.recover()
        self.scheduler = OrganismScheduler(self.loop, self.lifecycle, self.settings.runtime)
        if start_scheduler:
            self.outbox.start()
            self.scheduler.start()
            if (
                self.settings.telegram.enabled
                and self.settings.telegram.polling_enabled
                and self.telegram_notifier.configured
                and self.settings.telegram.allowed_chat_ids
            ):
                async def _status_summary() -> str:
                    st = await self.repo.get_organism_state(self.organism_id) if self.organism_id else {}
                    dr = await self.repo.load_drive_states(self.organism_id) if self.organism_id else []
                    dr_line = " | ".join(
                        f"{d['drive_type']}: sat={d['satisfaction']:.2f}" for d in sorted(dr, key=lambda x: x["drive_type"])
                    )
                    inv_block = ""
                    if self.investigation_manager is not None:
                        inv_threads = await self.investigation_manager.list_threads(active_only=True, limit=3)
                        inv_channels = await self.investigation_manager.list_discovered_channels(limit=10)
                        inv_lines = [
                            f"  - [{t.get('stage')}] {t.get('hops_taken', 0)}/{t.get('max_hops', 6)}跳: {str(t.get('core_question') or '')[:60]}"
                            for t in inv_threads
                        ]
                        inv_block = (
                            f"\n探索主线({len(inv_threads)}条活跃) | 已发现渠道: {len(inv_channels)}"
                            + (("\n" + "\n".join(inv_lines)) if inv_lines else "")
                        )
                    return (
                        f"AOC-001 ({self.lifecycle.state.value if self.lifecycle else 'UNKNOWN'})\n"
                        f"周期: #{(st or {}).get('cycle_count', 0)}\n"
                        f"内驱力: {dr_line or '暂无数据'}"
                        f"{inv_block}"
                    )

                self.telegram_poller = TelegramPoller(
                    settings=self.settings.telegram,
                    notifier=self.telegram_notifier,
                    webhook_handler=self.telegram_webhook,
                    human_bridge=self.human_bridge,
                    repo=self.repo,
                    lifecycle=self.lifecycle,
                    status_fn=_status_summary,
                    proposal_manager=self.proposal_manager,
                    succession_manager=self.succession_manager,
                    social_settings=self.settings.social,
                )
                self.telegram_poller.start()
        logger.info("runtime.started", organism=self.settings.organism.name, organism_id=str(self.organism_id))
        return {"organism_id": str(self.organism_id), "recovery": recovery}

    async def stop(self) -> None:
        if self.telegram_poller is not None:
            await self.telegram_poller.stop()
        await self.outbox.stop()
        if self.scheduler is not None:
            await self.scheduler.stop()
        elif self.lifecycle is not None:
            await self.lifecycle.stop("runtime stop")
        if self._owns_db:
            await self.db.dispose()
        logger.info("runtime.stopped")
