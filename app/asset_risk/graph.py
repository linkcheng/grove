"""AssetRiskSkill@1 root graph (docs/31 §4).

Fixed flow: validate_input -> retrieve_policy_knowledge -> read_asset_state
-> inference/risk_analysis -> typed_report.  Knowledge and live asset state
stay in their own seams with their own provenance semantics -- the graph
never merges them.  A failed seam stops the run with the seam's typed
failure class; the graph never invents facts to continue.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Hashable
from typing import Any, Literal, TypedDict
from uuid import UUID

from app.asset_risk.contracts import AssetStateQuery
from app.asset_risk.output_gate import AnswerStructureError
from app.asset_risk.read_tool import AssetStateReadTool
from app.contracts.canonical import KnowledgeRequest
from app.knowledge.port import KnowledgePort

ASSET_RISK_GRAPH_REF = "asset-risk.graph@1"
ASSET_RISK_GRAPH_STATE_SCHEMA = "state.asset-risk@1"

_DESCRIPTOR = {
    "ref": ASSET_RISK_GRAPH_REF,
    "state_schema_version": ASSET_RISK_GRAPH_STATE_SCHEMA,
    "nodes": ["validate_input", "retrieve_policy_knowledge", "read_asset_state", "inference", "typed_report"],
}
ASSET_RISK_GRAPH_CONTENT_HASH = hashlib.sha256(
    b"grove.asset-risk.graph.v1\x00" + json.dumps(_DESCRIPTOR, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()


class InferenceAnswer(TypedDict):
    answer: str


type InferenceCaller = Callable[[str, UUID, str], Awaitable[InferenceAnswer]]


class AssetRiskState(TypedDict, total=False):
    stage: Literal[
        "start",
        "validated",
        "knowledge_retrieved",
        "asset_read",
        "inferred",
        "terminal",
        "failed",
    ]
    tenant_id: str
    run_id: str
    asset_refs: tuple[str, ...]
    knowledge_refs: tuple[str, ...]
    knowledge_summaries: tuple[str, ...]
    knowledge_failure: str
    asset_view: dict[str, Any]
    asset_provenance: dict[str, Any]
    asset_failure: str
    inference_answer: str
    report: dict[str, Any]
    failure_class: str
    failure_message: str


def build_asset_risk_graph(
    *,
    knowledge_port: KnowledgePort,
    asset_tool: AssetStateReadTool,
    infer: InferenceCaller,
) -> Any:
    """Compile the fixed AssetRiskSkill graph over the three governed seams."""

    async def validate_input(state: AssetRiskState) -> AssetRiskState:
        if state.get("stage") != "start":
            raise ValueError(f"validate_input expects stage=start, got {state.get('stage')!r}")
        try:
            query = AssetStateQuery(asset_refs=tuple(state["asset_refs"]))
        except ValueError:
            return {
                **state,
                "stage": "failed",
                "failure_class": "input_contract_invalid",
                "failure_message": "asset selection does not satisfy AssetStateQuery@1",
            }
        return {**state, "stage": "validated", "asset_refs": tuple(sorted(set(query.asset_refs)))}

    async def retrieve_policy_knowledge(state: AssetRiskState) -> AssetRiskState:
        from uuid import uuid4

        from app.contracts.canonical import ContractMeta, KnowledgeFilter, RetrievalBudget

        request = KnowledgeRequest(
            meta=ContractMeta(
                contract_name="knowledge.request",
                contract_version="v1",
                message_id=uuid4(),
                tenant_id=state["tenant_id"],
                correlation_id=f"run:{state['run_id']}",
            ),
            decision_id=uuid4(),
            knowledge_request_id=uuid4(),
            run_id=UUID(state["run_id"]),
            authorization_decision_ref="authz.asset-risk@1",
            query="asset risk policy exposure collateral limits",
            knowledge_refs=state.get("knowledge_refs", ()),
            filter=KnowledgeFilter(),
            purpose="asset risk assessment",
            budget=RetrievalBudget(max_results=10, max_bytes=100_000, max_tokens=10_000, deadline_ms=5_000),
            required_citation_level="full",
        )
        from app.auth.context import ActiveTenantContext, Principal, PrincipalKind

        context = ActiveTenantContext(
            tenant_id=state["tenant_id"],
            principal=Principal("asset-risk-skill", PrincipalKind.WORKLOAD, ("execution.run",)),
        )
        outcome = await knowledge_port.retrieve(request, context=context)
        if not outcome.ok or outcome.result is None:
            failure = outcome.failure
            return {
                **state,
                "stage": "failed",
                "failure_class": "knowledge_unavailable",
                "failure_message": failure.safe_message if failure is not None else "knowledge retrieval failed",
            }
        result = outcome.result
        summaries = tuple(item.content[:512] for item in result.items)
        return {**state, "stage": "knowledge_retrieved", "knowledge_summaries": summaries}

    async def read_asset_state(state: AssetRiskState) -> AssetRiskState:
        from uuid import uuid4

        query = AssetStateQuery(asset_refs=state["asset_refs"])
        tool_result = await asset_tool.read(
            tenant_id=state["tenant_id"],
            run_id=UUID(state["run_id"]),
            node_id="read_asset_state",
            query=query,
            tool_request_id=uuid4(),
        )
        if tool_result.failure is not None:
            return {
                **state,
                "stage": "failed",
                "failure_class": tool_result.failure.failure_class,
                "failure_message": tool_result.failure.safe_message,
            }
        assert tool_result.output is not None and tool_result.provenance is not None
        return {
            **state,
            "stage": "asset_read",
            "asset_view": tool_result.output.model_dump(mode="json"),
            "asset_provenance": tool_result.provenance.model_dump(mode="json"),
        }

    async def inference(state: AssetRiskState) -> AssetRiskState:
        context_summary = json.dumps(
            {
                "knowledge": state.get("knowledge_summaries", ()),
                "assets": [
                    {"asset_ref": entry["asset_ref"], "exposure_amount": entry["exposure_amount"]}
                    for entry in state.get("asset_view", {}).get("assets", ())
                ],
            },
            sort_keys=True,
            ensure_ascii=False,
        )[:7168]
        try:
            result = await infer(state["tenant_id"], UUID(state["run_id"]), context_summary)
        except AnswerStructureError as error:
            # Fail closed (WS-7): a garbage answer (empty/placeholder/format
            # leak) never reaches the typed report; the kernel already spent
            # the issued schema-retry budget.
            return {
                **state,
                "stage": "failed",
                "failure_class": "inference_output_invalid",
                "failure_message": f"model answer failed the structural gate: {error}",
            }
        return {**state, "stage": "inferred", "inference_answer": result["answer"]}

    def typed_report(state: AssetRiskState) -> AssetRiskState:
        if state.get("stage") != "inferred":
            raise ValueError(f"typed_report expects stage=inferred, got {state.get('stage')!r}")
        report = {
            "kind": "asset_risk_report.v1",
            "answer": state["inference_answer"],
            "asset_provenance": state.get("asset_provenance", {}),
            "asset_view_hash": _asset_view_hash(state.get("asset_view", {})),
            "knowledge_items": len(state.get("knowledge_summaries", ())),
        }
        return {**state, "stage": "terminal", "report": report}

    from langgraph.graph import END, START, StateGraph

    _NODES = (
        "validate_input",
        "retrieve_policy_knowledge",
        "read_asset_state",
        "inference",
        "typed_report",
    )
    target_map: dict[Hashable, str] = {name: name for name in _NODES}
    target_map["end"] = END
    builder = StateGraph(AssetRiskState)
    builder.add_node("validate_input", validate_input)
    builder.add_node("retrieve_policy_knowledge", retrieve_policy_knowledge)
    builder.add_node("read_asset_state", read_asset_state)
    builder.add_node("inference", inference)
    builder.add_node("typed_report", typed_report)
    builder.add_conditional_edges(START, _route, target_map)
    for node in _NODES:
        builder.add_conditional_edges(node, _route, target_map)
    return builder.compile(checkpointer=None)


_STAGE_TO_NODE: dict[str, str] = {
    "start": "validate_input",
    "validated": "retrieve_policy_knowledge",
    "knowledge_retrieved": "read_asset_state",
    "asset_read": "inference",
    "inferred": "typed_report",
}


def _route(state: AssetRiskState) -> str:
    stage = state.get("stage")
    if stage in ("terminal", "failed"):
        return "end"
    next_node = _STAGE_TO_NODE.get(str(stage))
    if next_node is None:
        raise ValueError(f"unexpected stage {stage!r}")
    return next_node


_NEXT_STAGE: dict[str, str] = {
    "validate_input": "validated",
    "retrieve_policy_knowledge": "knowledge_retrieved",
    "read_asset_state": "asset_read",
    "inference": "inferred",
    "typed_report": "terminal",
}


def _asset_view_hash(view: dict[str, Any]) -> str:
    body = {key: view[key] for key in sorted(view) if key not in {"logical_read_key", "tool_request_id"}}
    return hashlib.sha256(
        b"grove.asset-risk.view.v1\x00"
        + json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


__all__ = [
    "ASSET_RISK_GRAPH_CONTENT_HASH",
    "ASSET_RISK_GRAPH_REF",
    "ASSET_RISK_GRAPH_STATE_SCHEMA",
    "AssetRiskState",
    "build_asset_risk_graph",
]
