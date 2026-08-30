"""Unit tests for projection schema dispatch and fail-closed handling."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from app.observation.facts import (
    EXECUTION_AUDIT_SCHEMA_REF,
    MESSAGE_COMPLETED_SCHEMA_REF,
    MESSAGE_DELTA_SCHEMA_REF,
    MESSAGE_STARTED_SCHEMA_REF,
    RUN_LIFECYCLE_SCHEMA_REF,
    UI_MESSAGE_DELTA_SCHEMA_REF,
)
from app.observation.projection import ProjectionReconciler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class _Result:
    def __init__(self, value: Any) -> None:
        self.value = value

    def one_or_none(self) -> Any:
        return self.value


def _reconciler() -> ProjectionReconciler:
    return ProjectionReconciler(cast(async_sessionmaker[AsyncSession], MagicMock()))


def _session(event_row: Any) -> AsyncSession:
    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=_Result(event_row))
    return cast(AsyncSession, session)


def _event_row(schema_ref: str) -> tuple[Any, ...]:
    return (
        "run.lifecycle",
        schema_ref,
        {"kind": "run_lifecycle"},
        datetime.now(UTC),
        "corr-1",
        uuid4(),
        "trace-1",
        "source-event-1",
    )


@pytest.mark.asyncio
async def test_missing_runtime_event_relays_orphaned_outbox_row() -> None:
    reconciler = _reconciler()
    relay = AsyncMock()
    with patch.object(reconciler, "_relay", relay):
        observed = await reconciler._apply_outbox_row(
            _session(None),
            tenant_id="tenant-a",
            outbox_id=4,
            run_id=uuid4(),
            event_id=uuid4(),
            run_seq=2,
            source="runtime_worker",
        )
    assert observed is None
    relay.assert_awaited_once()
    assert relay.await_args is not None
    assert relay.await_args.args[1] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("schema_ref", "projects"),
    [(RUN_LIFECYCLE_SCHEMA_REF, True), (EXECUTION_AUDIT_SCHEMA_REF, False)],
)
async def test_known_schema_dispatches_projection_or_audit_only(schema_ref: str, projects: bool) -> None:
    reconciler = _reconciler()
    project = AsyncMock()
    dead_letter = AsyncMock()
    advance = AsyncMock()
    relay = AsyncMock()
    run_id = uuid4()
    event_id = uuid4()
    with (
        patch.object(reconciler, "_project_lifecycle", project),
        patch.object(reconciler, "_dead_letter", dead_letter),
        patch.object(reconciler, "_advance_watermark", advance),
        patch.object(reconciler, "_relay", relay),
    ):
        observed = await reconciler._apply_outbox_row(
            _session(_event_row(schema_ref)),
            tenant_id="tenant-a",
            outbox_id=5,
            run_id=run_id,
            event_id=event_id,
            run_seq=3,
            source="runtime_worker",
        )
    assert observed is not None and observed[1] == schema_ref
    assert project.await_count == int(projects)
    dead_letter.assert_not_awaited()
    advance.assert_awaited_once()
    relay.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknown_schema_is_dead_lettered_before_watermark_advances() -> None:
    reconciler = _reconciler()
    dead_letter = AsyncMock()
    advance = AsyncMock()
    relay = AsyncMock()
    schema_ref = "grove.runtime.future.v9"
    with (
        patch.object(reconciler, "_dead_letter", dead_letter),
        patch.object(reconciler, "_advance_watermark", advance),
        patch.object(reconciler, "_relay", relay),
    ):
        observed = await reconciler._apply_outbox_row(
            _session(_event_row(schema_ref)),
            tenant_id="tenant-a",
            outbox_id=6,
            run_id=uuid4(),
            event_id=uuid4(),
            run_seq=4,
            source="runtime_worker",
        )
    assert observed is not None and observed[1] == schema_ref
    assert dead_letter.await_args is not None
    assert dead_letter.await_args.kwargs["reason"] == f"unknown payload schema ref: {schema_ref}"
    advance.assert_awaited_once()
    relay.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "schema_ref",
    [MESSAGE_STARTED_SCHEMA_REF, MESSAGE_DELTA_SCHEMA_REF, MESSAGE_COMPLETED_SCHEMA_REF],
)
async def test_message_facts_dispatch_to_message_projection(schema_ref: str) -> None:
    reconciler = _reconciler()
    project_message = AsyncMock()
    dead_letter = AsyncMock()
    advance = AsyncMock()
    relay = AsyncMock()
    with (
        patch.object(reconciler, "_project_message", project_message),
        patch.object(reconciler, "_dead_letter", dead_letter),
        patch.object(reconciler, "_advance_watermark", advance),
        patch.object(reconciler, "_relay", relay),
    ):
        observed = await reconciler._apply_outbox_row(
            _session(_event_row(schema_ref)),
            tenant_id="tenant-a",
            outbox_id=6,
            run_id=uuid4(),
            event_id=uuid4(),
            run_seq=4,
            source="runtime_worker",
        )
    assert observed is not None and observed[1] == schema_ref
    project_message.assert_awaited_once()
    dead_letter.assert_not_awaited()


@pytest.mark.asyncio
async def test_message_delta_projects_to_ui_payload_with_content() -> None:
    from app.contracts.canonical import MessageDelta as UiMessageDelta

    reconciler = _reconciler()
    append = AsyncMock()
    advance = AsyncMock()
    run_id = uuid4()
    payload = {
        "kind": "message_delta",
        "run_id": str(run_id),
        "message_id": str(uuid4()),
        "delta_seq": 0,
        "safe_delta": "评估正文",
    }
    with (
        patch.object(reconciler, "_append_ui_projection", append),
        patch.object(reconciler, "_advance_watermark", advance),
    ):
        await reconciler._apply_outbox_row(
            _session(
                ("message.delta", MESSAGE_DELTA_SCHEMA_REF, payload, datetime.now(UTC), "corr-1", uuid4(), None, "se-1")
            ),
            tenant_id="tenant-a",
            outbox_id=7,
            run_id=run_id,
            event_id=uuid4(),
            run_seq=5,
            source="runtime_worker",
        )
    append.assert_awaited_once()
    assert append.await_args is not None
    ui_payload = append.await_args.kwargs["ui_payload"]
    assert isinstance(ui_payload, UiMessageDelta) and ui_payload.safe_delta == "评估正文"
    assert append.await_args.kwargs["ui_schema_ref"] == UI_MESSAGE_DELTA_SCHEMA_REF
