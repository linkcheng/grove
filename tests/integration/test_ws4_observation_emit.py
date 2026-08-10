"""WS-4 observation emit integration tests against real PostgreSQL.

Proves the core WS-4 invariant: runtime events are emitted atomically with the
authority delivery transaction, run_seq is commit-ordered and monotonic, the
source-event-id dedup is idempotent, and tenant isolation holds via RLS.
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
from app.observation.facts import (
    build_lifecycle_emit_request,
    build_node_executed_emit_request,
)
from langchain_core.runnables.config import RunnableConfig
from langgraph.checkpoint.base import ChannelVersions, CheckpointMetadata, empty_checkpoint
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

RUNTIME_URL = "postgresql+psycopg://grove_runtime:grove_runtime_ws0@127.0.0.1:54329/grove"
API_URL = "postgresql+psycopg://grove_api:grove_api_ws0@127.0.0.1:54329/grove"
MIGRATION_URL = "postgresql+psycopg://grove_migration:grove_migration_ws0@127.0.0.1:54329/grove"
BUILD_HASH = "a" * 64
CONFERENCE_INPUT = "grove-conformance"


async def _submit_run(run_id: uuid.UUID, submission_id: uuid.UUID, tenant: str) -> None:
    payload_hash = run_id.hex.ljust(64, "0")[:64]
    payload_ref = f"start-payload-{run_id}"
    engine = create_async_engine(MIGRATION_URL)
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO tenant (tenant_id) VALUES (:t) ON CONFLICT DO NOTHING"), {"t": tenant})
        await conn.execute(
            text(
                "INSERT INTO workload_principal (tenant_id, principal_id, principal_kind, "
                "workload_ref, scopes, active) "
                "VALUES (:t, 'obs-worker', 'workload', 'obs-test', "
                "'[\"execution.run\"]'::jsonb, true) ON CONFLICT DO NOTHING"
            ),
            {"t": tenant},
        )
        await conn.execute(
            text(
                "INSERT INTO execution_principal (tenant_id, principal_id, principal_kind) "
                "VALUES (:t, 'obs-worker', 'workload') ON CONFLICT DO NOTHING"
            ),
            {"t": tenant},
        )
        spec_payload = {"runtime_build": {"ref": "test-build", "content_hash": BUILD_HASH}}
        await conn.execute(
            text(
                "INSERT INTO execution_spec (tenant_id, skill_spec_hash, spec_ref, spec_payload) "
                "VALUES (:t, :h, :ref, CAST(:payload AS JSONB)) ON CONFLICT DO NOTHING"
            ),
            {"t": tenant, "h": "b" * 64, "ref": "execution-spec:" + "b" * 64, "payload": json.dumps(spec_payload)},
        )
        await conn.execute(
            text(
                "INSERT INTO agent_run (tenant_id, run_id, submission_id, submission_digest, "
                "principal_id, principal_kind, skill_spec_hash, skill_spec_ref, "
                "runtime_build_ref, runtime_build_hash, status, revision) "
                "VALUES (:t, :rid, :sid, :dig, 'obs-worker', 'workload', "
                ":ssh, :ssr, 'test-build', :rbh, 'accepted', 0)"
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
                "VALUES (:t, :pref, :ph, 'start.v1', 'sensitive', 'run_completion', "
                'CAST(\'{"input":"test"}\' AS JSONB))'
            ),
            {"t": tenant, "pref": payload_ref, "ph": payload_hash},
        )
        await conn.execute(
            text(
                "INSERT INTO run_command (tenant_id, command_id, run_id, principal_id, principal_kind, "
                "command_seq, command_type, command_schema_version, command_digest, "
                "payload_ref, payload_hash, status) "
                "VALUES (:t, :cid, :rid, 'obs-worker', 'workload', "
                "0, 'start', 'start.v1', :dig, :pref, :ph, 'pending')"
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


def _make_driver() -> PostgresExecutionDriver:
    engine = create_async_engine(RUNTIME_URL)
    return PostgresExecutionDriver(
        session_factory=async_sessionmaker(engine, expire_on_commit=False), lease_seconds=30.0
    )


async def _write_checkpoint(claim: Any, state: ConformanceState) -> None:
    conninfo = RUNTIME_URL.replace("postgresql+psycopg://", "postgresql://")
    async with await psycopg.AsyncConnection.connect(conninfo=conninfo) as conn:
        checkpoint = empty_checkpoint()
        versions: ChannelVersions = {k: str(v) for k, v in state.items()}
        checkpoint["channel_versions"] = versions
        checkpoint["channel_values"] = dict(state)
        config: dict[str, Any] = {"configurable": {"thread_id": str(claim.run_id), "checkpoint_ns": ""}}
        saver = FencedPostgresSaver(conn, claim)
        await saver.aput(cast("RunnableConfig", config), checkpoint, cast(CheckpointMetadata, {}), versions)
        await conn.commit()


async def _runtime_events(run_id: uuid.UUID) -> list[dict[str, Any]]:
    engine = create_async_engine(MIGRATION_URL)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT run_seq, event_type, source_event_id, payload_schema_ref, payload "
                    "FROM runtime_event WHERE run_id = :r ORDER BY run_seq"
                ),
                {"r": run_id},
            )
            return [dict(row._mapping) for row in result.fetchall()]
    finally:
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
class TestObservationEmit:
    async def test_emit_atomic_monotonic_and_idempotent(self) -> None:
        tenant = f"obs-emit-{uuid.uuid4().hex[:8]}"
        run_id = uuid.uuid4()
        await _submit_run(run_id, uuid.uuid4(), tenant)
        driver = _make_driver()

        claim = await driver.claim(worker_id="obs-worker", runtime_build_hash=BUILD_HASH, tenant_id=tenant)
        assert claim is not None
        input_hash = compute_input_hash(CONFERENCE_INPUT)
        yielded = node_a({"stage": "start", "input_hash": input_hash, "value": 0})
        await _write_checkpoint(claim, yielded)
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
        payload_hash = canonical_hash(payload)
        receipt = await driver.finish_delivery(
            claim,
            outcome_kind="yield",
            continue_payload_ref=f"continue-payload:{payload_hash}",
            continue_payload_hash=payload_hash,
            continue_payload=payload,
            events=events,
        )
        assert receipt.result_code == "consumed"

        rows = await _runtime_events(run_id)
        assert [r["run_seq"] for r in rows] == [1, 2]

        claim2 = await driver.claim(worker_id="obs-worker", runtime_build_hash=BUILD_HASH, tenant_id=tenant)
        assert claim2 is not None
        terminal = node_b(yielded)
        await _write_checkpoint(claim2, terminal)
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

        rows = await _runtime_events(run_id)
        assert [r["run_seq"] for r in rows] == [1, 2, 3, 4]
        assert [r["event_type"] for r in rows] == ["node.executed", "run.lifecycle", "node.executed", "run.lifecycle"]

    async def test_emit_is_idempotent_on_duplicate_source(self) -> None:
        tenant = f"obs-dedup-{uuid.uuid4().hex[:8]}"
        run_id = uuid.uuid4()
        await _submit_run(run_id, uuid.uuid4(), tenant)
        driver = _make_driver()
        claim = await driver.claim(worker_id="obs-worker", runtime_build_hash=BUILD_HASH, tenant_id=tenant)
        assert claim is not None
        input_hash = compute_input_hash(CONFERENCE_INPUT)
        yielded = node_a({"stage": "start", "input_hash": input_hash, "value": 0})
        await _write_checkpoint(claim, yielded)
        occurred = datetime.now(UTC)
        events = [
            build_lifecycle_emit_request(
                run_id=run_id,
                command_seq=0,
                status="running",
                run_revision=1,
                occurred_at=occurred,
            )
        ]
        payload = {"outcome_kind": "yield", "input_hash": input_hash, "value": yielded["value"]}
        payload_hash = canonical_hash(payload)
        await driver.finish_delivery(
            claim,
            outcome_kind="yield",
            continue_payload_ref=f"continue-payload:{payload_hash}",
            continue_payload_hash=payload_hash,
            continue_payload=payload,
            events=events,
        )
        before = len(await _runtime_events(run_id))
        # The idempotent consume path must not duplicate the observation stream.
        receipt_again = await driver.finish_delivery(
            claim,
            outcome_kind="yield",
            continue_payload_ref=f"continue-payload:{payload_hash}",
            continue_payload_hash=payload_hash,
            continue_payload=payload,
            events=events,
        )
        assert receipt_again.result_code == "consumed"
        after = len(await _runtime_events(run_id))
        assert before == after

    async def test_rls_isolates_cross_tenant_events(self) -> None:
        tenant = f"obs-rls-{uuid.uuid4().hex[:8]}"
        other = f"obs-other-{uuid.uuid4().hex[:8]}"
        run_id = uuid.uuid4()
        await _submit_run(run_id, uuid.uuid4(), tenant)
        driver = _make_driver()
        claim = await driver.claim(worker_id="obs-worker", runtime_build_hash=BUILD_HASH, tenant_id=tenant)
        assert claim is not None
        input_hash = compute_input_hash(CONFERENCE_INPUT)
        yielded = node_a({"stage": "start", "input_hash": input_hash, "value": 0})
        await _write_checkpoint(claim, yielded)
        occurred = datetime.now(UTC)
        events = [
            build_lifecycle_emit_request(
                run_id=run_id, command_seq=0, status="running", run_revision=1, occurred_at=occurred
            )
        ]
        payload = {"outcome_kind": "yield", "input_hash": input_hash, "value": yielded["value"]}
        payload_hash = canonical_hash(payload)
        await driver.finish_delivery(
            claim,
            outcome_kind="yield",
            continue_payload_ref=f"continue-payload:{payload_hash}",
            continue_payload_hash=payload_hash,
            continue_payload=payload,
            events=events,
        )

        engine = create_async_engine(API_URL)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT set_config('grove.tenant_id', :t, true)"), {"t": tenant})
            own = (
                await conn.execute(text("SELECT count(*) FROM runtime_event WHERE run_id = :r"), {"r": run_id})
            ).scalar()
            await conn.execute(text("SELECT set_config('grove.tenant_id', :t, true)"), {"t": other})
            cross = (
                await conn.execute(text("SELECT count(*) FROM runtime_event WHERE run_id = :r"), {"r": run_id})
            ).scalar()
        await engine.dispose()
        assert own == 1
        assert cross == 0

        engine = create_async_engine(MIGRATION_URL)
        async with engine.connect() as conn:
            outbox = (
                await conn.execute(text("SELECT count(*) FROM runtime_event_outbox WHERE run_id = :r"), {"r": run_id})
            ).scalar()
        await engine.dispose()
        assert outbox == 1
