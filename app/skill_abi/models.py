"""Typed Skill Runtime Manifest and SkillExecutionSpec ABI models."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from re import fullmatch
from typing import Any, Literal
from uuid import UUID

from pydantic import AliasChoices, Field, field_validator, model_validator

from app.contracts.canonical import (
    CanonicalModel,
    VersionedRef,
    _unique_sorted,
    _validate_aware_utc,
    _validate_hash,
    _validate_identifier,
    _validate_ref,
    register_runtime_manifest_type,
)

CAPABILITY_PATTERN = r"^[a-z][a-z0-9_.\-]{0,63}$"
HASH_PATTERN = r"^[0-9a-f]{64}$"
ABI_VERSIONS = ("v1", "v2")
RUN_MODES = ("live", "replay", "fork_dry_run", "fork_commit")


def _same_exact_ref(left: VersionedRef, right: str | VersionedRef) -> bool:
    if isinstance(right, VersionedRef):
        return left.ref == right.ref and left.version == right.version and left.content_hash == right.content_hash
    return left.ref == right


class ABIModel(CanonicalModel):
    """Marker base kept separate to make static boundary checks straightforward."""

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Use the canonical field-set profile for ABI transport by default."""

        kwargs.setdefault("exclude_unset", True)
        return super().model_dump(*args, **kwargs)

    def model_dump_json(self, *args: Any, **kwargs: Any) -> str:
        """Keep JSON transport aligned with canonical hashing field presence."""

        kwargs.setdefault("exclude_unset", True)
        return super().model_dump_json(*args, **kwargs)


class PolicyRef(ABIModel):
    kind: Literal[
        "prompt",
        "model",
        "inference_retry",
        "knowledge",
        "memory",
        "workspace",
        "routing",
        "context",
        "run_mode",
        "redaction",
        "experience",
    ]
    policy: VersionedRef


class GraphBinding(ABIModel):
    graph: VersionedRef
    graph_state_schema_version: str = Field(min_length=1, max_length=128)

    _version = field_validator("graph_state_schema_version")(lambda value: _validate_ref(value, "graph state version"))


class ContractBinding(ABIModel):
    contracts: VersionedRef
    converter_bundle: VersionedRef | None = None


class MonotonicInputSubsetBinding(ABIModel):
    limit_schema_ref: str = Field(min_length=1, max_length=256)
    changed_limit_keys: tuple[str, ...] = Field(min_length=1, max_length=64)
    comparator: Literal["positive_integer_componentwise_lte"]
    resolver_attestation_hash: str = Field(pattern=HASH_PATTERN)

    _schema = field_validator("limit_schema_ref")(lambda value: _validate_ref(value, "limit schema"))
    _attestation = field_validator("resolver_attestation_hash")(
        lambda value: _validate_hash(value, "resolver attestation hash")
    )
    _keys = field_validator("changed_limit_keys")(_unique_sorted)


class BudgetBinding(ABIModel):
    evaluation_envelope: VersionedRef
    effective_budget: VersionedRef
    input_subset: MonotonicInputSubsetBinding | None = None


class PermissionBinding(ABIModel):
    run_authority_ref: str = Field(min_length=1, max_length=256)
    run_authority_hash: str = Field(pattern=HASH_PATTERN)
    authorization_policy: VersionedRef
    permission_preset: VersionedRef
    permission_envelope_hash: str = Field(pattern=HASH_PATTERN)
    effective_scopes: tuple[str, ...] = Field(max_length=256)

    _authority = field_validator("run_authority_ref")(lambda value: _validate_ref(value, "run authority"))
    _authority_hash = field_validator("run_authority_hash")(lambda value: _validate_hash(value, "run authority hash"))
    _envelope = field_validator("permission_envelope_hash")(
        lambda value: _validate_hash(value, "permission envelope hash")
    )
    _scopes = field_validator("effective_scopes")(_unique_sorted)

    @field_validator("effective_scopes")
    @classmethod
    def validate_scope_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for scope in value:
            _validate_identifier(scope, "permission scope")
        return value


class ToolBinding(ABIModel):
    tool_ref: VersionedRef = Field(validation_alias=AliasChoices("tool_ref", "tool"))
    operation: str = Field(min_length=1, max_length=128)
    resource_type: str = Field(min_length=1, max_length=128)
    effect_class: Literal["pure", "read", "workspace_local"]
    input_schema_ref: VersionedRef = Field(validation_alias=AliasChoices("input_schema_ref", "input_schema"))
    output_schema_ref: VersionedRef = Field(validation_alias=AliasChoices("output_schema_ref", "output_schema"))
    limits_policy_ref: VersionedRef = Field(validation_alias=AliasChoices("limits_policy_ref", "limits_policy"))
    adapter_compatibility_ref: VersionedRef = Field(
        validation_alias=AliasChoices("adapter_compatibility_ref", "adapter_compatibility")
    )
    partial_policy_ref: VersionedRef = Field(
        validation_alias=AliasChoices("partial_policy_ref", "partial_selection_policy_ref"),
    )
    selection_policy_ref: VersionedRef
    timeout_policy_ref: VersionedRef | None = None
    logical_call_budget: int = Field(ge=1, le=1_000_000)

    _operation = field_validator("operation", "resource_type")(
        lambda value: _validate_identifier(value, "binding identifier")
    )

    @property
    def tool(self) -> VersionedRef:
        return self.tool_ref

    @property
    def input_schema(self) -> VersionedRef:
        return self.input_schema_ref

    @property
    def output_schema(self) -> VersionedRef:
        return self.output_schema_ref


class KnowledgeBinding(ABIModel):
    """Typed, content-addressed Knowledge seam binding in a runtime Manifest."""

    knowledge_ref: VersionedRef = Field(validation_alias=AliasChoices("knowledge_ref", "resource_ref"))
    snapshot_ref: VersionedRef
    retrieval_policy_ref: VersionedRef
    input_schema_ref: VersionedRef | None = None
    output_schema_ref: VersionedRef | None = None
    limits_policy_ref: VersionedRef
    partial_policy_ref: VersionedRef
    selection_policy_ref: VersionedRef
    adapter_compatibility_ref: VersionedRef


class InputLimitBinding(ABIModel):
    """Manifest-declared input keys that a deployment may monotonically tighten."""

    key: str = Field(min_length=1, max_length=128)
    limit_schema_ref: VersionedRef
    comparator: Literal["positive_integer_componentwise_lte"]
    ceiling: int = Field(ge=1, le=1_000_000_000)
    failure_policy_ref: VersionedRef

    _key = field_validator("key")(_validate_identifier)


class DependencyGraphEntry(ABIModel):
    """Serializable exact graph edge used to recompute a closure proof."""

    skill: VersionedRef
    dependencies: tuple[VersionedRef, ...] = Field(default=(), max_length=256)

    @field_validator("dependencies")
    @classmethod
    def sort_dependencies(cls, value: tuple[VersionedRef, ...]) -> tuple[VersionedRef, ...]:
        refs = tuple((item.ref, item.version, item.content_hash) for item in value)
        if len(refs) != len(set(refs)):
            raise ValueError("dependency graph entry contains duplicate edges")
        return tuple(sorted(value, key=lambda item: (item.ref, item.version, item.content_hash)))


class DependencyClosureProof(ABIModel):
    """Resolver-issued proof of an exact dependency graph traversal."""

    resolver_version: VersionedRef
    root: VersionedRef
    closure: tuple[VersionedRef, ...] = Field(min_length=1, max_length=1024)
    # The graph is part of the proof input, not an implicit in-process
    # resolver cache.  A new process can therefore replay the exact edge set
    # and detect an evil/missing dependency before trusting the closure.
    dependency_graph: tuple[DependencyGraphEntry, ...] = Field(default=(), max_length=1024)
    proof_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("closure")
    @classmethod
    def sort_closure(cls, value: tuple[VersionedRef, ...]) -> tuple[VersionedRef, ...]:
        refs = tuple((item.ref, item.version, item.content_hash) for item in value)
        if len(refs) != len(set(refs)):
            raise ValueError("closure proof contains duplicate exact references")
        return tuple(sorted(value, key=lambda item: (item.ref, item.version, item.content_hash)))

    @field_validator("dependency_graph")
    @classmethod
    def sort_dependency_graph(cls, value: tuple[DependencyGraphEntry, ...]) -> tuple[DependencyGraphEntry, ...]:
        refs = tuple(item.skill.ref for item in value)
        if len(refs) != len(set(refs)):
            raise ValueError("dependency graph contains duplicate skill nodes")
        return tuple(sorted(value, key=lambda item: (item.skill.ref, item.skill.version, item.skill.content_hash)))

    _hash = field_validator("proof_hash")(lambda value: _validate_hash(value, "closure proof hash"))


class DependencyBinding(ABIModel):
    skill_ref: VersionedRef = Field(validation_alias=AliasChoices("skill_ref", "skill"))
    manifest_ref: VersionedRef = Field(validation_alias=AliasChoices("manifest_ref", "manifest"))
    input_mapping: VersionedRef | None = None
    output_mapping: VersionedRef | None = None

    @property
    def skill(self) -> VersionedRef:
        return self.skill_ref

    @property
    def manifest(self) -> VersionedRef:
        return self.manifest_ref


class SkillRuntimeManifest(ABIModel):
    """Immutable business closure; no credentials, framework objects or extras."""

    manifest_version: str = Field(min_length=1, max_length=32)
    skill_ref: VersionedRef = Field(validation_alias=AliasChoices("skill_ref", "skill"))
    input_schema_ref: VersionedRef = Field(validation_alias=AliasChoices("input_schema_ref", "input_schema"))
    output_schema_ref: VersionedRef = Field(validation_alias=AliasChoices("output_schema_ref", "output_schema"))
    dependencies: tuple[DependencyBinding, ...] = Field(max_length=256)
    dependency_closure_proof: DependencyClosureProof | None = None
    knowledge_bindings: tuple[KnowledgeBinding, ...] = Field(default=(), max_length=256)
    tool_bindings: tuple[ToolBinding, ...] = Field(max_length=256)
    action_bindings: tuple[VersionedRef, ...] = Field(default=(), max_length=256)
    skill_closure: tuple[VersionedRef, ...] = Field(default=(), max_length=256)
    input_limit_allowlist: tuple[InputLimitBinding, ...] = Field(default=(), max_length=128)
    required_capabilities: tuple[str, ...] = Field(max_length=64)
    manifest_hash: str = Field(default="", max_length=64)

    @model_validator(mode="before")
    @classmethod
    def accept_closure_proof_alias(cls, value: object) -> object:
        if isinstance(value, dict) and "closure_proof" in value and "dependency_closure_proof" not in value:
            data = dict(value)
            data["dependency_closure_proof"] = data.pop("closure_proof")
            return data
        return value

    _version = field_validator("manifest_version")(lambda value: _validate_ref(value, "manifest version"))
    _capabilities = field_validator("required_capabilities")(_unique_sorted)

    @field_validator("dependencies")
    @classmethod
    def sort_dependencies(cls, value: tuple[DependencyBinding, ...]) -> tuple[DependencyBinding, ...]:
        refs = tuple(item.skill_ref.ref for item in value)
        if len(refs) != len(set(refs)):
            raise ValueError("manifest dependencies must not repeat a skill ref")
        return tuple(
            sorted(value, key=lambda item: (item.skill_ref.ref, item.skill_ref.version, item.skill_ref.content_hash))
        )

    @field_validator("tool_bindings")
    @classmethod
    def sort_tools(cls, value: tuple[ToolBinding, ...]) -> tuple[ToolBinding, ...]:
        refs = tuple(item.tool_ref.ref for item in value)
        if len(refs) != len(set(refs)):
            raise ValueError("manifest tool bindings must not repeat a tool ref")
        return tuple(
            sorted(value, key=lambda item: (item.tool_ref.ref, item.tool_ref.version, item.tool_ref.content_hash))
        )

    @field_validator("knowledge_bindings")
    @classmethod
    def sort_knowledge(cls, value: tuple[KnowledgeBinding, ...]) -> tuple[KnowledgeBinding, ...]:
        refs = tuple(item.knowledge_ref.ref for item in value)
        if len(refs) != len(set(refs)):
            raise ValueError("manifest knowledge bindings must not repeat a resource ref")
        return tuple(sorted(value, key=lambda item: (item.knowledge_ref.ref, item.knowledge_ref.version)))

    @field_validator("input_limit_allowlist")
    @classmethod
    def sort_input_limits(cls, value: tuple[InputLimitBinding, ...]) -> tuple[InputLimitBinding, ...]:
        keys = tuple(item.key for item in value)
        if len(keys) != len(set(keys)):
            raise ValueError("manifest input limits must not repeat a key")
        return tuple(sorted(value, key=lambda item: item.key))

    @field_validator("action_bindings", "skill_closure")
    @classmethod
    def sort_exact_refs(cls, value: tuple[VersionedRef, ...]) -> tuple[VersionedRef, ...]:
        refs = tuple(item.ref for item in value)
        if len(refs) != len(set(refs)):
            raise ValueError("manifest closure references must not repeat")
        return tuple(sorted(value, key=lambda item: (item.ref, item.version, item.content_hash)))

    @field_validator("required_capabilities")
    @classmethod
    def validate_capability_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for capability in value:
            if not fullmatch(CAPABILITY_PATTERN, capability):
                raise ValueError(f"invalid capability name: {capability}")
        return value

    @model_validator(mode="after")
    def validate_manifest(self) -> SkillRuntimeManifest:
        tool_refs = tuple(item.tool_ref.ref for item in self.tool_bindings)
        if len(tool_refs) != len(set(tool_refs)):
            raise ValueError("manifest tool bindings must not repeat a tool ref")
        if self.manifest_hash and not fullmatch(HASH_PATTERN, self.manifest_hash):
            raise ValueError("manifest_hash must be a lower-case sha256 digest")
        if self.dependency_closure_proof is not None:
            proof_refs = self.dependency_closure_proof.closure
            if self.skill_closure and tuple(self.skill_closure) != proof_refs:
                raise ValueError("skill_closure must equal resolver-issued closure proof")
        return self

    @property
    def skill(self) -> VersionedRef:
        return self.skill_ref

    @property
    def input_schema(self) -> VersionedRef:
        return self.input_schema_ref

    @property
    def output_schema(self) -> VersionedRef:
        return self.output_schema_ref

    def find_tool_binding(self, reference: str | VersionedRef) -> ToolBinding:
        """Return the one exact Tool binding or fail closed."""

        candidates = [item for item in self.tool_bindings if _same_exact_ref(item.tool_ref, reference)]
        if not candidates and isinstance(reference, str):
            candidates = [item for item in self.tool_bindings if item.tool_ref.ref == reference]
        if len(candidates) != 1:
            raise ValueError(f"tool reference is not an exact Manifest binding: {reference}")
        return candidates[0]

    def find_knowledge_binding(self, reference: str | VersionedRef) -> KnowledgeBinding:
        candidates = [item for item in self.knowledge_bindings if _same_exact_ref(item.knowledge_ref, reference)]
        if not candidates and isinstance(reference, str):
            candidates = [item for item in self.knowledge_bindings if item.knowledge_ref.ref == reference]
        if len(candidates) != 1:
            raise ValueError(f"knowledge reference is not an exact Manifest binding: {reference}")
        return candidates[0]

    @property
    def closure_proof(self) -> DependencyClosureProof | None:
        return self.dependency_closure_proof

    def with_hash(self) -> SkillRuntimeManifest:
        from app.contracts.canonical import canonical_hash

        digest = canonical_hash(self, exclude_fields=("manifest_hash",))
        return self.model_copy(update={"manifest_hash": digest})

    def verify(
        self,
        *,
        expected_manifest_hash: str,
        artifact_payloads: Mapping[str, bytes] | None = None,
    ) -> None:
        """Verify this manifest through the runtime's data-only closure gate.

        The method is a small trust seam so contract helpers do not import
        the ``skill_abi`` package in reverse.  The runtime verifier remains
        the single implementation of closure/hash semantics.
        """

        if type(self) is not SkillRuntimeManifest:
            raise TypeError("runtime Manifest verification requires the exact SkillRuntimeManifest type")
        from app.skill_abi.runtime import verify_runtime_manifest

        verify_runtime_manifest(
            self,
            expected_manifest_hash=expected_manifest_hash,
            artifact_payloads=artifact_payloads,
        )


register_runtime_manifest_type(SkillRuntimeManifest)


class SkillExecutionSpec(ABIModel):
    abi_version: str
    spec_id: UUID
    issuer: str = Field(min_length=1, max_length=256)
    tenant_id: str = Field(min_length=1, max_length=128)
    source_agent_ref: VersionedRef | None = None
    run_mode: Literal["live", "replay", "fork_dry_run", "fork_commit"]
    skill: VersionedRef
    graph: GraphBinding
    contracts: ContractBinding
    runtime_manifest: VersionedRef
    runtime_build: VersionedRef
    permission: PermissionBinding
    required_capabilities: tuple[str, ...] = Field(max_length=64)
    budget: BudgetBinding
    policy_refs: tuple[PolicyRef, ...] = Field(max_length=64)
    evaluation_subject_hash: str = Field(default="", max_length=64)
    evaluation_evidence_set: VersionedRef
    skill_spec_hash: str = Field(default="", max_length=64)
    resolved_at: datetime
    resolver_version: str = Field(min_length=1, max_length=128)

    @field_validator("abi_version")
    @classmethod
    def validate_abi_version(cls, value: str) -> str:
        if value not in ABI_VERSIONS:
            raise ValueError(f"unsupported ABI version: {value}")
        return value

    _capabilities = field_validator("required_capabilities")(_unique_sorted)

    @field_validator("policy_refs")
    @classmethod
    def sort_policy_refs(cls, value: tuple[PolicyRef, ...]) -> tuple[PolicyRef, ...]:
        kinds = tuple(item.kind for item in value)
        if len(kinds) != len(set(kinds)):
            raise ValueError("policy_refs must contain at most one ref per kind")
        return tuple(sorted(value, key=lambda item: (item.kind, item.policy.ref, item.policy.version)))

    @field_validator("required_capabilities")
    @classmethod
    def validate_capabilities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for capability in value:
            if not fullmatch(CAPABILITY_PATTERN, capability):
                raise ValueError(f"invalid capability name: {capability}")
        return value

    @field_validator("evaluation_subject_hash", "skill_spec_hash")
    @classmethod
    def validate_optional_hash(cls, value: str) -> str:
        if value and not fullmatch(HASH_PATTERN, value):
            raise ValueError("hash must be a lower-case sha256 digest")
        return value

    _resolver = field_validator("resolver_version")(lambda value: _validate_ref(value, "resolver version"))
    _resolved = field_validator("resolved_at")(_validate_aware_utc)

    @model_validator(mode="after")
    def validate_semantics(self) -> SkillExecutionSpec:
        policy_kinds = tuple(item.kind for item in self.policy_refs)
        if len(policy_kinds) != len(set(policy_kinds)):
            raise ValueError("policy_refs must contain at most one ref per kind")
        if "workspace" in policy_kinds and "execution.workspace" not in self.required_capabilities:
            raise ValueError("workspace policy requires execution.workspace capability")
        if "memory" in policy_kinds and "memory.long_term" not in self.required_capabilities:
            raise ValueError("memory policy requires memory.long_term capability")
        if self.budget.effective_budget.content_hash != self.budget.evaluation_envelope.content_hash:
            if self.budget.input_subset is None:
                raise ValueError("different effective budget requires monotonic input subset attestation")
        return self


__all__ = [
    "ABIModel",
    "ABI_VERSIONS",
    "BudgetBinding",
    "ContractBinding",
    "DependencyClosureProof",
    "DependencyGraphEntry",
    "DependencyBinding",
    "GraphBinding",
    "InputLimitBinding",
    "KnowledgeBinding",
    "MonotonicInputSubsetBinding",
    "PermissionBinding",
    "PolicyRef",
    "SkillExecutionSpec",
    "SkillRuntimeManifest",
    "ToolBinding",
]
