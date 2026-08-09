"""Unit tests for the Observation API service layer (no DB)."""

from __future__ import annotations

from uuid import uuid4

from app.auth.context import ActiveTenantContext, Principal, PrincipalKind
from app.services import observation

RUN_ID = uuid4()


def _context() -> ActiveTenantContext:
    return ActiveTenantContext(
        tenant_id="tenant-a",
        principal=Principal(principal_id="user-1", kind=PrincipalKind.HUMAN),
    )


class TestCompleteness:
    def test_terminal_caught_up_is_complete(self) -> None:
        assert observation._completeness("succeeded", 2, 2, 0) == "complete"

    def test_lag_is_partial(self) -> None:
        assert observation._completeness("running", 2, 1, 0) == "partial"

    def test_unknown_schema_is_partial(self) -> None:
        assert observation._completeness("succeeded", 1, 1, 1) == "partial"

    def test_non_terminal_is_partial(self) -> None:
        assert observation._completeness("running", 0, 0, 0) == "partial"

