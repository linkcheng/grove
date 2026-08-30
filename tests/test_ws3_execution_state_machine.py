from __future__ import annotations

from uuid import UUID

import app.execution.state_machine as state_machine
import pytest
from app.execution.driver import (
    AppliedCommandMetadata,
    DeterministicExecutionDriver,
    ExecutionDriverError,
    InterruptBinding,
    ResumeRun,
    StartRun,
)

RUN = UUID("00000000-0000-0000-0000-000000000001")
TENANT = "tenant-a"
BUILD = "b" * 64
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def _start(*, command_id: UUID | None = None) -> StartRun:
    return StartRun(
        command_id=command_id or UUID("00000000-0000-0000-0000-000000000002"),
        tenant_id=TENANT,
        run_id=RUN,
        runtime_build_hash=BUILD,
        command_digest=HASH_A,
        command_type="start",
        command_schema_version="start.v1",
        payload_ref="artifact://start/1",
        payload_hash=HASH_B,
    )


def _interrupt() -> InterruptBinding:
    return InterruptBinding(
        interrupt_ref="interrupt://1",
        interrupt_hash=HASH_B,
        checkpoint_ref="checkpoint://1",
        checkpoint_hash=HASH_C,
        interrupt_schema_ref="schema://interrupt/v1",
        interrupt_schema_hash=HASH_A,
        nonce_hash=HASH_A,
    )


async def _applied_start(driver: DeterministicExecutionDriver) -> None:
    receipt = await driver.dispatch(_start())
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    await driver.record_applied(
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
    await driver.consume(claim)


@pytest.mark.asyncio
async def test_heartbeat_failure_keeps_the_same_snapshot_object(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = DeterministicExecutionDriver()
    await driver.dispatch(_start())
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    before = driver._snapshot

    def fail(*args: object, **kwargs: object) -> object:
        raise RuntimeError("candidate construction failed")

    monkeypatch.setattr(state_machine, "_replace_strict", fail)
    with pytest.raises(RuntimeError, match="candidate construction failed"):
        await driver.heartbeat(claim)
    assert driver._snapshot is before


@pytest.mark.asyncio
async def test_resume_applied_candidate_failure_keeps_nonce_and_root(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = DeterministicExecutionDriver()
    await _applied_start(driver)
    binding = _interrupt()
    await driver._fixture_set_user_interrupt(TENANT, RUN, binding)
    resume = ResumeRun(
        command_id=UUID("00000000-0000-0000-0000-000000000003"),
        tenant_id=TENANT,
        run_id=RUN,
        runtime_build_hash=BUILD,
        command_digest=HASH_C,
        command_type="resume",
        command_schema_version="resume.v1",
        expected_revision=1,
        input_ref="artifact://resume/1",
        input_hash=HASH_B,
        interrupt=binding,
    )
    receipt = await driver.dispatch(resume)
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    metadata = AppliedCommandMetadata(
        tenant_id=TENANT,
        run_id=RUN,
        command_id=receipt.command_id,
        command_seq=receipt.command_seq,
        command_digest=receipt.command_digest,
        checkpoint_ref="checkpoint://resume",
        checkpoint_hash=HASH_B,
        runtime_build_hash=BUILD,
        execution_fence=claim.execution_fence,
    )
    before = driver._snapshot

    def fail(*args: object, **kwargs: object) -> tuple[object, ...]:
        raise RuntimeError("nonce candidate failed")

    monkeypatch.setattr(state_machine, "_with_applied", fail)
    with pytest.raises(RuntimeError, match="nonce candidate failed"):
        await driver.record_applied(metadata)
    assert driver._snapshot is before
    assert driver._snapshot.runs[RUN].interrupt == binding
    assert binding.nonce_hash not in driver._snapshot.runs[RUN].consumed_interrupt_nonces


@pytest.mark.asyncio
async def test_malformed_root_key_or_owner_fails_before_idempotent_read_or_claim() -> None:
    driver = DeterministicExecutionDriver()
    receipt = await driver.dispatch(_start())
    valid = driver._snapshot
    run = valid.runs[RUN]
    malformed_roots = (
        state_machine.DriverSnapshot({UUID(int=99): run}),
        state_machine.DriverSnapshot(
            {
                RUN: state_machine.RunAggregate(
                    tenant_id=run.tenant_id,
                    run_id=UUID(int=99),
                    runtime_build_hash=run.runtime_build_hash,
                    commands=run.commands,
                    revision=run.revision,
                    next_command_seq=run.next_command_seq,
                )
            }
        ),
    )
    for malformed in malformed_roots:
        object.__setattr__(driver, "_snapshot", malformed)
        before = driver._snapshot
        with pytest.raises(ExecutionDriverError):
            await driver.dispatch(_start(command_id=receipt.command_id))
        with pytest.raises(ExecutionDriverError):
            await driver.applied_for(RUN)
        with pytest.raises(ExecutionDriverError):
            await driver.claim("worker-1", BUILD)
        assert driver._snapshot is before
        object.__setattr__(driver, "_snapshot", valid)


@pytest.mark.asyncio
@pytest.mark.parametrize("lease_seconds", [True, False, float("nan"), float("inf"), 10**100, 0, -1])
async def test_lease_bounds_fail_before_fence_or_snapshot_change(lease_seconds: object) -> None:
    driver = DeterministicExecutionDriver()
    await driver.dispatch(_start())
    before = driver._snapshot
    with pytest.raises(ValueError):
        await driver.claim("worker-1", BUILD, lease_seconds=lease_seconds)  # type: ignore[arg-type]
    assert driver._snapshot is before
    assert driver._snapshot.runs[RUN].execution_fence == 0


@pytest.mark.asyncio
async def test_fixture_wait_kind_is_a_closed_runtime_union_and_has_no_side_effect() -> None:
    driver = DeterministicExecutionDriver()
    await driver.dispatch(_start())
    before = driver._snapshot
    with pytest.raises(ValueError):
        await driver._fixture_set_run_wait(
            TENANT,
            RUN,
            wait_ref="wait://1",
            wait_hash=HASH_A,
            wait_kind="not-a-runtime-wait",
            source_ref="action://1",
            source_fact_version="v1",
            source_fact_hash=HASH_A,
            payload_ref="artifact://result/1",
            payload_hash=HASH_B,
        )
    assert driver._snapshot is before


@pytest.mark.asyncio
async def test_every_write_failure_keeps_the_same_root_snapshot() -> None:
    # dispatch admission failure
    dispatch_driver = DeterministicExecutionDriver()
    await dispatch_driver.dispatch(_start())
    before = dispatch_driver._snapshot
    with pytest.raises(ExecutionDriverError):
        await dispatch_driver.dispatch(
            ResumeRun(
                command_id=UUID("00000000-0000-0000-0000-000000000004"),
                tenant_id=TENANT,
                run_id=RUN,
                runtime_build_hash=BUILD,
                command_digest=HASH_C,
                command_type="resume",
                command_schema_version="resume.v1",
                expected_revision=0,
                input_ref="artifact://resume/1",
                input_hash=HASH_B,
                interrupt=_interrupt(),
            )
        )
    assert dispatch_driver._snapshot is before

    # claim and heartbeat failures happen before the fence/lease candidate is
    # committed.
    claim_driver = DeterministicExecutionDriver(lease_seconds=1)
    await claim_driver.dispatch(_start())
    before = claim_driver._snapshot
    with pytest.raises(ValueError):
        await claim_driver.claim("worker-1", BUILD, lease_seconds=float("inf"))
    assert claim_driver._snapshot is before
    claim = await claim_driver.claim("worker-1", BUILD)
    assert claim is not None
    claim_driver.advance_time(2)
    before = claim_driver._snapshot
    with pytest.raises(ExecutionDriverError):
        await claim_driver.heartbeat(claim)
    assert claim_driver._snapshot is before

    # consume requires proof and therefore leaves the active lease untouched.
    consume_driver = DeterministicExecutionDriver()
    await consume_driver.dispatch(_start())
    claim = await consume_driver.claim("worker-1", BUILD)
    assert claim is not None
    before = consume_driver._snapshot
    with pytest.raises(ExecutionDriverError):
        await consume_driver.consume(claim)
    assert consume_driver._snapshot is before

    # dead-letter rejects an already applied command without clearing its lease.
    dead_driver = DeterministicExecutionDriver()
    receipt = await dead_driver.dispatch(_start())
    claim = await dead_driver.claim("worker-1", BUILD)
    assert claim is not None
    await dead_driver.record_applied(
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
    before = dead_driver._snapshot
    with pytest.raises(ExecutionDriverError):
        await dead_driver.dead_letter(claim, "error://already-applied")
    assert dead_driver._snapshot is before

    # reconcile is blocked by a durable dead-letter closure.
    reconcile_driver = DeterministicExecutionDriver()
    await reconcile_driver.dispatch(_start())
    claim = await reconcile_driver.claim("worker-1", BUILD)
    assert claim is not None
    await reconcile_driver.dead_letter(claim, "error://dead")
    await reconcile_driver._fixture_set_run_status(TENANT, RUN, "running")
    before = reconcile_driver._snapshot
    with pytest.raises(ExecutionDriverError):
        await reconcile_driver.reconcile(TENANT, RUN)
    assert reconcile_driver._snapshot is before

    # fixture projections use the same commit seam and reject unknown status.
    fixture_driver = DeterministicExecutionDriver()
    await fixture_driver.dispatch(_start())
    before = fixture_driver._snapshot
    with pytest.raises(ExecutionDriverError):
        await fixture_driver._fixture_set_run_status(TENANT, RUN, "unknown")
    assert fixture_driver._snapshot is before


@pytest.mark.asyncio
async def test_public_results_are_detached_from_the_authoritative_snapshot() -> None:
    driver = DeterministicExecutionDriver()
    start_receipt = await driver.dispatch(_start())
    stored_receipt = driver._snapshot.runs[RUN].commands[0].receipt
    assert start_receipt is not stored_receipt
    start_receipt.__dict__["status"] = "consumed"
    assert stored_receipt.status == "pending"
    assert (await driver.dispatch(_start())).status == "pending"

    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    stored_claim = driver._snapshot.runs[RUN].commands[0].active_claim
    assert stored_claim is not None and claim is not stored_claim
    claim.__dict__["worker_id"] = "forged-worker"
    assert stored_claim.worker_id == "worker-1"


@pytest.mark.asyncio
async def test_applied_metadata_has_a_stored_identity_proof() -> None:
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
        checkpoint_ref="checkpoint://start",
        checkpoint_hash=HASH_B,
        runtime_build_hash=BUILD,
        execution_fence=claim.execution_fence,
    )
    returned = await driver.record_applied(metadata)
    returned.__dict__["checkpoint_ref"] = "checkpoint://forged"
    stored = await driver.applied_for(RUN)
    assert stored is not None
    assert stored.checkpoint_ref == "checkpoint://start"

    internal = driver._snapshot.runs[RUN].applied[0]
    object.__setattr__(internal.metadata, "checkpoint_ref", "checkpoint://forged")
    with pytest.raises(ExecutionDriverError):
        await driver.applied_for(RUN)


@pytest.mark.asyncio
async def test_empty_run_snapshot_is_rejected_as_missing_genesis() -> None:
    driver = DeterministicExecutionDriver()
    malformed = state_machine.DriverSnapshot(
        {
            RUN: state_machine.RunAggregate(
                tenant_id=TENANT,
                run_id=RUN,
                runtime_build_hash=BUILD,
                status="running",
            )
        }
    )
    object.__setattr__(driver, "_snapshot", malformed)
    with pytest.raises(ExecutionDriverError):
        await driver.applied_for(RUN)


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds", [True, False, 0, -1, float("nan"), float("inf"), 10**100])
async def test_fixture_clock_rejects_non_monotonic_or_unbounded_advances(seconds: object) -> None:
    driver = DeterministicExecutionDriver()
    before_offset = driver._offset
    with pytest.raises(ValueError):
        driver.advance_time(seconds)  # type: ignore[arg-type]
    assert driver._offset == before_offset


@pytest.mark.asyncio
async def test_fixture_clock_cannot_rollback_an_expired_claim() -> None:
    driver = DeterministicExecutionDriver(lease_seconds=5)
    await driver.dispatch(_start())
    claim = await driver.claim("worker-1", BUILD)
    assert claim is not None
    driver.advance_time(6)
    with pytest.raises(ValueError):
        driver.advance_time(-6)
    with pytest.raises(ExecutionDriverError):
        await driver.heartbeat(claim)


def test_ws7_lease_cap_is_300_seconds_in_both_enforcement_modules() -> None:
    # Owner-approved WS-7 raise (2026-08-26): real LLM generation latency
    # regularly exceeds the previous 90s cap, which blocked functional
    # validation.  The correctness invariant (invoke budget strictly below
    # lease minus margin) is unchanged; only the ceiling moves.
    from app.execution.postgres import MAX_LEASE_SECONDS as postgres_cap

    assert state_machine.MAX_LEASE_SECONDS == 300.0
    assert postgres_cap == 300.0
