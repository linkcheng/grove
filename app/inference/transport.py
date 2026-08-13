"""HTTP transport that observes and budgets every physical provider send."""

from __future__ import annotations

from typing import Any

import httpx

from app.inference.errors import InferenceError, InferenceErrorCode
from app.inference.ledger import current_invocation_budget


class LedgerTransport(httpx.AsyncBaseTransport):
    def __init__(self, delegate: httpx.AsyncBaseTransport) -> None:
        if not isinstance(delegate, httpx.AsyncBaseTransport):
            raise TypeError("delegate must be an async HTTP transport")
        self._delegate = delegate

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        budget = current_invocation_budget.get()
        if budget is None:
            raise InferenceError(InferenceErrorCode.INVALID_BINDING)
        await budget.reserve_send()
        response = await self._delegate.handle_async_request(request)
        await response.aread()
        input_tokens, output_tokens = _usage_tokens(response)
        await budget.record_usage(input_tokens, output_tokens)
        return response

    async def aclose(self) -> None:
        await self._delegate.aclose()


def _usage_tokens(response: httpx.Response) -> tuple[int, int]:
    try:
        payload: Any = response.json()
    except (ValueError, UnicodeDecodeError):
        return 0, 0
    if type(payload) is not dict or type(payload.get("usage")) is not dict:
        return 0, 0
    usage = payload["usage"]
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0))
    if type(input_tokens) is not int or input_tokens < 0 or type(output_tokens) is not int or output_tokens < 0:
        raise InferenceError(InferenceErrorCode.PROVIDER_PERMANENT)
    return input_tokens, output_tokens
