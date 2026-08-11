"""Unit tests for the Observation API service layer (no DB)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from app.auth.context import ActiveTenantContext, Principal, PrincipalKind
from app.observation.facts import RUN_LIFECYCLE_SCHEMA_REF, UI_PROJECTION_SCHEMA_REF
from app.services import observation
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

RUN_ID = uuid4()


def _context() -> ActiveTenantContext:
    return ActiveTenantContext(
        tenant_id="tenant-a",
        principal=Principal(principal_id="user-1", kind=PrincipalKind.HUMAN),
    )


class _Result:
    def __init__(self, value: Any) -> None:
        self.value = value

    def one_or_none(self) -> Any:
        return self.value

    def scalar_one(self) -> Any:
        return self.value

    def fetchall(self) -> list[Any]:
        return cast(list[Any], self.value)


def _session(*results: Any) -> AsyncSession:
    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=[_Result(result) for result in results])
    return cast(AsyncSession, session)


class TestCompleteness:
    def test_terminal_caught_up_is_complete(self) -> None:
        assert observation._completeness("succeeded", 2, 2, 0) == "complete"

    def test_lag_is_partial(self) -> None:
        assert observation._completeness("running", 2, 1, 0) == "partial"

    def test_unknown_schema_is_partial(self) -> None:
        assert observation._completeness("succeeded", 1, 1, 1) == "partial"

    def test_non_terminal_is_partial(self) -> None:
        assert observation._completeness("running", 0, 0, 0) == "partial"


@pytest.mark.asyncio
async def test_sse_coalescer_only_shares_overlapping_exact_reads() -> None:
    coalescer = observation.SSEBackfillCoalescer()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def loader() -> observation.SSEBackfillResult:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return True, ["row"]

    key: observation.SSEReadKey = ("tenant-a", "user-1", "human", RUN_ID, 4)
    tasks = [asyncio.create_task(coalescer.run(key, loader)) for _ in range(50)]
    await started.wait()
    await asyncio.sleep(0)
    release.set()
    assert await asyncio.gather(*tasks) == [(True, ["row"])] * 50
    assert calls == 1

    assert await coalescer.run(key, loader) == (True, ["row"])
    assert calls == 2


@pytest.mark.asyncio
async def test_sse_coalescer_validates_bound_and_falls_back_at_capacity() -> None:
    with pytest.raises(ValueError, match="positive int"):
        observation.SSEBackfillCoalescer(max_inflight=True)

    coalescer = observation.SSEBackfillCoalescer(max_inflight=1)
    blocker_started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def blocker() -> observation.SSEBackfillResult:
        blocker_started.set()
        await release.wait()
        return True, []

    async def fallback() -> observation.SSEBackfillResult:
        nonlocal calls
        calls += 1
        return True, ["fallback"]

    first_key: observation.SSEReadKey = ("tenant-a", "user-1", "human", RUN_ID, 0)
    second_key: observation.SSEReadKey = ("tenant-a", "user-1", "human", RUN_ID, 1)
    first = asyncio.create_task(coalescer.run(first_key, blocker))
    await blocker_started.wait()
    assert await coalescer.run(second_key, fallback) == (True, ["fallback"])
    assert calls == 1
    release.set()
    await first


@pytest.mark.asyncio
async def test_inspect_returns_unavailable_without_live_authorization() -> None:
    session = _session()
    with patch.object(observation, "_authorize_run", AsyncMock(return_value=False)):
        view = await observation.inspect(session, _context(), RUN_ID)
    assert view.completeness == "unavailable"
    assert view.status is None


@pytest.mark.asyncio
async def test_inspect_builds_complete_view_from_authority_and_projection_counts() -> None:
    session = _session(("succeeded", 3), 7, 5, 2, 2, 0)
    with patch.object(observation, "_authorize_run", AsyncMock(return_value=True)):
        view = await observation.inspect(session, _context(), RUN_ID)
    assert view.status == "succeeded"
    assert view.run_revision == 3
    assert view.last_run_seq == 7
    assert view.last_projection_seq == 5
    assert view.completeness == "complete"


@pytest.mark.asyncio
async def test_inspect_handles_authorized_run_removed_before_read() -> None:
    session = _session(None)
    with patch.object(observation, "_authorize_run", AsyncMock(return_value=True)):
        view = await observation.inspect(session, _context(), RUN_ID)
    assert view.completeness == "unavailable"


@pytest.mark.asyncio
async def test_runtime_and_ui_event_queries_return_typed_cursors() -> None:
    now = datetime.now(UTC)
    runtime_event_id = uuid4()
    ui_event_id = uuid4()
    runtime_session = _session(
        [
            (
                runtime_event_id,
                RUN_ID,
                4,
                "run.lifecycle",
                "runtime_worker",
                "source-4",
                RUN_LIFECYCLE_SCHEMA_REF,
                {"kind": "run_lifecycle"},
                now,
            )
        ]
    )
    ui_session = _session(
        [
            (
                ui_event_id,
                RUN_ID,
                8,
                UI_PROJECTION_SCHEMA_REF,
                {"kind": "run_status_changed"},
                now,
            )
        ]
    )
    with patch.object(observation, "_authorize_run", AsyncMock(return_value=True)):
        runtime_events, runtime_cursor = await observation.list_runtime_events(
            runtime_session, _context(), RUN_ID, 3, 20
        )
        ui_events, ui_cursor = await observation.list_ui_events(ui_session, _context(), RUN_ID, 7, 20)
    assert runtime_events[0].event_id == runtime_event_id
    assert runtime_cursor == 4
    assert ui_events[0].event_id == ui_event_id
    assert ui_cursor == 8


@pytest.mark.asyncio
async def test_event_queries_do_not_advance_cursor_when_unavailable_or_empty() -> None:
    with patch.object(observation, "_authorize_run", AsyncMock(return_value=False)):
        runtime_events, runtime_cursor = await observation.list_runtime_events(_session(), _context(), RUN_ID, 4, 20)
        ui_events, ui_cursor = await observation.list_ui_events(_session(), _context(), RUN_ID, 8, 20)
    assert (runtime_events, runtime_cursor) == ([], 4)
    assert (ui_events, ui_cursor) == ([], 8)

    with patch.object(observation, "_authorize_run", AsyncMock(return_value=True)):
        runtime_events, runtime_cursor = await observation.list_runtime_events(_session([]), _context(), RUN_ID, 4, 20)
        ui_events, ui_cursor = await observation.list_ui_events(_session([]), _context(), RUN_ID, 8, 20)
    assert (runtime_events, runtime_cursor) == ([], 4)
    assert (ui_events, ui_cursor) == ([], 8)


def _projection_row(sequence: int) -> tuple[Any, ...]:
    now = datetime.now(UTC)
    event_id = uuid4()
    return (
        event_id,
        "run",
        RUN_ID,
        sequence,
        "v1",
        "corr-1",
        event_id,
        "trace-1",
        UI_PROJECTION_SCHEMA_REF,
        {"kind": "run_status_changed", "run_id": str(RUN_ID), "status": "succeeded", "run_revision": 1},
        [
            {
                "source_kind": "runtime_event",
                "source_ref": f"runtime-event:{RUN_ID}:{sequence}",
                "source_hash": "0" * 64,
                "source_seq": sequence,
                "source_schema_ref": RUN_LIFECYCLE_SCHEMA_REF,
            }
        ],
        now,
    )


@pytest.mark.asyncio
async def test_snapshot_reduces_projection_and_marks_truncated_stream_partial() -> None:
    rows = [_projection_row(sequence) for sequence in range(1, 1002)]
    with patch.object(observation, "_authorize_run", AsyncMock(return_value=True)):
        state = await observation.snapshot(_session(rows), _context(), RUN_ID)
    assert state.status == "succeeded"
    assert state.last_projection_seq == 1000
    assert state.completeness == "partial"


@pytest.mark.asyncio
async def test_snapshot_is_unavailable_without_live_authorization() -> None:
    with patch.object(observation, "_authorize_run", AsyncMock(return_value=False)):
        state = await observation.snapshot(_session(), _context(), RUN_ID)
    assert state.run_id == RUN_ID
    assert state.completeness == "unavailable"


@pytest.mark.asyncio
async def test_stream_delivers_contiguous_rows_then_stops_on_revocation() -> None:
    now = datetime.now(UTC)
    row = (uuid4(), RUN_ID, 6, UI_PROJECTION_SCHEMA_REF, {"kind": "run_status_changed"}, now)
    loader = AsyncMock(side_effect=[(True, [row]), (False, [])])
    factory = cast(async_sessionmaker[AsyncSession], MagicMock())
    with patch.object(observation, "_load_stream_backfill", loader):
        events = [
            event
            async for event in observation.stream_ui_events(
                factory,
                _context(),
                RUN_ID,
                5,
                coalescer=observation.SSEBackfillCoalescer(),
            )
        ]
    assert [event.projection_seq for event in events] == [6]
    assert loader.await_count == 2


@pytest.mark.asyncio
async def test_stream_pauses_at_durable_gap_without_skipping() -> None:
    now = datetime.now(UTC)
    gap_row = (uuid4(), RUN_ID, 2, UI_PROJECTION_SCHEMA_REF, {"kind": "run_status_changed"}, now)
    loader = AsyncMock(side_effect=[(True, [gap_row]), (False, [])])
    factory = cast(async_sessionmaker[AsyncSession], MagicMock())
    with (
        patch.object(observation, "_load_stream_backfill", loader),
        patch.object(asyncio, "sleep", AsyncMock()),
    ):
        events = [event async for event in observation.stream_ui_events(factory, _context(), RUN_ID, 0)]
    assert events == []
    assert loader.await_count == 2
