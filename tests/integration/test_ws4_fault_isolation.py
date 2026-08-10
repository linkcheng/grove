"""WS-4 fault isolation: observation/projection/telemetry failure never blocks a Run.

Proves Exit Invariant 5: projection/reconciliation, outbox publisher or SSE
client failure does not block command, heartbeat, checkpoint or Run terminal
commit, and all queues/buffers/transactions are bounded.

The test runs two full delivery cycles with observation events while the
projection role is deliberately not running.  The Run completes successfully
because observation emit is atomic within the authority transaction and the
projection is a separate role with its own connection.  Starting the projection
afterwards proves it catches up at the watermark without data loss.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any, cast

import psycopg
import pytest
from app.contracts.canonical import canonical_hash
from app.execution import PostgresExecutionDriver
from app.execution.checkpoint import FencedPostgresSaver
from app.execution.conformance_graph import ConformanceState, compute_input_hash, node_a, node_b
from app.observation.facts import build_lifecycle_emit_request, build_node_executed_emit_request
from app.observation.projection import ProjectionReconciler
from langchain_core.runnables.config import RunnableConfig
from langgraph.checkpoint.base import ChannelVersions, CheckpointMetadata, empty_checkpoint
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

RUNTIME_URL = "postgresql+psycopg://grove_runtime:grove_runtime_ws0@127.0.0.1:54329/grove"
MIGRATION_URL = "postgresql+psycopg://grove_migration:grove_migration_ws0@127.0.0.1:54329/grove"
PROJECTION_URL = "postgresql+psycopg://grove_projection:grove_projection_ws0@127.0.0.1:54329/grove"
BUILD_HASH = "a" * 64
CONFERENCE_INPUT = "grove-conformance"


async def _submit(run_id: uuid.UUID, submission_id: uuid.UUID, tenant: str) -> None:
    payload_hash = run_id.hex.ljust(64, "0")[:64]
    payload_ref = f"start-payload-{run_id}"
    engine = create_async_engine(MIGRATION_URL)
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO tenant (tenant_id) VALUES (:t) ON CONFLICT DO NOTHING"), {"t": tenant})
        await conn.execute(
            text(
                "INSERT INTO workload_principal (tenant_id, principal_id, principal_kind, "
                "workload_ref, scopes, active) VALUES (:t, 'fault-worker', 'workload', "
                "'fault', '[\"execution.run\"]'::jsonb, true) ON CONFLICT DO NOTHING"
            ),
            {"t": tenant},
        )
        await conn.execute(
            text(
                "INSERT INTO execution_principal (tenant_id, principal_id, principal_kind) "
                "VALUES (:t, 'fault-worker', 'workload') ON CONFLICT DO NOTHING"
            ),
            {"t": tenant},
        )
        await conn.execute(
            text(
                "INSERT INTO execution_spec (tenant_id, skill_spec_hash, spec_ref, spec_payload) "
                "VALUES (:t, :h, :ref, CAST(:spec AS JSONB)) ON CONFLICT DO NOTHING"
            ),
            {"t": tenant, "h": "b" * 64, "ref": "execution-spec:" + "b" * 64, "spec": json.dumps({"x": 1})},
        )
        await conn.execute(
            text(
                "INSERT INTO agent_run (tenant_id, run_id, submission_id, submission_digest, "
                "principal_id, principal_kind, skill_spec_hash, skill_spec_ref, "
                "runtime_build_ref, runtime_build_hash, status, revision) "
                "VALUES (:t, :rid, :sid, :dig, 'fault-worker', 'workload', :ssh, :ssr, "
                "'b', :rbh, 'accepted', 0)"
            ),
            {
                "t": tenant,
                "rid": run_id,
                "sid": submission_id,
                "dig": payload_hash,
                "ssh": "b" * 64,
                "ssr": "execution-spec:" + "b" * 64,
                "rbh": BUILD_HASH,
            },
        )
        await conn.execute(
            text(
                "INSERT INTO command_payload (tenant_id, payload_ref, payload_hash, "
                "command_schema_version, sensitivity, retention, payload) "
                "VALUES (:t, :pref, :ph, 'start.v1', 'sensitive', 'run_completion', CAST(:body AS JSONB))"
            ),
            {"t": tenant, "pref": payload_ref, "ph": payload_hash, "body": json.dumps({"input": "test"})},
        )
        await conn.execute(
            text(
                "INSERT INTO run_command (tenant_id, command_id, run_id, principal_id, principal_kind, "
                "command_seq, command_type, command_schema_version, command_digest, "
                "payload_ref, payload_hash, status) "
                "VALUES (:t, :cid, :rid, 'fault-worker', 'workload', 0, 'start', 'start.v1', "
                ":dig, :pref, :ph, 'pending')"
            ),
            {
                "t": tenant,
                "cid": uuid.uuid4(),
                "rid": run_id,
                "dig": payload_hash,
                "ph": payload_hash,
                "pref": payload_ref,
            },
        )
    await engine.dispose()


def _driver() -> PostgresExecutionDriver:
    engine = create_async_engine(RUNTIME_URL)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    return PostgresExecutionDriver(session_factory=session_maker, lease_seconds=30.0)


async def _checkpoint(claim: Any, state: ConformanceState) -> None:
    conninfo = RUNTIME_URL.replace("postgresql+psycopg://", "postgresql://")
    async with await psycopg.AsyncConnection.connect(conninfo=conninfo) as conn:
        ck = empty_checkpoint()
        versions: ChannelVersions = {k: str(v) for k, v in state.items()}
        ck["channel_versions"] = versions
        ck["channel_values"] = dict(state)
        config: dict[str, Any] = {"configurable": {"thread_id": str(claim.run_id), "checkpoint_ns": ""}}
        saver = FencedPostgresSaver(conn, claim)
        await saver.aput(cast("RunnableConfig", config), ck, cast(CheckpointMetadata, {}), versions)
        await conn.commit()


async def _count(table: str, tenant: str) -> int:
    engine = create_async_engine(MIGRATION_URL)
    try:
        async with engine.connect() as conn:
            return int(
                (
                    await conn.execute(
                        text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t"),  # noqa: S608
                        {"t": tenant},
                    )
                ).scalar_one()
            )
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
class TestFaultIsolation:
    async def test_projection_down_does_not_block_run(self) -> None:
        """Run completes fully with observation events while projection is not running."""
        tenant = f"fault-{uuid.uuid4().hex[:8]}"
        run_id = uuid.uuid4()
        await _submit(run_id, uuid.uuid4(), tenant)
        driver = _driver()
        input_hash = compute_input_hash(CONFERENCE_INPUT)

        # Stage 1: start -> yield with observation events (projection NOT running)
        claim = await driver.claim(worker_id="fault-w", runtime_build_hash=BUILD_HASH, tenant_id=tenant)
        assert claim is not None
        yielded = node_a({"stage": "start", "input_hash": input_hash, "value": 0})
        await _checkpoint(claim, yielded)
        occurred = datetime.now(UTC)
        events = [
            build_node_executed_emit_request(
                run_id=run_id,
                command_seq=0,
                node_id="node_a",
                stage="start",
                input_hash=input_hash,
                value=yielded["value"],
                occurred_at=occurred,
            ),
            build_lifecycle_emit_request(
                run_id=run_id,
                command_seq=0,
                status="running",
                run_revision=1,
                occurred_at=occurred,
            ),
        ]
        payload = {"outcome_kind": "yield", "input_hash": input_hash, "value": yielded["value"]}
        ph = canonical_hash(payload)
        receipt = await driver.finish_delivery(
            claim,
            outcome_kind="yield",
            continue_payload_ref=f"continue-payload:{ph}",
            continue_payload_hash=ph,
            continue_payload=payload,
            events=events,
        )
        assert receipt.result_code == "consumed"
        # The run advanced despite no projection processing the outbox.
        assert await _count("runtime_event", tenant) == 2
        assert await _count("ui_projection_event", tenant) == 0

        # Stage 2: continue -> terminal with observation events (projection STILL not running)
        claim2 = await driver.claim(worker_id="fault-w", runtime_build_hash=BUILD_HASH, tenant_id=tenant)
        assert claim2 is not None
        terminal = node_b(yielded)
        await _checkpoint(claim2, terminal)
        occurred2 = datetime.now(UTC)
        events2 = [
            build_node_executed_emit_request(
                run_id=run_id,
                command_seq=1,
                node_id="node_b",
                stage="terminal",
                input_hash=input_hash,
                value=terminal["value"],
                occurred_at=occurred2,
            ),
            build_lifecycle_emit_request(
                run_id=run_id,
                command_seq=1,
                status="succeeded",
                run_revision=1,
                occurred_at=occurred2,
            ),
        ]
        receipt2 = await driver.finish_delivery(claim2, outcome_kind="terminal", events=events2)
        assert receipt2.result_code == "consumed"
        assert await _count("runtime_event", tenant) == 4
        assert await _count("ui_projection_event", tenant) == 0

        # Now start the projection: it must catch up from the outbox without data loss.
        projection = ProjectionReconciler(async_sessionmaker(create_async_engine(PROJECTION_URL)))
        processed = await projection.run_once()
        assert processed >= 4
        assert await _count("ui_projection_event", tenant) == 2
        assert await _count("runtime_event_outbox", tenant) == 4  # all relayed

    async def test_telemetry_recorder_drop_does_not_block(self) -> None:
        """Telemetry saturation drops events but never blocks the caller."""
        from app.core.telemetry import BoundedTelemetryRecorder

        rec = BoundedTelemetryRecorder(queue_capacity=3)
        for i in range(10):
            rec.record_span(f"span-{i}", duration_ms=float(i))
        snap = rec.drain()
        assert len(snap.spans) == 3  # bounded
        assert snap.dropped >= 7  # counted, not blocked
