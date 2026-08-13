from __future__ import annotations

import re
from pathlib import Path

import pytest
from app.core.config import ConfigurationError, Role, Settings, load_settings
from app.main import _load_cli_settings

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"
METADATA_RE = re.compile(
    r"^用途：(?P<use>[^；]+)；要求：(?P<required>[^；]+)；适用：(?P<scope>[^；]+)；敏感：(?P<sensitive>是|否)。?$"
)


def _parse_env_example() -> tuple[dict[str, str], dict[str, str], dict[str, dict[str, str]], str]:
    """Parse active and commented shell-compatible assignments without evaluating the file."""

    content = ENV_EXAMPLE.read_text(encoding="utf-8")
    assignments: dict[str, str] = {}
    commented_assignments: dict[str, str] = {}
    metadata: dict[str, dict[str, str]] = {}
    pending_metadata: dict[str, str] | None = None
    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            pending_metadata = None
            continue
        commented = line.startswith("#")
        candidate = line[1:].lstrip() if commented else line
        metadata_match = METADATA_RE.fullmatch(candidate) if commented else None
        if metadata_match:
            pending_metadata = metadata_match.groupdict()
            continue
        key, separator, value = candidate.partition("=")
        if not separator:
            assert commented, f"line {line_number} is not an assignment"
            pending_metadata = None
            continue
        assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key), f"invalid key on line {line_number}"
        target = commented_assignments if commented else assignments
        assert key not in assignments and key not in commented_assignments, f"duplicate key: {key}"
        assert pending_metadata is not None, f"assignment {key} has no adjacent metadata"
        target[key] = value.strip()
        metadata[key] = pending_metadata
        pending_metadata = None
    return assignments, commented_assignments, metadata, content


def test_env_example_covers_runtime_ai_and_tooling_configuration() -> None:
    assignments, commented_assignments, metadata, content = _parse_env_example()
    settings_keys = {f"GROVE_{name.upper()}" for name in Settings.model_fields}
    ai_gateway_keys = {
        "AI_GATEWAY_URL",
        "AI_GATEWAY_API_KEY",
        "AI_GATEWAY_MODEL",
        "AI_GATEWAY_CREDENTIAL_SLOT_ID",
        "AI_GATEWAY_RELEASE_AUTHORITY_DIR",
        "AI_GATEWAY_RELEASE_CANDIDATE_PATH",
        "AI_GATEWAY_RELEASE_EXPECTED_FACTS_PATH",
        "AI_GATEWAY_RELEASE_SIGNATURE_PATH",
        "AI_GATEWAY_PROVIDER_MANIFEST_PATH",
        "AI_GATEWAY_RELEASE_ROOT_PUBLIC_KEY_SHA256",
        "AI_GATEWAY_RELEASE_POLICY_REF",
        "AI_GATEWAY_RELEASE_POLICY_VERSION",
        "AI_GATEWAY_RELEASE_POLICY_SHA256",
    }
    tooling_keys = {
        "GROVE_MIGRATION_DATABASE_URL",
        "GROVE_API_BASE_URL",
        "GROVE_REVERSE_MANIFEST",
        "GROVE_INTEGRATION_LIBRARY",
        "COMPOSE_PROJECT_NAME",
        "CLEANROOM_REMOVE_VOLUMES",
    }
    assert set(assignments) == settings_keys | ai_gateway_keys
    assert {key for key in assignments if key.startswith("GROVE_")} == settings_keys
    assert tooling_keys <= commented_assignments.keys()
    assert not tooling_keys & assignments.keys()
    documented_keys = set(assignments) | set(commented_assignments)
    assert set(metadata) == documented_keys
    for fields in metadata.values():
        assert fields["use"].strip()
        assert fields["required"].strip()
        assert fields["scope"].strip()
        assert fields["sensitive"] in {"是", "否"}
        assert any(marker in fields["required"] for marker in ("必填", "可选"))
        assert any("\u4e00" <= character <= "\u9fff" for character in "".join(fields.values()))

    api_key = assignments["AI_GATEWAY_API_KEY"]
    assert re.fullmatch(r"__[A-Z0-9_]+__", api_key)
    assert "__REPLACE_WITH_API_DB_PASSWORD__" in assignments["GROVE_DATABASE_URL"]
    assert "__REPLACE_WITH_MIGRATION_DB_PASSWORD__" in commented_assignments["GROVE_MIGRATION_DATABASE_URL"]
    for key, value in {**assignments, **commented_assignments}.items():
        if metadata[key]["sensitive"] == "是":
            assert "change_me" not in value.lower()
    assert not re.search(r"(?:sk-|Bearer\s+)[A-Za-z0-9_-]{8,}", content, flags=re.IGNORECASE)


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
    if role == "runtime_worker":
        monkeypatch.setenv("GROVE_RUNTIME_BUILD_HASH", "a" * 64)
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


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GROVE_DATABASE_POOL_SIZE", "0"),
        ("GROVE_DATABASE_MAX_OVERFLOW", "51"),
        ("GROVE_DATABASE_POOL_TIMEOUT_SECONDS", "31"),
    ],
)
def test_database_pool_configuration_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError):
        load_settings()


def test_cli_role_conflict_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROVE_ROLE", "runtime_worker")
    with pytest.raises(ConfigurationError, match="conflicts"):
        _load_cli_settings("api")


def test_unknown_grove_environment_key_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROVE_ROLE", "api")
    monkeypatch.setenv("GROVE_ROEL", "runtime_worker")
    monkeypatch.setenv("GROVE_RUNTIME_BUILD_HASH", "a" * 64)
    with pytest.raises(ConfigurationError, match="unknown"):
        load_settings()


def test_role_must_be_explicitly_declared(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROVE_ROLE", raising=False)
    with pytest.raises(ConfigurationError, match="role"):
        load_settings()
