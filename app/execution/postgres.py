"""PostgreSQL production adapter for durable command acceptance and leases.

Command acceptance and worker delivery stay in PostgreSQL functions.  The
adapter only validates the closed Python contract, binds the trusted tenant to
the transaction, and maps the stable database result codes.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from time import perf_counter
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.contracts.canonical import canonical_bytes, canonical_hash
from app.core.telemetry import record_operation
from app.execution.contracts import (
    BIGINT_MAX,
    CancelRun,
    CommandConflict,
    DeliveryReceipt,
    ExecutionClaim,
    ExecutionCommand,
    ExecutionDriverError,
    ExecutionFenceExhausted,
    RunCommandReceipt,
    RunNotFound,
    RunStateConflict,
    StaleExecutionFence,
    VersionUnavailable,
    _bounded_seconds,
    _replace_strict,
    _strict_claim,
    _strict_command,
)
from app.observation.emitter import emit_runtime_events
from app.observation.facts import (
    API_COMMAND_SOURCE,
    RECONCILIATION_SOURCE,
    RUNTIME_WORKER_SOURCE,
    EmitEventRequest,
    ExecutionAuditAction,
    build_execution_audit_emit_request,
)

MAX_LEASE_SECONDS = 90.0


def _bounded_text(value: object, *, label: str, maximum: int) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise ValueError(f"{label} must be a non-empty string of at most {maximum} characters")
    return value


def _runtime_hash(value: object) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("runtime_build_hash must be a lower-case sha256 digest")
    return value


def _lease_seconds(value: object) -> float:
    return _bounded_seconds(value, label="lease_seconds", maximum=MAX_LEASE_SECONDS)


def _cancel_payload_binding(command: CancelRun) -> tuple[str, str, str]:
    """Build the typed, content-addressed wrapper stored for a cancel command."""

    payload: dict[str, str] = {}
    if command.reason_ref is not None and command.reason_hash is not None:
        payload = {"reason_ref": command.reason_ref, "reason_hash": command.reason_hash}
    payload_hash = canonical_hash(payload)
    payload_ref = f"command-payload:{payload_hash}"
    payload_json = canonical_bytes(payload).decode("utf-8")
    return payload_ref, payload_hash, payload_json


def _cancel_expected_revision(value: object) -> int:
    """Validate the BIGINT revision before it can cross the SQL boundary."""

    if type(value) is not int or not 0 <= value < BIGINT_MAX:
        raise ExecutionDriverError(f"cancel expected_revision must be an exact int in [0, {BIGINT_MAX})")
    return value


class PostgresExecutionDriver:
    """Small production persistence seam backed exclusively by database functions."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        command_session_factory: async_sessionmaker[AsyncSession] | None = None,
        reconciliation_session_factory: async_sessionmaker[AsyncSession] | None = None,
        lease_seconds: float = 30.0,
        operation_timeout_seconds: float = 5.0,
    ) -> None:
        if not isinstance(session_factory, async_sessionmaker):
            raise TypeError("session_factory must be an async_sessionmaker")
        if command_session_factory is not None and not isinstance(command_session_factory, async_sessionmaker):
            raise TypeError("command_session_factory must be an async_sessionmaker")
        if reconciliation_session_factory is not None and not isinstance(
            reconciliation_session_factory, async_sessionmaker
        ):
            raise TypeError("reconciliation_session_factory must be an async_sessionmaker")
        self._session_factory = session_factory
        self._command_session_factory = command_session_factory
        self._reconciliation_session_factory = reconciliation_session_factory
        self._lease_seconds = _lease_seconds(lease_seconds)
        self._operation_timeout_seconds = _bounded_seconds(
            operation_timeout_seconds,
            label="operation_timeout_seconds",
            maximum=30,
        )

    async def claim(
        self,
        worker_id: str,
        runtime_build_hash: str,
        tenant_id: str,
        lease_seconds: float | None = None,
    ) -> ExecutionClaim | None:
        started = perf_counter()
        worker = _bounded_text(worker_id, label="worker_id", maximum=256)
        build_hash = _runtime_hash(runtime_build_hash)
        tenant = _bounded_text(tenant_id, label="tenant_id", maximum=128)
        duration = self._lease_seconds if lease_seconds is None else _lease_seconds(lease_seconds)
        async with asyncio.timeout(self._operation_timeout_seconds):
            async with self._session_factory() as session, session.begin():
                await self._scope_transaction(session, tenant)
                result = await session.execute(
                    text(
                        "SELECT * FROM grove_claim_run_command("
                        ":tenant_id, :worker_id, :runtime_build_hash, :lease_seconds)"
                    ),
                    {
                        "tenant_id": tenant,
                        "worker_id": worker,
                        "runtime_build_hash": build_hash,
                        "lease_seconds": duration,
                    },
                )
                row = result.mappings().one_or_none()
                if row is None:
                    return None
                if row["result_code"] == "version_unavailable":
                    raise VersionUnavailable("no ready command matches the worker runtime build")
                if row["result_code"] == "fence_exhausted":
                    raise ExecutionFenceExhausted("execution fence cannot be incremented safely")
                if row["result_code"] != "claimed":
                    raise RuntimeError("database returned an unknown claim result")
                claim = ExecutionClaim.model_validate(
                    {
                        "command_id": row["command_id"],
                        "tenant_id": tenant,
                        "run_id": row["run_id"],
                        "command_seq": row["command_seq"],
                        "command_digest": row["command_digest"],
                        "runtime_build_hash": row["runtime_build_hash"],
                        "worker_id": worker,
                        "execution_fence": row["execution_fence"],
                        "lease_until": row["lease_until"].astimezone(UTC),
                        "graph_binding": {
                            "graph_ref": row["graph_ref"],
                            "graph_version": row["graph_version"],
                            "graph_state_schema_version": row["graph_state_schema_version"],
                        },
                    }
                )
                action: ExecutionAuditAction = "worker_takeover" if claim.execution_fence > 1 else "worker_claimed"
                await self._emit_observation_events(
                    session,
                    claim,
                    [
                        build_execution_audit_emit_request(
                            source=RUNTIME_WORKER_SOURCE,
                            run_id=claim.run_id,
                            command_id=claim.command_id,
                            command_seq=claim.command_seq,
                            command_type="start" if claim.command_seq == 0 else "continue",
                            action=action,
                            result_code="claimed",
                            occurred_at=datetime.now(UTC),
                            transition_key=f"{claim.command_id}:{claim.execution_fence}:claimed",
                        )
                    ],
                )
                record_operation(
                    "worker.claim",
                    duration_ms=float((perf_counter() - started) * 1000),
                    role="runtime_worker",
                    operation="claim",
                    outcome="ok",
                )
                return claim

    async def dispatch(self, command: ExecutionCommand) -> RunCommandReceipt:
        """Atomically accept the production ``CancelRun`` command.

        The current production slice deliberately exposes only cancellation;
        start/resume/internal delivery are implemented by their own later
        slices.  No pre-read is performed here: the SECURITY DEFINER function
        owns the run/command locks, revision CAS, fence revocation, and insert
        in one transaction.
        """

        trusted, _ = _strict_command(command)
        if type(trusted) is not CancelRun:
            raise NotImplementedError("PostgresExecutionDriver currently accepts only CancelRun")
        expected_revision = _cancel_expected_revision(trusted.expected_revision)
        command_factory = self._command_session_factory
        if command_factory is None:
            raise ExecutionDriverError("command_session_factory is required for cancel dispatch")
        payload_ref, payload_hash, payload_json = _cancel_payload_binding(trusted)
        async with asyncio.timeout(self._operation_timeout_seconds):
            async with command_factory() as session, session.begin():
                await self._scope_transaction(session, trusted.tenant_id)
                await self._insert_cancel_payload(
                    session,
                    tenant_id=trusted.tenant_id,
                    payload_ref=payload_ref,
                    payload_hash=payload_hash,
                    payload_json=payload_json,
                )
                result = await session.execute(
                    text(
                        "SELECT * FROM grove_accept_cancel_run("
                        ":tenant_id, :run_id, :command_id, :expected_revision, :command_digest, "
                        ":runtime_build_hash, :payload_ref, :payload_hash, CAST(:payload AS jsonb))"
                    ),
                    {
                        "tenant_id": trusted.tenant_id,
                        "run_id": trusted.run_id,
                        "command_id": trusted.command_id,
                        "expected_revision": expected_revision,
                        "command_digest": trusted.command_digest,
                        "runtime_build_hash": trusted.runtime_build_hash,
                        "payload_ref": payload_ref,
                        "payload_hash": payload_hash,
                        "payload": payload_json,
                    },
                )
                row = result.mappings().one_or_none()
                if row is None:
                    raise RunStateConflict("database returned no cancel acceptance result")
                result_code = row["result_code"]
                if result_code == "command_conflict":
                    raise CommandConflict("cancel command id is already bound to a different command")
                if result_code == "run_not_found":
                    raise RunNotFound(str(trusted.run_id))
                if result_code == "revision_conflict":
                    raise RunStateConflict("expected revision does not match the current run revision")
                if result_code == "invalid_state":
                    raise RunStateConflict("run is not cancellable in its current state")
                if result_code == "payload_conflict":
                    raise RunStateConflict("cancel payload artifact is missing or mismatched")
                if result_code == "build_conflict":
                    raise VersionUnavailable("cancel command runtime build does not match the run")
                if result_code == "revision_overflow":
                    raise ExecutionDriverError("cancel command revision cannot be incremented safely")
                if result_code == "fence_exhausted":
                    raise ExecutionFenceExhausted("cancel execution fence cannot be incremented safely")
                if result_code not in {"accepted", "idempotent"}:
                    raise RuntimeError("database returned an unknown cancel acceptance result")
                receipt = RunCommandReceipt.model_validate(
                    {
                        "command_id": row["command_id"],
                        "tenant_id": row["tenant_id"],
                        "run_id": row["run_id"],
                        "command_seq": row["command_seq"],
                        "command_type": row["command_type"],
                        "command_schema_version": row["command_schema_version"],
                        "command_digest": row["command_digest"],
                        "runtime_build_hash": row["runtime_build_hash"],
                        "status": row["status"],
                    }
                )
                await emit_runtime_events(
                    session,
                    tenant_id=receipt.tenant_id,
                    run_id=receipt.run_id,
                    causation_id=receipt.command_id,
                    events=[
                        build_execution_audit_emit_request(
                            source=API_COMMAND_SOURCE,
                            run_id=receipt.run_id,
                            command_id=receipt.command_id,
                            command_seq=receipt.command_seq,
                            command_type="cancel",
                            action="cancel_accepted",
                            result_code=result_code,
                            occurred_at=datetime.now(UTC),
                            transition_key=f"{receipt.command_id}:cancel-accepted",
                        )
                    ],
                )
                return receipt

    @staticmethod
    async def _insert_cancel_payload(
        session: AsyncSession,
        *,
        tenant_id: str,
        payload_ref: str,
        payload_hash: str,
        payload_json: str,
    ) -> None:
        """Insert the typed cancel wrapper before calling the DB acceptance seam."""

        await session.execute(
            text(
                "INSERT INTO command_payload ("
                "tenant_id, payload_ref, payload_hash, command_schema_version, "
                "sensitivity, retention, payload"
                ") VALUES ("
                ":tenant_id, :payload_ref, :payload_hash, 'cancel.v1', "
                "'sensitive', 'run_completion', CAST(:payload AS jsonb)"
                ") ON CONFLICT DO NOTHING"
            ),
            {
                "tenant_id": tenant_id,
                "payload_ref": payload_ref,
                "payload_hash": payload_hash,
                "payload": payload_json,
            },
        )
        existing = (
            (
                await session.execute(
                    text(
                        "SELECT payload_ref, payload_hash, command_schema_version, sensitivity, retention "
                        "FROM command_payload WHERE tenant_id = :tenant_id AND payload_ref = :payload_ref"
                    ),
                    {"tenant_id": tenant_id, "payload_ref": payload_ref},
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            existing is None
            or existing["payload_hash"] != payload_hash
            or existing["command_schema_version"] != "cancel.v1"
            or existing["sensitivity"] != "sensitive"
            or existing["retention"] != "run_completion"
        ):
            raise CommandConflict("cancel payload ref is already bound to a different immutable payload")

    async def heartbeat(self, claim: ExecutionClaim, lease_seconds: float | None = None) -> ExecutionClaim:
        trusted, _ = _strict_claim(claim)
        duration = self._lease_seconds if lease_seconds is None else _lease_seconds(lease_seconds)
        async with asyncio.timeout(self._operation_timeout_seconds):
            async with self._session_factory() as session, session.begin():
                await self._scope_transaction(session, trusted.tenant_id)
                result = await session.execute(
                    text(
                        "SELECT grove_heartbeat_run_command("
                        ":tenant_id, :run_id, :command_id, :command_seq, :command_digest, "
                        ":runtime_build_hash, :worker_id, :execution_fence, "
                        ":expected_lease_until, :lease_seconds)"
                    ),
                    {
                        "tenant_id": trusted.tenant_id,
                        "run_id": trusted.run_id,
                        "command_id": trusted.command_id,
                        "command_seq": trusted.command_seq,
                        "command_digest": trusted.command_digest,
                        "runtime_build_hash": trusted.runtime_build_hash,
                        "worker_id": trusted.worker_id,
                        "execution_fence": trusted.execution_fence,
                        "expected_lease_until": trusted.lease_until,
                        "lease_seconds": duration,
                    },
                )
                lease_until = result.scalar_one_or_none()
                if lease_until is None:
                    raise StaleExecutionFence("claim no longer owns a current database lease")
                renewed = _replace_strict(
                    trusted,
                    ExecutionClaim,
                    lease_until=lease_until.astimezone(UTC),
                )
                await self._emit_observation_events(
                    session,
                    renewed,
                    [
                        build_execution_audit_emit_request(
                            source=RUNTIME_WORKER_SOURCE,
                            run_id=renewed.run_id,
                            command_id=renewed.command_id,
                            command_seq=renewed.command_seq,
                            command_type="start" if renewed.command_seq == 0 else "continue",
                            action="lease_renewed",
                            result_code="renewed",
                            occurred_at=datetime.now(UTC),
                            transition_key=(
                                f"{renewed.command_id}:{renewed.execution_fence}:{renewed.lease_until.isoformat()}"
                            ),
                        )
                    ],
                )
                return renewed

    async def consume(self, claim: ExecutionClaim) -> RunCommandReceipt:
        """Consume only after the database proves an authoritative checkpoint."""

        trusted, _ = _strict_claim(claim)
        async with asyncio.timeout(self._operation_timeout_seconds):
            async with self._session_factory() as session, session.begin():
                await self._scope_transaction(session, trusted.tenant_id)
                result = await session.execute(
                    text(
                        "SELECT * FROM grove_consume_run_command("
                        ":tenant_id, :run_id, :command_id, :command_seq, :command_digest, "
                        ":runtime_build_hash, :worker_id, :execution_fence, "
                        ":expected_lease_until)"
                    ),
                    {
                        "tenant_id": trusted.tenant_id,
                        "run_id": trusted.run_id,
                        "command_id": trusted.command_id,
                        "command_seq": trusted.command_seq,
                        "command_digest": trusted.command_digest,
                        "runtime_build_hash": trusted.runtime_build_hash,
                        "worker_id": trusted.worker_id,
                        "execution_fence": trusted.execution_fence,
                        "expected_lease_until": trusted.lease_until,
                    },
                )
                row = result.mappings().one_or_none()
                if row is None:
                    raise RunStateConflict("database returned no consume result")
                result_code = row["result_code"]
                if result_code == "stale":
                    raise StaleExecutionFence("claim no longer owns the current database lease")
                if result_code == "no_proof":
                    raise RunStateConflict("authoritative checkpoint proof is missing")
                if result_code != "consumed":
                    raise RuntimeError("database returned an unknown consume result")
                receipt = RunCommandReceipt.model_validate(
                    {
                        "command_id": row["command_id"],
                        "tenant_id": row["tenant_id"],
                        "run_id": row["run_id"],
                        "command_seq": row["command_seq"],
                        "command_type": row["command_type"],
                        "command_schema_version": row["command_schema_version"],
                        "command_digest": row["command_digest"],
                        "runtime_build_hash": row["runtime_build_hash"],
                        "status": row["status"],
                    }
                )
                await self._emit_observation_events(
                    session,
                    trusted,
                    [
                        build_execution_audit_emit_request(
                            source=RUNTIME_WORKER_SOURCE,
                            run_id=receipt.run_id,
                            command_id=receipt.command_id,
                            command_seq=receipt.command_seq,
                            command_type=receipt.command_type,
                            action="command_applied",
                            result_code="consumed",
                            occurred_at=datetime.now(UTC),
                            transition_key=f"{receipt.command_id}:{trusted.execution_fence}:consumed",
                        )
                    ],
                )
                return receipt

    async def dead_letter(self, claim: ExecutionClaim, reason_ref: str) -> RunCommandReceipt:
        """Move one still-owned, unexpired claim to the durable dead-letter state."""

        trusted, _ = _strict_claim(claim)
        reason = _bounded_text(reason_ref, label="reason_ref", maximum=512)
        async with asyncio.timeout(self._operation_timeout_seconds):
            async with self._session_factory() as session, session.begin():
                await self._scope_transaction(session, trusted.tenant_id)
                result = await session.execute(
                    text(
                        "SELECT * FROM grove_dead_letter_run_command("
                        ":tenant_id, :run_id, :command_id, :command_seq, :command_digest, "
                        ":runtime_build_hash, :worker_id, :execution_fence, "
                        ":expected_lease_until, :reason_ref)"
                    ),
                    {
                        "tenant_id": trusted.tenant_id,
                        "run_id": trusted.run_id,
                        "command_id": trusted.command_id,
                        "command_seq": trusted.command_seq,
                        "command_digest": trusted.command_digest,
                        "runtime_build_hash": trusted.runtime_build_hash,
                        "worker_id": trusted.worker_id,
                        "execution_fence": trusted.execution_fence,
                        "expected_lease_until": trusted.lease_until,
                        "reason_ref": reason,
                    },
                )
                row = result.mappings().one_or_none()
                if row is None:
                    raise RunStateConflict("database returned no dead-letter result")
                result_code = row["result_code"]
                if result_code in {"stale", "expired"}:
                    raise StaleExecutionFence("claim no longer owns a current database lease")
                if result_code in {"applied", "no_proof"}:
                    raise RunStateConflict("authoritative checkpoint proof prevents dead-lettering")
                if result_code != "dead_letter":
                    raise RuntimeError("database returned an unknown dead-letter result")
                receipt = RunCommandReceipt.model_validate(
                    {
                        "command_id": row["command_id"],
                        "tenant_id": row["tenant_id"],
                        "run_id": row["run_id"],
                        "command_seq": row["command_seq"],
                        "command_type": row["command_type"],
                        "command_schema_version": row["command_schema_version"],
                        "command_digest": row["command_digest"],
                        "runtime_build_hash": row["runtime_build_hash"],
                        "status": row["status"],
                    }
                )
                await self._emit_observation_events(
                    session,
                    trusted,
                    [
                        build_execution_audit_emit_request(
                            source=RUNTIME_WORKER_SOURCE,
                            run_id=receipt.run_id,
                            command_id=receipt.command_id,
                            command_seq=receipt.command_seq,
                            command_type=receipt.command_type,
                            action="command_dead_lettered",
                            result_code="dead_letter",
                            occurred_at=datetime.now(UTC),
                            transition_key=(f"{receipt.command_id}:{trusted.execution_fence}:dead-letter:{reason}"),
                        )
                    ],
                )
                return receipt

    async def reconcile_expired(self, tenant_id: str, run_id: UUID) -> RunCommandReceipt | None:
        """Reconcile one expired lease using the explicitly isolated projection role.

        This operation only resolves an already expired leased command.  It does
        not create a continuation command or invoke the execution graph.
        """

        tenant = _bounded_text(tenant_id, label="tenant_id", maximum=128)
        if type(run_id) is not UUID:
            raise ExecutionDriverError("run_id must be an exact UUID")
        projection_factory = self._reconciliation_session_factory
        if projection_factory is None:
            raise ExecutionDriverError("reconciliation_session_factory is required for expired reconciliation")
        async with asyncio.timeout(self._operation_timeout_seconds):
            async with projection_factory() as session, session.begin():
                await self._scope_transaction(session, tenant)
                result = await session.execute(
                    text("SELECT * FROM grove_reconcile_expired_run_command(:tenant_id, :run_id)"),
                    {"tenant_id": tenant, "run_id": run_id},
                )
                row = result.mappings().one_or_none()
                if row is None or row["result_code"] in {"noop", "manual"}:
                    return None
                if row["result_code"] not in {"consumed", "requeued"}:
                    raise RuntimeError("database returned an unknown expired reconciliation result")
                receipt = RunCommandReceipt.model_validate(
                    {
                        "command_id": row["command_id"],
                        "tenant_id": row["tenant_id"],
                        "run_id": row["run_id"],
                        "command_seq": row["command_seq"],
                        "command_type": row["command_type"],
                        "command_schema_version": row["command_schema_version"],
                        "command_digest": row["command_digest"],
                        "runtime_build_hash": row["runtime_build_hash"],
                        "status": row["status"],
                    }
                )
                reconcile_action: ExecutionAuditAction = (
                    "expired_command_consumed" if row["result_code"] == "consumed" else "expired_command_requeued"
                )
                await emit_runtime_events(
                    session,
                    tenant_id=receipt.tenant_id,
                    run_id=receipt.run_id,
                    causation_id=receipt.command_id,
                    events=[
                        build_execution_audit_emit_request(
                            source=RECONCILIATION_SOURCE,
                            run_id=receipt.run_id,
                            command_id=receipt.command_id,
                            command_seq=receipt.command_seq,
                            command_type=receipt.command_type,
                            action=reconcile_action,
                            result_code=row["result_code"],
                            occurred_at=datetime.now(UTC),
                            transition_key=f"{receipt.command_id}:{reconcile_action}:{receipt.status}",
                        )
                    ],
                )
                return receipt

    @staticmethod
    async def _scope_transaction(session: AsyncSession, tenant_id: str) -> None:
        await session.execute(text("SET LOCAL statement_timeout = '5000ms'"))
        await session.execute(text("SET LOCAL lock_timeout = '2000ms'"))
        await session.execute(
            text("SELECT set_config('grove.tenant_id', :tenant_id, true)"),
            {"tenant_id": tenant_id},
        )

    async def _emit_observation_events(
        self,
        session: AsyncSession,
        claim: ExecutionClaim,
        events: Sequence[EmitEventRequest],
    ) -> None:
        """Emit observation events atomically inside the delivery transaction.

        Called after ``grove_finish_delivery`` reports ``consumed`` while the
        run→command locks are still held by the same transaction.  The emit
        allocates commit-ordered ``run_seq`` and inserts the runtime fact and
        outbox rows; it never mutates WS-3 authority state.
        """

        await emit_runtime_events(
            session,
            tenant_id=claim.tenant_id,
            run_id=claim.run_id,
            causation_id=claim.command_id,
            events=events,
        )

    async def finish_delivery(
        self,
        claim: ExecutionClaim,
        outcome_kind: str,
        continue_payload_ref: str | None = None,
        continue_payload_hash: str | None = None,
        continue_payload: dict[str, object] | None = None,
        events: Sequence[EmitEventRequest] | None = None,
    ) -> DeliveryReceipt:
        """Atomically consume the current command and finalize delivery.

        For ``yield``: marks the command consumed, inserts a deterministic
        ContinueRun, and increments run revision.  For ``terminal``: marks
        the command consumed and sets the run to ``succeeded``.
        """
        trusted, _ = _strict_claim(claim)
        if outcome_kind not in ("yield", "terminal"):
            raise ExecutionDriverError("outcome_kind must be 'yield' or 'terminal'")
        if outcome_kind == "yield":
            if not continue_payload_ref or not continue_payload_hash or continue_payload is None:
                raise ExecutionDriverError("yield delivery requires continue payload")
        payload_json = (
            json.dumps(continue_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            if continue_payload
            else None
        )
        async with asyncio.timeout(self._operation_timeout_seconds):
            async with self._session_factory() as session, session.begin():
                await self._scope_transaction(session, trusted.tenant_id)
                result = await session.execute(
                    text(
                        "SELECT * FROM grove_finish_delivery("
                        ":tenant_id, :run_id, :command_id, :command_seq, :command_digest, "
                        ":runtime_build_hash, :worker_id, :execution_fence, "
                        ":expected_lease_until, :outcome_kind, "
                        ":continue_payload_ref, :continue_payload_hash, :continue_payload)"
                    ),
                    {
                        "tenant_id": trusted.tenant_id,
                        "run_id": trusted.run_id,
                        "command_id": trusted.command_id,
                        "command_seq": trusted.command_seq,
                        "command_digest": trusted.command_digest,
                        "runtime_build_hash": trusted.runtime_build_hash,
                        "worker_id": trusted.worker_id,
                        "execution_fence": trusted.execution_fence,
                        "expected_lease_until": trusted.lease_until,
                        "outcome_kind": outcome_kind,
                        "continue_payload_ref": continue_payload_ref,
                        "continue_payload_hash": continue_payload_hash,
                        "continue_payload": payload_json,
                    },
                )
                row = result.mappings().one_or_none()
                if row is None:
                    raise RunStateConflict("database returned no delivery result")
                code = row["result_code"]
                if code == "stale":
                    raise StaleExecutionFence("claim no longer owns the current database lease")
                if code == "no_proof":
                    raise RunStateConflict("authoritative checkpoint proof is missing")
                if code != "consumed":
                    raise RuntimeError("database returned an unknown delivery result")
                audit_events = [
                    build_execution_audit_emit_request(
                        source=RUNTIME_WORKER_SOURCE,
                        run_id=trusted.run_id,
                        command_id=trusted.command_id,
                        command_seq=trusted.command_seq,
                        command_type="start" if trusted.command_seq == 0 else "continue",
                        action="command_applied",
                        result_code=outcome_kind,
                        run_revision=row["run_revision"],
                        occurred_at=datetime.now(UTC),
                        transition_key=f"{trusted.command_id}:{trusted.execution_fence}:finish:{outcome_kind}",
                    )
                ]
                if outcome_kind == "yield" and row["continue_command_id"] is not None:
                    audit_events.append(
                        build_execution_audit_emit_request(
                            source=RUNTIME_WORKER_SOURCE,
                            run_id=trusted.run_id,
                            command_id=row["continue_command_id"],
                            command_seq=trusted.command_seq + 1,
                            command_type="continue",
                            action="command_accepted",
                            result_code="accepted",
                            run_revision=row["run_revision"],
                            occurred_at=datetime.now(UTC),
                            transition_key=f"{row['continue_command_id']}:accepted",
                        )
                    )
                await self._emit_observation_events(session, trusted, [*(events or ()), *audit_events])
                return DeliveryReceipt.model_validate(dict(row))
