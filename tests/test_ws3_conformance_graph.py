"""Unit tests for the pure deterministic conformance graph."""

from __future__ import annotations

from typing import cast

import pytest
from app.execution.conformance_graph import (
    ConformanceState,
    build_conformance_graph,
    compute_input_hash,
    initial_state,
    node_a,
    node_b,
)


def test_input_hash_is_deterministic() -> None:
    h1 = compute_input_hash("hello")
    h2 = compute_input_hash("hello")
    assert h1 == h2
    assert len(h1) == 64


def test_different_inputs_produce_different_hashes() -> None:
    assert compute_input_hash("a") != compute_input_hash("b")


def test_node_a_produces_yielded_state() -> None:
    state = initial_state("test")
    result = node_a(cast(ConformanceState, dict(state)))
    assert result["stage"] == "yielded"
    assert result["yield_marker"] == "stage_a_done"
    assert isinstance(result["value"], int)
    assert 0 <= result["value"] < 2**31


def test_node_b_produces_terminal_state() -> None:
    yielded = node_a(initial_state("test"))
    result = node_b(cast(ConformanceState, dict(yielded)))
    assert result["stage"] == "terminal"
    assert result["output_payload"] is not None
    assert result["output_hash"] is not None
    assert len(result["output_hash"]) == 64
    assert result["output_hash"]


def test_node_a_rejects_wrong_stage() -> None:
    with pytest.raises(ValueError, match="stage=start"):
        node_a({"stage": "yielded", "input_hash": "x", "value": 0})


def test_node_b_rejects_wrong_stage() -> None:
    with pytest.raises(ValueError, match="stage=yielded"):
        node_b({"stage": "start", "input_hash": "x", "value": 0})


def test_graph_two_stage_invoke() -> None:
    graph = build_conformance_graph()
    s1 = graph.invoke(initial_state("conformance"))
    assert s1["stage"] == "yielded"
    s2 = graph.invoke(s1)
    assert s2["stage"] == "terminal"
    assert s2["output_hash"] is not None


def test_graph_deterministic_across_invocations() -> None:
    graph1 = build_conformance_graph()
    graph2 = build_conformance_graph()
    r1 = graph1.invoke(graph1.invoke(initial_state("determinism")))
    r2 = graph2.invoke(graph2.invoke(initial_state("determinism")))
    assert r1["value"] == r2["value"]
    assert r1["output_hash"] == r2["output_hash"]


def test_graph_rejects_terminal_stage_input() -> None:
    graph = build_conformance_graph()
    terminal = node_b(node_a(initial_state("test")))
    with pytest.raises(ValueError):
        graph.invoke(terminal)
