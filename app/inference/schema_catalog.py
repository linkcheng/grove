"""Closed, build-owned schema catalog for production inference."""

from __future__ import annotations

from app.contracts.canonical import (
    StructuredInferenceInput,
    StructuredInferenceOutput,
    VersionedRef,
    canonical_hash,
)
from app.inference.contracts import ProviderBindingManifest
from app.inference.errors import InferenceError, InferenceErrorCode

STRUCTURED_INPUT_REF = VersionedRef(
    ref="grove.schema.structured-inference-input@v1",
    version="v1",
    content_hash=canonical_hash(StructuredInferenceInput.model_json_schema()),
)
STRUCTURED_OUTPUT_REF = VersionedRef(
    ref="grove.schema.structured-inference-output@v1",
    version="v1",
    content_hash=canonical_hash(StructuredInferenceOutput.model_json_schema()),
)


def resolve_manifest_schema_binding(
    manifest: ProviderBindingManifest,
) -> tuple[type[StructuredInferenceInput], type[StructuredInferenceOutput]]:
    """Resolve only schemas compiled into, and hashed with, the runtime adapter."""

    if type(manifest) is not ProviderBindingManifest:
        raise TypeError("manifest must be an exact ProviderBindingManifest")
    if manifest.input_schema_ref != STRUCTURED_INPUT_REF or manifest.output_schema_ref != STRUCTURED_OUTPUT_REF:
        raise InferenceError(InferenceErrorCode.INVALID_BINDING)
    return StructuredInferenceInput, StructuredInferenceOutput
