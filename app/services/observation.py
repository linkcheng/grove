"""Tenant-aware Observation API service: inspect, query and SSE stream.

All reads are RLS-scoped to the Active Tenant Context.  The service never
touches WS-3 authority state and never returns internal checkpoint, thread or
payload-body detail beyond the safe public view.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.context import ActiveTenantContext
from app.observation.facts import (
    RUN_LIFECYCLE_SCHEMA_REF,
    TERMINAL_RUN_STATUSES,
    ObservationCompleteness,
    PublicRunStatus,
    RunInspectView,
    RuntimeEventView,
    UIProjectionEventView,
)

_STREAM_POLL_INTERVAL = 0.5
_STREAM_MAX_EVENTS = 1000


async def _scope(session: AsyncSession, context: ActiveTenantContext) -> None:
    await session.execute(text("SET LOCAL statement_timeout = '5000ms'"))
    await session.execute(text("SELECT set_config('grove.tenant_id', :t, true)"), {"t": context.tenant_id})


def _run_not_found(context: ActiveTenantContext, run_id: UUID) -> RunInspectView:
    return RunInspectView(
        run_id=run_id,
        tenant_id=context.tenant_id,
        completeness="unavailable",
        as_of=datetime.now(UTC),
    )


async def inspect(session: AsyncSession, context: ActiveTenantContext, run_id: UUID) -> RunInspectView:
    """Return the safe, public Run Inspect view with projection completeness."""
    await _scope(session, context)
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
    await _scope(session, context)
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
    await _scope(session, context)
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


async def stream_ui_events(
    session_factory: async_sessionmaker[AsyncSession],
    context: ActiveTenantContext,
    run_id: UUID,
    after_projection_seq: int,
) -> AsyncIterator[UIProjectionEventView]:
    """Snapshot -> backfill -> bounded realtime tail using a durable cursor.

    The validated Active Tenant Context is carried through the stream rather
    than reduced to a bare tenant id, and each iteration re-establishes the
    tenant RLS scope from it so every read is re-authorized against the
    current principal's tenant rather than trusting a one-time scope.
    """
    cursor = after_projection_seq
    emitted = 0
    while emitted < _STREAM_MAX_EVENTS:
        async with session_factory() as session:
            await session.execute(text("SELECT set_config('grove.tenant_id', :t, true)"), {"t": context.tenant_id})
            await session.execute(text("SET LOCAL statement_timeout = '5000ms'"))
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
        advanced = False
        for row in rows:
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
            yield view
        if not advanced:
            await asyncio.sleep(_STREAM_POLL_INTERVAL)


__all__ = ["inspect", "list_runtime_events", "list_ui_events", "stream_ui_events"]
