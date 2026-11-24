"""Async database engine and session factory with V5.0 per-instance schema isolation."""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

_VALID_SCHEMA_RE = re.compile(r"^(?:public|aoc_inst_[a-z0-9_]{4,32})$")


def validate_db_schema_name(schema_name: str | None) -> str:
    """Validate PostgreSQL schema name against a strict allowlist pattern (SQL-injection proof)."""
    name = (schema_name or "public").strip()
    if not _VALID_SCHEMA_RE.fullmatch(name):
        raise ValueError(
            f"Invalid database schema_name '{name}': must be 'public' or match 'aoc_inst_[a-z0-9_]{{4,32}}'"
        )
    return name


class Database:
    def __init__(self, url: str, echo: bool = False, schema_name: str = "public") -> None:
        self.url = url
        self.schema_name = validate_db_schema_name(schema_name)
        kwargs: dict[str, Any] = {"echo": echo, "future": True}
        if url.startswith("postgresql"):
            kwargs.update(pool_size=10, max_overflow=5, pool_pre_ping=True)
            if self.schema_name != "public":
                kwargs["connect_args"] = {"server_settings": {"search_path": f"{self.schema_name},public"}}
        elif url.startswith("sqlite"):
            # Map explicit schema="public" shared control-plane tables onto default SQLite schema.
            kwargs["execution_options"] = {"schema_translate_map": {"public": None}}
        self.engine: AsyncEngine = create_async_engine(url, **kwargs)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Transactional scope: commit on success, rollback on error."""
        async with self.sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def ping(self) -> bool:
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    async def dispose(self) -> None:
        await self.engine.dispose()
