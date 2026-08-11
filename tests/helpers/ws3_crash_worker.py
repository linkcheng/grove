"""Real subprocess used by the WS-3 crash-recovery integration matrix.

The parent test sends SIGKILL only after this process reports an exact durable
boundary.  Keeping the fault hook in a test helper avoids adding sleep/fault
switches to the production worker.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any, cast
from uuid import UUID

import psycopg
from app.contracts.canonical import canonical_hash
from app.execution import PostgresExecutionDriver
from app.execution.checkpoint import FencedPostgresSaver
from app.execution.conformance_graph import compute_input_hash, node_a
from langchain_core.runnables.config import RunnableConfig
from langgraph.checkpoint.base import ChannelVersions, CheckpointMetadata, empty_checkpoint
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def _ready(stage: str, **details: object) -> None:
    print(json.dumps({"stage": stage, **details}, sort_keys=True), flush=True)


async def _write_checkpoint(runtime_url: str, claim: Any) -> tuple[str, dict[str, object]]:
    input_hash = compute_input_hash("grove-conformance")
    state = node_a({"stage": "start", "input_hash": input_hash, "value": 0})
    conninfo = runtime_url.replace("postgresql+psycopg://", "postgresql://")
    async with await psycopg.AsyncConnection.connect(conninfo=conninfo) as connection:
        checkpoint = empty_checkpoint()
        versions: ChannelVersions = {key: str(value) for key, value in state.items()}
        checkpoint["channel_versions"] = versions
        checkpoint["channel_values"] = dict(state)
        config: dict[str, Any] = {"configurable": {"thread_id": str(claim.run_id), "checkpoint_ns": ""}}
        saver = FencedPostgresSaver(connection, claim)
        await saver.aput(
            cast(RunnableConfig, config),
            checkpoint,
            cast(CheckpointMetadata, {}),
            versions,
        )
        await connection.commit()
    return input_hash, {"outcome_kind": "yield", "input_hash": input_hash, "value": state["value"]}


async def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-url", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--run-id", required=True, type=UUID)
    parser.add_argument("--build-hash", required=True)
    parser.add_argument(
        "--stop-after",
        required=True,
        choices=("claim", "checkpoint", "finish"),
    )
    args = parser.parse_args()

    engine = create_async_engine(args.runtime_url)
    driver = PostgresExecutionDriver(
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
        lease_seconds=1.0,
    )
    try:
        claim = await driver.claim(
            worker_id=f"crash-process-{args.stop_after}",
            runtime_build_hash=args.build_hash,
            tenant_id=args.tenant,
            lease_seconds=1.0,
        )
        if claim is None or claim.run_id != args.run_id:
            raise RuntimeError("subprocess did not claim the expected run")
        _ready("claim", fence=claim.execution_fence, command_seq=claim.command_seq)
        if args.stop_after == "claim":
            await asyncio.Event().wait()

        input_hash, payload = await _write_checkpoint(args.runtime_url, claim)
        _ready("checkpoint", fence=claim.execution_fence, input_hash=input_hash)
        if args.stop_after == "checkpoint":
            await asyncio.Event().wait()

        payload_hash = canonical_hash(payload)
        receipt = await driver.finish_delivery(
            claim,
            outcome_kind="yield",
            continue_payload_ref=f"continue-payload:{payload_hash}",
            continue_payload_hash=payload_hash,
            continue_payload=payload,
        )
        _ready("finish", continue_command_id=str(receipt.continue_command_id))
        await asyncio.Event().wait()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
