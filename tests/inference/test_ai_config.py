from __future__ import annotations

import pytest
from app.inference.ai_config import load_ai_gateway_config
from pydantic import SecretStr, ValidationError


def test_gateway_config_uses_one_secret_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_GATEWAY_URL", "https://gateway.example/v1")
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "key-that-must-not-be-logged")
    monkeypatch.setenv("AI_GATEWAY_MODEL", "model@2026")
    monkeypatch.setenv("AI_GATEWAY_CREDENTIAL_SLOT_ID", "gateway-primary")
    config = load_ai_gateway_config(app_env="production")

    assert config.url == "https://gateway.example/v1"
    assert isinstance(config.api_key, SecretStr)
    assert config.api_key.get_secret_value() == "key-that-must-not-be-logged"
    assert config.model == "model@2026"
    assert "key-that-must-not-be-logged" not in repr(config)
    assert "key-that-must-not-be-logged" not in str(config.safe_dict())


@pytest.mark.parametrize(
    "url",
    (
        "http://gateway.example/v1",
        "https://gateway.example/v1?token=leak",
        "https://user:pass@gateway.example/v1",
        "https://gateway.example/v1/../admin",
        "https://gateway.example/v1/ ",
    ),
)
def test_production_url_is_closed(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("AI_GATEWAY_URL", url)
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "key")
    monkeypatch.setenv("AI_GATEWAY_MODEL", "model@2026")
    monkeypatch.setenv("AI_GATEWAY_CREDENTIAL_SLOT_ID", "gateway-primary")

    with pytest.raises(ValidationError):
        load_ai_gateway_config(app_env="production")


def test_local_http_is_only_for_loopback_test_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_GATEWAY_URL", "http://127.0.0.1/v1")
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "key")
    monkeypatch.setenv("AI_GATEWAY_MODEL", "model@2026")
    monkeypatch.setenv("AI_GATEWAY_CREDENTIAL_SLOT_ID", "gateway-primary")
    assert load_ai_gateway_config(app_env="test").url == "http://127.0.0.1/v1"

    with pytest.raises(ValidationError):
        load_ai_gateway_config(app_env="production")


def test_signed_versioned_gateway_path_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_GATEWAY_URL", "https://gateway.example/api/coding/paas/v4")
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "key")
    monkeypatch.setenv("AI_GATEWAY_MODEL", "model@2026")
    monkeypatch.setenv("AI_GATEWAY_CREDENTIAL_SLOT_ID", "gateway-primary")
    assert load_ai_gateway_config(app_env="production").url.endswith("/v4")


def test_unknown_or_openai_environment_is_not_a_second_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_GATEWAY_URL", "https://gateway.example/v1")
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "key")
    monkeypatch.setenv("AI_GATEWAY_MODEL", "model@2026")
    monkeypatch.setenv("AI_GATEWAY_CREDENTIAL_SLOT_ID", "gateway-primary")
    monkeypatch.setenv("OPENAI_API_KEY", "second-secret")
    config = load_ai_gateway_config(app_env="production")
    assert config.api_key.get_secret_value() == "key"


@pytest.mark.parametrize("secret", (None, "", "   ", " secret "))
def test_gateway_secret_missing_or_whitespace_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    secret: str | None,
) -> None:
    monkeypatch.setenv("AI_GATEWAY_URL", "https://gateway.example/v1")
    monkeypatch.setenv("AI_GATEWAY_MODEL", "model@2026")
    monkeypatch.setenv("AI_GATEWAY_CREDENTIAL_SLOT_ID", "gateway-primary")
    if secret is None:
        monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    else:
        monkeypatch.setenv("AI_GATEWAY_API_KEY", secret)
    with pytest.raises(ValidationError) as exc_info:
        load_ai_gateway_config(app_env="production")
    if secret and secret.strip():
        assert secret.strip() not in repr(exc_info.value)
