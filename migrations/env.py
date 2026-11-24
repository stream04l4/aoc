"""Alembic async migration environment with V5.0 per-instance PostgreSQL schema support."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import get_settings
from app.storage.database import validate_db_schema_name
from app.storage.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _get_url() -> str:
    return get_settings().database.url


def _get_schema() -> str:
    return validate_db_schema_name(get_settings().database.schema_name)


def run_migrations_offline() -> None:
    context.configure(
        url=_get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    is_pg = connection.dialect.name == "postgresql"
    schema_name = _get_schema()
    cfg_kwargs: dict = {"connection": connection, "target_metadata": target_metadata}
    if is_pg and schema_name != "public":
        cfg_kwargs["version_table_schema"] = schema_name
    context.configure(**cfg_kwargs)
    with context.begin_transaction():
        if is_pg and schema_name != "public":
            connection.execute(text(f'SET LOCAL search_path TO "{schema_name}", public'))
        context.run_migrations()


async def run_async_migrations() -> None:
    url = _get_url()
    schema_name = _get_schema()
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = url
    engine_kwargs: dict = {"prefix": "sqlalchemy.", "poolclass": pool.NullPool}
    if url.startswith("postgresql") and schema_name != "public":
        engine_kwargs["connect_args"] = {"server_settings": {"search_path": f"{schema_name},public"}}

    connectable = async_engine_from_config(section, **engine_kwargs)
    async with connectable.connect() as connection:
        if connection.dialect.name == "postgresql" and schema_name != "public":
            await connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema_name}"'))
            await connection.commit()
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
