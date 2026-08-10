"""Bounded OTLP exporter adapter: drain the recorder into OpenTelemetry SDK.

The in-process ``BoundedTelemetryRecorder`` owns the low-cardinality, redacted
span/metric contract.  This module is the single seam that bridges it to a real
OTel SDK tracer backed by an ``OTLPSpanExporter``.  Collector/backend failure
must never backpressure online execution (N-29, WS-4 Exit Invariant 5/7), so:

* export runs best-effort and never raises to the caller;
* the SDK ``BatchSpanProcessor`` provides a bounded queue with drop-on-full;
* if no OTLP endpoint is configured, export is a no-op (diagnostic-only).

Telemetry never carries tenant/principal/run/command/trace as metric labels;
those stay in controlled span/log context only.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from app.core.telemetry import BoundedTelemetryRecorder, TelemetrySnapshot, default_recorder

logger = logging.getLogger(__name__)

EXPORT_QUEUE_CAPACITY = 2048
EXPORT_MAX_RETRIES = 3
EXPORT_TIMEOUT_SECONDS = 5.0


class TelemetryPolicy:
    """Static policy capturing the OTel export configuration.

    Attributes are low-cardinality and validated at construction.  An empty
    endpoint disables export entirely (diagnostic-only), which is the default
    for unit/integration runs without a Collector.
    """

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        queue_capacity: int = EXPORT_QUEUE_CAPACITY,
        max_retries: int = EXPORT_MAX_RETRIES,
        timeout_seconds: float = EXPORT_TIMEOUT_SECONDS,
    ) -> None:
        if type(queue_capacity) is not int or queue_capacity <= 0:
            raise ValueError("queue_capacity must be a positive int")
        if type(max_retries) is not int or max_retries < 0:
            raise ValueError("max_retries must be a non-negative int")
        if type(timeout_seconds) not in {int, float} or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a positive number")
        resolved = endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
        if type(resolved) is not str:
            raise ValueError("endpoint must be a string")
        self._endpoint = resolved
        self._queue_capacity = queue_capacity
        self._max_retries = max_retries
        self._timeout_seconds = float(timeout_seconds)

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def enabled(self) -> bool:
        return bool(self._endpoint)

    @property
    def queue_capacity(self) -> int:
        return self._queue_capacity

    @property
    def max_retries(self) -> int:
        return self._max_retries

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds


class OTLPExporter:
    """Drain the bounded recorder into OpenTelemetry SDK spans.

    Export is best-effort and non-blocking.  Collector unavailability is logged
    at debug level and counted, never raised.  Callers (API request, worker
    claim, checkpoint, projector apply, SSE backfill) never block on telemetry.
    """

    def __init__(
        self,
        recorder: BoundedTelemetryRecorder | None = None,
        policy: TelemetryPolicy | None = None,
    ) -> None:
        self._recorder = recorder or default_recorder()
        self._policy = policy or TelemetryPolicy()
        self._lock = threading.Lock()
        self._exported_count = 0
        self._dropped_count = 0
        self._failed_count = 0
        self._tracer = self._build_tracer()

    def _build_tracer(self) -> Any:
        """Build an OTel tracer backed by a BatchSpanProcessor + OTLP exporter.

        Returns None when export is disabled or the SDK/exporter is unavailable,
        making telemetry diagnostic-only without failing the caller.
        """

        if not self._policy.enabled:
            return None
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
        except Exception:
            logger.debug("OTLP exporter unavailable; telemetry is diagnostic-only")
            return None
        try:
            resource = Resource.create({"service.name": "grove"})
            provider = TracerProvider(resource=resource)
            exporter = OTLPSpanExporter(endpoint=self._policy.endpoint, timeout=int(self._policy.timeout_seconds))
            # BatchSpanProcessor provides a bounded queue with drop-on-full and
            # bounded retry, satisfying the N-29 no-backpressure contract.
            provider.add_span_processor(
                BatchSpanProcessor(
                    exporter,
                    max_queue_size=self._policy.queue_capacity,
                    max_export_batch_size=min(512, self._policy.queue_capacity),
                    export_timeout_millis=int(self._policy.timeout_seconds * 1000),
                )
            )
            return provider.get_tracer("grove")
        except Exception:
            logger.debug("OTLP tracer construction failed; telemetry is diagnostic-only")
            return None

    def export(self, snapshot: TelemetrySnapshot) -> None:
        """Export one drained snapshot; best-effort, never raises."""

        if not self._policy.enabled or self._tracer is None:
            with self._lock:
                self._dropped_count += len(snapshot.spans) + len(snapshot.metrics)
            return
        for span in snapshot.spans:
            try:
                tracer = self._tracer
                attributes = {f"grove.{k}": v for k, v in span.labels.items()}
                with tracer.start_as_current_span(span.name, attributes=attributes) as otel_span:
                    otel_span.set_attribute("grove.duration_ms", span.duration_ms)
                with self._lock:
                    self._exported_count += 1
            except Exception:
                logger.debug("OTLP span export failed; telemetry drop counted")
                with self._lock:
                    self._failed_count += 1

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "exported": self._exported_count,
                "dropped": self._dropped_count,
                "failed": self._failed_count,
            }


__all__ = [
    "EXPORT_MAX_RETRIES",
    "EXPORT_QUEUE_CAPACITY",
    "EXPORT_TIMEOUT_SECONDS",
    "OTLPExporter",
    "TelemetryPolicy",
]
