"""Closed-world registry binding exact spec graph refs to execution kernels.

Routing matches the exact ``(graph ref, version, state-schema version)``
triple published in the run's ``SkillExecutionSpec``; unknown triples and
capability mismatches fail closed instead of falling back.  Graph artifact
content hashes are bound by the runtime build (the graph implementations ship
inside the image), so routing deliberately does not re-verify them here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from app.asset_risk.kernel import AssetRiskKernel
from app.execution.conformance_graph import build_conformance_graph
from app.execution.contracts import CONFORMANCE_GRAPH_BINDING, ClaimGraphBinding
from app.execution.inference_graph import (
    INFERENCE_GRAPH_BINDING,
    InferenceRequestFactory,
    build_inference_graph,
)
from app.inference import TypedInferencePort

# The exact binding the WS-1 fixture release publishes for the conformance
# graph; kept in sync by an anti-drift unit test against the fixture bundle.
# The conformance routing triple lives in app.execution.contracts so the
# claim contract stays self-contained; anti-drift against the fixture bundle
# is asserted by tests/test_ws6_graph_registry.py.


class GraphResolutionError(Exception):
    """Unknown graph binding or missing capability; never guessed around."""

    def __init__(
        self,
        reason: Literal["unknown_graph", "inference_unavailable", "asset_risk_unavailable"],
        detail: str,
    ) -> None:
        super().__init__(detail)
        self.reason = reason


ASSET_RISK_GRAPH_BINDING = ClaimGraphBinding(
    graph_ref="graph.asset-risk@1",
    graph_version="1",
    graph_state_schema_version="state.asset-risk@1",
)


@dataclass(frozen=True)
class GraphKernel:
    kind: Literal["conformance", "inference", "asset_risk"]
    graph_factory: Callable[[], object]


def _binding_triple(binding: ClaimGraphBinding) -> tuple[str, str, str]:
    return (binding.graph_ref, binding.graph_version, binding.graph_state_schema_version)


def resolve_graph_kernel(
    binding: ClaimGraphBinding,
    *,
    inference_port: TypedInferencePort | None,
    inference_request_factory: InferenceRequestFactory | None,
    asset_risk_kernel: AssetRiskKernel | None = None,
) -> GraphKernel:
    """Resolve one spec graph binding to its compiled kernel, fail closed."""

    triple = _binding_triple(binding)
    if triple == _binding_triple(CONFORMANCE_GRAPH_BINDING):
        return GraphKernel(kind="conformance", graph_factory=build_conformance_graph)
    if triple == _binding_triple(INFERENCE_GRAPH_BINDING):
        if inference_port is None or inference_request_factory is None:
            raise GraphResolutionError(
                "inference_unavailable",
                "run binds the inference graph but the worker has no production inference capability",
            )
        return GraphKernel(
            kind="inference",
            graph_factory=lambda: build_inference_graph(inference_port, inference_request_factory),
        )
    if triple == _binding_triple(ASSET_RISK_GRAPH_BINDING):
        if asset_risk_kernel is None:
            raise GraphResolutionError(
                "asset_risk_unavailable",
                "run binds the asset-risk graph but the worker has no composed asset-risk kernel",
            )
        return GraphKernel(kind="asset_risk", graph_factory=asset_risk_kernel.build_graph)
    raise GraphResolutionError("unknown_graph", f"unknown graph binding: {triple!r}")


__all__ = [
    "ASSET_RISK_GRAPH_BINDING",
    "CONFORMANCE_GRAPH_BINDING",
    "INFERENCE_GRAPH_BINDING",
    "GraphKernel",
    "GraphResolutionError",
    "resolve_graph_kernel",
]
