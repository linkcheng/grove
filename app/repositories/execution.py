"""Tenant-scoped persistence and authorization operations for WS-2."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from time import monotonic
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import ActiveTenantContext, PrincipalKind
from app.models.execution import (
    AgentRun,
    CommandPayload,
    ExecutionPrincipal,
    ExecutionSpec,
    Membership,
    RunCommand,
    Tenant,
    WorkloadPrincipal,
)

ALLOWED_OPERATIONS = frozenset({"execution.submit", "execution.query"})
SUBMISSION_LOCK_TIMEOUT_SECONDS = 3.0
SUBMISSION_LOCK_RETRY_INTERVAL_SECONDS = 0.05


class SubmissionLockTimeoutError(TimeoutError):
    """The bounded idempotency lock could not be acquired in time."""


async def set_tenant_scope(session: AsyncSession, context: ActiveTenantContext) -> None:
    """Bind the trusted tenant to the current transaction for PostgreSQL RLS."""

    await session.execute(
        text("SELECT set_config('grove.tenant_id', :tenant_id, true)"), {"tenant_id": context.tenant_id}
    )


async def lock_submission(session: AsyncSession, context: ActiveTenantContext, submission_id: UUID) -> None:
    """Serialize one tenant/submission transaction with a bounded try-lock.

    The advisory key is derived from the trusted tenant and submission only;
    callers must still perform the tenant-scoped read after acquiring it.
    ``pg_advisory_xact_lock`` is intentionally not used here: a stuck
    transaction must fail as a dependency timeout rather than hold an API
    request forever.
    """
    lock_key = f"grove.ws2.submit:{context.tenant_id}:{submission_id}"
    deadline = monotonic() + SUBMISSION_LOCK_TIMEOUT_SECONDS
    while True:
        result = await session.execute(
            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": lock_key},
        )
        if bool(result.scalar_one()):
            return
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise SubmissionLockTimeoutError("submission advisory lock timed out")
        await asyncio.sleep(min(SUBMISSION_LOCK_RETRY_INTERVAL_SECONDS, remaining))


async def authorize_operation(session: AsyncSession, context: ActiveTenantContext, operation: str) -> tuple[str, ...]:
    """Authorize only a closed operation against live tenant database state."""

    if operation not in ALLOWED_OPERATIONS:
        return ()
    tenant_status = await session.scalar(select(Tenant.status).where(Tenant.tenant_id == context.tenant_id))
    if tenant_status != "active":
        return ()
    principal_active = await session.scalar(
        select(ExecutionPrincipal.active).where(
            ExecutionPrincipal.tenant_id == context.tenant_id,
            ExecutionPrincipal.principal_id == context.principal.principal_id,
            ExecutionPrincipal.principal_kind == context.principal.kind.value,
        )
    )
    if principal_active is False:
        return ()
    if context.principal.kind is PrincipalKind.HUMAN:
        row = await session.execute(
            select(Membership.active, Membership.roles).where(
                Membership.tenant_id == context.tenant_id,
                Membership.principal_id == context.principal.principal_id,
                Membership.principal_kind == "human",
            )
        )
    else:
        row = await session.execute(
            select(WorkloadPrincipal.active, WorkloadPrincipal.scopes).where(
                WorkloadPrincipal.tenant_id == context.tenant_id,
                WorkloadPrincipal.principal_id == context.principal.principal_id,
                WorkloadPrincipal.principal_kind == "workload",
            )
        )
    record = row.one_or_none()
    if record is None or record[0] is not True:
        return ()
    grants = record[1]
    if not isinstance(grants, list) or any(not isinstance(item, str) for item in grants):
        return ()
    # Unknown grants are rejected rather than silently treated as broader
    # permissions. Credentials never contribute to this set.
    if any(item not in ALLOWED_OPERATIONS for item in grants):
        return ()
    effective = tuple(sorted(set(grants)))
    return effective if operation in effective else ()


async def authorize_owned_run_query(
    session: AsyncSession,
    context: ActiveTenantContext,
    run_id: UUID,
) -> bool:
    """Authorize ``execution.query`` and run ownership in one RLS-scoped read.

    SSE can have hundreds of concurrent clients.  Replaying the general
    authorization helper as four separate round trips on every durable-cursor
    iteration would make idle observation traffic consume the API pool.  This
    query preserves the same live authority inputs and closed-grant semantics
    while proving ownership atomically in one database snapshot.
    """

    allowed = json.dumps(sorted(ALLOWED_OPERATIONS))
    result = await session.execute(
        text(
            """
            SELECT EXISTS (
                SELECT 1
                  FROM tenant AS t
                  JOIN execution_principal AS ep
                    ON ep.tenant_id = t.tenant_id
                  LEFT JOIN membership AS m
                    ON ep.principal_kind = 'human'
                   AND m.tenant_id = ep.tenant_id
                   AND m.principal_id = ep.principal_id
                   AND m.principal_kind = ep.principal_kind
                  LEFT JOIN workload_principal AS w
                    ON ep.principal_kind = 'workload'
                   AND w.tenant_id = ep.tenant_id
                   AND w.principal_id = ep.principal_id
                   AND w.principal_kind = ep.principal_kind
                  JOIN agent_run AS r
                    ON r.tenant_id = ep.tenant_id
                   AND r.principal_id = ep.principal_id
                   AND r.principal_kind = ep.principal_kind
                 WHERE t.tenant_id = :tenant_id
                   AND t.status = 'active'
                   AND ep.principal_id = :principal_id
                   AND ep.principal_kind = :principal_kind
                   AND ep.active IS TRUE
                   AND CASE ep.principal_kind
                       WHEN 'human' THEN m.active
                       WHEN 'workload' THEN w.active
                       ELSE false
                   END IS TRUE
                   AND jsonb_typeof(CASE ep.principal_kind
                       WHEN 'human' THEN m.roles
                       WHEN 'workload' THEN w.scopes
                   END) = 'array'
                   AND (CASE ep.principal_kind
                       WHEN 'human' THEN m.roles
                       WHEN 'workload' THEN w.scopes
                   END) ? 'execution.query'
                   AND (CASE ep.principal_kind
                       WHEN 'human' THEN m.roles
                       WHEN 'workload' THEN w.scopes
                   END) <@ CAST(:allowed AS jsonb)
                   AND r.run_id = :run_id
            )
            """
        ),
        {
            "tenant_id": context.tenant_id,
            "principal_id": context.principal.principal_id,
            "principal_kind": context.principal.kind.value,
            "allowed": allowed,
            "run_id": run_id,
        },
    )
    return bool(result.scalar_one())


async def get_run_by_submission(
    session: AsyncSession, context: ActiveTenantContext, submission_id: UUID, *, lock: bool = False
) -> AgentRun | None:
    query = select(AgentRun).where(AgentRun.tenant_id == context.tenant_id, AgentRun.submission_id == submission_id)
    if lock:
        query = query.with_for_update()
    return (await session.execute(query)).scalar_one_or_none()


async def get_run(
    session: AsyncSession, context: ActiveTenantContext, run_id: UUID, *, lock: bool = False
) -> AgentRun | None:
    query = select(AgentRun).where(AgentRun.tenant_id == context.tenant_id, AgentRun.run_id == run_id)
    if lock:
        query = query.with_for_update()
    return (await session.execute(query)).scalar_one_or_none()


async def insert_spec_if_absent(
    session: AsyncSession,
    *,
    context: ActiveTenantContext,
    skill_spec_hash: str,
    spec_ref: str,
    spec_payload: dict[str, Any],
) -> ExecutionSpec:
    result = await session.execute(
        pg_insert(ExecutionSpec)
        .values(
            tenant_id=context.tenant_id,
            skill_spec_hash=skill_spec_hash,
            spec_ref=spec_ref,
            spec_payload=spec_payload,
        )
        .on_conflict_do_nothing(index_elements=[ExecutionSpec.tenant_id, ExecutionSpec.skill_spec_hash])
        .returning(ExecutionSpec.skill_spec_hash)
    )
    if result.scalar_one_or_none() is None:
        # The API role is not allowed to read the stored spec JSON.  The
        # content-addressed hash/ref pair is the idempotency proof; return a
        # local snapshot using the request's already-validated bytes.
        existing = (
            await session.execute(
                select(ExecutionSpec.skill_spec_hash, ExecutionSpec.spec_ref).where(
                    ExecutionSpec.tenant_id == context.tenant_id,
                    ExecutionSpec.skill_spec_hash == skill_spec_hash,
                )
            )
        ).one_or_none()
        if existing is None or existing[0] != skill_spec_hash or existing[1] != spec_ref:
            raise ValueError("immutable execution spec content conflict")
    return ExecutionSpec(
        tenant_id=context.tenant_id,
        skill_spec_hash=skill_spec_hash,
        spec_ref=spec_ref,
        spec_payload=spec_payload,
    )


async def insert_payload_if_absent(
    session: AsyncSession,
    *,
    context: ActiveTenantContext,
    payload_ref: str,
    payload_hash: str,
    command_schema_version: str,
    payload: dict[str, Any],
) -> CommandPayload:
    result = await session.execute(
        pg_insert(CommandPayload)
        .values(
            tenant_id=context.tenant_id,
            payload_ref=payload_ref,
            payload_hash=payload_hash,
            command_schema_version=command_schema_version,
            sensitivity="sensitive",
            retention="run_completion",
            payload=payload,
        )
        .on_conflict_do_nothing(index_elements=[CommandPayload.tenant_id, CommandPayload.payload_ref])
        .returning(CommandPayload.payload_ref)
    )
    if result.scalar_one_or_none() is None:
        existing = (
            await session.execute(
                select(
                    CommandPayload.payload_ref,
                    CommandPayload.payload_hash,
                    CommandPayload.command_schema_version,
                    CommandPayload.sensitivity,
                    CommandPayload.retention,
                ).where(
                    CommandPayload.tenant_id == context.tenant_id,
                    CommandPayload.payload_ref == payload_ref,
                )
            )
        ).one_or_none()
        if (
            existing is None
            or existing[1] != payload_hash
            or existing[2] != command_schema_version
            or existing[3] != "sensitive"
            or existing[4] != "run_completion"
        ):
            raise ValueError("immutable command payload content conflict")
        # The API role intentionally cannot read payload bytes.  The
        # content-addressed ref/hash is sufficient to prove idempotency.
        return CommandPayload(
            tenant_id=context.tenant_id,
            payload_ref=existing[0],
            payload_hash=existing[1],
            command_schema_version=existing[2],
            sensitivity=existing[3],
            retention=existing[4],
            payload=payload,
        )
    return CommandPayload(
        tenant_id=context.tenant_id,
        payload_ref=payload_ref,
        payload_hash=payload_hash,
        command_schema_version=command_schema_version,
        sensitivity="sensitive",
        retention="run_completion",
        payload=payload,
    )


async def insert_run_if_absent(
    session: AsyncSession,
    *,
    context: ActiveTenantContext,
    run_id: UUID,
    submission_id: UUID,
    submission_digest: str,
    skill_spec_hash: str,
    skill_spec_ref: str,
    runtime_build_ref: str,
    runtime_build_hash: str,
) -> AgentRun | None:
    result = await session.execute(
        pg_insert(AgentRun)
        .values(
            run_id=run_id,
            tenant_id=context.tenant_id,
            submission_id=submission_id,
            submission_digest=submission_digest,
            principal_id=context.principal.principal_id,
            principal_kind=context.principal.kind.value,
            skill_spec_hash=skill_spec_hash,
            skill_spec_ref=skill_spec_ref,
            runtime_build_ref=runtime_build_ref,
            runtime_build_hash=runtime_build_hash,
            status="accepted",
            revision=0,
        )
        .on_conflict_do_nothing(index_elements=[AgentRun.tenant_id, AgentRun.submission_id])
        .returning(AgentRun.run_id)
    )
    created_id = result.scalar_one_or_none()
    if created_id is None:
        return None
    # The insert already owns the newly-created row.  A follow-up FOR UPDATE
    # would require UPDATE privilege on the API role even though this path
    # only reads the accepted immutable snapshot.
    return await get_run(session, context, created_id)


async def get_command(session: AsyncSession, context: ActiveTenantContext, command_id: UUID) -> RunCommand | None:
    return (
        await session.execute(
            select(RunCommand).where(RunCommand.tenant_id == context.tenant_id, RunCommand.command_id == command_id)
        )
    ).scalar_one_or_none()


async def list_commands(session: AsyncSession, context: ActiveTenantContext, run_id: UUID) -> Sequence[RunCommand]:
    result = await session.execute(
        select(RunCommand)
        .where(RunCommand.tenant_id == context.tenant_id, RunCommand.run_id == run_id)
        .order_by(RunCommand.command_seq)
    )
    return result.scalars().all()


async def insert_command(
    session: AsyncSession,
    *,
    context: ActiveTenantContext,
    command_id: UUID,
    run_id: UUID,
    command_digest: str,
    payload_ref: str,
    payload_hash: str,
) -> RunCommand:
    values = {
        "command_id": command_id,
        "tenant_id": context.tenant_id,
        "run_id": run_id,
        "principal_id": context.principal.principal_id,
        "principal_kind": context.principal.kind.value,
        "command_seq": 0,
        "command_type": "start",
        "command_schema_version": "start.v1",
        "command_digest": command_digest,
        "payload_ref": payload_ref,
        "payload_hash": payload_hash,
        "status": "pending",
    }
    # Use an explicit column insert so the API role cannot set lease/fence
    # fields, even to NULL, and therefore needs no delivery-state privilege.
    await session.execute(pg_insert(RunCommand).values(**values))
    command = RunCommand(**values)
    return command
