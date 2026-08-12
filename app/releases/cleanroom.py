"""One-shot WS-5 cleanroom release verifier.

The supervisor opens the read-only authority directory and untrusted input
files, then starts this module with ``python -I`` and passes only file
descriptors plus four protected pins.  The process verifies exactly one
candidate and exits; it never caches authority across policy rotation.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from base64 import urlsafe_b64decode, urlsafe_b64encode
from hashlib import sha256
from typing import Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import ValidationError

from app.releases.core import (
    AUTHORITY_POLICY_SCHEMA_VERSION,
    EXPECTED_FACTS_DOMAIN,
    FACTS_SIGNATURE_SCHEMA_VERSION,
    MAX_EXPECTED_FACTS_BYTES,
    MAX_IDENTITY_BYTES,
    MAX_SIGNATURE_ENVELOPE_BYTES,
    MAX_TRUST_POLICY_BYTES,
    POLICY_SIGNATURE_DOMAIN,
    POLICY_SIGNATURE_SCHEMA_VERSION,
    AuthorityPolicy,
    ContentAddressedBinding,
    CoreReleaseExpectedFacts,
    CoreReleaseIdentity,
    ReleaseIdentityError,
    ReleaseIdentityErrorCode,
    ReleaseSignatureEnvelope,
    SigningKeyIdentity,
    TrustPolicy,
    VerifiedReleaseIdentity,
    canonical_verified_release_identity_bytes,
    core_release_identity_hash,
)

ROOT_PUBLIC_KEY_FILE: Final = "root-public-key.bin"
TRUST_POLICY_FILE: Final = "trust-policy.json"
POLICY_SIGNATURE_FILE: Final = "policy-signature.json"
MAX_ROOT_PUBLIC_KEY_BYTES: Final = 32
_FORBIDDEN_MODULE_PREFIXES: Final = (
    "app.execution",
    "app.releases.fixture",
    "app.worker",
    "fastapi",
    "langgraph",
    "pydantic_ai",
    "psycopg",
    "sqlalchemy",
)


class _UnsafeCleanroomInput(Exception):
    """Private control flow with no untrusted message."""


def _decode_base64url(value: str, *, expected_length: int) -> bytes:
    raw: bytes | None = None
    if type(value) is str and "=" not in value:
        try:
            raw = urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))
        except (ValueError, TypeError):
            pass
    if raw is None:
        raise _UnsafeCleanroomInput
    canonical = urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    if len(raw) != expected_length or canonical != value:
        raise _UnsafeCleanroomInput
    return raw


def _key_identity(key: SigningKeyIdentity) -> tuple[str, str, str]:
    return key.ref, key.version, key.key_id


def _signature_identity(envelope: ReleaseSignatureEnvelope) -> tuple[str, str, str]:
    return envelope.signer_ref, envelope.signer_version, envelope.key_id


def _read_exact_fd(file_descriptor: int, *, max_bytes: int) -> bytes:
    try:
        file_stat = os.fstat(file_descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size <= 0 or file_stat.st_size > max_bytes:
            raise _UnsafeCleanroomInput
        chunks: list[bytes] = []
        remaining = file_stat.st_size
        while remaining:
            chunk = os.read(file_descriptor, remaining)
            if not chunk:
                raise _UnsafeCleanroomInput
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(file_descriptor, 1):
            raise _UnsafeCleanroomInput
        final_stat = os.fstat(file_descriptor)
        if (
            final_stat.st_dev != file_stat.st_dev
            or final_stat.st_ino != file_stat.st_ino
            or final_stat.st_size != file_stat.st_size
            or final_stat.st_mtime_ns != file_stat.st_mtime_ns
            or final_stat.st_ctime_ns != file_stat.st_ctime_ns
        ):
            raise _UnsafeCleanroomInput
        data = b"".join(chunks)
        if len(data) != file_stat.st_size:
            raise _UnsafeCleanroomInput
        return data
    except (OSError, ValueError, _UnsafeCleanroomInput):
        pass
    raise ReleaseIdentityError(ReleaseIdentityErrorCode.EXTERNAL_FACTS_MISSING)


def _read_authority_file(directory_fd: int, name: str, *, max_bytes: int) -> bytes:
    file_descriptor: int | None = None
    try:
        file_descriptor = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
        file_stat = os.fstat(file_descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_mode & 0o222:
            raise _UnsafeCleanroomInput
        return _read_exact_fd(file_descriptor, max_bytes=max_bytes)
    except (OSError, ValueError, _UnsafeCleanroomInput):
        pass
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
    raise ReleaseIdentityError(ReleaseIdentityErrorCode.EXTERNAL_FACTS_MISSING)


def _load_authority(
    *,
    authority_directory_fd: int,
    authority_policy: AuthorityPolicy,
) -> TrustPolicy:
    directory_valid = False
    try:
        directory_stat = os.fstat(authority_directory_fd)
        if not stat.S_ISDIR(directory_stat.st_mode) or directory_stat.st_mode & 0o222:
            raise _UnsafeCleanroomInput
        directory_valid = True
    except (OSError, _UnsafeCleanroomInput):
        pass
    if not directory_valid:
        raise ReleaseIdentityError(ReleaseIdentityErrorCode.EXTERNAL_FACTS_MISSING)

    root_public_key_bytes = _read_authority_file(
        authority_directory_fd,
        ROOT_PUBLIC_KEY_FILE,
        max_bytes=MAX_ROOT_PUBLIC_KEY_BYTES,
    )
    if (
        len(root_public_key_bytes) != MAX_ROOT_PUBLIC_KEY_BYTES
        or sha256(root_public_key_bytes).hexdigest() != authority_policy.root_public_key_sha256
    ):
        raise ReleaseIdentityError(ReleaseIdentityErrorCode.ROOT_PIN_MISMATCH)
    trust_policy_bytes = _read_authority_file(
        authority_directory_fd,
        TRUST_POLICY_FILE,
        max_bytes=MAX_TRUST_POLICY_BYTES,
    )
    policy: TrustPolicy | None = None
    try:
        policy = TrustPolicy.model_validate_json(trust_policy_bytes)
    except ValueError:
        pass
    if policy is None:
        raise ReleaseIdentityError(ReleaseIdentityErrorCode.POLICY_IDENTITY_MISMATCH)
    if (
        sha256(trust_policy_bytes).hexdigest() != authority_policy.policy_sha256
        or policy.policy_ref != authority_policy.policy_ref
        or policy.policy_version != authority_policy.policy_version
        or policy.root_key.public_key_sha256 != authority_policy.root_public_key_sha256
    ):
        raise ReleaseIdentityError(ReleaseIdentityErrorCode.POLICY_IDENTITY_MISMATCH)
    policy_signature_bytes = _read_authority_file(
        authority_directory_fd,
        POLICY_SIGNATURE_FILE,
        max_bytes=MAX_SIGNATURE_ENVELOPE_BYTES,
    )
    signature_valid = False
    try:
        envelope = ReleaseSignatureEnvelope.model_validate_json(policy_signature_bytes)
        if envelope.schema_version == POLICY_SIGNATURE_SCHEMA_VERSION and _signature_identity(
            envelope
        ) == _key_identity(policy.root_key):
            Ed25519PublicKey.from_public_bytes(root_public_key_bytes).verify(
                _decode_base64url(envelope.signature, expected_length=64),
                POLICY_SIGNATURE_DOMAIN + trust_policy_bytes,
            )
            signature_valid = True
    except (ValueError, InvalidSignature, _UnsafeCleanroomInput):
        pass
    if not signature_valid:
        raise ReleaseIdentityError(ReleaseIdentityErrorCode.POLICY_SIGNATURE_INVALID)
    return policy


def _verify_once(
    *,
    authority_directory_fd: int,
    candidate_fd: int,
    expected_facts_fd: int,
    issuer_signature_fd: int,
    authority_policy: AuthorityPolicy,
) -> VerifiedReleaseIdentity:
    """Verify once from fresh authority bytes and four supervisor-owned pins."""

    policy = _load_authority(
        authority_directory_fd=authority_directory_fd,
        authority_policy=authority_policy,
    )
    candidate_bytes = _read_exact_fd(candidate_fd, max_bytes=MAX_IDENTITY_BYTES)
    facts_bytes = _read_exact_fd(expected_facts_fd, max_bytes=MAX_EXPECTED_FACTS_BYTES)
    signature_bytes = _read_exact_fd(
        issuer_signature_fd,
        max_bytes=MAX_SIGNATURE_ENVELOPE_BYTES,
    )
    candidate: CoreReleaseIdentity | None = None
    try:
        candidate = CoreReleaseIdentity.model_validate_json(candidate_bytes)
    except ValueError:
        pass
    if candidate is None:
        raise ReleaseIdentityError(ReleaseIdentityErrorCode.INVALID_CANDIDATE)
    facts: CoreReleaseExpectedFacts | None = None
    try:
        facts = CoreReleaseExpectedFacts.model_validate_json(facts_bytes)
    except ValueError:
        pass
    if facts is None:
        raise ReleaseIdentityError(ReleaseIdentityErrorCode.INVALID_EXPECTED_FACTS)
    if (
        facts.trust_policy.ref != authority_policy.policy_ref
        or facts.trust_policy.version != authority_policy.policy_version
        or facts.trust_policy.content_hash != authority_policy.policy_sha256
    ):
        raise ReleaseIdentityError(ReleaseIdentityErrorCode.POLICY_IDENTITY_MISMATCH)
    envelope: ReleaseSignatureEnvelope | None = None
    try:
        envelope = ReleaseSignatureEnvelope.model_validate_json(signature_bytes)
    except ValueError:
        pass
    if envelope is None or envelope.schema_version != FACTS_SIGNATURE_SCHEMA_VERSION:
        raise ReleaseIdentityError(ReleaseIdentityErrorCode.FACTS_SIGNATURE_INVALID)
    if _signature_identity(envelope) != _key_identity(facts.trusted_issuer):
        raise ReleaseIdentityError(ReleaseIdentityErrorCode.ISSUER_INACTIVE)
    issuer = next(
        (
            item
            for item in policy.issuers
            if _key_identity(item) == _signature_identity(envelope)
            and item.public_key_sha256 == facts.trusted_issuer.public_key_sha256
        ),
        None,
    )
    if issuer is None:
        raise ReleaseIdentityError(ReleaseIdentityErrorCode.ISSUER_INACTIVE)
    if issuer.status == "revoked":
        raise ReleaseIdentityError(ReleaseIdentityErrorCode.ISSUER_REVOKED)
    facts_signature_valid = False
    try:
        Ed25519PublicKey.from_public_bytes(_decode_base64url(issuer.public_key, expected_length=32)).verify(
            _decode_base64url(envelope.signature, expected_length=64),
            EXPECTED_FACTS_DOMAIN + facts_bytes,
        )
        facts_signature_valid = True
    except (ValueError, InvalidSignature, _UnsafeCleanroomInput):
        pass
    if not facts_signature_valid:
        raise ReleaseIdentityError(ReleaseIdentityErrorCode.FACTS_SIGNATURE_INVALID)
    candidate_hash = core_release_identity_hash(candidate)
    expected_identity_hash = core_release_identity_hash(facts.expected_identity)
    if (
        facts.expected_identity_hash != expected_identity_hash
        or candidate_hash != expected_identity_hash
        or candidate != facts.expected_identity
    ):
        raise ReleaseIdentityError(ReleaseIdentityErrorCode.EXTERNAL_FACTS_MISMATCH)
    return VerifiedReleaseIdentity(
        schema_version="core-release-identity-binding.v1",
        release_ref=candidate.release_ref,
        release_version=candidate.release_version,
        identity_hash=candidate_hash,
        business_profile_ref=None,
        business_profile_hash=None,
        expected_facts=ContentAddressedBinding(
            ref=facts.expected_facts.ref,
            version=facts.expected_facts.version,
            content_hash=sha256(facts_bytes).hexdigest(),
        ),
        trust_policy=facts.trust_policy,
        trust_root=policy.root_key,
        trusted_issuer=facts.trusted_issuer,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authority-fd", type=int, required=True)
    parser.add_argument("--candidate-fd", type=int, required=True)
    parser.add_argument("--expected-facts-fd", type=int, required=True)
    parser.add_argument("--issuer-signature-fd", type=int, required=True)
    parser.add_argument("--root-public-key-sha256", required=True)
    parser.add_argument("--policy-ref", required=True)
    parser.add_argument("--policy-version", required=True)
    parser.add_argument("--policy-sha256", required=True)
    return parser.parse_args()


def _runtime_is_isolated() -> bool:
    if not (sys.flags.isolated and sys.flags.ignore_environment and sys.flags.no_user_site and sys.flags.safe_path):
        return False
    return not any(
        module_name == prefix or module_name.startswith(prefix + ".")
        for module_name in sys.modules
        for prefix in _FORBIDDEN_MODULE_PREFIXES
    )


def main() -> int:
    args = _parse_args()
    if not _runtime_is_isolated():
        sys.stderr.write(ReleaseIdentityErrorCode.EXTERNAL_FACTS_MISSING.value + "\n")
        return 2
    policy: AuthorityPolicy | None = None
    try:
        policy = AuthorityPolicy.model_validate(
            {
                "schema_version": AUTHORITY_POLICY_SCHEMA_VERSION,
                "root_public_key_sha256": args.root_public_key_sha256,
                "policy_ref": args.policy_ref,
                "policy_version": args.policy_version,
                "policy_sha256": args.policy_sha256,
            }
        )
    except ValidationError:
        pass
    if policy is None:
        sys.stderr.write(ReleaseIdentityErrorCode.POLICY_IDENTITY_MISMATCH.value + "\n")
        return 2
    result: VerifiedReleaseIdentity | None = None
    error_code: ReleaseIdentityErrorCode | None = None
    try:
        result = _verify_once(
            authority_directory_fd=args.authority_fd,
            candidate_fd=args.candidate_fd,
            expected_facts_fd=args.expected_facts_fd,
            issuer_signature_fd=args.issuer_signature_fd,
            authority_policy=policy,
        )
    except ReleaseIdentityError as error:
        error_code = error.code
    if result is None:
        sys.stderr.write((error_code or ReleaseIdentityErrorCode.EXTERNAL_FACTS_MISSING).value + "\n")
        return 1
    sys.stdout.buffer.write(canonical_verified_release_identity_bytes(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
