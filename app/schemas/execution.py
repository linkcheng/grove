"""Closed WS-2 conformance submit/query schemas.

The public surface intentionally contains no resume/cancel/interrupt types.
Those commands belong to the later interaction workstream and must not leak
into the WS-2 persistence contract.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.contracts.canonical import SHA256_PATTERN
from app.releases.fixture import FixtureInput
from app.skill_abi.capability import PermissionPreset

_FIXTURE_SKILL_REF = "fixture.skill@1"
_FIXTURE_AGENT_REF = "fixture.agent@1"
_ALLOWED_PERMISSION_PRESETS = frozenset(f"permission.{preset.value}@1" for preset in PermissionPreset)


class ExecutionConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    deadline_ms: int | None = Field(default=None, ge=1, le=86_400_000)
    max_cost: float | None = Field(default=None, ge=0, le=1_000_000)
    data_residency: str | None = Field(default=None, min_length=1, max_length=128)


class ExecutionIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_id: UUID
    agent_ref: str | None = Field(default=None, min_length=1, max_length=256)
    skill_ref: str | None = Field(default=None, min_length=1, max_length=256)
    permission_preset_ref: str = Field(default="permission.interactive@1", min_length=1, max_length=256)
    input: FixtureInput
    constraints: ExecutionConstraints = Field(default_factory=ExecutionConstraints)

    @model_validator(mode="after")
    def validate_closed_fixture_refs(self) -> ExecutionIntent:
        if (self.agent_ref is None) == (self.skill_ref is None):
            raise ValueError("exactly one published agent_ref or skill_ref is required")
        if self.agent_ref is not None and self.agent_ref != _FIXTURE_AGENT_REF:
            raise ValueError("unknown agent_ref")
        if self.skill_ref is not None and self.skill_ref != _FIXTURE_SKILL_REF:
            raise ValueError("unknown skill_ref")
        if self.permission_preset_ref not in _ALLOWED_PERMISSION_PRESETS:
            raise ValueError("unknown permission preset")
        return self


class SubmitExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    submission_id: UUID
    intent: ExecutionIntent
    expected_skill_spec_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)


class RunHandle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    submission_id: UUID
    command_id: UUID
    status: Literal["accepted"]
    revision: int = Field(ge=0)
    skill_spec_hash: str = Field(pattern=SHA256_PATTERN)


class CommandReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: UUID
    run_id: UUID
    command_type: Literal["start"]
    command_schema_version: Literal["start.v1"]
    status: Literal["pending"]
    command_seq: int = Field(ge=0)


class RunQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run: RunHandle
    commands: list[CommandReceipt]


__all__ = [
    "CommandReceipt",
    "ExecutionConstraints",
    "ExecutionIntent",
    "FixtureInput",
    "RunHandle",
    "RunQuery",
    "SubmitExecution",
]
