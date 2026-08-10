"""Unit tests for the bounded OTLP exporter adapter."""

from __future__ import annotations

import pytest
from app.core.telemetry import BoundedTelemetryRecorder
from app.core.telemetry_export import OTLPExporter, TelemetryPolicy


def _recorder() -> BoundedTelemetryRecorder:
    rec = BoundedTelemetryRecorder()
    rec.record_span("s1", duration_ms=1.0, labels={"role": "api", "operation": "submit"})
    rec.record_metric("m1", value=1, labels={"role": "api", "outcome": "ok"})
    return rec


class TestTelemetryPolicy:
    def test_default_is_disabled_when_no_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        policy = TelemetryPolicy()
        assert policy.enabled is False
        assert policy.endpoint == ""

    def test_env_enables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
        policy = TelemetryPolicy()
        assert policy.enabled is True
        assert policy.endpoint == "http://collector:4318"

    def test_explicit_endpoint_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://env:4318")
        policy = TelemetryPolicy(endpoint="http://explicit:4318")
        assert policy.endpoint == "http://explicit:4318"

    def test_invalid_queue_capacity(self) -> None:
        with pytest.raises(ValueError):
            TelemetryPolicy(queue_capacity=0)

    def test_invalid_timeout(self) -> None:
        with pytest.raises(ValueError):
            TelemetryPolicy(timeout_seconds=0)

    def test_invalid_max_retries(self) -> None:
        with pytest.raises(ValueError):
            TelemetryPolicy(max_retries=-1)


class TestOTLPExporter:
    def test_disabled_exporter_counts_drops(self) -> None:
        rec = _recorder()
        exporter = OTLPExporter(rec, TelemetryPolicy(endpoint=""))
        snapshot = rec.drain()
        exporter.export(snapshot)
        stats = exporter.stats()
        assert stats["exported"] == 0
        assert stats["dropped"] == 2  # 1 span + 1 metric

    def test_enabled_exporter_best_effort(self) -> None:
        rec = _recorder()
        exporter = OTLPExporter(rec, TelemetryPolicy(endpoint="http://nonexistent:4318"))
        snapshot = rec.drain()
        # Export should not raise even with an unreachable endpoint.
        exporter.export(snapshot)
        # The tracer may be built but export is best-effort; stats should be accessible.
        stats = exporter.stats()
        assert isinstance(stats["exported"], int)
        assert isinstance(stats["failed"], int)

    def test_empty_snapshot_is_noop(self) -> None:
        rec = BoundedTelemetryRecorder()
        exporter = OTLPExporter(rec, TelemetryPolicy(endpoint="http://collector:4318"))
        snapshot = rec.drain()
        exporter.export(snapshot)
        assert exporter.stats()["exported"] == 0
