"""WS-6 C3: the asset.state.read@1 typed read tool seam (docs/31 §3/§5)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest
from app.asset_risk.contracts import AssetStateEntry, AssetStateQuery, AssetStateView
from app.asset_risk.read_tool import AssetStateReadCeiling, AssetStateReadTool
from app.contracts.canonical import CanonicalFailure, RetryOwner
from pydantic import ValidationError

RUN_ID = uuid4()
OBSERVED = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)


def _view(refs: tuple[str, ...], request_id: UUID | None = None) -> AssetStateView:
    return AssetStateView(
        tool_request_id=request_id if request_id is not None else uuid4(),
        logical_read_key="key",
        assets=tuple(
            AssetStateEntry(asset_ref=ref, asset_class="credit", exposure_amount=100, currency="CNY") for ref in refs
        ),
        observed_at=OBSERVED,
        source_revision_or_watermark="asset-state:rev-7",
    )


class ScriptedSource:
    """In-memory adapter double with call recording."""

    def __init__(self, view: AssetStateView | CanonicalFailure | None = None) -> None:
        self.view = view if view is not None else _view(("asset.a",))
        self.calls = 0
        self.seen_tenants: list[str] = []

    async def read(
        self,
        query: AssetStateQuery,
        *,
        tenant_id: str,
        logical_read_key: str,
        tool_request_id: UUID,
    ) -> AssetStateView | CanonicalFailure:
        self.calls += 1
        self.seen_tenants.append(tenant_id)
        if isinstance(self.view, CanonicalFailure):
            return self.view
        assert self.view is not None
        # The real adapter stamps the caller-provided key and request id.
        return self.view.model_copy(update={"logical_read_key": logical_read_key, "tool_request_id": tool_request_id})


def _tool(
    source: ScriptedSource,
    manifest_max: int = 8,
    *,
    deployment_max: int | None = None,
    tenant_max: int | None = None,
) -> AssetStateReadTool:
    return AssetStateReadTool(
        source=source,
        ceiling=AssetStateReadCeiling(
            manifest_max_asset_refs=manifest_max,
            deployment_max_asset_refs=deployment_max,
            tenant_max_asset_refs=tenant_max,
        ),
    )


def test_selection_expansion_fields_fail_before_the_provider() -> None:
    for payload in (
        {"asset_refs": ("asset.a",), "filter": {"class": "credit"}},
        {"asset_refs": ("asset.a",), "search": "x"},
        {"asset_refs": ("asset.a",), "all_assets": True},
        {"asset_refs": ("asset.a",), "limit": 10},
        {"asset_refs": ("asset.a",), "sort": "ref"},
    ):
        with pytest.raises(ValidationError):
            AssetStateQuery.model_validate(payload)
    source = ScriptedSource()
    import asyncio

    asyncio.run(
        _tool(source).read(
            tenant_id="t", run_id=RUN_ID, node_id="read_asset_state", query=AssetStateQuery(asset_refs=("asset.a",))
        )
    )
    assert source.calls == 1  # every invalid payload above was rejected pre-provider; only the valid one arrives


def test_refs_must_match_the_asset_grammar_and_be_unique() -> None:
    with pytest.raises(ValidationError):
        AssetStateQuery(asset_refs=("SELECT * FROM asset",))
    with pytest.raises(ValidationError):
        AssetStateQuery(asset_refs=("asset.a", "asset.a"))


def test_over_ceiling_selection_fails_before_the_provider() -> None:
    source = ScriptedSource()
    import asyncio

    result = asyncio.run(
        _tool(source, manifest_max=2).read(
            tenant_id="t",
            run_id=RUN_ID,
            node_id="n",
            query=AssetStateQuery(asset_refs=("asset.a", "asset.b", "asset.c")),
        )
    )
    assert result.failure is not None
    assert result.failure.failure_class == "input_contract_invalid"
    assert source.calls == 0


def test_ceilings_tighten_monotonically_and_never_widen() -> None:
    assert AssetStateReadCeiling(manifest_max_asset_refs=8).effective_max_asset_refs == 8
    assert AssetStateReadCeiling(manifest_max_asset_refs=8, deployment_max_asset_refs=4).effective_max_asset_refs == 4
    assert (
        AssetStateReadCeiling(
            manifest_max_asset_refs=8, deployment_max_asset_refs=4, tenant_max_asset_refs=2
        ).effective_max_asset_refs
        == 2
    )
    with pytest.raises(ValueError, match="tighten"):
        widening = AssetStateReadCeiling(manifest_max_asset_refs=4, deployment_max_asset_refs=8)
        _ = widening.effective_max_asset_refs


def test_partial_delivery_is_rejected_without_subset_leakage() -> None:
    source = ScriptedSource(view=_view(("asset.a",)))  # asked for a+b, got only a
    import asyncio

    result = asyncio.run(
        _tool(source).read(
            tenant_id="t",
            run_id=RUN_ID,
            node_id="n",
            query=AssetStateQuery(asset_refs=("asset.a", "asset.b")),
        )
    )
    assert result.failure is not None
    assert result.failure.failure_class == "resource_selection_unavailable"
    assert "asset.a" not in result.failure.safe_message
    assert result.output is None and result.provenance is None


def test_complete_view_carries_full_run_data_view_provenance() -> None:
    refs = ("asset.a", "asset.b")
    source = ScriptedSource(view=_view(refs))
    import asyncio

    result = asyncio.run(
        _tool(source).read(
            tenant_id="tenant-a", run_id=RUN_ID, node_id="read_asset_state", query=AssetStateQuery(asset_refs=refs)
        )
    )
    assert result.failure is None
    assert result.output is not None
    assert result.output.asset_refs == frozenset(refs)
    provenance = result.provenance
    assert provenance is not None
    assert provenance.source_ref == "asset.state.postgres"
    assert provenance.observed_at == OBSERVED
    assert provenance.source_revision_or_watermark == "asset-state:rev-7"
    assert provenance.result_content_hash == result.output.result_content_hash()
    assert result.output.logical_read_key.startswith("asset.state.read:")
    assert str(RUN_ID) in result.output.logical_read_key


def test_oversized_view_is_rejected_as_too_broad() -> None:
    refs = tuple(f"asset.bulk.{index}" for index in range(40))
    source = ScriptedSource(view=_view(refs))
    import asyncio

    result = asyncio.run(
        AssetStateReadTool(
            source=source, ceiling=AssetStateReadCeiling(manifest_max_asset_refs=64), result_bytes_limit=1024
        ).read(tenant_id="t", run_id=RUN_ID, node_id="n", query=AssetStateQuery(asset_refs=refs))
    )
    assert result.failure is not None
    assert result.failure.failure_class == "tool_query_too_broad"


def test_typed_adapter_failure_passes_through_unchanged() -> None:
    failure = CanonicalFailure(
        error_code="asset_state.resource_selection_unavailable",
        failure_class="resource_selection_unavailable",
        retry_owner=cast(RetryOwner, "run_coordination"),
        retryable=False,
        safe_message="the requested asset selection is unavailable",
    )
    source = ScriptedSource(view=failure)
    import asyncio

    result = asyncio.run(
        _tool(source).read(tenant_id="t", run_id=RUN_ID, node_id="n", query=AssetStateQuery(asset_refs=("asset.a",)))
    )
    assert result.failure is not None
    assert result.failure.error_code == failure.error_code
    assert result.output is None


def test_view_hash_is_stable_and_request_bound() -> None:
    first = _view(("asset.a",))
    second = _view(("asset.a",))
    assert first.result_content_hash() == second.result_content_hash()
    drifted = _view(("asset.b",))
    assert first.result_content_hash() != drifted.result_content_hash()


@pytest.mark.asyncio
async def test_ceiling_tamper_matrix_rejects_before_any_call() -> None:
    """POC-M 1c: forged limit keys, widened comparators and smuggled
    in-place mutations are all rejected before the source (run/provider)
    is ever called; a counting fake proves the call count stays zero."""

    calls: list[int] = []

    class _CountingSource:
        async def read(
            self, query: AssetStateQuery, *, tenant_id: str, logical_read_key: str, tool_request_id: UUID
        ) -> AssetStateView:
            calls.append(len(query.asset_refs))
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

    # 1) Forged limit key: the closed model rejects unknown ceiling fields.
    with pytest.raises(ValidationError):
        AssetStateReadCeiling.model_validate({"manifest_max_asset_refs": 8, "max_asset_refs_v2": 64})

    # 2) Widened comparator: a deployment value above the manifest pin is
    #    invalid configuration, never silently clamped.
    widened = AssetStateReadCeiling(manifest_max_asset_refs=8, deployment_max_asset_refs=64)
    with pytest.raises(ValueError, match="never widen"):
        _ = widened.effective_max_asset_refs

    # 3) In-place attestation mutation: the frozen model refuses assignment.
    sealed = AssetStateReadCeiling(manifest_max_asset_refs=8)
    with pytest.raises(ValidationError):
        sealed.manifest_max_asset_refs = 64

    # 4) A smuggled (construct-bypassed) widened ceiling still fails the
    #    tool seam before the source is called.
    smuggled = AssetStateReadCeiling.model_construct(manifest_max_asset_refs=2, deployment_max_asset_refs=99)
    tool = AssetStateReadTool(source=_CountingSource(), ceiling=smuggled)
    result = await tool.read(
        tenant_id="tenant-a",
        run_id=UUID(int=1),
        node_id="read_asset_state",
        query=AssetStateQuery(asset_refs=("asset.a", "asset.b", "asset.c")),
    )
    assert result.failure is not None
    assert result.failure.failure_class == "input_contract_invalid"
    assert result.output is None and result.provenance is None
    assert calls == []  # run/provider/database call count is zero
