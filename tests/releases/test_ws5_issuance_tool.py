"""End-to-end proof that the issuance tool's output satisfies the worker chain.

The tool generates keys, issues the release chain, and the resulting artifacts
must pass the real one-shot cleanroom verifier subprocess and back a provider
G2 smoke run against a local mock transport.  No network, no PostgreSQL.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from app.releases.core import AuthorityPolicy
from app.worker.inference import run_provider_g2_smoke
from scripts.ws5_issue_provider_binding import main as issuance_main

BUILD_HASH = "e" * 64


def _issue_into(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    keys = tmp_path / "keys"
    release = tmp_path / "release"
    assert issuance_main(["--generate-keys", str(keys)]) == 0
    assert (
        issuance_main(
            [
                "--output-dir",
                str(release),
                "--root-private-key",
                str(keys / "root-private.seed"),
                "--issuer-private-key",
                str(keys / "issuer-private.seed"),
                "--app-env",
                "test",
                "--gateway-url",
                "http://127.0.0.1:8000/v1",
                "--gateway-model",
                "model@2026",
                "--credential-slot-id",
                "gateway-primary",
                "--runtime-build-version",
                "v1",
                "--runtime-build-hash",
                BUILD_HASH,
            ]
        )
        == 0
    )
    pins = AuthorityPolicy.model_validate_json((release / "release-pins.json").read_bytes())
    for name, value in {
        "AI_GATEWAY_RELEASE_AUTHORITY_DIR": str(release / "authority"),
        "AI_GATEWAY_RELEASE_CANDIDATE_PATH": str(release / "core-release-identity.json"),
        "AI_GATEWAY_RELEASE_EXPECTED_FACTS_PATH": str(release / "core-release-expected-facts.json"),
        "AI_GATEWAY_RELEASE_SIGNATURE_PATH": str(release / "core-release-expected-facts.signature.json"),
        "AI_GATEWAY_PROVIDER_MANIFEST_PATH": str(release / "provider-binding-manifest.json"),
        "AI_GATEWAY_RELEASE_ROOT_PUBLIC_KEY_SHA256": pins.root_public_key_sha256,
        "AI_GATEWAY_RELEASE_POLICY_REF": pins.policy_ref,
        "AI_GATEWAY_RELEASE_POLICY_VERSION": pins.policy_version,
        "AI_GATEWAY_RELEASE_POLICY_SHA256": pins.policy_sha256,
    }.items():
        monkeypatch.setenv(name, value)
    for name, value in {
        "AI_GATEWAY_URL": "http://127.0.0.1:8000/v1",
        "AI_GATEWAY_API_KEY": "local-test-key",
        "AI_GATEWAY_MODEL": "model@2026",
        "AI_GATEWAY_CREDENTIAL_SLOT_ID": "gateway-primary",
    }.items():
        monkeypatch.setenv(name, value)


@pytest.mark.asyncio
async def test_issued_chain_passes_cleanroom_and_backs_g2_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _issue_into(tmp_path, monkeypatch)

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "issued-g2",
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
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
            },
        )

    evidence = await run_provider_g2_smoke(
        app_env="test",
        runtime_build_hash=BUILD_HASH,
        transport=httpx.MockTransport(handler),
    )
    assert evidence.sentinel == "G2_OK"
    assert evidence.physical_sends == 1
    assert evidence.input_tokens == 2
    assert evidence.output_tokens == 3


@pytest.mark.asyncio
async def test_issued_chain_rejects_tampered_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.inference.errors import InferenceError

    _issue_into(tmp_path, monkeypatch)
    monkeypatch.setenv("AI_GATEWAY_RELEASE_ROOT_PUBLIC_KEY_SHA256", "f" * 64)

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(InferenceError):
        await run_provider_g2_smoke(
            app_env="test",
            runtime_build_hash=BUILD_HASH,
            transport=httpx.MockTransport(handler),
        )


def test_generate_keys_refuses_to_overwrite_existing_material(tmp_path: Path) -> None:
    keys = tmp_path / "keys"
    assert issuance_main(["--generate-keys", str(keys)]) == 0
    assert issuance_main(["--generate-keys", str(keys)]) != 0


def test_issuance_refuses_non_empty_output_directory(
    tmp_path: Path,
) -> None:
    keys = tmp_path / "keys"
    release = tmp_path / "release"
    release.mkdir()
    assert issuance_main(["--generate-keys", str(keys)]) == 0
    (release / "stale.txt").write_text("occupied")
    assert (
        issuance_main(
            [
                "--output-dir",
                str(release),
                "--root-private-key",
                str(keys / "root-private.seed"),
                "--issuer-private-key",
                str(keys / "issuer-private.seed"),
                "--app-env",
                "test",
                "--gateway-url",
                "http://127.0.0.1:8000/v1",
                "--gateway-model",
                "model@2026",
                "--credential-slot-id",
                "gateway-primary",
                "--runtime-build-hash",
                BUILD_HASH,
            ]
        )
        != 0
    )


def test_issuance_requires_complete_inputs(tmp_path: Path) -> None:
    assert issuance_main(["--output-dir", str(tmp_path / "release")]) != 0
