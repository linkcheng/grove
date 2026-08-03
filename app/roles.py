"""Role entrypoint checks; no role runs a fake infinite loop."""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.config import ConfigurationError, Role, Settings, load_settings
from app.db.session import check_database, create_engine


def database_available(settings: Settings) -> bool:
    """Verify the configured role credential against the real PostgreSQL service."""

    async def probe() -> bool:
        engine = create_engine(settings)
        try:
            return await check_database(engine, settings.readiness_timeout_seconds)
        finally:
            await engine.dispose()

    return asyncio.run(probe())


def run_role_self_check(settings: Settings | None = None) -> dict[str, Any]:
    active = settings or load_settings()
    if active.role not in set(Role):
        raise ValueError(f"unsupported role: {active.role}")
    if not database_available(active):
        raise ConfigurationError(f"database credential check failed for role={active.role.value}")
    return {
        "role": active.role.value,
        "status": "configured",
        "database": "postgresql",
        "database_status": "connected",
        "capabilities": {"dbos": "disabled"},
    }
