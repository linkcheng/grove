"""One physical-request budget shared by all provider/schema retries."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from math import ceil
from time import monotonic

from app.inference.errors import InferenceError, InferenceErrorCode


@dataclass(slots=True)
class InvocationBudget:
    max_attempts: int
    max_tokens: int
    deadline_ms: int
    max_cost_micros: int
    base_cost_micros: int
    input_micros_per_million: int
    output_micros_per_million: int
    physical_sends: int = field(init=False, default=0)
    token_cost_micros: int = field(init=False, default=0)
    input_tokens: int = field(init=False, default=0)
    output_tokens: int = field(init=False, default=0)
    _base_spend_micros: int = field(init=False, default=0)
    _base_price_micros: int = field(init=False, default=0)
    _started: float = field(init=False, default_factory=monotonic)
    _lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        for name in (
            "max_attempts",
            "max_tokens",
            "deadline_ms",
            "max_cost_micros",
            "base_cost_micros",
            "input_micros_per_million",
            "output_micros_per_million",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < (1 if name in {"max_attempts", "max_tokens", "deadline_ms"} else 0):
                raise TypeError(f"{name} has an invalid exact integer value")
        self._base_price_micros = self.base_cost_micros
        self.base_cost_micros = 0

    @property
    def total_cost_micros(self) -> int:
        return self.base_cost_micros + self.token_cost_micros

    @property
    def elapsed_seconds(self) -> float:
        return monotonic() - self._started

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline_ms / 1000 - self.elapsed_seconds)

    async def reserve_send(self) -> None:
        async with self._lock:
            if monotonic() - self._started >= self.deadline_ms / 1000:
                raise InferenceError(InferenceErrorCode.DEADLINE_EXCEEDED)
            if self.physical_sends >= self.max_attempts:
                raise InferenceError(InferenceErrorCode.BUDGET_EXHAUSTED)
            if self.input_tokens + self.output_tokens >= self.max_tokens:
                raise InferenceError(InferenceErrorCode.BUDGET_EXHAUSTED)
            next_cost = self.total_cost_micros + self._base_price_micros
            if next_cost > self.max_cost_micros:
                raise InferenceError(InferenceErrorCode.BUDGET_EXHAUSTED)
            self.physical_sends += 1
            self._base_spend_micros += self._base_price_micros
            self.base_cost_micros = self._base_spend_micros

    async def record_usage(self, input_tokens: int, output_tokens: int) -> None:
        if type(input_tokens) is not int or input_tokens < 0 or type(output_tokens) is not int or output_tokens < 0:
            raise InferenceError(InferenceErrorCode.PROVIDER_PERMANENT)
        async with self._lock:
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            if self.input_tokens + self.output_tokens > self.max_tokens:
                raise InferenceError(InferenceErrorCode.BUDGET_EXHAUSTED)
            numerator = (
                self.input_tokens * self.input_micros_per_million + self.output_tokens * self.output_micros_per_million
            )
            self.token_cost_micros = ceil(numerator / 1_000_000)
            if self.total_cost_micros > self.max_cost_micros:
                raise InferenceError(InferenceErrorCode.BUDGET_EXHAUSTED)


current_invocation_budget: ContextVar[InvocationBudget | None] = ContextVar(
    "grove_inference_budget",
    default=None,
)
