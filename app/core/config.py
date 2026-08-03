"""Validated, secret-safe configuration for all application roles."""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigurationError(ValueError):
    """Raised when the process cannot start with the supplied configuration."""


class Role(StrEnum):
    API = "api"
    RUNTIME_WORKER = "runtime_worker"
    PROJECTION_RECONCILIATION = "projection_reconciliation"
    OFFLINE_GOVERNANCE = "offline_governance"


class Settings(BaseSettings):
    """Configuration shared by API and the three non-HTTP roles."""

    model_config = SettingsConfigDict(
        env_prefix="GROVE_",
        case_sensitive=False,
        extra="forbid",
        hide_input_in_errors=True,
        validate_default=True,
    )

    role: Role
    app_env: str = Field(default="development", min_length=1, max_length=32)
    app_timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=64)
    log_level: str = Field(default="INFO", min_length=1, max_length=16)
    database_url: SecretStr = SecretStr("postgresql+psycopg://grove_api:grove_api_ws0@localhost:5432/grove")
    app_image_id: str = Field(default="not_built", min_length=1, max_length=128)
    postgres_image_id: str = Field(default="not_resolved", min_length=1, max_length=128)
    readiness_timeout_seconds: float = Field(default=2.0, gt=0, le=10)

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        parsed = urlsplit(value.get_secret_value())
        if parsed.scheme not in {"postgresql", "postgresql+psycopg"} or not parsed.hostname:
            raise ValueError("GROVE_DATABASE_URL must be a PostgreSQL URL")
        return value

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("GROVE_LOG_LEVEL is invalid")
        return normalized

    def safe_dict(self) -> dict[str, Any]:
        """Return only non-secret values suitable for logs and evidence."""

        return {
            "role": self.role.value,
            "app_env": self.app_env,
            "app_timezone": self.app_timezone,
            "log_level": self.log_level,
            "app_image_id": self.app_image_id,
            "postgres_image_id": self.postgres_image_id,
            "readiness_timeout_seconds": self.readiness_timeout_seconds,
        }

    def database_url_value(self) -> str:
        """Read the database URL only at the database adapter boundary."""

        return self.database_url.get_secret_value()


def load_settings(overrides: Mapping[str, Any] | None = None) -> Settings:
    """Load and normalize settings, converting validation to a startup error."""

    known_names = {f"GROVE_{name.upper()}" for name in Settings.model_fields}
    unknown_names = sorted(
        name for name in os.environ if name.upper().startswith("GROVE_") and name.upper() not in known_names
    )
    if unknown_names:
        raise ConfigurationError(f"unknown GROVE configuration key: {unknown_names[0]}")
    try:
        return Settings(**dict(overrides or {}))
    except (ValidationError, ValueError) as exc:
        fields = []
        if isinstance(exc, ValidationError):
            fields = [".".join(str(part) for part in error.get("loc", ())) or "configuration" for error in exc.errors()]
        detail = ", ".join(fields) if fields else "configuration"
        raise ConfigurationError(f"invalid GROVE configuration: {detail}") from None
