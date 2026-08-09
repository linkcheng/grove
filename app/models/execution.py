"""SQLAlchemy models for the tenant-owned WS-2 submit/query seam."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from sqlalchemy.types import DateTime

from app.db.base import Base


class Tenant(Base):
    __tablename__ = "tenant"

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (CheckConstraint("status IN ('active', 'suspended')", name="tenant_status_ck"),)


class Membership(Base):
    __tablename__ = "membership"

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    principal_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    principal_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="human", server_default="human")
    user_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    roles: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenant.tenant_id"], name="membership_tenant_fk"),
        CheckConstraint("principal_kind = 'human'", name="membership_principal_kind_ck"),
        Index("membership_tenant_principal_idx", "tenant_id", "principal_id"),
    )


class WorkloadPrincipal(Base):
    __tablename__ = "workload_principal"

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    principal_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    principal_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="workload", server_default="workload"
    )
    workload_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenant.tenant_id"], name="workload_tenant_fk"),
        CheckConstraint("principal_kind = 'workload'", name="workload_principal_kind_ck"),
        Index("workload_tenant_principal_idx", "tenant_id", "principal_id"),
    )


class ExecutionPrincipal(Base):
    """The polymorphic principal FK target, materialized only from an identity row."""

    __tablename__ = "execution_principal"

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    principal_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    principal_kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenant.tenant_id"], name="execution_principal_tenant_fk"),
        CheckConstraint("principal_kind IN ('human', 'workload')", name="execution_principal_kind_ck"),
    )


class ExecutionSpec(Base):
    """Immutable, tenant-scoped WS-1 SkillExecutionSpec snapshot."""

    __tablename__ = "execution_spec"

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    skill_spec_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    spec_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    spec_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenant.tenant_id"], name="execution_spec_tenant_fk"),
        UniqueConstraint("tenant_id", "skill_spec_hash", "spec_ref", name="execution_spec_tenant_hash_ref_uq"),
        CheckConstraint("length(skill_spec_hash) = 64", name="execution_spec_hash_ck"),
        Index("execution_spec_tenant_hash_idx", "tenant_id", "skill_spec_hash"),
    )


class CommandPayload(Base):
    """One immutable tenant-scoped typed payload artifact referenced by a command."""

    __tablename__ = "command_payload"

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload_ref: Mapped[str] = mapped_column(String(256), primary_key=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    command_schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    sensitivity: Mapped[str] = mapped_column(
        String(32), nullable=False, default="sensitive", server_default="sensitive"
    )
    retention: Mapped[str] = mapped_column(
        String(32), nullable=False, default="run_completion", server_default="run_completion"
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenant.tenant_id"], name="command_payload_tenant_fk"),
        UniqueConstraint("tenant_id", "payload_hash", name="command_payload_hash_uq"),
        UniqueConstraint(
            "tenant_id",
            "payload_ref",
            "payload_hash",
            "command_schema_version",
            name="command_payload_ref_hash_schema_uq",
        ),
        CheckConstraint("length(payload_hash) = 64", name="command_payload_hash_ck"),
        CheckConstraint(
            "command_schema_version IN ('start.v1', 'resume.v1', 'cancel.v1', 'continue.v1', 'signal.v1')",
            name="command_payload_schema_version_ck",
        ),
        CheckConstraint("sensitivity = 'sensitive'", name="command_payload_sensitivity_ck"),
        CheckConstraint("retention = 'run_completion'", name="command_payload_retention_ck"),
    )


class AgentRun(Base):
    __tablename__ = "agent_run"

    run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    submission_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    submission_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    principal_id: Mapped[str] = mapped_column(String(128), nullable=False)
    principal_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    skill_spec_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    skill_spec_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    runtime_build_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    runtime_build_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="accepted", server_default="accepted")
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    execution_fence: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    lease_owner: Mapped[str | None] = mapped_column(String(256))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latest_checkpoint_id: Mapped[str | None] = mapped_column(String(256))
    latest_applied_command_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    latest_applied_command_digest: Mapped[str | None] = mapped_column(String(64))
    latest_applied_command_seq: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenant.tenant_id"], name="agent_run_tenant_fk"),
        ForeignKeyConstraint(
            ["tenant_id", "principal_id", "principal_kind"],
            ["execution_principal.tenant_id", "execution_principal.principal_id", "execution_principal.principal_kind"],
            name="agent_run_principal_fk",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "skill_spec_hash", "skill_spec_ref"],
            ["execution_spec.tenant_id", "execution_spec.skill_spec_hash", "execution_spec.spec_ref"],
            name="agent_run_spec_fk",
        ),
        UniqueConstraint("tenant_id", "submission_id", name="agent_run_tenant_submission_uq"),
        UniqueConstraint("tenant_id", "run_id", name="agent_run_tenant_run_uq"),
        UniqueConstraint("tenant_id", "run_id", "principal_id", "principal_kind", name="agent_run_run_principal_uq"),
        CheckConstraint("principal_kind IN ('human', 'workload')", name="agent_run_principal_kind_ck"),
        CheckConstraint(
            "status IN ('accepted', 'running', 'waiting_user_input', 'waiting_action_result', "
            "'waiting_child_result', 'cancel_requested', 'succeeded', 'failed', 'cancelled')",
            name="agent_run_status_ck",
        ),
        CheckConstraint("revision >= 0", name="agent_run_revision_ck"),
        CheckConstraint("execution_fence >= 0", name="agent_run_execution_fence_ck"),
        CheckConstraint(
            "latest_applied_command_seq IS NULL OR latest_applied_command_seq >= 0",
            name="agent_run_latest_applied_seq_ck",
        ),
        CheckConstraint("length(submission_digest) = 64", name="agent_run_submission_digest_ck"),
        CheckConstraint("length(skill_spec_hash) = 64", name="agent_run_skill_spec_hash_ck"),
        CheckConstraint(
            "runtime_build_hash ~ '^[0-9a-f]{64}$'",
            name="agent_run_runtime_build_hash_ck",
        ),
        Index("agent_run_tenant_idx", "tenant_id", "run_id"),
    )


class RunCommand(Base):
    __tablename__ = "run_command"

    command_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    principal_id: Mapped[str] = mapped_column(String(128), nullable=False)
    principal_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    command_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    command_type: Mapped[str] = mapped_column(String(16), nullable=False, default="start", server_default="start")
    command_schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    command_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    lease_owner: Mapped[str | None] = mapped_column(String(256))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_fence: Mapped[int | None] = mapped_column(BigInteger)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error_ref: Mapped[str | None] = mapped_column(String(512))
    consumed_worker_id: Mapped[str | None] = mapped_column(String(256))
    consumed_execution_fence: Mapped[int | None] = mapped_column(BigInteger)
    consumed_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_claim_provenance_hash: Mapped[str | None] = mapped_column(String(64))
    superseded_by_command_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    superseded_by_command_seq: Mapped[int | None] = mapped_column(BigInteger)
    superseded_by_command_digest: Mapped[str | None] = mapped_column(String(64))
    superseded_by_provenance_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["agent_run.tenant_id", "agent_run.run_id"],
            name="run_command_run_fk",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "principal_id", "principal_kind"],
            ["agent_run.tenant_id", "agent_run.run_id", "agent_run.principal_id", "agent_run.principal_kind"],
            name="run_command_principal_fk",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "principal_id", "principal_kind"],
            ["execution_principal.tenant_id", "execution_principal.principal_id", "execution_principal.principal_kind"],
            name="run_command_identity_fk",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "payload_ref", "payload_hash", "command_schema_version"],
            [
                "command_payload.tenant_id",
                "command_payload.payload_ref",
                "command_payload.payload_hash",
                "command_payload.command_schema_version",
            ],
            name="run_command_payload_fk",
        ),
        UniqueConstraint("tenant_id", "command_id", name="run_command_tenant_command_uq"),
        UniqueConstraint("tenant_id", "run_id", "command_seq", name="run_command_run_seq_uq"),
        CheckConstraint("principal_kind IN ('human', 'workload')", name="run_command_principal_kind_ck"),
        CheckConstraint(
            "command_type IN ('start', 'resume', 'cancel', 'continue', 'signal')",
            name="run_command_type_ck",
        ),
        CheckConstraint(
            "(command_type = 'start' AND command_schema_version = 'start.v1') OR "
            "(command_type = 'resume' AND command_schema_version = 'resume.v1') OR "
            "(command_type = 'cancel' AND command_schema_version = 'cancel.v1') OR "
            "(command_type = 'continue' AND command_schema_version = 'continue.v1') OR "
            "(command_type = 'signal' AND command_schema_version = 'signal.v1')",
            name="run_command_schema_version_ck",
        ),
        CheckConstraint("status IN ('pending', 'leased', 'consumed', 'dead_letter')", name="run_command_status_ck"),
        CheckConstraint("command_seq >= 0", name="run_command_seq_ck"),
        CheckConstraint("attempt_count >= 0", name="run_command_attempt_count_ck"),
        CheckConstraint(
            "(status = 'leased' AND lease_owner IS NOT NULL AND lease_until IS NOT NULL "
            "AND execution_fence IS NOT NULL) OR "
            "(status <> 'leased' AND lease_owner IS NULL AND lease_until IS NULL "
            "AND execution_fence IS NULL)",
            name="run_command_lease_shape_ck",
        ),
        CheckConstraint("length(command_digest) = 64", name="run_command_digest_ck"),
        CheckConstraint("length(payload_hash) = 64", name="run_command_payload_hash_ck"),
        CheckConstraint(
            "(status = 'consumed' AND consumed_worker_id IS NOT NULL "
            "AND consumed_execution_fence IS NOT NULL AND consumed_lease_until IS NOT NULL "
            "AND consumed_claim_provenance_hash ~ '^[0-9a-f]{64}$') OR "
            "(status <> 'consumed' AND consumed_worker_id IS NULL "
            "AND consumed_execution_fence IS NULL AND consumed_lease_until IS NULL "
            "AND consumed_claim_provenance_hash IS NULL)",
            name="run_command_consumed_provenance_ck",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "superseded_by_command_id"],
            ["run_command.tenant_id", "run_command.command_id"],
            name="run_command_superseded_target_fk",
        ),
        CheckConstraint(
            "(superseded_by_command_id IS NOT NULL AND superseded_by_command_seq IS NOT NULL "
            "AND superseded_by_command_digest IS NOT NULL "
            "AND superseded_by_command_digest ~ '^[0-9a-f]{64}$' "
            "AND (superseded_by_provenance_hash IS NULL OR "
            "superseded_by_provenance_hash ~ '^[0-9a-f]{64}$')) OR "
            "(superseded_by_command_id IS NULL AND superseded_by_command_seq IS NULL "
            "AND superseded_by_command_digest IS NULL AND superseded_by_provenance_hash IS NULL)",
            name="run_command_superseded_provenance_ck",
        ),
        Index("run_command_tenant_run_idx", "tenant_id", "run_id", "command_seq"),
    )
