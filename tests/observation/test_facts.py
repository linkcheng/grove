"""Unit tests for the WS-4 observation facts layer."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from app.observation.facts import (
    EXECUTION_AUDIT_SCHEMA_REF,
    NODE_EXECUTED_SCHEMA_REF,
    RUN_LIFECYCLE_SCHEMA_REF,
    RUNTIME_WORKER_SOURCE,
    EmitEventRequest,
    ExecutionAuditPayload,
    NodeExecutedPayload,
    RunLifecyclePayload,
    UnknownRuntimeSchemaError,
    build_execution_audit_emit_request,
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
            RunLifecyclePayload(kind="run_lifecycle", run_id=RUN_ID, status="zombie", run_revision=1)  # type: ignore[arg-type]

    def test_node_executed_payload_validates(self) -> None:
        payload = NodeExecutedPayload(
            kind="node_executed", node_id="node_a", stage="start", input_hash=ZERO_HASH, value=0
        )
        assert payload.value == 0

    def test_node_executed_rejects_bad_hash(self) -> None:
        with pytest.raises(ValidationError):
            NodeExecutedPayload(kind="node_executed", node_id="node_a", stage="start", input_hash="bad", value=0)

    def test_execution_audit_payload_is_safe_and_closed(self) -> None:
        payload = ExecutionAuditPayload(
            kind="execution_audit",
            action="worker_claimed",
            run_id=RUN_ID,
            command_id=uuid4(),
            command_seq=0,
            command_type="start",
            result_code="claimed",
        )
        assert "worker_id" not in payload.model_dump(mode="json")
        with pytest.raises(ValidationError):
            ExecutionAuditPayload.model_validate({**payload.model_dump(), "execution_fence": 1})


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
            run_id=RUN_ID,
            command_seq=0,
            node_id="node_a",
            stage="start",
            input_hash=ZERO_HASH,
            value=1,
            occurred_at=NOW,
        )
        assert request.payload_schema_ref == NODE_EXECUTED_SCHEMA_REF

    def test_audit_request_identity_is_deterministic_per_transition(self) -> None:
        command_id = uuid4()
        first = build_execution_audit_emit_request(
            source=RUNTIME_WORKER_SOURCE,
            run_id=RUN_ID,
            command_id=command_id,
            command_seq=0,
            command_type="start",
            action="worker_claimed",
            result_code="claimed",
            occurred_at=NOW,
            transition_key=f"{command_id}:1:claimed",
        )
        second = build_execution_audit_emit_request(
            source=RUNTIME_WORKER_SOURCE,
            run_id=RUN_ID,
            command_id=command_id,
            command_seq=0,
            command_type="start",
            action="worker_claimed",
            result_code="claimed",
            occurred_at=NOW,
            transition_key=f"{command_id}:1:claimed",
        )
        assert first.payload_schema_ref == EXECUTION_AUDIT_SCHEMA_REF
        assert first.source_event_id == second.source_event_id

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
                run_id=RUN_ID,
                command_seq=0,
                status="running",
                run_revision=1,
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
        meta = build_ui_projection_meta(tenant_id="tenant-a", correlation_id="corr-1", causation_id=uuid4())
        assert meta.contract_name == "ui.projection"
        assert meta.contract_version == "v1"
        assert meta.tenant_id == "tenant-a"


class TestAnswerMessageFacts:
    def test_builder_chunks_deltas_and_hashes_content(self) -> None:
        import hashlib

        from app.observation.facts import (
            ANSWER_MESSAGE_CHUNK_CHARS,
            MESSAGE_COMPLETED_SCHEMA_REF,
            MESSAGE_DELTA_SCHEMA_REF,
            MESSAGE_STARTED_SCHEMA_REF,
            build_answer_message_emit_requests,
        )

        answer = "评" * (ANSWER_MESSAGE_CHUNK_CHARS * 2 + 17)
        occurred = datetime.now(UTC)
        requests = build_answer_message_emit_requests(run_id=RUN_ID, command_seq=0, answer=answer, occurred_at=occurred)
        assert [request.event_type for request in requests] == [
            "message.started",
            "message.delta",
            "message.delta",
            "message.delta",
            "message.completed",
        ]
        assert [request.payload_schema_ref for request in requests] == [
            MESSAGE_STARTED_SCHEMA_REF,
            MESSAGE_DELTA_SCHEMA_REF,
            MESSAGE_DELTA_SCHEMA_REF,
            MESSAGE_DELTA_SCHEMA_REF,
            MESSAGE_COMPLETED_SCHEMA_REF,
        ]
        message_ids = {request.payload.message_id for request in requests}  # type: ignore[attr-defined]
        assert len(message_ids) == 1
        deltas = [request.payload for request in requests[1:-1]]
        assert "".join(delta.safe_delta for delta in deltas) == answer  # type: ignore[attr-defined]
        assert [delta.delta_seq for delta in deltas] == [0, 1, 2]  # type: ignore[attr-defined]
        completed = requests[-1].payload
        assert completed.last_delta_seq == 2  # type: ignore[attr-defined]
        assert completed.content_hash == hashlib.sha256(answer.encode("utf-8")).hexdigest()  # type: ignore[attr-defined]
        assert len({request.source_event_id for request in requests}) == len(requests)

    def test_builder_rejects_empty_or_non_string_answer(self) -> None:
        from app.observation.facts import build_answer_message_emit_requests

        for bad in ("", "  ", b"bytes", 123):
            with pytest.raises(ValueError):
                build_answer_message_emit_requests(
                    run_id=RUN_ID,
                    command_seq=0,
                    answer=bad,  # type: ignore[arg-type]
                    occurred_at=datetime.now(UTC),
                )

    def test_message_payloads_materialise_through_the_closed_registry(self) -> None:
        from app.observation.facts import (
            MESSAGE_COMPLETED_SCHEMA_REF,
            MESSAGE_DELTA_SCHEMA_REF,
            MESSAGE_STARTED_SCHEMA_REF,
            MessageCompletedPayload,
            MessageDeltaPayload,
            MessageStartedPayload,
        )

        started = parse_runtime_payload(
            MESSAGE_STARTED_SCHEMA_REF,
            {
                "kind": "message_started",
                "run_id": str(RUN_ID),
                "message_id": str(uuid4()),
                "content_schema_ref": "text.plain@1",
            },
        )
        assert isinstance(started, MessageStartedPayload) and started.kind == "message_started"
        delta = parse_runtime_payload(
            MESSAGE_DELTA_SCHEMA_REF,
            {
                "kind": "message_delta",
                "run_id": str(RUN_ID),
                "message_id": str(uuid4()),
                "delta_seq": 0,
                "safe_delta": "正文",
            },
        )
        assert isinstance(delta, MessageDeltaPayload) and delta.safe_delta == "正文"
        completed = parse_runtime_payload(
            MESSAGE_COMPLETED_SCHEMA_REF,
            {
                "kind": "message_completed",
                "run_id": str(RUN_ID),
                "message_id": str(uuid4()),
                "last_delta_seq": 0,
                "content_hash": "0" * 64,
            },
        )
        assert isinstance(completed, MessageCompletedPayload) and completed.content_hash == "0" * 64

    def test_ui_mappers_rename_and_preserve_content(self) -> None:
        from app.observation.facts import (
            MessageCompletedPayload,
            MessageDeltaPayload,
            MessageStartedPayload,
            message_completed_to_ui,
            message_delta_to_ui,
            message_started_to_ui,
        )

        message_id = uuid4()
        started = message_started_to_ui(
            MessageStartedPayload(
                kind="message_started",
                run_id=RUN_ID,
                message_id=message_id,
                content_schema_ref="text.plain@1",
            )
        )
        assert started.owner_run_id == RUN_ID
        assert started.role == "assistant"
        delta = message_delta_to_ui(
            MessageDeltaPayload(
                kind="message_delta",
                run_id=RUN_ID,
                message_id=message_id,
                delta_seq=1,
                safe_delta="文本",
            )
        )
        assert delta.safe_delta == "文本" and delta.delta_seq == 1
        completed = message_completed_to_ui(
            MessageCompletedPayload(
                kind="message_completed",
                run_id=RUN_ID,
                message_id=message_id,
                last_delta_seq=1,
                content_hash="0" * 64,
            )
        )
        assert completed.content_hash == "0" * 64 and completed.artifact_ref is None
