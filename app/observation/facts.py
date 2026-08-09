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

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, TypeVar
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.contracts.canonical import (
    SHA256_PATTERN,
    CanonicalModel,
    ContractMeta,
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

KNOWN_RUNTIME_PAYLOAD_SCHEMAS: frozenset[str] = frozenset(
    {RUN_LIFECYCLE_SCHEMA_REF, NODE_EXECUTED_SCHEMA_REF}
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


# Registry of the only schema refs the projection may materialise.  An unknown
# ref never reaches a Python payload model; the projection dead-letters it.
RUNTIME_PAYLOAD_REGISTRY: Mapping[str, type[CanonicalModel]] = {
    RUN_LIFECYCLE_SCHEMA_REF: RunLifecyclePayload,
    NODE_EXECUTED_SCHEMA_REF: NodeExecutedPayload,
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

# The runtime worker is the single observation source for the conformance slice.
RUNTIME_WORKER_SOURCE = "grove.runtime_worker"
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


# ---------------------------------------------------------------------------
# Pure projection mapping: runtime lifecycle fact -> typed UI projection.
# ---------------------------------------------------------------------------

UI_PROJECTION_CONTRACT_VERSION = "v1"
UI_PROJECTION_SCHEMA_REF = "grove.ui.run-status-changed.v1"


def lifecycle_to_run_status_changed(payload: RunLifecyclePayload) -> RunStatusChanged:
    """Map a runtime lifecycle fact to the typed UI projection payload."""

    return RunStatusChanged(
        kind="run_status_changed",
        run_id=payload.run_id,
        status=payload.status,
        run_revision=payload.run_revision,
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
    "MAX_RUNTIME_EVENT_BYTES",
    "NODE_EXECUTED_SCHEMA_REF",
    "NodeExecutedPayload",
    "ObservationCompleteness",
    "ProjectionCursor",
    "RUN_LIFECYCLE_SCHEMA_REF",
    "RUNTIME_WORKER_SOURCE",
    "RUNTIME_PAYLOAD_REGISTRY",
    "RunInspectView",
    "RunLifecyclePayload",
    "RuntimeEventView",
    "TERMINAL_RUN_STATUSES",
    "UI_PROJECTION_CONTRACT_VERSION",
    "UI_PROJECTION_SCHEMA_REF",
    "UIProjectionEventView",
    "UnknownRuntimeSchemaError",
    "PublicRunStatus",
    "build_lifecycle_emit_request",
    "build_node_executed_emit_request",
    "build_ui_projection_meta",
    "derive_source_event_id",
    "lifecycle_to_run_status_changed",
    "parse_runtime_payload",
    "ui_payload_adapter",
]
