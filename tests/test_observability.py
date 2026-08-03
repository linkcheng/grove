from __future__ import annotations

import io
import json
import logging
import queue

from app.core.observability import NonBlockingQueueHandler, configure_logging, log_event
from app.core.trace import trace_id_context


def test_structured_logging_adds_context_and_redacts_sensitive_fields() -> None:
    stream = io.StringIO()
    runtime = configure_logging("INFO", role="api", stream=stream)
    token = trace_id_context.set("trace_123")
    try:
        log_event(
            "request_complete",
            route="/api/v1/items/{item_id}",
            path="/items/private-value",
            password="do-not-log",  # noqa: S106 - synthetic redaction fixture
            status=200,
        )
    finally:
        trace_id_context.reset(token)
        runtime.stop()

    event = json.loads(stream.getvalue())
    assert event["event"] == "request_complete"
    assert event["trace_id"] == "trace_123"
    assert event["role"] == "api"
    assert event["route"] == "/api/v1/items/{item_id}"
    assert "path" not in event
    assert event["password"] == "[REDACTED]"  # noqa: S105 - expected redaction marker


def test_standard_library_logs_share_the_json_pipeline() -> None:
    stream = io.StringIO()
    runtime = configure_logging("INFO", role="api", stream=stream)
    try:
        logging.getLogger("alembic.runtime.migration").info("migration complete")
    finally:
        runtime.stop()

    event = json.loads(stream.getvalue())
    assert event["event"] == "migration complete"
    assert event["logger"] == "alembic.runtime.migration"
    assert event["role"] == "api"


def test_bounded_queue_drops_without_blocking_when_full() -> None:
    records: queue.Queue[logging.LogRecord] = queue.Queue(maxsize=1)
    handler = NonBlockingQueueHandler(records)
    record = logging.LogRecord("grove", logging.INFO, __file__, 1, "event", (), None)

    handler.emit(record)
    handler.emit(record)

    assert records.qsize() == 1
    assert handler.dropped_count == 1
