"""Real subprocess for the POC-M asset-risk crash-recovery matrix.

The parent test sends SIGKILL only after this process reports an exact
durable boundary of the real asset-risk flow:

  pre_read    -- claimed, before the physical asset read is issued
  post_read   -- the read returned, still inside the invoke, pre-checkpoint
  checkpoint  -- the fenced checkpoint committed, before finish delivery

Mirrors tests/helpers/ws6_inference_crash_worker.py: fault hooks stay in a
test helper, never in the production worker.  The read wrapper counts every
physical read and reports it with each stage so the parent can assert the
POC-M 4 database-call invariants.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.asset_risk.contracts import AssetStateQuery, AssetStateView
from app.asset_risk.graph import build_asset_risk_graph
from app.asset_risk.kernel import make_asset_risk_infer_caller
from app.asset_risk.read_tool import AssetStateReadCeiling, AssetStateReadTool
from app.contracts.canonical import canonical_hash
from app.execution import PostgresExecutionDriver
from app.knowledge.adapter import ImmutableSnapshotKnowledgeAdapter
from app.observation.facts import (
    build_domain_view_emit_request,
    build_lifecycle_emit_request,
    build_node_executed_emit_request,
)
from app.worker.inference import production_inference_lifespan
from app.worker.loop import RuntimeWorker
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def _ready(stage: str, **details: object) -> None:
    print(json.dumps({"stage": stage, **details}, sort_keys=True), flush=True)


class _SignallingSource:
    """Wrap the PG source: count reads, park at the requested boundary."""

    def __init__(self, inner: object, stop_after: str, reads: list[int]) -> None:
        self._inner = inner
        self._stop_after = stop_after
        self._reads = reads

    async def read(
        self, query: AssetStateQuery, *, tenant_id: str, logical_read_key: str, tool_request_id: UUID
    ) -> AssetStateView:
        if self._stop_after == "pre_read":
            _ready("pre_read", reads=len(self._reads))
            await asyncio.Event().wait()
        result = await self._inner.read(  # type: ignore[attr-defined]
            query, tenant_id=tenant_id, logical_read_key=logical_read_key, tool_request_id=tool_request_id
        )
        self._reads.append(1)
        if self._stop_after == "post_read":
            _ready("post_read", reads=len(self._reads))
            await asyncio.Event().wait()
        return result  # type: ignore[no-any-return]


async def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-url", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--run-id", required=True, type=UUID)
    parser.add_argument("--build-hash", required=True)
    parser.add_argument("--stop-after", required=True, choices=("pre_read", "post_read", "checkpoint"))
    args = parser.parse_args()

    engine = create_async_engine(args.runtime_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    driver = PostgresExecutionDriver(factory, lease_seconds=1.0)
    worker_id = f"asset-risk-crash-{args.stop_after}"
    reads: list[int] = []
    try:
        async with production_inference_lifespan(
            app_env=os.environ.get("GROVE_WS6_APP_ENV", "test"),
            runtime_build_hash=args.build_hash,
        ) as (port, request_factory):
            from app.asset_risk.composition import (
                PostgresPortfolioInputSource,
                build_reference_knowledge_snapshot,
            )
            from app.asset_risk.postgres_adapter import PostgresAssetStateSource

            knowledge = ImmutableSnapshotKnowledgeAdapter(build_reference_knowledge_snapshot((args.tenant,)))
            tool = AssetStateReadTool(
                source=_SignallingSource(PostgresAssetStateSource(factory), args.stop_after, reads),
                ceiling=AssetStateReadCeiling(manifest_max_asset_refs=16),
            )
            graph = build_asset_risk_graph(
                knowledge_port=knowledge,
                asset_tool=tool,
                infer=make_asset_risk_infer_caller(port, request_factory),  # type: ignore[arg-type]
            )
            worker = RuntimeWorker(
                driver=driver,
                tenant_id=args.tenant,
                worker_id=worker_id,
                runtime_build_hash=args.build_hash,
                database_url=args.runtime_url.replace("postgresql+psycopg://", "postgresql+asyncpg://"),
            )
            claim = await driver.claim(
                worker_id=worker_id,
                runtime_build_hash=args.build_hash,
                tenant_id=args.tenant,
                lease_seconds=1.0,
            )
            if claim is None or claim.run_id != args.run_id:
                raise RuntimeError("subprocess did not claim the expected run")
            _ready("claim", fence=claim.execution_fence, reads=len(reads))
            if args.stop_after == "claim":
                await asyncio.Event().wait()

            # Real inference outlives the 1s crash lease; renew immediately so
            # the fenced checkpoint write stays authorized (A5 semantics).
            claim = await driver.heartbeat(claim, lease_seconds=60.0)

            asset_refs = await PostgresPortfolioInputSource(factory, 16).asset_refs(args.tenant, args.run_id)
            terminal = await graph.ainvoke(
                {
                    "stage": "start",
                    "tenant_id": args.tenant,
                    "run_id": str(args.run_id),
                    "asset_refs": asset_refs,
                }
            )
            if terminal.get("stage") != "terminal":
                raise RuntimeError(f"asset-risk graph failed: {terminal.get('failure_class')}")
            await worker._write_checkpoint(claim, dict(terminal))
            _ready("checkpoint", fence=claim.execution_fence, reads=len(reads))
            if args.stop_after == "checkpoint":
                await asyncio.Event().wait()

            occurred = datetime.now(UTC)
            report = terminal.get("report", {})
            asset_view = terminal["asset_view"]
            asset_provenance = terminal["asset_provenance"]
            events = [
                build_domain_view_emit_request(
                    run_id=claim.run_id,
                    command_seq=claim.command_seq,
                    tool_request_id=UUID(str(asset_view["tool_request_id"])),
                    view_schema_ref="AssetStateView@1",
                    observed_at=datetime.fromisoformat(str(asset_view["observed_at"])),
                    source_ref=str(asset_provenance["source_ref"]),
                    result_hash=str(asset_provenance["result_content_hash"]),
                    item_count=len(asset_view["assets"]),
                    occurred_at=occurred,
                ),
                build_node_executed_emit_request(
                    run_id=claim.run_id,
                    command_seq=claim.command_seq,
                    node_id="asset_risk_skill",
                    stage="terminal",
                    input_hash=str(report.get("asset_view_hash", "")),
                    value=int(report.get("knowledge_items", 0)),
                    occurred_at=occurred,
                ),
                build_lifecycle_emit_request(
                    run_id=claim.run_id,
                    command_seq=claim.command_seq,
                    status="succeeded",
                    run_revision=claim.command_seq,
                    occurred_at=occurred,
                ),
            ]
            payload = {"outcome_kind": "terminal", "reads": len(reads), "nonce": str(uuid4())}
            receipt = await driver.finish_delivery(claim, outcome_kind="terminal", events=events)
            _ready("finish", fence=claim.execution_fence, status=receipt.status, digest=canonical_hash(payload))
            await asyncio.Event().wait()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
