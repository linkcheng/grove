"""Atomic persistence adapter for committed WS-4 observation facts."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.trace import current_trace_id
from app.observation.facts import EmitEventRequest


def _descriptors(events: Sequence[EmitEventRequest]) -> str:
    if not events:
        raise ValueError("at least one observation event is required")
    descriptors = []
    for request in events:
        descriptors.append(
            {
                "event_type": request.event_type,
                "source": request.source,
                "source_event_id": request.source_event_id,
                "payload_schema_ref": request.payload_schema_ref,
                "payload": json.loads(request.canonical_payload_bytes()),
                "occurred_at": request.occurred_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            }
        )
    return json.dumps(descriptors, separators=(",", ":"), ensure_ascii=False)


async def emit_runtime_events(
    session: AsyncSession,
    *,
    tenant_id: str,
    run_id: UUID,
    causation_id: UUID,
    events: Sequence[EmitEventRequest],
) -> None:
    """Emit facts/outbox rows in the caller's existing authority transaction."""

    await session.execute(
        text(
            "SELECT * FROM grove_emit_runtime_events("
            ":tenant_id, :run_id, :orchestration_id, :correlation_id, "
            ":causation_id, :trace_id, CAST(:events AS jsonb))"
        ),
        {
            "tenant_id": tenant_id,
            "run_id": run_id,
            "orchestration_id": run_id,
            "correlation_id": str(run_id),
            "causation_id": causation_id,
            "trace_id": current_trace_id(),
            "events": _descriptors(events),
        },
    )


async def emit_runtime_events_psycopg(
    connection: object,
    *,
    tenant_id: str,
    run_id: UUID,
    causation_id: UUID,
    events: Sequence[EmitEventRequest],
) -> None:
    """Psycopg variant used by the checkpoint transaction on the same connection."""

    async with connection.cursor() as cursor:  # type: ignore[attr-defined]
        await cursor.execute(
            "SELECT * FROM grove_emit_runtime_events(%s, %s, %s, %s, %s, %s, %s::jsonb)",
            (
                tenant_id,
                run_id,
                run_id,
                str(run_id),
                causation_id,
                current_trace_id(),
                _descriptors(events),
            ),
        )


__all__ = ["emit_runtime_events", "emit_runtime_events_psycopg"]
