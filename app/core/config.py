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
from pydantic import BaseModel, Field, SecretStr
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
    monitor_poll_seconds: float = 5
    max_wait_per_cycle_seconds: float = 120
    max_cycles: int = 0


class ApiSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 9200
    auth_enabled: bool = True
    admin_key: SecretStr | None = None
    internal_token: SecretStr | None = None


class DatabaseSettings(BaseModel):
    url: str = "postgresql+asyncpg://aoc:aoc@127.0.0.1:5433/aoc"
    echo: bool = False


class LLMSettings(BaseModel):
    base_url: str = "http://127.0.0.1:8000/v1"
    model: str = "default"
    api_key: SecretStr | None = None
    temperature: float = 0.3
    max_tokens: int = 4096
    timeout_seconds: float = 180
    max_schema_retries: int = 2


class MemorySettings(BaseModel):
    provider: str = "letta"
    letta_base_url: str = "http://127.0.0.1:8283"
    letta_token: SecretStr | None = None
    agent_name: str = "aoc-organism-memory"
    timeout_seconds: float = 60
    embedding_endpoint: str = "http://aoc:9200/v1"
    embedding_model: str = "aoc-local-embed-1024"
    embedding_dim: int = 1024


class PerceptionSettings(BaseModel):
    monitored_services: list[dict[str, Any]] = Field(default_factory=list)
    disk_paths: list[str] = Field(default_factory=lambda: ["/"])
    llm_probe: bool = True


class BudgetSettings(BaseModel):
    max_agent_tasks_per_day: int = 20
    max_task_timeout_seconds: int = 600
    max_concurrent_tasks: int = 1
    max_llm_calls_per_cycle: int = 8
    max_llm_tokens_per_day: int = 2_000_000
    max_task_retries: int = 2


class GovernanceSettings(BaseModel):
    auto_approve_max_level: str = "L1"
    workspace_root: str = "/home/ubuntu/aoc-workspaces"
    budget: BudgetSettings = Field(default_factory=BudgetSettings)


class LoggingSettings(BaseModel):
    level: str = "INFO"
    json_output: bool = Field(default=True, alias="json")

    model_config = {"populate_by_name": True}


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
