"""POC-M 4: SIGKILL at each asset-read boundary, then recover (gated).

Kill points follow docs/31 §4 exactly: before the physical read returns,
after the read but before the checkpoint commits, and after the checkpoint.
Recovery must keep a single writer, never lose committed facts, emit exactly
one domain-view fact, and -- the invariant this matrix pins -- make zero
asset-read database calls after the checkpoint (the accepted view resumes
from the checkpoint) while un-checkpointed attempts may re-read bounded.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from app.asset_risk.contracts import AssetStateQuery, AssetStateView
from app.asset_risk.graph import build_asset_risk_graph
from app.asset_risk.kernel import AssetRiskKernel, make_asset_risk_infer_caller
from app.contracts.canonical import CanonicalFailure
from app.execution import PostgresExecutionDriver
from app.knowledge.adapter import ImmutableSnapshotKnowledgeAdapter
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
API_URL = os.environ.get("GROVE_DATABASE_URL", "postgresql+psycopg://grove_api:grove_api_ws0@127.0.0.1:54329/grove")
BUILD_HASH = "e" * 64

EXPECTED_TOTAL_READS = {"pre_read": 1, "post_read": 2, "checkpoint": 1}


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


async def _submit_asset_risk_run(run_id: UUID, submission_id: UUID, tenant: str, asset_ref: str) -> None:
    """Insert the asset-risk-bound spec/run/command directly (as A4 does)."""

    payload_hash = run_id.hex.ljust(64, "0")[:64]
    payload_ref = f"start-payload-{run_id}"
    engine = create_async_engine(MIGRATION_URL)
    spec_payload = {
        "runtime_build": {"ref": "test-build", "content_hash": BUILD_HASH},
        "graph": {
            "graph": {"ref": "graph.asset-risk@1", "version": "1", "content_hash": "c" * 64},
            "graph_state_schema_version": "state.asset-risk@1",
        },
    }
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO tenant (tenant_id) VALUES (:t) ON CONFLICT DO NOTHING"), {"t": tenant})
        await conn.execute(
            text(
                "INSERT INTO workload_principal (tenant_id, principal_id, principal_kind, "
                "workload_ref, scopes, active) VALUES (:t, 'pocm-worker', 'workload', 'pocm-test', "
                "'[\"execution.run\"]'::jsonb, true) ON CONFLICT DO NOTHING"
            ),
            {"t": tenant},
        )
        await conn.execute(
            text(
                "INSERT INTO execution_principal (tenant_id, principal_id, principal_kind) "
                "VALUES (:t, 'pocm-worker', 'workload') ON CONFLICT DO NOTHING"
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
                "VALUES (:t, :run, :sub, :digest, 'pocm-worker', 'workload', :spec_hash, :spec_ref, "
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
                "payload_hash, status) VALUES (:command_id, :t, :run, 'pocm-worker', 'workload', "
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
        await conn.execute(
            text(
                "INSERT INTO asset_risk_asset_state (tenant_id, asset_ref, asset_class, exposure_amount, "
                "currency, status, source_revision) VALUES (:t, :ref, 'credit', 1000, 'CNY', 'active', 'rev-1') "
                "ON CONFLICT DO NOTHING"
            ),
            {"t": tenant, "ref": asset_ref},
        )
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_after", ["pre_read", "post_read", "checkpoint"])
async def test_asset_risk_crash_matrix_keeps_single_writer_and_read_invariants(stop_after: str) -> None:
    if os.environ.get("GROVE_RUN_PROVIDER_G3") != "1" or not _release_chain_configured():
        pytest.skip("set GROVE_RUN_PROVIDER_G3=1 with the issued release chain and gateway env")

    tenant = f"ws6-pocm-{stop_after}-{uuid4().hex[:8]}"
    run_id = uuid4()
    asset_ref = f"asset.{run_id.hex[:10]}"
    await _submit_asset_risk_run(run_id, uuid4(), tenant, asset_ref)

    process = subprocess.Popen(  # noqa: ASYNC220, S603 - fixed helper argv, real SIGKILL matrix
        [
            sys.executable,
            "tests/helpers/ws6_asset_risk_crash_worker.py",
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
        if "fence" in stage:
            assert stage["fence"] == 1  # the crashed attempt held fence 1
        helper_reads = int(stage.get("reads", 0))
    finally:
        process.kill()
        process.wait(timeout=5)

    runtime_engine = create_async_engine(RUNTIME_URL)
    factory = async_sessionmaker(runtime_engine, expire_on_commit=False)
    driver = PostgresExecutionDriver(factory)
    recovery_reads: list[int] = []

    class _CountingSource:
        def __init__(self, inner: object) -> None:
            self._inner = inner

        async def read(
            self, query: AssetStateQuery, *, tenant_id: str, logical_read_key: str, tool_request_id: UUID
        ) -> AssetStateView | CanonicalFailure:

            recovery_reads.append(1)
            result = await self._inner.read(  # type: ignore[attr-defined]
                query, tenant_id=tenant_id, logical_read_key=logical_read_key, tool_request_id=tool_request_id
            )
            return cast("AssetStateView | CanonicalFailure", result)

    try:
        async with production_inference_lifespan(
            app_env=os.environ.get("GROVE_WS6_APP_ENV", "test"),
            runtime_build_hash=BUILD_HASH,
        ) as (port, request_factory):
            from app.asset_risk.composition import (
                PostgresPortfolioInputSource,
                build_reference_knowledge_snapshot,
            )
            from app.asset_risk.postgres_adapter import PostgresAssetStateSource
            from app.asset_risk.read_tool import AssetStateReadCeiling, AssetStateReadTool

            knowledge = ImmutableSnapshotKnowledgeAdapter(build_reference_knowledge_snapshot((tenant,)))
            tool = AssetStateReadTool(
                source=_CountingSource(PostgresAssetStateSource(factory)),
                ceiling=AssetStateReadCeiling(manifest_max_asset_refs=16),
            )
            graph = build_asset_risk_graph(
                knowledge_port=knowledge,
                asset_tool=tool,
                infer=make_asset_risk_infer_caller(port, request_factory),  # type: ignore[arg-type]
            )
            kernel = AssetRiskKernel(
                graph_factory=lambda: graph,
                input_source=PostgresPortfolioInputSource(factory, 16),
            )
            worker = RuntimeWorker(
                driver=driver,
                tenant_id=tenant,
                worker_id="pocm-recovery-worker",
                runtime_build_hash=BUILD_HASH,
                database_url=RUNTIME_URL.replace("postgresql+psycopg://", "postgresql+asyncpg://"),
                asset_risk_kernel=kernel,
                poll_interval=0.01,
                invoke_budget_seconds=70.0,
            )
            claim = None
            for _ in range(280):
                claim = await driver.claim(
                    worker_id="pocm-recovery-worker",
                    runtime_build_hash=BUILD_HASH,
                    tenant_id=tenant,
                    lease_seconds=30.0,
                )
                if claim is not None:
                    break
                import asyncio

                await asyncio.sleep(0.5)
            assert claim is not None and claim.run_id == run_id
            await worker._process_claim(claim)

        status_engine = create_async_engine(MIGRATION_URL)
        async with status_engine.begin() as conn:
            status = (
                await conn.execute(
                    text("SELECT status FROM agent_run WHERE tenant_id = :t AND run_id = :r"),
                    {"t": tenant, "r": run_id},
                )
            ).scalar_one()
            domain_view_facts = (
                await conn.execute(
                    text(
                        "SELECT payload FROM runtime_event WHERE tenant_id = :t AND run_id = :r "
                        "AND payload_schema_ref = 'grove.runtime.domain-view-accepted.v1'"
                    ),
                    {"t": tenant, "r": run_id},
                )
            ).fetchall()
            takeovers = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM runtime_event WHERE tenant_id = :t AND run_id = :r "
                        "AND payload->>'action' = 'worker_takeover'"
                    ),
                    {"t": tenant, "r": run_id},
                )
            ).scalar_one()
        await status_engine.dispose()

        assert status == "succeeded"
        assert len(domain_view_facts) == 1  # exactly one accepted view fact survives
        assert takeovers == 1  # single writer: one takeover, one terminal
        total_reads = helper_reads + len(recovery_reads)
        assert total_reads == EXPECTED_TOTAL_READS[stop_after], (
            f"{stop_after}: helper={helper_reads} recovery={len(recovery_reads)} total={total_reads}"
        )
        if stop_after == "checkpoint":
            # docs/31 §4: after the checkpoint commits, takeover re-reads nothing.
            assert recovery_reads == []
        assert domain_view_facts[0][0]["result_hash"]  # the accepted view's hash survives
    finally:
        await runtime_engine.dispose()
