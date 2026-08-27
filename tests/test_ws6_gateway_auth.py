"""WS-6 gateway authentication: trusted header injection behind a shared secret."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from app.auth.context import PrincipalKind, _context_from_gateway_headers
from app.core.config import Role, Settings
from app.main import create_app
from fastapi import Request
from pydantic import SecretStr
from starlette.testclient import TestClient

GATEWAY_TOKEN = "g" * 32
TENANT = "tenant-gw"


def _gateway_settings(app_env: str = "test") -> Settings:
    return Settings(
        role=Role.API,
        app_env=app_env,
        auth_mode="gateway",
        gateway_auth_token=SecretStr(GATEWAY_TOKEN),
        database_url=SecretStr("postgresql+psycopg://grove_api:invalid@127.0.0.1:1/grove"),
        readiness_timeout_seconds=0.2,
    )


def _client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings), raise_server_exceptions=False)


def _submit(client: TestClient, headers: dict[str, str]) -> Any:
    return client.post(
        "/api/v1/executions/submit",
        json={
            "submission_id": str(uuid4()),
            "intent": {
                "intent_id": str(uuid4()),
                "skill_ref": "fixture.skill@1",
                "input": {"question": "hello"},
                "constraints": {},
            },
        },
        headers=headers,
    )


def _gateway_headers(**overrides: str) -> dict[str, str]:
    headers = {
        "x-grove-gateway-auth": GATEWAY_TOKEN,
        "x-grove-tenant-id": TENANT,
        "x-grove-principal-id": "user-gw",
    }
    headers.update(overrides)
    return headers


def test_gateway_settings_require_a_strong_shared_secret() -> None:
    with pytest.raises(ValueError, match="GROVE_GATEWAY_AUTH_TOKEN"):
        Settings(role=Role.API, app_env="test", auth_mode="gateway")
    with pytest.raises(ValueError, match="GROVE_GATEWAY_AUTH_TOKEN"):
        Settings(
            role=Role.API,
            app_env="test",
            auth_mode="gateway",
            gateway_auth_token=SecretStr("short-token"),
        )


def test_gateway_mode_is_valid_in_production_with_configured_token() -> None:
    settings = Settings(
        role=Role.API,
        app_env="production",
        auth_mode="gateway",
        gateway_auth_token=SecretStr(GATEWAY_TOKEN),
    )
    assert settings.auth_mode == "gateway"


def test_gateway_headers_bind_identity_with_gateway_strength() -> None:
    request = cast(Request, SimpleNamespace(headers=_gateway_headers()))
    context = _context_from_gateway_headers(request, GATEWAY_TOKEN)
    assert context is not None
    assert context.tenant_id == TENANT
    assert context.principal.principal_id == "user-gw"
    assert context.principal.kind is PrincipalKind.HUMAN
    assert context.auth_strength == "gateway"


def test_gateway_request_reaches_authorization_with_valid_secret() -> None:
    with _client(_gateway_settings()) as client:
        # The identity is accepted (auth passes); the unreachable database is
        # the next boundary, proving authentication completed first.
        response = _submit(client, _gateway_headers())
    assert response.status_code == 503
    assert response.json()["code"] == 50302


def test_gateway_request_rejects_wrong_secret() -> None:
    with _client(_gateway_settings()) as client:
        response = _submit(client, _gateway_headers(**{"x-grove-gateway-auth": "x" * 32}))
    assert response.status_code == 401
    assert response.json()["code"] == 40100


def test_gateway_request_rejects_missing_identity_headers() -> None:
    with _client(_gateway_settings()) as client:
        partial = _gateway_headers()
        del partial["x-grove-principal-id"]
        response = _submit(client, partial)
    assert response.status_code == 401


def test_gateway_request_rejects_client_supplied_authorization_claims() -> None:
    with _client(_gateway_settings()) as client:
        response = _submit(client, _gateway_headers(**{"x-grove-principal-roles": "admin"}))
    assert response.status_code == 401


def test_gateway_request_rejects_combined_fixture_and_gateway_credentials() -> None:
    with _client(_gateway_settings()) as client:
        response = _submit(
            client,
            _gateway_headers(**{"x-grove-auth": "fixture", "authorization": "Bearer fixture:tenant-a:user-a"}),
        )
    assert response.status_code == 401


def test_gateway_mode_rejects_plain_bearer_credentials() -> None:
    with _client(_gateway_settings()) as client:
        response = _submit(
            client,
            {"authorization": f"Bearer {GATEWAY_TOKEN}", "x-grove-tenant-id": TENANT},
        )
    assert response.status_code == 401


def test_gateway_mode_without_credentials_is_unauthenticated() -> None:
    with _client(_gateway_settings()) as client:
        response = _submit(client, {})
    assert response.status_code == 401
    assert response.json()["code"] == 40100


def test_disabled_mode_still_rejects_gateway_headers() -> None:
    settings = Settings(role=Role.API, app_env="test")
    with _client(settings) as client:
        response = _submit(client, _gateway_headers())
    assert response.status_code == 401
