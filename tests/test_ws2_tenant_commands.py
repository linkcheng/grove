from __future__ import annotations

import uuid

import pytest
from app.auth.context import AuthenticationError, PrincipalKind, _context_from_token
from app.core.config import Role, Settings
from app.core.errors import (
    AppError,
    CommandConflictError,
    CommandNotFoundError,
    DependencyUnavailableError,
    PermissionDeniedError,
    PlanChangedError,
    RunNotFoundError,
    RunStateConflictError,
    SubmissionConflictError,
)
from app.main import create_app
from app.schemas.execution import SubmitExecution
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError


def _submit_body() -> dict[str, object]:
    return {
        "submission_id": str(uuid.uuid4()),
        "intent": {
            "intent_id": str(uuid.uuid4()),
            "skill_ref": "fixture.skill@1",
            "input": {"question": "hello"},
            "constraints": {},
        },
    }


@pytest.mark.parametrize(
    "error",
    [
        PermissionDeniedError(),
        RunNotFoundError(),
        CommandNotFoundError(),
        PlanChangedError(),
        SubmissionConflictError(),
        CommandConflictError(),
        RunStateConflictError(),
    ],
)
def test_ws2_business_errors_use_http_200(error: AppError) -> None:
    assert error.status_code == 200


def test_dependency_unavailable_defaults_to_one_second_retry() -> None:
    error = DependencyUnavailableError()
    assert error.status_code == 503
    assert error.retry_after == 1


def test_fixture_auth_binds_one_tenant_and_supports_workload_principal() -> None:
    human = _context_from_token("fixture:tenant-a:user-a")
    assert human.tenant_id == "tenant-a"
    assert human.principal.principal_id == "user-a"
    assert human.principal.kind is PrincipalKind.HUMAN
    workload = _context_from_token("fixture:tenant-b:worker-a:workload")
    assert workload.tenant_id == "tenant-b"
    assert workload.principal.kind is PrincipalKind.WORKLOAD
    with pytest.raises(AuthenticationError):
        _context_from_token("tenant-a:user-a")
    with pytest.raises(AuthenticationError):
        _context_from_token("fixture:tenant-a:user-a:admin")


def test_submit_rejects_identity_and_fence_fields_in_payload() -> None:
    body = _submit_body()
    cast_input = body["intent"]
    assert isinstance(cast_input, dict)
    cast_input["input"] = {"tenant_id": "tenant-b"}
    with pytest.raises(ValidationError, match="not accepted"):
        SubmitExecution.model_validate(body)
    forged = {**body, "principal_id": "forged"}
    with pytest.raises(ValidationError):
        SubmitExecution.model_validate(forged)


def test_execution_requires_auth_and_keeps_unified_error_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROVE_ROLE", "api")
    app = create_app(Settings(role=Role.API))
    with TestClient(app) as client:
        response = client.post("/api/v1/execution/submit", json=_submit_body(), headers={"X-Request-ID": "ws2_auth"})
    assert response.status_code == 401
    assert response.json() == {
        "code": 40100,
        "message": "authentication required",
        "data": None,
        "trace_id": "ws2_auth",
        "error_code": "AuthenticationRequired",
        "correlation_id": "ws2_auth",
    }


def test_execution_rejects_disagreeing_tenant_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROVE_ROLE", "api")
    app = create_app(Settings(role=Role.API))
    headers = {
        "Authorization": "Bearer tenant-a:user-a",
        "X-Grove-Tenant-ID": "tenant-b",
        "X-Grove-Principal-ID": "user-a",
    }
    with TestClient(app) as client:
        response = client.post("/api/v1/execution/submit", json=_submit_body(), headers=headers)
    assert response.status_code == 401
    assert response.json()["code"] == 40100


def test_submit_and_query_unreachable_database_use_dependency_error_envelope() -> None:
    settings = Settings(
        role=Role.API,
        app_env="test",
        auth_mode="fixture",
        database_url=SecretStr("postgresql+psycopg://grove_api:invalid@127.0.0.1:1/grove"),
        readiness_timeout_seconds=0.2,
    )
    headers = {"Authorization": "Bearer fixture:tenant-a:user-a", "X-Request-ID": "db_down"}
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        submit = client.post("/api/v1/executions/submit", json=_submit_body(), headers=headers)
        query = client.get(f"/api/v1/executions/runs/{uuid.uuid4()}", headers=headers)
    for response in (submit, query):
        assert response.status_code == 503
        body = response.json()
        assert body["code"] == 50302
        assert body["error_code"] == "DependencyUnavailable"
        assert body["correlation_id"] == "db_down"
        assert body["retry_after"] == 1
        assert response.headers["retry-after"] == "1"
