"""Role entrypoint checks; no role runs a fake infinite loop."""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.config import ConfigurationError, Role, Settings, load_settings
from app.db.session import check_database, create_engine, session_factory


async def _projection_readiness(settings: Settings) -> dict[str, Any]:
    """Projection/Reconciliation domain health: backlog, dead-letter, unknown schema.

    Independent of the API readiness so a projector backlog or lag does not
    mark the API unready (WS-4 Exit Invariant 8).
    """
    from sqlalchemy import text

    engine = create_engine(settings)
    factory = session_factory(engine)
    try:
        async with factory() as session:
            row = (await session.execute(text("SELECT * FROM grove_observation_health()"))).one_or_none()
        if row is None:
            return {"backlog": 0, "dead_letter": 0, "status": "ready"}
        return {
            "backlog": int(row[0]),
            "dead_letter": int(row[1]),
            "status": "ready" if int(row[0]) == 0 else "catching_up",
        }
    finally:
        await engine.dispose()


def database_available(settings: Settings) -> bool:
    """Verify the configured role credential against the real PostgreSQL service."""

    async def probe() -> bool:
        engine = create_engine(settings)
        try:
            return await check_database(engine, settings.readiness_timeout_seconds)
        finally:
            await engine.dispose()

    return asyncio.run(probe())


def projection_available(settings: Settings) -> dict[str, Any]:
    """Return projection domain health; mockable seam for unit tests."""

    return asyncio.run(_projection_readiness(settings))


def run_role_self_check(settings: Settings | None = None) -> dict[str, Any]:
    active = settings or load_settings()
    if active.role not in set(Role):
        raise ValueError(f"unsupported role: {active.role}")
    if not database_available(active):
        raise ConfigurationError(f"database credential check failed for role={active.role.value}")
    result: dict[str, Any] = {
        "role": active.role.value,
        "status": "configured",
        "database": "postgresql",
        "database_status": "connected",
        "capabilities": {"dbos": "disabled"},
    }
    if active.role is Role.PROJECTION_RECONCILIATION:
        result["projection"] = projection_available(active)
    if active.role is Role.RUNTIME_WORKER:
        result["worker"] = {
            "worker_id": active.worker_id,
            "tenant_id": active.worker_tenant_id,
            "runtime_build_hash": active.runtime_build_hash,
            "claim_protocol": "advisory_fence_lease",
        }
    return result
