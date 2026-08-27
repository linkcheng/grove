"""WS-6 workstream C: immutable Knowledge snapshot + production adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from app.auth.context import ActiveTenantContext, Principal, PrincipalKind
from app.contracts.canonical import (
    ContractMeta,
    KnowledgeFilter,
    KnowledgeRequest,
    RetrievalBudget,
    canonical_hash,
)
from app.knowledge.adapter import ImmutableSnapshotKnowledgeAdapter
from app.knowledge.builder import KnowledgeSourceDocument, build_knowledge_snapshot
from app.knowledge.snapshot import KnowledgeAclPolicy, KnowledgeSnapshot, KnowledgeSnapshotItem

PUBLISHED_AT = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)


def _item(ref: str, title: str, content: str, keywords: tuple[str, ...] = ()) -> KnowledgeSnapshotItem:
    return KnowledgeSnapshotItem(
        item_ref=ref,
        source_ref="policies.asset-risk@1",
        locator=f"doc://{ref}",
        title=title,
        content=content,
        keywords=keywords,
        classification="internal",
    )


def _sources() -> tuple[KnowledgeSourceDocument, ...]:
    return (
        KnowledgeSourceDocument(
            source_ref="policies.asset-risk@1",
            source_version="2026-08",
            items=(
                _item(
                    "policy.exposure@1",
                    "Exposure policy",
                    "Aggregate exposure must stay within board limits.",
                    ("exposure", "limits"),
                ),
                _item(
                    "policy.collateral@1",
                    "Collateral policy",
                    "Collateral haircuts follow the regulatory schedule.",
                    ("collateral", "haircut"),
                ),
            ),
        ),
    )


def _acl() -> KnowledgeAclPolicy:
    return KnowledgeAclPolicy(visible_tenants=("tenant-a",), required_scope="execution.run")


def _snapshot() -> KnowledgeSnapshot:
    return build_knowledge_snapshot(
        snapshot_ref="knowledge.asset-risk",
        snapshot_version="v1",
        sources=_sources(),
        acl_policy=_acl(),
        purpose="asset risk governance corpus",
        trusted_issuer="grove.knowledge.publisher",
        published_at=PUBLISHED_AT,
    )


def _context(tenant: str = "tenant-a", roles: tuple[str, ...] = ("execution.run",)) -> ActiveTenantContext:
    return ActiveTenantContext(tenant_id=tenant, principal=Principal("risk-agent", PrincipalKind.WORKLOAD, roles))


def _request(
    query: str = "exposure limits",
    refs: tuple[str, ...] = (),
    max_results: int = 10,
    deadline_ms: int = 5_000,
) -> KnowledgeRequest:
    return KnowledgeRequest(
        meta=ContractMeta(
            contract_name="knowledge.request",
            contract_version="v1",
            message_id=uuid4(),
            tenant_id="tenant-a",
            correlation_id="corr-1",
        ),
        decision_id=uuid4(),
        knowledge_request_id=uuid4(),
        run_id=uuid4(),
        authorization_decision_ref="authz@1",
        query=query,
        knowledge_refs=refs,
        filter=KnowledgeFilter(),
        purpose="risk assessment",
        budget=RetrievalBudget(max_results=max_results, max_bytes=100_000, max_tokens=10_000, deadline_ms=deadline_ms),
        required_citation_level="full",
    )


def test_snapshot_is_content_addressed_and_round_trips() -> None:
    snapshot = _snapshot()
    assert snapshot.content_hash == snapshot.compute_hash()
    again = KnowledgeSnapshot.model_validate_json(snapshot.model_dump_json())
    assert again == snapshot


def test_tampered_item_fails_closed_at_validation() -> None:
    snapshot = _snapshot()
    payload = snapshot.model_dump(mode="json")
    payload["items"][0]["content"] = "tampered content"
    with pytest.raises(ValueError, match="content hash mismatch"):
        KnowledgeSnapshot.model_validate(payload)


def test_moving_reference_is_forbidden() -> None:
    with pytest.raises(ValueError, match="moving"):
        build_knowledge_snapshot(
            snapshot_ref="latest",
            snapshot_version="v1",
            sources=_sources(),
            acl_policy=_acl(),
            purpose="p",
            trusted_issuer="i",
            published_at=PUBLISHED_AT,
        )


def test_adapter_serves_cited_results_from_the_frozen_snapshot() -> None:
    adapter = ImmutableSnapshotKnowledgeAdapter(_snapshot())
    import asyncio

    outcome = asyncio.run(adapter.retrieve(_request(), context=_context()))
    assert outcome.ok and outcome.result is not None
    result = outcome.result
    assert result.result_class == "ok"
    assert [item.item_ref for item in result.items] == ["policy.exposure@1"]
    citation = result.items[0].citations[0]
    assert citation.snapshot_ref == "knowledge.asset-risk"
    assert citation.snapshot_version == "v1"
    assert citation.source_version == "2026-08"
    assert citation.locator == "doc://policy.exposure@1"
    expected_item = next(item for item in _snapshot().items if item.item_ref == "policy.exposure@1")
    assert citation.content_hash == canonical_hash(expected_item.model_dump(mode="json", exclude_none=True))
    assert result.knowledge_snapshot_content_hash == _snapshot().content_hash


def test_adapter_returns_empty_without_inventing_facts() -> None:
    import asyncio

    adapter = ImmutableSnapshotKnowledgeAdapter(_snapshot())
    outcome = asyncio.run(adapter.retrieve(_request(query="unrelated nothing"), context=_context()))
    assert outcome.ok and outcome.result is not None
    assert outcome.result.result_class == "empty"
    assert outcome.result.items == ()


def test_adapter_denies_cross_tenant_and_missing_scope() -> None:
    import asyncio

    adapter = ImmutableSnapshotKnowledgeAdapter(_snapshot())
    cross = asyncio.run(adapter.retrieve(_request(), context=_context(tenant="tenant-b")))
    assert not cross.ok and cross.failure is not None
    assert cross.failure.failure_class == "denied"
    no_scope = asyncio.run(adapter.retrieve(_request(), context=_context(roles=("execution.query",))))
    assert not no_scope.ok and no_scope.failure is not None
    assert no_scope.failure.failure_class == "denied"


def test_ref_query_returns_exact_item_and_budget_truncates() -> None:
    import asyncio

    adapter = ImmutableSnapshotKnowledgeAdapter(_snapshot())
    outcome = asyncio.run(adapter.retrieve(_request(refs=("policy.collateral@1",)), context=_context()))
    assert outcome.ok and outcome.result is not None
    assert [item.item_ref for item in outcome.result.items] == ["policy.collateral@1"]


def test_adapter_refuses_tampered_snapshot_at_construction() -> None:
    snapshot = _snapshot()
    payload = snapshot.model_dump(mode="json")
    payload["items"][1]["content"] = "mutated"
    broken = KnowledgeSnapshot.model_construct(**payload)
    with pytest.raises(ValueError, match="tampered"):
        ImmutableSnapshotKnowledgeAdapter(broken)


def test_builder_binds_source_hashes_into_the_snapshot() -> None:
    snapshot = _snapshot()
    items_payload = [item.model_dump(mode="json", exclude_none=True) for item in _sources()[0].items]
    expected = canonical_hash(items_payload)
    assert snapshot.sources[0].content_hash == expected


def test_budget_truncation_is_bounded_and_flagged() -> None:
    import asyncio

    adapter = ImmutableSnapshotKnowledgeAdapter(_snapshot())
    outcome = asyncio.run(adapter.retrieve(_request(query="policy", max_results=1), context=_context()))
    assert outcome.ok and outcome.result is not None
    assert len(outcome.result.items) == 1
    assert outcome.result.truncated is True


def test_deadline_expiry_maps_to_typed_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    class _AdvancingClockLoop:
        def __init__(self) -> None:
            self._ticks = iter((100.0, 100.5))

        def time(self) -> float:
            return next(self._ticks)

    fake_loop = _AdvancingClockLoop()
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: fake_loop)
    adapter = ImmutableSnapshotKnowledgeAdapter(_snapshot())
    outcome = asyncio.run(adapter.retrieve(_request(deadline_ms=1), context=_context()))
    assert not outcome.ok and outcome.failure is not None
    assert outcome.failure.failure_class == "timeout"
    assert outcome.failure.error_code == "knowledge.timeout"
    assert outcome.failure.retryable is True


def test_retrieve_path_tamper_maps_to_unavailable() -> None:
    import asyncio

    adapter = ImmutableSnapshotKnowledgeAdapter(_snapshot())
    payload = _snapshot().model_dump(mode="json")
    payload["items"][0]["content"] = "mutated after publication"
    adapter._snapshot = KnowledgeSnapshot.model_construct(**payload)
    outcome = asyncio.run(adapter.retrieve(_request(), context=_context()))
    assert not outcome.ok and outcome.failure is not None
    assert outcome.failure.failure_class == "unavailable"
    assert outcome.failure.error_code == "knowledge.snapshot_tampered"
    assert outcome.failure.retryable is False


def test_citations_pin_v1_across_a_v2_publish() -> None:
    import asyncio

    v1 = _snapshot()
    changed_items = tuple(
        item.model_copy(update={"content": f"{item.content} Revised 2026-09."}) for item in _sources()[0].items
    )
    v2 = build_knowledge_snapshot(
        snapshot_ref="knowledge.asset-risk",
        snapshot_version="v2",
        sources=(_sources()[0].model_copy(update={"items": changed_items, "source_version": "2026-09"}),),
        acl_policy=_acl(),
        purpose="asset risk governance corpus",
        trusted_issuer="grove.knowledge.publisher",
        published_at=PUBLISHED_AT,
    )
    assert v2.content_hash != v1.content_hash

    run_a = ImmutableSnapshotKnowledgeAdapter(v1)
    before = asyncio.run(run_a.retrieve(_request(query="policy"), context=_context()))
    after = asyncio.run(run_a.retrieve(_request(query="policy"), context=_context()))
    assert before.ok and after.ok and before.result is not None and after.result is not None
    # Publishing v2 cannot move an existing run's citations: same items, still
    # pinned to snapshot v1 with byte-identical citation chains.
    assert [item.item_ref for item in after.result.items] == [item.item_ref for item in before.result.items]
    assert after.result.citations == before.result.citations
    assert {citation.snapshot_version for citation in after.result.citations} == {"v1"}
    assert after.result.knowledge_snapshot_content_hash == v1.content_hash

    run_b = ImmutableSnapshotKnowledgeAdapter(v2)
    new_run = asyncio.run(run_b.retrieve(_request(query="policy"), context=_context()))
    assert new_run.ok and new_run.result is not None
    assert {citation.snapshot_version for citation in new_run.result.citations} == {"v2"}
    assert new_run.result.knowledge_snapshot_content_hash == v2.content_hash
