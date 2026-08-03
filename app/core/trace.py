"""Request trace ID propagation through contextvars."""

from __future__ import annotations

import re
from contextvars import ContextVar
from uuid import uuid4

TRACE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
trace_id_context: ContextVar[str | None] = ContextVar("grove_trace_id", default=None)


def resolve_trace_id(upstream: str | None) -> str:
    """Accept a safe upstream ID or create a UUID4 hex ID."""

    if upstream is not None and TRACE_ID_PATTERN.fullmatch(upstream):
        return upstream
    return uuid4().hex


def current_trace_id() -> str | None:
    return trace_id_context.get()
