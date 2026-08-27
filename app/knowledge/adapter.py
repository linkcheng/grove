"""Production Knowledge adapter: one immutable snapshot, document-keyword retrieval.

The adapter is deliberately the single MVP adapter shape (docs/30 §2): it
serves only from an already-published, hash-verified snapshot -- never a
moving reference and never the live corpus.  Retrieval is deterministic
keyword/ref matching; RAG/vector/SQL variants belong to Knowledge Expansion.
"""

from __future__ import annotations

import asyncio
from typing import Literal, cast
from uuid import uuid4

from app.auth.context import ActiveTenantContext
from app.contracts.canonical import (
    CanonicalFailure,
    Citation,
    ContractMeta,
    KnowledgeItem,
    KnowledgeRequest,
    KnowledgeResult,
    RetryOwner,
    canonical_hash,
)
from app.knowledge.port import KnowledgeOutcome
from app.knowledge.snapshot import KnowledgeSnapshot, KnowledgeSnapshotItem

_ACL_SCOPE_FAILURE = CanonicalFailure(
    error_code="knowledge.denied",
    failure_class="denied",
    retry_owner=cast(RetryOwner, "execution_kernel"),
    retryable=False,
    safe_message="the active principal is not permitted to read this knowledge snapshot",
)
_TAMPER_FAILURE = CanonicalFailure(
    error_code="knowledge.snapshot_tampered",
    failure_class="unavailable",
    retry_owner=cast(RetryOwner, "run_coordination"),
    retryable=False,
    safe_message="the knowledge snapshot failed its integrity check",
)


class ImmutableSnapshotKnowledgeAdapter:
    """Serve governed knowledge from exactly one frozen snapshot."""

    def __init__(self, snapshot: KnowledgeSnapshot) -> None:
        if snapshot.compute_hash() != snapshot.content_hash:
            raise ValueError("refusing to serve a tampered knowledge snapshot")
        self._snapshot = snapshot
        self._by_ref = {item.item_ref: item for item in snapshot.items}

    async def retrieve(self, request: KnowledgeRequest, *, context: ActiveTenantContext) -> KnowledgeOutcome:
        if self._snapshot.compute_hash() != self._snapshot.content_hash:
            return KnowledgeOutcome(failure=_TAMPER_FAILURE.model_copy(deep=True))
        if (
            context.tenant_id not in self._snapshot.acl_policy.visible_tenants
            or self._snapshot.acl_policy.required_scope not in _context_scopes(context)
        ):
            return KnowledgeOutcome(failure=_ACL_SCOPE_FAILURE.model_copy(deep=True))
        deadline = asyncio.get_running_loop().time() + request.budget.deadline_ms / 1000
        matched = self._match(request)
        served = matched[: request.budget.max_results]
        truncated = len(matched) > len(served)
        citations = tuple(
            Citation(
                snapshot_ref=self._snapshot.snapshot_ref,
                snapshot_version=self._snapshot.snapshot_version,
                source_version=next(
                    source.source_version for source in self._snapshot.sources if source.source_ref == item.source_ref
                ),
                locator=item.locator,
                content_hash=_item_hash(item),
            )
            for item in served
        )
        if asyncio.get_running_loop().time() > deadline:
            timeout = CanonicalFailure(
                error_code="knowledge.timeout",
                failure_class="timeout",
                retry_owner=cast(RetryOwner, "run_coordination"),
                retryable=True,
                safe_message="knowledge retrieval exceeded its fixed deadline",
            )
            return KnowledgeOutcome(failure=timeout)
        result_class: Literal["ok", "empty"] = "ok" if served else "empty"
        result = KnowledgeResult(
            meta=ContractMeta(
                contract_name="knowledge.result",
                contract_version="v1",
                message_id=uuid4(),
                tenant_id=context.tenant_id,
                correlation_id=request.meta.correlation_id,
                causation_id=request.meta.message_id,
            ),
            knowledge_request_id=request.knowledge_request_id,
            result_class=result_class,
            items=tuple(
                KnowledgeItem(
                    item_ref=item.item_ref,
                    content=item.content,
                    citations=(citations[index],),
                )
                for index, item in enumerate(served)
            ),
            citations=citations,
            knowledge_snapshot_ref=self._snapshot.snapshot_ref,
            knowledge_snapshot_version=self._snapshot.snapshot_version,
            knowledge_snapshot_content_hash=self._snapshot.content_hash,
            applied_acl_ref=f"acl.{self._snapshot.snapshot_ref}@{self._snapshot.snapshot_version}",
            applied_acl_hash=_acl_hash(self._snapshot),
            retrieval_policy_ref=self._snapshot.retrieval_build_ref,
            retrieval_policy_hash=_retrieval_hash(self._snapshot),
            truncated=truncated,
        )
        return KnowledgeOutcome(result=result)

    def _match(self, request: KnowledgeRequest) -> list[KnowledgeSnapshotItem]:
        terms = _query_terms(request.query)
        scored: list[tuple[int, KnowledgeSnapshotItem]] = []
        for item in self._snapshot.items:
            if request.knowledge_refs and item.item_ref not in request.knowledge_refs:
                continue
            score = 0
            haystack = f"{item.title} {item.content} {' '.join(item.keywords)}".lower()
            for term in terms:
                if term in haystack:
                    score += 1
            if request.knowledge_refs and item.item_ref in request.knowledge_refs:
                score += len(terms) + 1
            if score > 0:
                scored.append((score, item))
        scored.sort(key=lambda pair: (-pair[0], pair[1].item_ref))
        return [item for _, item in scored]


def _query_terms(query: str) -> tuple[str, ...]:
    return tuple(term for term in query.lower().split() if len(term) >= 2)


def _context_scopes(context: ActiveTenantContext) -> tuple[str, ...]:
    return context.principal.roles


def _item_hash(item: KnowledgeSnapshotItem) -> str:
    return canonical_hash(item.model_dump(mode="json", exclude_none=True))


def _acl_hash(snapshot: KnowledgeSnapshot) -> str:
    return canonical_hash(snapshot.acl_policy.model_dump(mode="json", exclude_none=True))


def _retrieval_hash(snapshot: KnowledgeSnapshot) -> str:
    return canonical_hash({"retrieval_build_ref": snapshot.retrieval_build_ref})


__all__ = ["ImmutableSnapshotKnowledgeAdapter"]
