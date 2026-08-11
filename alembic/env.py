"""Async Alembic environment for the empty WS-0 baseline schema."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from app.build.downgrade_preflight import DowngradePreflightError, check_sqlalchemy_connection
from app.core.config import load_settings
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config
if config.config_file_name is not None and config.get_section("loggers") is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def _is_downgrade_command() -> bool:
    command_options = getattr(config, "cmd_opts", None)
    command = getattr(command_options, "cmd", None)
    if not isinstance(command, tuple) or not command or not callable(command[0]):
        return False
    return getattr(command[0], "__name__", "") == "downgrade"


def run_migrations_offline() -> None:
    if _is_downgrade_command():
        raise DowngradePreflightError(
            "offline downgrade is prohibited; use scripts/ws3_downgrade.py against the live database"
        )
    context.configure(
        url=load_settings().database_url_value(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        if _is_downgrade_command():
            check_sqlalchemy_connection(connection)
        context.run_migrations()


async def run_async_migrations() -> None:
    settings = load_settings()
    connectable = async_engine_from_config(
        {"sqlalchemy.url": settings.database_url_value()},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
