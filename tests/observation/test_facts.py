"""Unit tests for the WS-4 observation facts layer."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from app.observation.facts import (
    NODE_EXECUTED_SCHEMA_REF,
    RUN_LIFECYCLE_SCHEMA_REF,
    EmitEventRequest,
    NodeExecutedPayload,
    RunLifecyclePayload,
    UnknownRuntimeSchemaError,
    build_lifecycle_emit_request,
    build_node_executed_emit_request,
    build_ui_projection_meta,
    derive_source_event_id,
    lifecycle_to_run_status_changed,
    parse_runtime_payload,
)
from pydantic import ValidationError

RUN_ID = UUID("11111111-1111-1111-1111-111111111111")
ZERO_HASH = "0" * 64
NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC)


class TestPayloads:
    def test_run_lifecycle_payload_round_trip(self) -> None:
        payload = RunLifecyclePayload(kind="run_lifecycle", run_id=RUN_ID, status="running", run_revision=1)
        again = RunLifecyclePayload.model_validate(payload.model_dump(mode="python"))
        assert again == payload

    def test_run_lifecycle_rejects_unknown_status(self) -> None:
        with pytest.raises(ValidationError):
            RunLifecyclePayload(kind="run_lifecycle", run_id=RUN_ID, status="zombie", run_revision=1)

    def test_node_executed_payload_validates(self) -> None:
        payload = NodeExecutedPayload(
            kind="node_executed", node_id="node_a", stage="start", input_hash=ZERO_HASH, value=0
        )
        assert payload.value == 0

    def test_node_executed_rejects_bad_hash(self) -> None:
        with pytest.raises(ValidationError):
            NodeExecutedPayload(kind="node_executed", node_id="node_a", stage="start", input_hash="bad", value=0)


class TestParseRuntimePayload:
    def test_known_schema_materialises(self) -> None:
        parsed = parse_runtime_payload(
            RUN_LIFECYCLE_SCHEMA_REF,
            {"kind": "run_lifecycle", "run_id": str(RUN_ID), "status": "running", "run_revision": 1},
        )
        assert isinstance(parsed, RunLifecyclePayload)

    def test_unknown_schema_raises(self) -> None:
        with pytest.raises(UnknownRuntimeSchemaError) as info:
            parse_runtime_payload("grove.runtime.future.v9", {"kind": "run_lifecycle"})
        assert info.value.schema_ref == "grove.runtime.future.v9"


class TestEmitEventRequest:
    def test_lifecycle_request_builds(self) -> None:
        request = build_lifecycle_emit_request(
            run_id=RUN_ID, command_seq=0, status="running", run_revision=1, occurred_at=NOW
        )
        assert request.event_type == "run.lifecycle"
        assert request.source == "grove.runtime_worker"
        assert request.payload_schema_ref == RUN_LIFECYCLE_SCHEMA_REF

    def test_node_request_builds(self) -> None:
        request = build_node_executed_emit_request(
            run_id=RUN_ID, command_seq=0, node_id="node_a", stage="start",
            input_hash=ZERO_HASH, value=1, occurred_at=NOW,
        )
        assert request.payload_schema_ref == NODE_EXECUTED_SCHEMA_REF

    def test_unknown_schema_ref_rejected(self) -> None:
        payload = RunLifecyclePayload(kind="run_lifecycle", run_id=RUN_ID, status="running", run_revision=1)
        with pytest.raises(UnknownRuntimeSchemaError):
            EmitEventRequest(
                event_type="run.lifecycle",
                source="grove.runtime_worker",
                source_event_id="e1",
                payload_schema_ref="grove.runtime.future.v9",
                payload=payload,
                occurred_at=NOW,
            )

    def test_wrong_payload_type_rejected(self) -> None:
        lifecycle = RunLifecyclePayload(kind="run_lifecycle", run_id=RUN_ID, status="running", run_revision=1)
        with pytest.raises(TypeError):
            EmitEventRequest(
                event_type="node.executed",
                source="grove.runtime_worker",
                source_event_id="e1",
                payload_schema_ref=NODE_EXECUTED_SCHEMA_REF,
                payload=lifecycle,
                occurred_at=NOW,
            )

    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises(ValueError):
            build_lifecycle_emit_request(
                run_id=RUN_ID, command_seq=0, status="running", run_revision=1,
                occurred_at=datetime(2026, 1, 1),
            )

    def test_canonical_payload_bytes_stable(self) -> None:
        request = build_lifecycle_emit_request(
            run_id=RUN_ID, command_seq=0, status="running", run_revision=1, occurred_at=NOW
        )
        again = build_lifecycle_emit_request(
            run_id=RUN_ID, command_seq=0, status="running", run_revision=1, occurred_at=NOW
        )
        assert request.canonical_payload_bytes() == again.canonical_payload_bytes()


class TestDeriveSourceEventId:
    def test_stable_and_unique(self) -> None:
        a = derive_source_event_id(RUN_ID, 0, "run.lifecycle", 0)
        b = derive_source_event_id(RUN_ID, 0, "run.lifecycle", 0)
        assert a == b
        c = derive_source_event_id(RUN_ID, 1, "run.lifecycle", 0)
        assert a != c

    def test_rejects_bad_inputs(self) -> None:
        with pytest.raises(ValueError):
            derive_source_event_id("not-a-uuid", 0, "x", 0)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            derive_source_event_id(RUN_ID, -1, "x", 0)


class TestProjectionMapping:
    def test_lifecycle_maps_to_run_status_changed(self) -> None:
        payload = RunLifecyclePayload(kind="run_lifecycle", run_id=RUN_ID, status="succeeded", run_revision=2)
        mapped = lifecycle_to_run_status_changed(payload)
        assert mapped.kind == "run_status_changed"
        assert mapped.status == "succeeded"
        assert mapped.run_id == RUN_ID

    def test_ui_projection_meta_contract_family(self) -> None:
        meta = build_ui_projection_meta(
            tenant_id="tenant-a", correlation_id="corr-1", causation_id=uuid4()
        )
        assert meta.contract_name == "ui.projection"
        assert meta.contract_version == "v1"
        assert meta.tenant_id == "tenant-a"
