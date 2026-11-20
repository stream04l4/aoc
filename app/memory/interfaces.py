"""Long-term Memory Interfaces (task book section 8)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.core.enums import MemoryType
from app.drives.interfaces import clamp01
from app.perception.interfaces import utcnow


class MemoryItem(BaseModel):
    memory_id: str | None = None
    external_id: str | None = None
    memory_type: str = MemoryType.EPISODIC.value
    title: str = ""
    content: str
    source: str = "aoc"
    importance: float = 0.5
    tags: list[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def normalized_importance(self) -> float:
        return clamp01(self.importance)


class MemoryWriteResult(BaseModel):
    stored_index_id: str | None = None
    external_id: str | None = None
    synced_to_letta: bool = False
    deduplicated: bool = False
    error: str | None = None


class MemoryInterface(ABC):
    @abstractmethod
    async def health(self) -> bool: ...

    @abstractmethod
    async def ensure_agent(self, organism_name: str, identity_summary: str) -> str | None: ...

    @abstractmethod
    async def store(self, item: MemoryItem) -> MemoryWriteResult: ...

    @abstractmethod
    async def search(
        self,
        query: str = "",
        memory_types: list[str] | None = None,
        limit: int = 10,
    ) -> list[MemoryItem]: ...

    @abstractmethod
    async def update_core_block(self, label: str, value: str) -> bool: ...

    @abstractmethod
    async def get_core_blocks(self) -> dict[str, str]: ...
