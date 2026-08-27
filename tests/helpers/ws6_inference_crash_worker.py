"""Real subprocess for the WS-6 inference crash-recovery matrix.

The parent test sends SIGKILL only after this process reports an exact
durable boundary (claim / checkpoint / finish) of the real inference flow.
Mirrors tests/helpers/ws3_crash_worker.py: the fault hooks stay in a test
helper, never in the production worker.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from uuid import UUID

from app.contracts.canonical import canonical_hash
from app.execution import PostgresExecutionDriver
from app.execution.inference_graph import (
    build_inference_graph,
    compute_inference_input_hash,
)
from app.observation.facts import build_lifecycle_emit_request, build_node_executed_emit_request
from app.worker.inference import production_inference_lifespan
from app.worker.loop import RuntimeWorker
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def _ready(stage: str, **details: object) -> None:
    print(json.dumps({"stage": stage, **details}, sort_keys=True), flush=True)


async def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-url", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--run-id", required=True, type=UUID)
    parser.add_argument("--build-hash", required=True)
    parser.add_argument("--stop-after", required=True, choices=("claim", "checkpoint", "finish"))
    args = parser.parse_args()

    engine = create_async_engine(args.runtime_url)
    driver = PostgresExecutionDriver(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        lease_seconds=1.0,
    )
    worker_id = f"inference-crash-{args.stop_after}"
    try:
        async with production_inference_lifespan(
            app_env=os.environ.get("GROVE_WS6_APP_ENV", "production"),
            runtime_build_hash=args.build_hash,
        ) as (port, request_factory):
            worker = RuntimeWorker(
                driver=driver,
                tenant_id=args.tenant,
                worker_id=worker_id,
                runtime_build_hash=args.build_hash,
                database_url=args.runtime_url.replace("postgresql+psycopg://", "postgresql+asyncpg://"),
                inference_port=port,
                inference_request_factory=request_factory,
            )
            claim = await driver.claim(
                worker_id=worker_id,
                runtime_build_hash=args.build_hash,
                tenant_id=args.tenant,
                lease_seconds=1.0,
            )
            if claim is None or claim.run_id != args.run_id:
                raise RuntimeError("subprocess did not claim the expected run")
            _ready("claim", fence=claim.execution_fence)
            if args.stop_after == "claim":
                await asyncio.Event().wait()

            # Real inference outlives the 1s crash lease; renew immediately
            # (before expiry, exactly like the production worker does ahead of
            # a long invoke) so the fenced checkpoint write stays authorized.
            claim = await driver.heartbeat(claim, lease_seconds=60.0)

            # Mirror RuntimeWorker._invoke_inference step by step so the
            # checkpoint boundary sits between the fenced write and delivery.
            if worker._inference_graph is None:
                worker._inference_graph = build_inference_graph(port, request_factory)
            input_hash = compute_inference_input_hash(claim.tenant_id, claim.run_id)
            terminal = await worker._inference_graph.ainvoke(
                {
                    "stage": "start",
                    "tenant_id": claim.tenant_id,
                    "run_id": str(claim.run_id),
                    "input_hash": input_hash,
                }
            )
            await worker._write_checkpoint(claim, dict(terminal))
            _ready("checkpoint", fence=claim.execution_fence, tokens=terminal["total_tokens"])
            if args.stop_after == "checkpoint":
                await asyncio.Event().wait()

            occurred = datetime.now(UTC)
            payload = {
                "outcome_kind": "terminal",
                "input_hash": input_hash,
                "value": terminal["total_tokens"],
            }
            events = [
                build_node_executed_emit_request(
                    run_id=claim.run_id,
                    command_seq=claim.command_seq,
                    node_id="infer",
                    stage="terminal",
                    input_hash=input_hash,
                    value=terminal["total_tokens"],
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
            receipt = await driver.finish_delivery(
                claim,
                outcome_kind="terminal",
                events=events,
            )
            _ready("finish", fence=claim.execution_fence, status=receipt.status, digest=canonical_hash(payload))
            await asyncio.Event().wait()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
