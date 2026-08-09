"""Atomic tenant-aware WS-2 submit/query services.

The service persists immutable resolver/spec and payload artifacts only.  It
never invokes a Graph, provider, worker, or interaction command.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import ActiveTenantContext
from app.contracts.canonical import VersionedRef, canonical_hash
from app.core.errors import (
    CommandNotFoundError,
    DependencyUnavailableError,
    EvaluationGateFailedError,
    PermissionDeniedError,
    PlanChangedError,
    RunNotFoundError,
    SubmissionConflictError,
)
from app.releases.fixture import (
    FIXTURE_CONSTRAINTS_PAYLOAD,
    FixtureReleaseError,
    load_fixture_evidence,
    load_fixture_release_bundle,
)
from app.repositories.execution import (
    SubmissionLockTimeoutError,
    authorize_operation,
    get_command,
    get_run,
    get_run_by_submission,
    insert_command,
    insert_payload_if_absent,
    insert_run_if_absent,
    insert_spec_if_absent,
    list_commands,
    lock_submission,
    set_tenant_scope,
)
from app.schemas.execution import CommandReceipt, ExecutionIntent, RunHandle, RunQuery, SubmitExecution
from app.skill_abi.capability import PermissionPreset
from app.skill_abi.models import SkillExecutionSpec
from app.skill_abi.runtime import (
    build_skill_execution_spec,
    compute_evaluation_subject_hash,
    verify_skill_execution_spec,
)

_COMMAND_SCHEMA_VERSION = "start.v1"
_FIXTURE_RESOLVED_AT = datetime(2026, 1, 1, tzinfo=UTC)
_ALLOWED_PRESETS = frozenset(f"permission.{preset.value}@1" for preset in PermissionPreset)


def _submission_digest(context: ActiveTenantContext, request: SubmitExecution) -> str:
    canonical_request = request.model_dump(mode="json")
    canonical_request.pop("expected_skill_spec_hash", None)
    return canonical_hash(
        {
            "operation": "submit",
            "tenant_id": context.tenant_id,
            "principal_id": context.principal.principal_id,
            "principal_kind": context.principal.kind.value,
            "submission_id": str(request.submission_id),
            "request": canonical_request,
        }
    )


def _payload(request: SubmitExecution) -> dict[str, Any]:
    """Return the only stored copy of the typed sensitive input."""

    return {
        "intent_id": str(request.intent.intent_id),
        "target": request.intent.agent_ref or request.intent.skill_ref,
        "permission_preset_ref": request.intent.permission_preset_ref,
        "sensitivity": "sensitive",
        "retention": "run_completion",
        "input": request.intent.input.model_dump(mode="json"),
        "constraints": request.intent.constraints.model_dump(mode="json"),
    }


def _build_fixture_spec(
    context: ActiveTenantContext,
    intent: ExecutionIntent,
    effective_scopes: tuple[str, ...],
) -> SkillExecutionSpec:
    """Resolve the published fixture into the existing WS-1 SkillExecutionSpec ABI."""

    preset = intent.permission_preset_ref
    if preset not in _ALLOWED_PRESETS:
        raise EvaluationGateFailedError()
    if intent.constraints.model_dump(mode="json") != FIXTURE_CONSTRAINTS_PAYLOAD:
        raise EvaluationGateFailedError()
    try:
        bundle = load_fixture_release_bundle()
    except (FixtureReleaseError, TypeError, ValueError) as exc:
        raise DependencyUnavailableError() from exc
    release = bundle.release
    refs = release.artifact_refs
    skill_ref = refs["skill"]
    source_agent_ref = refs["agent"] if intent.agent_ref is not None else None
    runtime_manifest_ref = refs["runtime_manifest"]
    graph_ref = refs["graph"]
    contracts_ref = refs["contracts"]
    runtime_build_ref = refs["runtime_build"]
    budget_ref = refs["budget"]
    preset_name = preset.removeprefix("permission.").removesuffix("@1")
    preset_ref = refs[f"permission_preset.{preset_name}"]
    authorization_policy_ref = refs["authorization_policy"]
    ceiling = tuple(sorted(bundle.authorization_policy.ceiling_scopes))
    if any(scope not in ceiling for scope in effective_scopes):
        raise DependencyUnavailableError()
    authority_payload = {
        "tenant_id": context.tenant_id,
        "principal_id": context.principal.principal_id,
        "principal_kind": context.principal.kind.value,
        "auth_strength": context.auth_strength,
        "roles_or_scopes": effective_scopes,
        "policy_revision": bundle.authorization_policy.revision,
    }
    permission_envelope_hash = canonical_hash(
        {
            "ceiling": ceiling,
            "effect": bundle.authorization_policy.effect,
            "policy_revision": bundle.authorization_policy.revision,
            "authorization_policy": authorization_policy_ref.model_dump(mode="json"),
        }
    )
    spec_identity = canonical_hash(
        {
            "intent": intent.model_dump(mode="json"),
            "tenant_id": context.tenant_id,
            "principal_id": context.principal.principal_id,
            "principal_kind": context.principal.kind.value,
            "auth_strength": context.auth_strength,
            "roles_or_scopes": effective_scopes,
        }
    )

    def build(evidence_ref: VersionedRef) -> SkillExecutionSpec:
        return build_skill_execution_spec(
            abi_version="v1",
            spec_id=uuid5(NAMESPACE_URL, f"grove:ws2:spec:{spec_identity}"),
            issuer=release.release_ref,
            tenant_id=context.tenant_id,
            source_agent_ref=source_agent_ref,
            run_mode="live",
            skill=skill_ref,
            graph={"graph": graph_ref, "graph_state_schema_version": bundle.graph_binding.graph_state_schema_version},
            contracts={"contracts": contracts_ref, "converter_bundle": None},
            runtime_manifest=runtime_manifest_ref,
            runtime_build=runtime_build_ref,
            permission={
                "run_authority_ref": f"authority.{context.tenant_id}.{context.principal.principal_id}@1",
                "run_authority_hash": canonical_hash(authority_payload),
                "authorization_policy": authorization_policy_ref,
                "permission_preset": preset_ref,
                "permission_envelope_hash": permission_envelope_hash,
                "effective_scopes": effective_scopes,
            },
            required_capabilities=(),
            budget={
                "evaluation_envelope": budget_ref,
                "effective_budget": budget_ref,
            },
            policy_refs=(),
            evaluation_evidence_set=evidence_ref,
            resolver_version=release.resolver_version,
            # The conformance resolver is deterministic: volatile wall-clock
            # fields must not make an idempotent retry mutate an immutable spec.
            resolved_at=_FIXTURE_RESOLVED_AT,
        )

    expected_subject_hash = bundle.evaluation_subject_hashes.get(preset_name)
    evidence_ref = refs.get(f"evidence_index.{preset_name}")
    if expected_subject_hash is None or evidence_ref is None:
        raise EvaluationGateFailedError()
    spec = build(evidence_ref)
    subject_hash = compute_evaluation_subject_hash(spec)
    if subject_hash != expected_subject_hash:
        raise EvaluationGateFailedError()
    evidence_payload = bundle.artifact_bytes[evidence_ref.ref]
    try:
        load_fixture_evidence(
            evidence_payload,
            expected_ref=evidence_ref,
            expected_subject_hash=subject_hash,
            expected_preset_ref=preset_ref.ref,
            release_ref=release.release_ref,
            artifact_registry=bundle.artifact_bytes,
        )
    except FixtureReleaseError as exc:
        raise EvaluationGateFailedError() from exc
    if spec.evaluation_subject_hash != subject_hash:
        raise EvaluationGateFailedError()
    verify_skill_execution_spec(spec)
    return spec


def _command_id(context: ActiveTenantContext, submission_id: UUID) -> UUID:
    """Derive start identity from tenant and submission, closing cross-tenant side channels."""

    return uuid5(NAMESPACE_URL, f"grove:ws2:start:{context.tenant_id}:{submission_id}")


def _run_handle(run: Any, command: Any) -> RunHandle:
    return RunHandle(
        run_id=run.run_id,
        submission_id=run.submission_id,
        command_id=command.command_id,
        status="accepted",
        revision=run.revision,
        skill_spec_hash=run.skill_spec_hash,
    )


def _command_receipt(command: Any) -> CommandReceipt:
    return CommandReceipt(
        command_id=command.command_id,
        run_id=command.run_id,
        command_type="start",
        command_schema_version=command.command_schema_version,
        status="pending",
        command_seq=command.command_seq,
    )


async def _authorize(session: AsyncSession, context: ActiveTenantContext, operation: str) -> tuple[str, ...]:
    await set_tenant_scope(session, context)
    effective = await authorize_operation(session, context, operation)
    if operation not in effective:
        raise PermissionDeniedError()
    return effective


async def submit(session: AsyncSession, context: ActiveTenantContext, request: SubmitExecution) -> RunHandle:
    """Authorize afresh, resolve an immutable WS-1 spec, and persist submit."""

    effective_scopes = await _authorize(session, context, "execution.submit")
    digest = _submission_digest(context, request)
    try:
        await lock_submission(session, context, request.submission_id)
    except SubmissionLockTimeoutError as exc:
        raise DependencyUnavailableError() from exc
    existing = await get_run_by_submission(session, context, request.submission_id)
    if existing is not None:
        if (
            existing.principal_id != context.principal.principal_id
            or existing.principal_kind != context.principal.kind.value
            or existing.submission_digest != digest
        ):
            raise SubmissionConflictError()
        if (
            request.expected_skill_spec_hash is not None
            and request.expected_skill_spec_hash != existing.skill_spec_hash
        ):
            raise PlanChangedError()
        command = await get_command(session, context, _command_id(context, request.submission_id))
        if command is None:
            raise SubmissionConflictError()
        return _run_handle(existing, command)

    # Only a new submission may resolve the current release and write new
    # immutable artifacts. Retries never manufacture orphan specs/payloads.
    spec = _build_fixture_spec(context, request.intent, effective_scopes)
    if request.expected_skill_spec_hash is not None and request.expected_skill_spec_hash != spec.skill_spec_hash:
        raise PlanChangedError()
    spec_ref = f"execution-spec:{spec.skill_spec_hash}"
    await insert_spec_if_absent(
        session,
        context=context,
        skill_spec_hash=spec.skill_spec_hash,
        spec_ref=spec_ref,
        spec_payload=spec.model_dump(mode="json"),
    )
    payload = _payload(request)
    payload_hash = canonical_hash(payload)
    payload_ref = f"command-payload:{payload_hash}"
    await insert_payload_if_absent(
        session,
        context=context,
        payload_ref=payload_ref,
        payload_hash=payload_hash,
        command_schema_version=_COMMAND_SCHEMA_VERSION,
        payload=payload,
    )
    command_id = _command_id(context, request.submission_id)
    run = await insert_run_if_absent(
        session,
        context=context,
        run_id=uuid4(),
        submission_id=request.submission_id,
        submission_digest=digest,
        skill_spec_hash=spec.skill_spec_hash,
        skill_spec_ref=spec_ref,
        runtime_build_ref=spec.runtime_build.ref,
        runtime_build_hash=spec.runtime_build.content_hash,
    )
    if run is None:
        # The advisory lock makes this path unreachable for a healthy
        # transaction; retain a fail-closed guard for a manually altered DB.
        raise SubmissionConflictError()
    command = await insert_command(
        session,
        context=context,
        command_id=command_id,
        run_id=run.run_id,
        command_digest=digest,
        payload_ref=payload_ref,
        payload_hash=payload_hash,
    )
    return _run_handle(run, command)


async def query_run(session: AsyncSession, context: ActiveTenantContext, run_id: UUID) -> RunQuery:
    await _authorize(session, context, "execution.query")
    run = await get_run(session, context, run_id)
    if (
        run is None
        or run.principal_id != context.principal.principal_id
        or run.principal_kind != context.principal.kind.value
    ):
        raise RunNotFoundError()
    commands = await list_commands(session, context, run_id)
    if not commands:
        raise RunNotFoundError()
    start = commands[0]
    return RunQuery(run=_run_handle(run, start), commands=[_command_receipt(item) for item in commands])


async def query_command(session: AsyncSession, context: ActiveTenantContext, command_id: UUID) -> CommandReceipt:
    await _authorize(session, context, "execution.query")
    command = await get_command(session, context, command_id)
    if command is None:
        raise CommandNotFoundError()
    run = await get_run(session, context, command.run_id)
    if (
        run is None
        or run.principal_id != context.principal.principal_id
        or run.principal_kind != context.principal.kind.value
    ):
        raise CommandNotFoundError()
    return _command_receipt(command)
