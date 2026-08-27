"""WS-6 A4: a real run bound to the inference kernel executes real inference.

Gated like the real-provider G2 smoke: requires a live PostgreSQL (compose),
the issued release chain environment (AI_GATEWAY_RELEASE_* pins) and the real
gateway credential.  Everything else skips -- this test never runs against a
mock provider, and its pass cannot be claimed without the gate.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from typing import Any
from uuid import UUID, uuid4

import pytest
from app.execution import PostgresExecutionDriver
from app.execution.contracts import CONFORMANCE_GRAPH_BINDING
from app.worker.inference import production_inference_lifespan
from app.worker.loop import RuntimeWorker
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.integration

RUNTIME_URL = os.environ.get(
    "GROVE_WS6_RUNTIME_URL", "postgresql+psycopg://grove_runtime:grove_runtime_ws0@127.0.0.1:54329/grove"
)
MIGRATION_URL = os.environ.get(
    "GROVE_MIGRATION_DATABASE_URL",
    "postgresql+psycopg://grove_migration:grove_migration_ws0@127.0.0.1:54329/grove",
)
BUILD_HASH = "e" * 64


def _wait_for_stage(process: subprocess.Popen[str], expected: str) -> dict[str, Any]:
    assert process.stdout is not None
    while True:
        line = process.stdout.readline()
        if not line:
            stderr = process.stderr.read() if process.stderr is not None else ""
            raise AssertionError(f"crash helper exited before {expected}: {stderr}")
        record = json.loads(line)
        if isinstance(record, dict) and record.get("stage") == expected:
            return record


def _release_chain_configured() -> bool:
    return all(
        os.environ.get(name)
        for name in (
            "AI_GATEWAY_RELEASE_AUTHORITY_DIR",
            "AI_GATEWAY_RELEASE_CANDIDATE_PATH",
            "AI_GATEWAY_RELEASE_EXPECTED_FACTS_PATH",
            "AI_GATEWAY_RELEASE_SIGNATURE_PATH",
            "AI_GATEWAY_PROVIDER_MANIFEST_PATH",
            "AI_GATEWAY_RELEASE_ROOT_PUBLIC_KEY_SHA256",
            "AI_GATEWAY_RELEASE_POLICY_REF",
            "AI_GATEWAY_RELEASE_POLICY_VERSION",
            "AI_GATEWAY_RELEASE_POLICY_SHA256",
        )
    )


async def _submit_inference_run(run_id: UUID, submission_id: UUID, tenant: str) -> None:
    """Insert tenant/principal/spec(inference binding)/run/command directly."""
    payload_hash = run_id.hex.ljust(64, "0")[:64]
    payload_ref = f"start-payload-{run_id}"
    engine = create_async_engine(MIGRATION_URL)
    graph_binding = os.environ.get("GROVE_WS6_E2E_GRAPH_BINDING", "graph.inference@1")
    state_schema = "state.asset-risk@1" if graph_binding == "graph.asset-risk@1" else "state.inference@1"
    spec_payload = {
        "runtime_build": {"ref": "test-build", "content_hash": BUILD_HASH},
        "graph": {
            "graph": {
                "ref": graph_binding,
                "version": "1",
                "content_hash": "c" * 64,
            },
            "graph_state_schema_version": state_schema,
        },
    }
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO tenant (tenant_id) VALUES (:t) ON CONFLICT DO NOTHING"), {"t": tenant})
        await conn.execute(
            text(
                "INSERT INTO workload_principal (tenant_id, principal_id, principal_kind, "
                "workload_ref, scopes, active) VALUES (:t, 'a4-worker', 'workload', 'a4-test', "
                "'[\"execution.run\"]'::jsonb, true) ON CONFLICT DO NOTHING"
            ),
            {"t": tenant},
        )
        await conn.execute(
            text(
                "INSERT INTO execution_principal (tenant_id, principal_id, principal_kind) "
                "VALUES (:t, 'a4-worker', 'workload') ON CONFLICT DO NOTHING"
            ),
            {"t": tenant},
        )
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
                "principal_id, principal_kind, skill_spec_hash, skill_spec_ref, status, revision, "
                "runtime_build_ref, runtime_build_hash) "
                "VALUES (:t, :run, :sub, :digest, 'a4-worker', 'workload', :spec_hash, :spec_ref, "
                "'accepted', 0, 'runtime-build:a', :build)"
            ),
            {
                "t": tenant,
                "run": run_id,
                "sub": submission_id,
                "digest": "d" * 64,
                "spec_hash": "b" * 64,
                "spec_ref": "execution-spec:" + "b" * 64,
                "build": BUILD_HASH,
            },
        )
        await conn.execute(
            text(
                "INSERT INTO command_payload (tenant_id, payload_ref, payload_hash, command_schema_version, "
                "payload, sensitivity, retention) VALUES (:t, :payload_ref, :payload_hash, 'start.v1', "
                "'{}'::jsonb, 'sensitive', 'run_completion')"
            ),
            {"t": tenant, "payload_ref": payload_ref, "payload_hash": payload_hash},
        )
        await conn.execute(
            text(
                "INSERT INTO run_command (command_id, tenant_id, run_id, principal_id, principal_kind, "
                "command_seq, command_type, command_schema_version, command_digest, payload_ref, "
                "payload_hash, status) VALUES (:command_id, :t, :run, 'a4-worker', 'workload', "
                "0, 'start', 'start.v1', :digest, :payload_ref, :payload_hash, 'pending')"
            ),
            {
                "command_id": run_id,
                "t": tenant,
                "run": run_id,
                "digest": "f" * 64,
                "payload_ref": payload_ref,
                "payload_hash": payload_hash,
            },
        )
    await engine.dispose()


async def _run_state(run_id: UUID, tenant: str) -> tuple[str, list[str], list[int]]:
    engine = create_async_engine(MIGRATION_URL)
    async with engine.begin() as conn:
        status = (
            await conn.execute(
                text("SELECT status FROM agent_run WHERE tenant_id = :t AND run_id = :r"),
                {"t": tenant, "r": run_id},
            )
        ).scalar_one()
        rows = (
            await conn.execute(
                text(
                    "SELECT payload->>'action', payload->>'kind', payload->>'value' "
                    "FROM runtime_event WHERE tenant_id = :t AND run_id = :r ORDER BY run_seq"
                ),
                {"t": tenant, "r": run_id},
            )
        ).fetchall()
        actions = [row[0] for row in rows if row[0] is not None]
        infer_values = [int(row[2]) for row in rows if row[1] == "node_executed"]
    await engine.dispose()
    return str(status), actions, infer_values


@pytest.mark.asyncio
async def test_real_inference_run_completes_through_the_kernel() -> None:
    if os.environ.get("GROVE_RUN_PROVIDER_A4") != "1" or not _release_chain_configured():
        pytest.skip("set GROVE_RUN_PROVIDER_A4=1 with the issued release chain and gateway env")

    tenant = f"ws6-a4-{uuid4().hex[:10]}"
    run_id = uuid4()
    await _submit_inference_run(run_id, uuid4(), tenant)

    runtime_engine = create_async_engine(RUNTIME_URL)
    driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
    try:
        async with production_inference_lifespan(
            app_env=os.environ.get("GROVE_WS6_APP_ENV", "test"),
            runtime_build_hash=BUILD_HASH,
        ) as (port, request_factory):
            worker = RuntimeWorker(
                driver=driver,
                tenant_id=tenant,
                worker_id="a4-worker",
                runtime_build_hash=BUILD_HASH,
                database_url=RUNTIME_URL.replace("postgresql+psycopg://", "postgresql+asyncpg://"),
                inference_port=port,
                inference_request_factory=request_factory,
                poll_interval=0.01,
            )
            claim = await driver.claim(
                worker_id="a4-worker",
                runtime_build_hash=BUILD_HASH,
                tenant_id=tenant,
                lease_seconds=30.0,
            )
            assert claim is not None
            assert claim.graph_binding.graph_ref == "graph.inference@1"
            assert claim.graph_binding.graph_state_schema_version == "state.inference@1"
            assert claim.graph_binding != CONFORMANCE_GRAPH_BINDING

            await worker._process_claim(claim)

            status, actions, infer_values = await _run_state(run_id, tenant)
            assert status == "succeeded"
            assert actions == ["worker_claimed", "checkpoint_applied", "command_applied"]
            # The real infer node's token usage is committed as evidence.
            assert len(infer_values) == 1
            assert infer_values[0] > 0
    finally:
        await runtime_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_after", ["claim", "checkpoint", "finish"])
async def test_real_inference_crash_matrix_preserves_single_writer(stop_after: str) -> None:
    """SIGKILL at each durable boundary of the real inference flow, then recover."""
    if os.environ.get("GROVE_RUN_PROVIDER_A4") != "1" or not _release_chain_configured():
        pytest.skip("set GROVE_RUN_PROVIDER_A4=1 with the issued release chain and gateway env")

    tenant = f"ws6-a5-{stop_after}-{uuid4().hex[:8]}"
    run_id = uuid4()
    await _submit_inference_run(run_id, uuid4(), tenant)

    process = subprocess.Popen(  # noqa: ASYNC220, S603 - fixed helper argv, real SIGKILL matrix
        [
            sys.executable,
            "tests/helpers/ws6_inference_crash_worker.py",
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
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stage = _wait_for_stage(process, stop_after)
        assert stage.get("fence") == 1
    finally:
        process.kill()
        process.wait(timeout=5)

    runtime_engine = create_async_engine(RUNTIME_URL)
    driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
    try:
        async with production_inference_lifespan(
            app_env=os.environ.get("GROVE_WS6_APP_ENV", "test"),
            runtime_build_hash=BUILD_HASH,
        ) as (port, request_factory):
            worker = RuntimeWorker(
                driver=driver,
                tenant_id=tenant,
                worker_id="a5-recovery-worker",
                runtime_build_hash=BUILD_HASH,
                database_url=RUNTIME_URL.replace("postgresql+psycopg://", "postgresql+asyncpg://"),
                inference_port=port,
                inference_request_factory=request_factory,
                poll_interval=0.01,
            )
            claim = None
            for _ in range(280):
                claim = await driver.claim(
                    worker_id="a5-recovery-worker",
                    runtime_build_hash=BUILD_HASH,
                    tenant_id=tenant,
                    lease_seconds=30.0,
                )
                if claim is not None:
                    break
                await asyncio.sleep(0.25)

            status, actions, infer_values = await _run_state(run_id, tenant)
            if stop_after == "finish":
                # The killed worker already committed the terminal delivery;
                # there is nothing left to claim and no fact was lost.
                assert claim is None
                assert status == "succeeded"
                assert actions == ["worker_claimed", "lease_renewed", "checkpoint_applied", "command_applied"]
            else:
                assert claim is not None
                assert claim.graph_binding.graph_ref == "graph.inference@1"
                await worker._process_claim(claim)
                status, actions, infer_values = await _run_state(run_id, tenant)
                assert status == "succeeded"
                if stop_after == "claim":
                    assert actions == ["worker_claimed", "worker_takeover", "checkpoint_applied", "command_applied"]
                else:
                    assert actions == [
                        "worker_claimed",
                        "lease_renewed",
                        "checkpoint_applied",
                        "worker_takeover",
                        "checkpoint_applied",
                        "command_applied",
                    ]
            # Exactly one committed infer-node token evidence survives.
            assert len(infer_values) == 1
            assert infer_values[0] > 0
    finally:
        await runtime_engine.dispose()


@pytest.mark.asyncio
async def test_real_asset_risk_run_completes_the_reference_loop() -> None:
    """docs/31 §2 closed loop: knowledge + live asset state -> real inference -> report."""
    if os.environ.get("GROVE_RUN_PROVIDER_A4") != "1" or not _release_chain_configured():
        pytest.skip("set GROVE_RUN_PROVIDER_A4=1 with the issued release chain and gateway env")

    from app.asset_risk.composition import compose_asset_risk_kernel
    from sqlalchemy.ext.asyncio import async_sessionmaker as _asm

    tenant = f"ws6-ar-{uuid4().hex[:8]}"
    run_id = uuid4()

    # 提交时就绑定 asset-risk 图（spec 是不可变工件，不能事后改写），
    # 并种入组合输入源会读取的资产组合。
    os.environ["GROVE_WS6_E2E_GRAPH_BINDING"] = "graph.asset-risk@1"
    try:
        await _submit_inference_run(run_id, uuid4(), tenant)
    finally:
        del os.environ["GROVE_WS6_E2E_GRAPH_BINDING"]
    engine = create_async_engine(MIGRATION_URL)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO asset_risk_asset_state (tenant_id, asset_ref, asset_class, exposure_amount, "
                "currency, status, source_revision) VALUES (:t, :ref, 'credit', 1000, 'CNY', 'active', 'rev-1') "
                "ON CONFLICT DO NOTHING"
            ),
            {"t": tenant, "ref": f"asset.{run_id.hex[:8]}"},
        )
    await engine.dispose()

    runtime_engine = create_async_engine(RUNTIME_URL)
    driver = PostgresExecutionDriver(async_sessionmaker(runtime_engine, expire_on_commit=False))
    try:
        async with production_inference_lifespan(
            app_env=os.environ.get("GROVE_WS6_APP_ENV", "test"),
            runtime_build_hash=BUILD_HASH,
        ) as (port, request_factory):
            kernel = compose_asset_risk_kernel(
                inference_port=port,
                inference_request_factory=request_factory,
                runtime_session_factory=_asm(runtime_engine, expire_on_commit=False),
                worker_tenant_id=tenant,
            )
            worker = RuntimeWorker(
                driver=driver,
                tenant_id=tenant,
                worker_id="ar-e2e-worker",
                runtime_build_hash=BUILD_HASH,
                database_url=RUNTIME_URL.replace("postgresql+psycopg://", "postgresql+asyncpg://"),
                asset_risk_kernel=kernel,
                poll_interval=0.01,
                invoke_budget_seconds=75.0,
            )
            claim = await driver.claim(
                worker_id="ar-e2e-worker", runtime_build_hash=BUILD_HASH, tenant_id=tenant, lease_seconds=90.0
            )
            assert claim is not None
            assert claim.graph_binding.graph_ref == "graph.asset-risk@1"

            await worker._process_claim(claim)

            status, actions, infer_values = await _run_state(run_id, tenant)
            assert status == "succeeded", f"actions={actions}"
            assert actions == ["worker_claimed", "checkpoint_applied", "command_applied"]
    finally:
        await runtime_engine.dispose()
