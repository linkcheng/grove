from __future__ import annotations

import ast
import inspect
import json

import pytest
from app.core.errors import AppError, DependencyUnavailableError
from app.core.observability import NonBlockingQueueHandler
from app.main import create_app
from fastapi import HTTPException
from fastapi.testclient import TestClient


def test_liveness_uses_unified_response_and_propagates_valid_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROVE_ROLE", "api")
    with TestClient(create_app()) as client:
        response = client.get("/api/v1/health/live", headers={"X-Request-ID": "req_123"})
    assert response.status_code == 200
    assert response.headers["x-request-id"] == "req_123"
    assert response.json() == {
        "code": 0,
        "message": "success",
        "data": {"status": "live"},
        "trace_id": "req_123",
    }


def test_liveness_discards_invalid_request_id_and_generates_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROVE_ROLE", "api")
    with TestClient(create_app()) as client:
        response = client.get("/api/v1/health/live", headers={"X-Request-ID": "bad id"})
    trace_id = response.headers["x-request-id"]
    assert response.status_code == 200
    assert len(trace_id) == 32
    assert response.json()["trace_id"] == trace_id


def test_unknown_route_is_unified_and_contains_trace_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROVE_ROLE", "api")
    with TestClient(create_app()) as client:
        response = client.get("/api/v1/not-found", headers={"X-Request-ID": "not_found"})
    assert response.status_code == 404
    assert response.headers["x-request-id"] == "not_found"
    assert response.json()["code"] == 40400
    assert response.json()["trace_id"] == "not_found"


def test_non_api_role_cannot_create_http_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROVE_ROLE", "runtime_worker")
    try:
        create_app()
    except ValueError as exc:
        assert "api" in str(exc)
    else:
        raise AssertionError("non-api role unexpectedly created an HTTP app")


def test_readiness_success_and_database_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROVE_ROLE", "api")

    async def available(_engine: object, _timeout: float) -> bool:
        return True

    monkeypatch.setattr("app.api.v1.health.check_database", available)
    with TestClient(create_app()) as client:
        success = client.get("/api/v1/health/ready", headers={"X-Request-ID": "ready_ok"})
    assert success.status_code == 200
    assert success.json()["data"] == {"status": "ready"}

    async def unavailable(_engine: object, _timeout: float) -> bool:
        return False

    monkeypatch.setattr("app.api.v1.health.check_database", unavailable)
    with TestClient(create_app()) as client:
        failure = client.get("/api/v1/health/ready", headers={"X-Request-ID": "ready_bad"})
    assert failure.status_code == 503
    assert failure.json()["code"] == 50301
    assert failure.json()["trace_id"] == "ready_bad"


def test_http_and_validation_exception_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROVE_ROLE", "api")
    app = create_app()

    @app.get("/raise-http/{status}")
    async def raise_http(status: int) -> None:
        raise HTTPException(status_code=status, detail="bad request")

    @app.get("/raise-error")
    async def raise_error() -> None:
        raise RuntimeError("unexpected")

    @app.get("/validate/{value}")
    async def validate(value: int) -> int:
        return value

    with TestClient(app, raise_server_exceptions=False) as client:
        bad_request = client.get("/raise-http/400", headers={"X-Request-ID": "bad_request"})
        assert bad_request.status_code == 400
        assert bad_request.json()["code"] == 40000
        server_error = client.get("/raise-http/500", headers={"X-Request-ID": "server_error"})
        assert server_error.status_code == 500
        assert server_error.json()["code"] == 50000
        unexpected = client.get("/raise-error", headers={"X-Request-ID": "unexpected"})
        assert unexpected.status_code == 500
        assert unexpected.json()["trace_id"] == "unexpected"
        invalid = client.get("/validate/not-an-int", headers={"X-Request-ID": "invalid"})
    assert invalid.status_code == 422
    assert invalid.json()["code"] == 42200


def test_app_error_handler_preserves_retry_after_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROVE_ROLE", "api")
    app = create_app()

    @app.get("/raise-dependency-error")
    async def raise_dependency_error() -> None:
        raise DependencyUnavailableError()

    @app.get("/raise-app-error")
    async def raise_app_error() -> None:
        raise AppError(40999, "conflict", error_code="Conflict", status_code=409)

    with TestClient(app, raise_server_exceptions=False) as client:
        dependency = client.get("/raise-dependency-error", headers={"X-Request-ID": "dep_error"})
        plain = client.get("/raise-app-error", headers={"X-Request-ID": "plain_error"})

    assert dependency.status_code == 503
    assert dependency.json()["retry_after"] == 1
    assert dependency.headers["retry-after"] == "1"
    assert plain.status_code == 409
    assert "retry_after" not in plain.json()
    assert "retry-after" not in plain.headers


def test_unexpected_exception_writes_structured_observability_fields(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GROVE_ROLE", "api")
    app = create_app()

    @app.get("/raise-error-for-log")
    async def raise_error_for_log() -> None:
        raise RuntimeError("unexpected")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/raise-error-for-log", headers={"X-Request-ID": "log_trace"})

    assert response.status_code == 500
    events = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")]
    assert any(
        event.get("event") == "unhandled_exception"
        and event.get("trace_id") == "log_trace"
        and event.get("status") == 500
        and isinstance(event.get("duration_ms"), float)
        for event in events
    )


def test_application_configures_a_real_structured_log_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROVE_ROLE", "api")
    app = create_app()
    with TestClient(app):
        assert isinstance(app.state.logging_runtime.queue_handler, NonBlockingQueueHandler)


def test_request_log_uses_route_template_without_raw_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GROVE_ROLE", "api")
    with TestClient(create_app()) as client:
        response = client.get("/DO_NOT_LOG_THIS", headers={"X-Request-ID": "safe_route"})

    assert response.status_code == 404
    output = capsys.readouterr().err
    assert "DO_NOT_LOG_THIS" not in output
    events = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    assert any(event.get("event") == "request_complete" and event.get("route") == "unmatched" for event in events)


def test_lifespan_owns_one_engine_and_disposes_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROVE_ROLE", "api")
    created: list[object] = []
    disposed: list[object] = []

    class FakeEngine:
        async def dispose(self) -> None:
            disposed.append(self)

    def fake_create_engine(_settings: object) -> FakeEngine:
        engine = FakeEngine()
        created.append(engine)
        return engine

    monkeypatch.setattr("app.main.create_engine", fake_create_engine)
    with TestClient(create_app()):
        assert len(created) == 1
    assert disposed == created


def test_create_app_is_composition_only() -> None:
    tree = ast.parse(inspect.getsource(create_app))
    factory = tree.body[0]
    assert isinstance(factory, ast.FunctionDef)
    nested_functions = [
        node
        for node in ast.walk(factory)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not factory
    ]
    assert nested_functions == []
