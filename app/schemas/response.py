"""Unified transport and business response schemas."""

from __future__ import annotations

from typing import Any, Generic, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, model_serializer

from app.core.trace import current_trace_id

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    model_config = ConfigDict(extra="forbid")

    code: int
    message: str = Field(min_length=1, max_length=256)
    data: T | None
    trace_id: str | None = Field(default=None, min_length=1, max_length=128)
    error_code: str | None = Field(default=None, min_length=1, max_length=128)
    correlation_id: str | None = Field(default=None, min_length=1, max_length=128)
    field_violations: list[dict[str, str]] | None = None
    retry_after: int | None = Field(default=None, ge=1)

    @model_serializer(mode="wrap")
    def serialize_response(self, handler: Any) -> dict[str, Any]:
        # Keep the historical success envelope compact while retaining a
        # stable error envelope (including ``data: null``) for failures.
        data = cast(dict[str, Any], handler(self))
        if self.error_code is None:
            for field in ("error_code", "correlation_id", "field_violations", "retry_after"):
                data.pop(field, None)
        else:
            # Optional diagnostic fields are emitted only when present.  The
            # stable error envelope still carries ``data: null``, the machine
            # readable code and the correlation/trace identifier.
            for field in ("field_violations", "retry_after"):
                if data.get(field) is None:
                    data.pop(field, None)
        return data


def ok(data: T, message: str = "success") -> ApiResponse[T]:
    return ApiResponse(code=0, message=message, data=data, trace_id=current_trace_id())


def fail(
    code: int,
    message: str,
    trace_id: str | None = None,
    *,
    error_code: str | None = None,
    field_violations: list[dict[str, str]] | None = None,
    retry_after: int | None = None,
) -> ApiResponse[None]:
    correlation_id = trace_id or current_trace_id()
    return ApiResponse(
        code=code,
        message=message,
        data=None,
        trace_id=correlation_id,
        error_code=error_code,
        correlation_id=correlation_id,
        field_violations=field_violations,
        retry_after=retry_after,
    )
