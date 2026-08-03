"""One-way trust-boundary enrichment helpers.

Model output is accepted as a payload only.  These helpers make the two
subsequent trust transitions explicit: a trusted node adapter enriches a
decision, then a policy node creates a request/command.  No helper performs
provider I/O.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypeVar, cast
from uuid import UUID

from pydantic import BaseModel

from app.contracts.canonical import (
    CanonicalDecision,
    ContractMeta,
    InferenceDecisionPayload,
    KnowledgeFilter,
    KnowledgeProposal,
    KnowledgeRequest,
    RetrievalBudget,
    ToolCommand,
    ToolProposal,
    TypedSchemaRegistry,
    derive_contract_meta,
    invoke_tool_command_builder,
    parse_inference_decision,
    parse_knowledge_proposal,
)

InputT = TypeVar("InputT", bound=BaseModel)


def enrich_decision(
    payload: InferenceDecisionPayload,
    *,
    meta: ContractMeta,
    run_id: UUID,
    decision_id: UUID,
    manifest: Any | None = None,
    schema_registry: Any | None = None,
    input_type: Any | None = None,
    output_type: Any | None = None,
    expected_manifest_hash: str,
) -> CanonicalDecision:
    """Inject trusted metadata into a validated model payload."""

    payload = parse_inference_decision(
        payload,
        manifest=manifest,
        schema_registry=schema_registry,
        input_type=input_type,
        output_type=output_type,
        expected_manifest_hash=expected_manifest_hash,
    )
    decision_meta = derive_contract_meta(
        meta,
        contract_name="canonical.decision",
        contract_version="v1",
        causation_id=meta.message_id,
    )

    if payload.kind == "final_answer":
        from app.contracts.canonical import FinalAnswer

        final_answer_generic: Any = FinalAnswer
        final_answer_type: Any = final_answer_generic[type(payload.output)]
        return cast(
            CanonicalDecision,
            final_answer_type(
                kind="final_answer",
                meta=decision_meta,
                run_id=run_id,
                decision_id=decision_id,
                output=payload.output,
                artifact_refs=(),
                rationale_summary=payload.rationale_summary,
                confidence=payload.confidence,
            ),
        )
    if payload.kind == "tool_proposal":
        tool_proposal_generic: Any = ToolProposal
        tool_proposal_type: Any = tool_proposal_generic[type(payload.input)]
        return cast(
            CanonicalDecision,
            tool_proposal_type(
                kind="tool_proposal",
                meta=decision_meta,
                run_id=run_id,
                decision_id=decision_id,
                tool_ref=payload.tool_ref,
                input=payload.input,
                rationale_summary=payload.rationale_summary,
                confidence=payload.confidence,
            ),
        )
    raise ValueError(f"unknown payload discriminator: {payload.kind}")


def enrich_knowledge_decision(
    payload: Any,
    *,
    meta: ContractMeta,
    run_id: UUID,
    decision_id: UUID,
) -> KnowledgeProposal:
    """Enrich only the schema-free KnowledgeProposal payload.

    This seam deliberately has no Manifest, schema registry, or expected hash;
    the dedicated parser rejects all executable proposal discriminators before
    trusted metadata is injected.
    """

    validated = parse_knowledge_proposal(payload)
    return KnowledgeProposal(
        kind="knowledge_proposal",
        meta=derive_contract_meta(
            meta,
            contract_name="canonical.decision",
            contract_version="v1",
            causation_id=meta.message_id,
        ),
        run_id=run_id,
        decision_id=decision_id,
        query=validated.query,
        knowledge_refs=validated.knowledge_refs,
        filter=validated.filter,
        rationale_summary=validated.rationale_summary,
        confidence=validated.confidence,
    )


def knowledge_request_from_decision(
    decision: KnowledgeProposal,
    *,
    authorization_decision_ref: str,
    knowledge_request_id: UUID,
    purpose: str,
    required_citation_level: Literal["none", "source", "full"] = "source",
    filter: KnowledgeFilter,
    budget: RetrievalBudget,
) -> KnowledgeRequest:
    """Create a request whose filter equals or tightens the proposal filter.

    A policy node may add tag/language constraints, but it cannot unset an
    existing language or remove an existing tag.  The caller-supplied filter
    therefore never replaces the proposal's lower-bound restrictions.
    """

    if not isinstance(decision, KnowledgeProposal) or decision.kind != "knowledge_proposal":
        raise ValueError("only KnowledgeProposal can become KnowledgeRequest")
    if not isinstance(filter, KnowledgeFilter):
        raise TypeError("knowledge request filter must be a validated KnowledgeFilter")
    if decision.filter.language is not None and filter.language != decision.filter.language:
        raise ValueError("knowledge request cannot unset or change the proposal language filter")
    proposal_tags = set(decision.filter.tags)
    requested_tags = set(filter.tags)
    if not proposal_tags.issubset(requested_tags):
        raise ValueError("knowledge request cannot remove proposal tag constraints")
    return KnowledgeRequest(
        meta=derive_contract_meta(
            decision.meta,
            contract_name="knowledge.request",
            contract_version="v1",
            causation_id=decision.meta.message_id,
        ),
        decision_id=decision.decision_id,
        knowledge_request_id=knowledge_request_id,
        run_id=decision.run_id,
        authorization_decision_ref=authorization_decision_ref,
        query=decision.query,
        knowledge_refs=decision.knowledge_refs,
        filter=filter,
        purpose=purpose,
        budget=budget,
        required_citation_level=required_citation_level,
    )


def tool_command_from_decision(
    decision: ToolProposal[InputT],
    *,
    authorization_decision_ref: str,
    tool_request_id: UUID,
    timeout_policy_ref: str,
    manifest: Any,
    expected_manifest_hash: str,
    tool_binding: Any,
    schema_registry: TypedSchemaRegistry,
    artifact_payloads: Mapping[str, bytes] | None = None,
) -> ToolCommand[InputT]:
    """Create a command only from one externally verified exact binding.

    There is intentionally no legacy overload: a command without a
    Spec-bound Manifest hash, exact ToolBinding and strict schema registry is
    not an executable object.
    """

    return cast(
        ToolCommand[InputT],
        invoke_tool_command_builder(
            decision,
            authorization_decision_ref=authorization_decision_ref,
            tool_request_id=tool_request_id,
            timeout_policy_ref=timeout_policy_ref,
            manifest=manifest,
            expected_manifest_hash=expected_manifest_hash,
            tool_binding=tool_binding,
            schema_registry=schema_registry,
            artifact_payloads=artifact_payloads,
        ),
    )


__all__ = [
    "enrich_decision",
    "enrich_knowledge_decision",
    "knowledge_request_from_decision",
    "tool_command_from_decision",
]
