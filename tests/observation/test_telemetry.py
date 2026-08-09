"""Unit tests for the bounded telemetry recorder."""

from __future__ import annotations

import pytest
from app.core.telemetry import BoundedTelemetryRecorder


class TestLabels:
    def test_allowed_labels_accepted(self) -> None:
        rec = BoundedTelemetryRecorder()
        rec.record_metric("op.count", value=1, labels={"role": "api", "operation": "submit"})
        snap = rec.drain()
        assert snap.metrics[0].labels == {"role": "api", "operation": "submit"}

    def test_high_cardinality_label_rejected(self) -> None:
        rec = BoundedTelemetryRecorder()
        with pytest.raises(ValueError):
            rec.record_metric("op.count", value=1, labels={"tenant_id": "t1"})

    def test_overlong_label_rejected(self) -> None:
        rec = BoundedTelemetryRecorder()
        with pytest.raises(ValueError):
            rec.record_metric("op.count", value=1, labels={"role": "x" * 65})


class TestBounded:
    def test_drops_when_full(self) -> None:
        rec = BoundedTelemetryRecorder(queue_capacity=2)
        rec.record_span("s1", duration_ms=1.0)
        rec.record_span("s2", duration_ms=2.0)
        rec.record_span("s3", duration_ms=3.0)  # dropped
        snap = rec.drain()
        assert len(snap.spans) == 2
        assert snap.dropped >= 1

    def test_non_negative_duration_required(self) -> None:
        rec = BoundedTelemetryRecorder()
        with pytest.raises(ValueError):
            rec.record_span("s1", duration_ms=-1.0)


class TestTrace:
    def test_span_carries_trace_context(self) -> None:
        from app.core.trace import trace_id_context

        rec = BoundedTelemetryRecorder()
        token = trace_id_context.set("trace-xyz")
        try:
            rec.record_span("s1", duration_ms=1.0)
        finally:
            trace_id_context.reset(token)
        snap = rec.drain()
        assert snap.spans[0].trace_id == "trace-xyz"
        # metric labels never carry the trace id
        rec.record_metric("m", value=1, labels={"role": "api"})
        snap2 = rec.drain()
        assert "trace_id" not in snap2.metrics[0].labels
