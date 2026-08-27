"""WS-6 graph registry, inference kernel and fixture anti-drift tests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import httpx
import pytest
from app.asset_risk.contracts import AssetStateQuery, AssetStateView
from app.auth.context import ActiveTenantContext
from app.contracts.canonical import (
    CanonicalFailure,
    CanonicalInferenceRequest,
    KnowledgeRequest,
    StructuredInferenceInput,
)
from app.execution.contracts import CONFORMANCE_GRAPH_BINDING, ClaimGraphBinding, ExecutionClaim
from app.execution.graph_registry import GraphResolutionError, resolve_graph_kernel
from app.execution.inference_graph import (
    INFERENCE_GRAPH_BINDING,
    InferenceRequestFactory,
    build_inference_graph,
    compute_inference_input_hash,
)
from app.inference import TypedInferencePort
from app.knowledge.port import KnowledgeOutcome
from app.worker.inference import make_inference_request_factory
from app.worker.loop import RuntimeWorker
from tests.inference.test_pydantic_ai_adapter import _manifest, _port


def _completion_transport(answer: str) -> httpx.MockTransport:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "inference-node",
                "object": "chat.completion",
                "created": 1,
                "model": "model@2026",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": f'{{"answer":"{answer}"}}'},
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
            },
        )

    return httpx.MockTransport(handler)


def _factory() -> InferenceRequestFactory:
    return make_inference_request_factory(_manifest())


def _claim(binding: ClaimGraphBinding, seq: int = 0) -> ExecutionClaim:
    return ExecutionClaim(
        command_id=uuid4(),
        tenant_id="tenant-a",
        run_id=uuid4(),
        command_seq=seq,
        command_digest="a" * 64,
        runtime_build_hash="b" * 64,
        worker_id="test-worker",
        execution_fence=1,
        lease_until=datetime.now(UTC) + timedelta(seconds=30),
        graph_binding=binding,
    )


def test_registry_resolves_conformance_binding_to_deterministic_kernel() -> None:
    kernel = resolve_graph_kernel(CONFORMANCE_GRAPH_BINDING, inference_port=None, inference_request_factory=None)
    assert kernel.kind == "conformance"


def test_registry_fails_closed_on_unknown_graph_binding() -> None:
    unknown = ClaimGraphBinding(
        graph_ref="graph.trojan@9", graph_version="9", graph_state_schema_version="state.trojan@9"
    )
    with pytest.raises(GraphResolutionError) as exc_info:
        resolve_graph_kernel(unknown, inference_port=None, inference_request_factory=None)
    assert exc_info.value.reason == "unknown_graph"


def test_registry_rejects_inference_binding_without_production_port() -> None:
    with pytest.raises(GraphResolutionError) as exc_info:
        resolve_graph_kernel(INFERENCE_GRAPH_BINDING, inference_port=None, inference_request_factory=None)
    assert exc_info.value.reason == "inference_unavailable"


def test_registry_resolves_inference_binding_with_verified_port() -> None:
    port = _port(_completion_transport("done"))
    kernel = resolve_graph_kernel(INFERENCE_GRAPH_BINDING, inference_port=port, inference_request_factory=_factory())
    assert kernel.kind == "inference"


@pytest.mark.asyncio
async def test_inference_graph_executes_one_infer_node_through_the_kernel() -> None:
    port = _port(_completion_transport("done"))
    graph = build_inference_graph(port, _factory())
    run_id = uuid4()
    terminal = await graph.ainvoke(
        {
            "stage": "start",
            "tenant_id": "tenant-a",
            "run_id": str(run_id),
            "input_hash": compute_inference_input_hash("tenant-a", run_id),
        }
    )
    assert terminal["stage"] == "terminal"
    assert terminal["answer"] == "done"
    assert terminal["total_tokens"] == 5
    assert terminal["provider_attempts"] == 1
    assert terminal["schema_retries"] == 0


def test_request_factory_binds_manifest_policies_into_legal_requests() -> None:
    request = _factory()("tenant-a", uuid4())
    manifest = _manifest()
    assert request.model_policy == manifest.model_policy
    assert request.retry_policy == manifest.retry_policy
    assert request.budget == manifest.budget_policy
    assert request.result_schema_ref == manifest.output_schema_ref.ref
    assert request.meta.tenant_id == "tenant-a"


def test_registry_conformance_binding_matches_fixture_release_bundle() -> None:
    from app.releases.fixture import load_fixture_release_bundle

    bundle = load_fixture_release_bundle().graph_binding
    assert (
        bundle.graph.ref,
        bundle.graph.version,
        bundle.graph_state_schema_version,
    ) == (
        CONFORMANCE_GRAPH_BINDING.graph_ref,
        CONFORMANCE_GRAPH_BINDING.graph_version,
        CONFORMANCE_GRAPH_BINDING.graph_state_schema_version,
    )


@pytest.mark.asyncio
async def test_worker_dispatches_inference_claim_to_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    claim = _claim(INFERENCE_GRAPH_BINDING)
    driver = MagicMock()
    driver.heartbeat = AsyncMock(return_value=claim)
    driver.finish_delivery = AsyncMock(
        return_value=MagicMock(continue_command_id=None, run_revision=1, status="succeeded")
    )
    driver.dead_letter = AsyncMock()
    worker = RuntimeWorker(
        driver=driver,
        tenant_id="tenant-a",
        worker_id="test-worker",
        runtime_build_hash="b" * 64,
        database_url="postgresql://localhost/test",
        inference_port=_port(_completion_transport("G2_OK")),
        inference_request_factory=_factory(),
        poll_interval=0.01,
    )
    monkeypatch.setattr(worker, "_write_checkpoint", AsyncMock())
    await worker._process_claim(claim)
    driver.finish_delivery.assert_called_once()
    assert driver.finish_delivery.call_args.kwargs["outcome_kind"] == "terminal"
    driver.dead_letter.assert_not_called()


@pytest.mark.asyncio
async def test_worker_dead_letters_unknown_graph_binding() -> None:
    unknown = ClaimGraphBinding(
        graph_ref="graph.future@2", graph_version="2", graph_state_schema_version="state.future@2"
    )
    claim = _claim(unknown)
    driver = MagicMock()
    driver.heartbeat = AsyncMock(return_value=claim)
    driver.finish_delivery = AsyncMock()
    driver.dead_letter = AsyncMock()
    worker = RuntimeWorker(
        driver=driver,
        tenant_id="tenant-a",
        worker_id="test-worker",
        runtime_build_hash="b" * 64,
        database_url="postgresql://localhost/test",
        poll_interval=0.01,
    )
    await worker._process_claim(claim)
    driver.dead_letter.assert_called_once()
    assert driver.dead_letter.call_args.kwargs["reason_ref"] == "graph-unknown_graph"
    driver.finish_delivery.assert_not_called()


@pytest.mark.asyncio
async def test_worker_dead_letters_inference_claim_without_port() -> None:
    claim = _claim(INFERENCE_GRAPH_BINDING)
    driver = MagicMock()
    driver.heartbeat = AsyncMock(return_value=claim)
    driver.finish_delivery = AsyncMock()
    driver.dead_letter = AsyncMock()
    worker = RuntimeWorker(
        driver=driver,
        tenant_id="tenant-a",
        worker_id="test-worker",
        runtime_build_hash="b" * 64,
        database_url="postgresql://localhost/test",
        poll_interval=0.01,
    )
    await worker._process_claim(claim)
    driver.dead_letter.assert_called_once()
    assert driver.dead_letter.call_args.kwargs["reason_ref"] == "graph-inference_unavailable"
    driver.finish_delivery.assert_not_called()


def test_claim_graph_binding_defaults_older_claims_to_conformance() -> None:
    claim = ExecutionClaim(
        command_id=uuid4(),
        tenant_id="tenant-a",
        run_id=UUID(int=0),
        command_seq=0,
        command_digest="a" * 64,
        runtime_build_hash="b" * 64,
        worker_id="test-worker",
        execution_fence=1,
        lease_until=datetime.now(UTC) + timedelta(seconds=30),
    )
    assert claim.graph_binding == CONFORMANCE_GRAPH_BINDING


def test_asset_risk_spec_variant_binds_published_evidence() -> None:
    from uuid import uuid4

    from app.auth.context import Principal, PrincipalKind
    from app.schemas.execution import ExecutionConstraints, ExecutionIntent, FixtureInput
    from app.services.execution import _build_fixture_spec

    def intent() -> ExecutionIntent:
        return ExecutionIntent(
            intent_id=uuid4(),
            skill_ref="fixture.skill@1",
            input=FixtureInput(question="hello"),
            constraints=ExecutionConstraints(),
        )

    context = ActiveTenantContext(tenant_id="tenant-a", principal=Principal("user-a", PrincipalKind.HUMAN))
    conformance = _build_fixture_spec(context, intent(), ("execution.query", "execution.submit"))
    asset_risk = _build_fixture_spec(
        context,
        intent(),
        ("execution.query", "execution.submit"),
        graph_binding="asset_risk",
    )
    assert conformance.graph.graph.ref == "graph.fixture@1"
    assert asset_risk.graph.graph.ref == "graph.asset-risk@1"
    assert asset_risk.graph.graph_state_schema_version == "state.asset-risk@1"
    assert asset_risk.skill_spec_hash != conformance.skill_spec_hash


@pytest.mark.asyncio
async def test_worker_dispatches_asset_risk_claim_to_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from app.asset_risk.graph import InferenceAnswer, build_asset_risk_graph
    from app.asset_risk.kernel import AssetRiskKernel, make_asset_risk_infer_caller
    from app.contracts.canonical import KnowledgeResult
    from app.execution.contracts import ClaimGraphBinding

    class _Knowledge:
        async def retrieve(self, request: KnowledgeRequest, *, context: ActiveTenantContext) -> KnowledgeOutcome:
            class _Outcome:
                ok = True
                failure = None
                from app.contracts.canonical import ContractMeta as _Meta

                result = KnowledgeResult(
                    meta=_Meta(
                        contract_name="knowledge.result",
                        contract_version="v1",
                        message_id=request.meta.message_id,
                        tenant_id=request.meta.tenant_id,
                        correlation_id=request.meta.correlation_id,
                    ),
                    knowledge_request_id=request.knowledge_request_id,
                    result_class="empty",
                    items=(),
                    citations=(),
                    knowledge_snapshot_ref="knowledge.asset-risk",
                    knowledge_snapshot_version="v1",
                    knowledge_snapshot_content_hash="a" * 64,
                    applied_acl_ref="acl@1",
                    applied_acl_hash="b" * 64,
                    retrieval_policy_ref="retrieval@1",
                    retrieval_policy_hash="c" * 64,
                    truncated=False,
                )

            return cast("KnowledgeOutcome", _Outcome())

    class _Source:
        async def read(
            self, query: AssetStateQuery, *, tenant_id: str, logical_read_key: str, tool_request_id: UUID
        ) -> AssetStateView | CanonicalFailure:
            from datetime import UTC, datetime

            from app.asset_risk.contracts import AssetStateEntry
            from app.asset_risk.contracts import AssetStateView as _View

            return _View(
                tool_request_id=tool_request_id,
                logical_read_key=logical_read_key,
                assets=tuple(
                    AssetStateEntry(asset_ref=ref, asset_class="credit", exposure_amount=1, currency="CNY")
                    for ref in query.asset_refs
                ),
                observed_at=datetime.now(UTC),
                source_revision_or_watermark="rev-1",
            )

    class _InputSource:
        async def asset_refs(self, tenant_id: str, run_id: UUID) -> tuple[str, ...]:
            return ("asset.a", "asset.b")

    from app.asset_risk.read_tool import AssetStateReadCeiling, AssetStateReadTool

    tool = AssetStateReadTool(source=_Source(), ceiling=AssetStateReadCeiling(manifest_max_asset_refs=8))
    graph = build_asset_risk_graph(
        knowledge_port=_Knowledge(),
        asset_tool=tool,
        infer=cast(
            "Callable[[str, UUID, str], Awaitable[InferenceAnswer]]",
            make_asset_risk_infer_caller(cast(TypedInferencePort, _FakePort()), _factory()),
        ),
    )
    kernel = AssetRiskKernel(graph_factory=lambda: graph, input_source=_InputSource())

    claim = _claim(
        ClaimGraphBinding(
            graph_ref="graph.asset-risk@1", graph_version="1", graph_state_schema_version="state.asset-risk@1"
        )
    )
    driver = MagicMock()
    driver.heartbeat = AsyncMock(return_value=claim)
    driver.finish_delivery = AsyncMock(
        return_value=MagicMock(continue_command_id=None, run_revision=1, status="succeeded")
    )
    driver.dead_letter = AsyncMock()
    worker = RuntimeWorker(
        driver=driver,
        tenant_id="tenant-a",
        worker_id="test-worker",
        runtime_build_hash="b" * 64,
        database_url="postgresql://localhost/test",
        asset_risk_kernel=kernel,
        poll_interval=0.01,
    )
    monkeypatch.setattr(worker, "_write_checkpoint", AsyncMock())
    monkeypatch.setattr(worker, "_load_prior_asset_risk_state", AsyncMock(return_value=None))
    await worker._process_claim(claim)
    driver.finish_delivery.assert_called_once()
    assert driver.finish_delivery.call_args.kwargs["outcome_kind"] == "terminal"
    driver.dead_letter.assert_not_called()
    # The accepted typed read view becomes a domain-view runtime fact in the
    # same terminal transaction (6.F.1 emission side).
    from app.observation.facts import DOMAIN_VIEW_ACCEPTED_SCHEMA_REF

    emitted = driver.finish_delivery.call_args.kwargs["events"]
    domain_view = [event for event in emitted if event.payload_schema_ref == DOMAIN_VIEW_ACCEPTED_SCHEMA_REF]
    assert len(domain_view) == 1
    fact = domain_view[0].payload
    assert fact.run_id == claim.run_id
    assert fact.view_schema_ref == "AssetStateView@1"
    assert fact.item_count == 2
    assert fact.source_ref == "asset.state.postgres"
    assert len(fact.result_hash) == 64  # bound to the accepted view's content hash


class _FakePort:
    async def infer(self, request: object, *, result_type: type) -> object:
        request = cast("CanonicalInferenceRequest[StructuredInferenceInput]", request)
        del result_type
        from app.contracts.canonical import (
            CanonicalInferenceResult,
            ModelUsage,
            StructuredInferenceOutput,
            derive_contract_meta,
        )

        return CanonicalInferenceResult[StructuredInferenceOutput](
            meta=derive_contract_meta(
                request.meta, contract_name="canonical.inference.result", causation_id=request.meta.message_id
            ),
            inference_request_id=request.inference_request_id,
            result=StructuredInferenceOutput(answer="risk ok"),
            model_ref="model@2026",
            usage=ModelUsage(input_tokens=1, output_tokens=1, cost_micros=1),
            provider_attempts=1,
            schema_retries=0,
            provider_response_ref=None,
        )


@pytest.mark.asyncio
async def test_resumed_asset_risk_claim_skips_input_source_and_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """POC-M 4: a checkpointed accepted view resumes without re-reading."""

    from app.asset_risk.graph import InferenceAnswer, build_asset_risk_graph
    from app.asset_risk.kernel import AssetRiskKernel, make_asset_risk_infer_caller
    from app.contracts.canonical import KnowledgeResult

    class _Knowledge:
        async def retrieve(self, request: KnowledgeRequest, *, context: ActiveTenantContext) -> KnowledgeOutcome:
            class _Outcome:
                ok = True
                failure = None
                result = KnowledgeResult(
                    meta=request.meta,
                    knowledge_request_id=request.knowledge_request_id,
                    result_class="empty",
                    items=(),
                    citations=(),
                    knowledge_snapshot_ref="knowledge.asset-risk",
                    knowledge_snapshot_version="v1",
                    knowledge_snapshot_content_hash="a" * 64,
                    applied_acl_ref="acl@1",
                    applied_acl_hash="b" * 64,
                    retrieval_policy_ref="retrieval@1",
                    retrieval_policy_hash="c" * 64,
                    truncated=False,
                )

            return cast("KnowledgeOutcome", _Outcome())

    read_calls: list[int] = []
    input_calls: list[int] = []

    class _CountingSource:
        async def read(
            self, query: AssetStateQuery, *, tenant_id: str, logical_read_key: str, tool_request_id: UUID
        ) -> AssetStateView:
            read_calls.append(len(query.asset_refs))
            from datetime import UTC, datetime

            from app.asset_risk.contracts import AssetStateEntry

            return AssetStateView(
                tool_request_id=tool_request_id,
                logical_read_key=logical_read_key,
                assets=tuple(
                    AssetStateEntry(asset_ref=ref, asset_class="credit", exposure_amount=1, currency="CNY")
                    for ref in query.asset_refs
                ),
                observed_at=datetime.now(UTC),
                source_revision_or_watermark="rev-1",
            )

    class _InputSource:
        async def asset_refs(self, tenant_id: str, run_id: UUID) -> tuple[str, ...]:
            input_calls.append(1)
            return ("asset.a",)

    from app.asset_risk.read_tool import AssetStateReadCeiling, AssetStateReadTool

    tool = AssetStateReadTool(source=_CountingSource(), ceiling=AssetStateReadCeiling(manifest_max_asset_refs=8))
    graph = build_asset_risk_graph(
        knowledge_port=_Knowledge(),
        asset_tool=tool,
        infer=cast(
            "Callable[[str, UUID, str], Awaitable[InferenceAnswer]]",
            make_asset_risk_infer_caller(cast(TypedInferencePort, _FakePort()), _factory()),
        ),
    )
    kernel = AssetRiskKernel(graph_factory=lambda: graph, input_source=_InputSource())

    claim = _claim(
        ClaimGraphBinding(
            graph_ref="graph.asset-risk@1", graph_version="1", graph_state_schema_version="state.asset-risk@1"
        )
    )
    driver = MagicMock()
    driver.heartbeat = AsyncMock(return_value=claim)
    driver.finish_delivery = AsyncMock(
        return_value=MagicMock(continue_command_id=None, run_revision=1, status="succeeded")
    )
    driver.dead_letter = AsyncMock()
    worker = RuntimeWorker(
        driver=driver,
        tenant_id="tenant-a",
        worker_id="test-worker",
        runtime_build_hash="b" * 64,
        database_url="postgresql://localhost/test",
        asset_risk_kernel=kernel,
        poll_interval=0.01,
    )
    monkeypatch.setattr(worker, "_write_checkpoint", AsyncMock())

    resumed_state = {
        "stage": "terminal",
        "tenant_id": "tenant-a",
        "run_id": str(claim.run_id),
        "asset_refs": ("asset.resumed",),
        "asset_view": {
            "tool_request_id": str(uuid4()),
            "logical_read_key": "resumed-key",
            "assets": [
                {
                    "asset_ref": "asset.resumed",
                    "asset_class": "credit",
                    "exposure_amount": 1,
                    "currency": "CNY",
                    "status": "active",
                }
            ],
            "observed_at": "2026-08-26T00:00:00Z",
            "source_revision_or_watermark": "rev-resumed",
        },
        "asset_provenance": {
            "source_ref": "asset.state.postgres",
            "observed_at": "2026-08-26T00:00:00Z",
            "source_revision_or_watermark": "rev-resumed",
            "result_content_hash": "f" * 64,
        },
        "knowledge_summaries": ("s1", "s2"),
        "inference_answer": "resumed answer",
        "report": {
            "kind": "asset_risk_report.v1",
            "answer": "resumed answer",
            "asset_view_hash": "a" * 64,
            "knowledge_items": 2,
        },
    }
    monkeypatch.setattr(worker, "_load_prior_asset_risk_state", AsyncMock(return_value=resumed_state))
    await worker._process_claim(claim)
    driver.finish_delivery.assert_called_once()
    assert driver.finish_delivery.call_args.kwargs["outcome_kind"] == "terminal"
    driver.dead_letter.assert_not_called()
    # Zero database calls on the recovery path: neither the portfolio input
    # source nor the physical asset read ran.
    assert input_calls == []
    assert read_calls == []
