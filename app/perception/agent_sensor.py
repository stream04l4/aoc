"""AgentSensor: observes external agent registry health and active/pending tasks."""

from __future__ import annotations

from typing import Any

from app.core.enums import TaskStatus
from app.perception.interfaces import Observation, SensorInterface, unavailable, utcnow
from app.storage.repository import StateRepository


class AgentSensor(SensorInterface):
    name = "agent"

    def __init__(self, repo: StateRepository) -> None:
        self.repo = repo

    async def observe(self) -> Observation:
        now = utcnow()
        try:
            agents = await self.repo.list_agents()
            active_rows = await self.repo.list_tasks(
                statuses=[
                    TaskStatus.PENDING.value,
                    TaskStatus.DISPATCHED.value,
                    TaskStatus.RUNNING.value,
                    TaskStatus.VERIFYING.value,
                    TaskStatus.REQUIRES_APPROVAL.value,
                ],
                limit=50,
            )
        except Exception as exc:
            return Observation(
                timestamp=now,
                available_resources={"agents": unavailable(f"agent query failed: {exc}")},
                confidence=0.0,
            )

        enabled = [a for a in agents if a.get("enabled")]
        healthy = [a for a in enabled if (a.get("probe") or {}).get("ok", False)]
        capabilities: set[str] = set()
        for a in healthy:
            for c in a.get("capabilities") or []:
                capabilities.add(str(c))

        agent_summary: dict[str, Any] = {
            "registered_count": len(agents),
            "enabled_count": len(enabled),
            "healthy_count": len(healthy),
            "capabilities": sorted(capabilities),
            "agents": [
                {
                    "agent_id": a["agent_id"],
                    "enabled": a["enabled"],
                    "experimental": a.get("experimental", False),
                    "healthy": bool((a.get("probe") or {}).get("ok", False)),
                    "capabilities": a.get("capabilities", []),
                }
                for a in agents
            ],
        }

        active_tasks = [
            {
                "task_id": t["task_id"],
                "objective": t["objective"],
                "assigned_agent": t["assigned_agent"],
                "status": t["status"],
                "attempt": t["attempt"],
                "created_at": t["created_at"].isoformat() if hasattr(t["created_at"], "isoformat") else str(t["created_at"]),
            }
            for t in active_rows
        ]

        return Observation(
            timestamp=now,
            available_resources={"agents": agent_summary},
            active_tasks=active_tasks,
            confidence=1.0,
        )
