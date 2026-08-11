"""Unit tests for the bounded OTLP exporter adapter."""

from __future__ import annotations

import threading

import pytest
from app.core.telemetry import BoundedTelemetryRecorder
from app.core.telemetry_export import OTLPExporter, TelemetryExportRuntime, TelemetryPolicy


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

    @pytest.mark.parametrize(
        "endpoint",
        ["grpc://collector:4318", "http://user:secret@collector:4318", "http://collector:4318?token=x"],
    )
    def test_endpoint_rejects_unsupported_or_secret_bearing_urls(self, endpoint: str) -> None:
        with pytest.raises(ValueError, match="credential-free"):
            TelemetryPolicy(endpoint=endpoint)


class TestOTLPExporter:
    def test_disabled_exporter_counts_drops(self) -> None:
        rec = _recorder()
        exporter = OTLPExporter(rec, TelemetryPolicy(endpoint=""))
        snapshot = rec.drain()
        exporter.export(snapshot)
        stats = exporter.stats()
        assert stats["exported"] == 0
        assert stats["dropped"] == 2  # 1 span + 1 metric

    def test_enabled_exporter_best_effort(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class FakeSpan:
            def __enter__(self) -> FakeSpan:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def set_attribute(self, _name: str, _value: object) -> None:
                return None

        class FakeTracer:
            def start_as_current_span(self, _name: str, **_kwargs: object) -> FakeSpan:
                return FakeSpan()

        class FakeInstrument:
            def record(self, _value: float, **_kwargs: object) -> None:
                return None

            def set(self, _value: float, **_kwargs: object) -> None:
                return None

            def add(self, _value: float, **_kwargs: object) -> None:
                return None

        class FakeMeter:
            def create_gauge(self, _name: str) -> FakeInstrument:
                return FakeInstrument()

        monkeypatch.setattr(OTLPExporter, "_build_providers", lambda _self: (FakeTracer(), FakeMeter()))
        rec = _recorder()
        exporter = OTLPExporter(rec, TelemetryPolicy(endpoint="http://nonexistent:4318"))
        snapshot = rec.drain()
        exporter.export(snapshot)
        stats = exporter.stats()
        assert stats["exported"] == 2
        assert stats["failed"] == 0

    def test_empty_snapshot_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(OTLPExporter, "_build_providers", lambda _self: (None, None))
        rec = BoundedTelemetryRecorder()
        exporter = OTLPExporter(rec, TelemetryPolicy(endpoint="http://collector:4318"))
        snapshot = rec.drain()
        exporter.export(snapshot)
        assert exporter.stats()["exported"] == 0


class TestTelemetryExportRuntime:
    def test_disabled_exporter_does_not_start_thread(self) -> None:
        runtime = TelemetryExportRuntime(
            OTLPExporter(BoundedTelemetryRecorder(), TelemetryPolicy(endpoint="")),
            interval_seconds=0.01,
        )
        runtime.start()
        assert runtime.running is False
        runtime.stop()

    def test_enabled_exporter_drains_periodically_and_on_stop(self) -> None:
        drained = threading.Event()

        class FakeExporter:
            enabled = True

            def drain_and_export(self) -> None:
                drained.set()

            def shutdown(self) -> None:
                return None

        runtime = TelemetryExportRuntime(FakeExporter(), interval_seconds=0.01)  # type: ignore[arg-type]
        runtime.start()
        assert drained.wait(timeout=0.5)
        assert runtime.running is True
        runtime.stop()
        assert runtime.running is False
