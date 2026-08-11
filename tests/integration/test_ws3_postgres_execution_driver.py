from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypedDict, cast

import psycopg
import pytest
from app.auth.context import _context_from_token
from app.build.manifest import WS3_SCHEMA_CONTRACT
from app.contracts.canonical import canonical_hash
from app.execution import (
    BIGINT_MAX,
    CancelRun,
    CommandConflict,
    ExecutionDriverError,
    ExecutionFenceExhausted,
    FencedPostgresSaver,
    PostgresExecutionDriver,
    RunStateConflict,
    StaleExecutionFence,
    VersionUnavailable,
)
from app.execution.contracts import RunNotFound
from app.schemas.execution import SubmitExecution
from app.services.execution import query_run, submit
from app.skill_abi.models import SkillExecutionSpec
from app.skill_abi.runtime import compute_evaluation_subject_hash, compute_skill_spec_hash
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import CheckpointMetadata, empty_checkpoint
from langgraph.checkpoint.serde.types import _DeltaSnapshot
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel
from scripts import ws3_preflight
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _role_url(api_url: str, role: str, password: str) -> str:
    return api_url.replace("grove_api:grove_api_ws0", f"{role}:{password}", 1)


def _request(submission_id: uuid.UUID) -> SubmitExecution:
    return SubmitExecution.model_validate(
        {
            "submission_id": str(submission_id),
            "intent": {
                "intent_id": str(uuid.uuid4()),
                "skill_ref": "fixture.skill@1",
                "input": {"question": "claim this durable command"},
                "constraints": {},
            },
        }
    )


async def _submit_start(
    api_url: str,
    migration_url: str,
    tenant_id: str,
    *,
    seed_identity: bool = True,
) -> tuple[Any, str]:
    owner = create_async_engine(migration_url)
    if seed_identity:
        async with owner.begin() as connection:
            await connection.execute(text("INSERT INTO tenant (tenant_id) VALUES (:tenant)"), {"tenant": tenant_id})
            await connection.execute(
                text(
                    "INSERT INTO membership (tenant_id, principal_id, user_ref, roles) "
                    "VALUES (:tenant, 'human', 'human', '[\"execution.submit\", \"execution.query\"]'::jsonb)"
                ),
                {"tenant": tenant_id},
            )

    engine = create_async_engine(api_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            context = _context_from_token(f"fixture:{tenant_id}:human")
            handle = await submit(session, context, _request(uuid.uuid4()))
        async with factory() as session, session.begin():
            query = await query_run(session, context, handle.run_id)
            assert query.run == handle
        async with owner.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT r.runtime_build_hash, s.spec_payload #>> '{runtime_build,content_hash}' "
                        "FROM agent_run r JOIN execution_spec s USING (tenant_id, skill_spec_hash) "
                        "WHERE r.run_id = :run_id"
                    ),
                    {"run_id": handle.run_id},
                )
            ).one()
        assert row[0] == row[1]
        return handle, str(row[0])
    finally:
        await engine.dispose()
        await owner.dispose()


async def _insert_runtime_build_variant(
    migration_url: str,
    *,
    tenant_id: str,
    source_run_id: uuid.UUID,
    runtime_build_hash: str,
) -> tuple[Any, str]:
    """Create a run bound to a different build at insertion time.

    Runtime build bindings are intentionally not mutated after insertion.  The
    fixture therefore copies the source spec and command payload while making
    a new content-addressed spec/run identity.
    """

    owner = create_async_engine(migration_url)
    variant_run_id = uuid.uuid4()
    try:
        async with owner.begin() as connection:
            source = (
                await connection.execute(
                    text(
                        "SELECT r.principal_id, r.principal_kind, s.spec_payload, "
                        "c.payload_ref, c.payload_hash "
                        "FROM agent_run AS r "
                        "JOIN execution_spec AS s "
                        "  ON s.tenant_id = r.tenant_id AND s.skill_spec_hash = r.skill_spec_hash "
                        "JOIN run_command AS c "
                        "  ON c.tenant_id = r.tenant_id AND c.run_id = r.run_id AND c.command_seq = 0 "
                        "WHERE r.tenant_id = :tenant AND r.run_id = :run_id"
                    ),
                    {"tenant": tenant_id, "run_id": source_run_id},
                )
            ).one()
            spec = SkillExecutionSpec.model_validate(source[2])
            variant_build = spec.runtime_build.model_copy(update={"content_hash": runtime_build_hash})
            variant = spec.model_copy(update={"runtime_build": variant_build})
            variant = variant.model_copy(update={"evaluation_subject_hash": compute_evaluation_subject_hash(variant)})
            variant = variant.model_copy(update={"skill_spec_hash": compute_skill_spec_hash(variant)})
            variant_payload = variant.model_dump(mode="json")
            variant_spec_ref = f"execution-spec:{variant.skill_spec_hash}"
            await connection.execute(
                text(
                    "INSERT INTO execution_spec (tenant_id, skill_spec_hash, spec_ref, spec_payload) "
                    "VALUES (:tenant, :skill_spec_hash, :spec_ref, CAST(:spec_payload AS jsonb))"
                ),
                {
                    "tenant": tenant_id,
                    "skill_spec_hash": variant.skill_spec_hash,
                    "spec_ref": variant_spec_ref,
                    "spec_payload": json.dumps(variant_payload, sort_keys=True, separators=(",", ":")),
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO agent_run (run_id, tenant_id, submission_id, submission_digest, "
                    "principal_id, principal_kind, skill_spec_hash, skill_spec_ref, runtime_build_ref, "
                    "runtime_build_hash, status, revision) VALUES "
                    "(:run_id, :tenant, :submission_id, :submission_digest, :principal_id, :principal_kind, "
                    ":skill_spec_hash, :skill_spec_ref, :runtime_build_ref, :runtime_build_hash, 'accepted', 0)"
                ),
                {
                    "run_id": variant_run_id,
                    "tenant": tenant_id,
                    "submission_id": uuid.uuid4(),
                    "submission_digest": "a" * 64,
                    "principal_id": source[0],
                    "principal_kind": source[1],
                    "skill_spec_hash": variant.skill_spec_hash,
                    "skill_spec_ref": variant_spec_ref,
                    "runtime_build_ref": variant.runtime_build.ref,
                    "runtime_build_hash": runtime_build_hash,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO run_command (command_id, tenant_id, run_id, principal_id, principal_kind, "
                    "command_seq, command_type, command_schema_version, command_digest, payload_ref, "
                    "payload_hash, status) VALUES "
                    "(:command_id, :tenant, :run_id, :principal_id, :principal_kind, 0, 'start', 'start.v1', "
                    ":command_digest, :payload_ref, :payload_hash, 'pending')"
                ),
                {
                    "command_id": uuid.uuid4(),
                    "tenant": tenant_id,
                    "run_id": variant_run_id,
                    "principal_id": source[0],
                    "principal_kind": source[1],
                    "command_digest": "b" * 64,
                    "payload_ref": source[3],
                    "payload_hash": source[4],
                },
            )
        return SimpleNamespace(run_id=variant_run_id), runtime_build_hash
    finally:
        await owner.dispose()


async def _lease_state(migration_url: str, run_id: uuid.UUID) -> tuple[Any, ...]:
    engine = create_async_engine(migration_url)
    try:
        async with engine.connect() as connection:
            return tuple(
                (
                    await connection.execute(
                        text(
                            "SELECT r.execution_fence, r.lease_owner, r.lease_until, "
                            "c.command_id, c.execution_fence, c.lease_owner, c.lease_until, c.attempt_count "
                            "FROM agent_run r JOIN run_command c USING (tenant_id, run_id) "
                            "WHERE r.run_id = :run_id ORDER BY c.command_seq"
                        ),
                        {"run_id": run_id},
                    )
                ).all()
            )
    finally:
        await engine.dispose()


async def _claim_authority_state(migration_url: str, run_id: uuid.UUID) -> tuple[Any, ...]:
    """Return every mutable run/command lease field for zero-write probes."""

    engine = create_async_engine(migration_url)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT r.status, r.revision, r.execution_fence, r.lease_owner, r.lease_until, "
                        "c.command_id, c.status, c.command_seq, c.command_digest, c.command_type, "
                        "c.command_schema_version, c.available_at, c.lease_owner, c.lease_until, "
                        "c.execution_fence, c.attempt_count, c.superseded_by_command_id, "
                        "c.superseded_by_command_seq, c.superseded_by_command_digest, "
                        "c.superseded_by_provenance_hash "
                        "FROM agent_run AS r JOIN run_command AS c USING (tenant_id, run_id) "
                        "WHERE r.run_id = :run_id ORDER BY c.command_seq"
                    ),
                    {"run_id": run_id},
                )
            ).all()
            return tuple(tuple(item) for item in row)
    finally:
        await engine.dispose()


async def _checkpoint_counts(migration_url: str, tenant_id: str) -> tuple[int, int, int]:
    engine = create_async_engine(migration_url)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT "
                        "(SELECT count(*) FROM checkpoints WHERE tenant_id = :tenant), "
                        "(SELECT count(*) FROM checkpoint_blobs WHERE tenant_id = :tenant), "
                        "(SELECT count(*) FROM checkpoint_writes WHERE tenant_id = :tenant)"
                    ),
                    {"tenant": tenant_id},
                )
            ).one()
            return (int(row[0]), int(row[1]), int(row[2]))
    finally:
        await engine.dispose()


async def _command_payload_counts(migration_url: str, tenant_id: str, run_id: uuid.UUID) -> tuple[int, int]:
    engine = create_async_engine(migration_url)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT "
                        "(SELECT count(*) FROM run_command WHERE tenant_id = :tenant AND run_id = :run_id), "
                        "(SELECT count(*) FROM command_payload WHERE tenant_id = :tenant)"
                    ),
                    {"tenant": tenant_id, "run_id": run_id},
                )
            ).one()
            return (int(row[0]), int(row[1]))
    finally:
        await engine.dispose()


async def _reconcile_snapshot(
    migration_url: str, tenant_id: str, run_id: uuid.UUID
) -> tuple[tuple[Any, ...], tuple[tuple[Any, ...], ...], int]:
    engine = create_async_engine(migration_url)
    try:
        async with engine.connect() as connection:
            run_row = (
                await connection.execute(
                    text(
                        "SELECT status, revision, execution_fence, lease_owner, lease_until, "
                        "latest_checkpoint_id, latest_applied_command_id, latest_applied_command_seq, "
                        "latest_applied_command_digest FROM agent_run "
                        "WHERE tenant_id = :tenant AND run_id = :run_id"
                    ),
                    {"tenant": tenant_id, "run_id": run_id},
                )
            ).one()
            command_rows = (
                await connection.execute(
                    text(
                        "SELECT command_id, command_seq, status, available_at, lease_owner, lease_until, "
                        "execution_fence, attempt_count, last_error_ref, superseded_by_command_id "
                        "FROM run_command WHERE tenant_id = :tenant AND run_id = :run_id "
                        "ORDER BY command_seq"
                    ),
                    {"tenant": tenant_id, "run_id": run_id},
                )
            ).all()
            checkpoint_count = await connection.scalar(
                text("SELECT count(*) FROM checkpoints WHERE tenant_id = :tenant AND thread_id = :thread_id"),
                {"tenant": tenant_id, "thread_id": str(run_id)},
            )
            return tuple(run_row), tuple(tuple(row) for row in command_rows), int(checkpoint_count or 0)
    finally:
        await engine.dispose()


async def _expire_claim(migration_url: str, claim: Any) -> datetime:
    """Set both durable lease columns to one exact past instant for a bounded test."""

    past = datetime.now(UTC) - timedelta(seconds=1)
    engine = create_async_engine(migration_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE agent_run SET lease_until = :past WHERE tenant_id = :tenant AND run_id = :run_id"),
                {"past": past, "tenant": claim.tenant_id, "run_id": claim.run_id},
            )
            await connection.execute(
                text(
                    "UPDATE run_command SET lease_until = :past WHERE tenant_id = :tenant AND command_id = :command_id"
                ),
                {"past": past, "tenant": claim.tenant_id, "command_id": claim.command_id},
            )
    finally:
        await engine.dispose()
    return past


async def _wait_for_backend_lock(migration_url: str, pid: int, *, deadline_seconds: float = 3.0) -> None:
    """Observe a real PostgreSQL lock wait before releasing the held row."""

    engine = create_async_engine(migration_url)
    deadline = asyncio.get_running_loop().time() + deadline_seconds
    try:
        async with engine.connect() as connection:
            while asyncio.get_running_loop().time() < deadline:
                row = (
                    await connection.execute(
                        text("SELECT state, wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
                        {"pid": pid},
                    )
                ).one_or_none()
                await connection.commit()
                if row is not None and row[0] == "active" and row[1] == "Lock":
                    return
                await asyncio.sleep(0.01)
    finally:
        await engine.dispose()
    raise AssertionError(f"backend pid {pid} did not reach a PostgreSQL lock wait")


async def _wait_for_database_clock(migration_url: str, target: datetime, *, deadline_seconds: float = 3.0) -> datetime:
    """Wait on PostgreSQL clock time, not a client sleep, under a fixed deadline."""

    engine = create_async_engine(migration_url)
    deadline = asyncio.get_running_loop().time() + deadline_seconds
    try:
        async with engine.connect() as connection:
            while asyncio.get_running_loop().time() < deadline:
                current = await connection.scalar(text("SELECT clock_timestamp()"))
                await connection.commit()
                if isinstance(current, datetime) and current >= target:
                    return current
                await asyncio.sleep(0.01)
    finally:
        await engine.dispose()
    raise AssertionError(f"database clock did not reach {target.isoformat()} within deadline")


async def _wait_for_advisory_xact_lock(raw_url: str, advisory_key: int, *, deadline_seconds: float = 3.0) -> None:
    """Observe a trigger-owned advisory xact lock without relying on sleeps."""

    connection = await psycopg.AsyncConnection.connect(raw_url)
    deadline = asyncio.get_running_loop().time() + deadline_seconds
    try:
        while asyncio.get_running_loop().time() < deadline:
            row = await (await connection.execute("SELECT pg_try_advisory_lock(%s)", (advisory_key,))).fetchone()
            await connection.commit()
            if row is not None and row[0] is False:
                return
            if row is not None and row[0] is True:
                await connection.execute("SELECT pg_advisory_unlock(%s)", (advisory_key,))
                await connection.commit()
            await asyncio.sleep(0.01)
    finally:
        await connection.close()
    raise AssertionError(f"advisory lock {advisory_key} was not held within deadline")


async def _raw_scoped_transaction(raw_url: str, tenant_id: str) -> psycopg.AsyncConnection:
    """Open a bounded raw transaction for deterministic lock-order probes."""

    connection = await psycopg.AsyncConnection.connect(raw_url)
    await connection.execute("BEGIN")
    await connection.execute("SET LOCAL lock_timeout = '5000ms'")
    await connection.execute("SET LOCAL statement_timeout = '5000ms'")
    await connection.execute("SELECT set_config(%s, %s, true)", ("grove.tenant_id", tenant_id))
    return connection


async def _raw_call(connection: psycopg.AsyncConnection, sql: str, params: tuple[Any, ...]) -> tuple[Any, ...] | None:
    """Run one database authority and commit or roll back its whole transaction."""

    try:
        cursor = await connection.execute(sql, params)
        row = await cursor.fetchone()
        await connection.commit()
        return tuple(row) if row is not None else None
    except BaseException:
        await connection.rollback()
        raise


async def _raw_reconcile(projection_raw_url: str, tenant_id: str, run_id: uuid.UUID) -> tuple[Any, ...] | None:
    connection = await _raw_scoped_transaction(projection_raw_url, tenant_id)
    try:
        return await _raw_call(
            connection,
            "SELECT * FROM grove_reconcile_expired_run_command(%s, %s)",
            (tenant_id, run_id),
        )
    finally:
        await connection.close()


async def _checkpoint_lock_order_write(connection: psycopg.AsyncConnection, claim: Any) -> tuple[Any, ...]:
    """Write one physical checkpoint through the production fenced saver."""

    checkpoint = empty_checkpoint()
    checkpoint["channel_versions"] = {"state": "lock-order-v1"}
    checkpoint["channel_values"] = {"state": {"winner": "checkpoint"}}
    config = cast(
        RunnableConfig,
        {"configurable": {"thread_id": str(claim.run_id), "checkpoint_ns": ""}},
    )
    saver = FencedPostgresSaver(connection, claim)
    result = await saver.aput(config, checkpoint, cast(CheckpointMetadata, {}), {"state": "lock-order-v1"})
    return ("checkpoint", result["configurable"]["checkpoint_id"])


def _dead_letter_args(claim: Any, reason: str) -> tuple[Any, ...]:
    return (
        claim.tenant_id,
        claim.run_id,
        claim.command_id,
        claim.command_seq,
        claim.command_digest,
        claim.runtime_build_hash,
        claim.worker_id,
        claim.execution_fence,
        claim.lease_until,
        reason,
    )


def _consume_args(claim: Any) -> tuple[Any, ...]:
    return (
        claim.tenant_id,
        claim.run_id,
        claim.command_id,
        claim.command_seq,
        claim.command_digest,
        claim.runtime_build_hash,
        claim.worker_id,
        claim.execution_fence,
        claim.lease_until,
    )


async def _insert_followup_command(
    migration_url: str,
    *,
    tenant_id: str,
    run_id: uuid.UUID,
    command_id: uuid.UUID,
    command_seq: int,
    command_digest: str,
    command_type: str = "start",
    command_schema_version: str = "start.v1",
) -> None:
    """Seed one later-sequence command through the migration owner for fence tests."""

    engine = create_async_engine(migration_url)
    try:
        async with engine.begin() as connection:
            source = (
                await connection.execute(
                    text(
                        "SELECT principal_id, principal_kind, payload_ref, payload_hash "
                        "FROM run_command WHERE tenant_id = :tenant AND run_id = :run_id AND command_seq = 0"
                    ),
                    {"tenant": tenant_id, "run_id": run_id},
                )
            ).one()
            if command_schema_version != "start.v1":
                payload_ref = f"fixture-{command_schema_version}-{command_id}"
                payload_hash = "4" * 64
                await connection.execute(
                    text(
                        "INSERT INTO command_payload ("
                        "tenant_id, payload_ref, payload_hash, command_schema_version, sensitivity, retention, payload"
                        ") VALUES (:tenant, :payload_ref, :payload_hash, :schema, 'sensitive', "
                        "'run_completion', '{}'::jsonb)"
                    ),
                    {
                        "tenant": tenant_id,
                        "payload_ref": payload_ref,
                        "payload_hash": payload_hash,
                        "schema": command_schema_version,
                    },
                )
            else:
                payload_ref = source[2]
                payload_hash = source[3]
            await connection.execute(
                text(
                    "INSERT INTO run_command ("
                    "command_id, tenant_id, run_id, principal_id, principal_kind, command_seq, "
                    "command_type, command_schema_version, command_digest, payload_ref, payload_hash, status"
                    ") VALUES ("
                    ":command_id, :tenant, :run_id, :principal_id, :principal_kind, :command_seq, "
                    ":command_type, :command_schema_version, :command_digest, :payload_ref, :payload_hash, 'pending'"
                    ")"
                ),
                {
                    "command_id": command_id,
                    "tenant": tenant_id,
                    "run_id": run_id,
                    "principal_id": source[0],
                    "principal_kind": source[1],
                    "command_seq": command_seq,
                    "command_digest": command_digest,
                    "command_type": command_type,
                    "command_schema_version": command_schema_version,
                    "payload_ref": payload_ref,
                    "payload_hash": payload_hash,
                },
            )
    finally:
        await engine.dispose()


def _cancel_command(
    *,
    tenant_id: str,
    run_id: uuid.UUID,
    runtime_build_hash: str,
    command_id: uuid.UUID | None = None,
    reason_ref: str | None = None,
    reason_hash: str | None = None,
) -> CancelRun:
    return CancelRun(
        command_id=command_id or uuid.uuid4(),
        tenant_id=tenant_id,
        run_id=run_id,
        runtime_build_hash=runtime_build_hash,
        command_digest="c" * 64,
        command_type="cancel",
        command_schema_version="cancel.v1",
        expected_revision=0,
        reason_ref=reason_ref,
        reason_hash=reason_hash,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancel_acceptance_revokes_active_claim_and_cancel_wins_claim() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-cancel-active-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    api_engine = create_async_engine(api_url)
    owner_engine = create_async_engine(migration_url)
    runtime_driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
    api_driver = PostgresExecutionDriver(
        async_sessionmaker(runtime_engine, expire_on_commit=False),
        command_session_factory=async_sessionmaker(api_engine, expire_on_commit=False),
    )
    try:
        old_claim = await runtime_driver.claim("cancel-old-worker", runtime_hash, tenant_id, 30)
        assert old_claim is not None and old_claim.command_seq == 0
        cancel = _cancel_command(tenant_id=tenant_id, run_id=handle.run_id, runtime_build_hash=runtime_hash)
        receipt = await api_driver.dispatch(cancel)
        assert receipt.command_id == cancel.command_id
        assert receipt.command_seq == 1
        assert receipt.status == "pending"

        async with owner_engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT r.status, r.revision, r.execution_fence, r.lease_owner, r.lease_until, "
                        "c.command_seq, c.command_type, c.status, c.lease_owner, c.lease_until, "
                        "c.superseded_by_command_id, c.superseded_by_command_seq, c.superseded_by_command_digest "
                        "FROM agent_run r JOIN run_command c USING (tenant_id, run_id) "
                        "WHERE r.run_id = :run_id ORDER BY c.command_seq"
                    ),
                    {"run_id": handle.run_id},
                )
            ).all()
        assert rows[0][0:5] == ("cancel_requested", 1, 2, None, None)
        assert rows[0][5:10] == (0, "start", "pending", None, None)
        assert rows[0][10:] == (cancel.command_id, 1, cancel.command_digest)
        assert rows[1][5:10] == (1, "cancel", "pending", None, None)

        before = await _lease_state(migration_url, handle.run_id)
        with pytest.raises(StaleExecutionFence):
            await runtime_driver.heartbeat(old_claim, 20)
        with pytest.raises(StaleExecutionFence):
            await runtime_driver.consume(old_claim)
        checkpoint_counts_before = await _checkpoint_counts(migration_url, tenant_id)
        raw_url = runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
        stale_checkpoint = empty_checkpoint()
        stale_checkpoint["id"] = "cancel-stale-checkpoint"
        stale_checkpoint["channel_versions"] = {"state": "stale"}
        with pytest.raises((psycopg.errors.SerializationFailure, psycopg.errors.CheckViolation)):
            async with await psycopg.AsyncConnection.connect(raw_url) as raw_connection:
                saver = FencedPostgresSaver(raw_connection, old_claim)
                await saver.aput(
                    {"configurable": {"thread_id": str(old_claim.run_id), "checkpoint_ns": ""}},
                    stale_checkpoint,
                    cast(CheckpointMetadata, {}),
                    {"state": "stale"},
                )
        assert await _lease_state(migration_url, handle.run_id) == before
        assert await _checkpoint_counts(migration_url, tenant_id) == checkpoint_counts_before

        retry = await api_driver.dispatch(cancel)
        assert retry == receipt
        new_claim = await runtime_driver.claim("cancel-new-worker", runtime_hash, tenant_id, 10)
        assert new_claim is not None
        assert new_claim.command_id == cancel.command_id
        assert new_claim.command_seq == 1
        assert new_claim.execution_fence == 3
    finally:
        await runtime_engine.dispose()
        await api_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancel_payload_wrapper_is_api_owned_and_role_tenant_bound() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-cancel-payload-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    mismatch_tenant = f"it-ws3-cancel-mismatch-{uuid.uuid4().hex[:12]}"
    await _submit_start(api_url, migration_url, mismatch_tenant)
    runtime_engine = create_async_engine(runtime_url)
    api_engine = create_async_engine(api_url)
    owner_engine = create_async_engine(migration_url)
    api_driver = PostgresExecutionDriver(
        async_sessionmaker(runtime_engine, expire_on_commit=False),
        command_session_factory=async_sessionmaker(api_engine, expire_on_commit=False),
    )
    reason_ref = "r" * 512
    reason_hash = "a" * 64
    cancel = _cancel_command(
        tenant_id=tenant_id,
        run_id=handle.run_id,
        runtime_build_hash=runtime_hash,
        reason_ref=reason_ref,
        reason_hash=reason_hash,
    )
    payload_hash = canonical_hash({"reason_ref": reason_ref, "reason_hash": reason_hash})
    payload_ref = f"command-payload:{payload_hash}"
    try:
        receipt = await api_driver.dispatch(cancel)
        assert receipt.command_id == cancel.command_id
        async with owner_engine.connect() as connection:
            payload = (
                await connection.execute(
                    text(
                        "SELECT payload_ref, payload_hash, command_schema_version, payload::text "
                        "FROM command_payload WHERE tenant_id = :tenant AND payload_ref = :payload_ref"
                    ),
                    {"tenant": tenant_id, "payload_ref": payload_ref},
                )
            ).one()
            command_binding = (
                await connection.execute(
                    text("SELECT payload_ref, payload_hash FROM run_command WHERE command_id = :command_id"),
                    {"command_id": cancel.command_id},
                )
            ).one()
        assert payload[0:3] == (payload_ref, payload_hash, "cancel.v1")
        assert json.loads(payload[3]) == {"reason_ref": reason_ref, "reason_hash": reason_hash}
        assert command_binding == (payload_ref, payload_hash)
        assert payload_ref != reason_ref
        assert await api_driver.dispatch(cancel) == receipt

        conflict = cancel.model_copy(update={"reason_ref": "s" * 512})
        with pytest.raises(CommandConflict):
            await api_driver.dispatch(conflict)
        conflict_hash = canonical_hash({"reason_ref": "s" * 512, "reason_hash": reason_hash})
        async with owner_engine.connect() as connection:
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM command_payload "
                        "WHERE tenant_id = :tenant AND payload_hash = :payload_hash"
                    ),
                    {"tenant": tenant_id, "payload_hash": conflict_hash},
                )
                == 0
            )

        runtime_raw_url = runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            async with await psycopg.AsyncConnection.connect(runtime_raw_url) as runtime_connection:
                await runtime_connection.execute("SELECT set_config('grove.tenant_id', %s, true)", (tenant_id,))
                await runtime_connection.execute(
                    "SELECT * FROM grove_accept_cancel_run(%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
                    (
                        tenant_id,
                        handle.run_id,
                        uuid.uuid4(),
                        1,
                        "e" * 64,
                        runtime_hash,
                        payload_ref,
                        payload_hash,
                        "{}",
                    ),
                )

        mismatch = _cancel_command(
            tenant_id=mismatch_tenant,
            run_id=handle.run_id,
            runtime_build_hash=runtime_hash,
        )
        with pytest.raises(RunNotFound):
            await api_driver.dispatch(mismatch)
        mismatch_hash = canonical_hash({})
        async with owner_engine.connect() as connection:
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM command_payload "
                        "WHERE tenant_id = :tenant AND payload_hash = :payload_hash"
                    ),
                    {"tenant": mismatch_tenant, "payload_hash": mismatch_hash},
                )
                == 0
            )
    finally:
        await runtime_engine.dispose()
        await api_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancel_rejects_poisoned_existing_payload_body_without_run_write() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-cancel-poison-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    reason_ref = "poison-reason"
    reason_hash = "a" * 64
    expected_payload = {"reason_ref": reason_ref, "reason_hash": reason_hash}
    payload_hash = canonical_hash(expected_payload)
    payload_ref = f"command-payload:{payload_hash}"
    owner_engine = create_async_engine(migration_url)
    api_engine = create_async_engine(api_url)
    runtime_engine = create_async_engine(runtime_url)
    api_driver = PostgresExecutionDriver(
        async_sessionmaker(runtime_engine, expire_on_commit=False),
        command_session_factory=async_sessionmaker(api_engine, expire_on_commit=False),
    )
    try:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO command_payload ("
                    "tenant_id, payload_ref, payload_hash, command_schema_version, sensitivity, retention, payload"
                    ") VALUES (:tenant, :ref, :hash, 'cancel.v1', 'sensitive', "
                    "'run_completion', CAST(:payload AS jsonb))"
                ),
                {
                    "tenant": tenant_id,
                    "ref": payload_ref,
                    "hash": payload_hash,
                    "payload": json.dumps({"reason_ref": "poisoned", "reason_hash": reason_hash}),
                },
            )
        before = await _lease_state(migration_url, handle.run_id)
        cancel = _cancel_command(
            tenant_id=tenant_id,
            run_id=handle.run_id,
            runtime_build_hash=runtime_hash,
            reason_ref=reason_ref,
            reason_hash=reason_hash,
        )
        with pytest.raises(RunStateConflict, match="payload"):
            await api_driver.dispatch(cancel)
        assert await _lease_state(migration_url, handle.run_id) == before
        async with owner_engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM run_command WHERE command_id = :command_id"),
                    {"command_id": cancel.command_id},
                )
                == 0
            )
            poisoned = await connection.scalar(
                text(
                    "SELECT payload->>'reason_ref' FROM command_payload "
                    "WHERE tenant_id = :tenant AND payload_ref = :ref"
                ),
                {"tenant": tenant_id, "ref": payload_ref},
            )
        assert poisoned == "poisoned"
    finally:
        await runtime_engine.dispose()
        await api_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancel_command_id_global_conflicts_are_stable_and_do_not_leak_rows() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-cancel-id-a-{uuid.uuid4().hex[:12]}"
    other_tenant_id = f"it-ws3-cancel-id-b-{uuid.uuid4().hex[:12]}"
    first_handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    same_tenant_handle, same_runtime_hash = await _submit_start(api_url, migration_url, tenant_id, seed_identity=False)
    other_handle, other_runtime_hash = await _submit_start(api_url, migration_url, other_tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    api_engine = create_async_engine(api_url)
    owner_engine = create_async_engine(migration_url)
    driver = PostgresExecutionDriver(
        async_sessionmaker(runtime_engine, expire_on_commit=False),
        command_session_factory=async_sessionmaker(api_engine, expire_on_commit=False),
    )
    command_id = uuid.uuid4()
    first = _cancel_command(
        tenant_id=tenant_id,
        run_id=first_handle.run_id,
        runtime_build_hash=runtime_hash,
        command_id=command_id,
    )
    second = _cancel_command(
        tenant_id=tenant_id,
        run_id=same_tenant_handle.run_id,
        runtime_build_hash=same_runtime_hash,
        command_id=command_id,
    )
    cross_tenant = _cancel_command(
        tenant_id=other_tenant_id,
        run_id=other_handle.run_id,
        runtime_build_hash=other_runtime_hash,
        command_id=command_id,
    )
    try:
        await driver.dispatch(first)
        before_same_run = await _lease_state(migration_url, same_tenant_handle.run_id)
        with pytest.raises(CommandConflict):
            await driver.dispatch(second)
        assert await _lease_state(migration_url, same_tenant_handle.run_id) == before_same_run
        with pytest.raises(CommandConflict):
            await driver.dispatch(cross_tenant)
        assert await _lease_state(migration_url, same_tenant_handle.run_id) == before_same_run
        async with owner_engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM command_payload WHERE tenant_id = :tenant AND payload_hash = :hash"),
                    {"tenant": tenant_id, "hash": canonical_hash({})},
                )
                == 1
            )
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM command_payload WHERE tenant_id = :tenant AND payload_hash = :hash"),
                    {"tenant": other_tenant_id, "hash": canonical_hash({})},
                )
                == 0
            )
    finally:
        await runtime_engine.dispose()
        await api_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancel_cross_tenant_command_id_race_has_one_domain_winner() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_a = f"it-ws3-cancel-race-a-{uuid.uuid4().hex[:12]}"
    tenant_b = f"it-ws3-cancel-race-b-{uuid.uuid4().hex[:12]}"
    handle_a, hash_a = await _submit_start(api_url, migration_url, tenant_a)
    handle_b, hash_b = await _submit_start(api_url, migration_url, tenant_b)
    runtime_engine = create_async_engine(runtime_url)
    api_engine_a = create_async_engine(api_url)
    api_engine_b = create_async_engine(api_url)
    command_id = uuid.uuid4()
    driver_a = PostgresExecutionDriver(
        async_sessionmaker(runtime_engine, expire_on_commit=False),
        command_session_factory=async_sessionmaker(api_engine_a, expire_on_commit=False),
    )
    driver_b = PostgresExecutionDriver(
        async_sessionmaker(runtime_engine, expire_on_commit=False),
        command_session_factory=async_sessionmaker(api_engine_b, expire_on_commit=False),
    )
    try:
        outcomes = await asyncio.gather(
            driver_a.dispatch(
                _cancel_command(
                    tenant_id=tenant_a,
                    run_id=handle_a.run_id,
                    runtime_build_hash=hash_a,
                    command_id=command_id,
                )
            ),
            driver_b.dispatch(
                _cancel_command(
                    tenant_id=tenant_b,
                    run_id=handle_b.run_id,
                    runtime_build_hash=hash_b,
                    command_id=command_id,
                )
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(outcome, CommandConflict) for outcome in outcomes) == 1
        assert sum(not isinstance(outcome, BaseException) for outcome in outcomes) == 1
        assert all(
            not isinstance(outcome, BaseException) or isinstance(outcome, CommandConflict) for outcome in outcomes
        )
        owner_engine = create_async_engine(migration_url)
        try:
            async with owner_engine.connect() as connection:
                command_count = await connection.scalar(
                    text("SELECT count(*) FROM run_command WHERE command_id = :command_id"),
                    {"command_id": command_id},
                )
                payload_count = await connection.scalar(
                    text(
                        "SELECT count(*) FROM command_payload p "
                        "JOIN run_command c ON c.tenant_id = p.tenant_id "
                        "AND c.payload_ref = p.payload_ref AND c.payload_hash = p.payload_hash "
                        "WHERE c.command_id = :command_id"
                    ),
                    {"command_id": command_id},
                )
            assert command_count == 1
            assert payload_count == 1
        finally:
            await owner_engine.dispose()
    finally:
        await runtime_engine.dispose()
        await api_engine_a.dispose()
        await api_engine_b.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancel_and_claim_fence_boundaries_fail_closed_without_overflow_writes() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-cancel-idempotent-fence-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    api_engine = create_async_engine(api_url)
    owner_engine = create_async_engine(migration_url)
    runtime_driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
    api_driver = PostgresExecutionDriver(
        async_sessionmaker(runtime_engine, expire_on_commit=False),
        command_session_factory=async_sessionmaker(api_engine, expire_on_commit=False),
    )
    try:
        accepted_command = _cancel_command(
            tenant_id=tenant_id,
            run_id=handle.run_id,
            runtime_build_hash=runtime_hash,
        )
        accepted_receipt = await api_driver.dispatch(accepted_command)
        async with owner_engine.begin() as connection:
            await connection.execute(
                text("UPDATE agent_run SET execution_fence = :max_value WHERE run_id = :run_id"),
                {"max_value": BIGINT_MAX, "run_id": handle.run_id},
            )
        before_idempotent_retry = await _lease_state(migration_url, handle.run_id)
        before_idempotent_rows = await _command_payload_counts(migration_url, tenant_id, handle.run_id)
        assert await api_driver.dispatch(accepted_command) == accepted_receipt
        assert await _lease_state(migration_url, handle.run_id) == before_idempotent_retry
        assert await _command_payload_counts(migration_url, tenant_id, handle.run_id) == before_idempotent_rows

        with pytest.raises(CommandConflict):
            await api_driver.dispatch(accepted_command.model_copy(update={"command_digest": "e" * 64}))
        assert await _lease_state(migration_url, handle.run_id) == before_idempotent_retry
        assert await _command_payload_counts(migration_url, tenant_id, handle.run_id) == before_idempotent_rows
        with pytest.raises(CommandConflict):
            await api_driver.dispatch(
                accepted_command.model_copy(update={"reason_ref": "reason", "reason_hash": "f" * 64})
            )
        assert await _lease_state(migration_url, handle.run_id) == before_idempotent_retry
        assert await _command_payload_counts(migration_url, tenant_id, handle.run_id) == before_idempotent_rows

        boundary_tenant = f"it-ws3-cancel-boundary-{uuid.uuid4().hex[:12]}"
        boundary_handle, boundary_runtime_hash = await _submit_start(api_url, migration_url, boundary_tenant)
        overflow_command = _cancel_command(
            tenant_id=boundary_tenant,
            run_id=boundary_handle.run_id,
            runtime_build_hash=boundary_runtime_hash,
        ).model_construct(expected_revision=BIGINT_MAX)
        with pytest.raises(ExecutionDriverError) as overflow_error:
            await api_driver.dispatch(overflow_command)
        assert "expected_revision" in str(overflow_error.value) or "validation" in str(overflow_error.value)

        async with owner_engine.begin() as connection:
            await connection.execute(
                text("UPDATE agent_run SET revision = :max_value WHERE run_id = :run_id"),
                {"max_value": BIGINT_MAX, "run_id": boundary_handle.run_id},
            )
        before_revision_overflow = await _lease_state(migration_url, boundary_handle.run_id)
        with pytest.raises(ExecutionDriverError, match="revision"):
            await api_driver.dispatch(
                _cancel_command(
                    tenant_id=boundary_tenant,
                    run_id=boundary_handle.run_id,
                    runtime_build_hash=boundary_runtime_hash,
                ).model_copy(update={"expected_revision": BIGINT_MAX - 1})
            )
        assert await _lease_state(migration_url, boundary_handle.run_id) == before_revision_overflow

        async with owner_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE agent_run SET revision = 0, execution_fence = :max_value, status = 'accepted' "
                    "WHERE run_id = :run_id"
                ),
                {"max_value": BIGINT_MAX, "run_id": boundary_handle.run_id},
            )
        before_fence_overflow = await _lease_state(migration_url, boundary_handle.run_id)
        with pytest.raises(ExecutionFenceExhausted):
            await api_driver.dispatch(
                _cancel_command(
                    tenant_id=boundary_tenant,
                    run_id=boundary_handle.run_id,
                    runtime_build_hash=boundary_runtime_hash,
                )
            )
        assert await _lease_state(migration_url, boundary_handle.run_id) == before_fence_overflow
        with pytest.raises(ExecutionFenceExhausted):
            await runtime_driver.claim("boundary-worker", boundary_runtime_hash, boundary_tenant, 10)
        assert await _lease_state(migration_url, boundary_handle.run_id) == before_fence_overflow
    finally:
        await runtime_engine.dispose()
        await api_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_runtime_build_binding_is_immutable_and_cancel_retry_stays_bound() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-runtime-build-immutable-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    api_engine = create_async_engine(api_url)
    owner_engine = create_async_engine(migration_url)
    api_driver = PostgresExecutionDriver(
        async_sessionmaker(runtime_engine, expire_on_commit=False),
        command_session_factory=async_sessionmaker(api_engine, expire_on_commit=False),
    )
    try:
        accepted_command = _cancel_command(tenant_id=tenant_id, run_id=handle.run_id, runtime_build_hash=runtime_hash)
        accepted_receipt = await api_driver.dispatch(accepted_command)
        before = await _lease_state(migration_url, handle.run_id)
        before_rows = await _command_payload_counts(migration_url, tenant_id, handle.run_id)
        different_hash = "e" * 64 if runtime_hash != "e" * 64 else "f" * 64
        for column, value in (("runtime_build_ref", "forged-runtime-build"), ("runtime_build_hash", different_hash)):
            with pytest.raises(SQLAlchemyError):
                async with owner_engine.begin() as connection:
                    await connection.execute(
                        text(f"UPDATE agent_run SET {column} = :value WHERE run_id = :run_id"),  # noqa: S608
                        {"value": value, "run_id": handle.run_id},
                    )
        async with owner_engine.connect() as connection:
            binding = (
                await connection.execute(
                    text("SELECT runtime_build_ref, runtime_build_hash FROM agent_run WHERE run_id = :run_id"),
                    {"run_id": handle.run_id},
                )
            ).one()
        assert binding[1] == runtime_hash
        assert await _lease_state(migration_url, handle.run_id) == before

        assert await api_driver.dispatch(accepted_command) == accepted_receipt
        assert await _lease_state(migration_url, handle.run_id) == before
        assert await _command_payload_counts(migration_url, tenant_id, handle.run_id) == before_rows
        with pytest.raises(CommandConflict):
            await api_driver.dispatch(accepted_command.model_copy(update={"runtime_build_hash": different_hash}))
        assert await _lease_state(migration_url, handle.run_id) == before
        assert await _command_payload_counts(migration_url, tenant_id, handle.run_id) == before_rows
    finally:
        await runtime_engine.dispose()
        await api_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancel_acceptance_rejects_conflicts_without_writes_and_handles_waiting_state() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-cancel-conflict-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    api_engine = create_async_engine(api_url)
    owner_engine = create_async_engine(migration_url)
    api_driver = PostgresExecutionDriver(
        async_sessionmaker(runtime_engine, expire_on_commit=False),
        command_session_factory=async_sessionmaker(api_engine, expire_on_commit=False),
    )
    try:
        bad_build = _cancel_command(tenant_id=tenant_id, run_id=handle.run_id, runtime_build_hash="d" * 64)
        before = await _lease_state(migration_url, handle.run_id)
        with pytest.raises(VersionUnavailable):
            await api_driver.dispatch(bad_build)
        assert await _lease_state(migration_url, handle.run_id) == before
        long_reason = "l" * 512
        long_reason_hash = "f" * 64
        long_reason_cancel = _cancel_command(
            tenant_id=tenant_id,
            run_id=handle.run_id,
            runtime_build_hash="d" * 64,
            reason_ref=long_reason,
            reason_hash=long_reason_hash,
        )
        long_reason_payload_hash = canonical_hash({"reason_ref": long_reason, "reason_hash": long_reason_hash})
        with pytest.raises(VersionUnavailable):
            await api_driver.dispatch(long_reason_cancel)
        async with owner_engine.connect() as connection:
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM command_payload "
                        "WHERE tenant_id = :tenant AND payload_hash = :payload_hash"
                    ),
                    {"tenant": tenant_id, "payload_hash": long_reason_payload_hash},
                )
                == 0
            )

        bad_revision = _cancel_command(tenant_id=tenant_id, run_id=handle.run_id, runtime_build_hash=runtime_hash)
        bad_revision = bad_revision.model_copy(update={"expected_revision": 1})
        with pytest.raises(RunStateConflict):
            await api_driver.dispatch(bad_revision)
        assert await _lease_state(migration_url, handle.run_id) == before

        accepted = await api_driver.dispatch(
            _cancel_command(tenant_id=tenant_id, run_id=handle.run_id, runtime_build_hash=runtime_hash)
        )
        conflict = accepted.model_copy(update={"command_digest": "e" * 64})
        with pytest.raises(CommandConflict):
            await api_driver.dispatch(
                _cancel_command(
                    tenant_id=tenant_id,
                    run_id=handle.run_id,
                    runtime_build_hash=runtime_hash,
                    command_id=accepted.command_id,
                ).model_copy(update={"command_digest": "e" * 64})
            )
        assert conflict.command_id == accepted.command_id

        terminal_tenant = f"it-ws3-cancel-terminal-{uuid.uuid4().hex[:12]}"
        terminal_handle, terminal_hash = await _submit_start(api_url, migration_url, terminal_tenant)
        async with owner_engine.begin() as connection:
            await connection.execute(
                text("UPDATE agent_run SET status = 'succeeded' WHERE run_id = :run_id"),
                {"run_id": terminal_handle.run_id},
            )
        with pytest.raises(RunStateConflict):
            await api_driver.dispatch(
                _cancel_command(
                    tenant_id=terminal_tenant,
                    run_id=terminal_handle.run_id,
                    runtime_build_hash=terminal_hash,
                )
            )

        waiting_tenant = f"it-ws3-cancel-waiting-{uuid.uuid4().hex[:12]}"
        waiting_handle, waiting_hash = await _submit_start(api_url, migration_url, waiting_tenant)
        async with owner_engine.begin() as connection:
            await connection.execute(
                text("UPDATE agent_run SET status = 'waiting_user_input' WHERE run_id = :run_id"),
                {"run_id": waiting_handle.run_id},
            )
        waiting_receipt = await api_driver.dispatch(
            _cancel_command(
                tenant_id=waiting_tenant,
                run_id=waiting_handle.run_id,
                runtime_build_hash=waiting_hash,
            )
        )
        assert waiting_receipt.command_seq == 1
    finally:
        await runtime_engine.dispose()
        await api_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_concurrent_cancel_commands_have_one_winner() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-cancel-concurrent-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine_a = create_async_engine(runtime_url)
    runtime_engine_b = create_async_engine(runtime_url)
    api_engine_a = create_async_engine(api_url)
    api_engine_b = create_async_engine(api_url)
    driver_a = PostgresExecutionDriver(
        async_sessionmaker(runtime_engine_a, expire_on_commit=False),
        command_session_factory=async_sessionmaker(api_engine_a, expire_on_commit=False),
    )
    driver_b = PostgresExecutionDriver(
        async_sessionmaker(runtime_engine_b, expire_on_commit=False),
        command_session_factory=async_sessionmaker(api_engine_b, expire_on_commit=False),
    )
    first = _cancel_command(tenant_id=tenant_id, run_id=handle.run_id, runtime_build_hash=runtime_hash)
    second = _cancel_command(tenant_id=tenant_id, run_id=handle.run_id, runtime_build_hash=runtime_hash)
    try:
        results = await asyncio.gather(driver_a.dispatch(first), driver_b.dispatch(second), return_exceptions=True)
        assert len([result for result in results if not isinstance(result, BaseException)]) == 1
        assert len([result for result in results if isinstance(result, RunStateConflict)]) == 1
    finally:
        await runtime_engine_a.dispose()
        await runtime_engine_b.dispose()
        await api_engine_a.dispose()
        await api_engine_b.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancel_lock_timeout_and_task_cancellation_roll_back_payload_and_state() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-cancel-rollback-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    api_engine = create_async_engine(api_url)
    owner_engine = create_async_engine(migration_url)
    api_driver = PostgresExecutionDriver(
        async_sessionmaker(runtime_engine, expire_on_commit=False),
        command_session_factory=async_sessionmaker(api_engine, expire_on_commit=False),
        operation_timeout_seconds=0.25,
    )
    lock = await owner_engine.connect()
    transaction = await lock.begin()
    try:
        await lock.execute(
            text("SELECT 1 FROM agent_run WHERE tenant_id = :tenant AND run_id = :run_id FOR UPDATE"),
            {"tenant": tenant_id, "run_id": handle.run_id},
        )
        timed_out = _cancel_command(
            tenant_id=tenant_id,
            run_id=handle.run_id,
            runtime_build_hash=runtime_hash,
            reason_ref="timeout-reason",
            reason_hash="b" * 64,
        )
        timed_out_hash = canonical_hash({"reason_ref": "timeout-reason", "reason_hash": "b" * 64})
        with pytest.raises(asyncio.TimeoutError):
            await api_driver.dispatch(timed_out)
        async with owner_engine.connect() as connection:
            run_state = (
                await connection.execute(
                    text("SELECT status, revision, execution_fence FROM agent_run WHERE run_id = :run_id"),
                    {"run_id": handle.run_id},
                )
            ).one()
            assert run_state == ("accepted", 0, 0)
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM command_payload "
                        "WHERE tenant_id = :tenant AND payload_hash = :payload_hash"
                    ),
                    {"tenant": tenant_id, "payload_hash": timed_out_hash},
                )
                == 0
            )

        cancelled = _cancel_command(
            tenant_id=tenant_id,
            run_id=handle.run_id,
            runtime_build_hash=runtime_hash,
            reason_ref="cancelled-reason",
            reason_hash="c" * 64,
        )
        cancelled_hash = canonical_hash({"reason_ref": "cancelled-reason", "reason_hash": "c" * 64})
        task = asyncio.create_task(api_driver.dispatch(cancelled))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with owner_engine.connect() as connection:
            run_state = (
                await connection.execute(
                    text("SELECT status, revision, execution_fence FROM agent_run WHERE run_id = :run_id"),
                    {"run_id": handle.run_id},
                )
            ).one()
            assert run_state == ("accepted", 0, 0)
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM command_payload "
                        "WHERE tenant_id = :tenant AND payload_hash = :payload_hash"
                    ),
                    {"tenant": tenant_id, "payload_hash": cancelled_hash},
                )
                == 0
            )
    finally:
        if transaction.is_active:
            await transaction.rollback()
        await lock.close()
        await runtime_engine.dispose()
        await api_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_heartbeat_is_strict_monotonic_compare_and_swap() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-heartbeat-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    engine = create_async_engine(runtime_url)
    driver = PostgresExecutionDriver(async_sessionmaker(engine, expire_on_commit=False))
    try:
        claim = await driver.claim("worker", runtime_hash, tenant_id, 10)
        assert claim is not None
        original_state = await _lease_state(migration_url, handle.run_id)
        with pytest.raises(StaleExecutionFence):
            await driver.heartbeat(claim, 0.1)
        assert await _lease_state(migration_url, handle.run_id) == original_state

        current = await driver.heartbeat(claim, 12)
        current_state = await _lease_state(migration_url, handle.run_id)
        with pytest.raises(StaleExecutionFence):
            await driver.heartbeat(claim, 15)
        assert await _lease_state(migration_url, handle.run_id) == current_state
        assert current.lease_until > claim.lease_until

        mutations = (
            {"tenant_id": "other-tenant"},
            {"worker_id": "other-worker"},
            {"execution_fence": claim.execution_fence + 1},
            {"run_id": uuid.uuid4()},
            {"command_id": uuid.uuid4()},
        )
        for mutation in mutations:
            forged = current.model_copy(update=mutation)
            before = await _lease_state(migration_url, handle.run_id)
            with pytest.raises(StaleExecutionFence):
                await driver.heartbeat(forged, 20)
            assert await _lease_state(migration_url, handle.run_id) == before

        expired_handle, _ = await _submit_start(api_url, migration_url, tenant_id, seed_identity=False)
        expired = await driver.claim("worker-expired", runtime_hash, tenant_id, 0.1)
        assert expired is not None and expired.run_id == expired_handle.run_id
        await asyncio.sleep(0.15)
        expired_state = await _lease_state(migration_url, expired_handle.run_id)
        with pytest.raises(StaleExecutionFence):
            await driver.heartbeat(expired, 1)
        assert await _lease_state(migration_url, expired_handle.run_id) == expired_state
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_locked_matching_command_is_busy_not_version_unavailable() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-locked-{uuid.uuid4().hex[:12]}"
    matching, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    mismatch, _ = await _insert_runtime_build_variant(
        migration_url,
        tenant_id=tenant_id,
        source_run_id=matching.run_id,
        runtime_build_hash="f" * 64,
    )
    owner = create_async_engine(migration_url)
    runtime = create_async_engine(runtime_url)
    driver = PostgresExecutionDriver(async_sessionmaker(runtime, expire_on_commit=False))
    lock = await owner.connect()
    transaction = await lock.begin()
    try:
        await lock.execute(
            text("SELECT 1 FROM run_command WHERE run_id = :run_id FOR UPDATE"),
            {"run_id": matching.run_id},
        )
        assert await driver.claim("worker", runtime_hash, tenant_id) is None
        await transaction.commit()
        claimed = await driver.claim("worker", runtime_hash, tenant_id)
        assert claimed is not None and claimed.run_id == matching.run_id
    finally:
        if transaction.is_active:
            await transaction.rollback()
        await lock.close()
        await owner.dispose()
        await runtime.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("matching_state", ("future", "leased"))
async def test_not_ready_matching_build_is_not_version_unavailable(matching_state: str) -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-not-ready-{uuid.uuid4().hex[:10]}"
    matching, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    _wrong, _ = await _insert_runtime_build_variant(
        migration_url,
        tenant_id=tenant_id,
        source_run_id=matching.run_id,
        runtime_build_hash="f" * 64,
    )
    owner = create_async_engine(migration_url)
    runtime = create_async_engine(runtime_url)
    driver = PostgresExecutionDriver(async_sessionmaker(runtime, expire_on_commit=False))
    try:
        async with owner.begin() as connection:
            if matching_state == "future":
                await connection.execute(
                    text("UPDATE run_command SET available_at = now() + interval '10 seconds' WHERE run_id = :run_id"),
                    {"run_id": matching.run_id},
                )
        if matching_state == "leased":
            claimed = await driver.claim("lease-holder", runtime_hash, tenant_id, 10)
            assert claimed is not None and claimed.run_id == matching.run_id

        assert await driver.claim("worker", runtime_hash, tenant_id) is None

        async with owner.begin() as connection:
            if matching_state == "future":
                await connection.execute(
                    text("UPDATE run_command SET available_at = now() WHERE run_id = :run_id"),
                    {"run_id": matching.run_id},
                )
            else:
                await connection.execute(
                    text("UPDATE run_command SET lease_until = now() - interval '1 second' WHERE run_id = :run_id"),
                    {"run_id": matching.run_id},
                )
                await connection.execute(
                    text("UPDATE agent_run SET lease_until = now() - interval '1 second' WHERE run_id = :run_id"),
                    {"run_id": matching.run_id},
                )
        available = await driver.claim("worker", runtime_hash, tenant_id)
        assert available is not None and available.run_id == matching.run_id
    finally:
        await owner.dispose()
        await runtime.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_heartbeat_cas_binds_full_claim_identity_and_is_single_winner() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-heartbeat-id-{uuid.uuid4().hex[:10]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    engine_a = create_async_engine(runtime_url)
    engine_b = create_async_engine(runtime_url)
    driver_a = PostgresExecutionDriver(async_sessionmaker(engine_a, expire_on_commit=False))
    driver_b = PostgresExecutionDriver(async_sessionmaker(engine_b, expire_on_commit=False))
    try:
        claim = await driver_a.claim("worker", runtime_hash, tenant_id, 10)
        assert claim is not None
        for mutation in (
            {"command_seq": claim.command_seq + 1},
            {"command_digest": "c" * 64},
            {"runtime_build_hash": "d" * 64},
        ):
            before = await _lease_state(migration_url, handle.run_id)
            with pytest.raises(StaleExecutionFence):
                await driver_a.heartbeat(claim.model_copy(update=mutation), 12)
            assert await _lease_state(migration_url, handle.run_id) == before

        results = await asyncio.gather(
            driver_a.heartbeat(claim, 12),
            driver_b.heartbeat(claim, 12),
            return_exceptions=True,
        )
        assert len([result for result in results if isinstance(result, type(claim))]) == 1
        assert len([result for result in results if isinstance(result, StaleExecutionFence)]) == 1
    finally:
        await engine_a.dispose()
        await engine_b.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("closed_state", ("consumed", "dead_letter", "terminal", "empty"))
async def test_closed_or_terminal_wrong_build_is_not_version_unavailable(closed_state: str) -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-closed-{uuid.uuid4().hex[:10]}"
    source_handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    handle, wrong_runtime_hash = await _insert_runtime_build_variant(
        migration_url,
        tenant_id=tenant_id,
        source_run_id=source_handle.run_id,
        runtime_build_hash="f" * 64,
    )
    owner = create_async_engine(migration_url)
    runtime = create_async_engine(runtime_url)
    driver = PostgresExecutionDriver(async_sessionmaker(runtime, expire_on_commit=False))
    try:
        async with owner.begin() as connection:
            for closed_run_id in (source_handle.run_id, handle.run_id):
                if closed_state == "empty":
                    await connection.execute(
                        text("DELETE FROM run_command WHERE run_id = :run_id"),
                        {"run_id": closed_run_id},
                    )
                elif closed_state == "terminal":
                    await connection.execute(
                        text("UPDATE agent_run SET status = 'succeeded' WHERE run_id = :run_id"),
                        {"run_id": closed_run_id},
                    )
                elif closed_state == "consumed":
                    await connection.execute(
                        text(
                            "UPDATE run_command SET status = 'consumed', consumed_provenance_kind = 'claim.v1', "
                            "consumed_worker_id = 'fixture', "
                            "consumed_execution_fence = 1, consumed_lease_until = now(), "
                            "consumed_claim_provenance_hash = :fingerprint WHERE run_id = :run_id"
                        ),
                        {"run_id": closed_run_id, "fingerprint": "a" * 64},
                    )
                else:
                    await connection.execute(
                        text("UPDATE run_command SET status = :status WHERE run_id = :run_id"),
                        {"status": closed_state, "run_id": closed_run_id},
                    )
        assert await driver.claim("worker", runtime_hash, tenant_id) is None
        assert wrong_runtime_hash != runtime_hash
    finally:
        await owner.dispose()
        await runtime.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_locked_run_is_skipped_and_same_run_remains_single_writer() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-runlock-{uuid.uuid4().hex[:12]}"
    locked_handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    available_handle, _ = await _submit_start(api_url, migration_url, tenant_id, seed_identity=False)
    owner = create_async_engine(migration_url)
    runtime_a = create_async_engine(runtime_url)
    runtime_b = create_async_engine(runtime_url)
    driver_a = PostgresExecutionDriver(async_sessionmaker(runtime_a, expire_on_commit=False))
    driver_b = PostgresExecutionDriver(async_sessionmaker(runtime_b, expire_on_commit=False))
    async with owner.begin() as connection:
        await connection.execute(
            text(
                "UPDATE run_command SET available_at = CASE WHEN run_id = :locked THEN now() - interval '2 seconds' "
                "ELSE now() - interval '1 second' END WHERE run_id IN (:locked, :available)"
            ),
            {"locked": locked_handle.run_id, "available": available_handle.run_id},
        )
    lock = await owner.connect()
    transaction = await lock.begin()
    try:
        await lock.execute(
            text("SELECT 1 FROM agent_run WHERE run_id = :run_id FOR UPDATE"),
            {"run_id": locked_handle.run_id},
        )
        claimed = await driver_a.claim("worker-a", runtime_hash, tenant_id, 0.2)
        assert claimed is not None and claimed.run_id == available_handle.run_id
        await transaction.commit()
        next_claim = await driver_b.claim("worker-b", runtime_hash, tenant_id, 0.2)
        assert next_claim is not None and next_claim.run_id == locked_handle.run_id
    finally:
        if transaction.is_active:
            await transaction.rollback()
        await lock.close()

    await asyncio.sleep(0.25)
    command_id = uuid.uuid4()
    async with owner.begin() as connection:
        await connection.execute(
            text(
                "UPDATE run_command SET status = 'consumed', consumed_provenance_kind = 'claim.v1', "
                "lease_owner = NULL, lease_until = NULL, "
                "execution_fence = NULL, consumed_worker_id = 'fixture', consumed_execution_fence = 1, "
                "consumed_lease_until = now(), consumed_claim_provenance_hash = :fingerprint WHERE run_id = :run_id"
            ),
            {"run_id": available_handle.run_id, "fingerprint": "a" * 64},
        )
        await connection.execute(
            text("UPDATE agent_run SET lease_owner = NULL, lease_until = NULL WHERE run_id = :run_id"),
            {"run_id": available_handle.run_id},
        )
        await connection.execute(
            text(
                "INSERT INTO run_command (command_id, tenant_id, run_id, principal_id, principal_kind, "
                "command_seq, command_type, command_schema_version, command_digest, payload_ref, payload_hash, status) "
                "SELECT :command_id, tenant_id, run_id, principal_id, principal_kind, 1, command_type, "
                "command_schema_version, :digest, payload_ref, payload_hash, 'pending' FROM run_command "
                "WHERE run_id = :run_id AND command_seq = 0"
            ),
            {"command_id": command_id, "run_id": locked_handle.run_id, "digest": "d" * 64},
        )
    try:
        results = await asyncio.gather(
            driver_a.claim("worker-c", runtime_hash, tenant_id, 0.2),
            driver_b.claim("worker-d", runtime_hash, tenant_id, 0.2),
        )
        assert len([claim for claim in results if claim is not None]) == 1
    finally:
        await owner.dispose()
        await runtime_a.dispose()
        await runtime_b.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_command_only_lock_is_bounded_and_leaves_both_rows_unchanged() -> None:
    """An abnormal command-only lock cannot create a partial claim or wait forever."""

    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-commandlock-{uuid.uuid4().hex[:12]}"
    first, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    second, _ = await _submit_start(api_url, migration_url, tenant_id, seed_identity=False)
    owner = create_async_engine(migration_url)
    runtime = create_async_engine(runtime_url)
    driver = PostgresExecutionDriver(async_sessionmaker(runtime, expire_on_commit=False))
    lock = await owner.connect()
    transaction = await lock.begin()
    try:
        await lock.execute(
            text("SELECT 1 FROM run_command WHERE run_id = :run_id FOR UPDATE"),
            {"run_id": first.run_id},
        )
        before_first = await _lease_state(migration_url, first.run_id)
        before_second = await _lease_state(migration_url, second.run_id)
        result = await asyncio.wait_for(driver.claim("command-lock-worker", runtime_hash, tenant_id, 1), timeout=2)
        assert result is None
        assert await _lease_state(migration_url, first.run_id) == before_first
        assert await _lease_state(migration_url, second.run_id) == before_second
    finally:
        if transaction.is_active:
            await transaction.rollback()
        await lock.close()
        await owner.dispose()
        await runtime.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_claim_discovery_then_supersede_is_zero_write() -> None:
    """A post-discovery supersede must invalidate both claim CAS updates."""

    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-claim-cas-supersede-{uuid.uuid4().hex[:10]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    owner = create_async_engine(migration_url)
    runtime = create_async_engine(runtime_url)
    driver = PostgresExecutionDriver(async_sessionmaker(runtime, expire_on_commit=False))
    target_command_id = uuid.uuid4()
    try:
        async with owner.begin() as connection:
            source = (
                await connection.execute(
                    text(
                        "SELECT command_id, principal_id, principal_kind, payload_ref, payload_hash, "
                        "command_digest FROM run_command "
                        "WHERE tenant_id = :tenant AND run_id = :run_id AND command_seq = 0"
                    ),
                    {"tenant": tenant_id, "run_id": handle.run_id},
                )
            ).one()
            await connection.execute(
                text(
                    "INSERT INTO run_command (command_id, tenant_id, run_id, principal_id, principal_kind, "
                    "command_seq, command_type, command_schema_version, command_digest, payload_ref, "
                    "payload_hash, status) VALUES (:command_id, :tenant, :run_id, :principal_id, :principal_kind, "
                    "1, 'start', 'start.v1', :digest, :payload_ref, :payload_hash, 'pending')"
                ),
                {
                    "command_id": target_command_id,
                    "tenant": tenant_id,
                    "run_id": handle.run_id,
                    "principal_id": source[1],
                    "principal_kind": source[2],
                    "digest": "f" * 64,
                    "payload_ref": source[3],
                    "payload_hash": source[4],
                },
            )
            await connection.execute(
                text(
                    """
                    CREATE OR REPLACE FUNCTION public.grove_test_claim_supersede() RETURNS trigger
                    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
                    BEGIN
                        IF NEW.tenant_id LIKE 'it-ws3-claim-cas-supersede-%' THEN
                            UPDATE public.run_command
                               SET superseded_by_command_id = (
                                       SELECT command_id
                                         FROM public.run_command
                                        WHERE tenant_id = NEW.tenant_id
                                          AND run_id = NEW.run_id
                                          AND command_seq = 1
                                   ),
                                   superseded_by_command_seq = 1,
                                   superseded_by_command_digest = repeat('f', 64)
                             WHERE tenant_id = NEW.tenant_id
                               AND run_id = NEW.run_id
                               AND command_seq = 0;
                        END IF;
                        RETURN NEW;
                    END $$
                    """
                )
            )
            await connection.execute(
                text(
                    "CREATE TRIGGER agent_run_test_claim_supersede BEFORE UPDATE OF execution_fence "
                    "ON public.agent_run FOR EACH ROW EXECUTE FUNCTION public.grove_test_claim_supersede()"
                )
            )
        before = await _lease_state(migration_url, handle.run_id)
        assert await driver.claim("claim-cas-supersede", runtime_hash, tenant_id, 10) is None
        assert await _lease_state(migration_url, handle.run_id) == before
        async with owner.begin() as connection:
            restored = (
                await connection.execute(
                    text(
                        "SELECT superseded_by_command_id, status, run_id FROM run_command "
                        "WHERE tenant_id = :tenant AND command_seq = 0"
                    ),
                    {"tenant": tenant_id},
                )
            ).one()
            assert restored == (None, "pending", handle.run_id)
    finally:
        async with owner.begin() as connection:
            await connection.execute(text("DROP TRIGGER IF EXISTS agent_run_test_claim_supersede ON public.agent_run"))
            await connection.execute(text("DROP FUNCTION IF EXISTS public.grove_test_claim_supersede()"))
            await connection.execute(
                text(
                    "UPDATE run_command SET superseded_by_command_id = NULL, superseded_by_command_seq = NULL, "
                    "superseded_by_command_digest = NULL, superseded_by_provenance_hash = NULL "
                    "WHERE tenant_id = :tenant AND superseded_by_command_id = :command_id"
                ),
                {"tenant": tenant_id, "command_id": target_command_id},
            )
            await connection.execute(
                text("DELETE FROM run_command WHERE tenant_id = :tenant AND command_id = :command_id"),
                {"tenant": tenant_id, "command_id": target_command_id},
            )
        await owner.dispose()
        await runtime.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_claim_discovery_then_command_run_rebind_is_zero_write() -> None:
    """A command rebound from candidate run A to B cannot be claimed for A."""

    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-claim-cas-rebind-{uuid.uuid4().hex[:10]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    rebound_handle, _ = await _insert_runtime_build_variant(
        migration_url,
        tenant_id=tenant_id,
        source_run_id=handle.run_id,
        runtime_build_hash="e" * 64,
    )
    owner = create_async_engine(migration_url)
    runtime = create_async_engine(runtime_url)
    driver = PostgresExecutionDriver(async_sessionmaker(runtime, expire_on_commit=False))
    try:
        async with owner.begin() as connection:
            await connection.execute(
                text("DELETE FROM run_command WHERE tenant_id = :tenant AND run_id = :run_id"),
                {"tenant": tenant_id, "run_id": rebound_handle.run_id},
            )
            await connection.execute(
                text(
                    """
                    CREATE OR REPLACE FUNCTION public.grove_test_claim_rebind() RETURNS trigger
                    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
                    BEGIN
                        IF NEW.tenant_id LIKE 'it-ws3-claim-cas-rebind-%' THEN
                            UPDATE public.run_command
                               SET run_id = (
                                       SELECT run_id
                                         FROM public.agent_run
                                        WHERE tenant_id = NEW.tenant_id
                                          AND run_id <> NEW.run_id
                                          AND runtime_build_hash <> NEW.runtime_build_hash
                                        ORDER BY created_at, run_id
                                        LIMIT 1
                                   )
                             WHERE tenant_id = NEW.tenant_id
                               AND run_id = NEW.run_id
                               AND command_seq = 0;
                        END IF;
                        RETURN NEW;
                    END $$
                    """
                )
            )
            await connection.execute(
                text(
                    "CREATE TRIGGER agent_run_test_claim_rebind BEFORE UPDATE OF execution_fence "
                    "ON public.agent_run FOR EACH ROW EXECUTE FUNCTION public.grove_test_claim_rebind()"
                )
            )
        before = await _lease_state(migration_url, handle.run_id)
        assert await driver.claim("claim-cas-rebind", runtime_hash, tenant_id, 10) is None
        assert await _lease_state(migration_url, handle.run_id) == before
        async with owner.begin() as connection:
            assert (
                await connection.scalar(
                    text("SELECT run_id FROM run_command WHERE tenant_id = :tenant AND command_seq = 0"),
                    {"tenant": tenant_id},
                )
                == handle.run_id
            )
    finally:
        async with owner.begin() as connection:
            await connection.execute(text("DROP TRIGGER IF EXISTS agent_run_test_claim_rebind ON public.agent_run"))
            await connection.execute(text("DROP FUNCTION IF EXISTS public.grove_test_claim_rebind()"))
            await connection.execute(
                text("UPDATE run_command SET run_id = :source_run WHERE tenant_id = :tenant AND run_id = :rebound_run"),
                {"tenant": tenant_id, "source_run": handle.run_id, "rebound_run": rebound_handle.run_id},
            )
            await connection.execute(
                text("DELETE FROM run_command WHERE tenant_id = :tenant AND run_id = :run_id"),
                {"tenant": tenant_id, "run_id": rebound_handle.run_id},
            )
            await connection.execute(
                text("DELETE FROM agent_run WHERE tenant_id = :tenant AND run_id = :run_id"),
                {"tenant": tenant_id, "run_id": rebound_handle.run_id},
            )
        await owner.dispose()
        await runtime.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_claim_real_serialization_failure_propagates_and_rolls_back() -> None:
    """A real SSI conflict is not the private command-CAS miss signal.

    The probe transaction takes a serializable SIREAD dependency on the
    candidate command before the claim starts.  The command trigger reads a
    second probe row, then exposes an advisory lock; the probe transaction
    updates that row and commits before the claim command UPDATE.  PostgreSQL
    consequently raises a genuine 40001 at the command UPDATE itself.  Both
    the direct two-row path and the public claim path must propagate it and
    leave the outer transaction with no durable mutation.
    """

    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    migration_raw_url = migration_url.replace("postgresql+psycopg://", "postgresql://", 1)
    runtime_raw_url = runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
    tenant_prefix = f"it-ws3-claim-serializable-{uuid.uuid4().hex[:10]}-"
    owner = create_async_engine(migration_url)

    async def run_case(*, claim_path: bool) -> None:
        tenant_id = f"{tenant_prefix}{'claim' if claim_path else 'direct'}"
        handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
        advisory_key = (handle.run_id.int & ((1 << 63) - 1)) or 1
        async with owner.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO public.grove_r2_claim_serialization_probe "
                    "(probe_key, probe_value, advisory_key) VALUES (:tenant, 0, :key)"
                ),
                {"tenant": tenant_id, "key": advisory_key},
            )
            command_id = await connection.scalar(
                text(
                    "SELECT command_id FROM run_command "
                    "WHERE tenant_id = :tenant AND run_id = :run_id AND command_seq = 0"
                ),
                {"tenant": tenant_id, "run_id": handle.run_id},
            )
        assert isinstance(command_id, uuid.UUID)
        before = await _claim_authority_state(migration_url, handle.run_id)
        probe_ready = asyncio.Event()
        trigger_seen = asyncio.Event()

        async def concurrent_probe() -> None:
            connection = await psycopg.AsyncConnection.connect(migration_raw_url)
            try:
                await connection.execute("BEGIN TRANSACTION ISOLATION LEVEL SERIALIZABLE")
                await (
                    await connection.execute(
                        "SELECT status FROM run_command WHERE tenant_id = %s AND command_id = %s",
                        (tenant_id, command_id),
                    )
                ).fetchone()
                probe_ready.set()
                await trigger_seen.wait()
                await connection.execute(
                    "UPDATE public.grove_r2_claim_serialization_probe "
                    "SET probe_value = probe_value + 1 WHERE probe_key = %s",
                    (tenant_id,),
                )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
            finally:
                await connection.close()

        async def invoke_path() -> tuple[Any, ...] | None:
            # The direct two-row probe uses the migration owner to exercise
            # the SQL UPDATE seam itself; the public claim path must use the
            # least-privileged runtime role exactly as production does.
            connection = await psycopg.AsyncConnection.connect(runtime_raw_url if claim_path else migration_raw_url)
            try:
                await connection.execute("BEGIN TRANSACTION ISOLATION LEVEL SERIALIZABLE")
                if claim_path:
                    await connection.execute("SELECT set_config(%s, %s, true)", ("grove.tenant_id", tenant_id))
                if claim_path:
                    cursor = await connection.execute(
                        "SELECT * FROM public.grove_claim_run_command(%s, %s, %s, %s)",
                        (tenant_id, "serializable-claim", runtime_hash, 10.0),
                    )
                    row = await cursor.fetchone()
                    await connection.commit()
                    return tuple(row) if row is not None else None
                await connection.execute(
                    "UPDATE public.agent_run SET status = 'running', execution_fence = execution_fence + 1, "
                    "lease_owner = 'serializable-direct', "
                    "lease_until = clock_timestamp() + interval '10 seconds' "
                    "WHERE tenant_id = %s AND run_id = %s",
                    (tenant_id, handle.run_id),
                )
                cursor = await connection.execute(
                    "UPDATE public.run_command SET status = 'leased', lease_owner = 'serializable-direct', "
                    "lease_until = clock_timestamp() + interval '10 seconds', execution_fence = 1, "
                    "attempt_count = attempt_count + 1 WHERE tenant_id = %s AND command_id = %s",
                    (tenant_id, command_id),
                )
                await connection.commit()
                return (cursor.rowcount,)
            except BaseException:
                await connection.rollback()
                raise
            finally:
                await connection.close()

        writer_task = asyncio.create_task(concurrent_probe())
        invocation_task: asyncio.Task[tuple[Any, ...] | None] | None = None
        try:
            await asyncio.wait_for(probe_ready.wait(), timeout=3)
            invocation_task = asyncio.create_task(invoke_path())
            await _wait_for_advisory_xact_lock(migration_raw_url, advisory_key, deadline_seconds=3)
            trigger_seen.set()
            if claim_path:
                try:
                    result = await asyncio.wait_for(invocation_task, timeout=3)
                except psycopg.errors.SerializationFailure as exc:
                    assert exc.sqlstate == "40001"
                else:
                    pytest.fail(f"claim swallowed real serialization failure and returned {result!r}")
            else:
                with pytest.raises(psycopg.errors.SerializationFailure) as failure:
                    await asyncio.wait_for(invocation_task, timeout=3)
                assert failure.value.sqlstate == "40001"
            await asyncio.wait_for(writer_task, timeout=3)
            assert await _claim_authority_state(migration_url, handle.run_id) == before
        finally:
            trigger_seen.set()
            if invocation_task is not None and not invocation_task.done():
                invocation_task.cancel()
                await asyncio.gather(invocation_task, return_exceptions=True)
            if not writer_task.done():
                writer_task.cancel()
            await asyncio.gather(writer_task, return_exceptions=True)

    try:
        async with owner.begin() as connection:
            await connection.execute(
                text("DROP TRIGGER IF EXISTS grove_r2_claim_serialization_probe_trigger ON public.run_command")
            )
            await connection.execute(text("DROP FUNCTION IF EXISTS public.grove_r2_claim_serialization_probe()"))
            await connection.execute(text("DROP TABLE IF EXISTS public.grove_r2_claim_serialization_probe"))
            await connection.execute(
                text(
                    "CREATE TABLE public.grove_r2_claim_serialization_probe ("
                    "probe_key TEXT PRIMARY KEY, probe_value INTEGER NOT NULL, advisory_key BIGINT NOT NULL)"
                )
            )
            await connection.execute(
                text(
                    """
                    CREATE FUNCTION public.grove_r2_claim_serialization_probe() RETURNS trigger
                    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
                    DECLARE
                        ignored_value INTEGER;
                        lock_key BIGINT;
                    BEGIN
                        SELECT probe_value, advisory_key INTO ignored_value, lock_key
                          FROM public.grove_r2_claim_serialization_probe
                         WHERE probe_key = NEW.tenant_id;
                        IF FOUND THEN
                            PERFORM pg_advisory_xact_lock(lock_key);
                            PERFORM pg_sleep(0.5);
                        END IF;
                        RETURN NEW;
                    END
                    $$
                    """
                )
            )
            await connection.execute(
                text(
                    "CREATE TRIGGER grove_r2_claim_serialization_probe_trigger "
                    "BEFORE UPDATE OF status ON public.run_command FOR EACH ROW "
                    "EXECUTE FUNCTION public.grove_r2_claim_serialization_probe()"
                )
            )
        await run_case(claim_path=False)
        await run_case(claim_path=True)
    finally:
        async with owner.begin() as connection:
            await connection.execute(
                text("DROP TRIGGER IF EXISTS grove_r2_claim_serialization_probe_trigger ON public.run_command")
            )
            await connection.execute(text("DROP FUNCTION IF EXISTS public.grove_r2_claim_serialization_probe()"))
            await connection.execute(text("DROP TABLE IF EXISTS public.grove_r2_claim_serialization_probe"))
        await owner.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_claim_samples_authority_clock_after_table_lock_wait() -> None:
    """A table-level wait crosses discovery time without creating an expired lease."""

    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    migration_raw_url = migration_url.replace("postgresql+psycopg://", "postgresql://", 1)
    runtime_raw_url = runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
    tenant_id = f"it-ws3-claim-clock-wait-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    holder: psycopg.AsyncConnection | None = None
    claim_connection: psycopg.AsyncConnection | None = None
    claim_task: asyncio.Task[object] | None = None
    try:
        holder = await psycopg.AsyncConnection.connect(migration_raw_url)
        await holder.execute("BEGIN")
        # EXCLUSIVE is compatible with this holder's own DML but conflicts
        # with the candidate's ROW SHARE table lock; SKIP LOCKED cannot skip
        # a table-level wait.
        await holder.execute("LOCK TABLE public.agent_run IN EXCLUSIVE MODE")
        claim_connection = await _raw_scoped_transaction(runtime_raw_url, tenant_id)
        pid_row = await (await claim_connection.execute("SELECT pg_backend_pid()")).fetchone()
        assert pid_row is not None
        claim_task = asyncio.create_task(
            _raw_call(
                claim_connection,
                "SELECT * FROM grove_claim_run_command(%s, %s, %s, %s)",
                (tenant_id, "claim-clock-wait-worker", runtime_hash, 1.0),
            )
        )
        await _wait_for_backend_lock(migration_url, int(pid_row[0]))
        target = await (await holder.execute("SELECT clock_timestamp() + interval '1.2 seconds'")).fetchone()
        assert target is not None
        await _wait_for_database_clock(migration_url, target[0])
        await holder.commit()
        holder = None
        result = await asyncio.wait_for(claim_task, timeout=4)
        assert result is not None and result[0] == "claimed"
        return_clock = await _wait_for_database_clock(migration_url, datetime.now(UTC), deadline_seconds=1)
        assert result[7] > return_clock
        assert result[2] == handle.run_id
    finally:
        if claim_task is not None and not claim_task.done():
            claim_task.cancel()
        if claim_task is not None:
            await asyncio.gather(claim_task, return_exceptions=True)
        if holder is not None:
            await holder.rollback()
            await holder.close()
        if claim_connection is not None:
            await claim_connection.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_heartbeat_wait_crossing_expected_expiry_is_zero_write() -> None:
    """Heartbeat cannot revive a claim after its authority locks waited past expiry."""

    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    migration_raw_url = migration_url.replace("postgresql+psycopg://", "postgresql://", 1)
    runtime_raw_url = runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
    tenant_id = f"it-ws3-heartbeat-clock-wait-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
    holder: psycopg.AsyncConnection | None = None
    heartbeat_connection: psycopg.AsyncConnection | None = None
    heartbeat_task: asyncio.Task[object] | None = None
    try:
        claim = await driver.claim("heartbeat-clock-worker", runtime_hash, tenant_id, 0.2)
        assert claim is not None and claim.run_id == handle.run_id
        holder = await psycopg.AsyncConnection.connect(migration_raw_url)
        await holder.execute("BEGIN")
        # A table-level conflict forces the authority lock itself to wait;
        # a row-level lock alone would be skipped by the protocol.
        await holder.execute("LOCK TABLE public.agent_run IN EXCLUSIVE MODE")
        heartbeat_connection = await _raw_scoped_transaction(runtime_raw_url, tenant_id)
        pid_row = await (await heartbeat_connection.execute("SELECT pg_backend_pid()")).fetchone()
        assert pid_row is not None
        heartbeat_task = asyncio.create_task(
            _raw_call(
                heartbeat_connection,
                "SELECT grove_heartbeat_run_command(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (*_consume_args(claim), 30.0),
            )
        )
        await _wait_for_backend_lock(migration_url, int(pid_row[0]))
        await _wait_for_database_clock(migration_url, claim.lease_until)
        await holder.commit()
        holder = None
        result = await asyncio.wait_for(heartbeat_task, timeout=4)
        assert result == (None,)
        state = await _lease_state(migration_url, claim.run_id)
        assert state[0][0] == claim.execution_fence
        assert state[0][1] == claim.worker_id and state[0][4] == claim.execution_fence
        assert state[0][2] == claim.lease_until and state[0][6] == claim.lease_until
        assert state[0][7] == 1
    finally:
        if heartbeat_task is not None and not heartbeat_task.done():
            heartbeat_task.cancel()
        if heartbeat_task is not None:
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        if holder is not None:
            await holder.rollback()
            await holder.close()
        if heartbeat_connection is not None:
            await heartbeat_connection.close()
        await runtime_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dead_letter_wait_crossing_expected_expiry_returns_expired_zero_write() -> None:
    """Dead-letter cannot mutate a claim after its authority locks waited past expiry."""

    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    migration_raw_url = migration_url.replace("postgresql+psycopg://", "postgresql://", 1)
    runtime_raw_url = runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
    tenant_id = f"it-ws3-dead-letter-clock-wait-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
    holder: psycopg.AsyncConnection | None = None
    dead_connection: psycopg.AsyncConnection | None = None
    dead_task: asyncio.Task[object] | None = None
    try:
        claim = await driver.claim("dead-letter-clock-worker", runtime_hash, tenant_id, 0.2)
        assert claim is not None and claim.run_id == handle.run_id
        holder = await psycopg.AsyncConnection.connect(migration_raw_url)
        await holder.execute("BEGIN")
        # Hold the table lock so dead-letter cannot sample authority time until
        # after the expected lease has expired.
        await holder.execute("LOCK TABLE public.agent_run IN EXCLUSIVE MODE")
        dead_connection = await _raw_scoped_transaction(runtime_raw_url, tenant_id)
        pid_row = await (await dead_connection.execute("SELECT pg_backend_pid()")).fetchone()
        assert pid_row is not None
        dead_task = asyncio.create_task(
            _raw_call(
                dead_connection,
                "SELECT * FROM grove_dead_letter_run_command(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                _dead_letter_args(claim, "expired-after-authority-wait"),
            )
        )
        await _wait_for_backend_lock(migration_url, int(pid_row[0]))
        await _wait_for_database_clock(migration_url, claim.lease_until)
        await holder.commit()
        holder = None
        result = await asyncio.wait_for(dead_task, timeout=4)
        assert result is not None and result[0] == "expired"
        state = await _lease_state(migration_url, claim.run_id)
        assert state[0][0] == claim.execution_fence
        assert state[0][1] == claim.worker_id and state[0][4] == claim.execution_fence
        assert state[0][2] == claim.lease_until and state[0][6] == claim.lease_until
        assert state[0][7] == 1
    finally:
        if dead_task is not None and not dead_task.done():
            dead_task.cancel()
        if dead_task is not None:
            await asyncio.gather(dead_task, return_exceptions=True)
        if holder is not None:
            await holder.rollback()
            await holder.close()
        if dead_connection is not None:
            await dead_connection.close()
        await runtime_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_claim_heartbeat_takeover_and_fence_high_water_are_durable() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-{uuid.uuid4().hex[:16]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)

    engine_a = create_async_engine(runtime_url)
    engine_b = create_async_engine(runtime_url)
    factory_a = async_sessionmaker(engine_a, expire_on_commit=False)
    factory_b = async_sessionmaker(engine_b, expire_on_commit=False)
    driver_a = PostgresExecutionDriver(factory_a, lease_seconds=0.25)
    driver_b = PostgresExecutionDriver(factory_b, lease_seconds=0.25)
    owner = create_async_engine(migration_url)
    try:
        first_results = await asyncio.gather(
            driver_a.claim("worker-a", runtime_hash, tenant_id),
            driver_b.claim("worker-b", runtime_hash, tenant_id),
        )
        claims = [claim for claim in first_results if claim is not None]
        assert len(claims) == 1
        first = claims[0]
        assert first.run_id == handle.run_id
        assert first.execution_fence == 1

        heartbeat = await (driver_a if first.worker_id == "worker-a" else driver_b).heartbeat(first, 0.4)
        assert heartbeat.execution_fence == 1
        assert heartbeat.lease_until > first.lease_until
        async with owner.connect() as connection:
            run_lease, command_lease = (
                await connection.execute(
                    text(
                        "SELECT r.lease_until, c.lease_until FROM agent_run r "
                        "JOIN run_command c USING (tenant_id, run_id) WHERE r.run_id = :run_id"
                    ),
                    {"run_id": handle.run_id},
                )
            ).one()
        assert run_lease == command_lease == heartbeat.lease_until

        await asyncio.sleep(0.45)
        second = await driver_b.claim("worker-takeover", runtime_hash, tenant_id, 0.2)
        assert second is not None
        assert second.execution_fence == 2

        async with owner.connect() as connection:
            before = (
                await connection.execute(
                    text(
                        "SELECT r.execution_fence, r.lease_owner, r.lease_until, "
                        "c.execution_fence, c.lease_owner, c.lease_until, c.attempt_count "
                        "FROM agent_run r JOIN run_command c USING (tenant_id, run_id) "
                        "WHERE r.run_id = :run_id"
                    ),
                    {"run_id": handle.run_id},
                )
            ).one()
        with pytest.raises(StaleExecutionFence):
            await driver_a.heartbeat(first, 0.3)
        async with owner.connect() as connection:
            after = (
                await connection.execute(
                    text(
                        "SELECT r.execution_fence, r.lease_owner, r.lease_until, "
                        "c.execution_fence, c.lease_owner, c.lease_until, c.attempt_count "
                        "FROM agent_run r JOIN run_command c USING (tenant_id, run_id) "
                        "WHERE r.run_id = :run_id"
                    ),
                    {"run_id": handle.run_id},
                )
            ).one()
        assert after == before

        await asyncio.sleep(0.25)
        rebuilt_engine = create_async_engine(runtime_url)
        try:
            rebuilt = PostgresExecutionDriver(async_sessionmaker(rebuilt_engine, expire_on_commit=False))
            third = await rebuilt.claim("worker-rebuilt", runtime_hash, tenant_id, 0.2)
            assert third is not None
            assert third.execution_fence == 3
        finally:
            await rebuilt_engine.dispose()
    finally:
        await engine_a.dispose()
        await engine_b.dispose()
        await owner.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_runtime_build_and_database_roles_fail_closed() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-role-{uuid.uuid4().hex[:16]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    runtime_factory = async_sessionmaker(runtime_engine, expire_on_commit=False)
    driver = PostgresExecutionDriver(runtime_factory)
    api_engine = create_async_engine(api_url)
    owner = create_async_engine(migration_url)
    try:
        with pytest.raises(VersionUnavailable):
            await driver.claim("worker", "f" * 64, tenant_id)

        with pytest.raises(SQLAlchemyError):
            async with runtime_engine.begin() as connection:
                await connection.execute(
                    text("SELECT set_config('grove.tenant_id', :tenant, true)"), {"tenant": tenant_id}
                )
                await connection.execute(
                    text("UPDATE agent_run SET execution_fence = 0 WHERE run_id = :run_id"),
                    {"run_id": handle.run_id},
                )
        with pytest.raises(SQLAlchemyError):
            async with api_engine.begin() as connection:
                await connection.execute(
                    text("SELECT set_config('grove.tenant_id', :tenant, true)"), {"tenant": tenant_id}
                )
                await connection.execute(
                    text("SELECT * FROM grove_claim_run_command(:tenant, 'worker', :runtime_hash, 30::float8)"),
                    {"tenant": tenant_id, "runtime_hash": runtime_hash},
                )
        with pytest.raises(SQLAlchemyError):
            async with runtime_engine.begin() as connection:
                await connection.execute(text("SELECT set_config('grove.tenant_id', 'another-tenant', true)"))
                await connection.execute(
                    text("SELECT * FROM grove_claim_run_command(:tenant, 'worker', :runtime_hash, 30::float8)"),
                    {"tenant": tenant_id, "runtime_hash": runtime_hash},
                )

        claimed = await driver.claim("worker", runtime_hash, tenant_id)
        assert claimed is not None
        with pytest.raises(SQLAlchemyError):
            async with owner.begin() as connection:
                await connection.execute(
                    text("UPDATE agent_run SET execution_fence = 0 WHERE run_id = :run_id"),
                    {"run_id": handle.run_id},
                )
    finally:
        await runtime_engine.dispose()
        await api_engine.dispose()
        await owner.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ws3_migration_schema_functions_and_grants_are_exact() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    engine = create_async_engine(migration_url)
    try:
        async with engine.connect() as connection:
            head = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            from app.build.manifest import migration_head

            assert head == migration_head(_PROJECT_ROOT)
            rows = await connection.execute(
                text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name IN ("
                    "'agent_run', 'run_command', 'checkpoints', 'checkpoint_blobs', 'checkpoint_writes')"
                )
            )
            columns: dict[str, set[str]] = {
                "agent_run": set(),
                "run_command": set(),
                "checkpoints": set(),
                "checkpoint_blobs": set(),
                "checkpoint_writes": set(),
            }
            for table_name, column_name in rows:
                columns[str(table_name)].add(str(column_name))
            assert columns["agent_run"] == {
                "run_id",
                "tenant_id",
                "submission_id",
                "submission_digest",
                "principal_id",
                "principal_kind",
                "skill_spec_hash",
                "skill_spec_ref",
                "runtime_build_ref",
                "runtime_build_hash",
                "status",
                "revision",
                "execution_fence",
                "lease_owner",
                "lease_until",
                "latest_checkpoint_id",
                "latest_applied_command_id",
                "latest_applied_command_digest",
                "latest_applied_command_seq",
                "created_at",
                "updated_at",
            }
            assert columns["run_command"] == {
                "command_id",
                "tenant_id",
                "run_id",
                "principal_id",
                "principal_kind",
                "command_seq",
                "command_type",
                "command_schema_version",
                "command_digest",
                "payload_ref",
                "payload_hash",
                "status",
                "available_at",
                "lease_owner",
                "lease_until",
                "execution_fence",
                "attempt_count",
                "last_error_ref",
                "consumed_worker_id",
                "consumed_execution_fence",
                "consumed_lease_until",
                "consumed_claim_provenance_hash",
                "consumed_provenance_kind",
                "superseded_by_command_id",
                "superseded_by_command_seq",
                "superseded_by_command_digest",
                "superseded_by_provenance_hash",
                "created_at",
            }
            assert columns["checkpoints"] == {
                "tenant_id",
                "thread_id",
                "checkpoint_ns",
                "checkpoint_id",
                "parent_checkpoint_id",
                "type",
                "checkpoint",
                "metadata",
                "content_hash",
                "claim_command_id",
                "claim_command_seq",
                "claim_command_digest",
                "claim_worker_id",
                "claim_execution_fence",
                "claim_lease_until",
                "claim_runtime_build_hash",
                "claim_provenance_hash",
            }
            assert columns["checkpoint_blobs"] == {
                "tenant_id",
                "thread_id",
                "checkpoint_ns",
                "channel",
                "version",
                "type",
                "blob",
                "content_hash",
                "claim_command_id",
                "claim_command_seq",
                "claim_command_digest",
                "claim_worker_id",
                "claim_execution_fence",
                "claim_lease_until",
                "claim_runtime_build_hash",
                "claim_provenance_hash",
            }
            assert columns["checkpoint_writes"] == {
                "tenant_id",
                "thread_id",
                "checkpoint_ns",
                "checkpoint_id",
                "task_id",
                "idx",
                "channel",
                "type",
                "blob",
                "task_path",
                "content_hash",
                "claim_command_id",
                "claim_command_seq",
                "claim_command_digest",
                "claim_worker_id",
                "claim_execution_fence",
                "claim_lease_until",
                "claim_runtime_build_hash",
                "claim_provenance_hash",
            }
            constraints = set(
                (
                    await connection.execute(
                        text(
                            "SELECT conname FROM pg_constraint WHERE conrelid IN "
                            "('agent_run'::regclass, 'run_command'::regclass, 'command_payload'::regclass)"
                        )
                    )
                ).scalars()
            )
            assert {
                "agent_run_execution_fence_ck",
                "agent_run_runtime_build_hash_ck",
                "run_command_type_ck",
                "run_command_schema_version_ck",
                "run_command_status_ck",
                "run_command_seq_ck",
                "run_command_digest_ck",
                "run_command_payload_hash_ck",
                "run_command_payload_fk",
                "run_command_attempt_count_ck",
                "run_command_lease_shape_ck",
                "agent_run_latest_applied_seq_ck",
                "run_command_consumed_provenance_ck",
                "run_command_superseded_provenance_ck",
                "run_command_superseded_target_fk",
                "command_payload_hash_ck",
                "command_payload_schema_version_ck",
                "command_payload_sensitivity_ck",
                "command_payload_retention_ck",
            } <= constraints
            functions = (
                await connection.execute(
                    text(
                        "SELECT p.proname, pg_get_function_identity_arguments(p.oid), p.prosecdef, p.proconfig "
                        "FROM pg_proc p "
                        "JOIN pg_namespace n ON n.oid = p.pronamespace "
                        "WHERE n.nspname = 'public' AND p.proname IN "
                        "('grove_checkpoint_authority_guard', 'grove_checkpoint_physical_guard', "
                        "'grove_execution_claim_lifecycle_valid', 'grove_checkpoint_claim_provenance', "
                        "'grove_checkpoint_tenant_guard', 'grove_accept_cancel_run', 'grove_claim_run_command', "
                        "'grove_heartbeat_run_command', 'grove_reject_agent_run_runtime_build_rebinding', "
                        "'grove_consume_run_command', 'grove_dead_letter_run_command', "
                        "'grove_reconcile_expired_run_command', 'grove_heartbeat_run_command_internal', "
                        "'grove_consume_run_command_internal', 'grove_dead_letter_run_command_internal', "
                        "'grove_reconcile_expired_run_command_internal', 'grove_active_tenant', "
                        "'grove_reject_execution_fence_regression', 'grove_reject_immutable_change', "
                        "'grove_reject_identity_key_change', 'grove_sync_execution_principal', "
                        "'grove_validate_execution_principal', 'grove_finish_delivery') "
                        "ORDER BY p.proname, pg_get_function_identity_arguments(p.oid)"
                    )
                )
            ).all()
            function_keys = {f"public.{row[0]}({row[1]})" for row in functions}
            assert function_keys == set(WS3_SCHEMA_CONTRACT["functions"])
            by_name = {row[0]: row for row in functions}
            assert all(
                by_name[name][2] is True and by_name[name][3] == ["search_path=pg_catalog, public"]
                for name in (
                    "grove_checkpoint_authority_guard",
                    "grove_checkpoint_physical_guard",
                    "grove_checkpoint_tenant_guard",
                    "grove_accept_cancel_run",
                    "grove_claim_run_command",
                    "grove_heartbeat_run_command",
                    "grove_consume_run_command",
                    "grove_dead_letter_run_command",
                    "grove_reconcile_expired_run_command",
                )
            )
            assert by_name["grove_checkpoint_claim_provenance"][2] is False
            assert by_name["grove_execution_claim_lifecycle_valid"][2] is False
            assert by_name["grove_reject_agent_run_runtime_build_rebinding"][2] is False
            assert all(
                by_name[name][2] is True and by_name[name][3] == ["search_path=pg_catalog, public"]
                for name in (
                    "grove_heartbeat_run_command_internal",
                    "grove_consume_run_command_internal",
                    "grove_dead_letter_run_command_internal",
                    "grove_reconcile_expired_run_command_internal",
                )
            )
            internal_signatures = (
                (
                    "grove_heartbeat_run_command_internal("
                    "text,uuid,uuid,bigint,text,text,text,bigint,timestamptz,double precision)"
                ),
                "grove_consume_run_command_internal(text,uuid,uuid,bigint,text,text,text,bigint,timestamptz)",
                (
                    "grove_dead_letter_run_command_internal("
                    "text,uuid,uuid,bigint,text,text,text,bigint,timestamptz,text)"
                ),
                "grove_reconcile_expired_run_command_internal(text,uuid)",
            )
            for signature in internal_signatures:
                grants = (
                    await connection.execute(
                        text(
                            "SELECT has_function_privilege(roles.role, :signature, 'EXECUTE') "
                            "FROM (VALUES ('public'), ('grove_api'), ('grove_runtime'), "
                            "('grove_projection'), ('grove_governance')) AS roles(role)"
                        ),
                        {"signature": signature},
                    )
                ).scalars()
                assert list(grants) == [False, False, False, False, False]
            internal_functions = (
                await connection.execute(
                    text(
                        "SELECT proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                        "WHERE n.nspname = 'public' AND proname LIKE '%\\_legacy' ESCAPE '\\'"
                    )
                )
            ).scalars()
            assert list(internal_functions) == []
            privileges = (
                await connection.execute(
                    text(
                        "SELECT "
                        "has_function_privilege('grove_api', "
                        "'grove_accept_cancel_run(text,uuid,uuid,bigint,text,text,text,text,jsonb)', 'EXECUTE'), "
                        "has_function_privilege('grove_runtime', "
                        "'grove_accept_cancel_run(text,uuid,uuid,bigint,text,text,text,text,jsonb)', 'EXECUTE'), "
                        "has_function_privilege('grove_governance', "
                        "'grove_accept_cancel_run(text,uuid,uuid,bigint,text,text,text,text,jsonb)', 'EXECUTE'), "
                        "has_function_privilege('grove_projection', "
                        "'grove_accept_cancel_run(text,uuid,uuid,bigint,text,text,text,text,jsonb)', 'EXECUTE'), "
                        "has_function_privilege('public', "
                        "'grove_accept_cancel_run(text,uuid,uuid,bigint,text,text,text,text,jsonb)', 'EXECUTE'), "
                        "has_function_privilege('grove_api', "
                        "'grove_claim_run_command(text,text,text,double precision)', 'EXECUTE'), "
                        "has_column_privilege('grove_api', 'agent_run', 'execution_fence', 'INSERT'), "
                        "has_column_privilege('grove_api', 'agent_run', 'execution_fence', 'UPDATE'), "
                        "has_column_privilege('grove_api', 'agent_run', 'runtime_build_hash', 'INSERT'), "
                        "has_column_privilege('grove_api', 'agent_run', 'runtime_build_ref', 'INSERT'), "
                        "has_function_privilege('grove_governance', "
                        "'grove_claim_run_command(text,text,text,double precision)', 'EXECUTE'), "
                        "has_function_privilege('grove_projection', "
                        "'grove_claim_run_command(text,text,text,double precision)', 'EXECUTE'), "
                        "has_function_privilege('public', "
                        "'grove_claim_run_command(text,text,text,double precision)', 'EXECUTE'), "
                        "has_function_privilege('public', "
                        "'grove_heartbeat_run_command(text,uuid,uuid,bigint,text,text,text,bigint,timestamp "
                        "with time zone,double precision)', 'EXECUTE'), "
                        "has_function_privilege('grove_runtime', "
                        "'grove_claim_run_command(text,text,text,double precision)', 'EXECUTE'), "
                        "has_column_privilege('grove_runtime', 'agent_run', 'execution_fence', 'UPDATE'), "
                        "has_function_privilege('grove_runtime', "
                        "'grove_heartbeat_run_command(text,uuid,uuid,bigint,text,text,text,bigint,timestamp "
                        "with time zone,double precision)', 'EXECUTE'), "
                        "has_column_privilege('grove_runtime', 'agent_run', 'tenant_id', 'UPDATE'), "
                        "has_function_privilege('grove_runtime', "
                        "'grove_consume_run_command(text,uuid,uuid,bigint,text,text,text,bigint,timestamp "
                        "with time zone)', 'EXECUTE'), "
                        "has_function_privilege('grove_runtime', "
                        "'grove_dead_letter_run_command(text,uuid,uuid,bigint,text,text,text,bigint,timestamp "
                        "with time zone,text)', 'EXECUTE'), "
                        "has_function_privilege('grove_projection', "
                        "'grove_reconcile_expired_run_command(text,uuid)', 'EXECUTE'), "
                        "has_function_privilege('public', "
                        "'grove_dead_letter_run_command(text,uuid,uuid,bigint,text,text,text,bigint,timestamp "
                        "with time zone,text)', 'EXECUTE'), "
                        "has_function_privilege('public', "
                        "'grove_reconcile_expired_run_command(text,uuid)', 'EXECUTE'), "
                        "has_table_privilege('grove_runtime', 'checkpoints', 'SELECT'), "
                        "has_table_privilege('grove_runtime', 'checkpoints', 'INSERT'), "
                        "has_table_privilege('grove_runtime', 'checkpoints', 'UPDATE'), "
                        "has_table_privilege('grove_api', 'checkpoints', 'SELECT'), "
                        "has_table_privilege('grove_governance', 'checkpoints', 'SELECT'), "
                        "has_table_privilege('grove_projection', 'checkpoints', 'SELECT'), "
                        "has_column_privilege('grove_api', 'command_payload', 'payload', 'SELECT'), "
                        "has_column_privilege('grove_runtime', 'command_payload', 'payload', 'SELECT'), "
                        "has_column_privilege('grove_projection', 'command_payload', 'payload', 'SELECT'), "
                        "has_column_privilege('grove_governance', 'command_payload', 'payload', 'SELECT'), "
                        "has_column_privilege('public', 'command_payload', 'payload', 'SELECT')"
                    )
                )
            ).one()
            assert privileges == (
                True,
                False,
                False,
                False,
                False,
                False,
                False,
                False,
                True,
                True,
                False,
                False,
                False,
                False,
                True,
                False,
                True,
                False,
                True,
                True,
                True,
                False,
                False,
                True,
                True,
                True,
                False,
                False,
                False,
                False,
                False,
                False,
                False,
                False,
            )
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fenced_checkpoint_and_consume_share_authoritative_claim_proof() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-checkpoint-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    owner_engine = create_async_engine(migration_url)
    driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
    raw_url = runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        claim = await driver.claim("checkpoint-worker", runtime_hash, tenant_id, 10)
        assert claim is not None and claim.run_id == handle.run_id
        with pytest.raises(RunStateConflict):
            await driver.consume(claim)

        config = cast(
            RunnableConfig,
            {
                "configurable": {
                    "thread_id": str(claim.run_id),
                    "checkpoint_ns": "",
                },
                "metadata": {"caller_tag": "from-config", "shared": "config"},
            },
        )
        checkpoint = empty_checkpoint()
        checkpoint["channel_versions"] = {"state": "1"}
        checkpoint["channel_values"] = {"state": {"answer": 42}}
        async with await psycopg.AsyncConnection.connect(raw_url) as raw_connection:
            saver = FencedPostgresSaver(raw_connection, claim)
            result_config = await saver.aput(
                config,
                checkpoint,
                cast(CheckpointMetadata, {"node": "test", "shared": "explicit"}),
                {"state": "1"},
            )
            loaded = await saver.aget_tuple(result_config)
            assert loaded is not None
            assert loaded.checkpoint["id"] == checkpoint["id"]
            loaded_metadata = cast(dict[str, object], loaded.metadata)
            assert loaded_metadata["applied_command_id"] == str(claim.command_id)
            assert loaded_metadata["claim_provenance_hash"]
            assert loaded_metadata["caller_tag"] == "from-config"
            assert loaded_metadata["shared"] == "explicit"

        async with owner_engine.connect() as connection:
            projection = (
                await connection.execute(
                    text(
                        "SELECT latest_checkpoint_id, latest_applied_command_seq "
                        "FROM agent_run WHERE tenant_id = :tenant AND run_id = :run_id"
                    ),
                    {"tenant": tenant_id, "run_id": claim.run_id},
                )
            ).one()
            assert projection[0] == checkpoint["id"]
            assert projection[1] == claim.command_seq

        receipt = await driver.consume(claim)
        assert receipt.status == "consumed"
        assert await driver.consume(claim) == receipt
    finally:
        await runtime_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dead_letter_and_expired_reconciliation_preserve_fences_and_attempts() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    projection_url = _role_url(api_url, "grove_projection", "grove_projection_ws0")
    tenant_id = f"it-ws3-dead-letter-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    projection_engine = create_async_engine(projection_url)
    owner_engine = create_async_engine(migration_url)
    runtime_factory = async_sessionmaker(runtime_engine, expire_on_commit=False)
    runtime_driver = PostgresExecutionDriver(runtime_factory)
    projection_driver = PostgresExecutionDriver(
        runtime_factory,
        reconciliation_session_factory=async_sessionmaker(projection_engine, expire_on_commit=False),
    )
    try:
        claim = await runtime_driver.claim("dead-letter-worker", runtime_hash, tenant_id, 10)
        assert claim is not None
        counts_before = await _checkpoint_counts(migration_url, tenant_id)
        receipt = await runtime_driver.dead_letter(claim, "provider-timeout")
        assert receipt.status == "dead_letter"
        assert await _checkpoint_counts(migration_url, tenant_id) == counts_before
        async with owner_engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT r.status, r.execution_fence, r.lease_owner, r.lease_until, "
                        "c.status, c.last_error_ref, c.execution_fence, c.lease_owner, c.lease_until "
                        "FROM agent_run r JOIN run_command c USING (tenant_id, run_id) "
                        "WHERE r.run_id = :run_id"
                    ),
                    {"run_id": handle.run_id},
                )
            ).one()
        assert row == (
            "running",
            claim.execution_fence,
            None,
            None,
            "dead_letter",
            "provider-timeout",
            None,
            None,
            None,
        )
        with pytest.raises(StaleExecutionFence):
            await runtime_driver.dead_letter(claim, "retry-after-dead-letter")

        retry_tenant = f"it-ws3-requeue-{uuid.uuid4().hex[:12]}"
        retry_handle, retry_hash = await _submit_start(api_url, migration_url, retry_tenant)
        retry_claim = await runtime_driver.claim("requeue-worker", retry_hash, retry_tenant, 30)
        assert retry_claim is not None
        assert await projection_driver.reconcile_expired(retry_tenant, retry_handle.run_id) is None
        async with owner_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE run_command SET available_at = clock_timestamp() - interval '1 second', "
                    "last_error_ref = 'prior-error' WHERE command_id = :command_id"
                ),
                {"command_id": retry_claim.command_id},
            )
        async with owner_engine.connect() as connection:
            before_retry = (
                await connection.execute(
                    text(
                        "SELECT available_at, last_error_ref, attempt_count, execution_fence "
                        "FROM run_command WHERE command_id = :command_id"
                    ),
                    {"command_id": retry_claim.command_id},
                )
            ).one()
        await _expire_claim(migration_url, retry_claim)
        requeued = await projection_driver.reconcile_expired(retry_tenant, retry_handle.run_id)
        assert requeued is not None and requeued.status == "pending"
        async with owner_engine.connect() as connection:
            after_retry = (
                await connection.execute(
                    text(
                        "SELECT c.status, c.available_at, c.last_error_ref, c.attempt_count, c.execution_fence, "
                        "r.execution_fence, r.lease_owner, r.lease_until "
                        "FROM run_command c JOIN agent_run r USING (tenant_id, run_id) "
                        "WHERE c.command_id = :command_id"
                    ),
                    {"command_id": retry_claim.command_id},
                )
            ).one()
        assert after_retry[:4] == ("pending", before_retry[0], before_retry[1], before_retry[2])
        assert after_retry[4:] == (None, retry_claim.execution_fence, None, None)
        replacement = await runtime_driver.claim("replacement-worker", retry_hash, retry_tenant, 10)
        assert replacement is not None
        assert replacement.command_id == retry_claim.command_id
        assert replacement.execution_fence == retry_claim.execution_fence + 1

        terminal_tenant = f"it-ws3-terminal-noop-{uuid.uuid4().hex[:12]}"
        terminal_handle, terminal_hash = await _submit_start(api_url, migration_url, terminal_tenant)
        terminal_claim = await runtime_driver.claim("terminal-worker", terminal_hash, terminal_tenant, 30)
        assert terminal_claim is not None
        terminal_expired_at = await _expire_claim(migration_url, terminal_claim)
        async with owner_engine.begin() as connection:
            await connection.execute(
                text("UPDATE agent_run SET status = 'succeeded' WHERE run_id = :run_id"),
                {"run_id": terminal_handle.run_id},
            )
        assert await projection_driver.reconcile_expired(terminal_tenant, terminal_handle.run_id) is None
        async with owner_engine.connect() as connection:
            terminal_state = (
                await connection.execute(
                    text(
                        "SELECT status, execution_fence, lease_owner, lease_until "
                        "FROM run_command WHERE command_id = :id"
                    ),
                    {"id": terminal_claim.command_id},
                )
            ).one()
        assert terminal_state == (
            "leased",
            terminal_claim.execution_fence,
            terminal_claim.worker_id,
            terminal_expired_at,
        )
    finally:
        await runtime_engine.dispose()
        await projection_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ("pristine", "current", "future"))
async def test_reconcile_cancel_requested_acceptance_matrix(mode: str) -> None:
    """A real cancel acceptance/claim path remains reconciliable by proof shape."""

    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    projection_url = _role_url(api_url, "grove_projection", "grove_projection_ws0")
    projection_raw_url = projection_url.replace("postgresql+psycopg://", "postgresql://", 1)
    tenant_id = f"it-ws3-cancel-reconcile-{mode}-{uuid.uuid4().hex[:10]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    api_engine = create_async_engine(api_url)
    projection_engine = create_async_engine(projection_url)
    runtime_factory = async_sessionmaker(runtime_engine, expire_on_commit=False)
    runtime_driver = PostgresExecutionDriver(runtime_factory)
    api_driver = PostgresExecutionDriver(
        runtime_factory,
        command_session_factory=async_sessionmaker(api_engine, expire_on_commit=False),
    )
    projection_driver = PostgresExecutionDriver(
        runtime_factory,
        reconciliation_session_factory=async_sessionmaker(projection_engine, expire_on_commit=False),
    )
    try:
        cancel = _cancel_command(tenant_id=tenant_id, run_id=handle.run_id, runtime_build_hash=runtime_hash)
        accepted = await api_driver.dispatch(cancel)
        assert accepted.status == "pending"
        claim = await runtime_driver.claim(
            f"cancel-reconcile-{mode}", runtime_hash, tenant_id, 0.5 if mode == "current" else 30
        )
        assert claim is not None and claim.command_id == cancel.command_id
        if mode == "current":
            checkpoint = empty_checkpoint()
            checkpoint["channel_versions"] = {"state": "cancel-current"}
            async with await psycopg.AsyncConnection.connect(
                runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
            ) as raw_connection:
                saver = FencedPostgresSaver(raw_connection, claim)
                await saver.aput(
                    {"configurable": {"thread_id": str(claim.run_id), "checkpoint_ns": ""}},
                    checkpoint,
                    cast(CheckpointMetadata, {}),
                    {"state": "cancel-current"},
                )
        if mode == "current":
            # Preserve the apply-time lease bytes in the checkpoint; natural
            # expiry is required for an exact-current proof.
            await asyncio.sleep(0.7)
        elif mode != "future":
            await _expire_claim(migration_url, claim)
        before = await _reconcile_snapshot(migration_url, tenant_id, handle.run_id)
        raw_result = await _raw_reconcile(projection_raw_url, tenant_id, handle.run_id)
        expected = {"pristine": "requeued", "current": "consumed", "future": "noop"}[mode]
        assert raw_result is not None and raw_result[0] == expected
        if mode == "future":
            assert await _reconcile_snapshot(migration_url, tenant_id, handle.run_id) == before
        else:
            reconciled = await projection_driver.reconcile_expired(tenant_id, handle.run_id)
            assert reconciled is None
    finally:
        await runtime_engine.dispose()
        await api_engine.dispose()
        await projection_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reconcile_cancel_requested_coherent_prior_uses_consumed_owner() -> None:
    """A cancel command may requeue only when its consumed prior proof is durable."""

    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    projection_url = _role_url(api_url, "grove_projection", "grove_projection_ws0")
    projection_raw_url = projection_url.replace("postgresql+psycopg://", "postgresql://", 1)
    tenant_id = f"it-ws3-cancel-prior-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    api_engine = create_async_engine(api_url)
    projection_engine = create_async_engine(projection_url)
    runtime_factory = async_sessionmaker(runtime_engine, expire_on_commit=False)
    runtime_driver = PostgresExecutionDriver(runtime_factory)
    api_driver = PostgresExecutionDriver(
        runtime_factory,
        command_session_factory=async_sessionmaker(api_engine, expire_on_commit=False),
    )
    try:
        start_claim = await runtime_driver.claim("cancel-prior-start", runtime_hash, tenant_id, 30)
        assert start_claim is not None
        checkpoint = empty_checkpoint()
        checkpoint["channel_versions"] = {"state": "cancel-prior"}
        async with await psycopg.AsyncConnection.connect(
            runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
        ) as raw_connection:
            saver = FencedPostgresSaver(raw_connection, start_claim)
            await saver.aput(
                {"configurable": {"thread_id": str(start_claim.run_id), "checkpoint_ns": ""}},
                checkpoint,
                cast(CheckpointMetadata, {}),
                {"state": "cancel-prior"},
            )
        assert (await runtime_driver.consume(start_claim)).status == "consumed"
        cancel = _cancel_command(tenant_id=tenant_id, run_id=handle.run_id, runtime_build_hash=runtime_hash)
        assert (await api_driver.dispatch(cancel)).status == "pending"
        cancel_claim = await runtime_driver.claim("cancel-prior-worker", runtime_hash, tenant_id, 30)
        assert cancel_claim is not None and cancel_claim.command_id == cancel.command_id
        await _expire_claim(migration_url, cancel_claim)
        result = await _raw_reconcile(projection_raw_url, tenant_id, handle.run_id)
        assert result is not None and result[0] == "requeued"
    finally:
        await runtime_engine.dispose()
        await api_engine.dispose()
        await projection_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reconcile_cancel_prior_command_uses_same_consumed_proof() -> None:
    """A consumed cancel prior has the same apply-time closure requirements."""

    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    projection_url = _role_url(api_url, "grove_projection", "grove_projection_ws0")
    projection_raw_url = projection_url.replace("postgresql+psycopg://", "postgresql://", 1)
    tenant_id = f"it-ws3-cancel-prior-cancel-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    api_engine = create_async_engine(api_url)
    projection_engine = create_async_engine(projection_url)
    runtime_factory = async_sessionmaker(runtime_engine, expire_on_commit=False)
    runtime_driver = PostgresExecutionDriver(runtime_factory)
    api_driver = PostgresExecutionDriver(
        runtime_factory,
        command_session_factory=async_sessionmaker(api_engine, expire_on_commit=False),
    )
    raw_url = runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        start_claim = await runtime_driver.claim("cancel-prior-start-2", runtime_hash, tenant_id, 30)
        assert start_claim is not None
        start_checkpoint = empty_checkpoint()
        start_checkpoint["channel_versions"] = {"state": "cancel-prior-start"}
        async with await psycopg.AsyncConnection.connect(raw_url) as raw_connection:
            saver = FencedPostgresSaver(raw_connection, start_claim)
            await saver.aput(
                {"configurable": {"thread_id": str(start_claim.run_id), "checkpoint_ns": ""}},
                start_checkpoint,
                cast(CheckpointMetadata, {}),
                {"state": "cancel-prior-start"},
            )
        assert (await runtime_driver.consume(start_claim)).status == "consumed"

        cancel = _cancel_command(tenant_id=tenant_id, run_id=handle.run_id, runtime_build_hash=runtime_hash)
        assert (await api_driver.dispatch(cancel)).status == "pending"
        prior_cancel = await runtime_driver.claim("cancel-prior-cancel", runtime_hash, tenant_id, 30)
        assert prior_cancel is not None and prior_cancel.command_id == cancel.command_id
        cancel_checkpoint = empty_checkpoint()
        cancel_checkpoint["channel_versions"] = {"state": "cancel-prior-cancel"}
        async with await psycopg.AsyncConnection.connect(raw_url) as raw_connection:
            saver = FencedPostgresSaver(raw_connection, prior_cancel)
            await saver.aput(
                {"configurable": {"thread_id": str(prior_cancel.run_id), "checkpoint_ns": ""}},
                cancel_checkpoint,
                cast(CheckpointMetadata, {}),
                {"state": "cancel-prior-cancel"},
            )
        assert (await runtime_driver.consume(prior_cancel)).status == "consumed"

        followup_id = uuid.uuid4()
        await _insert_followup_command(
            migration_url,
            tenant_id=tenant_id,
            run_id=handle.run_id,
            command_id=followup_id,
            command_seq=2,
            command_digest="d" * 64,
            command_type="cancel",
            command_schema_version="cancel.v1",
        )
        current = await runtime_driver.claim("cancel-prior-current", runtime_hash, tenant_id, 30)
        assert current is not None and current.command_id == followup_id
        await _expire_claim(migration_url, current)
        result = await _raw_reconcile(projection_raw_url, tenant_id, handle.run_id)
        assert result is not None and result[0] == "requeued"
    finally:
        await runtime_engine.dispose()
        await api_engine.dispose()
        await projection_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reconcile_cancel_requested_takeover_competes_with_projection() -> None:
    """Cancel takeover and reconciliation have one serialized durable owner."""

    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    projection_url = _role_url(api_url, "grove_projection", "grove_projection_ws0")
    projection_raw_url = projection_url.replace("postgresql+psycopg://", "postgresql://", 1)
    tenant_id = f"it-ws3-cancel-takeover-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    api_engine = create_async_engine(api_url)
    projection_engine = create_async_engine(projection_url)
    runtime_factory = async_sessionmaker(runtime_engine, expire_on_commit=False)
    runtime_driver = PostgresExecutionDriver(runtime_factory)
    api_driver = PostgresExecutionDriver(
        runtime_factory,
        command_session_factory=async_sessionmaker(api_engine, expire_on_commit=False),
    )
    try:
        cancel = _cancel_command(tenant_id=tenant_id, run_id=handle.run_id, runtime_build_hash=runtime_hash)
        await api_driver.dispatch(cancel)
        original = await runtime_driver.claim("cancel-takeover-original", runtime_hash, tenant_id, 30)
        assert original is not None
        await _expire_claim(migration_url, original)
        projection_task = _raw_reconcile(projection_raw_url, tenant_id, handle.run_id)
        raw_result, takeover = await asyncio.gather(
            projection_task,
            runtime_driver.claim("cancel-takeover-racer", runtime_hash, tenant_id, 30),
        )
        assert raw_result is not None and raw_result[0] in {"requeued", "noop"}
        if takeover is None:
            takeover = await runtime_driver.claim("cancel-takeover-retry", runtime_hash, tenant_id, 10)
        assert takeover is not None and takeover.command_id == original.command_id
        assert takeover.execution_fence > original.execution_fence
    finally:
        await runtime_engine.dispose()
        await api_engine.dispose()
        await projection_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_pair",
    ("accepted_start", "running_cancel", "cancel_requested_start", "waiting_start", "terminal_start", "running_resume"),
)
async def test_reconcile_invalid_lifecycle_type_pairs_are_manual_zero_write(invalid_pair: str) -> None:
    """Only running/start and cancel_requested/cancel are legal reconciliation pairs."""

    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    projection_url = _role_url(api_url, "grove_projection", "grove_projection_ws0")
    projection_raw_url = projection_url.replace("postgresql+psycopg://", "postgresql://", 1)
    tenant_id = f"it-ws3-invalid-pair-{invalid_pair}-{uuid.uuid4().hex[:10]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    owner_engine = create_async_engine(migration_url)
    runtime_driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
    try:
        claim = await runtime_driver.claim(f"invalid-pair-{invalid_pair}", runtime_hash, tenant_id, 30)
        assert claim is not None
        await _expire_claim(migration_url, claim)
        async with owner_engine.begin() as connection:
            if invalid_pair == "accepted_start":
                await connection.execute(
                    text("UPDATE agent_run SET status = 'accepted' WHERE tenant_id = :tenant AND run_id = :run_id"),
                    {"tenant": tenant_id, "run_id": handle.run_id},
                )
            elif invalid_pair in {"running_cancel", "running_resume"}:
                command_type = "cancel" if invalid_pair == "running_cancel" else "resume"
                command_schema_version = f"{command_type}.v1"
                payload_ref = f"fixture-invalid-{command_type}-{uuid.uuid4()}"
                payload_hash = "f" * 64
                await connection.execute(
                    text(
                        "INSERT INTO command_payload ("
                        "tenant_id, payload_ref, payload_hash, command_schema_version, sensitivity, retention, payload"
                        ") VALUES (:tenant, :payload_ref, :payload_hash, :schema, 'sensitive', "
                        "'run_completion', '{}'::jsonb)"
                    ),
                    {
                        "tenant": tenant_id,
                        "payload_ref": payload_ref,
                        "payload_hash": payload_hash,
                        "schema": command_schema_version,
                    },
                )
                await connection.execute(
                    text(
                        "UPDATE run_command SET command_type = :command_type, command_schema_version = :schema, "
                        "payload_ref = :payload_ref, payload_hash = :payload_hash "
                        "WHERE tenant_id = :tenant AND command_id = :id"
                    ),
                    {
                        "command_type": command_type,
                        "schema": command_schema_version,
                        "payload_ref": payload_ref,
                        "payload_hash": payload_hash,
                        "tenant": tenant_id,
                        "id": claim.command_id,
                    },
                )
            elif invalid_pair == "cancel_requested_start":
                await connection.execute(
                    text(
                        "UPDATE agent_run SET status = 'cancel_requested' "
                        "WHERE tenant_id = :tenant AND run_id = :run_id"
                    ),
                    {"tenant": tenant_id, "run_id": handle.run_id},
                )
            elif invalid_pair == "waiting_start":
                await connection.execute(
                    text(
                        "UPDATE agent_run SET status = 'waiting_user_input' "
                        "WHERE tenant_id = :tenant AND run_id = :run_id"
                    ),
                    {"tenant": tenant_id, "run_id": handle.run_id},
                )
            elif invalid_pair == "terminal_start":
                await connection.execute(
                    text("UPDATE agent_run SET status = 'succeeded' WHERE tenant_id = :tenant AND run_id = :run_id"),
                    {"tenant": tenant_id, "run_id": handle.run_id},
                )
            else:
                raise AssertionError(f"unhandled lifecycle/type pair: {invalid_pair}")
        before = await _reconcile_snapshot(migration_url, tenant_id, handle.run_id)
        result = await _raw_reconcile(projection_raw_url, tenant_id, handle.run_id)
        assert result is not None and result[0] == "manual"
        assert await _reconcile_snapshot(migration_url, tenant_id, handle.run_id) == before
    finally:
        await runtime_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reconcile_multiple_leased_rows_is_manual_and_zero_write() -> None:
    """A run with more than one leased row cannot be requeued by a one-row proof."""

    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    projection_url = _role_url(api_url, "grove_projection", "grove_projection_ws0")
    projection_raw_url = projection_url.replace("postgresql+psycopg://", "postgresql://", 1)
    tenant_id = f"it-ws3-reconcile-multiple-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    owner_engine = create_async_engine(migration_url)
    driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
    followup_id = uuid.uuid4()
    try:
        claim = await driver.claim("multiple-lease-worker", runtime_hash, tenant_id, 30)
        assert claim is not None
        await _insert_followup_command(
            migration_url,
            tenant_id=tenant_id,
            run_id=handle.run_id,
            command_id=followup_id,
            command_seq=1,
            command_digest="m" * 64,
        )
        expired_at = await _expire_claim(migration_url, claim)
        async with owner_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE run_command SET status = 'leased', lease_owner = :worker, lease_until = :expired, "
                    "execution_fence = :fence WHERE tenant_id = :tenant AND command_id = :command_id"
                ),
                {
                    "tenant": tenant_id,
                    "command_id": followup_id,
                    "worker": claim.worker_id,
                    "expired": expired_at,
                    "fence": claim.execution_fence,
                },
            )
        before = await _reconcile_snapshot(migration_url, tenant_id, handle.run_id)
        result = await _raw_reconcile(projection_raw_url, tenant_id, handle.run_id)
        assert result is not None and result[0] == "manual"
        assert await _reconcile_snapshot(migration_url, tenant_id, handle.run_id) == before
    finally:
        await runtime_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_expired_reconciliation_consumes_checkpoint_proof_without_reapplying() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    projection_url = _role_url(api_url, "grove_projection", "grove_projection_ws0")
    tenant_id = f"it-ws3-reconcile-proof-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    projection_engine = create_async_engine(projection_url)
    owner_engine = create_async_engine(migration_url)
    runtime_factory = async_sessionmaker(runtime_engine, expire_on_commit=False)
    runtime_driver = PostgresExecutionDriver(runtime_factory)
    projection_driver = PostgresExecutionDriver(
        runtime_factory,
        reconciliation_session_factory=async_sessionmaker(projection_engine, expire_on_commit=False),
    )
    raw_url = runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        claim = await runtime_driver.claim("crash-window-worker", runtime_hash, tenant_id, 0.5)
        assert claim is not None and claim.run_id == handle.run_id
        checkpoint = empty_checkpoint()
        checkpoint["channel_versions"] = {"state": "proof-v1"}
        checkpoint["channel_values"] = {"state": {"answer": 7}}
        async with await psycopg.AsyncConnection.connect(raw_url) as raw_connection:
            saver = FencedPostgresSaver(raw_connection, claim)
            await saver.aput(
                {"configurable": {"thread_id": str(claim.run_id), "checkpoint_ns": ""}},
                checkpoint,
                cast(CheckpointMetadata, {}),
                {"state": "proof-v1"},
            )
        counts_after_checkpoint = await _checkpoint_counts(migration_url, tenant_id)
        with pytest.raises(RunStateConflict, match="checkpoint proof"):
            await runtime_driver.dead_letter(claim, "must-not-dead-letter-applied")
        await asyncio.sleep(0.7)
        reconciled = await projection_driver.reconcile_expired(tenant_id, claim.run_id)
        assert reconciled is not None and reconciled.status == "consumed"
        assert await _checkpoint_counts(migration_url, tenant_id) == counts_after_checkpoint
        assert await projection_driver.reconcile_expired(tenant_id, claim.run_id) is None
        async with owner_engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT c.status, c.consumed_provenance_kind, c.consumed_worker_id, "
                        "c.consumed_execution_fence, "
                        "c.consumed_lease_until, c.consumed_claim_provenance_hash, "
                        "grove_checkpoint_claim_provenance(c.tenant_id, c.run_id, c.command_id, "
                        "c.command_seq, c.command_digest, r.runtime_build_hash, c.consumed_worker_id, "
                        "c.consumed_execution_fence, c.consumed_lease_until), "
                        "r.latest_checkpoint_id, r.latest_applied_command_id, r.latest_applied_command_seq "
                        "FROM run_command c JOIN agent_run r USING (tenant_id, run_id) "
                        "WHERE c.command_id = :command_id"
                    ),
                    {"command_id": claim.command_id},
                )
            ).one()
        assert row[0:5] == ("consumed", "claim.v1", claim.worker_id, claim.execution_fence, claim.lease_until)
        assert isinstance(row[5], str) and len(row[5]) == 64 and row[5] == row[6]
        assert row[7:] == (checkpoint["id"], claim.command_id, claim.command_seq)
    finally:
        await runtime_engine.dispose()
        await projection_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_expired_reconciliation_requeues_after_coherent_prior_checkpoint() -> None:
    """A durable lower-sequence proof permits requeue without rewriting high-water state."""

    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    projection_url = _role_url(api_url, "grove_projection", "grove_projection_ws0")
    projection_raw_url = projection_url.replace("postgresql+psycopg://", "postgresql://", 1)
    tenant_id = f"it-ws3-reconcile-prior-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    owner_engine = create_async_engine(migration_url)
    runtime_driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
    raw_url = runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
    followup_id = uuid.uuid4()
    try:
        first_claim = await runtime_driver.claim("prior-proof-worker", runtime_hash, tenant_id, 30)
        assert first_claim is not None and first_claim.command_seq == 0
        checkpoint = empty_checkpoint()
        checkpoint["channel_versions"] = {"state": "prior-proof"}
        checkpoint["channel_values"] = {"state": {"answer": 11}}
        async with await psycopg.AsyncConnection.connect(raw_url) as raw_connection:
            saver = FencedPostgresSaver(raw_connection, first_claim)
            await saver.aput(
                {"configurable": {"thread_id": str(first_claim.run_id), "checkpoint_ns": ""}},
                checkpoint,
                cast(CheckpointMetadata, {}),
                {"state": "prior-proof"},
            )
        consumed = await runtime_driver.consume(first_claim)
        assert consumed.status == "consumed"
        await _insert_followup_command(
            migration_url,
            tenant_id=tenant_id,
            run_id=handle.run_id,
            command_id=followup_id,
            command_seq=1,
            command_digest="c" * 64,
        )
        second_claim = await runtime_driver.claim("prior-proof-worker-2", runtime_hash, tenant_id, 30)
        assert second_claim is not None and second_claim.command_seq == 1
        await _expire_claim(migration_url, second_claim)
        before = await _reconcile_snapshot(migration_url, tenant_id, handle.run_id)
        result = await _raw_reconcile(projection_raw_url, tenant_id, handle.run_id)
        assert result is not None and result[0] == "requeued" and result[1] == second_claim.command_id
        after = await _reconcile_snapshot(migration_url, tenant_id, handle.run_id)
        assert after[0][5:9] == (
            checkpoint["id"],
            first_claim.command_id,
            first_claim.command_seq,
            first_claim.command_digest,
        )
        assert after[1][0][2] == "consumed"
        assert after[1][1][2] == "pending"
        assert after[2] == before[2]
    finally:
        await runtime_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    ("pending", "dead_letter", "leased", "worker", "fence", "lease", "provenance", "id", "seq", "digest"),
)
async def test_reconcile_prior_proof_requires_consumed_command_closure(mutation: str) -> None:
    """A self-consistent physical checkpoint cannot replace consumed command ownership."""

    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    projection_url = _role_url(api_url, "grove_projection", "grove_projection_ws0")
    projection_raw_url = projection_url.replace("postgresql+psycopg://", "postgresql://", 1)
    tenant_id = f"it-ws3-prior-closure-{mutation}-{uuid.uuid4().hex[:10]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    owner_engine = create_async_engine(migration_url)
    runtime_driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
    raw_url = runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        first_claim = await runtime_driver.claim(f"prior-closure-{mutation}", runtime_hash, tenant_id, 30)
        assert first_claim is not None
        checkpoint = empty_checkpoint()
        checkpoint["channel_versions"] = {"state": "prior-closure"}
        async with await psycopg.AsyncConnection.connect(raw_url) as raw_connection:
            saver = FencedPostgresSaver(raw_connection, first_claim)
            await saver.aput(
                {"configurable": {"thread_id": str(first_claim.run_id), "checkpoint_ns": ""}},
                checkpoint,
                cast(CheckpointMetadata, {}),
                {"state": "prior-closure"},
            )
        assert (await runtime_driver.consume(first_claim)).status == "consumed"
        current_id = uuid.uuid4()
        await _insert_followup_command(
            migration_url,
            tenant_id=tenant_id,
            run_id=handle.run_id,
            command_id=current_id,
            command_seq=1,
            command_digest="c" * 64,
        )
        current_claim = await runtime_driver.claim(f"current-closure-{mutation}", runtime_hash, tenant_id, 30)
        assert current_claim is not None and current_claim.command_seq == 1
        expired_at = await _expire_claim(migration_url, current_claim)
        async with owner_engine.begin() as connection:
            if mutation in {"pending", "dead_letter"}:
                await connection.execute(
                    text(
                        "UPDATE run_command SET status = :status, lease_owner = NULL, lease_until = NULL, "
                        "execution_fence = NULL, consumed_provenance_kind = NULL, consumed_worker_id = NULL, "
                        "consumed_execution_fence = NULL, "
                        "consumed_lease_until = NULL, consumed_claim_provenance_hash = NULL "
                        "WHERE tenant_id = :tenant AND command_id = :command_id"
                    ),
                    {"status": mutation, "tenant": tenant_id, "command_id": first_claim.command_id},
                )
            elif mutation == "leased":
                await connection.execute(
                    text(
                        "UPDATE run_command SET status = 'leased', lease_owner = 'prior-mutant', "
                        "lease_until = :expired, execution_fence = :fence, consumed_provenance_kind = NULL, "
                        "consumed_worker_id = NULL, "
                        "consumed_execution_fence = NULL, consumed_lease_until = NULL, "
                        "consumed_claim_provenance_hash = NULL WHERE tenant_id = :tenant AND command_id = :command_id"
                    ),
                    {
                        "expired": expired_at,
                        "fence": first_claim.execution_fence + 10,
                        "tenant": tenant_id,
                        "command_id": first_claim.command_id,
                    },
                )
            elif mutation == "worker":
                await connection.execute(
                    text(
                        "UPDATE run_command SET consumed_worker_id = 'prior-mutant' "
                        "WHERE tenant_id = :tenant AND command_id = :command_id"
                    ),
                    {"tenant": tenant_id, "command_id": first_claim.command_id},
                )
            elif mutation == "fence":
                await connection.execute(
                    text(
                        "UPDATE run_command SET consumed_execution_fence = :fence "
                        "WHERE tenant_id = :tenant AND command_id = :command_id"
                    ),
                    {
                        "fence": first_claim.execution_fence + 1,
                        "tenant": tenant_id,
                        "command_id": first_claim.command_id,
                    },
                )
            elif mutation == "lease":
                await connection.execute(
                    text(
                        "UPDATE run_command SET consumed_lease_until = :lease "
                        "WHERE tenant_id = :tenant AND command_id = :command_id"
                    ),
                    {
                        "lease": first_claim.lease_until + timedelta(seconds=1),
                        "tenant": tenant_id,
                        "command_id": first_claim.command_id,
                    },
                )
            elif mutation == "provenance":
                await connection.execute(
                    text(
                        "UPDATE run_command SET consumed_claim_provenance_hash = :provenance "
                        "WHERE tenant_id = :tenant AND command_id = :command_id"
                    ),
                    {
                        "provenance": "f" * 64,
                        "tenant": tenant_id,
                        "command_id": first_claim.command_id,
                    },
                )
            elif mutation == "id":
                await connection.execute(
                    text("UPDATE run_command SET command_id = :new_id WHERE command_id = :old_id"),
                    {"new_id": uuid.uuid4(), "old_id": first_claim.command_id},
                )
            elif mutation == "seq":
                await connection.execute(
                    text("UPDATE run_command SET command_seq = 2 WHERE command_id = :command_id"),
                    {"command_id": first_claim.command_id},
                )
            elif mutation == "digest":
                await connection.execute(
                    text("UPDATE run_command SET command_digest = :digest WHERE command_id = :command_id"),
                    {"digest": "e" * 64, "command_id": first_claim.command_id},
                )
            else:
                raise AssertionError(f"unhandled prior proof mutation: {mutation}")
        before = await _reconcile_snapshot(migration_url, tenant_id, handle.run_id)
        result = await _raw_reconcile(projection_raw_url, tenant_id, handle.run_id)
        assert result is not None and result[0] == "manual"
        assert await _reconcile_snapshot(migration_url, tenant_id, handle.run_id) == before
    finally:
        await runtime_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    (
        "projection_only_current",
        "projection_only_higher",
        "partial_pointer",
        "missing_physical",
        "forged_physical",
        "superseded",
        "terminal",
        "waiting_user_input",
        "cancel_requested",
        "partial_lease",
    ),
)
async def test_reconcile_invalid_proof_closure_cases_are_manual_zero_write(case: str) -> None:
    """Every ambiguous projection/lease shape is a stable manual zero-write result."""

    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    projection_url = _role_url(api_url, "grove_projection", "grove_projection_ws0")
    projection_raw_url = projection_url.replace("postgresql+psycopg://", "postgresql://", 1)
    tenant_id = f"it-ws3-reconcile-{case}-{uuid.uuid4().hex[:10]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    owner_engine = create_async_engine(migration_url)
    driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
    followup_id = uuid.uuid4()
    followup_digest = "b" * 64
    try:
        if case in {"projection_only_higher", "superseded"}:
            await _insert_followup_command(
                migration_url,
                tenant_id=tenant_id,
                run_id=handle.run_id,
                command_id=followup_id,
                command_seq=1,
                command_digest=followup_digest,
            )
        claim = await driver.claim(f"invalid-{case}", runtime_hash, tenant_id, 30)
        assert claim is not None
        if case == "forged_physical":
            raw_url = runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
            checkpoint = empty_checkpoint()
            checkpoint["id"] = "forged-physical"
            checkpoint["channel_versions"] = {"state": "forged"}
            async with await psycopg.AsyncConnection.connect(raw_url) as raw_connection:
                saver = FencedPostgresSaver(raw_connection, claim)
                await saver.aput(
                    {"configurable": {"thread_id": str(claim.run_id), "checkpoint_ns": ""}},
                    checkpoint,
                    cast(CheckpointMetadata, {}),
                    {"state": "forged"},
                )
        expired_at = await _expire_claim(migration_url, claim)
        async with owner_engine.begin() as connection:
            if case == "forged_physical":
                await connection.execute(text("ALTER TABLE checkpoints DISABLE TRIGGER checkpoints_tenant_guard"))
                await connection.execute(text("ALTER TABLE checkpoints DISABLE TRIGGER checkpoints_authority_guard"))
                await connection.execute(text("ALTER TABLE checkpoints DISABLE TRIGGER checkpoints_physical_guard"))
                await connection.execute(
                    text(
                        "UPDATE checkpoints SET claim_lease_until = :expired, claim_provenance_hash = :forged "
                        "WHERE tenant_id = :tenant AND checkpoint_id = :checkpoint"
                    ),
                    {
                        "expired": expired_at,
                        "forged": "f" * 64,
                        "tenant": tenant_id,
                        "checkpoint": checkpoint["id"],
                    },
                )
                await connection.execute(text("ALTER TABLE checkpoints ENABLE TRIGGER checkpoints_authority_guard"))
                await connection.execute(text("ALTER TABLE checkpoints ENABLE TRIGGER checkpoints_physical_guard"))
                await connection.execute(text("ALTER TABLE checkpoints ENABLE TRIGGER checkpoints_tenant_guard"))
            elif case == "projection_only_current":
                await connection.execute(
                    text(
                        "UPDATE agent_run SET latest_checkpoint_id = 'projection-only-current', "
                        "latest_applied_command_id = :command_id, latest_applied_command_seq = :seq, "
                        "latest_applied_command_digest = :digest WHERE tenant_id = :tenant AND run_id = :run_id"
                    ),
                    {
                        "tenant": tenant_id,
                        "run_id": claim.run_id,
                        "command_id": claim.command_id,
                        "seq": claim.command_seq,
                        "digest": claim.command_digest,
                    },
                )
            elif case == "projection_only_higher":
                await connection.execute(
                    text(
                        "UPDATE agent_run SET latest_checkpoint_id = 'projection-only-higher', "
                        "latest_applied_command_id = :command_id, latest_applied_command_seq = 1, "
                        "latest_applied_command_digest = :digest WHERE tenant_id = :tenant AND run_id = :run_id"
                    ),
                    {
                        "tenant": tenant_id,
                        "run_id": claim.run_id,
                        "command_id": followup_id,
                        "digest": followup_digest,
                    },
                )
            elif case == "partial_pointer":
                await connection.execute(
                    text(
                        "UPDATE agent_run SET latest_checkpoint_id = 'partial-pointer', "
                        "latest_applied_command_id = NULL, latest_applied_command_seq = NULL, "
                        "latest_applied_command_digest = NULL WHERE tenant_id = :tenant AND run_id = :run_id"
                    ),
                    {"tenant": tenant_id, "run_id": claim.run_id},
                )
            elif case == "missing_physical":
                await connection.execute(
                    text(
                        "UPDATE agent_run SET latest_checkpoint_id = 'missing-physical', "
                        "latest_applied_command_id = :command_id, latest_applied_command_seq = :seq, "
                        "latest_applied_command_digest = :digest WHERE tenant_id = :tenant AND run_id = :run_id"
                    ),
                    {
                        "tenant": tenant_id,
                        "run_id": claim.run_id,
                        "command_id": claim.command_id,
                        "seq": claim.command_seq,
                        "digest": claim.command_digest,
                    },
                )
            elif case == "superseded":
                await connection.execute(
                    text(
                        "UPDATE run_command SET superseded_by_command_id = :superseded, "
                        "superseded_by_command_seq = 1, superseded_by_command_digest = :digest, "
                        "superseded_by_provenance_hash = :provenance "
                        "WHERE tenant_id = :tenant AND command_id = :command_id"
                    ),
                    {
                        "tenant": tenant_id,
                        "command_id": claim.command_id,
                        "superseded": followup_id,
                        "digest": followup_digest,
                        "provenance": "f" * 64,
                    },
                )
            elif case in {"terminal", "waiting_user_input", "cancel_requested"}:
                status = {
                    "terminal": "succeeded",
                    "waiting_user_input": "waiting_user_input",
                    "cancel_requested": "cancel_requested",
                }[case]
                await connection.execute(
                    text("UPDATE agent_run SET status = :status WHERE tenant_id = :tenant AND run_id = :run_id"),
                    {"status": status, "tenant": tenant_id, "run_id": claim.run_id},
                )
            elif case == "partial_lease":
                await connection.execute(
                    text("UPDATE agent_run SET lease_owner = NULL WHERE tenant_id = :tenant AND run_id = :run_id"),
                    {"tenant": tenant_id, "run_id": claim.run_id},
                )
            else:
                raise AssertionError(f"unhandled reconciliation case: {case}")
        if case == "forged_physical":
            # Keep the exact projection pointer created by the saver; only the
            # physical provenance is forged, so the case must remain manual.
            assert expired_at < datetime.now(UTC)
        before = await _reconcile_snapshot(migration_url, tenant_id, claim.run_id)
        result = await _raw_reconcile(projection_raw_url, tenant_id, claim.run_id)
        assert result is not None and result[0] == "manual"
        assert await _reconcile_snapshot(migration_url, tenant_id, claim.run_id) == before
    finally:
        await runtime_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_expired_reconciliation_concurrent_projection_workers_have_one_winner() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    projection_url = _role_url(api_url, "grove_projection", "grove_projection_ws0")
    tenant_id = f"it-ws3-reconcile-race-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    projection_engine = create_async_engine(projection_url)
    runtime_driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
    projection_factory = async_sessionmaker(projection_engine, expire_on_commit=False)
    projection_a = PostgresExecutionDriver(
        async_sessionmaker(runtime_engine, expire_on_commit=False),
        reconciliation_session_factory=projection_factory,
    )
    projection_b = PostgresExecutionDriver(
        async_sessionmaker(runtime_engine, expire_on_commit=False),
        reconciliation_session_factory=projection_factory,
    )
    try:
        claim = await runtime_driver.claim("race-worker", runtime_hash, tenant_id, 30)
        assert claim is not None and claim.run_id == handle.run_id
        await _expire_claim(migration_url, claim)
        outcomes = await asyncio.gather(
            projection_a.reconcile_expired(tenant_id, claim.run_id),
            projection_b.reconcile_expired(tenant_id, claim.run_id),
        )
        assert sum(outcome is not None and outcome.status == "pending" for outcome in outcomes) == 1
        assert sum(outcome is None for outcome in outcomes) == 1
    finally:
        await runtime_engine.dispose()
        await projection_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_expired_reconciliation_concurrent_takeover_has_one_durable_owner() -> None:
    """Projection requeue and runtime takeover serialize on the run lock."""

    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    projection_url = _role_url(api_url, "grove_projection", "grove_projection_ws0")
    tenant_id = f"it-ws3-reconcile-takeover-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    projection_engine = create_async_engine(projection_url)
    runtime_factory = async_sessionmaker(runtime_engine, expire_on_commit=False)
    runtime_driver = PostgresExecutionDriver(runtime_factory)
    projection_driver = PostgresExecutionDriver(
        runtime_factory,
        reconciliation_session_factory=async_sessionmaker(projection_engine, expire_on_commit=False),
    )
    try:
        original = await runtime_driver.claim("takeover-before-reconcile", runtime_hash, tenant_id, 30)
        assert original is not None
        await _expire_claim(migration_url, original)
        reconciled, takeover = await asyncio.gather(
            projection_driver.reconcile_expired(tenant_id, handle.run_id),
            runtime_driver.claim("takeover-racer", runtime_hash, tenant_id, 30),
        )
        assert reconciled is None or reconciled.status == "pending"
        if takeover is None:
            takeover = await runtime_driver.claim("takeover-retry", runtime_hash, tenant_id, 10)
        assert takeover is not None
        assert takeover.command_id == original.command_id
        assert takeover.execution_fence > original.execution_fence
        state = await _reconcile_snapshot(migration_url, tenant_id, handle.run_id)
        assert state[0][3] == takeover.worker_id
        assert state[0][4] == takeover.lease_until
        leased_rows = [row for row in state[1] if row[2] == "leased"]
        assert len(leased_rows) == 1
        assert leased_rows[0][0] == original.command_id
        assert leased_rows[0][4] == takeover.worker_id
        assert leased_rows[0][6] == takeover.execution_fence
    finally:
        await runtime_engine.dispose()
        await projection_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dead_letter_and_reconcile_are_cross_tenant_and_role_scoped() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    projection_url = _role_url(api_url, "grove_projection", "grove_projection_ws0")
    tenant_a = f"it-ws3-role-a-{uuid.uuid4().hex[:12]}"
    tenant_b = f"it-ws3-role-b-{uuid.uuid4().hex[:12]}"
    handle_a, runtime_hash_a = await _submit_start(api_url, migration_url, tenant_a)
    handle_b, runtime_hash_b = await _submit_start(api_url, migration_url, tenant_b)
    runtime_engine = create_async_engine(runtime_url)
    projection_engine = create_async_engine(projection_url)
    owner_engine = create_async_engine(migration_url)
    runtime_driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
    projection_driver = PostgresExecutionDriver(
        async_sessionmaker(runtime_engine, expire_on_commit=False),
        reconciliation_session_factory=async_sessionmaker(projection_engine, expire_on_commit=False),
    )
    try:
        claim_a = await runtime_driver.claim("cross-tenant-worker", runtime_hash_a, tenant_a, 30)
        assert claim_a is not None and claim_a.run_id == handle_a.run_id
        forged = claim_a.model_copy(update={"tenant_id": tenant_b})
        before = await _lease_state(migration_url, handle_a.run_id)
        with pytest.raises(StaleExecutionFence):
            await runtime_driver.dead_letter(forged, "cross-tenant")
        assert await _lease_state(migration_url, handle_a.run_id) == before
        assert await projection_driver.reconcile_expired(tenant_b, handle_a.run_id) is None

        runtime_raw_url = runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
        projection_raw_url = projection_url.replace("postgresql+psycopg://", "postgresql://", 1)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            async with await psycopg.AsyncConnection.connect(runtime_raw_url) as connection:
                await connection.execute("SELECT set_config('grove.tenant_id', %s, true)", (tenant_a,))
                await connection.execute(
                    "SELECT * FROM grove_reconcile_expired_run_command(%s, %s)",
                    (tenant_a, handle_a.run_id),
                )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            async with await psycopg.AsyncConnection.connect(projection_raw_url) as connection:
                await connection.execute("SELECT set_config('grove.tenant_id', %s, true)", (tenant_b,))
                await connection.execute(
                    "SELECT * FROM grove_dead_letter_run_command(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        tenant_b,
                        handle_b.run_id,
                        uuid.uuid4(),
                        0,
                        "a" * 64,
                        runtime_hash_b,
                        "forged",
                        1,
                        datetime.now(UTC) + timedelta(seconds=30),
                        "role-bypass",
                    ),
                )
        owner_before = await _lease_state(migration_url, handle_b.run_id)
        assert owner_before[0][0] == 0
    finally:
        await runtime_engine.dispose()
        await projection_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lock_order_dead_letter_vs_checkpoint_write_has_one_authority() -> None:
    """The run-first checkpoint guard must serialize safely with dead-letter."""

    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    runtime_raw_url = runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
    migration_raw_url = migration_url.replace("postgresql+psycopg://", "postgresql://", 1)
    tenant_id = f"it-ws3-lock-checkpoint-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    owner_engine = create_async_engine(migration_url)
    holder: psycopg.AsyncConnection | None = None
    saver_connection: psycopg.AsyncConnection | None = None
    dead_connection: psycopg.AsyncConnection | None = None
    saver_task: asyncio.Task[object] | None = None
    dead_task: asyncio.Task[object] | None = None
    try:
        driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
        claim = await driver.claim("lock-checkpoint-worker", runtime_hash, tenant_id, 30)
        assert claim is not None and claim.run_id == handle.run_id
        before_counts = await _checkpoint_counts(migration_url, tenant_id)

        holder = await psycopg.AsyncConnection.connect(migration_raw_url)
        await holder.execute("BEGIN")
        await holder.execute(
            "SELECT 1 FROM run_command WHERE tenant_id = %s AND command_id = %s FOR UPDATE",
            (tenant_id, claim.command_id),
        )

        saver_connection = await psycopg.AsyncConnection.connect(runtime_raw_url)
        await saver_connection.execute("SET lock_timeout = '5000ms'")
        await saver_connection.execute("SET statement_timeout = '5000ms'")
        await saver_connection.commit()
        saver_pid_row = await (await saver_connection.execute("SELECT pg_backend_pid()")).fetchone()
        assert saver_pid_row is not None
        saver_pid = int(saver_pid_row[0])
        await saver_connection.commit()
        saver_task = asyncio.create_task(_checkpoint_lock_order_write(saver_connection, claim))
        await _wait_for_backend_lock(migration_url, saver_pid)

        dead_connection = await _raw_scoped_transaction(runtime_raw_url, tenant_id)
        dead_pid_row = await (await dead_connection.execute("SELECT pg_backend_pid()")).fetchone()
        assert dead_pid_row is not None
        dead_pid = int(dead_pid_row[0])
        dead_task = asyncio.create_task(
            _raw_call(
                dead_connection,
                "SELECT * FROM grove_dead_letter_run_command(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                _dead_letter_args(claim, "checkpoint-race"),
            )
        )
        await _wait_for_backend_lock(migration_url, dead_pid)

        await holder.rollback()
        await holder.close()
        holder = None
        results = await asyncio.wait_for(
            asyncio.gather(saver_task, dead_task, return_exceptions=True),
            timeout=4,
        )
        assert not any(isinstance(result, BaseException) for result in results), results
        assert results[0][0] == "checkpoint"
        assert results[1][0] == "applied"

        after_counts = await _checkpoint_counts(migration_url, tenant_id)
        assert after_counts[0] == before_counts[0] + 1
        assert after_counts[1] == before_counts[1] + 1
        assert after_counts[2] == before_counts[2]
        async with owner_engine.connect() as connection:
            state = (
                await connection.execute(
                    text(
                        "SELECT c.status, r.lease_owner, c.lease_owner "
                        "FROM run_command AS c JOIN agent_run AS r USING (tenant_id, run_id) "
                        "WHERE c.command_id = :command_id"
                    ),
                    {"command_id": claim.command_id},
                )
            ).one()
        assert state == ("leased", claim.worker_id, claim.worker_id)
    finally:
        if saver_task is not None and not saver_task.done():
            saver_task.cancel()
        if dead_task is not None and not dead_task.done():
            dead_task.cancel()
        if saver_task is not None or dead_task is not None:
            await asyncio.gather(
                *(task for task in (saver_task, dead_task) if task is not None), return_exceptions=True
            )
        if holder is not None:
            await holder.rollback()
            await holder.close()
        if saver_connection is not None:
            await saver_connection.close()
        if dead_connection is not None:
            await dead_connection.close()
        await runtime_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lock_order_dead_letter_vs_consume_has_one_authority() -> None:
    """Consume and dead-letter must serialize on run before command."""

    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    runtime_raw_url = runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
    migration_raw_url = migration_url.replace("postgresql+psycopg://", "postgresql://", 1)
    tenant_id = f"it-ws3-lock-consume-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    owner_engine = create_async_engine(migration_url)
    holder: psycopg.AsyncConnection | None = None
    consume_connection: psycopg.AsyncConnection | None = None
    dead_connection: psycopg.AsyncConnection | None = None
    consume_task: asyncio.Task[object] | None = None
    dead_task: asyncio.Task[object] | None = None
    try:
        driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
        claim = await driver.claim("lock-consume-worker", runtime_hash, tenant_id, 30)
        assert claim is not None and claim.run_id == handle.run_id

        holder = await psycopg.AsyncConnection.connect(migration_raw_url)
        await holder.execute("BEGIN")
        await holder.execute(
            "SELECT 1 FROM run_command WHERE tenant_id = %s AND command_id = %s FOR UPDATE",
            (tenant_id, claim.command_id),
        )

        consume_connection = await _raw_scoped_transaction(runtime_raw_url, tenant_id)
        consume_pid_row = await (await consume_connection.execute("SELECT pg_backend_pid()")).fetchone()
        assert consume_pid_row is not None
        consume_pid = int(consume_pid_row[0])
        consume_task = asyncio.create_task(
            _raw_call(
                consume_connection,
                "SELECT * FROM grove_consume_run_command(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                _consume_args(claim),
            )
        )
        await _wait_for_backend_lock(migration_url, consume_pid)

        dead_connection = await _raw_scoped_transaction(runtime_raw_url, tenant_id)
        dead_pid_row = await (await dead_connection.execute("SELECT pg_backend_pid()")).fetchone()
        assert dead_pid_row is not None
        dead_pid = int(dead_pid_row[0])
        dead_task = asyncio.create_task(
            _raw_call(
                dead_connection,
                "SELECT * FROM grove_dead_letter_run_command(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                _dead_letter_args(claim, "consume-race"),
            )
        )
        await _wait_for_backend_lock(migration_url, dead_pid)

        await holder.rollback()
        await holder.close()
        holder = None
        results = await asyncio.wait_for(
            asyncio.gather(consume_task, dead_task, return_exceptions=True),
            timeout=4,
        )
        assert not any(isinstance(result, BaseException) for result in results), results
        assert results[0][0] == "no_proof"  # type: ignore[index]
        assert results[1][0] == "dead_letter"  # type: ignore[index]
        async with owner_engine.connect() as connection:
            state = (
                await connection.execute(
                    text(
                        "SELECT c.status, r.lease_owner, c.lease_owner "
                        "FROM run_command AS c JOIN agent_run AS r USING (tenant_id, run_id) "
                        "WHERE c.command_id = :command_id"
                    ),
                    {"command_id": claim.command_id},
                )
            ).one()
        assert state == ("dead_letter", None, None)
        assert await _checkpoint_counts(migration_url, tenant_id) == (0, 0, 0)
    finally:
        if consume_task is not None and not consume_task.done():
            consume_task.cancel()
        if dead_task is not None and not dead_task.done():
            dead_task.cancel()
        if consume_task is not None or dead_task is not None:
            await asyncio.gather(
                *(task for task in (consume_task, dead_task) if task is not None), return_exceptions=True
            )
        if holder is not None:
            await holder.rollback()
            await holder.close()
        if consume_connection is not None:
            await consume_connection.close()
        if dead_connection is not None:
            await dead_connection.close()
        await runtime_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lock_order_reconcile_vs_heartbeat_has_one_authority() -> None:
    """Expired reconciliation and heartbeat must share the run-first order."""

    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    projection_url = _role_url(api_url, "grove_projection", "grove_projection_ws0")
    runtime_raw_url = runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
    projection_raw_url = projection_url.replace("postgresql+psycopg://", "postgresql://", 1)
    migration_raw_url = migration_url.replace("postgresql+psycopg://", "postgresql://", 1)
    tenant_id = f"it-ws3-lock-reconcile-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    owner_engine = create_async_engine(migration_url)
    holder: psycopg.AsyncConnection | None = None
    heartbeat_connection: psycopg.AsyncConnection | None = None
    reconcile_connection: psycopg.AsyncConnection | None = None
    heartbeat_task: asyncio.Task[object] | None = None
    reconcile_task: asyncio.Task[object] | None = None
    try:
        driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
        claim = await driver.claim("lock-reconcile-worker", runtime_hash, tenant_id, 30)
        assert claim is not None and claim.run_id == handle.run_id
        expired_at = await _expire_claim(migration_url, claim)

        holder = await psycopg.AsyncConnection.connect(migration_raw_url)
        await holder.execute("BEGIN")
        await holder.execute(
            "SELECT 1 FROM run_command WHERE tenant_id = %s AND command_id = %s FOR UPDATE",
            (tenant_id, claim.command_id),
        )

        heartbeat_connection = await _raw_scoped_transaction(runtime_raw_url, tenant_id)
        heartbeat_pid_row = await (await heartbeat_connection.execute("SELECT pg_backend_pid()")).fetchone()
        assert heartbeat_pid_row is not None
        heartbeat_pid = int(heartbeat_pid_row[0])
        heartbeat_task = asyncio.create_task(
            _raw_call(
                heartbeat_connection,
                "SELECT grove_heartbeat_run_command(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (*_consume_args(claim), 30.0),
            )
        )
        await _wait_for_backend_lock(migration_url, heartbeat_pid)

        reconcile_connection = await _raw_scoped_transaction(projection_raw_url, tenant_id)
        reconcile_pid_row = await (await reconcile_connection.execute("SELECT pg_backend_pid()")).fetchone()
        assert reconcile_pid_row is not None
        reconcile_pid = int(reconcile_pid_row[0])
        reconcile_task = asyncio.create_task(
            _raw_call(
                reconcile_connection,
                "SELECT * FROM grove_reconcile_expired_run_command(%s, %s)",
                (tenant_id, handle.run_id),
            )
        )
        await _wait_for_backend_lock(migration_url, reconcile_pid)

        await holder.rollback()
        await holder.close()
        holder = None
        results = await asyncio.wait_for(
            asyncio.gather(heartbeat_task, reconcile_task, return_exceptions=True),
            timeout=4,
        )
        assert not any(isinstance(result, BaseException) for result in results), results
        assert results[0] == (None,)
        assert results[1][0] == "requeued"  # type: ignore[index]
        async with owner_engine.connect() as connection:
            state = (
                await connection.execute(
                    text(
                        "SELECT r.execution_fence, r.lease_owner, r.lease_until, c.status, c.execution_fence, "
                        "c.lease_owner, c.lease_until "
                        "FROM run_command AS c JOIN agent_run AS r USING (tenant_id, run_id) "
                        "WHERE c.command_id = :command_id"
                    ),
                    {"command_id": claim.command_id},
                )
            ).one()
        assert state == (claim.execution_fence, None, None, "pending", None, None, None)
        assert expired_at < datetime.now(UTC)
    finally:
        if heartbeat_task is not None and not heartbeat_task.done():
            heartbeat_task.cancel()
        if reconcile_task is not None and not reconcile_task.done():
            reconcile_task.cancel()
        if heartbeat_task is not None or reconcile_task is not None:
            await asyncio.gather(
                *(task for task in (heartbeat_task, reconcile_task) if task is not None), return_exceptions=True
            )
        if holder is not None:
            await holder.rollback()
            await holder.close()
        if heartbeat_connection is not None:
            await heartbeat_connection.close()
        if reconcile_connection is not None:
            await reconcile_connection.close()
        await runtime_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dead_letter_and_reconcile_lock_timeout_and_task_cancel_are_zero_write() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    projection_url = _role_url(api_url, "grove_projection", "grove_projection_ws0")
    tenant_dead = f"it-ws3-dead-letter-timeout-{uuid.uuid4().hex[:12]}"
    dead_handle, dead_hash = await _submit_start(api_url, migration_url, tenant_dead)
    runtime_engine = create_async_engine(runtime_url)
    projection_engine = create_async_engine(projection_url)
    owner_engine = create_async_engine(migration_url)
    runtime_factory = async_sessionmaker(runtime_engine, expire_on_commit=False)
    dead_driver = PostgresExecutionDriver(runtime_factory, operation_timeout_seconds=0.25)
    reconcile_driver = PostgresExecutionDriver(
        runtime_factory,
        reconciliation_session_factory=async_sessionmaker(projection_engine, expire_on_commit=False),
        operation_timeout_seconds=0.25,
    )
    lock = await owner_engine.connect()
    transaction = await lock.begin()
    try:
        dead_claim = await dead_driver.claim("timeout-worker", dead_hash, tenant_dead, 30)
        assert dead_claim is not None and dead_claim.run_id == dead_handle.run_id
        await lock.execute(
            text("SELECT 1 FROM agent_run WHERE tenant_id = :tenant AND run_id = :run_id FOR UPDATE"),
            {"tenant": tenant_dead, "run_id": dead_handle.run_id},
        )
        before_dead = await _lease_state(migration_url, dead_handle.run_id)
        before_dead_checkpoints = await _checkpoint_counts(migration_url, tenant_dead)
        with pytest.raises(asyncio.TimeoutError):
            await dead_driver.dead_letter(dead_claim, "lock-timeout")
        assert await _lease_state(migration_url, dead_handle.run_id) == before_dead
        assert await _checkpoint_counts(migration_url, tenant_dead) == before_dead_checkpoints

        cancelled_task = asyncio.create_task(dead_driver.dead_letter(dead_claim, "task-cancelled"))
        await asyncio.sleep(0.05)
        cancelled_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_task
        assert await _lease_state(migration_url, dead_handle.run_id) == before_dead
        assert await _checkpoint_counts(migration_url, tenant_dead) == before_dead_checkpoints
        await transaction.rollback()
        await lock.close()
        lock = await owner_engine.connect()
        transaction = await lock.begin()

        tenant_reconcile = f"it-ws3-reconcile-timeout-{uuid.uuid4().hex[:12]}"
        reconcile_handle, reconcile_hash = await _submit_start(api_url, migration_url, tenant_reconcile)
        reconcile_claim = await dead_driver.claim("reconcile-timeout-worker", reconcile_hash, tenant_reconcile, 30)
        assert reconcile_claim is not None and reconcile_claim.run_id == reconcile_handle.run_id
        await _expire_claim(migration_url, reconcile_claim)
        await lock.execute(
            text("SELECT 1 FROM agent_run WHERE tenant_id = :tenant AND run_id = :run_id FOR UPDATE"),
            {"tenant": tenant_reconcile, "run_id": reconcile_handle.run_id},
        )
        before_reconcile = await _lease_state(migration_url, reconcile_handle.run_id)
        before_reconcile_checkpoints = await _checkpoint_counts(migration_url, tenant_reconcile)
        with pytest.raises(asyncio.TimeoutError):
            await reconcile_driver.reconcile_expired(tenant_reconcile, reconcile_handle.run_id)
        assert await _lease_state(migration_url, reconcile_handle.run_id) == before_reconcile
        assert await _checkpoint_counts(migration_url, tenant_reconcile) == before_reconcile_checkpoints

        cancelled_reconcile = asyncio.create_task(
            reconcile_driver.reconcile_expired(tenant_reconcile, reconcile_handle.run_id)
        )
        await asyncio.sleep(0.05)
        cancelled_reconcile.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_reconcile
        assert await _lease_state(migration_url, reconcile_handle.run_id) == before_reconcile
        assert await _checkpoint_counts(migration_url, tenant_reconcile) == before_reconcile_checkpoints
    finally:
        if transaction.is_active:
            await transaction.rollback()
        await lock.close()
        await runtime_engine.dispose()
        await projection_engine.dispose()
        await owner_engine.dispose()


class _GraphState(TypedDict):
    count: int


class _CheckpointModel(BaseModel):
    value: int


@pytest.mark.integration
@pytest.mark.asyncio
async def test_claim_bound_saver_compiles_and_runs_real_graph_with_pending_writes() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-graph-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
    raw_url = runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        claim = await driver.claim("graph-worker", runtime_hash, tenant_id, 10)
        assert claim is not None and claim.run_id == handle.run_id
        async with await psycopg.AsyncConnection.connect(raw_url) as raw_connection:
            saver = FencedPostgresSaver(raw_connection, claim)
            builder = StateGraph(_GraphState)

            def increment(state: _GraphState) -> dict[str, int]:
                return {"count": state["count"] + 1}

            builder.add_node("increment", increment)
            builder.add_edge(START, "increment")
            builder.add_edge("increment", END)
            graph = builder.compile(checkpointer=saver)
            config = cast(RunnableConfig, {"configurable": {"thread_id": str(claim.run_id)}})
            result = await graph.ainvoke({"count": 0}, config)
            assert result == {"count": 1}
            recovered = await saver.aget(config)
            assert recovered is not None
            assert recovered["channel_values"]["count"] == 1

        async with create_async_engine(migration_url).connect() as connection:
            checkpoint_count, write_count = (
                await connection.execute(
                    text(
                        "SELECT (SELECT count(*) FROM checkpoints WHERE tenant_id = :tenant), "
                        "(SELECT count(*) FROM checkpoint_writes WHERE tenant_id = :tenant)"
                    ),
                    {"tenant": tenant_id},
                )
            ).one()
            assert checkpoint_count >= 2
            assert write_count >= 1
    finally:
        await runtime_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_new_versions_subset_and_blob_closure_are_pinned_semantics() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-versions-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    owner_engine = create_async_engine(migration_url)
    driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
    raw_url = runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
    versions = {
        "blob": "blob-v1",
        "bytes": "bytes-v1",
        "model": "model-v1",
        "message": "message-v1",
        "primitive": "primitive-v1",
        "empty": "empty-v1",
    }
    try:
        claim = await driver.claim("versions-worker", runtime_hash, tenant_id, 10)
        assert claim is not None
        first = empty_checkpoint()
        first["id"] = "versions-seed"
        first["channel_versions"] = cast(Any, versions)
        first["channel_values"] = {
            "blob": {"value": "one"},
            "bytes": b"raw",
            "model": _CheckpointModel(value=7),
            "message": HumanMessage(content="hello"),
            "primitive": True,
            "empty": None,
        }
        async with await psycopg.AsyncConnection.connect(raw_url) as raw_connection:
            saver = FencedPostgresSaver(raw_connection, claim)
            await saver.aput(
                {"configurable": {"thread_id": str(claim.run_id), "checkpoint_ns": ""}},
                first,
                cast(CheckpointMetadata, {}),
                cast(Any, versions),
            )

            # ``new_versions`` is a changed-channel subset.  The unchanged
            # non-primitive channels reuse their existing referenced blobs.
            second = empty_checkpoint()
            second["id"] = "versions-subset"
            second["channel_versions"] = cast(
                Any,
                {
                    **versions,
                    "primitive": "primitive-v2",
                    "empty": "empty-v2",
                },
            )
            second["channel_values"] = {
                "blob": {"value": "one"},
                "bytes": b"raw",
                "model": _CheckpointModel(value=7),
                "message": HumanMessage(content="hello"),
                "primitive": False,
                "empty": None,
            }
            await saver.aput(
                {"configurable": {"thread_id": str(claim.run_id), "checkpoint_ns": ""}},
                second,
                cast(CheckpointMetadata, {}),
                {"primitive": "primitive-v2", "empty": "empty-v2"},
            )

            conflict = empty_checkpoint()
            conflict["id"] = "versions-conflict"
            conflict["channel_versions"] = cast(Any, versions)
            conflict["channel_values"] = {
                "blob": {"value": "different"},
                "bytes": b"raw",
                "model": _CheckpointModel(value=7),
                "message": HumanMessage(content="hello"),
                "primitive": True,
                "empty": None,
            }
            with pytest.raises((psycopg.errors.CheckViolation, psycopg.errors.SerializationFailure)):
                await saver.aput(
                    {"configurable": {"thread_id": str(claim.run_id), "checkpoint_ns": ""}},
                    conflict,
                    cast(CheckpointMetadata, {}),
                    {"blob": "blob-v1"},
                )

        async with owner_engine.connect() as connection:
            blob_row = (
                await connection.execute(
                    text(
                        "SELECT type, blob FROM checkpoint_blobs "
                        "WHERE tenant_id = :tenant AND channel = 'blob' AND version = 'blob-v1'"
                    ),
                    {"tenant": tenant_id},
                )
            ).one()
            assert blob_row[1] is not None
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM checkpoints "
                        "WHERE tenant_id = :tenant AND checkpoint_id = 'versions-conflict'"
                    ),
                    {"tenant": tenant_id},
                )
                == 0
            )
    finally:
        await runtime_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unchanged_blob_version_rejects_changed_value_omitted_from_new_versions() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-blob-omitted-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    owner_engine = create_async_engine(migration_url)
    driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
    raw_url = runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
    config = cast(RunnableConfig, {"configurable": {"thread_id": str(handle.run_id), "checkpoint_ns": ""}})
    try:
        claim = await driver.claim("blob-omitted-worker", runtime_hash, tenant_id, 10)
        assert claim is not None
        first = empty_checkpoint()
        first["id"] = "blob-first"
        first["channel_versions"] = {"state": "state-v1"}
        first["channel_values"] = {"state": {"answer": "first"}}
        async with await psycopg.AsyncConnection.connect(raw_url) as raw_connection:
            saver = FencedPostgresSaver(raw_connection, claim)
            await saver.aput(config, first, cast(CheckpointMetadata, {}), {"state": "state-v1"})

            exact = empty_checkpoint()
            exact["id"] = "blob-exact"
            exact["channel_versions"] = {"state": "state-v1"}
            exact["channel_values"] = {"state": {"answer": "first"}}
            await saver.aput(config, exact, cast(CheckpointMetadata, {}), {})
            loaded_exact = await saver.aget_tuple(
                cast(
                    RunnableConfig,
                    {
                        "configurable": {
                            "thread_id": str(claim.run_id),
                            "checkpoint_ns": "",
                            "checkpoint_id": "blob-exact",
                        }
                    },
                )
            )
            assert loaded_exact is not None
            assert loaded_exact.checkpoint["channel_values"]["state"] == {"answer": "first"}

            changed = empty_checkpoint()
            changed["id"] = "blob-changed"
            changed["channel_versions"] = {"state": "state-v1"}
            changed["channel_values"] = {"state": {"answer": "changed"}}
            with pytest.raises((psycopg.errors.CheckViolation, psycopg.errors.SerializationFailure)):
                await saver.aput(config, changed, cast(CheckpointMetadata, {}), {})

        async with owner_engine.connect() as connection:
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM checkpoints WHERE tenant_id = :tenant AND checkpoint_id = 'blob-changed'"
                    ),
                    {"tenant": tenant_id},
                )
                == 0
            )
            stored = await connection.scalar(
                text(
                    "SELECT checkpoint->'channel_values' FROM checkpoints "
                    "WHERE tenant_id = :tenant AND checkpoint_id = 'blob-exact'"
                ),
                {"tenant": tenant_id},
            )
            assert stored == {}
    finally:
        await runtime_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "first_value", "changed_value"),
    [
        ("blob-to-primitive", {"value": "blob"}, True),
        ("primitive-to-blob", False, {"value": "blob"}),
        ("primitive-content", True, False),
        ("none-to-blob", None, {"value": "blob"}),
        ("blob-to-none", {"value": "blob"}, None),
        ("delta-to-blob", _DeltaSnapshot({"value": "delta"}), {"value": "blob"}),
    ],
)
async def test_checkpoint_blob_identity_covers_all_representation_transitions(
    label: str, first_value: Any, changed_value: Any
) -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-representation-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    owner_engine = create_async_engine(migration_url)
    driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
    raw_url = runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
    channel = f"state-{label}"
    version = f"version-{label}"
    config = cast(RunnableConfig, {"configurable": {"thread_id": str(handle.run_id), "checkpoint_ns": ""}})
    try:
        claim = await driver.claim(f"representation-{label}", runtime_hash, tenant_id, 10)
        assert claim is not None
        first = empty_checkpoint()
        first["id"] = f"{label}-first"
        first["channel_versions"] = {channel: version}
        first["channel_values"] = {channel: first_value}
        exact = empty_checkpoint()
        exact["id"] = f"{label}-exact"
        exact["channel_versions"] = {channel: version}
        exact["channel_values"] = {channel: first_value}
        changed = empty_checkpoint()
        changed["id"] = f"{label}-changed"
        changed["channel_versions"] = {channel: version}
        changed["channel_values"] = {channel: changed_value}
        async with await psycopg.AsyncConnection.connect(raw_url) as raw_connection:
            saver = FencedPostgresSaver(raw_connection, claim)
            await saver.aput(config, first, cast(CheckpointMetadata, {}), {channel: version})
            await saver.aput(config, exact, cast(CheckpointMetadata, {}), {})
            loaded = await saver.aget_tuple(
                cast(
                    RunnableConfig,
                    {
                        "configurable": {
                            "thread_id": str(claim.run_id),
                            "checkpoint_ns": "",
                            "checkpoint_id": exact["id"],
                        }
                    },
                )
            )
            assert loaded is not None
            assert loaded.checkpoint["channel_values"][channel] == first_value
            with pytest.raises((psycopg.errors.CheckViolation, psycopg.errors.SerializationFailure)):
                await saver.aput(config, changed, cast(CheckpointMetadata, {}), {})
        async with owner_engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM checkpoints WHERE tenant_id = :tenant AND checkpoint_id = :checkpoint"),
                    {"tenant": tenant_id, "checkpoint": changed["id"]},
                )
                == 0
            )
    finally:
        await runtime_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_checkpoint_absent_channel_marker_rejects_later_value() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-absent-marker-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    owner_engine = create_async_engine(migration_url)
    driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
    raw_url = runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
    channel = "absent-state"
    version = "absent-v1"
    config = cast(RunnableConfig, {"configurable": {"thread_id": str(handle.run_id), "checkpoint_ns": ""}})
    try:
        claim = await driver.claim("absent-marker-worker", runtime_hash, tenant_id, 10)
        assert claim is not None
        first = empty_checkpoint()
        first["id"] = "absent-first"
        first["channel_versions"] = {channel: version}
        first["channel_values"] = {}
        exact = empty_checkpoint()
        exact["id"] = "absent-exact"
        exact["channel_versions"] = {channel: version}
        exact["channel_values"] = {}
        changed = empty_checkpoint()
        changed["id"] = "absent-changed"
        changed["channel_versions"] = {channel: version}
        changed["channel_values"] = {channel: True}
        async with await psycopg.AsyncConnection.connect(raw_url) as raw_connection:
            saver = FencedPostgresSaver(raw_connection, claim)
            await saver.aput(config, first, cast(CheckpointMetadata, {}), {channel: version})
            await saver.aput(config, exact, cast(CheckpointMetadata, {}), {})
            loaded = await saver.aget_tuple(
                cast(
                    RunnableConfig,
                    {
                        "configurable": {
                            "thread_id": str(claim.run_id),
                            "checkpoint_ns": "",
                            "checkpoint_id": exact["id"],
                        }
                    },
                )
            )
            assert loaded is not None
            assert loaded.checkpoint["channel_values"] == {}
            with pytest.raises((psycopg.errors.CheckViolation, psycopg.errors.SerializationFailure)):
                await saver.aput(config, changed, cast(CheckpointMetadata, {}), {})
        async with owner_engine.connect() as connection:
            marker = (
                await connection.execute(
                    text(
                        "SELECT type, blob FROM checkpoint_blobs "
                        "WHERE tenant_id = :tenant AND channel = :channel AND version = :version"
                    ),
                    {"tenant": tenant_id, "channel": channel, "version": version},
                )
            ).one()
            assert marker[0] == "empty"
            assert marker[1] is not None
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM checkpoints WHERE tenant_id = :tenant AND checkpoint_id = :checkpoint"),
                    {"tenant": tenant_id, "checkpoint": changed["id"]},
                )
                == 0
            )
    finally:
        await runtime_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_checkpoint_guc_blob_context_resets_inside_outer_transaction() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-guc-reset-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
    raw_url = runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
    config = cast(RunnableConfig, {"configurable": {"thread_id": str(handle.run_id), "checkpoint_ns": ""}})
    try:
        claim = await driver.claim("guc-reset-worker", runtime_hash, tenant_id, 10)
        assert claim is not None
        blob = empty_checkpoint()
        blob["id"] = "guc-blob"
        blob["channel_versions"] = {"state": "guc-v1"}
        blob["channel_values"] = {"state": {"value": "blob"}}
        primitive = empty_checkpoint()
        primitive["id"] = "guc-primitive"
        primitive["channel_versions"] = {}
        primitive["channel_values"] = {"state": True}
        async with await psycopg.AsyncConnection.connect(raw_url) as raw_connection:
            saver = FencedPostgresSaver(raw_connection, claim)
            async with raw_connection.transaction():
                await saver.aput(config, blob, cast(CheckpointMetadata, {}), {"state": "guc-v1"})
                await saver.aput(config, primitive, cast(CheckpointMetadata, {}), {})
    finally:
        await runtime_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_runtime_temp_privilege_is_denied_and_drift_fails_preflight() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    migration_raw_url = migration_url.replace("postgresql+psycopg://", "postgresql://", 1)
    runtime_raw_url = runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
    async with await psycopg.AsyncConnection.connect(runtime_raw_url) as runtime_connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await runtime_connection.execute("CREATE TEMP TABLE grove_temp_probe(value integer)")
    async with await psycopg.AsyncConnection.connect(migration_raw_url) as migration_connection:
        await migration_connection.execute(
            "DO $$ BEGIN EXECUTE format('GRANT TEMP ON DATABASE %I TO grove_runtime', current_database()); END $$"
        )
        await migration_connection.commit()
        try:
            with pytest.raises(ws3_preflight.WS3PreflightError, match="schema does not match"):
                ws3_preflight.check(Path.cwd(), migration_url)
        finally:
            await migration_connection.execute(
                "DO $$ BEGIN EXECUTE format('REVOKE TEMP ON DATABASE %I FROM grove_runtime', "
                "current_database()); END $$"
            )
            await migration_connection.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancel_schema_contract_reverse_drift_matrix_fails_preflight() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    raw_url = migration_url.replace("postgresql+psycopg://", "postgresql://", 1)
    mutations = (
        (
            "drop superseded target FK",
            "ALTER TABLE run_command DROP CONSTRAINT run_command_superseded_target_fk",
            "ALTER TABLE run_command ADD CONSTRAINT run_command_superseded_target_fk "
            "FOREIGN KEY (tenant_id, superseded_by_command_id) REFERENCES run_command (tenant_id, command_id)",
        ),
        (
            "drop payload binding FK",
            "ALTER TABLE run_command DROP CONSTRAINT run_command_payload_fk",
            "ALTER TABLE run_command ADD CONSTRAINT run_command_payload_fk "
            "FOREIGN KEY (tenant_id, payload_ref, payload_hash, command_schema_version) "
            "REFERENCES command_payload (tenant_id, payload_ref, payload_hash, command_schema_version)",
        ),
        (
            "grant governance cancel execute",
            "GRANT EXECUTE ON FUNCTION grove_accept_cancel_run("
            "TEXT, UUID, UUID, BIGINT, TEXT, TEXT, TEXT, TEXT, JSONB) "
            "TO grove_governance",
            "REVOKE EXECUTE ON FUNCTION grove_accept_cancel_run("
            "TEXT, UUID, UUID, BIGINT, TEXT, TEXT, TEXT, TEXT, JSONB) "
            "FROM grove_governance",
        ),
        (
            "grant API payload body select",
            "GRANT SELECT (payload) ON command_payload TO grove_api",
            "REVOKE SELECT (payload) ON command_payload FROM grove_api",
        ),
        (
            "drop run command type constraint",
            "ALTER TABLE run_command DROP CONSTRAINT run_command_type_ck",
            "ALTER TABLE run_command ADD CONSTRAINT run_command_type_ck CHECK "
            "(command_type IN ('start', 'resume', 'cancel', 'continue', 'signal'))",
        ),
        (
            "drop payload schema constraint",
            "ALTER TABLE command_payload DROP CONSTRAINT command_payload_schema_version_ck",
            "ALTER TABLE command_payload ADD CONSTRAINT command_payload_schema_version_ck CHECK "
            "(command_schema_version IN ('start.v1', 'resume.v1', 'cancel.v1', 'continue.v1', 'signal.v1'))",
        ),
    )
    async with await psycopg.AsyncConnection.connect(raw_url) as connection:
        for _label, mutation, restore in mutations:
            await connection.execute(mutation)
            await connection.commit()
            try:
                with pytest.raises(ws3_preflight.WS3PreflightError, match="schema does not match"):
                    ws3_preflight.check(Path.cwd(), migration_url)
            finally:
                await connection.execute(restore)
                await connection.commit()
            ws3_preflight.check(Path.cwd(), migration_url)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancel_data_bearing_downgrade_fails_closed_before_ddl() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-cancel-downgrade-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    api_engine = create_async_engine(api_url)
    owner_engine = create_async_engine(migration_url)
    driver = PostgresExecutionDriver(
        async_sessionmaker(runtime_engine, expire_on_commit=False),
        command_session_factory=async_sessionmaker(api_engine, expire_on_commit=False),
    )
    try:
        cancel = _cancel_command(tenant_id=tenant_id, run_id=handle.run_id, runtime_build_hash=runtime_hash)
        await driver.dispatch(cancel)
        async with owner_engine.connect() as connection:
            before = (
                await connection.execute(
                    text(
                        "SELECT (SELECT version_num FROM alembic_version), "
                        "(SELECT count(*) FROM run_command WHERE command_id = :command_id), "
                        "(SELECT count(*) FROM command_payload "
                        "WHERE tenant_id = :tenant AND command_schema_version = 'cancel.v1')"
                    ),
                    {"command_id": cancel.command_id, "tenant": tenant_id},
                )
            ).one()
        child_env = dict(os.environ)
        child_env["GROVE_DATABASE_URL"] = migration_url
        child_env["GROVE_ROLE"] = "api"
        child_env.pop("GROVE_MIGRATION_DATABASE_URL", None)
        # The integration harness uses this HTTP-only helper variable for API
        # tests; Alembic runs through strict Settings and must not inherit it.
        child_env.pop("GROVE_API_BASE_URL", None)
        result = subprocess.run(  # noqa: S603, ASYNC221 - bounded migration probe
            [sys.executable, "-m", "alembic", "downgrade", "ws3_checkpoint_fenced"],
            cwd=Path.cwd(),
            env=child_env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode != 0
        assert "WS3_DOWNGRADE_INCOMPATIBLE_LIVE_DATA" in (result.stdout + result.stderr)
        async with owner_engine.connect() as connection:
            after = (
                await connection.execute(
                    text(
                        "SELECT (SELECT version_num FROM alembic_version), "
                        "(SELECT count(*) FROM run_command WHERE command_id = :command_id), "
                        "(SELECT count(*) FROM command_payload "
                        "WHERE tenant_id = :tenant AND command_schema_version = 'cancel.v1')"
                    ),
                    {"command_id": cancel.command_id, "tenant": tenant_id},
                )
            ).one()
        assert after == before
    finally:
        await runtime_engine.dispose()
        await api_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_regular_pending_write_same_key_different_content_conflicts() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-write-conflict-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    owner_engine = create_async_engine(migration_url)
    driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
    raw_url = runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        claim = await driver.claim("write-conflict-worker", runtime_hash, tenant_id, 10)
        assert claim is not None
        config = cast(
            RunnableConfig,
            {
                "configurable": {
                    "thread_id": str(claim.run_id),
                    "checkpoint_ns": "",
                    "checkpoint_id": "writes",
                }
            },
        )
        async with await psycopg.AsyncConnection.connect(raw_url) as raw_connection:
            saver = FencedPostgresSaver(raw_connection, claim)
            await saver.aput_writes(config, [("regular", {"value": "first"})], "task", "path")
            await saver.aput_writes(config, [("regular", {"value": "first"})], "task", "path")
            with pytest.raises((psycopg.errors.CheckViolation, psycopg.errors.SerializationFailure)):
                await saver.aput_writes(config, [("regular", {"value": "changed"})], "task", "path")
            await saver.aput_writes(config, [("__interrupt__", {"value": "pause"})], "control", "path")
            with pytest.raises((psycopg.errors.CheckViolation, psycopg.errors.SerializationFailure)):
                await saver.aput_writes(config, [("__interrupt__", {"value": "resume"})], "control", "path")

        async with owner_engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT task_id, channel, type, blob FROM checkpoint_writes "
                        "WHERE tenant_id = :tenant AND checkpoint_id = 'writes' ORDER BY task_id"
                    ),
                    {"tenant": tenant_id},
                )
            ).all()
            assert [row[0] for row in rows] == ["control", "task"]
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM checkpoint_writes WHERE tenant_id = :tenant AND checkpoint_id = 'writes'"
                    ),
                    {"tenant": tenant_id},
                )
                == 2
            )
    finally:
        await runtime_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_preflight_rejects_disabled_checkpoint_trigger() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    raw_url = migration_url.replace("postgresql+psycopg://", "postgresql://", 1)
    async with await psycopg.AsyncConnection.connect(raw_url) as connection:
        await connection.execute("ALTER TABLE checkpoints DISABLE TRIGGER checkpoints_authority_guard")
        await connection.execute("ALTER TABLE checkpoints DISABLE TRIGGER checkpoints_physical_guard")
        await connection.commit()
        try:
            with pytest.raises(ws3_preflight.WS3PreflightError, match="schema does not match"):
                ws3_preflight.check(Path.cwd(), migration_url)
        finally:
            await connection.execute("ALTER TABLE checkpoints ENABLE TRIGGER checkpoints_authority_guard")
            await connection.execute("ALTER TABLE checkpoints ENABLE TRIGGER checkpoints_physical_guard")
            await connection.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_preflight_rejects_disabled_agent_run_runtime_build_trigger() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    raw_url = migration_url.replace("postgresql+psycopg://", "postgresql://", 1)
    async with await psycopg.AsyncConnection.connect(raw_url) as connection:
        await connection.execute("ALTER TABLE agent_run DISABLE TRIGGER agent_run_runtime_build_guard")
        await connection.commit()
        try:
            with pytest.raises(ws3_preflight.WS3PreflightError, match="schema does not match"):
                ws3_preflight.check(Path.cwd(), migration_url)
        finally:
            await connection.execute("ALTER TABLE agent_run ENABLE TRIGGER agent_run_runtime_build_guard")
            await connection.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_preflight_rejects_extra_protected_trigger_and_keeps_other_tables_out() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    raw_url = migration_url.replace("postgresql+psycopg://", "postgresql://", 1)
    protected_tables = ("agent_run", "checkpoints", "checkpoint_blobs", "checkpoint_writes")
    created_triggers: list[tuple[str, str]] = []
    async with await psycopg.AsyncConnection.connect(raw_url) as connection:
        try:
            await connection.execute(
                """
                CREATE OR REPLACE FUNCTION public.grove_r2_catalog_probe() RETURNS trigger
                LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$
                """
            )
            await connection.commit()
            for table in protected_tables:
                trigger_name = f"r2_extra_{table}"
                await connection.execute(
                    f"CREATE TRIGGER {trigger_name} BEFORE INSERT ON public.{table} "
                    "FOR EACH ROW EXECUTE FUNCTION public.grove_r2_catalog_probe()"
                )
                await connection.commit()
                created_triggers.append((table, trigger_name))
                with pytest.raises(ws3_preflight.WS3PreflightError, match="schema does not match"):
                    ws3_preflight.check(Path.cwd(), migration_url)
                await connection.execute(f"DROP TRIGGER {trigger_name} ON public.{table}")
                await connection.commit()
                created_triggers.pop()

            same_name = "r2_same_name"
            for table in ("agent_run", "checkpoints", "checkpoint_blobs"):
                await connection.execute(
                    f"CREATE TRIGGER {same_name} BEFORE INSERT ON public.{table} "
                    "FOR EACH ROW EXECUTE FUNCTION public.grove_r2_catalog_probe()"
                )
                await connection.commit()
                created_triggers.append((table, same_name))
            with pytest.raises(ws3_preflight.WS3PreflightError, match="schema does not match"):
                ws3_preflight.check(Path.cwd(), migration_url)
            for table, trigger_name in created_triggers:
                await connection.execute(f"DROP TRIGGER {trigger_name} ON public.{table}")
            created_triggers.clear()
            await connection.commit()

            await connection.execute(
                "CREATE TRIGGER r2_nonprotected BEFORE INSERT ON public.tenant "
                "FOR EACH ROW EXECUTE FUNCTION public.grove_r2_catalog_probe()"
            )
            await connection.commit()
            created_triggers.append(("tenant", "r2_nonprotected"))
            # v6 closes the trigger catalog over every non-extension public
            # relation, so a trigger on an identity table is drift too.
            with pytest.raises(ws3_preflight.WS3PreflightError, match="schema does not match"):
                ws3_preflight.check(Path.cwd(), migration_url)
            await connection.execute("DROP TRIGGER r2_nonprotected ON public.tenant")
            await connection.commit()
            created_triggers.pop()
        finally:
            for table, trigger_name in created_triggers:
                await connection.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON public.{table}")
            await connection.execute("DROP FUNCTION IF EXISTS public.grove_r2_catalog_probe()")
            await connection.commit()


@pytest.mark.integration
def test_v6_preflight_rejects_authority_role_acl_and_relation_tampering() -> None:
    """Persist the finite v6 authority tamper matrix with green restoration."""

    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    raw_migration_url = migration_url.replace("postgresql+psycopg://", "postgresql://", 1)
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0").replace(
        "postgresql+psycopg://", "postgresql://", 1
    )
    projection_url = _role_url(api_url, "grove_projection", "grove_projection_ws0").replace(
        "postgresql+psycopg://", "postgresql://", 1
    )

    def execute(sql: str) -> None:
        with psycopg.connect(raw_migration_url) as connection:
            connection.execute(sql)

    def expect_red() -> None:
        with pytest.raises(ws3_preflight.WS3PreflightError):
            ws3_preflight.check(Path.cwd(), migration_url)

    def expect_green() -> None:
        ws3_preflight.check(Path.cwd(), migration_url)

    expect_green()

    execute("ALTER ROLE grove_api BYPASSRLS")
    try:
        expect_red()
    finally:
        execute("ALTER ROLE grove_api NOBYPASSRLS")
    expect_green()

    execute("ALTER ROLE grove_api SUPERUSER")
    try:
        expect_red()
    finally:
        execute("ALTER ROLE grove_api NOSUPERUSER")
    expect_green()

    execute("GRANT grove_migration TO grove_runtime")
    execute("GRANT grove_runtime TO grove_projection")
    try:
        with psycopg.connect(runtime_url) as connection, connection.cursor() as cursor:
            cursor.execute("SET ROLE grove_migration")
            cursor.execute("SELECT current_user")
            row = cursor.fetchone()
            assert row is not None and row[0] == "grove_migration"
        with psycopg.connect(projection_url) as connection, connection.cursor() as cursor:
            cursor.execute("SET ROLE grove_migration")
            cursor.execute("SELECT current_user")
            row = cursor.fetchone()
            assert row is not None and row[0] == "grove_migration"
        expect_red()
    finally:
        execute("REVOKE grove_migration FROM grove_runtime")
        execute("REVOKE grove_runtime FROM grove_projection")
    expect_green()

    tamper_cases = (
        (
            "tenant trigger",
            "CREATE TRIGGER v6_tenant_extra BEFORE UPDATE ON public.tenant "
            "FOR EACH ROW EXECUTE FUNCTION public.grove_reject_identity_key_change()",
            "DROP TRIGGER IF EXISTS v6_tenant_extra ON public.tenant",
        ),
        (
            "permissive policy",
            "CREATE POLICY v6_extra_policy ON public.tenant USING (true) WITH CHECK (true)",
            "DROP POLICY IF EXISTS v6_extra_policy ON public.tenant",
        ),
        (
            "run_command rewrite",
            "CREATE RULE v6_extra_rule AS ON UPDATE TO public.run_command DO ALSO NOTHING",
            "DROP RULE IF EXISTS v6_extra_rule ON public.run_command",
        ),
        (
            "column-only infrastructure grant",
            "GRANT UPDATE (v) ON public.checkpoint_migrations TO grove_api",
            "REVOKE UPDATE (v) ON public.checkpoint_migrations FROM grove_api",
        ),
        (
            "inheritance",
            "CREATE TABLE public.v6_tenant_child (marker text) INHERITS (public.tenant)",
            "DROP TABLE IF EXISTS public.v6_tenant_child",
        ),
    )
    for _label, inject, restore in tamper_cases:
        execute(inject)
        try:
            expect_red()
        finally:
            execute(restore)
        expect_green()

    execute("ALTER TABLE public.tenant OWNER TO grove_api")
    try:
        expect_red()
    finally:
        execute("ALTER TABLE public.tenant OWNER TO grove_migration")
        execute("GRANT SELECT ON public.tenant TO grove_api, grove_runtime, grove_projection, grove_governance")
    expect_green()

    execute("ALTER TABLE public.checkpoint_migrations RENAME TO v6_checkpoint_migrations_backup")
    execute("CREATE VIEW public.checkpoint_migrations AS SELECT v FROM public.v6_checkpoint_migrations_backup")
    try:
        expect_red()
    finally:
        execute("DROP VIEW IF EXISTS public.checkpoint_migrations")
        execute("ALTER TABLE public.v6_checkpoint_migrations_backup RENAME TO checkpoint_migrations")
    expect_green()


@pytest.mark.integration
def test_v7_preflight_rejects_complete_authority_surface_tampering() -> None:
    """Every finite v7 WS-3 authority surface must fail closed and restore green."""

    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    raw_url = migration_url.replace("postgresql+psycopg://", "postgresql://", 1)

    def execute(sql: str) -> None:
        with psycopg.connect(raw_url) as connection:
            connection.execute(sql)

    def expect_red() -> None:
        with pytest.raises(ws3_preflight.WS3PreflightError, match="schema does not match"):
            ws3_preflight.check(Path.cwd(), migration_url)

    def expect_green() -> None:
        ws3_preflight.check(Path.cwd(), migration_url)

    expect_green()
    tamper_cases = (
        (
            "unlogged relation",
            "ALTER TABLE public.checkpoint_migrations SET UNLOGGED",
            "ALTER TABLE public.checkpoint_migrations SET LOGGED",
        ),
        (
            "replica identity",
            "ALTER TABLE public.tenant REPLICA IDENTITY FULL",
            "ALTER TABLE public.tenant REPLICA IDENTITY DEFAULT",
        ),
        (
            "extra constraint",
            "ALTER TABLE public.tenant ADD CONSTRAINT v7_extra_status_ck CHECK (length(status) > 0)",
            "ALTER TABLE public.tenant DROP CONSTRAINT v7_extra_status_ck",
        ),
        (
            "policy command",
            "DROP POLICY tenant_tenant_isolation ON public.tenant; "
            "CREATE POLICY tenant_tenant_isolation ON public.tenant FOR UPDATE "
            "USING (tenant_id = grove_active_tenant()) "
            "WITH CHECK (tenant_id = grove_active_tenant())",
            "DROP POLICY tenant_tenant_isolation ON public.tenant; "
            "CREATE POLICY tenant_tenant_isolation ON public.tenant "
            "USING (tenant_id = grove_active_tenant()) "
            "WITH CHECK (tenant_id = grove_active_tenant())",
        ),
        (
            "public ACL",
            "GRANT SELECT ON public.tenant TO PUBLIC",
            "REVOKE SELECT ON public.tenant FROM PUBLIC",
        ),
        (
            "grant option",
            "GRANT SELECT ON public.tenant TO grove_api WITH GRANT OPTION",
            "REVOKE SELECT ON public.tenant FROM grove_api; GRANT SELECT ON public.tenant TO grove_api",
        ),
        (
            "quoted comma role ACL",
            'CREATE ROLE "v7,unknown" NOLOGIN; GRANT DELETE ON public.checkpoint_migrations TO "v7,unknown"',
            'REVOKE DELETE ON public.checkpoint_migrations FROM "v7,unknown"; DROP ROLE "v7,unknown"',
        ),
    )
    for _label, inject, restore in tamper_cases:
        execute(inject)
        try:
            if _label == "quoted comma role ACL":
                # Prove the grant is materially usable by an unknown quoted
                # role, while rolling the DML back before preflight/restore.
                with psycopg.connect(raw_url) as connection:
                    connection.execute('SET ROLE "v7,unknown"')
                    assert connection.execute("DELETE FROM public.checkpoint_migrations").rowcount == 10
                    connection.rollback()
            expect_red()
        finally:
            execute(restore)
        expect_green()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_preflight_rejects_trigger_definition_and_missing_trigger() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    raw_url = migration_url.replace("postgresql+psycopg://", "postgresql://", 1)
    async with await psycopg.AsyncConnection.connect(raw_url) as connection:
        try:
            await connection.execute(
                """
                CREATE OR REPLACE FUNCTION public.grove_r2_catalog_probe() RETURNS trigger
                LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$
                """
            )
            await connection.commit()
            await connection.execute("DROP TRIGGER checkpoints_authority_guard ON public.checkpoints")
            await connection.execute("DROP TRIGGER checkpoints_physical_guard ON public.checkpoints")
            await connection.execute(
                "CREATE TRIGGER checkpoints_authority_guard BEFORE INSERT ON public.checkpoints "
                "FOR EACH ROW EXECUTE FUNCTION public.grove_r2_catalog_probe()"
            )
            await connection.commit()
            with pytest.raises(ws3_preflight.WS3PreflightError, match="schema does not match"):
                ws3_preflight.check(Path.cwd(), migration_url)

            await connection.execute("DROP TRIGGER checkpoints_authority_guard ON public.checkpoints")
            await connection.execute(
                "CREATE TRIGGER checkpoints_authority_guard BEFORE INSERT OR UPDATE ON public.checkpoints "
                "FOR EACH ROW EXECUTE FUNCTION public.grove_checkpoint_authority_guard()"
            )
            await connection.execute(
                "CREATE TRIGGER checkpoints_physical_guard BEFORE INSERT OR UPDATE ON public.checkpoints "
                "FOR EACH ROW EXECUTE FUNCTION public.grove_checkpoint_physical_guard()"
            )
            await connection.commit()
            await connection.execute("DROP TRIGGER checkpoints_tenant_guard ON public.checkpoints")
            await connection.commit()
            with pytest.raises(ws3_preflight.WS3PreflightError, match="schema does not match"):
                ws3_preflight.check(Path.cwd(), migration_url)
        finally:
            await connection.execute("DROP TRIGGER IF EXISTS checkpoints_authority_guard ON public.checkpoints")
            await connection.execute("DROP TRIGGER IF EXISTS checkpoints_physical_guard ON public.checkpoints")
            await connection.execute("DROP TRIGGER IF EXISTS checkpoints_tenant_guard ON public.checkpoints")
            await connection.execute(
                "CREATE TRIGGER checkpoints_authority_guard BEFORE INSERT OR UPDATE ON public.checkpoints "
                "FOR EACH ROW EXECUTE FUNCTION public.grove_checkpoint_authority_guard()"
            )
            await connection.execute(
                "CREATE TRIGGER checkpoints_tenant_guard BEFORE INSERT OR UPDATE ON public.checkpoints "
                "FOR EACH ROW EXECUTE FUNCTION public.grove_checkpoint_tenant_guard()"
            )
            await connection.execute(
                "CREATE TRIGGER checkpoints_physical_guard BEFORE INSERT OR UPDATE ON public.checkpoints "
                "FOR EACH ROW EXECUTE FUNCTION public.grove_checkpoint_physical_guard()"
            )
            await connection.execute("DROP FUNCTION IF EXISTS public.grove_r2_catalog_probe()")
            await connection.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_preflight_rejects_agent_run_runtime_build_trigger_definition_and_missing() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    raw_url = migration_url.replace("postgresql+psycopg://", "postgresql://", 1)
    async with await psycopg.AsyncConnection.connect(raw_url) as connection:
        try:
            await connection.execute(
                """
                CREATE OR REPLACE FUNCTION public.grove_r2_runtime_build_probe() RETURNS trigger
                LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$
                """
            )
            await connection.execute("DROP TRIGGER agent_run_runtime_build_guard ON public.agent_run")
            await connection.execute(
                "CREATE TRIGGER agent_run_runtime_build_guard "
                "BEFORE UPDATE OF runtime_build_ref, runtime_build_hash ON public.agent_run "
                "FOR EACH ROW EXECUTE FUNCTION public.grove_r2_runtime_build_probe()"
            )
            await connection.commit()
            with pytest.raises(ws3_preflight.WS3PreflightError, match="schema does not match"):
                ws3_preflight.check(Path.cwd(), migration_url)

            await connection.execute("DROP TRIGGER agent_run_runtime_build_guard ON public.agent_run")
            await connection.commit()
            with pytest.raises(ws3_preflight.WS3PreflightError, match="schema does not match"):
                ws3_preflight.check(Path.cwd(), migration_url)
        finally:
            await connection.execute("DROP TRIGGER IF EXISTS agent_run_runtime_build_guard ON public.agent_run")
            await connection.execute(
                "CREATE TRIGGER agent_run_runtime_build_guard "
                "BEFORE UPDATE OF runtime_build_ref, runtime_build_hash ON public.agent_run "
                "FOR EACH ROW EXECUTE FUNCTION public.grove_reject_agent_run_runtime_build_rebinding()"
            )
            await connection.execute("DROP FUNCTION IF EXISTS public.grove_r2_runtime_build_probe()")
            await connection.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_preflight_rejects_trigger_target_function_closure_drift() -> None:
    """The trigger contract follows tgfoid target identity and executable facts."""

    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    raw_url = migration_url.replace("postgresql+psycopg://", "postgresql://", 1)
    target = "public.grove_reject_execution_fence_regression()"
    canonical_definition = """
        CREATE OR REPLACE FUNCTION public.grove_reject_execution_fence_regression() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            IF NEW.execution_fence < OLD.execution_fence THEN
                RAISE EXCEPTION 'execution fence cannot decrease';
            END IF;
            RETURN NEW;
        END $$
    """
    mutations = {
        "body": """
            CREATE OR REPLACE FUNCTION public.grove_reject_execution_fence_regression() RETURNS trigger
            LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
            BEGIN
                IF NEW.execution_fence < OLD.execution_fence THEN
                    RAISE EXCEPTION 'execution fence target drift';
                END IF;
                RETURN NEW;
            END $$
        """,
        "owner": f"ALTER FUNCTION {target} OWNER TO grove_runtime",
        "acl": f"GRANT EXECUTE ON FUNCTION {target} TO grove_runtime",
        "security": f"ALTER FUNCTION {target} SECURITY DEFINER",
        "search_path": f"ALTER FUNCTION {target} SET search_path = public",
    }

    async with await psycopg.AsyncConnection.connect(raw_url) as connection:
        try:
            for mutation in mutations.values():
                await connection.execute(mutation)
                await connection.commit()
                with pytest.raises(ws3_preflight.WS3PreflightError, match="schema does not match"):
                    ws3_preflight.check(Path.cwd(), migration_url)

                # Restore every executable catalog fact, not just the body.  The
                # owner is reset before subsequent ALTER statements because the
                # mutation deliberately transfers ownership to grove_runtime.
                await connection.execute(canonical_definition)
                await connection.execute(f"ALTER FUNCTION {target} SECURITY INVOKER")
                await connection.execute(f"ALTER FUNCTION {target} OWNER TO grove_migration")
                await connection.execute(f"REVOKE ALL ON FUNCTION {target} FROM PUBLIC, grove_runtime")
                await connection.execute(f"GRANT EXECUTE ON FUNCTION {target} TO PUBLIC, grove_migration")
                await connection.execute(f"ALTER FUNCTION {target} SET search_path = pg_catalog, public")
                await connection.commit()
                ws3_preflight.check(Path.cwd(), migration_url)
        finally:
            # Keep the database canonical even if a preflight assertion aborts.
            await connection.execute(canonical_definition)
            await connection.execute(f"ALTER FUNCTION {target} SECURITY INVOKER")
            await connection.execute(f"ALTER FUNCTION {target} OWNER TO grove_migration")
            await connection.execute(f"REVOKE ALL ON FUNCTION {target} FROM PUBLIC, grove_runtime")
            await connection.execute(f"GRANT EXECUTE ON FUNCTION {target} TO PUBLIC, grove_migration")
            await connection.execute(f"ALTER FUNCTION {target} SET search_path = pg_catalog, public")
            await connection.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_preflight_rejects_same_named_trigger_target_signature_families() -> None:
    """Every protected tgfoid target must have an exact, fully pinned family."""

    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    raw_url = migration_url.replace("postgresql+psycopg://", "postgresql://", 1)
    overloads = (
        """
        CREATE OR REPLACE FUNCTION public.grove_reject_execution_fence_regression(text) RETURNS text
        LANGUAGE SQL AS $$ SELECT 'probe'::text $$
        """,
        """
        CREATE OR REPLACE FUNCTION public.grove_checkpoint_authority_guard(text) RETURNS text
        LANGUAGE SQL AS $$ SELECT 'probe'::text $$
        """,
        """
        CREATE OR REPLACE FUNCTION public.grove_checkpoint_physical_guard(text) RETURNS text
        LANGUAGE SQL AS $$ SELECT 'probe'::text $$
        """,
        """
        CREATE OR REPLACE FUNCTION public.grove_checkpoint_tenant_guard(text) RETURNS text
        LANGUAGE SQL AS $$ SELECT 'probe'::text $$
        """,
    )
    drops = (
        "DROP FUNCTION IF EXISTS public.grove_reject_execution_fence_regression(text)",
        "DROP FUNCTION IF EXISTS public.grove_checkpoint_authority_guard(text)",
        "DROP FUNCTION IF EXISTS public.grove_checkpoint_physical_guard(text)",
        "DROP FUNCTION IF EXISTS public.grove_checkpoint_tenant_guard(text)",
    )
    async with await psycopg.AsyncConnection.connect(raw_url) as connection:
        try:
            for creation_order in (overloads, tuple(reversed(overloads))):
                for definition in creation_order:
                    await connection.execute(definition)
                await connection.commit()
                with pytest.raises(ws3_preflight.WS3PreflightError, match="schema does not match"):
                    ws3_preflight.check(Path.cwd(), migration_url)
                for drop in drops:
                    await connection.execute(drop)
                await connection.commit()
                ws3_preflight.check(Path.cwd(), migration_url)
        finally:
            for drop in drops:
                await connection.execute(drop)
            await connection.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_preflight_rejects_function_overloads_before_and_after_legal_rebuild() -> None:
    """Function evidence uses complete identities, so overload order cannot hide drift."""

    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    raw_url = migration_url.replace("postgresql+psycopg://", "postgresql://", 1)
    overload_signature = "text, uuid, text"
    async with await psycopg.AsyncConnection.connect(raw_url) as connection:
        legal_definition = await connection.execute(
            "SELECT pg_get_functiondef('public.grove_reconcile_expired_run_command(text,uuid)'::regprocedure)"
        )
        legal_row = await legal_definition.fetchone()
        assert legal_row is not None
        legal_sql = legal_row[0]
        try:
            for rebuild_first in (False, True):
                if rebuild_first:
                    await connection.execute(legal_sql)
                await connection.execute(
                    "CREATE OR REPLACE FUNCTION public.grove_reconcile_expired_run_command(text, uuid, text) "
                    "RETURNS void LANGUAGE plpgsql SECURITY INVOKER AS $$ BEGIN RETURN; END $$"
                )
                await connection.commit()
                if not rebuild_first:
                    await connection.execute(legal_sql)
                    await connection.commit()
                with pytest.raises(ws3_preflight.WS3PreflightError, match="schema does not match"):
                    ws3_preflight.check(Path.cwd(), migration_url)
                await connection.execute(
                    f"DROP FUNCTION IF EXISTS public.grove_reconcile_expired_run_command({overload_signature})"
                )
                await connection.commit()
                ws3_preflight.check(Path.cwd(), migration_url)
        finally:
            await connection.execute(
                f"DROP FUNCTION IF EXISTS public.grove_reconcile_expired_run_command({overload_signature})"
            )
            await connection.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_takeover_same_pk_retry_preserves_apply_time_provenance() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-takeover-retry-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    owner_engine = create_async_engine(migration_url)
    driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
    raw_url = runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
    checkpoint = empty_checkpoint()
    checkpoint["id"] = "takeover-same-pk"
    checkpoint["channel_versions"] = {"state": "takeover-v1"}
    checkpoint["channel_values"] = {"state": {"answer": 42}}
    config = cast(RunnableConfig, {"configurable": {"thread_id": str(handle.run_id), "checkpoint_ns": ""}})
    try:
        old_claim = await driver.claim("takeover-old", runtime_hash, tenant_id, 10)
        assert old_claim is not None
        async with await psycopg.AsyncConnection.connect(raw_url) as raw_connection:
            saver = FencedPostgresSaver(raw_connection, old_claim)
            await saver.aput(
                config, checkpoint, cast(CheckpointMetadata, {"node": "apply"}), cast(Any, {"state": "takeover-v1"})
            )
            await saver.aput_writes(
                {
                    "configurable": {
                        "thread_id": str(old_claim.run_id),
                        "checkpoint_ns": "",
                        "checkpoint_id": checkpoint["id"],
                    }
                },
                [("state", {"pending": "value"})],
                "takeover-task",
                "takeover-path",
            )

        async with owner_engine.connect() as connection:
            before = (
                await connection.execute(
                    text(
                        "SELECT 'checkpoint' AS family, claim_command_id::text, claim_command_seq, "
                        "claim_command_digest, claim_worker_id, claim_execution_fence, claim_lease_until, "
                        "claim_runtime_build_hash, claim_provenance_hash, content_hash, metadata::text "
                        "FROM checkpoints WHERE tenant_id = :tenant AND checkpoint_id = :checkpoint "
                        "UNION ALL SELECT 'blob', claim_command_id::text, claim_command_seq, "
                        "claim_command_digest, claim_worker_id, claim_execution_fence, claim_lease_until, "
                        "claim_runtime_build_hash, claim_provenance_hash, content_hash, NULL "
                        "FROM checkpoint_blobs WHERE tenant_id = :tenant AND channel = 'state' "
                        "UNION ALL SELECT 'write', claim_command_id::text, claim_command_seq, "
                        "claim_command_digest, claim_worker_id, claim_execution_fence, claim_lease_until, "
                        "claim_runtime_build_hash, claim_provenance_hash, content_hash, NULL "
                        "FROM checkpoint_writes WHERE tenant_id = :tenant AND checkpoint_id = :checkpoint"
                    ),
                    {"tenant": tenant_id, "checkpoint": checkpoint["id"]},
                )
            ).all()
            projection_before = (
                await connection.execute(
                    text(
                        "SELECT latest_checkpoint_id, latest_applied_command_id, "
                        "latest_applied_command_seq, latest_applied_command_digest "
                        "FROM agent_run WHERE tenant_id = :tenant AND run_id = :run_id"
                    ),
                    {"tenant": tenant_id, "run_id": handle.run_id},
                )
            ).one()

        async with owner_engine.begin() as connection:
            expired_at = await connection.scalar(text("SELECT clock_timestamp() - interval '1 second'"))
            assert expired_at is not None
            await connection.execute(
                text("UPDATE agent_run SET lease_until = :expired_at WHERE tenant_id = :tenant AND run_id = :run_id"),
                {"expired_at": expired_at, "tenant": tenant_id, "run_id": handle.run_id},
            )
            await connection.execute(
                text(
                    "UPDATE run_command SET lease_until = :expired_at "
                    "WHERE tenant_id = :tenant AND command_id = :command_id"
                ),
                {"expired_at": expired_at, "tenant": tenant_id, "command_id": old_claim.command_id},
            )
        new_claim = await driver.claim("takeover-new", runtime_hash, tenant_id, 10)
        assert new_claim is not None and new_claim.execution_fence > old_claim.execution_fence
        async with await psycopg.AsyncConnection.connect(raw_url) as raw_connection:
            saver = FencedPostgresSaver(raw_connection, new_claim)
            await saver.aput(
                config, checkpoint, cast(CheckpointMetadata, {"node": "apply"}), cast(Any, {"state": "takeover-v1"})
            )
            await saver.aput_writes(
                {
                    "configurable": {
                        "thread_id": str(new_claim.run_id),
                        "checkpoint_ns": "",
                        "checkpoint_id": checkpoint["id"],
                    }
                },
                [("state", {"pending": "value"})],
                "takeover-task",
                "takeover-path",
            )
            changed = empty_checkpoint()
            changed["id"] = checkpoint["id"]
            changed["channel_versions"] = {"state": "takeover-v1"}
            changed["channel_values"] = {"state": {"answer": "changed"}}
            with pytest.raises((psycopg.errors.CheckViolation, psycopg.errors.SerializationFailure)):
                await saver.aput(
                    config,
                    changed,
                    cast(CheckpointMetadata, {"node": "apply"}),
                    cast(Any, {"state": "takeover-v1"}),
                )

        async with owner_engine.connect() as connection:
            after = (
                await connection.execute(
                    text(
                        "SELECT 'checkpoint' AS family, claim_command_id::text, claim_command_seq, "
                        "claim_command_digest, claim_worker_id, claim_execution_fence, claim_lease_until, "
                        "claim_runtime_build_hash, claim_provenance_hash, content_hash, metadata::text "
                        "FROM checkpoints WHERE tenant_id = :tenant AND checkpoint_id = :checkpoint "
                        "UNION ALL SELECT 'blob', claim_command_id::text, claim_command_seq, "
                        "claim_command_digest, claim_worker_id, claim_execution_fence, claim_lease_until, "
                        "claim_runtime_build_hash, claim_provenance_hash, content_hash, NULL "
                        "FROM checkpoint_blobs WHERE tenant_id = :tenant AND channel = 'state' "
                        "UNION ALL SELECT 'write', claim_command_id::text, claim_command_seq, "
                        "claim_command_digest, claim_worker_id, claim_execution_fence, claim_lease_until, "
                        "claim_runtime_build_hash, claim_provenance_hash, content_hash, NULL "
                        "FROM checkpoint_writes WHERE tenant_id = :tenant AND checkpoint_id = :checkpoint"
                    ),
                    {"tenant": tenant_id, "checkpoint": checkpoint["id"]},
                )
            ).all()
            projection_after = (
                await connection.execute(
                    text(
                        "SELECT latest_checkpoint_id, latest_applied_command_id, "
                        "latest_applied_command_seq, latest_applied_command_digest "
                        "FROM agent_run WHERE tenant_id = :tenant AND run_id = :run_id"
                    ),
                    {"tenant": tenant_id, "run_id": handle.run_id},
                )
            ).one()
            assert after == before
            assert projection_after == projection_before

        receipt = await driver.consume(new_claim)
        assert receipt.status == "consumed"
    finally:
        await runtime_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_graph_interrupt_recovery_reuses_checkpoint_and_pending_writes() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-graph-recovery-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    owner_engine = create_async_engine(migration_url)
    driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
    raw_url = runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
    config = cast(RunnableConfig, {"configurable": {"thread_id": str(handle.run_id)}})
    gate_calls = 0
    finish_calls = 0

    class RecoveryState(TypedDict):
        count: int
        answer: str

    def gate(state: RecoveryState) -> dict[str, str]:
        nonlocal gate_calls
        gate_calls += 1
        return {"answer": interrupt("provide answer")}

    def finish(state: RecoveryState) -> dict[str, int]:
        nonlocal finish_calls
        finish_calls += 1
        return {"count": state["count"] + 1}

    builder = StateGraph(RecoveryState)
    builder.add_node("gate", gate)
    builder.add_node("finish", finish)
    builder.add_edge(START, "gate")
    builder.add_edge("gate", "finish")
    builder.add_edge("finish", END)
    try:
        claim = await driver.claim("graph-recovery-worker", runtime_hash, tenant_id, 10)
        assert claim is not None
        async with await psycopg.AsyncConnection.connect(raw_url) as raw_connection:
            saver = FencedPostgresSaver(raw_connection, claim)
            first_graph = builder.compile(checkpointer=saver)
            first_result = await first_graph.ainvoke({"count": 0, "answer": ""}, config)
            assert first_result["count"] == 0

        async with owner_engine.connect() as connection:
            pending_count = await connection.scalar(
                text("SELECT count(*) FROM checkpoint_writes WHERE tenant_id = :tenant"),
                {"tenant": tenant_id},
            )
            assert pending_count and pending_count >= 1

        async with await psycopg.AsyncConnection.connect(raw_url) as raw_connection:
            saver = FencedPostgresSaver(raw_connection, claim)
            recovered = await saver.aget_tuple(config)
            assert recovered is not None
            resumed_graph = builder.compile(checkpointer=saver)
            final_result = await resumed_graph.ainvoke(Command(resume="accepted"), config)
            assert final_result == {"count": 1, "answer": "accepted"}

        assert gate_calls == 2
        assert finish_calls == 1
        receipt = await driver.consume(claim)
        assert receipt.status == "consumed"
        async with owner_engine.connect() as connection:
            projection = (
                await connection.execute(
                    text(
                        "SELECT latest_checkpoint_id, latest_applied_command_seq "
                        "FROM agent_run WHERE tenant_id = :tenant"
                    ),
                    {"tenant": tenant_id},
                )
            ).one()
            assert projection[0]
            assert projection[1] == claim.command_seq
    finally:
        await runtime_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_database_rejects_forged_checkpoint_hash_without_python_saver() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-forged-hash-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    owner_engine = create_async_engine(migration_url)
    driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
    raw_url = runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        claim = await driver.claim("forged-hash-worker", runtime_hash, tenant_id, 10)
        assert claim is not None and claim.run_id == handle.run_id
        async with await psycopg.AsyncConnection.connect(raw_url) as raw_connection:
            saver = FencedPostgresSaver(raw_connection, claim)
            async with raw_connection.transaction():
                await saver._set_claim_context()
                with pytest.raises(psycopg.errors.InvalidParameterValue):
                    async with raw_connection.cursor() as cursor:
                        await cursor.execute(
                            "INSERT INTO checkpoints "
                            "(tenant_id, thread_id, checkpoint_id, checkpoint, metadata) "
                            "VALUES (%s, %s, %s, %s::jsonb, %s::jsonb)",
                            (
                                tenant_id,
                                str(claim.run_id),
                                "forged-hash",
                                '{"v":1,"id":"forged-hash","channel_values":{},"channel_versions":{},"versions_seen":{}}',
                                '{"checkpoint_hash":"' + "f" * 64 + '"}',
                            ),
                        )
        async with owner_engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM checkpoints WHERE tenant_id = :tenant"),
                    {"tenant": tenant_id},
                )
                == 0
            )
    finally:
        await runtime_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stale_claim_cannot_write_blob_or_pending_write_rows() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-stale-aux-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    owner_engine = create_async_engine(migration_url)
    driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
    raw_url = runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        stale_claim = await driver.claim("stale-aux-worker", runtime_hash, tenant_id, 0.1)
        assert stale_claim is not None and stale_claim.run_id == handle.run_id
        await asyncio.sleep(0.2)
        current_claim = await driver.claim("current-aux-worker", runtime_hash, tenant_id, 10)
        assert current_claim is not None and current_claim.execution_fence == stale_claim.execution_fence + 1
        config = cast(
            RunnableConfig,
            {"configurable": {"thread_id": str(stale_claim.run_id), "checkpoint_ns": ""}},
        )
        checkpoint = empty_checkpoint()
        checkpoint["channel_versions"] = {"state": "1"}
        checkpoint["channel_values"] = {"state": {"nested": 1}}
        async with await psycopg.AsyncConnection.connect(raw_url) as raw_connection:
            saver = FencedPostgresSaver(raw_connection, stale_claim)
            with pytest.raises(psycopg.errors.SerializationFailure):
                await saver.aput(config, checkpoint, cast(CheckpointMetadata, {}), {"state": "1"})
            with pytest.raises(psycopg.errors.SerializationFailure):
                await saver.aput_writes(
                    {
                        "configurable": {
                            "thread_id": str(stale_claim.run_id),
                            "checkpoint_ns": "",
                            "checkpoint_id": checkpoint["id"],
                        }
                    },
                    [("state", {"pending": 1})],
                    "stale-task",
                )
        async with owner_engine.connect() as connection:
            counts = (
                await connection.execute(
                    text(
                        "SELECT (SELECT count(*) FROM checkpoints WHERE tenant_id = :tenant), "
                        "(SELECT count(*) FROM checkpoint_blobs WHERE tenant_id = :tenant), "
                        "(SELECT count(*) FROM checkpoint_writes WHERE tenant_id = :tenant)"
                    ),
                    {"tenant": tenant_id},
                )
            ).one()
            assert counts == (0, 0, 0)
    finally:
        await runtime_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_same_checkpoint_id_rejects_parent_or_physical_clobber() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-checkpoint-conflict-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    owner_engine = create_async_engine(migration_url)
    driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
    raw_url = runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        claim = await driver.claim("checkpoint-conflict-worker", runtime_hash, tenant_id, 10)
        assert claim is not None and claim.run_id == handle.run_id
        config = cast(
            RunnableConfig,
            {"configurable": {"thread_id": str(claim.run_id), "checkpoint_ns": ""}},
        )
        first = empty_checkpoint()
        first["id"] = "same-id"
        first["channel_versions"] = {"state": "1"}
        async with await psycopg.AsyncConnection.connect(raw_url) as raw_connection:
            saver = FencedPostgresSaver(raw_connection, claim)
            await saver.aput(config, first, cast(CheckpointMetadata, {}), {"state": "1"})
            changed = empty_checkpoint()
            changed["id"] = "same-id"
            changed["channel_versions"] = {"state": "2"}
            changed["channel_values"] = {"state": {"changed": True}}
            with pytest.raises((psycopg.errors.CheckViolation, psycopg.errors.InvalidParameterValue)):
                await saver.aput(
                    {
                        "configurable": {
                            "thread_id": str(claim.run_id),
                            "checkpoint_ns": "",
                            "checkpoint_id": "different-parent",
                        }
                    },
                    changed,
                    cast(CheckpointMetadata, {}),
                    {"state": "2"},
                )
        async with owner_engine.connect() as connection:
            stored = (
                await connection.execute(
                    text(
                        "SELECT checkpoint->'channel_values', content_hash FROM checkpoints "
                        "WHERE tenant_id = :tenant AND checkpoint_id = 'same-id'"
                    ),
                    {"tenant": tenant_id},
                )
            ).one()
            assert stored[0] == {}
            assert len(stored[1]) == 64
    finally:
        await runtime_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stale_claim_cannot_write_checkpoint_after_takeover() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-stale-checkpoint-{uuid.uuid4().hex[:12]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    owner_engine = create_async_engine(migration_url)
    driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
    raw_url = runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        stale_claim = await driver.claim("stale-checkpoint-worker", runtime_hash, tenant_id, 0.1)
        assert stale_claim is not None and stale_claim.run_id == handle.run_id
        await asyncio.sleep(0.2)
        current_claim = await driver.claim("current-checkpoint-worker", runtime_hash, tenant_id, 10)
        assert current_claim is not None
        assert current_claim.execution_fence == stale_claim.execution_fence + 1
        config = cast(
            RunnableConfig,
            {
                "configurable": {
                    "thread_id": str(stale_claim.run_id),
                    "checkpoint_ns": "",
                }
            },
        )
        stale_checkpoint = empty_checkpoint()
        stale_checkpoint["channel_versions"] = {"state": "1"}
        async with await psycopg.AsyncConnection.connect(raw_url) as raw_connection:
            saver = FencedPostgresSaver(raw_connection, stale_claim)
            with pytest.raises(psycopg.errors.SerializationFailure):
                await saver.aput(
                    config,
                    stale_checkpoint,
                    cast(CheckpointMetadata, {}),
                    {"state": "1"},
                )

        async with owner_engine.connect() as connection:
            checkpoint_count = await connection.scalar(
                text("SELECT count(*) FROM checkpoints WHERE tenant_id = :tenant"),
                {"tenant": tenant_id},
            )
            assert checkpoint_count == 0
    finally:
        await runtime_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_checkpoint_acl_and_rls_reject_cross_tenant_access() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_a = f"it-ws3-rls-a-{uuid.uuid4().hex[:10]}"
    tenant_b = f"it-ws3-rls-b-{uuid.uuid4().hex[:10]}"
    _, runtime_hash = await _submit_start(api_url, migration_url, tenant_b)
    runtime_engine = create_async_engine(runtime_url)
    api_engine = create_async_engine(api_url)
    driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
    raw_url = runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        claim = await driver.claim("rls-worker", runtime_hash, tenant_b, 10)
        assert claim is not None
        config = cast(
            RunnableConfig,
            {"configurable": {"thread_id": str(claim.run_id), "checkpoint_ns": ""}},
        )
        checkpoint = empty_checkpoint()
        checkpoint["channel_versions"] = {"state": "1"}
        async with await psycopg.AsyncConnection.connect(raw_url) as raw_connection:
            saver = FencedPostgresSaver(raw_connection, claim)
            await saver.aput(config, checkpoint, cast(CheckpointMetadata, {}), {"state": "1"})

        async with runtime_engine.begin() as connection:
            await connection.execute(text("SELECT set_config('grove.tenant_id', :tenant, true)"), {"tenant": tenant_a})
            assert await connection.scalar(text("SELECT count(*) FROM checkpoints")) == 0
            with pytest.raises(SQLAlchemyError):
                await connection.execute(
                    text(
                        "INSERT INTO checkpoints (tenant_id, thread_id, checkpoint_id, checkpoint, metadata) "
                        "VALUES (:tenant, :thread_id, 'forged', '{}'::jsonb, '{}'::jsonb)"
                    ),
                    {"tenant": tenant_b, "thread_id": str(claim.run_id)},
                )
        with pytest.raises(SQLAlchemyError):
            async with api_engine.connect() as connection:
                await connection.execute(text("SELECT count(*) FROM checkpoints"))
    finally:
        await runtime_engine.dispose()
        await api_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_same_valid_claim_can_commit_multiple_checkpoint_identities() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-checkpoint-sequence-{uuid.uuid4().hex[:10]}"
    _, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    owner_engine = create_async_engine(migration_url)
    driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
    raw_url = runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        claim = await driver.claim("sequence-worker", runtime_hash, tenant_id, 10)
        assert claim is not None

        async def write_checkpoint(checkpoint_id: str, parent_id: str | None = None) -> RunnableConfig:
            configurable: dict[str, str] = {"thread_id": str(claim.run_id), "checkpoint_ns": ""}
            if parent_id is not None:
                configurable["checkpoint_id"] = parent_id
            config = cast(
                RunnableConfig,
                {"configurable": configurable},
            )
            checkpoint = empty_checkpoint()
            checkpoint["id"] = checkpoint_id
            checkpoint["channel_versions"] = {"state": "1"}
            async with await psycopg.AsyncConnection.connect(raw_url) as raw_connection:
                saver = FencedPostgresSaver(raw_connection, claim)
                return await saver.aput(config, checkpoint, cast(CheckpointMetadata, {}), {"state": "1"})

        first_config = await write_checkpoint("sequence-a")
        second_config = await write_checkpoint("sequence-b", "sequence-a")
        assert first_config["configurable"]["checkpoint_id"] == "sequence-a"
        assert second_config["configurable"]["checkpoint_id"] == "sequence-b"
        async with owner_engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM checkpoints WHERE tenant_id = :tenant"),
                    {"tenant": tenant_id},
                )
            ) == 2
            projection = (
                await connection.execute(
                    text(
                        "SELECT latest_checkpoint_id, latest_applied_command_seq FROM agent_run "
                        "WHERE tenant_id = :tenant"
                    ),
                    {"tenant": tenant_id},
                )
            ).one()
            assert projection == ("sequence-b", claim.command_seq)
    finally:
        await runtime_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_consume_sequence_equal_retry_higher_and_superseded_lower() -> None:
    """Equal retry is idempotent; explicit cancel closure consumes a lower fixture claim."""

    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-sequence-consume-{uuid.uuid4().hex[:10]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    followup_id = uuid.uuid4()
    await _insert_followup_command(
        migration_url,
        tenant_id=tenant_id,
        run_id=handle.run_id,
        command_id=followup_id,
        command_seq=1,
        command_digest="2" * 64,
        command_type="cancel",
        command_schema_version="cancel.v1",
    )
    runtime_engine = create_async_engine(runtime_url)
    owner_engine = create_async_engine(migration_url)
    driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
    raw_url = runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        # A future-dated cancel lets this fixture obtain the original claim
        # before the explicit supersede proof is installed.  Production claim
        # never reclaims the superseded row below.
        async with owner_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE run_command SET available_at = clock_timestamp() + interval '2 seconds' "
                    "WHERE tenant_id = :tenant AND command_id = :command_id"
                ),
                {"tenant": tenant_id, "command_id": followup_id},
            )
        lower_claim = await driver.claim("sequence-lower", runtime_hash, tenant_id, 0.5)
        assert lower_claim is not None and lower_claim.command_seq == 0
        async with await psycopg.AsyncConnection.connect(raw_url) as raw_connection:
            saver = FencedPostgresSaver(raw_connection, lower_claim)
            checkpoint = empty_checkpoint()
            checkpoint["id"] = "sequence-lower-proof"
            checkpoint["channel_versions"] = {"state": "1"}
            await saver.aput(
                {"configurable": {"thread_id": str(lower_claim.run_id), "checkpoint_ns": ""}},
                checkpoint,
                cast(CheckpointMetadata, {}),
                {"state": "1"},
            )

        # Keep the lower command out of the ready queue while its original
        # lease expires, then make the cancel ready.  The placeholder proof is
        # replaced with the exact apply-time claim provenance after the cancel
        # is claimed.
        async with owner_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE run_command SET available_at = clock_timestamp() + interval '2 seconds', "
                    "superseded_by_command_id = :higher_id, superseded_by_command_seq = 1, "
                    "superseded_by_command_digest = :higher_digest, superseded_by_provenance_hash = :provenance "
                    "WHERE tenant_id = :tenant AND run_id = :run_id AND command_seq = 0"
                ),
                {
                    "tenant": tenant_id,
                    "run_id": handle.run_id,
                    "higher_id": followup_id,
                    "higher_digest": "2" * 64,
                    "provenance": "a" * 64,
                },
            )
            await connection.execute(
                text(
                    "UPDATE run_command SET available_at = clock_timestamp() "
                    "WHERE tenant_id = :tenant AND command_id = :higher_id"
                ),
                {"tenant": tenant_id, "higher_id": followup_id},
            )
            await connection.execute(
                text("UPDATE agent_run SET status = 'cancel_requested' WHERE tenant_id = :tenant AND run_id = :run_id"),
                {"tenant": tenant_id, "run_id": handle.run_id},
            )
        await asyncio.sleep(0.7)

        higher_claim = await driver.claim("sequence-higher", runtime_hash, tenant_id, 10)
        assert higher_claim is not None and higher_claim.command_seq == 1
        async with owner_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE run_command SET superseded_by_provenance_hash = "
                    "grove_checkpoint_claim_provenance(:tenant, :run_id, :command_id, :command_seq, "
                    ":command_digest, :build_hash, :worker_id, :fence, :lease_until) "
                    "WHERE tenant_id = :tenant AND command_id = :lower_id"
                ),
                {
                    "tenant": tenant_id,
                    "run_id": handle.run_id,
                    "command_id": higher_claim.command_id,
                    "command_seq": higher_claim.command_seq,
                    "command_digest": higher_claim.command_digest,
                    "build_hash": higher_claim.runtime_build_hash,
                    "worker_id": higher_claim.worker_id,
                    "fence": higher_claim.execution_fence,
                    "lease_until": higher_claim.lease_until,
                    "lower_id": lower_claim.command_id,
                },
            )
        async with await psycopg.AsyncConnection.connect(raw_url) as raw_connection:
            saver = FencedPostgresSaver(raw_connection, higher_claim)
            checkpoint = empty_checkpoint()
            checkpoint["id"] = "sequence-higher-proof"
            checkpoint["channel_versions"] = {"state": "2"}
            await saver.aput(
                {"configurable": {"thread_id": str(higher_claim.run_id), "checkpoint_ns": ""}},
                checkpoint,
                cast(CheckpointMetadata, {}),
                {"state": "2"},
            )
        higher_receipt = await driver.consume(higher_claim)
        assert higher_receipt.status == "consumed"
        assert higher_receipt.command_seq == 1
        assert await driver.consume(higher_claim) == higher_receipt

        async with owner_engine.connect() as connection:
            closed_higher = (
                await connection.execute(
                    text(
                        "SELECT status, lease_owner, lease_until, consumed_worker_id "
                        "FROM run_command WHERE tenant_id = :tenant AND command_id = :command_id"
                    ),
                    {"tenant": tenant_id, "command_id": higher_claim.command_id},
                )
            ).one()
            assert closed_higher == ("consumed", None, None, higher_claim.worker_id)

        assert await driver.claim("sequence-lower-retry", runtime_hash, tenant_id, 10) is None
        superseded_receipt = await driver.consume(lower_claim)
        assert superseded_receipt.status == "consumed"
        assert superseded_receipt.command_seq == 0

        async with owner_engine.connect() as connection:
            physical_count = await connection.scalar(
                text("SELECT count(*) FROM checkpoints WHERE tenant_id = :tenant"),
                {"tenant": tenant_id},
            )
            assert physical_count == 2
            closure = (
                await connection.execute(
                    text(
                        "SELECT status, superseded_by_command_id, superseded_by_command_seq, "
                        "superseded_by_command_digest, superseded_by_provenance_hash "
                        "FROM run_command WHERE tenant_id = :tenant AND command_id = :command_id"
                    ),
                    {"tenant": tenant_id, "command_id": lower_claim.command_id},
                )
            ).one()
            assert closure[0] == "consumed"
            assert closure[1:4] == (higher_claim.command_id, higher_claim.command_seq, higher_claim.command_digest)
            assert len(closure[4]) == 64
            projection = (
                await connection.execute(
                    text(
                        "SELECT latest_checkpoint_id, latest_applied_command_seq "
                        "FROM agent_run WHERE tenant_id = :tenant AND run_id = :run_id"
                    ),
                    {"tenant": tenant_id, "run_id": handle.run_id},
                )
            ).one()
            assert projection == ("sequence-higher-proof", 1)
    finally:
        await runtime_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_higher_projection_without_supersede_closure_cannot_consume_lower() -> None:
    """A forged higher projection is not an implicit supersede proof."""

    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-no-closure-{uuid.uuid4().hex[:10]}"
    handle, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    followup_id = uuid.uuid4()
    followup_digest = "3" * 64
    await _insert_followup_command(
        migration_url,
        tenant_id=tenant_id,
        run_id=handle.run_id,
        command_id=followup_id,
        command_seq=1,
        command_digest=followup_digest,
        command_type="cancel",
        command_schema_version="cancel.v1",
    )
    owner_seed = create_async_engine(migration_url)
    try:
        async with owner_seed.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE run_command SET available_at = clock_timestamp() + interval '2 seconds' "
                    "WHERE tenant_id = :tenant AND command_id = :command_id"
                ),
                {"tenant": tenant_id, "command_id": followup_id},
            )
    finally:
        await owner_seed.dispose()
    runtime_engine = create_async_engine(runtime_url)
    owner_engine = create_async_engine(migration_url)
    driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
    try:
        lower_claim = await driver.claim("no-closure-worker", runtime_hash, tenant_id, 10)
        assert lower_claim is not None and lower_claim.command_seq == 0
        async with owner_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE agent_run SET latest_checkpoint_id = 'forged-higher', "
                    "latest_applied_command_id = :command_id, latest_applied_command_digest = :digest, "
                    "latest_applied_command_seq = 1 WHERE tenant_id = :tenant AND run_id = :run_id"
                ),
                {
                    "tenant": tenant_id,
                    "run_id": handle.run_id,
                    "command_id": followup_id,
                    "digest": followup_digest,
                },
            )
        with pytest.raises(RunStateConflict, match="checkpoint proof"):
            await driver.consume(lower_claim)
        async with owner_engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT status, superseded_by_command_id, consumed_claim_provenance_hash "
                        "FROM run_command WHERE tenant_id = :tenant AND command_id = :command_id"
                    ),
                    {"tenant": tenant_id, "command_id": lower_claim.command_id},
                )
            ).one()
            assert row == ("leased", None, None)
    finally:
        await runtime_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_checkpoint_claims_are_isolated_for_concurrent_tenants() -> None:
    """Two tenants may write concurrently without sharing claim or physical rows."""

    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_ids = (f"it-ws3-concurrent-a-{uuid.uuid4().hex[:8]}", f"it-ws3-concurrent-b-{uuid.uuid4().hex[:8]}")
    submitted = await asyncio.gather(*(_submit_start(api_url, migration_url, tenant) for tenant in tenant_ids))
    runtime_engine = create_async_engine(runtime_url)
    driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
    raw_url = runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)

    async def claim_and_write(tenant: str, runtime_hash: str, marker: str) -> tuple[str, str]:
        claim = await driver.claim(f"concurrent-{marker}", runtime_hash, tenant, 10)
        assert claim is not None
        async with await psycopg.AsyncConnection.connect(raw_url) as raw_connection:
            saver = FencedPostgresSaver(raw_connection, claim)
            checkpoint = empty_checkpoint()
            checkpoint["id"] = f"concurrent-{marker}"
            checkpoint["channel_versions"] = {"state": marker}
            await saver.aput(
                {"configurable": {"thread_id": str(claim.run_id), "checkpoint_ns": ""}},
                checkpoint,
                cast(CheckpointMetadata, {"tenant_marker": marker}),
                {"state": marker},
            )
        return tenant, str(claim.run_id)

    try:
        identities = await asyncio.gather(
            *(
                claim_and_write(tenant, submission[1], marker)
                for marker, tenant, submission in zip(("a", "b"), tenant_ids, submitted, strict=True)
            )
        )
        owner_engine = create_async_engine(migration_url)
        try:
            async with owner_engine.connect() as connection:
                rows = (
                    await connection.execute(
                        text(
                            "SELECT tenant_id, count(*), min(claim_worker_id), min(metadata->>'tenant_marker') "
                            "FROM checkpoints WHERE tenant_id IN (:tenant_a, :tenant_b) "
                            "GROUP BY tenant_id ORDER BY tenant_id"
                        ),
                        {"tenant_a": tenant_ids[0], "tenant_b": tenant_ids[1]},
                    )
                ).all()
                assert [(row[0], row[1]) for row in rows] == [(tenant_ids[0], 1), (tenant_ids[1], 1)]
                assert {row[3] for row in rows} == {"a", "b"}
                assert {tenant for tenant, _run_id in identities} == set(tenant_ids)
        finally:
            await owner_engine.dispose()
    finally:
        await runtime_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_checkpoint_commit_then_worker_crash_allows_takeover_consume_without_reapply() -> None:
    api_url = os.environ["GROVE_DATABASE_URL"]
    migration_url = os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        _role_url(api_url, "grove_migration", "grove_migration_ws0"),
    )
    runtime_url = _role_url(api_url, "grove_runtime", "grove_runtime_ws0")
    tenant_id = f"it-ws3-crash-window-{uuid.uuid4().hex[:10]}"
    _, runtime_hash = await _submit_start(api_url, migration_url, tenant_id)
    runtime_engine = create_async_engine(runtime_url)
    owner_engine = create_async_engine(migration_url)
    driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
    raw_url = runtime_url.replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        # The checkpoint commit must happen while the original claim is valid;
        # the crash window begins only after that durable write.  A 100 ms
        # lease made this test depend on host load and could expire during the
        # connection/checkpoint setup itself.
        old_claim = await driver.claim("crashed-worker", runtime_hash, tenant_id, 5.0)
        assert old_claim is not None
        config = cast(
            RunnableConfig,
            {"configurable": {"thread_id": str(old_claim.run_id), "checkpoint_ns": ""}},
        )
        checkpoint = empty_checkpoint()
        checkpoint["channel_versions"] = {"state": "1"}
        async with await psycopg.AsyncConnection.connect(raw_url) as raw_connection:
            saver = FencedPostgresSaver(raw_connection, old_claim)
            await saver.aput(config, checkpoint, cast(CheckpointMetadata, {}), {"state": "1"})
        async with owner_engine.connect() as connection:
            before_takeover = await connection.scalar(
                text("SELECT count(*) FROM checkpoints WHERE tenant_id = :tenant"),
                {"tenant": tenant_id},
            )
        assert before_takeover == 1

        await asyncio.sleep(5.1)
        new_claim = await driver.claim("reclaimed-worker", runtime_hash, tenant_id, 10)
        assert new_claim is not None
        assert new_claim.execution_fence == old_claim.execution_fence + 1
        receipt = await driver.consume(new_claim)
        assert receipt.status == "consumed"
        with pytest.raises(StaleExecutionFence):
            await driver.consume(old_claim)
        assert await driver.consume(new_claim) == receipt

        async with owner_engine.connect() as connection:
            after_consume = await connection.scalar(
                text("SELECT count(*) FROM checkpoints WHERE tenant_id = :tenant"),
                {"tenant": tenant_id},
            )
            assert after_consume == before_takeover
            projection = (
                await connection.execute(
                    text(
                        "SELECT latest_checkpoint_id, latest_applied_command_seq FROM agent_run "
                        "WHERE tenant_id = :tenant"
                    ),
                    {"tenant": tenant_id},
                )
            ).one()
            assert projection == (checkpoint["id"], old_claim.command_seq)
    finally:
        await runtime_engine.dispose()
        await owner_engine.dispose()
