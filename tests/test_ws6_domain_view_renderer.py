"""WS-6 6.E.3 golden tests: domain-view milestones and the typed renderer.

Covers the docs/06 §7.2 contract (``domain_view_accepted`` dedup on
``tool_request_id + result_hash``, authoritative ordering, cross-tenant reset)
and the renderer seam (closed registry, unknown ref → ``partial`` marker with
no payload echo, Profile-owned ``AssetStateView@1`` golden output).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from app.asset_risk.rendering import (
    ASSET_STATE_VIEW_SCHEMA_REF,
    AssetStateViewRenderer,
    asset_risk_renderer_registry,
)
from app.contracts.canonical import (
    ContractMeta,
    DomainViewAccepted,
    ProjectionSourceRef,
    RunStatusChanged,
    UIProjectionEvent,
)
from app.observation.facts import UI_DOMAIN_VIEW_SCHEMA_REF, UI_PROJECTION_SCHEMA_REF
from app.observation.reducer import DomainViewMilestone, RunViewState, reduce_run_view
from app.observation.rendering import (
    DomainViewRenderer,
    PartialDomainView,
    RenderedDomainView,
    RenderedField,
    RendererRegistry,
)
from pydantic import ValidationError

RUN_ID = UUID("22222222-2222-2222-2222-222222222222")
RUN_ID_B = UUID("33333333-3333-3333-3333-333333333333")
OBSERVED_AT = datetime(2026, 8, 21, 9, 30, 0, tzinfo=UTC)
RESULT_HASH = "a" * 64
RESULT_HASH_B = "b" * 64


def _meta(tenant_id: str = "tenant-a") -> ContractMeta:
    return ContractMeta(
        contract_name="ui.projection",
        contract_version="v1",
        message_id=uuid4(),
        tenant_id=tenant_id,
        correlation_id="corr-1",
    )


def _source_ref(run_id: UUID, seq: int) -> ProjectionSourceRef:
    return ProjectionSourceRef(
        source_kind="runtime_event",
        source_ref=f"runtime-event:{run_id}:{seq}",
        source_hash="0" * 64,
        source_seq=seq,
        source_schema_ref="grove.runtime.run-lifecycle.v1",
    )


def domain_view_event(
    seq: int,
    *,
    tool_request_id: UUID | None = None,
    result_hash: str = RESULT_HASH,
    view_schema_ref: str = ASSET_STATE_VIEW_SCHEMA_REF,
    item_count: int | None = 3,
    tenant_id: str = "tenant-a",
    run_id: UUID = RUN_ID,
    event_id: UUID | None = None,
) -> UIProjectionEvent[DomainViewAccepted]:
    return UIProjectionEvent(
        meta=_meta(tenant_id=tenant_id),
        event_id=event_id or uuid4(),
        target_kind="run",
        target_ref=run_id,
        projection_seq=seq,
        payload_schema_ref=UI_DOMAIN_VIEW_SCHEMA_REF,
        payload=DomainViewAccepted(
            kind="domain_view_accepted",
            run_id=run_id,
            tool_request_id=tool_request_id or uuid4(),
            view_schema_ref=view_schema_ref,
            observed_at=OBSERVED_AT,
            source_ref="asset-state:postgres:rev-42",
            result_hash=result_hash,
            item_count=item_count,
        ),
        source_refs=(_source_ref(run_id, seq),),
        projected_at=OBSERVED_AT,
    )


def run_status_event(seq: int, *, tenant_id: str = "tenant-a", run_id: UUID = RUN_ID) -> UIProjectionEvent[Any]:
    return UIProjectionEvent(
        meta=_meta(tenant_id=tenant_id),
        event_id=uuid4(),
        target_kind="run",
        target_ref=run_id,
        projection_seq=seq,
        payload_schema_ref=UI_PROJECTION_SCHEMA_REF,
        payload=RunStatusChanged(kind="run_status_changed", run_id=run_id, status="running", run_revision=1),
        source_refs=(_source_ref(run_id, seq),),
        projected_at=OBSERVED_AT,
    )


def test_reducer_accumulates_domain_view_milestone() -> None:
    tool_request_id = uuid4()
    state = reduce_run_view([domain_view_event(1, tool_request_id=tool_request_id)])
    assert len(state.domain_views) == 1
    milestone = state.domain_views[0]
    assert milestone == DomainViewMilestone(
        tool_request_id=tool_request_id,
        view_schema_ref=ASSET_STATE_VIEW_SCHEMA_REF,
        observed_at=OBSERVED_AT,
        source_ref="asset-state:postgres:rev-42",
        result_hash=RESULT_HASH,
        item_count=3,
    )
    assert state.applied_event_count == 1


def test_reducer_dedupes_on_tool_request_and_result_hash() -> None:
    tool_request_id = uuid4()
    duplicate = domain_view_event(2, tool_request_id=tool_request_id)
    state = reduce_run_view(
        [
            domain_view_event(1, tool_request_id=tool_request_id),
            duplicate,
            domain_view_event(3, tool_request_id=tool_request_id, result_hash=RESULT_HASH_B),
            domain_view_event(4, tool_request_id=uuid4()),
        ]
    )
    keys = [(view.tool_request_id, view.result_hash) for view in state.domain_views]
    assert keys == sorted(keys)
    assert len(keys) == 3
    assert (tool_request_id, RESULT_HASH) in keys
    assert (tool_request_id, RESULT_HASH_B) in keys
    assert state.applied_event_count == 4


def test_tenant_switch_clears_domain_views() -> None:
    state = reduce_run_view(
        [
            run_status_event(1),
            domain_view_event(2),
            run_status_event(3, tenant_id="tenant-b", run_id=RUN_ID_B),
            domain_view_event(4, tenant_id="tenant-b", run_id=RUN_ID_B),
        ]
    )
    assert state.tenant_id == "tenant-b"
    assert len(state.domain_views) == 1
    # A tenant switch is a stream integrity boundary: only the new tenant's
    # milestone survives, and the view stays partial by definition.
    assert state.completeness == "partial"


def test_golden_replay_is_byte_identical() -> None:
    import copy

    ordered = [run_status_event(1), domain_view_event(2), domain_view_event(3, result_hash=RESULT_HASH_B)]
    shuffled = list(reversed([copy.deepcopy(event) for event in ordered]))
    assert reduce_run_view(ordered).model_dump_json() == reduce_run_view(shuffled).model_dump_json()


def test_registry_renders_unknown_ref_as_partial_without_payload() -> None:
    registry = asset_risk_renderer_registry()
    milestone = DomainViewMilestone(
        tool_request_id=uuid4(),
        view_schema_ref="OtherDomainView@9",
        observed_at=OBSERVED_AT,
        source_ref="asset-state:postgres:rev-42",
        result_hash=RESULT_HASH,
        item_count=7,
    )
    rendered = registry.render(milestone)
    assert isinstance(rendered, PartialDomainView)
    assert rendered == PartialDomainView(view_schema_ref="OtherDomainView@9")
    dumped: dict[str, Any] = rendered.model_dump()
    assert set(dumped) == {"kind", "view_schema_ref"}


def test_registry_rejects_duplicate_and_wrong_exact_type() -> None:
    with pytest.raises(ValueError, match="duplicate renderer"):
        RendererRegistry((AssetStateViewRenderer(), AssetStateViewRenderer()))
    registry = asset_risk_renderer_registry()
    with pytest.raises(TypeError, match="exact DomainViewMilestone"):
        registry.render(domain_view_event(1).payload)  # type: ignore[arg-type]


class _RogueRenderer(DomainViewRenderer):
    view_schema_ref = "RogueView@1"
    title = "rogue"

    def render(self, milestone: DomainViewMilestone) -> tuple[RenderedField, ...]:
        return tuple(RenderedField(kind="provenance", label=f"row-{index}", value="x" * 8) for index in range(9))


def test_registry_enforces_bounded_field_count() -> None:
    registry = RendererRegistry((_RogueRenderer(),))
    milestone = DomainViewMilestone(
        tool_request_id=uuid4(),
        view_schema_ref="RogueView@1",
        observed_at=OBSERVED_AT,
        source_ref="asset-state:postgres:rev-42",
        result_hash=RESULT_HASH,
        item_count=None,
    )
    with pytest.raises(ValidationError):
        registry.render(milestone)


def test_rendered_domain_view_is_closed_against_extra_fields() -> None:
    with pytest.raises(ValidationError):
        RenderedDomainView(
            view_schema_ref=ASSET_STATE_VIEW_SCHEMA_REF,
            title="资产状态已固定",
            fields=(RenderedField(kind="observed_at", label="观测时间", value="2026-08-21T09:30:00+00:00"),),
            short_result_hash=RESULT_HASH[:12],
            sql="SELECT * FROM asset_state",  # type: ignore[call-arg]
        )


def test_asset_state_renderer_golden_with_item_count() -> None:
    milestone = DomainViewMilestone(
        tool_request_id=uuid4(),
        view_schema_ref=ASSET_STATE_VIEW_SCHEMA_REF,
        observed_at=OBSERVED_AT,
        source_ref="asset-state:postgres:rev-42",
        result_hash=RESULT_HASH,
        item_count=3,
    )
    rendered = asset_risk_renderer_registry().render(milestone)
    assert isinstance(rendered, RenderedDomainView)
    assert rendered.title == "资产状态已固定"
    assert rendered.short_result_hash == RESULT_HASH[:12]
    assert [(field.kind, field.label, field.value) for field in rendered.fields] == [
        ("observed_at", "观测时间", "2026-08-21T09:30:00+00:00"),
        ("item_count", "记录数", "3"),
        ("completeness", "完整性", "complete"),
        ("provenance", "数据来源", f"asset-state:postgres:rev-42 @ {RESULT_HASH[:12]}"),
    ]


def test_asset_state_renderer_omits_count_when_absent() -> None:
    milestone = DomainViewMilestone(
        tool_request_id=uuid4(),
        view_schema_ref=ASSET_STATE_VIEW_SCHEMA_REF,
        observed_at=OBSERVED_AT,
        source_ref="asset-state:postgres:rev-42",
        result_hash=RESULT_HASH,
        item_count=None,
    )
    rendered = asset_risk_renderer_registry().render(milestone)
    assert isinstance(rendered, RenderedDomainView)
    assert [field.kind for field in rendered.fields] == [
        "observed_at",
        "completeness",
        "provenance",
    ]


def test_empty_state_default_has_no_domain_views() -> None:
    assert RunViewState().domain_views == ()
