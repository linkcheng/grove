"""Golden-fixture and edge-case tests for the headless UI projection reducer."""

from __future__ import annotations

import copy
import random
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.contracts.canonical import (
    ContractMeta,
    ProjectionSourceRef,
    RunStatusChanged,
    UIProjectionEvent,
)
from app.observation.facts import UI_PROJECTION_SCHEMA_REF
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
    status: str,
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
        payload=RunStatusChanged(
            kind="run_status_changed", run_id=run_id, status=status, run_revision=revision
        ),
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
    state = reduce_run_view(
        [run_status_event(1, "running", 1), run_status_event(2, "succeeded", 1)]
    )
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
    state = reduce_run_view(
        [run_status_event(1, "running", 1), run_status_event(3, "succeeded", 1)]
    )
    assert state.completeness == "partial"
    assert state.last_projection_seq == 3


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
