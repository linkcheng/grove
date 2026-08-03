"""WS-1 round-three regressions for exact hashes and typed trust seams."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, cast
from uuid import UUID, uuid4

import pytest
from app.contracts import (
    ActionProposal,
    ContractMeta,
    DelegateProposal,
    FinalAnswer,
    KnowledgeFilter,
    KnowledgeProposalPayload,
    RetrievalBudget,
    ToolProposal,
    ToolProposalPayload,
    TypedSchemaRegistry,
    VersionedRef,
    enrich_decision,
    enrich_knowledge_decision,
    knowledge_request_from_decision,
    parse_inference_decision,
    tool_command_from_decision,
)
from app.contracts.canonical import (
    _assert_safe_annotation,
    _manifest_schema_for_payload,
    _payload_schema_types,
    _resolve_schema_adapter,
    _resolve_schema_model,
    _schema_adapter,
    canonical_bytes,
    parse_canonical_decision,
    read_contract,
)
from app.skill_abi import (
    ArtifactHashMismatchError,
    ClosureViolationError,
    DependencyBinding,
    DependencyClosureProof,
    DependencyCycleError,
    DependencyGraphEntry,
    DependencyNode,
    InputLimitBinding,
    KnowledgeBinding,
    MissingArtifactError,
    PolicyRef,
    SkillExecutionSpec,
    SkillRuntimeManifest,
    ToolBinding,
    build_skill_execution_spec,
    build_tool_command_from_decision,
    compute_manifest_hash,
    issue_dependency_closure_proof,
    resolve_dependency_closure,
    verify_runtime_manifest,
    verify_skill_execution_spec,
)
from pydantic import BaseModel, Field, ValidationError
from scripts.check_contract_dependencies import find_violations


class StrictInput(BaseModel):
    model_config = {"extra": "forbid", "frozen": True}
    value: str


class StrictOutput(BaseModel):
    model_config = {"extra": "forbid", "frozen": True}
    answer: str


class LooseOutput(BaseModel):
    answer: str


def _ref(name: str, payload: bytes = b"payload") -> VersionedRef:
    return VersionedRef(ref=name, version="1", content_hash=hashlib.sha256(payload).hexdigest())


def _meta() -> ContractMeta:
    return ContractMeta(
        contract_name="canonical.inference",
        contract_version="v1",
        message_id=uuid4(),
        tenant_id="tenant-a",
        correlation_id="corr-a",
    )


def _manifest() -> tuple[SkillRuntimeManifest, ToolBinding]:
    binding = ToolBinding(
        tool_ref=_ref("tool.read@1", b"tool"),
        operation="read",
        resource_type="asset",
        effect_class="read",
        input_schema_ref=_ref("schema.input@1", b"input"),
        output_schema_ref=_ref("schema.output@1", b"output"),
        limits_policy_ref=_ref("limits@1", b"limits"),
        adapter_compatibility_ref=_ref("adapter@1", b"adapter"),
        partial_policy_ref=_ref("partial@1", b"partial"),
        selection_policy_ref=_ref("selection@1", b"selection"),
        timeout_policy_ref=_ref("timeout@1", b"timeout"),
        logical_call_budget=1,
    )
    return (
        SkillRuntimeManifest(
            manifest_version="v1",
            skill_ref=_ref("skill.root@1", b"root"),
            input_schema_ref=binding.input_schema_ref,
            output_schema_ref=binding.output_schema_ref,
            dependencies=(),
            tool_bindings=(binding,),
            required_capabilities=(),
        ).with_hash(),
        binding,
    )


def _spec() -> SkillExecutionSpec:
    return build_skill_execution_spec(
        abi_version="v1",
        spec_id=UUID("00000000-0000-0000-0000-000000000010"),
        issuer="resolver@1",
        tenant_id="tenant-a",
        run_mode="live",
        skill=VersionedRef(ref="skill.root@1", version="1", content_hash="1" * 64),
        graph={
            "graph": VersionedRef(ref="graph.root@1", version="1", content_hash="4" * 64),
            "graph_state_schema_version": "state@1",
        },
        contracts={
            "contracts": VersionedRef(ref="contracts.core@1", version="1", content_hash="5" * 64),
            "converter_bundle": None,
        },
        runtime_manifest=VersionedRef(ref="manifest.root@1", version="1", content_hash="6" * 64),
        runtime_build=VersionedRef(ref="build.root@1", version="1", content_hash="7" * 64),
        permission={
            "run_authority_ref": "authority@1",
            "run_authority_hash": "8" * 64,
            "authorization_policy": VersionedRef(ref="auth.policy@1", version="1", content_hash="9" * 64),
            "permission_preset": VersionedRef(ref="permission.interactive@1", version="1", content_hash="a" * 64),
            "permission_envelope_hash": "b" * 64,
            "effective_scopes": ("domain:read",),
        },
        required_capabilities=("graph", "knowledge"),
        budget={
            "evaluation_envelope": VersionedRef(ref="budget.eval@1", version="1", content_hash="c" * 64),
            "effective_budget": VersionedRef(ref="budget.eval@1", version="1", content_hash="c" * 64),
        },
        policy_refs=(
            {"kind": "model", "policy": VersionedRef(ref="model.policy@1", version="1", content_hash="d" * 64)},
            {"kind": "prompt", "policy": VersionedRef(ref="prompt.policy@1", version="1", content_hash="e" * 64)},
        ),
        evaluation_evidence_set=VersionedRef(ref="evidence.set@1", version="1", content_hash="f" * 64),
        resolver_version="resolver@1",
        resolved_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_manifest_hash_has_no_empty_or_null_compatibility_cleanup() -> None:
    manifest, binding = _manifest()
    assert (
        manifest.manifest_hash
        == hashlib.sha256(canonical_bytes(manifest, exclude_fields=("manifest_hash",))).hexdigest()
    )
    for field, value in (
        ("knowledge_bindings", ()),
        ("action_bindings", ()),
        ("input_limit_allowlist", ()),
    ):
        tampered = manifest.model_copy(update={field: value})
        with pytest.raises(ArtifactHashMismatchError):
            verify_runtime_manifest(tampered, expected_manifest_hash=manifest.manifest_hash)
    with_null = manifest.model_copy(
        update={"tool_bindings": (binding.model_copy(update={"timeout_policy_ref": None}),)}
    )
    with pytest.raises(ArtifactHashMismatchError):
        verify_runtime_manifest(with_null, expected_manifest_hash=manifest.manifest_hash)


def test_typed_instances_require_exact_manifest_registry_and_revalidation() -> None:
    manifest, binding = _manifest()
    registry = TypedSchemaRegistry()
    registry.register(binding.input_schema_ref, StrictInput, role="input")
    registry.register(binding.output_schema_ref, StrictOutput, role="output")
    payload = ToolProposalPayload[StrictInput](
        kind="tool_proposal",
        tool_ref=binding.tool_ref,
        input=StrictInput(value="x"),
        rationale_summary="r",
        confidence=0.5,
    )
    parsed_instance = parse_inference_decision(
        payload,
        manifest=manifest,
        schema_registry=registry,
        expected_manifest_hash=manifest.manifest_hash,
    )
    assert isinstance(cast(Any, parsed_instance).input, StrictInput)
    parsed_raw = parse_inference_decision(
        payload.model_dump(),
        manifest=manifest,
        schema_registry=registry,
        expected_manifest_hash=manifest.manifest_hash,
    )
    assert isinstance(cast(Any, parsed_raw).input, StrictInput)
    with pytest.raises(TypeError):
        cast(Any, parse_inference_decision)(payload)

    class OtherInput(BaseModel):
        model_config = {"extra": "forbid", "frozen": True}
        value: str

    wrong_registry = TypedSchemaRegistry()
    wrong_registry.register(binding.input_schema_ref, OtherInput, role="input")
    with pytest.raises(ValueError):
        parse_inference_decision(
            payload,
            manifest=manifest,
            schema_registry=wrong_registry,
            expected_manifest_hash=manifest.manifest_hash,
        )

    loose_payload = ToolProposalPayload[LooseOutput](
        kind="tool_proposal",
        tool_ref=binding.tool_ref,
        input=LooseOutput(answer="cleaned"),
        rationale_summary="r",
        confidence=0.5,
    )
    with pytest.raises(ValueError):
        parse_inference_decision(
            loose_payload,
            manifest=manifest,
            schema_registry=registry,
            expected_manifest_hash=manifest.manifest_hash,
        )
    with pytest.raises((TypeError, ValueError)):
        enrich_decision(
            loose_payload,
            meta=_meta(),
            run_id=uuid4(),
            decision_id=uuid4(),
            expected_manifest_hash="a" * 64,
        )
    forged = ToolProposalPayload[StrictInput].model_construct(
        kind="tool_proposal",
        tool_ref=binding.tool_ref,
        input={"value": "bypassed"},
        rationale_summary="r",
        confidence=0.5,
    )
    with pytest.raises(ValueError):
        parse_inference_decision(
            forged,
            manifest=manifest,
            schema_registry=registry,
            expected_manifest_hash=manifest.manifest_hash,
        )


def test_tool_builder_rejects_duck_typed_manifest_before_command() -> None:
    manifest, binding = _manifest()
    registry = TypedSchemaRegistry()
    registry.register(binding.input_schema_ref, StrictInput, role="input")
    decision = cast(
        ToolProposal[StrictInput],
        enrich_decision(
            ToolProposalPayload[StrictInput](
                kind="tool_proposal",
                tool_ref=binding.tool_ref,
                input=StrictInput(value="x"),
                rationale_summary="r",
                confidence=0.5,
            ),
            meta=_meta(),
            run_id=uuid4(),
            decision_id=uuid4(),
            manifest=manifest,
            schema_registry=registry,
            expected_manifest_hash=manifest.manifest_hash,
        ),
    )
    kwargs: dict[str, Any] = {
        "authorization_decision_ref": "auth@1",
        "tool_request_id": uuid4(),
        "timeout_policy_ref": "timeout@1",
        "expected_manifest_hash": manifest.manifest_hash,
        "tool_binding": binding,
        "schema_registry": registry,
    }

    class FakeManifest:
        def verify(self, **_: Any) -> None:
            return None

        def find_tool_binding(self, _: Any) -> ToolBinding:
            return binding

    with pytest.raises(TypeError):
        tool_command_from_decision(decision, manifest=FakeManifest(), **kwargs)
    command = build_tool_command_from_decision(decision, manifest=manifest, **kwargs)
    assert command.tool_ref == binding.tool_ref
    with pytest.raises(ArtifactHashMismatchError):
        build_tool_command_from_decision(
            decision,
            manifest=manifest,
            artifact_payloads={binding.tool_ref.ref: b"tampered"},
            **kwargs,
        )


def test_dependency_checker_rejects_all_dynamic_loading_primitives(tmp_path: Path) -> None:
    contracts = tmp_path / "app" / "contracts"
    contracts.mkdir(parents=True)
    (contracts / "bad.py").write_text(
        "import importlib as il\n"
        "from importlib import import_module as load\n"
        "def f():\n"
        "    name = 'import_' + 'module'\n"
        "    getattr(il, name)('x')\n"
        "    return eval('1') + exec('pass') + __import__('x')\n"
        "\n",
        encoding="utf-8",
    )
    violations = find_violations(tmp_path)
    assert sum("dynamic import" in item for item in violations) >= 5


def test_registry_type_closure_matches_canonical_serializer() -> None:
    class BadEnum(Enum):
        VALUE = "value"

    class BadSet(BaseModel):
        model_config = {"extra": "forbid", "frozen": True}
        value: set[str]

    class BadEnumModel(BaseModel):
        model_config = {"extra": "forbid", "frozen": True}
        value: BadEnum

    class BadDecimal(BaseModel):
        model_config = {"extra": "forbid", "frozen": True}
        value: Decimal

    class BadFraction(BaseModel):
        model_config = {"extra": "forbid", "frozen": True}
        value: Fraction

    registry = TypedSchemaRegistry()
    for index, schema in enumerate((BadSet, BadEnumModel, BadDecimal, BadFraction)):
        with pytest.raises(ValueError):
            registry.register(_ref(f"schema.bad.{index}@1"), schema, role="input")

    class Allowed(BaseModel):
        model_config = {"extra": "forbid", "frozen": True}
        mapping: dict[str, tuple[int, ...]]
        identifier: UUID
        observed_at: datetime
        value: float

    allowed_ref = _ref("schema.allowed@1")
    registry.register(allowed_ref, Allowed, role="input")
    parsed = registry.resolve(allowed_ref, role="input").validate_python(
        {
            "mapping": {"a": (1, 2)},
            "identifier": uuid4(),
            "observed_at": datetime(2026, 1, 1, tzinfo=UTC),
            "value": 1.25,
        }
    )
    assert canonical_bytes(parsed)
    with pytest.raises(ValueError):
        registry.resolve(allowed_ref, role="input").validate_python(
            {
                "mapping": {"a": (1,)},
                "identifier": uuid4(),
                "observed_at": datetime(2026, 1, 1, tzinfo=UTC),
                "value": float("nan"),
            }
        )


def test_schema_annotation_and_adapter_error_matrix() -> None:
    class NestedMap(BaseModel):
        model_config = {"extra": "forbid", "frozen": True}
        values: dict[str, int]

    class AnnotatedNested(BaseModel):
        model_config = {"extra": "forbid", "frozen": True}
        value: Annotated[NestedMap, "nested"]

    class UnionMap(BaseModel):
        model_config = {"extra": "forbid", "frozen": True}
        value: dict[str, int] | str

    class ListMap(BaseModel):
        model_config = {"extra": "forbid", "frozen": True}
        value: list[dict[str, int]]

    class Recursive(BaseModel):
        model_config = {"extra": "forbid", "frozen": True}
        value: str
        children: list[Recursive] = Field(default_factory=list)

    class EnumValue(Enum):
        VALUE = "value"

    safe_annotations: tuple[Any, ...] = (
        Annotated[str, "metadata"],
        Literal["ok"],
        str | int,
        list[str],
        tuple[int, ...],
        ClassVar[str],
    )
    for annotation in safe_annotations:
        _assert_safe_annotation(annotation, set(), allow_mapping=True)
    unsafe_annotations: tuple[Any, ...] = (
        set[str],
        frozenset[str],
        dict[int, str],
        Literal[EnumValue.VALUE],
        EnumValue,
        Any,
    )
    for annotation in unsafe_annotations:
        with pytest.raises(ValueError):
            _assert_safe_annotation(annotation, set(), allow_mapping=True)
    with pytest.raises(ValueError):
        _assert_safe_annotation(dict[str, str], set())
    with pytest.raises(ValueError):
        _assert_safe_annotation(list, set())
    with pytest.raises(ValueError):
        _assert_safe_annotation("unresolved", set())

    # These paths exercise recursive type closure discovery used by the
    # legacy scalar adapter while the registry uses its canonical adapter.
    for schema in (AnnotatedNested, UnionMap, ListMap):
        with pytest.raises(ValueError):
            _schema_adapter(schema)
    assert _schema_adapter(Recursive)
    allowed_ref = _ref("schema.adapter@1")
    registry = TypedSchemaRegistry()
    registry.register(allowed_ref, AnnotatedNested, role="input")
    exact = _resolve_schema_adapter(registry, allowed_ref, role="input")
    assert exact.validate_json('{"value":{"values":{"x":1}}}')
    assert _resolve_schema_model(registry, allowed_ref, role="input") is AnnotatedNested
    with pytest.raises(TypeError):
        _payload_schema_types(object())
    with pytest.raises(ValueError):
        canonical_bytes({"value": float("nan")})


def test_reader_and_manifest_schema_error_matrix() -> None:
    manifest, binding = _manifest()
    registry = TypedSchemaRegistry()
    registry.register(binding.input_schema_ref, StrictInput, role="input")
    registry.register(binding.output_schema_ref, StrictOutput, role="output")
    final = FinalAnswer[StrictOutput](
        kind="final_answer",
        meta=ContractMeta(
            contract_name="canonical.decision",
            contract_version="v1",
            message_id=uuid4(),
            tenant_id="tenant-a",
            correlation_id="corr-a",
        ),
        run_id=uuid4(),
        decision_id=uuid4(),
        output=StrictOutput(answer="ok"),
        artifact_refs=(),
        rationale_summary="r",
        confidence=0.5,
    )
    with pytest.raises((TypeError, ValueError)):
        parse_canonical_decision(final, expected_manifest_hash="a" * 64)
    assert (
        parse_canonical_decision(
            final,
            manifest=manifest,
            schema_registry=registry,
            expected_manifest_hash=manifest.manifest_hash,
        ).kind
        == "final_answer"
    )
    with pytest.raises(TypeError):
        read_contract("decision", final, version="v1", expected_manifest_hash="a" * 64)
    with pytest.raises(TypeError):
        read_contract(
            "decision",
            final,
            version="v1",
            manifest=manifest,
            schema_registry={},
            expected_manifest_hash="a" * 64,
        )

    action = ActionProposal[StrictInput](
        kind="action_proposal",
        meta=final.meta,
        run_id=final.run_id,
        decision_id=final.decision_id,
        action_ref="action@1",
        input=StrictInput(value="x"),
        rationale_summary="r",
        confidence=0.5,
    )
    delegate = DelegateProposal[StrictInput](
        kind="delegate_proposal",
        meta=final.meta,
        run_id=final.run_id,
        decision_id=final.decision_id,
        target_skill_ref="skill.child@1",
        input=StrictInput(value="x"),
        rationale_summary="r",
        confidence=0.5,
    )
    with pytest.raises((TypeError, ValueError)):
        parse_canonical_decision(action, input_type=StrictInput, expected_manifest_hash="a" * 64)
    with pytest.raises((TypeError, ValueError)):
        parse_canonical_decision(delegate, input_type=StrictInput, expected_manifest_hash="a" * 64)
    assert _manifest_schema_for_payload(
        {"kind": "final_answer"}, manifest, expected_manifest_hash=manifest.manifest_hash
    ) == (None, binding.output_schema_ref)
    with pytest.raises(ValueError):
        _manifest_schema_for_payload({"kind": "unknown"}, manifest, expected_manifest_hash=manifest.manifest_hash)


def test_closure_verifier_rejects_each_proof_shape_and_content_path() -> None:
    root_bytes = b"root-runtime"
    dep_bytes = b"dep-runtime"
    root_ref = _ref("skill.root@1", root_bytes)
    dep_ref = _ref("skill.dep@1", dep_bytes)
    root = DependencyNode(root_ref, (dep_ref,), artifact_payload=root_bytes)
    dep = DependencyNode(dep_ref, artifact_payload=dep_bytes)
    assert "skill.root@1" in repr(root)
    resolver = _ref("resolver@1", b"resolver")
    proof = issue_dependency_closure_proof(root, (dep,), resolver_version=resolver)
    dependency = DependencyBinding(
        skill_ref=dep_ref,
        manifest_ref=_ref("manifest.dep@1", b"manifest"),
    )
    manifest = SkillRuntimeManifest(
        manifest_version="v1",
        skill_ref=root_ref,
        input_schema_ref=_ref("schema.in@1", b"in"),
        output_schema_ref=_ref("schema.out@1", b"out"),
        dependencies=(dependency,),
        tool_bindings=(),
        skill_closure=proof.closure,
        dependency_closure_proof=proof,
        required_capabilities=(),
    ).with_hash()
    payloads = {root_ref.ref: root_bytes, dep_ref.ref: dep_bytes}
    verify_runtime_manifest(manifest, expected_manifest_hash=manifest.manifest_hash, artifact_payloads=payloads)
    assert resolve_dependency_closure(root, (dep,), content_loader=lambda ref: payloads[ref.ref]) == proof.closure
    with pytest.raises(MissingArtifactError):
        resolve_dependency_closure(root, (dep,), content_loader=lambda _: (_ for _ in ()).throw(KeyError()))
    with pytest.raises(MissingArtifactError):
        resolve_dependency_closure(root, (dep,), artifact_payloads={})
    with pytest.raises(MissingArtifactError):
        issue_dependency_closure_proof(DependencyNode(root_ref), (), resolver_version=resolver)
    assert compute_manifest_hash(manifest) == manifest.manifest_hash

    empty_graph = manifest.model_copy(
        update={"dependency_closure_proof": proof.model_copy(update={"dependency_graph": ()})}
    ).with_hash()
    with pytest.raises(ClosureViolationError):
        verify_runtime_manifest(
            empty_graph, expected_manifest_hash=empty_graph.manifest_hash, artifact_payloads=payloads
        )
    missing_root = manifest.model_copy(
        update={"dependency_closure_proof": proof.model_copy(update={"root": _ref("skill.other@1", b"other")})}
    ).with_hash()
    with pytest.raises(ClosureViolationError):
        verify_runtime_manifest(
            missing_root, expected_manifest_hash=missing_root.manifest_hash, artifact_payloads=payloads
        )
    unreachable = manifest.model_copy(
        update={
            "dependency_closure_proof": proof.model_copy(
                update={
                    "dependency_graph": proof.dependency_graph
                    + (DependencyGraphEntry(skill=_ref("skill.evil@1", b"evil")),)
                }
            )
        }
    ).with_hash()
    with pytest.raises(ClosureViolationError):
        verify_runtime_manifest(
            unreachable, expected_manifest_hash=unreachable.manifest_hash, artifact_payloads=payloads
        )
    wrong_closure = manifest.model_copy(
        update={"dependency_closure_proof": proof.model_copy(update={"closure": (root_ref,)})}
    ).with_hash()
    with pytest.raises(ClosureViolationError):
        verify_runtime_manifest(
            wrong_closure, expected_manifest_hash=wrong_closure.manifest_hash, artifact_payloads=payloads
        )
    wrong_dependencies = manifest.model_copy(update={"dependencies": ()}).with_hash()
    with pytest.raises(ClosureViolationError):
        verify_runtime_manifest(
            wrong_dependencies, expected_manifest_hash=wrong_dependencies.manifest_hash, artifact_payloads=payloads
        )
    cyclic = manifest.model_copy(
        update={
            "dependency_closure_proof": proof.model_copy(
                update={
                    "dependency_graph": (
                        DependencyGraphEntry(skill=root_ref, dependencies=(dep_ref,)),
                        DependencyGraphEntry(skill=dep_ref, dependencies=(root_ref,)),
                    )
                }
            )
        }
    ).with_hash()
    with pytest.raises(DependencyCycleError):
        verify_runtime_manifest(cyclic, expected_manifest_hash=cyclic.manifest_hash, artifact_payloads=payloads)
    no_proof = SkillRuntimeManifest(
        manifest_version="v1",
        skill_ref=root_ref,
        input_schema_ref=_ref("schema.in@1", b"in"),
        output_schema_ref=_ref("schema.out@1", b"out"),
        dependencies=(dependency,),
        tool_bindings=(),
        required_capabilities=(),
    ).with_hash()
    with pytest.raises(ClosureViolationError):
        verify_runtime_manifest(no_proof, expected_manifest_hash=no_proof.manifest_hash)


def test_runtime_manifest_and_spec_failure_class_matrix() -> None:
    manifest, binding = _manifest()
    with pytest.raises(TypeError):
        compute_manifest_hash(object())
    with pytest.raises(ArtifactHashMismatchError):
        verify_runtime_manifest(manifest, expected_manifest_hash="bad")
    with pytest.raises(ArtifactHashMismatchError):
        verify_runtime_manifest(manifest, expected_manifest_hash="0" * 64)
    with pytest.raises(ArtifactHashMismatchError):
        verify_runtime_manifest(
            manifest.model_copy(update={"manifest_hash": "0" * 64}), expected_manifest_hash="0" * 64
        )
    assert binding.tool_ref.ref == "tool.read@1"
    with pytest.raises(ArtifactHashMismatchError):
        verify_skill_execution_spec(_spec().model_copy(update={"evaluation_subject_hash": "0" * 64}))


def test_manifest_and_spec_model_validator_matrix() -> None:
    manifest, binding = _manifest()
    assert binding.tool is binding.tool_ref
    assert binding.input_schema is binding.input_schema_ref
    assert binding.output_schema is binding.output_schema_ref
    dependency = DependencyBinding(skill_ref=_ref("skill.dep@1", b"dep"), manifest_ref=_ref("manifest.dep@1", b"m"))
    assert dependency.skill is dependency.skill_ref
    assert dependency.manifest is dependency.manifest_ref
    for field, values in (
        ("dependencies", (dependency, dependency)),
        ("tool_bindings", (binding, binding)),
        ("action_bindings", (_ref("action@1"), _ref("action@1"))),
        ("skill_closure", (_ref("skill@1"), _ref("skill@1"))),
        (
            "input_limit_allowlist",
            (
                InputLimitBinding(
                    key="max_rows",
                    limit_schema_ref=_ref("limit.schema@1"),
                    comparator="positive_integer_componentwise_lte",
                    ceiling=10,
                    failure_policy_ref=_ref("failure@1"),
                ),
            )
            * 2,
        ),
    ):
        with pytest.raises(ValidationError):
            SkillRuntimeManifest.model_validate(
                {
                    **manifest.model_dump(mode="python", exclude_unset=False),
                    field: values,
                    "manifest_hash": "",
                }
            )
    with pytest.raises(ValidationError):
        SkillRuntimeManifest.model_validate(
            {**manifest.model_dump(mode="python", exclude_unset=False), "required_capabilities": ("Bad Capability",)}
        )
    with pytest.raises(ValidationError):
        SkillRuntimeManifest.model_validate(
            {**manifest.model_dump(mode="python", exclude_unset=False), "manifest_hash": "bad"}
        )

    spec = _spec()
    with pytest.raises(ValidationError):
        SkillExecutionSpec.model_validate({**spec.model_dump(mode="python", exclude_unset=False), "abi_version": "v9"})
    duplicate_policy = spec.policy_refs[0]
    with pytest.raises(ValidationError):
        SkillExecutionSpec.model_validate(
            {**spec.model_dump(mode="python", exclude_unset=False), "policy_refs": (duplicate_policy, duplicate_policy)}
        )
    workspace = PolicyRef(kind="workspace", policy=_ref("workspace@1"))
    with pytest.raises(ValidationError):
        SkillExecutionSpec.model_validate(
            {
                **spec.model_dump(mode="python", exclude_unset=False),
                "policy_refs": (workspace,),
                "required_capabilities": (),
            }
        )
    memory = PolicyRef(kind="memory", policy=_ref("memory@1"))
    with pytest.raises(ValidationError):
        SkillExecutionSpec.model_validate(
            {
                **spec.model_dump(mode="python", exclude_unset=False),
                "policy_refs": (memory,),
                "required_capabilities": (),
            }
        )
    with pytest.raises(ValidationError):
        SkillExecutionSpec.model_validate(
            {
                **spec.model_dump(mode="python", exclude_unset=False),
                "evaluation_subject_hash": "bad",
                "skill_spec_hash": "bad",
            }
        )
    different_budget = _ref("budget.other@1")
    with pytest.raises(ValidationError):
        SkillExecutionSpec.model_validate(
            {
                **spec.model_dump(mode="python", exclude_unset=False),
                "budget": {
                    "evaluation_envelope": spec.budget.evaluation_envelope,
                    "effective_budget": different_budget,
                },
            }
        )
    with pytest.raises(ValidationError):
        DependencyGraphEntry.model_validate({"skill": _ref("skill@1"), "dependencies": (_ref("dep@1"), _ref("dep@1"))})
    with pytest.raises(ValidationError):
        DependencyClosureProof.model_validate(
            {
                "resolver_version": _ref("resolver@1"),
                "root": _ref("skill@1"),
                "closure": (_ref("skill@1"), _ref("skill@1")),
                "proof_hash": "a" * 64,
            }
        )
    duplicate_graph = DependencyGraphEntry(skill=_ref("skill.graph@1"), dependencies=())
    with pytest.raises(ValidationError):
        DependencyClosureProof.model_validate(
            {
                "resolver_version": _ref("resolver.graph@1"),
                "root": duplicate_graph.skill,
                "closure": (duplicate_graph.skill,),
                "dependency_graph": (duplicate_graph, duplicate_graph),
                "proof_hash": "a" * 64,
            }
        )
    knowledge = KnowledgeBinding(
        knowledge_ref=_ref("knowledge@1"),
        snapshot_ref=_ref("snapshot@1"),
        retrieval_policy_ref=_ref("retrieval@1"),
        limits_policy_ref=_ref("limits@1"),
        partial_policy_ref=_ref("partial@1"),
        selection_policy_ref=_ref("selection@1"),
        adapter_compatibility_ref=_ref("adapter@1"),
    )
    with pytest.raises(ValidationError):
        SkillRuntimeManifest.model_validate(
            {**manifest.model_dump(mode="python", exclude_unset=False), "knowledge_bindings": (knowledge, knowledge)}
        )
    alias_payload = {**manifest.model_dump(mode="python", exclude_unset=False), "closure_proof": None}
    alias_payload.pop("dependency_closure_proof", None)
    assert SkillRuntimeManifest.model_validate(alias_payload).closure_proof is None
    assert manifest.skill is manifest.skill_ref
    assert manifest.input_schema is manifest.input_schema_ref
    assert manifest.output_schema is manifest.output_schema_ref
    assert manifest.closure_proof is None
    assert manifest.find_tool_binding(binding.tool_ref.ref) == binding
    manifest.verify(expected_manifest_hash=manifest.manifest_hash)
    with pytest.raises(ValueError):
        manifest.find_tool_binding("outside@1")
    with pytest.raises(ValueError):
        manifest.find_knowledge_binding("knowledge@1")


def test_knowledge_filter_is_monotonic() -> None:
    decision = enrich_knowledge_decision(
        KnowledgeProposalPayload(
            kind="knowledge_proposal",
            query="q",
            knowledge_refs=(),
            filter=KnowledgeFilter(tags=("a",), language="zh"),
            rationale_summary="r",
            confidence=0.5,
        ),
        meta=_meta(),
        run_id=uuid4(),
        decision_id=uuid4(),
    )
    budget = RetrievalBudget(max_results=1, max_bytes=1, max_tokens=1, deadline_ms=1)
    with pytest.raises(ValueError):
        knowledge_request_from_decision(
            decision,
            authorization_decision_ref="auth@1",
            knowledge_request_id=uuid4(),
            purpose="p",
            filter=KnowledgeFilter(),
            budget=budget,
        )
    with pytest.raises(TypeError):
        knowledge_request_from_decision(
            decision,
            authorization_decision_ref="auth@1",
            knowledge_request_id=uuid4(),
            purpose="p",
            filter=cast(Any, object()),
            budget=budget,
        )
    with pytest.raises(ValueError):
        knowledge_request_from_decision(
            decision,
            authorization_decision_ref="auth@1",
            knowledge_request_id=uuid4(),
            purpose="p",
            filter=KnowledgeFilter(language="zh"),
            budget=budget,
        )
    request = knowledge_request_from_decision(
        decision,
        authorization_decision_ref="auth@1",
        knowledge_request_id=uuid4(),
        purpose="p",
        filter=KnowledgeFilter(tags=("a", "b"), language="zh"),
        budget=budget,
    )
    assert request.filter.tags == ("a", "b")
