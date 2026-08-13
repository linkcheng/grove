"""Single secret/config boundary for the selected AI gateway."""

from __future__ import annotations

import os
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.contracts.canonical import VersionedRef


class AIGatewayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    app_env: Literal["production", "staging", "development", "test"]
    url: str = Field(min_length=1, max_length=2048)
    api_key: SecretStr
    model: str = Field(min_length=1, max_length=256)
    credential_slot_id: str = Field(pattern=r"^[a-z][a-z0-9._-]{0,127}$")

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str, info: Any) -> str:
        if type(value) is not str or value != value.strip() or "\\" in value:
            raise ValueError("invalid AI gateway URL")
        parsed = urlsplit(value)
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not parsed.path
            or parsed.path.endswith("/")
            or any(segment in {"", ".", ".."} for segment in parsed.path.split("/")[1:])
        ):
            raise ValueError("invalid AI gateway URL")
        app_env = info.data.get("app_env")
        if app_env in {"production", "staging"} and parsed.scheme != "https":
            raise ValueError("production AI gateway requires HTTPS")
        if app_env in {"development", "test"}:
            if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
                raise ValueError("plain HTTP is restricted to loopback")
            if parsed.scheme not in {"http", "https"}:
                raise ValueError("invalid AI gateway URL scheme")
        return value

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        # Reuse the canonical precise-ref grammar without retaining a fake hash.
        VersionedRef(ref=value, version="v1", content_hash="a" * 64)
        return value

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if type(secret) is not str or not secret or secret != secret.strip():
            raise ValueError("AI gateway credential is missing or invalid")
        return value

    def safe_dict(self) -> dict[str, str]:
        return {
            "url": self.url,
            "model": self.model,
            "credential_slot_id": self.credential_slot_id,
            "app_env": self.app_env,
        }


def load_ai_gateway_config(*, app_env: str) -> AIGatewayConfig:
    if type(app_env) is not str:
        raise TypeError("app_env must be an exact string")
    if app_env not in {"production", "staging", "development", "test"}:
        raise ValueError("unsupported application environment")
    checked_env = cast(Literal["production", "staging", "development", "test"], app_env)
    return AIGatewayConfig(
        app_env=checked_env,
        url=os.environ.get("AI_GATEWAY_URL", ""),
        api_key=SecretStr(os.environ.get("AI_GATEWAY_API_KEY", "")),
        model=os.environ.get("AI_GATEWAY_MODEL", ""),
        credential_slot_id=os.environ.get("AI_GATEWAY_CREDENTIAL_SLOT_ID", ""),
    )
