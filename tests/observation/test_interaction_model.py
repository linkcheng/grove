"""Golden contract tests for the reference RunInteractionModel (docs/06 §6–7)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from app.contracts.canonical import (
    CanonicalModel,
    ContractMeta,
    MessageStarted,
    ProjectionSourceRef,
    RunStatusChanged,
    UIProjectionEvent,
)
from app.observation.facts import UI_MESSAGE_STARTED_SCHEMA_REF, UI_PROJECTION_SCHEMA_REF
from app.observation.interaction_model import (
    CancelRun,
    InteractionSnapshot,
    RespondToInterrupt,
    RunIntentDispatchResult,
    RunInteractionModel,
    SnapshotBundle,
)
from app.observation.reducer import RunViewState, reduce_run_view

RUN_ID = UUID(int=1)
NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
UNKNOWN_SCHEMA = "grove.ui.unknown.v9"

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


def status_event(seq: int, *, schema_ref: str = UI_PROJECTION_SCHEMA_REF) -> AnyProjectionEvent:
    return UIProjectionEvent(
        meta=_meta(),
        event_id=uuid4(),
        target_kind="run",
        target_ref=RUN_ID,
        projection_seq=seq,
        payload_schema_ref=schema_ref,
        payload=RunStatusChanged(kind="run_status_changed", run_id=RUN_ID, status="running", run_revision=seq),
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


class Harness:
    """Deterministic in-memory transport double with recorded calls."""

    def __init__(self, snapshot_events: list[AnyProjectionEvent]) -> None:
        self.bundle = SnapshotBundle(view=reduce_run_view(snapshot_events), events=tuple(snapshot_events))
        self.batches: list[list[AnyProjectionEvent]] = []
        self.batch_calls: list[tuple[int, int]] = []
        self.unknown: list[str] = []
        self.dispatch_result = RunIntentDispatchResult(outcome="accepted")

    def model(self, *, batch_limit: int = 100) -> RunInteractionModel:
        return RunInteractionModel(
            snapshot_loader=self._load_snapshot,
            batch_loader=self._load_batch,
            intent_dispatcher=self._dispatch,
            unknown_schema_sink=self.unknown.append,
            backfill_batch_limit=batch_limit,
        )

    async def _load_snapshot(self) -> SnapshotBundle:
        return self.bundle

    async def _load_batch(self, after_seq: int, limit: int) -> list[AnyProjectionEvent]:
        self.batch_calls.append((after_seq, limit))
        return self.batches.pop(0) if self.batches else []

    async def _dispatch(self, intent: object) -> RunIntentDispatchResult:
        assert isinstance(intent, RespondToInterrupt | CancelRun)
        return self.dispatch_result


@pytest.mark.asyncio
async def test_open_binds_replay_stable_snapshot_and_cursor() -> None:
    events = [status_event(1), status_event(2)]
    harness = Harness(events)
    model = harness.model()
    snapshot = await model.open()
    assert snapshot.cursor == 2
    assert snapshot.reconnecting is False
    assert snapshot.view.applied_event_count == 2


@pytest.mark.asyncio
async def test_open_rejects_non_replay_stable_snapshot() -> None:
    events = [status_event(1), status_event(2)]
    forged_view = reduce_run_view(events).model_copy(update={"applied_event_count": 9})
    harness = Harness(events)
    harness.bundle = SnapshotBundle(view=forged_view, events=tuple(events))
    with pytest.raises(RuntimeError, match="replay-stable"):
        await harness.model().open()


@pytest.mark.asyncio
async def test_contiguous_event_applies_and_advances_cursor() -> None:
    harness = Harness([status_event(1)])
    model = harness.model()
    await model.open()
    snapshot = await model.apply_event(_any(status_event(2)))
    assert snapshot.cursor == 2
    assert snapshot.view.applied_event_count == 2
    assert snapshot.reconnecting is False


@pytest.mark.asyncio
async def test_duplicate_and_older_sequences_are_ignored() -> None:
    harness = Harness([status_event(1), status_event(2)])
    model = harness.model()
    await model.open()
    snapshot = await model.apply_event(_any(status_event(2)))
    snapshot = await model.apply_event(_any(status_event(1)))
    assert snapshot.cursor == 2
    assert snapshot.view.applied_event_count == 2
    assert harness.batch_calls == []


@pytest.mark.asyncio
async def test_gap_marks_reconnecting_then_backfills_in_order() -> None:
    harness = Harness([status_event(1), status_event(2)])
    model = harness.model()
    await model.open()
    held = status_event(5)
    snapshot = await model.apply_event(held)
    assert snapshot.reconnecting is True
    assert snapshot.cursor == 2
    harness.batches = [[status_event(3)], [status_event(4), held]]
    snapshot = await model.apply_event(held)
    assert snapshot.cursor == 5
    assert snapshot.reconnecting is False
    assert snapshot.view.applied_event_count == 5
    assert harness.batch_calls == [(2, 100), (2, 100), (3, 100)]


@pytest.mark.asyncio
async def test_gap_without_backfill_stays_reconnecting_and_never_applies_past_gap() -> None:
    harness = Harness([status_event(1), status_event(2)])
    model = harness.model()
    await model.open()
    snapshot = await model.apply_event(_any(status_event(6)))
    assert snapshot.reconnecting is True
    assert snapshot.cursor == 2
    later = await model.apply_event(_any(status_event(7)))
    assert later.cursor == 2
    assert later.view.applied_event_count == 2


@pytest.mark.asyncio
async def test_unknown_schema_reports_to_sink_and_marks_partial() -> None:
    harness = Harness([status_event(1)])
    model = harness.model()
    await model.open()
    snapshot = await model.apply_event(_any(status_event(2, schema_ref=UNKNOWN_SCHEMA)))
    assert snapshot.view.completeness == "partial"
    assert snapshot.view.unknown_schema_count == 1
    assert harness.unknown == [UNKNOWN_SCHEMA]


@pytest.mark.asyncio
async def test_backfill_respects_the_bounded_batch_limit() -> None:
    harness = Harness([status_event(1)])
    model = harness.model(batch_limit=2)
    await model.open()
    harness.batches = [[status_event(2), status_event(3)], [status_event(4)]]
    snapshot = await model.apply_event(_any(status_event(4)))
    assert snapshot.cursor == 4
    assert snapshot.reconnecting is False
    assert harness.batch_calls == [(1, 2)]


@pytest.mark.asyncio
async def test_dispatch_normalizes_transport_outcomes_only() -> None:
    harness = Harness([status_event(1)])
    model = harness.model()
    await model.open()
    for outcome in ("accepted", "rejected", "conflict"):
        harness.dispatch_result = RunIntentDispatchResult(outcome=outcome, error_code="E1")
        result = await model.dispatch(CancelRun())
        assert result.outcome == outcome
        assert result.error_code == "E1"


@pytest.mark.asyncio
async def test_stream_replays_are_deduped_against_snapshot_events() -> None:
    events = [status_event(1), message_event(2)]
    harness = Harness(events)
    model = harness.model()
    await model.open()
    for event in events:
        await model.apply_event(event)
    snapshot = await model.apply_event(_any(status_event(3)))
    assert snapshot.cursor == 3
    assert snapshot.view.applied_event_count == 3


@pytest.mark.asyncio
async def test_subscribe_notifies_on_state_changes_until_unsubscribed() -> None:
    harness = Harness([status_event(1)])
    model = harness.model()
    calls: list[int] = []
    unsubscribe = model.subscribe(lambda: calls.append(1))
    await model.open()
    unsubscribe()
    await model.apply_event(_any(status_event(2)))
    assert calls == [1]


@pytest.mark.asyncio
async def test_close_is_idempotent_and_guards_every_entry_point() -> None:
    harness = Harness([status_event(1)])
    model = harness.model()
    await model.open()
    model.close()
    model.close()
    with pytest.raises(RuntimeError, match="closed"):
        await model.apply_event(_any(status_event(2)))
    with pytest.raises(RuntimeError, match="closed"):
        await model.dispatch(CancelRun())
    with pytest.raises(RuntimeError, match="closed"):
        model.subscribe(lambda: None)


@pytest.mark.asyncio
async def test_events_and_intents_require_open_first() -> None:
    harness = Harness([status_event(1)])
    model = harness.model()
    with pytest.raises(RuntimeError, match="open"):
        await model.apply_event(_any(status_event(1)))
    with pytest.raises(RuntimeError, match="open"):
        await model.dispatch(CancelRun())


def test_interaction_snapshot_is_a_closed_presentation_model() -> None:
    with pytest.raises(ValueError):
        InteractionSnapshot.model_validate(
            {
                "view": RunViewState().model_dump(mode="json"),
                "reconnecting": False,
                "cursor": 0,
                "langgraph_state": {"secret": True},
            }
        )
