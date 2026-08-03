from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from app.contracts import (
    CanonicalInferenceRequest,
    CanonicalInferenceResult,
    CanonicalMessage,
    ContractMeta,
    FinalAnswerPayload,
    InferenceBudget,
    KnowledgeProposal,
    ModelUsage,
    ResolvedInferenceRetryPolicy,
    ResolvedModelPolicy,
    ToolProposal,
    ToolProposalPayload,
    TypedSchemaRegistry,
    VersionedRef,
    canonical_bytes,
    enrich_decision,
    enrich_knowledge_decision,
    parse_canonical_decision,
    parse_inference_decision,
    tool_command_from_decision,
)
from app.skill_abi import (
    ABIConversionError,
    ClosureViolationError,
    DependencyBinding,
    DependencyNode,
    PermissionDeniedError,
    PermissionInteractionRequiredError,
    PermissionPreset,
    SkillExecutionSpec,
    SkillRuntimeManifest,
    ToolBinding,
    UnknownABIVersionError,
    convert_abi_v1_to_v2,
    issue_dependency_closure_proof,
    read_skill_execution_spec,
    run_guarded,
    validate_closure_ref,
    verify_runtime_manifest,
)
from pydantic import BaseModel, ValidationError
from scripts.check_contract_dependencies import find_violations


class InputModel(BaseModel):
    model_config = {"extra": "forbid", "frozen": True}
    value: str


class OutputModel(BaseModel):
    model_config = {"extra": "forbid", "frozen": True}
    answer: str


def ref(name: str, version: str, marker: str) -> VersionedRef:
    return VersionedRef(ref=name, version=version, content_hash=marker * 64)


def meta(name: str = "canonical.inference") -> ContractMeta:
    return ContractMeta(
        contract_name=name,
        contract_version="v1",
        message_id=UUID("00000000-0000-0000-0000-000000000001"),
        tenant_id="tenant-a",
        correlation_id="corr-a",
        trace_id="trace-a",
    )


def tool_manifest() -> tuple[SkillRuntimeManifest, ToolBinding]:
    binding = ToolBinding(
        tool_ref=ref("tool.read@1", "1", "4"),
        operation="read",
        resource_type="asset",
        effect_class="read",
        input_schema_ref=ref("schema.input@1", "1", "2"),
        output_schema_ref=ref("schema.output@1", "1", "3"),
        limits_policy_ref=ref("limits@1", "1", "5"),
        adapter_compatibility_ref=ref("adapter@1", "1", "6"),
        partial_policy_ref=ref("partial@1", "1", "7"),
        selection_policy_ref=ref("selection@1", "1", "8"),
        timeout_policy_ref=ref("timeout@1", "1", "9"),
        logical_call_budget=2,
    )
    manifest = SkillRuntimeManifest(
        manifest_version="v1",
        skill_ref=ref("skill.root@1", "1", "1"),
        input_schema_ref=binding.input_schema_ref,
        output_schema_ref=binding.output_schema_ref,
        dependencies=(),
        tool_bindings=(binding,),
        required_capabilities=(),
    ).with_hash()
    return manifest, binding


def test_ask_never_reaches_provider_and_unattended_is_deny() -> None:
    called: list[str] = []
    for preset, effect, requires_prompt in (
        (PermissionPreset.INTERACTIVE, "external", None),
        (PermissionPreset.INTERACTIVE, "read", True),
        (PermissionPreset.WORKSPACE_EDIT, "external", None),
    ):
        with pytest.raises(PermissionInteractionRequiredError):
            run_guarded(
                lambda: called.append("provider"),
                preset=preset,
                effect=effect,
                requires_prompt=requires_prompt,
            )
    with pytest.raises(PermissionInteractionRequiredError):
        run_guarded(lambda: called.append("provider"), preset="read_only", effect="read", requires_prompt=True)
    with pytest.raises(PermissionDeniedError):
        run_guarded(lambda: called.append("provider"), preset=PermissionPreset.UNATTENDED, effect="external")
    assert called == []


def test_manifest_typed_json_parse_and_enrich_preserves_nested_payload() -> None:
    manifest, binding = tool_manifest()
    registry = TypedSchemaRegistry()
    registry.register(binding.input_schema_ref, InputModel, role="input")
    registry.register(binding.output_schema_ref, OutputModel, role="output")
    raw = {
        "kind": "tool_proposal",
        "tool_ref": "tool.read@1",
        "input": {"value": "kept"},
        "rationale_summary": "r",
        "confidence": 0.5,
    }
    parsed = parse_inference_decision(
        raw, manifest=manifest, schema_registry=registry, expected_manifest_hash=manifest.manifest_hash
    )
    assert isinstance(parsed, ToolProposalPayload)
    assert isinstance(parsed.input, InputModel)
    assert parsed.input.value == "kept"
    decision = enrich_decision(
        parsed,
        meta=meta(),
        run_id=uuid4(),
        decision_id=uuid4(),
        manifest=manifest,
        schema_registry=registry,
        expected_manifest_hash=manifest.manifest_hash,
    )
    assert isinstance(decision, ToolProposal)
    assert isinstance(decision.input, InputModel)
    assert decision.input.value == "kept"
    reparsed = parse_canonical_decision(
        decision.model_dump(),
        manifest=manifest,
        schema_registry=registry,
        expected_manifest_hash=manifest.manifest_hash,
    )
    assert isinstance(reparsed, ToolProposal)
    assert isinstance(reparsed.input, InputModel)
    assert reparsed.input.value == "kept"
    assert decision.meta.contract_name == "canonical.decision"
    assert decision.meta.message_id != meta().message_id
    with pytest.raises(ValueError):
        parse_inference_decision(
            {**raw, "input": {"value": "x", "tenant_id": "forged"}},
            manifest=manifest,
            schema_registry=registry,
            expected_manifest_hash=manifest.manifest_hash,
        )
    with pytest.raises(ValueError):
        registry.register(binding.input_schema_ref, InputModel, role="input")


def test_command_builder_requires_verified_manifest_and_exact_identity() -> None:
    manifest, binding = tool_manifest()
    registry = TypedSchemaRegistry()
    registry.register(binding.input_schema_ref, InputModel, role="input")
    payload = ToolProposalPayload[InputModel](
        kind="tool_proposal",
        tool_ref=binding.tool_ref,
        input=InputModel(value="x"),
        rationale_summary="r",
        confidence=0.5,
    )
    decision = enrich_decision(
        payload,
        meta=meta(),
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
        timeout_policy_ref="timeout@1",
        manifest=manifest,
        expected_manifest_hash=manifest.manifest_hash,
        tool_binding=binding,
        schema_registry=registry,
    )
    assert isinstance(command.tool_ref, VersionedRef)
    assert command.tool_ref == binding.tool_ref
    assert command.meta.message_id != decision.meta.message_id
    assert command.meta.causation_id == decision.meta.message_id
    with pytest.raises(ValueError):
        tool_command_from_decision(
            decision,
            authorization_decision_ref="auth@1",
            tool_request_id=uuid4(),
            timeout_policy_ref="timeout@1",
            manifest=manifest.model_copy(update={"manifest_hash": "0" * 64}),
            expected_manifest_hash=manifest.manifest_hash,
            tool_binding=binding,
            schema_registry=registry,
        )


def test_required_resolved_fields_and_family_version_are_closed() -> None:
    with pytest.raises(ValidationError):
        CanonicalInferenceRequest[InputModel].model_validate(
            {
                "meta": meta(),
                "inference_request_id": uuid4(),
                "run_id": uuid4(),
                "node_id": "n",
                "node_attempt": 0,
                "input": {"value": "x"},
                "result_schema_ref": "result@1",
                "prompt_policy_ref": "prompt@1",
                "model_policy_ref": "model@1",
                "inference_retry_policy_ref": "retry@1",
                "budget_policy_ref": "budget@1",
            }
        )
    with pytest.raises(ValidationError):
        CanonicalInferenceResult[OutputModel].model_validate(
            {
                "meta": meta(),
                "inference_request_id": uuid4(),
                "result": {"answer": "x"},
                "model_ref": "model@1",
                "provider_attempts": 1,
                "schema_retries": 0,
            }
        )
    from app.contracts import KnowledgeRequest

    with pytest.raises(ValidationError):
        cast(Any, KnowledgeRequest)(
            meta=meta("knowledge.request"),
            decision_id=uuid4(),
            knowledge_request_id=uuid4(),
            run_id=uuid4(),
            authorization_decision_ref="auth@1",
            query="q",
            knowledge_refs=(),
            purpose="answer",
            required_citation_level="source",
        )
    with pytest.raises(ValidationError):
        ContractMeta(
            contract_name="canonical.inference",
            contract_version="v999",
            message_id=uuid4(),
            tenant_id="t",
            correlation_id="c",
        )
    request = CanonicalInferenceRequest[InputModel](
        meta=meta("canonical.inference.request"),
        inference_request_id=uuid4(),
        run_id=uuid4(),
        node_id="n",
        node_attempt=0,
        input=InputModel(value="x"),
        context=None,
        context_refs=(),
        instructions=(CanonicalMessage(role="user", content="q"),),
        model_policy=ResolvedModelPolicy(model_ref="model@1", temperature=0, max_output_tokens=10),
        result_schema_ref="result@1",
        prompt_policy_ref="prompt@1",
        model_policy_ref="model-policy@1",
        retry_policy=ResolvedInferenceRetryPolicy(max_schema_retries=1, max_provider_retries=1),
        inference_retry_policy_ref="retry@1",
        budget=InferenceBudget(max_tokens=10, max_cost_micros=1, deadline_ms=100),
        budget_policy_ref="budget@1",
    )
    result = CanonicalInferenceResult[OutputModel](
        meta=ContractMeta(
            contract_name="canonical.inference.result",
            contract_version="v1",
            message_id=uuid4(),
            tenant_id="t",
            correlation_id="c",
            causation_id=request.meta.message_id,
        ),
        inference_request_id=request.inference_request_id,
        result=OutputModel(answer="ok"),
        model_ref="model@1",
        usage=ModelUsage(input_tokens=1, output_tokens=1, cost_micros=1),
        provider_attempts=1,
        schema_retries=0,
    )
    assert result.meta.causation_id == request.meta.message_id


def test_closure_proof_exact_identity_and_manifest_policy_boundary() -> None:
    root_bytes = b"root-artifact"
    dep_bytes = b"dep-artifact"
    root_ref = VersionedRef(ref="skill.root@1", version="1", content_hash=hashlib.sha256(root_bytes).hexdigest())
    dep_ref = VersionedRef(ref="skill.dep@1", version="1", content_hash=hashlib.sha256(dep_bytes).hexdigest())
    resolver_ref = ref("resolver@1", "1", "3")
    root = DependencyNode(root_ref, (dep_ref,), artifact_payload=root_bytes)
    dep = DependencyNode(dep_ref, artifact_payload=dep_bytes)
    proof = issue_dependency_closure_proof(root, (dep,), resolver_version=resolver_ref)
    manifest = SkillRuntimeManifest(
        manifest_version="v1",
        skill_ref=root.skill,
        input_schema_ref=ref("schema.in@1", "1", "4"),
        output_schema_ref=ref("schema.out@1", "1", "5"),
        dependencies=(DependencyBinding(skill_ref=dep.skill, manifest_ref=ref("manifest.dep@1", "1", "6")),),
        tool_bindings=(),
        skill_closure=proof.closure,
        dependency_closure_proof=proof,
        required_capabilities=(),
    ).with_hash()
    verify_runtime_manifest(manifest, expected_manifest_hash=manifest.manifest_hash)
    with pytest.raises((ClosureViolationError, ValueError)):
        verify_runtime_manifest(
            manifest.model_copy(update={"skill_closure": (root.skill,)}).with_hash(),
            expected_manifest_hash=manifest.manifest_hash,
            artifact_payloads={"skill.root@1": root_bytes, "skill.dep@1": dep_bytes},
        )
    with pytest.raises(ClosureViolationError):
        validate_closure_ref(ref("skill.dep@1", "1", "9"), proof.closure)
    with pytest.raises(ValidationError):
        SkillRuntimeManifest.model_validate(
            {
                **manifest.model_dump(),
                "manifest_hash": "",
                "policy_refs": ({"kind": "model", "policy": ref("model@1", "1", "a")},),
            }
        )
    assert "policy_refs" not in SkillRuntimeManifest.model_fields


def test_v1_v2_converter_is_explicit_and_no_unknown_fallback() -> None:
    payload = {"abi_version": "v1", "contracts": {"converter_bundle": None}}
    with pytest.raises(ABIConversionError):
        convert_abi_v1_to_v2(payload)
    assert payload["abi_version"] == "v1"
    with pytest.raises(UnknownABIVersionError):
        read_skill_execution_spec({"abi_version": "v9"})
    with pytest.raises(ABIConversionError):
        read_skill_execution_spec({"abi_version": "v1"})


def test_optional_datetime_and_null_bytes_are_distinct() -> None:
    checkpoint = {
        "checkpoint_ref": "checkpoint@1",
        "checkpoint_hash": "a" * 64,
        "tenant_id": "tenant-a",
        "run_id": uuid4(),
        "graph_version": "graph@1",
        "graph_state_schema_version": "state@1",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    from app.contracts import InterruptRef

    base = {
        "interrupt_ref": "interrupt@1",
        "interrupt_hash": "b" * 64,
        "tenant_id": "tenant-a",
        "run_id": checkpoint["run_id"],
        "checkpoint": checkpoint,
        "interrupt_schema_ref": "schema@1",
        "nonce_hash": "c" * 64,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    omitted = InterruptRef.model_validate(base)
    explicit = InterruptRef.model_validate({**base, "expires_at": None})
    assert canonical_bytes(omitted) != canonical_bytes(explicit)
    with pytest.raises(ValidationError):
        InterruptRef.model_validate({**base, "expires_at": datetime(2026, 1, 1)})


def test_dependency_checker_rejects_secondary_and_dynamic_imports(tmp_path: Path) -> None:
    contracts = tmp_path / "app" / "contracts"
    skill = tmp_path / "app" / "skill_abi"
    contracts.mkdir(parents=True)
    skill.mkdir(parents=True)
    (contracts / "bad.py").write_text("import os, sqlalchemy\nfrom app.skill_abi import runtime\n", encoding="utf-8")
    (skill / "bad.py").write_text(
        "from importlib import import_module as load\nload('app.contracts.canonical')\n",
        encoding="utf-8",
    )
    violations = find_violations(tmp_path)
    assert any("sqlalchemy" in item for item in violations)
    assert any("app.skill_abi" in item for item in violations)
    assert any("dynamic import" in item for item in violations)


def test_reader_and_guard_failure_matrix_covers_closed_branches() -> None:
    from app.contracts import (
        ArtifactRef,
        CanonicalFailure,
        Citation,
        FinalAnswer,
        InteractionItem,
        KnowledgeFilter,
        KnowledgeItem,
        KnowledgeProposalPayload,
        KnowledgeResult,
        MessageStarted,
        ProjectionSourceRef,
        RetrievalBudget,
        ToolProposal,
        ToolResult,
        ToolResultProvenance,
        UIProjectionEvent,
        UnknownContractError,
        derive_contract_meta,
        read_contract,
    )
    from app.contracts.canonical import (
        _manifest_schema_for_payload,
        _ref_key,
        _resolve_schema_adapter,
        _schema_adapter,
        _unique_sorted,
        _validate_hash,
    )
    from app.skill_abi import (
        ABIConverterRegistry,
        CapabilityUnavailableError,
        DisabledAdapter,
        MissingArtifactError,
        RetryBudget,
        RetryOwner,
        check_permission,
        compute_manifest_hash,
        ensure_scope_subset,
        evaluate_permission,
        intersect_scopes,
        resolve_dependency_closure,
        validate_manifest_proposal,
    )
    from pydantic import TypeAdapter

    with pytest.raises(ValueError):
        _validate_hash("bad")
    with pytest.raises(ValueError):
        _unique_sorted(("x", "x"), "keys")
    with pytest.raises(ValueError):
        derive_contract_meta(meta(), contract_name="bad name")
    with pytest.raises(ValueError):
        FinalAnswer.model_validate(
            {
                "kind": "final_answer",
                "meta": meta("wrong.family"),
                "run_id": uuid4(),
                "decision_id": uuid4(),
                "output": OutputModel(answer="x"),
                "artifact_refs": (),
                "rationale_summary": "r",
                "confidence": 0.1,
            }
        )
    with pytest.raises(ValueError):
        ToolProposal.model_validate(
            {
                "kind": "tool_proposal",
                "meta": meta("wrong.family"),
                "run_id": uuid4(),
                "decision_id": uuid4(),
                "tool_ref": "tool@1",
                "input": InputModel(value="x"),
                "rationale_summary": "r",
                "confidence": 0.1,
            }
        )

    final_payload = {"kind": "final_answer", "output": {"answer": "ok"}, "rationale_summary": "r", "confidence": 0.5}
    tool_payload = {
        "kind": "tool_proposal",
        "tool_ref": "tool@1",
        "input": {"value": "x"},
        "rationale_summary": "r",
        "confidence": 0.5,
    }
    for payload in (final_payload, tool_payload):
        with pytest.raises((TypeError, ValueError)):
            parse_inference_decision(
                payload,
                input_type=InputModel,
                output_type=OutputModel,
                expected_manifest_hash="a" * 64,
            )
    for payload in (
        {
            "kind": "knowledge_proposal",
            "query": "q",
            "knowledge_refs": (),
            "filter": {},
            "rationale_summary": "r",
            "confidence": 0.5,
        },
        {
            "kind": "action_proposal",
            "action_ref": "action@1",
            "input": {"value": "x"},
            "rationale_summary": "r",
            "confidence": 0.5,
        },
        {
            "kind": "delegate_proposal",
            "target_skill_ref": "skill@1",
            "input": {"value": "x"},
            "rationale_summary": "r",
            "confidence": 0.5,
        },
    ):
        with pytest.raises((TypeError, ValueError)):
            parse_inference_decision(
                payload,
                input_type=InputModel,
                output_type=OutputModel,
                expected_manifest_hash="a" * 64,
            )
    with pytest.raises((TypeError, ValueError)):
        parse_inference_decision(
            {"kind": "unknown"},
            input_type=InputModel,
            output_type=OutputModel,
            expected_manifest_hash="a" * 64,
        )
    with pytest.raises(TypeError):
        parse_inference_decision(
            final_payload,
            manifest=object(),
            schema_registry={},
            expected_manifest_hash="a" * 64,
        )

    # Plain mappings must use one exact schema identity; a missing/ambiguous
    # binding cannot silently fall back to a ref-only adapter.
    loose = type("Loose", (BaseModel,), {"__annotations__": {"value": str}})
    with pytest.raises((TypeError, ValueError)):
        _resolve_schema_adapter({"schema@1": loose}, ref("schema@1", "1", "a"), role="input")
    with pytest.raises((TypeError, ValueError)):
        _resolve_schema_adapter({}, ref("schema@1", "1", "a"), role="input")
    exact_registry = {("schema@1", "1", "a" * 64): InputModel}
    with pytest.raises(TypeError):
        _resolve_schema_adapter(exact_registry, ref("schema@1", "1", "a"), role="input")

    decision = enrich_knowledge_decision(
        KnowledgeProposalPayload(
            kind="knowledge_proposal",
            query="q",
            knowledge_refs=(),
            filter=KnowledgeFilter(),
            rationale_summary="r",
            confidence=0.5,
        ),
        meta=meta(),
        run_id=uuid4(),
        decision_id=uuid4(),
    )
    assert isinstance(decision, KnowledgeProposal)
    from app.contracts import knowledge_request_from_decision

    request = knowledge_request_from_decision(
        decision,
        authorization_decision_ref="auth@1",
        knowledge_request_id=uuid4(),
        purpose="answer",
        filter=KnowledgeFilter(tags=("a",)),
        budget=RetrievalBudget(max_results=2, max_bytes=2, max_tokens=2, deadline_ms=2),
    )
    assert request.filter.tags == ("a",)
    with pytest.raises(ValueError):
        knowledge_request_from_decision(
            cast(
                Any,
                ToolProposal[InputModel](
                    kind="tool_proposal",
                    meta=meta("canonical.decision"),
                    run_id=uuid4(),
                    decision_id=uuid4(),
                    tool_ref="tool@1",
                    input=InputModel(value="x"),
                    rationale_summary="r",
                    confidence=0.1,
                ),
            ),
            authorization_decision_ref="auth@1",
            knowledge_request_id=uuid4(),
            purpose="answer",
            filter=KnowledgeFilter(),
            budget=RetrievalBudget(max_results=1, max_bytes=1, max_tokens=1, deadline_ms=1),
        )

    with pytest.raises(TypeError):
        cast(Any, tool_command_from_decision)(
            FinalAnswer[OutputModel](
                kind="final_answer",
                meta=meta("canonical.decision"),
                run_id=uuid4(),
                decision_id=uuid4(),
                output=OutputModel(answer="x"),
                artifact_refs=(),
                rationale_summary="r",
                confidence=0.1,
            ),
            authorization_decision_ref="auth@1",
            tool_request_id=uuid4(),
            timeout_policy_ref="timeout@1",
        )

    with pytest.raises(UnknownContractError):
        read_contract("decision", {}, version="v2", expected_manifest_hash="a" * 64)
    with pytest.raises(UnknownContractError):
        read_contract("decision", {}, expected_manifest_hash="a" * 64)

    adapter = DisabledAdapter("memory.long_term")
    for call in (adapter.invoke, adapter.call):
        with pytest.raises(CapabilityUnavailableError):
            call()
    with pytest.raises(ValueError):
        RetryBudget(max_attempts=-1)
    with pytest.raises(ValueError):
        ABIConverterRegistry(supported_versions=("v1",)).register("v2", lambda _: cast(SkillExecutionSpec, None))
    with pytest.raises(ValueError):
        convert_abi_v1_to_v2({"abi_version": "v2"})

    dep = DependencyNode(ref("skill@1", "1", "a"), artifact_payload=b"ok")
    with pytest.raises(MissingArtifactError):
        resolve_dependency_closure(dep, (), artifact_payloads={})
    manifest, _ = tool_manifest()
    with pytest.raises(ClosureViolationError):
        validate_manifest_proposal(
            manifest,
            kind="unknown",
            reference="tool.read@1",
            expected_manifest_hash=manifest.manifest_hash,
        )
    with pytest.raises(TypeError):
        compute_manifest_hash(object())

    typed_in = InputModel
    typed_out = OutputModel
    for payload in (
        {
            "kind": "action_proposal",
            "action_ref": "action@1",
            "input": {"value": "x"},
            "rationale_summary": "r",
            "confidence": 0.5,
        },
        {
            "kind": "delegate_proposal",
            "target_skill_ref": "skill@1",
            "input": {"value": "x"},
            "rationale_summary": "r",
            "confidence": 0.5,
        },
    ):
        with pytest.raises((TypeError, ValueError)):
            parse_inference_decision(
                payload,
                input_type=typed_in,
                output_type=typed_out,
                expected_manifest_hash="a" * 64,
            )
    final_manifest, final_binding = tool_manifest()
    final_registry = TypedSchemaRegistry()
    final_registry.register(final_binding.output_schema_ref, OutputModel, role="output")
    final = parse_inference_decision(
        {"kind": "final_answer", "output": {"answer": "ok"}, "rationale_summary": "r", "confidence": 0.5},
        manifest=final_manifest,
        schema_registry=final_registry,
        expected_manifest_hash=final_manifest.manifest_hash,
    )
    assert isinstance(final, FinalAnswerPayload)
    assert isinstance(final.output, OutputModel)
    with pytest.raises((TypeError, ValueError)):
        parse_inference_decision(
            {"kind": "knowledge_proposal"},
            input_type=typed_in,
            output_type=typed_out,
            expected_manifest_hash="a" * 64,
        )
    final_decision = enrich_decision(
        final,
        meta=meta(),
        run_id=uuid4(),
        decision_id=uuid4(),
        manifest=final_manifest,
        schema_registry=final_registry,
        expected_manifest_hash=final_manifest.manifest_hash,
    )
    assert (
        parse_canonical_decision(
            final_decision.model_dump(),
            manifest=final_manifest,
            schema_registry=final_registry,
            expected_manifest_hash=final_manifest.manifest_hash,
        ).kind
        == "final_answer"
    )

    art = ArtifactRef(
        artifact_id=uuid4(),
        tenant_id="t",
        version="1",
        content_hash="a" * 64,
        media_type="application/json",
        sensitivity="internal",
        retention_policy_ref="retention@1",
    )
    with pytest.raises(ValidationError):
        FinalAnswer[OutputModel](
            kind="final_answer",
            meta=meta("canonical.decision"),
            run_id=uuid4(),
            decision_id=uuid4(),
            output=OutputModel(answer="x"),
            artifact_refs=(art, art),
            rationale_summary="r",
            confidence=0.1,
        )
    citation = Citation(
        snapshot_ref="snapshot@1",
        snapshot_version="1",
        source_version="source@1",
        locator="doc#1",
        content_hash="b" * 64,
    )
    item = KnowledgeItem(item_ref="item@1", content="x", citations=(citation,))
    knowledge_kwargs: dict[str, Any] = {
        "meta": meta("knowledge.result"),
        "knowledge_request_id": uuid4(),
        "result_class": "ok",
        "items": (item,),
        "citations": (citation,),
        "knowledge_snapshot_ref": "snapshot@1",
        "knowledge_snapshot_version": "1",
        "knowledge_snapshot_content_hash": "c" * 64,
        "applied_acl_ref": "acl@1",
        "applied_acl_hash": "d" * 64,
        "retrieval_policy_ref": "retrieval@1",
        "retrieval_policy_hash": "e" * 64,
        "truncated": False,
    }
    with pytest.raises(ValidationError):
        KnowledgeResult(**{**knowledge_kwargs, "citations": (citation, citation)})
    with pytest.raises(ValidationError):
        KnowledgeResult(**{**knowledge_kwargs, "result_class": "empty"})
    with pytest.raises(ValidationError):
        ToolResult[OutputModel](
            meta=meta("tool.result"), tool_request_id=uuid4(), output=OutputModel(answer="x"), artifact_refs=(art, art)
        )
    failure = CanonicalFailure(
        error_code="tool.failed",
        failure_class="failed",
        retry_owner=RetryOwner.NONE,
        retryable=False,
        safe_message="safe",
    )
    provenance = ToolResultProvenance(
        source_ref="source@1",
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        result_content_hash="f" * 64,
    )
    with pytest.raises(ValidationError):
        ToolResult[OutputModel](
            meta=meta("tool.result"),
            tool_request_id=uuid4(),
            output=OutputModel(answer="x"),
            artifact_refs=(),
            failure=failure,
        )
    assert ToolResult[OutputModel](
        meta=meta("tool.result"),
        tool_request_id=uuid4(),
        output=OutputModel(answer="x"),
        artifact_refs=(),
        provenance=provenance,
    ).output
    with pytest.raises(ValidationError):
        CanonicalFailure(
            error_code="tool.failed",
            failure_class="failed",
            retry_owner=RetryOwner.NONE,
            retryable=False,
            safe_message="bad\x00message",
        )

    source = ProjectionSourceRef(
        source_kind="runtime_event",
        source_ref="event@1",
        source_hash="1" * 64,
        source_schema_ref="event.schema@1",
    )
    started = MessageStarted(
        kind="message_started",
        message_id=uuid4(),
        owner_run_id=uuid4(),
        role="assistant",
        content_schema_ref="content@1",
    )
    with pytest.raises(ValidationError):
        UIProjectionEvent[MessageStarted](
            meta=meta("ui.projection"),
            event_id=uuid4(),
            target_kind="run",
            target_ref=uuid4(),
            projection_seq=1,
            payload_schema_ref="ui@1",
            payload=started,
            source_refs=(source, source),
            projected_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    interaction_kwargs: dict[str, Any] = {
        "interaction_id": uuid4(),
        "tenant_id": "t",
        "presentation_run_id": uuid4(),
        "owner_run_id": uuid4(),
        "orchestration_id": uuid4(),
        "kind": "user_input",
        "source": source,
        "payload_schema_ref": "payload@1",
        "safe_payload": started,
        "status": "pending",
        "revision": 0,
        "source_watermarks": (source,),
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    assert InteractionItem[MessageStarted](**interaction_kwargs).expires_at is None
    with pytest.raises(ValidationError):
        InteractionItem[MessageStarted](**{**interaction_kwargs, "source_watermarks": (source, source)})

    assert intersect_scopes() == ()
    with pytest.raises(PermissionDeniedError):
        ensure_scope_subset(("missing",), ())
    assert (
        evaluate_permission(PermissionPreset.READ_ONLY, "external", authorized=True, requires_prompt=True).value
        == "DENY"
    )
    with pytest.raises(PermissionDeniedError):
        check_permission(PermissionPreset.READ_ONLY, "external", authorized=True)

    assert _ref_key(ref("r@1", "1", "a"))[0] == "r@1"
    with pytest.raises(TypeError):
        _schema_adapter(object())
    with pytest.raises(ValueError):
        _schema_adapter(type("Loose", (BaseModel,), {"__annotations__": {"value": str}}))
    with pytest.raises(ValueError):
        TypedSchemaRegistry().resolve(ref("missing@1", "1", "a"), role="input")
    with pytest.raises((TypeError, ValueError)):
        _resolve_schema_adapter(None, ref("schema@1", "1", "a"), role="input")
    with pytest.raises((TypeError, ValueError)):
        _resolve_schema_adapter(
            {("schema@1", "1", "a" * 64): InputModel, "schema@1": InputModel},
            ref("schema@1", "1", "a"),
            role="input",
        )
    with pytest.raises((TypeError, ValueError)):
        parse_inference_decision(
            final_payload,
            input_type=TypeAdapter(InputModel),
            output_type=None,
            expected_manifest_hash="a" * 64,
        )
    with pytest.raises((TypeError, ValueError)):
        parse_inference_decision(
            tool_payload,
            input_type=None,
            output_type=TypeAdapter(OutputModel),
            expected_manifest_hash="a" * 64,
        )
    with pytest.raises((TypeError, ValueError)):
        parse_inference_decision(
            {
                "kind": "action_proposal",
                "action_ref": "action@1",
                "input": {},
                "rationale_summary": "r",
                "confidence": 0.5,
            },
            input_type=None,
            output_type=TypeAdapter(OutputModel),
            expected_manifest_hash="a" * 64,
        )
    with pytest.raises((TypeError, ValueError)):
        parse_inference_decision(
            {
                "kind": "delegate_proposal",
                "target_skill_ref": "skill@1",
                "input": {},
                "rationale_summary": "r",
                "confidence": 0.5,
            },
            input_type=None,
            output_type=TypeAdapter(OutputModel),
            expected_manifest_hash="a" * 64,
        )
    manifest, _ = tool_manifest()
    for manifest_payload in (
        {"kind": "knowledge_proposal"},
        {"kind": "action_proposal"},
        {"kind": "delegate_proposal"},
    ):
        assert _manifest_schema_for_payload(
            manifest_payload, manifest, expected_manifest_hash=manifest.manifest_hash
        ) == (None, None)
    with pytest.raises(ValueError):
        _manifest_schema_for_payload({"kind": "unknown"}, manifest, expected_manifest_hash=manifest.manifest_hash)
    with pytest.raises((TypeError, ValueError)):
        parse_canonical_decision(
            enrich_decision(
                FinalAnswerPayload[OutputModel](
                    kind="final_answer", output=OutputModel(answer="x"), rationale_summary="r", confidence=0.5
                ),
                meta=meta(),
                run_id=uuid4(),
                decision_id=uuid4(),
                expected_manifest_hash="a" * 64,
            ).model_dump(),
            manifest=manifest,
            schema_registry={},
            expected_manifest_hash=manifest.manifest_hash,
        )
    with pytest.raises((TypeError, ValueError)):
        enrich_decision(
            ToolProposalPayload[InputModel](
                kind="tool_proposal",
                tool_ref="tool.read@1",
                input=InputModel(value="x"),
                rationale_summary="r",
                confidence=0.5,
            ),
            meta=meta(),
            run_id=uuid4(),
            decision_id=uuid4(),
            expected_manifest_hash="a" * 64,
        )
    with pytest.raises((TypeError, ValueError)):
        parse_canonical_decision({"kind": "unknown"}, expected_manifest_hash="a" * 64)
    with pytest.raises((TypeError, ValueError)):
        read_contract(
            "decision",
            {**tool_payload, "contract_version": "v1"},
            version="v1",
            input_type=InputModel,
            expected_manifest_hash="a" * 64,
        )
    with pytest.raises(UnknownContractError):
        read_contract("inference.payload", final_payload, version="v2", expected_manifest_hash="a" * 64)
    with pytest.raises(UnknownContractError):
        read_contract("decision", {"contract_version": "v2"}, expected_manifest_hash="a" * 64)
