"""WS-1 Skill ABI hashing, explicit readers, closure and bootstrap guards."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from re import fullmatch
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from app.contracts.canonical import (
    CanonicalFailure,
    RetryOwner,
    ToolCommand,
    ToolProposal,
    TypedSchemaRegistry,
    VersionedRef,
    canonical_hash,
    derive_contract_meta,
    register_abi_unknown_error,
    register_contract_reader,
    register_runtime_manifest_verifier,
    register_tool_command_builder,
)
from app.skill_abi.models import (
    ABI_VERSIONS,
    DependencyClosureProof,
    DependencyGraphEntry,
    SkillExecutionSpec,
    SkillRuntimeManifest,
    ToolBinding,
)


class ABIConversionError(ValueError):
    """An explicit ABI converter failed; no fallback is permitted."""

    preserve_contract_error = True


class UnknownABIVersionError(ABIConversionError):
    """The requested ABI version has no registered reader."""


class ArtifactHashMismatchError(ValueError):
    """An immutable artifact does not match its declared digest."""


class MissingArtifactError(ValueError):
    """An exact artifact is unavailable."""


class ClosureViolationError(ValueError):
    """A proposal points outside the resolved dependency closure."""


class DependencyCycleError(ClosureViolationError):
    """Dependency closure contains a cycle."""


class DependencyConflictError(ClosureViolationError):
    """Two exact references resolve the same logical skill differently."""


class CapabilityUnavailableError(RuntimeError):
    """An optional adapter is disabled or unavailable."""


class MissingCapabilityError(CapabilityUnavailableError):
    """The deployment cannot satisfy a skill's required capabilities."""


class PermissionDeniedError(PermissionError):
    """A permission posture or authorization decision denies an operation."""


class PermissionInteractionRequiredError(PermissionError):
    """An authorized operation still requires explicit user interaction.

    ``ASK`` is not an execution grant.  A distinct exception keeps provider
    guards fail-closed while allowing callers to create a permission
    interaction from the decision.
    """

    pass


ManifestHashMismatchError = ArtifactHashMismatchError
SkillSpecHashMismatchError = ArtifactHashMismatchError
DependencyHashMismatchError = ArtifactHashMismatchError
MissingConverterError = ABIConversionError


@dataclass(frozen=True, slots=True)
class DependencyNode:
    """A small immutable graph node accepted by ``resolve_dependency_closure``."""

    skill: VersionedRef
    dependencies: tuple[VersionedRef, ...] = ()
    artifact_payload: bytes | None = None

    def __post_init__(self) -> None:
        dependencies = tuple(self.dependencies)
        if len({item.ref for item in dependencies}) != len(dependencies):
            raise ValueError("dependency node contains duplicate references")
        object.__setattr__(
            self,
            "dependencies",
            tuple(sorted(dependencies, key=lambda item: (item.ref, item.version, item.content_hash))),
        )

    def __repr__(self) -> str:
        return f"DependencyNode(skill={self.skill.ref!r}, dependencies={len(self.dependencies)})"


def _logical_skill_id(ref: str) -> str:
    return ref.rsplit("@", 1)[0]


def _ref_key(ref: VersionedRef) -> tuple[str, str, str]:
    return ref.ref, ref.version, ref.content_hash


def _is_exact_generic_origin(value: Any, origin: type[Any]) -> bool:
    value_type = type(value)
    if value_type is origin:
        return True
    try:
        metadata = value_type.__pydantic_generic_metadata__
    except AttributeError:
        metadata = {}
    return metadata.get("origin") is origin


def _closure_proof_payload(
    root: VersionedRef,
    closure: Sequence[VersionedRef],
    resolver_version: VersionedRef | None = None,
    dependency_graph: Sequence[DependencyGraphEntry] = (),
) -> dict[str, Any]:
    return {
        "root": root.model_dump(mode="python"),
        "closure": tuple(item.model_dump(mode="python") for item in sorted(closure, key=_ref_key)),
        "resolver_version": resolver_version.model_dump(mode="python") if resolver_version is not None else None,
        "dependency_graph": tuple(
            {
                "skill": entry.skill.model_dump(mode="python"),
                "dependencies": tuple(item.model_dump(mode="python") for item in entry.dependencies),
            }
            for entry in sorted(dependency_graph, key=lambda item: _ref_key(item.skill))
        ),
    }


def _graph_entries(nodes: Sequence[DependencyNode], root: DependencyNode) -> tuple[DependencyGraphEntry, ...]:
    entries: dict[str, DependencyGraphEntry] = {}
    for node in (root, *nodes):
        entry = DependencyGraphEntry(skill=node.skill, dependencies=node.dependencies)
        previous = entries.get(node.skill.ref)
        if previous is not None and previous != entry:
            raise DependencyConflictError(f"conflicting graph entry for {node.skill.ref}")
        entries[node.skill.ref] = entry
    return tuple(sorted(entries.values(), key=lambda item: _ref_key(item.skill)))


def _content_for_ref(
    reference: VersionedRef,
    *,
    artifact_payloads: Mapping[str, bytes] | None,
    content_loader: Callable[[VersionedRef], bytes] | None,
) -> bytes:
    if artifact_payloads is not None:
        payload = artifact_payloads.get(reference.ref)
        if payload is None:
            raise MissingArtifactError(f"missing artifact bytes for {reference.ref}")
        return payload
    if content_loader is not None:
        try:
            return content_loader(reference)
        except KeyError as exc:
            raise MissingArtifactError(f"missing artifact bytes for {reference.ref}") from exc
    raise MissingArtifactError(f"trusted artifact content is required for {reference.ref}")


def resolve_dependency_closure(
    root: DependencyNode,
    nodes: Sequence[DependencyNode],
    *,
    artifact_payloads: Mapping[str, bytes] | None = None,
    content_loader: Callable[[VersionedRef], bytes] | None = None,
) -> tuple[VersionedRef, ...]:
    """Validate and return a deterministic exact dependency closure.

    Nodes are looked up by exact ``ref``.  A logical skill cannot appear at two
    versions in one closure, and every referenced node must be present.  When
    artifact bytes are supplied, their SHA-256 is checked against the exact
    reference hash as an additional tamper guard.
    """

    by_ref: dict[str, DependencyNode] = {}
    by_logical: dict[str, VersionedRef] = {}
    for node in (root, *nodes):
        previous = by_ref.get(node.skill.ref)
        if previous is not None and _ref_key(previous.skill) != _ref_key(node.skill):
            raise DependencyConflictError(f"conflicting exact version/hash for {node.skill.ref}")
        by_ref[node.skill.ref] = node
        logical = _logical_skill_id(node.skill.ref)
        previous_ref = by_logical.get(logical)
        if previous_ref is not None and _ref_key(previous_ref) != _ref_key(node.skill):
            raise DependencyConflictError(f"multiple versions of logical skill {logical}")
        by_logical[logical] = node.skill

    visiting: set[str] = set()
    visited: set[str] = set()
    result: list[VersionedRef] = []

    def visit(ref: VersionedRef) -> None:
        if ref.ref in visiting:
            raise DependencyCycleError(f"dependency cycle at {ref.ref}")
        node = by_ref.get(ref.ref)
        if node is None or _ref_key(node.skill) != _ref_key(ref):
            raise ClosureViolationError(f"dependency outside closure: {ref.ref}")
        if ref.ref in visited:
            return
        visiting.add(ref.ref)
        for dependency in node.dependencies:
            visit(dependency)
        visiting.remove(ref.ref)
        visited.add(ref.ref)
        payload: bytes | None = None
        if artifact_payloads is not None or content_loader is not None:
            payload = _content_for_ref(ref, artifact_payloads=artifact_payloads, content_loader=content_loader)
        elif node.artifact_payload is not None:
            payload = node.artifact_payload
        if payload is not None and sha256(payload).hexdigest() != ref.content_hash:
            raise ArtifactHashMismatchError(f"dependency artifact hash mismatch: {ref.ref}")
        result.append(ref)

    visit(root.skill)
    closure = tuple(sorted(result, key=_ref_key))
    # Resolving is also the only place that can issue a closure proof.  The
    # plain tuple return remains compatible with existing callers; callers
    # that publish a Manifest should use ``issue_dependency_closure_proof``.
    return closure


def issue_dependency_closure_proof(
    root: DependencyNode,
    nodes: Sequence[DependencyNode],
    *,
    resolver_version: VersionedRef,
    artifact_payloads: Mapping[str, bytes] | None = None,
    content_loader: Callable[[VersionedRef], bytes] | None = None,
) -> DependencyClosureProof:
    """Resolve an exact graph and issue a non-self-asserted proof artifact."""

    graph = _graph_entries(nodes, root)
    # Issuance is stricter than traversal: every graph node, including one
    # that would otherwise be unreachable, must have verified immutable bytes.
    # This prevents publishing a proof whose later verifier would discover a
    # missing artifact only after the proof had already been signed.
    verified_payloads: dict[str, bytes] = {}
    for node in (root, *nodes):
        payload: bytes
        if artifact_payloads is not None or content_loader is not None:
            payload = _content_for_ref(node.skill, artifact_payloads=artifact_payloads, content_loader=content_loader)
        else:
            node_payload = node.artifact_payload
            if node_payload is None:
                raise MissingArtifactError(f"trusted artifact content is required for {node.skill.ref}")
            payload = node_payload
        validate_artifact(payload, node.skill.content_hash)
        verified_payloads[node.skill.ref] = payload
    closure = resolve_dependency_closure(
        root,
        nodes,
        artifact_payloads=verified_payloads,
    )
    graph_refs = tuple(sorted((entry.skill for entry in graph), key=_ref_key))
    if graph_refs != tuple(sorted(closure, key=_ref_key)):
        raise ClosureViolationError("cannot issue a closure proof with unreachable or missing graph nodes")
    proof_hash = canonical_hash(_closure_proof_payload(root.skill, closure, resolver_version, graph))
    proof = DependencyClosureProof(
        resolver_version=resolver_version,
        root=root.skill,
        closure=closure,
        dependency_graph=graph,
        proof_hash=proof_hash,
    )
    return proof


def validate_closure_ref(proposal_ref: str | VersionedRef, closure: Sequence[VersionedRef]) -> None:
    expected = proposal_ref.ref if isinstance(proposal_ref, VersionedRef) else proposal_ref
    if isinstance(proposal_ref, VersionedRef):
        matched = any(_ref_key(ref) == _ref_key(proposal_ref) for ref in closure)
    else:
        # A string is retained as a read-only compatibility form, but it must
        # identify one and only one exact binding.  Ambiguous ref-only
        # proposals are rejected instead of selecting an arbitrary version.
        matches = [ref for ref in closure if ref.ref == expected]
        matched = len(matches) == 1
    if not matched:
        raise ClosureViolationError(f"reference is outside manifest closure: {expected}")


def validate_manifest_proposal(
    manifest: Any,
    *,
    kind: str,
    reference: str | VersionedRef,
    expected_manifest_hash: str,
    artifact_payloads: Mapping[str, bytes] | None = None,
    content_loader: Callable[[VersionedRef], bytes] | None = None,
) -> None:
    """Reject Skill/Tool/Action proposals outside a typed manifest closure."""

    from app.skill_abi.models import SkillRuntimeManifest

    if type(manifest) is not SkillRuntimeManifest:
        raise ClosureViolationError("proposal requires a validated SkillRuntimeManifest")
    verify_runtime_manifest(
        manifest,
        expected_manifest_hash=expected_manifest_hash,
        artifact_payloads=artifact_payloads,
        content_loader=content_loader,
    )
    if kind == "skill":
        refs = (manifest.skill_ref, *tuple(item.skill_ref for item in manifest.dependencies))
        refs = (*refs, *manifest.skill_closure)
    elif kind == "tool":
        refs = tuple(item.tool_ref for item in manifest.tool_bindings)
    elif kind == "action":
        refs = manifest.action_bindings
    else:
        raise ClosureViolationError(f"unknown closure kind: {kind}")
    validate_closure_ref(reference, refs)


def validate_artifact(payload: bytes, expected_hash: str) -> None:
    actual = sha256(payload).hexdigest()
    if actual != expected_hash:
        raise ArtifactHashMismatchError("artifact hash mismatch")


def compute_evaluation_subject_hash(spec: SkillExecutionSpec) -> str:
    """Hash behavior-affecting bindings only, excluding volatile run identity."""

    if type(spec) is not SkillExecutionSpec:
        raise TypeError("evaluation subject hashing requires the exact SkillExecutionSpec type")
    behavior_policies = tuple(
        item.model_dump(mode="python", exclude_unset=True) for item in spec.policy_refs if item.kind != "experience"
    )
    contracts = spec.contracts.model_dump(mode="python", exclude_unset=True)
    # ABI converter provenance is a format concern, not a behavior change.
    # Keeping it outside the evaluation subject allows a pure v1→v2 rewrite
    # to preserve the original evidence binding.
    contracts.pop("converter_bundle", None)
    payload = {
        "skill": spec.skill.model_dump(mode="python", exclude_unset=True),
        "run_mode": spec.run_mode,
        "graph": spec.graph.model_dump(mode="python", exclude_unset=True),
        "contracts": contracts,
        "runtime_manifest": spec.runtime_manifest.model_dump(mode="python", exclude_unset=True),
        "runtime_build": spec.runtime_build.model_dump(mode="python", exclude_unset=True),
        "permission_envelope": spec.permission.permission_envelope_hash,
        "authorization_policy": spec.permission.authorization_policy.model_dump(mode="python", exclude_unset=True),
        "permission_preset": spec.permission.permission_preset.model_dump(mode="python", exclude_unset=True),
        "behavior_policy_refs": behavior_policies,
        "evaluation_budget": spec.budget.evaluation_envelope.model_dump(mode="python", exclude_unset=True),
    }
    return canonical_hash(payload)


def compute_skill_spec_hash(spec: SkillExecutionSpec) -> str:
    if type(spec) is not SkillExecutionSpec:
        raise TypeError("skill spec hashing requires the exact SkillExecutionSpec type")
    payload = spec.model_dump(mode="python", exclude_unset=True)
    payload.pop("spec_id", None)
    payload.pop("skill_spec_hash", None)
    payload.pop("resolved_at", None)
    permission = payload.get("permission")
    if isinstance(permission, dict):
        permission.pop("run_authority_ref", None)
    return canonical_hash(payload)


def build_skill_execution_spec(**values: Any) -> SkillExecutionSpec:
    """Build a spec and fill both hashes with the documented exclusions."""

    initial = SkillExecutionSpec.model_validate(values)
    subject_hash = compute_evaluation_subject_hash(initial)
    with_subject = initial.model_copy(update={"evaluation_subject_hash": subject_hash})
    return with_subject.model_copy(update={"skill_spec_hash": compute_skill_spec_hash(with_subject)})


def verify_skill_execution_spec(spec: SkillExecutionSpec) -> None:
    if type(spec) is not SkillExecutionSpec:
        raise TypeError("skill spec verification requires the exact SkillExecutionSpec type")
    expected_subject = compute_evaluation_subject_hash(spec)
    if spec.evaluation_subject_hash != expected_subject:
        raise ArtifactHashMismatchError("evaluation_subject_hash mismatch")
    expected_spec = compute_skill_spec_hash(spec)
    if spec.skill_spec_hash != expected_spec:
        raise ArtifactHashMismatchError("skill_spec_hash mismatch")


def verify_runtime_manifest(
    manifest: Any,
    *,
    expected_manifest_hash: str,
    artifact_payloads: Mapping[str, bytes] | None = None,
    content_loader: Callable[[VersionedRef], bytes] | None = None,
) -> None:
    """Verify a Manifest against an external hash and deterministic closure.

    The expected hash is Spec-bound input.  It is intentionally not inferred
    from ``manifest.manifest_hash``: mutating a Manifest and recomputing its
    own field cannot manufacture trust.  Closure proof verification is fully
    data based and therefore works in a fresh Python process.
    """

    from app.skill_abi.models import SkillRuntimeManifest

    if type(manifest) is not SkillRuntimeManifest or not manifest.manifest_hash:
        raise ArtifactHashMismatchError("runtime manifest hash is missing")
    if any(type(binding) is not ToolBinding for binding in manifest.tool_bindings):
        raise ArtifactHashMismatchError("runtime manifest contains a non-concrete ToolBinding")
    if not fullmatch(r"[0-9a-f]{64}", expected_manifest_hash):
        raise ArtifactHashMismatchError("expected runtime manifest hash is invalid")
    if expected_manifest_hash == "0" * 64 or manifest.manifest_hash == "0" * 64:
        raise ArtifactHashMismatchError("runtime manifest hash cannot be all zero")
    if manifest.manifest_hash != expected_manifest_hash:
        raise ArtifactHashMismatchError("runtime manifest does not match Spec-bound hash")
    proof = manifest.dependency_closure_proof
    if proof is not None:
        graph = tuple(DependencyNode(entry.skill, entry.dependencies) for entry in proof.dependency_graph)
        if not graph:
            raise ClosureViolationError("runtime manifest closure proof has no exact dependency graph")
        root_nodes = [node for node in graph if _ref_key(node.skill) == _ref_key(proof.root)]
        if len(root_nodes) != 1:
            raise ClosureViolationError("runtime manifest closure proof root is absent from graph")
        closure = resolve_dependency_closure(
            root_nodes[0],
            tuple(node for node in graph if node is not root_nodes[0]),
            artifact_payloads=artifact_payloads,
            content_loader=content_loader,
        )
        graph_refs = tuple(sorted((node.skill for node in graph), key=_ref_key))
        if graph_refs != tuple(sorted(closure, key=_ref_key)):
            raise ClosureViolationError("dependency graph contains unreachable or missing closure nodes")
        expected_proof_hash = canonical_hash(
            _closure_proof_payload(proof.root, closure, proof.resolver_version, proof.dependency_graph)
        )
        if tuple(closure) != tuple(proof.closure) or expected_proof_hash != proof.proof_hash:
            raise ClosureViolationError("runtime manifest closure proof is invalid")
        if _ref_key(proof.root) != _ref_key(manifest.skill_ref):
            raise ClosureViolationError("runtime manifest closure proof root does not match skill")
        if tuple(manifest.skill_closure) != tuple(proof.closure):
            raise ClosureViolationError("runtime manifest closure does not match resolver proof")
        dependency_refs = tuple(item.skill_ref for item in manifest.dependencies)
        proof_dependencies = tuple(item for item in proof.closure if _ref_key(item) != _ref_key(proof.root))
        if tuple(dependency_refs) != tuple(proof_dependencies):
            raise ClosureViolationError("Manifest DependencyBinding set differs from proof closure")
    elif manifest.dependencies or manifest.skill_closure:
        raise ClosureViolationError("non-trivial runtime manifest requires an exact closure proof")
    elif manifest.skill_closure:
        raise ClosureViolationError("skill_closure requires an exact closure proof")
    expected = canonical_hash(manifest, exclude_fields=("manifest_hash",))
    if manifest.manifest_hash != expected:
        raise ArtifactHashMismatchError("runtime manifest hash mismatch")
    if artifact_payloads:
        # When callers provide additional immutable artifact bytes (for
        # example a ToolBinding's schema/policy artifacts), verify every
        # supplied known reference.  Closure traversal above remains the
        # authoritative requirement for dependency artifacts; this loop makes
        # the command seam reject a tampered binding artifact as well.
        refs: dict[str, VersionedRef] = {
            manifest.skill_ref.ref: manifest.skill_ref,
            manifest.input_schema_ref.ref: manifest.input_schema_ref,
            manifest.output_schema_ref.ref: manifest.output_schema_ref,
        }
        for dependency in manifest.dependencies:
            refs[dependency.skill_ref.ref] = dependency.skill_ref
            refs[dependency.manifest_ref.ref] = dependency.manifest_ref
            if dependency.input_mapping is not None:
                refs[dependency.input_mapping.ref] = dependency.input_mapping
            if dependency.output_mapping is not None:
                refs[dependency.output_mapping.ref] = dependency.output_mapping
        for binding in manifest.tool_bindings:
            for reference in (
                binding.tool_ref,
                binding.input_schema_ref,
                binding.output_schema_ref,
                binding.limits_policy_ref,
                binding.adapter_compatibility_ref,
                binding.partial_policy_ref,
                binding.selection_policy_ref,
                binding.timeout_policy_ref,
            ):
                if reference is not None:
                    refs[reference.ref] = reference
        for knowledge_binding in manifest.knowledge_bindings:
            for reference in (
                knowledge_binding.knowledge_ref,
                knowledge_binding.snapshot_ref,
                knowledge_binding.retrieval_policy_ref,
                knowledge_binding.input_schema_ref,
                knowledge_binding.output_schema_ref,
                knowledge_binding.limits_policy_ref,
                knowledge_binding.partial_policy_ref,
                knowledge_binding.selection_policy_ref,
                knowledge_binding.adapter_compatibility_ref,
            ):
                if reference is not None:
                    refs[reference.ref] = reference
        for reference in (*manifest.action_bindings, *manifest.skill_closure):
            refs[reference.ref] = reference
        for key, payload in artifact_payloads.items():
            reference = refs.get(key)
            if reference is not None:
                validate_artifact(payload, reference.content_hash)


register_runtime_manifest_verifier(verify_runtime_manifest)


def compute_manifest_hash(manifest: Any) -> str:
    if type(manifest) is not SkillRuntimeManifest:
        raise TypeError("compute_manifest_hash requires SkillRuntimeManifest")
    return canonical_hash(manifest, exclude_fields=("manifest_hash",))


def build_tool_command_from_decision(
    decision: Any,
    *,
    authorization_decision_ref: str,
    tool_request_id: UUID,
    timeout_policy_ref: str,
    manifest: SkillRuntimeManifest,
    expected_manifest_hash: str,
    tool_binding: ToolBinding,
    schema_registry: TypedSchemaRegistry,
    artifact_payloads: Mapping[str, bytes] | None = None,
) -> ToolCommand[Any]:
    """Construct a ToolCommand only after the concrete ABI verifier succeeds."""

    if not _is_exact_generic_origin(decision, ToolProposal):
        raise ValueError("tool command requires a validated ToolProposal")
    if type(manifest) is not SkillRuntimeManifest:
        raise TypeError("tool command requires a concrete SkillRuntimeManifest")
    if type(tool_binding) is not ToolBinding:
        raise TypeError("tool command requires a concrete ToolBinding")
    if type(schema_registry) is not TypedSchemaRegistry:
        raise TypeError("tool command requires a TypedSchemaRegistry")
    verify_runtime_manifest(
        manifest,
        expected_manifest_hash=expected_manifest_hash,
        artifact_payloads=artifact_payloads,
    )
    if tool_binding.logical_call_budget < 1:
        raise ValueError("tool command logical call budget must be positive")
    bound = manifest.find_tool_binding(tool_binding.tool_ref)
    if bound != tool_binding:
        raise ValueError("ToolBinding does not match the verified Manifest byte-for-byte")
    if not isinstance(decision.tool_ref, VersionedRef) or decision.tool_ref != tool_binding.tool_ref:
        raise ValueError("tool proposal must carry the exact VersionedRef ToolBinding identity")
    registered_input = schema_registry.resolve_model(tool_binding.input_schema_ref, role="input")
    decision_input = type(decision).model_fields.get("input")
    if decision_input is None or decision_input.annotation is not registered_input:
        raise ValueError("tool proposal input schema does not match the exact registry binding")
    parsed_input = schema_registry.resolve(tool_binding.input_schema_ref, role="input").validate_python(
        decision.input.model_dump(mode="python", exclude_unset=False)
        if isinstance(decision.input, BaseModel)
        else decision.input
    )
    if tool_binding.timeout_policy_ref is None:
        raise ValueError("tool command requires a Manifest timeout policy binding")
    if tool_binding.timeout_policy_ref.ref != timeout_policy_ref:
        raise ValueError("timeout policy is not the Manifest binding")
    return ToolCommand(
        meta=derive_contract_meta(
            decision.meta,
            contract_name="tool.command",
            contract_version="v1",
            causation_id=decision.meta.message_id,
        ),
        decision_id=decision.decision_id,
        tool_request_id=tool_request_id,
        run_id=decision.run_id,
        authorization_decision_ref=authorization_decision_ref,
        tool_ref=tool_binding.tool_ref,
        input=parsed_input,
        timeout_policy_ref=timeout_policy_ref,
    )


register_tool_command_builder(build_tool_command_from_decision)


class ABIConverterRegistry:
    """Explicit ABI reader/converter registry; there is no latest fallback."""

    def __init__(self, supported_versions: Sequence[str] = ABI_VERSIONS) -> None:
        self._supported_versions = frozenset(supported_versions)
        self._readers: dict[str, Callable[[dict[str, Any]], SkillExecutionSpec]] = {}

    @property
    def versions(self) -> tuple[str, ...]:
        return tuple(sorted(self._readers))

    def register(self, version: str, converter: Callable[[dict[str, Any]], SkillExecutionSpec]) -> None:
        if version not in self._supported_versions:
            raise UnknownABIVersionError(f"unsupported ABI version: {version}")
        if version in self._readers:
            raise ValueError(f"ABI converter already registered: {version}")
        self._readers[version] = converter

    def read(self, payload: Mapping[str, Any]) -> SkillExecutionSpec:
        version = payload.get("abi_version")
        if not isinstance(version, str) or version not in self._readers:
            raise UnknownABIVersionError(f"unknown ABI converter: {version!r}")
        try:
            return self._readers[version](dict(payload))
        except UnknownABIVersionError:
            raise
        except Exception as exc:
            raise ABIConversionError(f"failed to convert SkillExecutionSpec {version}") from exc


ABI_REGISTRY = ABIConverterRegistry()


def register_abi_converter(version: str, converter: Callable[[dict[str, Any]], SkillExecutionSpec]) -> None:
    ABI_REGISTRY.register(version, converter)


def read_skill_execution_spec(payload: Mapping[str, Any]) -> SkillExecutionSpec:
    return ABI_REGISTRY.read(payload)


def convert_abi_v1_to_v2(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Explicit, one-way ABI v1 → v2 conversion.

    v2 records the source ABI in the converter bundle reference and never
    mutates the caller's mapping.  There is deliberately no v2 → v1 or
    ``latest`` fallback: historical v1 runs are read by the v1 reader, while
    a resolver that upgrades a spec must make this conversion explicit.
    """

    if payload.get("abi_version") != "v1":
        raise UnknownABIVersionError("v1 to v2 conversion requires abi_version='v1'")
    # Conversion is an artifact operation, never a partial dict fixture.  A
    # complete, already-hashed v1 Spec is required before any field changes.
    source = _read_v1(dict(payload))
    verify_skill_execution_spec(source)
    converted = dict(source.model_dump(mode="python", exclude_unset=True))
    contracts = dict(converted["contracts"])
    converter_bundle = {
        "ref": "converter.abi.v1-to-v2",
        "version": "1",
        "content_hash": sha256(b"grove-abi-v1-to-v2").hexdigest(),
    }
    if contracts.get("converter_bundle") not in (None, converter_bundle):
        raise ABIConversionError("v1 spec carries an incompatible converter bundle")
    contracts["converter_bundle"] = converter_bundle
    converted["contracts"] = contracts
    converted["abi_version"] = "v2"
    # Validate the complete result and prove its hashes immediately.  The
    # evidence/evaluation subject remains bound to the source behavior.
    # Validate the complete converted shape before filling its derived hash;
    # the source hash was already verified above and no field-set cleanup is
    # permitted at this boundary.
    target = SkillExecutionSpec.model_validate(converted)
    if target.evaluation_subject_hash != source.evaluation_subject_hash:
        raise ABIConversionError("ABI conversion changed evaluation subject")
    converted["skill_spec_hash"] = compute_skill_spec_hash(target)
    result = _read_v2(converted)
    verify_skill_execution_spec(result)
    return result.model_dump(mode="python", exclude_unset=True)


def _read_v1(payload: dict[str, Any]) -> SkillExecutionSpec:
    if payload.get("abi_version") != "v1":
        raise UnknownABIVersionError(f"v1 reader received {payload.get('abi_version')!r}")
    try:
        spec = SkillExecutionSpec.model_validate(payload)
        verify_skill_execution_spec(spec)
        return spec
    except Exception as exc:
        raise ABIConversionError("failed to convert SkillExecutionSpec v1") from exc


def _read_v2(payload: dict[str, Any]) -> SkillExecutionSpec:
    if payload.get("abi_version") != "v2":
        raise UnknownABIVersionError(f"v2 reader received {payload.get('abi_version')!r}")
    try:
        spec = SkillExecutionSpec.model_validate(payload)
        verify_skill_execution_spec(spec)
        return spec
    except Exception as exc:
        raise ABIConversionError("failed to convert SkillExecutionSpec v2") from exc


register_abi_converter("v1", _read_v1)
register_abi_converter("v2", _read_v2)
register_abi_unknown_error(UnknownABIVersionError)


def _read_via_registry(name: str, payload: Any) -> Any:
    if name == "skill_execution_spec":
        return read_skill_execution_spec(payload)
    raise ValueError(f"unknown contract converter: {name}")


try:
    register_contract_reader("skill_execution_spec", "v1", _read_v1)
    register_contract_reader("skill_execution_spec", "v2", _read_v2)
except ValueError:
    # Import reloads must not replace a reader with a different implementation.
    pass


@dataclass(frozen=True)
class RetryBudget:
    max_attempts: int
    consumed: int = 0

    def __post_init__(self) -> None:
        if self.max_attempts < 0 or self.consumed < 0 or self.consumed > self.max_attempts:
            raise ValueError("retry budget must satisfy 0 <= consumed <= max_attempts")

    @property
    def remaining(self) -> int:
        return self.max_attempts - self.consumed


def retry_allowed(failure: CanonicalFailure, *, owner: RetryOwner, budget: RetryBudget) -> bool:
    return failure.retryable and failure.retry_owner == owner and budget.remaining > 0


__all__ = [
    "ABIConversionError",
    "ABIConverterRegistry",
    "ABI_REGISTRY",
    "ABI_VERSIONS",
    "ArtifactHashMismatchError",
    "build_tool_command_from_decision",
    "CapabilityUnavailableError",
    "ClosureViolationError",
    "DependencyConflictError",
    "DependencyCycleError",
    "DependencyHashMismatchError",
    "DependencyNode",
    "MissingArtifactError",
    "MissingConverterError",
    "PermissionDeniedError",
    "PermissionInteractionRequiredError",
    "RetryBudget",
    "SkillExecutionSpec",
    "SkillSpecHashMismatchError",
    "ManifestHashMismatchError",
    "UnknownABIVersionError",
    "build_skill_execution_spec",
    "compute_evaluation_subject_hash",
    "compute_manifest_hash",
    "compute_skill_spec_hash",
    "convert_abi_v1_to_v2",
    "issue_dependency_closure_proof",
    "read_skill_execution_spec",
    "register_abi_converter",
    "resolve_dependency_closure",
    "retry_allowed",
    "validate_artifact",
    "validate_closure_ref",
    "validate_manifest_proposal",
    "verify_skill_execution_spec",
    "verify_runtime_manifest",
]
