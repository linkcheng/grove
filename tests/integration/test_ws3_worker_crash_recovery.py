"""Crash recovery integration tests for the runtime_worker loop.

Verifies the core N-25 invariant: kill a worker at various points
and confirm a second worker can safely recover without duplicate
side effects or orphaned commands.

These tests use real PostgreSQL with the grove_runtime role.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from app.execution import PostgresExecutionDriver, StaleExecutionFence
from app.execution.conformance_graph import compute_input_hash, node_a, node_b
from app.worker.loop import RuntimeWorker
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

API_URL = os.environ.get("WS3_API_DATABASE_URL", "postgresql+psycopg://grove_api:grove_api_ws0@127.0.0.1:54329/grove")
RUNTIME_URL = os.environ.get(
    "WS3_RUNTIME_DATABASE_URL",
    "postgresql+psycopg://grove_runtime:grove_runtime_ws0@127.0.0.1:54329/grove",
)
MIGRATION_URL = os.environ.get(
    "WS3_MIGRATION_DATABASE_URL",
    "postgresql+psycopg://grove_migration:grove_migration_ws0@127.0.0.1:54329/grove",
)
BUILD_HASH = "a" * 64
TENANT_BASE = "crash-test-tenant"
_CRASH_HELPER = Path(__file__).parents[1] / "helpers" / "ws3_crash_worker.py"


async def _submit_run(
    run_id: uuid.UUID,
    submission_id: uuid.UUID,
    tenant: str = TENANT_BASE,
    runtime_build_hash: str = BUILD_HASH,
) -> None:
    """Insert a tenant, spec, run, and pending start command directly."""
    payload_hash = run_id.hex.ljust(64, "0")[:64]
    payload_ref = f"start-payload-{run_id}"
    engine = create_async_engine(MIGRATION_URL)
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO tenant (tenant_id) VALUES (:t) ON CONFLICT DO NOTHING"), {"t": tenant})
        await conn.execute(
            text(
                "INSERT INTO workload_principal (tenant_id, principal_id, principal_kind, "
                "workload_ref, scopes, active) "
                "VALUES (:t, 'crash-worker', 'workload', 'crash-test', "
                "'[\"execution.run\"]'::jsonb, true) ON CONFLICT DO NOTHING"
            ),
            {"t": tenant},
        )
        await conn.execute(
            text(
                "INSERT INTO execution_principal (tenant_id, principal_id, principal_kind) "
                "VALUES (:t, 'crash-worker', 'workload') ON CONFLICT DO NOTHING"
            ),
            {"t": tenant},
        )
        spec_payload = {"runtime_build": {"ref": "test-build", "content_hash": runtime_build_hash}}
        import json

        await conn.execute(
            text(
                "INSERT INTO execution_spec (tenant_id, skill_spec_hash, spec_ref, spec_payload) "
                "VALUES (:t, :h, :ref, CAST(:payload AS JSONB)) ON CONFLICT DO NOTHING"
            ),
            {
                "t": tenant,
                "h": "b" * 64,
                "ref": "execution-spec:" + "b" * 64,
                "payload": json.dumps(spec_payload),
            },
        )
        await conn.execute(
            text(
                "INSERT INTO agent_run (tenant_id, run_id, submission_id, submission_digest, "
                "principal_id, principal_kind, skill_spec_hash, skill_spec_ref, "
                "runtime_build_ref, runtime_build_hash, status, revision) "
                "VALUES (:t, :rid, :sid, :dig, 'crash-worker', 'workload', "
                ":ssh, :ssr, 'test-build', :rbh, 'accepted', 0)"
            ),
            {
                "t": tenant,
                "rid": run_id,
                "sid": submission_id,
                "dig": payload_hash,
                "ssh": "b" * 64,
                "ssr": "execution-spec:" + "b" * 64,
                "rbh": runtime_build_hash,
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
                "VALUES (:t, :cid, :rid, 'crash-worker', 'workload', "
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


async def _get_run_state(run_id: uuid.UUID, tenant: str = TENANT_BASE) -> dict[str, Any]:
    engine = create_async_engine(MIGRATION_URL)
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT status, revision, execution_fence, lease_owner, "
                "latest_checkpoint_id, latest_applied_command_seq "
                "FROM agent_run WHERE tenant_id = :t AND run_id = :r"
            ),
            {"t": tenant, "r": run_id},
        )
        row = result.fetchone()
        result2 = await conn.execute(
            text(
                "SELECT command_seq, command_type, status, lease_owner "
                "FROM run_command WHERE tenant_id = :t AND run_id = :r ORDER BY command_seq"
            ),
            {"t": tenant, "r": run_id},
        )
        commands = result2.fetchall()
    await engine.dispose()
    if row is None:
        return {"exists": False}
    return {
        "exists": True,
        "status": row[0],
        "revision": row[1],
        "fence": row[2],
        "lease_owner": row[3],
        "checkpoint": row[4],
        "applied_seq": row[5],
        "commands": [{"seq": c[0], "type": c[1], "status": c[2], "lease_owner": c[3]} for c in commands],
    }


async def _audit_actions(run_id: uuid.UUID, tenant: str) -> list[str]:
    engine = create_async_engine(MIGRATION_URL)
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT payload->>'action' FROM runtime_event "
                        "WHERE tenant_id = :t AND run_id = :r "
                        "AND payload_schema_ref = 'grove.runtime.execution-audit.v1' ORDER BY run_seq"
                    ),
                    {"t": tenant, "r": run_id},
                )
            )
            .scalars()
            .all()
        )
    await engine.dispose()
    return list(rows)


def _make_runtime_driver() -> PostgresExecutionDriver:
    engine = create_async_engine(RUNTIME_URL)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    return PostgresExecutionDriver(session_factory=session_maker, lease_seconds=30.0)


def _start_crash_process(*, tenant: str, run_id: uuid.UUID, stop_after: str) -> subprocess.Popen[str]:
    return subprocess.Popen(  # noqa: S603 - fixed interpreter and local test helper
        [
            sys.executable,
            str(_CRASH_HELPER),
            "--runtime-url",
            RUNTIME_URL,
            "--tenant",
            tenant,
            "--run-id",
            str(run_id),
            "--build-hash",
            BUILD_HASH,
            "--stop-after",
            stop_after,
        ],
        cwd=Path(__file__).parents[2],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for_stage(process: subprocess.Popen[str], expected: str) -> dict[str, Any]:
    assert process.stdout is not None
    while True:
        line = process.stdout.readline()
        if not line:
            stderr = process.stderr.read() if process.stderr is not None else ""
            raise AssertionError(f"crash helper exited before {expected}: {stderr}")
        record = json.loads(line)
        if not isinstance(record, dict):
            raise AssertionError("crash helper emitted a non-object record")
        if record.get("stage") == expected:
            return record


def _kill_process(process: subprocess.Popen[str]) -> None:
    process.kill()
    process.wait(timeout=5)
    assert process.returncode is not None and process.returncode < 0


@pytest.mark.integration
@pytest.mark.asyncio
class TestWorkerCrashRecovery:
    """N-25 crash recovery: single writer, no duplicate, no orphan."""

    async def test_claim_then_expire_then_reclaim_by_second_worker(self) -> None:
        tenant = f"crash-test-{uuid.uuid4().hex[:8]}"
        """Worker A claims but crashes before checkpoint; lease expires; worker B reclaims."""
        run_id = uuid.uuid4()
        submission_id = uuid.uuid4()
        await _submit_run(run_id, submission_id, tenant)

        # Worker A claims.
        driver_a = _make_runtime_driver()
        claim_a = await driver_a.claim(
            worker_id="worker-a",
            runtime_build_hash=BUILD_HASH,
            tenant_id=tenant,
            lease_seconds=1.0,
        )
        assert claim_a is not None
        assert claim_a.worker_id == "worker-a"

        # Worker A "crashes" — no checkpoint, no consume.
        # Wait for lease to expire.
        await asyncio.sleep(2.0)

        # Worker A's claim is now stale.
        with pytest.raises(StaleExecutionFence):
            await driver_a.heartbeat(claim_a)

        # Worker B reclaims the same command.
        driver_b = _make_runtime_driver()
        claim_b = await driver_b.claim(
            worker_id="worker-b",
            runtime_build_hash=BUILD_HASH,
            tenant_id=tenant,
            lease_seconds=30.0,
        )
        assert claim_b is not None
        assert claim_b.worker_id == "worker-b"
        assert claim_b.command_id == claim_a.command_id
        assert claim_b.execution_fence > claim_a.execution_fence

    async def test_production_worker_loop_emits_full_committed_audit_chain(self) -> None:
        tenant = f"worker-audit-{uuid.uuid4().hex[:8]}"
        run_id = uuid.uuid4()
        await _submit_run(run_id, uuid.uuid4(), tenant)
        worker = RuntimeWorker(
            driver=_make_runtime_driver(),
            tenant_id=tenant,
            worker_id="worker-audit",
            runtime_build_hash=BUILD_HASH,
            database_url=RUNTIME_URL.replace("postgresql+psycopg://", "postgresql+asyncpg://"),
            poll_interval=0.01,
        )

        assert await worker._poll_once() is True
        assert await worker._poll_once() is True

        state = await _get_run_state(run_id, tenant)
        assert state["status"] == "succeeded"
        actions = await _audit_actions(run_id, tenant)
        assert actions == [
            "worker_claimed",
            "checkpoint_applied",
            "command_applied",
            "command_accepted",
            "worker_takeover",
            "checkpoint_applied",
            "command_applied",
        ]

    @pytest.mark.parametrize("stop_after", ["claim", "checkpoint", "finish"])
    async def test_real_process_kill_matrix_preserves_single_writer(
        self,
        stop_after: str,
    ) -> None:
        """SIGKILL at three durable boundaries, then recover with worker B."""

        tenant = f"crash-process-{uuid.uuid4().hex[:8]}"
        run_id = uuid.uuid4()
        await _submit_run(run_id, uuid.uuid4(), tenant)

        process = _start_crash_process(tenant=tenant, run_id=run_id, stop_after=stop_after)
        try:
            marker = await asyncio.to_thread(_wait_for_stage, process, stop_after)
            first_fence = marker.get("fence")
            await asyncio.to_thread(_kill_process, process)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

        if stop_after != "finish":
            await asyncio.sleep(2.0)

        driver_b = _make_runtime_driver()
        claim_b = await driver_b.claim(
            worker_id="worker-b-after-sigkill",
            runtime_build_hash=BUILD_HASH,
            tenant_id=tenant,
            lease_seconds=30.0,
        )
        assert claim_b is not None
        if stop_after != "finish":
            assert isinstance(first_fence, int)
            assert claim_b.execution_fence > first_fence
            assert claim_b.command_seq == 0
        else:
            assert claim_b.command_seq == 1

        state = await _get_run_state(run_id, tenant)
        leased = [command for command in state["commands"] if command["status"] == "leased"]
        assert len(leased) == 1
        assert leased[0]["lease_owner"] == "worker-b-after-sigkill"
        assert sum(command["type"] == "continue" for command in state["commands"]) <= 1

    async def test_checkpoint_survives_crash_second_worker_finishes(self) -> None:
        tenant = f"crash-test-{uuid.uuid4().hex[:8]}"
        """Worker A writes checkpoint but crashes before finish; worker B finishes."""
        run_id = uuid.uuid4()
        submission_id = uuid.uuid4()
        await _submit_run(run_id, submission_id, tenant)

        # Worker A claims and writes checkpoint.
        driver_a = _make_runtime_driver()
        claim_a = await driver_a.claim(
            worker_id="worker-a",
            runtime_build_hash=BUILD_HASH,
            tenant_id=tenant,
            lease_seconds=2.0,
        )
        assert claim_a is not None

        # Write checkpoint as worker A.
        from typing import cast

        from app.execution.checkpoint import FencedPostgresSaver
        from langchain_core.runnables.config import RunnableConfig
        from langgraph.checkpoint.base import ChannelVersions, CheckpointMetadata, empty_checkpoint

        input_hash = compute_input_hash("grove-conformance")
        yielded = node_a({"stage": "start", "input_hash": input_hash, "value": 0})

        conninfo = RUNTIME_URL.replace("postgresql+psycopg://", "postgresql://")
        async with await psycopg.AsyncConnection.connect(conninfo=conninfo) as conn:
            checkpoint = empty_checkpoint()
            versions: ChannelVersions = {k: str(v) for k, v in yielded.items()}
            checkpoint["channel_versions"] = versions
            checkpoint["channel_values"] = dict(yielded)
            config: dict[str, Any] = {"configurable": {"thread_id": str(run_id), "checkpoint_ns": ""}}
            saver = FencedPostgresSaver(conn, claim_a)
            await saver.aput(cast("RunnableConfig", config), checkpoint, cast(CheckpointMetadata, {}), versions)
            await conn.commit()

        # Worker A "crashes" after checkpoint, before finish_delivery.
        # Wait for lease expiry.
        await asyncio.sleep(3.0)

        # Worker B reclaims and finishes delivery.
        driver_b = _make_runtime_driver()
        claim_b = await driver_b.claim(
            worker_id="worker-b",
            runtime_build_hash=BUILD_HASH,
            tenant_id=tenant,
            lease_seconds=30.0,
        )
        assert claim_b is not None
        assert claim_b.execution_fence > claim_a.execution_fence

        # finish_delivery should succeed because checkpoint proof exists for
        # the command (even though it was written by worker A's fence).
        # The consume function checks checkpoint proof binding to command identity,
        # not worker identity.
        from app.contracts.canonical import canonical_hash

        payload = {"outcome_kind": "yield", "input_hash": input_hash, "value": yielded["value"]}
        payload_hash = canonical_hash(payload)
        payload_ref = f"continue-payload:{payload_hash}"

        receipt = await driver_b.finish_delivery(
            claim_b,
            outcome_kind="yield",
            continue_payload_ref=payload_ref,
            continue_payload_hash=payload_hash,
            continue_payload=payload,
        )
        assert receipt.result_code == "consumed"
        assert receipt.continue_command_id is not None

        # Verify state: run is running, continue command exists.
        state = await _get_run_state(run_id, tenant)
        assert state["status"] == "running"
        assert state["revision"] == 1
        assert len(state["commands"]) == 2  # start (consumed) + continue (pending)
        assert state["commands"][0]["status"] == "consumed"
        assert state["commands"][1]["type"] == "continue"
        assert state["commands"][1]["status"] == "pending"

    async def test_idempotent_finish_delivery_after_retry(self) -> None:
        tenant = f"crash-test-{uuid.uuid4().hex[:8]}"
        """finish_delivery is idempotent: same claim twice returns same result."""
        run_id = uuid.uuid4()
        submission_id = uuid.uuid4()
        await _submit_run(run_id, submission_id, tenant)

        driver = _make_runtime_driver()
        claim = await driver.claim(
            worker_id="worker-idem",
            runtime_build_hash=BUILD_HASH,
            tenant_id=tenant,
            lease_seconds=30.0,
        )
        assert claim is not None

        # Write checkpoint.
        from typing import cast

        from app.execution.checkpoint import FencedPostgresSaver
        from langchain_core.runnables.config import RunnableConfig
        from langgraph.checkpoint.base import ChannelVersions, CheckpointMetadata, empty_checkpoint

        input_hash = compute_input_hash("grove-conformance")
        yielded = node_a({"stage": "start", "input_hash": input_hash, "value": 0})

        conninfo = RUNTIME_URL.replace("postgresql+psycopg://", "postgresql://")
        async with await psycopg.AsyncConnection.connect(conninfo=conninfo) as conn:
            checkpoint = empty_checkpoint()
            versions: ChannelVersions = {k: str(v) for k, v in yielded.items()}
            checkpoint["channel_versions"] = versions
            checkpoint["channel_values"] = dict(yielded)
            config: dict[str, Any] = {"configurable": {"thread_id": str(run_id), "checkpoint_ns": ""}}
            saver = FencedPostgresSaver(conn, claim)
            await saver.aput(cast("RunnableConfig", config), checkpoint, cast(CheckpointMetadata, {}), versions)
            await conn.commit()

        # First finish_delivery.
        from app.contracts.canonical import canonical_hash

        payload = {"outcome_kind": "yield", "input_hash": input_hash, "value": yielded["value"]}
        payload_hash = canonical_hash(payload)
        payload_ref = f"continue-payload:{payload_hash}"

        receipt1 = await driver.finish_delivery(
            claim,
            outcome_kind="yield",
            continue_payload_ref=payload_ref,
            continue_payload_hash=payload_hash,
            continue_payload=payload,
        )
        assert receipt1.result_code == "consumed"

        # Second finish_delivery with same claim — should be idempotent.
        receipt2 = await driver.finish_delivery(
            claim,
            outcome_kind="yield",
            continue_payload_ref=payload_ref,
            continue_payload_hash=payload_hash,
            continue_payload=payload,
        )
        assert receipt2.result_code == "consumed"
        # Same continue command.
        assert receipt2.continue_command_id == receipt1.continue_command_id

        # No duplicate continue commands.
        state = await _get_run_state(run_id, tenant)
        continue_count = sum(1 for c in state["commands"] if c["type"] == "continue")
        assert continue_count == 1

    async def test_concurrent_workers_only_one_claims(self) -> None:
        tenant = f"crash-test-{uuid.uuid4().hex[:8]}"
        """Two workers polling simultaneously: only one gets the claim (SKIP LOCKED)."""
        run_id = uuid.uuid4()
        submission_id = uuid.uuid4()
        await _submit_run(run_id, submission_id, tenant)

        driver_a = _make_runtime_driver()
        driver_b = _make_runtime_driver()

        # Both try to claim concurrently.
        claim_a_task = asyncio.create_task(
            driver_a.claim(worker_id="worker-concurrent-a", runtime_build_hash=BUILD_HASH, tenant_id=tenant)
        )
        claim_b_task = asyncio.create_task(
            driver_b.claim(worker_id="worker-concurrent-b", runtime_build_hash=BUILD_HASH, tenant_id=tenant)
        )

        claim_a = await claim_a_task
        claim_b = await claim_b_task

        # Exactly one should have gotten the claim.
        winners = [c for c in (claim_a, claim_b) if c is not None]
        assert len(winners) == 1

    async def test_terminal_delivery_marks_run_succeeded(self) -> None:
        tenant = f"crash-test-{uuid.uuid4().hex[:8]}"
        """Full two-stage run: start → yield → continue → terminal → succeeded."""
        run_id = uuid.uuid4()
        submission_id = uuid.uuid4()
        await _submit_run(run_id, submission_id, tenant)

        driver = _make_runtime_driver()
        from typing import cast

        from app.contracts.canonical import canonical_hash
        from app.execution.checkpoint import FencedPostgresSaver
        from langchain_core.runnables.config import RunnableConfig
        from langgraph.checkpoint.base import ChannelVersions, CheckpointMetadata, empty_checkpoint

        # Stage 1: start → yield
        claim1 = await driver.claim(
            worker_id="worker-full",
            runtime_build_hash=BUILD_HASH,
            tenant_id=tenant,
        )
        assert claim1 is not None

        input_hash = compute_input_hash("grove-conformance")
        yielded = node_a({"stage": "start", "input_hash": input_hash, "value": 0})

        conninfo = RUNTIME_URL.replace("postgresql+psycopg://", "postgresql://")
        async with await psycopg.AsyncConnection.connect(conninfo=conninfo) as conn:
            checkpoint = empty_checkpoint()
            versions: ChannelVersions = {k: str(v) for k, v in yielded.items()}
            checkpoint["channel_versions"] = versions
            checkpoint["channel_values"] = dict(yielded)
            config = {"configurable": {"thread_id": str(run_id), "checkpoint_ns": ""}}
            saver = FencedPostgresSaver(conn, claim1)
            await saver.aput(cast("RunnableConfig", config), checkpoint, cast(CheckpointMetadata, {}), versions)
            await conn.commit()

        payload = {"outcome_kind": "yield", "input_hash": input_hash, "value": yielded["value"]}
        payload_hash = canonical_hash(payload)
        receipt1 = await driver.finish_delivery(
            claim1,
            outcome_kind="yield",
            continue_payload_ref=f"continue-payload:{payload_hash}",
            continue_payload_hash=payload_hash,
            continue_payload=payload,
        )
        assert receipt1.continue_command_id is not None

        # Stage 2: continue → terminal
        claim2 = await driver.claim(
            worker_id="worker-full",
            runtime_build_hash=BUILD_HASH,
            tenant_id=tenant,
        )
        assert claim2 is not None
        assert claim2.command_seq == 1

        terminal = node_b(yielded)
        async with await psycopg.AsyncConnection.connect(conninfo=conninfo) as conn:
            checkpoint = empty_checkpoint()
            versions = {k: str(v) for k, v in terminal.items()}
            checkpoint["channel_versions"] = versions
            checkpoint["channel_values"] = dict(terminal)
            config = {"configurable": {"thread_id": str(run_id), "checkpoint_ns": ""}}
            saver = FencedPostgresSaver(conn, claim2)
            await saver.aput(cast("RunnableConfig", config), checkpoint, cast(CheckpointMetadata, {}), versions)
            await conn.commit()

        receipt2 = await driver.finish_delivery(claim2, outcome_kind="terminal")
        assert receipt2.result_code == "consumed"

        state = await _get_run_state(run_id, tenant)
        assert state["status"] == "succeeded"
        assert len(state["commands"]) == 2
        assert all(c["status"] == "consumed" for c in state["commands"])
