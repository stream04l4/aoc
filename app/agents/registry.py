"""Agent Registry: probes installed agents, records real capabilities, and selects agents."""

from __future__ import annotations

from typing import Any

from app.agents.base import AgentCapability, AgentInterface, AgentProbeResult
from app.agents.codex import CodexAdapter
from app.agents.generic_cli import GenericCLIAdapter
from app.agents.hermes import HermesAdapter
from app.core.config import LLMSettings, get_agent_config
from app.observability.logging import get_logger
from app.storage.repository import StateRepository

logger = get_logger("aoc.agents.registry")


class AgentRegistry:
    def __init__(self, repo: StateRepository | None = None) -> None:
        self.repo = repo
        self._adapters: dict[str, AgentInterface] = {}
        self._probes: dict[str, AgentProbeResult] = {}

    def register(self, adapter: AgentInterface, probe: AgentProbeResult | None = None) -> None:
        aid = adapter.capability.agent_id
        self._adapters[aid] = adapter
        if probe is not None:
            self._probes[aid] = probe

    def unregister(self, agent_id: str) -> bool:
        self._probes.pop(agent_id, None)
        return self._adapters.pop(agent_id, None) is not None

    def get(self, agent_id: str) -> AgentInterface | None:
        return self._adapters.get(agent_id)

    def get_probe(self, agent_id: str) -> AgentProbeResult | None:
        return self._probes.get(agent_id)

    def load_from_config(
        self,
        cfg: dict[str, Any] | None = None,
        llm_settings: LLMSettings | None = None,
    ) -> None:
        data = cfg if cfg is not None else get_agent_config()
        for entry in data.get("agents", []):
            cap = AgentCapability(
                agent_id=entry["agent_id"],
                name=entry["name"],
                agent_type=entry.get("agent_type", "cli"),
                capabilities=list(entry.get("capabilities", [])),
                supported_modes=list(entry.get("supported_modes", [])),
                max_concurrency=int(entry.get("max_concurrency", 1)),
                enabled=bool(entry.get("enabled", True)),
                experimental=bool(entry.get("experimental", False)),
            )
            kind = entry.get("adapter", "generic_cli")
            binary = entry.get("binary", cap.agent_id)
            options = entry.get("options") or {}
            if kind == "codex":
                adapter: AgentInterface = CodexAdapter(cap, binary=binary, options=options, llm_settings=llm_settings)
            elif kind == "hermes":
                adapter = HermesAdapter(cap, binary=binary, options=options, llm_settings=llm_settings)
            else:
                adapter = GenericCLIAdapter(cap, binary=binary, options=options)
            self.register(adapter)

    async def probe_all(self) -> dict[str, AgentProbeResult]:
        """Probe every registered agent and persist actual verified status to DB."""
        results: dict[str, AgentProbeResult] = {}
        for aid, adapter in self._adapters.items():
            cap = adapter.capability
            if not cap.enabled:
                probe = AgentProbeResult(ok=False, error="disabled in config")
            else:
                probe = await adapter.probe()
            self._probes[aid] = probe
            results[aid] = probe
            logger.info(
                "agent.probed",
                agent_id=aid,
                enabled=cap.enabled,
                ok=probe.ok,
                experimental=cap.experimental,
                version=probe.version,
                error=probe.error,
            )
            if self.repo is not None:
                await self.repo.upsert_agent(
                    cap.model_dump(mode="json"),
                    experimental=cap.experimental,
                    probe=probe.model_dump(mode="json"),
                )
        return results

    def list_healthy(self, exclude: set[str] | None = None) -> list[AgentCapability]:
        excl = exclude or set()
        out: list[AgentCapability] = []
        for aid, adapter in self._adapters.items():
            if aid in excl:
                continue
            cap = adapter.capability
            probe = self._probes.get(aid)
            if cap.enabled and probe is not None and probe.ok:
                out.append(cap)
        # Prefer non-experimental agents first
        out.sort(key=lambda c: (c.experimental, c.agent_id))
        return out

    def select_agent(
        self,
        preferred_agent: str | None = None,
        required_capability: str | None = None,
        exclude: set[str] | None = None,
    ) -> AgentInterface | None:
        """Select a healthy agent matching preferred_agent or required_capability, excluding already-tried ones."""
        healthy = self.list_healthy(exclude=exclude)
        if not healthy:
            return None

        if preferred_agent:
            for cap in healthy:
                if cap.agent_id == preferred_agent:
                    return self._adapters[cap.agent_id]

        if required_capability:
            for cap in healthy:
                if required_capability in cap.capabilities:
                    return self._adapters[cap.agent_id]

        return self._adapters[healthy[0].agent_id]

    def summary_for_brain(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for aid, adapter in self._adapters.items():
            cap = adapter.capability
            probe = self._probes.get(aid)
            items.append(
                {
                    "agent_id": cap.agent_id,
                    "name": cap.name,
                    "capabilities": cap.capabilities,
                    "supported_modes": cap.supported_modes,
                    "enabled": cap.enabled,
                    "experimental": cap.experimental,
                    "healthy": bool(probe and probe.ok),
                }
            )
        return items
