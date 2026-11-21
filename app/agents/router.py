"""Explainable Execution Router (V2.0 P1-6, Task Book Section 5.7.2).

Scores candidate AgentProviders across 5 explicit multiplicative factors:
  Score = CapabilityMatch * EmpiricalReliability * Availability * CostEfficiency * RiskCompatibility
and persists a full explainable decision record to `decision_logs`.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field

from app.agents.base import AgentInterface
from app.agents.registry import AgentRegistry
from app.storage.repository import StateRepository


class CandidateScore(BaseModel):
    agent_id: str
    provider_kind: str = "agent_provider"
    capability_match: float
    empirical_reliability: float
    availability: float
    cost_efficiency: float
    risk_compatibility: float
    total_score: float
    reason: str = ""


class RoutingDecision(BaseModel):
    selected_agent_id: str | None = None
    provider_kind: str = "agent_provider"
    candidates: list[CandidateScore] = Field(default_factory=list)
    reason: str = ""


class AgentRouter:
    def __init__(self, registry: AgentRegistry, repo: StateRepository | None = None) -> None:
        self.registry = registry
        self.repo = repo

    async def _empirical_stats(self) -> dict[str, tuple[float, float]]:
        """Return {agent_id: (reliability_score, cost_efficiency_score)} from recent agent_executions."""
        if self.repo is None:
            return {}
        try:
            recent = await self.repo.list_executions(limit=100)
        except Exception:
            return {}

        by_agent: dict[str, dict[str, float]] = {}
        for ex in recent:
            aid = str(ex.get("agent_id") or "")
            if not aid:
                continue
            st = by_agent.setdefault(aid, {"ok": 0.0, "fail": 0.0, "wall_sum": 0.0, "n": 0.0})
            status = str(ex.get("status") or "")
            if status == "SUCCEEDED":
                st["ok"] += 1.0
            elif status in {"FAILED", "TIMEOUT", "ORPHANED"}:
                st["fail"] += 1.0
            wall = float((ex.get("resource_usage") or {}).get("wall_seconds", 0.0))
            if wall > 0:
                st["wall_sum"] += wall
                st["n"] += 1.0

        out: dict[str, tuple[float, float]] = {}
        for aid, s in by_agent.items():
            # Conservative Laplace prior (alpha=2, beta=1 -> 0.67 baseline before evidence)
            rel = (2.0 + s["ok"]) / (3.0 + s["ok"] + s["fail"])
            avg_wall = (s["wall_sum"] / s["n"]) if s["n"] > 0 else 30.0
            cost_eff = max(0.3, min(1.0, 1.0 / (1.0 + avg_wall / 120.0)))
            out[aid] = (round(rel, 4), round(cost_eff, 4))
        return out

    async def select(
        self,
        *,
        preferred_agent: str | None = None,
        required_capability: str | None = None,
        sandbox_mode: str = "read-only",
        exclude: set[str] | None = None,
        cycle_id: uuid.UUID | None = None,
        log_decision: bool = True,
    ) -> tuple[AgentInterface | None, RoutingDecision]:
        healthy_caps = self.registry.list_healthy(exclude=exclude)
        if not healthy_caps:
            dec = RoutingDecision(selected_agent_id=None, reason="No healthy enabled AgentProvider available")
            if log_decision and self.repo is not None:
                await self.repo.log_decision(
                    decision_type="execution_routing",
                    selected=None,
                    alternatives=[],
                    reason=dec.reason,
                    payload=dec.model_dump(mode="json"),
                    cycle_id=cycle_id,
                )
            return None, dec

        emp_stats = await self._empirical_stats()
        scored: list[CandidateScore] = []

        for cap in healthy_caps:
            # 1. CapabilityMatch
            if preferred_agent and cap.agent_id == preferred_agent:
                cap_match = 1.0
            elif required_capability and required_capability in cap.capabilities:
                cap_match = 0.90
            elif not required_capability or required_capability == "general_task":
                cap_match = 0.75
            else:
                cap_match = 0.45

            # 2. EmpiricalReliability & 4. CostEfficiency
            rel, cost_eff = emp_stats.get(cap.agent_id, (0.75, 0.80))

            # 3. Availability (penalize experimental agents slightly)
            avail = 0.70 if cap.experimental else 1.0

            # 5. RiskCompatibility
            if not cap.supported_modes or sandbox_mode in cap.supported_modes:
                risk_compat = 1.0
            else:
                risk_compat = 0.25

            total = round(cap_match * rel * avail * cost_eff * risk_compat, 5)
            scored.append(
                CandidateScore(
                    agent_id=cap.agent_id,
                    capability_match=cap_match,
                    empirical_reliability=rel,
                    availability=avail,
                    cost_efficiency=cost_eff,
                    risk_compatibility=risk_compat,
                    total_score=total,
                    reason=f"cap={cap_match} * rel={rel} * avail={avail} * cost={cost_eff} * risk={risk_compat}",
                )
            )

        scored.sort(key=lambda c: (-c.total_score, c.agent_id))
        winner = scored[0]
        decision = RoutingDecision(
            selected_agent_id=winner.agent_id,
            candidates=scored,
            reason=f"Selected AgentProvider '{winner.agent_id}' (score={winner.total_score}: {winner.reason})",
        )

        if log_decision and self.repo is not None:
            await self.repo.log_decision(
                decision_type="execution_routing",
                selected=winner.agent_id,
                alternatives=[c.agent_id for c in scored[1:]],
                reason=decision.reason,
                payload=decision.model_dump(mode="json"),
                cycle_id=cycle_uuid_or_none(cycle_id),
            )
        return self.registry.get(winner.agent_id), decision


def cycle_uuid_or_none(cycle_id: uuid.UUID | None) -> uuid.UUID | None:
    return cycle_id
