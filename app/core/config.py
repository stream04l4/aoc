"""Configuration management.

Settings are loaded from ``config/settings.yaml`` and overridden by environment
variables prefixed with ``AOC_`` (nested keys use ``__``, e.g. ``AOC_LLM__API_KEY``).
Drive, agent and policy parameters live in their own YAML files and are loaded
read-only; the organism has no code path to write them back.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = Path(os.environ.get("AOC_CONFIG_DIR", PROJECT_ROOT / "config"))


def load_yaml(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


class OrganismSettings(BaseModel):
    name: str = "AOC-001"
    version: str = "1.0.0"


class RuntimeSettings(BaseModel):
    cycle_interval_seconds: float = 60
    idle_sleep_seconds: float = 300
    critical_interval_seconds: float = 30
    throttled_max_sleep_seconds: float = 900
    monitor_poll_seconds: float = 5
    max_wait_per_cycle_seconds: float = 120
    max_cycles: int = 0
    stale_active_goal_hours: int = 12


class ApiSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 9200
    auth_enabled: bool = True
    admin_key: SecretStr | None = None
    internal_token: SecretStr | None = None


class DatabaseSettings(BaseModel):
    url: str = "postgresql+asyncpg://aoc:aoc@127.0.0.1:5433/aoc"
    echo: bool = False
    schema_name: str = "public"


class LLMSettings(BaseModel):
    base_url: str = "http://127.0.0.1:8000/v1"
    model: str = "default"
    api_key: SecretStr | None = None
    temperature: float = 0.3
    max_tokens: int = 9999
    timeout_seconds: float = 180
    max_schema_retries: int = 2


class MemorySettings(BaseModel):
    provider: str = "letta"
    letta_base_url: str = "http://127.0.0.1:8283"
    letta_token: SecretStr | None = None
    agent_name: str = "aoc-organism-memory"
    timeout_seconds: float = 60
    embedding_endpoint: str = "http://aoc-core:9200/v1"
    embedding_model: str = "aoc-local-embed-512"
    embedding_dim: int = 512


class PerceptionSettings(BaseModel):
    monitored_services: list[dict[str, Any]] = Field(default_factory=list)
    disk_paths: list[str] = Field(default_factory=lambda: ["/"])
    llm_probe: bool = True


class BudgetSettings(BaseModel):
    max_agent_tasks_per_day: int = 9999
    max_task_timeout_seconds: int = 600
    max_concurrent_tasks: int = 1
    max_llm_calls_per_cycle: int = 9999
    max_tokens_per_cycle: int = 99_990_000
    max_concurrent_llm_calls: int = 16
    max_llm_tokens_per_day: int = 999_900_000
    emergency_llm_calls_per_day: int = 9999
    emergency_llm_tokens_per_day: int = 99_990_000
    hard_daily_llm_tokens_cap: int = 999_990_000
    max_task_retries: int = 2
    circuit_breaker_threshold: int = 5
    circuit_breaker_cooldown_seconds: float = 30.0


class GovernanceSettings(BaseModel):
    auto_approve_max_level: str = "L1"
    workspace_root: str = "/home/ubuntu/aoc-workspaces"
    budget: BudgetSettings = Field(default_factory=BudgetSettings)


class LoggingSettings(BaseModel):
    level: str = "INFO"
    json_output: bool = Field(default=True, alias="json")

    model_config = {"populate_by_name": True}


class LifecycleSettings(BaseModel):
    task_poll_interval: float = 5.0
    max_inflight_tasks: int = 1
    task_reconciliation_interval: float = 30.0
    outbox_poll_interval: float = 2.0
    outbox_batch_size: int = 20
    outbox_max_attempts: int = 5
    outbox_base_backoff_seconds: float = 10.0
    retry_backoff_seconds: float = 1.0


class RunnerSettings(BaseModel):
    enabled: bool = False
    runner_id: str = "host-runner-1"
    socket_path: str = "/run/aoc-runner/runner.sock"
    state_dir: str = "/var/lib/aoc-runner"
    workspace_root: str = "/home/ubuntu/aoc-workspaces"
    instances_root: str = "/home/ubuntu/aoc-instances"
    instance_id: str | None = None
    cgroup_memory_max: str = "2G"
    cgroup_pids_max: int = 128
    heartbeat_interval_seconds: float = 15.0
    heartbeat_timeout_seconds: float = 60.0
    request_timeout_seconds: float = 30.0
    systemd_unit: str | None = None
    auth_token: SecretStr | None = None


class EmbeddingSettings(BaseModel):
    provider: str = "hash_fallback"  # local_onnx | remote_openai | hash_fallback
    model_key: str = "BAAI/bge-small-zh-v1.5:onnx-qint8"
    model_name: str = "BAAI/bge-small-zh-v1.5"
    model_version: str = "onnx-qint8"
    dimensions: int = 512
    normalized: bool = True
    max_length: int = 256
    query_prefix: str = "为这个句子生成表示以用于检索相关文章："
    passage_prefix: str = ""
    cache_dir: str = str(PROJECT_ROOT / ".cache" / "embeddings")
    onnx_threads: int = 2
    remote_endpoint: str = "http://80.158.109.51:9999/v1"
    remote_model: str = "BAAI/bge-m3"
    remote_api_key: SecretStr | None = None
    timeout_seconds: float = 15.0
    fallback_on_error: bool = True


class TelegramSettings(BaseModel):
    enabled: bool = False
    polling_enabled: bool = False
    polling_interval_seconds: float = 5.0
    bot_token: SecretStr | None = None
    webhook_secret: SecretStr | None = None
    allowed_chat_ids: list[str] = Field(default_factory=list)
    allowed_user_ids: list[str] = Field(default_factory=list)
    api_base_url: str = "https://api.telegram.org"
    timeout_seconds: float = 10.0

    @field_validator("allowed_chat_ids", "allowed_user_ids", mode="before")
    @classmethod
    def _parse_id_list(cls, v: Any) -> list[str]:
        if v is None or v == "":
            return []
        if isinstance(v, str):
            return [part.strip() for part in v.split(",") if part.strip()]
        if isinstance(v, (list, tuple, set)):
            return [str(x).strip() for x in v if str(x).strip()]
        return [str(v).strip()]


class KernelV3Settings(BaseModel):
    """V3.0 Thin Kernel cognitive guards & Feature Flags (zero-bloat, reversible)."""

    # Plan Preflight is a hard execution safety boundary (fail-closed)
    plan_preflight_enabled: bool = True
    duplicate_plan_window_minutes: int = 60

    # Goal deduplication, cooldown & deterministic Information Gain v2
    goal_dedup_enabled: bool = True
    goal_dedup_window_hours: float = 6.0
    goal_dedup_lexical_threshold: float = 0.72
    goal_dedup_semantic_threshold: float = 0.86
    goal_failed_cooldown_minutes: int = 60
    goal_low_ig_cooldown_minutes: int = 120
    goal_low_ig_threshold: float = 0.15
    ig_v2_enabled: bool = True
    # V3.0 M2: optional runtime override for AffectModulator (None = inherit config/affect.yaml)
    affect_enabled: bool | None = None
    # V3.0 M3: external MCP read-only ToolProvider in AutonomousLoop & dynamic world polling
    mcp_in_loop_enabled: bool = True
    max_mcp_calls_per_cycle: int = 3
    world_poll_on_perceive: bool | None = None
    # V3.0 M4: safe RSI foundation — auto-crystallize & L0/L1 auto-validate workspace skills
    skill_auto_crystallize_enabled: bool = True
    # V4.0 M1: external Skill Runtime, MCP bridge & Skill-first Planning
    skill_runtime_enabled: bool = True
    skill_first_planning_enabled: bool = True
    skill_mcp_bridge_enabled: bool = True
    skill_exec_timeout_seconds: float = 15.0
    # V4.0 M2: bounded RSI skill evolution limits (kernel-enforced, immutable by skills)
    skill_evolution_enabled: bool = True
    max_skill_versions_per_canonical: int = 10
    skill_semantic_dedup_threshold: float = 0.65
    # V4.0 M3: external Playbook self-evolution & open public-web curiosity perception (SSRF guarded)
    playbooks_enabled: bool = True
    open_public_perception_enabled: bool = False
    max_internal_curiosity_ratio: float = 0.45
    # V4.1: Proactive rate-limited human outreach & bidirectional Telegram guidance
    human_outreach_enabled: bool = True
    human_outreach_cooldown_minutes: int = 120
    human_outreach_topic_dedup_hours: float = 24.0
    # V4.2: Emergent epistemic foraging (general SearXNG web_search + multi-hop InvestigationManager)
    searxng_url: str = ""
    investigations_enabled: bool = True
    max_active_investigations: int = 2
    investigation_max_hops: int = 6
    investigation_low_ig_stall_streak: int = 2
    # V5.2 M1: Brain-Native Research & Synthesis path (bypasses Codex for pure research/synthesis)
    brain_research_enabled: bool = False
    max_brain_research_mcp_calls: int = 4
    # V5.2 M2: Dynamic Interest Portfolio, cross-domain quotas & topic-fatigue de-weighting
    interest_portfolio_enabled: bool = False
    max_same_domain_streak: int = 3
    max_same_topic_streak: int = 3
    portfolio_starvation_cycles: int = 6
    # V5.2 M3: Explicit directed skill refactoring, skill_first routing enrichment & stale DRAFT expiry
    explicit_skill_refactor_enabled: bool = False
    draft_skill_expiry_hours: int = 48
    # V5.2 M4: Unfreeze reproduction quality drive, periodic meta-reflection critic & self-evolution observability
    reproduction_quality_scoring_enabled: bool = False
    meta_reflection_enabled: bool = False
    meta_reflection_interval_hours: int = 12
    meta_reflection_failure_threshold: int = 5
    meta_reflection_l2_cooldown_hours: int = 24
    # V5.3 M1: Failure-case injection & unused VALIDATED skill lifecycle GC
    skill_lifecycle_gc_enabled: bool = False
    skill_unused_consolidate_days: int = 7
    skill_unused_expiry_days: int = 14
    skill_lifecycle_audit_interval_hours: int = 6
    # V5.3 M2: Deterministic skill_first pre-selector, 1-shot schema auto-heal & 2-fail refactor auto-queue
    skill_first_auto_heal_enabled: bool = False
    skill_match_hint_threshold: float = 0.55
    skill_consecutive_fail_refactor_threshold: int = 2
    # V5.3 M3: Full failure-cluster telemetry & meta-reflection causal attribution / auto-rollback
    meta_reflection_attribution_enabled: bool = False
    meta_reflection_attribution_min_hours: float = 6.0
    meta_reflection_attribution_min_cycles: int = 5
    meta_reflection_regression_delta: float = 0.08
    # V5.3 M4: L2 Evolution Bridge (Meta-Reflection L2 cards -> V5.0 ProposalManager + ConstitutionGuard)
    l2_evolution_bridge_enabled: bool = False


class EvolutionSettings(BaseModel):
    """V5.0 bounded evolutionary reproduction & isolated spawner settings."""

    enabled: bool = False
    instances_root: str = "/home/ubuntu/aoc-instances"
    repo_root: str = str(PROJECT_ROOT)
    docker_image: str = "aoc-aoc:latest"
    docker_network: str = "aoc_default"
    max_concurrent_instances: int = 2
    min_free_disk_ratio: float = 0.20
    candidate_memory_limit: str = "1024m"
    candidate_port_start: int = 9201
    candidate_port_end: int = 9210
    health_timeout_seconds: float = 60.0


DEFAULT_SOCIAL_REACTION_EMOJIS: tuple[str, ...] = ("👀", "👍", "🤔", "🙈", "🔥", "❤️", "🎉", "👌")


class SocialSettings(BaseModel):
    """V5.1 unified organism social cognition & natural Telegram rhythm settings."""

    enabled: bool = False
    auto_enable_affect: bool = True
    dialogue_window_turns: int = 16
    max_inbox_batch: int = 5
    min_proactive_interval_minutes: int = 45
    proactive_backoff_multiplier: float = 2.0
    proactive_max_backoff_minutes: int = 480
    proactive_enabled: bool = True
    max_defer_count: int = 2
    proactive_valence_threshold: float = 0.55
    proactive_arousal_threshold: float = 0.48
    proactive_frustration_threshold: float = 0.60
    proactive_boredom_threshold: float = 0.65
    proactive_high_ig_threshold: float = 0.68
    milestone_dedup_hours: int = 24
    social_memory_max_importance: float = 0.72
    default_defer_minutes: int = 15
    min_defer_minutes: int = 3
    max_defer_minutes: int = 180
    max_reply_chars: int = 1200
    memory_promotion_min_importance: float = 0.65
    allowed_reaction_emojis: list[str] = Field(default_factory=lambda: list(DEFAULT_SOCIAL_REACTION_EMOJIS))
    fallback_reaction_emoji: str = "👀"
    wake_debounce_seconds: float = 1.5

    @field_validator("allowed_reaction_emojis", mode="before")
    @classmethod
    def _parse_emoji_list(cls, v: Any) -> list[str]:
        if v is None or v == "":
            return list(DEFAULT_SOCIAL_REACTION_EMOJIS)
        if isinstance(v, str):
            parsed = [part.strip() for part in v.split(",") if part.strip()]
            return parsed or list(DEFAULT_SOCIAL_REACTION_EMOJIS)
        if isinstance(v, (list, tuple, set)):
            parsed = [str(x).strip() for x in v if str(x).strip()]
            return parsed or list(DEFAULT_SOCIAL_REACTION_EMOJIS)
        return [str(v).strip()] or list(DEFAULT_SOCIAL_REACTION_EMOJIS)


class _YamlSource(PydanticBaseSettingsSource):
    """Lowest-priority source: config/settings.yaml."""

    def get_field_value(self, field, field_name):  # pragma: no cover - unused
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return load_yaml("settings.yaml")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AOC_",
        env_nested_delimiter="__",
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    organism: OrganismSettings = Field(default_factory=OrganismSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    perception: PerceptionSettings = Field(default_factory=PerceptionSettings)
    governance: GovernanceSettings = Field(default_factory=GovernanceSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    lifecycle: LifecycleSettings = Field(default_factory=LifecycleSettings)
    runner: RunnerSettings = Field(default_factory=RunnerSettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    kernel_v3: KernelV3Settings = Field(default_factory=KernelV3Settings)
    evolution: EvolutionSettings = Field(default_factory=EvolutionSettings)
    social: SocialSettings = Field(default_factory=SocialSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        # priority: init > env > .env > yaml
        return (init_settings, env_settings, dotenv_settings, _YamlSource(settings_cls))


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_drive_config() -> dict[str, Any]:
    return load_yaml("drives.yaml")


@lru_cache
def get_agent_config() -> dict[str, Any]:
    return load_yaml("agents.yaml")


@lru_cache
def get_policy_config() -> dict[str, Any]:
    return load_yaml("policy.yaml")


@lru_cache
def get_lifecycle_config() -> dict[str, Any]:
    return load_yaml("lifecycle.yaml")


@lru_cache
def get_runner_config() -> dict[str, Any]:
    return load_yaml("runner.yaml")


@lru_cache
def get_embedding_config() -> dict[str, Any]:
    return load_yaml("embedding.yaml")


@lru_cache
def get_memory_config() -> dict[str, Any]:
    return load_yaml("memory.yaml")


@lru_cache
def get_sensors_config() -> dict[str, Any]:
    return load_yaml("sensors.yaml")


@lru_cache
def get_self_model_config() -> dict[str, Any]:
    return load_yaml("self_model.yaml")


@lru_cache
def get_affect_config() -> dict[str, Any]:
    return load_yaml("affect.yaml")
