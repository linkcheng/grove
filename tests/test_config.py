from __future__ import annotations

import pytest
from app.core.config import ConfigurationError, Role, Settings, load_settings
from app.main import _load_cli_settings


def test_role_is_closed_and_allows_only_four_roles() -> None:
    assert set(Role) == {
        Role.API,
        Role.RUNTIME_WORKER,
        Role.PROJECTION_RECONCILIATION,
        Role.OFFLINE_GOVERNANCE,
    }


@pytest.mark.parametrize("role", ["api", "runtime_worker", "projection_reconciliation", "offline_governance"])
def test_settings_accepts_declared_roles(monkeypatch: pytest.MonkeyPatch, role: str) -> None:
    monkeypatch.setenv("GROVE_ROLE", role)
    settings = load_settings()
    assert settings.role.value == role


@pytest.mark.parametrize("role", ["", "worker", "projection", "api/worker", "API"])
def test_settings_rejects_unknown_roles(monkeypatch: pytest.MonkeyPatch, role: str) -> None:
    monkeypatch.setenv("GROVE_ROLE", role)
    with pytest.raises(ConfigurationError):
        load_settings()


def test_settings_rejects_invalid_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    invalid_url = "sqlite:///not-postgres?password=super-secret"
    monkeypatch.setenv("GROVE_DATABASE_URL", invalid_url)
    with pytest.raises(ConfigurationError) as error:
        load_settings()
    assert invalid_url not in repr(error.value)


def test_settings_never_exposes_secret_in_safe_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROVE_DATABASE_URL", "postgresql+psycopg://user:secret@localhost/grove")
    monkeypatch.setenv("GROVE_ROLE", "api")
    settings = Settings(role=Role.API)
    safe = settings.safe_dict()
    assert "secret" not in repr(safe)
    assert "DATABASE_URL" not in repr(safe)


def test_log_level_is_normalized_and_invalid_values_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROVE_LOG_LEVEL", "warning")
    assert Settings(role=Role.API).log_level == "WARNING"
    monkeypatch.setenv("GROVE_LOG_LEVEL", "verbose")
    with pytest.raises(ConfigurationError):
        load_settings()


def test_readiness_timeout_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROVE_READINESS_TIMEOUT_SECONDS", "0")
    with pytest.raises(ConfigurationError):
        load_settings()


def test_cli_role_conflict_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROVE_ROLE", "runtime_worker")
    with pytest.raises(ConfigurationError, match="conflicts"):
        _load_cli_settings("api")


def test_unknown_grove_environment_key_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROVE_ROLE", "api")
    monkeypatch.setenv("GROVE_ROEL", "runtime_worker")
    with pytest.raises(ConfigurationError, match="unknown"):
        load_settings()


def test_role_must_be_explicitly_declared(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROVE_ROLE", raising=False)
    with pytest.raises(ConfigurationError, match="role"):
        load_settings()
