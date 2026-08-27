"""WS-6 6.F.3: UI projection consistency under reconnect, reorder, duplicates.

G3 evidence: the interaction view (including typed domain-view milestones and
their rendered output) is byte-identical across an uninterrupted replay, a
reordered/duplicated delivery, and a reconnect-with-backfill delivery; while
reconnecting, nothing past the gap is applied and the view stays partial.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID, uuid4

import pytest
from app.asset_risk.rendering import ASSET_STATE_VIEW_SCHEMA_REF, asset_risk_renderer_registry
from app.contracts.canonical import (
    CanonicalModel,
    ContractMeta,
    DomainViewAccepted,
    MessageStarted,
    ProjectionSourceRef,
    RunStatusChanged,
    UIProjectionEvent,
)
from app.observation.facts import (
    UI_DOMAIN_VIEW_SCHEMA_REF,
    UI_MESSAGE_STARTED_SCHEMA_REF,
    UI_PROJECTION_SCHEMA_REF,
)
from app.observation.interaction_model import (
    RunIntentDispatchResult,
    RunInteractionModel,
    SnapshotBundle,
)
from app.observation.reducer import reduce_run_view

RUN_ID = UUID(int=1)
NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
RESULT_HASH = "c" * 64
TOOL_REQUEST_ID = UUID(int=77)

type AnyProjectionEvent = UIProjectionEvent[CanonicalModel]


def _any(event: UIProjectionEvent[Any]) -> AnyProjectionEvent:
    return cast(AnyProjectionEvent, event)


def _meta() -> ContractMeta:
    return ContractMeta(
        contract_name="ui.projection",
        contract_version="v1",
        message_id=uuid4(),
        tenant_id="tenant-a",
        correlation_id="corr-1",
    )


def _source_ref(seq: int) -> ProjectionSourceRef:
    return ProjectionSourceRef(
        source_kind="runtime_event",
        source_ref=f"runtime-event:{RUN_ID}:{seq}",
        source_hash="0" * 64,
        source_seq=seq,
        source_schema_ref="grove.runtime.run-lifecycle.v1",
    )


def status_event(seq: int, status: Literal["running", "succeeded"] = "running") -> AnyProjectionEvent:
    return UIProjectionEvent(
        meta=_meta(),
        event_id=uuid4(),
        target_kind="run",
        target_ref=RUN_ID,
        projection_seq=seq,
        payload_schema_ref=UI_PROJECTION_SCHEMA_REF,
        payload=RunStatusChanged(kind="run_status_changed", run_id=RUN_ID, status=status, run_revision=seq),
        source_refs=(_source_ref(seq),),
        projected_at=NOW,
    )


def message_event(seq: int) -> AnyProjectionEvent:
    return UIProjectionEvent(
        meta=_meta(),
        event_id=uuid4(),
        target_kind="run",
        target_ref=RUN_ID,
        projection_seq=seq,
        payload_schema_ref=UI_MESSAGE_STARTED_SCHEMA_REF,
        payload=MessageStarted(
            kind="message_started",
            message_id=uuid4(),
            owner_run_id=RUN_ID,
            role="assistant",
            content_schema_ref="grove.content.text.v1",
        ),
        source_refs=(_source_ref(seq),),
        projected_at=NOW,
    )


def domain_view_event(seq: int) -> AnyProjectionEvent:
    """The same accepted milestone redelivered at every seq (new event ids)."""

    return UIProjectionEvent(
        meta=_meta(),
        event_id=uuid4(),
        target_kind="run",
        target_ref=RUN_ID,
        projection_seq=seq,
        payload_schema_ref=UI_DOMAIN_VIEW_SCHEMA_REF,
        payload=DomainViewAccepted(
            kind="domain_view_accepted",
            run_id=RUN_ID,
            tool_request_id=TOOL_REQUEST_ID,
            view_schema_ref=ASSET_STATE_VIEW_SCHEMA_REF,
            observed_at=NOW,
            source_ref="asset-state:postgres:rev-42",
            result_hash=RESULT_HASH,
            item_count=3,
        ),
        source_refs=(_source_ref(seq),),
        projected_at=NOW,
    )


class Harness:
    def __init__(self, snapshot_events: list[AnyProjectionEvent]) -> None:
        self.bundle = SnapshotBundle(view=reduce_run_view(snapshot_events), events=tuple(snapshot_events))
        self.batches: list[list[AnyProjectionEvent]] = []
        self.dispatch_result = RunIntentDispatchResult(outcome="accepted")

    def model(self) -> RunInteractionModel:
        return RunInteractionModel(
            snapshot_loader=self._load_snapshot,
            batch_loader=self._load_batch,
            intent_dispatcher=self._dispatch,
        )

    async def _load_snapshot(self) -> SnapshotBundle:
        return self.bundle

    async def _load_batch(self, after_seq: int, limit: int) -> list[AnyProjectionEvent]:
        return self.batches.pop(0) if self.batches else []

    async def _dispatch(self, intent: object) -> RunIntentDispatchResult:
        return self.dispatch_result


@pytest.mark.asyncio
async def test_reconnect_backfill_converges_to_the_uninterrupted_replay() -> None:
    e1, e2, e3, e4, e5 = (
        status_event(1),
        message_event(2),
        domain_view_event(3),
        domain_view_event(4),
        status_event(5, "succeeded"),
    )
    harness = Harness([copy.deepcopy(e1), copy.deepcopy(e2)])
    model = harness.model()
    await model.open()
    snapshot = await model.apply_event(e5)
    # While reconnecting nothing past the gap is applied and the view is partial.
    assert snapshot.reconnecting is True
    assert snapshot.cursor == 2
    assert snapshot.view.domain_views == ()
    assert snapshot.view.completeness == "partial"

    harness.batches = [[copy.deepcopy(e3), copy.deepcopy(e4)]]
    snapshot = await model.apply_event(e5)
    assert snapshot.reconnecting is False
    assert snapshot.cursor == 5
    expected = reduce_run_view([e1, e2, e3, e4, e5])
    assert snapshot.view.model_dump_json() == expected.model_dump_json()
    # The redelivered milestone collapsed to one entry and the run is terminal.
    assert len(snapshot.view.domain_views) == 1
    assert snapshot.view.completeness == "complete"


def test_reordered_and_duplicated_delivery_is_byte_identical() -> None:
    events = [
        status_event(1),
        domain_view_event(2),
        domain_view_event(3),
        message_event(4),
        status_event(5, "succeeded"),
    ]
    direct = reduce_run_view(events)
    shuffled = [copy.deepcopy(event) for event in reversed(events)]
    # A fixed, non-trivial permutation: rotate the reversed stream so neither
    # the original nor the reverse order is preserved.
    rotated = shuffled[2:] + shuffled[:2]
    duplicated = rotated + [copy.deepcopy(rotated[0]), copy.deepcopy(rotated[-1])]
    reordered = reduce_run_view(duplicated)
    assert reordered.model_dump_json() == direct.model_dump_json()
    assert len(direct.domain_views) == 1
    assert direct.completeness == "complete"


def test_renderer_output_is_stable_across_replay_variants() -> None:
    events = [
        status_event(1),
        domain_view_event(2),
        domain_view_event(3),
        status_event(4, "succeeded"),
    ]
    registry = asset_risk_renderer_registry()
    direct = [registry.render(milestone) for milestone in reduce_run_view(events).domain_views]
    replay = reduce_run_view(list(reversed([copy.deepcopy(e) for e in events])))
    assert [registry.render(milestone) for milestone in replay.domain_views] == direct
    import json

    assert len(direct) == 1
    assert json.loads(direct[0].model_dump_json()) == {
        "kind": "rendered",
        "view_schema_ref": ASSET_STATE_VIEW_SCHEMA_REF,
        "title": "资产状态已固定",
        "fields": [
            {"kind": "observed_at", "label": "观测时间", "value": NOW.isoformat()},
            {"kind": "item_count", "label": "记录数", "value": "3"},
            {"kind": "completeness", "label": "完整性", "value": "complete"},
            {
                "kind": "provenance",
                "label": "数据来源",
                "value": f"asset-state:postgres:rev-42 @ {RESULT_HASH[:12]}",
            },
        ],
        "short_result_hash": RESULT_HASH[:12],
    }


@pytest.mark.asyncio
async def test_stream_replay_of_a_milestone_never_duplicates_the_view() -> None:
    e1, e2 = status_event(1), domain_view_event(2)
    harness = Harness([copy.deepcopy(e1), copy.deepcopy(e2)])
    model = harness.model()
    await model.open()
    # A pre-cursor redelivery is ignored by the ordering rule ...
    stale = await model.apply_event(_any(domain_view_event(2)))
    assert stale.cursor == 2
    assert stale.view.applied_event_count == 2
    # ... and a post-cursor redelivery with a fresh event id collapses on
    # tool_request_id + result_hash instead of adding a second milestone.
    redelivered = await model.apply_event(_any(domain_view_event(3)))
    assert redelivered.cursor == 3
    assert redelivered.view.applied_event_count == 3
    assert len(redelivered.view.domain_views) == 1
