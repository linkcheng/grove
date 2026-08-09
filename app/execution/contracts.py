"""Closed public contracts for the deterministic execution driver.

This module contains only boundary values and pure helpers.  Durable mutable
state belongs to :mod:`app.execution.state_machine`; the driver never stores a
second command index beside its immutable snapshot.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, TypeVar, cast
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.contracts.canonical import canonical_hash

HASH_PATTERN = r"^[0-9a-f]{64}$"
BIGINT_MAX = 2**63 - 1

type CommandType = Literal["start", "resume", "cancel", "continue", "signal"]
type RunStatus = Literal[
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
type CommandStatus = Literal["pending", "leased", "consumed", "dead_letter"]
type InternalAuthorityIssuer = Literal["driver_reconciler", "action_completion_bridge", "child_completion_bridge"]
TerminalRunStatus = frozenset({"succeeded", "failed", "cancelled"})


def _bounded_seconds(value: object, *, label: str, maximum: int | float) -> float:
    """Validate an exact numeric bound before converting it to ``float``."""

    if type(value) not in {int, float}:
        raise ValueError(f"{label} must be finite, positive, and <= {maximum:g}")
    numeric = cast(int | float, value)
    if (type(numeric) is float and not math.isfinite(numeric)) or numeric <= 0 or numeric > maximum:
        raise ValueError(f"{label} must be finite, positive, and <= {maximum:g}")
    return float(numeric)


class ExecutionDriverError(ValueError):
    """Base error for deterministic driver contract failures."""


@dataclass(frozen=True, slots=True)
class InternalDispatchAuthority:
    """Non-copyable-by-value capability used for internal command delivery."""

    issuer: InternalAuthorityIssuer

    def __post_init__(self) -> None:
        if self.issuer not in {"driver_reconciler", "action_completion_bridge", "child_completion_bridge"}:
            raise ValueError("unknown internal dispatch authority issuer")


class CommandConflict(ExecutionDriverError):
    """A command id was reused with a different immutable digest or binding."""


class RunSignalConflict(CommandConflict):
    """A trusted signal id was reused with a different digest."""


class CommandNotFound(ExecutionDriverError):
    """A worker referred to an unknown command."""


class RunNotFound(ExecutionDriverError):
    """A command or worker referred to an unknown run."""


class RunStateConflict(ExecutionDriverError):
    """A command is not valid for the run's current lifecycle state."""


class StaleExecutionFence(ExecutionDriverError):
    """A lease owner no longer has write authority for a run."""


class VersionUnavailable(ExecutionDriverError):
    """No worker with the command's exact runtime build is available."""


class ExecutionFenceExhausted(ExecutionDriverError):
    """The durable BIGINT execution fence cannot be incremented safely."""


class _CommandBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: UUID
    tenant_id: str = Field(min_length=1, max_length=128)
    run_id: UUID
    runtime_build_hash: str = Field(pattern=HASH_PATTERN)
    command_digest: str = Field(pattern=HASH_PATTERN)


class StartRun(_CommandBase):
    """Persisted public start command; input is an opaque artifact reference."""

    command_type: Literal["start"]
    command_schema_version: Literal["start.v1"]
    payload_ref: str = Field(min_length=1, max_length=512)
    payload_hash: str = Field(pattern=HASH_PATTERN)


class InterruptBinding(BaseModel):
    """Closed opaque binding for one user interrupt and checkpoint nonce."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    interrupt_ref: str = Field(min_length=1, max_length=512)
    interrupt_hash: str = Field(pattern=HASH_PATTERN)
    checkpoint_ref: str = Field(min_length=1, max_length=512)
    checkpoint_hash: str = Field(pattern=HASH_PATTERN)
    interrupt_schema_ref: str = Field(min_length=1, max_length=512)
    interrupt_schema_hash: str = Field(pattern=HASH_PATTERN)
    nonce_hash: str = Field(pattern=HASH_PATTERN)


class ResumeRun(_CommandBase):
    """Public resume command carrying only a content-addressed input ref."""

    command_type: Literal["resume"]
    command_schema_version: Literal["resume.v1"]
    expected_revision: int = Field(ge=0)
    input_ref: str = Field(min_length=1, max_length=512)
    input_hash: str = Field(pattern=HASH_PATTERN)
    interrupt: InterruptBinding


class CancelRun(_CommandBase):
    """Public cancellation command; no state patch or executable payload."""

    command_type: Literal["cancel"]
    command_schema_version: Literal["cancel.v1"]
    expected_revision: int = Field(ge=0, lt=BIGINT_MAX)
    reason_ref: str | None = Field(default=None, min_length=1, max_length=512)
    reason_hash: str | None = Field(default=None, pattern=HASH_PATTERN)

    @model_validator(mode="after")
    def _reason_pair(self) -> CancelRun:
        if (self.reason_ref is None) != (self.reason_hash is None):
            raise ValueError("reason_ref and reason_hash must be supplied together")
        return self


class ContinueRun(_CommandBase):
    """Driver/reconciler-only continuation with no user input."""

    command_type: Literal["continue"]
    command_schema_version: Literal["continue.v1"]
    revision: int = Field(ge=0)
    checkpoint_ref: str | None = Field(default=None, min_length=1, max_length=512)
    checkpoint_hash: str | None = Field(default=None, pattern=HASH_PATTERN)

    @model_validator(mode="after")
    def _checkpoint_pair(self) -> ContinueRun:
        if (self.checkpoint_ref is None) != (self.checkpoint_hash is None):
            raise ValueError("checkpoint_ref and checkpoint_hash must be supplied together")
        return self


class _SignalPayloadBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_ref: str = Field(min_length=1, max_length=512)
    source_fact_version: str = Field(min_length=1, max_length=128)
    source_fact_hash: str = Field(pattern=HASH_PATTERN)
    payload_ref: str = Field(min_length=1, max_length=512)
    payload_hash: str = Field(pattern=HASH_PATTERN)


class ActionCompletionPayload(_SignalPayloadBase):
    """Typed result payload produced by an ActionCompletionBridge."""

    payload_type: Literal["action_completed"]


class ChildCompletionPayload(_SignalPayloadBase):
    """Typed result payload produced by a ChildCompletionBridge."""

    payload_type: Literal["child_run_completed"]


type SignalPayload = Annotated[
    ActionCompletionPayload | ChildCompletionPayload,
    Field(discriminator="payload_type"),
]


class RunSignal(_CommandBase):
    """Trusted internal signal; it is distinct from public ``ResumeRun``."""

    command_type: Literal["signal"]
    command_schema_version: Literal["signal.v1"]
    signal_id: UUID
    wait_ref: str = Field(min_length=1, max_length=512)
    wait_hash: str = Field(pattern=HASH_PATTERN)
    payload: SignalPayload


type ExecutionCommand = Annotated[
    StartRun | ResumeRun | CancelRun | ContinueRun | RunSignal,
    Field(discriminator="command_type"),
]


class RunCommandReceipt(BaseModel):
    """Immutable command acceptance/status metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: UUID
    tenant_id: str = Field(min_length=1, max_length=128)
    run_id: UUID
    command_seq: int = Field(ge=0)
    command_type: CommandType
    command_schema_version: str = Field(min_length=1, max_length=64)
    command_digest: str = Field(pattern=HASH_PATTERN)
    runtime_build_hash: str = Field(pattern=HASH_PATTERN)
    status: CommandStatus
    lease_owner: str | None = Field(default=None, min_length=1, max_length=256)
    execution_fence: int | None = Field(default=None, ge=0)
    lease_until: datetime | None = None

    @field_validator("lease_until")
    @classmethod
    def _lease_is_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("lease_until must be timezone-aware")
        return None if value is None else value.astimezone(UTC)


class ExecutionClaim(BaseModel):
    """Immutable proof of one worker's current fenced lease."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: UUID
    tenant_id: str = Field(min_length=1, max_length=128)
    run_id: UUID
    command_seq: int = Field(ge=0)
    command_digest: str = Field(pattern=HASH_PATTERN)
    runtime_build_hash: str = Field(pattern=HASH_PATTERN)
    worker_id: str = Field(min_length=1, max_length=256)
    execution_fence: int = Field(ge=1)
    lease_until: datetime

    @field_validator("lease_until")
    @classmethod
    def _lease_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("lease_until must be timezone-aware")
        return value.astimezone(UTC)


class AppliedCommandMetadata(BaseModel):
    """Opaque checkpoint metadata proving a command semantic application."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1, max_length=128)
    run_id: UUID
    command_id: UUID
    command_seq: int = Field(ge=0)
    command_digest: str = Field(pattern=HASH_PATTERN)
    checkpoint_ref: str = Field(min_length=1, max_length=512)
    checkpoint_hash: str = Field(pattern=HASH_PATTERN)
    runtime_build_hash: str = Field(pattern=HASH_PATTERN)
    execution_fence: int = Field(ge=1)


class DeliveryReceipt(BaseModel):
    """Result of an atomic finish_delivery operation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    result_code: str = Field(pattern="^consumed$")
    command_id: UUID
    tenant_id: str = Field(min_length=1, max_length=128)
    run_id: UUID
    command_seq: int = Field(ge=0)
    command_type: str = Field(min_length=1, max_length=16)
    command_schema_version: str = Field(min_length=1, max_length=32)
    command_digest: str = Field(pattern=HASH_PATTERN)
    runtime_build_hash: str = Field(pattern=HASH_PATTERN)
    status: str = Field(min_length=1, max_length=16)
    continue_command_id: UUID | None = None
    continue_command_seq: int | None = Field(default=None, ge=0)
    run_revision: int = Field(ge=0)


_ModelT = TypeVar("_ModelT", bound=BaseModel)

# Only exact model classes listed here may cross a trust seam.  In particular,
# ``model_copy(update=...)`` is not evidence of a freshly validated value.
_STRICT_MODEL_TYPES: frozenset[type[BaseModel]] = frozenset(
    {
        StartRun,
        ResumeRun,
        CancelRun,
        ContinueRun,
        ActionCompletionPayload,
        ChildCompletionPayload,
        RunSignal,
        InterruptBinding,
       RunCommandReceipt,
       ExecutionClaim,
       AppliedCommandMetadata,
       DeliveryReceipt,
   }
)
_COMMAND_TYPES: frozenset[type[BaseModel]] = frozenset({StartRun, ResumeRun, CancelRun, ContinueRun, RunSignal})


def _raw_model_fields(value: BaseModel) -> dict[str, object]:
    """Read a model's raw fields without allowing hidden extras."""

    model_type = type(value)
    if model_type not in _STRICT_MODEL_TYPES:
        raise TypeError(f"untrusted model type: {model_type.__name__}")
    raw = dict(vars(value))
    unknown = set(raw).difference(model_type.model_fields)
    if unknown or getattr(value, "__pydantic_extra__", None):
        raise ValueError(f"untrusted model fields: {sorted(unknown)}")
    return raw


def _prepare_strict_input(value: object) -> object:
    """Recursively expose nested model raw fields to strict validation."""

    value_type = type(value)
    if value_type in _STRICT_MODEL_TYPES:
        raw = _raw_model_fields(cast(BaseModel, value))
        return {key: _prepare_strict_input(item) for key, item in raw.items()}
    if isinstance(value, BaseModel):
        raise TypeError(f"untrusted model type: {value_type.__name__}")
    if value_type is dict:
        prepared: dict[str, object] = {}
        for key, item in cast(dict[object, object], value).items():
            if type(key) is not str:
                raise TypeError("model input keys must be exact strings")
            prepared[key] = _prepare_strict_input(item)
        return prepared
    if value_type is list:
        return [_prepare_strict_input(item) for item in cast(list[object], value)]
    if value_type is tuple:
        return tuple(_prepare_strict_input(item) for item in cast(tuple[object, ...], value))
    if value is None or value_type in {str, int, float, bool, UUID, datetime}:
        return value
    raise TypeError(f"untrusted primitive type: {value_type.__name__}")


def _strict_validate_raw(raw: Mapping[str, object], expected_type: type[_ModelT]) -> _ModelT:
    prepared = cast(dict[str, Any], _prepare_strict_input(raw))
    validated = expected_type.model_validate(prepared, strict=True)
    if type(validated) is not expected_type:
        raise TypeError(f"validated model type changed: {type(validated).__name__}")
    return validated


def _strict_validate(value: object, expected_type: type[_ModelT]) -> _ModelT:
    if type(value) is not expected_type:
        raise ExecutionDriverError(f"expected exact {expected_type.__name__} instance")
    try:
        return _strict_validate_raw(_raw_model_fields(cast(BaseModel, value)), expected_type)
    except (ValidationError, TypeError, ValueError) as exc:
        if isinstance(exc, ExecutionDriverError):
            raise
        raise ExecutionDriverError(f"strict {expected_type.__name__} validation failed") from exc


def _strict_identity(value: object, expected_type: type[_ModelT]) -> tuple[_ModelT, str]:
    trusted = _strict_validate(value, expected_type)
    return trusted, canonical_hash(trusted)


def _replace_strict(value: _ModelT, expected_type: type[_ModelT], **updates: object) -> _ModelT:
    raw = _raw_model_fields(value)
    raw.update(updates)
    return _strict_validate_raw(raw, expected_type)


def _strict_command(command: object) -> tuple[ExecutionCommand, str]:
    command_type = type(command)
    if command_type not in _COMMAND_TYPES:
        raise TypeError("execution command must be an exact known concrete model")
    if command_type is StartRun:
        trusted, fingerprint = _strict_identity(command, cast(type[BaseModel], StartRun))
    elif command_type is ResumeRun:
        trusted, fingerprint = _strict_identity(command, cast(type[BaseModel], ResumeRun))
    elif command_type is CancelRun:
        trusted, fingerprint = _strict_identity(command, cast(type[BaseModel], CancelRun))
    elif command_type is ContinueRun:
        trusted, fingerprint = _strict_identity(command, cast(type[BaseModel], ContinueRun))
    else:
        trusted, fingerprint = _strict_identity(command, cast(type[BaseModel], RunSignal))
    return cast(ExecutionCommand, trusted), fingerprint


def _strict_claim(claim: object) -> tuple[ExecutionClaim, str]:
    return _strict_identity(claim, ExecutionClaim)


def _strict_metadata(metadata: object) -> tuple[AppliedCommandMetadata, str]:
    return _strict_identity(metadata, AppliedCommandMetadata)


_CONTINUE_NAMESPACE = UUID("8d3e2e5a-5d3c-4b4a-a509-a955650e0b09")
_SIGNAL_NAMESPACE = UUID("bc9ebf9d-076a-4b26-978e-4d03099d1c1e")
_SIGNAL_FACT_NAMESPACE = UUID("0e4a14b0-8fc1-4d76-b9bb-f2d4cba8e0b8")


def derive_continue_command_id(tenant_id: str, run_id: UUID, revision: int) -> UUID:
    """Derive the one continuation identity for ``(tenant, run, revision)``."""

    if (
        type(tenant_id) is not str
        or not tenant_id
        or type(run_id) is not UUID
        or type(revision) is not int
        or revision < 0
    ):
        raise ValueError("tenant_id, run_id and non-negative revision are required")
    return uuid5(
        _CONTINUE_NAMESPACE,
        canonical_hash(
            {
                "command_type": "continue",
                "tenant_id": tenant_id,
                "run_id": run_id,
                "revision": revision,
            }
        ),
    )


def derive_signal_command_id(tenant_id: str, run_id: UUID, signal_id: UUID) -> UUID:
    """Derive a stable command id for one trusted terminal fact."""

    if type(tenant_id) is not str or not tenant_id or type(run_id) is not UUID or type(signal_id) is not UUID:
        raise ValueError("tenant_id, run_id and signal_id are required")
    return uuid5(
        _SIGNAL_NAMESPACE,
        canonical_hash(
            {
                "command_type": "signal",
                "tenant_id": tenant_id,
                "run_id": run_id,
                "signal_id": signal_id,
            }
        ),
    )


def derive_signal_id(source_ref: str, source_fact_version: str, source_fact_hash: str) -> UUID:
    """Derive one stable signal identity from a terminal source fact."""

    if (
        type(source_ref) is not str
        or not source_ref
        or type(source_fact_version) is not str
        or not source_fact_version
        or type(source_fact_hash) is not str
        or not re.fullmatch(HASH_PATTERN, source_fact_hash)
    ):
        raise ValueError("source terminal fact binding is invalid")
    return uuid5(
        _SIGNAL_FACT_NAMESPACE,
        canonical_hash(
            {
                "source_fact_hash": source_fact_hash,
                "source_fact_version": source_fact_version,
                "source_ref": source_ref,
            }
        ),
    )


__all__ = [
    "ActionCompletionPayload",
    "AppliedCommandMetadata",
    "CancelRun",
    "ChildCompletionPayload",
    "CommandConflict",
    "CommandNotFound",
   "CommandStatus",
   "CommandType",
   "ContinueRun",
   "DeliveryReceipt",
   "ExecutionClaim",
    "ExecutionCommand",
    "ExecutionDriverError",
    "ExecutionFenceExhausted",
    "BIGINT_MAX",
    "HASH_PATTERN",
    "InternalDispatchAuthority",
    "InternalAuthorityIssuer",
    "InterruptBinding",
    "ResumeRun",
    "RunCommandReceipt",
    "RunNotFound",
    "RunSignal",
    "RunSignalConflict",
    "RunStateConflict",
    "RunStatus",
    "SignalPayload",
    "StartRun",
    "StaleExecutionFence",
    "TerminalRunStatus",
    "VersionUnavailable",
    "_COMMAND_TYPES",
    "_ModelT",
    "_bounded_seconds",
    "_prepare_strict_input",
    "_raw_model_fields",
    "_replace_strict",
    "_strict_claim",
    "_strict_command",
    "_strict_identity",
    "_strict_metadata",
    "_strict_validate",
    "_strict_validate_raw",
    "derive_continue_command_id",
    "derive_signal_command_id",
    "derive_signal_id",
]
