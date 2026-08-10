from __future__ import annotations

import pytest
from app.roles import run_role_self_check


@pytest.mark.parametrize("role", ["api", "runtime_worker", "projection_reconciliation", "offline_governance"])
def test_role_self_check_returns_configuration_summary(monkeypatch: pytest.MonkeyPatch, role: str) -> None:
    monkeypatch.setenv("GROVE_ROLE", role)
    if role == "runtime_worker":
        monkeypatch.setenv("GROVE_RUNTIME_BUILD_HASH", "a" * 64)
    monkeypatch.setattr("app.roles.database_available", lambda _settings: True)
    monkeypatch.setattr(
        "app.roles.projection_available", lambda _settings: {"backlog": 0, "dead_letter": 0, "status": "ready"}
    )
    result = run_role_self_check()
    assert result["role"] == role
    assert result["status"] == "configured"
    assert result["database"] == "postgresql"
    assert result["database_status"] == "connected"
    if role == "projection_reconciliation":
        assert result["projection"] == {"backlog": 0, "dead_letter": 0, "status": "ready"}
    if role == "runtime_worker":
        assert result["worker"]["worker_id"]
        assert result["worker"]["claim_protocol"] == "advisory_fence_lease"


def test_role_self_check_fails_before_start_for_bad_role(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROVE_ROLE", "unknown")
    with pytest.raises(ValueError):
        run_role_self_check()


def test_role_self_check_fails_when_database_credential_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROVE_ROLE", "runtime_worker")
    monkeypatch.setenv("GROVE_RUNTIME_BUILD_HASH", "a" * 64)
    monkeypatch.setattr("app.roles.database_available", lambda _settings: False)
    with pytest.raises(ValueError, match="database credential"):
        run_role_self_check()
