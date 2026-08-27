"""Supervisor-side issuance tool for the MVP production inference chain.

Builds the exact artifact set the runtime worker composition root consumes:
a read-only authority directory (root public key + trust policy + policy
signature), the ``CoreReleaseIdentity`` candidate, expected facts with the
issuer signature, the hash-bound ``ProviderBindingManifest``, and the four
deployment pins (``release-pins.json`` plus export lines on stdout).

Private keys are raw 32-byte Ed25519 seed files held outside this repository;
this tool never writes or logs key material.  The real AI gateway credential
is not needed for issuance: the manifest binds only the URL, model and
credential slot id, never the key.

Scope (ROADMAP 2026-08-20 范围调整): development/test/integration and MVP G2
runs.  The external-issuer ceremony and the Core ImplementationAcceptance
Record are WS-7 preconditions and deliberately out of scope here.
"""

from __future__ import annotations

import argparse
import os
import sys
from base64 import urlsafe_b64encode
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from typing import Any

from app.contracts.canonical import canonical_bytes, canonical_hash
from app.inference.ai_config import AIGatewayConfig
from app.inference.contracts import ProviderBindingManifest
from app.inference.schema_catalog import STRUCTURED_INPUT_REF, STRUCTURED_OUTPUT_REF
from app.releases.core import (
    AUTHORITY_POLICY_SCHEMA_VERSION,
    EXPECTED_FACTS_DOMAIN,
    FACTS_SIGNATURE_SCHEMA_VERSION,
    POLICY_SIGNATURE_DOMAIN,
    POLICY_SIGNATURE_SCHEMA_VERSION,
    AuthorityPolicy,
    ContentAddressedBinding,
    CoreReleaseExpectedFacts,
    CoreReleaseIdentity,
    ExpectedFactsDocumentBinding,
    IssuerSigningKey,
    ReleaseSignatureEnvelope,
    SigningKeyIdentity,
    TrustPolicy,
    canonical_authority_policy_bytes,
    canonical_core_release_bytes,
    canonical_expected_facts_bytes,
    canonical_signature_envelope_bytes,
    canonical_trust_policy_bytes,
    core_release_identity_hash,
)
from app.worker.inference import (
    _adapter_fingerprint,
    _distribution_fingerprint,
    _endpoint_config_fingerprint,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import SecretStr

_ROOT_SEED = "root-private.seed"
_ISSUER_SEED = "issuer-private.seed"


def _fail(message: str) -> int:
    print(f"issuance error: {message}", file=sys.stderr)
    return 2


def _b64(data: bytes) -> str:
    return urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _public_bytes(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes_raw()


def _load_seed(path: Path) -> Ed25519PrivateKey:
    data = path.read_bytes()
    if len(data) != 32:
        raise ValueError(f"{path} must contain exactly 32 raw Ed25519 seed bytes")
    return Ed25519PrivateKey.from_private_bytes(data)


def _key_identity(ref: str, version: str, key_id: str, private_key: Ed25519PrivateKey) -> SigningKeyIdentity:
    return SigningKeyIdentity(
        ref=ref,
        version=version,
        key_id=key_id,
        public_key_sha256=sha256(_public_bytes(private_key)).hexdigest(),
    )


def _signature_envelope_bytes(
    *,
    schema_version: str,
    signer: SigningKeyIdentity,
    private_key: Ed25519PrivateKey,
    domain: bytes,
    payload: bytes,
) -> bytes:
    return canonical_signature_envelope_bytes(
        ReleaseSignatureEnvelope(
            schema_version=schema_version,  # type: ignore[arg-type]
            signer_ref=signer.ref,
            signer_version=signer.version,
            key_id=signer.key_id,
            algorithm="Ed25519",
            signature=_b64(private_key.sign(domain + payload)),
        )
    )


def _write_read_only(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    path.chmod(0o444)


def _generate_keys(directory: Path) -> int:
    root_seed = directory / _ROOT_SEED
    issuer_seed = directory / _ISSUER_SEED
    if root_seed.exists() or issuer_seed.exists():
        return _fail(f"refusing to overwrite existing key material in {directory}")
    directory.mkdir(parents=True, exist_ok=True)
    for seed_path in (root_seed, issuer_seed):
        seed_path.write_bytes(Ed25519PrivateKey.generate().private_bytes_raw())
        seed_path.chmod(0o600)
    print(f"generated key material in {directory} (mode 0600, keep outside the repository)")
    print(f"root public key sha256:   {sha256(_load_seed(root_seed).public_key().public_bytes_raw()).hexdigest()}")
    print(f"issuer public key sha256: {sha256(_load_seed(issuer_seed).public_key().public_bytes_raw()).hexdigest()}")
    return 0


def _build_manifest(
    *,
    app_env: str,
    gateway_url: str,
    gateway_model: str,
    credential_slot_id: str,
    runtime_build_version: str,
    runtime_build_hash: str,
    structured_output_mode: str,
    temperature: float,
    max_output_tokens: int,
    max_schema_retries: int,
    max_provider_retries: int,
    max_tokens: int,
    max_cost_micros: int,
    deadline_ms: int,
    base_cost_micros: int,
    input_micros_per_million: int,
    output_micros_per_million: int,
) -> tuple[ProviderBindingManifest, bytes, AIGatewayConfig]:
    config = AIGatewayConfig(
        app_env=app_env,  # type: ignore[arg-type]
        url=gateway_url,
        api_key=SecretStr("not-required-for-issuance"),
        model=gateway_model,
        credential_slot_id=credential_slot_id,
    )
    manifest_data: dict[str, Any] = {
        "schema_version": "provider-binding-manifest.v1",
        "provider_type": "openai-compatible",
        "endpoint_url": config.url,
        "provider_profile": {
            "profile_ref": {"ref": "provider-profile/gateway", "version": "v1", "content_hash": "9" * 64},
            "supports_tools": False,
            "supports_json_schema_output": structured_output_mode == "native",
            "supports_json_object_output": True,
            "default_structured_output_mode": structured_output_mode,
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
        "runtime_build_version": runtime_build_version,
        "runtime_build_hash": runtime_build_hash,
        "model_policy": {
            "model_ref": config.model,
            "temperature": temperature,
            "max_output_tokens": max_output_tokens,
        },
        "retry_policy": {
            "max_schema_retries": max_schema_retries,
            "max_provider_retries": max_provider_retries,
        },
        "budget_policy": {
            "max_tokens": max_tokens,
            "max_cost_micros": max_cost_micros,
            "deadline_ms": deadline_ms,
        },
        "pricing_policy": {
            "currency": "CNY",
            "input_micros_per_million": input_micros_per_million,
            "output_micros_per_million": output_micros_per_million,
            "base_cost_micros": base_cost_micros,
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
    profile["profile_ref"]["content_hash"] = canonical_hash(
        {key: value for key, value in profile.items() if key != "profile_ref"}
    )
    for value_name, ref_name in (
        ("model_policy", "model_policy_ref"),
        ("retry_policy", "retry_policy_ref"),
        ("budget_policy", "budget_policy_ref"),
        ("pricing_policy", "pricing_policy_ref"),
    ):
        manifest_data[ref_name]["content_hash"] = canonical_hash(manifest_data[value_name])
    draft = ProviderBindingManifest.model_validate(manifest_data)
    manifest_data["endpoint_config_fingerprint"] = _endpoint_config_fingerprint(config, draft)
    manifest = ProviderBindingManifest.model_validate(manifest_data)
    return manifest, canonical_bytes(manifest), config


def _binding(ref: str, digest: str, version: str = "v1") -> dict[str, str]:
    return {"ref": ref, "version": version, "content_hash": digest}


def _build_candidate(
    *,
    manifest: ProviderBindingManifest,
    manifest_bytes: bytes,
    release_ref: str,
    source_commit: str,
) -> CoreReleaseIdentity:
    return CoreReleaseIdentity.model_validate(
        {
            "schema_version": "core-release-identity.v1",
            "release_ref": release_ref,
            "release_version": "v1",
            "source_commit": source_commit,
            "uv_lock": _binding("uv.lock@mvp-local", "b" * 64),
            "sbom": {
                "artifact": _binding("sbom.runtime@mvp-local", "c" * 64),
                "signature": _binding("sbom.runtime.signature@mvp-local", "d" * 64),
            },
            "runtime_build_manifest": _binding(
                "runtime.manifest@mvp-local",
                manifest.runtime_build_hash,
                manifest.runtime_build_version,
            ),
            "runtime_image_digest": "sha256:" + "f" * 64,
            "migration": _binding("migration.ws-head@mvp-local", "a" * 64),
            "contracts": {
                "contract": _binding("contracts.canonical@v1", "b" * 64),
                "abi": _binding("abi.skill-execution@v1", "c" * 64),
                "state_schema": _binding("state.execution@v1", "d" * 64),
            },
            "deployment_topology": _binding("topology.core@v1", "e" * 64),
            "deployment_config": _binding("config.cleanroom@v1", "a" * 64),
            "capability_profile": _binding("capability.core@v1", "b" * 64),
            "reference_target": {
                **_binding("reference-target.core-cleanroom@v1", "c" * 64),
                "target_kind": "cleanroom_reference",
            },
            "target_environment": {
                **_binding("environment.cleanroom@v1", "d" * 64),
                "environment_kind": "cleanroom",
            },
            "deployment_cell": _binding("deployment-cell.cleanroom@v1", "e" * 64),
            "execution": {
                "model": _binding(manifest.model_identifier, manifest.model_hash),
                "provider": {
                    "ref": "provider.selected@g2",
                    "version": "v1",
                    "content_hash": sha256(manifest_bytes).hexdigest(),
                },
                "model_policy": {
                    "ref": manifest.model_policy_ref.ref,
                    "version": manifest.model_policy_ref.version,
                    "content_hash": manifest.model_policy_ref.content_hash,
                },
                "adapter": {
                    "ref": manifest.adapter_version,
                    "version": "v1",
                    "content_hash": manifest.adapter_hash,
                },
            },
            "business_profile_ref": None,
            "business_profile_hash": None,
        }
    )


def _issue(args: argparse.Namespace) -> int:
    output = Path(args.output_dir)
    if output.exists() and any(output.iterdir()):
        return _fail(f"output directory {output} exists and is not empty")
    root_private = _load_seed(Path(args.root_private_key))
    issuer_private = _load_seed(Path(args.issuer_private_key))

    manifest, manifest_bytes, config = _build_manifest(
        app_env=args.app_env,
        gateway_url=args.gateway_url,
        gateway_model=args.gateway_model,
        credential_slot_id=args.credential_slot_id,
        runtime_build_version=args.runtime_build_version,
        runtime_build_hash=args.runtime_build_hash,
        structured_output_mode=args.structured_output_mode,
        temperature=args.temperature,
        max_output_tokens=args.max_output_tokens,
        max_schema_retries=args.max_schema_retries,
        max_provider_retries=args.max_provider_retries,
        max_tokens=args.max_tokens,
        max_cost_micros=args.max_cost_micros,
        deadline_ms=args.deadline_ms,
        base_cost_micros=args.base_cost_micros,
        input_micros_per_million=args.input_micros_per_million,
        output_micros_per_million=args.output_micros_per_million,
    )
    candidate = _build_candidate(
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        release_ref=args.release_ref,
        source_commit=args.source_commit,
    )

    root_identity = _key_identity("root.release@v1", "v1", "root-k1", root_private)
    issuer = IssuerSigningKey(
        ref="issuer.release@k1",
        version="v1",
        key_id="issuer-k1",
        public_key_sha256=sha256(_public_bytes(issuer_private)).hexdigest(),
        public_key=_b64(_public_bytes(issuer_private)),
        status="active",
    )
    policy = TrustPolicy(
        schema_version="core-release-trust-policy.v1",
        policy_ref=args.policy_ref,
        policy_version=args.policy_version,
        root_key=root_identity,
        issuers=(issuer,),
    )
    policy_bytes = canonical_trust_policy_bytes(policy)
    facts = CoreReleaseExpectedFacts(
        schema_version="core-release-expected-facts.v1",
        expected_identity=candidate,
        expected_identity_hash=core_release_identity_hash(candidate),
        expected_facts=ExpectedFactsDocumentBinding(ref="expected-facts.cleanroom@v1", version="v1"),
        trust_policy=ContentAddressedBinding.model_validate(
            _binding(policy.policy_ref, sha256(policy_bytes).hexdigest(), policy.policy_version)
        ),
        trusted_issuer=SigningKeyIdentity(
            ref=issuer.ref,
            version=issuer.version,
            key_id=issuer.key_id,
            public_key_sha256=issuer.public_key_sha256,
        ),
    )
    facts_bytes = canonical_expected_facts_bytes(facts)

    authority = output / "authority"
    authority.mkdir(parents=True)
    _write_read_only(authority / "root-public-key.bin", _public_bytes(root_private))
    _write_read_only(authority / "trust-policy.json", policy_bytes)
    _write_read_only(
        authority / "policy-signature.json",
        _signature_envelope_bytes(
            schema_version=POLICY_SIGNATURE_SCHEMA_VERSION,
            signer=root_identity,
            private_key=root_private,
            domain=POLICY_SIGNATURE_DOMAIN,
            payload=policy_bytes,
        ),
    )
    authority.chmod(0o555)
    _write_read_only(output / "core-release-identity.json", canonical_core_release_bytes(candidate))
    _write_read_only(output / "core-release-expected-facts.json", facts_bytes)
    _write_read_only(
        output / "core-release-expected-facts.signature.json",
        _signature_envelope_bytes(
            schema_version=FACTS_SIGNATURE_SCHEMA_VERSION,
            signer=SigningKeyIdentity(
                ref=issuer.ref,
                version=issuer.version,
                key_id=issuer.key_id,
                public_key_sha256=issuer.public_key_sha256,
            ),
            private_key=issuer_private,
            domain=EXPECTED_FACTS_DOMAIN,
            payload=facts_bytes,
        ),
    )
    _write_read_only(output / "provider-binding-manifest.json", manifest_bytes)

    pins = AuthorityPolicy(
        schema_version=AUTHORITY_POLICY_SCHEMA_VERSION,
        root_public_key_sha256=sha256(_public_bytes(root_private)).hexdigest(),
        policy_ref=policy.policy_ref,
        policy_version=policy.policy_version,
        policy_sha256=sha256(policy_bytes).hexdigest(),
    )
    pins_path = output / "release-pins.json"
    pins_path.write_bytes(canonical_authority_policy_bytes(pins))
    pins_path.chmod(0o444)

    print(f"issued release chain in {output}")
    for name, value in {
        "AI_GATEWAY_RELEASE_AUTHORITY_DIR": str(authority),
        "AI_GATEWAY_RELEASE_CANDIDATE_PATH": str(output / "core-release-identity.json"),
        "AI_GATEWAY_RELEASE_EXPECTED_FACTS_PATH": str(output / "core-release-expected-facts.json"),
        "AI_GATEWAY_RELEASE_SIGNATURE_PATH": str(output / "core-release-expected-facts.signature.json"),
        "AI_GATEWAY_PROVIDER_MANIFEST_PATH": str(output / "provider-binding-manifest.json"),
        "AI_GATEWAY_RELEASE_ROOT_PUBLIC_KEY_SHA256": pins.root_public_key_sha256,
        "AI_GATEWAY_RELEASE_POLICY_REF": pins.policy_ref,
        "AI_GATEWAY_RELEASE_POLICY_VERSION": pins.policy_version,
        "AI_GATEWAY_RELEASE_POLICY_SHA256": pins.policy_sha256,
    }.items():
        print(f"export {name}={value}")
    print(f"# gateway binding: url={config.url} model={config.model} slot={config.credential_slot_id}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--generate-keys", metavar="DIR", help="generate root/issuer Ed25519 seed files into DIR and exit"
    )
    parser.add_argument("--output-dir", help="release output directory (must be empty or absent)")
    parser.add_argument("--root-private-key", help="path to 32-byte root Ed25519 seed file")
    parser.add_argument("--issuer-private-key", help="path to 32-byte issuer Ed25519 seed file")
    parser.add_argument("--app-env", default="test", choices=["test", "development", "production"])
    parser.add_argument(
        "--gateway-url",
        default=os.environ.get("AI_GATEWAY_URL", ""),
        help="defaults to $AI_GATEWAY_URL",
    )
    parser.add_argument(
        "--gateway-model",
        default=os.environ.get("AI_GATEWAY_MODEL", ""),
        help="defaults to $AI_GATEWAY_MODEL",
    )
    parser.add_argument(
        "--credential-slot-id",
        default=os.environ.get("AI_GATEWAY_CREDENTIAL_SLOT_ID", ""),
        help="defaults to $AI_GATEWAY_CREDENTIAL_SLOT_ID",
    )
    parser.add_argument("--runtime-build-version", default="v1")
    parser.add_argument("--runtime-build-hash", required=False, default="")
    parser.add_argument("--release-ref", default="core.release@mvp-local")
    parser.add_argument("--source-commit", default="1" * 40)
    parser.add_argument("--policy-ref", default="policy.release@v1")
    parser.add_argument("--policy-version", default="v1")
    parser.add_argument("--structured-output-mode", default="prompted", choices=["prompted", "native"])
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-output-tokens", type=int, default=1024)
    parser.add_argument("--max-schema-retries", type=int, default=0)
    parser.add_argument("--max-provider-retries", type=int, default=0)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--max-cost-micros", type=int, default=1_000_000)
    parser.add_argument("--deadline-ms", type=int, default=120_000)
    parser.add_argument("--base-cost-micros", type=int, default=1)
    parser.add_argument("--input-micros-per-million", type=int, default=1)
    parser.add_argument("--output-micros-per-million", type=int, default=1)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.generate_keys:
        if args.output_dir or args.root_private_key or args.issuer_private_key:
            return _fail("--generate-keys cannot be combined with issuance options")
        return _generate_keys(Path(args.generate_keys))
    missing = [
        name
        for name, value in (
            ("--output-dir", args.output_dir),
            ("--root-private-key", args.root_private_key),
            ("--issuer-private-key", args.issuer_private_key),
            ("--gateway-url", args.gateway_url),
            ("--gateway-model", args.gateway_model),
            ("--credential-slot-id", args.credential_slot_id),
            ("--runtime-build-hash", args.runtime_build_hash),
        )
        if not value
    ]
    if missing:
        return _fail(f"missing required options: {', '.join(missing)}")
    if len(args.runtime_build_hash) != 64:
        return _fail("--runtime-build-hash must be a 64-character sha256 hex string")
    return _issue(args)


if __name__ == "__main__":
    raise SystemExit(main())
