"""Async PostgreSQL session and health-check adapter."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.trace import current_trace_id


def create_engine(settings: Settings) -> AsyncEngine:
    engine = create_async_engine(
        settings.database_url_value(),
        pool_pre_ping=True,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout=settings.database_pool_timeout_seconds,
        connect_args={"application_name": "grove-ws0"},
    )

    @event.listens_for(engine.sync_engine, "before_cursor_execute", retval=True)
    def add_trace_comment(
        _conn: Any,
        _cursor: Any,
        statement: str,
        parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> tuple[str, Any]:
        trace_id = current_trace_id()
        if trace_id and not statement.lstrip().startswith("/* trace_id="):
            statement = f"/* trace_id={trace_id} */ {statement}"
        return statement, parameters

    return engine


async def check_database(engine: AsyncEngine, timeout_seconds: float) -> bool:
    """Run a bounded probe on the application-owned engine.

    Connection failures are an expected readiness result. Other exceptions are
    intentionally allowed to propagate so programming defects are not reported
    as database outages.
    """

    try:
        async with asyncio.timeout(timeout_seconds):
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        return True
    except (SQLAlchemyError, OSError, TimeoutError):
        return False


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create a session factory for the engine owned by the app lifespan."""

    return async_sessionmaker(engine, expire_on_commit=False)


async def get_read_db(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        try:
            yield session
        finally:
            await session.rollback()


async def get_write_db(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
