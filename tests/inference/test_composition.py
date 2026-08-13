from __future__ import annotations

import inspect
import os

import pytest
from app.inference.errors import InferenceError, InferenceErrorCode
from app.worker.inference import production_inference_lifespan
from pydantic import BaseModel, ConfigDict


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    question: str


class Output(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    answer: str


def test_composition_surface_owns_verification_and_provider_inputs() -> None:
    signature = inspect.signature(production_inference_lifespan)
    assert list(signature.parameters) == ["app_env", "runtime_build_hash"]
    for forbidden in (
        "model",
        "model_client",
        "manifest",
        "verified_identity",
        "expected_hash",
        "trust_root",
    ):
        assert forbidden not in signature.parameters


@pytest.mark.asyncio
async def test_missing_release_configuration_fails_before_client_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in tuple(os.environ):
        if name.startswith("AI_GATEWAY_RELEASE_") or name == "AI_GATEWAY_PROVIDER_MANIFEST_PATH":
            monkeypatch.delenv(name, raising=False)
    with pytest.raises(InferenceError) as exc_info:
        async with production_inference_lifespan(
            app_env="production",
            runtime_build_hash="a" * 64,
        ):
            pytest.fail("composition must not yield")
    assert exc_info.value.code is InferenceErrorCode.INVALID_BINDING


@pytest.mark.asyncio
async def test_composition_rejects_duck_runtime_hash_without_user_code() -> None:
    class Evil:
        calls = 0

        def __getattribute__(self, _: str) -> object:
            Evil.calls += 1
            raise AssertionError

    with pytest.raises(TypeError):
        async with production_inference_lifespan(
            app_env="production",
            runtime_build_hash=Evil(),  # type: ignore[arg-type]
        ):
            pytest.fail("composition must not yield")
    assert Evil.calls == 0
