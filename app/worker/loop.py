"""Bounded runtime worker loop: claim -> invoke -> checkpoint -> finish_delivery.

The worker is a non-HTTP internal role that consumes PostgreSQL claims.
It uses a fixed pure deterministic conformance graph, writes checkpoints
through FencedPostgresSaver, and atomically finalizes delivery via
grove_finish_delivery.  No provider, tool, model, or external IO.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import psycopg
from langgraph.checkpoint.base import CheckpointMetadata, empty_checkpoint

from app.contracts.canonical import canonical_hash
from app.execution import (
    ExecutionClaim,
    PostgresExecutionDriver,
    StaleExecutionFence,
)
from app.execution.checkpoint import FencedPostgresSaver
from app.execution.conformance_graph import (
    ConformanceState,
    compute_input_hash,
    node_a,
    node_b,
)
from app.observation.facts import build_lifecycle_emit_request, build_node_executed_emit_request

logger = logging.getLogger(__name__)

LEASE_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 0.5
LEASE_MARGIN_SECONDS = 10.0
INVOKE_BUDGET_SECONDS = 12.0
TOTAL_BUDGET_SECONDS = 15.0

CONFERENCE_INPUT = "grove-conformance"


class WorkerShutdown(Exception):
    """Raised to break the poll loop."""


class RuntimeWorker:
    """Single-tenant bounded poll loop backed by PostgreSQL claims."""

    def __init__(
        self,
        *,
        driver: PostgresExecutionDriver,
        tenant_id: str,
        worker_id: str,
        runtime_build_hash: str,
        database_url: str,
        poll_interval: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self._driver = driver
        self._tenant_id = tenant_id
        self._worker_id = worker_id
        self._runtime_build_hash = runtime_build_hash
        self._database_url = database_url
        self._poll_interval = poll_interval
        self._shutdown = asyncio.Event()

    def request_shutdown(self) -> None:
        self._shutdown.set()

    async def run(self) -> None:
        logger.info("worker.start worker_id=%s tenant=%s", self._worker_id, self._tenant_id)
        while not self._shutdown.is_set():
            try:
                claimed = await self._poll_once()
                if not claimed:
                    await self._sleep_or_shutdown()
            except WorkerShutdown:
                break
            except Exception:
                logger.exception("worker.iteration_error")
                await self._sleep_or_shutdown()
        logger.info("worker.stop worker_id=%s", self._worker_id)

    async def _sleep_or_shutdown(self) -> None:
        try:
            await asyncio.wait_for(self._shutdown.wait(), timeout=self._poll_interval)
            raise WorkerShutdown()
        except TimeoutError:
            pass

    async def _poll_once(self) -> bool:
        claim = await self._driver.claim(
            worker_id=self._worker_id,
            runtime_build_hash=self._runtime_build_hash,
            tenant_id=self._tenant_id,
            lease_seconds=LEASE_SECONDS,
        )
        if claim is None:
            return False
        await self._process_claim(claim)
        return True

    async def _process_claim(self, claim: ExecutionClaim) -> None:
        """Heartbeat if needed, invoke graph, write checkpoint, finish delivery."""
        is_start = claim.command_seq == 0
        now = datetime.now(UTC)
        remaining = claim.lease_until - now
        if remaining < timedelta(seconds=LEASE_MARGIN_SECONDS + INVOKE_BUDGET_SECONDS):
            claim = await self._driver.heartbeat(claim, lease_seconds=LEASE_SECONDS)

        try:
            async with asyncio.timeout(TOTAL_BUDGET_SECONDS):
                if is_start:
                    await self._invoke_start(claim)
                else:
                    await self._invoke_continue(claim)
        except StaleExecutionFence:
            logger.warning("worker.stale_fence run=%s seq=%d", claim.run_id, claim.command_seq)
        except TimeoutError:
            logger.error("worker.budget_exceeded run=%s seq=%d", claim.run_id, claim.command_seq)
            await self._safe_dead_letter(claim, "budget-exceeded")
        except Exception:
            logger.exception("worker.invoke_error run=%s seq=%d", claim.run_id, claim.command_seq)
            await self._safe_dead_letter(claim, "invoke-error")

    async def _safe_dead_letter(self, claim: ExecutionClaim, reason_ref: str) -> None:
        try:
            await self._driver.dead_letter(claim, reason_ref=reason_ref)
        except Exception:
            logger.exception("worker.dead_letter_failed run=%s", claim.run_id)

    async def _invoke_start(self, claim: ExecutionClaim) -> None:
        """First stage: node_a -> yield, write checkpoint, finish delivery(yield)."""
        input_hash = compute_input_hash(CONFERENCE_INPUT)
        yielded = node_a({"stage": "start", "input_hash": input_hash, "value": 0})

        await self._write_checkpoint(claim, yielded)

        payload = {
            "outcome_kind": "yield",
            "input_hash": input_hash,
            "value": yielded["value"],
        }
        payload_hash = canonical_hash(payload)
        payload_ref = f"continue-payload:{payload_hash}"
        occurred = datetime.now(UTC)
        events = [
            build_node_executed_emit_request(
                run_id=claim.run_id,
                command_seq=claim.command_seq,
                node_id="node_a",
                stage="start",
                input_hash=input_hash,
                value=yielded["value"],
                occurred_at=occurred,
            ),
            build_lifecycle_emit_request(
                run_id=claim.run_id,
                command_seq=claim.command_seq,
                status="running",
                run_revision=claim.command_seq + 1,
                occurred_at=occurred,
            ),
        ]
        receipt = await self._driver.finish_delivery(
            claim,
            outcome_kind="yield",
            continue_payload_ref=payload_ref,
            continue_payload_hash=payload_hash,
            continue_payload=payload,
            events=events,
        )
        logger.info(
            "worker.yield run=%s seq=%d continue=%s revision=%d",
            claim.run_id, claim.command_seq,
            receipt.continue_command_id, receipt.run_revision,
        )

    async def _invoke_continue(self, claim: ExecutionClaim) -> None:
        """Second stage: node_b -> terminal, write checkpoint, finish delivery(terminal)."""
        input_hash = compute_input_hash(CONFERENCE_INPUT)
        yielded = node_a({"stage": "start", "input_hash": input_hash, "value": 0})
        terminal = node_b(yielded)

        await self._write_checkpoint(claim, terminal)

        occurred = datetime.now(UTC)
        events = [
            build_node_executed_emit_request(
                run_id=claim.run_id,
                command_seq=claim.command_seq,
                node_id="node_b",
                stage="terminal",
                input_hash=input_hash,
                value=terminal["value"],
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
        receipt = await self._driver.finish_delivery(claim, outcome_kind="terminal", events=events)
        logger.info(
            "worker.terminal run=%s seq=%d status=%s",
            claim.run_id, claim.command_seq, receipt.status,
        )

    async def _write_checkpoint(self, claim: ExecutionClaim, state: ConformanceState) -> None:
        """Write one physical checkpoint through the production fenced saver."""
        conninfo = self._database_url.replace("postgresql+asyncpg://", "postgresql://")
        async with await psycopg.AsyncConnection.connect(conninfo=conninfo) as connection:
            checkpoint = empty_checkpoint()
            versions = {k: str(v) for k, v in state.items()}
            checkpoint["channel_versions"] = versions
            checkpoint["channel_values"] = dict(state)
            config: dict[str, Any] = {
                "configurable": {
                    "thread_id": str(claim.run_id),
                    "checkpoint_ns": "",
                }
            }
            saver = FencedPostgresSaver(connection, claim)
            await saver.aput(
                config,
                checkpoint,
                cast(CheckpointMetadata, {}),
                versions,
            )
            await connection.commit()


async def run_worker(
    *,
    driver: PostgresExecutionDriver,
    tenant_id: str,
    worker_id: str,
    runtime_build_hash: str,
    database_url: str,
    poll_interval: float = POLL_INTERVAL_SECONDS,
) -> None:
    """Run a bounded worker loop until SIGTERM/SIGINT."""
    worker = RuntimeWorker(
        driver=driver,
        tenant_id=tenant_id,
        worker_id=worker_id,
        runtime_build_hash=runtime_build_hash,
        database_url=database_url,
        poll_interval=poll_interval,
    )
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, worker.request_shutdown)
        except NotImplementedError:
            pass
    await worker.run()
