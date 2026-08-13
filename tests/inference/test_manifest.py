from __future__ import annotations

from hashlib import sha256

import pytest
from app.contracts.canonical import canonical_bytes, canonical_hash
from app.inference.contracts import (
    ProviderBindingManifest,
    load_provider_binding_manifest,
)
from pydantic import ValidationError


def _manifest_data() -> dict[str, object]:
    ref = {"ref": "schema/input", "version": "v1", "content_hash": "a" * 64}
    out_ref = {"ref": "schema/output", "version": "v1", "content_hash": "b" * 64}
    data: dict[str, object] = {
        "schema_version": "provider-binding-manifest.v1",
        "provider_type": "openai-compatible",
        "endpoint_url": "https://gateway.example/v1",
        "provider_profile": {
            "profile_ref": {"ref": "provider-profile/gateway", "version": "v1", "content_hash": "9" * 64},
            "supports_tools": False,
            "supports_json_schema_output": True,
            "supports_json_object_output": True,
            "default_structured_output_mode": "native",
            "openai_chat_supports_max_completion_tokens": False,
        },
        "model_identifier": "model@2026",
        "model_hash": "f" * 64,
        "endpoint_config_fingerprint": "c" * 64,
        "sdk_version": "3.0.0",
        "sdk_hash": "1" * 64,
        "pydantic_ai_version": "2.22.0",
        "pydantic_ai_hash": "2" * 64,
        "adapter_version": "grove.inference.v2",
        "adapter_hash": "3" * 64,
        "runtime_build_version": "build@2026",
        "runtime_build_hash": "d" * 64,
        "model_policy": {"model_ref": "model@2026", "temperature": 0.0, "max_output_tokens": 128},
        "retry_policy": {"max_schema_retries": 1, "max_provider_retries": 1},
        "budget_policy": {"max_tokens": 1000, "max_cost_micros": 100000, "deadline_ms": 15000},
        "pricing_policy": {
            "currency": "CNY",
            "input_micros_per_million": 100,
            "output_micros_per_million": 200,
            "base_cost_micros": 10,
        },
        "input_schema_ref": ref,
        "output_schema_ref": out_ref,
        "prompt_policy_ref": {"ref": "prompt@v1", "version": "v1", "content_hash": "4" * 64},
        "model_policy_ref": {"ref": "model-policy@v1", "version": "v1", "content_hash": "5" * 64},
        "retry_policy_ref": {"ref": "retry@v1", "version": "v1", "content_hash": "6" * 64},
        "budget_policy_ref": {"ref": "budget@v1", "version": "v1", "content_hash": "7" * 64},
        "pricing_policy_ref": {"ref": "pricing@v1", "version": "v1", "content_hash": "8" * 64},
        "sdk_max_retries": 0,
        "credential_slot_id": "gateway-primary",
    }
    profile = data["provider_profile"]
    assert type(profile) is dict
    profile_ref = profile["profile_ref"]
    assert type(profile_ref) is dict
    profile_ref["content_hash"] = canonical_hash({key: value for key, value in profile.items() if key != "profile_ref"})
    for value_name, ref_name in (
        ("model_policy", "model_policy_ref"),
        ("retry_policy", "retry_policy_ref"),
        ("budget_policy", "budget_policy_ref"),
        ("pricing_policy", "pricing_policy_ref"),
    ):
        reference = data[ref_name]
        assert type(reference) is dict
        reference["content_hash"] = canonical_hash(data[value_name])
    return data


def test_manifest_is_strict_and_frozen() -> None:
    manifest = ProviderBindingManifest.model_validate(_manifest_data())
    assert manifest.sdk_max_retries == 0
    with pytest.raises((ValidationError, TypeError)):
        manifest.model_config["extra"] = "allow"
    with pytest.raises(ValidationError):
        ProviderBindingManifest.model_validate({**_manifest_data(), "unexpected": True})
    with pytest.raises(ValidationError):
        ProviderBindingManifest.model_validate({**_manifest_data(), "sdk_max_retries": 1})


def test_manifest_loader_hashes_raw_bytes_before_schema() -> None:
    raw = canonical_bytes(_manifest_data())
    expected_hash = sha256(raw).hexdigest()
    loaded = load_provider_binding_manifest(raw, expected_hash=expected_hash)
    assert loaded.model_identifier == "model@2026"

    with pytest.raises(ValueError):
        load_provider_binding_manifest(raw, expected_hash="e" * 64)


def test_manifest_loader_rejects_duplicate_and_noncanonical_json() -> None:
    duplicate = b'{"schema_version":"provider-binding-manifest.v1","schema_version":"provider-binding-manifest.v1"}\n'
    with pytest.raises(ValueError):
        load_provider_binding_manifest(duplicate, expected_hash=sha256(duplicate).hexdigest())
