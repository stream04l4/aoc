"""Shared pytest fixtures for AOC unit and integration tests."""

from __future__ import annotations

import pytest
import pytest_asyncio

from app.storage.database import Database
from app.storage.models import Base
from app.storage.repository import StateRepository


@pytest_asyncio.fixture
async def db(tmp_path) -> Database:
    """In-memory / tmp SQLite async DB with full schema for fast unit tests."""
    db_file = tmp_path / "test_aoc.sqlite"
    database = Database(f"sqlite+aiosqlite:///{db_file}")
    async with database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield database
    finally:
        await database.dispose()


@pytest_asyncio.fixture
async def repo(db: Database) -> StateRepository:
    return StateRepository(db)
