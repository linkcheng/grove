"""Golden-fixture and edge-case tests for the headless UI projection reducer."""

from __future__ import annotations

import copy
import random
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from app.contracts.canonical import (
    CanonicalModel,
    ContractMeta,
    InteractionItem,
    InteractionResolved,
    InteractionUpserted,
    MessageCompleted,
    MessageDelta,
    MessageStarted,
    ProjectionSourceRef,
    RunStatusChanged,
    UIProjectionEvent,
)
from app.observation.facts import UI_PROJECTION_SCHEMA_REF, PublicRunStatus
from app.observation.reducer import RunViewState, reduce_run_view

RUN_ID = UUID("22222222-2222-2222-2222-222222222222")
RUN_ID_B = UUID("33333333-3333-3333-3333-333333333333")
NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC)


def _source_ref(run_id: UUID, seq: int = 1) -> ProjectionSourceRef:
    return ProjectionSourceRef(
        source_kind="runtime_event",
        source_ref=f"runtime-event:{run_id}:{seq}",
        source_hash="0" * 64,
        source_seq=seq,
        source_schema_ref="grove.runtime.run-lifecycle.v1",
    )


def _meta(tenant_id: str = "tenant-a", correlation_id: str = "corr-1") -> ContractMeta:
    return ContractMeta(
        contract_name="ui.projection",
        contract_version="v1",
        message_id=uuid4(),
        tenant_id=tenant_id,
        correlation_id=correlation_id,
    )


def run_status_event(
    seq: int,
    status: PublicRunStatus,
    revision: int,
    *,
    tenant_id: str = "tenant-a",
    run_id: UUID = RUN_ID,
    event_id: UUID | None = None,
    payload_schema_ref: str = UI_PROJECTION_SCHEMA_REF,
) -> UIProjectionEvent[RunStatusChanged]:
    return UIProjectionEvent(
        meta=_meta(tenant_id=tenant_id),
        event_id=event_id or uuid4(),
        target_kind="run",
        target_ref=run_id,
        projection_seq=seq,
        payload_schema_ref=payload_schema_ref,
        payload=RunStatusChanged(kind="run_status_changed", run_id=run_id, status=status, run_revision=revision),
        source_refs=(_source_ref(run_id, seq),),
        projected_at=NOW,
    )


def test_empty_stream_is_complete_empty() -> None:
    state = reduce_run_view([])
    assert state == RunViewState()
    assert state.completeness == "complete"


def test_non_terminal_run_is_partial() -> None:
    state = reduce_run_view([run_status_event(1, "running", 1)])
    assert state.status == "running"
    assert state.completeness == "partial"


def test_terminal_run_is_complete() -> None:
    state = reduce_run_view([run_status_event(1, "running", 1), run_status_event(2, "succeeded", 1)])
    assert state.status == "succeeded"
    assert state.completeness == "complete"
    assert state.last_projection_seq == 2


def test_duplicate_events_collapse() -> None:
    eid = uuid4()
    a = run_status_event(1, "running", 1, event_id=eid)
    b = run_status_event(1, "running", 1, event_id=eid)
    state = reduce_run_view([a, b])
    assert state.applied_event_count == 1


def test_disorder_is_sorted_golden_determinism() -> None:
    ordered = [run_status_event(1, "running", 1), run_status_event(2, "succeeded", 1)]
    shuffled = list(reversed([copy.deepcopy(e) for e in ordered]))
    state_ordered = reduce_run_view(ordered)
    state_shuffled = reduce_run_view(shuffled)
    assert state_ordered.model_dump_json() == state_shuffled.model_dump_json()


def test_gap_marks_partial() -> None:
    state = reduce_run_view([run_status_event(1, "running", 1), run_status_event(3, "succeeded", 1)])
    assert state.completeness == "partial"
    # The view freezes at the contiguous watermark (seq 1); the terminal
    # transition at seq 3 is never applied across the gap at seq 2.
    assert state.last_projection_seq == 1
    assert state.status == "running"


def test_gap_freezes_before_terminal() -> None:
    # A gap before the terminal transition must not optimistically complete.
    state = reduce_run_view(
        [
            run_status_event(1, "running", 1),
            run_status_event(2, "succeeded", 1),
            run_status_event(4, "failed", 1),
        ]
    )
    assert state.completeness == "partial"
    assert state.status == "succeeded"
    assert state.last_projection_seq == 2


def test_unknown_schema_counted_and_partial() -> None:
    state = reduce_run_view(
        [
            run_status_event(1, "running", 1),
            run_status_event(2, "succeeded", 1, payload_schema_ref="grove.ui.future.v9"),
        ]
    )
    assert state.unknown_schema_count == 1
    assert state.completeness == "partial"


def test_tenant_switch_resets_state() -> None:
    events = [
        run_status_event(1, "running", 1, tenant_id="tenant-a"),
        run_status_event(2, "succeeded", 1, tenant_id="tenant-b"),
    ]
    state = reduce_run_view(events)
    assert state.tenant_id == "tenant-b"
    assert state.completeness == "partial"


def test_golden_replay_is_byte_identical() -> None:
    rng = random.Random(20260809)  # noqa: S311
    base = [
        run_status_event(1, "running", 1),
        run_status_event(2, "succeeded", 1),
    ]
    run_a = [copy.deepcopy(e) for e in base]
    run_b = [copy.deepcopy(e) for e in base]
    rng.shuffle(run_b)
    assert reduce_run_view(run_a).model_dump_json() == reduce_run_view(run_b).model_dump_json()


def test_target_switch_marks_partial() -> None:
    events = [
        run_status_event(1, "running", 1, run_id=RUN_ID),
        run_status_event(2, "running", 1, run_id=RUN_ID_B),
    ]
    state = reduce_run_view(events)
    assert state.run_id == RUN_ID_B
    assert state.completeness == "partial"


# ---------------------------------------------------------------------------
# Message and interaction projection paths (pure-function coverage).
# ---------------------------------------------------------------------------

MSG_STARTED_SCHEMA = "grove.ui.message-started.v1"
MSG_DELTA_SCHEMA = "grove.ui.message-delta.v1"
MSG_COMPLETED_SCHEMA = "grove.ui.message-completed.v1"
INTERACTION_UPSERTED_SCHEMA = "grove.ui.interaction-upserted.v1"
INTERACTION_RESOLVED_SCHEMA = "grove.ui.interaction-resolved.v1"


class _StubPayload(CanonicalModel):
    """Minimal canonical payload for InteractionItem.safe_payload."""

    value: int = 0


def _message_started_event(
    seq: int, message_id: UUID, *, role: str = "assistant", schema_ref: str = MSG_STARTED_SCHEMA
) -> UIProjectionEvent[MessageStarted]:
    return UIProjectionEvent(
        meta=_meta(),
        event_id=uuid4(),
        target_kind="run",
        target_ref=RUN_ID,
        projection_seq=seq,
        payload_schema_ref=schema_ref,
        payload=MessageStarted(
            kind="message_started",
            message_id=message_id,
            owner_run_id=RUN_ID,
            role=role,  # type: ignore[arg-type]
            content_schema_ref="grove.content.text.v1",
        ),
        source_refs=(_source_ref(RUN_ID, seq),),
        projected_at=NOW,
    )


def _message_delta_event(seq: int, message_id: UUID, delta_seq: int) -> UIProjectionEvent[MessageDelta]:  # noqa: E501
    return UIProjectionEvent(
        meta=_meta(),
        event_id=uuid4(),
        target_kind="run",
        target_ref=RUN_ID,
        projection_seq=seq,
        payload_schema_ref=MSG_DELTA_SCHEMA,
        payload=MessageDelta(kind="message_delta", message_id=message_id, delta_seq=delta_seq, safe_delta="x"),
        source_refs=(_source_ref(RUN_ID, seq),),
        projected_at=NOW,
    )


def _message_completed_event(seq: int, message_id: UUID, last_delta_seq: int) -> UIProjectionEvent[MessageCompleted]:
    return UIProjectionEvent(
        meta=_meta(),
        event_id=uuid4(),
        target_kind="run",
        target_ref=RUN_ID,
        projection_seq=seq,
        payload_schema_ref=MSG_COMPLETED_SCHEMA,
        payload=MessageCompleted(
            kind="message_completed",
            message_id=message_id,
            last_delta_seq=last_delta_seq,
            content_hash="a" * 64,
        ),
        source_refs=(_source_ref(RUN_ID, seq),),
        projected_at=NOW,
    )


def _interaction_upserted_event(
    seq: int, interaction_id: UUID, *, kind: str = "user_input", revision: int = 0
) -> UIProjectionEvent[InteractionUpserted]:
    item = InteractionItem(
        interaction_id=interaction_id,
        tenant_id="tenant-a",
        presentation_run_id=RUN_ID,
        owner_run_id=RUN_ID,
        orchestration_id=RUN_ID,
        kind=kind,  # type: ignore[arg-type]
        source=_source_ref(RUN_ID, seq),
        payload_schema_ref="grove.ui.interaction-payload.v1",
        safe_payload=cast(CanonicalModel, _StubPayload()),
        status="pending",
        revision=revision,
        source_watermarks=(),
        created_at=NOW,
    )
    return UIProjectionEvent(
        meta=_meta(),
        event_id=uuid4(),
        target_kind="run",
        target_ref=RUN_ID,
        projection_seq=seq,
        payload_schema_ref=INTERACTION_UPSERTED_SCHEMA,
        payload=InteractionUpserted(kind="interaction_upserted", interaction=item),
        source_refs=(_source_ref(RUN_ID, seq),),
        projected_at=NOW,
    )


def _interaction_resolved_event(
    seq: int, interaction_id: UUID, item_revision: int, *, status: str = "resolved"
) -> UIProjectionEvent[InteractionResolved]:
    return UIProjectionEvent(
        meta=_meta(),
        event_id=uuid4(),
        target_kind="run",
        target_ref=RUN_ID,
        projection_seq=seq,
        payload_schema_ref=INTERACTION_RESOLVED_SCHEMA,
        payload=InteractionResolved(
            kind="interaction_resolved",
            interaction_id=interaction_id,
            item_revision=item_revision,
            status=status,  # type: ignore[arg-type]
            source=_source_ref(RUN_ID, seq),
        ),
        source_refs=(_source_ref(RUN_ID, seq),),
        projected_at=NOW,
    )


def test_message_lifecycle_accumulates_deltas() -> None:
    mid = uuid4()
    events: list[UIProjectionEvent[Any]] = [
        _message_started_event(1, mid),
        _message_delta_event(2, mid, 0),
        _message_delta_event(3, mid, 1),
        run_status_event(4, "succeeded", 1),
    ]
    state = reduce_run_view(events)
    assert len(state.messages) == 1
    msg = state.messages[0]
    assert msg.message_id == mid
    assert msg.delta_count == 2
    assert msg.last_delta_seq == 1
    assert msg.completed is False
    assert state.completeness == "complete"


def test_message_completed_sets_hash() -> None:
    mid = uuid4()
    events: list[UIProjectionEvent[Any]] = [
        _message_delta_event(1, mid, 0),
        _message_completed_event(2, mid, 3),
    ]
    state = reduce_run_view(events)
    assert len(state.messages) == 1
    assert state.messages[0].completed is True
    assert state.messages[0].content_hash == "a" * 64
    assert state.messages[0].last_delta_seq == 3


def test_stale_delta_ignored() -> None:
    mid = uuid4()
    events = [_message_delta_event(1, mid, 5), _message_delta_event(2, mid, 3)]
    state = reduce_run_view(events)
    assert state.messages[0].delta_count == 1
    assert state.messages[0].last_delta_seq == 5


def test_interaction_upsert_and_resolve() -> None:
    iid = uuid4()
    events: list[UIProjectionEvent[Any]] = [
        _interaction_upserted_event(1, iid, kind="user_input", revision=0),
        _interaction_resolved_event(2, iid, item_revision=1, status="resolved"),
        run_status_event(3, "succeeded", 1),
    ]
    state = reduce_run_view(events)
    assert len(state.interactions) == 1
    item = state.interactions[0]
    assert item.interaction_id == iid
    assert item.status == "resolved"
    assert item.revision == 1
    assert state.completeness == "complete"


def test_interaction_resolve_without_upsert_defaults_kind() -> None:
    iid = uuid4()
    events = [_interaction_resolved_event(1, iid, item_revision=0)]
    state = reduce_run_view(events)
    assert len(state.interactions) == 1
    assert state.interactions[0].kind == "user_input"


def test_interaction_resolve_lower_revision_ignored() -> None:
    iid = uuid4()
    events = [
        _interaction_resolved_event(1, iid, item_revision=2),
        _interaction_resolved_event(2, iid, item_revision=1),
    ]
    state = reduce_run_view(events)
    assert state.interactions[0].revision == 2
