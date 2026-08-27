"""Protocol-unit coverage for ProjectionReconciler with a scripted session.

The reconciler's real SQL semantics are proven by the WS-4 integration suite
against PostgreSQL.  These unit tests drive the same orchestration protocol
(poll -> validate -> project/dead-letter -> watermark -> relay, shutdown,
rebuild and health) through a scripted session fake so the loop control flow,
schema dispatch and idempotent relay behaviour stay covered in the unit gate.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from app.observation.facts import EXECUTION_AUDIT_SCHEMA_REF, RUN_LIFECYCLE_SCHEMA_REF
from app.observation.projection import ProjectionReconciler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

RUN_ID = uuid4()
CAUSATION_ID = uuid4()
OCCURRED_AT = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)
UNKNOWN_SCHEMA_REF = "grove.runtime.unknown.v9"


def _lifecycle_payload() -> dict[str, Any]:
    return {"kind": "run_lifecycle", "run_id": str(RUN_ID), "status": "running", "run_revision": 1}


def _event_row(schema_ref: str, payload: dict[str, Any]) -> tuple[Any, ...]:
    return (
        "run.status_changed",
        schema_ref,
        payload,
        OCCURRED_AT,
        "corr-1",
        CAUSATION_ID,
        "trace-1",
        "source-event-1",
    )


def _outbox_row(outbox_id: int, event_id: UUID, run_seq: int) -> tuple[Any, ...]:
    return (outbox_id, "tenant-a", RUN_ID, event_id, run_seq, "runtime_outbox")


class FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def fetchall(self) -> list[Any]:
        return list(self._rows)

    def scalar_one(self) -> Any:
        return self._rows[0]

    def one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


class FakeSession:
    """Routes raw SQL to a script and records every executed statement."""

    def __init__(self, script: Callable[[str, dict[str, Any]], FakeResult | None]) -> None:
        self._script = script
        self.executed: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> FakeResult:
        sql = str(statement)
        bound = params or {}
        self.executed.append((sql, bound))
        routed = self._script(sql, bound)
        return FakeResult([]) if routed is None else routed

    def begin(self) -> FakeSession:
        return self

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False


class FakeSessionFactory:
    def __init__(self, session: FakeSession) -> None:
        self._session = session

    def __call__(self) -> FakeSession:
        return self._session


def _reconciler(session: FakeSession) -> ProjectionReconciler:
    factory = cast("async_sessionmaker[AsyncSession]", FakeSessionFactory(session))
    return ProjectionReconciler(factory)


def _statements(session: FakeSession, needle: str) -> list[str]:
    return [sql for sql, _ in session.executed if needle in sql]


def test_run_once_projects_audit_and_dead_letters_unknown_schemas() -> None:
    batched: list[list[tuple[Any, ...]]] = [
        [
            _outbox_row(1, uuid4(), 1),
            _outbox_row(2, uuid4(), 2),
            _outbox_row(3, uuid4(), 3),
        ]
    ]
    events: dict[UUID, tuple[Any, ...]] = {
        batched[0][0][3]: _event_row(RUN_LIFECYCLE_SCHEMA_REF, _lifecycle_payload()),
        batched[0][1][3]: _event_row(EXECUTION_AUDIT_SCHEMA_REF, {"kind": "execution_audit"}),
        batched[0][2][3]: _event_row(UNKNOWN_SCHEMA_REF, {"kind": "future"}),
    }

    def script(sql: str, params: dict[str, Any]) -> FakeResult | None:
        if "grove_fetch_observation_batch" in sql:
            return FakeResult(batched.pop(0) if batched else [])
        if "FROM runtime_event WHERE event_id" in sql:
            return FakeResult([events[params["eid"]]])
        if "COALESCE(MAX(projection_seq)" in sql:
            return FakeResult([1])
        return None

    session = FakeSession(script)
    applied = asyncio.run(_reconciler(session).run_once())
    assert applied == 3
    assert len(_statements(session, "INSERT INTO ui_projection_event")) == 1
    dead_letter_statements = [(sql, params) for sql, params in session.executed if "runtime_event_dead_letter" in sql]
    assert len(dead_letter_statements) == 1
    assert dead_letter_statements[0][1]["reason"] == f"unknown payload schema ref: {UNKNOWN_SCHEMA_REF}"
    assert len(_statements(session, "INSERT INTO projection_watermark")) == 3
    assert len(_statements(session, "UPDATE runtime_event_outbox")) == 3


def test_missing_runtime_event_relays_without_watermark_advance() -> None:
    batched: list[list[tuple[Any, ...]]] = [[_outbox_row(7, uuid4(), 4)]]

    def script(sql: str, params: dict[str, Any]) -> FakeResult | None:
        if "grove_fetch_observation_batch" in sql:
            return FakeResult(batched.pop(0) if batched else [])
        if "FROM runtime_event WHERE event_id" in sql:
            return FakeResult([])
        return None

    session = FakeSession(script)
    applied = asyncio.run(_reconciler(session).run_once())
    assert applied == 1
    assert len(_statements(session, "UPDATE runtime_event_outbox")) == 1
    assert _statements(session, "INSERT INTO projection_watermark") == []
    assert _statements(session, "INSERT INTO ui_projection_event") == []


@pytest.mark.asyncio
async def test_run_loop_stops_after_processed_batch_on_shutdown() -> None:
    batched: list[list[tuple[Any, ...]]] = [[_outbox_row(11, uuid4(), 5)]]

    def script(sql: str, params: dict[str, Any]) -> FakeResult | None:
        if "grove_fetch_observation_batch" in sql:
            rows = batched.pop(0) if batched else []
            reconciler.request_shutdown()
            return FakeResult(rows)
        if "FROM runtime_event WHERE event_id" in sql:
            return FakeResult([_event_row(RUN_LIFECYCLE_SCHEMA_REF, _lifecycle_payload())])
        if "COALESCE(MAX(projection_seq)" in sql:
            return FakeResult([1])
        return None

    session = FakeSession(script)
    reconciler = _reconciler(session)
    await asyncio.wait_for(reconciler.run(), timeout=2.0)
    assert len(_statements(session, "INSERT INTO ui_projection_event")) == 1


@pytest.mark.asyncio
async def test_run_loop_survives_iteration_errors_until_shutdown() -> None:
    calls: list[int] = []

    def script(sql: str, _: dict[str, Any]) -> FakeResult | None:
        if "grove_fetch_observation_batch" in sql:
            calls.append(1)
            raise RuntimeError("database unavailable")
        return None

    session = FakeSession(script)
    reconciler = _reconciler(session)
    task = asyncio.create_task(reconciler.run())
    await asyncio.sleep(0.05)
    reconciler.request_shutdown()
    await asyncio.wait_for(task, timeout=2.0)
    assert len(calls) >= 1


def test_rebuild_reprojects_every_lifecycle_fact() -> None:
    rebuild_rows = [
        (uuid4(), RUN_ID, 1, _lifecycle_payload(), OCCURRED_AT, "corr-1", CAUSATION_ID, "trace-1"),
        (uuid4(), RUN_ID, 2, _lifecycle_payload(), OCCURRED_AT, "corr-1", CAUSATION_ID, "trace-1"),
    ]

    def script(sql: str, _: dict[str, Any]) -> FakeResult | None:
        if "FROM runtime_event re WHERE" in sql:
            return FakeResult(rebuild_rows)
        if "COALESCE(MAX(projection_seq)" in sql:
            return FakeResult([1])
        return None

    session = FakeSession(script)
    rebuilt = asyncio.run(_reconciler(session).rebuild("tenant-a"))
    assert rebuilt == 2
    assert len(_statements(session, "DELETE FROM ui_projection_event")) == 1
    assert len(_statements(session, "DELETE FROM projection_watermark")) == 1
    assert len(_statements(session, "INSERT INTO ui_projection_event")) == 2


def test_health_reports_backlog_and_unknown_schema_counts() -> None:
    def script(sql: str, _: dict[str, Any]) -> FakeResult | None:
        if "grove_observation_health" in sql:
            return FakeResult([(4, 2)])
        return None

    health = asyncio.run(_reconciler(FakeSession(script)).health())
    assert health == {"status": "ready", "backlog": 4, "unknown_schema": 2}


def test_health_defaults_when_helper_returns_no_row() -> None:
    def script(_: str, __: dict[str, Any]) -> FakeResult | None:
        return None

    health = asyncio.run(_reconciler(FakeSession(script)).health())
    assert health == {"status": "ready", "backlog": 0, "unknown_schema": 0}
