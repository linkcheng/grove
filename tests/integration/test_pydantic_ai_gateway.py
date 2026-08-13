from __future__ import annotations

import os
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path

import httpx
import pytest
from app.contracts.canonical import canonical_bytes, canonical_hash
from app.inference.ai_config import load_ai_gateway_config
from app.inference.contracts import ProviderBindingManifest
from app.inference.schema_catalog import STRUCTURED_INPUT_REF, STRUCTURED_OUTPUT_REF
from app.releases.core import (
    EXPECTED_FACTS_DOMAIN,
    FACTS_SIGNATURE_SCHEMA_VERSION,
    CoreReleaseIdentity,
    canonical_core_release_bytes,
    canonical_expected_facts_bytes,
)
from app.worker.inference import (
    _adapter_fingerprint,
    _distribution_fingerprint,
    _endpoint_config_fingerprint,
    run_provider_g2_smoke,
)
from tests.releases.test_core_release_authority import (
    K1_PRIVATE,
    _authority_mount,
    _authority_policy,
    _candidate_payload,
    _facts,
    _policy,
    _signature_bytes,
    _write_read_only,
)

pytestmark = pytest.mark.integration


def _install_signed_provider_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    app_env: str,
) -> ProviderBindingManifest:
    """Install a real signed release/manifest chain for the Worker entry test."""

    if app_env == "test":
        monkeypatch.setenv("AI_GATEWAY_URL", "http://127.0.0.1/v1")
        monkeypatch.setenv("AI_GATEWAY_API_KEY", "local-test-key")
        monkeypatch.setenv("AI_GATEWAY_MODEL", "model@2026")
        monkeypatch.setenv("AI_GATEWAY_CREDENTIAL_SLOT_ID", "gateway-primary")
    config = load_ai_gateway_config(app_env=app_env)
    manifest_data: dict[str, object] = {
        "schema_version": "provider-binding-manifest.v1",
        "provider_type": "openai-compatible",
        "endpoint_url": config.url,
        "provider_profile": {
            "profile_ref": {"ref": "provider-profile/gateway", "version": "v1", "content_hash": "9" * 64},
            "supports_tools": False,
            "supports_json_schema_output": False,
            "supports_json_object_output": True,
            "default_structured_output_mode": "prompted",
            "openai_chat_supports_max_completion_tokens": False,
        },
        "model_identifier": config.model,
        "model_hash": sha256(config.model.encode()).hexdigest(),
        "endpoint_config_fingerprint": "0" * 64,
        "sdk_version": version("openai"),
        "sdk_hash": _distribution_fingerprint("openai"),
        "pydantic_ai_version": version("pydantic-ai-slim"),
        "pydantic_ai_hash": _distribution_fingerprint("pydantic-ai-slim"),
        "adapter_version": "grove.inference.v2",
        "adapter_hash": _adapter_fingerprint(),
        "runtime_build_version": "v1",
        "runtime_build_hash": "e" * 64,
        "model_policy": {"model_ref": config.model, "temperature": 0.0, "max_output_tokens": 1024},
        "retry_policy": {"max_schema_retries": 0, "max_provider_retries": 0},
        "budget_policy": {"max_tokens": 4096, "max_cost_micros": 1_000_000, "deadline_ms": 120_000},
        "pricing_policy": {
            "currency": "CNY",
            "input_micros_per_million": 1,
            "output_micros_per_million": 1,
            "base_cost_micros": 1,
        },
        "input_schema_ref": STRUCTURED_INPUT_REF,
        "output_schema_ref": STRUCTURED_OUTPUT_REF,
        "prompt_policy_ref": {"ref": "prompt.g2@v1", "version": "v1", "content_hash": "4" * 64},
        "model_policy_ref": {"ref": "policy.model.g2@v1", "version": "v1", "content_hash": "5" * 64},
        "retry_policy_ref": {"ref": "policy.retry.g2@v1", "version": "v1", "content_hash": "6" * 64},
        "budget_policy_ref": {"ref": "policy.budget.g2@v1", "version": "v1", "content_hash": "7" * 64},
        "pricing_policy_ref": {"ref": "policy.pricing.g2@v1", "version": "v1", "content_hash": "8" * 64},
        "sdk_max_retries": 0,
        "credential_slot_id": config.credential_slot_id,
    }
    profile = manifest_data["provider_profile"]
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
        reference = manifest_data[ref_name]
        assert type(reference) is dict
        reference["content_hash"] = canonical_hash(manifest_data[value_name])
    draft = ProviderBindingManifest.model_validate(manifest_data)
    manifest_data["endpoint_config_fingerprint"] = _endpoint_config_fingerprint(config, draft)
    manifest = ProviderBindingManifest.model_validate(manifest_data)
    manifest_bytes = canonical_bytes(manifest)

    candidate_data = _candidate_payload()
    execution = candidate_data["execution"]
    assert type(execution) is dict
    execution["model"] = {"ref": manifest.model_identifier, "version": "v1", "content_hash": manifest.model_hash}
    execution["provider"] = {
        "ref": "provider.selected@g2",
        "version": "v1",
        "content_hash": sha256(manifest_bytes).hexdigest(),
    }
    execution["model_policy"] = {
        "ref": manifest.model_policy_ref.ref,
        "version": manifest.model_policy_ref.version,
        "content_hash": manifest.model_policy_ref.content_hash,
    }
    execution["adapter"] = {
        "ref": manifest.adapter_version,
        "version": "v1",
        "content_hash": manifest.adapter_hash,
    }
    candidate = CoreReleaseIdentity.model_validate(candidate_data)
    policy = _policy()
    pins = _authority_policy(policy)
    facts = _facts(candidate, policy)
    facts_bytes = canonical_expected_facts_bytes(facts)
    authority = _authority_mount(tmp_path, policy)
    candidate_path = tmp_path / "candidate.json"
    facts_path = tmp_path / "facts.json"
    signature_path = tmp_path / "facts-signature.json"
    manifest_path = tmp_path / "provider-binding-manifest.json"
    _write_read_only(candidate_path, canonical_core_release_bytes(candidate))
    _write_read_only(facts_path, facts_bytes)
    _write_read_only(
        signature_path,
        _signature_bytes(
            schema_version=FACTS_SIGNATURE_SCHEMA_VERSION,
            signer=facts.trusted_issuer,
            private_key=K1_PRIVATE,
            domain=EXPECTED_FACTS_DOMAIN,
            payload=facts_bytes,
        ),
    )
    _write_read_only(manifest_path, manifest_bytes)
    for name, value in {
        "AI_GATEWAY_RELEASE_AUTHORITY_DIR": str(authority),
        "AI_GATEWAY_RELEASE_CANDIDATE_PATH": str(candidate_path),
        "AI_GATEWAY_RELEASE_EXPECTED_FACTS_PATH": str(facts_path),
        "AI_GATEWAY_RELEASE_SIGNATURE_PATH": str(signature_path),
        "AI_GATEWAY_PROVIDER_MANIFEST_PATH": str(manifest_path),
        "AI_GATEWAY_RELEASE_ROOT_PUBLIC_KEY_SHA256": pins.root_public_key_sha256,
        "AI_GATEWAY_RELEASE_POLICY_REF": pins.policy_ref,
        "AI_GATEWAY_RELEASE_POLICY_VERSION": pins.policy_version,
        "AI_GATEWAY_RELEASE_POLICY_SHA256": pins.policy_sha256,
    }.items():
        monkeypatch.setenv(name, value)
    return manifest


@pytest.mark.asyncio
async def test_worker_provider_entry_uses_signed_manifest_and_local_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_signed_provider_binding(tmp_path, monkeypatch, app_env="test")
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "local-g2",
                "object": "chat.completion",
                "created": 1,
                "model": "model@2026",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": '{"answer":"G2_OK"}'},
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
            },
        )

    evidence = await run_provider_g2_smoke(
        app_env="test",
        runtime_build_hash="e" * 64,
        transport=httpx.MockTransport(handler),
    )
    assert evidence.sentinel == "G2_OK"
    assert evidence.physical_sends == len(requests) == 1
    assert evidence.schema_retries == 0
    assert evidence.input_tokens == 3
    assert evidence.output_tokens == 4
    assert evidence.cost_micros == 2
    assert "local-test-key" not in evidence.model_dump_json()
    assert "G2_OK" not in requests[0].url.path


@pytest.mark.asyncio
async def test_real_gateway_structured_output_usage_and_physical_request_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.environ.get("GROVE_RUN_PROVIDER_G2") != "1":
        pytest.skip("set GROVE_RUN_PROVIDER_G2=1 for the real provider gate")
    manifest = _install_signed_provider_binding(tmp_path, monkeypatch, app_env="production")
    evidence = await run_provider_g2_smoke(
        app_env="production",
        runtime_build_hash=manifest.runtime_build_hash,
    )
    assert evidence.sentinel == "G2_OK"
    assert evidence.physical_sends == 1
    assert evidence.schema_retries == 0
    assert evidence.input_tokens > 0
    assert evidence.output_tokens > 0
    assert evidence.cost_micros == 2
