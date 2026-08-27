"""AssetRisk kernel composition: the worker-side seams in one object.

The kernel bundles the compiled graph factory, the typed run-input source
(explicit ``asset_refs`` only) and nothing else.  The production inference
caller adapter lives here too, keeping ``app.inference`` privates out of the
graph and the worker loop.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol
from uuid import UUID

from app.contracts.canonical import CanonicalMessage, InferenceContext
from app.execution.inference_graph import InferenceRequestFactory
from app.inference import TypedInferencePort

type TypedInferencePortLike = TypedInferencePort

# The skill's own inference task.  The manifest-bound factory carries the
# conformance sentinel instruction ("Return G2_OK..."); the AssetRisk skill
# must never inherit it -- the skill owns its task text, the manifest owns
# model/endpoint/limit policies (ADR-0001 kernel boundary, docs/31 §2).
ASSET_RISK_INSTRUCTION = (
    "You are the governed asset-risk assessment skill. The context holds the "
    "frozen policy corpus and the accepted asset state view for this run. "
    "Assess every asset in the view: judge exposure against the cited board "
    "limits per asset class, apply collateral haircuts per the regulatory "
    "schedule, and treat frozen assets as contributing zero exposure relief. "
    "Ground every judgement in the cited policy items only; do not invent "
    "limits or facts. Answer in Chinese, reference the applied policy refs "
    "for each judgement, and finish with one overall conclusion for the "
    "portfolio."
)


class AssetRiskInputSource(Protocol):
    """Explicit asset selection for one run; never derived from free text."""

    async def asset_refs(self, tenant_id: str, run_id: UUID) -> tuple[str, ...]: ...


class AssetRiskKernel:
    """Graph factory plus input source, injected by the composition root."""

    def __init__(
        self,
        *,
        graph_factory: Callable[[], object],
        input_source: AssetRiskInputSource,
    ) -> None:
        self._graph_factory = graph_factory
        self.input_source = input_source

    def build_graph(self) -> object:
        return self._graph_factory()


async def infer_with(port: TypedInferencePortLike, request: object) -> str:
    """Call ``TypedInferencePort.infer`` on the sealed port, answer only."""
    from typing import cast

    from app.contracts.canonical import CanonicalInferenceResult, StructuredInferenceOutput

    result = cast(
        "CanonicalInferenceResult[StructuredInferenceOutput]",
        await port.infer(request, result_type=StructuredInferenceOutput),  # type: ignore[arg-type]
    )
    return result.result.answer


def make_asset_risk_infer_caller(
    port: TypedInferencePortLike,
    request_factory: InferenceRequestFactory,
) -> Callable[[str, UUID, str], Awaitable[dict[str, str]]]:
    """Adapt the sealed inference port into the graph's inference caller.

    The bounded context summary rides the canonical request's ``context``
    field; policies stay bound to the verified manifest via the factory.
    """

    async def caller(tenant_id: str, run_id: UUID, context_summary: str) -> dict[str, str]:
        request = request_factory(tenant_id, run_id).model_copy(
            update={
                "context": InferenceContext(summary=context_summary[:7168]),
                "instructions": (CanonicalMessage(role="user", content=ASSET_RISK_INSTRUCTION),),
            }
        )
        result = await infer_with(port, request)
        return {"answer": result}

    return caller


__all__ = ["AssetRiskInputSource", "AssetRiskKernel", "make_asset_risk_infer_caller"]
