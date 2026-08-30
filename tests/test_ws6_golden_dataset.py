"""WS-6 6.F.2: the frozen golden dataset evaluates the closed-loop structure.

Each golden case drives the asset-risk graph end to end (fixture inference
port, frozen knowledge corpus, in-memory read source) and the typed artifacts
are evaluated against the frozen expectations.  LLM answer quality is
deliberately out of scope here -- it belongs to the owner-run human review.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from app.asset_risk.contracts import (
    AssetStateEntry,
    AssetStateQuery,
    AssetStateView,
)
from app.asset_risk.golden import (
    GOLDEN_CASES,
    GOLDEN_DATASET_REF,
    GoldenCase,
    evaluate_typed_report,
    golden_dataset_hash,
)
from app.auth.context import ActiveTenantContext
from app.contracts.canonical import (
    Citation,
    ContractMeta,
    KnowledgeItem,
    KnowledgeRequest,
    KnowledgeResult,
)
from app.knowledge.port import KnowledgeOutcome
from app.worker.inference import make_inference_request_factory
from tests.asset_risk_answer_fixture import GATE_PASSING_FIXTURE_ANSWER
from tests.inference.test_pydantic_ai_adapter import _manifest


def _citation(ref: str) -> Citation:
    return Citation(
        snapshot_ref="knowledge.asset-risk",
        snapshot_version="v1",
        source_version="2026-08",
        locator=f"doc://{ref}",
        content_hash="e" * 64,
    )


class _FrozenKnowledge:
    """Serves the frozen policy corpus: exactly two citable summaries."""

    async def retrieve(self, request: KnowledgeRequest, *, context: ActiveTenantContext) -> KnowledgeOutcome:
        del context

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
                    KnowledgeItem(
                        item_ref="policy.collateral@1",
                        content="Collateral haircuts follow the regulatory schedule.",
                        citations=(_citation("policy.collateral@1"),),
                    ),
                ),
                citations=(
                    _citation("policy.exposure@1"),
                    _citation("policy.collateral@1"),
                ),
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


class _GoldenSource:
    """Serves exactly the requested golden refs from one frozen snapshot."""

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
            observed_at=datetime(2026, 8, 26, tzinfo=UTC),
            source_revision_or_watermark="asset-state:rev-golden",
        )


class _FixturePort:
    async def infer(self, request: object, *, result_type: type) -> object:
        from app.contracts.canonical import (
            CanonicalInferenceRequest,
            CanonicalInferenceResult,
            ModelUsage,
            StructuredInferenceInput,
            StructuredInferenceOutput,
            derive_contract_meta,
        )

        request = cast("CanonicalInferenceRequest[StructuredInferenceInput]", request)
        del result_type
        return CanonicalInferenceResult[StructuredInferenceOutput](
            meta=derive_contract_meta(
                request.meta, contract_name="canonical.inference.result", causation_id=request.meta.message_id
            ),
            inference_request_id=request.inference_request_id,
            result=StructuredInferenceOutput(answer=GATE_PASSING_FIXTURE_ANSWER),
            model_ref="model@2026",
            usage=ModelUsage(input_tokens=1, output_tokens=1, cost_micros=1),
            provider_attempts=1,
            schema_retries=0,
            provider_response_ref=None,
        )


@pytest.mark.asyncio
async def test_every_golden_case_passes_the_structural_evaluation() -> None:
    from app.asset_risk.graph import InferenceAnswer, build_asset_risk_graph
    from app.asset_risk.kernel import make_asset_risk_infer_caller
    from app.asset_risk.read_tool import AssetStateReadCeiling, AssetStateReadTool

    tool = AssetStateReadTool(source=_GoldenSource(), ceiling=AssetStateReadCeiling(manifest_max_asset_refs=16))
    graph = build_asset_risk_graph(
        knowledge_port=_FrozenKnowledge(),
        asset_tool=tool,
        infer=cast(
            "Callable[[str, UUID, str], Awaitable[InferenceAnswer]]",
            make_asset_risk_infer_caller(cast(Any, _FixturePort()), make_inference_request_factory(_manifest())),
        ),
    )
    dataset_hash = golden_dataset_hash()
    for case in GOLDEN_CASES:
        terminal = await graph.ainvoke(
            {
                "stage": "start",
                "tenant_id": "tenant-golden",
                "run_id": str(uuid4()),
                "asset_refs": case.asset_refs,
            }
        )
        assert terminal.get("stage") == "terminal", terminal.get("failure_class")
        result = evaluate_typed_report(case, asset_view=terminal["asset_view"], report=terminal["report"])
        assert result.dataset_hash == dataset_hash
        failed = [check.check for check in result.checks if not check.passed]
        assert not failed, f"{case.case_ref}: failed checks {failed}"


def test_dataset_is_frozen_and_content_addressed() -> None:
    assert GOLDEN_DATASET_REF == "golden.asset-risk-reference@1"
    assert len(GOLDEN_CASES) == 3
    assert re.fullmatch(r"[0-9a-f]{64}", golden_dataset_hash())


def test_tampered_binding_fails_the_evaluation() -> None:
    case = GoldenCase(case_ref="golden.tamper@1", asset_refs=("asset.golden.credit-1",), expected_knowledge_items=2)
    view = {
        "tool_request_id": str(uuid4()),
        "logical_read_key": "key",
        "assets": [
            {
                "asset_ref": "asset.golden.credit-1",
                "asset_class": "credit",
                "exposure_amount": 100,
                "currency": "CNY",
                "status": "active",
            }
        ],
        "observed_at": "2026-08-26T00:00:00Z",
        "source_revision_or_watermark": "asset-state:rev-golden",
    }
    report = {
        "kind": "asset_risk_report.v1",
        "answer": "x",
        "asset_provenance": {
            "source_ref": "asset.state.postgres",
            "result_content_hash": "d" * 64,
        },
        "asset_view_hash": "0" * 64,  # not bound to the view
        "knowledge_items": 1,  # not the frozen corpus size
    }
    result = evaluate_typed_report(case, asset_view=view, report=report)
    failed = {check.check for check in result.checks if not check.passed}
    assert failed == {"view_hash_binding", "knowledge_items"}
    assert not result.passed
