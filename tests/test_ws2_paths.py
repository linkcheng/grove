from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from app.api.v1 import execution as execution_api
from app.auth.context import (
    ActiveTenantContext,
    AuthenticationError,
    Principal,
    PrincipalKind,
    _context_from_development_headers,
    _context_from_token,
    active_tenant_context,
    authenticate_request,
    current_tenant_context,
)
from app.core.errors import (
    CommandNotFoundError,
    DependencyUnavailableError,
    EvaluationGateFailedError,
    PlanChangedError,
    RunNotFoundError,
    SubmissionConflictError,
)
from app.releases import fixture as fixture_module
from app.releases.fixture import (
    FIXTURE_ARTIFACT_REGISTRY,
    FIXTURE_RELEASE_ARTIFACT,
    FIXTURE_RELEASE_REF,
    FIXTURE_RELEASE_REGISTRY,
    FixtureReleaseError,
    build_fixture_evidence,
    load_fixture_evidence,
    load_fixture_release,
    load_fixture_release_bundle,
)
from app.repositories import execution as repository
from app.repositories.execution import SubmissionLockTimeoutError
from app.schemas.execution import CommandReceipt, ExecutionConstraints, RunHandle, RunQuery, SubmitExecution
from app.services import execution
from app.services import execution as execution_service
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request


def _context(kind: PrincipalKind = PrincipalKind.HUMAN) -> ActiveTenantContext:
    return ActiveTenantContext("tenant-a", Principal("principal-a", kind))


def _request() -> SubmitExecution:
    return SubmitExecution.model_validate(
        {
            "submission_id": str(uuid4()),
            "intent": {
                "intent_id": str(uuid4()),
                "skill_ref": "fixture.skill@1",
                "input": {"question": "hello"},
                "constraints": {},
            },
        }
    )


def _starlette_request(headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
        "state": {},
    }
    return Request(scope)


def test_auth_context_rejects_invalid_contexts_and_uses_one_context() -> None:
    with pytest.raises(AuthenticationError):
        Principal("bad id", PrincipalKind.HUMAN)
    with pytest.raises(AuthenticationError):
        Principal("user", PrincipalKind.HUMAN, ("z", "a"))
    with pytest.raises(AuthenticationError):
        ActiveTenantContext("bad tenant", Principal("user", PrincipalKind.HUMAN))
    with pytest.raises(AuthenticationError):
        ActiveTenantContext("tenant", Principal("user", PrincipalKind.HUMAN), "not valid")
    with pytest.raises(AuthenticationError):
        current_tenant_context()
    token = active_tenant_context.set(_context())
    try:
        assert current_tenant_context().tenant_id == "tenant-a"
    finally:
        active_tenant_context.reset(token)


@pytest.mark.parametrize(
    "token",
    ["", "tenant:user", "fixture:tenant", "fixture:tenant:user:unknown", "fixture:tenant:user name"],
)
def test_fixture_token_parser_is_strict(token: str) -> None:
    with pytest.raises(AuthenticationError):
        _context_from_token(token)


def test_authentication_headers_and_bearer_context_must_agree() -> None:
    assert _context_from_development_headers(_starlette_request({})) is None
    with pytest.raises(AuthenticationError):
        _context_from_development_headers(_starlette_request({"x-grove-auth": "fixture", "x-grove-tenant-id": "a"}))
    with pytest.raises(AuthenticationError):
        _context_from_development_headers(
            _starlette_request(
                {
                    "x-grove-auth": "fixture",
                    "x-grove-tenant-id": "a",
                    "x-grove-principal-id": "u",
                    "x-grove-principal-roles": "execution.submit",
                }
            )
        )
    headers = {
        "authorization": "Bearer fixture:tenant-a:principal-a",
        "x-grove-auth": "fixture",
        "x-grove-tenant-id": "tenant-a",
        "x-grove-principal-id": "principal-a",
    }
    request = _starlette_request(headers)
    assert authenticate_request(request).principal.principal_id == "principal-a"
    mismatch = _starlette_request({**headers, "x-grove-tenant-id": "tenant-b"})
    with pytest.raises(AuthenticationError):
        authenticate_request(mismatch)


def test_fixture_schema_rejects_unknown_target_and_preset() -> None:
    body = _request().model_dump(mode="json")
    body["intent"]["skill_ref"] = "unknown.skill@1"
    with pytest.raises(ValueError):
        SubmitExecution.model_validate(body)
    body = _request().model_dump(mode="json")
    body["intent"]["permission_preset_ref"] = "permission.unattended@1"
    assert SubmitExecution.model_validate(body).intent.permission_preset_ref == "permission.unattended@1"


def test_fixture_spec_uses_the_ws1_execution_abi() -> None:
    context = _context()
    spec = execution._build_fixture_spec(context, _request().intent, ("execution.query", "execution.submit"))
    assert spec.tenant_id == context.tenant_id
    assert spec.skill.ref == "fixture.skill@1"
    assert spec.permission.effective_scopes == ("execution.query", "execution.submit")
    assert spec.resolved_at == execution._FIXTURE_RESOLVED_AT


def test_fixture_spec_rejects_unpublished_constraints_before_persistence() -> None:
    request = _request()
    constrained = request.intent.model_copy(update={"constraints": ExecutionConstraints(deadline_ms=10)})
    with pytest.raises(EvaluationGateFailedError):
        execution._build_fixture_spec(_context(), constrained, ("execution.query", "execution.submit"))


def test_fixture_release_is_one_content_addressed_trusted_input() -> None:
    release = load_fixture_release()
    assert release.release_ref == FIXTURE_RELEASE_REF
    assert FIXTURE_RELEASE_REGISTRY[FIXTURE_RELEASE_REF] == FIXTURE_RELEASE_ARTIFACT
    with pytest.raises(FixtureReleaseError):
        load_fixture_release("release.missing@1")
    with pytest.raises(FixtureReleaseError, match="not allowed"):
        load_fixture_release("release.other@1", registry={"release.other@1": FIXTURE_RELEASE_ARTIFACT})
    tampered = dict(FIXTURE_RELEASE_REGISTRY)
    tampered[FIXTURE_RELEASE_REF] = FIXTURE_RELEASE_ARTIFACT + b"tampered"
    with pytest.raises(FixtureReleaseError, match="hash mismatch"):
        load_fixture_release(registry=tampered)
    nested = dict(FIXTURE_ARTIFACT_REGISTRY)
    nested["manifest.fixture@1"] = nested["manifest.fixture@1"] + b"tampered"
    with pytest.raises(FixtureReleaseError):
        load_fixture_release(artifact_registry=nested)
    missing = dict(FIXTURE_ARTIFACT_REGISTRY)
    del missing["manifest.fixture@1"]
    with pytest.raises(FixtureReleaseError, match="unavailable"):
        load_fixture_release(artifact_registry=missing)


def test_fixture_evidence_loader_requires_exact_subject_and_content_hash() -> None:
    bundle = load_fixture_release_bundle()
    subject_hash = bundle.evaluation_subject_hashes["interactive"]
    reference, payload = build_fixture_evidence(
        release_ref=bundle.release.release_ref,
        preset_ref="permission.interactive@1",
        evaluation_subject_hash=subject_hash,
    )
    with pytest.raises(FixtureReleaseError, match="mismatch"):
        load_fixture_evidence(
            payload + b"tampered",
            expected_ref=reference,
            expected_subject_hash=subject_hash,
            expected_preset_ref="permission.interactive@1",
            release_ref=bundle.release.release_ref,
        )
    with pytest.raises(FixtureReleaseError, match="pre-published"):
        load_fixture_evidence(
            payload,
            expected_ref=reference,
            expected_subject_hash="b" * 64,
            expected_preset_ref="permission.interactive@1",
            release_ref=bundle.release.release_ref,
        )
    with pytest.raises(FixtureReleaseError, match="pre-published"):
        build_fixture_evidence(
            release_ref=bundle.release.release_ref,
            preset_ref="permission.interactive@1",
            evaluation_subject_hash="a" * 64,
        )
    with pytest.raises(FixtureReleaseError, match="published index"):
        load_fixture_evidence(
            payload,
            expected_ref=reference.model_copy(update={"version": "2"}),
            expected_subject_hash=subject_hash,
            expected_preset_ref="permission.interactive@1",
            release_ref=bundle.release.release_ref,
        )


def test_fixture_evidence_builder_fails_closed_when_published_index_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = load_fixture_release_bundle()
    refs = dict(bundle.release.artifact_refs)
    del refs["evidence_index.interactive"]
    fake_bundle = replace(bundle, release=bundle.release.model_copy(update={"artifact_refs": refs}))
    monkeypatch.setattr(fixture_module, "load_fixture_release_bundle", lambda *_args, **_kwargs: fake_bundle)
    with pytest.raises(FixtureReleaseError, match="unavailable"):
        fixture_module.build_fixture_evidence(
            release_ref=bundle.release.release_ref,
            preset_ref="permission.interactive@1",
            evaluation_subject_hash=bundle.evaluation_subject_hashes["interactive"],
        )


def test_fixture_permission_presets_bind_authority_and_envelope_at_distinct_layers() -> None:
    context = _context()
    request = _request()
    specs = [
        execution._build_fixture_spec(
            context,
            request.intent.model_copy(update={"permission_preset_ref": preset}),
            ("execution.query", "execution.submit"),
        )
        for preset in (
            "permission.interactive@1",
            "permission.read_only@1",
            "permission.workspace_edit@1",
            "permission.unattended@1",
        )
    ]
    assert len({spec.permission.permission_envelope_hash for spec in specs}) == 1
    stronger_auth = ActiveTenantContext(context.tenant_id, context.principal, auth_strength="mfa")
    stronger = execution._build_fixture_spec(stronger_auth, request.intent, ("execution.query", "execution.submit"))
    assert stronger.permission.permission_envelope_hash == specs[0].permission.permission_envelope_hash
    assert stronger.permission.run_authority_hash != specs[0].permission.run_authority_hash
    assert stronger.skill_spec_hash != specs[0].skill_spec_hash
    assert stronger.spec_id != specs[0].spec_id
    reduced_scopes = execution._build_fixture_spec(context, request.intent, ("execution.submit",))
    assert reduced_scopes.permission.permission_envelope_hash == specs[0].permission.permission_envelope_hash
    assert reduced_scopes.permission.run_authority_hash != specs[0].permission.run_authority_hash
    assert reduced_scopes.skill_spec_hash != specs[0].skill_spec_hash
    assert all(spec.issuer == FIXTURE_RELEASE_REF for spec in specs)


@pytest.mark.asyncio
async def test_execution_api_routes_delegate_to_service(monkeypatch: pytest.MonkeyPatch) -> None:
    context = _context()
    run_id = uuid4()
    command_id = uuid4()
    handle = RunHandle(
        run_id=run_id,
        submission_id=uuid4(),
        command_id=command_id,
        status="accepted",
        revision=0,
        skill_spec_hash="a" * 64,
    )
    receipt = CommandReceipt(
        command_id=command_id,
        run_id=run_id,
        command_type="start",
        command_schema_version="start.v1",
        status="pending",
        command_seq=0,
    )
    query = RunQuery(run=handle, commands=[receipt])
    monkeypatch.setattr(execution_service, "submit", lambda *_args: _async_value(handle))
    monkeypatch.setattr(execution_service, "query_run", lambda *_args: _async_value(query))
    monkeypatch.setattr(execution_service, "query_command", lambda *_args: _async_value(receipt))
    assert (await execution_api.submit(_request(), context, _session())).data == handle
    assert (await execution_api.query_run(run_id, context, _session())).data == query
    assert (await execution_api.query_command(command_id, context, _session())).data == receipt


@pytest.mark.asyncio
async def test_execution_api_session_dependencies_delegate(monkeypatch: pytest.MonkeyPatch) -> None:
    session = object()
    factory = object()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_factory=factory)))

    async def fake_db(_factory: Any) -> Any:
        yield session

    monkeypatch.setattr(execution_api, "get_write_db", fake_db)
    write_generator = execution_api._write_session(cast(Request, request))
    assert await anext(write_generator) is session
    monkeypatch.setattr(execution_api, "get_read_db", fake_db)
    read_generator = execution_api._read_session(cast(Request, request))
    assert await anext(read_generator) is session


@pytest.mark.asyncio
async def test_submit_service_persists_spec_payload_run_and_command(monkeypatch: pytest.MonkeyPatch) -> None:
    context = _context()
    request = _request()
    run = SimpleNamespace(run_id=uuid4(), submission_id=request.submission_id, revision=0, skill_spec_hash="a" * 64)
    command = SimpleNamespace(command_id=uuid4())
    monkeypatch.setattr(execution, "_authorize", lambda *_args, **_kwargs: _scopes())
    monkeypatch.setattr(execution, "lock_submission", lambda *_args, **_kwargs: _async_value(None))
    monkeypatch.setattr(execution, "get_run_by_submission", lambda *_args, **_kwargs: _async_value(None))
    monkeypatch.setattr(
        execution,
        "_build_fixture_spec",
        lambda *_args: SimpleNamespace(
            skill_spec_hash="a" * 64,
            runtime_build=SimpleNamespace(ref="runtime-build:a", content_hash="b" * 64),
            model_dump=lambda **_: {"skill_spec_hash": "a" * 64},
        ),
    )
    monkeypatch.setattr(execution, "insert_spec_if_absent", lambda *_args, **_kwargs: _async_value(None))
    monkeypatch.setattr(execution, "insert_payload_if_absent", lambda *_args, **_kwargs: _async_value(None))
    monkeypatch.setattr(execution, "insert_run_if_absent", lambda *_args, **_kwargs: _async_value(run))
    monkeypatch.setattr(execution, "insert_command", lambda *_args, **_kwargs: _async_value(command))
    handle = await execution.submit(_session(), context, request)
    assert handle.run_id == run.run_id and handle.command_id == command.command_id


@pytest.mark.asyncio
async def test_submit_service_idempotency_conflicts_and_plan_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    context = _context()
    request = _request()
    run = SimpleNamespace(
        run_id=uuid4(),
        submission_id=request.submission_id,
        revision=0,
        skill_spec_hash="a" * 64,
        principal_id=context.principal.principal_id,
        principal_kind=context.principal.kind.value,
        submission_digest=execution._submission_digest(context, request),
    )
    command = SimpleNamespace(command_id=execution._command_id(context, request.submission_id))
    monkeypatch.setattr(execution, "_authorize", lambda *_args, **_kwargs: _scopes())
    monkeypatch.setattr(execution, "lock_submission", lambda *_args, **_kwargs: _async_value(None))
    monkeypatch.setattr(
        execution,
        "_build_fixture_spec",
        lambda *_args: SimpleNamespace(skill_spec_hash="a" * 64, model_dump=lambda **_: {"skill_spec_hash": "a" * 64}),
    )
    monkeypatch.setattr(execution, "insert_spec_if_absent", lambda *_args, **_kwargs: _async_value(None))
    monkeypatch.setattr(execution, "insert_payload_if_absent", lambda *_args, **_kwargs: _async_value(None))
    monkeypatch.setattr(execution, "insert_run_if_absent", lambda *_args, **_kwargs: _async_value(None))
    monkeypatch.setattr(execution, "get_run_by_submission", lambda *_args, **_kwargs: _async_value(run))
    monkeypatch.setattr(execution, "get_command", lambda *_args, **_kwargs: _async_value(command))
    result = await execution.submit(_session(), context, request)
    assert result.run_id == run.run_id
    conflicting = _request().model_copy(update={"submission_id": request.submission_id})
    monkeypatch.setattr(execution, "get_run_by_submission", lambda *_args, **_kwargs: _async_value(run))
    with pytest.raises(SubmissionConflictError):
        await execution.submit(_session(), context, conflicting)
    expected = request.model_copy(update={"expected_skill_spec_hash": "b" * 64})
    monkeypatch.setattr(execution, "insert_run_if_absent", lambda *_args, **_kwargs: _async_value(run))
    with pytest.raises(PlanChangedError):
        await execution.submit(_session(), context, expected)


@pytest.mark.asyncio
async def test_submit_maps_bounded_submission_lock_timeout_to_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    async def timeout(*_args: Any, **_kwargs: Any) -> None:
        raise SubmissionLockTimeoutError("busy")

    monkeypatch.setattr(execution, "_authorize", lambda *_args, **_kwargs: _scopes())
    monkeypatch.setattr(execution, "lock_submission", timeout)
    with pytest.raises(DependencyUnavailableError):
        await execution.submit(_session(), _context(), _request())


@pytest.mark.asyncio
async def test_query_service_reads_only_authorized_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    context = _context()
    run_id = uuid4()
    run = SimpleNamespace(
        run_id=run_id,
        submission_id=uuid4(),
        revision=0,
        skill_spec_hash="a" * 64,
        principal_id=context.principal.principal_id,
        principal_kind=context.principal.kind.value,
    )
    command = SimpleNamespace(
        command_id=uuid4(),
        run_id=run_id,
        command_schema_version="start.v1",
        command_seq=0,
        payload_ref="command-payload:x",
        payload_hash="b" * 64,
    )
    monkeypatch.setattr(execution, "_authorize", lambda *_args, **_kwargs: _scopes())
    monkeypatch.setattr(execution, "get_run", lambda *_args, **_kwargs: _async_value(run))
    monkeypatch.setattr(execution, "list_commands", lambda *_args, **_kwargs: _async_value([command]))
    queried = await execution.query_run(_session(), context, run_id)
    assert queried.run.run_id == run_id
    monkeypatch.setattr(execution, "get_command", lambda *_args, **_kwargs: _async_value(command))
    monkeypatch.setattr(execution, "get_run", lambda *_args, **_kwargs: _async_value(run))
    receipt = await execution.query_command(_session(), context, command.command_id)
    assert receipt.command_id == command.command_id
    monkeypatch.setattr(execution, "get_run", lambda *_args, **_kwargs: _async_value(None))
    with pytest.raises(RunNotFoundError):
        await execution.query_run(_session(), context, run_id)
    monkeypatch.setattr(execution, "get_command", lambda *_args, **_kwargs: _async_value(None))
    with pytest.raises(CommandNotFoundError):
        await execution.query_command(_session(), context, command.command_id)


def _session() -> AsyncSession:
    return cast(AsyncSession, object())


def _scopes() -> Awaitable[tuple[str, ...]]:
    async def result() -> tuple[str, ...]:
        return ("execution.query", "execution.submit")

    return result()


def _async_value(value: Any) -> Awaitable[Any]:
    async def result() -> Any:
        return value

    return result()


class _Result:
    def __init__(
        self,
        *,
        scalar_one_or_none_value: Any = None,
        scalar_one_value: Any = None,
        one_or_none_value: Any = None,
        rows: list[Any] | None = None,
    ) -> None:
        self.scalar_one_or_none_value = scalar_one_or_none_value
        self.scalar_one_value = scalar_one_value
        self.one_or_none_value = one_or_none_value
        self.rows = rows or []

    def scalar_one_or_none(self) -> Any:
        return self.scalar_one_or_none_value

    def scalar_one(self) -> Any:
        return self.scalar_one_value

    def one_or_none(self) -> Any:
        return self.one_or_none_value

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return self.rows


class _RepositorySession:
    def __init__(
        self, *, execute_results: list[_Result] | None = None, scalar_results: list[Any] | None = None
    ) -> None:
        self.execute_results = execute_results or []
        self.scalar_results = scalar_results or []
        self.added: list[Any] = []
        self.flush_count = 0
        self.executed: list[Any] = []

    async def execute(self, statement: Any, *_args: Any, **_kwargs: Any) -> _Result:
        self.executed.append(statement)
        return self.execute_results.pop(0)

    async def scalar(self, _statement: Any, *_args: Any, **_kwargs: Any) -> Any:
        return self.scalar_results.pop(0)

    def add(self, value: Any) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        self.flush_count += 1


class _TryLockSession:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, _statement: Any, *_args: Any, **_kwargs: Any) -> _Result:
        self.calls += 1
        return _Result(scalar_one_value=False)


@pytest.mark.asyncio
async def test_submission_try_lock_times_out_with_a_bounded_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository, "SUBMISSION_LOCK_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(repository, "SUBMISSION_LOCK_RETRY_INTERVAL_SECONDS", 0.001)
    session = _TryLockSession()
    with pytest.raises(SubmissionLockTimeoutError):
        await repository.lock_submission(_repository_session(session), _context(), uuid4())
    assert 1 <= session.calls <= 30


def _repository_session(value: Any) -> AsyncSession:
    return cast(AsyncSession, value)


@pytest.mark.asyncio
async def test_repository_scope_and_authorization_branches() -> None:
    context = _context()
    session = _RepositorySession(
        scalar_results=["active", True],
        execute_results=[_Result(one_or_none_value=(True, ["execution.query", "execution.submit"]))],
    )
    scope_session = _RepositorySession(execute_results=[_Result()])
    await repository.set_tenant_scope(_repository_session(scope_session), context)
    assert len(scope_session.executed) == 1
    assert await repository.authorize_operation(_repository_session(session), context, "execution.query") == (
        "execution.query",
        "execution.submit",
    )

    assert (
        await repository.authorize_operation(_repository_session(_RepositorySession()), context, "execution.resume")
        == ()
    )
    assert (
        await repository.authorize_operation(
            _repository_session(_RepositorySession(scalar_results=["inactive"])), context, "execution.query"
        )
        == ()
    )
    assert (
        await repository.authorize_operation(
            _repository_session(_RepositorySession(scalar_results=["active", False])), context, "execution.query"
        )
        == ()
    )

    grants_values: tuple[Any, ...] = (None, {}, ["not-an-operation"], [1], ["execution.submit"])
    for grants in grants_values:
        scoped = _RepositorySession(
            scalar_results=["active", None],
            execute_results=[_Result(one_or_none_value=(True, grants))],
        )
        assert await repository.authorize_operation(_repository_session(scoped), context, "execution.query") == ()

    missing = _RepositorySession(scalar_results=["active", None], execute_results=[_Result(one_or_none_value=None)])
    assert await repository.authorize_operation(_repository_session(missing), context, "execution.query") == ()
    inactive = _RepositorySession(
        scalar_results=["active", None], execute_results=[_Result(one_or_none_value=(False, []))]
    )
    assert await repository.authorize_operation(_repository_session(inactive), context, "execution.query") == ()

    workload = ActiveTenantContext("tenant-a", Principal("worker-a", PrincipalKind.WORKLOAD))
    workload_session = _RepositorySession(
        scalar_results=["active", None],
        execute_results=[_Result(one_or_none_value=(True, ["execution.submit"]))],
    )
    assert await repository.authorize_operation(
        _repository_session(workload_session), workload, "execution.submit"
    ) == ("execution.submit",)


@pytest.mark.asyncio
async def test_repository_reads_and_inserts_immutable_records() -> None:
    context = _context()
    run_id = uuid4()
    submission_id = uuid4()
    run: Any = SimpleNamespace(run_id=run_id)
    command: Any = SimpleNamespace(command_id=uuid4())
    read_session = _RepositorySession(
        execute_results=[
            _Result(scalar_one_or_none_value=run),
            _Result(scalar_one_or_none_value=run),
            _Result(scalar_one_or_none_value=command),
            _Result(rows=[command]),
        ]
    )
    assert (
        await repository.get_run_by_submission(_repository_session(read_session), context, submission_id, lock=True)
        is run
    )
    assert await repository.get_run(_repository_session(read_session), context, run_id, lock=True) is run
    assert await repository.get_command(_repository_session(read_session), context, command.command_id) is command
    assert await repository.list_commands(_repository_session(read_session), context, run_id) == [command]

    spec_inserted = _RepositorySession(
        execute_results=[
            _Result(scalar_one_or_none_value="hash"),
        ]
    )
    inserted_spec = await repository.insert_spec_if_absent(
        _repository_session(spec_inserted),
        context=context,
        skill_spec_hash="hash",
        spec_ref="spec:x",
        spec_payload={"x": 1},
    )
    assert inserted_spec.skill_spec_hash == "hash" and inserted_spec.spec_payload == {"x": 1}

    spec_existing = _RepositorySession(
        execute_results=[_Result(scalar_one_or_none_value=None), _Result(one_or_none_value=("hash", "spec:x"))]
    )
    existing_spec = await repository.insert_spec_if_absent(
        _repository_session(spec_existing),
        context=context,
        skill_spec_hash="hash",
        spec_ref="spec:x",
        spec_payload={"x": 1},
    )
    assert existing_spec.skill_spec_hash == "hash" and existing_spec.spec_ref == "spec:x"
    conflict_spec = _RepositorySession(
        execute_results=[_Result(scalar_one_or_none_value=None), _Result(one_or_none_value=("hash", "spec:y"))]
    )
    with pytest.raises(ValueError, match="execution spec"):
        await repository.insert_spec_if_absent(
            _repository_session(conflict_spec),
            context=context,
            skill_spec_hash="hash",
            spec_ref="spec:x",
            spec_payload={"x": 1},
        )

    payload: Any = SimpleNamespace(
        payload_ref="payload:x",
        payload_hash="hash",
        command_schema_version="start.v1",
        sensitivity="sensitive",
        retention="run_completion",
        payload={"x": 1},
    )
    payload_inserted = _RepositorySession(
        execute_results=[_Result(scalar_one_or_none_value="payload:x"), _Result(scalar_one_value=payload)]
    )
    inserted_payload = await repository.insert_payload_if_absent(
        _repository_session(payload_inserted),
        context=context,
        payload_ref="payload:x",
        payload_hash="hash",
        command_schema_version="start.v1",
        payload={"x": 1},
    )
    assert (
        inserted_payload.payload_hash == payload.payload_hash
        and inserted_payload.command_schema_version == payload.command_schema_version
    )
    payload_existing = _RepositorySession(
        execute_results=[
            _Result(scalar_one_or_none_value=None),
            _Result(one_or_none_value=("payload:x", "hash", "start.v1", "sensitive", "run_completion")),
        ]
    )
    existing_payload = await repository.insert_payload_if_absent(
        _repository_session(payload_existing),
        context=context,
        payload_ref="payload:x",
        payload_hash="hash",
        command_schema_version="start.v1",
        payload={"x": 1},
    )
    assert existing_payload.payload_ref == payload.payload_ref
    conflict_payload = _RepositorySession(
        execute_results=[_Result(scalar_one_or_none_value=None), _Result(one_or_none_value=None)]
    )
    with pytest.raises(ValueError, match="command payload"):
        await repository.insert_payload_if_absent(
            _repository_session(conflict_payload),
            context=context,
            payload_ref="payload:x",
            payload_hash="different",
            command_schema_version="start.v1",
            payload={"x": 1},
        )


@pytest.mark.asyncio
async def test_repository_run_and_command_writes() -> None:
    context = _context()
    run_id = uuid4()
    run: Any = SimpleNamespace(run_id=run_id)
    created = _RepositorySession(
        execute_results=[_Result(scalar_one_or_none_value=run_id), _Result(scalar_one_or_none_value=run)]
    )
    assert (
        await repository.insert_run_if_absent(
            _repository_session(created),
            context=context,
            run_id=run_id,
            submission_id=uuid4(),
            submission_digest="a" * 64,
            skill_spec_hash="b" * 64,
            skill_spec_ref="spec:b",
            runtime_build_ref="runtime-build:b",
            runtime_build_hash="c" * 64,
        )
        is run
    )
    absent = _RepositorySession(execute_results=[_Result()])
    assert (
        await repository.insert_run_if_absent(
            _repository_session(absent),
            context=context,
            run_id=run_id,
            submission_id=uuid4(),
            submission_digest="a" * 64,
            skill_spec_hash="b" * 64,
            skill_spec_ref="spec:b",
            runtime_build_ref="runtime-build:b",
            runtime_build_hash="c" * 64,
        )
        is None
    )

    command_session = _RepositorySession(execute_results=[_Result()])
    command = await repository.insert_command(
        _repository_session(command_session),
        context=context,
        command_id=uuid4(),
        run_id=run_id,
        command_digest="c" * 64,
        payload_ref="payload:c",
        payload_hash="c" * 64,
    )
    assert command.status == "pending"
    assert command.command_schema_version == "start.v1"
    assert len(command_session.executed) == 1
    assert command_session.flush_count == 0
