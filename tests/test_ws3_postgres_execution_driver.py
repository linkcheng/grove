from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest
from app.contracts.canonical import canonical_hash
from app.execution.contracts import (
    BIGINT_MAX,
    CancelRun,
    CommandConflict,
    ExecutionClaim,
    ExecutionDriverError,
    ExecutionFenceExhausted,
    RunNotFound,
    RunStateConflict,
    StaleExecutionFence,
    VersionUnavailable,
)
from app.execution.postgres import MAX_LEASE_SECONDS, PostgresExecutionDriver
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Result:
    def __init__(self, *, row: Mapping[str, object] | None = None, scalar: object = None) -> None:
        self._row = row
        self._scalar = scalar

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> Mapping[str, object] | None:
        return self._row

    def scalar_one_or_none(self) -> object:
        return self._scalar


class _Session:
    def __init__(self, results: list[_Result]) -> None:
        self.results = results
        self.calls: list[tuple[object, Mapping[str, object] | None]] = []

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def begin(self) -> _Transaction:
        return _Transaction()

    async def execute(self, statement: object, _params: Mapping[str, object] | None = None) -> _Result:
        self.calls.append((statement, _params))
        return self.results.pop(0)


class _Factory:
    def __init__(self, results: list[_Result]) -> None:
        self.session = _Session(results)
        self.calls = 0

    def __call__(self) -> _Session:
        self.calls += 1
        return self.session


class _IntegerSubclass(int):
    pass


class _FloatSubclass(float):
    pass


def _driver(results: list[_Result]) -> tuple[PostgresExecutionDriver, _Factory]:
    factory = _Factory(results)
    driver = object.__new__(PostgresExecutionDriver)
    driver._session_factory = factory  # type: ignore[assignment]
    driver._command_session_factory = None
    driver._reconciliation_session_factory = None
    driver._lease_seconds = 30.0
    driver._operation_timeout_seconds = 1.0
    return driver, factory


def _dual_driver(
    runtime_results: list[_Result], command_results: list[_Result]
) -> tuple[PostgresExecutionDriver, _Factory, _Factory]:
    runtime_factory = _Factory(runtime_results)
    command_factory = _Factory(command_results)
    driver = object.__new__(PostgresExecutionDriver)
    driver._session_factory = runtime_factory  # type: ignore[assignment]
    driver._command_session_factory = command_factory  # type: ignore[assignment]
    driver._reconciliation_session_factory = None
    driver._lease_seconds = 30.0
    driver._operation_timeout_seconds = 1.0
    return driver, runtime_factory, command_factory


def _scope_results(result: _Result) -> list[_Result]:
    return [_Result(), _Result(), _Result(), result]


def _dispatch_results(result: _Result) -> list[_Result]:
    payload_hash = canonical_hash({})
    payload_row = {
        "payload_ref": f"command-payload:{payload_hash}",
        "payload_hash": payload_hash,
        "command_schema_version": "cancel.v1",
        "sensitivity": "sensitive",
        "retention": "run_completion",
    }
    return [_Result(), _Result(), _Result(), _Result(), _Result(row=payload_row), result]


def _claim_row(claim: ExecutionClaim) -> dict[str, object]:
    return {
        "result_code": "claimed",
        "command_id": claim.command_id,
        "run_id": claim.run_id,
        "command_seq": claim.command_seq,
        "command_digest": claim.command_digest,
        "runtime_build_hash": claim.runtime_build_hash,
        "execution_fence": claim.execution_fence,
        "lease_until": claim.lease_until,
    }


def _unused_session_factory() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("postgresql+psycopg://unused:unused@127.0.0.1/unused")
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _duration_values(maximum: int | float) -> tuple[tuple[str, int | float], ...]:
    return (
        ("true", True),
        ("false", False),
        ("int-subclass", _IntegerSubclass(1)),
        ("float-subclass", _FloatSubclass(1.0)),
        ("nan", float("nan")),
        ("positive-infinity", float("inf")),
        ("negative-infinity", float("-inf")),
        ("zero", 0),
        ("negative", -1),
        ("ordinary-overflow", maximum + 1),
        ("huge-integer", 10**10000),
    )


def _invalid_duration_cases(maximum: int | float) -> list[object]:
    return [pytest.param(value, id=case_id) for case_id, value in _duration_values(maximum)]


def _constructor_invalid_duration_cases() -> list[object]:
    return [
        pytest.param(field, value, id=f"{field.removesuffix('_seconds')}-{case_id}")
        for field, maximum in (("lease_seconds", MAX_LEASE_SECONDS), ("operation_timeout_seconds", 30))
        for case_id, value in _duration_values(maximum)
    ]


def _claim() -> ExecutionClaim:
    now = datetime.now(UTC)
    return ExecutionClaim(
        command_id=uuid4(),
        tenant_id="tenant-a",
        run_id=uuid4(),
        command_seq=0,
        command_digest="a" * 64,
        runtime_build_hash="b" * 64,
        worker_id="worker-a",
        execution_fence=1,
        lease_until=now + timedelta(seconds=30),
    )


def _cancel() -> CancelRun:
    return CancelRun(
        command_id=uuid4(),
        tenant_id="tenant-a",
        run_id=uuid4(),
        runtime_build_hash="b" * 64,
        command_digest="c" * 64,
        command_type="cancel",
        command_schema_version="cancel.v1",
        expected_revision=1,
    )


@pytest.mark.asyncio
async def test_claim_maps_database_outcomes_at_the_public_seam() -> None:
    driver, _ = _driver(_scope_results(_Result()))
    assert await driver.claim("worker-a", "b" * 64, "tenant-a") is None

    driver, _ = _driver(_scope_results(_Result(row={"result_code": "version_unavailable"})))
    with pytest.raises(VersionUnavailable):
        await driver.claim("worker-a", "b" * 64, "tenant-a")

    driver, _ = _driver(_scope_results(_Result(row={"result_code": "unexpected"})))
    with pytest.raises(RuntimeError, match="unknown claim result"):
        await driver.claim("worker-a", "b" * 64, "tenant-a")

    claim = _claim()
    driver, factory = _driver(
        _scope_results(
            _Result(
                row={
                    "result_code": "claimed",
                    "command_id": claim.command_id,
                    "run_id": claim.run_id,
                    "command_seq": claim.command_seq,
                    "command_digest": claim.command_digest,
                    "runtime_build_hash": claim.runtime_build_hash,
                    "execution_fence": claim.execution_fence,
                    "lease_until": claim.lease_until,
                }
            )
        )
    )
    actual = await driver.claim(claim.worker_id, claim.runtime_build_hash, claim.tenant_id)
    assert actual == claim
    assert len(factory.session.calls) == 4


@pytest.mark.asyncio
async def test_cancel_dispatch_maps_database_receipt_and_stable_conflicts() -> None:
    cancel = _cancel()
    receipt = {
        "result_code": "accepted",
        "command_id": cancel.command_id,
        "tenant_id": cancel.tenant_id,
        "run_id": cancel.run_id,
        "command_seq": 1,
        "command_type": "cancel",
        "command_schema_version": "cancel.v1",
        "command_digest": cancel.command_digest,
        "runtime_build_hash": cancel.runtime_build_hash,
        "status": "pending",
    }
    driver, _, factory = _dual_driver([], _dispatch_results(_Result(row=receipt)))
    actual = await driver.dispatch(cancel)
    assert actual.command_id == cancel.command_id
    assert actual.command_seq == 1
    assert actual.status == "pending"
    assert "grove_accept_cancel_run" in str(factory.session.calls[-1][0])
    no_reason_params = factory.session.calls[3][1]
    assert no_reason_params is not None
    assert no_reason_params["payload_ref"] == f"command-payload:{canonical_hash({})}"
    assert no_reason_params["payload_hash"] == canonical_hash({})
    assert no_reason_params["payload"] == "{}\n"
    function_params = factory.session.calls[-1][1]
    assert function_params is not None
    assert function_params["payload"] == "{}\n"

    for result_code, error_type in (
        ("command_conflict", CommandConflict),
        ("run_not_found", RunNotFound),
        ("revision_conflict", RunStateConflict),
        ("invalid_state", RunStateConflict),
        ("payload_conflict", RunStateConflict),
        ("build_conflict", VersionUnavailable),
        ("revision_overflow", ExecutionDriverError),
        ("fence_exhausted", ExecutionFenceExhausted),
    ):
        driver, _, _ = _dual_driver([], _dispatch_results(_Result(row={"result_code": result_code})))
        with pytest.raises(error_type):
            await driver.dispatch(cancel)


@pytest.mark.asyncio
async def test_cancel_dispatch_requires_explicit_api_factory() -> None:
    driver, runtime_factory = _driver(
        _scope_results(
            _Result(
                row={
                    "result_code": "accepted",
                    "command_id": _cancel().command_id,
                    "tenant_id": "tenant-a",
                    "run_id": _cancel().run_id,
                    "command_seq": 1,
                    "command_type": "cancel",
                    "command_schema_version": "cancel.v1",
                    "command_digest": "c" * 64,
                    "runtime_build_hash": "b" * 64,
                    "status": "pending",
                }
            )
        )
    )
    with pytest.raises(ExecutionDriverError, match="command_session_factory"):
        await driver.dispatch(_cancel())
    assert runtime_factory.calls == 0


@pytest.mark.asyncio
async def test_cancel_dispatch_inserts_content_addressed_wrapper_before_function() -> None:
    cancel = _cancel().model_copy(update={"reason_ref": "r" * 512, "reason_hash": "d" * 64})
    receipt = {
        "result_code": "accepted",
        "command_id": cancel.command_id,
        "tenant_id": cancel.tenant_id,
        "run_id": cancel.run_id,
        "command_seq": 1,
        "command_type": "cancel",
        "command_schema_version": "cancel.v1",
        "command_digest": cancel.command_digest,
        "runtime_build_hash": cancel.runtime_build_hash,
        "status": "pending",
    }
    expected_payload_hash = canonical_hash({"reason_ref": cancel.reason_ref, "reason_hash": cancel.reason_hash})
    driver, runtime_factory, command_factory = _dual_driver(
        [],
        [
            _Result(),
            _Result(),
            _Result(),
            _Result(),
            _Result(
                row={
                    "payload_ref": f"command-payload:{expected_payload_hash}",
                    "payload_hash": expected_payload_hash,
                    "command_schema_version": "cancel.v1",
                    "sensitivity": "sensitive",
                    "retention": "run_completion",
                }
            ),
            _Result(row=receipt),
        ],
    )
    await driver.dispatch(cancel)
    assert runtime_factory.calls == 0
    assert command_factory.calls == 1
    insert_params = command_factory.session.calls[3][1]
    assert insert_params is not None
    assert len(cast(str, insert_params["payload_ref"])) <= 256
    assert insert_params["payload_ref"] != cancel.reason_ref
    assert insert_params["payload_hash"] == expected_payload_hash
    assert insert_params["payload"] == ('{"reason_hash":"' + "d" * 64 + '","reason_ref":"' + "r" * 512 + '"}\n')


@pytest.mark.asyncio
async def test_cancel_dispatch_rejects_non_exact_or_non_cancel_before_database_access() -> None:
    driver, factory = _driver([])
    with pytest.raises(ExecutionDriverError):
        await driver.dispatch(_cancel().model_copy(update={"expected_revision": True}))
    with pytest.raises(TypeError):
        await driver.dispatch(_claim())  # type: ignore[arg-type]
    assert factory.session.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "revision",
    [
        pytest.param(BIGINT_MAX, id="bigint-max"),
        pytest.param(BIGINT_MAX + 1, id="bigint-overflow"),
        pytest.param(True, id="bool"),
        pytest.param(_IntegerSubclass(0), id="int-subclass"),
    ],
)
async def test_cancel_dispatch_rejects_revision_overflow_before_database_access(revision: object) -> None:
    command = _cancel().model_construct(expected_revision=revision)
    driver, runtime_factory, command_factory = _dual_driver([], [])
    with pytest.raises(ExecutionDriverError):
        await driver.dispatch(command)
    assert runtime_factory.calls == 0
    assert command_factory.calls == 0


@pytest.mark.asyncio
async def test_claim_maps_fence_exhaustion_to_stable_domain_error() -> None:
    driver, factory = _driver(_scope_results(_Result(row={"result_code": "fence_exhausted"})))
    with pytest.raises(ExecutionFenceExhausted):
        await driver.claim("worker-a", "b" * 64, "tenant-a")
    assert len(factory.session.calls) == 4


@pytest.mark.asyncio
async def test_heartbeat_maps_stale_and_current_database_results() -> None:
    claim = _claim()
    driver, _ = _driver(_scope_results(_Result(scalar=None)))
    with pytest.raises(StaleExecutionFence):
        await driver.heartbeat(claim)

    extended = claim.lease_until + timedelta(seconds=10)
    driver, _ = _driver(_scope_results(_Result(scalar=extended)))
    actual = await driver.heartbeat(claim)
    assert actual.execution_fence == claim.execution_fence
    assert actual.lease_until == extended

    with pytest.raises(ExecutionDriverError, match="exact ExecutionClaim"):
        await driver.heartbeat(SimpleNamespace())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_heartbeat_rejects_coercion_and_subclasses_before_database_access() -> None:
    class ClaimSubclass(ExecutionClaim):
        pass

    class StringSubclass(str):
        pass

    class IntegerSubclass(int):
        pass

    class DatetimeSubclass(datetime):
        pass

    class UUIDSubclass(UUID):
        pass

    claim = _claim()
    invalid = (
        claim.model_copy(update={"execution_fence": True}),
        claim.model_copy(update={"lease_until": claim.lease_until.isoformat()}),
        claim.model_copy(update={"worker_id": StringSubclass(claim.worker_id)}),
        claim.model_copy(update={"command_seq": IntegerSubclass(claim.command_seq)}),
        claim.model_copy(update={"lease_until": DatetimeSubclass.fromtimestamp(claim.lease_until.timestamp(), UTC)}),
        claim.model_copy(update={"command_id": UUIDSubclass(str(claim.command_id))}),
        ClaimSubclass.model_validate(claim.model_dump(mode="python")),
    )
    for value in invalid:
        driver, factory = _driver([])
        with pytest.raises(ExecutionDriverError):
            await driver.heartbeat(value)
        assert factory.session.calls == []


@pytest.mark.asyncio
async def test_driver_rejects_unbounded_inputs_before_database_access() -> None:
    driver, _ = _driver([])
    for args in (
        ("", "b" * 64, "tenant-a", None),
        ("worker", "latest", "tenant-a", None),
        ("worker", "b" * 64, "", None),
        ("worker", "b" * 64, "tenant-a", 91),
    ):
        with pytest.raises(ValueError):
            await driver.claim(*args)

    engine = create_async_engine("postgresql+psycopg://unused:unused@127.0.0.1/unused")
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        with pytest.raises(ValueError, match="operation_timeout_seconds"):
            PostgresExecutionDriver(factory, operation_timeout_seconds=31)
        with pytest.raises(ValueError, match="lease_seconds"):
            PostgresExecutionDriver(factory, lease_seconds=91)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", _invalid_duration_cases(MAX_LEASE_SECONDS))
async def test_claim_rejects_every_invalid_lease_duration_before_opening_database_session(
    duration: int | float,
) -> None:
    driver, factory = _driver([])
    with pytest.raises(ValueError, match="lease_seconds"):
        await driver.claim("worker-a", "b" * 64, "tenant-a", duration)
    assert factory.calls == 0
    assert factory.session.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", _invalid_duration_cases(MAX_LEASE_SECONDS))
async def test_heartbeat_rejects_every_invalid_lease_duration_before_opening_database_session(
    duration: int | float,
) -> None:
    driver, factory = _driver([])
    with pytest.raises(ValueError, match="lease_seconds"):
        await driver.heartbeat(_claim(), duration)
    assert factory.calls == 0
    assert factory.session.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field,duration", _constructor_invalid_duration_cases())
async def test_constructor_rejects_invalid_lease_and_timeout_durations(
    field: str,
    duration: int | float,
) -> None:
    engine, session_factory = _unused_session_factory()
    try:
        with pytest.raises(ValueError, match=field):
            if field == "lease_seconds":
                PostgresExecutionDriver(session_factory, lease_seconds=duration)
            else:
                PostgresExecutionDriver(session_factory, operation_timeout_seconds=duration)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "duration"),
    [
        pytest.param("lease_seconds", 1, id="lease-int"),
        pytest.param("lease_seconds", 1.5, id="lease-float"),
        pytest.param("lease_seconds", MAX_LEASE_SECONDS, id="lease-max"),
        pytest.param("operation_timeout_seconds", 1, id="timeout-int"),
        pytest.param("operation_timeout_seconds", 1.5, id="timeout-float"),
        pytest.param("operation_timeout_seconds", 30, id="timeout-max"),
    ],
)
async def test_constructor_accepts_valid_lease_and_timeout_durations(field: str, duration: int | float) -> None:
    engine, session_factory = _unused_session_factory()
    try:
        if field == "lease_seconds":
            driver = PostgresExecutionDriver(session_factory, lease_seconds=duration)
        else:
            driver = PostgresExecutionDriver(session_factory, operation_timeout_seconds=duration)
        assert getattr(driver, f"_{field}") == float(duration)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "duration",
    [pytest.param(1, id="int"), pytest.param(1.5, id="float"), pytest.param(MAX_LEASE_SECONDS, id="max")],
)
async def test_claim_accepts_valid_lease_durations_through_fake_database_result(duration: int | float) -> None:
    claim = _claim()
    driver, factory = _driver(_scope_results(_Result(row=_claim_row(claim))))
    actual = await driver.claim(claim.worker_id, claim.runtime_build_hash, claim.tenant_id, duration)
    assert actual == claim
    assert factory.calls == 1
    assert len(factory.session.calls) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "duration",
    [pytest.param(1, id="int"), pytest.param(1.5, id="float"), pytest.param(MAX_LEASE_SECONDS, id="max")],
)
async def test_heartbeat_accepts_valid_lease_durations_through_fake_database_result(duration: int | float) -> None:
    claim = _claim()
    extended = claim.lease_until + timedelta(seconds=10)
    driver, factory = _driver(_scope_results(_Result(scalar=extended)))
    actual = await driver.heartbeat(claim, duration)
    assert actual.lease_until == extended
    assert actual.execution_fence == claim.execution_fence
    assert factory.calls == 1
    assert len(factory.session.calls) == 4


def _consumed_row(claim: ExecutionClaim, *, result_code: str = "consumed") -> dict[str, object]:
    return {
        "result_code": result_code,
        "command_id": claim.command_id,
        "tenant_id": claim.tenant_id,
        "run_id": claim.run_id,
        "command_seq": claim.command_seq,
        "command_type": "start",
        "command_schema_version": "start.v1",
        "command_digest": claim.command_digest,
        "runtime_build_hash": claim.runtime_build_hash,
        "status": "consumed",
    }


def _dead_letter_row(claim: ExecutionClaim, *, result_code: str = "dead_letter") -> dict[str, object]:
    return {
        "result_code": result_code,
        "command_id": claim.command_id,
        "tenant_id": claim.tenant_id,
        "run_id": claim.run_id,
        "command_seq": claim.command_seq,
        "command_type": "start",
        "command_schema_version": "start.v1",
        "command_digest": claim.command_digest,
        "runtime_build_hash": claim.runtime_build_hash,
        "status": "dead_letter",
    }


def _reconciled_row(claim: ExecutionClaim, *, result_code: str) -> dict[str, object]:
    status = "consumed" if result_code == "consumed" else "pending"
    return {
        "result_code": result_code,
        "command_id": claim.command_id,
        "tenant_id": claim.tenant_id,
        "run_id": claim.run_id,
        "command_seq": claim.command_seq,
        "command_type": "start",
        "command_schema_version": "start.v1",
        "command_digest": claim.command_digest,
        "runtime_build_hash": claim.runtime_build_hash,
        "status": status,
    }


@pytest.mark.asyncio
async def test_dead_letter_maps_receipt_and_stable_database_outcomes() -> None:
    claim = _claim()
    driver, factory = _driver(_scope_results(_Result(row=_dead_letter_row(claim))))
    receipt = await driver.dead_letter(claim, "worker-timeout")
    assert receipt.command_id == claim.command_id
    assert receipt.tenant_id == claim.tenant_id
    assert receipt.status == "dead_letter"
    assert factory.calls == 1
    assert len(factory.session.calls) == 4
    assert "grove_dead_letter_run_command" in str(factory.session.calls[-1][0])
    assert factory.session.calls[-1][1]["reason_ref"] == "worker-timeout"  # type: ignore[index]

    for result_code, error in (
        ("stale", StaleExecutionFence),
        ("expired", StaleExecutionFence),
        ("applied", RunStateConflict),
    ):
        driver, factory = _driver(_scope_results(_Result(row=_dead_letter_row(claim, result_code=result_code))))
        with pytest.raises(error):
            await driver.dead_letter(claim, "worker-timeout")
        assert factory.calls == 1
        assert len(factory.session.calls) == 4

    driver, _ = _driver(_scope_results(_Result(row=_dead_letter_row(claim, result_code="unexpected"))))
    with pytest.raises(RuntimeError, match="unknown dead-letter result"):
        await driver.dead_letter(claim, "worker-timeout")


@pytest.mark.asyncio
async def test_dead_letter_rejects_reason_before_database_access() -> None:
    class StringSubclass(str):
        pass

    claim = _claim()
    for reason in ("", "r" * 513, True, StringSubclass("reason")):
        driver, factory = _driver([])
        with pytest.raises(ValueError, match="reason_ref"):
            await driver.dead_letter(claim, reason)  # type: ignore[arg-type]
        assert factory.calls == 0
        assert factory.session.calls == []


@pytest.mark.asyncio
async def test_reconcile_expired_requires_an_explicit_projection_session_factory() -> None:
    driver, runtime_factory = _driver([])
    with pytest.raises(ExecutionDriverError, match="reconciliation_session_factory"):
        await driver.reconcile_expired("tenant-a", uuid4())
    assert runtime_factory.calls == 0
    assert runtime_factory.session.calls == []


@pytest.mark.asyncio
async def test_reconcile_expired_uses_projection_factory_and_maps_outcomes() -> None:
    claim = _claim()
    driver, runtime_factory = _driver([])
    projection_factory = _Factory(_scope_results(_Result(row=_reconciled_row(claim, result_code="consumed"))))
    driver._reconciliation_session_factory = projection_factory  # type: ignore[assignment]
    receipt = await driver.reconcile_expired(claim.tenant_id, claim.run_id)
    assert receipt is not None
    assert receipt.command_id == claim.command_id
    assert receipt.status == "consumed"
    assert runtime_factory.calls == 0
    assert projection_factory.calls == 1
    assert len(projection_factory.session.calls) == 4
    assert "grove_reconcile_expired_run_command" in str(projection_factory.session.calls[-1][0])

    driver, runtime_factory = _driver([])
    projection_factory = _Factory(_scope_results(_Result(row=_reconciled_row(claim, result_code="requeued"))))
    driver._reconciliation_session_factory = projection_factory  # type: ignore[assignment]
    receipt = await driver.reconcile_expired(claim.tenant_id, claim.run_id)
    assert receipt is not None
    assert receipt.status == "pending"
    assert runtime_factory.calls == 0

    driver, runtime_factory = _driver([])
    projection_factory = _Factory(_scope_results(_Result(row={"result_code": "noop"})))
    driver._reconciliation_session_factory = projection_factory  # type: ignore[assignment]
    assert await driver.reconcile_expired(claim.tenant_id, claim.run_id) is None
    assert runtime_factory.calls == 0


@pytest.mark.asyncio
async def test_reconcile_expired_rejects_invalid_identity_before_database_access() -> None:
    driver, runtime_factory = _driver([])
    projection_factory = _Factory([])
    driver._reconciliation_session_factory = projection_factory  # type: ignore[assignment]
    for tenant_id, run_id in (("", uuid4()), (True, uuid4()), ("tenant-a", str(uuid4()))):
        with pytest.raises((ValueError, ExecutionDriverError)):
            await driver.reconcile_expired(tenant_id, run_id)  # type: ignore[arg-type]
    assert runtime_factory.calls == 0
    assert projection_factory.calls == 0


@pytest.mark.asyncio
async def test_consume_maps_authoritative_receipt_and_business_failures() -> None:
    claim = _claim()
    driver, factory = _driver(_scope_results(_Result(row=_consumed_row(claim))))
    receipt = await driver.consume(claim)
    assert receipt.command_id == claim.command_id
    assert receipt.tenant_id == claim.tenant_id
    assert receipt.status == "consumed"
    assert factory.calls == 1
    assert len(factory.session.calls) == 4

    for result_code, error in (("stale", StaleExecutionFence), ("no_proof", RunStateConflict)):
        driver, factory = _driver(_scope_results(_Result(row=_consumed_row(claim, result_code=result_code))))
        with pytest.raises(error):
            await driver.consume(claim)
        assert factory.calls == 1
        assert len(factory.session.calls) == 4

    driver, _ = _driver(_scope_results(_Result(row=_consumed_row(claim, result_code="unexpected"))))
    with pytest.raises(RuntimeError, match="unknown consume result"):
        await driver.consume(claim)


@pytest.mark.asyncio
async def test_consume_rejects_invalid_claim_before_database_access() -> None:
    class ClaimSubclass(ExecutionClaim):
        pass

    claim = _claim()
    driver, factory = _driver([])
    with pytest.raises(ExecutionDriverError, match="exact ExecutionClaim"):
        await driver.consume(ClaimSubclass.model_validate(claim.model_dump(mode="python")))
    assert factory.calls == 0
    assert factory.session.calls == []
