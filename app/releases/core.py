"""WS-5 Core release identity and cleanroom authority verification.

The release candidate and expected-facts document are untrusted inputs.  The
only trust entry is :func:`load_verified_release_authority`, which reads exact
root/policy pins from protected cleanroom configuration and exact signed
material from a read-only authority mount.
"""

from __future__ import annotations

import os
import re
import stat
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from json import JSONDecodeError, loads
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Self, cast
from weakref import WeakKeyDictionary

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, BeforeValidator, ConfigDict, ValidationError, model_validator

from app.contracts.canonical import canonical_bytes as canonical_contract_bytes

IDENTITY_SCHEMA_VERSION: Literal["core-release-identity.v1"] = "core-release-identity.v1"
EXPECTED_FACTS_SCHEMA_VERSION: Literal["core-release-expected-facts.v1"] = "core-release-expected-facts.v1"
IDENTITY_BINDING_SCHEMA_VERSION: Literal["core-release-identity-binding.v1"] = "core-release-identity-binding.v1"
TRUST_POLICY_SCHEMA_VERSION: Literal["core-release-trust-policy.v1"] = "core-release-trust-policy.v1"
AUTHORITY_POLICY_SCHEMA_VERSION: Literal["core-release-authority-policy.v1"] = "core-release-authority-policy.v1"
POLICY_SIGNATURE_SCHEMA_VERSION: Literal["core-release-policy-signature.v1"] = "core-release-policy-signature.v1"
FACTS_SIGNATURE_SCHEMA_VERSION: Literal["core-release-expected-facts-signature.v1"] = (
    "core-release-expected-facts-signature.v1"
)

POLICY_SIGNATURE_DOMAIN = b"GROVE-WS5-AUTHORITY-POLICY\0"
EXPECTED_FACTS_DOMAIN = b"GROVE-WS5-EXPECTED-FACTS\0"

AUTHORITY_MOUNT_ENV = "GROVE_RELEASE_AUTHORITY_MOUNT"
ROOT_PUBLIC_KEY_SHA256_ENV = "GROVE_RELEASE_ROOT_PUBLIC_KEY_SHA256"
EXPECTED_POLICY_REF_ENV = "GROVE_RELEASE_EXPECTED_POLICY_REF"
EXPECTED_POLICY_VERSION_ENV = "GROVE_RELEASE_EXPECTED_POLICY_VERSION"
EXPECTED_POLICY_SHA256_ENV = "GROVE_RELEASE_EXPECTED_POLICY_SHA256"
ROOT_PUBLIC_KEY_FILE = "root-public-key.bin"
TRUST_POLICY_FILE = "trust-policy.json"
POLICY_SIGNATURE_FILE = "policy-signature.json"

MAX_IDENTITY_BYTES = 256 * 1024
MAX_EXPECTED_FACTS_BYTES = 256 * 1024
MAX_TRUST_POLICY_BYTES = 64 * 1024
MAX_AUTHORITY_POLICY_BYTES = 8 * 1024
MAX_SIGNATURE_ENVELOPE_BYTES = 8 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 4096

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\-]*(?:[/@:][A-Za-z0-9][A-Za-z0-9._+\-]*)*$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\-]{0,63}$")
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\-]{0,127}$")
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_MOVING_ALIASES = frozenset(
    {"latest", "main", "master", "stable", "current", "head", "release", "dev", "develop", "trunk"}
)
_INVALID_CANONICAL_DOCUMENT = "invalid_canonical_release_document"
_AUTHORITY_SEAL = object()


class ReleaseIdentityErrorCode(StrEnum):
    """Stable, non-sensitive failures at the authority and identity seam."""

    INVALID_CANDIDATE = "invalid_candidate"
    INVALID_EXPECTED_FACTS = "invalid_expected_facts"
    EXTERNAL_FACTS_MISSING = "external_facts_missing"
    EXTERNAL_FACTS_MISMATCH = "external_facts_mismatch"
    BUSINESS_PROFILE_NOT_NULL = "business_profile_not_null"
    TARGET_NOT_CLEANROOM = "target_not_cleanroom"
    ROOT_PIN_MISMATCH = "root_pin_mismatch"
    POLICY_SIGNATURE_INVALID = "policy_signature_invalid"
    POLICY_IDENTITY_MISMATCH = "policy_identity_mismatch"
    ISSUER_INACTIVE = "issuer_inactive"
    ISSUER_REVOKED = "issuer_revoked"
    FACTS_SIGNATURE_INVALID = "facts_signature_invalid"


class ReleaseIdentityError(ValueError):
    """A stable failure that never includes caller-controlled material."""

    def __init__(self, code: ReleaseIdentityErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


class _UnsafeReleaseInput(Exception):
    """Private control-flow marker without an input-bearing message."""


def _exact_text(value: object) -> str:
    if type(value) is not str:
        raise ValueError("value must be an exact string")
    return value


def _exact_sha256(value: object) -> str:
    value = _exact_text(value)
    if _SHA256_RE.fullmatch(value) is None or value == "0" * 64:
        raise ValueError("value must be a non-zero lower-case sha256 digest")
    return value


def _exact_ref(value: object) -> str:
    value = _exact_text(value)
    if _REF_RE.fullmatch(value) is None:
        raise ValueError("value must use the precise reference grammar")
    segments = re.split(r"[/@:]", value)
    if any(segment in {".", ".."} or segment.startswith(".") or segment.endswith(".") for segment in segments):
        raise ValueError("value must not contain empty or dot reference segments")
    terminal = segments[-1].lower()
    terminal_component = re.split(r"[._+\-]", terminal)[-1]
    if (
        value.lower() in _MOVING_ALIASES
        or terminal in _MOVING_ALIASES
        or terminal_component in _MOVING_ALIASES - {"release"}
    ):
        raise ValueError("value must not end in a moving alias")
    return value


def _exact_version(value: object) -> str:
    value = _exact_text(value)
    if (
        _VERSION_RE.fullmatch(value) is None
        or value.startswith(".")
        or value.endswith(".")
        or value.lower() in _MOVING_ALIASES
    ):
        raise ValueError("value must be an exact non-floating version")
    return value


def _exact_key_id(value: object) -> str:
    value = _exact_text(value)
    if _KEY_ID_RE.fullmatch(value) is None:
        raise ValueError("value must be an exact key identifier")
    return value


def _exact_b64url(value: object) -> str:
    value = _exact_text(value)
    if _B64URL_RE.fullmatch(value) is None or "=" in value:
        raise ValueError("value must be unpadded base64url")
    return value


def _exact_commit(value: object) -> str:
    value = _exact_text(value)
    if _COMMIT_RE.fullmatch(value) is None or value == "0" * len(value):
        raise ValueError("source commit must be a lower-case git digest")
    return value


def _exact_image_digest(value: object) -> str:
    value = _exact_text(value)
    if _IMAGE_DIGEST_RE.fullmatch(value) is None or value == "sha256:" + "0" * 64:
        raise ValueError("runtime image must be an exact sha256 digest")
    return value


def _exact_tuple(value: object) -> tuple[object, ...]:
    if type(value) is not tuple:
        raise ValueError("value must be an exact tuple")
    return value


def _exact_literal(value: object, expected: str) -> str:
    if type(value) is not str or value != expected:
        raise ValueError("value must be the exact closed string")
    return value


def _literal_validator(expected: str) -> Any:
    return lambda value: _exact_literal(value, expected)


ExactSha256 = Annotated[str, BeforeValidator(_exact_sha256)]
PreciseRef = Annotated[str, BeforeValidator(_exact_ref)]
ExactVersion = Annotated[str, BeforeValidator(_exact_version)]
ExactKeyId = Annotated[str, BeforeValidator(_exact_key_id)]
ExactBase64Url = Annotated[str, BeforeValidator(_exact_b64url)]
SourceCommit = Annotated[str, BeforeValidator(_exact_commit)]
ImageDigest = Annotated[str, BeforeValidator(_exact_image_digest)]
IdentitySchema = Annotated[
    Literal["core-release-identity.v1"], BeforeValidator(_literal_validator(IDENTITY_SCHEMA_VERSION))
]
ExpectedFactsSchema = Annotated[
    Literal["core-release-expected-facts.v1"], BeforeValidator(_literal_validator(EXPECTED_FACTS_SCHEMA_VERSION))
]
BindingSchema = Annotated[
    Literal["core-release-identity-binding.v1"], BeforeValidator(_literal_validator(IDENTITY_BINDING_SCHEMA_VERSION))
]
TrustPolicySchema = Annotated[
    Literal["core-release-trust-policy.v1"], BeforeValidator(_literal_validator(TRUST_POLICY_SCHEMA_VERSION))
]
AuthorityPolicySchema = Annotated[
    Literal["core-release-authority-policy.v1"], BeforeValidator(_literal_validator(AUTHORITY_POLICY_SCHEMA_VERSION))
]
SignatureSchema = Annotated[
    Literal["core-release-policy-signature.v1", "core-release-expected-facts-signature.v1"],
    BeforeValidator(_exact_text),
]
TargetKind = Annotated[Literal["cleanroom_reference"], BeforeValidator(_literal_validator("cleanroom_reference"))]
EnvironmentKind = Annotated[Literal["cleanroom"], BeforeValidator(_literal_validator("cleanroom"))]
Algorithm = Annotated[Literal["Ed25519"], BeforeValidator(_literal_validator("Ed25519"))]
IssuerStatus = Annotated[Literal["active", "revoked"], BeforeValidator(_exact_text)]


def _json_pairs_no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise _UnsafeReleaseInput
        result[key] = value
    return result


def _safe_json_shape(value: object, *, depth: int = 0, nodes: list[int] | None = None) -> None:
    if nodes is None:
        nodes = [0]
    nodes[0] += 1
    if nodes[0] > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
        raise _UnsafeReleaseInput
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise _UnsafeReleaseInput
            _safe_json_shape(item, depth=depth + 1, nodes=nodes)
    elif type(value) is list:
        for item in value:
            _safe_json_shape(item, depth=depth + 1, nodes=nodes)
    elif type(value) not in {str, int, bool, type(None)}:
        raise _UnsafeReleaseInput


def _raise_unsafe_constant(_: str) -> None:
    raise _UnsafeReleaseInput


def _decode_json_document(data: object, *, max_bytes: int) -> object:
    if type(data) is not bytes or not data or len(data) > max_bytes:
        raise ValueError(_INVALID_CANONICAL_DOCUMENT)
    try:
        decoded = loads(
            data.decode("utf-8"),
            object_pairs_hook=_json_pairs_no_duplicates,
            parse_constant=_raise_unsafe_constant,
        )
        _safe_json_shape(decoded)
        return decoded
    except (
        JSONDecodeError,
        UnicodeDecodeError,
        RecursionError,
        MemoryError,
        TypeError,
        ValueError,
        _UnsafeReleaseInput,
    ):
        raise ValueError(_INVALID_CANONICAL_DOCUMENT) from None


class _ReleaseModel(BaseModel):
    """Strict frozen model with one duplicate-safe canonical JSON reader."""

    _max_json_bytes: ClassVar[int] = MAX_IDENTITY_BYTES
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_assignment=True)

    @model_validator(mode="before")
    @classmethod
    def _reject_duck_models(cls, value: object) -> object:
        if type(value) is not dict and type(value) is not cls:
            raise ValueError("release model input must be an exact dict or model")
        return value

    @classmethod
    def _prepare_json_payload(cls, value: object) -> object:
        return value

    @classmethod
    def model_validate_json(
        cls,
        json_data: str | bytes | bytearray,
        *,
        strict: bool | None = None,
        extra: Any | None = None,
        context: Any | None = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Self:
        del strict, extra, context, by_alias, by_name
        decoded = _decode_json_document(json_data, max_bytes=cls._max_json_bytes)
        try:
            parsed = cls.model_validate(cls._prepare_json_payload(decoded))
            if _canonical_model_bytes(parsed) != json_data:
                raise _UnsafeReleaseInput
            return parsed
        except (ValidationError, TypeError, ValueError, _UnsafeReleaseInput):
            raise ValueError(_INVALID_CANONICAL_DOCUMENT) from None


class ContentAddressedBinding(_ReleaseModel):
    ref: PreciseRef
    version: ExactVersion
    content_hash: ExactSha256


class ExpectedFactsDocumentBinding(_ReleaseModel):
    ref: PreciseRef
    version: ExactVersion


class SignedArtifactBinding(_ReleaseModel):
    artifact: ContentAddressedBinding
    signature: ContentAddressedBinding


class ContractSchemaBinding(_ReleaseModel):
    contract: ContentAddressedBinding
    abi: ContentAddressedBinding
    state_schema: ContentAddressedBinding


class ReferenceTargetBinding(ContentAddressedBinding):
    target_kind: TargetKind


class TargetEnvironmentBinding(ContentAddressedBinding):
    environment_kind: EnvironmentKind


class ExecutionBinding(_ReleaseModel):
    model: ContentAddressedBinding
    provider: ContentAddressedBinding
    model_policy: ContentAddressedBinding
    adapter: ContentAddressedBinding


class CoreReleaseIdentity(_ReleaseModel):
    """One exact Core candidate; it carries no release trust root."""

    schema_version: IdentitySchema
    release_ref: PreciseRef
    release_version: ExactVersion
    source_commit: SourceCommit
    uv_lock: ContentAddressedBinding
    sbom: SignedArtifactBinding
    runtime_build_manifest: ContentAddressedBinding
    runtime_image_digest: ImageDigest
    migration: ContentAddressedBinding
    contracts: ContractSchemaBinding
    deployment_topology: ContentAddressedBinding
    deployment_config: ContentAddressedBinding
    capability_profile: ContentAddressedBinding
    reference_target: ReferenceTargetBinding
    target_environment: TargetEnvironmentBinding
    deployment_cell: ContentAddressedBinding
    execution: ExecutionBinding
    business_profile_ref: None
    business_profile_hash: None

    @model_validator(mode="after")
    def _validate_core_scope(self) -> CoreReleaseIdentity:
        if self.business_profile_ref is not None or self.business_profile_hash is not None:
            raise ValueError("Core identity business profile fields must be null")
        if self.reference_target.target_kind != "cleanroom_reference":
            raise ValueError("Core identity target must be a cleanroom reference")
        if self.target_environment.environment_kind != "cleanroom":
            raise ValueError("Core identity environment must be cleanroom")
        return self


class SigningKeyIdentity(_ReleaseModel):
    ref: PreciseRef
    version: ExactVersion
    key_id: ExactKeyId
    public_key_sha256: ExactSha256


class IssuerSigningKey(SigningKeyIdentity):
    public_key: ExactBase64Url
    status: IssuerStatus

    @model_validator(mode="after")
    def _validate_public_key(self) -> IssuerSigningKey:
        raw = _decode_base64url(self.public_key, expected_length=32)
        if sha256(raw).hexdigest() != self.public_key_sha256:
            raise ValueError("issuer public-key hash mismatch")
        return self


class TrustPolicy(_ReleaseModel):
    _max_json_bytes = MAX_TRUST_POLICY_BYTES

    schema_version: TrustPolicySchema
    policy_ref: PreciseRef
    policy_version: ExactVersion
    root_key: SigningKeyIdentity
    issuers: Annotated[tuple[IssuerSigningKey, ...], BeforeValidator(_exact_tuple)]

    @classmethod
    def _prepare_json_payload(cls, value: object) -> object:
        if type(value) is not dict or type(value.get("issuers")) is not list:
            return value
        prepared = dict(value)
        prepared["issuers"] = tuple(value["issuers"])
        return prepared

    @model_validator(mode="after")
    def _validate_issuers(self) -> TrustPolicy:
        identities = tuple((item.ref, item.version, item.key_id) for item in self.issuers)
        if not identities or len(identities) > 16 or identities != tuple(sorted(identities)):
            raise ValueError("policy issuer keys must be non-empty, bounded and sorted")
        if len(set(identities)) != len(identities):
            raise ValueError("policy issuer key identities must be unique")
        return self


class AuthorityPolicy(_ReleaseModel):
    """Protected cleanroom pins; this document is not candidate-controlled."""

    _max_json_bytes = MAX_AUTHORITY_POLICY_BYTES

    schema_version: AuthorityPolicySchema
    root_public_key_sha256: ExactSha256
    policy_ref: PreciseRef
    policy_version: ExactVersion
    policy_sha256: ExactSha256


class CoreReleaseExpectedFacts(_ReleaseModel):
    _max_json_bytes = MAX_EXPECTED_FACTS_BYTES

    schema_version: ExpectedFactsSchema
    expected_identity: CoreReleaseIdentity
    expected_identity_hash: ExactSha256
    expected_facts: ExpectedFactsDocumentBinding
    trust_policy: ContentAddressedBinding
    trusted_issuer: SigningKeyIdentity


class ReleaseSignatureEnvelope(_ReleaseModel):
    _max_json_bytes = MAX_SIGNATURE_ENVELOPE_BYTES

    schema_version: SignatureSchema
    signer_ref: PreciseRef
    signer_version: ExactVersion
    key_id: ExactKeyId
    algorithm: Algorithm
    signature: ExactBase64Url

    @model_validator(mode="after")
    def _validate_signature_size(self) -> ReleaseSignatureEnvelope:
        _decode_base64url(self.signature, expected_length=64)
        return self


class VerifiedReleaseIdentity(_ReleaseModel):
    """A verified identity binding, never an IAR, PASS or publication result."""

    schema_version: BindingSchema
    release_ref: PreciseRef
    release_version: ExactVersion
    identity_hash: ExactSha256
    business_profile_ref: None
    business_profile_hash: None
    expected_facts: ContentAddressedBinding
    trust_policy: ContentAddressedBinding
    trust_root: SigningKeyIdentity
    trusted_issuer: SigningKeyIdentity


_MODEL_CHILDREN: dict[type[_ReleaseModel], dict[str, type[_ReleaseModel]]] = {
    ContentAddressedBinding: {},
    ExpectedFactsDocumentBinding: {},
    SignedArtifactBinding: {
        "artifact": ContentAddressedBinding,
        "signature": ContentAddressedBinding,
    },
    ContractSchemaBinding: {
        "contract": ContentAddressedBinding,
        "abi": ContentAddressedBinding,
        "state_schema": ContentAddressedBinding,
    },
    ReferenceTargetBinding: {},
    TargetEnvironmentBinding: {},
    ExecutionBinding: {
        "model": ContentAddressedBinding,
        "provider": ContentAddressedBinding,
        "model_policy": ContentAddressedBinding,
        "adapter": ContentAddressedBinding,
    },
    CoreReleaseIdentity: {
        "uv_lock": ContentAddressedBinding,
        "sbom": SignedArtifactBinding,
        "runtime_build_manifest": ContentAddressedBinding,
        "migration": ContentAddressedBinding,
        "contracts": ContractSchemaBinding,
        "deployment_topology": ContentAddressedBinding,
        "deployment_config": ContentAddressedBinding,
        "capability_profile": ContentAddressedBinding,
        "reference_target": ReferenceTargetBinding,
        "target_environment": TargetEnvironmentBinding,
        "deployment_cell": ContentAddressedBinding,
        "execution": ExecutionBinding,
    },
    SigningKeyIdentity: {},
    IssuerSigningKey: {},
    TrustPolicy: {"root_key": SigningKeyIdentity},
    AuthorityPolicy: {},
    CoreReleaseExpectedFacts: {
        "expected_identity": CoreReleaseIdentity,
        "expected_facts": ExpectedFactsDocumentBinding,
        "trust_policy": ContentAddressedBinding,
        "trusted_issuer": SigningKeyIdentity,
    },
    ReleaseSignatureEnvelope: {},
    VerifiedReleaseIdentity: {
        "expected_facts": ContentAddressedBinding,
        "trust_policy": ContentAddressedBinding,
        "trust_root": SigningKeyIdentity,
        "trusted_issuer": SigningKeyIdentity,
    },
}


def _canonical_model_bytes(model: BaseModel) -> bytes:
    return canonical_contract_bytes(model.model_dump(mode="json", exclude_unset=False))


def _canonical_exact_model(model: object, expected_type: type[_ReleaseModel]) -> bytes:
    validated = _revalidate_exact_model(model, expected_type)
    return _canonical_model_bytes(validated)


def canonical_core_release_bytes(candidate: CoreReleaseIdentity) -> bytes:
    return _canonical_exact_model(candidate, CoreReleaseIdentity)


def canonical_expected_facts_bytes(expected_facts: CoreReleaseExpectedFacts) -> bytes:
    return _canonical_exact_model(expected_facts, CoreReleaseExpectedFacts)


def canonical_trust_policy_bytes(policy: TrustPolicy) -> bytes:
    return _canonical_exact_model(policy, TrustPolicy)


def canonical_authority_policy_bytes(policy: AuthorityPolicy) -> bytes:
    return _canonical_exact_model(policy, AuthorityPolicy)


def canonical_signature_envelope_bytes(envelope: ReleaseSignatureEnvelope) -> bytes:
    return _canonical_exact_model(envelope, ReleaseSignatureEnvelope)


def core_release_identity_hash(candidate: CoreReleaseIdentity) -> str:
    return sha256(canonical_core_release_bytes(candidate)).hexdigest()


def _safe_model_payload(model: BaseModel, expected_type: type[_ReleaseModel]) -> dict[str, object]:
    if type(model) is not expected_type:
        raise _UnsafeReleaseInput
    values = object.__getattribute__(model, "__dict__")
    if type(values) is not dict:
        raise _UnsafeReleaseInput
    fields = expected_type.model_fields
    value_keys = tuple(values.keys())
    if (
        type(fields) is not dict
        or any(type(key) is not str for key in value_keys)
        or len(value_keys) != len(fields)
        or frozenset(value_keys) != frozenset(fields)
    ):
        raise _UnsafeReleaseInput
    children = _MODEL_CHILDREN.get(expected_type)
    if children is None:
        raise _UnsafeReleaseInput
    payload: dict[str, object] = {}
    for field_name, field_info in fields.items():
        del field_info
        value = values[field_name]
        child_type = children.get(field_name)
        if child_type is not None:
            if type(value) is not child_type:
                raise _UnsafeReleaseInput
            payload[field_name] = _safe_model_payload(value, child_type)
        elif type(value) is tuple:
            if expected_type is not TrustPolicy or field_name != "issuers":
                raise _UnsafeReleaseInput
            converted: list[object] = []
            for item in value:
                if type(item) is IssuerSigningKey:
                    converted.append(_safe_model_payload(item, IssuerSigningKey))
                else:
                    raise _UnsafeReleaseInput
            payload[field_name] = tuple(converted)
        elif type(value) in {str, type(None)}:
            payload[field_name] = value
        else:
            raise _UnsafeReleaseInput
    return payload


def _try_revalidate_exact_model(model: object, expected_type: type[_ReleaseModel]) -> _ReleaseModel | None:
    validated: _ReleaseModel | None = None
    try:
        if type(model) is not expected_type:
            raise _UnsafeReleaseInput
        payload = _safe_model_payload(cast(BaseModel, model), expected_type)
        validated = expected_type.model_validate(payload)
    except (ValidationError, TypeError, ValueError, _UnsafeReleaseInput):
        pass
    return validated


def _revalidate_exact_model(model: object, expected_type: type[_ReleaseModel]) -> _ReleaseModel:
    validated = _try_revalidate_exact_model(model, expected_type)
    if validated is None:
        raise ReleaseIdentityError(ReleaseIdentityErrorCode.INVALID_CANDIDATE)
    return validated


def _validated_candidate(candidate: object) -> CoreReleaseIdentity:
    validated = _try_revalidate_exact_model(candidate, CoreReleaseIdentity)
    if validated is not None:
        return cast(CoreReleaseIdentity, validated)
    code = ReleaseIdentityErrorCode.INVALID_CANDIDATE
    if type(candidate) is CoreReleaseIdentity:
        values = object.__getattribute__(candidate, "__dict__")
        if type(values) is dict:
            if values.get("business_profile_ref") is not None or values.get("business_profile_hash") is not None:
                code = ReleaseIdentityErrorCode.BUSINESS_PROFILE_NOT_NULL
            target = values.get("reference_target")
            if type(target) is ReferenceTargetBinding:
                target_values = object.__getattribute__(target, "__dict__")
                if type(target_values) is dict and target_values.get("target_kind") != "cleanroom_reference":
                    code = ReleaseIdentityErrorCode.TARGET_NOT_CLEANROOM
    raise ReleaseIdentityError(code)


def _decode_base64url(value: str, *, expected_length: int) -> bytes:
    if type(value) is not str or _B64URL_RE.fullmatch(value) is None or "=" in value:
        raise ValueError("invalid base64url")
    try:
        raw = urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))
    except (ValueError, TypeError):
        raise ValueError("invalid base64url") from None
    canonical = urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    if len(raw) != expected_length or canonical != value:
        raise ValueError("invalid decoded length")
    return raw


def _signature_identity(envelope: ReleaseSignatureEnvelope) -> tuple[str, str, str]:
    return envelope.signer_ref, envelope.signer_version, envelope.key_id


def _key_identity(key: SigningKeyIdentity) -> tuple[str, str, str]:
    return key.ref, key.version, key.key_id


class VerifiedReleaseAuthority:
    """Sealed authority loaded from the cleanroom-owned trust chain."""

    __slots__ = ("__weakref__",)

    def __new__(cls, *, _seal: object) -> VerifiedReleaseAuthority:
        if _seal is not _AUTHORITY_SEAL:
            raise TypeError("VerifiedReleaseAuthority is created only by the cleanroom loader")
        return super().__new__(cls)

    def verify_core_release_identity(
        self,
        candidate: CoreReleaseIdentity,
        expected_facts_bytes: bytes,
        issuer_signature_bytes: bytes,
    ) -> VerifiedReleaseIdentity:
        """Verify only candidate-controlled material against this sealed authority."""

        return _verify_core_release_identity(self, candidate, expected_facts_bytes, issuer_signature_bytes)


@dataclass(frozen=True, slots=True)
class _VerifiedAuthorityState:
    authority_policy: AuthorityPolicy
    policy: TrustPolicy
    policy_bytes: bytes


_VERIFIED_AUTHORITY_STATES: WeakKeyDictionary[VerifiedReleaseAuthority, _VerifiedAuthorityState] = WeakKeyDictionary()


def _authority_state(authority: object) -> _VerifiedAuthorityState:
    if type(authority) is not VerifiedReleaseAuthority:
        raise ReleaseIdentityError(ReleaseIdentityErrorCode.POLICY_IDENTITY_MISMATCH)
    try:
        state = _VERIFIED_AUTHORITY_STATES.get(authority)
        if (
            type(state) is not _VerifiedAuthorityState
            or type(state.authority_policy) is not AuthorityPolicy
            or type(state.policy) is not TrustPolicy
            or type(state.policy_bytes) is not bytes
            or canonical_trust_policy_bytes(state.policy) != state.policy_bytes
            or sha256(state.policy_bytes).hexdigest() != state.authority_policy.policy_sha256
            or state.policy.policy_ref != state.authority_policy.policy_ref
            or state.policy.policy_version != state.authority_policy.policy_version
        ):
            raise _UnsafeReleaseInput
    except (AttributeError, ReleaseIdentityError, _UnsafeReleaseInput):
        raise ReleaseIdentityError(ReleaseIdentityErrorCode.POLICY_IDENTITY_MISMATCH) from None
    return state


def _load_verified_release_authority_from_bytes(
    *,
    authority_policy_bytes: bytes,
    root_public_key_bytes: bytes,
    trust_policy_bytes: bytes,
    policy_signature_bytes: bytes,
) -> VerifiedReleaseAuthority:
    """Private pure helper behind the cleanroom file loader."""

    try:
        authority_policy = AuthorityPolicy.model_validate_json(authority_policy_bytes)
    except ValueError:
        raise ReleaseIdentityError(ReleaseIdentityErrorCode.POLICY_IDENTITY_MISMATCH) from None
    if type(root_public_key_bytes) is not bytes or len(root_public_key_bytes) != 32:
        raise ReleaseIdentityError(ReleaseIdentityErrorCode.ROOT_PIN_MISMATCH)
    if sha256(root_public_key_bytes).hexdigest() != authority_policy.root_public_key_sha256:
        raise ReleaseIdentityError(ReleaseIdentityErrorCode.ROOT_PIN_MISMATCH)
    try:
        policy = TrustPolicy.model_validate_json(trust_policy_bytes)
    except ValueError:
        raise ReleaseIdentityError(ReleaseIdentityErrorCode.POLICY_IDENTITY_MISMATCH) from None
    if (
        sha256(trust_policy_bytes).hexdigest() != authority_policy.policy_sha256
        or policy.policy_ref != authority_policy.policy_ref
        or policy.policy_version != authority_policy.policy_version
        or policy.root_key.public_key_sha256 != authority_policy.root_public_key_sha256
    ):
        raise ReleaseIdentityError(ReleaseIdentityErrorCode.POLICY_IDENTITY_MISMATCH)
    try:
        envelope = ReleaseSignatureEnvelope.model_validate_json(policy_signature_bytes)
        if envelope.schema_version != POLICY_SIGNATURE_SCHEMA_VERSION:
            raise _UnsafeReleaseInput
        if _signature_identity(envelope) != _key_identity(policy.root_key):
            raise _UnsafeReleaseInput
        Ed25519PublicKey.from_public_bytes(root_public_key_bytes).verify(
            _decode_base64url(envelope.signature, expected_length=64),
            POLICY_SIGNATURE_DOMAIN + trust_policy_bytes,
        )
    except (ValueError, InvalidSignature, _UnsafeReleaseInput):
        raise ReleaseIdentityError(ReleaseIdentityErrorCode.POLICY_SIGNATURE_INVALID) from None
    authority = VerifiedReleaseAuthority(_seal=_AUTHORITY_SEAL)
    _VERIFIED_AUTHORITY_STATES[authority] = _VerifiedAuthorityState(authority_policy, policy, trust_policy_bytes)
    return authority


def _read_mount_file(directory_fd: int, name: str, *, max_bytes: int) -> bytes:
    file_fd: int | None = None
    try:
        file_fd = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
        file_stat = os.fstat(file_fd)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_mode & 0o222
            or file_stat.st_size <= 0
            or file_stat.st_size > max_bytes
        ):
            raise _UnsafeReleaseInput
        chunks: list[bytes] = []
        remaining = file_stat.st_size
        while remaining:
            chunk = os.read(file_fd, remaining)
            if not chunk:
                raise _UnsafeReleaseInput
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(file_fd, 1):
            raise _UnsafeReleaseInput
        final_stat = os.fstat(file_fd)
        if (
            final_stat.st_dev != file_stat.st_dev
            or final_stat.st_ino != file_stat.st_ino
            or final_stat.st_size != file_stat.st_size
            or final_stat.st_mtime_ns != file_stat.st_mtime_ns
            or final_stat.st_ctime_ns != file_stat.st_ctime_ns
        ):
            raise _UnsafeReleaseInput
        data = b"".join(chunks)
        if len(data) != file_stat.st_size:
            raise _UnsafeReleaseInput
        return data
    except (OSError, ValueError, _UnsafeReleaseInput):
        raise ReleaseIdentityError(ReleaseIdentityErrorCode.EXTERNAL_FACTS_MISSING) from None
    finally:
        if file_fd is not None:
            os.close(file_fd)


def load_verified_release_authority() -> VerifiedReleaseAuthority:
    """Load pinned config values and signed material from the cleanroom.

    The verifier call has no root, policy, issuer-key or hash parameters.  The
    cleanroom process owner injects all four pins directly as protected deploy
    configuration and provides one read-only authority mount.  Replacing the
    mount cannot replace the separately pinned root or exact policy identity.
    """

    mount_path_value = os.environ.get(AUTHORITY_MOUNT_ENV)
    pin_values = {
        "schema_version": AUTHORITY_POLICY_SCHEMA_VERSION,
        "root_public_key_sha256": os.environ.get(ROOT_PUBLIC_KEY_SHA256_ENV),
        "policy_ref": os.environ.get(EXPECTED_POLICY_REF_ENV),
        "policy_version": os.environ.get(EXPECTED_POLICY_VERSION_ENV),
        "policy_sha256": os.environ.get(EXPECTED_POLICY_SHA256_ENV),
    }
    if type(mount_path_value) is not str or any(type(value) is not str for value in pin_values.values()):
        raise ReleaseIdentityError(ReleaseIdentityErrorCode.EXTERNAL_FACTS_MISSING)
    try:
        authority_policy = AuthorityPolicy.model_validate(pin_values)
    except ValidationError:
        raise ReleaseIdentityError(ReleaseIdentityErrorCode.POLICY_IDENTITY_MISMATCH) from None
    mount_path = Path(mount_path_value)
    mount_fd: int | None = None
    try:
        if not mount_path.is_absolute():
            raise _UnsafeReleaseInput
        mount_fd = os.open(mount_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY)
        mount_stat = os.fstat(mount_fd)
        if not stat.S_ISDIR(mount_stat.st_mode) or mount_stat.st_mode & 0o222:
            raise _UnsafeReleaseInput
    except (OSError, _UnsafeReleaseInput):
        if mount_fd is not None:
            os.close(mount_fd)
        raise ReleaseIdentityError(ReleaseIdentityErrorCode.EXTERNAL_FACTS_MISSING) from None
    if mount_fd is None:
        raise ReleaseIdentityError(ReleaseIdentityErrorCode.EXTERNAL_FACTS_MISSING)
    try:
        return _load_verified_release_authority_from_bytes(
            authority_policy_bytes=canonical_authority_policy_bytes(authority_policy),
            root_public_key_bytes=_read_mount_file(mount_fd, ROOT_PUBLIC_KEY_FILE, max_bytes=32),
            trust_policy_bytes=_read_mount_file(mount_fd, TRUST_POLICY_FILE, max_bytes=MAX_TRUST_POLICY_BYTES),
            policy_signature_bytes=_read_mount_file(
                mount_fd,
                POLICY_SIGNATURE_FILE,
                max_bytes=MAX_SIGNATURE_ENVELOPE_BYTES,
            ),
        )
    finally:
        os.close(mount_fd)


def _verify_core_release_identity(
    authority: VerifiedReleaseAuthority,
    candidate: CoreReleaseIdentity,
    expected_facts_bytes: bytes,
    issuer_signature_bytes: bytes,
) -> VerifiedReleaseIdentity:
    state = _authority_state(authority)
    validated_candidate = _validated_candidate(candidate)
    try:
        facts = CoreReleaseExpectedFacts.model_validate_json(expected_facts_bytes)
    except ValueError:
        raise ReleaseIdentityError(ReleaseIdentityErrorCode.INVALID_EXPECTED_FACTS) from None
    policy = state.policy
    authority_policy = state.authority_policy
    if (
        facts.trust_policy.ref != authority_policy.policy_ref
        or facts.trust_policy.version != authority_policy.policy_version
        or facts.trust_policy.content_hash != authority_policy.policy_sha256
    ):
        raise ReleaseIdentityError(ReleaseIdentityErrorCode.POLICY_IDENTITY_MISMATCH)
    try:
        envelope = ReleaseSignatureEnvelope.model_validate_json(issuer_signature_bytes)
    except ValueError:
        raise ReleaseIdentityError(ReleaseIdentityErrorCode.FACTS_SIGNATURE_INVALID) from None
    if envelope.schema_version != FACTS_SIGNATURE_SCHEMA_VERSION:
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
    try:
        Ed25519PublicKey.from_public_bytes(_decode_base64url(issuer.public_key, expected_length=32)).verify(
            _decode_base64url(envelope.signature, expected_length=64),
            EXPECTED_FACTS_DOMAIN + expected_facts_bytes,
        )
    except (ValueError, InvalidSignature):
        raise ReleaseIdentityError(ReleaseIdentityErrorCode.FACTS_SIGNATURE_INVALID) from None
    expected_identity_hash = core_release_identity_hash(facts.expected_identity)
    candidate_hash = core_release_identity_hash(validated_candidate)
    if (
        facts.expected_identity_hash != expected_identity_hash
        or candidate_hash != expected_identity_hash
        or validated_candidate != facts.expected_identity
    ):
        raise ReleaseIdentityError(ReleaseIdentityErrorCode.EXTERNAL_FACTS_MISMATCH)
    return VerifiedReleaseIdentity(
        schema_version=IDENTITY_BINDING_SCHEMA_VERSION,
        release_ref=validated_candidate.release_ref,
        release_version=validated_candidate.release_version,
        identity_hash=candidate_hash,
        business_profile_ref=None,
        business_profile_hash=None,
        expected_facts=ContentAddressedBinding(
            ref=facts.expected_facts.ref,
            version=facts.expected_facts.version,
            content_hash=sha256(expected_facts_bytes).hexdigest(),
        ),
        trust_policy=facts.trust_policy,
        trust_root=policy.root_key,
        trusted_issuer=facts.trusted_issuer,
    )


__all__ = [
    "AUTHORITY_MOUNT_ENV",
    "AUTHORITY_POLICY_SCHEMA_VERSION",
    "EXPECTED_POLICY_REF_ENV",
    "EXPECTED_POLICY_SHA256_ENV",
    "EXPECTED_POLICY_VERSION_ENV",
    "EXPECTED_FACTS_DOMAIN",
    "EXPECTED_FACTS_SCHEMA_VERSION",
    "FACTS_SIGNATURE_SCHEMA_VERSION",
    "IDENTITY_BINDING_SCHEMA_VERSION",
    "IDENTITY_SCHEMA_VERSION",
    "MAX_AUTHORITY_POLICY_BYTES",
    "MAX_EXPECTED_FACTS_BYTES",
    "MAX_IDENTITY_BYTES",
    "MAX_JSON_DEPTH",
    "MAX_JSON_NODES",
    "MAX_SIGNATURE_ENVELOPE_BYTES",
    "MAX_TRUST_POLICY_BYTES",
    "POLICY_SIGNATURE_DOMAIN",
    "POLICY_SIGNATURE_SCHEMA_VERSION",
    "ROOT_PUBLIC_KEY_SHA256_ENV",
    "TRUST_POLICY_SCHEMA_VERSION",
    "AuthorityPolicy",
    "ContentAddressedBinding",
    "ContractSchemaBinding",
    "CoreReleaseExpectedFacts",
    "CoreReleaseIdentity",
    "ExecutionBinding",
    "ExpectedFactsDocumentBinding",
    "IssuerSigningKey",
    "ReferenceTargetBinding",
    "ReleaseIdentityError",
    "ReleaseIdentityErrorCode",
    "ReleaseSignatureEnvelope",
    "SignedArtifactBinding",
    "SigningKeyIdentity",
    "TargetEnvironmentBinding",
    "TrustPolicy",
    "VerifiedReleaseAuthority",
    "VerifiedReleaseIdentity",
    "canonical_authority_policy_bytes",
    "canonical_core_release_bytes",
    "canonical_expected_facts_bytes",
    "canonical_signature_envelope_bytes",
    "canonical_trust_policy_bytes",
    "core_release_identity_hash",
    "load_verified_release_authority",
]
