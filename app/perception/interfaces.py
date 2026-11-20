"""Perception interfaces (task book section 5)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator

UNAVAILABLE = "unavailable"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def unavailable(reason: str) -> dict[str, Any]:
    """Explicit marker for data that could not be collected. Never interpret as 'normal'."""
    return {"status": UNAVAILABLE, "reason": reason}


def is_unavailable(value: Any) -> bool:
    return value is None or (isinstance(value, dict) and value.get("status") == UNAVAILABLE)


class Observation(BaseModel):
    timestamp: datetime = Field(default_factory=utcnow)

    system_state: dict[str, Any] = Field(default_factory=dict)
    environment_state: dict[str, Any] = Field(default_factory=dict)

    available_resources: dict[str, Any] = Field(default_factory=dict)

    active_tasks: list[dict[str, Any]] = Field(default_factory=list)

    external_events: list[dict[str, Any]] = Field(default_factory=list)

    confidence: float = 1.0

    # Per-sensor bookkeeping: {"system": {"ok": true, "error": null, "timestamp": ...}}
    sensor_status: dict[str, Any] = Field(default_factory=dict)

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, v: float) -> float:
        return max(0.0, min(1.0, float(v)))


class SensorInterface(ABC):
    """A sensor contributes a partial Observation. Sensors are independently registrable."""

    name: str = "sensor"

    @abstractmethod
    async def observe(self) -> Observation:
        ...


class PerceptionInterface(ABC):
    """What the rest of the organism sees of the perception module."""

    @abstractmethod
    def register(self, sensor: SensorInterface) -> None: ...

    @abstractmethod
    def unregister(self, name: str) -> bool: ...

    @abstractmethod
    async def perceive(self) -> Observation: ...
