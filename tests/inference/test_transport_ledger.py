from __future__ import annotations

import asyncio

import httpx
import pytest
from app.inference.errors import InferenceError
from app.inference.ledger import InvocationBudget, current_invocation_budget
from app.inference.transport import LedgerTransport


@pytest.mark.asyncio
async def test_transport_reserves_physical_sends_and_records_cost() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"usage": {"prompt_tokens": 3, "completion_tokens": 4}})

    delegate = httpx.MockTransport(handler)
    budget = InvocationBudget(
        max_attempts=2,
        max_tokens=100,
        deadline_ms=1000,
        max_cost_micros=1000,
        base_cost_micros=10,
        input_micros_per_million=100,
        output_micros_per_million=200,
    )
    transport = LedgerTransport(delegate)
    async with httpx.AsyncClient(transport=transport, base_url="https://gateway.example") as client:
        token = current_invocation_budget.set(budget)
        try:
            response = await client.get("/v1/models")
        finally:
            current_invocation_budget.reset(token)
    assert response.status_code == 200
    assert len(requests) == 1
    assert budget.physical_sends == 1
    assert budget.base_cost_micros == 10
    assert budget.token_cost_micros == 1  # ceil(3*100/1e6) + ceil(4*200/1e6)


@pytest.mark.asyncio
async def test_transport_exhaustion_does_not_delegate() -> None:
    sends = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sends
        sends += 1
        return httpx.Response(200)

    budget = InvocationBudget(
        max_attempts=1,
        max_tokens=100,
        deadline_ms=1000,
        max_cost_micros=100,
        base_cost_micros=1,
        input_micros_per_million=1,
        output_micros_per_million=1,
    )
    transport = LedgerTransport(httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=transport, base_url="https://gateway.example") as client:
        token = current_invocation_budget.set(budget)
        try:
            await client.get("/first")
            with pytest.raises(InferenceError):
                await client.get("/second")
        finally:
            current_invocation_budget.reset(token)
    assert sends == 1
    assert budget.physical_sends == 1


@pytest.mark.asyncio
async def test_cancelled_request_resets_context() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)
        return httpx.Response(200)

    budget = InvocationBudget(
        max_attempts=1,
        max_tokens=100,
        deadline_ms=1000,
        max_cost_micros=100,
        base_cost_micros=1,
        input_micros_per_million=1,
        output_micros_per_million=1,
    )
    transport = LedgerTransport(httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=transport, base_url="https://gateway.example") as client:

        async def run() -> None:
            token = current_invocation_budget.set(budget)
            try:
                await client.get("/cancel")
            finally:
                current_invocation_budget.reset(token)

        task = asyncio.create_task(run())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert current_invocation_budget.get() is None


@pytest.mark.asyncio
async def test_token_exhaustion_prevents_the_next_physical_send() -> None:
    sends = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sends
        sends += 1
        return httpx.Response(200, json={"usage": {"prompt_tokens": 6, "completion_tokens": 4}})

    budget = InvocationBudget(
        max_attempts=2,
        max_tokens=10,
        deadline_ms=1000,
        max_cost_micros=100,
        base_cost_micros=1,
        input_micros_per_million=1,
        output_micros_per_million=1,
    )
    transport = LedgerTransport(httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=transport, base_url="https://gateway.example") as client:
        token = current_invocation_budget.set(budget)
        try:
            await client.get("/first")
            with pytest.raises(InferenceError):
                await client.get("/second")
        finally:
            current_invocation_budget.reset(token)
    assert sends == budget.physical_sends == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_payload", [{}, {"usage": None}])
async def test_missing_provider_usage_fails_closed(provider_payload: object) -> None:
    sends = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sends
        sends += 1
        return httpx.Response(200, json=provider_payload)

    budget = InvocationBudget(
        max_attempts=1,
        max_tokens=100,
        deadline_ms=1000,
        max_cost_micros=100,
        base_cost_micros=1,
        input_micros_per_million=1,
        output_micros_per_million=1,
    )
    transport = LedgerTransport(httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=transport, base_url="https://gateway.example") as client:
        token = current_invocation_budget.set(budget)
        try:
            with pytest.raises(InferenceError):
                await client.get("/missing-usage")
        finally:
            current_invocation_budget.reset(token)
    assert sends == budget.physical_sends == 1


@pytest.mark.asyncio
async def test_local_preparation_time_does_not_consume_provider_deadline() -> None:
    sends = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sends
        sends += 1
        return httpx.Response(200, json={"usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    budget = InvocationBudget(
        max_attempts=1,
        max_tokens=100,
        deadline_ms=50,
        max_cost_micros=100,
        base_cost_micros=1,
        input_micros_per_million=1,
        output_micros_per_million=1,
    )
    await asyncio.sleep(0.12)
    transport = LedgerTransport(httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=transport, base_url="https://gateway.example") as client:
        token = current_invocation_budget.set(budget)
        try:
            response = await client.get("/after-local-prep")
        finally:
            current_invocation_budget.reset(token)
    assert response.status_code == 200
    assert sends == budget.physical_sends == 1


@pytest.mark.asyncio
async def test_deadline_starts_at_first_send_and_expires_afterwards() -> None:
    sends = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sends
        sends += 1
        await asyncio.sleep(0.06)
        return httpx.Response(200, json={"usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    budget = InvocationBudget(
        max_attempts=2,
        max_tokens=100,
        deadline_ms=50,
        max_cost_micros=100,
        base_cost_micros=1,
        input_micros_per_million=1,
        output_micros_per_million=1,
    )
    transport = LedgerTransport(httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=transport, base_url="https://gateway.example") as client:
        token = current_invocation_budget.set(budget)
        try:
            await client.get("/first-send")
            with pytest.raises(InferenceError):
                await client.get("/second-send-after-deadline")
        finally:
            current_invocation_budget.reset(token)
    assert sends == 1
