"""Minimal pure deterministic two-stage conformance graph.

node_a computes a stable value from the input hash and yields.
node_b computes a terminal value and produces a deterministic output.

No provider, tool, model, network, file, or random source is used.
The graph is the deterministic test kernel for the runtime_worker loop.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, TypedDict


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _int_from_hash(digest: str) -> int:
    return int.from_bytes(bytes.fromhex(digest[:16]), "big") % (2**31)


class ConformanceState(TypedDict, total=False):
    stage: Literal["start", "yielded", "terminal"]
    input_hash: str
    value: int
    yield_marker: str | None
    output_payload: str | None
    output_hash: str | None


def compute_input_hash(text: str) -> str:
    payload = json.dumps({"text": text}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return _sha256(b"grove.conformance.input.v1\x00" + payload.encode("utf-8"))


def node_a(state: ConformanceState) -> ConformanceState:
    if state.get("stage") != "start":
        raise ValueError(f"node_a expects stage=start, got {state.get('stage')!r}")
    input_hash = state["input_hash"]
    digest = _sha256(b"grove.conformance.node_a.v1\x00" + input_hash.encode("ascii"))
    value = _int_from_hash(digest)
    return {"stage": "yielded", "input_hash": input_hash, "value": value, "yield_marker": "stage_a_done"}


def node_b(state: ConformanceState) -> ConformanceState:
    if state.get("stage") != "yielded":
        raise ValueError(f"node_b expects stage=yielded, got {state.get('stage')!r}")
    input_hash = state["input_hash"]
    value = state["value"]
    yield_marker = state.get("yield_marker", "stage_a_done")
    digest_body = json.dumps(
        {"input_hash": input_hash, "value": value, "yield_marker": yield_marker},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = _sha256(b"grove.conformance.node_b.v1\x00" + digest_body.encode("utf-8"))
    terminal_value = _int_from_hash(digest)
    output_payload = json.dumps(
        {"input_hash": input_hash, "value": terminal_value},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    output_hash = _sha256(b"grove.conformance.output.v1\x00" + output_payload.encode("utf-8"))
    return {
        "stage": "terminal",
        "input_hash": input_hash,
        "value": terminal_value,
        "yield_marker": yield_marker,
        "output_payload": output_payload,
        "output_hash": output_hash,
    }


def _route(state: ConformanceState) -> str:
    stage = state.get("stage")
    if stage == "start":
        return "node_a"
    if stage == "yielded":
        return "node_b"
    raise ValueError(f"unexpected stage {stage!r}")


def build_conformance_graph() -> Any:
    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(ConformanceState)
    builder.add_node("node_a", node_a)
    builder.add_node("node_b", node_b)
    builder.add_conditional_edges(START, _route, {"node_a": "node_a", "node_b": "node_b"})
    builder.add_edge("node_a", END)
    builder.add_edge("node_b", END)
    return builder.compile(checkpointer=None)


def initial_state(text: str) -> ConformanceState:
    return {"stage": "start", "input_hash": compute_input_hash(text), "value": 0}


__all__ = [
    "ConformanceState",
    "build_conformance_graph",
    "compute_input_hash",
    "initial_state",
    "node_a",
    "node_b",
]
