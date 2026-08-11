"""Tenant-aware Observation API: Run Inspect, event query and UI SSE stream."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.context import ActiveTenantContext, require_active_tenant_context
from app.core.telemetry import default_recorder
from app.db.session import get_read_db
from app.schemas.observation import EventListResponse
from app.schemas.response import ApiResponse, ok
from app.services import observation

router = APIRouter(prefix="/observations", tags=["observation"])
Context = Annotated[ActiveTenantContext, Depends(require_active_tenant_context)]


async def _read_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async for session in get_read_db(factory):
        yield session


ReadSession = Annotated[AsyncSession, Depends(_read_session)]


@router.get("/runs/{run_id}/inspect", response_model=ApiResponse[dict[str, Any]])
async def inspect_run(run_id: UUID, context: Context, session: ReadSession) -> ApiResponse[dict[str, Any]]:
    view = await observation.inspect(session, context, run_id)
    return ok(view.model_dump(mode="json"))


@router.get("/runs/{run_id}/events", response_model=ApiResponse[EventListResponse])
async def list_events(
    run_id: UUID,
    context: Context,
    session: ReadSession,
    after_run_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ApiResponse[EventListResponse]:
    events, next_cursor = await observation.list_runtime_events(session, context, run_id, after_run_seq, limit)
    return ok(EventListResponse(events=events, next_cursor=next_cursor))


@router.get("/runs/{run_id}/ui", response_model=ApiResponse[EventListResponse])
async def list_ui_events(
    run_id: UUID,
    context: Context,
    session: ReadSession,
    after_projection_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ApiResponse[EventListResponse]:
    events, next_cursor = await observation.list_ui_events(session, context, run_id, after_projection_seq, limit)
    return ok(EventListResponse(events=events, next_cursor=next_cursor))


@router.get("/runs/{run_id}/ui/snapshot", response_model=ApiResponse[dict[str, Any]])
async def get_ui_snapshot(run_id: UUID, context: Context, session: ReadSession) -> ApiResponse[dict[str, Any]]:
    state = await observation.snapshot(session, context, run_id)
    return ok(state.model_dump(mode="json"))


@router.get("/runs/{run_id}/interactions", response_model=ApiResponse[dict[str, Any]])
async def list_interactions(run_id: UUID, context: Context, session: ReadSession) -> ApiResponse[dict[str, Any]]:
    state = await observation.snapshot(session, context, run_id)
    return ok(
        {
            "items": [item.model_dump(mode="json") for item in state.interactions],
            "completeness": state.completeness,
            "next_cursor": state.last_projection_seq,
        }
    )


@router.get("/runs/{run_id}/ui/stream")
async def stream_ui(
    run_id: UUID,
    context: Context,
    request: Request,
    after_projection_seq: Annotated[int, Query(ge=0)] = 0,
) -> StreamingResponse:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory

    async def event_source() -> AsyncIterator[bytes]:
        labels = {"role": "api", "operation": "sse_stream", "outcome": "active"}
        default_recorder().record_metric("sse.connections", value=1, labels=labels, kind="up_down_counter")
        try:
            async for view in observation.stream_ui_events(
                factory,
                context,
                run_id,
                after_projection_seq,
                coalescer=request.app.state.sse_backfill_coalescer,
            ):
                payload = json.dumps(view.model_dump(mode="json"), separators=(",", ":"))
                yield f"id: {view.projection_seq}\ndata: {payload}\n\n".encode()
        finally:
            default_recorder().record_metric("sse.connections", value=-1, labels=labels, kind="up_down_counter")

    return StreamingResponse(event_source(), media_type="text/event-stream")
