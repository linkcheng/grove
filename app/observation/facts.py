"""WS-4 observation facts: versioned payloads, emit requests and read-model views.

The canonical RuntimeEvent / UIProjectionEvent / InteractionItem contracts are
already frozen in :mod:`app.contracts.canonical` (WS-1 contract spine).  This
module only adds the WS-4-specific versioned payload schemas produced by the
deterministic conformance runtime, the lightweight emit-request shape used to
cross the persistence seam, the cursor/view types consumed by the Observation
API, and the pure mapping from a runtime lifecycle fact to a typed
``RunStatusChanged`` UI projection payload.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, TypeVar
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from app.contracts.canonical import (
    SHA256_PATTERN,
    CanonicalModel,
    ContractMeta,
    DomainViewAccepted,
    MessageCompleted,
    MessageDelta,
    MessageStarted,
    RunStatusChanged,
)

# The public run status surface mirrors the authority run lifecycle.  It is
# duplicated here as a closed Literal so observation payloads cannot drift from
# the values the authority state machine may actually emit.
PublicRunStatus = Literal[
    "accepted",
    "running",
    "waiting_user_input",
    "waiting_action_result",
    "waiting_child_result",
    "cancel_requested",
    "succeeded",
    "failed",
    "cancelled",
]
TERMINAL_RUN_STATUSES = frozenset({"succeeded", "failed", "cancelled"})

# ---------------------------------------------------------------------------
# Versioned observation payloads produced by the deterministic runtime.
# ---------------------------------------------------------------------------

RUN_LIFECYCLE_SCHEMA_REF = "grove.runtime.run-lifecycle.v1"
NODE_EXECUTED_SCHEMA_REF = "grove.runtime.node-executed.v1"
EXECUTION_AUDIT_SCHEMA_REF = "grove.runtime.execution-audit.v1"
DOMAIN_VIEW_ACCEPTED_SCHEMA_REF = "grove.runtime.domain-view-accepted.v1"
MESSAGE_STARTED_SCHEMA_REF = "grove.runtime.message-started.v1"
MESSAGE_DELTA_SCHEMA_REF = "grove.runtime.message-delta.v1"
MESSAGE_COMPLETED_SCHEMA_REF = "grove.runtime.message-completed.v1"

RUNTIME_WORKER_SOURCE: Literal["grove.runtime_worker"] = "grove.runtime_worker"
API_COMMAND_SOURCE: Literal["grove.api.command"] = "grove.api.command"
RECONCILIATION_SOURCE: Literal["grove.projection_reconciliation"] = "grove.projection_reconciliation"
type ObservationSource = Literal[
    "grove.runtime_worker",
    "grove.api.command",
    "grove.projection_reconciliation",
]
type ExecutionAuditAction = Literal[
    "command_accepted",
    "worker_claimed",
    "worker_takeover",
    "lease_renewed",
    "checkpoint_applied",
    "command_applied",
    "cancel_accepted",
    "command_dead_lettered",
    "expired_command_consumed",
    "expired_command_requeued",
]

KNOWN_RUNTIME_PAYLOAD_SCHEMAS: frozenset[str] = frozenset(
    {
        RUN_LIFECYCLE_SCHEMA_REF,
        NODE_EXECUTED_SCHEMA_REF,
        EXECUTION_AUDIT_SCHEMA_REF,
        DOMAIN_VIEW_ACCEPTED_SCHEMA_REF,
        MESSAGE_STARTED_SCHEMA_REF,
        MESSAGE_DELTA_SCHEMA_REF,
        MESSAGE_COMPLETED_SCHEMA_REF,
    }
)


class RunLifecyclePayload(CanonicalModel):
    """An authority run lifecycle transition observed by the runtime."""

    kind: Literal["run_lifecycle"]
    run_id: UUID
    status: PublicRunStatus
    run_revision: int = Field(ge=0)


class NodeExecutedPayload(CanonicalModel):
    """A deterministic graph node execution observed by the runtime."""

    kind: Literal["node_executed"]
    node_id: str = Field(min_length=1, max_length=128)
    stage: Literal["start", "continue", "terminal"]
    input_hash: str = Field(pattern=SHA256_PATTERN)
    value: int = Field(ge=0)


class ExecutionAuditPayload(CanonicalModel):
    """A safe, versioned audit fact for one committed execution transition.

    Worker identity, lease timestamps, fence values, payload references and
    failure detail deliberately stay out of this public fact.  Their authority
    remains in WS-3 tables; the audit stream records only the transition class
    and stable public command/run identity.
    """

    kind: Literal["execution_audit"]
    action: ExecutionAuditAction
    run_id: UUID
    command_id: UUID
    command_seq: int = Field(ge=0)
    command_type: Literal["start", "resume", "cancel", "continue", "signal"] | None = None
    run_revision: int | None = Field(default=None, ge=0)
    result_code: str = Field(min_length=1, max_length=64)


class DomainViewAcceptedPayload(CanonicalModel):
    """A typed domain read view accepted and checkpointed for one run.

    The worker emits this fact when a Profile-owned typed read tool's
    all-or-nothing view has been accepted; the projection maps it to the UI
    ``domain_view_accepted`` milestone consumed by the Profile renderer
    (docs/06 §7.2, docs/31 §6).  It carries only the safe projection surface --
    never SQL, table names or the raw tool payload.
    """

    kind: Literal["domain_view_accepted"]
    run_id: UUID
    tool_request_id: UUID
    view_schema_ref: str = Field(min_length=1, max_length=256)
    observed_at: datetime
    source_ref: str = Field(min_length=1, max_length=256)
    result_hash: str = Field(pattern=SHA256_PATTERN)
    item_count: int | None = Field(default=None, ge=0, le=1_000_000)

    @field_validator("observed_at")
    @classmethod
    def observed_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        return value.astimezone(UTC)


class MessageStartedPayload(CanonicalModel):
    """One run-visible message opened by the runtime (e.g. the typed answer).

    The worker emits the message fact family when a run reaches a terminal
    typed report; the projection maps it to the UI ``message_*`` payloads.
    The delta text is the already-gated public surface -- never SQL, tool
    payloads or provider metadata.
    """

    kind: Literal["message_started"]
    run_id: UUID
    message_id: UUID
    role: Literal["assistant"] = "assistant"
    content_schema_ref: str = Field(min_length=1, max_length=256)


class MessageDeltaPayload(CanonicalModel):
    """One ordered, bounded text delta of a run-visible message."""

    kind: Literal["message_delta"]
    run_id: UUID
    message_id: UUID
    delta_seq: int = Field(ge=0)
    safe_delta: str = Field(min_length=1, max_length=8192)


class MessageCompletedPayload(CanonicalModel):
    """The content-addressed completion marker of a run-visible message."""

    kind: Literal["message_completed"]
    run_id: UUID
    message_id: UUID
    last_delta_seq: int = Field(ge=0)
    content_hash: str = Field(pattern=SHA256_PATTERN)


# Registry of the only schema refs the projection may materialise.  An unknown
# ref never reaches a Python payload model; the projection dead-letters it.
RUNTIME_PAYLOAD_REGISTRY: Mapping[str, type[CanonicalModel]] = {
    RUN_LIFECYCLE_SCHEMA_REF: RunLifecyclePayload,
    NODE_EXECUTED_SCHEMA_REF: NodeExecutedPayload,
    EXECUTION_AUDIT_SCHEMA_REF: ExecutionAuditPayload,
    DOMAIN_VIEW_ACCEPTED_SCHEMA_REF: DomainViewAcceptedPayload,
    MESSAGE_STARTED_SCHEMA_REF: MessageStartedPayload,
    MESSAGE_DELTA_SCHEMA_REF: MessageDeltaPayload,
    MESSAGE_COMPLETED_SCHEMA_REF: MessageCompletedPayload,
}


class UnknownRuntimeSchemaError(ValueError):
    """A runtime payload schema ref is not in the known closed registry."""

    def __init__(self, schema_ref: str) -> None:
        super().__init__(f"unknown runtime payload schema ref: {schema_ref}")
        self.schema_ref = schema_ref
        self.preserve_contract_error = True


def parse_runtime_payload(schema_ref: str, raw: Any) -> CanonicalModel:
    """Materialise exactly one known versioned runtime payload; reject unknown."""

    model = RUNTIME_PAYLOAD_REGISTRY.get(schema_ref)
    if model is None:
        raise UnknownRuntimeSchemaError(schema_ref)
    return model.model_validate(raw)


# ---------------------------------------------------------------------------
# Emit request crossing the persistence seam (run_seq allocated by the DB).
# ---------------------------------------------------------------------------

MAX_RUNTIME_EVENT_BYTES = 65_536


@dataclass(frozen=True, slots=True)
class EmitEventRequest:
    """One runtime event awaiting commit-ordered ``run_seq`` allocation.

    ``run_seq`` is deliberately absent: only the authoritative transaction,
    while holding the run lock, may assign it.  Everything else is built and
    content-hashed before crossing the persistence seam so the database only
    persists pre-validated, canonical bytes.
    """

    event_type: str
    source: str
    source_event_id: str
    payload_schema_ref: str
    payload: CanonicalModel
    occurred_at: datetime

    def __post_init__(self) -> None:
        if type(self.event_type) is not str or not self.event_type:
            raise ValueError("event_type must be a non-empty string")
        if type(self.source) is not str or not self.source:
            raise ValueError("source must be a non-empty string")
        if type(self.source_event_id) is not str or not self.source_event_id:
            raise ValueError("source_event_id must be a non-empty string")
        if type(self.occurred_at) is not datetime or self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at must be a timezone-aware datetime")
        if self.payload_schema_ref not in RUNTIME_PAYLOAD_REGISTRY:
            raise UnknownRuntimeSchemaError(self.payload_schema_ref)
        if type(self.payload) is not RUNTIME_PAYLOAD_REGISTRY[self.payload_schema_ref]:
            raise TypeError("payload must be the exact model bound to its payload_schema_ref")

    def canonical_payload_bytes(self) -> bytes:
        from app.contracts.canonical import canonical_bytes

        return canonical_bytes(self.payload)


def derive_source_event_id(run_id: UUID, command_seq: int, event_type: str, index: int) -> str:
    """Derive a stable, unique-per-transition source event id.

    The id is deterministic over ``(run, command, event, index)`` so a retried
    authority transaction cannot create a duplicate observation stream; the
    database ``(tenant, source, source_event_id)`` uniqueness constraint is the
    last line of defence, not the first.
    """

    if type(run_id) is not UUID:
        raise ValueError("run_id must be an exact UUID")
    if type(command_seq) is not int or command_seq < 0:
        raise ValueError("command_seq must be a non-negative int")
    if type(event_type) is not str or not event_type:
        raise ValueError("event_type must be a non-empty string")
    if type(index) is not int or index < 0:
        raise ValueError("index must be a non-negative int")
    return f"{run_id}:{command_seq}:{event_type}:{index}"


def build_execution_audit_emit_request(
    *,
    source: ObservationSource,
    run_id: UUID,
    command_id: UUID,
    command_seq: int,
    action: ExecutionAuditAction,
    result_code: str,
    occurred_at: datetime,
    transition_key: str,
    command_type: Literal["start", "resume", "cancel", "continue", "signal"] | None = None,
    run_revision: int | None = None,
) -> EmitEventRequest:
    """Build an idempotent audit fact for an exact committed transition."""

    import hashlib

    if type(transition_key) is not str or not transition_key or len(transition_key) > 1024:
        raise ValueError("transition_key must be a non-empty bounded string")
    transition_hash = hashlib.sha256(transition_key.encode("utf-8")).hexdigest()
    event_type = f"execution.{action}"
    payload = ExecutionAuditPayload(
        kind="execution_audit",
        action=action,
        run_id=run_id,
        command_id=command_id,
        command_seq=command_seq,
        command_type=command_type,
        run_revision=run_revision,
        result_code=result_code,
    )
    return EmitEventRequest(
        event_type=event_type,
        source=source,
        source_event_id=f"{run_id}:{action}:{transition_hash}",
        payload_schema_ref=EXECUTION_AUDIT_SCHEMA_REF,
        payload=payload,
        occurred_at=occurred_at,
    )


def build_lifecycle_emit_request(
    *,
    run_id: UUID,
    command_seq: int,
    status: PublicRunStatus,
    run_revision: int,
    occurred_at: datetime,
    event_type: str = "run.lifecycle",
) -> EmitEventRequest:
    """Build the canonical run-lifecycle observation request for one transition."""

    payload = RunLifecyclePayload(kind="run_lifecycle", run_id=run_id, status=status, run_revision=run_revision)
    return EmitEventRequest(
        event_type=event_type,
        source=RUNTIME_WORKER_SOURCE,
        source_event_id=derive_source_event_id(run_id, command_seq, event_type, 0),
        payload_schema_ref=RUN_LIFECYCLE_SCHEMA_REF,
        payload=payload,
        occurred_at=occurred_at,
    )


def build_node_executed_emit_request(
    *,
    run_id: UUID,
    command_seq: int,
    node_id: str,
    stage: Literal["start", "continue", "terminal"],
    input_hash: str,
    value: int,
    occurred_at: datetime,
    event_type: str = "node.executed",
) -> EmitEventRequest:
    """Build the canonical node-executed observation request for one node."""

    payload = NodeExecutedPayload(
        kind="node_executed", node_id=node_id, stage=stage, input_hash=input_hash, value=value
    )
    return EmitEventRequest(
        event_type=event_type,
        source=RUNTIME_WORKER_SOURCE,
        source_event_id=derive_source_event_id(run_id, command_seq, event_type, 0),
        payload_schema_ref=NODE_EXECUTED_SCHEMA_REF,
        payload=payload,
        occurred_at=occurred_at,
    )


def build_domain_view_emit_request(
    *,
    run_id: UUID,
    command_seq: int,
    tool_request_id: UUID,
    view_schema_ref: str,
    observed_at: datetime,
    source_ref: str,
    result_hash: str,
    item_count: int | None,
    occurred_at: datetime,
    event_type: str = "domain.view.accepted",
) -> EmitEventRequest:
    """Build the domain-view acceptance fact for one committed typed read."""

    payload = DomainViewAcceptedPayload(
        kind="domain_view_accepted",
        run_id=run_id,
        tool_request_id=tool_request_id,
        view_schema_ref=view_schema_ref,
        observed_at=observed_at,
        source_ref=source_ref,
        result_hash=result_hash,
        item_count=item_count,
    )
    return EmitEventRequest(
        event_type=event_type,
        source=RUNTIME_WORKER_SOURCE,
        source_event_id=derive_source_event_id(run_id, command_seq, event_type, 0),
        payload_schema_ref=DOMAIN_VIEW_ACCEPTED_SCHEMA_REF,
        payload=payload,
        occurred_at=occurred_at,
    )


# One run-visible answer message is chunked into bounded deltas so a single
# fact stays far below MAX_RUNTIME_EVENT_BYTES even for long assessments.
ANSWER_MESSAGE_CHUNK_CHARS = 4096
ANSWER_MESSAGE_CONTENT_SCHEMA_REF = "text.plain@1"


def build_answer_message_emit_requests(
    *,
    run_id: UUID,
    command_seq: int,
    answer: str,
    occurred_at: datetime,
) -> list[EmitEventRequest]:
    """Build the started/deltas/completed fact family for the typed answer.

    ``answer`` must already have passed the runtime structural gate; this
    builder only chunks and content-addresses it.  The completion marker
    hashes the exact concatenated delta text so the UI can verify assembly.
    """

    if type(answer) is not str or not answer:
        raise ValueError("answer must be a non-empty exact str")
    chunks = [
        answer[index : index + ANSWER_MESSAGE_CHUNK_CHARS]
        for index in range(0, len(answer), ANSWER_MESSAGE_CHUNK_CHARS)
    ]
    message_id = uuid5(NAMESPACE_URL, f"grove:answer-message:{run_id}:{command_seq}")
    requests = [
        EmitEventRequest(
            event_type="message.started",
            source=RUNTIME_WORKER_SOURCE,
            source_event_id=derive_source_event_id(run_id, command_seq, "message.started", 0),
            payload_schema_ref=MESSAGE_STARTED_SCHEMA_REF,
            payload=MessageStartedPayload(
                kind="message_started",
                run_id=run_id,
                message_id=message_id,
                content_schema_ref=ANSWER_MESSAGE_CONTENT_SCHEMA_REF,
            ),
            occurred_at=occurred_at,
        )
    ]
    for delta_seq, chunk in enumerate(chunks):
        requests.append(
            EmitEventRequest(
                event_type="message.delta",
                source=RUNTIME_WORKER_SOURCE,
                source_event_id=derive_source_event_id(run_id, command_seq, "message.delta", delta_seq),
                payload_schema_ref=MESSAGE_DELTA_SCHEMA_REF,
                payload=MessageDeltaPayload(
                    kind="message_delta",
                    run_id=run_id,
                    message_id=message_id,
                    delta_seq=delta_seq,
                    safe_delta=chunk,
                ),
                occurred_at=occurred_at,
            )
        )
    requests.append(
        EmitEventRequest(
            event_type="message.completed",
            source=RUNTIME_WORKER_SOURCE,
            source_event_id=derive_source_event_id(run_id, command_seq, "message.completed", 0),
            payload_schema_ref=MESSAGE_COMPLETED_SCHEMA_REF,
            payload=MessageCompletedPayload(
                kind="message_completed",
                run_id=run_id,
                message_id=message_id,
                last_delta_seq=len(chunks) - 1,
                content_hash=hashlib.sha256("".join(chunks).encode("utf-8")).hexdigest(),
            ),
            occurred_at=occurred_at,
        )
    )
    return requests


# ---------------------------------------------------------------------------
# Pure projection mapping: runtime lifecycle fact -> typed UI projection.
# ---------------------------------------------------------------------------

UI_PROJECTION_CONTRACT_VERSION = "v1"
UI_PROJECTION_SCHEMA_REF = "grove.ui.run-status-changed.v1"

# The canonical UI projection schema refs recognised by the headless reducer.
# Each maps to one discriminated variant of the frozen ``UIProjectionPayload``
# union; an unknown ref is never applied optimistically.
UI_MESSAGE_STARTED_SCHEMA_REF = "grove.ui.message-started.v1"
UI_MESSAGE_DELTA_SCHEMA_REF = "grove.ui.message-delta.v1"
UI_MESSAGE_COMPLETED_SCHEMA_REF = "grove.ui.message-completed.v1"
UI_INTERACTION_UPSERTED_SCHEMA_REF = "grove.ui.interaction-upserted.v1"
UI_INTERACTION_RESOLVED_SCHEMA_REF = "grove.ui.interaction-resolved.v1"
UI_DOMAIN_VIEW_SCHEMA_REF = "grove.ui.domain-view-accepted.v1"
UI_CHILD_STATUS_SCHEMA_REF = "grove.ui.child-status-changed.v1"
KNOWN_UI_PROJECTION_SCHEMA_REFS: frozenset[str] = frozenset(
    {
        UI_PROJECTION_SCHEMA_REF,
        UI_MESSAGE_STARTED_SCHEMA_REF,
        UI_MESSAGE_DELTA_SCHEMA_REF,
        UI_MESSAGE_COMPLETED_SCHEMA_REF,
        UI_INTERACTION_UPSERTED_SCHEMA_REF,
        UI_INTERACTION_RESOLVED_SCHEMA_REF,
        UI_DOMAIN_VIEW_SCHEMA_REF,
        UI_CHILD_STATUS_SCHEMA_REF,
    }
)


def lifecycle_to_run_status_changed(payload: RunLifecyclePayload) -> RunStatusChanged:
    """Map a runtime lifecycle fact to the typed UI projection payload."""

    return RunStatusChanged(
        kind="run_status_changed",
        run_id=payload.run_id,
        status=payload.status,
        run_revision=payload.run_revision,
    )


def domain_view_to_ui_accepted(payload: DomainViewAcceptedPayload) -> DomainViewAccepted:
    """Map a runtime domain-view fact to the typed UI projection payload."""

    return DomainViewAccepted(
        kind="domain_view_accepted",
        run_id=payload.run_id,
        tool_request_id=payload.tool_request_id,
        view_schema_ref=payload.view_schema_ref,
        observed_at=payload.observed_at,
        source_ref=payload.source_ref,
        result_hash=payload.result_hash,
        item_count=payload.item_count,
    )


def message_started_to_ui(payload: MessageStartedPayload) -> MessageStarted:
    """Map a runtime message-started fact to the typed UI projection payload."""

    return MessageStarted(
        kind="message_started",
        message_id=payload.message_id,
        owner_run_id=payload.run_id,
        role=payload.role,
        content_schema_ref=payload.content_schema_ref,
    )


def message_delta_to_ui(payload: MessageDeltaPayload) -> MessageDelta:
    """Map a runtime message-delta fact to the typed UI projection payload."""

    return MessageDelta(
        kind="message_delta",
        message_id=payload.message_id,
        delta_seq=payload.delta_seq,
        safe_delta=payload.safe_delta,
    )


def message_completed_to_ui(payload: MessageCompletedPayload) -> MessageCompleted:
    """Map a runtime message-completed fact to the typed UI projection payload."""

    return MessageCompleted(
        kind="message_completed",
        message_id=payload.message_id,
        last_delta_seq=payload.last_delta_seq,
        content_hash=payload.content_hash,
        artifact_ref=None,
    )


def build_ui_projection_meta(
    *,
    tenant_id: str,
    correlation_id: str,
    causation_id: UUID,
    trace_id: str | None = None,
) -> ContractMeta:
    """Build the ``ui.projection`` contract meta for one projected event."""

    return ContractMeta(
        contract_name="ui.projection",
        contract_version=UI_PROJECTION_CONTRACT_VERSION,
        message_id=uuid4(),
        tenant_id=tenant_id,
        correlation_id=correlation_id,
        causation_id=causation_id,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# Observation API cursors and read-model views.
# ---------------------------------------------------------------------------

ObservationCompleteness = Literal["complete", "partial", "stale", "unavailable"]


class EventCursor(BaseModel):
    """Cursor over a single run's commit-ordered runtime event stream."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_seq: int = Field(ge=0)


class ProjectionCursor(BaseModel):
    """Cursor over one target's commit-ordered UI projection stream."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    projection_seq: int = Field(ge=0)


class RunInspectView(BaseModel):
    """The safe, public Run Inspect view returned by the Observation API."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    tenant_id: str = Field(min_length=1, max_length=128)
    status: PublicRunStatus | None = None
    run_revision: int = Field(default=0, ge=0)
    completeness: ObservationCompleteness
    last_run_seq: int = Field(default=0, ge=0)
    last_projection_seq: int = Field(default=0, ge=0)
    unknown_schema_count: int = Field(default=0, ge=0)
    as_of: datetime


class RuntimeEventView(BaseModel):
    """The safe runtime event row exposed to API/SSE consumers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID
    run_id: UUID
    run_seq: int = Field(ge=1)
    event_type: str = Field(min_length=1, max_length=128)
    source: str = Field(min_length=1, max_length=128)
    source_event_id: str = Field(min_length=1, max_length=256)
    payload_schema_ref: str = Field(min_length=1, max_length=256)
    payload: dict[str, Any]
    occurred_at: datetime


class UIProjectionEventView(BaseModel):
    """The safe UI projection event row exposed to API/SSE consumers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID
    target_ref: UUID
    projection_seq: int = Field(ge=1)
    payload_schema_ref: str = Field(min_length=1, max_length=256)
    payload: dict[str, Any]
    projected_at: datetime


PayloadT = TypeVar("PayloadT", bound=CanonicalModel)
_UIPayloadAdapter: TypeAdapter[Any] | None = None


def ui_payload_adapter() -> TypeAdapter[Any]:
    """Lazily build the closed UI projection payload adapter."""

    global _UIPayloadAdapter
    if _UIPayloadAdapter is None:
        from app.contracts.canonical import UIProjectionPayload

        _UIPayloadAdapter = TypeAdapter(UIProjectionPayload)
    return _UIPayloadAdapter


__all__ = [
    "EventCursor",
    "EmitEventRequest",
    "ExecutionAuditAction",
    "ExecutionAuditPayload",
    "EXECUTION_AUDIT_SCHEMA_REF",
    "API_COMMAND_SOURCE",
    "MAX_RUNTIME_EVENT_BYTES",
    "NODE_EXECUTED_SCHEMA_REF",
    "NodeExecutedPayload",
    "ObservationCompleteness",
    "ProjectionCursor",
    "RUN_LIFECYCLE_SCHEMA_REF",
    "RUNTIME_WORKER_SOURCE",
    "RECONCILIATION_SOURCE",
    "RUNTIME_PAYLOAD_REGISTRY",
    "RunInspectView",
    "RunLifecyclePayload",
    "RuntimeEventView",
    "TERMINAL_RUN_STATUSES",
    "UI_PROJECTION_CONTRACT_VERSION",
    "UI_PROJECTION_SCHEMA_REF",
    "UIProjectionEventView",
    "UI_CHILD_STATUS_SCHEMA_REF",
    "UI_DOMAIN_VIEW_SCHEMA_REF",
    "UI_INTERACTION_RESOLVED_SCHEMA_REF",
    "UI_INTERACTION_UPSERTED_SCHEMA_REF",
    "UI_MESSAGE_COMPLETED_SCHEMA_REF",
    "UI_MESSAGE_DELTA_SCHEMA_REF",
    "UI_MESSAGE_STARTED_SCHEMA_REF",
    "UnknownRuntimeSchemaError",
    "DOMAIN_VIEW_ACCEPTED_SCHEMA_REF",
    "DomainViewAcceptedPayload",
    "PublicRunStatus",
    "build_domain_view_emit_request",
    "build_lifecycle_emit_request",
    "build_node_executed_emit_request",
    "build_execution_audit_emit_request",
    "build_ui_projection_meta",
    "derive_source_event_id",
    "domain_view_to_ui_accepted",
    "lifecycle_to_run_status_changed",
    "parse_runtime_payload",
    "ui_payload_adapter",
    "KNOWN_UI_PROJECTION_SCHEMA_REFS",
]
