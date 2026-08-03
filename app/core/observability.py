"""Structured, secret-safe logging with non-blocking application writes."""

from __future__ import annotations

import copy
import logging
import logging.handlers
import queue
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, TextIO

import structlog

from app.core.trace import current_trace_id

LOG_QUEUE_CAPACITY = 4096
_DROP_FIELDS = frozenset({"path", "raw_path"})
_SENSITIVE_FIELDS = frozenset(
    {
        "api_key",
        "authorization",
        "body",
        "credential",
        "headers",
        "password",
        "prompt",
        "query",
        "refresh_token",
        "secret",
        "token",
        "url",
    }
)
_DATABASE_URL = re.compile(r"(postgresql(?:\+psycopg)?://[^:/@\s]+:)[^@\s]+(@)", re.IGNORECASE)
_MANAGED_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpx2", "httpcore", "httpcore2")
_URL_LOGGERS = frozenset({"httpx", "httpx2", "httpcore", "httpcore2"})
_active_runtime: LoggingRuntime | None = None


def _is_sensitive_field(name: str) -> bool:
    normalized = name.lower()
    return normalized in _SENSITIVE_FIELDS or normalized.endswith(("_password", "_secret", "_credential", "_token"))


def _sanitize_text(value: str) -> str:
    return _DATABASE_URL.sub(r"\1[REDACTED]\2", value)


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _is_sensitive_field(str(key)) else _sanitize_value(item)
            for key, item in value.items()
            if str(key).lower() not in _DROP_FIELDS
        }
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_value(item) for item in value)
    return value


def _add_trace_id(_logger: Any, _method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    trace_id = current_trace_id()
    if trace_id is not None:
        event_dict.setdefault("trace_id", trace_id)
    return event_dict


def _sanitize_event(_logger: Any, _method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for name in tuple(event_dict):
        if name.startswith("_"):
            continue
        if name.lower() in _DROP_FIELDS:
            event_dict.pop(name, None)
        elif _is_sensitive_field(name):
            event_dict[name] = "[REDACTED]"
        else:
            event_dict[name] = _sanitize_value(event_dict[name])
    return event_dict


@dataclass(frozen=True, slots=True)
class _AddProcessContext:
    role: str

    def __call__(self, _logger: Any, _method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        event_dict.setdefault("role", self.role)
        return event_dict


class NonBlockingQueueHandler(logging.handlers.QueueHandler):
    """Preserve structured records and drop instead of blocking when full."""

    def __init__(self, event_queue: queue.Queue[Any]) -> None:
        super().__init__(event_queue)
        self.dropped_count = 0

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        return copy.copy(record)

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.dropped_count += 1


class GracefulQueueListener(logging.handlers.QueueListener):
    """Allow shutdown to wait for queue space instead of raising queue.Full."""

    def __init__(
        self,
        event_queue: queue.Queue[Any],
        *handlers: logging.Handler,
        respect_handler_level: bool = False,
    ) -> None:
        self._event_queue = event_queue
        super().__init__(event_queue, *handlers, respect_handler_level=respect_handler_level)

    def enqueue_sentinel(self) -> None:
        self._event_queue.put(None)


@dataclass(slots=True)
class _LoggerState:
    logger: logging.Logger
    handlers: list[logging.Handler]
    level: int
    propagate: bool
    disabled: bool


@dataclass(slots=True)
class LoggingRuntime:
    """Own the queue listener and restore process logging on shutdown."""

    listener: GracefulQueueListener
    queue_handler: NonBlockingQueueHandler
    root_logger: logging.Logger
    previous_root_handlers: list[logging.Handler]
    previous_root_level: int
    managed_states: list[_LoggerState]
    level: str
    role: str
    _stopped: bool = field(default=False, init=False)

    def stop(self) -> None:
        global _active_runtime

        if self._stopped:
            return
        self.listener.stop()
        self.root_logger.handlers[:] = self.previous_root_handlers
        self.root_logger.setLevel(self.previous_root_level)
        for state in self.managed_states:
            state.logger.handlers[:] = state.handlers
            state.logger.setLevel(state.level)
            state.logger.propagate = state.propagate
            state.logger.disabled = state.disabled
        self._stopped = True
        if _active_runtime is self:
            _active_runtime = None


def active_logging_runtime() -> LoggingRuntime | None:
    return _active_runtime if _active_runtime is not None and not _active_runtime._stopped else None


def configure_logging(
    level: str,
    *,
    role: str,
    stream: TextIO | None = None,
    queue_capacity: int = LOG_QUEUE_CAPACITY,
) -> LoggingRuntime:
    """Configure one JSON pipeline for application and standard-library logs."""

    global _active_runtime

    if queue_capacity <= 0:
        raise ValueError("queue_capacity must be positive")
    normalized_level = level.upper()
    existing = active_logging_runtime()
    if existing is not None:
        if existing.level != normalized_level or existing.role != role:
            raise RuntimeError("logging is already configured for a different process context")
        return existing
    level_number = logging.getLevelNamesMapping()[normalized_level]
    shared_processors: list[Any] = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        _AddProcessContext(role),
        _add_trace_id,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _sanitize_event,
    ]
    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(sort_keys=True),
        ],
    )
    output_handler = logging.StreamHandler(stream or sys.stderr)
    output_handler.setLevel(level_number)
    output_handler.setFormatter(formatter)

    event_queue: queue.Queue[Any] = queue.Queue(maxsize=queue_capacity)
    queue_handler = NonBlockingQueueHandler(event_queue)
    queue_handler.setLevel(level_number)
    listener = GracefulQueueListener(event_queue, output_handler, respect_handler_level=True)

    root_logger = logging.getLogger()
    previous_root_handlers = list(root_logger.handlers)
    previous_root_level = root_logger.level
    root_logger.handlers[:] = [queue_handler]
    root_logger.setLevel(level_number)

    managed_states: list[_LoggerState] = []
    for name in _MANAGED_LOGGERS:
        managed = logging.getLogger(name)
        managed_states.append(
            _LoggerState(managed, list(managed.handlers), managed.level, managed.propagate, managed.disabled)
        )
        managed.handlers.clear()
        managed.setLevel(max(level_number, logging.WARNING) if name in _URL_LOGGERS else level_number)
        managed.propagate = True
        managed.disabled = name == "uvicorn.access" or name in _URL_LOGGERS

    listener.start()
    runtime = LoggingRuntime(
        listener=listener,
        queue_handler=queue_handler,
        root_logger=root_logger,
        previous_root_handlers=previous_root_handlers,
        previous_root_level=previous_root_level,
        managed_states=managed_states,
        level=normalized_level,
        role=role,
    )
    _active_runtime = runtime
    return runtime


_logger = structlog.get_logger("grove")


def log_event(event: str, **fields: Any) -> None:
    """Emit a stable event name plus structured, processor-sanitized fields."""

    _logger.info(event, **fields)
