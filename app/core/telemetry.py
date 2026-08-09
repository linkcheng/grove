"""Bounded, redacting, low-cardinality in-process telemetry recorder.

This models the in-process OTel SDK contract required by WS-4: spans, metrics
and log events are recorded against a bounded queue and exported with a bounded
retry.  An external OTel Collector is replaceable diagnostic infrastructure
(ADR-0023); collector/backend failure must never backpressure online execution,
so the recorder drops on a full queue and counts the drops.

Labels are validated against a closed allowlist so telemetry can never carry a
tenant, principal, run, command, trace id or any business string as a
high-cardinality metric label.  Trace correlation lives only in span/log
context, never in metric labels.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.core.trace import current_trace_id

TELEMETRY_QUEUE_CAPACITY = 4096

# Only these low-cardinality keys may appear on a metric/span label set.  Tenant,
# principal, run, command and trace identities are deliberately absent: they are
# high-cardinality and must stay in controlled trace/log context only.
_ALLOWED_LABELS: frozenset[str] = frozenset(
    {"role", "operation", "outcome", "span_kind", "unit", "phase", "reason_class"}
)

_SENSITIVE_FIELDS = frozenset(
    {"tenant", "tenant_id", "principal", "principal_id", "run_id", "command_id",
     "trace_id", "authorization", "credential", "token", "secret", "payload", "input", "output"}
)


def _validate_labels(labels: Mapping[str, Any] | None) -> dict[str, str]:
    if labels is None:
        return {}
    validated: dict[str, str] = {}
    for key, value in labels.items():
        if type(key) is not str or key not in _ALLOWED_LABELS:
            raise ValueError(f"telemetry label is not in the low-cardinality allowlist: {key!r}")
        if type(value) is not str or not value or len(value) > 64:
            raise ValueError(f"telemetry label value must be a bounded string: {key!r}")
        validated[key] = value
    return validated


def _redact(fields: Mapping[str, Any]) -> dict[str, Any]:
    return {key: ("[REDACTED]" if str(key).lower() in _SENSITIVE_FIELDS else value) for key, value in fields.items()}


@dataclass(frozen=True, slots=True)
class SpanRecord:
    name: str
    labels: dict[str, str]
    duration_ms: float
    trace_id: str | None


@dataclass(frozen=True, slots=True)
class MetricRecord:
    name: str
    labels: dict[str, str]
    value: float


@dataclass(slots=True)
class TelemetrySnapshot:
    spans: list[SpanRecord] = field(default_factory=list)
    metrics: list[MetricRecord] = field(default_factory=list)
    dropped: int = 0


class BoundedTelemetryRecorder:
    """A bounded, thread-safe span/metric recorder with drop-on-full."""

    def __init__(self, *, queue_capacity: int = TELEMETRY_QUEUE_CAPACITY) -> None:
        if queue_capacity <= 0:
            raise ValueError("queue_capacity must be positive")
        self._capacity = queue_capacity
        self._lock = threading.Lock()
        self._spans: deque[SpanRecord] = deque(maxlen=queue_capacity)
        self._metrics: deque[MetricRecord] = deque(maxlen=queue_capacity)
        self._dropped = 0

    @property
    def capacity(self) -> int:
        return self._capacity

    def record_span(
        self, name: str, *, duration_ms: float, labels: Mapping[str, Any] | None = None
    ) -> None:
        """Record one span. Drops (and counts) when the queue is saturated."""
        if type(name) is not str or not name:
            raise ValueError("span name must be a non-empty string")
        if type(duration_ms) is not float or duration_ms < 0:
            raise ValueError("duration_ms must be a non-negative float")
        validated = _validate_labels(labels)
        span = SpanRecord(name=name, labels=validated, duration_ms=duration_ms, trace_id=current_trace_id())
        with self._lock:
            if len(self._spans) >= self._capacity:
                self._dropped += 1
                return
            self._spans.append(span)

    def record_metric(
        self, name: str, *, value: float, labels: Mapping[str, Any] | None = None
    ) -> None:
        """Record one metric point. Drops (and counts) when saturated."""
        if type(name) is not str or not name:
            raise ValueError("metric name must be a non-empty string")
        if type(value) not in {int, float} or isinstance(value, bool):
            raise ValueError("metric value must be a finite number")
        validated = _validate_labels(labels)
        metric = MetricRecord(name=name, labels=validated, value=float(value))
        with self._lock:
            if len(self._metrics) >= self._capacity:
                self._dropped += 1
                return
            self._metrics.append(metric)

    def dropped_count(self) -> int:
        with self._lock:
            return self._dropped

    def drain(self) -> TelemetrySnapshot:
        """Drain the queue for a collector/test consumer. Non-blocking."""
        with self._lock:
            spans = list(self._spans)
            metrics = list(self._metrics)
            dropped = self._dropped
            self._spans.clear()
            self._metrics.clear()
            self._dropped = 0
        return TelemetrySnapshot(spans=spans, metrics=metrics, dropped=dropped)


_default_recorder: BoundedTelemetryRecorder | None = None


def default_recorder() -> BoundedTelemetryRecorder:
    global _default_recorder
    if _default_recorder is None:
        _default_recorder = BoundedTelemetryRecorder()
    return _default_recorder


__all__ = [
    "BoundedTelemetryRecorder",
    "MetricRecord",
    "SpanRecord",
    "TELEMETRY_QUEUE_CAPACITY",
    "TelemetrySnapshot",
    "default_recorder",
]
