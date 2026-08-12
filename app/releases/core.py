"""WS-5 canonical release contracts shared with the cleanroom verifier.

This module intentionally contains no authority loader or verification object.
Production verification runs only in the one-shot isolated cleanroom CLI so
candidate/plugin code never shares the verifier's Python module state.
"""

from __future__ import annotations

import re
from base64 import urlsafe_b64decode, urlsafe_b64encode
from enum import StrEnum
from hashlib import sha256
from json import JSONDecodeError, loads
from typing import Annotated, Any, ClassVar, Literal, Self, cast

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

MAX_IDENTITY_BYTES = 256 * 1024
MAX_EXPECTED_FACTS_BYTES = 256 * 1024
MAX_TRUST_POLICY_BYTES = 64 * 1024
MAX_AUTHORITY_POLICY_BYTES = 8 * 1024
MAX_SIGNATURE_ENVELOPE_BYTES = 8 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 4096
MAX_REF_LENGTH = 512
MAX_REF_SEGMENT_LENGTH = 128

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
    if len(value) > MAX_REF_LENGTH:
        raise ValueError("value exceeds the reference length limit")
    segments = re.split(r"[/@:]", value)
    if any(not segment or len(segment) > MAX_REF_SEGMENT_LENGTH for segment in segments):
        raise ValueError("value contains an empty or oversized reference segment")
    if _REF_RE.fullmatch(value) is None:
        raise ValueError("value must use the precise reference grammar")
    if any(segment in {".", ".."} or segment.startswith(".") or segment.endswith(".") for segment in segments):
        raise ValueError("value must not contain empty or dot reference segments")
    terminal = segments[-1].lower()
    terminal_without_suffix = terminal.rstrip("._+-")
    terminal_components = tuple(part for part in re.split(r"[._+\-]+", terminal_without_suffix) if part)
    terminal_component = terminal_components[-1] if terminal_components else ""
    if (
        value.lower() in _MOVING_ALIASES
        or terminal_without_suffix in _MOVING_ALIASES
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


def _decode_base64url(value: str, *, expected_length: int) -> bytes:
    if type(value) is not str or _B64URL_RE.fullmatch(value) is None or "=" in value:
        raise ValueError("invalid base64url")
    raw: bytes | None = None
    try:
        raw = urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))
    except (ValueError, TypeError):
        pass
    if raw is None:
        raise ValueError("invalid base64url")
    canonical = urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    if len(raw) != expected_length or canonical != value:
        raise ValueError("invalid decoded length")
    return raw


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
    decoded: object | None = None
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
        pass
    if decoded is None:
        raise ValueError(_INVALID_CANONICAL_DOCUMENT)
    return decoded


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
        parsed: Self | None = None
        canonical = False
        try:
            parsed = cls.model_validate(cls._prepare_json_payload(decoded))
            if _canonical_model_bytes(parsed) != json_data:
                raise _UnsafeReleaseInput
            canonical = True
        except (ValidationError, TypeError, ValueError, _UnsafeReleaseInput):
            pass
        if parsed is None or not canonical:
            raise ValueError(_INVALID_CANONICAL_DOCUMENT)
        return parsed


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


def canonical_verified_release_identity_bytes(identity: VerifiedReleaseIdentity) -> bytes:
    return _canonical_exact_model(identity, VerifiedReleaseIdentity)


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


__all__ = [
    "AUTHORITY_POLICY_SCHEMA_VERSION",
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
    "MAX_REF_LENGTH",
    "MAX_REF_SEGMENT_LENGTH",
    "MAX_SIGNATURE_ENVELOPE_BYTES",
    "MAX_TRUST_POLICY_BYTES",
    "POLICY_SIGNATURE_DOMAIN",
    "POLICY_SIGNATURE_SCHEMA_VERSION",
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
    "VerifiedReleaseIdentity",
    "canonical_authority_policy_bytes",
    "canonical_core_release_bytes",
    "canonical_expected_facts_bytes",
    "canonical_signature_envelope_bytes",
    "canonical_trust_policy_bytes",
    "canonical_verified_release_identity_bytes",
    "core_release_identity_hash",
]
