"""Immutable execution-driver state and pure transitions.

The module deliberately has no lock, clock, or side effects.  A transition
receives one validated :class:`DriverSnapshot`, computes every value that can
fail, validates its complete candidate, and returns it to the driver commit
kernel.  The only mutable reference in the production adapter is the driver's
current snapshot pointer.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, Literal, cast
from uuid import UUID

from app.contracts.canonical import canonical_hash
from app.execution.contracts import (
    HASH_PATTERN,
    ActionCompletionPayload,
    AppliedCommandMetadata,
    CancelRun,
    CommandConflict,
    CommandNotFound,
    CommandStatus,
    ContinueRun,
    ExecutionClaim,
    ExecutionCommand,
    ExecutionDriverError,
    InternalAuthorityIssuer,
    InterruptBinding,
    ResumeRun,
    RunCommandReceipt,
    RunNotFound,
    RunSignal,
    RunSignalConflict,
    RunStateConflict,
    RunStatus,
    StaleExecutionFence,
    StartRun,
    VersionUnavailable,
    _bounded_seconds,
    _replace_strict,
    _strict_claim,
    _strict_command,
    _strict_identity,
    _strict_metadata,
    derive_continue_command_id,
    derive_signal_command_id,
    derive_signal_id,
)

WAIT_KINDS = frozenset({"action_result", "child_result"})
MAX_LEASE_SECONDS = 90.0
VALID_RUN_STATUSES = frozenset(
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
VALID_COMMAND_STATUSES = frozenset({"pending", "leased", "consumed", "dead_letter"})
_RECONCILER_ISSUER: InternalAuthorityIssuer = "driver_reconciler"

type CommandRelation = Literal["applied", "superseded", "unapplied"]
type CommandLifecycle = Literal[
    "pending",
    "leased",
    "consumed_idempotent",
    "consumed_superseded",
    "dead_letter",
]


@dataclass(frozen=True, slots=True)
class CommandAggregate:
    """One command and every lease/receipt fact owned by its run."""

    command: ExecutionCommand
    receipt: RunCommandReceipt
    command_fingerprint: str
    receipt_fingerprint: str
    status: CommandStatus = "pending"
    worker_id: str | None = None
    execution_fence: int | None = None
    lease_until: datetime | None = None
    dead_letter_ref: str | None = None
    consumed_worker_id: str | None = None
    consumed_execution_fence: int | None = None
    consumed_lease_until: datetime | None = None
    consumed_idempotent: bool = False
    superseded: bool = False
    active_claim: ExecutionClaim | None = None
    active_claim_fingerprint: str | None = None
    consumed_claim: ExecutionClaim | None = None
    consumed_claim_fingerprint: str | None = None
    claim_history: tuple[ExecutionClaim, ...] = ()
    claim_history_fingerprints: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AppliedRecord:
    """One applied checkpoint fact bound to the exact claim that produced it."""

    metadata: AppliedCommandMetadata
    fingerprint: str
    claim: ExecutionClaim
    claim_fingerprint: str


@dataclass(frozen=True, slots=True)
class ClaimFenceIssued:
    """Run-wide issuance proof for one distinct worker claim generation."""

    fence: int
    command_id: UUID
    worker_id: str
    claim_fingerprint: str


@dataclass(frozen=True, slots=True)
class CancelFenceRevoked:
    """Run-wide proof that accepting a cancel revoked prior writers."""

    fence: int
    cancel_command_id: UUID


type FenceEvent = ClaimFenceIssued | CancelFenceRevoked


@dataclass(frozen=True, slots=True)
class RunAggregate:
    """Immutable aggregate root for one run; commands are nested in this root."""

    tenant_id: str
    run_id: UUID
    runtime_build_hash: str
    status: RunStatus = "accepted"
    revision: int = 0
    next_command_seq: int = 0
    fence_events: tuple[FenceEvent, ...] = ()
    lease_command_id: UUID | None = None
    lease_owner: str | None = None
    lease_until: datetime | None = None
    wait_ref: str | None = None
    wait_hash: str | None = None
    wait_kind: str | None = None
    wait_source_ref: str | None = None
    wait_source_fact_version: str | None = None
    wait_source_fact_hash: str | None = None
    wait_payload_ref: str | None = None
    wait_payload_hash: str | None = None
    interrupt: InterruptBinding | None = None
    interrupt_fingerprint: str | None = None
    consumed_interrupt_nonces: frozenset[str] = frozenset()
    commands: tuple[CommandAggregate, ...] = ()
    applied: tuple[AppliedRecord, ...] = ()

    @property
    def execution_fence(self) -> int:
        """Derive the only fence high-water value from the temporal ledger."""

        if not self.fence_events:
            return 0
        event = self.fence_events[-1]
        if type(event) not in {ClaimFenceIssued, CancelFenceRevoked}:
            raise ValueError("fence ledger contains an unreadable event")
        if type(event.fence) is not int or isinstance(event.fence, bool):
            raise ValueError("fence ledger contains an invalid high-water fence")
        return event.fence


@dataclass(frozen=True, slots=True)
class DriverSnapshot:
    """The sole durable root.  Its run map is copied and read-only."""

    runs: Mapping[UUID, RunAggregate]

    def __post_init__(self) -> None:
        # Always copy through a mapping proxy.  A frozen dataclass alone would
        # still permit mutation through a dict supplied by a caller.
        object.__setattr__(self, "runs", MappingProxyType(dict(self.runs)))


def empty_snapshot() -> DriverSnapshot:
    return DriverSnapshot(MappingProxyType({}))


def _is_sha256(value: object) -> bool:
    return type(value) is str and re.fullmatch(HASH_PATTERN, value) is not None


def _is_aware_utc(value: object) -> bool:
    return type(value) is datetime and value.tzinfo is not None and value.utcoffset() is not None


def _run_fields(run: RunAggregate) -> dict[str, object]:
    return {
        "tenant_id": run.tenant_id,
        "run_id": run.run_id,
        "runtime_build_hash": run.runtime_build_hash,
        "status": run.status,
        "revision": run.revision,
        "next_command_seq": run.next_command_seq,
        "fence_events": run.fence_events,
        "lease_command_id": run.lease_command_id,
        "lease_owner": run.lease_owner,
        "lease_until": run.lease_until,
        "wait_ref": run.wait_ref,
        "wait_hash": run.wait_hash,
        "wait_kind": run.wait_kind,
        "wait_source_ref": run.wait_source_ref,
        "wait_source_fact_version": run.wait_source_fact_version,
        "wait_source_fact_hash": run.wait_source_fact_hash,
        "wait_payload_ref": run.wait_payload_ref,
        "wait_payload_hash": run.wait_payload_hash,
        "interrupt": run.interrupt,
        "interrupt_fingerprint": run.interrupt_fingerprint,
        "consumed_interrupt_nonces": run.consumed_interrupt_nonces,
        "commands": run.commands,
        "applied": run.applied,
    }


def _replace_run(run: RunAggregate, **updates: object) -> RunAggregate:
    if "execution_fence" in updates:
        raise TypeError("execution_fence is derived from fence_events")
    fields = _run_fields(run)
    fields.update(updates)
    return RunAggregate(**cast(dict[str, Any], fields))


def _command_fields(command: CommandAggregate) -> dict[str, object]:
    return {
        "command": command.command,
        "receipt": command.receipt,
        "command_fingerprint": command.command_fingerprint,
        "receipt_fingerprint": command.receipt_fingerprint,
        "status": command.status,
        "worker_id": command.worker_id,
        "execution_fence": command.execution_fence,
        "lease_until": command.lease_until,
        "dead_letter_ref": command.dead_letter_ref,
        "consumed_worker_id": command.consumed_worker_id,
        "consumed_execution_fence": command.consumed_execution_fence,
        "consumed_lease_until": command.consumed_lease_until,
        "consumed_idempotent": command.consumed_idempotent,
        "superseded": command.superseded,
        "active_claim": command.active_claim,
        "active_claim_fingerprint": command.active_claim_fingerprint,
        "consumed_claim": command.consumed_claim,
        "consumed_claim_fingerprint": command.consumed_claim_fingerprint,
        "claim_history": command.claim_history,
        "claim_history_fingerprints": command.claim_history_fingerprints,
    }


def _replace_command_state(command: CommandAggregate, **updates: object) -> CommandAggregate:
    fields = _command_fields(command)
    fields.update(updates)
    return CommandAggregate(**cast(dict[str, Any], fields))


def _snapshot_with_run(snapshot: DriverSnapshot, run: RunAggregate) -> DriverSnapshot:
    runs = dict(snapshot.runs)
    runs[run.run_id] = run
    return DriverSnapshot(runs)


def _find_command(snapshot: DriverSnapshot, command_id: UUID) -> tuple[RunAggregate, CommandAggregate] | None:
    for run in snapshot.runs.values():
        for command in run.commands:
            if command.receipt.command_id == command_id:
                return run, command
    return None


def _find_command_or_raise(snapshot: DriverSnapshot, command_id: UUID) -> tuple[RunAggregate, CommandAggregate]:
    found = _find_command(snapshot, command_id)
    if found is None:
        raise CommandNotFound(str(command_id))
    return found


def _applied_record_for_seq(run: RunAggregate, sequence: int) -> AppliedRecord | None:
    for record in run.applied:
        if record.metadata.command_seq == sequence:
            return record
    return None


def _applied_for_seq(run: RunAggregate, sequence: int) -> AppliedCommandMetadata | None:
    record = _applied_record_for_seq(run, sequence)
    return None if record is None else record.metadata


def _with_applied(
    run: RunAggregate, metadata: AppliedCommandMetadata, claim: ExecutionClaim
) -> tuple[AppliedRecord, ...]:
    if any(item.metadata.command_seq == metadata.command_seq for item in run.applied):
        raise ValueError("same command sequence has already been recorded")
    _, fingerprint = _strict_metadata(metadata)
    trusted_claim, claim_fingerprint = _strict_claim(claim)
    if (
        metadata.tenant_id != trusted_claim.tenant_id
        or metadata.run_id != trusted_claim.run_id
        or metadata.command_id != trusted_claim.command_id
        or metadata.command_seq != trusted_claim.command_seq
        or metadata.command_digest != trusted_claim.command_digest
        or metadata.runtime_build_hash != trusted_claim.runtime_build_hash
        or metadata.execution_fence != trusted_claim.execution_fence
    ):
        raise ValueError("applied metadata is not bound to its exact execution claim")
    record = AppliedRecord(
        metadata=metadata,
        fingerprint=fingerprint,
        claim=trusted_claim,
        claim_fingerprint=claim_fingerprint,
    )
    return tuple(sorted((*run.applied, record), key=lambda item: item.metadata.command_seq))


def _receipt_with(receipt: RunCommandReceipt, **updates: object) -> tuple[RunCommandReceipt, str]:
    updated = _replace_strict(receipt, RunCommandReceipt, **updates)
    return updated, canonical_hash(updated)


def _validate_now(now: datetime) -> datetime:
    if not _is_aware_utc(now):
        raise ValueError("clock must return a timezone-aware datetime")
    return now.astimezone(UTC)


def validate_lease_seconds(value: object, *, allow_none: bool = True) -> float | None:
    """Validate a bounded exact numeric lease before any candidate mutation."""

    if value is None and allow_none:
        return None
    return _bounded_seconds(value, label="lease_seconds", maximum=MAX_LEASE_SECONDS)


def validate_snapshot(snapshot: DriverSnapshot) -> None:
    """Prove key/owner/sequence/lease/applied closure for the entire root."""

    try:
        if type(snapshot) is not DriverSnapshot or type(snapshot.runs) is not MappingProxyType:
            raise ExecutionDriverError("driver root must be an immutable DriverSnapshot")
        _validate_primitive_closure(snapshot)
        global_command_ids: set[UUID] = set()
        for map_key, run in snapshot.runs.items():
            _validate_run(run, map_key, global_command_ids)
    except ExecutionDriverError:
        raise
    except (TypeError, ValueError) as exc:
        raise ExecutionDriverError("execution driver root integrity validation failed") from exc


def _exact_optional_str(value: object, *, label: str, hash_value: bool = False) -> None:
    if value is None:
        return
    if type(value) is not str or not value:
        raise ValueError(f"{label} must be an exact non-empty string")
    if hash_value and not _is_sha256(value):
        raise ValueError(f"{label} must be a lowercase SHA-256")


def _exact_optional_int(value: object, *, label: str) -> None:
    if value is not None and (type(value) is not int or isinstance(value, bool) or value < 0):
        raise ValueError(f"{label} must be an exact non-negative integer")


def _exact_optional_datetime(value: object, *, label: str) -> None:
    if value is not None and (type(value) is not datetime or not _is_aware_utc(value)):
        raise ValueError(f"{label} must be an exact timezone-aware datetime")


def _validate_claim_primitives(claim: object, fingerprint: object | None = None) -> None:
    _strict_claim(claim)
    if fingerprint is not None:
        _exact_optional_str(fingerprint, label="claim fingerprint", hash_value=True)


def _validate_command_primitives(command_state: object) -> None:
    if type(command_state) is not CommandAggregate:
        raise ValueError("command registry contains an unreadable entry")
    state = command_state
    _strict_command(state.command)
    _strict_identity(state.receipt, RunCommandReceipt)
    _exact_optional_str(state.command_fingerprint, label="command fingerprint", hash_value=True)
    _exact_optional_str(state.receipt_fingerprint, label="receipt fingerprint", hash_value=True)
    _exact_optional_str(state.status, label="command status")
    _exact_optional_str(state.worker_id, label="worker id")
    _exact_optional_int(state.execution_fence, label="execution fence")
    _exact_optional_datetime(state.lease_until, label="lease until")
    _exact_optional_str(state.dead_letter_ref, label="dead letter reference")
    _exact_optional_str(state.consumed_worker_id, label="consumed worker id")
    _exact_optional_int(state.consumed_execution_fence, label="consumed execution fence")
    _exact_optional_datetime(state.consumed_lease_until, label="consumed lease until")
    if type(state.consumed_idempotent) is not bool or type(state.superseded) is not bool:
        raise ValueError("command lifecycle flags must be exact booleans")
    if type(state.claim_history) is not tuple or type(state.claim_history_fingerprints) is not tuple:
        raise ValueError("claim history containers must be exact tuples")
    if len(state.claim_history) != len(state.claim_history_fingerprints):
        raise ValueError("claim history proof count is inconsistent")
    for claim, fingerprint in zip(state.claim_history, state.claim_history_fingerprints, strict=True):
        _validate_claim_primitives(claim, fingerprint)
    for optional_claim, optional_fingerprint in (
        (state.active_claim, state.active_claim_fingerprint),
        (state.consumed_claim, state.consumed_claim_fingerprint),
    ):
        if optional_claim is None:
            if optional_fingerprint is not None:
                raise ValueError("missing claim retains an identity proof")
        else:
            _validate_claim_primitives(optional_claim, optional_fingerprint)


def _validate_primitive_closure(snapshot: DriverSnapshot) -> None:
    """Reject primitive subclasses before equality, hashing or membership."""

    for map_key, run in snapshot.runs.items():
        if type(map_key) is not UUID or type(run) is not RunAggregate:
            raise ValueError("run registry contains an unreadable key or aggregate")
        if type(run.run_id) is not UUID:
            raise ValueError("run id must be an exact UUID")
        _exact_optional_str(run.tenant_id, label="tenant id")
        _exact_optional_str(run.runtime_build_hash, label="runtime build hash", hash_value=True)
        _exact_optional_str(run.status, label="run status")
        _exact_optional_int(run.revision, label="run revision")
        _exact_optional_int(run.next_command_seq, label="next command sequence")
        if type(run.fence_events) is not tuple:
            raise ValueError("fence ledger must be an exact tuple")
        for event in run.fence_events:
            if type(event) is ClaimFenceIssued:
                _exact_optional_int(event.fence, label="claim fence")
                if type(event.command_id) is not UUID:
                    raise ValueError("claim event command id must be an exact UUID")
                _exact_optional_str(event.worker_id, label="claim event worker id")
                _exact_optional_str(event.claim_fingerprint, label="claim event fingerprint", hash_value=True)
            elif type(event) is CancelFenceRevoked:
                _exact_optional_int(event.fence, label="cancel fence")
                if type(event.cancel_command_id) is not UUID:
                    raise ValueError("cancel event command id must be an exact UUID")
            else:
                raise ValueError("fence ledger contains an unreadable event")
        if run.lease_command_id is not None and type(run.lease_command_id) is not UUID:
            raise ValueError("lease command id must be an exact UUID")
        _exact_optional_str(run.lease_owner, label="lease owner")
        _exact_optional_datetime(run.lease_until, label="run lease until")
        for label, value, hash_value in (
            ("wait ref", run.wait_ref, False),
            ("wait hash", run.wait_hash, True),
            ("wait kind", run.wait_kind, False),
            ("wait source ref", run.wait_source_ref, False),
            ("wait source fact version", run.wait_source_fact_version, False),
            ("wait source fact hash", run.wait_source_fact_hash, True),
            ("wait payload ref", run.wait_payload_ref, False),
            ("wait payload hash", run.wait_payload_hash, True),
            ("interrupt fingerprint", run.interrupt_fingerprint, True),
        ):
            _exact_optional_str(value, label=label, hash_value=hash_value)
        if run.interrupt is not None:
            _strict_identity(run.interrupt, InterruptBinding)
        if type(run.consumed_interrupt_nonces) is not frozenset:
            raise ValueError("consumed interrupt nonces must be an exact frozenset")
        for nonce in run.consumed_interrupt_nonces:
            _exact_optional_str(nonce, label="consumed interrupt nonce", hash_value=True)
        if type(run.commands) is not tuple or type(run.applied) is not tuple:
            raise ValueError("run command and applied registries must be exact tuples")
        for command_state in run.commands:
            _validate_command_primitives(command_state)
        for record in run.applied:
            if type(record) is not AppliedRecord:
                raise ValueError("applied registry contains an unreadable entry")
            _strict_metadata(record.metadata)
            _exact_optional_str(record.fingerprint, label="applied fingerprint", hash_value=True)
            _validate_claim_primitives(record.claim, record.claim_fingerprint)


def _validate_run(run: RunAggregate, map_key: object, global_command_ids: set[UUID]) -> None:
    if type(run) is not RunAggregate or type(map_key) is not UUID or map_key != run.run_id:
        raise ValueError("run registry key is not bound to the aggregate run_id")
    if (
        type(run.tenant_id) is not str
        or not run.tenant_id
        or type(run.run_id) is not UUID
        or not _is_sha256(run.runtime_build_hash)
        or run.status not in VALID_RUN_STATUSES
        or type(run.revision) is not int
        or isinstance(run.revision, bool)
        or run.revision < 0
        or type(run.next_command_seq) is not int
        or isinstance(run.next_command_seq, bool)
        or run.next_command_seq < 0
        or type(run.fence_events) is not tuple
        or type(run.commands) is not tuple
        or type(run.applied) is not tuple
        or type(run.consumed_interrupt_nonces) is not frozenset
    ):
        raise ValueError("run aggregate scalar or container binding is invalid")
    if run.revision != len(run.commands) or run.next_command_seq != len(run.commands):
        raise ValueError("run revision and command sequence are inconsistent")
    if not run.commands:
        raise ValueError("every registered run must have a genesis start command")
    if any(type(nonce) is not str or not _is_sha256(nonce) for nonce in run.consumed_interrupt_nonces):
        raise ValueError("consumed interrupt nonce set is invalid")
    if run.interrupt is None:
        if run.interrupt_fingerprint is not None:
            raise ValueError("cleared interrupt retains an identity proof")
    else:
        _, interrupt_fingerprint = _strict_identity(run.interrupt, InterruptBinding)
        if run.interrupt_fingerprint != interrupt_fingerprint:
            raise ValueError("interrupt binding identity proof is inconsistent")
    _validate_wait_projection(run)

    command_by_seq: dict[int, CommandAggregate] = {}
    command_by_id: dict[UUID, CommandAggregate] = {}
    for command_state in run.commands:
        _validate_command_state(command_state)
        command = command_state.command
        receipt = command_state.receipt
        command_id = receipt.command_id
        if command_id in global_command_ids or command_id in command_by_id:
            raise ValueError("command id is not globally unique")
        global_command_ids.add(command_id)
        if (
            command_id != command.command_id
            or command.run_id != run.run_id
            or receipt.run_id != run.run_id
            or command.tenant_id != run.tenant_id
            or receipt.tenant_id != run.tenant_id
            or command.runtime_build_hash != run.runtime_build_hash
            or receipt.runtime_build_hash != run.runtime_build_hash
            or receipt.command_type != command.command_type
            or receipt.command_schema_version != command.command_schema_version
            or receipt.command_digest != command.command_digest
            or receipt.status != command_state.status
            or receipt.command_seq in command_by_seq
            or receipt.command_seq < 0
        ):
            raise ValueError("command ownership or receipt binding is inconsistent")
        command_by_id[command_id] = command_state
        command_by_seq[receipt.command_seq] = command_state

    if set(command_by_seq) != set(range(len(run.commands))):
        raise ValueError("command sequence is not a complete prefix")

    genesis = command_by_seq[0]
    if type(genesis.command) is not StartRun or genesis.receipt.command_seq != 0:
        raise ValueError("run command sequence must begin with exactly one start command")
    if sum(type(item.command) is StartRun for item in command_by_seq.values()) != 1:
        raise ValueError("run must contain exactly one start command")
    if (
        genesis.command.tenant_id != run.tenant_id
        or genesis.command.run_id != run.run_id
        or genesis.command.runtime_build_hash != run.runtime_build_hash
        or genesis.receipt.tenant_id != run.tenant_id
        or genesis.receipt.run_id != run.run_id
        or genesis.receipt.runtime_build_hash != run.runtime_build_hash
    ):
        raise ValueError("genesis start command is not bound to its run owner")

    _validate_fence_ledger(run, command_by_id)
    _validate_run_lease(run, command_by_id)
    _validate_applied(run, command_by_seq)


def _claim_identity(claim: ExecutionClaim) -> tuple[object, ...]:
    return (
        claim.command_id,
        claim.tenant_id,
        claim.run_id,
        claim.command_seq,
        claim.command_digest,
        claim.runtime_build_hash,
        claim.worker_id,
        claim.execution_fence,
    )


def _validate_fence_ledger(run: RunAggregate, command_by_id: Mapping[UUID, CommandAggregate]) -> None:
    """Prove the run-wide temporal source for claims and cancel revocations."""

    claim_events: dict[int, ClaimFenceIssued] = {}
    cancel_events: dict[UUID, CancelFenceRevoked] = {}
    previous_claim_seq = -1
    for expected_fence, event in enumerate(run.fence_events, start=1):
        if type(event) not in {ClaimFenceIssued, CancelFenceRevoked} or event.fence != expected_fence:
            raise ValueError("fence ledger must be an exact continuous ordered prefix")
        if type(event.fence) is not int or isinstance(event.fence, bool):
            raise ValueError("fence ledger contains an invalid fence")
        if isinstance(event, ClaimFenceIssued):
            if (
                type(event.command_id) is not UUID
                or type(event.worker_id) is not str
                or not event.worker_id
                or len(event.worker_id) > 256
                or not _is_sha256(event.claim_fingerprint)
            ):
                raise ValueError("claim fence issuance event is malformed")
            command_state = command_by_id.get(event.command_id)
            if command_state is None:
                raise ValueError("claim fence issuance has no accepted command")
            command_seq = command_state.receipt.command_seq
            if command_seq < previous_claim_seq:
                raise ValueError("claim fence issuance rolled back the accepted command sequence")
            previous_claim_seq = command_seq
            claim_events[event.fence] = event
        else:
            if type(event.cancel_command_id) is not UUID or event.cancel_command_id in cancel_events:
                raise ValueError("cancel fence revocation event is malformed")
            cancel_events[event.cancel_command_id] = event

    history_fences: set[int] = set()
    for command_id, command_state in command_by_id.items():
        previous_fence = 0
        previous_claim: ExecutionClaim | None = None
        for claim, fingerprint in zip(
            command_state.claim_history, command_state.claim_history_fingerprints, strict=True
        ):
            fence = claim.execution_fence
            if fence < previous_fence:
                raise ValueError("claim history rolled back its fence")
            issuance = claim_events.get(fence)
            if issuance is None or issuance.command_id != command_id or issuance.worker_id != claim.worker_id:
                raise ValueError("claim history has no matching run-wide issuance event")
            if fence != previous_fence:
                if issuance.claim_fingerprint != fingerprint:
                    raise ValueError("claim issuance event is not bound to the first claim identity")
                history_fences.add(fence)
            else:
                if (
                    previous_claim is None
                    or _claim_identity(previous_claim) != _claim_identity(claim)
                    or claim.lease_until <= previous_claim.lease_until
                ):
                    raise ValueError("same-fence history is not a strict heartbeat extension")
            previous_fence = fence
            previous_claim = claim

    if history_fences != set(claim_events):
        raise ValueError("claim issuance ledger and command histories are not bidirectionally closed")

    cancel_commands = [
        (command_id, command_state)
        for command_id, command_state in command_by_id.items()
        if type(command_state.command) is CancelRun
    ]
    if len(cancel_commands) > 1:
        raise ValueError("a run accepts at most one cancel command")
    if set(cancel_events) != {command_id for command_id, _ in cancel_commands}:
        raise ValueError("cancel revocation ledger and accepted cancel commands are not bidirectionally closed")
    if not cancel_commands:
        return

    cancel_id, cancel_state = cancel_commands[0]
    cancel_seq = cancel_state.receipt.command_seq
    if cancel_seq != len(command_by_id) - 1:
        raise ValueError("cancel must be the last accepted command for its run")
    revocation = cancel_events[cancel_id]
    for event in claim_events.values():
        claimed_seq = command_by_id[event.command_id].receipt.command_seq
        if event.fence < revocation.fence:
            if claimed_seq >= cancel_seq:
                raise ValueError("claim issuance precedes the command acceptance that made it possible")
        elif event.fence > revocation.fence and event.command_id != cancel_id:
            raise ValueError("post-revocation claims must belong to the exact cancel command")
    for command_id, command_state in command_by_id.items():
        if command_id != cancel_id and command_state.status in {"pending", "leased"}:
            raise ValueError("cancel revocation did not supersede an older outstanding command")
        if command_state.superseded and (command_id == cancel_id or command_state.receipt.command_seq >= cancel_seq):
            raise ValueError("command supersession is not closed by the unique cancel revocation")


def _validate_receipt_claim(receipt: RunCommandReceipt, claim: ExecutionClaim | None, *, label: str) -> None:
    receipt_values = (receipt.lease_owner, receipt.execution_fence, receipt.lease_until)
    if claim is None:
        if any(value is not None for value in receipt_values):
            raise ValueError(f"{label} receipt retains lease provenance without a claim")
        return
    trusted_claim, _ = _strict_claim(claim)
    if any(value is None for value in receipt_values):
        raise ValueError(f"{label} receipt has incomplete lease provenance")
    if (
        receipt.lease_owner != trusted_claim.worker_id
        or receipt.execution_fence != trusted_claim.execution_fence
        or receipt.lease_until != trusted_claim.lease_until
    ):
        raise ValueError(f"{label} receipt lease provenance is inconsistent")


def _validate_claim_provenance(
    command_state: CommandAggregate,
    claim: ExecutionClaim,
    *,
    label: str,
    require_final: bool = True,
) -> None:
    trusted_claim, fingerprint = _strict_claim(claim)
    if require_final:
        matches_history = bool(
            command_state.claim_history
            and fingerprint == command_state.claim_history_fingerprints[-1]
            and command_state.claim_history[-1] == trusted_claim
        )
    else:
        matches_history = any(
            history_fingerprint == fingerprint and history_claim == trusted_claim
            for history_claim, history_fingerprint in zip(
                command_state.claim_history,
                command_state.claim_history_fingerprints,
                strict=True,
            )
        )
    if not matches_history:
        suffix = "final claim-history provenance" if require_final else "claim history"
        raise ValueError(f"{label} claim is not present in the command {suffix}")
    expected = (
        command_state.consumed_claim_fingerprint if label == "consumed" else command_state.active_claim_fingerprint
    )
    if label == "consumed":
        if expected != fingerprint:
            raise ValueError("consumed claim binding is inconsistent")
        if (
            command_state.consumed_worker_id != trusted_claim.worker_id
            or command_state.consumed_execution_fence != trusted_claim.execution_fence
            or command_state.consumed_lease_until != trusted_claim.lease_until
        ):
            raise ValueError("consumed claim binding is inconsistent")
    else:
        if expected != fingerprint:
            raise ValueError("active lease binding is inconsistent")
        if (
            command_state.worker_id != trusted_claim.worker_id
            or command_state.execution_fence != trusted_claim.execution_fence
            or command_state.lease_until != trusted_claim.lease_until
        ):
            raise ValueError("active lease binding is inconsistent")


def _validate_command_lifecycle(command_state: CommandAggregate, receipt: RunCommandReceipt) -> CommandLifecycle:
    """Discriminate the command's one closed lifecycle variant.

    The discriminator intentionally treats a consumed command with missing
    provenance as invalid rather than guessing whether it was idempotent or
    superseded.  Every successful transition must land in exactly one branch.
    """

    active_values = (
        command_state.active_claim,
        command_state.active_claim_fingerprint,
        command_state.worker_id,
        command_state.execution_fence,
        command_state.lease_until,
    )
    consumed_values = (
        command_state.consumed_claim,
        command_state.consumed_claim_fingerprint,
        command_state.consumed_worker_id,
        command_state.consumed_execution_fence,
        command_state.consumed_lease_until,
    )
    if command_state.status == "pending":
        if (
            command_state.claim_history
            or command_state.claim_history_fingerprints
            or any(value is not None for value in (*active_values, *consumed_values))
            or command_state.consumed_idempotent
            or command_state.superseded
            or command_state.dead_letter_ref is not None
        ):
            raise ValueError("pending command has fields outside the pending lifecycle variant")
        _validate_receipt_claim(receipt, None, label="pending")
        return "pending"

    if command_state.status == "leased":
        if (
            command_state.superseded
            or command_state.consumed_idempotent
            or command_state.dead_letter_ref is not None
            or any(value is not None for value in consumed_values)
            or not command_state.claim_history
            or command_state.active_claim is None
        ):
            raise ValueError("leased command has fields outside the leased lifecycle variant")
        _validate_claim_provenance(command_state, command_state.active_claim, label="active")
        _validate_receipt_claim(receipt, command_state.active_claim, label="leased")
        return "leased"

    if command_state.status == "consumed":
        if any(value is not None for value in active_values) or command_state.dead_letter_ref is not None:
            raise ValueError("consumed command retains active or dead-letter fields")
        if command_state.superseded:
            if command_state.consumed_idempotent:
                raise ValueError("superseded command cannot be idempotent-consumed")
            if command_state.claim_history:
                if command_state.consumed_claim is None or any(value is None for value in consumed_values):
                    raise ValueError("leased superseded command has incomplete consumed provenance")
                _validate_claim_provenance(command_state, command_state.consumed_claim, label="consumed")
                _validate_receipt_claim(receipt, command_state.consumed_claim, label="superseded")
            else:
                if any(value is not None for value in consumed_values):
                    raise ValueError("pending superseded command retains consumed provenance")
                _validate_receipt_claim(receipt, None, label="superseded")
            return "consumed_superseded"
        if not command_state.consumed_idempotent or command_state.consumed_claim is None:
            raise ValueError("consumed command has no complete idempotent provenance")
        if any(value is None for value in consumed_values) or not command_state.claim_history:
            raise ValueError("consumed command has incomplete idempotent provenance")
        _validate_claim_provenance(
            command_state,
            command_state.consumed_claim,
            label="consumed",
            require_final=False,
        )
        _validate_receipt_claim(receipt, command_state.consumed_claim, label="consumed")
        return "consumed_idempotent"

    if command_state.status == "dead_letter":
        if (
            type(command_state.dead_letter_ref) is not str
            or not command_state.dead_letter_ref
            or len(command_state.dead_letter_ref) > 512
            or command_state.superseded
            or command_state.consumed_idempotent
            or any(value is not None for value in (*active_values, *consumed_values))
            or not command_state.claim_history
        ):
            raise ValueError("dead-letter command has fields outside the dead-letter lifecycle variant")
        _validate_receipt_claim(receipt, command_state.claim_history[-1], label="dead-letter")
        return "dead_letter"

    raise ValueError("command status is outside the closed lifecycle union")


def _validate_command_state(command_state: CommandAggregate) -> None:
    if type(command_state) is not CommandAggregate:
        raise ValueError("command registry contains an unreadable entry")
    command, command_fingerprint = _strict_command(command_state.command)
    receipt, receipt_fingerprint = _strict_identity(command_state.receipt, RunCommandReceipt)
    if (
        command_state.command_fingerprint != command_fingerprint
        or command_state.receipt_fingerprint != receipt_fingerprint
        or type(command_state.status) is not str
        or command_state.status not in VALID_COMMAND_STATUSES
        or type(command_state.claim_history) is not tuple
        or type(command_state.claim_history_fingerprints) is not tuple
        or len(command_state.claim_history) != len(command_state.claim_history_fingerprints)
        or type(command_state.superseded) is not bool
        or type(command_state.consumed_idempotent) is not bool
    ):
        raise ValueError("command registry identity or container is inconsistent")
    for claim, expected in zip(command_state.claim_history, command_state.claim_history_fingerprints, strict=True):
        trusted_claim, fingerprint = _strict_claim(claim)
        if fingerprint != expected or trusted_claim.command_id != receipt.command_id:
            raise ValueError("claim history is not bound to its command")
        if (
            trusted_claim.tenant_id != receipt.tenant_id
            or trusted_claim.run_id != receipt.run_id
            or trusted_claim.command_seq != receipt.command_seq
            or trusted_claim.command_digest != receipt.command_digest
            or trusted_claim.runtime_build_hash != receipt.runtime_build_hash
        ):
            raise ValueError("claim history crossed command identity")
    _validate_command_lifecycle(command_state, receipt)


def _validate_run_lease(run: RunAggregate, command_by_id: Mapping[UUID, CommandAggregate]) -> None:
    if run.lease_command_id is None:
        if run.lease_owner is not None or run.lease_until is not None:
            raise ValueError("run retains lease fields without a lease command")
        if any(command.status == "leased" for command in command_by_id.values()):
            raise ValueError("leased command is not bound to the run lease")
        return
    if (
        type(run.lease_command_id) is not UUID
        or type(run.lease_owner) is not str
        or not run.lease_owner
        or not _is_aware_utc(run.lease_until)
    ):
        raise ValueError("run lease binding is invalid")
    leased = [(command_id, command) for command_id, command in command_by_id.items() if command.status == "leased"]
    if len(leased) != 1 or leased[0][0] != run.lease_command_id:
        raise ValueError("run has more than one or the wrong leased command")
    command = leased[0][1]
    if command.worker_id != run.lease_owner or command.lease_until != run.lease_until:
        raise ValueError("run lease does not match command lease")
    if command.execution_fence != run.execution_fence:
        raise ValueError("run lease fence does not match command lease")


def _validate_wait_projection(run: RunAggregate) -> None:
    wait_values = (
        run.wait_ref,
        run.wait_hash,
        run.wait_kind,
        run.wait_source_ref,
        run.wait_source_fact_version,
        run.wait_source_fact_hash,
        run.wait_payload_ref,
        run.wait_payload_hash,
    )
    if run.wait_kind is None:
        if any(value is not None for value in wait_values):
            raise ValueError("run wait projection is only partially cleared")
        return
    if type(run.wait_kind) is not str or run.wait_kind not in WAIT_KINDS:
        raise ValueError("run wait kind is outside the closed runtime union")
    if any(value is None for value in wait_values):
        raise ValueError("run wait projection is incomplete")
    if any(type(value) is not str or not value for value in wait_values if value is not None):
        raise ValueError("run wait projection contains an invalid reference")
    if (
        not _is_sha256(run.wait_hash)
        or not _is_sha256(run.wait_source_fact_hash)
        or not _is_sha256(run.wait_payload_hash)
    ):
        raise ValueError("run wait projection contains an invalid hash")
    expected_status = "waiting_action_result" if run.wait_kind == "action_result" else "waiting_child_result"
    if run.status != expected_status:
        raise ValueError("run wait kind does not match the waiting status")


def _validate_applied(run: RunAggregate, command_by_seq: Mapping[int, CommandAggregate]) -> None:
    metadata_by_seq: dict[int, AppliedCommandMetadata] = {}
    records_by_seq: dict[int, AppliedRecord] = {}
    metadata_ids: set[UUID] = set()
    previous = -1
    for record in run.applied:
        if type(record) is not AppliedRecord:
            raise ValueError("applied registry contains an unreadable entry")
        trusted, fingerprint = _strict_metadata(record.metadata)
        if record.fingerprint != fingerprint:
            raise ValueError("applied metadata identity proof is inconsistent")
        trusted_claim, claim_fingerprint = _strict_claim(record.claim)
        if record.claim_fingerprint != claim_fingerprint:
            raise ValueError("applied claim identity proof is inconsistent")
        if (
            trusted.command_seq <= previous
            or trusted.command_seq in metadata_by_seq
            or trusted.command_id in metadata_ids
        ):
            raise ValueError("applied metadata sequence or identity is not unique")
        previous = trusted.command_seq
        command_state = command_by_seq.get(trusted.command_seq)
        if command_state is None:
            raise ValueError("applied checkpoint has no corresponding command")
        command = command_state.command
        receipt = command_state.receipt
        if (
            trusted.tenant_id != run.tenant_id
            or trusted.run_id != run.run_id
            or trusted.command_id != receipt.command_id
            or trusted.command_id != command.command_id
            or trusted.command_seq != receipt.command_seq
            or trusted.command_digest != receipt.command_digest
            or trusted.runtime_build_hash != run.runtime_build_hash
            or trusted.tenant_id != trusted_claim.tenant_id
            or trusted.run_id != trusted_claim.run_id
            or trusted.command_id != trusted_claim.command_id
            or trusted.command_seq != trusted_claim.command_seq
            or trusted.command_digest != trusted_claim.command_digest
            or trusted.runtime_build_hash != trusted_claim.runtime_build_hash
            or trusted.execution_fence != trusted_claim.execution_fence
            or command_state.status not in {"leased", "consumed"}
            or command_state.superseded
            or not any(
                history_claim == trusted_claim and history_fingerprint == claim_fingerprint
                for history_claim, history_fingerprint in zip(
                    command_state.claim_history,
                    command_state.claim_history_fingerprints,
                    strict=True,
                )
            )
        ):
            raise ValueError("applied checkpoint metadata is not bound to its command")
        metadata_by_seq[trusted.command_seq] = trusted
        records_by_seq[trusted.command_seq] = record
        metadata_ids.add(trusted.command_id)

    for command_state in command_by_seq.values():
        sequence = command_state.receipt.command_seq
        if command_state.superseded:
            if (
                command_state.status != "consumed"
                or sequence in metadata_by_seq
                or not any(
                    later.receipt.command_seq > sequence and isinstance(later.command, CancelRun)
                    for later in command_by_seq.values()
                )
            ):
                raise ValueError("superseded command does not have a cancel closure")
        elif command_state.status == "consumed" and sequence not in metadata_by_seq:
            raise ValueError("consumed command has no applied proof")
        elif command_state.status == "consumed" and command_state.consumed_claim is not None:
            final_fingerprint = command_state.claim_history_fingerprints[-1]
            if command_state.consumed_claim_fingerprint != final_fingerprint:
                record = records_by_seq[sequence]
                cancel_closes_delivery = any(
                    later.receipt.command_seq > sequence and isinstance(later.command, CancelRun)
                    for later in command_by_seq.values()
                )
                if (
                    not cancel_closes_delivery
                    or command_state.consumed_claim != record.claim
                    or command_state.consumed_claim_fingerprint != record.claim_fingerprint
                ):
                    raise ValueError("non-final consumed claim is not the apply-time claim closed by cancel")

    if metadata_by_seq:
        max_applied = max(metadata_by_seq)
        for sequence in range(max_applied + 1):
            command_state = command_by_seq.get(sequence)
            if command_state is None:
                raise ValueError("applied prefix has a missing command sequence")
            if sequence not in metadata_by_seq and not command_state.superseded:
                raise ValueError("applied prefix contains an unclosed sequence")


def _lease_valid(run: RunAggregate, now: datetime) -> bool:
    return run.lease_command_id is not None and run.lease_until is not None and run.lease_until > now


def _classify(run: RunAggregate, command_state: CommandAggregate) -> CommandRelation:
    exact = _applied_for_seq(run, command_state.receipt.command_seq)
    if exact is not None:
        if command_state.superseded:
            raise ValueError("a superseded command cannot also be applied")
        if (
            exact.command_id != command_state.receipt.command_id
            or exact.command_seq != command_state.receipt.command_seq
            or exact.command_digest != command_state.receipt.command_digest
        ):
            raise ValueError("applied metadata does not match command")
        return "applied"
    if command_state.superseded:
        if command_state.status != "consumed":
            raise ValueError("supersession closure is not terminal")
        return "superseded"
    if any(record.metadata.command_seq > command_state.receipt.command_seq for record in run.applied):
        raise ValueError("higher applied sequence cannot prove this command")
    return "unapplied"


def _require_applied(run: RunAggregate, command_state: CommandAggregate) -> AppliedCommandMetadata:
    if _classify(run, command_state) != "applied":
        raise RunStateConflict("command has no authoritative applied checkpoint proof")
    metadata = _applied_for_seq(run, command_state.receipt.command_seq)
    if metadata is None:
        raise ValueError("applied checkpoint proof disappeared")
    return metadata


def _validate_claim(
    snapshot: DriverSnapshot, claim: ExecutionClaim, now: datetime
) -> tuple[RunAggregate, CommandAggregate, str]:
    trusted_claim, fingerprint = _strict_claim(claim)
    found = _find_command(snapshot, trusted_claim.command_id)
    if found is None:
        raise CommandNotFound(str(trusted_claim.command_id))
    run, command_state = found
    if (
        command_state.active_claim is None
        or command_state.active_claim_fingerprint != fingerprint
        or trusted_claim.tenant_id != run.tenant_id
        or trusted_claim.run_id != command_state.receipt.run_id
        or trusted_claim.tenant_id != command_state.receipt.tenant_id
        or trusted_claim.command_seq != command_state.receipt.command_seq
        or trusted_claim.command_digest != command_state.receipt.command_digest
        or trusted_claim.runtime_build_hash != command_state.receipt.runtime_build_hash
        or command_state.status != "leased"
        or run.lease_command_id != trusted_claim.command_id
        or run.lease_owner != trusted_claim.worker_id
        or run.execution_fence != trusted_claim.execution_fence
        or command_state.execution_fence != trusted_claim.execution_fence
        or command_state.lease_until != trusted_claim.lease_until
        or trusted_claim.lease_until <= now
    ):
        raise StaleExecutionFence("worker lease or execution fence is stale")
    return run, command_state, fingerprint


def _validate_consumed_claim(claim: ExecutionClaim, command_state: CommandAggregate) -> None:
    _, fingerprint = _strict_claim(claim)
    if command_state.consumed_claim is None or command_state.consumed_claim_fingerprint != fingerprint:
        raise StaleExecutionFence("consumed command claim binding is stale")


def _check_run_binding(run: RunAggregate, command: ExecutionCommand) -> None:
    if run.tenant_id != command.tenant_id:
        raise CommandConflict("tenant does not match the run binding")


def _ensure_continue_admissible(run: RunAggregate) -> None:
    if any(command.status == "dead_letter" for command in run.commands):
        raise RunStateConflict("run has an unresolved dead-letter command")


def _validate_signal_binding(run: RunAggregate, command: RunSignal) -> None:
    expected_signal_id = derive_signal_id(
        command.payload.source_ref,
        command.payload.source_fact_version,
        command.payload.source_fact_hash,
    )
    if command.signal_id != expected_signal_id:
        raise RunSignalConflict("signal id is not derived from its source terminal fact")
    expected_id = derive_signal_command_id(command.tenant_id, command.run_id, command.signal_id)
    if command.command_id != expected_id:
        raise CommandConflict("signal command id is not derived from signal identity")
    if run.status not in {"waiting_action_result", "waiting_child_result"}:
        raise RunStateConflict("signal target is not waiting for an internal result")
    expected_payload = "action_completed" if run.status == "waiting_action_result" else "child_run_completed"
    expected_wait_kind = "action_result" if run.status == "waiting_action_result" else "child_result"
    if run.wait_kind != expected_wait_kind:
        raise RunStateConflict("run wait kind does not match the waiting status")
    if command.payload.payload_type != expected_payload:
        raise RunStateConflict("signal payload does not match the run wait")
    payload = command.payload
    if (
        run.wait_ref != command.wait_ref
        or run.wait_hash != command.wait_hash
        or run.wait_source_ref != payload.source_ref
        or run.wait_source_fact_version != payload.source_fact_version
        or run.wait_source_fact_hash != payload.source_fact_hash
        or run.wait_payload_ref != payload.payload_ref
        or run.wait_payload_hash != payload.payload_hash
    ):
        raise RunStateConflict("signal does not match the current opaque wait binding")
    if any(isinstance(item.command, RunSignal) and item.status in {"pending", "leased"} for item in run.commands):
        raise RunStateConflict("run already has an outstanding signal")


def transition_dispatch(
    snapshot: DriverSnapshot,
    command: ExecutionCommand,
    *,
    authority_issuer: InternalAuthorityIssuer | None = None,
) -> tuple[DriverSnapshot, RunCommandReceipt]:
    """Purely accept one command or return its exact existing receipt."""

    trusted_command, command_fingerprint = _strict_command(command)
    if isinstance(trusted_command, (ContinueRun, RunSignal)) and authority_issuer is None:
        raise RunStateConflict("internal command requires dispatch_internal authority")
    if authority_issuer is not None and authority_issuer not in {
        "driver_reconciler",
        "action_completion_bridge",
        "child_completion_bridge",
    }:
        raise TypeError("internal dispatch issuer is outside the closed authority union")
    if isinstance(trusted_command, ContinueRun) and authority_issuer != "driver_reconciler":
        raise RunStateConflict("continue requires the driver reconciler authority")
    if isinstance(trusted_command, RunSignal) and authority_issuer is not None:
        expected = (
            "action_completion_bridge"
            if isinstance(trusted_command.payload, ActionCompletionPayload)
            else "child_completion_bridge"
        )
        if authority_issuer != expected:
            raise RunStateConflict("signal payload requires its matching completion bridge authority")

    run = snapshot.runs.get(trusted_command.run_id)
    if run is not None and isinstance(trusted_command, ContinueRun):
        _ensure_continue_admissible(run)
    existing = _find_command(snapshot, trusted_command.command_id)
    if existing is not None:
        existing_run, existing_state = existing
        if existing_state.command_fingerprint != command_fingerprint:
            if isinstance(trusted_command, RunSignal):
                raise RunSignalConflict("signal id is already bound to a different command binding")
            raise CommandConflict("command id is already bound to a different command binding")
        # The root validator already proved this owner and all reverse
        # relations.  Returning here cannot resurrect an orphan receipt.
        if existing_run.run_id != trusted_command.run_id:
            raise ExecutionDriverError("idempotent command owner crossed run identity")
        return snapshot, existing_state.receipt

    if isinstance(trusted_command, StartRun):
        if run is not None:
            _check_run_binding(run, trusted_command)
            raise CommandConflict("a run accepts only one start command")
        run = RunAggregate(
            tenant_id=trusted_command.tenant_id,
            run_id=trusted_command.run_id,
            runtime_build_hash=trusted_command.runtime_build_hash,
        )
    elif run is None:
        raise RunNotFound(str(trusted_command.run_id))
    else:
        _check_run_binding(run, trusted_command)

    if trusted_command.runtime_build_hash != run.runtime_build_hash:
        raise VersionUnavailable(
            f"run requires runtime build {run.runtime_build_hash}, got {trusted_command.runtime_build_hash}"
        )
    if isinstance(trusted_command, (ResumeRun, CancelRun)) and trusted_command.expected_revision != run.revision:
        raise RunStateConflict("expected revision does not match the current run revision")
    if not isinstance(trusted_command, (StartRun, CancelRun)) and any(
        item.status in {"pending", "leased"} for item in run.commands
    ):
        raise RunStateConflict("run already has an outstanding command")
    if isinstance(trusted_command, ContinueRun):
        if run.status != "running":
            raise RunStateConflict("continue requires a running run")
        expected_id = derive_continue_command_id(
            trusted_command.tenant_id, trusted_command.run_id, trusted_command.revision
        )
        if trusted_command.command_id != expected_id or trusted_command.revision != run.revision:
            raise RunStateConflict("continue command is not the driver's current revision")
    if isinstance(trusted_command, RunSignal):
        _validate_signal_binding(run, trusted_command)
    if isinstance(trusted_command, ResumeRun):
        if run.status != "waiting_user_input":
            raise RunStateConflict("resume requires waiting_user_input")
        if run.interrupt is None or canonical_hash(trusted_command.interrupt) != canonical_hash(run.interrupt):
            raise RunStateConflict("resume interrupt binding does not match the current interrupt")
        if trusted_command.interrupt.nonce_hash in run.consumed_interrupt_nonces:
            raise RunStateConflict("resume interrupt nonce was already consumed")
    if isinstance(trusted_command, CancelRun) and run.status not in {
        "accepted",
        "running",
        "waiting_user_input",
        "waiting_action_result",
        "waiting_child_result",
    }:
        raise RunStateConflict("run is not cancellable in its current state")

    command_seq = run.next_command_seq
    next_revision = run.revision + 1
    fence_events = run.fence_events
    updated_commands = list(run.commands)
    if isinstance(trusted_command, CancelRun):
        fence_events = (*fence_events, CancelFenceRevoked(run.execution_fence + 1, trusted_command.command_id))
        for index, older in enumerate(updated_commands):
            if older.receipt.command_seq >= command_seq or older.status not in {"pending", "leased"}:
                continue
            applied_record = _applied_record_for_seq(run, older.receipt.command_seq)
            has_authoritative_applied_proof = applied_record is not None
            consumed_claim = (
                applied_record.claim
                if applied_record is not None
                else older.active_claim
                if older.status == "leased"
                else None
            )
            consumed_fingerprint = (
                applied_record.claim_fingerprint
                if applied_record is not None
                else older.active_claim_fingerprint
                if older.status == "leased"
                else None
            )
            updated_receipt, updated_receipt_fingerprint = _receipt_with(
                older.receipt,
                status="consumed",
                lease_owner=None if consumed_claim is None else consumed_claim.worker_id,
                execution_fence=None if consumed_claim is None else consumed_claim.execution_fence,
                lease_until=None if consumed_claim is None else consumed_claim.lease_until,
            )
            updated_commands[index] = _replace_command_state(
                older,
                status="consumed",
                consumed_claim=consumed_claim,
                consumed_claim_fingerprint=consumed_fingerprint,
                consumed_worker_id=None if consumed_claim is None else consumed_claim.worker_id,
                consumed_execution_fence=None if consumed_claim is None else consumed_claim.execution_fence,
                consumed_lease_until=None if consumed_claim is None else consumed_claim.lease_until,
                consumed_idempotent=has_authoritative_applied_proof,
                superseded=not has_authoritative_applied_proof,
                worker_id=None,
                execution_fence=None,
                lease_until=None,
                active_claim=None,
                active_claim_fingerprint=None,
                dead_letter_ref=None,
                receipt=updated_receipt,
                receipt_fingerprint=updated_receipt_fingerprint,
            )

    receipt = RunCommandReceipt(
        command_id=trusted_command.command_id,
        tenant_id=trusted_command.tenant_id,
        run_id=trusted_command.run_id,
        command_seq=command_seq,
        command_type=trusted_command.command_type,
        command_schema_version=trusted_command.command_schema_version,
        command_digest=trusted_command.command_digest,
        runtime_build_hash=trusted_command.runtime_build_hash,
        status="pending",
    )
    receipt, receipt_fingerprint = _strict_identity(receipt, RunCommandReceipt)
    command_state = CommandAggregate(
        command=trusted_command,
        receipt=receipt,
        command_fingerprint=command_fingerprint,
        receipt_fingerprint=receipt_fingerprint,
    )
    updated_commands.append(command_state)
    next_lease_command = None if isinstance(trusted_command, CancelRun) else run.lease_command_id
    next_lease_owner = None if isinstance(trusted_command, CancelRun) else run.lease_owner
    next_lease_until = None if isinstance(trusted_command, CancelRun) else run.lease_until
    candidate_run = _replace_run(
        run,
        commands=tuple(updated_commands),
        revision=next_revision,
        next_command_seq=command_seq + 1,
        fence_events=fence_events,
        lease_command_id=next_lease_command,
        lease_owner=next_lease_owner,
        lease_until=next_lease_until,
        status="cancel_requested" if isinstance(trusted_command, CancelRun) else run.status,
    )
    candidate = _snapshot_with_run(snapshot, candidate_run)
    return candidate, receipt


def transition_claim(
    snapshot: DriverSnapshot,
    *,
    worker_id: str,
    runtime_build_hash: str,
    now: datetime,
    lease_seconds: float,
    tenant_id: str | None = None,
) -> tuple[DriverSnapshot, ExecutionClaim | None]:
    now = _validate_now(now)
    if type(worker_id) is not str or not worker_id or len(worker_id) > 256:
        raise ValueError("worker_id is required")
    if type(runtime_build_hash) is not str or not _is_sha256(runtime_build_hash):
        raise ValueError("runtime_build_hash must be lowercase SHA-256")
    if tenant_id is not None and (type(tenant_id) is not str or not tenant_id):
        raise ValueError("tenant_id must be a non-empty string")
    lease_seconds = cast(float, validate_lease_seconds(lease_seconds, allow_none=False))
    candidates: list[tuple[RunAggregate, CommandAggregate]] = []
    for run in snapshot.runs.values():
        for command in run.commands:
            if tenant_id is not None and command.command.tenant_id != tenant_id:
                continue
            if command.status == "pending" or (
                command.status == "leased" and command.lease_until is not None and command.lease_until <= now
            ):
                candidates.append((run, command))
    candidates.sort(key=lambda item: (item[0].run_id.int, item[1].receipt.command_seq))
    if not candidates:
        return snapshot, None
    exact = [item for item in candidates if item[1].command.runtime_build_hash == runtime_build_hash]
    if not exact:
        expected = candidates[0][1].command.runtime_build_hash
        raise VersionUnavailable(f"no worker for exact runtime build {expected}")
    for run, command_state in exact:
        if _lease_valid(run, now):
            continue
        if command_state.command.runtime_build_hash != run.runtime_build_hash:
            raise VersionUnavailable(f"run requires runtime build {run.runtime_build_hash}")
        lease_until = now + timedelta(seconds=lease_seconds)
        execution_fence = run.execution_fence + 1
        claim = ExecutionClaim(
            command_id=command_state.receipt.command_id,
            tenant_id=command_state.receipt.tenant_id,
            run_id=command_state.receipt.run_id,
            command_seq=command_state.receipt.command_seq,
            command_digest=command_state.receipt.command_digest,
            runtime_build_hash=command_state.receipt.runtime_build_hash,
            worker_id=worker_id,
            execution_fence=execution_fence,
            lease_until=lease_until,
        )
        claim, claim_fingerprint = _strict_identity(claim, ExecutionClaim)
        fence_event = ClaimFenceIssued(
            fence=execution_fence,
            command_id=claim.command_id,
            worker_id=claim.worker_id,
            claim_fingerprint=claim_fingerprint,
        )
        receipt, receipt_fingerprint = _receipt_with(
            command_state.receipt,
            status="leased",
            lease_owner=worker_id,
            execution_fence=execution_fence,
            lease_until=lease_until,
        )
        updated_command = _replace_command_state(
            command_state,
            status="leased",
            worker_id=worker_id,
            execution_fence=execution_fence,
            lease_until=lease_until,
            receipt=receipt,
            receipt_fingerprint=receipt_fingerprint,
            active_claim=claim,
            active_claim_fingerprint=claim_fingerprint,
            claim_history=(*command_state.claim_history, claim),
            claim_history_fingerprints=(*command_state.claim_history_fingerprints, claim_fingerprint),
        )
        updated_run = _replace_run(
            run,
            fence_events=(*run.fence_events, fence_event),
            lease_command_id=command_state.receipt.command_id,
            lease_owner=worker_id,
            lease_until=lease_until,
            status=("running" if isinstance(command_state.command, (StartRun, ResumeRun, RunSignal)) else run.status),
            wait_ref=None if isinstance(command_state.command, (ResumeRun, RunSignal)) else run.wait_ref,
            wait_hash=None if isinstance(command_state.command, (ResumeRun, RunSignal)) else run.wait_hash,
            wait_kind=None if isinstance(command_state.command, (ResumeRun, RunSignal)) else run.wait_kind,
            wait_source_ref=None if isinstance(command_state.command, (ResumeRun, RunSignal)) else run.wait_source_ref,
            wait_source_fact_version=None
            if isinstance(command_state.command, (ResumeRun, RunSignal))
            else run.wait_source_fact_version,
            wait_source_fact_hash=None
            if isinstance(command_state.command, (ResumeRun, RunSignal))
            else run.wait_source_fact_hash,
            wait_payload_ref=None
            if isinstance(command_state.command, (ResumeRun, RunSignal))
            else run.wait_payload_ref,
            wait_payload_hash=None
            if isinstance(command_state.command, (ResumeRun, RunSignal))
            else run.wait_payload_hash,
            commands=tuple(updated_command if item is command_state else item for item in run.commands),
        )
        return _snapshot_with_run(snapshot, updated_run), claim
    return snapshot, None


def transition_heartbeat(
    snapshot: DriverSnapshot,
    claim: ExecutionClaim,
    *,
    now: datetime,
    lease_seconds: float,
) -> tuple[DriverSnapshot, ExecutionClaim]:
    now = _validate_now(now)
    lease_seconds = cast(float, validate_lease_seconds(lease_seconds, allow_none=False))
    run, command_state, _ = _validate_claim(snapshot, claim, now)
    lease_until = now + timedelta(seconds=lease_seconds)
    renewed = _replace_strict(claim, ExecutionClaim, lease_until=lease_until)
    renewed, renewed_fingerprint = _strict_identity(renewed, ExecutionClaim)
    receipt, receipt_fingerprint = _receipt_with(command_state.receipt, lease_until=lease_until)
    updated_command = _replace_command_state(
        command_state,
        lease_until=lease_until,
        receipt=receipt,
        receipt_fingerprint=receipt_fingerprint,
        active_claim=renewed,
        active_claim_fingerprint=renewed_fingerprint,
        claim_history=(*command_state.claim_history, renewed),
        claim_history_fingerprints=(*command_state.claim_history_fingerprints, renewed_fingerprint),
    )
    updated_run = _replace_run(
        run,
        lease_until=lease_until,
        commands=tuple(updated_command if item is command_state else item for item in run.commands),
    )
    return _snapshot_with_run(snapshot, updated_run), renewed


def transition_consume(
    snapshot: DriverSnapshot, claim: ExecutionClaim, *, now: datetime
) -> tuple[DriverSnapshot, RunCommandReceipt]:
    now = _validate_now(now)
    trusted_claim, _ = _strict_claim(claim)
    found = _find_command(snapshot, trusted_claim.command_id)
    if found is None:
        raise CommandNotFound(str(trusted_claim.command_id))
    run, command_state = found
    if command_state.status == "consumed" and command_state.consumed_idempotent:
        _validate_consumed_claim(trusted_claim, command_state)
        _require_applied(run, command_state)
        return snapshot, command_state.receipt
    if command_state.status == "consumed":
        raise StaleExecutionFence("command was consumed by another execution claim")
    run, command_state, _ = _validate_claim(snapshot, trusted_claim, now)
    _require_applied(run, command_state)
    updated_command = _replace_command_state(
        command_state,
        status="consumed",
        consumed_worker_id=trusted_claim.worker_id,
        consumed_execution_fence=trusted_claim.execution_fence,
        consumed_lease_until=trusted_claim.lease_until,
        consumed_claim=trusted_claim,
        consumed_claim_fingerprint=canonical_hash(trusted_claim),
        consumed_idempotent=True,
        worker_id=None,
        execution_fence=None,
        lease_until=None,
        active_claim=None,
        active_claim_fingerprint=None,
        receipt=_receipt_with(command_state.receipt, status="consumed")[0],
        receipt_fingerprint=_receipt_with(command_state.receipt, status="consumed")[1],
    )
    updated_run = _replace_run(
        run,
        lease_command_id=None,
        lease_owner=None,
        lease_until=None,
        commands=tuple(updated_command if item is command_state else item for item in run.commands),
    )
    return _snapshot_with_run(snapshot, updated_run), updated_command.receipt


def transition_dead_letter(
    snapshot: DriverSnapshot, claim: ExecutionClaim, *, now: datetime, reason_ref: str
) -> tuple[DriverSnapshot, RunCommandReceipt]:
    now = _validate_now(now)
    if type(reason_ref) is not str or not reason_ref or len(reason_ref) > 512:
        raise ValueError("reason_ref is required")
    run, command_state, _ = _validate_claim(snapshot, claim, now)
    if _classify(run, command_state) == "applied":
        raise RunStateConflict("an applied command cannot be dead-lettered")
    receipt, receipt_fingerprint = _receipt_with(command_state.receipt, status="dead_letter")
    updated_command = _replace_command_state(
        command_state,
        status="dead_letter",
        dead_letter_ref=reason_ref,
        worker_id=None,
        execution_fence=None,
        lease_until=None,
        active_claim=None,
        active_claim_fingerprint=None,
        receipt=receipt,
        receipt_fingerprint=receipt_fingerprint,
    )
    updated_run = _replace_run(
        run,
        lease_command_id=None,
        lease_owner=None,
        lease_until=None,
        commands=tuple(updated_command if item is command_state else item for item in run.commands),
    )
    return _snapshot_with_run(snapshot, updated_run), receipt


def transition_record_applied(
    snapshot: DriverSnapshot, metadata: AppliedCommandMetadata, *, now: datetime
) -> tuple[DriverSnapshot, AppliedCommandMetadata]:
    now = _validate_now(now)
    metadata, _ = _strict_metadata(metadata)
    found = _find_command(snapshot, metadata.command_id)
    if found is None:
        raise CommandNotFound(str(metadata.command_id))
    run, command_state = found
    if (
        metadata.tenant_id != run.tenant_id
        or metadata.tenant_id != command_state.receipt.tenant_id
        or metadata.run_id != command_state.receipt.run_id
        or metadata.command_id != command_state.receipt.command_id
        or metadata.command_seq != command_state.receipt.command_seq
        or metadata.command_digest != command_state.receipt.command_digest
        or metadata.runtime_build_hash != run.runtime_build_hash
        or metadata.execution_fence < 1
    ):
        raise ValueError("applied command metadata is not bound to the command")
    if (
        command_state.status != "leased"
        or command_state.active_claim is None
        or run.lease_command_id != metadata.command_id
        or run.lease_until is None
        or run.lease_until <= now
        or command_state.lease_until is None
        or command_state.lease_until <= now
        or run.execution_fence != metadata.execution_fence
        or command_state.execution_fence != metadata.execution_fence
    ):
        raise StaleExecutionFence("applied command lease or execution fence is stale")
    if isinstance(command_state.command, ResumeRun):
        resume_interrupt = command_state.command.interrupt
        if (
            run.interrupt is None
            or canonical_hash(run.interrupt) != canonical_hash(resume_interrupt)
            or resume_interrupt.nonce_hash in run.consumed_interrupt_nonces
        ):
            raise RunStateConflict("resume interrupt nonce is no longer available")
    current = _applied_for_seq(run, metadata.command_seq)
    if current is not None:
        _, current_fingerprint = _strict_metadata(current)
        _, metadata_fingerprint = _strict_metadata(metadata)
        if current_fingerprint != metadata_fingerprint:
            raise ValueError("same command sequence has different applied metadata")
        raise RunStateConflict("applied command metadata was already recorded")

    applied = _with_applied(run, metadata, command_state.active_claim)
    updated_run = _replace_run(
        run,
        applied=applied,
        consumed_interrupt_nonces=(
            frozenset((*run.consumed_interrupt_nonces, command_state.command.interrupt.nonce_hash))
            if isinstance(command_state.command, ResumeRun)
            else run.consumed_interrupt_nonces
        ),
        interrupt=None if isinstance(command_state.command, ResumeRun) else run.interrupt,
        interrupt_fingerprint=None if isinstance(command_state.command, ResumeRun) else run.interrupt_fingerprint,
    )
    return _snapshot_with_run(snapshot, updated_run), metadata


def transition_set_run_status(
    snapshot: DriverSnapshot, tenant_id: str, run_id: UUID, status: object
) -> tuple[DriverSnapshot, None]:
    if type(status) is not str or status not in VALID_RUN_STATUSES:
        raise RunStateConflict("run status is invalid")
    run = snapshot.runs.get(run_id)
    if run is None:
        raise RunNotFound(str(run_id))
    if run.tenant_id != tenant_id:
        raise CommandConflict("tenant does not match the run binding")
    return _snapshot_with_run(snapshot, _replace_run(run, status=cast(RunStatus, status))), None


def _validate_ref(value: object, label: str) -> str:
    if type(value) is not str or not value or len(value) > 512:
        raise ValueError(f"{label} is required")
    return value


def transition_set_run_wait(
    snapshot: DriverSnapshot,
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
) -> tuple[DriverSnapshot, None]:
    _validate_ref(wait_ref, "wait_ref")
    _validate_ref(source_ref, "source_ref")
    _validate_ref(source_fact_version, "source_fact_version")
    _validate_ref(payload_ref, "payload_ref")
    for value, label in (
        (wait_hash, "wait_hash"),
        (source_fact_hash, "source_fact_hash"),
        (payload_hash, "payload_hash"),
    ):
        if not _is_sha256(value):
            raise ValueError(f"{label} must be lowercase SHA-256")
    if type(wait_kind) is not str or wait_kind not in WAIT_KINDS:
        raise ValueError("wait_kind is not in the closed runtime union")
    run = snapshot.runs.get(run_id)
    if run is None:
        raise RunNotFound(str(run_id))
    if run.tenant_id != tenant_id:
        raise CommandConflict("tenant does not match the run binding")
    updated = _replace_run(
        run,
        wait_ref=wait_ref,
        wait_hash=wait_hash,
        wait_kind=wait_kind,
        wait_source_ref=source_ref,
        wait_source_fact_version=source_fact_version,
        wait_source_fact_hash=source_fact_hash,
        wait_payload_ref=payload_ref,
        wait_payload_hash=payload_hash,
        status="waiting_action_result" if wait_kind == "action_result" else "waiting_child_result",
    )
    return _snapshot_with_run(snapshot, updated), None


def transition_set_user_interrupt(
    snapshot: DriverSnapshot, tenant_id: str, run_id: UUID, binding: InterruptBinding
) -> tuple[DriverSnapshot, None]:
    binding, fingerprint = _strict_identity(binding, InterruptBinding)
    run = snapshot.runs.get(run_id)
    if run is None:
        raise RunNotFound(str(run_id))
    if run.tenant_id != tenant_id:
        raise CommandConflict("tenant does not match the run binding")
    return _snapshot_with_run(
        snapshot,
        _replace_run(run, interrupt=binding, interrupt_fingerprint=fingerprint, status="waiting_user_input"),
    ), None


def transition_reconcile(
    snapshot: DriverSnapshot, tenant_id: str, run_id: UUID, *, now: datetime
) -> tuple[DriverSnapshot, RunCommandReceipt | None]:
    now = _validate_now(now)
    run = snapshot.runs.get(run_id)
    if run is None:
        raise RunNotFound(str(run_id))
    if run.tenant_id != tenant_id:
        raise CommandConflict("tenant does not match the run binding")
    if run.status != "running" or _lease_valid(run, now):
        return snapshot, None
    outstanding = [item for item in run.commands if item.status in {"pending", "leased"}]
    if outstanding:
        outstanding.sort(key=lambda item: item.receipt.command_seq)
        return snapshot, outstanding[0].receipt
    command_id = derive_continue_command_id(tenant_id, run_id, run.revision)
    command = ContinueRun(
        command_id=command_id,
        tenant_id=tenant_id,
        run_id=run_id,
        runtime_build_hash=run.runtime_build_hash,
        command_digest=canonical_hash(
            {
                "command_id": command_id,
                "command_type": "continue",
                "revision": run.revision,
                "run_id": run_id,
                "runtime_build_hash": run.runtime_build_hash,
                "tenant_id": tenant_id,
            }
        ),
        command_type="continue",
        command_schema_version="continue.v1",
        revision=run.revision,
    )
    return transition_dispatch(snapshot, command, authority_issuer=_RECONCILER_ISSUER)


def transition_set_superseded_observation(snapshot: DriverSnapshot, command_id: UUID) -> bool:
    run, command = _find_command_or_raise(snapshot, command_id)
    return command.superseded


def transition_get_status(snapshot: DriverSnapshot, tenant_id: str, run_id: UUID) -> RunStatus:
    run = snapshot.runs.get(run_id)
    if run is None:
        raise RunNotFound(str(run_id))
    if run.tenant_id != tenant_id:
        raise CommandConflict("tenant does not match the run binding")
    return run.status


def transition_is_applied(
    snapshot: DriverSnapshot,
    command: ExecutionCommand | ExecutionClaim,
    *,
    now: datetime,
) -> bool:
    now = _validate_now(now)
    if type(command) is ExecutionClaim:
        trusted_claim, _ = _strict_claim(command)
        run, command_state, _ = _validate_claim(snapshot, trusted_claim, now)
    else:
        trusted_command, incoming_fingerprint = _strict_command(command)
        found = _find_command(snapshot, trusted_command.command_id)
        if found is None:
            raise CommandNotFound(str(trusted_command.command_id))
        run, command_state = found
        if command_state.command_fingerprint != incoming_fingerprint:
            if type(trusted_command) is RunSignal:
                raise RunSignalConflict("command binding does not match the stored signal")
            raise CommandConflict("command binding does not match the stored command")
    return _classify(run, command_state) == "applied"


def transition_applied_for(snapshot: DriverSnapshot, run_id: UUID) -> AppliedCommandMetadata | None:
    run = snapshot.runs.get(run_id)
    if run is None or not run.applied:
        return None
    return run.applied[-1].metadata


__all__ = [
    "AppliedRecord",
    "CancelFenceRevoked",
    "ClaimFenceIssued",
    "CommandAggregate",
    "CommandRelation",
    "DriverSnapshot",
    "FenceEvent",
    "MAX_LEASE_SECONDS",
    "RunAggregate",
    "WAIT_KINDS",
    "empty_snapshot",
    "transition_applied_for",
    "transition_claim",
    "transition_consume",
    "transition_dead_letter",
    "transition_dispatch",
    "transition_get_status",
    "transition_heartbeat",
    "transition_is_applied",
    "transition_reconcile",
    "transition_record_applied",
    "transition_set_run_status",
    "transition_set_run_wait",
    "transition_set_superseded_observation",
    "transition_set_user_interrupt",
    "validate_lease_seconds",
    "validate_snapshot",
]
