"""Focused WS-1 round-2 fail-closed regression matrix."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast
from uuid import uuid4

import pytest
from app.contracts import (
    ContractMeta,
    FinalAnswer,
    FinalAnswerPayload,
    KnowledgeProposalPayload,
    ToolProposal,
    ToolProposalPayload,
    TypedSchemaRegistry,
    VersionedRef,
    enrich_decision,
    enrich_knowledge_decision,
    parse_canonical_decision,
    parse_inference_decision,
    read_contract,
    tool_command_from_decision,
)
from app.contracts.canonical import (
    _manifest_schema_for_payload,
    _resolve_schema_adapter,
    _schema_adapter,
)
from app.skill_abi import (
    ABIConversionError,
    ABIConverterRegistry,
    ArtifactHashMismatchError,
    ClosureViolationError,
    DependencyBinding,
    DependencyCycleError,
    DependencyGraphEntry,
    DependencyNode,
    MissingArtifactError,
    SkillRuntimeManifest,
    ToolBinding,
    issue_dependency_closure_proof,
    resolve_dependency_closure,
    verify_runtime_manifest,
)
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from scripts.check_contract_dependencies import find_violations


class Nested(BaseModel):
    model_config = {"extra": "forbid", "frozen": True}
    text: str


class NestedLoose(BaseModel):
    text: str


class Input(BaseModel):
    model_config = {"extra": "forbid", "frozen": True}
    value: str
    nested: Nested | None = None


class Output(BaseModel):
    model_config = {"extra": "forbid", "frozen": True}
    answer: str


def _ref(name: str, payload: bytes) -> VersionedRef:
    return VersionedRef(ref=name, version="1", content_hash=hashlib.sha256(payload).hexdigest())


def _meta(name: str = "canonical.inference") -> ContractMeta:
    return ContractMeta(
        contract_name=name,
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
        input_schema_ref=_ref("schema.input@1", b"input-schema"),
        output_schema_ref=_ref("schema.output@1", b"output-schema"),
        limits_policy_ref=_ref("limits@1", b"limits"),
        adapter_compatibility_ref=_ref("adapter@1", b"adapter"),
        partial_policy_ref=_ref("partial@1", b"partial"),
        selection_policy_ref=_ref("selection@1", b"selection"),
        timeout_policy_ref=_ref("timeout@1", b"timeout"),
        logical_call_budget=1,
    )
    manifest = SkillRuntimeManifest(
        manifest_version="v1",
        skill_ref=_ref("skill.root@1", b"root"),
        input_schema_ref=binding.input_schema_ref,
        output_schema_ref=binding.output_schema_ref,
        dependencies=(),
        tool_bindings=(binding,),
        required_capabilities=(),
    ).with_hash()
    return manifest, binding


def test_typed_registry_rejects_open_models_and_reader_preserves_envelope() -> None:
    manifest, binding = _manifest()
    registry = TypedSchemaRegistry()
    registry.register(binding.input_schema_ref, Input, role="input")
    registry.register(binding.output_schema_ref, Output, role="output")
    with pytest.raises(TypeError):
        registry.register(binding.input_schema_ref, TypeAdapter(Input), role="input")
    with pytest.raises(ValueError):
        registry.register(binding.input_schema_ref, NestedLoose, role="input")

    class HasLooseNested(BaseModel):
        model_config = {"extra": "forbid", "frozen": True}
        nested: NestedLoose

    with pytest.raises(ValueError):
        registry.register(binding.input_schema_ref, HasLooseNested, role="input")

    raw = {
        "kind": "tool_proposal",
        "tool_ref": binding.tool_ref.model_dump(mode="python"),
        "input": {"value": "kept", "nested": {"text": "nested"}},
        "rationale_summary": "r",
        "confidence": 0.5,
    }
    parsed = parse_inference_decision(
        raw, manifest=manifest, schema_registry=registry, expected_manifest_hash=manifest.manifest_hash
    )
    assert isinstance(parsed, ToolProposalPayload)
    assert isinstance(parsed.input, Input)
    assert parsed.input.nested == Nested(text="nested")
    with pytest.raises((TypeError, ValueError)):
        parse_inference_decision(raw, expected_manifest_hash="a" * 64)
    with pytest.raises(ValidationError):
        parse_inference_decision(
            {**raw, "top_extra": True},
            manifest=manifest,
            schema_registry=registry,
            expected_manifest_hash=manifest.manifest_hash,
        )
    with pytest.raises(ValueError):
        parse_inference_decision(
            {**raw, "input": {"value": "x", "nested": {"text": "x", "sql": "drop"}}},
            manifest=manifest,
            schema_registry=registry,
            expected_manifest_hash=manifest.manifest_hash,
        )
    read = read_contract(
        "inference.payload",
        raw,
        version="v1",
        manifest=manifest,
        schema_registry=registry,
        expected_manifest_hash=manifest.manifest_hash,
    )
    assert isinstance(read, ToolProposalPayload)
    with pytest.raises(ValidationError):
        read_contract(
            "inference.payload",
            {**raw, "extra": 1},
            version="v1",
            manifest=manifest,
            schema_registry=registry,
            expected_manifest_hash=manifest.manifest_hash,
        )


def test_closed_canonical_readers_and_family_version_fail_closed() -> None:
    manifest, binding = _manifest()
    registry = TypedSchemaRegistry()
    registry.register(binding.input_schema_ref, Input, role="input")
    registry.register(binding.output_schema_ref, Output, role="output")
    final_raw = {
        "kind": "final_answer",
        "output": {"answer": "ok"},
        "rationale_summary": "r",
        "confidence": 0.5,
    }
    parsed = parse_inference_decision(
        final_raw, manifest=manifest, schema_registry=registry, expected_manifest_hash=manifest.manifest_hash
    )
    assert isinstance(parsed, FinalAnswerPayload)
    assert isinstance(parsed.output, Output)
    final = enrich_decision(
        parsed,
        meta=_meta(),
        run_id=uuid4(),
        decision_id=uuid4(),
        manifest=manifest,
        schema_registry=registry,
        expected_manifest_hash=manifest.manifest_hash,
    )
    assert isinstance(final, FinalAnswer)
    canonical = parse_canonical_decision(
        final.model_dump(),
        manifest=manifest,
        schema_registry=registry,
        expected_manifest_hash=manifest.manifest_hash,
    )
    assert isinstance(canonical, FinalAnswer)
    with pytest.raises((TypeError, ValueError)):
        parse_canonical_decision(final.model_dump(), expected_manifest_hash="a" * 64)
    with pytest.raises((TypeError, ValueError)):
        parse_canonical_decision(
            {**final.model_dump(), "top_extra": True},
            output_type=Output,
            expected_manifest_hash="a" * 64,
        )
    with pytest.raises((TypeError, ValueError)):
        parse_canonical_decision(
            {"kind": "action_proposal", "action_ref": "action@1", "input": {"value": "x"}},
            expected_manifest_hash="a" * 64,
        )
    with pytest.raises(ValueError):
        read_contract(
            "decision",
            final.model_dump(),
            version="v2",
            output_type=Output,
            expected_manifest_hash="a" * 64,
        )
    with pytest.raises(TypeError):
        parse_inference_decision(42, expected_manifest_hash="a" * 64)
    with pytest.raises(TypeError):
        _manifest_schema_for_payload(
            {"kind": "tool_proposal", "tool_ref": "missing@1"},
            object(),
            expected_manifest_hash="a" * 64,
        )
    with pytest.raises(ValueError):
        _manifest_schema_for_payload({"kind": "unknown"}, manifest, expected_manifest_hash=manifest.manifest_hash)


def test_command_builder_requires_verified_exact_binding_and_schema() -> None:
    manifest, binding = _manifest()
    registry = TypedSchemaRegistry()
    registry.register(binding.input_schema_ref, Input, role="input")
    decision = enrich_decision(
        ToolProposalPayload[Input](
            kind="tool_proposal",
            tool_ref=binding.tool_ref,
            input=Input(value="x"),
            rationale_summary="r",
            confidence=0.5,
        ),
        meta=_meta(),
        run_id=uuid4(),
        decision_id=uuid4(),
        manifest=manifest,
        schema_registry=registry,
        expected_manifest_hash=manifest.manifest_hash,
    )
    assert isinstance(decision, ToolProposal)
    command = tool_command_from_decision(
        decision,
        authorization_decision_ref="auth@1",
        tool_request_id=uuid4(),
        timeout_policy_ref=binding.timeout_policy_ref.ref if binding.timeout_policy_ref else "timeout@1",
        manifest=manifest,
        expected_manifest_hash=manifest.manifest_hash,
        tool_binding=binding,
        schema_registry=registry,
    )
    assert command.tool_ref == binding.tool_ref
    with pytest.raises(ValueError):
        tool_command_from_decision(
            decision,
            authorization_decision_ref="auth@1",
            tool_request_id=uuid4(),
            timeout_policy_ref="timeout@1",
            manifest=manifest,
            expected_manifest_hash="0" * 64,
            tool_binding=binding,
            schema_registry=registry,
        )
    with pytest.raises(ValueError):
        tool_command_from_decision(
            decision,
            authorization_decision_ref="auth@1",
            tool_request_id=uuid4(),
            timeout_policy_ref="timeout@1",
            manifest=manifest,
            expected_manifest_hash=manifest.manifest_hash,
            tool_binding=binding.model_copy(update={"logical_call_budget": 0}),
            schema_registry=registry,
        )
    with pytest.raises(TypeError):
        tool_command_from_decision(
            decision,
            authorization_decision_ref="auth@1",
            tool_request_id=uuid4(),
            timeout_policy_ref="timeout@1",
            manifest=cast(Any, object()),
            expected_manifest_hash=manifest.manifest_hash,
            tool_binding=binding,
            schema_registry=registry,
        )
    with pytest.raises(TypeError):
        tool_command_from_decision(
            decision,
            authorization_decision_ref="auth@1",
            tool_request_id=uuid4(),
            timeout_policy_ref="timeout@1",
            manifest=manifest,
            expected_manifest_hash=manifest.manifest_hash,
            tool_binding=cast(Any, object()),
            schema_registry=registry,
        )
    with pytest.raises(ValueError):
        tool_command_from_decision(
            decision,
            authorization_decision_ref="auth@1",
            tool_request_id=uuid4(),
            timeout_policy_ref="timeout@1",
            manifest=manifest,
            expected_manifest_hash=manifest.manifest_hash,
            tool_binding=binding.model_copy(update={"operation": "write"}),
            schema_registry=registry,
        )
    with pytest.raises(ValueError):
        tool_command_from_decision(
            decision,
            authorization_decision_ref="auth@1",
            tool_request_id=uuid4(),
            timeout_policy_ref="timeout@1",
            manifest=manifest,
            expected_manifest_hash=manifest.manifest_hash,
            tool_binding=binding,
            schema_registry=TypedSchemaRegistry(),
        )
    with pytest.raises(ValueError):
        tool_command_from_decision(
            decision.model_copy(update={"tool_ref": "tool.read@1"}),
            authorization_decision_ref="auth@1",
            tool_request_id=uuid4(),
            timeout_policy_ref="timeout@1",
            manifest=manifest,
            expected_manifest_hash=manifest.manifest_hash,
            tool_binding=binding,
            schema_registry=registry,
        )
    with pytest.raises(ValueError):
        tool_command_from_decision(
            decision,
            authorization_decision_ref="auth@1",
            tool_request_id=uuid4(),
            timeout_policy_ref="wrong@1",
            manifest=manifest,
            expected_manifest_hash=manifest.manifest_hash,
            tool_binding=binding,
            schema_registry=registry,
        )


def test_dependency_closure_is_process_independent_and_rejects_tampering() -> None:
    root_bytes = b"root-artifact"
    dep_bytes = b"dep-artifact"
    root_ref = _ref("skill.root@1", root_bytes)
    dep_ref = _ref("skill.dep@1", dep_bytes)
    root = DependencyNode(root_ref, (dep_ref,), artifact_payload=root_bytes)
    dep = DependencyNode(dep_ref, artifact_payload=dep_bytes)
    proof = issue_dependency_closure_proof(root, (dep,), resolver_version=_ref("resolver@1", b"resolver"))
    manifest = SkillRuntimeManifest(
        manifest_version="v1",
        skill_ref=root_ref,
        input_schema_ref=_ref("schema.in@1", b"in"),
        output_schema_ref=_ref("schema.out@1", b"out"),
        dependencies=(DependencyBinding(skill_ref=dep_ref, manifest_ref=_ref("manifest.dep@1", b"manifest")),),
        tool_bindings=(),
        skill_closure=proof.closure,
        dependency_closure_proof=proof,
        required_capabilities=(),
    ).with_hash()
    payloads = {root_ref.ref: root_bytes, dep_ref.ref: dep_bytes}
    verify_runtime_manifest(manifest, expected_manifest_hash=manifest.manifest_hash, artifact_payloads=payloads)
    script = (
        "import json,sys; from app.skill_abi import SkillRuntimeManifest,verify_runtime_manifest; "
        "m=SkillRuntimeManifest.model_validate_json(sys.stdin.readline()); "
        "h=sys.stdin.readline().strip(); p=json.loads(sys.stdin.readline()); "
        "verify_runtime_manifest(m, expected_manifest_hash=h, artifact_payloads={k:v.encode() for k,v in p.items()})"
    )
    encoded = json.dumps({key: value.decode() for key, value in payloads.items()})
    child = subprocess.run(  # noqa: S603 - deliberate fresh-process trust-boundary regression
        [sys.executable, "-c", script],
        input=f"{manifest.model_dump_json()}\n{manifest.manifest_hash}\n{encoded}\n",
        text=True,
        capture_output=True,
        check=False,
    )
    assert child.returncode == 0, child.stderr
    with pytest.raises(ArtifactHashMismatchError):
        verify_runtime_manifest(
            manifest,
            expected_manifest_hash=manifest.manifest_hash,
            artifact_payloads={root_ref.ref: b"tampered", dep_ref.ref: dep_bytes},
        )
    extra_graph = proof.dependency_graph + (DependencyGraphEntry(skill=_ref("skill.evil@1", b"evil")),)
    evil_proof = proof.model_copy(update={"dependency_graph": extra_graph})
    evil_manifest = manifest.model_copy(update={"dependency_closure_proof": evil_proof}).with_hash()
    with pytest.raises(ClosureViolationError):
        verify_runtime_manifest(
            evil_manifest,
            expected_manifest_hash=evil_manifest.manifest_hash,
            artifact_payloads=payloads,
        )
    cycle_entry = DependencyGraphEntry(skill=root_ref, dependencies=(dep_ref,))
    cycle_dep = DependencyGraphEntry(skill=dep_ref, dependencies=(root_ref,))
    cycle_proof = proof.model_copy(update={"dependency_graph": (cycle_entry, cycle_dep)})
    cycle_manifest = manifest.model_copy(update={"dependency_closure_proof": cycle_proof}).with_hash()
    with pytest.raises(DependencyCycleError):
        verify_runtime_manifest(
            cycle_manifest,
            expected_manifest_hash=cycle_manifest.manifest_hash,
            artifact_payloads=payloads,
        )
    with pytest.raises(MissingArtifactError):
        issue_dependency_closure_proof(
            root,
            (dep,),
            resolver_version=_ref("resolver@1", b"resolver"),
            artifact_payloads={},
        )


def test_dependency_checker_nested_dynamic_import_and_proposal_filter() -> None:
    from pathlib import Path
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "app/contracts").mkdir(parents=True)
        (root / "app/skill_abi").mkdir(parents=True)
        (root / "app/contracts/bad.py").write_text(
            "def load():\n import importlib as il\n f=getattr(il, 'import_module')\n return f('x')\n",
            encoding="utf-8",
        )
        assert any("dynamic import" in violation for violation in find_violations(root))
    with pytest.raises(ValidationError):
        cast(Any, KnowledgeProposalPayload)(
            kind="knowledge_proposal",
            query="q",
            knowledge_refs=(),
            rationale_summary="r",
            confidence=0.5,
        )
    with pytest.raises((TypeError, ValueError)):
        _resolve_schema_adapter({"schema@1": Input}, _ref("schema@1", b"schema"), role="input")


def test_runtime_reader_and_registry_error_paths_are_explicit() -> None:
    with pytest.raises(ValueError):
        TypeAdapter(Input).validate_python({"value": "x", "tenant_id": "bad"})
    registry = ABIConverterRegistry(supported_versions=("v1",))
    registry.register("v1", cast(Any, lambda _: (_ for _ in ()).throw(RuntimeError("bad"))))
    with pytest.raises(ValueError):
        registry.register("v1", cast(Any, lambda _: None))
    with pytest.raises(ValueError):
        registry.read({"abi_version": "v1"})
    with pytest.raises(ABIConversionError):
        registry.register("v2", cast(Any, lambda _: None))


def test_negative_branch_matrix_covers_schema_closure_and_trust_guards() -> None:
    from app.contracts.canonical import ContractReaderRegistry, _assert_safe_annotation
    from app.skill_abi import (
        DependencyConflictError,
        RetryBudget,
        SkillSpecHashMismatchError,
        build_skill_execution_spec,
        compute_manifest_hash,
        validate_closure_ref,
        validate_manifest_proposal,
        verify_skill_execution_spec,
    )
    from app.skill_abi.runtime import _closure_proof_payload

    class AnySchema(BaseModel):
        model_config = {"extra": "forbid", "frozen": True}
        value: Any

    class MappingSchema(BaseModel):
        model_config = {"extra": "forbid", "frozen": True}
        value: dict[str, str]

    class AnnotatedSchema(BaseModel):
        model_config = {"extra": "forbid", "frozen": True}
        value: Annotated[str, Field(min_length=1)]

    class LiteralSchema(BaseModel):
        model_config = {"extra": "forbid", "frozen": True}
        value: Literal["ok"]

    class OptionalSchema(BaseModel):
        model_config = {"extra": "forbid", "frozen": True}
        value: str | None

    for schema in (AnySchema, MappingSchema):
        with pytest.raises(ValueError):
            _schema_adapter(schema)
    assert _schema_adapter(AnnotatedSchema)
    assert _schema_adapter(LiteralSchema)
    assert _schema_adapter(OptionalSchema)
    with pytest.raises(TypeError):
        _schema_adapter(BaseModel)
    with pytest.raises(ValueError):
        _assert_safe_annotation("unresolved", set())
    _assert_safe_annotation(None, set())
    _assert_safe_annotation(tuple[str, ...], set())
    with pytest.raises(ValueError):
        _assert_safe_annotation(dict[str, str], set())
    with pytest.raises(ValueError):
        _assert_safe_annotation(object, set())

    reader = ContractReaderRegistry()
    reader.register("demo.contract", "v1", lambda payload: payload["ok"])
    assert reader.read("demo.contract", "v1", {"ok": True}) is True
    with pytest.raises(ValueError):
        reader.read("demo.contract", "v1", {})
    with pytest.raises(ValueError):
        reader.read("missing.contract", "v1", {})
    with pytest.raises(TypeError):
        compute_manifest_hash(object())

    root_ref = _ref("skill.root@1", b"root")
    dep_ref = _ref("skill.dep@1", b"dep")
    with pytest.raises(DependencyConflictError):
        resolve_dependency_closure(
            DependencyNode(root_ref, (dep_ref,)),
            (DependencyNode(dep_ref), DependencyNode(_ref("skill.dep@1", b"other"))),
        )
    with pytest.raises(ArtifactHashMismatchError):
        resolve_dependency_closure(DependencyNode(root_ref, artifact_payload=b"wrong"), ())
    assert resolve_dependency_closure(DependencyNode(root_ref), (), content_loader=lambda reference: b"root") == (
        root_ref,
    )
    with pytest.raises(MissingArtifactError):
        resolve_dependency_closure(
            DependencyNode(root_ref),
            (),
            content_loader=lambda reference: (_ for _ in ()).throw(KeyError()),
        )
    with pytest.raises(ClosureViolationError):
        validate_closure_ref("missing@1", (root_ref,))
    with pytest.raises(ClosureViolationError):
        validate_manifest_proposal(object(), kind="skill", reference=root_ref, expected_manifest_hash="0" * 64)
    with pytest.raises(ArtifactHashMismatchError):
        verify_runtime_manifest(_manifest()[0], expected_manifest_hash="bad")
    with pytest.raises(ArtifactHashMismatchError):
        verify_runtime_manifest(_manifest()[0], expected_manifest_hash="0" * 64)
    with pytest.raises(ValueError):
        RetryBudget(max_attempts=1, consumed=2)

    proof = issue_dependency_closure_proof(
        DependencyNode(root_ref, (dep_ref,), artifact_payload=b"root"),
        (DependencyNode(dep_ref, artifact_payload=b"dep"),),
        resolver_version=_ref("resolver@1", b"resolver"),
    )
    assert _closure_proof_payload(proof.root, proof.closure, proof.resolver_version, proof.dependency_graph)
    empty_proof = proof.model_copy(update={"dependency_graph": ()})
    empty_manifest = SkillRuntimeManifest(
        manifest_version="v1",
        skill_ref=root_ref,
        input_schema_ref=_ref("schema.in@1", b"in"),
        output_schema_ref=_ref("schema.out@1", b"out"),
        dependencies=(DependencyBinding(skill_ref=dep_ref, manifest_ref=_ref("manifest.dep@1", b"manifest")),),
        tool_bindings=(),
        skill_closure=proof.closure,
        dependency_closure_proof=empty_proof,
        required_capabilities=(),
    ).with_hash()
    with pytest.raises(ClosureViolationError):
        verify_runtime_manifest(
            empty_manifest,
            expected_manifest_hash=empty_manifest.manifest_hash,
            artifact_payloads={root_ref.ref: b"root", dep_ref.ref: b"dep"},
        )

    spec = build_skill_execution_spec(
        abi_version="v1",
        spec_id=uuid4(),
        issuer="resolver@1",
        tenant_id="tenant-a",
        run_mode="live",
        skill=root_ref,
        graph={"graph": _ref("graph@1", b"graph"), "graph_state_schema_version": "state@1"},
        contracts={"contracts": _ref("contracts@1", b"contracts")},
        runtime_manifest=_ref("manifest@1", b"manifest"),
        runtime_build=_ref("build@1", b"build"),
        permission={
            "run_authority_ref": "authority@1",
            "run_authority_hash": "a" * 64,
            "authorization_policy": _ref("auth@1", b"auth"),
            "permission_preset": _ref("preset@1", b"preset"),
            "permission_envelope_hash": "b" * 64,
            "effective_scopes": (),
        },
        required_capabilities=(),
        budget={"evaluation_envelope": _ref("budget@1", b"budget"), "effective_budget": _ref("budget@1", b"budget")},
        policy_refs=(),
        evaluation_evidence_set=_ref("evidence@1", b"evidence"),
        resolver_version="resolver@1",
        resolved_at=datetime.now(UTC),
    )
    with pytest.raises(SkillSpecHashMismatchError):
        verify_skill_execution_spec(spec.model_copy(update={"skill_spec_hash": "0" * 64}))


def test_trust_builder_rejects_non_tool_and_timeoutless_bindings() -> None:
    manifest, binding = _manifest()
    registry = TypedSchemaRegistry()
    registry.register(binding.input_schema_ref, Input, role="input")
    final = FinalAnswer[Output].model_construct(kind="final_answer")
    with pytest.raises(ValueError):
        cast(Any, tool_command_from_decision)(
            final,
            authorization_decision_ref="auth@1",
            tool_request_id=uuid4(),
            timeout_policy_ref="timeout@1",
            manifest=manifest,
            expected_manifest_hash=manifest.manifest_hash,
            tool_binding=binding,
            schema_registry=registry,
        )
    decision = enrich_decision(
        ToolProposalPayload[Input](
            kind="tool_proposal",
            tool_ref=binding.tool_ref,
            input=Input(value="x"),
            rationale_summary="r",
            confidence=0.5,
        ),
        meta=_meta(),
        run_id=uuid4(),
        decision_id=uuid4(),
        manifest=manifest,
        schema_registry=registry,
        expected_manifest_hash=manifest.manifest_hash,
    )
    assert isinstance(decision, ToolProposal)
    outside = binding.model_copy(update={"tool_ref": _ref("tool.outside@1", b"outside")})
    with pytest.raises(ValueError):
        tool_command_from_decision(
            decision,
            authorization_decision_ref="auth@1",
            tool_request_id=uuid4(),
            timeout_policy_ref="timeout@1",
            manifest=manifest,
            expected_manifest_hash=manifest.manifest_hash,
            tool_binding=outside,
            schema_registry=registry,
        )
    timeoutless = binding.model_copy(update={"timeout_policy_ref": None})
    timeoutless_manifest = manifest.model_copy(update={"tool_bindings": (timeoutless,)}).with_hash()
    with pytest.raises(ValueError):
        tool_command_from_decision(
            decision,
            authorization_decision_ref="auth@1",
            tool_request_id=uuid4(),
            timeout_policy_ref="timeout@1",
            manifest=timeoutless_manifest,
            expected_manifest_hash=timeoutless_manifest.manifest_hash,
            tool_binding=timeoutless,
            schema_registry=registry,
        )


def test_enrichment_rejects_unknown_constructed_discriminator() -> None:
    malformed = KnowledgeProposalPayload.model_construct(kind="unknown")
    with pytest.raises(ValueError):
        cast(Any, enrich_knowledge_decision)(malformed, meta=_meta(), run_id=uuid4(), decision_id=uuid4())
