"""Versioned, trusted-boundary contracts used by the WS-1 contract spine.

This module is deliberately independent from FastAPI, database, LangGraph and
provider adapters.  It owns the one canonical JSON profile used by contracts;
the runtime-build manifest reuses the serializer through the small public
helpers below.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum, StrEnum
from hashlib import sha256
from json import JSONDecodeError, dumps, loads
from math import isfinite
from re import fullmatch
from types import GetSetDescriptorType, MemberDescriptorType, UnionType
from typing import Annotated, Any, Generic, Literal, TypeVar, Union, cast, get_args, get_origin
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_core import SchemaValidator

CANONICAL_SEPARATOR = (",", ":")
SHA256_PATTERN = r"^[0-9a-f]{64}$"
REF_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,255}$"
IDENTIFIER_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9_.:@+\-]{0,127}$"
CAPABILITY_PATTERN = r"^[a-z][a-z0-9_.\-]{0,63}$"
MOVING_ALIASES = frozenset(
    {"latest", "main", "master", "stable", "current", "head", "release", "dev", "develop", "trunk"}
)
_PYDANTIC_MODEL_METACLASS = type(BaseModel)
# WS-1 has one Canonical Contract family.  ABI v2 is a separate, explicit
# reader in ``app.skill_abi``; advertising it here would make a caller able
# to parse a v1 payload through an unimplemented Canonical v2 family.
SUPPORTED_CONTRACT_VERSIONS = frozenset({"v1"})


class CanonicalModel(BaseModel):
    """Strict immutable base for every canonical message."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_assignment=True,
        str_strip_whitespace=True,
    )


CanonicalContract = CanonicalModel
CanonicalBaseModel = CanonicalModel


def _validate_ref(value: str, field_name: str = "reference") -> str:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be an exact string")
    if not fullmatch(REF_PATTERN, value):
        raise ValueError(f"{field_name} must be a precise non-latest reference")
    segments = value.replace("@", "/").replace(":", "/").split("/")
    terminal = next((segment for segment in reversed(segments) if segment), "").lower().rstrip("._+-")
    components = tuple(
        part for part in terminal.replace(".", "_").replace("+", "_").replace("-", "_").split("_") if part
    )
    terminal_component = components[-1] if components else ""
    if value.lower() in MOVING_ALIASES or terminal in MOVING_ALIASES or terminal_component in MOVING_ALIASES:
        raise ValueError(f"{field_name} must be a precise non-moving reference")
    return value


def _validate_hash(value: str, field_name: str = "content_hash") -> str:
    if not fullmatch(SHA256_PATTERN, value):
        raise ValueError(f"{field_name} must be a lower-case sha256 digest")
    return value


def _validate_expected_manifest_hash(value: Any) -> str:
    """Validate the external Manifest binding before crossing a schema seam.

    A Manifest's self-reported hash is data under test, not an authority.  The
    expected digest therefore has to be supplied by the already-bound Spec (or
    Contract) and must be a concrete, non-zero SHA-256 value.  Keeping this
    check in the contracts package lets the reverse-registered ABI verifier
    receive a guaranteed external binding without importing the runtime.
    """

    if value is None:
        raise TypeError("expected runtime manifest hash is required at a schema-bearing seam")
    if type(value) is not str:
        raise TypeError("expected runtime manifest hash must be a string")
    if not fullmatch(SHA256_PATTERN, value) or value == "0" * 64:
        raise ValueError("expected runtime manifest hash must be a non-zero lower-case sha256 digest")
    return value


def _validate_identifier(value: str, field_name: str = "identifier") -> str:
    if not fullmatch(IDENTIFIER_PATTERN, value):
        raise ValueError(f"{field_name} has an invalid format")
    return value


def _validate_aware_utc(value: datetime | None) -> datetime | None:
    """Normalize an optional timestamp without dereferencing ``None``.

    Optional lifecycle timestamps are intentionally distinct from omitted
    fields and explicit ``null`` in canonical JSON.  Validators therefore
    accept ``None`` and only enforce UTC awareness for an actual timestamp.
    """

    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _unique_sorted(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return tuple(sorted(values))


def _ref_key(value: VersionedRef) -> tuple[str, str, str]:
    return value.ref, value.version, value.content_hash


class VersionedRef(CanonicalModel):
    ref: str = Field(min_length=1, max_length=256)
    version: str = Field(min_length=1, max_length=64)
    content_hash: str = Field(pattern=SHA256_PATTERN)

    _ref = field_validator("ref")(lambda value: _validate_ref(value, "ref"))
    _version = field_validator("version")(lambda value: _validate_ref(value, "version"))
    _hash = field_validator("content_hash")(lambda value: _validate_hash(value))


class ContractMeta(CanonicalModel):
    contract_name: str = Field(min_length=1, max_length=128)
    contract_version: str = Field(min_length=1, max_length=32)
    message_id: UUID
    tenant_id: str = Field(min_length=1, max_length=128)
    correlation_id: str = Field(min_length=1, max_length=128)
    causation_id: UUID | None = None
    trace_id: str | None = Field(default=None, min_length=1, max_length=128)

    _name = field_validator("contract_name")(lambda value: _validate_identifier(value, "contract_name"))

    @field_validator("contract_version")
    @classmethod
    def validate_contract_version(cls, value: str) -> str:
        value = _validate_ref(value, "contract_version")
        if value not in SUPPORTED_CONTRACT_VERSIONS:
            raise ValueError(f"unsupported contract version: {value}")
        return value


def _require_meta_family(meta: ContractMeta, allowed_names: frozenset[str], family: str) -> None:
    if meta.contract_name not in allowed_names:
        raise ValueError(f"{family} requires a family-specific ContractMeta name")


def derive_contract_meta(
    previous: ContractMeta,
    *,
    contract_name: str,
    contract_version: str = "v1",
    message_id: UUID | None = None,
    causation_id: UUID | None = None,
) -> ContractMeta:
    """Create the metadata for a new message at a trust seam.

    Correlation and trace identity span a run, while every message owns a
    fresh ``message_id``.  ``causation_id`` is explicit so callers cannot
    accidentally reuse one immutable ``ContractMeta`` instance across a
    Payload → Decision → Request/Command transition.
    """

    return ContractMeta(
        contract_name=contract_name,
        contract_version=contract_version,
        message_id=message_id or uuid4(),
        tenant_id=previous.tenant_id,
        correlation_id=previous.correlation_id,
        causation_id=causation_id,
        trace_id=previous.trace_id,
    )


class ArtifactRef(CanonicalModel):
    artifact_id: UUID
    tenant_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    content_hash: str = Field(pattern=SHA256_PATTERN)
    media_type: str = Field(min_length=1, max_length=128)
    schema_ref: str | None = Field(default=None, min_length=1, max_length=256)
    sensitivity: Literal["public", "internal", "confidential", "restricted"]
    retention_policy_ref: str = Field(min_length=1, max_length=256)

    _version = field_validator("version")(lambda value: _validate_ref(value, "version"))
    _hash = field_validator("content_hash")(lambda value: _validate_hash(value))
    _schema = field_validator("schema_ref")(lambda value: None if value is None else _validate_ref(value, "schema_ref"))
    _retention = field_validator("retention_policy_ref")(lambda value: _validate_ref(value, "retention_policy_ref"))


class CheckpointRef(CanonicalModel):
    checkpoint_ref: str = Field(min_length=1, max_length=256)
    checkpoint_hash: str = Field(pattern=SHA256_PATTERN)
    tenant_id: str = Field(min_length=1, max_length=128)
    run_id: UUID
    graph_version: str = Field(min_length=1, max_length=128)
    graph_state_schema_version: str = Field(min_length=1, max_length=128)
    created_at: datetime

    _ref = field_validator("checkpoint_ref")(lambda value: _validate_ref(value, "checkpoint_ref"))
    _hash = field_validator("checkpoint_hash")(lambda value: _validate_hash(value, "checkpoint_hash"))
    _graph = field_validator("graph_version", "graph_state_schema_version")(
        lambda value: _validate_ref(value, "version")
    )
    _created = field_validator("created_at")(_validate_aware_utc)


class InterruptRef(CanonicalModel):
    interrupt_ref: str = Field(min_length=1, max_length=256)
    interrupt_hash: str = Field(pattern=SHA256_PATTERN)
    tenant_id: str = Field(min_length=1, max_length=128)
    run_id: UUID
    checkpoint: CheckpointRef
    interrupt_schema_ref: str = Field(min_length=1, max_length=256)
    nonce_hash: str = Field(pattern=SHA256_PATTERN)
    created_at: datetime
    expires_at: datetime | None = None

    _ref = field_validator("interrupt_ref", "interrupt_schema_ref")(lambda value: _validate_ref(value, "reference"))
    _hash = field_validator("interrupt_hash", "nonce_hash")(lambda value: _validate_hash(value, "hash"))
    _created = field_validator("created_at", "expires_at")(_validate_aware_utc)

    @model_validator(mode="after")
    def validate_binding(self) -> InterruptRef:
        if self.checkpoint.tenant_id != self.tenant_id or self.checkpoint.run_id != self.run_id:
            raise ValueError("interrupt checkpoint must bind the same tenant and run")
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("interrupt expiry must be after creation")
        return self


class EvaluationEvidenceRef(CanonicalModel):
    evaluation_run_id: UUID
    tenant_id: str = Field(min_length=1, max_length=128)
    evaluation_subject_hash: str = Field(pattern=SHA256_PATTERN)
    suite_ref: str = Field(min_length=1, max_length=256)
    decision: Literal["passed", "failed", "inconclusive"]
    evidence_bundle_hash: str = Field(pattern=SHA256_PATTERN)
    issuer: str = Field(min_length=1, max_length=256)
    attestation_ref: ArtifactRef

    _subject = field_validator("evaluation_subject_hash", "evidence_bundle_hash")(
        lambda value: _validate_hash(value, "evidence hash")
    )
    _suite = field_validator("suite_ref")(lambda value: _validate_ref(value, "suite_ref"))


class TraceRef(CanonicalModel):
    trace_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    redaction_policy_ref: str = Field(min_length=1, max_length=256)

    _redaction = field_validator("redaction_policy_ref")(lambda value: _validate_ref(value, "redaction_policy_ref"))


InferenceInputT = TypeVar("InferenceInputT", bound=BaseModel, covariant=True)
InferenceOutputT = TypeVar("InferenceOutputT", bound=BaseModel, covariant=True)
PayloadT = TypeVar("PayloadT", bound=CanonicalModel)


class CanonicalMessage(CanonicalModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = Field(min_length=1, max_length=16384)
    content_schema_ref: str | None = Field(default=None, max_length=256)

    _schema = field_validator("content_schema_ref")(
        lambda value: None if value is None else _validate_ref(value, "content schema")
    )


class InferenceContext(CanonicalModel):
    context_ref: str | None = Field(default=None, max_length=256)
    summary: str | None = Field(default=None, max_length=8192)

    _ref = field_validator("context_ref")(lambda value: None if value is None else _validate_ref(value, "context ref"))


class ResolvedModelPolicy(CanonicalModel):
    model_ref: str = Field(min_length=1, max_length=256)
    temperature: float = Field(ge=0.0, le=2.0)
    max_output_tokens: int = Field(ge=1, le=1_000_000)

    _model = field_validator("model_ref")(lambda value: _validate_ref(value, "model ref"))


class ResolvedInferenceRetryPolicy(CanonicalModel):
    max_schema_retries: int = Field(ge=0, le=100)
    max_provider_retries: int = Field(ge=0, le=100)


class InferenceBudget(CanonicalModel):
    max_tokens: int = Field(ge=1, le=10_000_000)
    max_cost_micros: int = Field(ge=0, le=10_000_000_000)
    deadline_ms: int = Field(ge=1, le=86_400_000)


class ModelUsage(CanonicalModel):
    input_tokens: int = Field(ge=0, le=10_000_000)
    output_tokens: int = Field(ge=0, le=10_000_000)
    cost_micros: int = Field(ge=0, le=10_000_000_000)


class KnowledgeFilter(CanonicalModel):
    tags: tuple[str, ...] = Field(default=(), max_length=32)
    language: str | None = Field(default=None, min_length=2, max_length=16)

    _tags = field_validator("tags")(_unique_sorted)

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for tag in value:
            if not 1 <= len(tag) <= 64:
                raise ValueError("knowledge filter tags must be 1..64 characters")
        return value


class RetrievalBudget(CanonicalModel):
    max_results: int = Field(ge=1, le=10_000)
    max_bytes: int = Field(ge=1, le=100_000_000)
    max_tokens: int = Field(ge=1, le=10_000_000)
    deadline_ms: int = Field(ge=1, le=86_400_000)


class FinalAnswerPayload(CanonicalModel, Generic[InferenceOutputT]):
    kind: Literal["final_answer"]
    output: InferenceOutputT
    rationale_summary: str = Field(min_length=1, max_length=2048)
    confidence: float = Field(ge=0.0, le=1.0)


class KnowledgeProposalPayload(CanonicalModel):
    kind: Literal["knowledge_proposal"]
    query: str = Field(min_length=1, max_length=4096)
    knowledge_refs: tuple[str, ...] = Field(max_length=32)
    # Retrieval selection is part of the model proposal.  A policy node may
    # tighten it, but it must never have to invent a missing filter.
    filter: KnowledgeFilter
    rationale_summary: str = Field(min_length=1, max_length=2048)
    confidence: float = Field(ge=0.0, le=1.0)

    _refs = field_validator("knowledge_refs")(_unique_sorted)

    @field_validator("knowledge_refs")
    @classmethod
    def validate_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_validate_ref(item, "knowledge ref") for item in value)


class ToolProposalPayload(CanonicalModel, Generic[InferenceInputT]):
    kind: Literal["tool_proposal"]
    tool_ref: str | VersionedRef
    input: InferenceInputT
    rationale_summary: str = Field(min_length=1, max_length=2048)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("tool_ref")
    @classmethod
    def validate_tool_ref(cls, value: str | VersionedRef) -> str | VersionedRef:
        return value if isinstance(value, VersionedRef) else _validate_ref(value, "tool_ref")


class ActionProposalPayload(CanonicalModel, Generic[InferenceInputT]):
    """Typed suggestion only; Action runtime is optional and out of WS-1."""

    kind: Literal["action_proposal"]
    action_ref: str = Field(min_length=1, max_length=256)
    input: InferenceInputT
    rationale_summary: str = Field(min_length=1, max_length=2048)
    confidence: float = Field(ge=0.0, le=1.0)

    _ref = field_validator("action_ref")(lambda value: _validate_ref(value, "action_ref"))


class DelegateProposalPayload(CanonicalModel, Generic[InferenceInputT]):
    """Typed suggestion only; delegation runtime is optional and out of WS-1."""

    kind: Literal["delegate_proposal"]
    target_skill_ref: str = Field(min_length=1, max_length=256)
    input: InferenceInputT
    rationale_summary: str = Field(min_length=1, max_length=2048)
    confidence: float = Field(ge=0.0, le=1.0)

    _ref = field_validator("target_skill_ref")(lambda value: _validate_ref(value, "target_skill_ref"))


InferenceDecisionPayload = Annotated[
    FinalAnswerPayload[BaseModel]
    | KnowledgeProposalPayload
    | ToolProposalPayload[BaseModel]
    | ActionProposalPayload[BaseModel]
    | DelegateProposalPayload[BaseModel],
    Field(discriminator="kind"),
]


class FinalAnswer(CanonicalModel, Generic[InferenceOutputT]):
    kind: Literal["final_answer"]
    meta: ContractMeta
    run_id: UUID
    decision_id: UUID
    output: InferenceOutputT
    artifact_refs: tuple[ArtifactRef, ...] = Field(max_length=32)
    rationale_summary: str = Field(min_length=1, max_length=2048)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("artifact_refs")
    @classmethod
    def validate_artifacts(cls, value: tuple[ArtifactRef, ...]) -> tuple[ArtifactRef, ...]:
        ids = tuple(str(item.artifact_id) for item in value)
        if len(ids) != len(set(ids)):
            raise ValueError("artifact_refs must be unique and stably sorted")
        return tuple(sorted(value, key=lambda item: str(item.artifact_id)))

    @model_validator(mode="after")
    def validate_meta_family(self) -> FinalAnswer[InferenceOutputT]:
        _require_meta_family(self.meta, frozenset({"canonical.decision"}), "FinalAnswer")
        return self


class KnowledgeProposal(CanonicalModel):
    kind: Literal["knowledge_proposal"]
    meta: ContractMeta
    run_id: UUID
    decision_id: UUID
    query: str = Field(min_length=1, max_length=4096)
    knowledge_refs: tuple[str, ...] = Field(max_length=32)
    filter: KnowledgeFilter
    rationale_summary: str = Field(min_length=1, max_length=2048)
    confidence: float = Field(ge=0.0, le=1.0)

    _refs = field_validator("knowledge_refs")(_unique_sorted)

    @field_validator("knowledge_refs")
    @classmethod
    def validate_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_validate_ref(item, "knowledge ref") for item in value)

    @model_validator(mode="after")
    def validate_meta_family(self) -> KnowledgeProposal:
        _require_meta_family(self.meta, frozenset({"canonical.decision"}), "KnowledgeProposal")
        return self


class ToolProposal(CanonicalModel, Generic[InferenceInputT]):
    kind: Literal["tool_proposal"]
    meta: ContractMeta
    run_id: UUID
    decision_id: UUID
    tool_ref: str | VersionedRef
    input: InferenceInputT
    rationale_summary: str = Field(min_length=1, max_length=2048)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("tool_ref")
    @classmethod
    def validate_tool_ref(cls, value: str | VersionedRef) -> str | VersionedRef:
        return value if isinstance(value, VersionedRef) else _validate_ref(value, "tool_ref")

    @model_validator(mode="after")
    def validate_meta_family(self) -> ToolProposal[InferenceInputT]:
        _require_meta_family(self.meta, frozenset({"canonical.decision"}), "ToolProposal")
        return self


class ActionProposal(CanonicalModel, Generic[InferenceInputT]):
    kind: Literal["action_proposal"]
    meta: ContractMeta
    run_id: UUID
    decision_id: UUID
    action_ref: str = Field(min_length=1, max_length=256)
    input: InferenceInputT
    rationale_summary: str = Field(min_length=1, max_length=2048)
    confidence: float = Field(ge=0.0, le=1.0)

    _ref = field_validator("action_ref")(lambda value: _validate_ref(value, "action_ref"))


class DelegateProposal(CanonicalModel, Generic[InferenceInputT]):
    kind: Literal["delegate_proposal"]
    meta: ContractMeta
    run_id: UUID
    decision_id: UUID
    target_skill_ref: str = Field(min_length=1, max_length=256)
    input: InferenceInputT
    rationale_summary: str = Field(min_length=1, max_length=2048)
    confidence: float = Field(ge=0.0, le=1.0)

    _ref = field_validator("target_skill_ref")(lambda value: _validate_ref(value, "target_skill_ref"))


CanonicalDecision = Annotated[
    FinalAnswer[BaseModel]
    | KnowledgeProposal
    | ToolProposal[BaseModel]
    | ActionProposal[BaseModel]
    | DelegateProposal[BaseModel],
    Field(discriminator="kind"),
]


class StructuredInferenceInput(CanonicalModel):
    """Build-owned Core conformance input schema for production inference."""

    question: str


class StructuredInferenceOutput(CanonicalModel):
    """Build-owned Core conformance output schema for production inference."""

    answer: str


class CanonicalInferenceRequest(CanonicalModel, Generic[InferenceInputT]):
    meta: ContractMeta
    inference_request_id: UUID
    run_id: UUID
    node_id: str = Field(min_length=1, max_length=128)
    node_attempt: int = Field(ge=0, le=1000)
    input: InferenceInputT
    context: InferenceContext | None = None
    context_refs: tuple[ArtifactRef, ...] = Field(default=(), max_length=32)
    instructions: tuple[CanonicalMessage, ...] = Field(default=(), max_length=128)
    model_policy: ResolvedModelPolicy
    result_schema_ref: str = Field(min_length=1, max_length=256)
    prompt_policy_ref: str = Field(min_length=1, max_length=256)
    model_policy_ref: str = Field(min_length=1, max_length=256)
    retry_policy: ResolvedInferenceRetryPolicy
    inference_retry_policy_ref: str = Field(min_length=1, max_length=256)
    budget: InferenceBudget
    budget_policy_ref: str = Field(min_length=1, max_length=256)

    _refs = field_validator(
        "result_schema_ref",
        "prompt_policy_ref",
        "model_policy_ref",
        "inference_retry_policy_ref",
        "budget_policy_ref",
    )(lambda value: _validate_ref(value, "reference"))

    @field_validator("context_refs")
    @classmethod
    def sort_context_refs(cls, value: tuple[ArtifactRef, ...]) -> tuple[ArtifactRef, ...]:
        ids = tuple(str(item.artifact_id) for item in value)
        if len(ids) != len(set(ids)):
            raise ValueError("context_refs must not contain duplicates")
        return tuple(sorted(value, key=lambda item: str(item.artifact_id)))

    @model_validator(mode="after")
    def validate_tenant(self) -> CanonicalInferenceRequest[InferenceInputT]:
        if self.meta.tenant_id == "":
            raise ValueError("tenant is required")
        if any(item.tenant_id != self.meta.tenant_id for item in self.context_refs):
            raise ValueError("context_refs must belong to the request tenant")
        _require_meta_family(
            self.meta,
            frozenset({"canonical.inference.request"}),
            "CanonicalInferenceRequest",
        )
        return self


class CanonicalInferenceResult(CanonicalModel, Generic[InferenceOutputT]):
    meta: ContractMeta
    inference_request_id: UUID
    result: InferenceOutputT
    model_ref: str = Field(min_length=1, max_length=256)
    usage: ModelUsage
    provider_attempts: int = Field(ge=1, le=1000)
    schema_retries: int = Field(ge=0, le=1000)
    provider_response_ref: ArtifactRef | None = None

    _model = field_validator("model_ref")(lambda value: _validate_ref(value, "model_ref"))

    @model_validator(mode="after")
    def validate_meta_family(self) -> CanonicalInferenceResult[InferenceOutputT]:
        _require_meta_family(
            self.meta,
            frozenset({"canonical.inference.result"}),
            "CanonicalInferenceResult",
        )
        return self


class KnowledgeRequest(CanonicalModel):
    meta: ContractMeta
    decision_id: UUID
    knowledge_request_id: UUID
    run_id: UUID
    authorization_decision_ref: str = Field(min_length=1, max_length=256)
    query: str = Field(min_length=1, max_length=4096)
    knowledge_refs: tuple[str, ...] = Field(max_length=32)
    filter: KnowledgeFilter
    purpose: str = Field(min_length=1, max_length=256)
    budget: RetrievalBudget
    required_citation_level: Literal["none", "source", "full"]

    _auth = field_validator("authorization_decision_ref")(
        lambda value: _validate_ref(value, "authorization_decision_ref")
    )
    _refs = field_validator("knowledge_refs")(_unique_sorted)

    @model_validator(mode="after")
    def validate_meta_family(self) -> KnowledgeRequest:
        _require_meta_family(self.meta, frozenset({"knowledge.request"}), "KnowledgeRequest")
        return self

    @field_validator("knowledge_refs")
    @classmethod
    def validate_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_validate_ref(item, "knowledge ref") for item in value)


class Citation(CanonicalModel):
    snapshot_ref: str = Field(min_length=1, max_length=256)
    snapshot_version: str = Field(min_length=1, max_length=64)
    source_version: str = Field(min_length=1, max_length=128)
    locator: str = Field(min_length=1, max_length=512)
    content_hash: str = Field(pattern=SHA256_PATTERN)

    _refs = field_validator("snapshot_ref")(lambda value: _validate_ref(value, "snapshot_ref"))
    _version = field_validator("snapshot_version")(lambda value: _validate_ref(value, "snapshot_version"))
    _hash = field_validator("content_hash")(lambda value: _validate_hash(value))


class KnowledgeItem(CanonicalModel):
    item_ref: str = Field(min_length=1, max_length=256)
    content: str = Field(min_length=1, max_length=16384)
    citations: tuple[Citation, ...] = Field(min_length=1, max_length=16)

    _ref = field_validator("item_ref")(lambda value: _validate_ref(value, "item_ref"))


class KnowledgeResult(CanonicalModel):
    meta: ContractMeta
    knowledge_request_id: UUID
    result_class: Literal["ok", "empty"]
    items: tuple[KnowledgeItem, ...] = Field(max_length=128)
    citations: tuple[Citation, ...] = Field(max_length=256)
    knowledge_snapshot_ref: str = Field(min_length=1, max_length=256)
    knowledge_snapshot_version: str = Field(min_length=1, max_length=64)
    knowledge_snapshot_content_hash: str = Field(pattern=SHA256_PATTERN)
    applied_acl_ref: str = Field(min_length=1, max_length=256)
    applied_acl_hash: str = Field(pattern=SHA256_PATTERN)
    retrieval_policy_ref: str = Field(min_length=1, max_length=256)
    retrieval_policy_hash: str = Field(pattern=SHA256_PATTERN)
    truncated: bool

    _refs = field_validator("knowledge_snapshot_ref", "applied_acl_ref", "retrieval_policy_ref")(
        lambda value: _validate_ref(value, "reference")
    )
    _hashes = field_validator("knowledge_snapshot_content_hash", "applied_acl_hash", "retrieval_policy_hash")(
        lambda value: _validate_hash(value)
    )

    @field_validator("citations")
    @classmethod
    def sort_citations(cls, value: tuple[Citation, ...]) -> tuple[Citation, ...]:
        keys = tuple((item.snapshot_ref, item.locator, item.content_hash) for item in value)
        if len(keys) != len(set(keys)):
            raise ValueError("citations must not contain duplicates")
        return tuple(sorted(value, key=lambda item: (item.snapshot_ref, item.locator, item.content_hash)))

    @model_validator(mode="after")
    def validate_result(self) -> KnowledgeResult:
        if self.result_class == "empty" and (self.items or self.citations):
            raise ValueError("empty knowledge result cannot carry items or citations")
        if self.result_class == "ok" and any(not item.citations for item in self.items):
            raise ValueError("every knowledge item requires a citation")
        return self

    @model_validator(mode="after")
    def validate_meta_family(self) -> KnowledgeResult:
        _require_meta_family(self.meta, frozenset({"knowledge.result"}), "KnowledgeResult")
        return self


class ToolResultProvenance(CanonicalModel):
    source_ref: str = Field(min_length=1, max_length=256)
    observed_at: datetime
    source_revision_or_watermark: str | None = Field(default=None, max_length=256)
    result_content_hash: str = Field(pattern=SHA256_PATTERN)

    _source = field_validator("source_ref")(lambda value: _validate_ref(value, "source_ref"))
    _observed = field_validator("observed_at")(_validate_aware_utc)
    _hash = field_validator("result_content_hash")(lambda value: _validate_hash(value))


class ToolCommand(CanonicalModel, Generic[InferenceInputT]):
    meta: ContractMeta
    decision_id: UUID
    tool_request_id: UUID
    run_id: UUID
    authorization_decision_ref: str = Field(min_length=1, max_length=256)
    tool_ref: str | VersionedRef
    input: InferenceInputT
    timeout_policy_ref: str = Field(min_length=1, max_length=256)

    _refs = field_validator("authorization_decision_ref", "timeout_policy_ref")(
        lambda value: _validate_ref(value, "reference")
    )

    @field_validator("tool_ref")
    @classmethod
    def validate_tool_ref(cls, value: str | VersionedRef) -> str | VersionedRef:
        return value if isinstance(value, VersionedRef) else _validate_ref(value, "tool_ref")

    @model_validator(mode="after")
    def validate_meta_family(self) -> ToolCommand[InferenceInputT]:
        _require_meta_family(self.meta, frozenset({"tool.command"}), "ToolCommand")
        return self


class ToolResult(CanonicalModel, Generic[InferenceOutputT]):
    meta: ContractMeta
    tool_request_id: UUID
    output: InferenceOutputT | None = None
    artifact_refs: tuple[ArtifactRef, ...] = Field(max_length=32)
    provenance: ToolResultProvenance | None = None
    failure: CanonicalFailure | None = None

    @field_validator("artifact_refs")
    @classmethod
    def sort_artifacts(cls, value: tuple[ArtifactRef, ...]) -> tuple[ArtifactRef, ...]:
        ids = tuple(str(item.artifact_id) for item in value)
        if len(ids) != len(set(ids)):
            raise ValueError("artifact_refs must not contain duplicates")
        return tuple(sorted(value, key=lambda item: str(item.artifact_id)))

    @model_validator(mode="after")
    def validate_outcome(self) -> ToolResult[InferenceOutputT]:
        if self.failure is None:
            if self.output is None and not self.artifact_refs:
                raise ValueError("successful ToolResult needs output or artifact")
        elif self.output is not None or self.artifact_refs or self.provenance is not None:
            raise ValueError("failed ToolResult cannot carry output, artifact or provenance")
        return self

    @model_validator(mode="after")
    def validate_meta_family(self) -> ToolResult[InferenceOutputT]:
        _require_meta_family(self.meta, frozenset({"tool.result"}), "ToolResult")
        return self


class RetryOwner(StrEnum):
    NONE = "none"
    TYPED_INFERENCE = "typed_inference"
    EXECUTION_KERNEL = "execution_kernel"
    RUN_COORDINATION = "run_coordination"
    DURABLE_ACTION = "durable_action"
    OPERATOR = "operator"


class CanonicalFailure(CanonicalModel):
    error_code: str = Field(min_length=1, max_length=128)
    failure_class: str = Field(min_length=1, max_length=128)
    retry_owner: RetryOwner
    retryable: bool
    safe_message: str = Field(min_length=1, max_length=1024)
    detail_ref: ArtifactRef | None = None

    _codes = field_validator("error_code", "failure_class")(lambda value: _validate_identifier(value, "failure code"))

    @field_validator("safe_message")
    @classmethod
    def validate_safe_message(cls, value: str) -> str:
        if any(ord(char) < 0x20 and char not in "\t" for char in value):
            raise ValueError("safe_message contains a control character")
        return value


class RuntimeEvent(CanonicalModel, Generic[PayloadT]):
    meta: ContractMeta
    event_id: UUID
    run_id: UUID
    orchestration_id: UUID
    run_seq: int = Field(ge=1, le=2**63 - 1)
    event_type: str = Field(min_length=1, max_length=128)
    source: str = Field(min_length=1, max_length=128)
    source_event_id: str = Field(min_length=1, max_length=256)
    payload_schema_ref: str = Field(min_length=1, max_length=256)
    payload: PayloadT
    occurred_at: datetime

    _refs = field_validator("payload_schema_ref")(lambda value: _validate_ref(value, "payload_schema_ref"))
    _occurred = field_validator("occurred_at")(_validate_aware_utc)

    @model_validator(mode="after")
    def validate_meta_family(self) -> RuntimeEvent[PayloadT]:
        _require_meta_family(self.meta, frozenset({"runtime.event"}), "RuntimeEvent")
        return self


class ProjectionSourceRef(CanonicalModel):
    source_kind: Literal["runtime_event", "interrupt", "action_approval", "run_delegation"]
    source_ref: str = Field(min_length=1, max_length=256)
    source_hash: str = Field(pattern=SHA256_PATTERN)
    source_revision: int | None = Field(default=None, ge=0)
    source_seq: int | None = Field(default=None, ge=1)
    source_schema_ref: str = Field(min_length=1, max_length=256)

    _refs = field_validator("source_ref", "source_schema_ref")(lambda value: _validate_ref(value, "reference"))
    _hash = field_validator("source_hash")(lambda value: _validate_hash(value, "source_hash"))


class InteractionItem(CanonicalModel, Generic[PayloadT]):
    interaction_id: UUID
    tenant_id: str = Field(min_length=1, max_length=128)
    presentation_run_id: UUID
    owner_run_id: UUID
    orchestration_id: UUID
    kind: Literal["user_input", "permission_request", "business_approval"]
    source: ProjectionSourceRef
    payload_schema_ref: str = Field(min_length=1, max_length=256)
    safe_payload: PayloadT
    status: Literal["pending", "resolved", "expired", "cancelled", "stale"]
    revision: int = Field(ge=0)
    source_watermarks: tuple[ProjectionSourceRef, ...] = Field(max_length=32)
    created_at: datetime
    expires_at: datetime | None = None
    resolved_at: datetime | None = None

    _schema = field_validator("payload_schema_ref")(lambda value: _validate_ref(value, "payload_schema_ref"))
    _times = field_validator("created_at", "expires_at", "resolved_at")(_validate_aware_utc)

    @field_validator("source_watermarks")
    @classmethod
    def sort_watermarks(cls, value: tuple[ProjectionSourceRef, ...]) -> tuple[ProjectionSourceRef, ...]:
        keys = tuple((item.source_ref, item.source_hash) for item in value)
        if len(keys) != len(set(keys)):
            raise ValueError("source_watermarks must not contain duplicates")
        return tuple(sorted(value, key=lambda item: (item.source_ref, item.source_hash)))


class MessageStarted(CanonicalModel):
    kind: Literal["message_started"]
    message_id: UUID
    owner_run_id: UUID
    role: Literal["user", "assistant", "system"]
    content_schema_ref: str = Field(min_length=1, max_length=256)

    _schema = field_validator("content_schema_ref")(lambda value: _validate_ref(value, "content_schema_ref"))


class MessageDelta(CanonicalModel):
    kind: Literal["message_delta"]
    message_id: UUID
    delta_seq: int = Field(ge=0)
    safe_delta: str = Field(min_length=1, max_length=8192)


class MessageCompleted(CanonicalModel):
    kind: Literal["message_completed"]
    message_id: UUID
    last_delta_seq: int = Field(ge=0)
    content_hash: str = Field(pattern=SHA256_PATTERN)
    artifact_ref: ArtifactRef | None = None

    _hash = field_validator("content_hash")(lambda value: _validate_hash(value, "content_hash"))


class InteractionUpserted(CanonicalModel):
    kind: Literal["interaction_upserted"]
    interaction: InteractionItem[CanonicalModel]


class InteractionResolved(CanonicalModel):
    kind: Literal["interaction_resolved"]
    interaction_id: UUID
    item_revision: int = Field(ge=0)
    status: Literal["resolved", "expired", "cancelled", "stale"]
    source: ProjectionSourceRef


class RunStatusChanged(CanonicalModel):
    kind: Literal["run_status_changed"]
    run_id: UUID
    status: Literal[
        "accepted",
        "running",
        "waiting_user_input",
        "waiting_action_result",
        "waiting_child_result",
        "cancel_requested",
        "succeeded",
        "failed",
        "cancelled",
    ]
    run_revision: int = Field(ge=0)


class DomainViewAccepted(CanonicalModel):
    kind: Literal["domain_view_accepted"]
    run_id: UUID
    tool_request_id: UUID
    view_schema_ref: str = Field(min_length=1, max_length=256)
    observed_at: datetime
    source_ref: str = Field(min_length=1, max_length=256)
    result_hash: str = Field(pattern=SHA256_PATTERN)
    item_count: int | None = Field(default=None, ge=0, le=1_000_000)

    _refs = field_validator("view_schema_ref", "source_ref")(lambda value: _validate_ref(value, "reference"))
    _hash = field_validator("result_hash")(lambda value: _validate_hash(value, "result_hash"))
    _observed = field_validator("observed_at")(_validate_aware_utc)


class ChildStatusChanged(CanonicalModel):
    kind: Literal["child_status_changed"]
    parent_run_id: UUID
    child_run_id: UUID
    delegation_id: UUID
    status: Literal["accepted", "running", "succeeded", "failed", "cancelled"]
    run_revision: int = Field(ge=0)


UIProjectionPayload = Annotated[
    MessageStarted
    | MessageDelta
    | MessageCompleted
    | InteractionUpserted
    | InteractionResolved
    | RunStatusChanged
    | DomainViewAccepted
    | ChildStatusChanged,
    Field(discriminator="kind"),
]


class UIProjectionEvent(CanonicalModel, Generic[PayloadT]):
    meta: ContractMeta
    event_id: UUID
    target_kind: Literal["run", "orchestration"]
    target_ref: UUID
    projection_seq: int = Field(ge=1)
    payload_schema_ref: str = Field(min_length=1, max_length=256)
    payload: PayloadT
    source_refs: tuple[ProjectionSourceRef, ...] = Field(max_length=32)
    projected_at: datetime

    _schema = field_validator("payload_schema_ref")(lambda value: _validate_ref(value, "payload_schema_ref"))
    _projected = field_validator("projected_at")(_validate_aware_utc)

    @field_validator("source_refs")
    @classmethod
    def sort_source_refs(cls, value: tuple[ProjectionSourceRef, ...]) -> tuple[ProjectionSourceRef, ...]:
        keys = tuple((item.source_ref, item.source_hash) for item in value)
        if len(keys) != len(set(keys)):
            raise ValueError("source_refs must not contain duplicates")
        return tuple(sorted(value, key=lambda item: (item.source_ref, item.source_hash)))

    @model_validator(mode="after")
    def validate_meta_family(self) -> UIProjectionEvent[PayloadT]:
        _require_meta_family(self.meta, frozenset({"ui.projection"}), "UIProjectionEvent")
        return self


def _normalise(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value).lower()
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime must be timezone-aware")
        utc_value = value.astimezone(UTC)
        timespec = "microseconds" if utc_value.microsecond else "seconds"
        return utc_value.isoformat(timespec=timespec).replace("+00:00", "Z")
    if isinstance(value, BaseModel):
        return _normalise(value.model_dump(mode="python", exclude_unset=True))
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("canonical JSON object keys must be strings")
        return {key: _normalise(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalise(item) for item in value]
    if isinstance(value, (set, frozenset)):
        raise TypeError("sets are not allowed in canonical JSON; use sorted semantic tuples")
    if isinstance(value, Enum):
        # Canonical contracts use StrEnum for a few closed protocol values.
        # User-provided typed schemas reject all Enum annotations before they
        # reach this serializer; ordinary Enum values remain unsupported.
        if isinstance(value, str):
            return str(value)
        raise TypeError(f"unsupported canonical value type: {type(value).__name__}")
    if isinstance(value, float) and not isfinite(value):
        raise ValueError("canonical JSON numbers must be finite")
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"unsupported canonical value type: {type(value).__name__}")


def canonical_bytes(value: Any, *, exclude_fields: tuple[str, ...] = ()) -> bytes:
    """Return deterministic UTF-8 canonical JSON bytes.

    The trailing newline is part of the frozen profile and preserves the WS-0
    manifest convention.  Optional fields omitted from a Pydantic model remain
    absent; explicitly supplied ``null`` remains explicit.
    """

    normalised = _normalise(value)
    if exclude_fields and isinstance(normalised, dict):
        normalised = {key: item for key, item in normalised.items() if key not in exclude_fields}
    return (
        dumps(
            normalised,
            ensure_ascii=False,
            sort_keys=True,
            separators=CANONICAL_SEPARATOR,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def canonical_hash(value: Any, *, exclude_fields: tuple[str, ...] = ()) -> str:
    return sha256(canonical_bytes(value, exclude_fields=exclude_fields)).hexdigest()


# Descriptive aliases used by callers that want to make the hash boundary
# visible in code review.  They intentionally resolve to the same serializer.
canonical_json_bytes = canonical_bytes
canonical_json_hash = canonical_hash


_FORBIDDEN_UNTRUSTED_FIELDS = frozenset(
    {
        "tenant",
        "tenant_id",
        "principal",
        "principal_ref",
        "authorization",
        "authorization_decision_ref",
        "credential",
        "credentials",
        "adapter",
        "adapter_client",
        "adapter_config",
        "provider",
        "provider_client",
        "database_url",
        "connection",
        "endpoint",
        "host",
        "scope",
        "scopes",
        "sql",
        "database",
        "schema",
        "table",
        "column",
        "join",
        "limit",
    }
)


def _reject_untrusted_fields(value: Any) -> None:
    """Reject identity/adapter fields before a permissive user model can drop them."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and key in _FORBIDDEN_UNTRUSTED_FIELDS:
                raise ValueError(f"untrusted payload field is not allowed: {key}")
            _reject_untrusted_fields(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            _reject_untrusted_fields(item)


def _is_model_type(value: Any) -> bool:
    if type(value) is not _PYDANTIC_MODEL_METACLASS or value is BaseModel:
        return False
    model_mro = type.__getattribute__(value, "__mro__")
    return type(model_mro) is tuple and any(base is BaseModel for base in model_mro)


def _read_base_model_storage(value: Any, runtime_type: Any) -> tuple[Any, Any, Any]:
    base_storage = object.__getattribute__(BaseModel, "__dict__")
    dict_descriptor = base_storage["__dict__"]
    extras_descriptor = base_storage["__pydantic_extra__"]
    fields_set_descriptor = base_storage["__pydantic_fields_set__"]
    if (
        type(dict_descriptor) is not GetSetDescriptorType
        or type(extras_descriptor) is not MemberDescriptorType
        or type(fields_set_descriptor) is not MemberDescriptorType
    ):
        raise TypeError("invalid BaseModel storage descriptors")
    storage = dict_descriptor.__get__(value, runtime_type)
    extras = extras_descriptor.__get__(value, runtime_type)
    fields_set = fields_set_descriptor.__get__(value, runtime_type)
    return storage, extras, fields_set


def _read_model_field_catalog(runtime_type: Any) -> dict[str, FieldInfo]:
    runtime_namespace = type.__getattribute__(runtime_type, "__dict__")
    if "model_fields" in runtime_namespace:
        raise TypeError("model field descriptor overrides are not allowed")
    runtime_fields = runtime_namespace.get("__pydantic_fields__")
    if type(runtime_fields) is not dict or any(
        type(name) is not str or type(field) is not FieldInfo for name, field in runtime_fields.items()
    ):
        raise TypeError("invalid model field catalog")
    return cast(dict[str, FieldInfo], runtime_fields)


def _assert_safe_annotation(
    annotation: Any,
    seen: set[type[BaseModel]],
    *,
    allow_mapping: bool = False,
) -> None:
    """Prove that a user schema cannot silently accept/drop nested fields.

    ``extra='forbid'`` on the top-level model is insufficient when a nested
    model is permissive (or when an ``Any``/arbitrary mapping field can carry
    tenant, SQL or adapter data).  Only scalar/collection annotations and
    recursively strict Pydantic models are admitted at this trust seam.
    """

    if annotation is Any or annotation is object:
        raise ValueError("typed execution schemas cannot contain Any/object fields")
    if annotation is None or annotation is type(None):
        return
    if _is_model_type(annotation):
        _assert_strict_model(annotation, seen, allow_mapping=allow_mapping)
        return
    origin = get_origin(annotation)
    if origin is Annotated:
        args = get_args(annotation)
        _assert_safe_annotation(args[0], seen, allow_mapping=allow_mapping)
        return
    if origin is Literal:
        literal_values = get_args(annotation)
        if any(
            isinstance(item, (Enum, set, frozenset, dict, list, tuple, Mapping))
            or (isinstance(item, float) and not isfinite(item))
            for item in literal_values
        ):
            raise ValueError("typed execution schema Literal contains an unsupported canonical value")
        return
    if origin is None:
        # This is an explicit serializer type whitelist.  In particular, an
        # arbitrary Enum, Decimal/Fraction, bytes or untyped container cannot
        # be admitted merely because Pydantic can validate it.
        if annotation in {str, int, float, bool, UUID, datetime}:
            return
        if type(annotation) is type(Enum):
            raise ValueError("typed execution schemas cannot contain Enum fields")
        # A forward reference or unresolved type variable cannot be proven.
        raise ValueError(f"typed execution schema contains unresolved annotation: {annotation!r}")
    if origin in (dict, Mapping):
        if not allow_mapping:
            raise ValueError("typed execution schemas cannot contain arbitrary mappings")
        arguments = get_args(annotation)
        if len(arguments) != 2 or arguments[0] is not str:
            raise ValueError("typed execution schemas require str-key mappings")
        _assert_safe_annotation(arguments[1], seen, allow_mapping=allow_mapping)
        return
    if origin in (set, frozenset):
        raise ValueError("typed execution schemas cannot contain set/frozenset fields")
    if origin in (list, tuple):
        arguments = get_args(annotation)
        if not arguments:
            raise ValueError("typed execution schemas require typed list/tuple elements")
        for argument in arguments:
            if argument is not Ellipsis:
                _assert_safe_annotation(argument, seen, allow_mapping=allow_mapping)
        return
    if origin in (Union, UnionType):
        for argument in get_args(annotation):
            _assert_safe_annotation(argument, seen, allow_mapping=allow_mapping)
        return
    for argument in get_args(annotation):
        if argument is Ellipsis:
            continue
        _assert_safe_annotation(argument, seen, allow_mapping=allow_mapping)


def _assert_strict_model(
    model: type[BaseModel],
    seen: set[type[BaseModel]] | None = None,
    *,
    allow_mapping: bool = True,
) -> None:
    if model is BaseModel:
        raise ValueError("bare BaseModel is not an execution schema")
    config = type.__getattribute__(model, "model_config")
    if config.get("extra") != "forbid" or config.get("frozen") is not True:
        raise ValueError("typed execution schemas must set extra='forbid' and frozen=True")
    visited = seen if seen is not None else set()
    if model in visited:
        return
    visited.add(model)
    fields = type.__getattribute__(model, "model_fields")
    for field in fields.values():
        _assert_safe_annotation(field.annotation, visited, allow_mapping=allow_mapping)


def _schema_model(schema: Any) -> type[BaseModel]:
    if type(schema) is TypeAdapter:
        raise TypeError("typed schema registry accepts model types, not TypeAdapter instances")
    if not _is_model_type(schema):
        raise TypeError("typed schema must be a concrete strict BaseModel type")
    _assert_strict_model(schema)
    return cast(type[BaseModel], schema)


def _schema_has_mapping_field(model: type[BaseModel], seen: set[type[BaseModel]] | None = None) -> bool:
    visited = seen if seen is not None else set()
    if model in visited:
        return False
    visited.add(model)

    def contains(annotation: Any) -> bool:
        origin = get_origin(annotation)
        if origin in (dict, Mapping):
            return True
        if origin is Annotated:
            return contains(get_args(annotation)[0])
        if origin in (Union, UnionType):
            return any(contains(argument) for argument in get_args(annotation))
        if origin in (list, tuple):
            return any(argument is not Ellipsis and contains(argument) for argument in get_args(annotation))
        if _is_model_type(annotation):
            return _schema_has_mapping_field(annotation, visited)
        return False

    return any(contains(field.annotation) for field in model.model_fields.values())


def _schema_adapter(schema: Any, *, allow_mapping: bool = False) -> TypeAdapter[Any]:
    """Construct the only adapters allowed at the typed trust boundary."""

    model = _schema_model(schema)
    if not allow_mapping and _schema_has_mapping_field(model):
        # The public helper historically represented closed scalar schemas;
        # registry-bound adapters explicitly opt into the serializer's
        # deterministic str-key mapping profile.
        raise ValueError("mapping adapters must be resolved through TypedSchemaRegistry")
    return TypeAdapter(model)


class _CanonicalAdapter:
    """Registry adapter that rejects values the canonical profile cannot encode."""

    def __init__(self, model: type[BaseModel]) -> None:
        self._adapter = TypeAdapter(model)

    def validate_python(self, value: Any, **kwargs: Any) -> Any:
        parsed = self._adapter.validate_python(value, **kwargs)
        try:
            canonical_bytes(parsed)
        except (TypeError, ValueError) as exc:
            raise ValueError("validated schema value is not canonically serializable") from exc
        return parsed

    def validate_json(self, value: Any, **kwargs: Any) -> Any:
        parsed = self._adapter.validate_json(value, **kwargs)
        try:
            canonical_bytes(parsed)
        except (TypeError, ValueError) as exc:
            raise ValueError("validated schema value is not canonically serializable") from exc
        return parsed


def _schema_ref_identity(reference: Any) -> tuple[str, str, str]:
    """Return safe exact ref fields without dereferencing a duck object."""

    if type(reference) is not VersionedRef:
        raise TypeError("schema_ref must be the exact VersionedRef type")
    ref = reference.ref
    version = reference.version
    content_hash = reference.content_hash
    if type(ref) is not str or type(version) is not str or type(content_hash) is not str:
        raise TypeError("schema_ref fields must use exact canonical scalar types")
    _validate_ref(ref, "schema ref")
    _validate_ref(version, "schema version")
    _validate_hash(content_hash, "schema hash")
    return ref, version, content_hash


def _schema_role(role: Any, *, allow_both: bool) -> Literal["input", "output", "both"]:
    """Validate a registry role before using it as a key or branch selector."""

    allowed = {"input", "output", "both"} if allow_both else {"input", "output"}
    if type(role) is not str or role not in allowed:
        suffix = ", or both" if allow_both else " or output"
        raise TypeError(f"schema role must be exactly input{suffix}")
    return cast(Literal["input", "output", "both"], role)


class TypedSchemaRegistry:
    """Exact VersionedRef → strict model bindings used by trusted readers."""

    def __init__(self) -> None:
        self._bindings: dict[tuple[str, str, str, str], type[BaseModel]] = {}
        self._preflight_validators: dict[tuple[str, str, str, str], SchemaValidator] = {}

    def register(
        self,
        reference: VersionedRef,
        schema: Any,
        *,
        role: Literal["input", "output", "both"] = "both",
    ) -> None:
        if type(self) is not TypedSchemaRegistry:
            raise TypeError("schema registry must use the exact TypedSchemaRegistry type")
        ref, version, content_hash = _schema_ref_identity(reference)
        checked_role = _schema_role(role, allow_both=True)
        model = _schema_model(schema)
        try:
            preflight_validator = SchemaValidator(_safe_preflight_core_schema(TypeAdapter(model).core_schema))
        except (TypeError, ValueError):
            raise ValueError("typed execution schema has an executable or unsupported Core constraint") from None
        keys = ("input", "output") if checked_role == "both" else (checked_role,)
        for binding_role in keys:
            key = (binding_role, ref, version, content_hash)
            if key in self._bindings:
                raise ValueError(f"schema binding already registered: {ref}@{version}")
            self._bindings[key] = model
            self._preflight_validators[key] = preflight_validator

    def resolve_model(self, reference: VersionedRef, *, role: str) -> type[BaseModel]:
        if type(self) is not TypedSchemaRegistry:
            raise TypeError("schema registry must use the exact TypedSchemaRegistry type")
        ref, version, content_hash = _schema_ref_identity(reference)
        checked_role = _schema_role(role, allow_both=False)
        try:
            return self._bindings[(checked_role, ref, version, content_hash)]
        except KeyError as exc:
            raise ValueError(f"no exact {checked_role} schema binding for {ref}@{version}") from exc

    def resolve(self, reference: VersionedRef, *, role: str) -> _CanonicalAdapter:
        if type(self) is not TypedSchemaRegistry:
            raise TypeError("schema registry must use the exact TypedSchemaRegistry type")
        return _CanonicalAdapter(self.resolve_model(reference, role=role))

    def _preflight_validate(self, reference: VersionedRef, *, role: str, value: Any) -> None:
        """Run only native Core constraints; user Pydantic hooks are absent."""

        ref, version, content_hash = _schema_ref_identity(reference)
        checked_role = _schema_role(role, allow_both=False)
        try:
            validator = self._preflight_validators[(checked_role, ref, version, content_hash)]
        except KeyError as exc:
            raise ValueError(f"no exact {checked_role} schema binding for {ref}@{version}") from exc
        validator.validate_python(value, strict=True)


SchemaRegistry = TypedSchemaRegistry


@dataclass(frozen=True, slots=True)
class CanonicalReadLimits:
    """Boundaries applied while decoding an untrusted canonical document.

    The values are deliberately plain exact integers.  A caller cannot pass a
    bool, an integer subclass, or a duck-typed limits object whose comparison
    or arithmetic could execute user code at the raw-data boundary.
    """

    max_bytes: int = 1_048_576
    max_depth: int = 32
    max_nodes: int = 10_000

    def __post_init__(self) -> None:
        for name, value in (
            ("max_bytes", self.max_bytes),
            ("max_depth", self.max_depth),
            ("max_nodes", self.max_nodes),
        ):
            if type(value) is not int or value < 1:
                raise TypeError(f"{name} must be a positive exact integer")


DEFAULT_CANONICAL_READ_LIMITS = CanonicalReadLimits()


class CanonicalCodecError(ValueError):
    """Stable, input-independent failure at the raw canonical seam."""


InferenceContextState = Literal["omitted", "null", "present"]


@dataclass(frozen=True, slots=True)
class DecodedCanonicalInferenceRequest(Generic[InferenceInputT]):
    """A validated request plus the explicit three-state context presence."""

    request: CanonicalInferenceRequest[InferenceInputT]
    context_state: InferenceContextState


def _resolve_schema_model(registry: Any, reference: VersionedRef, *, role: str) -> type[BaseModel]:
    """Resolve only through the concrete, exact typed schema registry."""

    if type(registry) is not TypedSchemaRegistry:
        raise TypeError("schema resolution requires an exact TypedSchemaRegistry")
    return registry.resolve_model(reference, role=role)


def _resolve_schema_adapter(registry: Any, reference: VersionedRef, *, role: str) -> Any:
    if type(registry) is not TypedSchemaRegistry:
        raise TypeError("schema resolution requires an exact TypedSchemaRegistry")
    return registry.resolve(reference, role=role)


def _decision_adapter(input_type: Any | None, output_type: Any | None) -> TypeAdapter[Any]:
    variants: list[Any] = [KnowledgeProposalPayload]
    if output_type is not None:
        variants.insert(0, _parameterize(FinalAnswerPayload, _schema_model(output_type)))
    if input_type is not None:
        model = _schema_model(input_type)
        variants.extend(
            (
                _parameterize(ToolProposalPayload, model),
                _parameterize(ActionProposalPayload, model),
                _parameterize(DelegateProposalPayload, model),
            )
        )
    if len(variants) == 1:
        return TypeAdapter(variants[0])
    # ``|`` is evaluated left-to-right for runtime Pydantic generic aliases;
    # building the union explicitly keeps all branch envelopes closed.
    union_type: Any = variants[0]
    for variant in variants[1:]:
        union_type = union_type | variant
    decision_type: Any = Annotated[union_type, Field(discriminator="kind")]
    return TypeAdapter(decision_type)


def _parameterize(generic: Any, model: type[BaseModel]) -> Any:
    return generic[model]


def _safe_preflight_core_schema(schema: Any) -> Any:
    """Clone Pydantic Core schema with every executable user hook removed.

    Native Core constraints (length, pattern, numeric bounds, container shape,
    unions, UUID and datetime parsing) remain.  Model nodes are reduced to
    ``model-fields`` so custom ``__init__`` and ``model_post_init`` cannot run.
    """

    if callable(schema):
        raise ValueError("callable Core schema values are not allowed at the canonical trust boundary")
    if type(schema) is list:
        return [_safe_preflight_core_schema(item) for item in schema]
    if type(schema) is tuple:
        return tuple(_safe_preflight_core_schema(item) for item in schema)
    if type(schema) is not dict:
        return schema

    schema_type = schema.get("type")
    if schema_type in {"function-before", "function-after", "function-wrap"}:
        inner = schema.get("schema")
        if type(inner) is not dict:
            raise ValueError("typed execution schema hook has no native inner schema")
        sanitized = _safe_preflight_core_schema(inner)
        if type(sanitized) is dict and type(schema.get("ref")) is str:
            sanitized["ref"] = schema["ref"]
        return sanitized
    if schema_type == "function-plain":
        raise ValueError("plain Pydantic validators are not allowed at the canonical trust boundary")
    if schema_type == "model":
        if schema.get("custom_init") is True:
            raise ValueError("custom model initializers are not allowed at the canonical trust boundary")
        inner = schema.get("schema")
        if type(inner) is not dict:
            raise ValueError("typed execution model has no native field schema")
        sanitized = _safe_preflight_core_schema(inner)
        if type(sanitized) is dict and type(schema.get("ref")) is str:
            sanitized["ref"] = schema["ref"]
        return sanitized
    if schema_type == "default" and schema.get("default_factory") is not None:
        raise ValueError("default factories are not allowed at the canonical trust boundary")

    if schema_type == "tagged-union" and callable(schema.get("discriminator")):
        raise ValueError("callable discriminators are not allowed at the canonical trust boundary")

    ignored_keys = {"cls", "function", "metadata", "post_init", "serialization"}
    cloned = {key: _safe_preflight_core_schema(value) for key, value in schema.items() if key not in ignored_keys}
    if schema_type == "model-fields":
        cloned["computed_fields"] = []
    return cloned


def _read_limit_values(limits: Any) -> tuple[int, int, int]:
    """Copy revalidated exact integers out of a potentially forged wrapper."""

    if type(limits) is not CanonicalReadLimits:
        raise TypeError("canonical read limits must use the exact CanonicalReadLimits type")
    max_bytes = limits.max_bytes
    max_depth = limits.max_depth
    max_nodes = limits.max_nodes
    for name, value in (("max_bytes", max_bytes), ("max_depth", max_depth), ("max_nodes", max_nodes)):
        if type(value) is not int or value < 1:
            raise TypeError(f"{name} must be a positive exact integer")
    return max_bytes, max_depth, max_nodes


def _validate_raw_json_tree(value: Any, *, max_depth: int, max_nodes: int) -> None:
    """Validate only exact built-in JSON values, without invoking user code."""

    nodes = 0

    def visit(current: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > max_nodes:
            raise CanonicalCodecError("canonical payload node limit exceeded")

        current_type = type(current)
        if current is None or current_type is bool or current_type is int:
            return
        if current_type is str:
            try:
                current.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                raise CanonicalCodecError("canonical JSON string is not valid UTF-8") from None
            return
        if current_type is float:
            if not isfinite(current):
                raise CanonicalCodecError("canonical JSON numbers must be finite")
            return
        if current_type is list:
            if depth >= max_depth:
                raise CanonicalCodecError("canonical payload depth limit exceeded")
            for item in current:
                visit(item, depth + 1)
            return
        if current_type is dict:
            if depth >= max_depth:
                raise CanonicalCodecError("canonical payload depth limit exceeded")
            for key, item in current.items():
                if type(key) is not str:
                    raise CanonicalCodecError("canonical JSON object keys must be exact strings")
                visit(item, depth + 1)
            return
        raise CanonicalCodecError("canonical payload contains an unsupported JSON value")

    visit(value, 0)


def _canonical_raw_bytes(raw: dict[str, Any], *, max_bytes: int) -> bytes:
    """Encode an already-proven exact JSON tree without exposing failures."""

    encoded: bytes | None = None
    try:
        candidate = canonical_bytes(raw)
        if type(candidate) is bytes:
            encoded = candidate
    except (TypeError, ValueError, RecursionError, MemoryError):
        pass
    if encoded is None:
        raise CanonicalCodecError("canonical payload encoding failed")
    if len(encoded) > max_bytes:
        raise CanonicalCodecError("canonical payload size limit exceeded")
    return encoded


def _duplicate_key_object(pairs: list[tuple[Any, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if type(key) is not str:
            # ``json.loads`` currently produces exact strings for object keys;
            # keep this guard explicit so a future decoder cannot widen it.
            raise CanonicalCodecError("canonical JSON object keys must be exact strings")
        if key in result:
            raise CanonicalCodecError("canonical JSON object contains duplicate keys")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise CanonicalCodecError("canonical JSON contains a non-finite number")


def _checked_schema_ref(reference: Any) -> VersionedRef:
    """Check exact ref identity before reading any of its fields."""

    try:
        _schema_ref_identity(reference)
    except (TypeError, ValueError):
        raise CanonicalCodecError("schema_ref is invalid") from None
    return cast(VersionedRef, reference)


def _checked_role(role: Any) -> Literal["input", "output"]:
    return cast(Literal["input", "output"], _schema_role(role, allow_both=False))


def _union_branches(value: Any, annotation: Any) -> tuple[Any, ...]:
    """Prefer a branch matching the exact raw JSON scalar/container type."""

    def score(branch: Any) -> int:
        branch_origin = get_origin(branch)
        if branch_origin is Annotated:
            return score(get_args(branch)[0])
        if branch_origin is Literal:
            return 0 if any(type(item) is type(value) and item == value for item in get_args(branch)) else 2
        exact_scalar = {str: str, bool: bool, int: int, float: float}.get(type(value))
        if exact_scalar is not None and branch is exact_scalar:
            return 0
        if type(value) is list and branch_origin is list:
            return 0
        if type(value) is dict and branch_origin in (dict, Mapping):
            return 0
        return 1

    return tuple(sorted(get_args(annotation), key=score))


def _raw_schema_preflight(value: Any, annotation: Any, *, path: str = "$") -> None:
    """Prove raw keys and leaf JSON types before Pydantic can run hooks."""

    if annotation is Any or annotation is object:
        raise CanonicalCodecError(f"unsupported schema annotation at {path}")
    if annotation is None or annotation is type(None):
        if value is not None:
            raise CanonicalCodecError(f"invalid null at {path}")
        return
    if _is_model_type(annotation):
        _assert_strict_model(annotation)
        if type(value) is not dict:
            raise CanonicalCodecError(f"expected object at {path}")
        fields = annotation.model_fields
        for key, field in fields.items():
            if field.is_required() and key not in value:
                raise CanonicalCodecError(f"schema required field missing at {path}")
        for key in value:
            if type(key) is not str or key not in fields:
                raise CanonicalCodecError(f"schema extra field at {path}")
        for key, item in value.items():
            _raw_schema_preflight(item, fields[key].annotation, path=f"{path}.{key}")
        return

    origin = get_origin(annotation)
    if origin is Annotated:
        _raw_schema_preflight(value, get_args(annotation)[0], path=path)
        return
    if origin is Literal:
        for literal in get_args(annotation):
            if type(literal) is type(value) and value == literal:
                return
        raise CanonicalCodecError(f"invalid literal at {path}")
    if origin in (Union, UnionType):
        for branch in _union_branches(value, annotation):
            try:
                _raw_schema_preflight(value, branch, path=path)
            except CanonicalCodecError:
                continue
            return
        raise CanonicalCodecError(f"invalid union value at {path}")
    if origin in (dict, Mapping):
        if type(value) is not dict:
            raise CanonicalCodecError(f"expected object at {path}")
        args = get_args(annotation)
        if len(args) != 2 or args[0] is not str:
            raise CanonicalCodecError(f"unsupported mapping annotation at {path}")
        for key, item in value.items():
            if type(key) is not str:
                raise CanonicalCodecError(f"mapping key must be a string at {path}")
            _raw_schema_preflight(item, args[1], path=f"{path}.{key}")
        return
    if origin in (list, tuple):
        if type(value) is not list:
            raise CanonicalCodecError(f"expected array at {path}")
        args = get_args(annotation)
        if not args:
            raise CanonicalCodecError(f"untyped array at {path}")
        item_annotations = tuple(argument for argument in args if argument is not Ellipsis)
        if origin is tuple and len(item_annotations) > 1 and len(item_annotations) != len(value):
            raise CanonicalCodecError(f"tuple length mismatch at {path}")
        if origin is tuple and len(args) == 2 and args[1] is Ellipsis:
            item_annotations = (args[0],)
        for index, item in enumerate(value):
            annotation_for_item = item_annotations[index] if len(item_annotations) > 1 else item_annotations[0]
            _raw_schema_preflight(item, annotation_for_item, path=f"{path}[{index}]")
        return
    if annotation is UUID or annotation is datetime:
        if type(value) is not str:
            raise CanonicalCodecError(f"expected canonical string at {path}")
        return
    if annotation is str:
        valid = type(value) is str
    elif annotation is bool:
        valid = type(value) is bool
    elif annotation is int:
        valid = type(value) is int
    elif annotation is float:
        valid = type(value) in (int, float) and not isinstance(value, bool) and isfinite(value)
    else:
        raise CanonicalCodecError(f"unsupported schema annotation at {path}")
    if not valid:
        raise CanonicalCodecError(f"invalid canonical scalar at {path}")


def _coerce_raw_schema(value: Any, annotation: Any, *, path: str = "$") -> Any:
    """Convert canonical JSON spellings to strict Pydantic runtime values."""

    if annotation is None or annotation is type(None):
        return None
    origin = get_origin(annotation)
    if origin is Annotated:
        return _coerce_raw_schema(value, get_args(annotation)[0], path=path)
    if origin is Literal:
        return value
    if origin in (Union, UnionType):
        for branch in _union_branches(value, annotation):
            try:
                _raw_schema_preflight(value, branch, path=path)
                return _coerce_raw_schema(value, branch, path=path)
            except CanonicalCodecError:
                continue
        raise CanonicalCodecError(f"invalid union value at {path}")
    if _is_model_type(annotation):
        return {
            key: _coerce_raw_schema(item, annotation.model_fields[key].annotation, path=f"{path}.{key}")
            for key, item in value.items()
        }
    if origin in (dict, Mapping):
        item_annotation = get_args(annotation)[1]
        return {key: _coerce_raw_schema(item, item_annotation, path=f"{path}.{key}") for key, item in value.items()}
    if origin in (list, tuple):
        args = get_args(annotation)
        item_annotations = tuple(argument for argument in args if argument is not Ellipsis)
        if origin is tuple and len(args) == 2 and args[1] is Ellipsis:
            item_annotations = (args[0],)
        converted = [
            _coerce_raw_schema(
                item,
                item_annotations[index] if len(item_annotations) > 1 else item_annotations[0],
                path=f"{path}[{index}]",
            )
            for index, item in enumerate(value)
        ]
        return tuple(converted) if origin is tuple else converted
    if annotation is UUID:
        try:
            parsed_uuid = UUID(value)
        except (ValueError, AttributeError, TypeError):
            raise CanonicalCodecError(f"invalid UUID at {path}") from None
        if value != str(parsed_uuid):
            raise CanonicalCodecError(f"UUID is not in canonical form at {path}")
        return parsed_uuid
    if annotation is datetime:
        try:
            parsed_datetime = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, AttributeError, TypeError):
            raise CanonicalCodecError(f"invalid datetime at {path}") from None
        if parsed_datetime.tzinfo is None or parsed_datetime.utcoffset() is None:
            raise CanonicalCodecError(f"datetime must be timezone-aware at {path}")
        return parsed_datetime.astimezone(UTC)
    return value


class SafeCanonicalCodec:
    """The sole raw canonical reader for schema-bound contract values.

    Raw values are proven to be exact built-in JSON containers/scalars before
    the trusted registry resolves a model or Pydantic is allowed to validate
    it.  This ordering makes malformed objects, model-copy extras and parser
    bombs side-effect free at the schema/provider boundary.
    """

    def __init__(self, registry: TypedSchemaRegistry) -> None:
        if type(registry) is not TypedSchemaRegistry:
            raise TypeError("SafeCanonicalCodec requires the exact TypedSchemaRegistry type")
        self._registry = registry

    def _prepare_dict(
        self,
        raw: Any,
        *,
        limits: CanonicalReadLimits | None,
    ) -> dict[str, Any]:
        if type(raw) is not dict:
            raise TypeError("canonical raw payload must be an exact dict")
        selected_limits = DEFAULT_CANONICAL_READ_LIMITS if limits is None else limits
        max_bytes, max_depth, max_nodes = _read_limit_values(selected_limits)
        _validate_raw_json_tree(raw, max_depth=max_depth, max_nodes=max_nodes)
        _canonical_raw_bytes(raw, max_bytes=max_bytes)
        return cast(dict[str, Any], raw)

    def _read_validated_dict(
        self,
        raw: dict[str, Any],
        *,
        schema_ref: VersionedRef,
        role: Literal["input", "output"],
    ) -> Any:
        # Both the ref and role are checked only after the complete raw tree;
        # registry resolution and schema generation therefore cannot observe a
        # malformed object.
        reference = _checked_schema_ref(schema_ref)
        checked_role = _checked_role(role)
        try:
            model = self._registry.resolve_model(reference, role=checked_role)
            _raw_schema_preflight(raw, model)
            prepared = _coerce_raw_schema(raw, model)
            self._registry._preflight_validate(reference, role=checked_role, value=prepared)
            parsed = TypeAdapter(model).validate_python(prepared, strict=True)
            canonical_bytes(parsed)
            return parsed
        except CanonicalCodecError:
            raise
        except Exception:
            raise CanonicalCodecError("canonical schema validation failed") from None

    def read_dict(
        self,
        raw: Any,
        *,
        schema_ref: VersionedRef,
        role: Literal["input", "output"],
        limits: CanonicalReadLimits | None = None,
    ) -> Any:
        prepared = self._prepare_dict(raw, limits=limits)
        return self._read_validated_dict(prepared, schema_ref=schema_ref, role=role)

    def read_bytes(
        self,
        payload: Any,
        *,
        expected_hash: str,
        schema_ref: VersionedRef,
        role: Literal["input", "output"],
        limits: CanonicalReadLimits | None = None,
    ) -> Any:
        selected_limits = DEFAULT_CANONICAL_READ_LIMITS if limits is None else limits
        max_bytes, max_depth, max_nodes = _read_limit_values(selected_limits)
        if type(payload) is not bytes:
            raise TypeError("canonical raw payload bytes must use the exact bytes type")
        if len(payload) > max_bytes:
            raise CanonicalCodecError("canonical payload size limit exceeded")
        if type(expected_hash) is not str or not fullmatch(SHA256_PATTERN, expected_hash) or expected_hash == "0" * 64:
            raise TypeError("expected_hash must be a lower-case sha256 digest")
        if sha256(payload).hexdigest() != expected_hash:
            raise CanonicalCodecError("canonical payload hash mismatch")
        try:
            text = payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise CanonicalCodecError("canonical payload is not valid UTF-8") from None
        try:
            raw = loads(
                text,
                object_pairs_hook=_duplicate_key_object,
                parse_constant=_reject_json_constant,
            )
        except CanonicalCodecError:
            raise
        except (JSONDecodeError, RecursionError, ValueError, TypeError):
            raise CanonicalCodecError("canonical payload is not valid JSON") from None
        _validate_raw_json_tree(raw, max_depth=max_depth, max_nodes=max_nodes)
        if type(raw) is not dict:
            raise CanonicalCodecError("canonical JSON root must be an object")
        prepared = cast(dict[str, Any], raw)
        encoded = _canonical_raw_bytes(prepared, max_bytes=max_bytes)
        if payload != encoded:
            raise CanonicalCodecError("canonical payload bytes mismatch")
        return self._read_validated_dict(prepared, schema_ref=schema_ref, role=role)


def _check_raw_request_tenant(raw: dict[str, Any]) -> None:
    """Reject obvious cross-tenant context refs before resolving input schema."""

    meta = raw.get("meta")
    context_refs = raw.get("context_refs")
    if type(meta) is not dict or type(context_refs) is not list:
        return
    request_tenant = meta.get("tenant_id")
    if type(request_tenant) is not str:
        return
    for context_ref in context_refs:
        if type(context_ref) is not dict:
            continue
        context_tenant = context_ref.get("tenant_id")
        if type(context_tenant) is str and context_tenant != request_tenant:
            raise CanonicalCodecError("context_refs tenant mismatch")


def read_canonical_inference_request(
    raw: Any,
    *,
    input_schema_ref: VersionedRef,
    registry: TypedSchemaRegistry,
    limits: CanonicalReadLimits | None = None,
) -> DecodedCanonicalInferenceRequest[BaseModel]:
    """Decode a raw request while preserving context omitted/null/present."""

    codec = SafeCanonicalCodec(registry)
    prepared = codec._prepare_dict(raw, limits=limits)
    _check_raw_request_tenant(prepared)
    reference = _checked_schema_ref(input_schema_ref)
    try:
        input_model = registry.resolve_model(reference, role="input")
        request_model = _parameterize(CanonicalInferenceRequest, input_model)
        _raw_schema_preflight(prepared, request_model)
        coerced = _coerce_raw_schema(prepared, request_model)
        request_adapter = TypeAdapter(request_model)
        SchemaValidator(_safe_preflight_core_schema(request_adapter.core_schema)).validate_python(coerced, strict=True)
        request = request_adapter.validate_python(coerced, strict=True)
        canonical_bytes(request)
    except CanonicalCodecError:
        raise
    except Exception:
        raise CanonicalCodecError("canonical inference request validation failed") from None
    if "context" not in prepared:
        context_state: InferenceContextState = "omitted"
    elif prepared["context"] is None:
        context_state = "null"
    else:
        context_state = "present"
    return DecodedCanonicalInferenceRequest(request=request, context_state=context_state)


def validate_canonical_inference_request(
    request: Any,
    *,
    input_schema_ref: VersionedRef,
    registry: TypedSchemaRegistry,
    limits: CanonicalReadLimits | None = None,
) -> DecodedCanonicalInferenceRequest[BaseModel]:
    """Re-read an already constructed request through the raw trust seam.

    Production inference callers receive a typed object, but ``model_copy``
    and low-level object construction can bypass Pydantic validation.  The
    object is therefore accepted only when its exact instance storage is a
    closed canonical tree; it is then serialized and decoded by the same raw
    reader used for external JSON.  No property, model serializer or Pydantic
    hook is consulted before the storage shape is proven safe.
    """

    selected_limits = DEFAULT_CANONICAL_READ_LIMITS if limits is None else limits
    _, max_depth, max_nodes = _read_limit_values(selected_limits)
    request_model = type(request)
    raw = _safe_model_storage_tree(
        request,
        None,
        path="$",
        max_depth=max_depth,
        max_nodes=max_nodes,
    )
    codec = SafeCanonicalCodec(registry)
    codec._prepare_dict(raw, limits=limits)
    reference = _checked_schema_ref(input_schema_ref)
    input_model = registry.resolve_model(reference, role="input")
    expected_request_model = _parameterize(CanonicalInferenceRequest, input_model)
    if request_model is not expected_request_model:
        raise TypeError("inference request must use the exact registered request type")
    _safe_model_storage_tree(request, expected_request_model, path="$")
    return read_canonical_inference_request(
        raw,
        input_schema_ref=reference,
        registry=registry,
        limits=limits,
    )


def _safe_model_storage_tree(
    value: Any,
    expected: Any,
    *,
    path: str,
    max_depth: int | None = None,
    max_nodes: int | None = None,
) -> Any:
    if (max_depth is None) is not (max_nodes is None):
        raise TypeError("storage projection limits must be provided together")
    remaining_nodes = [max_nodes] if max_nodes is not None else None
    try:
        return _safe_model_storage_value(
            value,
            expected,
            path=path,
            depth=0,
            max_depth=max_depth,
            remaining_nodes=remaining_nodes,
        )
    except (MemoryError, RecursionError):
        raise CanonicalCodecError("canonical payload traversal failed") from None


def _safe_model_storage_value(
    value: Any,
    expected: Any,
    *,
    path: str,
    depth: int,
    max_depth: int | None,
    remaining_nodes: list[int] | None,
) -> Any:
    if remaining_nodes is not None:
        remaining = remaining_nodes[0]
        remaining -= 1
        remaining_nodes[0] = remaining
        if remaining < 0:
            raise CanonicalCodecError("canonical payload node limit exceeded")
    if expected is None:
        runtime_type = type(value)
        if _is_model_type(runtime_type):
            if max_depth is not None and depth >= max_depth:
                raise CanonicalCodecError("canonical payload depth limit exceeded")
            try:
                storage, extras, fields_set = _read_base_model_storage(value, runtime_type)
            except (AttributeError, TypeError):
                raise TypeError(f"invalid model storage at {path}") from None
            if type(storage) is not dict or type(fields_set) is not set:
                raise TypeError(f"invalid model storage at {path}")
            if extras is not None and (type(extras) is not dict or extras):
                raise TypeError(f"invalid model extras at {path}")
            if any(type(key) is not str for key in storage) or any(type(name) is not str for name in fields_set):
                raise TypeError(f"invalid model storage keys at {path}")
            try:
                runtime_fields = _read_model_field_catalog(runtime_type)
            except TypeError:
                raise TypeError(f"invalid model field catalog at {path}") from None
            if not fields_set.issubset(storage) or not set(storage).issubset(runtime_fields):
                raise ValueError(f"model storage contains unvalidated fields at {path}")
            return {
                name: _safe_model_storage_value(
                    storage[name],
                    None,
                    path=f"{path}.{name}",
                    depth=depth + 1,
                    max_depth=max_depth,
                    remaining_nodes=remaining_nodes,
                )
                for name in fields_set
            }
        if runtime_type is tuple:
            if max_depth is not None and depth >= max_depth:
                raise CanonicalCodecError("canonical payload depth limit exceeded")
            return [
                _safe_model_storage_value(
                    item,
                    None,
                    path=f"{path}[{index}]",
                    depth=depth + 1,
                    max_depth=max_depth,
                    remaining_nodes=remaining_nodes,
                )
                for index, item in enumerate(value)
            ]
        if runtime_type is list:
            if max_depth is not None and depth >= max_depth:
                raise CanonicalCodecError("canonical payload depth limit exceeded")
            return [
                _safe_model_storage_value(
                    item,
                    None,
                    path=f"{path}[{index}]",
                    depth=depth + 1,
                    max_depth=max_depth,
                    remaining_nodes=remaining_nodes,
                )
                for index, item in enumerate(value)
            ]
        if runtime_type is dict:
            if max_depth is not None and depth >= max_depth:
                raise CanonicalCodecError("canonical payload depth limit exceeded")
            if any(type(key) is not str for key in value):
                raise TypeError(f"canonical mapping keys must be exact strings at {path}")
            return {
                key: _safe_model_storage_value(
                    item,
                    None,
                    path=f"{path}.{key}",
                    depth=depth + 1,
                    max_depth=max_depth,
                    remaining_nodes=remaining_nodes,
                )
                for key, item in value.items()
            }
        if runtime_type is UUID:
            return str(value).lower()
        if runtime_type is datetime:
            if type(value.tzinfo) is not type(UTC):
                raise TypeError(f"unexpected datetime timezone at {path}")
            timespec = "microseconds" if value.microsecond else "seconds"
            return value.isoformat(timespec=timespec).replace("+00:00", "Z")
        if runtime_type in (str, int, float, bool) or value is None:
            return value
        raise TypeError(f"unsupported inference request value at {path}")
    origin = get_origin(expected)
    arguments = get_args(expected)
    if _is_model_type(expected):
        if type(value) is not expected:
            raise TypeError(f"unexpected model type at {path}")
        storage, extras, fields_set = _read_base_model_storage(value, expected)
        fields = _read_model_field_catalog(expected)
        if type(storage) is not dict or type(fields_set) is not set:
            raise TypeError(f"invalid model storage at {path}")
        if extras is not None and (type(extras) is not dict or extras):
            raise TypeError(f"invalid model extras at {path}")
        if any(type(key) is not str for key in storage) or any(type(name) is not str for name in fields_set):
            raise TypeError(f"invalid model storage keys at {path}")
        if not fields_set.issubset(storage) or not set(storage).issubset(fields):
            raise ValueError(f"model storage contains unvalidated fields at {path}")
        return {
            name: _safe_model_storage_value(
                storage[name],
                fields[name].annotation,
                path=f"{path}.{name}",
                depth=depth + 1,
                max_depth=max_depth,
                remaining_nodes=remaining_nodes,
            )
            for name in fields_set
        }
    if origin is Annotated:
        return _safe_model_storage_value(
            value,
            arguments[0],
            path=path,
            depth=depth,
            max_depth=max_depth,
            remaining_nodes=remaining_nodes,
        )
    if origin in (Union, UnionType):
        for branch in _union_branches(value, expected):
            try:
                return _safe_model_storage_value(
                    value,
                    branch,
                    path=path,
                    depth=depth,
                    max_depth=max_depth,
                    remaining_nodes=remaining_nodes,
                )
            except (TypeError, ValueError):
                continue
        raise TypeError(f"no exact union branch at {path}")
    if origin in (tuple, list):
        expected_type = tuple if origin is tuple else list
        if type(value) is not expected_type:
            raise TypeError(f"unexpected sequence type at {path}")
        item_schema = arguments[0] if arguments else Any
        if origin is tuple and len(arguments) > 1 and arguments[-1] is not Ellipsis:
            if len(value) != len(arguments):
                raise ValueError(f"tuple length mismatch at {path}")
            return [
                _safe_model_storage_value(
                    item,
                    item_expected,
                    path=f"{path}[{index}]",
                    depth=depth + 1,
                    max_depth=max_depth,
                    remaining_nodes=remaining_nodes,
                )
                for index, (item, item_expected) in enumerate(zip(value, arguments, strict=True))
            ]
        return [
            _safe_model_storage_value(
                item,
                item_schema,
                path=f"{path}[{index}]",
                depth=depth + 1,
                max_depth=max_depth,
                remaining_nodes=remaining_nodes,
            )
            for index, item in enumerate(value)
        ]
    if origin in (dict, Mapping):
        if type(value) is not dict:
            raise TypeError(f"unexpected mapping type at {path}")
        key_schema, item_schema = arguments
        if key_schema is not str or any(type(key) is not str for key in value):
            raise TypeError(f"canonical mapping keys must be exact strings at {path}")
        return {
            key: _safe_model_storage_value(
                item,
                item_schema,
                path=f"{path}.{key}",
                depth=depth + 1,
                max_depth=max_depth,
                remaining_nodes=remaining_nodes,
            )
            for key, item in value.items()
        }
    if expected is UUID:
        if type(value) is not UUID:
            raise TypeError(f"unexpected UUID type at {path}")
        return str(value).lower()
    if expected is datetime:
        if type(value) is not datetime or type(value.tzinfo) is not type(UTC):
            raise TypeError(f"unexpected datetime type at {path}")
        utc_value = value.astimezone(UTC)
        timespec = "microseconds" if utc_value.microsecond else "seconds"
        return utc_value.isoformat(timespec=timespec).replace("+00:00", "Z")
    if expected is type(None):
        if value is not None:
            raise TypeError(f"unexpected null type at {path}")
        return None
    if expected in (str, int, float, bool):
        if type(value) is not expected:
            raise TypeError(f"unexpected scalar type at {path}")
        return value
    if get_origin(expected) is Literal:
        if type(value) not in (str, int, bool) or value not in arguments:
            raise TypeError(f"unexpected literal at {path}")
        return value
    raise TypeError(f"unsupported inference request annotation at {path}")


def _ensure_canonical_serializable(value: Any) -> Any:
    try:
        canonical_bytes(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("validated schema value is not canonically serializable") from exc
    return value


def _manifest_schema_for_payload(
    payload: Any,
    manifest: Any,
    *,
    expected_manifest_hash: str,
) -> tuple[VersionedRef | None, VersionedRef | None]:
    if not _is_runtime_manifest(manifest):
        raise TypeError("manifest must be a validated SkillRuntimeManifest")
    invoke_runtime_manifest_verifier(manifest, expected_manifest_hash=expected_manifest_hash)
    kind = payload.get("kind") if isinstance(payload, Mapping) else payload.kind
    if kind == "final_answer":
        return None, manifest.output_schema_ref
    if kind == "tool_proposal":
        tool_ref = payload.get("tool_ref") if isinstance(payload, Mapping) else payload.tool_ref
        if isinstance(tool_ref, Mapping):
            tool_ref = VersionedRef.model_validate(tool_ref)
        binding = manifest.find_tool_binding(tool_ref)
        return binding.input_schema_ref, None
    if kind in {"knowledge_proposal", "action_proposal", "delegate_proposal"}:
        # Knowledge proposals are schema-free in WS-1.  Optional Action and
        # delegation runtimes have no input-schema binding here and therefore
        # fail closed in the callers before they can become trusted objects.
        return None, None
    raise ValueError(f"unknown decision payload kind: {kind!r}")


def _require_manifest_registry(
    manifest: Any,
    schema_registry: Any,
    *,
    expected_manifest_hash: str,
) -> None:
    """Require the sole concrete trust-boundary objects before schema use."""

    if not _is_runtime_manifest(manifest):
        raise TypeError("schema-bearing contracts require an exact SkillRuntimeManifest")
    if type(schema_registry) is not TypedSchemaRegistry:
        raise TypeError("schema-bearing contracts require an exact TypedSchemaRegistry")
    # The registered verifier recomputes the Manifest hash from immutable data;
    # it is not allowed to rely on an overridable instance method.
    invoke_runtime_manifest_verifier(manifest, expected_manifest_hash=expected_manifest_hash)


_PAYLOAD_TYPES = (
    FinalAnswerPayload,
    KnowledgeProposalPayload,
    ToolProposalPayload,
    ActionProposalPayload,
    DelegateProposalPayload,
)
_DECISION_TYPES = (FinalAnswer, KnowledgeProposal, ToolProposal, ActionProposal, DelegateProposal)


def _is_exact_canonical_instance(value: Any, allowed: tuple[type[Any], ...]) -> bool:
    value_type = type(value)
    if value_type in allowed:
        return True
    # Pydantic creates a distinct, non-subclassable-in-practice specialized
    # class for ``ToolProposalPayload[Input]``/friends.  Its generic metadata
    # points directly at the canonical origin; a user subclass has ``origin``
    # set to ``None`` and is therefore rejected.
    try:
        metadata = value_type.__pydantic_generic_metadata__
    except AttributeError:
        metadata = {}
    return metadata.get("origin") in allowed


def _payload_schema_types(payload: Any) -> tuple[type[BaseModel] | None, type[BaseModel] | None]:
    """Return the concrete nested schema classes carried by a payload instance."""

    if not _is_exact_canonical_instance(payload, _PAYLOAD_TYPES + _DECISION_TYPES):
        raise TypeError("decision instance is not a canonical payload or decision class")
    kind = payload.kind
    if kind == "knowledge_proposal":
        return None, None
    if kind not in {"final_answer", "tool_proposal", "action_proposal", "delegate_proposal"}:
        raise ValueError(f"unknown decision payload kind: {kind!r}")
    field_name = "output" if kind == "final_answer" else "input"
    field = type(payload).model_fields.get(field_name)
    if field is None:
        raise TypeError("decision payload has no typed schema field")
    return (_schema_model(field.annotation), None) if field_name == "input" else (None, _schema_model(field.annotation))


def _revalidate_payload_instance(
    payload: Any,
    *,
    manifest: Any | None,
    schema_registry: Any | None,
    expected_manifest_hash: str,
) -> InferenceDecisionPayload:
    input_type, output_type = _payload_schema_types(payload)
    if input_type is not None and type(payload.input) is not input_type:
        raise ValueError("payload input instance does not match its declared strict schema class")
    if output_type is not None and type(payload.output) is not output_type:
        raise ValueError("payload output instance does not match its declared strict schema class")
    if payload.kind != "knowledge_proposal":
        expected_hash = _validate_expected_manifest_hash(expected_manifest_hash)
        _require_manifest_registry(manifest, schema_registry, expected_manifest_hash=expected_hash)
        registry = cast(TypedSchemaRegistry, schema_registry)
        input_ref, output_ref = _manifest_schema_for_payload(
            payload.model_dump(mode="python", exclude_unset=False),
            manifest,
            expected_manifest_hash=expected_hash,
        )
        if input_ref is not None:
            registered_input = registry.resolve_model(input_ref, role="input")
            if input_type is not registered_input:
                raise ValueError("payload input schema does not match the exact Manifest registry binding")
            input_type = registered_input
        if output_ref is not None:
            registered_output = registry.resolve_model(output_ref, role="output")
            if output_type is not registered_output:
                raise ValueError("payload output schema does not match the exact Manifest registry binding")
            output_type = registered_output
        if payload.kind == "final_answer" and output_ref is None:
            raise ValueError("final_answer requires an exact Manifest output schema reference")
        if payload.kind in {"tool_proposal", "action_proposal", "delegate_proposal"} and input_ref is None:
            raise ValueError(f"{payload.kind} requires an exact Manifest input schema reference")
    raw = payload.model_dump(mode="python", exclude_unset=False)
    _reject_untrusted_fields(raw.get("output"))
    _reject_untrusted_fields(raw.get("input"))
    kind = raw.get("kind")
    if kind == "final_answer" and output_type is None:
        raise ValueError("final_answer requires an exact output schema registry binding")
    if kind in {"tool_proposal", "action_proposal", "delegate_proposal"} and input_type is None:
        raise ValueError(f"{kind} requires an exact input schema registry binding")
    return cast(
        InferenceDecisionPayload,
        _ensure_canonical_serializable(_decision_adapter(input_type, output_type).validate_python(raw)),
    )


def parse_inference_decision(
    payload: Any,
    *,
    input_type: Any | None = None,
    output_type: Any | None = None,
    manifest: Any | None = None,
    schema_registry: Any | None = None,
    expected_manifest_hash: str,
) -> InferenceDecisionPayload:
    """Parse a model payload using the exact typed schema bound by a Manifest.

    Raw execution envelopes and schema-bearing typed instances must carry an
    exact Manifest registry binding; there is no design-time ``BaseModel`` or
    private compatibility fallback.
    """

    if isinstance(payload, BaseModel):
        if not _is_exact_canonical_instance(payload, _PAYLOAD_TYPES):
            raise TypeError("decision payload must be an exact canonical payload class")
        if cast(Any, payload).kind == "knowledge_proposal":
            raise TypeError("schema-free KnowledgeProposal requires parse_knowledge_proposal")
        return _revalidate_payload_instance(
            payload,
            manifest=manifest,
            schema_registry=schema_registry,
            expected_manifest_hash=expected_manifest_hash,
        )
    raw = payload
    if not isinstance(raw, Mapping):
        raise TypeError("decision payload must be a mapping or validated payload model")
    # Decision metadata is injected by the trusted node and legitimately
    # carries tenant/authorization identity.  Only model-controlled nested
    # payloads are subject to the untrusted-field guard here.
    _reject_untrusted_fields(raw.get("output"))
    _reject_untrusted_fields(raw.get("input"))
    kind = raw.get("kind")
    if kind == "knowledge_proposal":
        raise TypeError("schema-free KnowledgeProposal requires parse_knowledge_proposal")
    expected_hash = _validate_expected_manifest_hash(expected_manifest_hash)
    _require_manifest_registry(manifest, schema_registry, expected_manifest_hash=expected_hash)
    input_ref, output_ref = _manifest_schema_for_payload(raw, manifest, expected_manifest_hash=expected_hash)
    if input_ref is not None:
        resolved_input = _resolve_schema_model(schema_registry, input_ref, role="input")
        if input_type is not None and input_type is not resolved_input:
            raise ValueError("input schema type is not the exact Manifest registry binding")
        input_type = resolved_input
    if output_ref is not None:
        resolved_output = _resolve_schema_model(schema_registry, output_ref, role="output")
        if output_type is not None and output_type is not resolved_output:
            raise ValueError("output schema type is not the exact Manifest registry binding")
        output_type = resolved_output
    if kind == "final_answer" and output_type is None:
        raise ValueError("final_answer requires an exact output schema registry binding")
    if kind == "final_answer" and output_ref is None:
        raise ValueError("final_answer requires an exact Manifest output schema reference")
    if kind in {"tool_proposal", "action_proposal", "delegate_proposal"} and input_type is None:
        raise ValueError(f"{kind} requires an exact input schema registry binding")
    if kind in {"tool_proposal", "action_proposal", "delegate_proposal"} and input_ref is None:
        raise ValueError(f"{kind} requires an exact Manifest input schema reference")
    # A raw mapping must use a strict model on every custom payload branch.
    # KnowledgeProposal has no caller-supplied nested model and remains a
    # fully closed CanonicalModel branch.
    return cast(
        InferenceDecisionPayload,
        _ensure_canonical_serializable(_decision_adapter(input_type, output_type).validate_python(raw)),
    )


def parse_knowledge_proposal(payload: Any) -> KnowledgeProposalPayload:
    """Parse the schema-free KnowledgeProposal payload in its own seam.

    Knowledge retrieval is the only WS-1 decision branch without a typed
    execution input/output schema.  This entrypoint intentionally accepts no
    Manifest, registry, or expected hash and rejects every other discriminator
    before validation, so it cannot become a Tool/Final/Action/Delegate
    compatibility path.
    """

    raw: Any
    if isinstance(payload, BaseModel):
        if not _is_exact_canonical_instance(payload, (KnowledgeProposalPayload,)):
            raise TypeError("knowledge proposal must be an exact KnowledgeProposalPayload")
        raw = payload.model_dump(mode="python", exclude_unset=False)
    elif isinstance(payload, Mapping):
        raw = payload
    else:
        raise TypeError("knowledge proposal must be a mapping or validated payload model")
    if raw.get("kind") != "knowledge_proposal":
        raise ValueError("knowledge proposal discriminator is required")
    return cast(
        KnowledgeProposalPayload,
        _ensure_canonical_serializable(KnowledgeProposalPayload.model_validate(raw)),
    )


def parse_canonical_decision(
    payload: Any,
    *,
    input_type: Any | None = None,
    output_type: Any | None = None,
    manifest: Any | None = None,
    schema_registry: Any | None = None,
    expected_manifest_hash: str,
) -> CanonicalDecision:
    """Read an enriched Decision without erasing its concrete payload model."""

    declared_input: type[BaseModel] | None = None
    declared_output: type[BaseModel] | None = None
    raw: Any
    if isinstance(payload, BaseModel):
        if not _is_exact_canonical_instance(payload, _DECISION_TYPES):
            raise TypeError("decision model must be an exact canonical decision class")
        raw = payload.model_dump(mode="python", exclude_unset=False)
        declared_input, declared_output = _payload_schema_types(payload)
        if cast(Any, payload).kind == "knowledge_proposal":
            raise TypeError("schema-free KnowledgeProposal requires parse_knowledge_decision")
        canonical_payload = cast(Any, payload)
        if declared_input is not None and type(canonical_payload.input) is not declared_input:
            raise ValueError("decision input instance does not match its declared strict schema class")
        if declared_output is not None and type(canonical_payload.output) is not declared_output:
            raise ValueError("decision output instance does not match its declared strict schema class")
    else:
        raw = payload
    if not isinstance(raw, Mapping):
        raise TypeError("decision must be a mapping or validated decision model")
    # ``meta`` is trusted node metadata and may carry tenant identity.  Only
    # the model-controlled input/output objects are untrusted at this seam.
    _reject_untrusted_fields(raw.get("output"))
    _reject_untrusted_fields(raw.get("input"))
    kind = raw.get("kind")
    if kind == "knowledge_proposal":
        raise TypeError("schema-free KnowledgeProposal requires parse_knowledge_decision")
    expected_hash = _validate_expected_manifest_hash(expected_manifest_hash)
    _require_manifest_registry(manifest, schema_registry, expected_manifest_hash=expected_hash)
    input_ref, output_ref = _manifest_schema_for_payload(raw, manifest, expected_manifest_hash=expected_hash)
    if input_ref is not None:
        resolved_input = _resolve_schema_model(schema_registry, input_ref, role="input")
        if declared_input is not None and declared_input is not resolved_input:
            raise ValueError("decision input schema does not match the exact Manifest registry binding")
        if input_type is not None and input_type is not resolved_input:
            raise ValueError("input schema type is not the exact Manifest registry binding")
        input_type = resolved_input
    if output_ref is not None:
        resolved_output = _resolve_schema_model(schema_registry, output_ref, role="output")
        if declared_output is not None and declared_output is not resolved_output:
            raise ValueError("decision output schema does not match the exact Manifest registry binding")
        if output_type is not None and output_type is not resolved_output:
            raise ValueError("output schema type is not the exact Manifest registry binding")
        output_type = resolved_output
    if kind == "final_answer" and output_type is None:
        raise ValueError("final_answer requires an exact output schema registry binding")
    if kind == "final_answer" and output_ref is None:
        raise ValueError("final_answer requires an exact Manifest output schema reference")
    if kind in {"tool_proposal", "action_proposal", "delegate_proposal"} and input_type is None:
        raise ValueError(f"{kind} requires an exact input schema registry binding")
    if kind in {"tool_proposal", "action_proposal", "delegate_proposal"} and input_ref is None:
        raise ValueError(f"{kind} requires an exact Manifest input schema reference")
    decision_generics: dict[str, Any] = {
        "final_answer": FinalAnswer,
        "tool_proposal": ToolProposal,
    }
    if not isinstance(kind, str):
        raise ValueError(f"unknown decision kind: {kind!r}")
    decision_generic = decision_generics.get(kind)
    if decision_generic is None:
        raise ValueError(f"unknown decision kind: {kind!r}")
    schema_type = output_type if kind == "final_answer" else input_type
    return cast(
        CanonicalDecision,
        _ensure_canonical_serializable(
            TypeAdapter(_parameterize(decision_generic, _schema_model(schema_type))).validate_python(raw)
        ),
    )


def parse_knowledge_decision(payload: Any) -> KnowledgeProposal:
    """Parse an enriched schema-free KnowledgeProposal without Manifest input."""

    raw: Any
    if isinstance(payload, BaseModel):
        if not _is_exact_canonical_instance(payload, (KnowledgeProposal,)):
            raise TypeError("knowledge decision must be an exact KnowledgeProposal")
        raw = payload.model_dump(mode="python", exclude_unset=False)
    elif isinstance(payload, Mapping):
        raw = payload
    else:
        raise TypeError("knowledge decision must be a mapping or validated decision model")
    if raw.get("kind") != "knowledge_proposal":
        raise ValueError("knowledge decision discriminator is required")
    return cast(
        KnowledgeProposal,
        _ensure_canonical_serializable(KnowledgeProposal.model_validate(raw)),
    )


def parse_ui_projection_payload(payload: Any) -> UIProjectionPayload:
    return TypeAdapter(UIProjectionPayload).validate_python(payload)


class UnknownContractError(ValueError):
    """No explicit reader exists for a contract family/version."""


UnknownContractVersionError = UnknownContractError


class ContractReaderRegistry:
    """Closed registry keyed by exact contract family and version."""

    def __init__(self) -> None:
        self._readers: dict[tuple[str, str], Any] = {}

    @property
    def versions(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self._readers))

    def register(self, name: str, version: str, reader: Any) -> None:
        key = (_validate_ref(name, "contract name"), _validate_ref(version, "contract version"))
        if key in self._readers:
            raise ValueError(f"contract reader already registered: {key}")
        self._readers[key] = reader

    def read(self, name: str, version: str, payload: Any) -> Any:
        reader = self._readers.get((name, version))
        if reader is None:
            raise UnknownContractError(f"unknown contract reader: {name}@{version}")
        try:
            try:
                validate_python = cast(Any, reader).validate_python
            except AttributeError:
                return reader(payload)
            return validate_python(payload)
        except Exception as exc:
            try:
                preserve_contract_error = cast(Any, exc).preserve_contract_error
            except AttributeError:
                preserve_contract_error = False
            if preserve_contract_error:
                raise
            raise ValueError(f"invalid {name}@{version} payload") from exc


CONTRACT_READER_REGISTRY = ContractReaderRegistry()
CONTRACT_READER_REGISTRY.register("ui_projection", "v1", TypeAdapter(UIProjectionPayload))

_ABI_UNKNOWN_ERROR_FACTORY: Any | None = None
_RUNTIME_MANIFEST_TYPE: type[Any] | None = None
_RUNTIME_MANIFEST_VERIFIER: Any | None = None
_TOOL_COMMAND_BUILDER: Any | None = None


def register_abi_unknown_error(error_type: Any) -> None:
    """Install the ABI package's typed unknown-version error without a reverse import."""

    global _ABI_UNKNOWN_ERROR_FACTORY
    _ABI_UNKNOWN_ERROR_FACTORY = error_type


def register_runtime_manifest_type(manifest_type: type[Any]) -> None:
    """Register the one concrete SkillRuntimeManifest class at the seam.

    ``contracts`` must not import ``skill_abi`` in reverse.  The ABI package
    registers its concrete model during import, allowing readers to reject
    look-alike objects without duck typing or a lazy import escape hatch.
    """

    global _RUNTIME_MANIFEST_TYPE
    if _RUNTIME_MANIFEST_TYPE is not None and _RUNTIME_MANIFEST_TYPE is not manifest_type:
        raise ValueError("runtime Manifest type is already registered")
    _RUNTIME_MANIFEST_TYPE = manifest_type


def _is_runtime_manifest(value: Any) -> bool:
    return _RUNTIME_MANIFEST_TYPE is not None and type(value) is _RUNTIME_MANIFEST_TYPE


def register_runtime_manifest_verifier(verifier: Any) -> None:
    """Install the ABI package's authoritative data-only Manifest verifier."""

    global _RUNTIME_MANIFEST_VERIFIER
    if _RUNTIME_MANIFEST_VERIFIER is not None and _RUNTIME_MANIFEST_VERIFIER is not verifier:
        raise ValueError("runtime Manifest verifier is already registered")
    _RUNTIME_MANIFEST_VERIFIER = verifier


def invoke_runtime_manifest_verifier(manifest: Any, *, expected_manifest_hash: str) -> None:
    if _RUNTIME_MANIFEST_VERIFIER is None:
        raise RuntimeError("Skill ABI runtime Manifest verifier is not registered")
    if not _is_runtime_manifest(manifest):
        raise TypeError("runtime Manifest verifier requires the exact SkillRuntimeManifest type")
    expected = _validate_expected_manifest_hash(expected_manifest_hash)
    _RUNTIME_MANIFEST_VERIFIER(manifest, expected_manifest_hash=expected)


def register_tool_command_builder(builder: Any) -> None:
    """Install the ABI package's concrete ToolCommand builder.

    This inversion keeps the dependency direction one-way while preserving a
    stable contract-package import for existing callers.  The installed
    builder performs all concrete Manifest and artifact verification.
    """

    global _TOOL_COMMAND_BUILDER
    if _TOOL_COMMAND_BUILDER is not None and _TOOL_COMMAND_BUILDER is not builder:
        raise ValueError("ToolCommand builder is already registered")
    _TOOL_COMMAND_BUILDER = builder


def invoke_tool_command_builder(*args: Any, **kwargs: Any) -> Any:
    if _TOOL_COMMAND_BUILDER is None:
        raise RuntimeError("Skill ABI ToolCommand builder is not registered")
    return _TOOL_COMMAND_BUILDER(*args, **kwargs)


def register_contract_reader(name: str, version: str, reader: Any) -> None:
    CONTRACT_READER_REGISTRY.register(name, version, reader)


def read_contract(
    name: str,
    payload: Any,
    *,
    version: str | None = None,
    input_type: Any | None = None,
    output_type: Any | None = None,
    manifest: Any | None = None,
    schema_registry: Any | None = None,
    expected_manifest_hash: str,
) -> Any:
    """Read only an explicitly registered contract version; never use latest."""

    if version is None:
        if not isinstance(payload, dict) or not isinstance(payload.get("contract_version"), str):
            if name == "skill_execution_spec" and isinstance(payload, dict):
                version = str(payload.get("abi_version", ""))
            else:
                raise UnknownContractError("contract version is required")
        else:
            version = str(payload["contract_version"])
    if name == "inference.payload":
        if version != "v1":
            raise UnknownContractError(f"unknown contract reader: {name}@{version}")
        if isinstance(payload, BaseModel) and (manifest is None or type(schema_registry) is not TypedSchemaRegistry):
            raise TypeError("typed inference payloads require an exact Manifest and TypedSchemaRegistry")
        return parse_inference_decision(
            payload,
            input_type=input_type,
            output_type=output_type,
            manifest=manifest,
            schema_registry=schema_registry,
            expected_manifest_hash=expected_manifest_hash,
        )
    if name == "decision":
        if version != "v1":
            raise UnknownContractError(f"unknown contract reader: {name}@{version}")
        if isinstance(payload, BaseModel) and (manifest is None or type(schema_registry) is not TypedSchemaRegistry):
            raise TypeError("typed decisions require an exact Manifest and TypedSchemaRegistry")
        return parse_canonical_decision(
            payload,
            input_type=input_type,
            output_type=output_type,
            manifest=manifest,
            schema_registry=schema_registry,
            expected_manifest_hash=expected_manifest_hash,
        )
    if name == "skill_execution_spec":
        payload_version = payload.get("abi_version") if isinstance(payload, Mapping) else None
        if version != payload_version:
            raise UnknownContractError(f"ABI version mismatch: requested {version}, payload {payload_version}")
        if (name, version) not in CONTRACT_READER_REGISTRY.versions and _ABI_UNKNOWN_ERROR_FACTORY is not None:
            raise _ABI_UNKNOWN_ERROR_FACTORY(f"unknown ABI converter: {version!r}")
    return CONTRACT_READER_REGISTRY.read(name, version, payload)


__all__ = [
    "ActionProposal",
    "ActionProposalPayload",
    "ArtifactRef",
    "CanonicalDecision",
    "CanonicalBaseModel",
    "CanonicalContract",
    "CanonicalFailure",
    "CanonicalInferenceRequest",
    "CanonicalInferenceResult",
    "CanonicalCodecError",
    "CanonicalReadLimits",
    "CanonicalMessage",
    "CanonicalModel",
    "CheckpointRef",
    "Citation",
    "ContractMeta",
    "derive_contract_meta",
    "ContractReaderRegistry",
    "CONTRACT_READER_REGISTRY",
    "DecodedCanonicalInferenceRequest",
    "DEFAULT_CANONICAL_READ_LIMITS",
    "DelegateProposal",
    "DelegateProposalPayload",
    "DomainViewAccepted",
    "EvaluationEvidenceRef",
    "FinalAnswer",
    "FinalAnswerPayload",
    "InferenceDecisionPayload",
    "InferenceBudget",
    "InferenceContext",
    "InteractionItem",
    "InteractionResolved",
    "InterruptRef",
    "KnowledgeItem",
    "KnowledgeProposal",
    "KnowledgeProposalPayload",
    "KnowledgeFilter",
    "KnowledgeRequest",
    "KnowledgeResult",
    "MessageCompleted",
    "MessageDelta",
    "MessageStarted",
    "ProjectionSourceRef",
    "ResolvedInferenceRetryPolicy",
    "ResolvedModelPolicy",
    "RetrievalBudget",
    "ModelUsage",
    "RunStatusChanged",
    "RuntimeEvent",
    "ToolCommand",
    "ToolProposal",
    "ToolProposalPayload",
    "ToolResult",
    "ToolResultProvenance",
    "TraceRef",
    "TypedSchemaRegistry",
    "SchemaRegistry",
    "SafeCanonicalCodec",
    "StructuredInferenceInput",
    "StructuredInferenceOutput",
    "SUPPORTED_CONTRACT_VERSIONS",
    "UIProjectionEvent",
    "UIProjectionPayload",
    "UnknownContractError",
    "UnknownContractVersionError",
    "VersionedRef",
    "canonical_bytes",
    "canonical_hash",
    "canonical_json_bytes",
    "canonical_json_hash",
    "parse_canonical_decision",
    "parse_knowledge_decision",
    "parse_knowledge_proposal",
    "parse_inference_decision",
    "parse_ui_projection_payload",
    "read_canonical_inference_request",
    "read_contract",
    "register_abi_unknown_error",
    "register_contract_reader",
]
