from __future__ import annotations

import inspect

import app.inference as inference
from app.contracts import StructuredInferenceInput, StructuredInferenceOutput


def test_public_surface_is_the_deep_port_only() -> None:
    assert inference.__all__ == ["TypedInferencePort", "InferenceError", "InferenceErrorCode"]
    assert hasattr(inference, "TypedInferencePort")
    assert not hasattr(inference, "ProviderBindingManifest")
    assert not hasattr(inference, "PydanticAIAdapter")
    assert not hasattr(inference, "create_provider")
    assert not hasattr(inference, "verified_token")
    signature = inspect.signature(inference.TypedInferencePort.infer)
    assert list(signature.parameters) == ["self", "request", "result_type"]
    assert signature.parameters["result_type"].kind is inspect.Parameter.KEYWORD_ONLY
    assert StructuredInferenceInput.model_fields.keys() == {"question"}
    assert StructuredInferenceOutput.model_fields.keys() == {"answer"}
