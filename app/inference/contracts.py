"""Private, signed-content-addressed provider binding contract."""

from __future__ import annotations

from hashlib import sha256
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.contracts.canonical import (
    InferenceBudget,
    ResolvedInferenceRetryPolicy,
    ResolvedModelPolicy,
    SafeCanonicalCodec,
    TypedSchemaRegistry,
    VersionedRef,
    canonical_hash,
)


class _ManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class PricingPolicy(_ManifestModel):
    currency: Literal["CNY"]
    input_micros_per_million: int = Field(ge=0, le=10_000_000_000)
    output_micros_per_million: int = Field(ge=0, le=10_000_000_000)
    base_cost_micros: int = Field(ge=0, le=10_000_000_000)


class ProviderProfilePolicy(_ManifestModel):
    profile_ref: VersionedRef
    supports_tools: bool
    supports_json_schema_output: bool
    supports_json_object_output: bool
    default_structured_output_mode: Literal["native", "prompted"]
    openai_chat_supports_max_completion_tokens: bool

    @model_validator(mode="after")
    def validate_output_mode(self) -> ProviderProfilePolicy:
        if self.default_structured_output_mode == "native" and not self.supports_json_schema_output:
            raise ValueError("native output requires JSON Schema support")
        if self.default_structured_output_mode == "prompted" and not self.supports_json_object_output:
            raise ValueError("prompted output requires JSON object support")
        return self


class ProviderBindingManifest(_ManifestModel):
    schema_version: Literal["provider-binding-manifest.v1"]
    provider_type: Literal["openai-compatible"]
    endpoint_url: str = Field(min_length=1, max_length=2048)
    provider_profile: ProviderProfilePolicy
    model_identifier: str = Field(min_length=1, max_length=256)
    model_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    endpoint_config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    sdk_version: str = Field(min_length=1, max_length=64)
    sdk_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    pydantic_ai_version: str = Field(min_length=1, max_length=64)
    pydantic_ai_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    adapter_version: Literal["grove.inference.v2"]
    adapter_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_build_version: str = Field(min_length=1, max_length=128)
    runtime_build_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_policy: ResolvedModelPolicy
    retry_policy: ResolvedInferenceRetryPolicy
    budget_policy: InferenceBudget
    pricing_policy: PricingPolicy
    input_schema_ref: VersionedRef
    output_schema_ref: VersionedRef
    prompt_policy_ref: VersionedRef
    model_policy_ref: VersionedRef
    retry_policy_ref: VersionedRef
    budget_policy_ref: VersionedRef
    pricing_policy_ref: VersionedRef
    sdk_max_retries: Literal[0]
    credential_slot_id: str = Field(pattern=r"^[a-z][a-z0-9._-]{0,127}$")

    @model_validator(mode="after")
    def validate_binding(self) -> ProviderBindingManifest:
        if self.model_policy.model_ref != self.model_identifier:
            raise ValueError("model policy must bind the selected model")
        profile = self.provider_profile
        profile_payload = profile.model_dump(mode="python", exclude={"profile_ref"})
        if (
            profile.profile_ref.content_hash != canonical_hash(profile_payload)
            or self.model_policy_ref.content_hash != canonical_hash(self.model_policy)
            or self.retry_policy_ref.content_hash != canonical_hash(self.retry_policy)
            or self.budget_policy_ref.content_hash != canonical_hash(self.budget_policy)
            or self.pricing_policy_ref.content_hash != canonical_hash(self.pricing_policy)
        ):
            raise ValueError("provider binding policy hash mismatch")
        return self


# The manifest is itself part of the trust boundary.  Prevent runtime code
# from mutating the Pydantic configuration after the schema was compiled.
ProviderBindingManifest.model_config = MappingProxyType(dict(ProviderBindingManifest.model_config))  # type: ignore[assignment]


_MANIFEST_SCHEMA_REF = VersionedRef(
    ref="grove.provider-binding-manifest@v1",
    version="v1",
    content_hash=sha256(b"grove.provider-binding-manifest.v1.schema").hexdigest(),
)


def load_provider_binding_manifest(payload: object, *, expected_hash: str) -> ProviderBindingManifest:
    registry = TypedSchemaRegistry()
    registry.register(_MANIFEST_SCHEMA_REF, ProviderBindingManifest, role="input")
    manifest = SafeCanonicalCodec(registry).read_bytes(
        payload,
        expected_hash=expected_hash,
        schema_ref=_MANIFEST_SCHEMA_REF,
        role="input",
    )
    if type(manifest) is not ProviderBindingManifest:
        raise ValueError("invalid provider binding manifest")
    return manifest
