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

from app.asset_risk.output_gate import AnswerStructureError, validate_answer_structure
from app.contracts.canonical import (
    CanonicalInferenceRequest,
    CanonicalMessage,
    InferenceContext,
    StructuredInferenceInput,
)
from app.execution.inference_graph import InferenceRequestFactory
from app.inference import TypedInferencePort
from app.inference.errors import InferenceError, InferenceErrorCode

type TypedInferencePortLike = TypedInferencePort

# The skill's own inference task.  The manifest-bound factory carries the
# conformance sentinel instruction ("Return G2_OK..."); the AssetRisk skill
# must never inherit it -- the skill owns its task text, the manifest owns
# model/endpoint/limit policies (ADR-0001 kernel boundary, docs/31 §2).
# The output-format contract mirrors what the prompted-JSON transport
# expects; stating it in the task text measurably stabilizes the answer
# shape (WS-7 answer-quality iteration).
ASSET_RISK_INSTRUCTION = (
    "You are the governed asset-risk assessment skill. The context holds the "
    "frozen policy corpus and the accepted asset state view for this run. "
    "Assess every asset in the view: judge exposure against the cited board "
    "limits per asset class, apply collateral haircuts per the regulatory "
    "schedule, and treat frozen assets as contributing zero exposure relief. "
    "Ground every judgement in the cited policy items only; do not invent "
    "limits or facts. Answer in Chinese, reference the applied policy refs "
    "for each judgement, and finish with one overall conclusion for the "
    "portfolio.\n"
    '输出格式硬性要求：最终回复必须是且仅是一个 JSON 对象，唯一键为 "answer"，'
    "其值为完整的中文风险评估正文（不少于 200 字，包含逐资产判断与组合结论）。"
    "不得输出 JSON 以外的任何文本；不得把占位符（如 $your_answer）、格式说明或"
    "模式定义写进答案值；答案值必须是直接可读的评估正文。"
)

# Retry-time corrective suffix.  Degenerate outputs (too short, format echo)
# are correlated across identical requests, so an exact resend tends to
# reproduce the same failure; the corrective text breaks the correlation
# without changing the task or the manifest-bound policies.
ASSET_RISK_RETRY_SUFFIX = (
    "\n注意：你上一条回复未满足输出格式要求（过短或格式泄漏）。请重新作答："
    '仅输出一个 JSON 对象（唯一键 "answer"），值为完整中文风险评估正文，'
    "逐资产判断并给出组合结论，正文不少于 200 字，不包含任何格式说明。"
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
    Every answer passes the runtime structural gate (WS-7): a garbage
    answer (empty/placeholder/format-leak) is retried on a fresh request
    within the issued ``max_schema_retries`` budget and fails closed with
    ``AnswerStructureError`` when the budget is exhausted.
    """

    async def caller(tenant_id: str, run_id: UUID, context_summary: str) -> dict[str, str]:
        def build_request(corrective: bool) -> CanonicalInferenceRequest[StructuredInferenceInput]:
            content = ASSET_RISK_INSTRUCTION + (ASSET_RISK_RETRY_SUFFIX if corrective else "")
            return request_factory(tenant_id, run_id).model_copy(
                update={
                    "context": InferenceContext(summary=context_summary[:7168]),
                    "instructions": (CanonicalMessage(role="user", content=content),),
                }
            )

        first_request = build_request(corrective=False)
        max_attempts = 1 + first_request.retry_policy.max_schema_retries
        last_error: AnswerStructureError | None = None
        for attempt in range(max_attempts):
            request = first_request if attempt == 0 else build_request(corrective=True)
            try:
                result = await infer_with(port, request)
                answer = validate_answer_structure(result)
            except AnswerStructureError as error:
                last_error = error
                continue
            except InferenceError as error:
                # Retry ownership split: the adapter owns provider/network
                # errors (its own transient retries), and this outer budget
                # retries ONLY the unparseable-output class (INVALID_RESULT),
                # which shares one root cause with structural garbage -- the
                # unenforced prompted-JSON gateway mode.  Any other inference
                # error fails fast with its original type, so the worker's
                # dead-letter semantics stay unchanged.  The wall-clock
                # invoke budget (strictly below lease minus margin), not the
                # attempt count, bounds worst-case provider sends.
                if error.code is not InferenceErrorCode.INVALID_RESULT or attempt + 1 >= max_attempts:
                    raise
                continue
            return {"answer": answer}
        raise AnswerStructureError(
            f"answer failed the structural gate after {max_attempts} attempts: {last_error}"
        ) from last_error

    return caller


__all__ = ["AssetRiskInputSource", "AssetRiskKernel", "make_asset_risk_infer_caller"]
