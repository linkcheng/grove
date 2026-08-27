"""Unit-protocol coverage for the runtime-worker inference composition root.

These tests reuse the real signed release chain and the real one-shot cleanroom
verifier subprocess, but no PostgreSQL and no network: the provider side is a
local ``httpx.MockTransport``.  They are therefore not integration tests in the
compose sense; the real-provider variant stays gated in
``tests/integration/test_pydantic_ai_gateway.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
from app.inference.errors import InferenceError, InferenceErrorCode
from app.inference.pydantic_ai_adapter import PydanticAIInferencePort
from app.worker import inference as worker_inference
from app.worker.inference import (
    ProviderG2Evidence,
    _read_preopened_regular_fd,
    production_inference_lifespan,
    run_provider_g2_smoke,
)
from pydantic import ValidationError
from tests.integration.test_pydantic_ai_gateway import _install_signed_provider_binding


def _completion_transport(answer: str) -> httpx.MockTransport:
    async def handler(_: httpx.Request) -> httpx.Response:
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
                        "message": {"role": "assistant", "content": f'{{"answer":"{answer}"}}'},
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
            },
        )

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_g2_smoke_local_transport_returns_bounded_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_signed_provider_binding(tmp_path, monkeypatch, app_env="test")
    evidence = await run_provider_g2_smoke(
        app_env="test",
        runtime_build_hash="e" * 64,
        transport=_completion_transport("G2_OK"),
    )
    assert evidence.sentinel == "G2_OK"
    assert evidence.physical_sends == 1
    assert evidence.schema_retries == 0
    assert evidence.input_tokens == 3
    assert evidence.output_tokens == 4
    assert evidence.cost_micros == 2


@pytest.mark.asyncio
async def test_g2_smoke_rejects_wrong_sentinel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_signed_provider_binding(tmp_path, monkeypatch, app_env="test")
    with pytest.raises(InferenceError) as exc_info:
        await run_provider_g2_smoke(
            app_env="test",
            runtime_build_hash="e" * 64,
            transport=_completion_transport("NOT_G2"),
        )
    assert exc_info.value.code is InferenceErrorCode.INVALID_RESULT


@pytest.mark.asyncio
async def test_g2_smoke_rejects_injected_transport_outside_dev_environments() -> None:
    with pytest.raises(InferenceError) as exc_info:
        await run_provider_g2_smoke(
            app_env="production",
            runtime_build_hash="e" * 64,
            transport=_completion_transport("G2_OK"),
        )
    assert exc_info.value.code is InferenceErrorCode.INVALID_BINDING


@pytest.mark.asyncio
async def test_g2_smoke_rejects_non_string_app_env() -> None:
    with pytest.raises(TypeError):
        await run_provider_g2_smoke(
            app_env=object(),  # type: ignore[arg-type]
            runtime_build_hash="e" * 64,
        )


@pytest.mark.asyncio
async def test_lifespan_rejects_malformed_runtime_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_signed_provider_binding(tmp_path, monkeypatch, app_env="test")
    with pytest.raises(TypeError):
        async with production_inference_lifespan(app_env="test", runtime_build_hash="e" * 63):
            pytest.fail("composition must not yield")


@pytest.mark.asyncio
async def test_tampered_candidate_fails_inside_cleanroom_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_signed_provider_binding(tmp_path, monkeypatch, app_env="test")
    tampered = tmp_path / "candidate.tampered.json"
    tampered.write_bytes((tmp_path / "candidate.json").read_bytes() + b" ")
    monkeypatch.setenv("AI_GATEWAY_RELEASE_CANDIDATE_PATH", str(tampered))
    with pytest.raises(InferenceError) as exc_info:
        await run_provider_g2_smoke(
            app_env="test",
            runtime_build_hash="e" * 64,
            transport=_completion_transport("G2_OK"),
        )
    assert exc_info.value.code is InferenceErrorCode.INVALID_BINDING


@pytest.mark.asyncio
async def test_swapped_provider_manifest_fails_hash_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_signed_provider_binding(tmp_path, monkeypatch, app_env="test")
    swapped = tmp_path / "provider-binding-manifest.swapped.json"
    swapped.write_bytes((tmp_path / "provider-binding-manifest.json").read_bytes() + b"\n")
    monkeypatch.setenv("AI_GATEWAY_PROVIDER_MANIFEST_PATH", str(swapped))
    with pytest.raises(InferenceError) as exc_info:
        await run_provider_g2_smoke(
            app_env="test",
            runtime_build_hash="e" * 64,
            transport=_completion_transport("G2_OK"),
        )
    assert exc_info.value.code is InferenceErrorCode.INVALID_BINDING


@pytest.mark.asyncio
async def test_sdk_version_drift_fails_runtime_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_signed_provider_binding(tmp_path, monkeypatch, app_env="test")
    monkeypatch.setattr(worker_inference, "version", lambda _name: "0.0.0")
    with pytest.raises(InferenceError) as exc_info:
        await run_provider_g2_smoke(
            app_env="test",
            runtime_build_hash="e" * 64,
            transport=_completion_transport("G2_OK"),
        )
    assert exc_info.value.code is InferenceErrorCode.INVALID_BINDING


@pytest.mark.asyncio
async def test_missing_release_file_fails_before_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_signed_provider_binding(tmp_path, monkeypatch, app_env="test")
    monkeypatch.setenv(
        "AI_GATEWAY_RELEASE_CANDIDATE_PATH",
        str(tmp_path / "does-not-exist.json"),
    )
    with pytest.raises(InferenceError) as exc_info:
        await run_provider_g2_smoke(
            app_env="test",
            runtime_build_hash="e" * 64,
            transport=_completion_transport("G2_OK"),
        )
    assert exc_info.value.code is InferenceErrorCode.INVALID_BINDING


def test_invalid_pin_shape_fails_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_GATEWAY_RELEASE_ROOT_PUBLIC_KEY_SHA256", "not-a-sha256")
    with pytest.raises(InferenceError) as exc_info:
        worker_inference._load_production_inference_files()
    assert exc_info.value.code is InferenceErrorCode.INVALID_BINDING


@pytest.mark.asyncio
async def test_production_lifespan_yields_sealed_port_without_sending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_signed_provider_binding(tmp_path, monkeypatch, app_env="test")
    async with production_inference_lifespan(app_env="test", runtime_build_hash="e" * 64) as port:
        assert type(port) is PydanticAIInferencePort
        assert port.physical_sends == 0


def test_read_preopened_regular_fd_rejects_non_regular_descriptors() -> None:
    read_fd, write_fd = os.pipe()
    try:
        with pytest.raises(InferenceError) as exc_info:
            _read_preopened_regular_fd(read_fd, max_bytes=1024)
    finally:
        os.close(read_fd)
        os.close(write_fd)
    assert exc_info.value.code is InferenceErrorCode.INVALID_BINDING


def test_read_preopened_regular_fd_rejects_non_integer_input() -> None:
    with pytest.raises(TypeError):
        _read_preopened_regular_fd("not-an-fd", max_bytes=1024)


def test_read_preopened_regular_fd_requires_zero_offset(tmp_path: Path) -> None:
    target = tmp_path / "offset.json"
    target.write_bytes(b"{}")
    descriptor = os.open(target, os.O_RDONLY)
    try:
        os.lseek(descriptor, 1, os.SEEK_SET)
        with pytest.raises(InferenceError) as exc_info:
            _read_preopened_regular_fd(descriptor, max_bytes=1024)
    finally:
        os.close(descriptor)
    assert exc_info.value.code is InferenceErrorCode.INVALID_BINDING


def test_read_preopened_regular_fd_rejects_oversized_files(tmp_path: Path) -> None:
    target = tmp_path / "oversized.json"
    target.write_bytes(b"x" * 16)
    descriptor = os.open(target, os.O_RDONLY)
    try:
        with pytest.raises(InferenceError) as exc_info:
            _read_preopened_regular_fd(descriptor, max_bytes=8)
    finally:
        os.close(descriptor)
    assert exc_info.value.code is InferenceErrorCode.INVALID_BINDING


def test_provider_g2_evidence_enforces_bounded_strict_fields() -> None:
    assert (
        ProviderG2Evidence(
            sentinel="G2_OK",
            physical_sends=1,
            schema_retries=0,
            input_tokens=1,
            output_tokens=1,
            cost_micros=1,
        ).sentinel
        == "G2_OK"
    )
    with pytest.raises(ValidationError):
        ProviderG2Evidence(
            sentinel="G2_OK",
            physical_sends=0,
            schema_retries=0,
            input_tokens=1,
            output_tokens=1,
            cost_micros=1,
        )
    with pytest.raises(ValidationError):
        ProviderG2Evidence.model_validate(
            {
                "sentinel": "G2_OK",
                "physical_sends": 1,
                "schema_retries": 0,
                "input_tokens": 1,
                "output_tokens": 1,
                "cost_micros": 1,
                "extra": "forbidden",
            }
        )
