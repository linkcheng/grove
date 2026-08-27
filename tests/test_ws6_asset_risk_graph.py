"""WS-6 D2: the AssetRiskSkill root graph over the three governed seams."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from app.asset_risk.contracts import AssetStateEntry, AssetStateQuery, AssetStateView
from app.asset_risk.graph import ASSET_RISK_GRAPH_REF, InferenceAnswer, build_asset_risk_graph
from app.asset_risk.read_tool import AssetStateReadCeiling, AssetStateReadTool
from app.auth.context import ActiveTenantContext
from app.contracts.canonical import (
    CanonicalFailure,
    Citation,
    ContractMeta,
    KnowledgeItem,
    KnowledgeRequest,
    KnowledgeResult,
    RetryOwner,
)
from app.knowledge.port import KnowledgeOutcome

RUN_ID = uuid4()
TENANT = "tenant-a"
OBSERVED = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)


def _view(refs: tuple[str, ...]) -> AssetStateView:
    return AssetStateView(
        tool_request_id=uuid4(),
        logical_read_key="asset.state.read:key",
        assets=tuple(
            AssetStateEntry(asset_ref=ref, asset_class="credit", exposure_amount=100, currency="CNY") for ref in refs
        ),
        observed_at=OBSERVED,
        source_revision_or_watermark="asset-state:rev-7",
    )


class FakeKnowledge:
    def __init__(self, failure: CanonicalFailure | None = None) -> None:
        self.failure = failure

    async def retrieve(self, request: KnowledgeRequest, *, context: ActiveTenantContext) -> KnowledgeOutcome:
        if self.failure is not None:
            return KnowledgeOutcome(failure=self.failure)
        citation = Citation(
            snapshot_ref="knowledge.asset-risk",
            snapshot_version="v1",
            source_version="2026-08",
            locator="doc://policy.exposure@1",
            content_hash="d" * 64,
        )
        return KnowledgeOutcome(
            result=KnowledgeResult(
                meta=ContractMeta(
                    contract_name="knowledge.result",
                    contract_version="v1",
                    message_id=uuid4(),
                    tenant_id=TENANT,
                    correlation_id="c",
                ),
                knowledge_request_id=uuid4(),
                result_class="ok",
                items=(
                    KnowledgeItem(
                        item_ref="policy.exposure@1",
                        content="Board exposure limits apply.",
                        citations=(citation,),
                    ),
                ),
                citations=(citation,),
                knowledge_snapshot_ref="knowledge.asset-risk",
                knowledge_snapshot_version="v1",
                knowledge_snapshot_content_hash="a" * 64,
                applied_acl_ref="acl@1",
                applied_acl_hash="b" * 64,
                retrieval_policy_ref="retrieval@1",
                retrieval_policy_hash="c" * 64,
                truncated=False,
            )
        )


class FakeSource:
    def __init__(self, view: AssetStateView | None) -> None:
        self.view = view

    async def read(
        self, query: AssetStateQuery, *, tenant_id: str, logical_read_key: str, tool_request_id: UUID
    ) -> AssetStateView | CanonicalFailure:
        if self.view is None:
            return CanonicalFailure(
                error_code="asset_state.resource_selection_unavailable",
                failure_class="resource_selection_unavailable",
                retry_owner=cast(RetryOwner, "run_coordination"),
                retryable=False,
                safe_message="the requested asset selection is unavailable",
            )
        return self.view.model_copy(update={"logical_read_key": logical_read_key, "tool_request_id": tool_request_id})


async def _infer_ok(tenant_id: str, run_id: UUID, summary: str) -> InferenceAnswer:
    return {"answer": "risk within board limits"}


def _graph(refs_view: AssetStateView | None, knowledge_failure: CanonicalFailure | None = None) -> Any:
    tool = AssetStateReadTool(source=FakeSource(refs_view), ceiling=AssetStateReadCeiling(manifest_max_asset_refs=8))
    return build_asset_risk_graph(knowledge_port=FakeKnowledge(knowledge_failure), asset_tool=tool, infer=_infer_ok)


def _state(refs: tuple[str, ...] = ("asset.a", "asset.b")) -> dict[str, object]:
    return {"stage": "start", "tenant_id": TENANT, "run_id": str(RUN_ID), "asset_refs": refs}


@pytest.mark.asyncio
async def test_happy_path_produces_a_typed_report_with_full_provenance() -> None:
    refs = ("asset.a", "asset.b")
    graph = _graph(_view(refs))
    terminal = await graph.ainvoke(_state(refs))
    assert terminal["stage"] == "terminal"
    report = terminal["report"]
    assert report["kind"] == "asset_risk_report.v1"
    assert report["answer"] == "risk within board limits"
    assert report["knowledge_items"] == 1
    assert report["asset_provenance"]["source_revision_or_watermark"] == "asset-state:rev-7"
    assert len(report["asset_view_hash"]) == 64


@pytest.mark.asyncio
async def test_invalid_selection_fails_closed_before_any_seam() -> None:
    graph = _graph(_view(("asset.a",)))
    result = await graph.ainvoke({**_state(("asset.a", "asset.a"))})
    assert result["stage"] == "failed"
    assert result["failure_class"] == "input_contract_invalid"


@pytest.mark.asyncio
async def test_knowledge_failure_stops_the_run_with_the_typed_class() -> None:
    failure = CanonicalFailure(
        error_code="knowledge.denied",
        failure_class="denied",
        retry_owner=cast(RetryOwner, "execution_kernel"),
        retryable=False,
        safe_message="principal is not permitted",
    )
    graph = _graph(_view(("asset.a",)), knowledge_failure=failure)
    result = await graph.ainvoke(_state(("asset.a",)))
    assert result["stage"] == "failed"
    assert result["failure_class"] == "knowledge_unavailable"
    assert "permitted" in result["failure_message"]


@pytest.mark.asyncio
async def test_partial_asset_delivery_stops_the_run_without_leakage() -> None:
    graph = _graph(_view(("asset.a",)))  # two requested, one delivered
    result = await graph.ainvoke(_state(("asset.a", "asset.b")))
    assert result["stage"] == "failed"
    assert result["failure_class"] == "resource_selection_unavailable"
    assert "asset.b" not in result["failure_message"]


@pytest.mark.asyncio
async def test_inference_receives_knowledge_and_asset_context() -> None:
    seen: list[str] = []

    async def infer(tenant_id: str, run_id: UUID, summary: str) -> InferenceAnswer:
        seen.append(summary)
        return {"answer": "ok"}

    tool = AssetStateReadTool(
        source=FakeSource(_view(("asset.a",))), ceiling=AssetStateReadCeiling(manifest_max_asset_refs=8)
    )
    graph = build_asset_risk_graph(knowledge_port=FakeKnowledge(None), asset_tool=tool, infer=infer)
    await graph.ainvoke(_state(("asset.a",)))
    assert len(seen) == 1
    assert "Board exposure limits" in seen[0]
    assert "asset.a" in seen[0]
    assert "100" in seen[0]


def test_graph_descriptor_is_stable() -> None:
    from app.asset_risk.graph import ASSET_RISK_GRAPH_CONTENT_HASH

    assert len(ASSET_RISK_GRAPH_CONTENT_HASH) == 64
    assert ASSET_RISK_GRAPH_REF == "asset-risk.graph@1"


@pytest.mark.asyncio
async def test_inference_request_carries_the_skill_instruction_not_the_g2_sentinel() -> None:
    """The manifest factory's conformance sentinel must never reach the model."""

    from app.asset_risk.kernel import ASSET_RISK_INSTRUCTION, make_asset_risk_infer_caller
    from app.execution.inference_graph import INFERENCE_INSTRUCTION
    from app.worker.inference import make_inference_request_factory
    from tests.inference.test_pydantic_ai_adapter import _manifest

    captured: dict[str, Any] = {}

    class _CapturingPort:
        async def infer(self, request: object, *, result_type: type) -> object:
            captured["request"] = request
            from app.contracts.canonical import (
                CanonicalInferenceRequest,
                CanonicalInferenceResult,
                ModelUsage,
                StructuredInferenceInput,
                StructuredInferenceOutput,
                derive_contract_meta,
            )

            canonical = cast("CanonicalInferenceRequest[StructuredInferenceInput]", request)
            del result_type
            return CanonicalInferenceResult[StructuredInferenceOutput](
                meta=derive_contract_meta(
                    canonical.meta, contract_name="canonical.inference.result", causation_id=canonical.meta.message_id
                ),
                inference_request_id=canonical.inference_request_id,
                result=StructuredInferenceOutput(answer="ok"),
                model_ref="model@2026",
                usage=ModelUsage(input_tokens=1, output_tokens=1, cost_micros=1),
                provider_attempts=1,
                schema_retries=0,
                provider_response_ref=None,
            )

    caller = make_asset_risk_infer_caller(cast(Any, _CapturingPort()), make_inference_request_factory(_manifest()))
    answer = await caller("tenant-a", uuid4(), '{"assets": []}')
    assert answer == {"answer": "ok"}
    request = captured["request"]
    contents = [item.content for item in request.instructions]
    assert ASSET_RISK_INSTRUCTION in contents
    assert INFERENCE_INSTRUCTION not in contents
    assert "G2_OK" not in "".join(contents)
