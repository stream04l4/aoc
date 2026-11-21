"""DigitalWorldSensor protocol and WorldEvent schemas (V2.0 P1-5)."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.perception.interfaces import utcnow


def compute_world_event_hash(source_id: str, title: str, content: str, link: str = "") -> str:
    canonical = "|".join(
        [
            source_id.strip().lower(),
            title.strip().lower(),
            link.strip().lower(),
            content.strip()[:2000],
        ]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class WorldSourceConfig(BaseModel):
    source_id: str
    sensor_type: str  # rss | git | web | system
    name: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    reliability_score: float = 0.7
    poll_interval_seconds: int = 900
    enabled: bool = True


class WorldEvent(BaseModel):
    source_id: str
    event_type: str  # rss_item | git_commit | git_tag | web_change | system_event
    title: str
    content: str
    content_hash: str = ""
    source_reliability: float = 0.7
    importance_score: float = 0.0
    observed_at: datetime = Field(default_factory=utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def ensure_hash(self) -> str:
        if not self.content_hash:
            link = str(self.metadata.get("link") or self.metadata.get("url") or self.metadata.get("commit_sha") or "")
            self.content_hash = compute_world_event_hash(self.source_id, self.title, self.content, link=link)
        return self.content_hash


class SensorPollResult(BaseModel):
    source_id: str
    events: list[WorldEvent] = Field(default_factory=list)
    new_cursor: dict[str, Any] = Field(default_factory=dict)
    not_modified: bool = False
    error: str | None = None


class DigitalWorldSensor(ABC):
    sensor_type: str = "generic"

    @abstractmethod
    async def poll(self, source: WorldSourceConfig, cursor: dict[str, Any]) -> SensorPollResult:
        """Poll a single external source using its persisted checkpoint cursor."""
