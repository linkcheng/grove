"""Tenant-aware Observation API service: inspect, query and SSE stream.

All reads are RLS-scoped to the Active Tenant Context.  The service never
touches WS-3 authority state and never returns internal checkpoint, thread or
payload-body detail beyond the safe public view.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import UTC, datetime
from functools import partial
from time import perf_counter
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.context import ActiveTenantContext
from app.contracts.canonical import CanonicalModel, ContractMeta, ProjectionSourceRef, UIProjectionEvent
from app.core.telemetry import default_recorder, record_operation
from app.observation.facts import (
    RUN_LIFECYCLE_SCHEMA_REF,
    TERMINAL_RUN_STATUSES,
    ObservationCompleteness,
    PublicRunStatus,
    RunInspectView,
    RuntimeEventView,
    UIProjectionEventView,
    ui_payload_adapter,
)
from app.observation.reducer import RunViewState, reduce_run_view
from app.repositories.execution import authorize_owned_run_query

_STREAM_POLL_INTERVAL = 0.5
_STREAM_MAX_EVENTS = 1000
_STREAM_MAX_COALESCED_READS = 1024

SSEReadKey = tuple[str, str, str, UUID, int]
SSEBackfillResult = tuple[bool, list[Any]]


class SSEBackfillCoalescer:
    """Single-flight identical concurrent SSE reads without caching results.

    The key contains the complete authenticated tenant/principal identity,
    target run and durable cursor.  Only callers that overlap while the exact
    same live authorization/read task is still running share it; completed
    results are removed immediately, so the next stream iteration always
    re-authorizes against PostgreSQL.
    """

    def __init__(self, *, max_inflight: int = _STREAM_MAX_COALESCED_READS) -> None:
        if type(max_inflight) is not int or max_inflight <= 0:
            raise ValueError("max_inflight must be a positive int")
        self._max_inflight = max_inflight
        self._tasks: dict[SSEReadKey, asyncio.Task[SSEBackfillResult]] = {}

    async def run(
        self,
        key: SSEReadKey,
        loader: Callable[[], Coroutine[Any, Any, SSEBackfillResult]],
    ) -> SSEBackfillResult:
        task = self._tasks.get(key)
        if task is None:
            if len(self._tasks) >= self._max_inflight:
                return await loader()
            task = asyncio.create_task(loader())
            self._tasks[key] = task
            task.add_done_callback(partial(self._remove, key))
        return await asyncio.shield(task)

    def _remove(self, key: SSEReadKey, task: asyncio.Task[SSEBackfillResult]) -> None:
        if self._tasks.get(key) is task:
            self._tasks.pop(key, None)


async def _scope(session: AsyncSession, context: ActiveTenantContext) -> None:
    await session.execute(text("SET LOCAL statement_timeout = '5000ms'"))
    await session.execute(text("SELECT set_config('grove.tenant_id', :t, true)"), {"t": context.tenant_id})


async def _authorize_run(session: AsyncSession, context: ActiveTenantContext, run_id: UUID) -> bool:
    """Re-authorize the current principal and prove ownership without leaking existence."""

    await _scope(session, context)
    return await authorize_owned_run_query(session, context, run_id)


def _run_not_found(context: ActiveTenantContext, run_id: UUID) -> RunInspectView:
    return RunInspectView(
        run_id=run_id,
        tenant_id=context.tenant_id,
        completeness="unavailable",
        as_of=datetime.now(UTC),
    )


async def inspect(session: AsyncSession, context: ActiveTenantContext, run_id: UUID) -> RunInspectView:
    """Return the safe, public Run Inspect view with projection completeness."""
    if not await _authorize_run(session, context, run_id):
        return _run_not_found(context, run_id)
    run = (
        await session.execute(
            text("SELECT status, revision FROM agent_run WHERE tenant_id = :t AND run_id = :r"),
            {"t": context.tenant_id, "r": run_id},
        )
    ).one_or_none()
    if run is None:
        return _run_not_found(context, run_id)
    status: PublicRunStatus = run[0]
    revision = run[1]
    last_run_seq = (
        await session.execute(
            text("SELECT COALESCE(MAX(run_seq), 0) FROM runtime_event WHERE tenant_id = :t AND run_id = :r"),
            {"t": context.tenant_id, "r": run_id},
        )
    ).scalar_one()
    last_projection_seq = (
        await session.execute(
            text(
                "SELECT COALESCE(MAX(projection_seq), 0) FROM ui_projection_event "
                "WHERE tenant_id = :t AND target_ref = :r"
            ),
            {"t": context.tenant_id, "r": run_id},
        )
    ).scalar_one()
    # Completeness is decided by comparing projectable facts to projected
    # facts, not raw run_seq vs projection_seq.  The runtime emits both
    # lifecycle and audit-only (node_executed) events per stage, but only
    # lifecycle facts are projected 1:1, so a seq comparison would make every
    # terminal run look permanently lagged.
    lifecycle_event_count = (
        await session.execute(
            text(
                "SELECT count(*) FROM runtime_event "
                "WHERE tenant_id = :t AND run_id = :r AND payload_schema_ref = :schema"
            ),
            {"t": context.tenant_id, "r": run_id, "schema": RUN_LIFECYCLE_SCHEMA_REF},
        )
    ).scalar_one()
    projected_count = (
        await session.execute(
            text("SELECT count(*) FROM ui_projection_event WHERE tenant_id = :t AND target_ref = :r"),
            {"t": context.tenant_id, "r": run_id},
        )
    ).scalar_one()
    unknown = (
        await session.execute(
            text("SELECT count(*) FROM runtime_event_dead_letter WHERE tenant_id = :t AND run_id = :r"),
            {"t": context.tenant_id, "r": run_id},
        )
    ).scalar_one()
    completeness = _completeness(status, lifecycle_event_count, projected_count, unknown)
    return RunInspectView(
        run_id=run_id,
        tenant_id=context.tenant_id,
        status=status,
        run_revision=revision,
        completeness=completeness,
        last_run_seq=last_run_seq,
        last_projection_seq=last_projection_seq,
        unknown_schema_count=unknown,
        as_of=datetime.now(UTC),
    )


def _completeness(
    status: PublicRunStatus, lifecycle_event_count: int, projected_count: int, unknown: int
) -> ObservationCompleteness:
    if unknown > 0:
        return "partial"
    terminal = status in TERMINAL_RUN_STATUSES
    # Projection lag: fewer projected UI events than projectable lifecycle
    # facts.  Counts are compared (not seqs) because audit-only runtime events
    # are intentionally never projected.
    if projected_count < lifecycle_event_count:
        return "partial"
    if terminal:
        return "complete"
    return "partial"


async def list_runtime_events(
    session: AsyncSession,
    context: ActiveTenantContext,
    run_id: UUID,
    after_run_seq: int,
    limit: int,
) -> tuple[list[RuntimeEventView], int]:
    if not await _authorize_run(session, context, run_id):
        return [], after_run_seq
    rows = (
        await session.execute(
            text(
                "SELECT event_id, run_id, run_seq, event_type, source, source_event_id, "
                "payload_schema_ref, payload, occurred_at "
                "FROM runtime_event WHERE tenant_id = :t AND run_id = :r AND run_seq > :after "
                "ORDER BY run_seq LIMIT :limit"
            ),
            {"t": context.tenant_id, "r": run_id, "after": after_run_seq, "limit": limit},
        )
    ).fetchall()
    events = [
        RuntimeEventView(
            event_id=row[0],
            run_id=row[1],
            run_seq=row[2],
            event_type=row[3],
            source=row[4],
            source_event_id=row[5],
            payload_schema_ref=row[6],
            payload=row[7],
            occurred_at=row[8],
        )
        for row in rows
    ]
    next_cursor = events[-1].run_seq if events else after_run_seq
    return events, next_cursor


async def list_ui_events(
    session: AsyncSession,
    context: ActiveTenantContext,
    run_id: UUID,
    after_projection_seq: int,
    limit: int,
) -> tuple[list[UIProjectionEventView], int]:
    if not await _authorize_run(session, context, run_id):
        return [], after_projection_seq
    rows = (
        await session.execute(
            text(
                "SELECT event_id, target_ref, projection_seq, payload_schema_ref, payload, projected_at "
                "FROM ui_projection_event WHERE tenant_id = :t AND target_ref = :r "
                "AND projection_seq > :after ORDER BY projection_seq LIMIT :limit"
            ),
            {"t": context.tenant_id, "r": run_id, "after": after_projection_seq, "limit": limit},
        )
    ).fetchall()
    events = [
        UIProjectionEventView(
            event_id=row[0],
            target_ref=row[1],
            projection_seq=row[2],
            payload_schema_ref=row[3],
            payload=row[4],
            projected_at=row[5],
        )
        for row in rows
    ]
    next_cursor = events[-1].projection_seq if events else after_projection_seq
    return events, next_cursor


async def snapshot(
    session: AsyncSession,
    context: ActiveTenantContext,
    run_id: UUID,
) -> RunViewState:
    """Rebuild the bounded headless UI snapshot from its persisted projection stream."""

    if not await _authorize_run(session, context, run_id):
        return RunViewState(tenant_id=context.tenant_id, run_id=run_id, completeness="unavailable")
    rows = (
        await session.execute(
            text(
                "SELECT event_id, target_kind, target_ref, projection_seq, contract_version, "
                "correlation_id, causation_id, trace_id, payload_schema_ref, payload, source_refs, projected_at "
                "FROM ui_projection_event WHERE tenant_id = :t AND target_ref = :r "
                "ORDER BY projection_seq LIMIT 1001"
            ),
            {"t": context.tenant_id, "r": run_id},
        )
    ).fetchall()
    truncated = len(rows) > 1000
    events: list[UIProjectionEvent[CanonicalModel]] = []
    for row in rows[:1000]:
        payload = ui_payload_adapter().validate_python(row[9])
        events.append(
            UIProjectionEvent[CanonicalModel](
                meta=ContractMeta(
                    contract_name="ui.projection",
                    contract_version=row[4],
                    message_id=row[0],
                    tenant_id=context.tenant_id,
                    correlation_id=row[5],
                    causation_id=row[6],
                    trace_id=row[7],
                ),
                event_id=row[0],
                target_kind=row[1],
                target_ref=row[2],
                projection_seq=row[3],
                payload_schema_ref=row[8],
                payload=payload,
                source_refs=tuple(ProjectionSourceRef.model_validate(item) for item in row[10]),
                projected_at=row[11],
            )
        )
    state = reduce_run_view(events)
    if truncated:
        return state.model_copy(update={"completeness": "partial"})
    return state


async def stream_ui_events(
    session_factory: async_sessionmaker[AsyncSession],
    context: ActiveTenantContext,
    run_id: UUID,
    after_projection_seq: int,
    *,
    coalescer: SSEBackfillCoalescer | None = None,
) -> AsyncIterator[UIProjectionEventView]:
    """Snapshot -> backfill -> bounded realtime tail using a durable cursor.

    The validated Active Tenant Context is carried through the stream rather
    than reduced to a bare tenant id, and each iteration re-establishes the
    tenant RLS scope from it so every read is re-authorized against the
    current principal's tenant rather than trusting a one-time scope.
    """
    cursor = after_projection_seq
    emitted = 0
    if cursor > 0:
        default_recorder().record_metric(
            "sse.event.count",
            value=1,
            labels={"role": "api", "operation": "sse_stream", "outcome": "reconnect"},
            kind="counter",
        )
    while emitted < _STREAM_MAX_EVENTS:
        backfill_started = perf_counter()
        loader = partial(_load_stream_backfill, session_factory, context, run_id, cursor)
        if coalescer is None:
            authorized, rows = await loader()
        else:
            key: SSEReadKey = (
                context.tenant_id,
                context.principal.principal_id,
                context.principal.kind.value,
                run_id,
                cursor,
            )
            authorized, rows = await coalescer.run(key, loader)
        if not authorized:
            return
        record_operation(
            "sse.backfill",
            duration_ms=float((perf_counter() - backfill_started) * 1000),
            role="api",
            operation="sse_backfill",
            outcome="ok",
        )
        advanced = False
        for row in rows:
            if row[2] != cursor + 1:
                default_recorder().record_metric(
                    "sse.event.count",
                    value=1,
                    labels={"role": "api", "operation": "sse_stream", "outcome": "gap"},
                    kind="counter",
                )
                break
            view = UIProjectionEventView(
                event_id=row[0],
                target_ref=row[1],
                projection_seq=row[2],
                payload_schema_ref=row[3],
                payload=row[4],
                projected_at=row[5],
            )
            cursor = row[2]
            advanced = True
            emitted += 1
            delivery_seconds = max(0.0, (datetime.now(UTC) - view.projected_at).total_seconds())
            default_recorder().record_metric(
                "sse.delivery.seconds",
                value=delivery_seconds,
                labels={"role": "api", "operation": "sse_stream", "outcome": "delivered"},
                kind="histogram",
            )
            default_recorder().record_metric(
                "sse.event.count",
                value=1,
                labels={"role": "api", "operation": "sse_stream", "outcome": "delivered"},
                kind="counter",
            )
            yield view
        if not advanced:
            await asyncio.sleep(_STREAM_POLL_INTERVAL)


async def _load_stream_backfill(
    session_factory: async_sessionmaker[AsyncSession],
    context: ActiveTenantContext,
    run_id: UUID,
    cursor: int,
) -> SSEBackfillResult:
    async with session_factory() as session:
        if not await _authorize_run(session, context, run_id):
            return False, []
        rows = (
            await session.execute(
                text(
                    "SELECT event_id, target_ref, projection_seq, payload_schema_ref, payload, projected_at "
                    "FROM ui_projection_event WHERE tenant_id = :t AND target_ref = :r "
                    "AND projection_seq > :c ORDER BY projection_seq LIMIT 50"
                ),
                {"t": context.tenant_id, "r": run_id, "c": cursor},
            )
        ).fetchall()
        return True, list(rows)


__all__ = [
    "SSEBackfillCoalescer",
    "inspect",
    "list_runtime_events",
    "list_ui_events",
    "snapshot",
    "stream_ui_events",
]
