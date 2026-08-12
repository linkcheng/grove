"""Security contract for the WS-5 cleanroom-owned release authority."""

from __future__ import annotations

import os
from base64 import urlsafe_b64encode
from hashlib import sha256
from inspect import signature
from pathlib import Path
from traceback import format_exception
from typing import Any, cast

import pytest
from app.contracts.canonical import canonical_bytes
from app.releases.core import (
    AUTHORITY_MOUNT_ENV,
    AUTHORITY_POLICY_SCHEMA_VERSION,
    EXPECTED_FACTS_DOMAIN,
    EXPECTED_POLICY_REF_ENV,
    EXPECTED_POLICY_SHA256_ENV,
    EXPECTED_POLICY_VERSION_ENV,
    FACTS_SIGNATURE_SCHEMA_VERSION,
    POLICY_SIGNATURE_DOMAIN,
    POLICY_SIGNATURE_SCHEMA_VERSION,
    ROOT_PUBLIC_KEY_SHA256_ENV,
    AuthorityPolicy,
    ContentAddressedBinding,
    CoreReleaseExpectedFacts,
    CoreReleaseIdentity,
    ExpectedFactsDocumentBinding,
    IssuerSigningKey,
    ReleaseIdentityError,
    ReleaseIdentityErrorCode,
    ReleaseSignatureEnvelope,
    SigningKeyIdentity,
    TrustPolicy,
    VerifiedReleaseAuthority,
    VerifiedReleaseIdentity,
    _load_verified_release_authority_from_bytes,
    canonical_authority_policy_bytes,
    canonical_core_release_bytes,
    canonical_expected_facts_bytes,
    canonical_signature_envelope_bytes,
    canonical_trust_policy_bytes,
    core_release_identity_hash,
    load_verified_release_authority,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
HASH_F = "f" * 64
COMMIT = "1" * 40
ROOT_PRIVATE = Ed25519PrivateKey.from_private_bytes(b"\x01" * 32)
K1_PRIVATE = Ed25519PrivateKey.from_private_bytes(b"\x02" * 32)
K2_PRIVATE = Ed25519PrivateKey.from_private_bytes(b"\x03" * 32)
ATTACKER_PRIVATE = Ed25519PrivateKey.from_private_bytes(b"\x04" * 32)


def _b64(data: bytes) -> str:
    return urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _public_key(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes_raw()


def _set_cleanroom_pins(
    monkeypatch: pytest.MonkeyPatch,
    *,
    mount: Path,
    pins: AuthorityPolicy,
) -> None:
    monkeypatch.setenv(AUTHORITY_MOUNT_ENV, str(mount))
    monkeypatch.setenv(ROOT_PUBLIC_KEY_SHA256_ENV, pins.root_public_key_sha256)
    monkeypatch.setenv(EXPECTED_POLICY_REF_ENV, pins.policy_ref)
    monkeypatch.setenv(EXPECTED_POLICY_VERSION_ENV, pins.policy_version)
    monkeypatch.setenv(EXPECTED_POLICY_SHA256_ENV, pins.policy_sha256)


def _binding(ref: str, digest: str = HASH_A, version: str = "v1") -> dict[str, str]:
    return {"ref": ref, "version": version, "content_hash": digest}


def _binding_model(ref: str, digest: str, version: str = "v1") -> ContentAddressedBinding:
    return ContentAddressedBinding.model_validate(_binding(ref, digest, version))


def _candidate_payload() -> dict[str, object]:
    return {
        "schema_version": "core-release-identity.v1",
        "release_ref": "core.release@2026.08.12",
        "release_version": "v1",
        "source_commit": COMMIT,
        "uv_lock": _binding("uv.lock@2026.08.12", HASH_B),
        "sbom": {
            "artifact": _binding("sbom.runtime@2026.08.12", HASH_C),
            "signature": _binding("sbom.runtime.signature@2026.08.12", HASH_D),
        },
        "runtime_build_manifest": _binding("runtime.manifest@2026.08.12", HASH_E),
        "runtime_image_digest": "sha256:" + "f" * 64,
        "migration": _binding("migration.ws5@2026.08.12", HASH_A),
        "contracts": {
            "contract": _binding("contracts.canonical@v1", HASH_B),
            "abi": _binding("abi.skill-execution@v1", HASH_C),
            "state_schema": _binding("state.execution@v1", HASH_D),
        },
        "deployment_topology": _binding("topology.core@v1", HASH_E),
        "deployment_config": _binding("config.cleanroom@v1", HASH_A),
        "capability_profile": _binding("capability.core@v1", HASH_B),
        "reference_target": {
            **_binding("reference-target.core-cleanroom@v1", HASH_C),
            "target_kind": "cleanroom_reference",
        },
        "target_environment": {
            **_binding("environment.cleanroom@v1", HASH_D),
            "environment_kind": "cleanroom",
        },
        "deployment_cell": _binding("deployment-cell.cleanroom@v1", HASH_E),
        "execution": {
            "model": _binding("model.structured-output@v1", HASH_A),
            "provider": _binding("provider.selected@v1", HASH_B),
            "model_policy": _binding("policy.model@v1", HASH_C),
            "adapter": _binding("adapter.pydantic-ai@v1", HASH_D),
        },
        "business_profile_ref": None,
        "business_profile_hash": None,
    }


def _candidate(ref: str = "core.release@2026.08.12") -> CoreReleaseIdentity:
    payload = _candidate_payload()
    payload["release_ref"] = ref
    return CoreReleaseIdentity.model_validate(payload)


def _key_identity(
    ref: str,
    version: str,
    key_id: str,
    private_key: Ed25519PrivateKey,
) -> SigningKeyIdentity:
    public_key = _public_key(private_key)
    return SigningKeyIdentity(
        ref=ref,
        version=version,
        key_id=key_id,
        public_key_sha256=sha256(public_key).hexdigest(),
    )


def _issuer(
    ref: str,
    version: str,
    key_id: str,
    private_key: Ed25519PrivateKey,
    status: str = "active",
) -> IssuerSigningKey:
    public_key = _public_key(private_key)
    return IssuerSigningKey(
        ref=ref,
        version=version,
        key_id=key_id,
        public_key_sha256=sha256(public_key).hexdigest(),
        public_key=_b64(public_key),
        status=cast(Any, status),
    )


def _policy(
    *,
    version: str = "v1",
    issuers: tuple[IssuerSigningKey, ...] | None = None,
    root_private: Ed25519PrivateKey = ROOT_PRIVATE,
    ref: str = "policy.release@v1",
) -> TrustPolicy:
    root = _key_identity("root.release@v1", "v1", "root-k1", root_private)
    return TrustPolicy(
        schema_version="core-release-trust-policy.v1",
        policy_ref=ref,
        policy_version=version,
        root_key=root,
        issuers=(issuers if issuers is not None else (_issuer("issuer.release@k1", "v1", "issuer-k1", K1_PRIVATE),)),
    )


def _signature_bytes(
    *,
    schema_version: str,
    signer: SigningKeyIdentity,
    private_key: Ed25519PrivateKey,
    domain: bytes,
    payload: bytes,
) -> bytes:
    envelope = ReleaseSignatureEnvelope(
        schema_version=cast(Any, schema_version),
        signer_ref=signer.ref,
        signer_version=signer.version,
        key_id=signer.key_id,
        algorithm="Ed25519",
        signature=_b64(private_key.sign(domain + payload)),
    )
    return canonical_signature_envelope_bytes(envelope)


def _authority(
    policy: TrustPolicy | None = None,
    *,
    root_private: Ed25519PrivateKey = ROOT_PRIVATE,
) -> VerifiedReleaseAuthority:
    policy = policy or _policy(root_private=root_private)
    policy_bytes = canonical_trust_policy_bytes(policy)
    authority_policy = AuthorityPolicy(
        schema_version=AUTHORITY_POLICY_SCHEMA_VERSION,
        root_public_key_sha256=sha256(_public_key(ROOT_PRIVATE)).hexdigest(),
        policy_ref=policy.policy_ref,
        policy_version=policy.policy_version,
        policy_sha256=sha256(policy_bytes).hexdigest(),
    )
    policy_signature = _signature_bytes(
        schema_version=POLICY_SIGNATURE_SCHEMA_VERSION,
        signer=policy.root_key,
        private_key=root_private,
        domain=POLICY_SIGNATURE_DOMAIN,
        payload=policy_bytes,
    )
    return _load_verified_release_authority_from_bytes(
        authority_policy_bytes=canonical_authority_policy_bytes(authority_policy),
        root_public_key_bytes=_public_key(root_private),
        trust_policy_bytes=policy_bytes,
        policy_signature_bytes=policy_signature,
    )


def _facts(
    candidate: CoreReleaseIdentity,
    policy: TrustPolicy,
    issuer: IssuerSigningKey | None = None,
) -> CoreReleaseExpectedFacts:
    issuer = issuer or policy.issuers[0]
    policy_bytes = canonical_trust_policy_bytes(policy)
    return CoreReleaseExpectedFacts(
        schema_version="core-release-expected-facts.v1",
        expected_identity=candidate,
        expected_identity_hash=core_release_identity_hash(candidate),
        expected_facts=ExpectedFactsDocumentBinding(
            ref="expected-facts.cleanroom@v1",
            version="v1",
        ),
        trust_policy=_binding_model(policy.policy_ref, sha256(policy_bytes).hexdigest(), policy.policy_version),
        trusted_issuer=SigningKeyIdentity(
            ref=issuer.ref,
            version=issuer.version,
            key_id=issuer.key_id,
            public_key_sha256=issuer.public_key_sha256,
        ),
    )


def _verify(
    authority: VerifiedReleaseAuthority,
    candidate: CoreReleaseIdentity,
    facts: CoreReleaseExpectedFacts,
    *,
    private_key: Ed25519PrivateKey = K1_PRIVATE,
    domain: bytes = EXPECTED_FACTS_DOMAIN,
    signature_signer: SigningKeyIdentity | None = None,
) -> VerifiedReleaseIdentity:
    facts_bytes = canonical_expected_facts_bytes(facts)
    signer = signature_signer or facts.trusted_issuer
    signature_bytes = _signature_bytes(
        schema_version=FACTS_SIGNATURE_SCHEMA_VERSION,
        signer=signer,
        private_key=private_key,
        domain=domain,
        payload=facts_bytes,
    )
    return authority.verify_core_release_identity(candidate, facts_bytes, signature_bytes)


def _expect_code(code: ReleaseIdentityErrorCode, fn: Any) -> None:
    with pytest.raises(ReleaseIdentityError) as exc_info:
        fn()
    assert exc_info.value.code == code
    assert exc_info.value.__cause__ is None


def _replace_path(model: Any, path: tuple[str, ...], value: object) -> Any:
    if len(path) == 1:
        return model.model_copy(update={path[0]: value})
    child = getattr(model, path[0])
    return model.model_copy(update={path[0]: _replace_path(child, path[1:], value)})


def test_cleanroom_authority_accepts_exact_signed_expected_facts() -> None:
    policy = _policy()
    authority = _authority(policy)
    candidate = _candidate()

    result = _verify(authority, candidate, _facts(candidate, policy))

    assert result.identity_hash == core_release_identity_hash(candidate)
    assert result.trust_policy.content_hash == sha256(canonical_trust_policy_bytes(policy)).hexdigest()
    assert result.trusted_issuer.ref == "issuer.release@k1"
    assert "PASS" not in result.model_dump_json()


def test_public_interfaces_have_no_caller_supplied_policy_root_or_hash_parameters() -> None:
    assert tuple(signature(load_verified_release_authority).parameters) == ()
    assert tuple(signature(VerifiedReleaseAuthority.verify_core_release_identity).parameters) == (
        "self",
        "candidate",
        "expected_facts_bytes",
        "issuer_signature_bytes",
    )


def test_old_caller_constructed_anchor_and_pure_verifier_are_not_exported() -> None:
    import app.releases as releases

    assert not hasattr(releases, "ExpectedFactsTrustAnchor")
    assert not hasattr(releases, "verify_core_release_identity")
    assert "_load_verified_release_authority_from_bytes" not in releases.__all__


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", "core-release-identity.v2"),
        ("source_commit", "not-a-commit"),
        ("runtime_image_digest", "runtime:latest"),
        ("release_ref", "release.latest"),
        ("release_ref", 123),
    ],
)
def test_candidate_missing_extra_invalid_and_coercible_values_fail_closed(field: str, value: object) -> None:
    invalid = _candidate_payload()
    invalid[field] = value
    with pytest.raises(ValidationError):
        CoreReleaseIdentity.model_validate(invalid)

    missing = _candidate_payload()
    del missing[field]
    with pytest.raises(ValidationError):
        CoreReleaseIdentity.model_validate(missing)

    extra = _candidate_payload()
    extra["candidate_supplied_trust_root"] = HASH_A
    with pytest.raises(ValidationError):
        CoreReleaseIdentity.model_validate(extra)


@pytest.mark.parametrize("field", ["business_profile_ref", "business_profile_hash"])
def test_core_business_profile_fields_are_required_explicit_nulls(field: str) -> None:
    missing = _candidate_payload()
    del missing[field]
    with pytest.raises(ValidationError):
        CoreReleaseIdentity.model_validate(missing)

    present = _candidate_payload()
    present[field] = "profile.attacker@v1"
    with pytest.raises(ValidationError):
        CoreReleaseIdentity.model_validate(present)


@pytest.mark.parametrize(
    "path",
    [
        ("source_commit",),
        ("uv_lock", "content_hash"),
        ("sbom", "artifact", "content_hash"),
        ("sbom", "signature", "content_hash"),
        ("runtime_build_manifest", "content_hash"),
        ("runtime_image_digest",),
        ("migration", "content_hash"),
        ("contracts", "contract", "content_hash"),
        ("contracts", "abi", "content_hash"),
        ("contracts", "state_schema", "content_hash"),
        ("deployment_topology", "content_hash"),
        ("deployment_config", "content_hash"),
        ("capability_profile", "content_hash"),
        ("reference_target", "content_hash"),
        ("target_environment", "content_hash"),
        ("deployment_cell", "content_hash"),
        ("execution", "model", "content_hash"),
        ("execution", "provider", "content_hash"),
        ("execution", "model_policy", "content_hash"),
        ("execution", "adapter", "content_hash"),
    ],
)
def test_each_candidate_binding_must_match_signed_expected_facts(path: tuple[str, ...]) -> None:
    candidate = _candidate()
    policy = _policy()
    facts = _facts(candidate, policy)
    if path == ("source_commit",):
        replacement = "2" * 40
    elif path == ("runtime_image_digest",):
        replacement = "sha256:" + "1" * 64
    else:
        replacement = HASH_F
    tampered = _replace_path(candidate, path, replacement)

    _expect_code(
        ReleaseIdentityErrorCode.EXTERNAL_FACTS_MISMATCH,
        lambda: _verify(_authority(policy), tampered, facts),
    )


def test_invalid_model_copy_is_revalidated_without_leaking_input() -> None:
    marker = "provider-credential-marker-that-must-not-escape"
    candidate = _candidate().model_copy(update={"runtime_image_digest": marker})
    policy = _policy()
    facts = _facts(_candidate(), policy)

    with pytest.raises(ReleaseIdentityError) as exc_info:
        _verify(_authority(policy), candidate, facts)

    rendered = "".join(format_exception(exc_info.value))
    assert exc_info.value.code == ReleaseIdentityErrorCode.INVALID_CANDIDATE
    assert marker not in rendered
    assert exc_info.value.__context__ is None


def test_sealed_authority_rejects_direct_construction_and_tampered_internals() -> None:
    policy = _policy()
    with pytest.raises(TypeError):
        VerifiedReleaseAuthority(_seal=object())

    authority = _authority(policy)
    with pytest.raises(AttributeError):
        object.__setattr__(authority, "_policy_bytes", b"attacker")

    unregistered = object.__new__(VerifiedReleaseAuthority)
    _expect_code(
        ReleaseIdentityErrorCode.POLICY_IDENTITY_MISMATCH,
        lambda: _verify(unregistered, _candidate(), _facts(_candidate(), policy)),
    )


def test_verifier_rejects_nested_model_subclasses_at_the_exact_type_boundary() -> None:
    class BindingSubclass(ContentAddressedBinding):
        pass

    candidate = _candidate()
    injected = BindingSubclass.model_validate(candidate.uv_lock.model_dump())
    tampered = candidate.model_copy(update={"uv_lock": injected})
    policy = _policy()

    _expect_code(
        ReleaseIdentityErrorCode.INVALID_CANDIDATE,
        lambda: _verify(_authority(policy), tampered, _facts(candidate, policy)),
    )


def test_public_loader_requires_both_cleanroom_owned_locations(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in (
        AUTHORITY_MOUNT_ENV,
        ROOT_PUBLIC_KEY_SHA256_ENV,
        EXPECTED_POLICY_REF_ENV,
        EXPECTED_POLICY_VERSION_ENV,
        EXPECTED_POLICY_SHA256_ENV,
    ):
        monkeypatch.delenv(variable, raising=False)
    _expect_code(ReleaseIdentityErrorCode.EXTERNAL_FACTS_MISSING, load_verified_release_authority)


@pytest.mark.parametrize(
    "bad_facts",
    [
        b"",
        b"x" * (256 * 1024 + 1),
        b"[" * 34 + b"]" * 34,
        b'{"schema_version":"core-release-expected-facts.v1","schema_version":"attacker"}\n',
        b'{"x":NaN}\n',
        b"\xff\xfe",
    ],
)
def test_verifier_rejects_malformed_duplicate_and_bounded_expected_facts(bad_facts: bytes) -> None:
    policy = _policy()
    authority = _authority(policy)
    signature_bytes = _signature_bytes(
        schema_version=FACTS_SIGNATURE_SCHEMA_VERSION,
        signer=_facts(_candidate(), policy).trusted_issuer,
        private_key=K1_PRIVATE,
        domain=EXPECTED_FACTS_DOMAIN,
        payload=bad_facts,
    )
    _expect_code(
        ReleaseIdentityErrorCode.INVALID_EXPECTED_FACTS,
        lambda: authority.verify_core_release_identity(_candidate(), bad_facts, signature_bytes),
    )


@pytest.mark.parametrize("bad_signature", [b"", b"{}\n", b"x" * (8 * 1024 + 1), b"\xff"])
def test_verifier_maps_malformed_signature_envelopes_to_stable_failure(bad_signature: bytes) -> None:
    policy = _policy()
    facts = _facts(_candidate(), policy)
    _expect_code(
        ReleaseIdentityErrorCode.FACTS_SIGNATURE_INVALID,
        lambda: _authority(policy).verify_core_release_identity(
            _candidate(),
            canonical_expected_facts_bytes(facts),
            bad_signature,
        ),
    )


@pytest.mark.parametrize(
    "mutation",
    [
        {"root_public_key_sha256": "0" * 64},
        {"policy_version": "latest"},
        {"policy_ref": "policy.release@v1/.."},
    ],
)
def test_authority_policy_rejects_invalid_pins(mutation: dict[str, str]) -> None:
    payload = {
        "schema_version": AUTHORITY_POLICY_SCHEMA_VERSION,
        "root_public_key_sha256": HASH_A,
        "policy_ref": "policy.release@v1",
        "policy_version": "v1",
        "policy_sha256": HASH_B,
        **mutation,
    }
    with pytest.raises(ValidationError):
        AuthorityPolicy.model_validate(payload)


@pytest.mark.parametrize(
    "issuers",
    [
        (),
        (
            _issuer("issuer.release@k2", "v1", "issuer-k2", K2_PRIVATE),
            _issuer("issuer.release@k1", "v1", "issuer-k1", K1_PRIVATE),
        ),
        (
            _issuer("issuer.release@k1", "v1", "issuer-k1", K1_PRIVATE),
            _issuer("issuer.release@k1", "v1", "issuer-k1", K1_PRIVATE),
        ),
    ],
)
def test_policy_issuer_registry_is_nonempty_sorted_and_unique(issuers: tuple[IssuerSigningKey, ...]) -> None:
    with pytest.raises(ValidationError):
        _policy(issuers=issuers)


def test_policy_rejects_mismatched_issuer_public_key_hash() -> None:
    payload = _issuer("issuer.release@k1", "v1", "issuer-k1", K1_PRIVATE).model_dump()
    payload["public_key_sha256"] = HASH_A
    with pytest.raises(ValidationError):
        IssuerSigningKey.model_validate(payload)


def test_policy_signature_wrong_schema_and_signer_identity_are_rejected() -> None:
    policy = _policy()
    policy_bytes = canonical_trust_policy_bytes(policy)
    pins = AuthorityPolicy(
        schema_version=AUTHORITY_POLICY_SCHEMA_VERSION,
        root_public_key_sha256=sha256(_public_key(ROOT_PRIVATE)).hexdigest(),
        policy_ref=policy.policy_ref,
        policy_version=policy.policy_version,
        policy_sha256=sha256(policy_bytes).hexdigest(),
    )
    facts_schema = _signature_bytes(
        schema_version=FACTS_SIGNATURE_SCHEMA_VERSION,
        signer=policy.root_key,
        private_key=ROOT_PRIVATE,
        domain=POLICY_SIGNATURE_DOMAIN,
        payload=policy_bytes,
    )
    _expect_code(
        ReleaseIdentityErrorCode.POLICY_SIGNATURE_INVALID,
        lambda: _load_verified_release_authority_from_bytes(
            authority_policy_bytes=canonical_authority_policy_bytes(pins),
            root_public_key_bytes=_public_key(ROOT_PRIVATE),
            trust_policy_bytes=policy_bytes,
            policy_signature_bytes=facts_schema,
        ),
    )
    wrong_signer = policy.root_key.model_copy(update={"key_id": "root-k2"})
    wrong_identity = _signature_bytes(
        schema_version=POLICY_SIGNATURE_SCHEMA_VERSION,
        signer=wrong_signer,
        private_key=ROOT_PRIVATE,
        domain=POLICY_SIGNATURE_DOMAIN,
        payload=policy_bytes,
    )
    _expect_code(
        ReleaseIdentityErrorCode.POLICY_SIGNATURE_INVALID,
        lambda: _load_verified_release_authority_from_bytes(
            authority_policy_bytes=canonical_authority_policy_bytes(pins),
            root_public_key_bytes=_public_key(ROOT_PRIVATE),
            trust_policy_bytes=policy_bytes,
            policy_signature_bytes=wrong_identity,
        ),
    )


def test_authority_loader_rejects_malformed_policy_and_root_key_length() -> None:
    policy = _policy()
    policy_bytes = canonical_trust_policy_bytes(policy)
    pins = AuthorityPolicy(
        schema_version=AUTHORITY_POLICY_SCHEMA_VERSION,
        root_public_key_sha256=sha256(_public_key(ROOT_PRIVATE)).hexdigest(),
        policy_ref=policy.policy_ref,
        policy_version=policy.policy_version,
        policy_sha256=sha256(policy_bytes).hexdigest(),
    )
    signature_bytes = _signature_bytes(
        schema_version=POLICY_SIGNATURE_SCHEMA_VERSION,
        signer=policy.root_key,
        private_key=ROOT_PRIVATE,
        domain=POLICY_SIGNATURE_DOMAIN,
        payload=policy_bytes,
    )
    _expect_code(
        ReleaseIdentityErrorCode.ROOT_PIN_MISMATCH,
        lambda: _load_verified_release_authority_from_bytes(
            authority_policy_bytes=canonical_authority_policy_bytes(pins),
            root_public_key_bytes=b"short",
            trust_policy_bytes=policy_bytes,
            policy_signature_bytes=signature_bytes,
        ),
    )
    _expect_code(
        ReleaseIdentityErrorCode.POLICY_IDENTITY_MISMATCH,
        lambda: _load_verified_release_authority_from_bytes(
            authority_policy_bytes=canonical_authority_policy_bytes(pins),
            root_public_key_bytes=_public_key(ROOT_PRIVATE),
            trust_policy_bytes=b"{}\n",
            policy_signature_bytes=signature_bytes,
        ),
    )


def test_public_loader_reads_only_protected_config_and_fixed_mount_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy()
    policy_bytes = canonical_trust_policy_bytes(policy)
    pins = AuthorityPolicy(
        schema_version=AUTHORITY_POLICY_SCHEMA_VERSION,
        root_public_key_sha256=sha256(_public_key(ROOT_PRIVATE)).hexdigest(),
        policy_ref=policy.policy_ref,
        policy_version=policy.policy_version,
        policy_sha256=sha256(policy_bytes).hexdigest(),
    )
    mount = tmp_path / "authority"
    mount.mkdir()
    files = {
        mount / "root-public-key.bin": _public_key(ROOT_PRIVATE),
        mount / "trust-policy.json": policy_bytes,
        mount / "policy-signature.json": _signature_bytes(
            schema_version=POLICY_SIGNATURE_SCHEMA_VERSION,
            signer=policy.root_key,
            private_key=ROOT_PRIVATE,
            domain=POLICY_SIGNATURE_DOMAIN,
            payload=policy_bytes,
        ),
    }
    for path, data in files.items():
        path.write_bytes(data)
        path.chmod(0o444)
    mount.chmod(0o555)
    _set_cleanroom_pins(monkeypatch, mount=mount, pins=pins)

    authority = load_verified_release_authority()
    candidate = _candidate()
    result = _verify(authority, candidate, _facts(candidate, policy))

    assert result.identity_hash == core_release_identity_hash(candidate)


def test_public_loader_fails_closed_when_authority_mount_is_writable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy()
    policy_bytes = canonical_trust_policy_bytes(policy)
    pins = AuthorityPolicy(
        schema_version=AUTHORITY_POLICY_SCHEMA_VERSION,
        root_public_key_sha256=sha256(_public_key(ROOT_PRIVATE)).hexdigest(),
        policy_ref=policy.policy_ref,
        policy_version=policy.policy_version,
        policy_sha256=sha256(policy_bytes).hexdigest(),
    )
    mount = tmp_path / "authority"
    mount.mkdir()
    _set_cleanroom_pins(monkeypatch, mount=mount, pins=pins)

    _expect_code(ReleaseIdentityErrorCode.EXTERNAL_FACTS_MISSING, load_verified_release_authority)


def test_public_loader_rejects_oversized_mount_file_before_reading_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy()
    policy_bytes = canonical_trust_policy_bytes(policy)
    pins = AuthorityPolicy(
        schema_version=AUTHORITY_POLICY_SCHEMA_VERSION,
        root_public_key_sha256=sha256(_public_key(ROOT_PRIVATE)).hexdigest(),
        policy_ref=policy.policy_ref,
        policy_version=policy.policy_version,
        policy_sha256=sha256(policy_bytes).hexdigest(),
    )
    mount = tmp_path / "authority"
    mount.mkdir()
    files = {
        mount / "root-public-key.bin": _public_key(ROOT_PRIVATE),
        mount / "trust-policy.json": b"x" * (64 * 1024 + 1),
        mount / "policy-signature.json": b"{}\n",
    }
    for path, data in files.items():
        path.write_bytes(data)
        path.chmod(0o444)
    mount.chmod(0o555)
    _set_cleanroom_pins(monkeypatch, mount=mount, pins=pins)
    read_calls = 0
    original_read = os.read

    def counted_read(file_descriptor: int, byte_count: int) -> bytes:
        nonlocal read_calls
        read_calls += 1
        return original_read(file_descriptor, byte_count)

    monkeypatch.setattr(os, "read", counted_read)

    _expect_code(ReleaseIdentityErrorCode.EXTERNAL_FACTS_MISSING, load_verified_release_authority)
    assert read_calls == 2


def test_public_loader_reads_complete_file_when_os_read_returns_short_chunks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy()
    policy_bytes = canonical_trust_policy_bytes(policy)
    pins = AuthorityPolicy(
        schema_version=AUTHORITY_POLICY_SCHEMA_VERSION,
        root_public_key_sha256=sha256(_public_key(ROOT_PRIVATE)).hexdigest(),
        policy_ref=policy.policy_ref,
        policy_version=policy.policy_version,
        policy_sha256=sha256(policy_bytes).hexdigest(),
    )
    mount = tmp_path / "authority"
    mount.mkdir()
    files = {
        mount / "root-public-key.bin": _public_key(ROOT_PRIVATE),
        mount / "trust-policy.json": policy_bytes,
        mount / "policy-signature.json": _signature_bytes(
            schema_version=POLICY_SIGNATURE_SCHEMA_VERSION,
            signer=policy.root_key,
            private_key=ROOT_PRIVATE,
            domain=POLICY_SIGNATURE_DOMAIN,
            payload=policy_bytes,
        ),
    }
    for path, data in files.items():
        path.write_bytes(data)
        path.chmod(0o444)
    mount.chmod(0o555)
    _set_cleanroom_pins(monkeypatch, mount=mount, pins=pins)
    original_read = os.read

    def short_read(file_descriptor: int, byte_count: int) -> bytes:
        return original_read(file_descriptor, min(byte_count, 7))

    monkeypatch.setattr(os, "read", short_read)

    assert isinstance(load_verified_release_authority(), VerifiedReleaseAuthority)


def test_public_loader_rejects_trailing_bytes_even_when_first_read_is_a_valid_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy()
    policy_bytes = canonical_trust_policy_bytes(policy)
    pins = AuthorityPolicy(
        schema_version=AUTHORITY_POLICY_SCHEMA_VERSION,
        root_public_key_sha256=sha256(_public_key(ROOT_PRIVATE)).hexdigest(),
        policy_ref=policy.policy_ref,
        policy_version=policy.policy_version,
        policy_sha256=sha256(policy_bytes).hexdigest(),
    )
    mount = tmp_path / "authority"
    mount.mkdir()
    files = {
        mount / "root-public-key.bin": _public_key(ROOT_PRIVATE),
        mount / "trust-policy.json": policy_bytes + b"trailing-tamper",
        mount / "policy-signature.json": b"{}\n",
    }
    for path, data in files.items():
        path.write_bytes(data)
        path.chmod(0o444)
    mount.chmod(0o555)
    _set_cleanroom_pins(monkeypatch, mount=mount, pins=pins)
    original_read = os.read

    def prefix_first_read(file_descriptor: int, byte_count: int) -> bytes:
        return original_read(file_descriptor, min(byte_count, len(policy_bytes)))

    monkeypatch.setattr(os, "read", prefix_first_read)

    _expect_code(ReleaseIdentityErrorCode.POLICY_IDENTITY_MISMATCH, load_verified_release_authority)


def test_whole_graph_reanchor_is_rejected_while_cleanroom_pins_stay_fixed() -> None:
    authority = _authority()
    attacker_candidate = _candidate("core.release.attacker@v1")
    attacker_policy = _policy(
        root_private=ATTACKER_PRIVATE,
        ref="policy.attacker@v1",
        issuers=(_issuer("issuer.attacker@v1", "v1", "attacker-k1", ATTACKER_PRIVATE),),
    )
    attacker_facts = _facts(attacker_candidate, attacker_policy, attacker_policy.issuers[0])

    _expect_code(
        ReleaseIdentityErrorCode.POLICY_IDENTITY_MISMATCH,
        lambda: _verify(
            authority,
            attacker_candidate,
            attacker_facts,
            private_key=ATTACKER_PRIVATE,
        ),
    )


def test_root_pin_policy_signature_and_policy_identity_fail_with_stable_codes() -> None:
    policy = _policy()
    policy_bytes = canonical_trust_policy_bytes(policy)
    policy_signature = _signature_bytes(
        schema_version=POLICY_SIGNATURE_SCHEMA_VERSION,
        signer=policy.root_key,
        private_key=ROOT_PRIVATE,
        domain=POLICY_SIGNATURE_DOMAIN,
        payload=policy_bytes,
    )
    pins = AuthorityPolicy(
        schema_version=AUTHORITY_POLICY_SCHEMA_VERSION,
        root_public_key_sha256=sha256(_public_key(ROOT_PRIVATE)).hexdigest(),
        policy_ref=policy.policy_ref,
        policy_version=policy.policy_version,
        policy_sha256=sha256(policy_bytes).hexdigest(),
    )

    _expect_code(
        ReleaseIdentityErrorCode.ROOT_PIN_MISMATCH,
        lambda: _load_verified_release_authority_from_bytes(
            authority_policy_bytes=canonical_authority_policy_bytes(pins),
            root_public_key_bytes=_public_key(ATTACKER_PRIVATE),
            trust_policy_bytes=policy_bytes,
            policy_signature_bytes=policy_signature,
        ),
    )

    bad_signature = _signature_bytes(
        schema_version=POLICY_SIGNATURE_SCHEMA_VERSION,
        signer=policy.root_key,
        private_key=ATTACKER_PRIVATE,
        domain=POLICY_SIGNATURE_DOMAIN,
        payload=policy_bytes,
    )
    _expect_code(
        ReleaseIdentityErrorCode.POLICY_SIGNATURE_INVALID,
        lambda: _load_verified_release_authority_from_bytes(
            authority_policy_bytes=canonical_authority_policy_bytes(pins),
            root_public_key_bytes=_public_key(ROOT_PRIVATE),
            trust_policy_bytes=policy_bytes,
            policy_signature_bytes=bad_signature,
        ),
    )

    wrong_pins = pins.model_copy(update={"policy_ref": "policy.other@v1"})
    _expect_code(
        ReleaseIdentityErrorCode.POLICY_IDENTITY_MISMATCH,
        lambda: _load_verified_release_authority_from_bytes(
            authority_policy_bytes=canonical_authority_policy_bytes(wrong_pins),
            root_public_key_bytes=_public_key(ROOT_PRIVATE),
            trust_policy_bytes=policy_bytes,
            policy_signature_bytes=policy_signature,
        ),
    )


def test_policy_signature_cannot_cross_signature_domain() -> None:
    policy = _policy()
    policy_bytes = canonical_trust_policy_bytes(policy)
    pins = AuthorityPolicy(
        schema_version=AUTHORITY_POLICY_SCHEMA_VERSION,
        root_public_key_sha256=sha256(_public_key(ROOT_PRIVATE)).hexdigest(),
        policy_ref=policy.policy_ref,
        policy_version=policy.policy_version,
        policy_sha256=sha256(policy_bytes).hexdigest(),
    )
    cross_domain = _signature_bytes(
        schema_version=POLICY_SIGNATURE_SCHEMA_VERSION,
        signer=policy.root_key,
        private_key=ROOT_PRIVATE,
        domain=EXPECTED_FACTS_DOMAIN,
        payload=policy_bytes,
    )

    _expect_code(
        ReleaseIdentityErrorCode.POLICY_SIGNATURE_INVALID,
        lambda: _load_verified_release_authority_from_bytes(
            authority_policy_bytes=canonical_authority_policy_bytes(pins),
            root_public_key_bytes=_public_key(ROOT_PRIVATE),
            trust_policy_bytes=policy_bytes,
            policy_signature_bytes=cross_domain,
        ),
    )


def test_facts_tamper_rehash_and_cross_domain_signature_are_rejected() -> None:
    policy = _policy()
    authority = _authority(policy)
    candidate = _candidate()
    facts = _facts(candidate, policy)
    tampered = facts.model_copy(update={"expected_identity_hash": HASH_E})

    _expect_code(
        ReleaseIdentityErrorCode.FACTS_SIGNATURE_INVALID,
        lambda: authority.verify_core_release_identity(
            candidate,
            canonical_expected_facts_bytes(tampered),
            _signature_bytes(
                schema_version=FACTS_SIGNATURE_SCHEMA_VERSION,
                signer=facts.trusted_issuer,
                private_key=K1_PRIVATE,
                domain=EXPECTED_FACTS_DOMAIN,
                payload=canonical_expected_facts_bytes(facts),
            ),
        ),
    )
    _expect_code(
        ReleaseIdentityErrorCode.FACTS_SIGNATURE_INVALID,
        lambda: _verify(authority, candidate, facts, domain=POLICY_SIGNATURE_DOMAIN),
    )


def test_unlisted_wrong_key_id_inactive_and_revoked_issuers_are_rejected() -> None:
    candidate = _candidate()
    active = _issuer("issuer.release@k1", "v1", "issuer-k1", K1_PRIVATE)
    revoked = _issuer("issuer.release@k2", "v1", "issuer-k2", K2_PRIVATE, "revoked")
    policy = _policy(issuers=(active, revoked))
    authority = _authority(policy)

    unlisted = _issuer("issuer.attacker@v1", "v1", "attacker", ATTACKER_PRIVATE)
    _expect_code(
        ReleaseIdentityErrorCode.ISSUER_INACTIVE,
        lambda: _verify(
            authority,
            candidate,
            _facts(candidate, policy, unlisted),
            private_key=ATTACKER_PRIVATE,
        ),
    )
    wrong_key_id = SigningKeyIdentity(
        ref=active.ref,
        version=active.version,
        key_id="wrong-key",
        public_key_sha256=active.public_key_sha256,
    )
    _expect_code(
        ReleaseIdentityErrorCode.ISSUER_INACTIVE,
        lambda: _verify(
            authority,
            candidate,
            _facts(candidate, policy, active),
            signature_signer=wrong_key_id,
        ),
    )
    _expect_code(
        ReleaseIdentityErrorCode.ISSUER_REVOKED,
        lambda: _verify(
            authority,
            candidate,
            _facts(candidate, policy, revoked),
            private_key=K2_PRIVATE,
        ),
    )


def test_policy_rotation_requires_current_exact_policy_without_fallback() -> None:
    candidate = _candidate()
    k1 = _issuer("issuer.release@k1", "v1", "issuer-k1", K1_PRIVATE)
    k2 = _issuer("issuer.release@k2", "v1", "issuer-k2", K2_PRIVATE)
    policy_vn = _policy(version="v1", ref="policy.release@v1", issuers=(k1,))
    policy_vn1 = _policy(version="v2", ref="policy.release@v2", issuers=(k1, k2))
    policy_vn2 = _policy(
        version="v3",
        ref="policy.release@v3",
        issuers=(
            _issuer("issuer.release@k1", "v1", "issuer-k1", K1_PRIVATE, "revoked"),
            k2,
        ),
    )

    assert _verify(_authority(policy_vn), candidate, _facts(candidate, policy_vn)).trusted_issuer.ref == k1.ref
    assert _verify(_authority(policy_vn1), candidate, _facts(candidate, policy_vn1, k1)).trusted_issuer.ref == k1.ref
    assert (
        _verify(
            _authority(policy_vn1), candidate, _facts(candidate, policy_vn1, k2), private_key=K2_PRIVATE
        ).trusted_issuer.ref
        == k2.ref
    )
    _expect_code(
        ReleaseIdentityErrorCode.ISSUER_REVOKED,
        lambda: _verify(_authority(policy_vn2), candidate, _facts(candidate, policy_vn2, policy_vn2.issuers[0])),
    )
    assert (
        _verify(
            _authority(policy_vn2), candidate, _facts(candidate, policy_vn2, k2), private_key=K2_PRIVATE
        ).trusted_issuer.ref
        == k2.ref
    )
    _expect_code(
        ReleaseIdentityErrorCode.POLICY_IDENTITY_MISMATCH,
        lambda: _verify(_authority(policy_vn2), candidate, _facts(candidate, policy_vn, k1)),
    )


@pytest.mark.parametrize(
    "model_and_bytes",
    [
        (
            CoreReleaseIdentity,
            b'{"schema_version":"core-release-identity.v1","schema_version":"core-release-identity.v1"}\n',
        ),
        (
            CoreReleaseIdentity,
            b'{"schema_version":"core-release-identity.v1","uv_lock":{"ref":"a@1","ref":"b@1"}}\n',
        ),
        (
            TrustPolicy,
            b'{"schema_version":"core-release-trust-policy.v1","schema_version":"core-release-trust-policy.v1"}\n',
        ),
        (
            CoreReleaseExpectedFacts,
            b'{"schema_version":"core-release-expected-facts.v1","schema_version":"core-release-expected-facts.v1"}\n',
        ),
        (
            AuthorityPolicy,
            b'{"schema_version":"core-release-authority-policy.v1","schema_version":"core-release-authority-policy.v1"}\n',
        ),
    ],
)
def test_all_exported_json_readers_reject_top_level_and_nested_duplicate_keys(
    model_and_bytes: tuple[type[Any], bytes],
) -> None:
    model, raw = model_and_bytes
    with pytest.raises(ValueError, match="invalid_canonical_release_document"):
        model.model_validate_json(raw)


def test_json_readers_require_exact_canonical_bounded_bytes() -> None:
    raw = canonical_core_release_bytes(_candidate())
    assert CoreReleaseIdentity.model_validate_json(raw) == _candidate()
    for bad in (raw[:-1], raw + b"\n", b" " + raw, b"[" * 34 + b"]" * 34):
        with pytest.raises(ValueError, match="invalid_canonical_release_document"):
            CoreReleaseIdentity.model_validate_json(bad)
    with pytest.raises(ValueError, match="invalid_canonical_release_document"):
        CoreReleaseIdentity.model_validate_json(raw.decode("utf-8"))


@pytest.mark.parametrize(
    "bad_ref",
    [
        "refs/heads/main/",
        "refs/heads/main/.",
        "refs/heads/main/..",
        "refs//heads/abc123",
        "registry:stable/",
        "registry:stable/.",
        "/registry/version-1",
        "registry/version-1/",
        "registry::version-1",
        "registry/version-1.",
        "refs/heads/main",
        "registry:stable",
    ],
)
def test_precise_refs_reject_empty_dot_trailing_and_moving_segments(bad_ref: str) -> None:
    payload = _candidate_payload()
    cast(dict[str, object], payload["migration"])["ref"] = bad_ref
    with pytest.raises(ValidationError):
        CoreReleaseIdentity.model_validate(payload)


def test_precise_ref_keeps_valid_core_release_name() -> None:
    assert _candidate("core.release").release_ref == "core.release"


def test_frozen_canonical_documents_have_exact_golden_bytes_and_hashes() -> None:
    policy = _policy()
    policy_bytes = canonical_trust_policy_bytes(policy)
    candidate = _candidate()
    facts = _facts(candidate, policy)
    authority_policy = AuthorityPolicy(
        schema_version=AUTHORITY_POLICY_SCHEMA_VERSION,
        root_public_key_sha256=sha256(_public_key(ROOT_PRIVATE)).hexdigest(),
        policy_ref=policy.policy_ref,
        policy_version=policy.policy_version,
        policy_sha256=sha256(policy_bytes).hexdigest(),
    )
    documents = {
        "identity": canonical_core_release_bytes(candidate),
        "expected_facts": canonical_expected_facts_bytes(facts),
        "trust_policy": policy_bytes,
        "authority_policy": canonical_authority_policy_bytes(authority_policy),
    }

    assert documents == GOLDEN_DOCUMENT_BYTES
    assert {name: sha256(raw).hexdigest() for name, raw in documents.items()} == GOLDEN_DOCUMENT_HASHES


_GOLDEN_DIR = Path(__file__).with_name("golden")
GOLDEN_DOCUMENT_BYTES = {
    "identity": (_GOLDEN_DIR / "core-release-identity.v1.json").read_bytes(),
    "expected_facts": (_GOLDEN_DIR / "core-release-expected-facts.v1.json").read_bytes(),
    "trust_policy": (_GOLDEN_DIR / "core-release-trust-policy.v1.json").read_bytes(),
    "authority_policy": (_GOLDEN_DIR / "core-release-authority-policy.v1.json").read_bytes(),
}
GOLDEN_DOCUMENT_HASHES = {
    "identity": "8f56af310c2443525d61886203c88a9138c10018b05442d7cd2c15b913dc805b",
    "expected_facts": "39fc81779df9aa634be639bd3686e9a81ef66711091cd4ef0ac3aba7599db0e6",
    "trust_policy": "16011981828aea34ce5e4d4aaa8c30d23e3c661ca0ab725c5b1bf7233c509111",
    "authority_policy": "45c98db1928fd05bacd88ee93f18c457a6a1959ba5a8e577db6c389c3fd33c12",
}


def test_canonical_transport_profile_is_shared_with_contract_serializer() -> None:
    assert canonical_authority_policy_bytes(
        AuthorityPolicy(
            schema_version=AUTHORITY_POLICY_SCHEMA_VERSION,
            root_public_key_sha256=HASH_A,
            policy_ref="policy.release@v1",
            policy_version="v1",
            policy_sha256=HASH_B,
        )
    ) == canonical_bytes(
        {
            "schema_version": AUTHORITY_POLICY_SCHEMA_VERSION,
            "root_public_key_sha256": HASH_A,
            "policy_ref": "policy.release@v1",
            "policy_version": "v1",
            "policy_sha256": HASH_B,
        }
    )
