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
from time import monotonic
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.core.telemetry import BoundedTelemetryRecorder, TelemetrySnapshot, default_recorder

logger = logging.getLogger(__name__)

EXPORT_QUEUE_CAPACITY = 2048
EXPORT_MAX_RETRIES = 3
EXPORT_TIMEOUT_SECONDS = 5.0
EXPORT_INTERVAL_SECONDS = 5.0


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
        if resolved:
            parsed = urlsplit(resolved)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("endpoint must be a credential-free HTTP(S) URL")
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
        self._tracer_provider: Any = None
        self._meter_provider: Any = None
        self._tracer, self._meter = self._build_providers()
        self._instruments: dict[tuple[str, str], Any] = {}

    @property
    def enabled(self) -> bool:
        """Return whether a configured OTLP backend can be used."""

        return self._policy.enabled and self._tracer is not None and self._meter is not None

    def _build_providers(self) -> tuple[Any, Any]:
        """Build OTel trace and metric providers backed by bounded OTLP export.

        Returns ``(None, None)`` when disabled or the SDK is unavailable,
        making telemetry diagnostic-only without failing the caller.
        """

        if not self._policy.enabled:
            return None, None
        try:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
        except Exception:
            logger.debug("OTLP exporter unavailable; telemetry is diagnostic-only")
            return None, None
        try:
            resource = Resource.create({"service.name": "grove"})
            trace_provider = TracerProvider(resource=resource)
            trace_exporter = OTLPSpanExporter(
                endpoint=_signal_endpoint(self._policy.endpoint, "traces"),
                timeout=int(self._policy.timeout_seconds),
            )
            # BatchSpanProcessor provides a bounded queue with drop-on-full and
            # bounded retry, satisfying the N-29 no-backpressure contract.
            trace_provider.add_span_processor(
                BatchSpanProcessor(
                    trace_exporter,
                    max_queue_size=self._policy.queue_capacity,
                    max_export_batch_size=min(512, self._policy.queue_capacity),
                    export_timeout_millis=int(self._policy.timeout_seconds * 1000),
                )
            )
            metric_exporter = OTLPMetricExporter(
                endpoint=_signal_endpoint(self._policy.endpoint, "metrics"),
                timeout=int(self._policy.timeout_seconds),
            )
            metric_reader = PeriodicExportingMetricReader(
                metric_exporter,
                export_interval_millis=int(EXPORT_INTERVAL_SECONDS * 1000),
                export_timeout_millis=int(self._policy.timeout_seconds * 1000),
            )
            meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
            self._tracer_provider = trace_provider
            self._meter_provider = meter_provider
            return trace_provider.get_tracer("grove"), meter_provider.get_meter("grove")
        except Exception:
            logger.debug("OTLP provider construction failed; telemetry is diagnostic-only")
            return None, None

    def export(self, snapshot: TelemetrySnapshot) -> None:
        """Export one drained snapshot; best-effort, never raises."""

        if not self.enabled:
            with self._lock:
                self._dropped_count += len(snapshot.spans) + len(snapshot.metrics)
            return
        for span in snapshot.spans:
            try:
                tracer = self._tracer
                attributes = {f"grove.{k}": v for k, v in span.labels.items()}
                name = span.name if span.name.startswith("grove.") else f"grove.{span.name}"
                with tracer.start_as_current_span(name, attributes=attributes) as otel_span:
                    otel_span.set_attribute("grove.duration_ms", span.duration_ms)
                with self._lock:
                    self._exported_count += 1
            except Exception:
                logger.debug("OTLP span export failed; telemetry drop counted")
                with self._lock:
                    self._failed_count += 1
        for metric in snapshot.metrics:
            try:
                instrument = self._metric_instrument(metric.name, metric.kind)
                attributes = {f"grove.{key}": value for key, value in metric.labels.items()}
                if metric.kind == "histogram":
                    instrument.record(metric.value, attributes=attributes)
                elif metric.kind == "gauge":
                    instrument.set(metric.value, attributes=attributes)
                else:
                    instrument.add(metric.value, attributes=attributes)
                with self._lock:
                    self._exported_count += 1
            except Exception:
                logger.debug("OTLP metric export failed; telemetry drop counted")
                with self._lock:
                    self._failed_count += 1

    def _metric_instrument(self, name: str, kind: str) -> Any:
        key = (name, kind)
        instrument = self._instruments.get(key)
        if instrument is not None:
            return instrument
        meter = self._meter
        metric_name = name if name.startswith("grove.") else f"grove.{name}"
        if kind == "counter":
            instrument = meter.create_counter(metric_name)
        elif kind == "histogram":
            instrument = meter.create_histogram(metric_name)
        elif kind == "up_down_counter":
            instrument = meter.create_up_down_counter(metric_name)
        else:
            instrument = meter.create_gauge(metric_name)
        self._instruments[key] = instrument
        return instrument

    def drain_and_export(self) -> None:
        """Atomically drain the bounded recorder and export one batch."""

        self.export(self._recorder.drain())

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "exported": self._exported_count,
                "dropped": self._dropped_count,
                "failed": self._failed_count,
            }

    def shutdown(self) -> None:
        """Best-effort bounded shutdown for SDK providers."""

        for provider in (self._meter_provider, self._tracer_provider):
            if provider is None:
                continue
            try:
                provider.shutdown(timeout_millis=int(self._policy.timeout_seconds * 1000))
            except Exception:
                logger.debug("OTLP provider shutdown failed")


class TelemetryExportRuntime:
    """Own a bounded best-effort export thread for one process lifecycle.

    The thread is only started when OTLP export is configured and the SDK was
    constructed successfully.  Online roles therefore never wait for the
    Collector, while shutdown performs one final bounded drain.
    """

    def __init__(
        self,
        exporter: OTLPExporter | None = None,
        *,
        interval_seconds: float = EXPORT_INTERVAL_SECONDS,
    ) -> None:
        if type(interval_seconds) not in {int, float} or isinstance(interval_seconds, bool) or interval_seconds <= 0:
            raise ValueError("interval_seconds must be a positive number")
        self._exporter = exporter or OTLPExporter()
        self._interval_seconds = float(interval_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start export when configured; disabled telemetry remains bounded in memory."""

        if not self._exporter.enabled:
            return
        with self._lock:
            if self._thread is not None:
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="grove-telemetry-export",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        """Stop the export loop and perform one final best-effort drain."""

        with self._lock:
            thread = self._thread
        if thread is None:
            return
        self._stop.set()
        thread.join(timeout=self._interval_seconds + 1.0)
        if thread.is_alive():
            logger.debug("telemetry export thread did not stop within its bounded deadline")
        self._exporter.shutdown()
        with self._lock:
            self._thread = None

    def _run(self) -> None:
        next_export = monotonic() + self._interval_seconds
        while not self._stop.wait(max(0.0, next_export - monotonic())):
            self._exporter.drain_and_export()
            next_export = monotonic() + self._interval_seconds
        self._exporter.drain_and_export()


def _signal_endpoint(endpoint: str, signal: str) -> str:
    parsed = urlsplit(endpoint)
    path = parsed.path.rstrip("/")
    for known in ("traces", "metrics", "logs"):
        suffix = f"/v1/{known}"
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    return urlunsplit((parsed.scheme, parsed.netloc, f"{path}/v1/{signal}", "", ""))


__all__ = [
    "EXPORT_MAX_RETRIES",
    "EXPORT_INTERVAL_SECONDS",
    "EXPORT_QUEUE_CAPACITY",
    "EXPORT_TIMEOUT_SECONDS",
    "OTLPExporter",
    "TelemetryExportRuntime",
    "TelemetryPolicy",
]
