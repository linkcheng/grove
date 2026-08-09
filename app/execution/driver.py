"""Deterministic Execution Driver interface and transactional adapter.

The public seam is intentionally small: command dispatch plus worker lease,
checkpoint-proof and reconciliation operations.  All durable facts live in a
single immutable snapshot owned by :class:`DeterministicExecutionDriver`;
``state_machine`` computes candidates without locks or side effects.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, TypeVar, runtime_checkable
from uuid import UUID

from app.contracts.canonical import canonical_hash
from app.execution.contracts import (
    HASH_PATTERN,
    ActionCompletionPayload,
    AppliedCommandMetadata,
    CancelRun,
    ChildCompletionPayload,
    CommandConflict,
    CommandNotFound,
    CommandStatus,
    CommandType,
    ContinueRun,
    ExecutionClaim,
    ExecutionCommand,
    ExecutionDriverError,
    InternalAuthorityIssuer,
    InternalDispatchAuthority,
    InterruptBinding,
    ResumeRun,
    RunCommandReceipt,
    RunNotFound,
    RunSignal,
    RunSignalConflict,
    RunStateConflict,
    RunStatus,
    SignalPayload,
    StaleExecutionFence,
    StartRun,
    TerminalRunStatus,
    VersionUnavailable,
    _prepare_strict_input,
    _replace_strict,
    _strict_claim,
    _strict_command,
    _strict_identity,
    _strict_metadata,
    _strict_validate,
    _strict_validate_raw,
    derive_continue_command_id,
    derive_signal_command_id,
    derive_signal_id,
)
from app.execution.state_machine import (
    MAX_LEASE_SECONDS,
    WAIT_KINDS,
    AppliedRecord,
    CancelFenceRevoked,
    ClaimFenceIssued,
    CommandAggregate,
    DriverSnapshot,
    RunAggregate,
    empty_snapshot,
    transition_applied_for,
    transition_claim,
    transition_consume,
    transition_dead_letter,
    transition_dispatch,
    transition_get_status,
    transition_heartbeat,
    transition_is_applied,
    transition_reconcile,
    transition_record_applied,
    transition_set_run_status,
    transition_set_run_wait,
    transition_set_superseded_observation,
    transition_set_user_interrupt,
    validate_lease_seconds,
    validate_snapshot,
)


@runtime_checkable
class ExecutionDriver(Protocol):
    """The sole external command-acceptance seam."""

    async def dispatch(self, command: ExecutionCommand) -> RunCommandReceipt:
        raise NotImplementedError


_ResultT = TypeVar("_ResultT")
_Transition = Callable[[DriverSnapshot], tuple[DriverSnapshot, _ResultT]]

MAX_TIME_ADVANCE_SECONDS = 1_000_000_000.0
_INTERNAL_AUTHORITY_ISSUERS = frozenset({"driver_reconciler", "action_completion_bridge", "child_completion_bridge"})
_PUBLIC_STATUS_VALUES = frozenset(
    {
        "accepted",
        "running",
        "waiting_user_input",
        "waiting_action_result",
        "waiting_child_result",
        "cancel_requested",
        "succeeded",
        "failed",
        "cancelled",
    }
)


@dataclass(frozen=True, slots=True)
class _AuthorityBinding:
    """Immutable registration proof for one opaque internal capability."""

    capability: InternalDispatchAuthority
    issuer: InternalAuthorityIssuer
    fingerprint: str


def _authority_fingerprint(authority: InternalDispatchAuthority) -> str:
    if type(authority) is not InternalDispatchAuthority or type(authority.issuer) is not str:
        raise ValueError("internal dispatch authority must be an exact known capability")
    if authority.issuer not in _INTERNAL_AUTHORITY_ISSUERS:
        raise ValueError("unknown internal dispatch authority issuer")
    return canonical_hash({"issuer": authority.issuer})


def _detach_public_result(result: object) -> object:
    """Return a strict, recursively detached value from the public result union."""

    if result is None or type(result) is bool:
        return result
    if type(result) is RunCommandReceipt:
        return _strict_validate(result, RunCommandReceipt)
    if type(result) is ExecutionClaim:
        return _strict_validate(result, ExecutionClaim)
    if type(result) is AppliedCommandMetadata:
        return _strict_validate(result, AppliedCommandMetadata)
    if type(result) is str and result in _PUBLIC_STATUS_VALUES:
        return result
    raise ExecutionDriverError("transition returned an unknown public result type")


def _public_uuid(value: object, *, label: str) -> UUID:
    if type(value) is not UUID:
        raise ExecutionDriverError(f"{label} must be an exact UUID")
    return value


def _public_text(value: object, *, label: str) -> str:
    if type(value) is not str or not value:
        raise ExecutionDriverError(f"{label} must be an exact non-empty string")
    return value


def _public_command(command: object) -> tuple[ExecutionCommand, str]:
    try:
        return _strict_command(command)
    except ExecutionDriverError:
        raise
    except (TypeError, ValueError) as exc:
        raise ExecutionDriverError("execution command boundary validation failed") from exc


class DeterministicExecutionDriver:
    """In-memory reference adapter used by contract and crash-window tests."""

    __slots__ = (
        "_lease_seconds",
        "_clock",
        "_internal_authorities",
        "_authority_bindings",
        "_offset",
        "_clock_watermark",
        "_lock",
        "_snapshot",
    )

    def __init__(
        self,
        *,
        lease_seconds: float = 30.0,
        clock: Callable[[], datetime] | None = None,
        internal_authorities: Iterable[InternalDispatchAuthority] = (),
    ) -> None:
        configured_lease = validate_lease_seconds(lease_seconds, allow_none=False)
        assert configured_lease is not None
        authorities = tuple(internal_authorities)
        if any(type(authority) is not InternalDispatchAuthority for authority in authorities):
            raise ValueError("internal authorities must be exact capabilities")
        bindings = tuple(
            _AuthorityBinding(authority, authority.issuer, _authority_fingerprint(authority))
            for authority in authorities
        )
        self._lease_seconds = configured_lease
        self._clock = clock or (lambda: datetime.now(UTC))
        self._internal_authorities = authorities
        self._authority_bindings = bindings
        self._offset = timedelta(0)
        self._clock_watermark: datetime | None = None
        self._lock = asyncio.Lock()
        self._snapshot = empty_snapshot()

    def _now(self) -> datetime:
        """Read the injected clock through a fail-closed monotonic seam.

        The watermark is deliberately independent of ``_snapshot``.  A failed
        transition may roll back the candidate root, but it must never roll
        back the latest observed wall-clock value used for lease authority.
        Callers invoke this method while holding the driver's lock.
        """

        now = self._clock()
        if type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        try:
            observed = now.astimezone(UTC) + self._offset
        except (OverflowError, ValueError) as exc:
            raise ValueError("clock value is outside the supported datetime range") from exc
        watermark = self._clock_watermark
        if watermark is not None and observed < watermark:
            raise ValueError("clock moved backward behind the monotonic watermark")
        if watermark is None or observed > watermark:
            self._clock_watermark = observed
        return observed

    def _check_internal_authority(self, authority: InternalDispatchAuthority) -> InternalAuthorityIssuer:
        """Require both opaque identity and the issuer proof captured at registration."""

        for binding in self._authority_bindings:
            if binding.capability is not authority:
                continue
            if type(authority.issuer) is not str or authority.issuer != binding.issuer:
                raise RunStateConflict("internal dispatch authority registration binding changed")
            try:
                fingerprint = _authority_fingerprint(authority)
            except ValueError as exc:
                raise RunStateConflict("internal dispatch authority registration binding changed") from exc
            if fingerprint != binding.fingerprint:
                raise RunStateConflict("internal dispatch authority registration binding changed")
            return binding.issuer
        raise RunStateConflict("internal dispatch authority is not registered by identity")

    def _commit(self, transition: _Transition[_ResultT]) -> _ResultT:
        """Validate, compute and commit exactly one immutable candidate."""

        before = self._snapshot
        validate_snapshot(before)
        candidate, result = transition(before)
        validate_snapshot(candidate)
        detached_result = _detach_public_result(result)
        # This is the sole assignment after initialization.  Every operation
        # has completed all fallible work before this line.
        self._snapshot = candidate
        return detached_result  # type: ignore[return-value]

    async def dispatch(self, command: ExecutionCommand) -> RunCommandReceipt:
        trusted_command, _ = _public_command(command)
        async with self._lock:
            return self._commit(lambda snapshot: transition_dispatch(snapshot, trusted_command))

    async def dispatch_internal(
        self, command: ContinueRun | RunSignal, authority: InternalDispatchAuthority
    ) -> RunCommandReceipt:
        trusted_command, _ = _strict_command(command)
        if type(trusted_command) not in {ContinueRun, RunSignal}:
            raise TypeError("internal dispatch requires a continue or signal command")
        if type(authority) is not InternalDispatchAuthority:
            raise TypeError("internal dispatch authority must be an exact capability")
        async with self._lock:
            issuer = self._check_internal_authority(authority)
            return self._commit(
                lambda snapshot: transition_dispatch(snapshot, trusted_command, authority_issuer=issuer)
            )

    async def claim(
        self,
        worker_id: str,
        runtime_build_hash: str,
        tenant_id: str | None = None,
        lease_seconds: float | None = None,
    ) -> ExecutionClaim | None:
        """Atomically claim the earliest eligible exact-build command."""

        resolved_lease = (
            self._lease_seconds if lease_seconds is None else validate_lease_seconds(lease_seconds, allow_none=False)
        )
        assert resolved_lease is not None
        async with self._lock:
            return self._commit(
                lambda snapshot: transition_claim(
                    snapshot,
                    worker_id=worker_id,
                    runtime_build_hash=runtime_build_hash,
                    tenant_id=tenant_id,
                    lease_seconds=resolved_lease,
                    now=self._now(),
                )
            )

    async def heartbeat(self, claim: ExecutionClaim, lease_seconds: float | None = None) -> ExecutionClaim:
        resolved_lease = (
            self._lease_seconds if lease_seconds is None else validate_lease_seconds(lease_seconds, allow_none=False)
        )
        assert resolved_lease is not None
        trusted_claim, _ = _strict_claim(claim)
        async with self._lock:
            return self._commit(
                lambda snapshot: transition_heartbeat(
                    snapshot, trusted_claim, now=self._now(), lease_seconds=resolved_lease
                )
            )

    async def consume(self, claim: ExecutionClaim) -> RunCommandReceipt:
        trusted_claim, _ = _strict_claim(claim)
        async with self._lock:
            return self._commit(lambda snapshot: transition_consume(snapshot, trusted_claim, now=self._now()))

    async def dead_letter(self, claim: ExecutionClaim, reason_ref: str) -> RunCommandReceipt:
        trusted_claim, _ = _strict_claim(claim)
        async with self._lock:
            return self._commit(
                lambda snapshot: transition_dead_letter(snapshot, trusted_claim, now=self._now(), reason_ref=reason_ref)
            )

    async def record_applied(self, metadata: AppliedCommandMetadata) -> AppliedCommandMetadata:
        trusted_metadata, _ = _strict_metadata(metadata)
        async with self._lock:
            return self._commit(lambda snapshot: transition_record_applied(snapshot, trusted_metadata, now=self._now()))

    async def applied_for(self, run_id: UUID) -> AppliedCommandMetadata | None:
        run_id = _public_uuid(run_id, label="run_id")
        async with self._lock:
            validate_snapshot(self._snapshot)
            result = transition_applied_for(self._snapshot, run_id)
            detached_result = _detach_public_result(result)
            return detached_result  # type: ignore[return-value]

    async def is_command_applied(self, command: ExecutionCommand | ExecutionClaim) -> bool:
        if type(command) is ExecutionClaim:
            trusted_input: ExecutionCommand | ExecutionClaim = _strict_claim(command)[0]
        else:
            trusted_input = _public_command(command)[0]
        async with self._lock:
            validate_snapshot(self._snapshot)
            return transition_is_applied(self._snapshot, trusted_input, now=self._now())

    async def is_superseded(self, command_id: UUID) -> bool:
        command_id = _public_uuid(command_id, label="command_id")
        async with self._lock:
            validate_snapshot(self._snapshot)
            return transition_set_superseded_observation(self._snapshot, command_id)

    async def get_run_status(self, tenant_id: str, run_id: UUID) -> RunStatus:
        tenant_id = _public_text(tenant_id, label="tenant_id")
        run_id = _public_uuid(run_id, label="run_id")
        async with self._lock:
            validate_snapshot(self._snapshot)
            return transition_get_status(self._snapshot, tenant_id, run_id)

    async def reconcile(self, tenant_id: str, run_id: UUID) -> RunCommandReceipt | None:
        tenant_id = _public_text(tenant_id, label="tenant_id")
        run_id = _public_uuid(run_id, label="run_id")
        async with self._lock:
            return self._commit(lambda snapshot: transition_reconcile(snapshot, tenant_id, run_id, now=self._now()))

    # The following lifecycle projections are deliberately private.  They are
    # explicit test fixtures for constructing waiting/running projections and
    # are not part of ExecutionDriver's production interface.
    async def _fixture_set_run_status(self, tenant_id: str, run_id: UUID, status: object) -> None:
        async with self._lock:
            self._commit(lambda snapshot: transition_set_run_status(snapshot, tenant_id, run_id, status))

    async def _fixture_set_run_wait(
        self,
        tenant_id: str,
        run_id: UUID,
        *,
        wait_ref: str,
        wait_hash: str,
        wait_kind: object,
        source_ref: str,
        source_fact_version: str,
        source_fact_hash: str,
        payload_ref: str,
        payload_hash: str,
    ) -> None:
        async with self._lock:
            self._commit(
                lambda snapshot: transition_set_run_wait(
                    snapshot,
                    tenant_id,
                    run_id,
                    wait_ref=wait_ref,
                    wait_hash=wait_hash,
                    wait_kind=wait_kind,
                    source_ref=source_ref,
                    source_fact_version=source_fact_version,
                    source_fact_hash=source_fact_hash,
                    payload_ref=payload_ref,
                    payload_hash=payload_hash,
                )
            )

    async def _fixture_set_user_interrupt(self, tenant_id: str, run_id: UUID, binding: InterruptBinding) -> None:
        async with self._lock:
            self._commit(lambda snapshot: transition_set_user_interrupt(snapshot, tenant_id, run_id, binding))

    def advance_time(self, seconds: float) -> None:
        """Move the deterministic clock without sleeping the test process."""

        if type(seconds) not in {int, float} or isinstance(seconds, bool):
            raise ValueError("seconds must be an exact finite positive int or float")
        try:
            numeric_seconds = float(seconds)
        except (OverflowError, ValueError) as exc:
            raise ValueError("seconds must be an exact finite positive int or float") from exc
        if not math.isfinite(numeric_seconds) or numeric_seconds <= 0 or numeric_seconds > MAX_TIME_ADVANCE_SECONDS:
            raise ValueError(f"seconds must be finite, positive, and <= {MAX_TIME_ADVANCE_SECONDS:g}")
        try:
            delta = timedelta(seconds=numeric_seconds)
            if delta <= timedelta(0):
                raise ValueError("seconds must be representable as a positive timedelta")
            offset = self._offset + delta
            if offset <= self._offset:
                raise ValueError("clock offset must increase")
            if offset.total_seconds() > MAX_TIME_ADVANCE_SECONDS:
                raise ValueError("clock offset exceeds the deterministic fixture bound")
            base_now = self._clock()
            if type(base_now) is not datetime or base_now.tzinfo is None or base_now.utcoffset() is None:
                raise ValueError("clock must return a timezone-aware datetime")
            candidate_now = base_now.astimezone(UTC) + offset
            if self._clock_watermark is not None and candidate_now < self._clock_watermark:
                raise ValueError("clock advance would move behind the monotonic watermark")
        except (OverflowError, ValueError) as exc:
            if isinstance(exc, ValueError) and str(exc).startswith(("seconds must", "clock offset", "clock advance")):
                raise
            raise ValueError("clock offset is outside the supported datetime range") from exc
        self._offset = offset


__all__ = [
    "ActionCompletionPayload",
    "AppliedCommandMetadata",
    "AppliedRecord",
    "CancelFenceRevoked",
    "ClaimFenceIssued",
    "CancelRun",
    "ChildCompletionPayload",
    "CommandAggregate",
    "CommandConflict",
    "CommandNotFound",
    "CommandStatus",
    "CommandType",
    "ContinueRun",
    "DeterministicExecutionDriver",
    "DriverSnapshot",
    "ExecutionClaim",
    "ExecutionCommand",
    "ExecutionDriver",
    "ExecutionDriverError",
    "HASH_PATTERN",
    "InternalDispatchAuthority",
    "InterruptBinding",
    "MAX_LEASE_SECONDS",
    "MAX_TIME_ADVANCE_SECONDS",
    "ResumeRun",
    "RunAggregate",
    "RunCommandReceipt",
    "RunNotFound",
    "RunSignal",
    "RunSignalConflict",
    "RunStateConflict",
    "RunStatus",
    "SignalPayload",
    "StartRun",
    "StaleExecutionFence",
    "TerminalRunStatus",
    "VersionUnavailable",
    "WAIT_KINDS",
    "_prepare_strict_input",
    "_replace_strict",
    "_strict_claim",
    "_strict_command",
    "_strict_identity",
    "_strict_metadata",
    "_strict_validate",
    "_strict_validate_raw",
    "derive_continue_command_id",
    "derive_signal_command_id",
    "derive_signal_id",
]
