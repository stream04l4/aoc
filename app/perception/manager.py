"""SensorManager: registers/unregisters sensors and merges observations with fault isolation."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from app.observability.logging import get_logger
from app.observability.metrics import SENSOR_FAILURES
from app.perception.interfaces import Observation, PerceptionInterface, SensorInterface, unavailable, utcnow
from app.storage.repository import StateRepository

logger = get_logger("aoc.perception")


class SensorManager(PerceptionInterface):
    def __init__(self, repo: StateRepository | None = None, sensor_timeout_seconds: float = 15.0) -> None:
        self._sensors: dict[str, SensorInterface] = {}
        self.repo = repo
        self.sensor_timeout_seconds = sensor_timeout_seconds

    def register(self, sensor: SensorInterface) -> None:
        self._sensors[sensor.name] = sensor

    def unregister(self, name: str) -> bool:
        return self._sensors.pop(name, None) is not None

    def list_sensors(self) -> list[str]:
        return list(self._sensors.keys())

    async def perceive(
        self,
        cycle_id: uuid.UUID | None = None,
        phase: str = "pre",
        persist: bool = False,
    ) -> Observation:
        now = utcnow()
        if not self._sensors:
            obs = Observation(
                timestamp=now,
                system_state={"sensors": unavailable("no sensors registered")},
                confidence=0.0,
            )
            if persist and self.repo is not None:
                await self.repo.save_observation(obs, cycle_id=cycle_id, phase=phase)
            return obs

        async def _run_one(name: str, sensor: SensorInterface) -> tuple[str, Observation | None, str | None]:
            try:
                res = await asyncio.wait_for(sensor.observe(), timeout=self.sensor_timeout_seconds)
                return name, res, None
            except Exception as exc:
                SENSOR_FAILURES.labels(sensor=name).inc()
                logger.warning("perception.sensor_failed", sensor=name, error=str(exc))
                return name, None, str(exc)

        results = await asyncio.gather(*(_run_one(n, s) for n, s in self._sensors.items()))

        system_state: dict[str, Any] = {}
        environment_state: dict[str, Any] = {}
        available_resources: dict[str, Any] = {}
        active_tasks: list[dict[str, Any]] = []
        external_events: list[dict[str, Any]] = []
        sensor_status: dict[str, Any] = {}
        confidences: list[float] = []

        for name, sub_obs, err in results:
            if err is not None or sub_obs is None:
                sensor_status[name] = {
                    "ok": False,
                    "error": err or "empty observation",
                    "timestamp": now.isoformat(),
                }
                # Explicitly mark failed sensor output as unavailable
                system_state.setdefault(f"sensor_{name}", unavailable(err or "sensor failed"))
                confidences.append(0.0)
                continue

            sensor_status[name] = {
                "ok": True,
                "error": None,
                "timestamp": sub_obs.timestamp.isoformat(),
                "confidence": sub_obs.confidence,
            }
            confidences.append(sub_obs.confidence)
            system_state.update(sub_obs.system_state)
            environment_state.update(sub_obs.environment_state)
            available_resources.update(sub_obs.available_resources)
            active_tasks.extend(sub_obs.active_tasks)
            external_events.extend(sub_obs.external_events)

        merged_conf = sum(confidences) / len(confidences) if confidences else 0.0
        merged = Observation(
            timestamp=now,
            system_state=system_state,
            environment_state=environment_state,
            available_resources=available_resources,
            active_tasks=active_tasks,
            external_events=external_events,
            confidence=merged_conf,
            sensor_status=sensor_status,
        )

        if persist and self.repo is not None:
            await self.repo.save_observation(merged, cycle_id=cycle_id, phase=phase)

        return merged
