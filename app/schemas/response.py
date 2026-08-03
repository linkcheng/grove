"""Unified transport and business response schemas."""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from app.core.trace import current_trace_id

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    model_config = ConfigDict(extra="forbid")

    code: int
    message: str = Field(min_length=1, max_length=256)
    data: T | None
    trace_id: str | None = Field(default=None, min_length=1, max_length=128)


def ok(data: T, message: str = "success") -> ApiResponse[T]:
    return ApiResponse(code=0, message=message, data=data, trace_id=current_trace_id())


def fail(code: int, message: str, trace_id: str | None = None) -> ApiResponse[None]:
    return ApiResponse(code=code, message=message, data=None, trace_id=trace_id or current_trace_id())
