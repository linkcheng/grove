"""Bounded runtime worker loop: claim -> invoke -> checkpoint -> finish_delivery.

The worker is a non-HTTP internal role that consumes PostgreSQL claims.
It invokes the compiled pure deterministic conformance graph through the
LangGraph kernel (``graph.ainvoke``; node functions are never called
directly), writes checkpoints through FencedPostgresSaver, and atomically
finalizes delivery via grove_finish_delivery.  No provider, tool, model,
or external IO.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Any, cast
from uuid import UUID

import psycopg
from langchain_core.runnables.config import RunnableConfig
from langgraph.checkpoint.base import ChannelVersions, CheckpointMetadata, empty_checkpoint

from app.asset_risk.contracts import ASSET_STATE_VIEW_SCHEMA_REF
from app.asset_risk.kernel import AssetRiskKernel
from app.contracts.canonical import canonical_hash
from app.core.telemetry import record_operation
from app.execution import (
    ExecutionClaim,
    PostgresExecutionDriver,
    StaleExecutionFence,
)
from app.execution.checkpoint import FencedPostgresSaver
from app.execution.conformance_graph import (
    ConformanceState,
    build_conformance_graph,
    compute_input_hash,
)
from app.execution.graph_registry import GraphResolutionError, resolve_graph_kernel
from app.execution.inference_graph import (
    InferenceRequestFactory,
    InferenceState,
    build_inference_graph,
    compute_inference_input_hash,
)
from app.inference import TypedInferencePort
from app.observation.facts import (
    build_answer_message_emit_requests,
    build_domain_view_emit_request,
    build_lifecycle_emit_request,
    build_node_executed_emit_request,
)

logger = logging.getLogger(__name__)

LEASE_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 0.5
LEASE_MARGIN_SECONDS = 10.0
INVOKE_BUDGET_SECONDS = 12.0
TOTAL_BUDGET_SECONDS = 15.0
# Production (real-inference) sizing: one asset-risk answer may spend up to
# 1 + max_schema_retries full generations (flash chain: 3 x ~60s) inside the
# unsplittable invoke+checkpoint critical section.  The invariant
# invoke budget < lease - margin must hold; both constants move together.
PRODUCTION_INVOKE_BUDGET_SECONDS = 200.0
PRODUCTION_LEASE_SECONDS = 240.0

CONFERENCE_INPUT = "grove-conformance"


def _psycopg_conninfo(database_url: str) -> str:
    """Strip the SQLAlchemy driver prefix for a raw psycopg conninfo string.

    The production role settings use ``postgresql+psycopg://`` (the async
    engine form proven by the API role) while the E2E harness historically
    passed ``postgresql+asyncpg://``; the physical checkpoint connection is
    bare psycopg either way, so both driver prefixes normalize to plain
    ``postgresql://`` and anything else fails closed in psycopg parsing.
    """

    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg://"):
        if database_url.startswith(prefix):
            return "postgresql://" + database_url[len(prefix) :]
    return database_url


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
        inference_port: TypedInferencePort | None = None,
        inference_request_factory: InferenceRequestFactory | None = None,
        asset_risk_kernel: AssetRiskKernel | None = None,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        invoke_budget_seconds: float = TOTAL_BUDGET_SECONDS,
    ) -> None:
        self._driver = driver
        self._tenant_id = tenant_id
        self._worker_id = worker_id
        self._runtime_build_hash = runtime_build_hash
        self._database_url = database_url
        self._inference_port = inference_port
        self._inference_request_factory = inference_request_factory
        self._asset_risk_kernel = asset_risk_kernel
        self._poll_interval = poll_interval
        self._invoke_budget_seconds = invoke_budget_seconds
        # invoke+checkpoint is the unsplittable critical section, so every
        # claim and heartbeat renewal must carry a lease that covers the
        # budget plus the margin -- derived here so the invariant holds by
        # construction instead of depending on call-site constants agreeing
        # (the walkthrough takeover loop was exactly that disagreement).
        self._claim_lease_seconds = max(LEASE_SECONDS, invoke_budget_seconds + LEASE_MARGIN_SECONDS)
        self._shutdown = asyncio.Event()
        # Compiled once: the graph is pure and deterministic, so the same
        # compiled kernel serves every claim (per ADR-0001 LangGraph is the
        # only execution kernel; node functions are never invoked directly).
        self._graph = build_conformance_graph()
        self._inference_graph: Any | None = None

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
            lease_seconds=self._claim_lease_seconds,
        )
        if claim is None:
            return False
        await self._process_claim(claim)
        return True

    async def _process_claim(self, claim: ExecutionClaim) -> None:
        """Heartbeat if needed, invoke graph, write checkpoint, finish delivery."""
        try:
            kernel = resolve_graph_kernel(
                claim.graph_binding,
                inference_port=self._inference_port,
                inference_request_factory=self._inference_request_factory,
                asset_risk_kernel=self._asset_risk_kernel,
            )
        except GraphResolutionError as error:
            logger.error(
                "worker.graph_unresolved run=%s seq=%d reason=%s",
                claim.run_id,
                claim.command_seq,
                error.reason,
            )
            await self._safe_dead_letter(claim, f"graph-{error.reason}")
            return
        is_start = claim.command_seq == 0
        now = datetime.now(UTC)
        remaining = claim.lease_until - now
        if remaining < timedelta(seconds=LEASE_MARGIN_SECONDS + self._invoke_budget_seconds):
            claim = await self._driver.heartbeat(claim, lease_seconds=self._claim_lease_seconds)

        try:
            invoke_started = perf_counter()
            async with asyncio.timeout(self._invoke_budget_seconds):
                if kernel.kind == "inference":
                    await self._invoke_inference(claim)
                elif kernel.kind == "asset_risk":
                    await self._invoke_asset_risk(claim)
                elif is_start:
                    await self._invoke_start(claim)
                else:
                    await self._invoke_continue(claim)
            record_operation(
                "run.invoke",
                duration_ms=float((perf_counter() - invoke_started) * 1000),
                role="runtime_worker",
                operation="invoke",
                outcome="ok",
            )
        except StaleExecutionFence:
            record_operation(
                "run.invoke",
                duration_ms=float((perf_counter() - invoke_started) * 1000),
                role="runtime_worker",
                operation="invoke",
                outcome="stale",
            )
            logger.warning("worker.stale_fence run=%s seq=%d", claim.run_id, claim.command_seq)
        except TimeoutError:
            record_operation(
                "run.invoke",
                duration_ms=float((perf_counter() - invoke_started) * 1000),
                role="runtime_worker",
                operation="invoke",
                outcome="error",
            )
            logger.error("worker.budget_exceeded run=%s seq=%d", claim.run_id, claim.command_seq)
            await self._safe_dead_letter(claim, "budget-exceeded")
        except Exception:
            record_operation(
                "run.invoke",
                duration_ms=float((perf_counter() - invoke_started) * 1000),
                role="runtime_worker",
                operation="invoke",
                outcome="error",
            )
            logger.exception("worker.invoke_error run=%s seq=%d", claim.run_id, claim.command_seq)
            await self._safe_dead_letter(claim, "invoke-error")

    async def _safe_dead_letter(self, claim: ExecutionClaim, reason_ref: str) -> None:
        try:
            await self._driver.dead_letter(claim, reason_ref=reason_ref)
        except Exception:
            logger.exception("worker.dead_letter_failed run=%s", claim.run_id)

    async def _run_stage(self, state: ConformanceState) -> ConformanceState:
        """Execute exactly one stage through the compiled deterministic kernel."""
        result = await self._graph.ainvoke(dict(state))
        return cast(ConformanceState, result)

    async def _invoke_start(self, claim: ExecutionClaim) -> None:
        """First stage: node_a -> yield, write checkpoint, finish delivery(yield)."""
        input_hash = compute_input_hash(CONFERENCE_INPUT)
        node_started = perf_counter()
        yielded = await self._run_stage({"stage": "start", "input_hash": input_hash, "value": 0})
        record_operation(
            "graph.node",
            duration_ms=float((perf_counter() - node_started) * 1000),
            role="runtime_worker",
            operation="node_a",
            outcome="ok",
        )

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
            claim.run_id,
            claim.command_seq,
            receipt.continue_command_id,
            receipt.run_revision,
        )

    async def _invoke_continue(self, claim: ExecutionClaim) -> None:
        """Second stage: node_b -> terminal, write checkpoint, finish delivery(terminal)."""
        input_hash = compute_input_hash(CONFERENCE_INPUT)
        yielded = await self._run_stage({"stage": "start", "input_hash": input_hash, "value": 0})
        node_started = perf_counter()
        terminal = await self._run_stage(yielded)
        record_operation(
            "graph.node",
            duration_ms=float((perf_counter() - node_started) * 1000),
            role="runtime_worker",
            operation="node_b",
            outcome="ok",
        )

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
            claim.run_id,
            claim.command_seq,
            receipt.status,
        )

    async def _invoke_asset_risk(self, claim: ExecutionClaim) -> None:
        """AssetRisk kernel: input source -> graph -> checkpoint -> terminal."""
        if self._asset_risk_kernel is None:
            raise RuntimeError("asset-risk kernel resolved without composition")
        graph: Any = self._asset_risk_kernel.build_graph()
        # Recovery (docs/31 §4 / POC-M 4): when a prior attempt already
        # checkpointed the accepted view, resume from those channel values --
        # the physical asset read and the portfolio enumeration both stay at
        # zero database calls; only an un-checkpointed attempt may re-read.
        resumed = await self._load_prior_asset_risk_state(claim)
        if resumed is not None:
            state = {**resumed, "tenant_id": claim.tenant_id, "run_id": str(claim.run_id)}
        else:
            asset_refs = await self._asset_risk_kernel.input_source.asset_refs(claim.tenant_id, claim.run_id)
            state = {
                "stage": "start",
                "tenant_id": claim.tenant_id,
                "run_id": str(claim.run_id),
                "asset_refs": asset_refs,
            }
        node_started = perf_counter()
        terminal = await graph.ainvoke(state)
        record_operation(
            "graph.node",
            duration_ms=float((perf_counter() - node_started) * 1000),
            role="runtime_worker",
            operation="asset_risk_skill",
            outcome="ok" if terminal.get("stage") == "terminal" else "failed",
        )
        if terminal.get("stage") != "terminal":
            failure_class = str(terminal.get("failure_class", "skill_failed"))
            await self._safe_dead_letter(claim, f"asset-risk.{failure_class}")
            return
        await self._write_checkpoint(claim, dict(terminal))
        occurred = datetime.now(UTC)
        report = terminal.get("report", {})
        answer_text = report.get("answer")
        # Exact-type check before the emit seam: a coerced str() here would
        # let a malformed checkpoint value masquerade as a gated answer.
        if type(answer_text) is not str or not answer_text:
            raise ValueError("terminal asset-risk report must carry the gated answer text")
        asset_view = terminal["asset_view"]
        asset_provenance = terminal["asset_provenance"]
        # The accepted, checkpointed typed read view becomes a runtime fact in
        # the same terminal transaction; the projection turns it into the UI
        # domain-view milestone.  Strict extraction: a terminal state without
        # the accepted view is a contract violation and fails loudly here
        # rather than silently skipping the milestone.
        events = [
            build_domain_view_emit_request(
                run_id=claim.run_id,
                command_seq=claim.command_seq,
                tool_request_id=UUID(str(asset_view["tool_request_id"])),
                view_schema_ref=ASSET_STATE_VIEW_SCHEMA_REF,
                observed_at=datetime.fromisoformat(str(asset_view["observed_at"])),
                source_ref=str(asset_provenance["source_ref"]),
                result_hash=str(asset_provenance["result_content_hash"]),
                item_count=len(asset_view["assets"]),
                occurred_at=occurred,
            ),
            # The gated typed answer becomes one run-visible assistant
            # message (started/deltas/completed facts) so the UI projection
            # can present the report text with its content hash (WS-7).
            *build_answer_message_emit_requests(
                run_id=claim.run_id,
                command_seq=claim.command_seq,
                answer=answer_text,
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
        receipt = await self._driver.finish_delivery(claim, outcome_kind="terminal", events=events)
        logger.info(
            "worker.asset_risk_terminal run=%s seq=%d status=%s",
            claim.run_id,
            claim.command_seq,
            receipt.status,
        )

    async def _invoke_inference(self, claim: ExecutionClaim) -> None:
        """Inference kernel: infer node -> terminal, checkpoint, finish delivery."""
        if self._inference_graph is None:
            if self._inference_port is None or self._inference_request_factory is None:
                raise RuntimeError("inference kernel resolved without a production port")
            self._inference_graph = build_inference_graph(self._inference_port, self._inference_request_factory)
        input_hash = compute_inference_input_hash(claim.tenant_id, claim.run_id)
        state: InferenceState = {
            "stage": "start",
            "tenant_id": claim.tenant_id,
            "run_id": str(claim.run_id),
            "input_hash": input_hash,
        }
        node_started = perf_counter()
        terminal = cast(InferenceState, await self._inference_graph.ainvoke(dict(state)))
        record_operation(
            "graph.node",
            duration_ms=float((perf_counter() - node_started) * 1000),
            role="runtime_worker",
            operation="infer",
            outcome="ok",
        )

        await self._write_checkpoint(claim, dict(terminal))

        occurred = datetime.now(UTC)
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
        receipt = await self._driver.finish_delivery(claim, outcome_kind="terminal", events=events)
        logger.info(
            "worker.inference_terminal run=%s seq=%d attempts=%d tokens=%d status=%s",
            claim.run_id,
            claim.command_seq,
            terminal["provider_attempts"],
            terminal["total_tokens"],
            receipt.status,
        )

    async def _load_prior_asset_risk_state(self, claim: ExecutionClaim) -> dict[str, Any] | None:
        """Read the prior attempt's checkpointed asset-risk state, if any.

        Only a state that already carries the accepted asset view counts as
        resumable; anything earlier (or absent) restarts the run from the
        input source, which is the bounded physical re-read path.
        """

        conninfo = _psycopg_conninfo(self._database_url)
        async with await psycopg.AsyncConnection.connect(conninfo=conninfo) as connection:
            config = RunnableConfig(configurable={"thread_id": str(claim.run_id), "checkpoint_ns": ""})
            saver = FencedPostgresSaver(connection, claim)
            found = await saver.aget_tuple(config)
            if found is None:
                return None
            values = dict(found.checkpoint.get("channel_values", {})) if found.checkpoint else {}
            if "asset_view" not in values:
                return None
            return values

    async def _write_checkpoint(self, claim: ExecutionClaim, state: Mapping[str, object]) -> None:
        """Write one physical checkpoint through the production fenced saver."""
        conninfo = _psycopg_conninfo(self._database_url)
        async with await psycopg.AsyncConnection.connect(conninfo=conninfo) as connection:
            checkpoint = empty_checkpoint()
            versions: ChannelVersions = {k: str(v) for k, v in state.items()}
            checkpoint["channel_versions"] = versions
            checkpoint["channel_values"] = dict(state)
            config = RunnableConfig(
                configurable={
                    "thread_id": str(claim.run_id),
                    "checkpoint_ns": "",
                }
            )
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
    inference_port: TypedInferencePort | None = None,
    inference_request_factory: InferenceRequestFactory | None = None,
    asset_risk_kernel: AssetRiskKernel | None = None,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    invoke_budget_seconds: float = TOTAL_BUDGET_SECONDS,
) -> None:
    """Run a bounded worker loop until SIGTERM/SIGINT."""
    worker = RuntimeWorker(
        driver=driver,
        tenant_id=tenant_id,
        worker_id=worker_id,
        runtime_build_hash=runtime_build_hash,
        database_url=database_url,
        inference_port=inference_port,
        inference_request_factory=inference_request_factory,
        asset_risk_kernel=asset_risk_kernel,
        poll_interval=poll_interval,
        invoke_budget_seconds=invoke_budget_seconds,
    )
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, worker.request_shutdown)
        except NotImplementedError:
            pass
    await worker.run()
