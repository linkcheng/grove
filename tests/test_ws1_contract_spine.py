from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from app.contracts import (
    ActionProposalPayload,
    ArtifactRef,
    CanonicalFailure,
    CanonicalInferenceRequest,
    CanonicalInferenceResult,
    CanonicalMessage,
    CheckpointRef,
    Citation,
    ContractMeta,
    ContractReaderRegistry,
    DomainViewAccepted,
    EvaluationEvidenceRef,
    FinalAnswer,
    FinalAnswerPayload,
    InferenceBudget,
    InferenceContext,
    InteractionResolved,
    InterruptRef,
    KnowledgeFilter,
    KnowledgeItem,
    KnowledgeProposal,
    KnowledgeProposalPayload,
    KnowledgeRequest,
    KnowledgeResult,
    MessageCompleted,
    MessageDelta,
    MessageStarted,
    ModelUsage,
    ProjectionSourceRef,
    ResolvedInferenceRetryPolicy,
    ResolvedModelPolicy,
    RetrievalBudget,
    ToolCommand,
    ToolProposal,
    ToolProposalPayload,
    ToolResult,
    ToolResultProvenance,
    TraceRef,
    TypedSchemaRegistry,
    UIProjectionEvent,
    UnknownContractError,
    canonical_bytes,
    canonical_hash,
    enrich_decision,
    enrich_knowledge_decision,
    knowledge_request_from_decision,
    parse_canonical_decision,
    parse_inference_decision,
    parse_ui_projection_payload,
    read_contract,
    tool_command_from_decision,
)
from app.skill_abi import (
    ABIConversionError,
    ArtifactHashMismatchError,
    CapabilityUnavailableError,
    ClosureViolationError,
    DependencyCycleError,
    DependencyNode,
    DisabledAdapter,
    MissingCapabilityError,
    PermissionDeniedError,
    PermissionPreset,
    RetryBudget,
    RetryOwner,
    SkillExecutionSpec,
    SkillRuntimeManifest,
    ToolBinding,
    UnknownABIVersionError,
    VersionedRef,
    build_skill_execution_spec,
    check_permission,
    compute_evaluation_subject_hash,
    compute_skill_spec_hash,
    convert_abi_v1_to_v2,
    ensure_scope_subset,
    evaluate_permission,
    intersect_scopes,
    require_capabilities,
    resolve_dependency_closure,
    retry_allowed,
    run_guarded,
    validate_artifact,
    validate_closure_ref,
    validate_manifest_proposal,
    verify_runtime_manifest,
    verify_skill_execution_spec,
)
from pydantic import BaseModel, ValidationError


class Answer(BaseModel):
    model_config = {"extra": "forbid", "frozen": True}
    answer: str


def _meta(name: str = "canonical.inference") -> ContractMeta:
    return ContractMeta(
        contract_name=name,
        contract_version="v1",
        message_id=UUID("00000000-0000-0000-0000-000000000001"),
        tenant_id="tenant-a",
        correlation_id="corr-1",
    )


def _artifact() -> ArtifactRef:
    return ArtifactRef(
        artifact_id=UUID("00000000-0000-0000-0000-000000000002"),
        tenant_id="tenant-a",
        version="v1",
        content_hash="a" * 64,
        media_type="application/json",
        sensitivity="internal",
        retention_policy_ref="retention@1",
    )


def _manifest() -> SkillRuntimeManifest:
    return SkillRuntimeManifest(
        manifest_version="v1",
        skill_ref=VersionedRef(ref="skill.root@1", version="1", content_hash="1" * 64),
        input_schema_ref=VersionedRef(ref="schema.input@1", version="1", content_hash="2" * 64),
        output_schema_ref=VersionedRef(ref="schema.output@1", version="1", content_hash="3" * 64),
        dependencies=(),
        tool_bindings=(),
        required_capabilities=("graph", "knowledge"),
    )


def _manifest_with_tool() -> tuple[SkillRuntimeManifest, ToolBinding]:
    binding = ToolBinding(
        tool_ref=VersionedRef(ref="tool.read@1", version="1", content_hash="4" * 64),
        operation="read",
        resource_type="asset",
        effect_class="read",
        input_schema_ref=VersionedRef(ref="schema.input@1", version="1", content_hash="2" * 64),
        output_schema_ref=VersionedRef(ref="schema.output@1", version="1", content_hash="3" * 64),
        limits_policy_ref=VersionedRef(ref="limits@1", version="1", content_hash="5" * 64),
        adapter_compatibility_ref=VersionedRef(ref="adapter@1", version="1", content_hash="6" * 64),
        partial_policy_ref=VersionedRef(ref="partial@1", version="1", content_hash="7" * 64),
        selection_policy_ref=VersionedRef(ref="selection@1", version="1", content_hash="8" * 64),
        timeout_policy_ref=VersionedRef(ref="timeout@1", version="1", content_hash="9" * 64),
        logical_call_budget=1,
    )
    manifest = SkillRuntimeManifest(
        manifest_version="v1",
        skill_ref=VersionedRef(ref="skill.root@1", version="1", content_hash="1" * 64),
        input_schema_ref=binding.input_schema_ref,
        output_schema_ref=binding.output_schema_ref,
        dependencies=(),
        tool_bindings=(binding,),
        required_capabilities=(),
    ).with_hash()
    return manifest, binding


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


def test_payload_decision_request_trust_split_and_closed_union() -> None:
    payload = FinalAnswerPayload[Answer](
        kind="final_answer",
        output=Answer(answer="ok"),
        rationale_summary="short",
        confidence=0.9,
    )
    assert "meta" not in payload.model_dump()
    manifest = _manifest().with_hash()
    registry = TypedSchemaRegistry()
    registry.register(manifest.output_schema_ref, Answer, role="output")
    parsed_payload = parse_inference_decision(
        payload.model_dump(),
        manifest=manifest,
        schema_registry=registry,
        expected_manifest_hash=manifest.manifest_hash,
    )
    assert isinstance(parsed_payload, FinalAnswerPayload)
    assert isinstance(parsed_payload.output, Answer)
    with pytest.raises(ValidationError):
        FinalAnswerPayload[Answer](  # type: ignore[call-arg]
            kind="final_answer",
            output=Answer(answer="ok"),
            rationale_summary="short",
            confidence=0.9,
            tenant_id="forged",
        )
    with pytest.raises(ValidationError):
        KnowledgeProposalPayload(  # type: ignore[call-arg]
            kind="knowledge_proposal",
            query="q",
            knowledge_refs=(),
            filter=KnowledgeFilter(),
            rationale_summary="x",
            confidence=0.1,
            extra_field="nope",
        )


def test_canonical_profile_golden_and_order_independent() -> None:
    value = {
        "z": "last",
        "id": UUID("ABCDEFAB-CDEF-ABCD-EFAB-CDEFABCDEFAB"),
        "at": datetime(2026, 1, 1, 8, tzinfo=UTC),
        "first": "中文",
    }
    expected = (
        '{"at":"2026-01-01T08:00:00Z","first":"中文","id":"abcdefab-cdef-abcd-efab-cdefabcdefab","z":"last"}\n'
    ).encode()
    assert canonical_bytes(value) == expected
    assert canonical_hash(value) == "22cc65b0dd880371f554dd45733f9501b249c9f03bbea12fb16d6c47d3edb0a5"
    assert canonical_bytes({"first": "中文", "z": "last", "id": value["id"], "at": value["at"]}) == expected
    with pytest.raises(ValueError, match="timezone"):
        canonical_bytes({"at": datetime(2026, 1, 1)})


def test_canonical_optional_absent_and_explicit_null_are_distinct() -> None:
    absent = _artifact()
    explicit = ArtifactRef.model_validate({**absent.model_dump(), "schema_ref": None})
    assert canonical_bytes(absent) != canonical_bytes(explicit)


def test_spec_hashes_exclude_only_documented_fields_and_are_stable() -> None:
    spec = _spec()
    first_subject = compute_evaluation_subject_hash(spec)
    first_hash = compute_skill_spec_hash(spec)
    changed = spec.model_copy(
        update={
            "spec_id": uuid4(),
            "resolved_at": datetime(2030, 1, 1, tzinfo=UTC),
            "permission": spec.permission.model_copy(update={"run_authority_ref": "different@1"}),
        }
    )
    assert compute_evaluation_subject_hash(changed) == first_subject
    assert compute_skill_spec_hash(changed) == first_hash
    assert compute_skill_spec_hash(spec.model_copy(update={"run_mode": "fork_dry_run"})) != first_hash


def test_manifest_hash_excludes_self_and_rejects_latest() -> None:
    manifest = _manifest()
    first = manifest.with_hash()
    second = first.model_copy(update={"manifest_hash": "0" * 64}).with_hash()
    assert first.manifest_hash == second.manifest_hash
    with pytest.raises(ValidationError):
        VersionedRef(ref="skill.latest", version="latest", content_hash="1" * 64)


def test_explicit_readers_fail_unknown_and_convert_v1_v2() -> None:
    payload = _spec().model_dump(mode="json")
    payload["abi_version"] = "v1"
    parsed = read_contract("skill_execution_spec", payload, expected_manifest_hash="a" * 64)
    assert parsed.abi_version == "v1"
    payload["abi_version"] = "future"
    with pytest.raises(UnknownABIVersionError):
        read_contract("skill_execution_spec", payload, expected_manifest_hash="a" * 64)
    with pytest.raises((ABIConversionError, ValueError)):
        read_contract("unknown.contract", payload, expected_manifest_hash="a" * 64)


def test_v1_to_v2_conversion_requires_complete_spec_and_preserves_evidence() -> None:
    source = _spec()
    converted = convert_abi_v1_to_v2(source.model_dump(mode="json"))
    assert converted["abi_version"] == "v2"
    assert converted["evaluation_subject_hash"] == source.evaluation_subject_hash
    assert converted["evaluation_evidence_set"] == source.evaluation_evidence_set.model_dump(mode="json")
    parsed = SkillExecutionSpec.model_validate(converted)
    verify_skill_execution_spec(parsed)
    with pytest.raises(ABIConversionError):
        convert_abi_v1_to_v2({"abi_version": "v1"})


def test_dependency_closure_detects_cycle_conflict_hash_and_external_ref() -> None:
    a = DependencyNode(
        skill=VersionedRef(ref="skill.a@1", version="1", content_hash="a" * 64),
        dependencies=(VersionedRef(ref="skill.b@1", version="1", content_hash="b" * 64),),
    )
    b = DependencyNode(
        skill=VersionedRef(ref="skill.b@1", version="1", content_hash="b" * 64),
        dependencies=(VersionedRef(ref="skill.a@1", version="1", content_hash="a" * 64),),
    )
    with pytest.raises(DependencyCycleError):
        resolve_dependency_closure(a, (a, b))
    missing = DependencyNode(
        skill=a.skill,
        dependencies=(VersionedRef(ref="skill.missing@1", version="1", content_hash="c" * 64),),
    )
    with pytest.raises(ClosureViolationError):
        resolve_dependency_closure(missing, (missing,))


def test_capability_permission_and_disabled_adapter_fail_before_provider() -> None:
    with pytest.raises(MissingCapabilityError):
        require_capabilities(("graph", "knowledge"), ("graph",))
    assert evaluate_permission(PermissionPreset.INTERACTIVE, "read", authorized=True) == "AUTO"
    assert evaluate_permission(PermissionPreset.READ_ONLY, "external", authorized=True) == "DENY"
    assert evaluate_permission(PermissionPreset.UNATTENDED, "workspace_local", authorized=True) == "DENY"
    with pytest.raises(PermissionDeniedError):
        check_permission(PermissionPreset.READ_ONLY, "external", authorized=True)
    called: list[str] = []
    with pytest.raises(CapabilityUnavailableError):
        run_guarded(DisabledAdapter("memory.long_term"), lambda: called.append("provider"))
    assert called == []


def test_provider_guard_blocks_all_failures_before_callback() -> None:
    called: list[str] = []
    with pytest.raises(ArtifactHashMismatchError):
        run_guarded(
            lambda: called.append("provider"),
            artifact=(b"bytes", "0" * 64),
        )
    assert called == []
    with pytest.raises(ClosureViolationError):
        run_guarded(
            lambda: called.append("provider"),
            allowed_refs=("tool.allowed@1",),
            proposal_ref="tool.external@1",
        )
    assert called == []


def test_retry_owner_and_budget_are_closed() -> None:
    failure = CanonicalFailure(
        error_code="inference.schema_invalid",
        failure_class="schema",
        retry_owner=RetryOwner.TYPED_INFERENCE,
        retryable=True,
        safe_message="invalid output",
    )
    assert retry_allowed(failure, owner=RetryOwner.TYPED_INFERENCE, budget=RetryBudget(max_attempts=1))
    assert not retry_allowed(failure, owner=RetryOwner.TYPED_INFERENCE, budget=RetryBudget(max_attempts=0))
    assert not retry_allowed(failure, owner=RetryOwner.EXECUTION_KERNEL, budget=RetryBudget(max_attempts=5))


def test_artifact_validation() -> None:
    payload = b"immutable"
    validate_artifact(payload, hashlib.sha256(payload).hexdigest())
    with pytest.raises(ArtifactHashMismatchError):
        validate_artifact(payload, "0" * 64)


def test_reference_and_projection_contract_boundaries() -> None:
    checkpoint = CheckpointRef(
        checkpoint_ref="checkpoint@1",
        checkpoint_hash="1" * 64,
        tenant_id="tenant-a",
        run_id=UUID("00000000-0000-0000-0000-000000000003"),
        graph_version="graph@1",
        graph_state_schema_version="state@1",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    interrupt = InterruptRef(
        interrupt_ref="interrupt@1",
        interrupt_hash="2" * 64,
        tenant_id="tenant-a",
        run_id=checkpoint.run_id,
        checkpoint=checkpoint,
        interrupt_schema_ref="interrupt.schema@1",
        nonce_hash="3" * 64,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        expires_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    assert interrupt.checkpoint.run_id == checkpoint.run_id
    with pytest.raises(ValidationError):
        VersionedRef(ref="skill@latest", version="1", content_hash="a" * 64)
    with pytest.raises(ValidationError):
        VersionedRef(ref="skill@1", version="latest", content_hash="a" * 64)
    with pytest.raises(ValidationError):
        CheckpointRef(
            checkpoint_ref="checkpoint@1",
            checkpoint_hash="1" * 64,
            tenant_id="tenant-a",
            run_id=checkpoint.run_id,
            graph_version="graph@1",
            graph_state_schema_version="state@1",
            created_at=datetime(2026, 1, 1),
        )
    with pytest.raises(ValidationError):
        InterruptRef(
            interrupt_ref="interrupt@1",
            interrupt_hash="2" * 64,
            tenant_id="other",
            run_id=checkpoint.run_id,
            checkpoint=checkpoint,
            interrupt_schema_ref="interrupt.schema@1",
            nonce_hash="3" * 64,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    with pytest.raises(ValidationError):
        InterruptRef(
            interrupt_ref="interrupt@1",
            interrupt_hash="2" * 64,
            tenant_id="tenant-a",
            run_id=checkpoint.run_id,
            checkpoint=checkpoint,
            interrupt_schema_ref="interrupt.schema@1",
            nonce_hash="3" * 64,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            expires_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
    evidence = EvaluationEvidenceRef(
        evaluation_run_id=uuid4(),
        tenant_id="tenant-a",
        evaluation_subject_hash="4" * 64,
        suite_ref="suite@1",
        decision="passed",
        evidence_bundle_hash="5" * 64,
        issuer="evaluator@1",
        attestation_ref=_artifact(),
    )
    assert evidence.decision == "passed"
    assert TraceRef(trace_id="trace-1", tenant_id="tenant-a", redaction_policy_ref="redaction@1")

    source = ProjectionSourceRef(
        source_kind="runtime_event",
        source_ref="event@1",
        source_hash="6" * 64,
        source_revision=1,
        source_seq=1,
        source_schema_ref="event.schema@1",
    )
    started = MessageStarted(
        kind="message_started",
        message_id=uuid4(),
        owner_run_id=checkpoint.run_id,
        role="assistant",
        content_schema_ref="content@1",
    )
    assert parse_ui_projection_payload(started.model_dump()).kind == "message_started"
    event = UIProjectionEvent[MessageStarted](
        meta=_meta("ui.projection"),
        event_id=uuid4(),
        target_kind="run",
        target_ref=checkpoint.run_id,
        projection_seq=1,
        payload_schema_ref="ui.message_started@1",
        payload=started,
        source_refs=(source,),
        projected_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert event.payload.kind == "message_started"
    with pytest.raises(ValidationError):
        MessageStarted(
            kind="message_started",
            message_id=uuid4(),
            owner_run_id=checkpoint.run_id,
            role="assistant",
            content_schema_ref="content@1",
            unsafe="nope",  # type: ignore[call-arg]
        )
    resolved = InteractionResolved(
        kind="interaction_resolved",
        interaction_id=uuid4(),
        item_revision=1,
        status="resolved",
        source=source,
    )
    assert resolved.status == "resolved"


def test_knowledge_tool_result_and_failure_contracts() -> None:
    citation = Citation(
        snapshot_ref="snapshot@1",
        snapshot_version="1",
        source_version="source@1",
        locator="doc#1",
        content_hash="7" * 64,
    )
    item = KnowledgeItem(item_ref="item@1", content="fact", citations=(citation,))
    ok = KnowledgeResult(
        meta=_meta("knowledge.result"),
        knowledge_request_id=uuid4(),
        result_class="ok",
        items=(item,),
        citations=(citation,),
        knowledge_snapshot_ref="snapshot@1",
        knowledge_snapshot_version="1",
        knowledge_snapshot_content_hash="8" * 64,
        applied_acl_ref="acl@1",
        applied_acl_hash="9" * 64,
        retrieval_policy_ref="retrieval@1",
        retrieval_policy_hash="a" * 64,
        truncated=False,
    )
    assert ok.items[0].citations[0].locator == "doc#1"
    empty = ok.model_copy(update={"result_class": "empty", "items": (), "citations": ()})
    assert empty.result_class == "empty"
    with pytest.raises(ValidationError):
        KnowledgeResult.model_validate(ok.model_dump(exclude_unset=False) | {"result_class": "empty"})
    failure = CanonicalFailure(
        error_code="knowledge.timeout",
        failure_class="timeout",
        retry_owner=RetryOwner.RUN_COORDINATION,
        retryable=False,
        safe_message="timed out",
    )
    with pytest.raises(ValidationError):
        ToolResult[Answer](meta=_meta("tool.result"), tool_request_id=uuid4(), artifact_refs=())
    failed = ToolResult[Answer](meta=_meta("tool.result"), tool_request_id=uuid4(), artifact_refs=(), failure=failure)
    assert failed.failure == failure
    provenance = ToolResultProvenance(
        source_ref="source@1",
        observed_at=datetime(2026, 1, 1, 8, tzinfo=UTC),
        source_revision_or_watermark="w1",
        result_content_hash="b" * 64,
    )
    success = ToolResult[Answer](
        meta=_meta("tool.result"),
        tool_request_id=uuid4(),
        output=Answer(answer="ok"),
        artifact_refs=(),
        provenance=provenance,
    )
    assert success.provenance is not None
    with pytest.raises(ValidationError):
        ToolResult[Answer](
            meta=_meta("tool.result"),
            tool_request_id=uuid4(),
            output=Answer(answer="bad"),
            artifact_refs=(),
            failure=failure,
        )
    request = KnowledgeRequest(
        meta=_meta("knowledge.request"),
        decision_id=uuid4(),
        knowledge_request_id=uuid4(),
        run_id=uuid4(),
        authorization_decision_ref="decision@1",
        query="q",
        knowledge_refs=("z@1", "a@1"),
        filter=KnowledgeFilter(),
        purpose="answer",
        budget=RetrievalBudget(max_results=1, max_bytes=1, max_tokens=1, deadline_ms=1),
        required_citation_level="source",
    )
    assert request.knowledge_refs == ("a@1", "z@1")


def test_trust_split_enrichment_and_policy_command() -> None:
    run_id = uuid4()
    decision_id = uuid4()
    final_payload = FinalAnswerPayload[Answer](
        kind="final_answer", output=Answer(answer="ok"), rationale_summary="r", confidence=1
    )
    manifest, binding = _manifest_with_tool()
    registry = TypedSchemaRegistry()
    registry.register(binding.input_schema_ref, Answer, role="input")
    registry.register(binding.output_schema_ref, Answer, role="output")
    final_decision = enrich_decision(
        final_payload,
        meta=_meta("canonical.inference.request"),
        run_id=run_id,
        decision_id=decision_id,
        manifest=manifest,
        schema_registry=registry,
        expected_manifest_hash=manifest.manifest_hash,
    )
    assert isinstance(final_decision, FinalAnswer)
    knowledge_payload = KnowledgeProposalPayload(
        kind="knowledge_proposal",
        query="q",
        knowledge_refs=("z@1", "a@1"),
        filter=KnowledgeFilter(),
        rationale_summary="r",
        confidence=0.5,
    )
    knowledge_decision = enrich_knowledge_decision(
        knowledge_payload, meta=_meta(), run_id=run_id, decision_id=decision_id
    )
    assert isinstance(knowledge_decision, KnowledgeProposal)
    knowledge_request = knowledge_request_from_decision(
        knowledge_decision,
        authorization_decision_ref="authorization@1",
        knowledge_request_id=uuid4(),
        purpose="answer",
        filter=KnowledgeFilter(),
        budget=RetrievalBudget(max_results=1, max_bytes=1, max_tokens=1, deadline_ms=1),
    )
    assert knowledge_request.run_id == run_id
    tool_payload = ToolProposalPayload[Answer](
        kind="tool_proposal",
        tool_ref="tool.read@1",
        input=Answer(answer="input"),
        rationale_summary="r",
        confidence=0.5,
    )
    tool_decision = enrich_decision(
        tool_payload,
        meta=_meta("canonical.inference.result"),
        run_id=run_id,
        decision_id=decision_id,
        manifest=manifest,
        schema_registry=registry,
        expected_manifest_hash=manifest.manifest_hash,
    )
    assert isinstance(tool_decision, ToolProposal)
    tool_decision = tool_decision.model_copy(update={"tool_ref": binding.tool_ref})
    command = tool_command_from_decision(
        tool_decision,
        authorization_decision_ref="authorization@1",
        tool_request_id=uuid4(),
        timeout_policy_ref="timeout@1",
        manifest=manifest,
        expected_manifest_hash=manifest.manifest_hash,
        tool_binding=binding,
        schema_registry=registry,
    )
    assert isinstance(command, ToolCommand)
    with pytest.raises((TypeError, ValueError)):
        enrich_decision(
            ActionProposalPayload[Answer](
                kind="action_proposal",
                action_ref="action@1",
                input=Answer(answer="x"),
                rationale_summary="r",
                confidence=0.5,
            ),
            meta=_meta(),
            run_id=run_id,
            decision_id=decision_id,
            expected_manifest_hash="a" * 64,
        )
    assert (
        parse_inference_decision(
            final_payload.model_dump(),
            manifest=manifest,
            schema_registry=registry,
            expected_manifest_hash=manifest.manifest_hash,
        ).kind
        == "final_answer"
    )
    assert (
        parse_canonical_decision(
            final_decision.model_dump(),
            manifest=manifest,
            schema_registry=registry,
            expected_manifest_hash=manifest.manifest_hash,
        ).kind
        == "final_answer"
    )


def test_skill_manifest_closure_and_hash_guards() -> None:
    manifest = _manifest().with_hash()
    verify_runtime_manifest(manifest, expected_manifest_hash=manifest.manifest_hash)
    assert manifest.manifest_hash
    validate_manifest_proposal(
        manifest,
        kind="skill",
        reference="skill.root@1",
        expected_manifest_hash=manifest.manifest_hash,
    )
    with pytest.raises(ClosureViolationError):
        validate_manifest_proposal(
            manifest,
            kind="tool",
            reference="tool.outside@1",
            expected_manifest_hash=manifest.manifest_hash,
        )
    with pytest.raises(ClosureViolationError):
        validate_manifest_proposal(
            manifest,
            kind="action",
            reference="action.outside@1",
            expected_manifest_hash=manifest.manifest_hash,
        )
    with pytest.raises(ClosureViolationError):
        validate_manifest_proposal(
            manifest,
            kind="future",
            reference="skill.root@1",
            expected_manifest_hash=manifest.manifest_hash,
        )
    with pytest.raises(ArtifactHashMismatchError):
        verify_runtime_manifest(
            manifest.model_copy(update={"manifest_hash": "0" * 64}),
            expected_manifest_hash=manifest.manifest_hash,
        )
    with pytest.raises(ArtifactHashMismatchError):
        verify_runtime_manifest(_manifest(), expected_manifest_hash=manifest.manifest_hash)
    verify_skill_execution_spec(_spec())
    with pytest.raises(ArtifactHashMismatchError):
        verify_skill_execution_spec(_spec().model_copy(update={"skill_spec_hash": "0" * 64}))

    assert compute_evaluation_subject_hash(_spec()) != compute_evaluation_subject_hash(
        _spec().model_copy(
            update={
                "permission": _spec().permission.model_copy(
                    update={
                        "permission_preset": VersionedRef(
                            ref="permission.unattended@1", version="1", content_hash="0" * 64
                        )
                    }
                )
            }
        )
    )


def test_dependency_variants_and_explicit_reader_failures() -> None:
    root_ref = VersionedRef(ref="skill.root@1", version="1", content_hash="1" * 64)
    dep_ref = VersionedRef(ref="skill.dep@1", version="1", content_hash="2" * 64)
    root = DependencyNode(root_ref, (dep_ref,))
    dependency = DependencyNode(dep_ref)
    assert resolve_dependency_closure(root, (dependency,)) == (dep_ref, root_ref)
    with pytest.raises(ValueError):
        DependencyNode(root_ref, (dep_ref, dep_ref))
    with pytest.raises(DependencyCycleError):
        resolve_dependency_closure(root, (DependencyNode(dep_ref, (root_ref,)),))
    dep_v2 = VersionedRef(ref="skill.dep@2", version="2", content_hash="3" * 64)
    with pytest.raises(ClosureViolationError):
        resolve_dependency_closure(root, (DependencyNode(dep_v2), dependency))
    with pytest.raises(ArtifactHashMismatchError):
        resolve_dependency_closure(root, (dependency,), artifact_payloads={"skill.dep@1": b"bad"})
    with pytest.raises(ValueError):
        resolve_dependency_closure(root, (dependency,), artifact_payloads={})
    validate_closure_ref(dep_ref, (root_ref, dep_ref))
    with pytest.raises(ClosureViolationError):
        validate_closure_ref("skill.outside@1", (root_ref, dep_ref))
    source = _spec()
    payload = source.model_dump(mode="python", exclude_unset=True)
    payload["abi_version"] = "v2"
    candidate = SkillExecutionSpec.model_validate(payload)
    payload["skill_spec_hash"] = compute_skill_spec_hash(candidate)
    with pytest.raises(ABIConversionError):
        read_contract(
            "skill_execution_spec",
            {**payload, "abi_version": "v1", "skill": {}},
            expected_manifest_hash="a" * 64,
        )
    with pytest.raises(UnknownABIVersionError):
        read_contract("skill_execution_spec", {**payload, "abi_version": "v99"}, expected_manifest_hash="a" * 64)
    assert read_contract("skill_execution_spec", payload, expected_manifest_hash="a" * 64).abi_version == "v2"


def test_capability_postures_scopes_and_guard_order() -> None:
    assert intersect_scopes(("a", "b"), ("b", "c"), ("b",)) == ("b",)
    ensure_scope_subset(("b",), ("a", "b"))
    with pytest.raises(PermissionDeniedError):
        ensure_scope_subset(("c",), ("a", "b"))
    assert evaluate_permission(PermissionPreset.INTERACTIVE, "read", authorized=True) == "AUTO"
    assert evaluate_permission(PermissionPreset.INTERACTIVE, "external", authorized=True) == "ASK"
    assert evaluate_permission(PermissionPreset.WORKSPACE_EDIT, "workspace_local", authorized=True) == "AUTO"
    assert evaluate_permission(PermissionPreset.WORKSPACE_EDIT, "external", authorized=True) == "ASK"
    assert evaluate_permission(PermissionPreset.READ_ONLY, "pure", authorized=True) == "AUTO"
    assert evaluate_permission(PermissionPreset.READ_ONLY, "workspace_local", authorized=True) == "DENY"
    assert evaluate_permission(PermissionPreset.UNATTENDED, "read", authorized=True) == "AUTO"
    assert evaluate_permission(PermissionPreset.UNATTENDED, "external", authorized=True) == "DENY"
    assert evaluate_permission(PermissionPreset.INTERACTIVE, "read", authorized=False) == "DENY"
    with pytest.raises(PermissionDeniedError):
        evaluate_permission("bypass", "read", authorized=True)
    with pytest.raises(PermissionDeniedError):
        evaluate_permission(PermissionPreset.INTERACTIVE, "unknown", authorized=True)
    with pytest.raises(PermissionDeniedError):
        check_permission(PermissionPreset.READ_ONLY, "external", authorized=True)
    require_capabilities(("graph",), ("graph", "knowledge"))
    called: list[str] = []
    assert (
        run_guarded(lambda: called.append("ok"), required_capabilities=("graph",), available_capabilities=("graph",))
        is None
    )
    assert called == ["ok"]
    with pytest.raises(MissingCapabilityError):
        run_guarded(lambda: called.append("bad"), required_capabilities=("graph",), available_capabilities=())
    with pytest.raises(PermissionDeniedError):
        run_guarded(
            lambda: called.append("bad"),
            preset=PermissionPreset.READ_ONLY,
            effect="external",
            authorized=True,
        )
    with pytest.raises(CapabilityUnavailableError):
        run_guarded(DisabledAdapter("memory.long_term"), lambda: called.append("disabled"))


def test_canonical_boundary_rejections_and_inference_bindings() -> None:
    with pytest.raises(ValidationError):
        VersionedRef(ref="bad space", version="1", content_hash="a" * 64)
    with pytest.raises(ValidationError):
        VersionedRef(ref="skill@1", version="1", content_hash="A" * 64)
    with pytest.raises(ValidationError):
        ContractMeta(
            contract_name="bad name",
            contract_version="v1",
            message_id=uuid4(),
            tenant_id="tenant-a",
            correlation_id="corr",
        )
    with pytest.raises(ValidationError):
        ArtifactRef(
            artifact_id=uuid4(),
            tenant_id="tenant-a",
            version="1",
            content_hash="a" * 64,
            media_type="application/json",
            sensitivity="secret",  # type: ignore[arg-type]
            retention_policy_ref="retention@1",
        )
    with pytest.raises(TypeError):
        canonical_bytes({1: "not a string key"})
    with pytest.raises(TypeError):
        canonical_bytes({"v": {"x"}})
    with pytest.raises(TypeError):
        canonical_bytes({"v": object()})
    with pytest.raises(ValueError):
        canonical_bytes({"at": datetime(2026, 1, 1)})

    context = InferenceContext(context_ref="context@1", summary="summary")
    model_policy = ResolvedModelPolicy(model_ref="model@1", temperature=0.1, max_output_tokens=100)
    retry_policy = ResolvedInferenceRetryPolicy(max_schema_retries=1, max_provider_retries=2)
    budget = InferenceBudget(max_tokens=100, max_cost_micros=10, deadline_ms=1000)
    message = CanonicalMessage(role="user", content="hello", content_schema_ref="message@1")
    request = CanonicalInferenceRequest[Answer](
        meta=_meta("canonical.inference.request"),
        inference_request_id=uuid4(),
        run_id=uuid4(),
        node_id="answer",
        node_attempt=0,
        input=Answer(answer="input"),
        context=context,
        context_refs=(_artifact(),),
        instructions=(message,),
        model_policy=model_policy,
        result_schema_ref="result@1",
        prompt_policy_ref="prompt@1",
        model_policy_ref="model.policy@1",
        retry_policy=retry_policy,
        inference_retry_policy_ref="retry@1",
        budget=budget,
        budget_policy_ref="budget@1",
    )
    assert request.model_policy == model_policy
    result = CanonicalInferenceResult[Answer](
        meta=_meta("canonical.inference.result"),
        inference_request_id=request.inference_request_id,
        result=Answer(answer="done"),
        model_ref="model@1",
        usage=ModelUsage(input_tokens=1, output_tokens=1, cost_micros=1),
        provider_attempts=1,
        schema_retries=0,
    )
    assert result.usage is not None
    with pytest.raises(ValidationError):
        InferenceBudget(max_tokens=0, max_cost_micros=0, deadline_ms=0)
    with pytest.raises(ValidationError):
        KnowledgeFilter(tags=("",))
    assert KnowledgeFilter(tags=("z", "a")).tags == ("a", "z")
    assert RetrievalBudget(max_results=1, max_bytes=1, max_tokens=1, deadline_ms=1).max_results == 1
    assert (
        DomainViewAccepted(
            kind="domain_view_accepted",
            run_id=uuid4(),
            tool_request_id=uuid4(),
            view_schema_ref="view@1",
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
            source_ref="source@1",
            result_hash="b" * 64,
        ).item_count
        is None
    )
    assert MessageDelta(kind="message_delta", message_id=uuid4(), delta_seq=0, safe_delta="d").delta_seq == 0
    assert (
        MessageCompleted(
            kind="message_completed", message_id=uuid4(), last_delta_seq=0, content_hash="c" * 64
        ).last_delta_seq
        == 0
    )


def test_registry_and_rejection_branches_are_explicit() -> None:
    registry = ContractReaderRegistry()
    registry.register("custom.contract", "v1", lambda value: value)
    assert registry.read("custom.contract", "v1", {"ok": True}) == {"ok": True}
    assert registry.versions == (("custom.contract", "v1"),)
    with pytest.raises(ValueError):
        registry.register("custom.contract", "v1", lambda value: value)
    with pytest.raises(UnknownContractError):
        registry.read("unknown.contract", "v1", {})
    with pytest.raises(ValueError):
        registry.register("bad name", "v1", lambda value: value)
        with pytest.raises((TypeError, ValueError)):
            read_contract("inference.payload", {}, version="v1", expected_manifest_hash="a" * 64)
    with pytest.raises(UnknownContractError):
        read_contract("inference.payload", {}, expected_manifest_hash="a" * 64)
    with pytest.raises(UnknownContractError):
        read_contract(
            "skill_execution_spec",
            _spec().model_dump(mode="json"),
            version="v2",
            expected_manifest_hash="a" * 64,
        )
