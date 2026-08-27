"""Minimal single-node inference graph executed by the LangGraph kernel.

The ``infer`` node builds one canonical structured-output request
deterministically from the verified manifest policies and the run state,
calls ``TypedInferencePort.infer`` and records the typed answer plus the
transport-ledger facts into the graph state.  No tool, memory, provider
object, or external IO beyond the sealed port.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any, Literal, TypedDict
from uuid import UUID

from app.contracts.canonical import (
    CanonicalInferenceRequest,
    StructuredInferenceInput,
    StructuredInferenceOutput,
)
from app.execution.contracts import ClaimGraphBinding
from app.inference import TypedInferencePort

INFERENCE_GRAPH_REF = "graph.inference@1"
INFERENCE_GRAPH_STATE_SCHEMA = "state.inference@1"
INFERENCE_QUESTION = "Return the exact sentinel required by the response schema."
INFERENCE_INSTRUCTION = "Return G2_OK as the answer."

_DESCRIPTOR = {
    "ref": INFERENCE_GRAPH_REF,
    "state_schema_version": INFERENCE_GRAPH_STATE_SCHEMA,
    "nodes": ["infer"],
}
INFERENCE_GRAPH_CONTENT_HASH = hashlib.sha256(
    b"grove.inference.graph.v1\x00" + json.dumps(_DESCRIPTOR, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()

INFERENCE_GRAPH_BINDING = ClaimGraphBinding(
    graph_ref=INFERENCE_GRAPH_REF,
    graph_version="1",
    graph_state_schema_version=INFERENCE_GRAPH_STATE_SCHEMA,
)


type InferenceRequestFactory = Callable[[str, UUID], CanonicalInferenceRequest[StructuredInferenceInput]]


class InferenceState(TypedDict, total=False):
    stage: Literal["start", "terminal"]
    tenant_id: str
    run_id: str
    input_hash: str
    answer: str
    total_tokens: int
    provider_attempts: int
    schema_retries: int


def compute_inference_input_hash(tenant_id: str, run_id: UUID) -> str:
    payload = json.dumps(
        {"tenant_id": tenant_id, "run_id": str(run_id), "question": INFERENCE_QUESTION},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(b"grove.inference.input.v1\x00" + payload.encode("utf-8")).hexdigest()


def build_inference_graph(
    port: TypedInferencePort,
    request_factory: InferenceRequestFactory,
) -> Any:
    """Compile the single-infer-node kernel bound to one sealed port."""

    async def infer_node(state: InferenceState) -> InferenceState:
        if state.get("stage") != "start":
            raise ValueError(f"infer node expects stage=start, got {state.get('stage')!r}")
        request = request_factory(state["tenant_id"], UUID(state["run_id"]))
        result = await port.infer(request, result_type=StructuredInferenceOutput)
        return {
            "stage": "terminal",
            "tenant_id": state["tenant_id"],
            "run_id": state["run_id"],
            "input_hash": state["input_hash"],
            "answer": result.result.answer,
            "total_tokens": result.usage.input_tokens + result.usage.output_tokens,
            "provider_attempts": result.provider_attempts,
            "schema_retries": result.schema_retries,
        }

    def _route(state: InferenceState) -> str:
        stage = state.get("stage")
        if stage == "start":
            return "infer"
        raise ValueError(f"unexpected stage {stage!r}")

    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(InferenceState)
    builder.add_node("infer", infer_node)
    builder.add_conditional_edges(START, _route, {"infer": "infer"})
    builder.add_edge("infer", END)
    return builder.compile(checkpointer=None)


__all__ = [
    "INFERENCE_GRAPH_BINDING",
    "INFERENCE_QUESTION",
    "InferenceRequestFactory",
    "InferenceState",
    "build_inference_graph",
    "compute_inference_input_hash",
]
