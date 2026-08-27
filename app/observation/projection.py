"""Projection/Reconciliation role: durable outbox consumer -> UI read model.

The projection is the single owner of the rebuildable UI projection read model.
It consumes the runtime event outbox with a bounded poll loop (the durable
cursor compensation for ``LISTEN/NOTIFY``), validates each event's versioned
schema, and produces typed ``UIProjectionEvent`` rows.  Unknown schemas are
dead-lettered rather than guessed; the read model is always rebuildable from
the authoritative ``runtime_event`` facts.  The projection never touches WS-3
authority state and never blocks a Run.

Tenant discovery uses a SECURITY DEFINER helper (``grove_fetch_observation_batch``)
because the reconciler cannot enumerate tenants through RLS without an active
tenant context.  All read-model writes remain RLS-scoped to the per-row tenant
context.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.canonical import CanonicalModel, ProjectionSourceRef, canonical_hash
from app.core.telemetry import default_recorder, record_operation
from app.observation.facts import (
    DOMAIN_VIEW_ACCEPTED_SCHEMA_REF,
    EXECUTION_AUDIT_SCHEMA_REF,
    NODE_EXECUTED_SCHEMA_REF,
    RUN_LIFECYCLE_SCHEMA_REF,
    UI_DOMAIN_VIEW_SCHEMA_REF,
    DomainViewAcceptedPayload,
    RunLifecyclePayload,
    build_ui_projection_meta,
    domain_view_to_ui_accepted,
    lifecycle_to_run_status_changed,
    parse_runtime_payload,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 0.5
BATCH_SIZE = 100
UI_TARGET_KIND: Literal["run", "orchestration"] = "run"
UI_SCHEMA_REF = "grove.ui.run-status-changed.v1"
_AUDIT_ONLY_SCHEMAS = frozenset({NODE_EXECUTED_SCHEMA_REF, EXECUTION_AUDIT_SCHEMA_REF})


class ProjectionShutdown(Exception):
    """Raised to break the projection poll loop."""


async def _scope_tenant(session: AsyncSession, tenant_id: str) -> None:
    await session.execute(text("SET LOCAL statement_timeout = '5000ms'"))
    await session.execute(text("SET LOCAL lock_timeout = '2000ms'"))
    await session.execute(text("SELECT set_config('grove.tenant_id', :t, true)"), {"t": tenant_id})


async def _set_tenant(session: AsyncSession, tenant_id: str) -> None:
    await session.execute(text("SELECT set_config('grove.tenant_id', :t, true)"), {"t": tenant_id})


def _source_refs_json(run_id: UUID, run_seq: int, source_hash: str) -> str:
    ref = ProjectionSourceRef(
        source_kind="runtime_event",
        source_ref=f"runtime-event:{run_id}:{run_seq}",
        source_hash=source_hash,
        source_seq=run_seq,
        source_schema_ref=RUN_LIFECYCLE_SCHEMA_REF,
    )
    return json.dumps([ref.model_dump(mode="json")])


class ProjectionReconciler:
    """Consume the runtime event outbox into a rebuildable UI read model."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        batch_size: int = BATCH_SIZE,
    ) -> None:
        self._session_factory = session_factory
        self._batch_size = batch_size
        self._shutdown = asyncio.Event()

    def request_shutdown(self) -> None:
        self._shutdown.set()

    async def run(self) -> None:
        logger.info("projection.start")
        try:
            while not self._shutdown.is_set():
                try:
                    processed = await self.run_once()
                    if processed == 0:
                        await self._sleep_or_shutdown()
                except ProjectionShutdown:
                    break
                except Exception:
                    logger.exception("projection.iteration_error")
                    await self._sleep_or_shutdown()
        except ProjectionShutdown:
            # A shutdown request that arrives during the error-backoff sleep must
            # stop the loop cleanly; the role entry point does not catch it.
            pass
        logger.info("projection.stop")

    async def _sleep_or_shutdown(self) -> None:
        try:
            await asyncio.wait_for(self._shutdown.wait(), timeout=POLL_INTERVAL_SECONDS)
            raise ProjectionShutdown()
        except TimeoutError:
            pass

    async def run_once(self) -> int:
        """Process one bounded cross-tenant pass. Returns applied count."""
        applied = 0
        while not self._shutdown.is_set():
            count = await self._process_batch()
            if count == 0:
                break
            applied += count
        return applied

    async def _process_batch(self) -> int:
        rows = await self._fetch_pending()
        if not rows:
            return 0
        started = perf_counter()
        observed_rows: list[tuple[datetime, str]] = []
        async with self._session_factory() as session, session.begin():
            await session.execute(text("SET LOCAL statement_timeout = '5000ms'"))
            await session.execute(text("SET LOCAL lock_timeout = '2000ms'"))
            for row in rows:
                outbox_id, tenant_id, run_id, event_id, run_seq, source = row
                await _set_tenant(session, tenant_id)
                observed = await self._apply_outbox_row(
                    session,
                    tenant_id=tenant_id,
                    outbox_id=outbox_id,
                    run_id=run_id,
                    event_id=event_id,
                    run_seq=run_seq,
                    source=source,
                )
                if observed is not None:
                    observed_rows.append(observed)
        per_event_duration_ms = float((perf_counter() - started) * 1000) / len(rows)
        for observed in observed_rows:
            self._record_applied(observed, per_event_duration_ms)
        return len(rows)

    async def _fetch_pending(self) -> list[Any]:
        async with self._session_factory() as session:
            result = await session.execute(
                text("SELECT * FROM grove_fetch_observation_batch(:limit)"), {"limit": self._batch_size}
            )
            return list(result.fetchall())

    def _record_applied(self, observed: tuple[datetime, str], duration_ms: float) -> None:
        record_operation(
            "projector.apply",
            duration_ms=duration_ms,
            role="projection_reconciliation",
            operation="apply",
            outcome="ok",
        )
        occurred_at, schema_ref = observed
        schema_outcome = (
            "unknown_schema"
            if schema_ref
            not in {
                RUN_LIFECYCLE_SCHEMA_REF,
                *_AUDIT_ONLY_SCHEMAS,
            }
            else "projected"
        )
        default_recorder().record_metric(
            "projection.lag.seconds",
            value=max(0.0, (datetime.now(UTC) - occurred_at).total_seconds()),
            labels={"role": "projection_reconciliation", "operation": "apply", "outcome": schema_outcome},
            kind="histogram",
        )
        default_recorder().record_metric(
            "observation.event.count",
            value=1,
            labels={"role": "projection_reconciliation", "operation": "apply", "outcome": schema_outcome},
            kind="counter",
        )

    async def _apply_outbox_row(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        outbox_id: int,
        run_id: UUID,
        event_id: UUID,
        run_seq: int,
        source: str,
    ) -> tuple[datetime, str] | None:
        event_row = (
            await session.execute(
                text(
                    "SELECT event_type, payload_schema_ref, payload, occurred_at, "
                    "correlation_id, causation_id, trace_id, source_event_id "
                    "FROM runtime_event WHERE event_id = :eid"
                ),
                {"eid": event_id},
            )
        ).one_or_none()
        if event_row is None:
            await self._relay(session, outbox_id)
            return None
        event_type, schema_ref, payload, occurred_at = event_row[0], event_row[1], event_row[2], event_row[3]
        correlation_id, causation_id, trace_id, source_event_id = (
            event_row[4],
            event_row[5],
            event_row[6],
            event_row[7],
        )

        if schema_ref == RUN_LIFECYCLE_SCHEMA_REF:
            await self._project_lifecycle(
                session,
                tenant_id=tenant_id,
                run_id=run_id,
                event_id=event_id,
                run_seq=run_seq,
                payload=payload,
                occurred_at=occurred_at,
                correlation_id=correlation_id,
                causation_id=causation_id,
                trace_id=trace_id,
            )
        elif schema_ref == DOMAIN_VIEW_ACCEPTED_SCHEMA_REF:
            await self._project_domain_view(
                session,
                tenant_id=tenant_id,
                run_id=run_id,
                event_id=event_id,
                run_seq=run_seq,
                payload=payload,
                occurred_at=occurred_at,
                correlation_id=correlation_id,
                causation_id=causation_id,
                trace_id=trace_id,
            )
        elif schema_ref in _AUDIT_ONLY_SCHEMAS:
            pass
        else:
            await self._dead_letter(
                session,
                tenant_id=tenant_id,
                run_id=run_id,
                event_id=event_id,
                run_seq=run_seq,
                source=source,
                source_event_id=source_event_id,
                event_type=event_type,
                schema_ref=schema_ref,
                payload=payload,
                reason=f"unknown payload schema ref: {schema_ref}",
            )
        await self._advance_watermark(session, tenant_id, source, outbox_id, run_seq)
        await self._relay(session, outbox_id)
        return occurred_at, schema_ref

    async def _project_lifecycle(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        run_id: UUID,
        event_id: UUID,
        run_seq: int,
        payload: Any,
        occurred_at: datetime,
        correlation_id: str,
        causation_id: UUID,
        trace_id: str,
    ) -> None:
        parsed = parse_runtime_payload(RUN_LIFECYCLE_SCHEMA_REF, payload)
        assert isinstance(parsed, RunLifecyclePayload)
        await self._append_ui_projection(
            session,
            tenant_id=tenant_id,
            run_id=run_id,
            event_id=event_id,
            run_seq=run_seq,
            ui_schema_ref=UI_SCHEMA_REF,
            ui_payload=lifecycle_to_run_status_changed(parsed),
            source_hash=canonical_hash(parsed),
            occurred_at=occurred_at,
            correlation_id=correlation_id,
            causation_id=causation_id,
            trace_id=trace_id,
        )

    async def _project_domain_view(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        run_id: UUID,
        event_id: UUID,
        run_seq: int,
        payload: Any,
        occurred_at: datetime,
        correlation_id: str,
        causation_id: UUID,
        trace_id: str,
    ) -> None:
        parsed = parse_runtime_payload(DOMAIN_VIEW_ACCEPTED_SCHEMA_REF, payload)
        assert isinstance(parsed, DomainViewAcceptedPayload)
        await self._append_ui_projection(
            session,
            tenant_id=tenant_id,
            run_id=run_id,
            event_id=event_id,
            run_seq=run_seq,
            ui_schema_ref=UI_DOMAIN_VIEW_SCHEMA_REF,
            ui_payload=domain_view_to_ui_accepted(parsed),
            source_hash=canonical_hash(parsed),
            occurred_at=occurred_at,
            correlation_id=correlation_id,
            causation_id=causation_id,
            trace_id=trace_id,
        )

    async def _append_ui_projection(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        run_id: UUID,
        event_id: UUID,
        run_seq: int,
        ui_schema_ref: str,
        ui_payload: CanonicalModel,
        source_hash: str,
        occurred_at: datetime,
        correlation_id: str,
        causation_id: UUID,
        trace_id: str,
    ) -> None:
        meta = build_ui_projection_meta(
            tenant_id=tenant_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            trace_id=trace_id,
        )
        # Serialize same-target projections so projection_seq stays single-writer
        # per run: two concurrent reconcilers cannot both read the same MAX and
        # produce a colliding projection_seq.  The lock is transaction-scoped.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lk)::bigint)"),
            {"lk": f"{tenant_id}:{run_id}"},
        )
        next_seq = (
            await session.execute(
                text(
                    "SELECT COALESCE(MAX(projection_seq), 0) + 1 FROM ui_projection_event "
                    "WHERE tenant_id = :t AND target_kind = :k AND target_ref = :r"
                ),
                {"t": tenant_id, "k": UI_TARGET_KIND, "r": run_id},
            )
        ).scalar_one()
        await session.execute(
            text(
                "INSERT INTO ui_projection_event ("
                "tenant_id, target_kind, target_ref, event_id, projection_seq, "
                "contract_version, correlation_id, causation_id, trace_id, "
                "payload_schema_ref, payload, source_refs, projected_at"
                ") VALUES ("
                ":t, :k, :r, :eid, :seq, :cv, :corr, :caus, :trace, "
                ":schema, CAST(:payload AS jsonb), CAST(:src AS jsonb), :at"
                ") ON CONFLICT (tenant_id, event_id) DO NOTHING"
            ),
            {
                "t": tenant_id,
                "k": UI_TARGET_KIND,
                "r": run_id,
                "eid": event_id,
                "seq": next_seq,
                "cv": meta.contract_version,
                "corr": correlation_id,
                "caus": causation_id,
                "trace": trace_id,
                "schema": ui_schema_ref,
                "payload": json.dumps(ui_payload.model_dump(mode="json"), sort_keys=True),
                "src": _source_refs_json(run_id, run_seq, source_hash),
                "at": occurred_at,
            },
        )

    async def _dead_letter(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        run_id: UUID,
        event_id: UUID,
        run_seq: int,
        source: str,
        source_event_id: str,
        event_type: str,
        schema_ref: str,
        payload: Any,
        reason: str,
    ) -> None:
        await session.execute(
            text(
                "INSERT INTO runtime_event_dead_letter ("
                "tenant_id, run_id, event_id, run_seq, source, source_event_id, "
                "event_type, payload_schema_ref, payload, reason"
                ") VALUES ("
                ":t, :r, :eid, :rs, :src, :seid, :et, :schema, CAST(:payload AS jsonb), :reason"
                ") ON CONFLICT (tenant_id, event_id) DO NOTHING"
            ),
            {
                "t": tenant_id,
                "r": run_id,
                "eid": event_id,
                "rs": run_seq,
                "src": source,
                "seid": source_event_id,
                "et": event_type,
                "schema": schema_ref,
                "payload": json.dumps(payload),
                "reason": reason,
            },
        )

    async def _advance_watermark(
        self, session: AsyncSession, tenant_id: str, source: str, outbox_id: int, run_seq: int
    ) -> None:
        await session.execute(
            text(
                "INSERT INTO projection_watermark ("
                "tenant_id, source, last_outbox_id, last_run_seq, event_count"
                ") VALUES (:t, :s, :oid, :rs, 1) "
                "ON CONFLICT (tenant_id, source) DO UPDATE SET "
                "last_outbox_id = GREATEST(projection_watermark.last_outbox_id, EXCLUDED.last_outbox_id), "
                "last_run_seq = GREATEST(projection_watermark.last_run_seq, EXCLUDED.last_run_seq), "
                "event_count = projection_watermark.event_count + 1, updated_at = now()"
            ),
            {"t": tenant_id, "s": source, "oid": outbox_id, "rs": run_seq},
        )

    async def _relay(self, session: AsyncSession, outbox_id: int) -> None:
        await session.execute(
            text("UPDATE runtime_event_outbox SET relayed_at = now() WHERE outbox_id = :oid"),
            {"oid": outbox_id},
        )

    async def rebuild(self, tenant_id: str) -> int:
        """Rebuild the UI read model from authoritative runtime_event facts."""

        _PROJECTABLE_SCHEMAS = (RUN_LIFECYCLE_SCHEMA_REF, DOMAIN_VIEW_ACCEPTED_SCHEMA_REF)
        async with self._session_factory() as session, session.begin():
            await _scope_tenant(session, tenant_id)
            await session.execute(text("DELETE FROM ui_projection_event WHERE tenant_id = :t"), {"t": tenant_id})
            await session.execute(text("DELETE FROM projection_watermark WHERE tenant_id = :t"), {"t": tenant_id})
            rows = (
                await session.execute(
                    text(
                        "SELECT re.event_id, re.run_id, re.run_seq, re.payload, re.occurred_at, "
                        "re.correlation_id, re.causation_id, re.trace_id, re.payload_schema_ref "
                        "FROM runtime_event re WHERE re.tenant_id = :t "
                        "AND re.payload_schema_ref = ANY(:schemas) ORDER BY re.run_seq"
                    ),
                    {"t": tenant_id, "schemas": list(_PROJECTABLE_SCHEMAS)},
                )
            ).fetchall()
            for row in rows:
                project = self._project_lifecycle if row[8] == RUN_LIFECYCLE_SCHEMA_REF else self._project_domain_view
                await project(
                    session,
                    tenant_id=tenant_id,
                    run_id=row[1],
                    event_id=row[0],
                    run_seq=row[2],
                    payload=row[3],
                    occurred_at=row[4],
                    correlation_id=row[5],
                    causation_id=row[6],
                    trace_id=row[7],
                )
            return len(rows)

    async def health(self) -> dict[str, Any]:
        """Return low-cardinality projection health for the readiness endpoint."""
        async with self._session_factory() as session:
            result = (await session.execute(text("SELECT * FROM grove_observation_health()"))).one_or_none()
        if result is None:
            backlog = 0
            unknown_schema = 0
        else:
            backlog = int(result[0])
            unknown_schema = int(result[1])
        default_recorder().record_metric(
            "observation.backlog",
            value=backlog,
            labels={"role": "projection_reconciliation", "operation": "health", "outcome": "ready"},
        )
        return {"status": "ready", "backlog": backlog, "unknown_schema": unknown_schema}


__all__ = ["ProjectionReconciler", "ProjectionShutdown"]
