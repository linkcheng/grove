"""Runtime-worker composition root for the production inference capability."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from hashlib import sha256
from importlib.metadata import distribution, version
from pathlib import Path
from typing import Final, Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.contracts.canonical import (
    CanonicalInferenceRequest,
    CanonicalMessage,
    ContractMeta,
    StructuredInferenceInput,
    StructuredInferenceOutput,
    canonical_hash,
)
from app.inference.ai_config import AIGatewayConfig, load_ai_gateway_config
from app.inference.contracts import ProviderBindingManifest, load_provider_binding_manifest
from app.inference.errors import InferenceError, InferenceErrorCode
from app.inference.port import TypedInferencePort
from app.inference.pydantic_ai_adapter import PydanticAIInferencePort
from app.releases.core import CoreReleaseIdentity, VerifiedReleaseIdentity, core_release_identity_hash

_MAX_CANDIDATE_BYTES: Final = 1_000_000
_MAX_PROVIDER_MANIFEST_BYTES: Final = 1_000_000
_VERIFIER_TIMEOUT_SECONDS: Final = 15


class ProviderG2Evidence(BaseModel):
    """Non-sensitive result of the explicit provider smoke entry point."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    sentinel: Literal["G2_OK"]
    physical_sends: int = Field(ge=1, le=1000)
    schema_retries: int = Field(ge=0, le=1000)
    input_tokens: int = Field(ge=0, le=10_000_000)
    output_tokens: int = Field(ge=0, le=10_000_000)
    cost_micros: int = Field(ge=0, le=10_000_000_000)


class _ProductionInferenceFiles(BaseModel):
    """Deployment-owned paths and pins; never accepted from a node caller."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    authority_directory: Path
    candidate_path: Path
    expected_facts_path: Path
    issuer_signature_path: Path
    provider_manifest_path: Path
    root_public_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_ref: str = Field(min_length=1, max_length=256)
    policy_version: str = Field(min_length=1, max_length=64)
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


@asynccontextmanager
async def production_inference_lifespan(
    *,
    app_env: str,
    runtime_build_hash: str,
) -> AsyncIterator[TypedInferencePort]:
    """Verify signed release bytes and own the single production SDK client."""

    async with _production_inference_lifespan_impl(
        app_env=app_env,
        runtime_build_hash=runtime_build_hash,
        transport=None,
    ) as (port, _manifest):
        yield port


@asynccontextmanager
async def _production_inference_lifespan_impl(
    *,
    app_env: str,
    runtime_build_hash: str,
    transport: httpx.AsyncBaseTransport | None,
) -> AsyncIterator[tuple[PydanticAIInferencePort, ProviderBindingManifest]]:
    """Internal composition seam that also carries the verified manifest.

    ``transport`` is only used by the explicit local G2 test entry point.  The
    normal worker lifespan always passes ``None`` and creates its own transport
    and SDK client in the sealed adapter.
    """

    if type(runtime_build_hash) is not str or len(runtime_build_hash) != 64:
        raise TypeError("runtime_build_hash must be an exact sha256 string")
    files = _load_production_inference_files()
    descriptors = _open_release_descriptors(files)
    port: PydanticAIInferencePort | None = None
    try:
        verified = _run_cleanroom_verifier(files, descriptors)
        os.lseek(descriptors[1], 0, os.SEEK_SET)
        candidate_bytes = _read_preopened_regular_fd(descriptors[1], max_bytes=_MAX_CANDIDATE_BYTES)
        os.lseek(descriptors[4], 0, os.SEEK_SET)
        manifest_bytes = _read_preopened_regular_fd(
            descriptors[4],
            max_bytes=_MAX_PROVIDER_MANIFEST_BYTES,
        )
        try:
            candidate = CoreReleaseIdentity.model_validate_json(candidate_bytes)
        except ValueError:
            raise InferenceError(InferenceErrorCode.INVALID_BINDING) from None
        if (
            core_release_identity_hash(candidate) != verified.identity_hash
            or candidate.release_ref != verified.release_ref
            or candidate.release_version != verified.release_version
        ):
            raise InferenceError(InferenceErrorCode.INVALID_BINDING)
        try:
            manifest = load_provider_binding_manifest(
                manifest_bytes,
                expected_hash=candidate.execution.provider.content_hash,
            )
            if sha256(manifest_bytes).hexdigest() != candidate.execution.provider.content_hash:
                raise InferenceError(InferenceErrorCode.INVALID_BINDING)
            config = load_ai_gateway_config(app_env=app_env)
            _validate_runtime_binding(candidate, manifest, config, runtime_build_hash)
        except InferenceError:
            raise
        except Exception:
            raise InferenceError(InferenceErrorCode.INVALID_BINDING) from None
        port = PydanticAIInferencePort._compose(
            manifest=manifest,
            gateway_config=config,
            transport=transport,
        )
        yield port, manifest
    finally:
        if port is not None:
            await port.aclose()
        for descriptor in descriptors:
            os.close(descriptor)


async def run_provider_g2_smoke(
    *,
    app_env: str,
    runtime_build_hash: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ProviderG2Evidence:
    """Run the explicit Worker-owned structured-output provider smoke.

    This is deliberately separate from ``production_inference_lifespan``:
    ordinary worker startup only composes a port and never sends a provider
    request.  The entry point constructs the canonical request from the
    verified manifest and returns only bounded, non-sensitive evidence.
    """

    if type(app_env) is not str:
        raise TypeError("app_env must be an exact string")
    if transport is not None and app_env not in {"development", "test", "integration"}:
        raise InferenceError(InferenceErrorCode.INVALID_BINDING)

    async with _production_inference_lifespan_impl(
        app_env=app_env,
        runtime_build_hash=runtime_build_hash,
        transport=transport,
    ) as (port, manifest):
        request = _build_provider_g2_request(manifest)
        result = await port.infer(request, result_type=StructuredInferenceOutput)
        if result.result.answer != "G2_OK":
            raise InferenceError(InferenceErrorCode.INVALID_RESULT)
        return ProviderG2Evidence(
            sentinel="G2_OK",
            physical_sends=result.provider_attempts,
            schema_retries=result.schema_retries,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            cost_micros=result.usage.cost_micros,
        )


def _build_provider_g2_request(
    manifest: ProviderBindingManifest,
) -> CanonicalInferenceRequest[StructuredInferenceInput]:
    """Build the one fixed conformance request behind the Worker seam."""

    return CanonicalInferenceRequest[StructuredInferenceInput](
        meta=ContractMeta(
            contract_name="canonical.inference.request",
            contract_version="v1",
            message_id=uuid4(),
            tenant_id="g2",
            correlation_id="g2-provider",
        ),
        inference_request_id=uuid4(),
        run_id=uuid4(),
        node_id="g2-provider",
        node_attempt=0,
        input=StructuredInferenceInput(question="Return the exact sentinel required by the response schema."),
        instructions=(CanonicalMessage(role="user", content="Return G2_OK as the answer."),),
        model_policy=manifest.model_policy,
        result_schema_ref=manifest.output_schema_ref.ref,
        prompt_policy_ref=manifest.prompt_policy_ref.ref,
        model_policy_ref=manifest.model_policy_ref.ref,
        retry_policy=manifest.retry_policy,
        inference_retry_policy_ref=manifest.retry_policy_ref.ref,
        budget=manifest.budget_policy,
        budget_policy_ref=manifest.budget_policy_ref.ref,
    )


def _load_production_inference_files() -> _ProductionInferenceFiles:
    names = {
        "authority_directory": "AI_GATEWAY_RELEASE_AUTHORITY_DIR",
        "candidate_path": "AI_GATEWAY_RELEASE_CANDIDATE_PATH",
        "expected_facts_path": "AI_GATEWAY_RELEASE_EXPECTED_FACTS_PATH",
        "issuer_signature_path": "AI_GATEWAY_RELEASE_SIGNATURE_PATH",
        "provider_manifest_path": "AI_GATEWAY_PROVIDER_MANIFEST_PATH",
        "root_public_key_sha256": "AI_GATEWAY_RELEASE_ROOT_PUBLIC_KEY_SHA256",
        "policy_ref": "AI_GATEWAY_RELEASE_POLICY_REF",
        "policy_version": "AI_GATEWAY_RELEASE_POLICY_VERSION",
        "policy_sha256": "AI_GATEWAY_RELEASE_POLICY_SHA256",
    }
    values: dict[str, object] = {}
    for field_name, environment_name in names.items():
        value = os.environ.get(environment_name)
        if value is None or value == "":
            raise InferenceError(InferenceErrorCode.INVALID_BINDING)
        values[field_name] = Path(value) if field_name.endswith(("path", "directory")) else value
    try:
        return _ProductionInferenceFiles.model_validate(values)
    except ValueError:
        raise InferenceError(InferenceErrorCode.INVALID_BINDING) from None


def _open_release_descriptors(files: _ProductionInferenceFiles) -> tuple[int, int, int, int, int]:
    opened: list[int] = []
    try:
        opened.append(os.open(files.authority_directory, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY))
        for path in (
            files.candidate_path,
            files.expected_facts_path,
            files.issuer_signature_path,
            files.provider_manifest_path,
        ):
            opened.append(os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW))
        return opened[0], opened[1], opened[2], opened[3], opened[4]
    except OSError:
        for descriptor in opened:
            os.close(descriptor)
        raise InferenceError(InferenceErrorCode.INVALID_BINDING) from None


def _run_cleanroom_verifier(
    files: _ProductionInferenceFiles,
    descriptors: tuple[int, int, int, int, int],
) -> VerifiedReleaseIdentity:
    command = [
        sys.executable,
        "-I",
        "-m",
        "app.releases.cleanroom",
        "--authority-fd",
        str(descriptors[0]),
        "--candidate-fd",
        str(descriptors[1]),
        "--expected-facts-fd",
        str(descriptors[2]),
        "--issuer-signature-fd",
        str(descriptors[3]),
        "--root-public-key-sha256",
        files.root_public_key_sha256,
        "--policy-ref",
        files.policy_ref,
        "--policy-version",
        files.policy_version,
        "--policy-sha256",
        files.policy_sha256,
    ]
    try:
        result = subprocess.run(  # noqa: S603 - interpreter/module/options are fixed by this composition root.
            command,
            env={},
            pass_fds=descriptors[:4],
            capture_output=True,
            check=False,
            timeout=_VERIFIER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise InferenceError(InferenceErrorCode.INVALID_BINDING) from None
    if result.returncode != 0 or result.stderr or not result.stdout:
        raise InferenceError(InferenceErrorCode.INVALID_BINDING)
    try:
        return VerifiedReleaseIdentity.model_validate_json(result.stdout)
    except ValueError:
        raise InferenceError(InferenceErrorCode.INVALID_BINDING) from None


def _validate_runtime_binding(
    candidate: CoreReleaseIdentity,
    manifest: ProviderBindingManifest,
    config: AIGatewayConfig,
    runtime_build_hash: str,
) -> None:
    execution = candidate.execution
    if (
        execution.model.ref != manifest.model_identifier
        or execution.model.content_hash != manifest.model_hash
        or execution.model_policy.content_hash != canonical_hash(manifest.model_policy)
        or execution.adapter.ref != manifest.adapter_version
        or execution.adapter.content_hash != manifest.adapter_hash
        or candidate.runtime_build_manifest.version != manifest.runtime_build_version
        or candidate.runtime_build_manifest.content_hash != manifest.runtime_build_hash
        or candidate.runtime_build_manifest.content_hash != runtime_build_hash
        or version("openai") != manifest.sdk_version
        or _distribution_fingerprint("openai") != manifest.sdk_hash
        or version("pydantic-ai-slim") != manifest.pydantic_ai_version
        or _distribution_fingerprint("pydantic-ai-slim") != manifest.pydantic_ai_hash
        or _adapter_fingerprint() != manifest.adapter_hash
        or config.url != manifest.endpoint_url
        or _endpoint_config_fingerprint(config, manifest) != manifest.endpoint_config_fingerprint
    ):
        raise InferenceError(InferenceErrorCode.INVALID_BINDING)


def _endpoint_config_fingerprint(config: AIGatewayConfig, manifest: ProviderBindingManifest) -> str:
    return canonical_hash(
        {
            "credential_slot_id": config.credential_slot_id,
            "provider_profile": manifest.provider_profile.model_dump(mode="json"),
            "provider_type": manifest.provider_type,
            "url": config.url,
        }
    )


def _distribution_fingerprint(distribution_name: str) -> str:
    package = distribution(distribution_name)
    files = package.files
    if files is None:
        raise InferenceError(InferenceErrorCode.INVALID_BINDING)
    digest = sha256()
    selected = sorted(item for item in files if "__pycache__" not in item.parts and item.suffix not in {".pyc", ".pyo"})
    for item in selected:
        path = Path(str(package.locate_file(item)))
        if not path.is_file():
            raise InferenceError(InferenceErrorCode.INVALID_BINDING)
        data = path.read_bytes()
        encoded_name = item.as_posix().encode("utf-8")
        digest.update(len(encoded_name).to_bytes(8, "big"))
        digest.update(encoded_name)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _adapter_fingerprint() -> str:
    digest = sha256()
    app_root = Path(__file__).parents[1]
    adapter_root = app_root / "inference"
    paths = [
        *adapter_root.glob("*.py"),
        app_root / "contracts" / "canonical.py",
        app_root / "main.py",
        Path(__file__),
        Path(__file__).with_name("loop.py"),
    ]
    for path in sorted(paths):
        data = path.read_bytes()
        encoded_name = path.relative_to(app_root).as_posix().encode("utf-8")
        digest.update(len(encoded_name).to_bytes(8, "big"))
        digest.update(encoded_name)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _read_preopened_regular_fd(file_descriptor: object, *, max_bytes: int) -> bytes:
    if type(file_descriptor) is not int or file_descriptor < 0:
        raise TypeError("file descriptor must be a non-negative exact integer")
    try:
        before = os.fstat(file_descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_size > max_bytes:
            raise OSError
        if os.lseek(file_descriptor, 0, os.SEEK_CUR) != 0:
            raise OSError
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(file_descriptor, remaining)
            if not chunk:
                raise OSError
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(file_descriptor, 1):
            raise OSError
        after = os.fstat(file_descriptor)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
        ):
            raise OSError
        return b"".join(chunks)
    except OSError:
        raise InferenceError(InferenceErrorCode.INVALID_BINDING) from None
