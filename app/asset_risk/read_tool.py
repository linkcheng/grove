"""The asset.state.read@1 enforcement seam (docs/31 §3/§5).

Everything that can be rejected before a provider or database call is
rejected here: unknown selection fields (closed schema), over-ceiling
selections and selection-policy violations.  The adapter contract returns
either one complete view or one typed failure -- never a subset, never a
count, never an existence leak.
"""

from __future__ import annotations

from typing import Protocol, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.asset_risk.contracts import AssetStateQuery, AssetStateView
from app.contracts.canonical import (
    CanonicalFailure,
    ContractMeta,
    RetryOwner,
    ToolResult,
    ToolResultProvenance,
)

LARGE_RESULT_BYTES = 32_768


class AssetStateReadCeiling(BaseModel):
    """Monotonic input ceilings exactly as pinned by the Manifest.

    The manifest maximum is REQUIRED (no environment-variable fallback);
    deployment/tenant values may only tighten.  A value above its parent
    ceiling is invalid configuration, never silently clamped.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_max_asset_refs: int = Field(ge=1, le=1024)
    deployment_max_asset_refs: int | None = Field(default=None, ge=1, le=1024)
    tenant_max_asset_refs: int | None = Field(default=None, ge=1, le=1024)

    @property
    def effective_max_asset_refs(self) -> int:
        effective = self.manifest_max_asset_refs
        for optional_ceiling in (self.deployment_max_asset_refs, self.tenant_max_asset_refs):
            if optional_ceiling is not None:
                if optional_ceiling > effective:
                    raise ValueError("input ceiling may only tighten, never widen")
                effective = optional_ceiling
        return effective


class AssetStateSource(Protocol):
    """The Profile-owned adapter seam: one transaction, one complete view."""

    async def read(
        self,
        query: AssetStateQuery,
        *,
        tenant_id: str,
        logical_read_key: str,
        tool_request_id: UUID,
    ) -> AssetStateView | CanonicalFailure: ...


class AssetStateReadTool:
    """Enforce the fixed Tool Binding around one adapter call."""

    def __init__(
        self,
        *,
        source: AssetStateSource,
        ceiling: AssetStateReadCeiling,
        result_bytes_limit: int = LARGE_RESULT_BYTES,
    ) -> None:
        if result_bytes_limit < 1:
            raise ValueError("result_bytes_limit must be positive")
        self._source = source
        self._ceiling = ceiling
        self._result_bytes_limit = result_bytes_limit

    async def read(
        self,
        *,
        tenant_id: str,
        run_id: UUID,
        node_id: str,
        query: AssetStateQuery,
        tool_request_id: UUID | None = None,
    ) -> ToolResult[AssetStateView]:
        try:
            effective = self._ceiling.effective_max_asset_refs
        except ValueError:
            # A tampered or smuggled ceiling is invalid configuration: fail
            # closed through the typed contract before any source call.
            return _failed_result(
                tenant_id,
                run_id,
                _failure(
                    "input_contract_invalid",
                    "the pinned input ceiling is invalid",
                    retryable=False,
                ),
            )
        if len(query.asset_refs) > effective:
            return _failed_result(
                tenant_id,
                run_id,
                _failure(
                    "input_contract_invalid",
                    "asset selection exceeds the pinned ceiling",
                    retryable=False,
                ),
            )
        request_id = tool_request_id if tool_request_id is not None else uuid4()
        logical_read_key = f"{TOOL_KEY_PREFIX}:{run_id}:{node_id}:{_stable_refs(query)}"
        outcome = await self._source.read(
            query, tenant_id=tenant_id, logical_read_key=logical_read_key, tool_request_id=request_id
        )
        if isinstance(outcome, CanonicalFailure):
            return _failed_result(tenant_id, run_id, outcome)
        if outcome.asset_refs != frozenset(query.asset_refs):
            # All-or-nothing: a partial delivery is indistinguishable from a
            # total selection failure and leaks no subset or counts.
            return _failed_result(
                tenant_id,
                run_id,
                _failure(
                    "resource_selection_unavailable",
                    "the requested asset selection is unavailable",
                    retryable=False,
                ),
            )
        if len(outcome.model_dump_json()) > self._result_bytes_limit:
            return _failed_result(
                tenant_id,
                run_id,
                _failure(
                    "tool_query_too_broad",
                    "the asset view exceeds the pinned result size budget",
                    retryable=False,
                ),
            )
        provenance = ToolResultProvenance(
            source_ref="asset.state.postgres",
            observed_at=outcome.observed_at,
            source_revision_or_watermark=outcome.source_revision_or_watermark,
            result_content_hash=outcome.result_content_hash(),
        )
        return ToolResult[AssetStateView](
            meta=ContractMeta(
                contract_name="tool.result",
                contract_version="v1",
                message_id=uuid4(),
                tenant_id=tenant_id,
                correlation_id=f"run:{run_id}",
            ),
            tool_request_id=request_id,
            output=outcome,
            artifact_refs=(),
            provenance=provenance,
            failure=None,
        )


TOOL_KEY_PREFIX = "asset.state.read"


def _stable_refs(query: AssetStateQuery) -> str:
    return ",".join(sorted(query.asset_refs))


def _failure(failure_class: str, safe_message: str, *, retryable: bool) -> CanonicalFailure:
    return CanonicalFailure(
        error_code=f"asset_state.{failure_class}",
        failure_class=failure_class,
        retry_owner=cast(RetryOwner, "run_coordination"),
        retryable=retryable,
        safe_message=safe_message,
        detail_ref=None,
    )


def _failed_result(tenant_id: str, run_id: UUID, failure: CanonicalFailure) -> ToolResult[AssetStateView]:
    return ToolResult[AssetStateView](
        meta=ContractMeta(
            contract_name="tool.result",
            contract_version="v1",
            message_id=uuid4(),
            tenant_id=tenant_id,
            correlation_id=f"run:{run_id}",
        ),
        tool_request_id=uuid4(),
        output=None,
        artifact_refs=(),
        provenance=None,
        failure=failure,
    )


__all__ = ["AssetStateReadCeiling", "AssetStateReadTool", "AssetStateSource", "LARGE_RESULT_BYTES"]
