from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import cast
from uuid import UUID, uuid4

import app.execution.driver as driver_module
import app.execution.state_machine as state_machine
import pytest
from app.execution.driver import (
    MAX_TIME_ADVANCE_SECONDS,
    ActionCompletionPayload,
    AppliedCommandMetadata,
    CancelRun,
    ChildCompletionPayload,
    CommandConflict,
    CommandNotFound,
    ContinueRun,
    DeterministicExecutionDriver,
    ExecutionClaim,
    ExecutionCommand,
    ExecutionDriver,
    ExecutionDriverError,
    InternalDispatchAuthority,
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
    _strict_command,
    _strict_identity,
    derive_continue_command_id,
    derive_signal_command_id,
    derive_signal_id,
)
from app.execution.state_machine import CommandAggregate, DriverSnapshot, FenceEvent, RunAggregate
from pydantic import BaseModel, TypeAdapter, ValidationError

TENANT = "tenant-a"
BUILD = "b" * 64
BUILD_V1 = "1" * 64
BUILD_V2 = "2" * 64
BUILD_NEW = "3" * 64
HASH_A = "a" * 64
HASH_B = "c" * 64
HASH_C = "d" * 64
RUN = UUID("00000000-0000-0000-0000-000000000001")


def _run(driver: DeterministicExecutionDriver) -> RunAggregate:
    return driver._snapshot.runs[RUN]


def _install_run(driver: DeterministicExecutionDriver, run: RunAggregate) -> None:
    """Install malformed state only through the private state-machine seam."""

    object.__setattr__(driver, "_snapshot", DriverSnapshot({RUN: run}))


def _install_snapshot(driver: DeterministicExecutionDriver, snapshot: DriverSnapshot) -> None:
    object.__setattr__(driver, "_snapshot", snapshot)


def _alternate_hash(value: str) -> str:
    candidate = "0" * 64
    return candidate if value != candidate else "1" * 64


def _polluted_applied_record(record: state_machine.AppliedRecord, field: str) -> state_machine.AppliedRecord:
    if field == "claim_fingerprint":
        return replace(record, claim_fingerprint=_alternate_hash(record.claim_fingerprint))
    if field == "claim.worker_id":
        polluted_claim = record.claim.model_copy(update={"worker_id": "worker-polluted"})
    elif field == "claim.lease_until":
        polluted_claim = record.claim.model_copy(
            update={"lease_until": record.claim.lease_until + timedelta(seconds=1)}
        )
    elif field == "claim.execution_fence":
        polluted_claim = record.claim.model_copy(update={"execution_fence": record.claim.execution_fence + 1})
    else:
        raise AssertionError(f"unhandled applied-record pollution: {field}")
    _, claim_fingerprint = _strict_identity(polluted_claim, ExecutionClaim)
    return replace(record, claim=polluted_claim, claim_fingerprint=claim_fingerprint)


def _command(driver: DeterministicExecutionDriver, command_id: UUID) -> CommandAggregate:
    for item in _run(driver).commands:
        if item.receipt.command_id == command_id:
            return item
    raise AssertionError(f"unknown command {command_id}")


def _interrupt(*, nonce_hash: str = HASH_A) -> InterruptBinding:
    return InterruptBinding(
        interrupt_ref="interrupt://1",
        interrupt_hash=HASH_B,
        checkpoint_ref="checkpoint://1",
        checkpoint_hash=HASH_C,
        interrupt_schema_ref="schema://interrupt/v1",
        interrupt_schema_hash=HASH_A,
        nonce_hash=nonce_hash,
    )


def _start(
    *,
    command_id: UUID | None = None,
    digest: str = HASH_A,
    tenant_id: str = TENANT,
    run_id: UUID = RUN,
    runtime_build_hash: str = BUILD,
    payload_hash: str = HASH_B,
    payload_ref: str = "artifact://start/1",
) -> StartRun:
    return StartRun(
        command_id=command_id or uuid4(),
        tenant_id=tenant_id,
        run_id=run_id,
        runtime_build_hash=runtime_build_hash,
        command_digest=digest,
        command_type="start",
        command_schema_version="start.v1",
        payload_ref=payload_ref,
        payload_hash=payload_hash,
    )


def _continue(*, command_id: UUID | None = None, revision: int = 0) -> ContinueRun:
    return ContinueRun(
        command_id=command_id or derive_continue_command_id(TENANT, RUN, revision),
        tenant_id=TENANT,
        run_id=RUN,
        runtime_build_hash=BUILD,
        command_digest=HASH_C,
        command_type="continue",
        command_schema_version="continue.v1",
        revision=revision,
    )


def _aggregate(command: ExecutionCommand, command_seq: int) -> CommandAggregate:
    command, command_fingerprint = _strict_command(command)
    receipt = RunCommandReceipt(
        command_id=command.command_id,
        tenant_id=command.tenant_id,
        run_id=command.run_id,
        command_seq=command_seq,
        command_type=command.command_type,
        command_schema_version=command.command_schema_version,
        command_digest=command.command_digest,
        runtime_build_hash=command.runtime_build_hash,
        status="pending",
    )
    receipt, receipt_fingerprint = _strict_identity(receipt, RunCommandReceipt)
    return CommandAggregate(
        command=command,
        receipt=receipt,
        command_fingerprint=command_fingerprint,
        receipt_fingerprint=receipt_fingerprint,
    )


def _resume(
    *,
    command_id: UUID | None = None,
    digest: str = HASH_C,
    expected_revision: int = 1,
    interrupt: InterruptBinding | None = None,
) -> ResumeRun:
    return ResumeRun(
        command_id=command_id or uuid4(),
        tenant_id=TENANT,
        run_id=RUN,
        runtime_build_hash=BUILD,
        command_digest=digest,
        command_type="resume",
        command_schema_version="resume.v1",
        expected_revision=expected_revision,
        input_ref="artifact://resume/1",
        input_hash=HASH_B,
        interrupt=interrupt or _interrupt(),
    )


def _cancel(*, command_id: UUID | None = None, digest: str = HASH_C, expected_revision: int = 1) -> CancelRun:
    return CancelRun(
        command_id=command_id or uuid4(),
        tenant_id=TENANT,
        run_id=RUN,
        runtime_build_hash=BUILD,
        command_digest=digest,
        command_type="cancel",
        command_schema_version="cancel.v1",
        expected_revision=expected_revision,
    )


def _signal(
    *,
    command_id: UUID | None = None,
    digest: str = HASH_C,
    signal_id: UUID | None = None,
    wait_hash: str = HASH_A,
    source_ref: str = "action://1",
    source_fact_hash: str = HASH_A,
) -> RunSignal:
    resolved_signal_id = signal_id or derive_signal_id(source_ref, "v1", source_fact_hash)
    return RunSignal(
        command_id=command_id or uuid4(),
        tenant_id=TENANT,
        run_id=RUN,
        runtime_build_hash=BUILD,
        command_digest=digest,
        command_type="signal",
        command_schema_version="signal.v1",
        signal_id=resolved_signal_id,
        wait_ref="wait://1",
        wait_hash=wait_hash,
        payload=ActionCompletionPayload(
            source_ref=source_ref,
            source_fact_version="v1",
            source_fact_hash=source_fact_hash,
            payload_ref="artifact://action-result/1",
            payload_hash=HASH_B,
            payload_type="action_completed",
        ),
    )


async def _apply_and_consume(
    driver: DeterministicExecutionDriver,
    claim: ExecutionClaim,
    *,
    checkpoint_ref: str = "checkpoint://test",
    checkpoint_hash: str = HASH_B,
) -> AppliedCommandMetadata:
    """Build the durable applied proof before acknowledging a lease."""

    metadata = AppliedCommandMetadata(
        tenant_id=claim.tenant_id,
        run_id=claim.run_id,
        command_id=claim.command_id,
        command_seq=claim.command_seq,
        command_digest=claim.command_digest,
        checkpoint_ref=checkpoint_ref,
        checkpoint_hash=checkpoint_hash,
        runtime_build_hash=claim.runtime_build_hash,
        execution_fence=claim.execution_fence,
    )
    await driver.record_applied(metadata)
    await driver.consume(claim)
    return metadata


@pytest.mark.asyncio
async def test_run_fence_high_water_cannot_be_rolled_back_after_completed_claims() -> None:
    """A completed writer generation must remain reserved run-wide."""

    driver = DeterministicExecutionDriver()
    await driver.dispatch(_start())
    start_claim = await driver.claim("worker-1", BUILD)
    assert start_claim is not None and start_claim.execution_fence == 1
    await _apply_and_consume(driver, start_claim, checkpoint_ref="checkpoint://start")

    await driver._fixture_set_run_status(TENANT, RUN, "running")
    first_continue = await driver.reconcile(TENANT, RUN)
    assert first_continue is not None
    continue_claim = await driver.claim("worker-1", BUILD)
    assert continue_claim is not None and continue_claim.execution_fence == 2
    await _apply_and_consume(driver, continue_claim, checkpoint_ref="checkpoint://continue-1")

    await driver._fixture_set_run_status(TENANT, RUN, "running")
    pending = await driver.reconcile(TENANT, RUN)
    assert pending is not None and pending.command_seq == 2

    run = _run(driver)
    assert "execution_fence" not in RunAggregate.__dataclass_fields__
    with pytest.raises(TypeError):
        state_machine._replace_run(run, execution_fence=1)
    _install_run(driver, replace(run, fence_events=run.fence_events[:-1]))
    before = driver._snapshot
    with pytest.raises(ExecutionDriverError):
        state_machine.validate_snapshot(driver._snapshot)
    assert driver._snapshot is before
    with pytest.raises(ExecutionDriverError):
        await driver.claim("worker-2", BUILD)
    assert driver._snapshot is before


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["lower", "delete", "reorder", "duplicate", "cross_command"])
async def test_fence_ledger_rejects_sequence_and_cross_command_corruption(corruption: str) -> None:
    driver = DeterministicExecutionDriver(lease_seconds=1)
    start = await driver.dispatch(_start())
    first = await driver.claim("worker-1", BUILD)
    assert first is not None
    driver.advance_time(2)
    second = await driver.claim("worker-2", BUILD)
    assert second is not None
    events = _run(driver).fence_events
    assert len(events) == 2

    if corruption == "lower":
        malformed_events: tuple[FenceEvent, ...] = (replace(events[0], fence=0), events[1])
    elif corruption == "delete":
        malformed_events = events[1:]
    elif corruption == "reorder":
        malformed_events = tuple(reversed(events))
    elif corruption == "duplicate":
        malformed_events = (events[0], events[0])
    elif corruption == "cross_command":
        forged_command = uuid4()
        assert type(events[1]) is state_machine.ClaimFenceIssued
        second_event = events[1]
        malformed_events = (events[0], replace(second_event, command_id=forged_command))
    else:
        raise AssertionError(corruption)

    _install_run(driver, replace(_run(driver), fence_events=malformed_events))
    before = driver._snapshot
    with pytest.raises(ExecutionDriverError):
        await driver.dispatch(_start(command_id=start.command_id))
    assert driver._snapshot is before


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["bool_fence", "empty_worker", "bad_fingerprint", "orphan_issuance"])
async def test_claim_fence_event_fields_and_reverse_closure_are_fail_closed(corruption: str) -> None:
    driver = DeterministicExecutionDriver()
    await driver.dispatch(_start())
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    event = _run(driver).fence_events[0]
    assert type(event) is state_machine.ClaimFenceIssued
    if corruption == "bool_fence":
        events: tuple[FenceEvent, ...] = (replace(event, fence=True),)
    elif corruption == "empty_worker":
        events = (replace(event, worker_id=""),)
    elif corruption == "bad_fingerprint":
        events = (replace(event, claim_fingerprint="bad"),)
    elif corruption == "orphan_issuance":
        events = (
            event,
            state_machine.ClaimFenceIssued(
                fence=2,
                command_id=event.command_id,
                worker_id=event.worker_id,
                claim_fingerprint=event.claim_fingerprint,
            ),
        )
    else:
        raise AssertionError(corruption)
    _install_run(driver, replace(_run(driver), fence_events=events))
    before = driver._snapshot
    with pytest.raises(ExecutionDriverError):
        await driver.applied_for(RUN)
    assert driver._snapshot is before


@pytest.mark.asyncio
async def test_cancel_revocation_event_cannot_be_duplicated() -> None:
    driver = DeterministicExecutionDriver()
    await driver.dispatch(_start())
    await driver.dispatch(_cancel(expected_revision=1))
    event = _run(driver).fence_events[0]
    assert type(event) is state_machine.CancelFenceRevoked
    _install_run(driver, replace(_run(driver), fence_events=(event, replace(event, fence=2))))
    before = driver._snapshot
    with pytest.raises(ExecutionDriverError):
        await driver.applied_for(RUN)
    assert driver._snapshot is before


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["missing", "unknown_ref", "non_cancel_ref"])
async def test_cancel_revocation_requires_reverse_closure_to_exact_cancel(corruption: str) -> None:
    driver = DeterministicExecutionDriver()
    start = await driver.dispatch(_start())
    await driver.dispatch(_cancel(expected_revision=1))
    event = _run(driver).fence_events[0]
    assert type(event) is state_machine.CancelFenceRevoked
    if corruption == "missing":
        events: tuple[FenceEvent, ...] = ()
    elif corruption == "unknown_ref":
        events = (replace(event, cancel_command_id=uuid4()),)
    elif corruption == "non_cancel_ref":
        events = (replace(event, cancel_command_id=start.command_id),)
    else:
        raise AssertionError(corruption)
    _install_run(driver, replace(_run(driver), fence_events=events))
    before = driver._snapshot
    with pytest.raises(ExecutionDriverError):
        state_machine.validate_snapshot(before)
    with pytest.raises(ExecutionDriverError):
        await driver.applied_for(RUN)
    assert driver._snapshot is before


@pytest.mark.asyncio
async def test_fence_ledger_rejects_real_commands_reordered_across_command_sequence() -> None:
    """Complete per-command closure cannot hide a run-wide temporal rollback."""

    driver = DeterministicExecutionDriver()
    start = await driver.dispatch(_start())
    start_claim = await driver.claim("worker-start", BUILD)
    assert start_claim is not None
    await _apply_and_consume(driver, start_claim, checkpoint_ref="checkpoint://start")
    await driver._fixture_set_run_status(TENANT, RUN, "running")
    continued = await driver.reconcile(TENANT, RUN)
    assert continued is not None
    continue_claim = await driver.claim("worker-continue", BUILD)
    assert continue_claim is not None
    await _apply_and_consume(driver, continue_claim, checkpoint_ref="checkpoint://continue")

    run = _run(driver)
    target_fences = {start.command_id: 2, continued.command_id: 1}
    rebuilt_commands: list[CommandAggregate] = []
    rebuilt_claims: dict[UUID, tuple[ExecutionClaim, str]] = {}
    for command_state in run.commands:
        assert command_state.consumed_claim is not None
        rebuilt_claim, rebuilt_claim_fingerprint = _strict_identity(
            command_state.consumed_claim.model_copy(
                update={"execution_fence": target_fences[command_state.receipt.command_id]}
            ),
            ExecutionClaim,
        )
        rebuilt_receipt, rebuilt_receipt_fingerprint = _strict_identity(
            command_state.receipt.model_copy(update={"execution_fence": rebuilt_claim.execution_fence}),
            RunCommandReceipt,
        )
        rebuilt_claims[rebuilt_claim.command_id] = (rebuilt_claim, rebuilt_claim_fingerprint)
        rebuilt_commands.append(
            replace(
                command_state,
                receipt=rebuilt_receipt,
                receipt_fingerprint=rebuilt_receipt_fingerprint,
                consumed_claim=rebuilt_claim,
                consumed_claim_fingerprint=rebuilt_claim_fingerprint,
                consumed_execution_fence=rebuilt_claim.execution_fence,
                claim_history=(rebuilt_claim,),
                claim_history_fingerprints=(rebuilt_claim_fingerprint,),
            )
        )
    rebuilt_applied: list[state_machine.AppliedRecord] = []
    for record in run.applied:
        fence = target_fences[record.metadata.command_id]
        metadata, fingerprint = _strict_identity(
            record.metadata.model_copy(update={"execution_fence": fence}),
            AppliedCommandMetadata,
        )
        apply_claim, apply_claim_fingerprint = rebuilt_claims[record.metadata.command_id]
        rebuilt_applied.append(
            state_machine.AppliedRecord(
                metadata=metadata,
                fingerprint=fingerprint,
                claim=apply_claim,
                claim_fingerprint=apply_claim_fingerprint,
            )
        )
    continue_rebuilt, continue_fingerprint = rebuilt_claims[continued.command_id]
    start_rebuilt, start_fingerprint = rebuilt_claims[start.command_id]
    reordered_events: tuple[FenceEvent, ...] = (
        state_machine.ClaimFenceIssued(1, continued.command_id, continue_rebuilt.worker_id, continue_fingerprint),
        state_machine.ClaimFenceIssued(2, start.command_id, start_rebuilt.worker_id, start_fingerprint),
    )
    _install_run(
        driver,
        replace(
            run,
            commands=tuple(rebuilt_commands),
            applied=tuple(rebuilt_applied),
            fence_events=reordered_events,
        ),
    )
    before = driver._snapshot
    with pytest.raises(ExecutionDriverError):
        state_machine.validate_snapshot(before)
    with pytest.raises(ExecutionDriverError):
        await driver.applied_for(RUN)
    assert driver._snapshot is before


@pytest.mark.asyncio
async def test_fence_ledger_rejects_two_complete_cancel_commands_and_revocations() -> None:
    driver = DeterministicExecutionDriver()
    await driver.dispatch(_start())
    first_cancel = await driver.dispatch(_cancel(expected_revision=1))
    second_cancel_command = _cancel(expected_revision=2)
    second_cancel = _aggregate(second_cancel_command, 2)
    run = _run(driver)
    _install_run(
        driver,
        replace(
            run,
            revision=3,
            next_command_seq=3,
            commands=(*run.commands, second_cancel),
            fence_events=(
                run.fence_events[0],
                state_machine.CancelFenceRevoked(2, second_cancel_command.command_id),
            ),
        ),
    )
    before = driver._snapshot
    assert first_cancel.command_type == "cancel"
    with pytest.raises(ExecutionDriverError):
        state_machine.validate_snapshot(before)
    with pytest.raises(ExecutionDriverError):
        await driver.get_run_status(TENANT, RUN)
    assert driver._snapshot is before

    clean = DeterministicExecutionDriver()
    await clean.dispatch(_start())
    clean_before = clean._snapshot
    with pytest.raises(ExecutionDriverError):
        await clean.get_run_status(_ExplodingStr(TENANT), RUN)
    with pytest.raises(ExecutionDriverError):
        await clean.applied_for(cast(UUID, _ExplodingUUID(RUN.hex)))
    assert clean._snapshot is clean_before


@pytest.mark.asyncio
async def test_post_cancel_non_cancel_claim_is_rejected_before_heartbeat_swap() -> None:
    driver = DeterministicExecutionDriver(lease_seconds=30)
    await driver.dispatch(_start())
    cancel = await driver.dispatch(_cancel(expected_revision=1))
    continued_command = _continue(revision=2)
    continued = _aggregate(continued_command, 2)
    lease_until = datetime.now(UTC) + timedelta(hours=1)
    claim, claim_fingerprint = _strict_identity(
        ExecutionClaim(
            command_id=continued_command.command_id,
            tenant_id=TENANT,
            run_id=RUN,
            command_seq=2,
            command_digest=continued_command.command_digest,
            runtime_build_hash=BUILD,
            worker_id="worker-after-cancel",
            execution_fence=2,
            lease_until=lease_until,
        ),
        ExecutionClaim,
    )
    receipt, receipt_fingerprint = _strict_identity(
        continued.receipt.model_copy(
            update={
                "status": "leased",
                "lease_owner": claim.worker_id,
                "execution_fence": claim.execution_fence,
                "lease_until": claim.lease_until,
            }
        ),
        RunCommandReceipt,
    )
    leased_continue = replace(
        continued,
        status="leased",
        worker_id=claim.worker_id,
        execution_fence=claim.execution_fence,
        lease_until=claim.lease_until,
        active_claim=claim,
        active_claim_fingerprint=claim_fingerprint,
        claim_history=(claim,),
        claim_history_fingerprints=(claim_fingerprint,),
        receipt=receipt,
        receipt_fingerprint=receipt_fingerprint,
    )
    run = _run(driver)
    assert type(run.fence_events[0]) is state_machine.CancelFenceRevoked
    _install_run(
        driver,
        replace(
            run,
            revision=3,
            next_command_seq=3,
            commands=(*run.commands, leased_continue),
            fence_events=(
                run.fence_events[0],
                state_machine.ClaimFenceIssued(2, claim.command_id, claim.worker_id, claim_fingerprint),
            ),
            lease_command_id=claim.command_id,
            lease_owner=claim.worker_id,
            lease_until=claim.lease_until,
        ),
    )
    before = driver._snapshot
    assert _command(driver, cancel.command_id).status == "pending"
    with pytest.raises(ExecutionDriverError):
        state_machine.validate_snapshot(before)
    with pytest.raises(ExecutionDriverError):
        await driver.heartbeat(claim)
    assert driver._snapshot is before


@pytest.mark.asyncio
async def test_post_revocation_claim_for_real_older_command_hits_temporal_guard() -> None:
    driver = DeterministicExecutionDriver()
    await driver.dispatch(_start())
    old_claim = await driver.claim("worker-old", BUILD)
    assert old_claim is not None
    await driver.dispatch(_cancel(expected_revision=1))
    late_claim, late_fingerprint = _strict_identity(
        old_claim.model_copy(
            update={
                "worker_id": "worker-forged-after-cancel",
                "execution_fence": 3,
                "lease_until": old_claim.lease_until + timedelta(minutes=1),
            }
        ),
        ExecutionClaim,
    )
    run = _run(driver)
    old_command = _command(driver, old_claim.command_id)
    rebuilt_receipt, rebuilt_receipt_fingerprint = _strict_identity(
        old_command.receipt.model_copy(
            update={
                "lease_owner": late_claim.worker_id,
                "execution_fence": late_claim.execution_fence,
                "lease_until": late_claim.lease_until,
            }
        ),
        RunCommandReceipt,
    )
    rebuilt_old_command = replace(
        old_command,
        receipt=rebuilt_receipt,
        receipt_fingerprint=rebuilt_receipt_fingerprint,
        consumed_claim=late_claim,
        consumed_claim_fingerprint=late_fingerprint,
        consumed_worker_id=late_claim.worker_id,
        consumed_execution_fence=late_claim.execution_fence,
        consumed_lease_until=late_claim.lease_until,
        claim_history=(*old_command.claim_history, late_claim),
        claim_history_fingerprints=(*old_command.claim_history_fingerprints, late_fingerprint),
    )
    _install_run(
        driver,
        replace(
            run,
            commands=tuple(rebuilt_old_command if item is old_command else item for item in run.commands),
            fence_events=(
                *run.fence_events,
                state_machine.ClaimFenceIssued(
                    3,
                    late_claim.command_id,
                    late_claim.worker_id,
                    late_fingerprint,
                ),
            ),
        ),
    )
    before = driver._snapshot
    with pytest.raises(ExecutionDriverError) as exc_info:
        state_machine.validate_snapshot(before)
    assert exc_info.value.__cause__ is not None
    assert "post-revocation claims" in str(exc_info.value.__cause__)
    with pytest.raises(ExecutionDriverError):
        await driver.applied_for(RUN)
    assert driver._snapshot is before


@pytest.mark.asyncio
async def test_cancel_claim_cannot_appear_before_its_revocation() -> None:
    driver = DeterministicExecutionDriver()
    await driver.dispatch(_start())
    cancel = await driver.dispatch(_cancel(expected_revision=1))
    lease_until = datetime.now(UTC) + timedelta(hours=1)
    claim, claim_fingerprint = _strict_identity(
        ExecutionClaim(
            command_id=cancel.command_id,
            tenant_id=TENANT,
            run_id=RUN,
            command_seq=cancel.command_seq,
            command_digest=cancel.command_digest,
            runtime_build_hash=BUILD,
            worker_id="worker-cancel-before-revocation",
            execution_fence=1,
            lease_until=lease_until,
        ),
        ExecutionClaim,
    )
    cancel_state = _command(driver, cancel.command_id)
    receipt, receipt_fingerprint = _strict_identity(
        cancel_state.receipt.model_copy(
            update={
                "status": "leased",
                "lease_owner": claim.worker_id,
                "execution_fence": claim.execution_fence,
                "lease_until": claim.lease_until,
            }
        ),
        RunCommandReceipt,
    )
    leased_cancel = replace(
        cancel_state,
        status="leased",
        worker_id=claim.worker_id,
        execution_fence=claim.execution_fence,
        lease_until=claim.lease_until,
        active_claim=claim,
        active_claim_fingerprint=claim_fingerprint,
        claim_history=(claim,),
        claim_history_fingerprints=(claim_fingerprint,),
        receipt=receipt,
        receipt_fingerprint=receipt_fingerprint,
    )
    run = _run(driver)
    _install_run(
        driver,
        replace(
            run,
            commands=tuple(leased_cancel if item is cancel_state else item for item in run.commands),
            fence_events=(
                state_machine.ClaimFenceIssued(1, claim.command_id, claim.worker_id, claim_fingerprint),
                state_machine.CancelFenceRevoked(2, cancel.command_id),
            ),
            lease_command_id=claim.command_id,
            lease_owner=claim.worker_id,
            lease_until=claim.lease_until,
        ),
    )
    with pytest.raises(ExecutionDriverError) as exc_info:
        state_machine.validate_snapshot(driver._snapshot)
    assert exc_info.value.__cause__ is not None
    assert "precedes the command acceptance" in str(exc_info.value.__cause__)


@pytest.mark.asyncio
async def test_cancel_revocation_cannot_leave_real_older_command_pending() -> None:
    driver = DeterministicExecutionDriver()
    await driver.dispatch(_start())
    original_start = _run(driver).commands[0]
    await driver.dispatch(_cancel(expected_revision=1))
    run = _run(driver)
    _install_run(driver, replace(run, commands=(original_start, run.commands[1])))
    before = driver._snapshot
    with pytest.raises(ExecutionDriverError) as exc_info:
        state_machine.validate_snapshot(before)
    assert exc_info.value.__cause__ is not None
    assert "did not supersede an older outstanding command" in str(exc_info.value.__cause__)
    with pytest.raises(ExecutionDriverError):
        await driver.applied_for(RUN)
    assert driver._snapshot is before


@pytest.mark.asyncio
async def test_unreadable_last_fence_event_is_wrapped_before_heartbeat_reads_high_water() -> None:
    driver = DeterministicExecutionDriver()
    await driver.dispatch(_start())
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    _install_run(driver, replace(_run(driver), fence_events=cast(tuple[FenceEvent, ...], (object(),))))
    before = driver._snapshot
    with pytest.raises(ExecutionDriverError):
        state_machine.validate_snapshot(before)
    with pytest.raises(ExecutionDriverError):
        await driver.heartbeat(claim)
    assert driver._snapshot is before


@pytest.mark.asyncio
async def test_heartbeat_is_same_claim_only_and_must_strictly_extend_lease() -> None:
    driver = DeterministicExecutionDriver(lease_seconds=10)
    await driver.dispatch(_start())
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    before = driver._snapshot
    with pytest.raises(ExecutionDriverError):
        await driver.heartbeat(claim, lease_seconds=1)
    assert driver._snapshot is before

    driver.advance_time(1)
    renewed = await driver.heartbeat(claim, lease_seconds=10)
    assert renewed.execution_fence == claim.execution_fence
    assert renewed.command_id == claim.command_id
    assert renewed.worker_id == claim.worker_id
    assert renewed.lease_until > claim.lease_until
    assert len(_run(driver).fence_events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("leased_before_cancel", [False, True])
async def test_cancel_acceptance_and_claim_each_reserve_a_distinct_fence(
    leased_before_cancel: bool,
) -> None:
    driver = DeterministicExecutionDriver()
    await driver.dispatch(_start())
    old_claim = None
    if leased_before_cancel:
        old_claim = await driver.claim("worker-old", BUILD)
        assert old_claim is not None and old_claim.execution_fence == 1

    cancel = await driver.dispatch(_cancel(expected_revision=1))
    run = _run(driver)
    revocation = run.fence_events[-1]
    assert type(revocation) is state_machine.CancelFenceRevoked
    assert revocation.cancel_command_id == cancel.command_id
    assert revocation.fence == (2 if leased_before_cancel else 1)
    if old_claim is not None:
        with pytest.raises(StaleExecutionFence):
            await driver.heartbeat(old_claim)

    cancel_claim = await driver.claim("worker-cancel", BUILD)
    assert cancel_claim is not None
    assert cancel_claim.command_id == cancel.command_id
    assert cancel_claim.execution_fence == revocation.fence + 1
    issuance = _run(driver).fence_events[-1]
    assert type(issuance) is state_machine.ClaimFenceIssued
    assert issuance.command_id == cancel.command_id
    assert issuance.fence == cancel_claim.execution_fence


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_kind", ["applied", "consumed", "dead_letter"])
async def test_terminal_command_provenance_requires_claim_issuance_event(terminal_kind: str) -> None:
    driver = DeterministicExecutionDriver()
    receipt = await driver.dispatch(_start())
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    if terminal_kind in {"applied", "consumed"}:
        await driver.record_applied(
            AppliedCommandMetadata(
                tenant_id=TENANT,
                run_id=RUN,
                command_id=receipt.command_id,
                command_seq=receipt.command_seq,
                command_digest=receipt.command_digest,
                checkpoint_ref="checkpoint://ledger-proof",
                checkpoint_hash=HASH_B,
                runtime_build_hash=BUILD,
                execution_fence=claim.execution_fence,
            )
        )
    with pytest.raises(CommandNotFound):
        await driver.is_command_applied(_start(command_id=uuid4()))
    if terminal_kind == "consumed":
        await driver.consume(claim)
    elif terminal_kind == "dead_letter":
        await driver.dead_letter(claim, "error://ledger-proof")

    _install_run(driver, replace(_run(driver), fence_events=()))
    before = driver._snapshot
    with pytest.raises(ExecutionDriverError):
        await driver.applied_for(RUN)
    assert driver._snapshot is before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    ["read", "idempotent_dispatch", "claim", "reconcile", "record", "consume"],
)
async def test_every_driver_seam_rejects_malformed_fence_ledger_before_return_or_swap(
    operation: str,
) -> None:
    driver = DeterministicExecutionDriver()
    start_command = _start()
    await driver.dispatch(start_command)
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    metadata = await _apply_and_consume(driver, claim)
    await driver._fixture_set_run_status(TENANT, RUN, "running")
    await driver.reconcile(TENANT, RUN)

    run = _run(driver)
    _install_run(driver, replace(run, fence_events=run.fence_events[:-1]))
    before = driver._snapshot
    calls: dict[str, Callable[[], Awaitable[object]]] = {
        "read": lambda: driver.applied_for(RUN),
        "idempotent_dispatch": lambda: driver.dispatch(start_command),
        "claim": lambda: driver.claim("worker-2", BUILD),
        "reconcile": lambda: driver.reconcile(TENANT, RUN),
        "record": lambda: driver.record_applied(metadata),
        "consume": lambda: driver.consume(claim),
    }
    with pytest.raises(ExecutionDriverError):
        await calls[operation]()
    assert driver._snapshot is before


@pytest.mark.asyncio
async def test_transition_uses_frozen_authority_issuer_after_capability_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = InternalDispatchAuthority("driver_reconciler")
    driver = DeterministicExecutionDriver(internal_authorities=(authority,))
    await driver.dispatch(_start())
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    await _apply_and_consume(driver, claim)
    await driver._fixture_set_run_status(TENANT, RUN, "running")
    command = _continue(revision=1)
    original = cast(
        Callable[..., tuple[DriverSnapshot, RunCommandReceipt]],
        driver_module.transition_dispatch,  # type: ignore[attr-defined]
    )

    def mutate_after_check(
        snapshot: DriverSnapshot,
        incoming: ExecutionCommand,
        *,
        authority_issuer: object = None,
    ) -> tuple[DriverSnapshot, RunCommandReceipt]:
        object.__setattr__(authority, "issuer", "action_completion_bridge")
        return original(snapshot, incoming, authority_issuer=authority_issuer)

    monkeypatch.setattr(driver_module, "transition_dispatch", mutate_after_check)
    accepted = await driver.dispatch_internal(command, authority)
    assert accepted.command_id == command.command_id
    assert authority.issuer == "action_completion_bridge"


@pytest.mark.asyncio
async def test_dispatch_is_idempotent_and_sequences_commands() -> None:
    driver = DeterministicExecutionDriver()
    command_id = uuid4()
    first = await driver.dispatch(_start(command_id=command_id))
    retry = await driver.dispatch(_start(command_id=command_id))
    assert retry == first
    assert first.command_seq == 0

    start_claim = await driver.claim("worker-1", BUILD)
    assert start_claim is not None
    await _apply_and_consume(driver, start_claim)
    await driver._fixture_set_user_interrupt(TENANT, RUN, _interrupt())
    second = await driver.dispatch(_resume())
    assert second.command_seq == 1
    with pytest.raises(CommandConflict):
        await driver.dispatch(_start(command_id=command_id, digest=HASH_C))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformation",
    ["empty", "seq0_non_start", "misplaced_start", "duplicate_start", "owner_mismatch"],
)
async def test_genesis_closure_rejects_all_public_seams_before_state_swap(malformation: str) -> None:
    driver = DeterministicExecutionDriver()
    original = await driver.dispatch(_start(command_id=UUID(int=10)))
    run = _run(driver)
    if malformation == "empty":
        malformed = replace(run, commands=(), revision=0, next_command_seq=0)
    elif malformation == "seq0_non_start":
        malformed = replace(
            run,
            commands=(_aggregate(_continue(), 0),),
            revision=1,
            next_command_seq=1,
            status="running",
        )
    elif malformation == "misplaced_start":
        malformed = replace(
            run,
            commands=(_aggregate(_continue(), 0), _aggregate(_start(command_id=UUID(int=11)), 1)),
            revision=2,
            next_command_seq=2,
            status="running",
        )
    elif malformation == "duplicate_start":
        malformed = replace(
            run,
            commands=(run.commands[0], _aggregate(_start(command_id=UUID(int=11)), 1)),
            revision=2,
            next_command_seq=2,
        )
    elif malformation == "owner_mismatch":
        malformed = replace(run, runtime_build_hash=BUILD_NEW)
    else:
        raise AssertionError(malformation)

    async def validate() -> None:
        state_machine.validate_snapshot(driver._snapshot)

    async def dispatch() -> None:
        await driver.dispatch(_start(command_id=UUID(int=12)))

    async def claim() -> None:
        await driver.claim("worker-1", BUILD)

    async def read() -> None:
        await driver.applied_for(RUN)

    async def reconcile() -> None:
        await driver.reconcile(TENANT, RUN)

    expected_genesis = {
        "empty": "every registered run must have a genesis start command",
        "seq0_non_start": "run command sequence must begin with exactly one start command",
        "misplaced_start": "run command sequence must begin with exactly one start command",
        "duplicate_start": "run must contain exactly one start command",
        "owner_mismatch": "command ownership or receipt binding is inconsistent",
    }[malformation]

    for operation in (validate, dispatch, claim, read, reconcile):
        _install_run(driver, malformed)
        before = driver._snapshot
        with pytest.raises(ExecutionDriverError) as caught:
            await operation()
        cause = caught.value.__cause__
        assert cause is not None and expected_genesis in str(cause)
        assert driver._snapshot is before

    # The original receipt is only used to ensure the malformed registry was
    # derived from a real accepted run rather than an unbound fixture.
    assert original.command_seq == 0


def test_commit_accepts_only_the_closed_public_result_union() -> None:
    driver = DeterministicExecutionDriver()
    assert driver._commit(lambda snapshot: (snapshot, None)) is None
    assert driver._commit(lambda snapshot: (snapshot, True)) is True
    assert driver._commit(lambda snapshot: (snapshot, "running")) == "running"
    before = driver._snapshot
    with pytest.raises(ExecutionDriverError):
        driver._commit(lambda snapshot: (snapshot, object()))
    assert driver._snapshot is before


@pytest.mark.asyncio
async def test_applied_record_duplicate_is_rejected_by_the_state_transition() -> None:
    driver = DeterministicExecutionDriver()
    receipt = await driver.dispatch(_start())
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    metadata = AppliedCommandMetadata(
        tenant_id=TENANT,
        run_id=RUN,
        command_id=receipt.command_id,
        command_seq=receipt.command_seq,
        command_digest=receipt.command_digest,
        checkpoint_ref="checkpoint://duplicate",
        checkpoint_hash=HASH_B,
        runtime_build_hash=BUILD,
        execution_fence=claim.execution_fence,
    )
    await driver.record_applied(metadata)
    with pytest.raises(ValueError):
        state_machine._with_applied(_run(driver), metadata, claim)


def test_advance_time_validates_clock_range_before_changing_offset() -> None:
    naive_driver = DeterministicExecutionDriver(clock=lambda: datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        naive_driver.advance_time(1)
    assert naive_driver._offset == timedelta(0)

    overflow_driver = DeterministicExecutionDriver(clock=lambda: datetime.max.replace(tzinfo=UTC))
    with pytest.raises(ValueError):
        overflow_driver.advance_time(1)
    assert overflow_driver._offset == timedelta(0)

    bounded_driver = DeterministicExecutionDriver()
    bounded_driver.advance_time(MAX_TIME_ADVANCE_SECONDS)
    before = bounded_driver._offset
    with pytest.raises(ValueError):
        bounded_driver.advance_time(1)
    assert bounded_driver._offset == before

    overflow_numeric_driver = DeterministicExecutionDriver()
    with pytest.raises(ValueError):
        overflow_numeric_driver.advance_time(10**1000)
    assert overflow_numeric_driver._offset == timedelta(0)


@pytest.mark.parametrize("seconds", [1e-7, 1e-300, 5e-324])
def test_advance_time_rejects_positive_values_lost_by_timedelta_rounding(seconds: float) -> None:
    driver = DeterministicExecutionDriver()
    before_offset = driver._offset
    with pytest.raises(ValueError, match="representable"):
        driver.advance_time(seconds)
    assert driver._offset == before_offset


def test_command_union_is_closed_and_does_not_allow_state_patches() -> None:
    with pytest.raises(ValidationError):
        StartRun.model_validate({**_start().model_dump(), "state_patch": {"status": "succeeded"}})
    with pytest.raises(ValidationError):
        TypeAdapter(ExecutionCommand).validate_python({**_start().model_dump(), "command_type": "unknown"})
    with pytest.raises(ValidationError):
        ContinueRun.model_validate(
            {
                "command_id": uuid4(),
                "tenant_id": TENANT,
                "run_id": RUN,
                "runtime_build_hash": BUILD,
                "command_digest": HASH_C,
                "revision": 0,
                "input_ref": "user-input",
            }
        )

    signal = RunSignal(
        command_id=uuid4(),
        tenant_id=TENANT,
        run_id=RUN,
        runtime_build_hash=BUILD,
        command_digest=HASH_C,
        command_type="signal",
        command_schema_version="signal.v1",
        signal_id=derive_signal_id("action://1", "v1", HASH_A),
        wait_ref="wait://1",
        wait_hash=HASH_A,
        payload=ActionCompletionPayload(
            source_ref="action://1",
            source_fact_version="v1",
            source_fact_hash=HASH_A,
            payload_ref="artifact://action-result/1",
            payload_hash=HASH_B,
            payload_type="action_completed",
        ),
    )
    assert signal.payload.payload_ref == "artifact://action-result/1"


@pytest.mark.asyncio
async def test_claim_fence_takeover_and_stale_worker_are_rejected() -> None:
    driver = DeterministicExecutionDriver(lease_seconds=5)
    receipt = await driver.dispatch(_start())
    first = await driver.claim(worker_id="worker-1", runtime_build_hash=BUILD)
    assert first is not None
    assert first.execution_fence == 1
    assert first.command_seq == receipt.command_seq

    driver.advance_time(6)
    second = await driver.claim(worker_id="worker-2", runtime_build_hash=BUILD)
    assert second is not None
    assert second.execution_fence == 2
    with pytest.raises(StaleExecutionFence):
        await driver.heartbeat(first)
    with pytest.raises(StaleExecutionFence):
        await driver.consume(first)


@pytest.mark.asyncio
async def test_injected_clock_rollback_keeps_watermark_and_rejects_every_claim_write_path() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    observed = [t0]
    driver = DeterministicExecutionDriver(lease_seconds=5, clock=lambda: observed[0])
    await driver.dispatch(_start())
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None

    observed[0] = t0 + timedelta(seconds=6)
    before = driver._snapshot
    with pytest.raises(StaleExecutionFence):
        await driver.heartbeat(claim)
    assert driver._snapshot is before
    assert driver._clock_watermark == t0 + timedelta(seconds=6)

    metadata = AppliedCommandMetadata(
        tenant_id=claim.tenant_id,
        run_id=claim.run_id,
        command_id=claim.command_id,
        command_seq=claim.command_seq,
        command_digest=claim.command_digest,
        checkpoint_ref="checkpoint://clock",
        checkpoint_hash=HASH_B,
        runtime_build_hash=claim.runtime_build_hash,
        execution_fence=claim.execution_fence,
    )
    observed[0] = t0

    async def heartbeat() -> object:
        return await driver.heartbeat(claim)

    async def record() -> object:
        return await driver.record_applied(metadata)

    async def consume() -> object:
        return await driver.consume(claim)

    async def dead_letter() -> object:
        return await driver.dead_letter(claim, "error://clock")

    async def is_applied() -> object:
        return await driver.is_command_applied(claim)

    async def claim_again() -> object:
        return await driver.claim("worker-2", BUILD)

    for operation in (heartbeat, record, consume, dead_letter, is_applied, claim_again):
        with pytest.raises(ValueError, match="backward"):
            await operation()
        assert driver._snapshot is before
        assert driver._clock_watermark == t0 + timedelta(seconds=6)


@pytest.mark.asyncio
async def test_clock_dependency_failures_do_not_poison_or_rollback_watermark() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    state: dict[str, object] = {"mode": "raise", "value": t0}

    def clock() -> datetime:
        if state["mode"] == "raise":
            raise RuntimeError("clock unavailable")
        if state["mode"] == "naive":
            return datetime(2026, 1, 1)
        return state["value"]  # type: ignore[return-value]

    driver = DeterministicExecutionDriver(clock=clock)
    await driver.dispatch(_start())
    with pytest.raises(RuntimeError, match="clock unavailable"):
        await driver.claim("worker-1", BUILD)
    assert driver._clock_watermark is None

    state["mode"] = "naive"
    with pytest.raises(ValueError, match="timezone-aware"):
        await driver.claim("worker-1", BUILD)
    assert driver._clock_watermark is None

    state["mode"] = "valid"
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    assert driver._clock_watermark == t0

    state["value"] = t0 + timedelta(seconds=2)
    assert await driver.is_command_applied(claim) is False
    assert driver._clock_watermark == t0 + timedelta(seconds=2)

    state["value"] = t0 + timedelta(seconds=1)
    with pytest.raises(ValueError, match="backward"):
        await driver.is_command_applied(claim)
    assert driver._clock_watermark == t0 + timedelta(seconds=2)


@pytest.mark.asyncio
async def test_clock_advance_rejects_watermark_rollback_and_raw_overflow_without_mutation() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    observed = [t0]
    driver = DeterministicExecutionDriver(clock=lambda: observed[0])
    await driver.dispatch(_start())
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    observed[0] = t0 + timedelta(seconds=6)
    assert await driver.is_command_applied(claim) is False
    observed[0] = t0
    before_offset = driver._offset
    before_watermark = driver._clock_watermark
    with pytest.raises(ValueError, match="behind"):
        driver.advance_time(1)
    assert driver._offset == before_offset
    assert driver._clock_watermark == before_watermark

    overflow = DeterministicExecutionDriver(clock=lambda: datetime.max.replace(tzinfo=UTC))
    await overflow.dispatch(_start())
    object.__setattr__(overflow, "_offset", timedelta(seconds=1))
    before_watermark = overflow._clock_watermark
    with pytest.raises(ValueError, match="supported datetime range"):
        await overflow.claim("worker-1", BUILD)
    assert overflow._clock_watermark == before_watermark


def test_authority_registration_rejects_unknown_fingerprint_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="exact known capability"):
        driver_module._authority_fingerprint(cast(InternalDispatchAuthority, object()))
    unknown = InternalDispatchAuthority("driver_reconciler")
    object.__setattr__(unknown, "issuer", "unknown")
    with pytest.raises(ValueError, match="unknown"):
        driver_module._authority_fingerprint(unknown)

    registered = InternalDispatchAuthority("driver_reconciler")
    driver = DeterministicExecutionDriver(internal_authorities=(registered,))

    def fail_fingerprint(_: InternalDispatchAuthority) -> str:
        raise ValueError("fingerprint dependency failed")

    monkeypatch.setattr(driver_module, "_authority_fingerprint", fail_fingerprint)
    with pytest.raises(RunStateConflict, match="registration binding"):
        driver._check_internal_authority(registered)


@pytest.mark.asyncio
async def test_execution_driver_protocol_method_is_a_real_fail_fast_seam() -> None:
    protocol = cast(ExecutionDriver, object())
    with pytest.raises(NotImplementedError):
        # The protocol method intentionally has no default adapter.
        await ExecutionDriver.dispatch(protocol, _start())


@pytest.mark.asyncio
async def test_cancel_revokes_old_invocation_before_new_claim() -> None:
    driver = DeterministicExecutionDriver()
    start_receipt = await driver.dispatch(_start())
    old_claim = await driver.claim("worker-1", BUILD)
    assert old_claim is not None
    await driver.dispatch(_cancel())
    assert (await driver.dispatch(_start(command_id=start_receipt.command_id))).status == "consumed"
    with pytest.raises(StaleExecutionFence):
        await driver.heartbeat(old_claim)
    with pytest.raises(StaleExecutionFence):
        await driver.consume(old_claim)
    cancel_claim = await driver.claim("worker-2", BUILD)
    assert cancel_claim is not None
    assert cancel_claim.command_seq == 1
    assert cancel_claim.execution_fence > old_claim.execution_fence


@pytest.mark.asyncio
async def test_claim_requires_exact_runtime_build_without_latest_fallback() -> None:
    driver = DeterministicExecutionDriver()
    await driver.dispatch(_start())
    with pytest.raises(VersionUnavailable):
        await driver.claim(worker_id="wrong-build", runtime_build_hash=BUILD_NEW)
    assert await driver.claim(worker_id="worker-1", runtime_build_hash=BUILD) is not None


@pytest.mark.asyncio
async def test_consume_exact_once_and_applied_metadata_ordering() -> None:
    driver = DeterministicExecutionDriver()
    receipt = await driver.dispatch(_start())
    claim = await driver.claim(worker_id="worker-1", runtime_build_hash=BUILD)
    assert claim is not None
    applied = AppliedCommandMetadata(
        tenant_id=TENANT,
        run_id=RUN,
        command_id=receipt.command_id,
        command_seq=receipt.command_seq,
        command_digest=receipt.command_digest,
        checkpoint_ref="checkpoint://1",
        checkpoint_hash=HASH_B,
        runtime_build_hash=BUILD,
        execution_fence=claim.execution_fence,
    )
    await driver.record_applied(applied)
    await driver.consume(claim)
    assert (await driver.consume(claim)).status == "consumed"
    assert await driver.applied_for(RUN) == applied

    with pytest.raises(ValueError):
        await driver.record_applied(
            applied.model_copy(update={"command_seq": applied.command_seq, "command_digest": "other"})
        )
    with pytest.raises(ValueError):
        await driver.record_applied(applied.model_copy(update={"command_seq": applied.command_seq - 1}))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("tenant_id", "tenant-forged"),
        ("run_id", UUID("00000000-0000-0000-0000-000000000002")),
        ("command_seq", 99),
        ("command_id", UUID("00000000-0000-0000-0000-000000000099")),
        ("command_digest", HASH_C),
        ("runtime_build_hash", HASH_C),
        ("worker_id", "worker-forged"),
        ("execution_fence", 99),
        ("lease_until", datetime(2030, 1, 1, tzinfo=UTC)),
    ],
)
async def test_consumed_retry_requires_complete_claim_identity(field: str, replacement: object) -> None:
    driver = DeterministicExecutionDriver()
    await driver.dispatch(_start())
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    await _apply_and_consume(driver, claim)

    forged = claim.model_copy(update={field: replacement})
    with pytest.raises(ExecutionDriverError):
        await driver.consume(forged)
    assert (await driver.consume(claim)).status == "consumed"


@pytest.mark.asyncio
async def test_non_target_applied_prefix_corruption_fails_record_observe_and_consume() -> None:
    reconciler = InternalDispatchAuthority("driver_reconciler")
    driver = DeterministicExecutionDriver(internal_authorities=(reconciler,))
    await driver.dispatch(_start())
    start_claim = await driver.claim("worker-1", BUILD)
    assert start_claim is not None
    start_metadata = await _apply_and_consume(driver, start_claim)
    await driver._fixture_set_run_status(TENANT, RUN, "running")
    continue_receipt = await driver.reconcile(TENANT, RUN)
    assert continue_receipt is not None
    continue_claim = await driver.claim("worker-2", BUILD)
    assert continue_claim is not None

    polluted = start_metadata.model_copy(update={"command_digest": HASH_C})
    _install_run(
        driver,
        replace(
            _run(driver),
            applied=cast(tuple[state_machine.AppliedRecord, ...], (polluted, *_run(driver).applied[1:])),
        ),
    )
    before = driver._snapshot

    with pytest.raises(ValueError):
        await driver.record_applied(
            AppliedCommandMetadata(
                tenant_id=TENANT,
                run_id=RUN,
                command_id=continue_claim.command_id,
                command_seq=continue_claim.command_seq,
                command_digest=continue_claim.command_digest,
                checkpoint_ref="checkpoint://continue",
                checkpoint_hash=HASH_B,
                runtime_build_hash=BUILD,
                execution_fence=continue_claim.execution_fence,
            )
        )
    with pytest.raises(ValueError):
        await driver.is_command_applied(continue_claim)
    with pytest.raises(ValueError):
        await driver.consume(continue_claim)
    assert driver._snapshot is before
    assert _command(driver, continue_receipt.command_id).status == "leased"
    assert _run(driver).lease_command_id == continue_claim.command_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pollution",
    [
        "command_id",
        "command_digest",
        "tenant_id",
        "run_id",
        "runtime_build_hash",
        "metadata_sequence",
        "mapping_key",
        "unknown_registry",
        "missing_registry",
        "cross_command",
        "cross_command_fence",
        "duplicate_identity",
    ],
)
async def test_every_applied_prefix_pollution_fails_closed_without_side_effects(pollution: str) -> None:
    reconciler = InternalDispatchAuthority("driver_reconciler")
    driver = DeterministicExecutionDriver(internal_authorities=(reconciler,))
    await driver.dispatch(_start())
    start_claim = await driver.claim("worker-1", BUILD)
    assert start_claim is not None
    start_metadata = await _apply_and_consume(driver, start_claim)
    await driver._fixture_set_run_status(TENANT, RUN, "running")
    continuation = await driver.reconcile(TENANT, RUN)
    assert continuation is not None
    target_claim = await driver.claim("worker-2", BUILD)
    assert target_claim is not None

    state = _run(driver)
    if pollution == "command_id":
        polluted_metadata = start_metadata.model_copy(update={"command_id": uuid4()})
    elif pollution == "command_digest":
        polluted_metadata = start_metadata.model_copy(update={"command_digest": HASH_C})
    elif pollution == "tenant_id":
        polluted_metadata = start_metadata.model_copy(update={"tenant_id": "tenant-b"})
    elif pollution == "run_id":
        polluted_metadata = start_metadata.model_copy(update={"run_id": UUID(int=2)})
    elif pollution == "runtime_build_hash":
        polluted_metadata = start_metadata.model_copy(update={"runtime_build_hash": BUILD_NEW})
    elif pollution == "metadata_sequence" or pollution == "mapping_key":
        polluted_metadata = start_metadata.model_copy(update={"command_seq": 99})
    elif pollution == "unknown_registry":
        polluted_metadata = start_metadata.model_copy(update={"command_id": uuid4()})
    elif pollution == "cross_command":
        polluted_metadata = start_metadata.model_copy(update={"command_id": continuation.command_id})
    elif pollution == "cross_command_fence":
        polluted_metadata = start_metadata.model_copy(update={"execution_fence": target_claim.execution_fence})
    elif pollution in {"missing_registry", "duplicate_identity"}:
        polluted_metadata = start_metadata
    else:
        raise AssertionError(f"unhandled pollution {pollution}")
    malformed_commands = state.commands
    if pollution == "missing_registry":
        malformed_commands = state.commands[1:]
    elif pollution == "duplicate_identity":
        malformed_commands = (*state.commands, state.commands[0])
    malformed = replace(
        state,
        applied=cast(tuple[state_machine.AppliedRecord, ...], (polluted_metadata,)),
        commands=malformed_commands,
    )
    _install_run(driver, malformed)
    before = driver._snapshot
    with pytest.raises(ValueError):
        await driver.record_applied(
            AppliedCommandMetadata(
                tenant_id=TENANT,
                run_id=RUN,
                command_id=target_claim.command_id,
                command_seq=target_claim.command_seq,
                command_digest=target_claim.command_digest,
                checkpoint_ref="checkpoint://continue",
                checkpoint_hash=HASH_B,
                runtime_build_hash=BUILD,
                execution_fence=target_claim.execution_fence,
            )
        )
    with pytest.raises(ValueError):
        await driver.is_command_applied(target_claim)
    with pytest.raises(ValueError):
        await driver.consume(target_claim)
    assert driver._snapshot is before


@pytest.mark.asyncio
async def test_complete_applied_prefix_and_takeover_remain_valid() -> None:
    reconciler = InternalDispatchAuthority("driver_reconciler")
    driver = DeterministicExecutionDriver(lease_seconds=5, internal_authorities=(reconciler,))
    start = await driver.dispatch(_start())
    first = await driver.claim("worker-1", BUILD)
    assert first is not None
    await _apply_and_consume(driver, first)
    await driver._fixture_set_run_status(TENANT, RUN, "running")
    continuation = await driver.reconcile(TENANT, RUN)
    assert continuation is not None
    old_claim = await driver.claim("worker-2", BUILD)
    assert old_claim is not None
    await driver.record_applied(
        AppliedCommandMetadata(
            tenant_id=TENANT,
            run_id=RUN,
            command_id=continuation.command_id,
            command_seq=continuation.command_seq,
            command_digest=continuation.command_digest,
            checkpoint_ref="checkpoint://continue",
            checkpoint_hash=HASH_B,
            runtime_build_hash=BUILD,
            execution_fence=old_claim.execution_fence,
        )
    )
    driver.advance_time(6)
    takeover = await driver.claim("worker-3", BUILD)
    assert takeover is not None
    assert takeover.execution_fence > old_claim.execution_fence
    assert await driver.is_command_applied(takeover)
    await driver.consume(takeover)
    assert start.command_seq == 0


@pytest.mark.asyncio
async def test_is_command_applied_requires_full_command_and_claim_binding() -> None:
    driver = DeterministicExecutionDriver()
    receipt = await driver.dispatch(_start())
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    metadata = AppliedCommandMetadata(
        tenant_id=TENANT,
        run_id=RUN,
        command_id=receipt.command_id,
        command_seq=receipt.command_seq,
        command_digest=receipt.command_digest,
        checkpoint_ref="checkpoint://1",
        checkpoint_hash=HASH_B,
        runtime_build_hash=BUILD,
        execution_fence=claim.execution_fence,
    )
    await driver.record_applied(metadata)
    assert await driver.is_command_applied(_start(command_id=receipt.command_id))
    with pytest.raises(CommandConflict):
        await driver.is_command_applied(_start(command_id=receipt.command_id, payload_ref="artifact://other"))
    with pytest.raises(CommandConflict):
        await driver.is_command_applied(_start(command_id=receipt.command_id, tenant_id="tenant-b"))
    with pytest.raises(StaleExecutionFence):
        await driver.is_command_applied(
            claim.model_copy(update={"run_id": UUID("00000000-0000-0000-0000-000000000002")})
        )
    assert await driver.is_command_applied(claim)


@pytest.mark.asyncio
async def test_resume_interrupt_binding_is_exact_and_nonce_is_consumed_on_apply() -> None:
    driver = DeterministicExecutionDriver()
    start = await driver.dispatch(_start())
    start_claim = await driver.claim("worker-1", BUILD)
    assert start_claim is not None
    start_metadata = AppliedCommandMetadata(
        tenant_id=TENANT,
        run_id=RUN,
        command_id=start.command_id,
        command_seq=start.command_seq,
        command_digest=start.command_digest,
        checkpoint_ref="checkpoint://start",
        checkpoint_hash=HASH_B,
        runtime_build_hash=BUILD,
        execution_fence=start_claim.execution_fence,
    )
    await driver.record_applied(start_metadata)
    await driver.consume(start_claim)

    binding = _interrupt()
    await driver._fixture_set_user_interrupt(TENANT, RUN, binding)
    before = (_run(driver).revision, len(_run(driver).commands))
    with pytest.raises(RunStateConflict):
        await driver.dispatch(
            _resume(
                expected_revision=1,
                interrupt=binding.model_copy(update={"checkpoint_hash": HASH_A}),
            )
        )
    assert (_run(driver).revision, len(_run(driver).commands)) == before

    resume = await driver.dispatch(_resume(expected_revision=1, interrupt=binding))
    resume_claim = await driver.claim("worker-1", BUILD)
    assert resume_claim is not None
    assert _run(driver).interrupt == binding
    resume_metadata = AppliedCommandMetadata(
        tenant_id=TENANT,
        run_id=RUN,
        command_id=resume.command_id,
        command_seq=resume.command_seq,
        command_digest=resume.command_digest,
        checkpoint_ref="checkpoint://resume",
        checkpoint_hash=HASH_C,
        runtime_build_hash=BUILD,
        execution_fence=resume_claim.execution_fence,
    )
    await driver.record_applied(resume_metadata)
    assert _run(driver).interrupt is None
    assert binding.nonce_hash in _run(driver).consumed_interrupt_nonces
    await driver.consume(resume_claim)

    await driver._fixture_set_user_interrupt(TENANT, RUN, binding)
    with pytest.raises(RunStateConflict):
        await driver.dispatch(_resume(expected_revision=2, interrupt=binding))
    assert _run(driver).revision == 2


@pytest.mark.asyncio
async def test_dead_letter_does_not_change_applied_metadata() -> None:
    driver = DeterministicExecutionDriver()
    await driver.dispatch(_start())
    claim = await driver.claim(worker_id="worker-1", runtime_build_hash=BUILD)
    assert claim is not None
    before = await driver.applied_for(RUN)
    receipt = await driver.dead_letter(claim, reason_ref="error://dead-letter")
    assert receipt.status == "dead_letter"
    assert await driver.applied_for(RUN) == before


@pytest.mark.asyncio
async def test_reconcile_only_running_unleased_run_creates_stable_continue() -> None:
    driver = DeterministicExecutionDriver()
    await driver.dispatch(_start())
    claim = await driver.claim(worker_id="worker-1", runtime_build_hash=BUILD)
    assert claim is not None
    await _apply_and_consume(driver, claim)
    await driver._fixture_set_run_status(TENANT, RUN, "running")
    first = await driver.reconcile(TENANT, RUN)
    second = await driver.reconcile(TENANT, RUN)
    assert first is not None
    assert second == first
    assert first.command_type == "continue"
    assert first.command_id == derive_continue_command_id(TENANT, RUN, 1)
    assert first.command_seq == 1

    for status in ("waiting_user_input", "waiting_action_result", "succeeded", "failed", "cancelled"):
        await driver._fixture_set_run_status(TENANT, RUN, status)
        assert await driver.reconcile(TENANT, RUN) is None


@pytest.mark.asyncio
async def test_two_workers_claim_one_command() -> None:
    driver = DeterministicExecutionDriver()
    await driver.dispatch(_start())
    claims = await asyncio.gather(
        driver.claim(worker_id="worker-1", runtime_build_hash=BUILD),
        driver.claim(worker_id="worker-2", runtime_build_hash=BUILD),
    )
    assert sum(claim is not None for claim in claims) == 1


@pytest.mark.asyncio
async def test_mixed_build_queue_claims_exact_matching_later_run() -> None:
    driver = DeterministicExecutionDriver()
    run_v1 = UUID("00000000-0000-0000-0000-000000000002")
    run_v2 = UUID("00000000-0000-0000-0000-000000000003")
    await driver.dispatch(_start(run_id=run_v1, runtime_build_hash=BUILD_V1, digest=HASH_A))
    await driver.dispatch(_start(run_id=run_v2, runtime_build_hash=BUILD_V2, digest=HASH_B))
    claim = await driver.claim("worker-v2", BUILD_V2)
    assert claim is not None
    assert claim.run_id == run_v2


@pytest.mark.asyncio
async def test_same_id_and_digest_requires_complete_command_binding() -> None:
    driver = DeterministicExecutionDriver()
    command_id = uuid4()
    first = await driver.dispatch(_start(command_id=command_id))
    with pytest.raises(CommandConflict):
        await driver.dispatch(_start(command_id=command_id, payload_ref="artifact://start/changed"))
    with pytest.raises(CommandConflict):
        await driver.dispatch(_cancel(command_id=command_id, digest=HASH_A, expected_revision=0))
    retry = await driver.dispatch(_start(command_id=command_id))
    assert retry == first
    assert retry.command_seq == 0
    start_claim = await driver.claim("worker-1", BUILD)
    assert start_claim is not None
    await _apply_and_consume(driver, start_claim)
    await driver._fixture_set_user_interrupt(TENANT, RUN, _interrupt())
    resume = await driver.dispatch(_resume())
    assert resume.command_seq == 1


@pytest.mark.asyncio
async def test_expected_revision_cas_and_single_outstanding_resume() -> None:
    driver = DeterministicExecutionDriver()
    await driver.dispatch(_start())
    start_claim = await driver.claim("worker-1", BUILD)
    assert start_claim is not None
    await _apply_and_consume(driver, start_claim)
    await driver._fixture_set_user_interrupt(TENANT, RUN, _interrupt())
    with pytest.raises(RunStateConflict):
        await driver.dispatch(_resume(expected_revision=0))
    first = await driver.dispatch(_resume(expected_revision=1))
    assert first.command_seq == 1
    with pytest.raises(RunStateConflict):
        await driver.dispatch(_resume(command_id=uuid4(), digest=HASH_A, expected_revision=2))
    cancel = await driver.dispatch(_cancel(expected_revision=2))
    assert cancel.command_seq == 2


@pytest.mark.asyncio
async def test_all_non_cancel_commands_obey_one_outstanding_delivery() -> None:
    reconciler = InternalDispatchAuthority("driver_reconciler")
    driver = DeterministicExecutionDriver(internal_authorities=(reconciler,))
    await driver.dispatch(_start())
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    before = (_run(driver).revision, len(_run(driver).commands))
    continue_command = ContinueRun(
        command_id=derive_continue_command_id(TENANT, RUN, 1),
        tenant_id=TENANT,
        run_id=RUN,
        runtime_build_hash=BUILD,
        command_digest=HASH_A,
        command_type="continue",
        command_schema_version="continue.v1",
        revision=1,
    )
    with pytest.raises(RunStateConflict):
        await driver.dispatch_internal(continue_command, reconciler)
    assert (_run(driver).revision, len(_run(driver).commands)) == before

    await _apply_and_consume(driver, claim)
    await driver._fixture_set_run_status(TENANT, RUN, "running")
    first = await driver.reconcile(TENANT, RUN)
    assert first is not None
    before = (_run(driver).revision, len(_run(driver).commands))
    second = ContinueRun(
        command_id=uuid4(),
        tenant_id=TENANT,
        run_id=RUN,
        runtime_build_hash=BUILD,
        command_digest=first.command_digest,
        command_type="continue",
        command_schema_version="continue.v1",
        revision=1,
    )
    with pytest.raises(RunStateConflict):
        await driver.dispatch_internal(second, reconciler)
    assert (_run(driver).revision, len(_run(driver).commands)) == before


@pytest.mark.asyncio
async def test_cancel_is_the_only_command_allowed_to_supersede_and_only_once() -> None:
    driver = DeterministicExecutionDriver()
    await driver.dispatch(_start())
    first = await driver.dispatch(_cancel(expected_revision=1))
    before = (_run(driver).revision, len(_run(driver).commands))
    with pytest.raises(RunStateConflict):
        await driver.dispatch(_cancel(expected_revision=2))
    assert (_run(driver).revision, len(_run(driver).commands)) == before

    unknown_status_driver = DeterministicExecutionDriver()
    await unknown_status_driver.dispatch(_start())
    _install_run(unknown_status_driver, replace(_run(unknown_status_driver), status="unexpected"))  # type: ignore[arg-type]
    with pytest.raises(ExecutionDriverError):
        await unknown_status_driver.dispatch(_cancel(expected_revision=1))
    assert _run(unknown_status_driver).revision == 1
    assert first.command_seq == 1


@pytest.mark.asyncio
async def test_cancel_expected_revision_cas_has_no_side_effect_on_mismatch() -> None:
    driver = DeterministicExecutionDriver()
    await driver.dispatch(_start())
    with pytest.raises(RunStateConflict):
        await driver.dispatch(_cancel(expected_revision=0))
    start_claim = await driver.claim("worker-1", BUILD)
    assert start_claim is not None
    await _apply_and_consume(driver, start_claim)
    await driver._fixture_set_user_interrupt(TENANT, RUN, _interrupt())
    resume = await driver.dispatch(_resume(expected_revision=1))
    assert resume.command_seq == 1


@pytest.mark.asyncio
async def test_direct_continue_and_signal_require_internal_authorizer() -> None:
    driver = DeterministicExecutionDriver()
    await driver.dispatch(_start())
    start_claim = await driver.claim("worker-1", BUILD)
    assert start_claim is not None
    await _apply_and_consume(driver, start_claim)
    await driver._fixture_set_run_status(TENANT, RUN, "running")
    continue_command = ContinueRun(
        command_id=derive_continue_command_id(TENANT, RUN, 1),
        tenant_id=TENANT,
        run_id=RUN,
        runtime_build_hash=BUILD,
        command_digest=HASH_A,
        command_type="continue",
        command_schema_version="continue.v1",
        revision=1,
    )
    with pytest.raises(RunStateConflict):
        await driver.dispatch(continue_command)

    bridge_authority = InternalDispatchAuthority("action_completion_bridge")
    authorized = DeterministicExecutionDriver(internal_authorities=(bridge_authority,))
    await authorized.dispatch(_start())
    claim = await authorized.claim("worker-1", BUILD)
    assert claim is not None
    await _apply_and_consume(authorized, claim)
    await authorized._fixture_set_run_wait(
        TENANT,
        RUN,
        wait_ref="wait://1",
        wait_hash=HASH_A,
        wait_kind="action_result",
        source_ref="action://1",
        source_fact_version="v1",
        source_fact_hash=HASH_A,
        payload_ref="artifact://action-result/1",
        payload_hash=HASH_B,
    )
    signal = _signal(command_id=derive_continue_command_id(TENANT, RUN, 2))
    with pytest.raises(RunStateConflict):
        await authorized.dispatch(signal)
    accepted = await authorized.dispatch_internal(
        _signal(command_id=derive_signal_command_id(TENANT, RUN, signal.signal_id), signal_id=signal.signal_id),
        bridge_authority,
    )
    assert accepted.command_seq == 1


@pytest.mark.asyncio
async def test_signal_binding_and_single_outstanding_signal() -> None:
    bridge_authority = InternalDispatchAuthority("action_completion_bridge")
    driver = DeterministicExecutionDriver(internal_authorities=(bridge_authority,))
    await driver.dispatch(_start())
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    await _apply_and_consume(driver, claim)
    await driver._fixture_set_run_wait(
        TENANT,
        RUN,
        wait_ref="wait://1",
        wait_hash=HASH_A,
        wait_kind="action_result",
        source_ref="action://1",
        source_fact_version="v1",
        source_fact_hash=HASH_A,
        payload_ref="artifact://action-result/1",
        payload_hash=HASH_B,
    )
    mismatched = _signal(wait_hash=HASH_C)
    with pytest.raises(ExecutionDriverError):
        await driver.dispatch_internal(
            _signal(
                command_id=derive_signal_command_id(TENANT, RUN, mismatched.signal_id),
                signal_id=mismatched.signal_id,
                wait_hash=HASH_C,
            ),
            bridge_authority,
        )
    first = _signal()
    accepted = await driver.dispatch_internal(
        _signal(command_id=derive_signal_command_id(TENANT, RUN, first.signal_id), signal_id=first.signal_id),
        bridge_authority,
    )
    assert accepted.command_seq == 1
    second = _signal(source_fact_hash=HASH_C, digest=HASH_A)
    with pytest.raises(RunStateConflict):
        await driver.dispatch_internal(
            _signal(
                command_id=derive_signal_command_id(TENANT, RUN, second.signal_id),
                signal_id=second.signal_id,
                digest=HASH_A,
                source_fact_hash=HASH_C,
            ),
            bridge_authority,
        )


@pytest.mark.asyncio
async def test_signal_source_identity_and_wait_kind_are_rechecked_at_dispatch() -> None:
    bridge_authority = InternalDispatchAuthority("action_completion_bridge")

    driver = DeterministicExecutionDriver(internal_authorities=(bridge_authority,))
    await driver.dispatch(_start())
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    await _apply_and_consume(driver, claim)
    await driver._fixture_set_run_wait(
        TENANT,
        RUN,
        wait_ref="wait://1",
        wait_hash=HASH_A,
        wait_kind="action_result",
        source_ref="action://1",
        source_fact_version="v1",
        source_fact_hash=HASH_A,
        payload_ref="artifact://action-result/1",
        payload_hash=HASH_B,
    )
    valid = _signal()
    changed_signal_id = uuid4()
    forged = valid.model_copy(
        update={
            "signal_id": changed_signal_id,
            "command_id": derive_signal_command_id(TENANT, RUN, changed_signal_id),
        }
    )
    with pytest.raises(RunSignalConflict):
        await driver.dispatch_internal(forged, bridge_authority)

    _install_run(driver, replace(_run(driver), wait_kind="child_result"))
    with pytest.raises(ExecutionDriverError):
        await driver.dispatch_internal(
            valid.model_copy(update={"command_id": derive_signal_command_id(TENANT, RUN, valid.signal_id)}),
            bridge_authority,
        )


@pytest.mark.asyncio
async def test_internal_authority_requires_registered_identity_and_matching_issuer() -> None:
    registered = InternalDispatchAuthority("action_completion_bridge")
    driver = DeterministicExecutionDriver(internal_authorities=(registered,))
    await driver.dispatch(_start())
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    await _apply_and_consume(driver, claim)
    await driver._fixture_set_run_wait(
        TENANT,
        RUN,
        wait_ref="wait://1",
        wait_hash=HASH_A,
        wait_kind="action_result",
        source_ref="action://1",
        source_fact_version="v1",
        source_fact_hash=HASH_A,
        payload_ref="artifact://action-result/1",
        payload_hash=HASH_B,
    )
    signal = _signal(
        command_id=derive_signal_command_id(TENANT, RUN, _signal().signal_id),
    )
    with pytest.raises(RunStateConflict):
        await driver.dispatch_internal(signal, InternalDispatchAuthority("action_completion_bridge"))
    wrong_issuer = InternalDispatchAuthority("child_completion_bridge")
    untrusted = DeterministicExecutionDriver(internal_authorities=(wrong_issuer,))
    await untrusted.dispatch(_start())
    claim = await untrusted.claim("worker-1", BUILD)
    assert claim is not None
    await _apply_and_consume(untrusted, claim)
    await untrusted._fixture_set_run_wait(
        TENANT,
        RUN,
        wait_ref="wait://1",
        wait_hash=HASH_A,
        wait_kind="action_result",
        source_ref="action://1",
        source_fact_version="v1",
        source_fact_hash=HASH_A,
        payload_ref="artifact://action-result/1",
        payload_hash=HASH_B,
    )
    with pytest.raises(RunStateConflict):
        await untrusted.dispatch_internal(signal, wrong_issuer)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutated_issuer", ["driver_reconciler", "child_completion_bridge", "unknown", ""])
async def test_registered_authority_issuer_mutation_revokes_internal_dispatch_until_restored(
    mutated_issuer: str,
) -> None:
    authority = InternalDispatchAuthority("action_completion_bridge")
    driver = DeterministicExecutionDriver(internal_authorities=(authority,))
    await driver.dispatch(_start())
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    await _apply_and_consume(driver, claim)
    await driver._fixture_set_run_wait(
        TENANT,
        RUN,
        wait_ref="wait://1",
        wait_hash=HASH_A,
        wait_kind="action_result",
        source_ref="action://1",
        source_fact_version="v1",
        source_fact_hash=HASH_A,
        payload_ref="artifact://action-result/1",
        payload_hash=HASH_B,
    )
    signal = _signal(command_id=derive_signal_command_id(TENANT, RUN, _signal().signal_id))
    before = driver._snapshot
    object.__setattr__(authority, "issuer", mutated_issuer)
    with pytest.raises(RunStateConflict):
        await driver.dispatch_internal(signal, authority)
    assert driver._snapshot is before

    object.__setattr__(authority, "issuer", "action_completion_bridge")
    accepted = await driver.dispatch_internal(signal, authority)
    assert accepted.command_seq == 1


@pytest.mark.asyncio
async def test_cancel_supersedes_older_delivery_and_closes_applied_gap() -> None:
    driver = DeterministicExecutionDriver()
    start = await driver.dispatch(_start())
    cancel = await driver.dispatch(_cancel(expected_revision=1))
    assert cancel.command_seq == 1
    assert (await driver.dispatch(_start(command_id=start.command_id))).status == "consumed"
    assert await driver.is_superseded(start.command_id)
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    assert claim.command_id == cancel.command_id
    metadata = AppliedCommandMetadata(
        tenant_id=TENANT,
        run_id=RUN,
        command_id=cancel.command_id,
        command_seq=cancel.command_seq,
        command_digest=cancel.command_digest,
        checkpoint_ref="checkpoint://cancel",
        checkpoint_hash=HASH_B,
        runtime_build_hash=BUILD,
        execution_fence=claim.execution_fence,
    )
    await driver.record_applied(metadata)
    assert not await driver.is_command_applied(_start(command_id=start.command_id))


@pytest.mark.asyncio
@pytest.mark.parametrize("command_kind", ["start", "continue"])
async def test_cancel_closes_applied_unconsumed_delivery_without_superseding_it(command_kind: str) -> None:
    reconciler = InternalDispatchAuthority("driver_reconciler")
    driver = DeterministicExecutionDriver(internal_authorities=(reconciler,))
    start_receipt = await driver.dispatch(_start())
    start_claim = await driver.claim("worker-start", BUILD)
    assert start_claim is not None
    if command_kind == "continue":
        await _apply_and_consume(driver, start_claim)
        await driver._fixture_set_run_status(TENANT, RUN, "running")
        receipt = await driver.dispatch_internal(_continue(revision=1), reconciler)
        old_claim = await driver.claim("worker-continue", BUILD)
        expected_revision = 2
    else:
        receipt = start_receipt
        old_claim = start_claim
        expected_revision = 1
    assert old_claim is not None
    applied = AppliedCommandMetadata(
        tenant_id=TENANT,
        run_id=RUN,
        command_id=receipt.command_id,
        command_seq=receipt.command_seq,
        command_digest=receipt.command_digest,
        checkpoint_ref=f"checkpoint://{command_kind}",
        checkpoint_hash=HASH_B,
        runtime_build_hash=BUILD,
        execution_fence=old_claim.execution_fence,
    )
    await driver.record_applied(applied)

    cancel_command = _cancel(expected_revision=expected_revision)
    cancel_receipt = await driver.dispatch(cancel_command)
    closed = _command(driver, receipt.command_id)
    assert closed.status == "consumed"
    assert closed.consumed_idempotent is True
    assert closed.superseded is False
    revocation = _run(driver).fence_events[-1]
    assert isinstance(revocation, state_machine.CancelFenceRevoked)
    assert revocation.cancel_command_id == cancel_receipt.command_id
    assert _run(driver).commands[-1].receipt.command_id == cancel_receipt.command_id
    assert await driver.dispatch(cancel_command) == cancel_receipt
    before = driver._snapshot
    for stale_write in (lambda: driver.heartbeat(old_claim), lambda: driver.record_applied(applied)):
        with pytest.raises(ExecutionDriverError):
            await stale_write()
    assert driver._snapshot is before
    assert await driver.consume(old_claim) == closed.receipt
    assert await driver.is_command_applied(closed.command)
    with pytest.raises(StaleExecutionFence):
        await driver.is_command_applied(old_claim)
    assert driver._snapshot is before


@pytest.mark.asyncio
async def test_cancel_after_takeover_preserves_apply_time_claim_provenance() -> None:
    driver = DeterministicExecutionDriver(lease_seconds=5)
    receipt = await driver.dispatch(_start())
    applied_claim = await driver.claim("worker-apply", BUILD)
    assert applied_claim is not None
    applied = AppliedCommandMetadata(
        tenant_id=TENANT,
        run_id=RUN,
        command_id=receipt.command_id,
        command_seq=receipt.command_seq,
        command_digest=receipt.command_digest,
        checkpoint_ref="checkpoint://apply-before-takeover-cancel",
        checkpoint_hash=HASH_B,
        runtime_build_hash=BUILD,
        execution_fence=applied_claim.execution_fence,
    )
    await driver.record_applied(applied)
    _, applied_claim_fingerprint = _strict_identity(applied_claim, ExecutionClaim)
    record = _run(driver).applied[0]
    assert record.claim == applied_claim
    assert record.claim_fingerprint == applied_claim_fingerprint

    driver.advance_time(6)
    takeover_claim = await driver.claim("worker-takeover", BUILD)
    assert takeover_claim is not None
    assert takeover_claim.execution_fence > applied_claim.execution_fence
    record = _run(driver).applied[0]
    assert record.claim == applied_claim
    assert record.claim_fingerprint == applied_claim_fingerprint

    await driver.dispatch(_cancel(expected_revision=1))
    closed = _command(driver, receipt.command_id)
    assert closed.status == "consumed"
    assert closed.consumed_idempotent is True
    assert closed.consumed_claim == applied_claim
    assert closed.consumed_worker_id == applied_claim.worker_id
    assert closed.consumed_execution_fence == applied_claim.execution_fence
    assert closed.consumed_lease_until == applied_claim.lease_until
    record = _run(driver).applied[0]
    assert record.claim == applied_claim
    assert record.claim_fingerprint == applied_claim_fingerprint
    assert closed.consumed_claim != takeover_claim
    assert await driver.consume(applied_claim) == closed.receipt
    record = _run(driver).applied[0]
    assert record.claim == applied_claim
    assert record.claim_fingerprint == applied_claim_fingerprint
    with pytest.raises(StaleExecutionFence):
        await driver.consume(takeover_claim)


class _AlwaysEqual(str):
    def __eq__(self, other: object) -> bool:
        return True

    __hash__ = str.__hash__


class _ExplodingStr(str):
    def __eq__(self, other: object) -> bool:
        raise AttributeError("hostile primitive method executed")

    def __hash__(self) -> int:
        raise AttributeError("hostile primitive method executed")

    def __len__(self) -> int:
        raise AttributeError("hostile primitive method executed")


class _ExplodingUUID(UUID):
    def __eq__(self, other: object) -> bool:
        raise AttributeError("hostile UUID method executed")

    def __hash__(self) -> int:
        raise AttributeError("hostile UUID method executed")


class _ExplodingInt(int):
    def __eq__(self, other: object) -> bool:
        raise AttributeError("hostile integer method executed")

    def __hash__(self) -> int:
        raise AttributeError("hostile integer method executed")


class _ExplodingDatetime(datetime):
    def utcoffset(self) -> timedelta | None:
        raise AttributeError("hostile datetime method executed")


@pytest.mark.asyncio
@pytest.mark.parametrize("proof_field", ["command", "claim_history", "applied"])
async def test_exact_hash_proofs_reject_equality_subclasses_before_idempotent_return(
    proof_field: str,
) -> None:
    driver = DeterministicExecutionDriver()
    original = _start()
    receipt = await driver.dispatch(original)
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    applied = await driver.record_applied(
        AppliedCommandMetadata(
            tenant_id=TENANT,
            run_id=RUN,
            command_id=receipt.command_id,
            command_seq=receipt.command_seq,
            command_digest=receipt.command_digest,
            checkpoint_ref="checkpoint://start",
            checkpoint_hash=HASH_B,
            runtime_build_hash=BUILD,
            execution_fence=claim.execution_fence,
        )
    )
    run = _run(driver)
    command_state = run.commands[0]
    if proof_field == "command":
        tampered_command = command_state.command.model_copy(
            update={"payload_ref": "artifact://tampered", "payload_hash": HASH_C}
        )
        polluted = replace(
            command_state,
            command=tampered_command,
            command_fingerprint=_AlwaysEqual("not-a-hash"),
        )
        polluted_run = replace(run, commands=(polluted,))
    elif proof_field == "claim_history":
        polluted = replace(
            command_state,
            claim_history_fingerprints=(_AlwaysEqual("not-a-hash"),),
            active_claim_fingerprint=_AlwaysEqual("not-a-hash"),
        )
        polluted_run = replace(run, commands=(polluted,))
    else:
        polluted_record = replace(run.applied[0], fingerprint=_AlwaysEqual("not-a-hash"))
        polluted_run = replace(run, applied=(polluted_record,))
    _install_run(driver, polluted_run)
    before = driver._snapshot

    with pytest.raises(ExecutionDriverError):
        state_machine.validate_snapshot(driver._snapshot)
    with pytest.raises(ExecutionDriverError):
        await driver.dispatch(original)
    with pytest.raises(ExecutionDriverError):
        await driver.applied_for(RUN)
    with pytest.raises(ExecutionDriverError):
        await driver.record_applied(applied)
    assert driver._snapshot is before


@pytest.mark.asyncio
async def test_primitive_subclasses_fail_closed_before_user_methods_or_pydantic_normalization() -> None:
    driver = DeterministicExecutionDriver()
    command = _start()
    await driver.dispatch(command)
    run = _run(driver)
    _install_run(driver, replace(run, status=cast(RunStatus, _ExplodingStr("running"))))
    before = driver._snapshot
    with pytest.raises(ExecutionDriverError):
        await driver.get_run_status(TENANT, RUN)
    assert driver._snapshot is before

    fresh = DeterministicExecutionDriver()
    hostile_payload = command.model_copy(update={"payload_ref": _ExplodingStr("artifact://hostile")})
    with pytest.raises(ExecutionDriverError):
        await fresh.dispatch(hostile_payload)
    with pytest.raises(ExecutionDriverError):
        await fresh.dispatch(cast(ExecutionCommand, object()))
    assert fresh._snapshot.runs == {}

    claimed = DeterministicExecutionDriver()
    await claimed.dispatch(_start())
    claim = await claimed.claim("worker-1", BUILD)
    assert claim is not None
    hostile_claim = claim.model_copy(update={"worker_id": _ExplodingStr("worker-1")})
    snapshot_before_write = claimed._snapshot
    with pytest.raises(ExecutionDriverError):
        await claimed.heartbeat(hostile_claim)
    assert claimed._snapshot is snapshot_before_write

    hostile_clock = DeterministicExecutionDriver(clock=lambda: _ExplodingDatetime.now(UTC))
    await hostile_clock.dispatch(_start())
    hostile_clock_before = hostile_clock._snapshot
    with pytest.raises(ValueError, match="timezone-aware"):
        await hostile_clock.claim("worker-1", BUILD)
    assert hostile_clock._snapshot is hostile_clock_before


@pytest.mark.asyncio
async def test_snapshot_primitive_closure_rejects_every_aggregate_boundary_before_operations() -> None:
    pending = DeterministicExecutionDriver()
    await pending.dispatch(_start())
    pending_run = _run(pending)
    pending_command = pending_run.commands[0]

    leased = DeterministicExecutionDriver()
    await leased.dispatch(_start())
    leased_claim = await leased.claim("worker-1", BUILD)
    assert leased_claim is not None
    leased_run = _run(leased)
    claim_event = leased_run.fence_events[0]
    assert isinstance(claim_event, state_machine.ClaimFenceIssued)

    cancelled = DeterministicExecutionDriver()
    await cancelled.dispatch(_start())
    await cancelled.dispatch(_cancel(expected_revision=1))
    cancelled_run = _run(cancelled)
    cancel_event = cancelled_run.fence_events[-1]
    assert isinstance(cancel_event, state_machine.CancelFenceRevoked)

    hostile_uuid = _ExplodingUUID(RUN.hex)
    hostile_datetime = _ExplodingDatetime.now(UTC)
    malformed_runs = (
        replace(pending_run, revision=cast(int, _ExplodingInt(1))),
        replace(pending_run, run_id=cast(UUID, hostile_uuid)),
        replace(pending_run, fence_events=cast(tuple[FenceEvent, ...], [])),
        replace(pending_run, consumed_interrupt_nonces=cast(frozenset[str], set())),
        replace(pending_run, commands=cast(tuple[CommandAggregate, ...], [pending_command])),
        replace(
            pending_run,
            commands=(replace(pending_command, consumed_idempotent=cast(bool, 1)),),
        ),
        replace(
            pending_run,
            commands=(replace(pending_command, claim_history=cast(tuple[ExecutionClaim, ...], [])),),
        ),
        replace(
            pending_run,
            commands=(replace(pending_command, active_claim_fingerprint=HASH_A),),
        ),
        replace(pending_run, commands=cast(tuple[CommandAggregate, ...], (object(),))),
        replace(leased_run, lease_until=cast(datetime, hostile_datetime)),
        replace(leased_run, lease_command_id=cast(UUID, hostile_uuid)),
        replace(
            leased_run,
            fence_events=(replace(claim_event, command_id=cast(UUID, hostile_uuid)),),
        ),
        replace(
            cancelled_run,
            fence_events=(*cancelled_run.fence_events[:-1], replace(cancel_event, cancel_command_id=hostile_uuid)),
        ),
    )
    for malformed_run in malformed_runs:
        snapshot = DriverSnapshot({RUN: malformed_run})
        with pytest.raises(ExecutionDriverError) as caught:
            state_machine.validate_snapshot(snapshot)
        assert not isinstance(caught.value.__cause__, AttributeError)


@pytest.mark.asyncio
async def test_command_lifecycle_closed_union_rejects_missing_provenance_and_dead_letter_history() -> None:
    # Every transition-produced variant is accepted by the discriminator.
    pending = DeterministicExecutionDriver()
    await pending.dispatch(_start())
    state_machine.validate_snapshot(pending._snapshot)

    leased = DeterministicExecutionDriver()
    await leased.dispatch(_start())
    leased_claim = await leased.claim("worker-1", BUILD)
    assert leased_claim is not None
    state_machine.validate_snapshot(leased._snapshot)

    consumed = DeterministicExecutionDriver()
    consumed_receipt = await consumed.dispatch(_start())
    consumed_claim = await consumed.claim("worker-1", BUILD)
    assert consumed_claim is not None
    await _apply_and_consume(consumed, consumed_claim)
    state_machine.validate_snapshot(consumed._snapshot)

    superseded_pending = DeterministicExecutionDriver()
    await superseded_pending.dispatch(_start())
    await superseded_pending.dispatch(_cancel(expected_revision=1))
    state_machine.validate_snapshot(superseded_pending._snapshot)

    superseded_leased = DeterministicExecutionDriver()
    await superseded_leased.dispatch(_start())
    superseded_claim = await superseded_leased.claim("worker-1", BUILD)
    assert superseded_claim is not None
    await superseded_leased.dispatch(_cancel(expected_revision=1))
    state_machine.validate_snapshot(superseded_leased._snapshot)

    dead_letter = DeterministicExecutionDriver()
    await dead_letter.dispatch(_start())
    dead_claim = await dead_letter.claim("worker-1", BUILD)
    assert dead_claim is not None
    await dead_letter.dead_letter(dead_claim, "error://dead")
    state_machine.validate_snapshot(dead_letter._snapshot)

    # A consumed receipt with its applied proof but no consumed provenance is
    # neither idempotent-consumed nor superseded-consumed.
    consumed_state = consumed._snapshot.runs[RUN]
    consumed_command = consumed_state.commands[0]
    malformed_consumed = replace(
        consumed_command,
        consumed_worker_id=None,
        consumed_execution_fence=None,
        consumed_lease_until=None,
        consumed_claim=None,
        consumed_claim_fingerprint=None,
        consumed_idempotent=False,
        superseded=False,
    )
    object.__setattr__(
        consumed,
        "_snapshot",
        DriverSnapshot({RUN: replace(consumed_state, commands=(malformed_consumed,))}),
    )
    before = consumed._snapshot
    with pytest.raises(ExecutionDriverError) as caught:
        await consumed.applied_for(RUN)
    assert caught.value.__cause__ is not None and "consumed" in str(caught.value.__cause__)
    assert consumed._snapshot is before

    # A dead-letter command must have real claim provenance; a pending genesis
    # cannot be relabeled dead-letter by setting only status/reason/receipt.
    dead_state = dead_letter._snapshot.runs[RUN]
    dead_command = dead_state.commands[0]
    dead_receipt, dead_receipt_fingerprint = state_machine._receipt_with(
        dead_command.receipt, status="dead_letter", lease_owner=None, execution_fence=None, lease_until=None
    )
    malformed_dead = replace(
        dead_command,
        status="dead_letter",
        dead_letter_ref="error://forged",
        receipt=dead_receipt,
        receipt_fingerprint=dead_receipt_fingerprint,
        claim_history=(),
        claim_history_fingerprints=(),
    )
    object.__setattr__(dead_letter, "_snapshot", DriverSnapshot({RUN: replace(dead_state, commands=(malformed_dead,))}))
    before = dead_letter._snapshot
    with pytest.raises(ExecutionDriverError) as caught:
        await dead_letter.applied_for(RUN)
    assert caught.value.__cause__ is not None and "dead-letter" in str(caught.value.__cause__)
    assert dead_letter._snapshot is before

    # Keep the helper variables live so this remains an explicit matrix rather
    # than silently relying on an internal index.
    assert consumed_receipt.command_seq == 0


@pytest.mark.asyncio
async def test_duplicate_applied_write_after_expiry_is_stale() -> None:
    driver = DeterministicExecutionDriver(lease_seconds=5)
    receipt = await driver.dispatch(_start())
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    metadata = AppliedCommandMetadata(
        tenant_id=TENANT,
        run_id=RUN,
        command_id=receipt.command_id,
        command_seq=receipt.command_seq,
        command_digest=receipt.command_digest,
        checkpoint_ref="checkpoint://1",
        checkpoint_hash=HASH_B,
        runtime_build_hash=BUILD,
        execution_fence=claim.execution_fence,
    )
    await driver.record_applied(metadata)
    driver.advance_time(5)
    with pytest.raises(StaleExecutionFence):
        await driver.record_applied(metadata)


def test_command_discriminator_fields_are_required() -> None:
    payload = _start().model_dump()
    payload.pop("command_type")
    payload.pop("command_schema_version")
    with pytest.raises(ValidationError):
        StartRun.model_validate(payload)


@pytest.mark.asyncio
async def test_command_receipt_claim_and_applied_metadata_are_frozen() -> None:
    command = _start()
    with pytest.raises(ValidationError):
        command.command_digest = HASH_C
    driver = DeterministicExecutionDriver()
    receipt = await driver.dispatch(command)
    with pytest.raises(ValidationError):
        receipt.status = "consumed"
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    with pytest.raises(ValidationError):
        claim.worker_id = "worker-2"
    metadata = AppliedCommandMetadata(
        tenant_id=TENANT,
        run_id=RUN,
        command_id=receipt.command_id,
        command_seq=receipt.command_seq,
        command_digest=receipt.command_digest,
        checkpoint_ref="checkpoint://1",
        checkpoint_hash=HASH_B,
        runtime_build_hash=BUILD,
        execution_fence=claim.execution_fence,
    )
    with pytest.raises(ValidationError):
        metadata.checkpoint_ref = "checkpoint://2"


@pytest.mark.asyncio
async def test_expired_lease_loses_metadata_write_authority_before_takeover() -> None:
    driver = DeterministicExecutionDriver(lease_seconds=5)
    receipt = await driver.dispatch(_start())
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    driver.advance_time(6)
    metadata = AppliedCommandMetadata(
        tenant_id=TENANT,
        run_id=RUN,
        command_id=receipt.command_id,
        command_seq=receipt.command_seq,
        command_digest=receipt.command_digest,
        checkpoint_ref="checkpoint://expired",
        checkpoint_hash=HASH_B,
        runtime_build_hash=BUILD,
        execution_fence=claim.execution_fence,
    )
    with pytest.raises(StaleExecutionFence):
        await driver.record_applied(metadata)
    with pytest.raises(StaleExecutionFence):
        await driver.heartbeat(claim)
    with pytest.raises(StaleExecutionFence):
        await driver.consume(claim)


@pytest.mark.asyncio
async def test_only_final_claim_can_repeat_consume_after_takeover() -> None:
    driver = DeterministicExecutionDriver(lease_seconds=5)
    await driver.dispatch(_start())
    first = await driver.claim("worker-1", BUILD)
    assert first is not None
    driver.advance_time(6)
    second = await driver.claim("worker-2", BUILD)
    assert second is not None
    await _apply_and_consume(driver, second)
    assert (await driver.consume(second)).status == "consumed"
    with pytest.raises(StaleExecutionFence):
        await driver.consume(first)


@pytest.mark.asyncio
async def test_checkpoint_before_consume_recovers_without_duplicate_apply() -> None:
    driver = DeterministicExecutionDriver(lease_seconds=5)
    receipt = await driver.dispatch(_start())
    old_claim = await driver.claim("worker-1", BUILD)
    assert old_claim is not None
    applied = AppliedCommandMetadata(
        tenant_id=TENANT,
        run_id=RUN,
        command_id=receipt.command_id,
        command_seq=receipt.command_seq,
        command_digest=receipt.command_digest,
        checkpoint_ref="checkpoint://crash-window",
        checkpoint_hash=HASH_B,
        runtime_build_hash=BUILD,
        execution_fence=old_claim.execution_fence,
    )
    await driver.record_applied(applied)
    _, old_claim_fingerprint = _strict_identity(old_claim, ExecutionClaim)
    record = _run(driver).applied[0]
    assert record.claim == old_claim
    assert record.claim_fingerprint == old_claim_fingerprint

    # Simulate a worker crash after checkpoint commit but before command
    # acknowledgement; takeover gets a new fence while the old proof remains
    # authoritative by command identity.
    driver.advance_time(6)
    new_claim = await driver.claim("worker-2", BUILD)
    assert new_claim is not None
    assert new_claim.execution_fence > old_claim.execution_fence
    assert await driver.is_command_applied(new_claim)
    record = _run(driver).applied[0]
    assert record.claim == old_claim
    assert record.claim_fingerprint == old_claim_fingerprint
    await driver.consume(new_claim)
    assert (await driver.consume(new_claim)).status == "consumed"

    record = _run(driver).applied[0]
    assert record.claim == old_claim
    assert record.claim_fingerprint == old_claim_fingerprint
    assert record.claim_fingerprint == _strict_identity(record.claim, ExecutionClaim)[1]


@pytest.mark.asyncio
async def test_applied_record_rejects_rebinding_to_takeover_claim_with_recomputed_fingerprint() -> None:
    driver = DeterministicExecutionDriver(lease_seconds=5)
    receipt = await driver.dispatch(_start())
    applied_claim = await driver.claim("worker-apply", BUILD)
    assert applied_claim is not None
    await driver.record_applied(
        AppliedCommandMetadata(
            tenant_id=TENANT,
            run_id=RUN,
            command_id=receipt.command_id,
            command_seq=receipt.command_seq,
            command_digest=receipt.command_digest,
            checkpoint_ref="checkpoint://immutable-apply-claim",
            checkpoint_hash=HASH_B,
            runtime_build_hash=BUILD,
            execution_fence=applied_claim.execution_fence,
        )
    )
    driver.advance_time(6)
    takeover_claim = await driver.claim("worker-takeover", BUILD)
    assert takeover_claim is not None
    takeover_claim, takeover_fingerprint = _strict_identity(takeover_claim, ExecutionClaim)

    run = _run(driver)
    applied_record = run.applied[0]
    polluted_record = replace(
        applied_record,
        claim=takeover_claim,
        claim_fingerprint=takeover_fingerprint,
    )
    _install_run(driver, replace(run, applied=(polluted_record,)))
    malformed = driver._snapshot

    with pytest.raises(ExecutionDriverError):
        await driver.applied_for(RUN)
    assert driver._snapshot is malformed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    ["claim_fingerprint", "claim.worker_id", "claim.lease_until", "claim.execution_fence"],
)
async def test_applied_record_rejects_single_field_pollution_without_snapshot_mutation(field: str) -> None:
    driver = DeterministicExecutionDriver()
    await driver.dispatch(_start())
    claim = await driver.claim("worker-apply", BUILD)
    assert claim is not None
    await _apply_and_consume(driver, claim)

    original = _run(driver).applied[0]
    polluted = _polluted_applied_record(original, field)
    if field != "claim_fingerprint":
        assert polluted.claim_fingerprint == _strict_identity(polluted.claim, ExecutionClaim)[1]
        assert polluted.claim_fingerprint != original.claim_fingerprint
    _install_run(driver, replace(_run(driver), applied=(polluted,)))
    malformed = driver._snapshot
    with pytest.raises(ExecutionDriverError):
        await driver.applied_for(RUN)
    assert driver._snapshot is malformed


@pytest.mark.asyncio
async def test_applied_metadata_accepts_only_a_proved_or_superseded_prefix() -> None:
    driver = DeterministicExecutionDriver()
    start = await driver.dispatch(_start())
    cancel = await driver.dispatch(_cancel(expected_revision=1))
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    assert claim.command_id == cancel.command_id
    metadata = AppliedCommandMetadata(
        tenant_id=TENANT,
        run_id=RUN,
        command_id=cancel.command_id,
        command_seq=cancel.command_seq,
        command_digest=cancel.command_digest,
        checkpoint_ref="checkpoint://cancel",
        checkpoint_hash=HASH_B,
        runtime_build_hash=BUILD,
        execution_fence=claim.execution_fence,
    )
    await driver.record_applied(metadata)
    await driver.consume(claim)
    assert not await driver.is_command_applied(_start(command_id=start.command_id))


@pytest.mark.asyncio
async def test_start_state_and_command_applicability_are_fail_closed() -> None:
    driver = DeterministicExecutionDriver()
    first = await driver.dispatch(_start())
    assert await driver.get_run_status(TENANT, RUN) == "accepted"
    with pytest.raises(CommandConflict):
        await driver.dispatch(_start(command_id=uuid4(), digest=HASH_C))
    assert (await driver.get_run_status(TENANT, RUN)) == "accepted"
    with pytest.raises(RunStateConflict):
        await driver.dispatch(_resume())
    assert first.command_seq == 0

    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    await _apply_and_consume(driver, claim)
    await driver._fixture_set_run_status(TENANT, RUN, "waiting_user_input")
    invalid_continue = ContinueRun(
        command_id=derive_continue_command_id(TENANT, RUN, 1),
        tenant_id=TENANT,
        run_id=RUN,
        runtime_build_hash=BUILD,
        command_digest=HASH_C,
        command_type="continue",
        command_schema_version="continue.v1",
        revision=1,
    )
    with pytest.raises(RunStateConflict):
        await driver.dispatch(invalid_continue)

    await driver._fixture_set_run_status(TENANT, RUN, "succeeded")
    with pytest.raises(RunStateConflict):
        await driver.dispatch(_cancel())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("runtime_build_hash", "build-a"),
        ("command_digest", "digest-a"),
        ("payload_hash", "payload-a"),
    ],
)
def test_content_addressed_hashes_are_lowercase_sha256(field: str, value: str) -> None:
    payload = _start().model_dump()
    payload[field] = value
    with pytest.raises(ValidationError):
        StartRun.model_validate(payload)


def test_compatibility_aliases_and_unused_surface_are_not_exported() -> None:
    import app.execution.driver as driver_module

    assert not hasattr(driver_module, "CommandConflictError")
    assert not hasattr(driver_module, "NoCommandAvailable")


def test_claim_and_receipt_are_immutable_and_bind_identity_fields() -> None:
    now = datetime.now(UTC)
    assert now.tzinfo is not None
    with pytest.raises(ValidationError):
        StartRun.model_validate({**_start().model_dump(), "tenant_id": "tenant-b", "auth": "forged"})


@pytest.mark.asyncio
async def test_dispatch_rejects_unvalidated_model_copy_with_unknown_command_field() -> None:
    """A validated Pydantic instance is not an authority after model_copy()."""

    driver = DeterministicExecutionDriver()
    forged = _start().model_copy(update={"state_patch": {"status": "succeeded"}})
    with pytest.raises(ExecutionDriverError):
        await driver.dispatch(forged)
    assert driver._snapshot.runs == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        {"command_seq": False},
        {"execution_fence": True},
        {"unknown_claim_field": "forged"},
    ],
)
async def test_claim_identity_revalidates_raw_model_fields_on_consumed_retry(
    mutation: dict[str, object],
) -> None:
    driver = DeterministicExecutionDriver()
    await driver.dispatch(_start())
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    await _apply_and_consume(driver, claim)

    forged = claim.model_copy(update=mutation)
    with pytest.raises(ExecutionDriverError):
        await driver.consume(forged)
    assert (await driver.consume(claim)).status == "consumed"


@pytest.mark.asyncio
async def test_claim_datetime_identity_uses_canonical_utc_instant() -> None:
    driver = DeterministicExecutionDriver()
    await driver.dispatch(_start())
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    await _apply_and_consume(driver, claim)

    same_instant = claim.model_copy(update={"lease_until": claim.lease_until.astimezone(timezone(timedelta(hours=8)))})
    assert (await driver.consume(same_instant)).status == "consumed"

    naive = claim.model_copy(update={"lease_until": datetime(2030, 1, 1)})
    with pytest.raises(ExecutionDriverError):
        await driver.consume(naive)


@pytest.mark.asyncio
async def test_nested_interrupt_model_copy_extra_is_rejected_before_dispatch() -> None:
    driver = DeterministicExecutionDriver()
    start = await driver.dispatch(_start())
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    await _apply_and_consume(driver, claim)
    binding = _interrupt()
    await driver._fixture_set_user_interrupt(TENANT, RUN, binding)
    forged_interrupt = binding.model_copy(update={"nested_extra": "forged"})
    forged = _resume(interrupt=forged_interrupt)
    before = (_run(driver).revision, len(_run(driver).commands))
    with pytest.raises(ExecutionDriverError):
        await driver.dispatch(forged)
    assert (_run(driver).revision, len(_run(driver).commands)) == before
    assert start.command_seq == 0


@pytest.mark.asyncio
async def test_missing_applied_prefix_rejects_all_admission_and_observation_seams() -> None:
    driver = DeterministicExecutionDriver(lease_seconds=5)
    await driver.dispatch(_start())
    first = await driver.claim("worker-1", BUILD)
    assert first is not None
    await _apply_and_consume(driver, first)
    await driver._fixture_set_run_status(TENANT, RUN, "running")
    continuation = await driver.reconcile(TENANT, RUN)
    assert continuation is not None
    second = await driver.claim("worker-2", BUILD)
    assert second is not None
    await _apply_and_consume(driver, second)

    _install_run(driver, replace(_run(driver), applied=_run(driver).applied[1:]))
    before = driver._snapshot
    for operation in (
        lambda: driver.applied_for(RUN),
        lambda: driver.is_command_applied(second),
        lambda: driver.consume(second),
        lambda: driver.dead_letter(second, reason_ref="error://corrupt"),
        lambda: driver.reconcile(TENANT, RUN),
    ):
        with pytest.raises(ExecutionDriverError):
            await operation()
    assert driver._snapshot is before


@pytest.mark.asyncio
async def test_reverse_registry_orphan_rejects_before_record_write() -> None:
    driver = DeterministicExecutionDriver()
    receipt = await driver.dispatch(_start())
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    _install_run(driver, replace(_run(driver), commands=()))
    metadata = AppliedCommandMetadata(
        tenant_id=TENANT,
        run_id=RUN,
        command_id=receipt.command_id,
        command_seq=receipt.command_seq,
        command_digest=receipt.command_digest,
        checkpoint_ref="checkpoint://orphan",
        checkpoint_hash=HASH_B,
        runtime_build_hash=BUILD,
        execution_fence=claim.execution_fence,
    )
    with pytest.raises((ExecutionDriverError, ValueError)):
        await driver.record_applied(metadata)
    assert _run(driver).applied == ()


@pytest.mark.asyncio
async def test_prospective_record_requires_target_claim_history_before_write() -> None:
    driver = DeterministicExecutionDriver()
    receipt = await driver.dispatch(_start())
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    malformed_command = replace(_command(driver, receipt.command_id), claim_history=())
    _install_run(driver, replace(_run(driver), commands=(malformed_command,)))
    metadata = AppliedCommandMetadata(
        tenant_id=TENANT,
        run_id=RUN,
        command_id=receipt.command_id,
        command_seq=receipt.command_seq,
        command_digest=receipt.command_digest,
        checkpoint_ref="checkpoint://missing-history",
        checkpoint_hash=HASH_B,
        runtime_build_hash=BUILD,
        execution_fence=claim.execution_fence,
    )
    with pytest.raises(ExecutionDriverError):
        await driver.record_applied(metadata)
    assert _run(driver).applied == ()


@pytest.mark.asyncio
async def test_dead_letter_rejects_command_with_existing_applied_proof() -> None:
    driver = DeterministicExecutionDriver()
    await driver.dispatch(_start())
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    await driver.record_applied(
        AppliedCommandMetadata(
            tenant_id=TENANT,
            run_id=RUN,
            command_id=claim.command_id,
            command_seq=claim.command_seq,
            command_digest=claim.command_digest,
            checkpoint_ref="checkpoint://applied",
            checkpoint_hash=HASH_B,
            runtime_build_hash=BUILD,
            execution_fence=claim.execution_fence,
        )
    )
    before = _command(driver, claim.command_id).status
    with pytest.raises(ExecutionDriverError):
        await driver.dead_letter(claim, reason_ref="error://after-applied")
    assert _command(driver, claim.command_id).status == before == "leased"


@pytest.mark.asyncio
async def test_consume_requires_applied_proof_and_preserves_unapplied_lease() -> None:
    driver = DeterministicExecutionDriver()
    start = await driver.dispatch(_start())
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None

    with pytest.raises(RunStateConflict):
        await driver.consume(claim)

    # A rejected consume must not turn the lease into an acknowledged command
    # or drop the worker's write authority.
    assert (await driver.dispatch(_start(command_id=start.command_id))).status == "leased"
    renewed = await driver.heartbeat(claim)
    assert renewed.execution_fence == claim.execution_fence


@pytest.mark.asyncio
async def test_unresolved_dead_letter_blocks_direct_internal_continue_dispatch() -> None:
    reconciler = InternalDispatchAuthority("driver_reconciler")
    driver = DeterministicExecutionDriver(internal_authorities=(reconciler,))
    await driver.dispatch(_start())
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    await driver.dead_letter(claim, reason_ref="error://dead-letter")
    await driver._fixture_set_run_status(TENANT, RUN, "running")

    with pytest.raises(RunStateConflict):
        await driver.reconcile(TENANT, RUN)

    command = ContinueRun(
        command_id=derive_continue_command_id(TENANT, RUN, 1),
        tenant_id=TENANT,
        run_id=RUN,
        runtime_build_hash=BUILD,
        command_digest=HASH_A,
        command_type="continue",
        command_schema_version="continue.v1",
        revision=1,
    )
    with pytest.raises(RunStateConflict):
        await driver.dispatch_internal(command, reconciler)


@pytest.mark.asyncio
async def test_resume_record_applied_rejects_duplicate_nonce_write() -> None:
    driver = DeterministicExecutionDriver()
    start = await driver.dispatch(_start())
    start_claim = await driver.claim("worker-1", BUILD)
    assert start_claim is not None
    await driver.record_applied(
        AppliedCommandMetadata(
            tenant_id=TENANT,
            run_id=RUN,
            command_id=start.command_id,
            command_seq=start.command_seq,
            command_digest=start.command_digest,
            checkpoint_ref="checkpoint://start",
            checkpoint_hash=HASH_B,
            runtime_build_hash=BUILD,
            execution_fence=start_claim.execution_fence,
        )
    )
    await driver.consume(start_claim)
    binding = _interrupt()
    await driver._fixture_set_user_interrupt(TENANT, RUN, binding)
    resume = await driver.dispatch(_resume(expected_revision=1, interrupt=binding))
    resume_claim = await driver.claim("worker-1", BUILD)
    assert resume_claim is not None
    metadata = AppliedCommandMetadata(
        tenant_id=TENANT,
        run_id=RUN,
        command_id=resume.command_id,
        command_seq=resume.command_seq,
        command_digest=resume.command_digest,
        checkpoint_ref="checkpoint://resume",
        checkpoint_hash=HASH_C,
        runtime_build_hash=BUILD,
        execution_fence=resume_claim.execution_fence,
    )
    await driver.record_applied(metadata)
    with pytest.raises(RunStateConflict):
        await driver.record_applied(metadata)


def test_signal_identity_uses_unambiguous_source_tuple_encoding() -> None:
    tuples = [
        ("source:ref", "fact", HASH_A),
        ("source", "ref:fact", HASH_A),
        ("source|ref", "fact", HASH_A),
        ("source", "ref|fact", HASH_A),
        ("source\nref", "fact\x1f", HASH_A),
        ("source", "ref\nfact\x1f", HASH_A),
    ]
    identifiers = [derive_signal_id(*source_tuple) for source_tuple in tuples]
    assert len(set(identifiers)) == len(tuples)
    assert derive_signal_id(*tuples[0]) == derive_signal_id(*tuples[0])


def test_driver_contract_validation_rejects_ambiguous_or_untrusted_inputs() -> None:
    with pytest.raises(ValueError):
        InternalDispatchAuthority("unknown")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        derive_continue_command_id("", RUN, 0)
    with pytest.raises(ValueError):
        derive_continue_command_id(TENANT, RUN, -1)
    with pytest.raises(ValueError):
        derive_signal_command_id("", RUN, uuid4())
    with pytest.raises(ValueError):
        derive_signal_id("", "v1", HASH_A)
    with pytest.raises(ValueError):
        derive_signal_id("source", "", HASH_A)
    with pytest.raises(ValueError):
        derive_signal_id("source", "v1", "not-a-hash")
    with pytest.raises(ValidationError):
        CancelRun(
            command_id=uuid4(),
            tenant_id=TENANT,
            run_id=RUN,
            runtime_build_hash=BUILD,
            command_digest=HASH_A,
            command_type="cancel",
            command_schema_version="cancel.v1",
            expected_revision=0,
            reason_ref="reason://1",
        )
    with pytest.raises(ValidationError):
        ContinueRun(
            command_id=uuid4(),
            tenant_id=TENANT,
            run_id=RUN,
            runtime_build_hash=BUILD,
            command_digest=HASH_A,
            command_type="continue",
            command_schema_version="continue.v1",
            revision=0,
            checkpoint_ref="checkpoint://1",
        )
    with pytest.raises(ValidationError):
        RunCommandReceipt(
            command_id=uuid4(),
            tenant_id=TENANT,
            run_id=RUN,
            command_seq=0,
            command_type="start",
            command_schema_version="start.v1",
            command_digest=HASH_A,
            runtime_build_hash=BUILD,
            status="pending",
            lease_until=datetime.now(),
        )
    with pytest.raises(ValidationError):
        ExecutionClaim(
            command_id=uuid4(),
            tenant_id=TENANT,
            run_id=RUN,
            command_seq=0,
            command_digest=HASH_A,
            runtime_build_hash=BUILD,
            worker_id="worker-1",
            execution_fence=1,
            lease_until=datetime.now(),
        )


@pytest.mark.asyncio
async def test_driver_argument_and_claim_lookup_guards_fail_closed() -> None:
    with pytest.raises(ValueError):
        DeterministicExecutionDriver(lease_seconds=0)

    naive_clock_driver = DeterministicExecutionDriver(clock=lambda: datetime.now())
    await naive_clock_driver.dispatch(_start())
    with pytest.raises(ValueError):
        await naive_clock_driver.claim("worker-1", BUILD)

    driver = DeterministicExecutionDriver()
    await driver.dispatch(_start())
    with pytest.raises(ValueError):
        await driver.claim("", BUILD)
    with pytest.raises(ValueError):
        await driver.claim("worker-1", "")
    with pytest.raises(ValueError):
        await driver.claim("worker-1", BUILD, lease_seconds=0)
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    with pytest.raises(ValueError):
        await driver.heartbeat(claim, lease_seconds=0)
    with pytest.raises(CommandNotFound):
        await driver.consume(claim.model_copy(update={"command_id": uuid4()}))
    with pytest.raises(StaleExecutionFence):
        await driver.consume(claim.model_copy(update={"run_id": UUID("00000000-0000-0000-0000-000000000002")}))
    with pytest.raises(ValueError):
        await driver.dead_letter(claim, reason_ref="")
    with pytest.raises(CommandNotFound):
        await driver.record_applied(
            AppliedCommandMetadata(
                tenant_id=TENANT,
                run_id=RUN,
                command_id=uuid4(),
                command_seq=0,
                command_digest=HASH_A,
                checkpoint_ref="checkpoint://unknown",
                checkpoint_hash=HASH_B,
                runtime_build_hash=BUILD,
                execution_fence=claim.execution_fence,
            )
        )


@pytest.mark.asyncio
async def test_deterministic_duration_validation_rejects_unconvertible_integers_without_state_change() -> None:
    huge = 10**10000
    with pytest.raises(ValueError, match="lease_seconds"):
        DeterministicExecutionDriver(lease_seconds=huge)

    driver = DeterministicExecutionDriver()
    await driver.dispatch(_start())
    before_claim = driver._snapshot
    with pytest.raises(ValueError, match="lease_seconds"):
        await driver.claim("worker-1", BUILD, lease_seconds=huge)
    assert driver._snapshot is before_claim

    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    before_heartbeat = driver._snapshot
    with pytest.raises(ValueError, match="lease_seconds"):
        await driver.heartbeat(claim, lease_seconds=huge)
    assert driver._snapshot is before_heartbeat


@pytest.mark.asyncio
async def test_applied_relation_classifier_rejects_corrupt_or_high_proofs() -> None:
    driver = DeterministicExecutionDriver()
    receipt = await driver.dispatch(_start())
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    metadata = await _apply_and_consume(driver, claim)
    command = _start(command_id=receipt.command_id)

    valid_snapshot = driver._snapshot
    for polluted in (
        object(),
        metadata.model_copy(update={"run_id": UUID(int=2)}),
        metadata.model_copy(update={"command_digest": HASH_C}),
    ):
        _install_run(driver, replace(_run(driver), applied=(polluted,)))  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            await driver.is_command_applied(command)
        _install_snapshot(driver, valid_snapshot)

    polluted_command = replace(_command(driver, receipt.command_id), superseded=True)
    _install_run(driver, replace(_run(driver), commands=(polluted_command,)))
    with pytest.raises(ValueError):
        await driver.is_command_applied(command)
    _install_snapshot(driver, valid_snapshot)

    high_driver = DeterministicExecutionDriver()
    high_receipt = await high_driver.dispatch(_start())
    high_claim = await high_driver.claim("worker-1", BUILD)
    assert high_claim is not None
    high_metadata = AppliedCommandMetadata(
        tenant_id=TENANT,
        run_id=RUN,
        command_id=high_receipt.command_id,
        command_seq=1,
        command_digest=high_receipt.command_digest,
        checkpoint_ref="checkpoint://high",
        checkpoint_hash=HASH_B,
        runtime_build_hash=BUILD,
        execution_fence=high_claim.execution_fence,
    )
    _install_run(
        high_driver,
        replace(
            _run(high_driver),
            applied=cast(tuple[state_machine.AppliedRecord, ...], (high_metadata,)),
        ),
    )
    with pytest.raises(ValueError):
        await high_driver.is_command_applied(_start(command_id=high_receipt.command_id))

    nonterminal_driver = DeterministicExecutionDriver()
    nonterminal_receipt = await nonterminal_driver.dispatch(_start())
    nonterminal_claim = await nonterminal_driver.claim("worker-1", BUILD)
    assert nonterminal_claim is not None
    polluted_nonterminal = replace(_command(nonterminal_driver, nonterminal_receipt.command_id), superseded=True)
    _install_run(nonterminal_driver, replace(_run(nonterminal_driver), commands=(polluted_nonterminal,)))
    with pytest.raises(ValueError):
        await nonterminal_driver.is_command_applied(_start(command_id=nonterminal_receipt.command_id))


@pytest.mark.asyncio
async def test_duplicate_applied_metadata_with_different_checkpoint_is_rejected() -> None:
    driver = DeterministicExecutionDriver()
    receipt = await driver.dispatch(_start())
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    metadata = AppliedCommandMetadata(
        tenant_id=TENANT,
        run_id=RUN,
        command_id=receipt.command_id,
        command_seq=receipt.command_seq,
        command_digest=receipt.command_digest,
        checkpoint_ref="checkpoint://original",
        checkpoint_hash=HASH_B,
        runtime_build_hash=BUILD,
        execution_fence=claim.execution_fence,
    )
    await driver.record_applied(metadata)
    with pytest.raises(ValueError):
        await driver.record_applied(metadata.model_copy(update={"checkpoint_ref": "checkpoint://changed"}))
    with pytest.raises(RunStateConflict):
        await driver.record_applied(metadata)


@pytest.mark.asyncio
async def test_unproved_prefix_cannot_be_hidden_by_a_later_command() -> None:
    reconciler = InternalDispatchAuthority("driver_reconciler")
    driver = DeterministicExecutionDriver(internal_authorities=(reconciler,))
    start = await driver.dispatch(_start())
    # A consumed command with no applied proof is an invalid persisted prefix;
    # the classifier must reject a later applied command rather than silently
    # treating that prefix as acknowledged.
    start_state = _command(driver, start.command_id)
    malformed_start = replace(start_state, status="consumed")
    _install_run(driver, replace(_run(driver), status="running", commands=(malformed_start,)))
    continue_command = ContinueRun(
        command_id=derive_continue_command_id(TENANT, RUN, 1),
        tenant_id=TENANT,
        run_id=RUN,
        runtime_build_hash=BUILD,
        command_digest=HASH_A,
        command_type="continue",
        command_schema_version="continue.v1",
        revision=1,
    )
    with pytest.raises(ExecutionDriverError):
        await driver.dispatch_internal(continue_command, reconciler)
    assert len(_run(driver).commands) == 1


@pytest.mark.asyncio
async def test_test_only_lifecycle_hooks_fail_closed_for_unknown_runs_and_tenants() -> None:
    driver = DeterministicExecutionDriver()
    await driver.dispatch(_start())
    unknown_run = UUID("00000000-0000-0000-0000-000000000099")
    with pytest.raises(RunNotFound):
        await driver._fixture_set_run_status(TENANT, unknown_run, "running")
    with pytest.raises(CommandConflict):
        await driver._fixture_set_run_status("tenant-b", RUN, "running")
    with pytest.raises(RunNotFound):
        await driver.get_run_status(TENANT, unknown_run)
    with pytest.raises(CommandConflict):
        await driver.get_run_status("tenant-b", RUN)
    with pytest.raises(ValueError):
        await driver._fixture_set_run_wait(
            TENANT,
            RUN,
            wait_ref="",
            wait_hash=HASH_A,
            wait_kind="action_result",
            source_ref="action://1",
            source_fact_version="v1",
            source_fact_hash=HASH_A,
            payload_ref="artifact://result/1",
            payload_hash=HASH_B,
        )
    with pytest.raises(ValueError):
        await driver._fixture_set_run_wait(
            TENANT,
            RUN,
            wait_ref="wait://1",
            wait_hash="bad",
            wait_kind="action_result",
            source_ref="action://1",
            source_fact_version="v1",
            source_fact_hash=HASH_A,
            payload_ref="artifact://result/1",
            payload_hash=HASH_B,
        )
    with pytest.raises(RunNotFound):
        await driver._fixture_set_run_wait(
            TENANT,
            unknown_run,
            wait_ref="wait://1",
            wait_hash=HASH_A,
            wait_kind="action_result",
            source_ref="action://1",
            source_fact_version="v1",
            source_fact_hash=HASH_A,
            payload_ref="artifact://result/1",
            payload_hash=HASH_B,
        )
    with pytest.raises(CommandConflict):
        await driver._fixture_set_run_wait(
            "tenant-b",
            RUN,
            wait_ref="wait://1",
            wait_hash=HASH_A,
            wait_kind="action_result",
            source_ref="action://1",
            source_fact_version="v1",
            source_fact_hash=HASH_A,
            payload_ref="artifact://result/1",
            payload_hash=HASH_B,
        )
    with pytest.raises(RunNotFound):
        await driver._fixture_set_user_interrupt(TENANT, unknown_run, _interrupt())
    with pytest.raises(CommandConflict):
        await driver._fixture_set_user_interrupt("tenant-b", RUN, _interrupt())
    with pytest.raises(CommandNotFound):
        await driver.is_superseded(uuid4())
    with pytest.raises(RunNotFound):
        await driver.reconcile(TENANT, unknown_run)
    with pytest.raises(CommandConflict):
        await driver.reconcile("tenant-b", RUN)


def test_strict_boundary_recursively_handles_only_known_model_shapes() -> None:
    from app.execution.driver import _prepare_strict_input, _strict_command, _strict_validate

    class ForeignModel(BaseModel):
        value: int

    class StartSubclass(StartRun):
        pass

    assert _prepare_strict_input([1, "two"]) == [1, "two"]
    assert _prepare_strict_input((1, "two")) == (1, "two")
    with pytest.raises(TypeError):
        _prepare_strict_input(ForeignModel(value=1))
    with pytest.raises(ExecutionDriverError):
        _strict_validate(StartSubclass.model_validate(_start().model_dump()), StartRun)
    with pytest.raises(TypeError):
        _strict_command(object())


@pytest.mark.asyncio
async def test_internal_dispatch_rejects_public_command_and_subclass_capability() -> None:
    class AuthoritySubclass(InternalDispatchAuthority):
        pass

    with pytest.raises(ValueError):
        DeterministicExecutionDriver(internal_authorities=(AuthoritySubclass("driver_reconciler"),))

    driver = DeterministicExecutionDriver()
    with pytest.raises(TypeError):
        await driver.dispatch_internal(
            cast(ContinueRun | RunSignal, _start()), InternalDispatchAuthority("driver_reconciler")
        )

    continue_command = ContinueRun(
        command_id=derive_continue_command_id(TENANT, RUN, 0),
        tenant_id=TENANT,
        run_id=RUN,
        runtime_build_hash=BUILD,
        command_digest=HASH_A,
        command_type="continue",
        command_schema_version="continue.v1",
        revision=0,
    )
    with pytest.raises(TypeError):
        await driver.dispatch_internal(continue_command, AuthoritySubclass("driver_reconciler"))

    wrong_authority = InternalDispatchAuthority("action_completion_bridge")
    wrong_driver = DeterministicExecutionDriver(internal_authorities=(wrong_authority,))
    with pytest.raises(RunStateConflict):
        await wrong_driver.dispatch_internal(continue_command, wrong_authority)


@pytest.mark.asyncio
async def test_claim_tenant_filter_has_no_cross_tenant_candidate() -> None:
    driver = DeterministicExecutionDriver()
    await driver.dispatch(_start())
    assert await driver.claim("worker-1", BUILD, tenant_id="tenant-b") is None


@pytest.mark.asyncio
async def test_dispatch_admission_matrix_rejects_wrong_run_build_status_and_signal_bindings() -> None:
    unknown = DeterministicExecutionDriver()
    with pytest.raises(RunNotFound):
        await unknown.dispatch(_resume())

    driver = DeterministicExecutionDriver()
    await driver.dispatch(_start())
    start_claim = await driver.claim("worker-1", BUILD)
    assert start_claim is not None
    await _apply_and_consume(driver, start_claim)
    with pytest.raises(VersionUnavailable):
        await driver.dispatch(_resume().model_copy(update={"runtime_build_hash": BUILD_NEW}))
    with pytest.raises(CommandConflict):
        await driver.dispatch(_resume().model_copy(update={"tenant_id": "tenant-b"}))
    with pytest.raises(RunStateConflict):
        await driver.dispatch(_resume())

    reconciler = InternalDispatchAuthority("driver_reconciler")
    continue_driver = DeterministicExecutionDriver(internal_authorities=(reconciler,))
    await continue_driver.dispatch(_start())
    claim = await continue_driver.claim("worker-1", BUILD)
    assert claim is not None
    await _apply_and_consume(continue_driver, claim)
    valid_continue = ContinueRun(
        command_id=derive_continue_command_id(TENANT, RUN, 1),
        tenant_id=TENANT,
        run_id=RUN,
        runtime_build_hash=BUILD,
        command_digest=HASH_A,
        command_type="continue",
        command_schema_version="continue.v1",
        revision=1,
    )
    await continue_driver._fixture_set_run_status(TENANT, RUN, "waiting_user_input")
    with pytest.raises(RunStateConflict):
        await continue_driver.dispatch_internal(valid_continue, reconciler)
    await continue_driver._fixture_set_run_status(TENANT, RUN, "running")
    with pytest.raises(RunStateConflict):
        await continue_driver.dispatch_internal(valid_continue.model_copy(update={"command_id": uuid4()}), reconciler)

    signal_authority = InternalDispatchAuthority("action_completion_bridge")
    child_authority = InternalDispatchAuthority("child_completion_bridge")
    signal_driver = DeterministicExecutionDriver(internal_authorities=(signal_authority, child_authority))
    await signal_driver.dispatch(_start())
    signal_claim = await signal_driver.claim("worker-1", BUILD)
    assert signal_claim is not None
    await _apply_and_consume(signal_driver, signal_claim)
    signal = _signal()
    with pytest.raises(RunStateConflict):
        await signal_driver.dispatch_internal(
            _signal(command_id=derive_signal_command_id(TENANT, RUN, signal.signal_id)), signal_authority
        )
    await signal_driver._fixture_set_run_wait(
        TENANT,
        RUN,
        wait_ref="wait://1",
        wait_hash=HASH_A,
        wait_kind="action_result",
        source_ref="action://1",
        source_fact_version="v1",
        source_fact_hash=HASH_A,
        payload_ref="artifact://action-result/1",
        payload_hash=HASH_B,
    )
    with pytest.raises(CommandConflict):
        await signal_driver.dispatch_internal(signal.model_copy(update={"command_id": uuid4()}), signal_authority)
    child_payload = ChildCompletionPayload(
        source_ref="action://1",
        source_fact_version="v1",
        source_fact_hash=HASH_A,
        payload_ref="artifact://action-result/1",
        payload_hash=HASH_B,
        payload_type="child_run_completed",
    )
    child_signal = RunSignal(
        command_id=derive_signal_command_id(TENANT, RUN, derive_signal_id("action://1", "v1", HASH_A)),
        tenant_id=TENANT,
        run_id=RUN,
        runtime_build_hash=BUILD,
        command_digest=HASH_C,
        command_type="signal",
        command_schema_version="signal.v1",
        signal_id=derive_signal_id("action://1", "v1", HASH_A),
        wait_ref="wait://1",
        wait_hash=HASH_A,
        payload=child_payload,
    )
    with pytest.raises(RunStateConflict):
        await signal_driver.dispatch_internal(child_signal, child_authority)
    valid_signal = signal.model_copy(update={"command_id": derive_signal_command_id(TENANT, RUN, signal.signal_id)})
    await signal_driver.dispatch_internal(valid_signal, signal_authority)
    with pytest.raises(RunSignalConflict):
        await signal_driver.dispatch_internal(
            valid_signal.model_copy(update={"command_digest": HASH_A}), signal_authority
        )
