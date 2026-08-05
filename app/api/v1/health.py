"""Liveness and readiness endpoints only for WS-0."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request, Response
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.db.session import check_database
from app.schemas.health import HealthData
from app.schemas.response import ApiResponse, fail, ok

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", response_model=ApiResponse[HealthData])
async def liveness() -> ApiResponse[HealthData]:
    return ok(HealthData(status="live"))


@router.get("/ready", response_model=ApiResponse[HealthData])
async def readiness(request: Request, response: Response) -> ApiResponse[HealthData]:
    settings: Settings = request.app.state.settings
    engine: AsyncEngine = request.app.state.db_engine
    available = await check_database(engine, settings.readiness_timeout_seconds)
    if not available:
        response.status_code = 503
        return cast(
            ApiResponse[HealthData],
            fail(50301, "database unavailable", error_code="DependencyUnavailable", retry_after=1),
        )
    return ok(HealthData(status="ready"))
