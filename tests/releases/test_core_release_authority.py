"""WS-5 release contracts and one-shot cleanroom verifier security tests."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from argparse import Namespace
from base64 import urlsafe_b64encode
from hashlib import sha256
from pathlib import Path
from traceback import format_exception
from typing import Any, cast

import pytest
from app.contracts.canonical import canonical_bytes
from app.releases import cleanroom as cleanroom_module
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
    ReleaseIdentityError,
    ReleaseIdentityErrorCode,
    ReleaseSignatureEnvelope,
    SigningKeyIdentity,
    TrustPolicy,
    VerifiedReleaseIdentity,
    canonical_authority_policy_bytes,
    canonical_core_release_bytes,
    canonical_expected_facts_bytes,
    canonical_signature_envelope_bytes,
    canonical_trust_policy_bytes,
    core_release_identity_hash,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
COMMIT = "1" * 40
ROOT_PRIVATE = Ed25519PrivateKey.from_private_bytes(b"\x01" * 32)
K1_PRIVATE = Ed25519PrivateKey.from_private_bytes(b"\x02" * 32)
K2_PRIVATE = Ed25519PrivateKey.from_private_bytes(b"\x03" * 32)
ATTACKER_PRIVATE = Ed25519PrivateKey.from_private_bytes(b"\x04" * 32)


def _b64(data: bytes) -> str:
    return urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _public_key(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes_raw()


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
    return TrustPolicy(
        schema_version="core-release-trust-policy.v1",
        policy_ref=ref,
        policy_version=version,
        root_key=_key_identity("root.release@v1", "v1", "root-k1", root_private),
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
    return canonical_signature_envelope_bytes(
        ReleaseSignatureEnvelope(
            schema_version=cast(Any, schema_version),
            signer_ref=signer.ref,
            signer_version=signer.version,
            key_id=signer.key_id,
            algorithm="Ed25519",
            signature=_b64(private_key.sign(domain + payload)),
        )
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
        expected_facts=ExpectedFactsDocumentBinding(ref="expected-facts.cleanroom@v1", version="v1"),
        trust_policy=_binding_model(policy.policy_ref, sha256(policy_bytes).hexdigest(), policy.policy_version),
        trusted_issuer=SigningKeyIdentity(
            ref=issuer.ref,
            version=issuer.version,
            key_id=issuer.key_id,
            public_key_sha256=issuer.public_key_sha256,
        ),
    )


def _authority_policy(policy: TrustPolicy) -> AuthorityPolicy:
    policy_bytes = canonical_trust_policy_bytes(policy)
    return AuthorityPolicy(
        schema_version=AUTHORITY_POLICY_SCHEMA_VERSION,
        root_public_key_sha256=sha256(_public_key(ROOT_PRIVATE)).hexdigest(),
        policy_ref=policy.policy_ref,
        policy_version=policy.policy_version,
        policy_sha256=sha256(policy_bytes).hexdigest(),
    )


def _write_read_only(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    path.chmod(0o444)


def _replace_read_only(path: Path, data: bytes) -> None:
    path.parent.chmod(0o755)
    path.chmod(0o644)
    path.write_bytes(data)
    path.chmod(0o444)
    path.parent.chmod(0o555)


def _authority_mount(
    tmp_path: Path,
    policy: TrustPolicy,
    *,
    root_private: Ed25519PrivateKey = ROOT_PRIVATE,
) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    mount = tmp_path / "authority"
    mount.mkdir()
    policy_bytes = canonical_trust_policy_bytes(policy)
    _write_read_only(mount / "root-public-key.bin", _public_key(root_private))
    _write_read_only(mount / "trust-policy.json", policy_bytes)
    _write_read_only(
        mount / "policy-signature.json",
        _signature_bytes(
            schema_version=POLICY_SIGNATURE_SCHEMA_VERSION,
            signer=policy.root_key,
            private_key=root_private,
            domain=POLICY_SIGNATURE_DOMAIN,
            payload=policy_bytes,
        ),
    )
    mount.chmod(0o555)
    return mount


def _invoke_cleanroom(
    tmp_path: Path,
    *,
    policy: TrustPolicy,
    pins: AuthorityPolicy | None = None,
    candidate: CoreReleaseIdentity | None = None,
    facts: CoreReleaseExpectedFacts | None = None,
    facts_private_key: Ed25519PrivateKey = K1_PRIVATE,
    signature_domain: bytes = EXPECTED_FACTS_DOMAIN,
    signature_signer: SigningKeyIdentity | None = None,
    mount: Path | None = None,
    authority_fd: int | None = None,
    isolated: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    candidate = candidate or _candidate()
    facts = facts or _facts(candidate, policy)
    pins = pins or _authority_policy(policy)
    if mount is None and authority_fd is None:
        mount = _authority_mount(tmp_path, policy)
    facts_bytes = canonical_expected_facts_bytes(facts)
    signature_bytes = _signature_bytes(
        schema_version=FACTS_SIGNATURE_SCHEMA_VERSION,
        signer=signature_signer or facts.trusted_issuer,
        private_key=facts_private_key,
        domain=signature_domain,
        payload=facts_bytes,
    )
    candidate_path = tmp_path / "candidate.json"
    facts_path = tmp_path / "facts.json"
    signature_path = tmp_path / "facts-signature.json"
    _write_read_only(candidate_path, canonical_core_release_bytes(candidate))
    _write_read_only(facts_path, facts_bytes)
    _write_read_only(signature_path, signature_bytes)
    descriptors = [
        (
            os.dup(authority_fd)
            if authority_fd is not None
            else os.open(cast(Path, mount), os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY)
        ),
        os.open(candidate_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW),
        os.open(facts_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW),
        os.open(signature_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW),
    ]
    command = [sys.executable]
    if isolated:
        command.append("-I")
    command.extend(
        [
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
            pins.root_public_key_sha256,
            "--policy-ref",
            pins.policy_ref,
            "--policy-version",
            pins.policy_version,
            "--policy-sha256",
            pins.policy_sha256,
        ]
    )
    try:
        return subprocess.run(  # noqa: S603 - interpreter/module/options are fixed by the release supervisor.
            command,
            env={},
            pass_fds=tuple(descriptors),
            capture_output=True,
            check=False,
            timeout=10,
        )
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def _assert_error(result: subprocess.CompletedProcess[bytes], code: ReleaseIdentityErrorCode) -> None:
    assert result.returncode != 0
    assert result.stdout == b""
    assert result.stderr == (code.value + "\n").encode()


def _verify_in_process_for_unit_coverage(
    tmp_path: Path,
    *,
    policy: TrustPolicy,
    pins: AuthorityPolicy | None = None,
    candidate: CoreReleaseIdentity | None = None,
    facts: CoreReleaseExpectedFacts | None = None,
    facts_private_key: Ed25519PrivateKey = K1_PRIVATE,
    signature_domain: bytes = EXPECTED_FACTS_DOMAIN,
    signature_signer: SigningKeyIdentity | None = None,
    signature_schema: str = FACTS_SIGNATURE_SCHEMA_VERSION,
    mount: Path | None = None,
    candidate_bytes: bytes | None = None,
    facts_bytes: bytes | None = None,
    signature_bytes: bytes | None = None,
) -> VerifiedReleaseIdentity:
    """Test-only adapter; production callers must use the isolated CLI."""

    tmp_path.mkdir(parents=True, exist_ok=True)
    candidate = candidate or _candidate()
    facts = facts or _facts(candidate, policy)
    mount = mount or _authority_mount(tmp_path, policy)
    facts_bytes = facts_bytes if facts_bytes is not None else canonical_expected_facts_bytes(facts)
    candidate_bytes = candidate_bytes if candidate_bytes is not None else canonical_core_release_bytes(candidate)
    if signature_bytes is None:
        signature_bytes = _signature_bytes(
            schema_version=signature_schema,
            signer=signature_signer or facts.trusted_issuer,
            private_key=facts_private_key,
            domain=signature_domain,
            payload=facts_bytes,
        )
    candidate_path = tmp_path / "unit-candidate.json"
    facts_path = tmp_path / "unit-facts.json"
    signature_path = tmp_path / "unit-signature.json"
    _write_read_only(candidate_path, candidate_bytes)
    _write_read_only(facts_path, facts_bytes)
    _write_read_only(
        signature_path,
        signature_bytes,
    )
    descriptors = [
        os.open(mount, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY),
        os.open(candidate_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW),
        os.open(facts_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW),
        os.open(signature_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW),
    ]
    try:
        return cleanroom_module._verify_once(
            authority_directory_fd=descriptors[0],
            candidate_fd=descriptors[1],
            expected_facts_fd=descriptors[2],
            issuer_signature_fd=descriptors[3],
            authority_policy=pins or _authority_policy(policy),
        )
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def _walk_exception_chain(error: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _expect_in_process_code(code: ReleaseIdentityErrorCode, function: Any) -> None:
    with pytest.raises(ReleaseIdentityError) as exc_info:
        function()
    assert exc_info.value.code == code
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert len(_walk_exception_chain(exc_info.value)) == 1


def test_cleanroom_cli_accepts_exact_signed_expected_facts(tmp_path: Path) -> None:
    policy = _policy()
    result = _invoke_cleanroom(tmp_path, policy=policy)

    assert result.returncode == 0
    assert result.stderr == b""
    verified = VerifiedReleaseIdentity.model_validate_json(result.stdout)
    assert verified.identity_hash == core_release_identity_hash(_candidate())
    assert verified.trusted_issuer.ref == "issuer.release@k1"


def test_cleanroom_unit_adapter_matches_real_process_result(tmp_path: Path) -> None:
    policy = _policy()
    verified = _verify_in_process_for_unit_coverage(tmp_path, policy=policy)

    assert verified.identity_hash == core_release_identity_hash(_candidate())


def test_authority_root_policy_and_signature_failures_have_stable_codes(tmp_path: Path) -> None:
    policy = _policy()
    pins = _authority_policy(policy)
    _expect_in_process_code(
        ReleaseIdentityErrorCode.ROOT_PIN_MISMATCH,
        lambda: _verify_in_process_for_unit_coverage(
            tmp_path / "root",
            policy=policy,
            pins=pins.model_copy(update={"root_public_key_sha256": HASH_A}),
        ),
    )
    _expect_in_process_code(
        ReleaseIdentityErrorCode.POLICY_IDENTITY_MISMATCH,
        lambda: _verify_in_process_for_unit_coverage(
            tmp_path / "identity",
            policy=policy,
            pins=pins.model_copy(update={"policy_ref": "policy.other@v1"}),
        ),
    )
    bad_signature_mount = _authority_mount(tmp_path / "signature", policy)
    policy_bytes = canonical_trust_policy_bytes(policy)
    _replace_read_only(
        bad_signature_mount / "policy-signature.json",
        _signature_bytes(
            schema_version=POLICY_SIGNATURE_SCHEMA_VERSION,
            signer=policy.root_key,
            private_key=ATTACKER_PRIVATE,
            domain=POLICY_SIGNATURE_DOMAIN,
            payload=policy_bytes,
        ),
    )
    _expect_in_process_code(
        ReleaseIdentityErrorCode.POLICY_SIGNATURE_INVALID,
        lambda: _verify_in_process_for_unit_coverage(
            tmp_path / "signature-inputs",
            policy=policy,
            mount=bad_signature_mount,
        ),
    )


def test_authority_mount_and_file_failures_are_stable(tmp_path: Path) -> None:
    policy = _policy()
    writable_mount = _authority_mount(tmp_path / "writable", policy)
    writable_mount.chmod(0o755)
    _expect_in_process_code(
        ReleaseIdentityErrorCode.EXTERNAL_FACTS_MISSING,
        lambda: _verify_in_process_for_unit_coverage(
            tmp_path / "writable-inputs",
            policy=policy,
            mount=writable_mount,
        ),
    )

    missing_file_mount = _authority_mount(tmp_path / "missing", policy)
    missing_file_mount.chmod(0o755)
    (missing_file_mount / "policy-signature.json").unlink()
    missing_file_mount.chmod(0o555)
    _expect_in_process_code(
        ReleaseIdentityErrorCode.EXTERNAL_FACTS_MISSING,
        lambda: _verify_in_process_for_unit_coverage(
            tmp_path / "missing-inputs",
            policy=policy,
            mount=missing_file_mount,
        ),
    )

    malformed_policy_mount = _authority_mount(tmp_path / "malformed", policy)
    _replace_read_only(malformed_policy_mount / "trust-policy.json", b"{}\n")
    _expect_in_process_code(
        ReleaseIdentityErrorCode.POLICY_IDENTITY_MISMATCH,
        lambda: _verify_in_process_for_unit_coverage(
            tmp_path / "malformed-inputs",
            policy=policy,
            mount=malformed_policy_mount,
        ),
    )


def test_candidate_facts_signature_and_identity_failures_are_stable(tmp_path: Path) -> None:
    policy = _policy()
    candidate = _candidate()
    facts = _facts(candidate, policy)
    _expect_in_process_code(
        ReleaseIdentityErrorCode.INVALID_CANDIDATE,
        lambda: _verify_in_process_for_unit_coverage(
            tmp_path / "candidate",
            policy=policy,
            candidate_bytes=b"{}\n",
        ),
    )
    _expect_in_process_code(
        ReleaseIdentityErrorCode.INVALID_EXPECTED_FACTS,
        lambda: _verify_in_process_for_unit_coverage(
            tmp_path / "facts",
            policy=policy,
            facts_bytes=b"{}\n",
        ),
    )
    _expect_in_process_code(
        ReleaseIdentityErrorCode.FACTS_SIGNATURE_INVALID,
        lambda: _verify_in_process_for_unit_coverage(
            tmp_path / "signature",
            policy=policy,
            signature_bytes=b"{}\n",
        ),
    )
    _expect_in_process_code(
        ReleaseIdentityErrorCode.FACTS_SIGNATURE_INVALID,
        lambda: _verify_in_process_for_unit_coverage(
            tmp_path / "domain",
            policy=policy,
            signature_domain=POLICY_SIGNATURE_DOMAIN,
        ),
    )
    other_candidate = _candidate("core.release@2026.08.13")
    _expect_in_process_code(
        ReleaseIdentityErrorCode.EXTERNAL_FACTS_MISMATCH,
        lambda: _verify_in_process_for_unit_coverage(
            tmp_path / "identity",
            policy=policy,
            candidate=other_candidate,
            facts=facts,
        ),
    )


def test_unlisted_wrong_key_and_revoked_issuers_are_stable(tmp_path: Path) -> None:
    candidate = _candidate()
    active = _issuer("issuer.release@k1", "v1", "issuer-k1", K1_PRIVATE)
    revoked = _issuer("issuer.release@k2", "v1", "issuer-k2", K2_PRIVATE, "revoked")
    policy = _policy(issuers=(active, revoked))
    unlisted = _issuer("issuer.attacker@v1", "v1", "attacker-k1", ATTACKER_PRIVATE)
    _expect_in_process_code(
        ReleaseIdentityErrorCode.ISSUER_INACTIVE,
        lambda: _verify_in_process_for_unit_coverage(
            tmp_path / "unlisted",
            policy=policy,
            facts=_facts(candidate, policy, unlisted),
            facts_private_key=ATTACKER_PRIVATE,
        ),
    )
    wrong_key = SigningKeyIdentity(
        ref=active.ref,
        version=active.version,
        key_id="wrong-key",
        public_key_sha256=active.public_key_sha256,
    )
    _expect_in_process_code(
        ReleaseIdentityErrorCode.ISSUER_INACTIVE,
        lambda: _verify_in_process_for_unit_coverage(
            tmp_path / "wrong-key",
            policy=policy,
            signature_signer=wrong_key,
        ),
    )
    _expect_in_process_code(
        ReleaseIdentityErrorCode.ISSUER_REVOKED,
        lambda: _verify_in_process_for_unit_coverage(
            tmp_path / "revoked",
            policy=policy,
            facts=_facts(candidate, policy, revoked),
            facts_private_key=K2_PRIVATE,
        ),
    )


def test_policy_binding_in_facts_cannot_fallback(tmp_path: Path) -> None:
    current_policy = _policy(ref="policy.release@v2", version="v2")
    old_policy = _policy(ref="policy.release@v1", version="v1")
    _expect_in_process_code(
        ReleaseIdentityErrorCode.POLICY_IDENTITY_MISMATCH,
        lambda: _verify_in_process_for_unit_coverage(
            tmp_path,
            policy=current_policy,
            facts=_facts(_candidate(), old_policy),
        ),
    )


def test_production_module_exposes_no_in_process_authority_or_bytes_loader() -> None:
    import app.releases as releases
    import app.releases.cleanroom as cleanroom
    import app.releases.core as core

    for name in (
        "VerifiedReleaseAuthority",
        "load_verified_release_authority",
        "_load_verified_release_authority_from_bytes",
        "_VERIFIED_AUTHORITY_STATES",
        "_AUTHORITY_SEAL",
        "verify_once",
    ):
        assert not hasattr(core, name)
        assert not hasattr(releases, name)
        assert not hasattr(cleanroom, name)


def test_cleanroom_process_has_no_application_plugin_imports() -> None:
    assert cleanroom_module.__file__ is not None
    source_path = Path(cleanroom_module.__file__)
    imported_application_modules = {
        node.module
        for node in ast.walk(ast.parse(source_path.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom) and node.module is not None and node.module.startswith("app.")
    }

    assert imported_application_modules == {"app.releases.core"}


def test_cleanroom_cli_requires_isolated_python(tmp_path: Path) -> None:
    _assert_error(
        _invoke_cleanroom(tmp_path, policy=_policy(), isolated=False),
        ReleaseIdentityErrorCode.EXTERNAL_FACTS_MISSING,
    )


def test_runtime_isolation_requires_flags_and_rejects_forbidden_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not cleanroom_module._runtime_is_isolated()
    monkeypatch.setattr(
        sys,
        "flags",
        Namespace(isolated=1, ignore_environment=1, no_user_site=1, safe_path=True),
    )
    monkeypatch.setattr(cleanroom_module, "_FORBIDDEN_MODULE_PREFIXES", ())
    assert cleanroom_module._runtime_is_isolated()
    monkeypatch.setattr(cleanroom_module, "_FORBIDDEN_MODULE_PREFIXES", ("app.releases.cleanroom",))
    assert not cleanroom_module._runtime_is_isolated()


def test_cli_main_glue_emits_only_canonical_result_or_stable_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    policy = _policy()
    pins = _authority_policy(policy)
    verified = _verify_in_process_for_unit_coverage(tmp_path, policy=policy)
    arguments = Namespace(
        authority_fd=10,
        candidate_fd=11,
        expected_facts_fd=12,
        issuer_signature_fd=13,
        root_public_key_sha256=pins.root_public_key_sha256,
        policy_ref=pins.policy_ref,
        policy_version=pins.policy_version,
        policy_sha256=pins.policy_sha256,
    )
    monkeypatch.setattr(cleanroom_module, "_parse_args", lambda: arguments)
    monkeypatch.setattr(cleanroom_module, "_runtime_is_isolated", lambda: True)
    monkeypatch.setattr(cleanroom_module, "_verify_once", lambda **_: verified)

    assert cleanroom_module.main() == 0
    output = capfd.readouterr()
    assert output.err == ""
    assert VerifiedReleaseIdentity.model_validate_json(output.out.encode()) == verified

    def fail_verification(**_: object) -> VerifiedReleaseIdentity:
        raise ReleaseIdentityError(ReleaseIdentityErrorCode.ISSUER_REVOKED)

    monkeypatch.setattr(cleanroom_module, "_verify_once", fail_verification)
    assert cleanroom_module.main() == 1
    output = capfd.readouterr()
    assert output.out == ""
    assert output.err == ReleaseIdentityErrorCode.ISSUER_REVOKED.value + "\n"


def test_cli_main_glue_rejects_invalid_pins_and_nonisolated_runtime(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    pins = _authority_policy(_policy())
    arguments = Namespace(
        authority_fd=-1,
        candidate_fd=-1,
        expected_facts_fd=-1,
        issuer_signature_fd=-1,
        root_public_key_sha256=pins.root_public_key_sha256,
        policy_ref=pins.policy_ref,
        policy_version=pins.policy_version,
        policy_sha256=pins.policy_sha256,
    )
    monkeypatch.setattr(cleanroom_module, "_parse_args", lambda: arguments)
    monkeypatch.setattr(cleanroom_module, "_runtime_is_isolated", lambda: False)
    assert cleanroom_module.main() == 2
    assert capfd.readouterr().err == ReleaseIdentityErrorCode.EXTERNAL_FACTS_MISSING.value + "\n"

    arguments.root_public_key_sha256 = "0" * 64
    monkeypatch.setattr(cleanroom_module, "_runtime_is_isolated", lambda: True)
    assert cleanroom_module.main() == 2
    assert capfd.readouterr().err == ReleaseIdentityErrorCode.POLICY_IDENTITY_MISMATCH.value + "\n"


def test_fixed_pins_reject_whole_authority_reanchor(tmp_path: Path) -> None:
    trusted_policy = _policy()
    attacker_candidate = _candidate("core.release.attacker@v1")
    attacker_policy = _policy(
        root_private=ATTACKER_PRIVATE,
        ref="policy.attacker@v1",
        issuers=(_issuer("issuer.attacker@v1", "v1", "attacker-k1", ATTACKER_PRIVATE),),
    )
    attacker_mount = _authority_mount(tmp_path, attacker_policy, root_private=ATTACKER_PRIVATE)

    _assert_error(
        _invoke_cleanroom(
            tmp_path,
            policy=attacker_policy,
            pins=_authority_policy(trusted_policy),
            candidate=attacker_candidate,
            facts=_facts(attacker_candidate, attacker_policy, attacker_policy.issuers[0]),
            facts_private_key=ATTACKER_PRIVATE,
            mount=attacker_mount,
        ),
        ReleaseIdentityErrorCode.ROOT_PIN_MISMATCH,
    )


def test_each_invocation_reloads_current_policy_and_rejects_revoked_cached_issuer(tmp_path: Path) -> None:
    candidate = _candidate()
    k1 = _issuer("issuer.release@k1", "v1", "issuer-k1", K1_PRIVATE)
    active_policy = _policy(ref="policy.release@v1", version="v1", issuers=(k1,))
    active = _invoke_cleanroom(tmp_path / "active", policy=active_policy, candidate=candidate)
    assert active.returncode == 0

    revoked_policy = _policy(
        ref="policy.release@v2",
        version="v2",
        issuers=(_issuer("issuer.release@k1", "v1", "issuer-k1", K1_PRIVATE, "revoked"),),
    )
    revoked = _invoke_cleanroom(
        tmp_path / "revoked",
        policy=revoked_policy,
        candidate=candidate,
        facts=_facts(candidate, revoked_policy, revoked_policy.issuers[0]),
    )
    _assert_error(revoked, ReleaseIdentityErrorCode.ISSUER_REVOKED)


def test_supervisor_preopened_fd_avoids_intermediate_symlink_resolution(tmp_path: Path) -> None:
    policy = _policy()
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    real_mount = _authority_mount(real_parent, policy)
    trusted_fd = os.open(real_mount, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY)
    trusted_parent = tmp_path / "trusted-parent"
    real_parent.rename(trusted_parent)
    attacker_parent = tmp_path / "attacker-parent"
    attacker_policy = _policy(
        root_private=ATTACKER_PRIVATE,
        ref="policy.attacker@v1",
        issuers=(_issuer("issuer.attacker@v1", "v1", "attacker-k1", ATTACKER_PRIVATE),),
    )
    _authority_mount(attacker_parent, attacker_policy, root_private=ATTACKER_PRIVATE)
    real_parent.symlink_to(attacker_parent, target_is_directory=True)
    try:
        result = _invoke_cleanroom(tmp_path / "inputs", policy=policy, authority_fd=trusted_fd)
    finally:
        os.close(trusted_fd)

    assert result.returncode == 0
    assert real_parent.is_symlink()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", "core-release-identity.v2"),
        ("source_commit", "not-a-commit"),
        ("runtime_image_digest", "runtime:latest"),
        ("release_ref", 123),
        ("release_version", "latest"),
    ],
)
def test_candidate_invalid_missing_extra_and_coercible_values_fail_closed(field: str, value: object) -> None:
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


def test_exact_nested_type_and_tuple_boundaries_fail_closed() -> None:
    class BindingSubclass(ContentAddressedBinding):
        pass

    candidate = _candidate()
    injected = BindingSubclass.model_validate(candidate.uv_lock.model_dump())
    with pytest.raises(ReleaseIdentityError) as exc_info:
        canonical_core_release_bytes(candidate.model_copy(update={"uv_lock": injected}))
    assert exc_info.value.code == ReleaseIdentityErrorCode.INVALID_CANDIDATE
    assert exc_info.value.__context__ is None

    policy_payload = _policy().model_dump()
    policy_payload["issuers"] = list(policy_payload["issuers"])
    with pytest.raises(ValidationError):
        TrustPolicy.model_validate(policy_payload)


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


def test_key_hash_signature_size_and_authority_pins_are_strict() -> None:
    issuer_payload = _issuer("issuer.release@k1", "v1", "issuer-k1", K1_PRIVATE).model_dump()
    issuer_payload["public_key_sha256"] = HASH_A
    with pytest.raises(ValidationError):
        IssuerSigningKey.model_validate(issuer_payload)

    signature_payload = ReleaseSignatureEnvelope(
        schema_version=FACTS_SIGNATURE_SCHEMA_VERSION,
        signer_ref="issuer.release@k1",
        signer_version="v1",
        key_id="issuer-k1",
        algorithm="Ed25519",
        signature=_b64(b"x" * 64),
    ).model_dump()
    signature_payload["signature"] = _b64(b"short")
    with pytest.raises(ValidationError):
        ReleaseSignatureEnvelope.model_validate(signature_payload)

    for mutation in (
        {"root_public_key_sha256": "0" * 64},
        {"policy_version": "latest"},
        {"policy_ref": "policy.release@v1/.."},
    ):
        with pytest.raises(ValidationError):
            AuthorityPolicy.model_validate(
                {
                    "schema_version": AUTHORITY_POLICY_SCHEMA_VERSION,
                    "root_public_key_sha256": HASH_A,
                    "policy_ref": "policy.release@v1",
                    "policy_version": "v1",
                    "policy_sha256": HASH_B,
                    **mutation,
                }
            )


def test_cli_argument_parser_requires_all_fd_and_pin_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    pins = _authority_policy(_policy())
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cleanroom",
            "--authority-fd",
            "10",
            "--candidate-fd",
            "11",
            "--expected-facts-fd",
            "12",
            "--issuer-signature-fd",
            "13",
            "--root-public-key-sha256",
            pins.root_public_key_sha256,
            "--policy-ref",
            pins.policy_ref,
            "--policy-version",
            pins.policy_version,
            "--policy-sha256",
            pins.policy_sha256,
        ],
    )
    parsed = cleanroom_module._parse_args()
    assert parsed.authority_fd == 10
    assert parsed.policy_sha256 == pins.policy_sha256


@pytest.mark.parametrize(
    "bad_ref",
    [
        "refs/heads/main",
        "refs/heads/main+",
        "refs/heads/main-",
        "refs/heads/main_",
        "refs/heads/main.",
        "refs/heads/main/",
        "refs/heads/main@",
        "refs/heads/main:",
        "registry/stable+",
        "registry/stable-",
        "image/latest_",
        "image/latest.",
        "refs//heads/abc123",
        "registry/version/.",
        "registry/version/..",
        "x" * 513,
        "registry/" + "x" * 129,
    ],
)
def test_precise_refs_reject_moving_alias_suffixes_and_bounds(bad_ref: str) -> None:
    payload = _candidate_payload()
    cast(dict[str, object], payload["migration"])["ref"] = bad_ref
    with pytest.raises(ValidationError):
        CoreReleaseIdentity.model_validate(payload)


def test_precise_ref_keeps_valid_core_release_name() -> None:
    assert _candidate("core.release").release_ref == "core.release"


@pytest.mark.parametrize(
    "malicious",
    [
        b'{"schema_version":"core-release-identity.v1","marker":"nested-context-secret"}\n',
        b'{"schema_version":"core-release-identity.v1","schema_version":"nested-context-secret"}\n',
        b'[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[["nested-context-secret"]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]\n',
    ],
)
def test_canonical_reader_clears_full_exception_chain_and_traceback(malicious: bytes) -> None:
    with pytest.raises(ValueError) as exc_info:
        CoreReleaseIdentity.model_validate_json(malicious)

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert len(_walk_exception_chain(exc_info.value)) == 1
    assert "nested-context-secret" not in "".join(format_exception(exc_info.value))


def test_subprocess_failure_never_serializes_candidate_input(tmp_path: Path) -> None:
    policy = _policy()
    marker = "candidate-secret-marker"
    payload = _candidate_payload()
    payload["runtime_image_digest"] = marker
    candidate_path = tmp_path / "candidate.json"
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    candidate_path.write_bytes(canonical_bytes(payload))
    mount = _authority_mount(tmp_path, policy)
    facts = _facts(_candidate(), policy)
    facts_path = tmp_path / "facts.json"
    signature_path = tmp_path / "signature.json"
    facts_bytes = canonical_expected_facts_bytes(facts)
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
    descriptors = [
        os.open(mount, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY),
        os.open(candidate_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW),
        os.open(facts_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW),
        os.open(signature_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW),
    ]
    pins = _authority_policy(policy)
    try:
        result = subprocess.run(  # noqa: S603
            [
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
                pins.root_public_key_sha256,
                "--policy-ref",
                pins.policy_ref,
                "--policy-version",
                pins.policy_version,
                "--policy-sha256",
                pins.policy_sha256,
            ],
            env={},
            pass_fds=tuple(descriptors),
            capture_output=True,
            check=False,
            timeout=10,
        )
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
    _assert_error(result, ReleaseIdentityErrorCode.INVALID_CANDIDATE)
    assert marker.encode() not in result.stderr


def test_signature_domains_and_revocation_fail_closed(tmp_path: Path) -> None:
    candidate = _candidate()
    active = _issuer("issuer.release@k1", "v1", "issuer-k1", K1_PRIVATE)
    revoked = _issuer("issuer.release@k2", "v1", "issuer-k2", K2_PRIVATE, "revoked")
    policy = _policy(issuers=(active, revoked))
    _assert_error(
        _invoke_cleanroom(
            tmp_path / "cross-domain",
            policy=policy,
            candidate=candidate,
            signature_domain=POLICY_SIGNATURE_DOMAIN,
        ),
        ReleaseIdentityErrorCode.FACTS_SIGNATURE_INVALID,
    )
    _assert_error(
        _invoke_cleanroom(
            tmp_path / "revoked",
            policy=policy,
            candidate=candidate,
            facts=_facts(candidate, policy, revoked),
            facts_private_key=K2_PRIVATE,
        ),
        ReleaseIdentityErrorCode.ISSUER_REVOKED,
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


def test_frozen_canonical_documents_have_exact_golden_bytes_and_hashes() -> None:
    policy = _policy()
    policy_bytes = canonical_trust_policy_bytes(policy)
    candidate = _candidate()
    facts = _facts(candidate, policy)
    authority_policy = _authority_policy(policy)
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
