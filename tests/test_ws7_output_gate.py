"""WS-7 T2: runtime structural gate over the AssetRisk model answer.

The gateway's prompted-JSON mode does not enforce its advertised schema
(verified fake support), so answers randomly arrive empty, as leaked
placeholders ("$your_answer") or as schema-echo gibberish.  The kernel
must retry within the issued schema-retry budget and fail closed as a
typed failure instead of shipping a garbage answer into the typed report.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from app.asset_risk.contracts import AssetStateEntry, AssetStateQuery, AssetStateView
from app.auth.context import ActiveTenantContext
from app.contracts.canonical import (
    CanonicalInferenceRequest,
    CanonicalInferenceResult,
    KnowledgeRequest,
    ModelUsage,
    StructuredInferenceInput,
    StructuredInferenceOutput,
    derive_contract_meta,
)
from app.inference.errors import InferenceError, InferenceErrorCode
from app.knowledge.port import KnowledgeOutcome
from app.worker.inference import make_inference_request_factory
from tests.inference.test_pydantic_ai_adapter import _manifest

_GOOD_ANSWER = (
    "经对照冻结政策语料评估：本组合仅含一项信用资产，敞口1200元，未见超限证据；"
    "抵押品折价规则适用但视图未载明折扣率数值，不虚构比率。组合整体结论：暂判合规，"
    "建议补充具名限额数值后复核。"
)
assert len(_GOOD_ANSWER) >= 80


class _ScriptedPort:
    """Returns the scripted answers in order, recording every request."""

    def __init__(self, answers: tuple[str, ...]) -> None:
        self._answers = answers
        self.requests: list[CanonicalInferenceRequest[StructuredInferenceInput]] = []

    async def infer(self, request: object, *, result_type: type) -> object:
        del result_type
        canonical = cast("CanonicalInferenceRequest[StructuredInferenceInput]", request)
        self.requests.append(canonical)
        answer = self._answers[min(len(self.requests) - 1, len(self._answers) - 1)]
        return _result(canonical, answer)


class _FlakyPort:
    """Raises InferenceError for the first N calls, then answers normally."""

    def __init__(
        self, failures: int, answer: str, *, code: InferenceErrorCode = InferenceErrorCode.INVALID_RESULT
    ) -> None:
        self._failures = failures
        self._answer = answer
        self._code = code
        self.calls = 0

    async def infer(self, request: object, *, result_type: type) -> object:
        canonical = cast("CanonicalInferenceRequest[StructuredInferenceInput]", request)
        self.calls += 1
        if self.calls <= self._failures:
            raise InferenceError(self._code)
        return _result(canonical, self._answer)


def _result(
    canonical: CanonicalInferenceRequest[StructuredInferenceInput], answer: str
) -> CanonicalInferenceResult[StructuredInferenceOutput]:
    return CanonicalInferenceResult[StructuredInferenceOutput](
        meta=derive_contract_meta(
            canonical.meta,
            contract_name="canonical.inference.result",
            causation_id=canonical.meta.message_id,
        ),
        inference_request_id=canonical.inference_request_id,
        result=StructuredInferenceOutput(answer=answer),
        model_ref="model@2026",
        usage=ModelUsage(input_tokens=1, output_tokens=1, cost_micros=1),
        provider_attempts=1,
        schema_retries=0,
        provider_response_ref=None,
    )


# --- structural gate -------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "reason_fragment"),
    [
        ("", "empty"),
        ("   \n\t ", "empty"),
        ("短答案", "shorter than 80"),
        ("$your_answer", "leaks prompted-format text"),
        ("评估结论：$your_answer 不适用", "leaks prompted-format text"),
        ('模式定义仅为 {"additionalProperties": false}', "leaks prompted-format text"),
        ('{"properties": {"answer": {"type": "string"}}}', "leaks prompted-format text"),
        ('{"required": ["answer"]}', "leaks prompted-format text"),
        ('{"$schema": "https://example.invalid"}', "leaks prompted-format text"),
        ('{"answer": "好的，这是一个足够长的中文风险评估答案，用于测试原始 JSON 回声形态。"}', "raw JSON echo"),
    ],
)
def test_gate_rejects_structurally_invalid_answers(answer: str, reason_fragment: str) -> None:
    from app.asset_risk.output_gate import AnswerStructureError, validate_answer_structure

    with pytest.raises(AnswerStructureError, match=re.escape(reason_fragment)):
        validate_answer_structure(answer)


def test_gate_rejects_non_exact_str_types() -> None:
    from app.asset_risk.output_gate import AnswerStructureError, validate_answer_structure

    class _StrSubclass(str):
        pass

    for bad in (b"bytes", 123, None, _StrSubclass(_GOOD_ANSWER)):
        with pytest.raises(AnswerStructureError, match="exact str"):
            validate_answer_structure(bad)


def test_gate_accepts_realistic_chinese_answer_and_strips_padding() -> None:
    from app.asset_risk.output_gate import validate_answer_structure

    validated = validate_answer_structure(f"  \n{_GOOD_ANSWER}\n  ")
    assert validated == _GOOD_ANSWER


def test_gate_leak_markers_are_frozen_and_deterministic() -> None:
    from app.asset_risk.output_gate import LEAK_MARKERS

    assert LEAK_MARKERS == frozenset(
        {"$your_answer", "additionalProperties", '"properties"', '"required"', '{"$schema"'}
    )


# --- kernel bounded retry --------------------------------------------------


@pytest.mark.asyncio
async def test_kernel_retries_structural_garbage_then_accepts_good_answer() -> None:
    from app.asset_risk.kernel import make_asset_risk_infer_caller

    port = _ScriptedPort(("$your_answer", "   ", _GOOD_ANSWER))
    caller = make_asset_risk_infer_caller(cast(Any, port), make_inference_request_factory(_manifest(schema_retries=2)))
    answer = await caller("tenant-a", uuid4(), '{"assets": []}')
    assert answer == {"answer": _GOOD_ANSWER}
    assert len(port.requests) == 3
    # Every attempt carries the skill-owned instruction on a fresh request.
    for request in port.requests:
        assert any(item.content.startswith("You are the governed asset-risk") for item in request.instructions)


@pytest.mark.asyncio
async def test_kernel_fails_closed_after_the_issued_schema_retry_budget() -> None:
    from app.asset_risk.kernel import make_asset_risk_infer_caller
    from app.asset_risk.output_gate import AnswerStructureError

    port = _ScriptedPort(("$your_answer",))
    caller = make_asset_risk_infer_caller(cast(Any, port), make_inference_request_factory(_manifest(schema_retries=2)))
    with pytest.raises(AnswerStructureError, match="3 attempts"):
        await caller("tenant-a", uuid4(), '{"assets": []}')
    # The issued budget is authoritative: exactly 1 + max_schema_retries calls.
    assert len(port.requests) == 1 + port.requests[0].retry_policy.max_schema_retries
    assert len({request.inference_request_id for request in port.requests}) == len(port.requests)


@pytest.mark.asyncio
async def test_kernel_retries_adapter_level_inference_errors_within_same_budget() -> None:
    from app.asset_risk.kernel import make_asset_risk_infer_caller

    port = _FlakyPort(failures=2, answer=_GOOD_ANSWER)
    caller = make_asset_risk_infer_caller(cast(Any, port), make_inference_request_factory(_manifest(schema_retries=2)))
    answer = await caller("tenant-a", uuid4(), '{"assets": []}')
    assert answer == {"answer": _GOOD_ANSWER}
    assert port.calls == 3


@pytest.mark.asyncio
async def test_kernel_propagates_inference_error_after_budget_exhaustion() -> None:
    from app.asset_risk.kernel import make_asset_risk_infer_caller

    port = _FlakyPort(failures=99, answer=_GOOD_ANSWER)
    caller = make_asset_risk_infer_caller(cast(Any, port), make_inference_request_factory(_manifest(schema_retries=2)))
    with pytest.raises(InferenceError, match="invalid_result"):
        await caller("tenant-a", uuid4(), '{"assets": []}')
    assert port.calls == 3


@pytest.mark.asyncio
async def test_kernel_fails_fast_on_non_parse_inference_errors() -> None:
    from app.asset_risk.kernel import make_asset_risk_infer_caller

    port = _FlakyPort(failures=99, answer=_GOOD_ANSWER, code=InferenceErrorCode.PROVIDER_TRANSIENT)
    caller = make_asset_risk_infer_caller(cast(Any, port), make_inference_request_factory(_manifest(schema_retries=2)))
    with pytest.raises(InferenceError, match="provider_transient"):
        await caller("tenant-a", uuid4(), '{"assets": []}')
    # Provider/network errors are the adapter's own retry domain; the skill
    # layer must not multiply that budget.
    assert port.calls == 1


# --- graph fail-closed typed failure ----------------------------------------


class _FrozenKnowledge:
    async def retrieve(self, request: KnowledgeRequest, *, context: ActiveTenantContext) -> KnowledgeOutcome:
        from app.contracts.canonical import (
            Citation,
            ContractMeta,
            KnowledgeItem,
            KnowledgeResult,
        )

        del context

        def _citation(ref: str) -> Citation:
            return Citation(
                snapshot_ref="knowledge.asset-risk",
                snapshot_version="v1",
                source_version="2026-08",
                locator=f"doc://{ref}",
                content_hash="e" * 64,
            )

        class _Outcome:
            ok = True
            failure = None
            result = KnowledgeResult(
                meta=ContractMeta(
                    contract_name="knowledge.result",
                    contract_version="v1",
                    message_id=uuid4(),
                    tenant_id=request.meta.tenant_id,
                    correlation_id=request.meta.correlation_id,
                ),
                knowledge_request_id=request.knowledge_request_id,
                result_class="ok",
                items=(
                    KnowledgeItem(
                        item_ref="policy.exposure@1",
                        content="Aggregate exposure must stay within board limits.",
                        citations=(_citation("policy.exposure@1"),),
                    ),
                ),
                citations=(_citation("policy.exposure@1"),),
                knowledge_snapshot_ref="knowledge.asset-risk",
                knowledge_snapshot_version="v1",
                knowledge_snapshot_content_hash="a" * 64,
                applied_acl_ref="acl@1",
                applied_acl_hash="b" * 64,
                retrieval_policy_ref="retrieval@1",
                retrieval_policy_hash="c" * 64,
                truncated=False,
            )

        return cast(KnowledgeOutcome, _Outcome())


class _SingleAssetSource:
    async def read(
        self, query: AssetStateQuery, *, tenant_id: str, logical_read_key: str, tool_request_id: UUID
    ) -> AssetStateView:
        del tenant_id
        return AssetStateView(
            tool_request_id=tool_request_id,
            logical_read_key=logical_read_key,
            assets=tuple(
                AssetStateEntry(asset_ref=ref, asset_class="credit", exposure_amount=100, currency="CNY")
                for ref in sorted(query.asset_refs)
            ),
            observed_at=datetime(2026, 8, 27, tzinfo=UTC),
            source_revision_or_watermark="asset-state:rev-g7",
        )


@pytest.mark.asyncio
async def test_graph_fails_closed_with_typed_failure_when_gate_exhausts_budget() -> None:
    from app.asset_risk.graph import InferenceAnswer, build_asset_risk_graph
    from app.asset_risk.kernel import make_asset_risk_infer_caller
    from app.asset_risk.read_tool import AssetStateReadCeiling, AssetStateReadTool

    port = _ScriptedPort(("$your_answer",))
    graph = build_asset_risk_graph(
        knowledge_port=cast(Any, _FrozenKnowledge()),
        asset_tool=AssetStateReadTool(
            source=_SingleAssetSource(), ceiling=AssetStateReadCeiling(manifest_max_asset_refs=16)
        ),
        infer=cast(
            "Callable[[str, UUID, str], Awaitable[InferenceAnswer]]",
            make_asset_risk_infer_caller(cast(Any, port), make_inference_request_factory(_manifest())),
        ),
    )
    terminal = await graph.ainvoke(
        {
            "stage": "start",
            "tenant_id": "tenant-a",
            "run_id": str(uuid4()),
            "asset_refs": ("asset.demo-1",),
        }
    )
    assert terminal["stage"] == "failed"
    assert terminal["failure_class"] == "inference_output_invalid"
    assert "structural gate" in terminal["failure_message"]
    # Fail closed: no garbage answer and no typed report reach the domain.
    assert "inference_answer" not in terminal
    assert "report" not in terminal


@pytest.mark.asyncio
async def test_kernel_retry_request_carries_the_corrective_suffix() -> None:
    from app.asset_risk.kernel import ASSET_RISK_INSTRUCTION, ASSET_RISK_RETRY_SUFFIX, make_asset_risk_infer_caller

    port = _ScriptedPort(("短", "还是短", _GOOD_ANSWER))
    caller = make_asset_risk_infer_caller(cast(Any, port), make_inference_request_factory(_manifest(schema_retries=2)))
    answer = await caller("tenant-a", uuid4(), '{"assets": []}')
    assert answer == {"answer": _GOOD_ANSWER}
    contents = [item.content for request in port.requests for item in request.instructions]
    assert contents[0] == ASSET_RISK_INSTRUCTION
    for retried in contents[1:]:
        assert retried == ASSET_RISK_INSTRUCTION + ASSET_RISK_RETRY_SUFFIX
