"""Unit tests for the runtime worker loop logic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from app.execution.contracts import ExecutionClaim
from app.worker.loop import RuntimeWorker


def _make_claim(seq: int = 0) -> ExecutionClaim:
    return ExecutionClaim(
        command_id=uuid4(),
        tenant_id="test-tenant",
        run_id=uuid4(),
        command_seq=seq,
        command_digest="a" * 64,
        runtime_build_hash="b" * 64,
        worker_id="test-worker",
        execution_fence=1,
        lease_until=datetime.now(UTC) + timedelta(seconds=30),
    )


@pytest.mark.asyncio
async def test_worker_shuts_down_cleanly() -> None:
    driver = MagicMock()
    driver.claim = AsyncMock(return_value=None)
    worker = RuntimeWorker(
        driver=driver,
        tenant_id="test-tenant",
        worker_id="test-worker",
        runtime_build_hash="b" * 64,
        database_url="postgresql://localhost/test",
        poll_interval=0.01,
    )
    worker.request_shutdown()
    await worker.run()
    driver.claim.assert_not_called()


@pytest.mark.asyncio
async def test_worker_processes_start_command_and_finishes_yield() -> None:
    claim = _make_claim(seq=0)
    driver = MagicMock()
    driver.claim = AsyncMock(return_value=claim)
    driver.heartbeat = AsyncMock(return_value=claim)
    driver.finish_delivery = AsyncMock(
        return_value=MagicMock(
            continue_command_id=uuid4(),
            run_revision=1,
            status="consumed",
        )
    )
    driver.dead_letter = AsyncMock()

    worker = RuntimeWorker(
        driver=driver,
        tenant_id="test-tenant",
        worker_id="test-worker",
        runtime_build_hash="b" * 64,
        database_url="postgresql://localhost/test",
        poll_interval=0.01,
    )

    with patch.object(worker, "_write_checkpoint", new_callable=AsyncMock):
        await worker._process_claim(claim)

    driver.finish_delivery.assert_called_once()
    call_kwargs = driver.finish_delivery.call_args
    assert call_kwargs.kwargs["outcome_kind"] == "yield"


@pytest.mark.asyncio
async def test_worker_processes_continue_command_and_finishes_terminal() -> None:
    claim = _make_claim(seq=1)
    driver = MagicMock()
    driver.claim = AsyncMock(return_value=claim)
    driver.heartbeat = AsyncMock(return_value=claim)
    driver.finish_delivery = AsyncMock(
        return_value=MagicMock(
            continue_command_id=None,
            run_revision=1,
            status="consumed",
        )
    )
    driver.dead_letter = AsyncMock()

    worker = RuntimeWorker(
        driver=driver,
        tenant_id="test-tenant",
        worker_id="test-worker",
        runtime_build_hash="b" * 64,
        database_url="postgresql://localhost/test",
        poll_interval=0.01,
    )

    with patch.object(worker, "_write_checkpoint", new_callable=AsyncMock):
        await worker._process_claim(claim)

    driver.finish_delivery.assert_called_once()
    call_kwargs = driver.finish_delivery.call_args
    assert call_kwargs.kwargs["outcome_kind"] == "terminal"


@pytest.mark.asyncio
async def test_worker_dead_letters_on_invoke_error() -> None:
    claim = _make_claim(seq=0)
    driver = MagicMock()
    driver.claim = AsyncMock(return_value=claim)
    driver.heartbeat = AsyncMock(return_value=claim)
    driver.finish_delivery = AsyncMock(side_effect=RuntimeError("boom"))
    driver.dead_letter = AsyncMock()

    worker = RuntimeWorker(
        driver=driver,
        tenant_id="test-tenant",
        worker_id="test-worker",
        runtime_build_hash="b" * 64,
        database_url="postgresql://localhost/test",
        poll_interval=0.01,
    )

    with patch.object(worker, "_write_checkpoint", new_callable=AsyncMock):
        await worker._process_claim(claim)

    driver.dead_letter.assert_called_once()
