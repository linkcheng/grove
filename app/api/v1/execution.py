"""Tenant-aware WS-2 submit and read-only query routes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.context import ActiveTenantContext, require_active_tenant_context
from app.db.session import get_read_db, get_write_db
from app.schemas.execution import CommandReceipt, RunHandle, RunQuery, SubmitExecution
from app.schemas.response import ApiResponse, ok
from app.services import execution

router = APIRouter(tags=["execution"])
Context = Annotated[ActiveTenantContext, Depends(require_active_tenant_context)]


async def _write_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async for session in get_write_db(factory):
        yield session


async def _read_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async for session in get_read_db(factory):
        yield session


WriteSession = Annotated[AsyncSession, Depends(_write_session)]
ReadSession = Annotated[AsyncSession, Depends(_read_session)]


@router.post("/executions/submit", response_model=ApiResponse[RunHandle])
@router.post("/execution/submit", response_model=ApiResponse[RunHandle], include_in_schema=False)
async def submit(request: SubmitExecution, context: Context, session: WriteSession) -> ApiResponse[RunHandle]:
    return ok(await execution.submit(session, context, request))


@router.get("/executions/runs/{run_id}", response_model=ApiResponse[RunQuery])
@router.get("/execution/runs/{run_id}", response_model=ApiResponse[RunQuery], include_in_schema=False)
async def query_run(run_id: UUID, context: Context, session: ReadSession) -> ApiResponse[RunQuery]:
    return ok(await execution.query_run(session, context, run_id))


@router.get("/executions/commands/{command_id}", response_model=ApiResponse[CommandReceipt])
@router.get("/execution/commands/{command_id}", response_model=ApiResponse[CommandReceipt], include_in_schema=False)
async def query_command(command_id: UUID, context: Context, session: ReadSession) -> ApiResponse[CommandReceipt]:
    return ok(await execution.query_command(session, context, command_id))
