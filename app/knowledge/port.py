"""KnowledgePort: the only retrieval seam the graph may see (docs/30 §2)."""

from __future__ import annotations

from typing import Protocol

from app.auth.context import ActiveTenantContext
from app.contracts.canonical import CanonicalFailure, KnowledgeRequest, KnowledgeResult


class KnowledgeError(Exception):
    """Typed retrieval failure carrying the canonical failure envelope."""

    def __init__(self, failure: CanonicalFailure) -> None:
        super().__init__(failure.safe_message)
        self.failure = failure


class KnowledgeOutcome:
    """``KnowledgeResult | CanonicalFailure`` with a closed surface."""

    __slots__ = ("result", "failure")

    def __init__(self, *, result: KnowledgeResult | None = None, failure: CanonicalFailure | None = None) -> None:
        if (result is None) == (failure is None):
            raise ValueError("exactly one of result or failure is required")
        self.result = result
        self.failure = failure

    @property
    def ok(self) -> bool:
        return self.failure is None


class KnowledgePort(Protocol):
    async def retrieve(self, request: KnowledgeRequest, *, context: ActiveTenantContext) -> KnowledgeOutcome: ...


__all__ = ["KnowledgeError", "KnowledgeOutcome", "KnowledgePort"]
