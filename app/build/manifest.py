"""Canonical, content-addressed RuntimeBuildManifest generation and verification."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app import __version__
from app.build.catalog_authority import (
    CATALOG_AUTHORITY_COMPILER_VERSION,
    CatalogAuthorityError,
    expected_catalog_artifact_hash,
    expected_catalog_authority_root,
    expected_catalog_authority_sections,
    trusted_catalog_authority_anchor,
)
from app.contracts.canonical import canonical_bytes as canonical_contract_bytes
from app.core.config import Role

MANIFEST_SCHEMA_VERSION = 1
ManifestMode = Literal["draft", "release"]
EVIDENCE_PLACEHOLDER = "not_generated"
EVIDENCE_REF_PATTERN = re.compile(r"^ci-evidence/sha256/([0-9a-f]{64})/[A-Za-z0-9_.-]+$")
MIGRATION_EXECUTION_FILES = (
    "alembic.ini",
    "alembic/env.py",
    "alembic/script.py.mako",
    "app/build/catalog_authority.py",
    "app/build/downgrade_preflight.py",
    "app/build/ws3_catalog_authority_v1.json",
)
WS2_BUSINESS_RELATIONS = frozenset(
    {
        "tenant",
        "membership",
        "workload_principal",
        "execution_principal",
        "execution_spec",
        "command_payload",
        "agent_run",
        "run_command",
    }
)
WS3_CHECKPOINT_RELATIONS = frozenset({"checkpoints", "checkpoint_blobs", "checkpoint_writes"})
WS3_INFRASTRUCTURE_RELATIONS = frozenset({"checkpoint_migrations"})
WS3_BUSINESS_RELATIONS = WS2_BUSINESS_RELATIONS | WS3_CHECKPOINT_RELATIONS
WS3_SCHEMA_CONTRACT_VERSION = "ws3-execution-authority-v8"

# WS-4 adds the observation slice (runtime event/outbox, rebuildable UI
# projection read model, projection watermark, dead-letter).  These are
# authoritative committed facts, not infrastructure; they extend the WS-3
# business relation set.  No new infrastructure tables are introduced.
WS4_OBSERVATION_RELATIONS = frozenset(
    {
        "runtime_event",
        "runtime_event_outbox",
        "ui_projection_event",
        "projection_watermark",
        "runtime_event_dead_letter",
    }
)
WS4_BUSINESS_RELATIONS = WS3_BUSINESS_RELATIONS | WS4_OBSERVATION_RELATIONS
WS4_MIGRATION_HEADS = frozenset(
    {
        "ws4_observation_slice",
        "ws4_recon_helpers",
        "ws4_authority_audit_emitters",
        "ws3_consumed_provenance_compat",
    }
)

# v7 keeps an independent expected object inventory.  The reader must first
# enumerate this complete public `pg_class` universe (including empty
# relkinds) and only then project the twelve relation entries below.  This
# prevents a new object from disappearing merely because it is absent from the
# authority relation registry.
WS3_AUTHORITY_OBJECT_RELKINDS = ("r", "p", "S", "v", "m", "f", "c", "i")
WS3_AUTHORITY_INDEX_SPECS: dict[str, tuple[str, bool, tuple[str, ...]]] = {
    "agent_run_pkey": ("agent_run", True, ("run_id",)),
    "agent_run_run_principal_uq": ("agent_run", True, ("tenant_id", "run_id", "principal_id", "principal_kind")),
    "agent_run_tenant_idx": ("agent_run", False, ("tenant_id", "run_id")),
    "agent_run_tenant_run_uq": ("agent_run", True, ("tenant_id", "run_id")),
    "agent_run_tenant_submission_uq": ("agent_run", True, ("tenant_id", "submission_id")),
    "alembic_version_pkc": ("alembic_version", True, ("version_num",)),
    "checkpoint_blobs_pkey": (
        "checkpoint_blobs",
        True,
        ("thread_id", "checkpoint_ns", "channel", "version"),
    ),
    "checkpoint_blobs_thread_id_idx": ("checkpoint_blobs", False, ("thread_id",)),
    "checkpoint_migrations_pkey": ("checkpoint_migrations", True, ("v",)),
    "checkpoint_writes_pkey": (
        "checkpoint_writes",
        True,
        ("thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "idx"),
    ),
    "checkpoint_writes_thread_id_idx": ("checkpoint_writes", False, ("thread_id",)),
    "checkpoints_pkey": ("checkpoints", True, ("thread_id", "checkpoint_ns", "checkpoint_id")),
    "checkpoints_thread_id_idx": ("checkpoints", False, ("thread_id",)),
    "command_payload_hash_uq": ("command_payload", True, ("tenant_id", "payload_hash")),
    "command_payload_pkey": ("command_payload", True, ("tenant_id", "payload_ref")),
    "command_payload_ref_hash_schema_uq": (
        "command_payload",
        True,
        ("tenant_id", "payload_ref", "payload_hash", "command_schema_version"),
    ),
    "execution_principal_pkey": ("execution_principal", True, ("tenant_id", "principal_id", "principal_kind")),
    "execution_spec_pkey": ("execution_spec", True, ("tenant_id", "skill_spec_hash")),
    "execution_spec_tenant_hash_idx": ("execution_spec", False, ("tenant_id", "skill_spec_hash")),
    "execution_spec_tenant_hash_ref_uq": (
        "execution_spec",
        True,
        ("tenant_id", "skill_spec_hash", "spec_ref"),
    ),
    "membership_pkey": ("membership", True, ("tenant_id", "principal_id")),
    "membership_tenant_principal_idx": ("membership", False, ("tenant_id", "principal_id")),
    "run_command_pkey": ("run_command", True, ("command_id",)),
    "run_command_run_seq_uq": ("run_command", True, ("tenant_id", "run_id", "command_seq")),
    "run_command_tenant_command_uq": ("run_command", True, ("tenant_id", "command_id")),
    "run_command_tenant_run_idx": ("run_command", False, ("tenant_id", "run_id", "command_seq")),
    "tenant_pkey": ("tenant", True, ("tenant_id",)),
    "workload_principal_pkey": ("workload_principal", True, ("tenant_id", "principal_id")),
    "workload_tenant_principal_idx": ("workload_principal", False, ("tenant_id", "principal_id")),
}
WS3_AUTHORITY_OBJECT_TABLE_NAMES = (
    "agent_run",
    "alembic_version",
    "checkpoint_blobs",
    "checkpoint_migrations",
    "checkpoint_writes",
    "checkpoints",
    "command_payload",
    "execution_principal",
    "execution_spec",
    "membership",
    "run_command",
    "tenant",
    "workload_principal",
)
WS3_AUTHORITY_OBJECT_COMPOSITE_NAMES = ("geometry_dump", "valid_detail")


def _index_definition(name: str, table: str, unique: bool, columns: tuple[str, ...]) -> str:
    prefix = "CREATE UNIQUE INDEX" if unique else "CREATE INDEX"
    return f"{prefix} {name} ON public.{table} USING btree ({', '.join(columns)})"


def _authority_object_expected_facts() -> dict[str, dict[str, Any]]:
    objects: dict[str, dict[str, Any]] = {}
    for name in WS3_AUTHORITY_OBJECT_TABLE_NAMES:
        registry_entry = {
            "schema": "public",
            "name": name,
            "relkind": "r",
            "owner": "grove_migration",
            "relpersistence": "p",
            "replica_identity": "d",
            "reloptions": [],
            "rls": [False, False] if name in {"alembic_version", "checkpoint_migrations"} else [True, True],
            "is_partition": False,
            "parent": None,
            "children": [],
            "partition_bound": None,
            "index": None,
        }
        objects[f"public.{name}"] = registry_entry
    for name in WS3_AUTHORITY_OBJECT_COMPOSITE_NAMES:
        objects[f"public.{name}"] = {
            "schema": "public",
            "name": name,
            "relkind": "c",
            "owner": "grove",
            "relpersistence": "p",
            "replica_identity": "n",
            "reloptions": [],
            "rls": [False, False],
            "is_partition": False,
            "parent": None,
            "children": [],
            "partition_bound": None,
            "index": None,
        }
    for name, (table, unique, columns) in WS3_AUTHORITY_INDEX_SPECS.items():
        objects[f"public.{name}"] = {
            "schema": "public",
            "name": name,
            "relkind": "i",
            "owner": "grove_migration",
            "relpersistence": "p",
            "replica_identity": "n",
            "reloptions": [],
            "rls": [False, False],
            "is_partition": False,
            "parent": None,
            "children": [],
            "partition_bound": None,
            "index": {
                "table": f"public.{table}",
                "method": "btree",
                "unique": unique,
                "primary": name.endswith(("_pkey", "_pkc")),
                "valid": True,
                "ready": True,
                "live": True,
                "definition": _index_definition(name, table, unique, columns),
            },
        }
    return objects


WS3_AUTHORITY_OBJECT_INVENTORY = _authority_object_expected_facts()

# The authority surface is intentionally defined once.  Migration evidence,
# manifest expectations and the preflight relation/privilege closure all read
# this registry; they must not grow independent, copied relation lists.
WS3_AUTHORITY_ONLINE_ROLES = (
    "grove_api",
    "grove_runtime",
    "grove_projection",
    "grove_governance",
    "public",
)
WS3_AUTHORITY_ROLES = (
    "grove_api",
    "grove_runtime",
    "grove_projection",
    "grove_governance",
    "grove_migration",
)
WS3_AUTHORITY_ROLE_ATTRIBUTES = (
    "rolsuper",
    "rolinherit",
    "rolcreaterole",
    "rolcreatedb",
    "rolcanlogin",
    "rolreplication",
    "rolconnlimit",
    "rolbypassrls",
)
WS3_AUTHORITY_GRANT_ROLES = WS3_AUTHORITY_ONLINE_ROLES + ("grove_migration",)
WS3_AUTHORITY_GRANT_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
WS3_AUTHORITY_MUTATION_PRIVILEGES = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "TRIGGER")
# PostgreSQL exposes INSERT/UPDATE (and REFERENCES) at column granularity;
# DELETE/TRUNCATE/TRIGGER are relation-level privileges only.
WS3_AUTHORITY_COLUMN_MUTATION_PRIVILEGES = ("INSERT", "UPDATE")
# Populated from WS3_AUTHORITY_RELATION_REGISTRY below.  Keeping this as a
# derived tuple prevents readers from drifting to a second relation list.
WS3_AUTHORITY_RELATION_NAMES: tuple[str, ...] = ()
WS3_AUTHORITY_MUTATION_RELATION_NAMES = (
    "agent_run",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoints",
    "command_payload",
    "execution_spec",
    "run_command",
)
WS3_AUTHORITY_EXCLUDED_RELATION_NAMES = (
    "tenant",
    "membership",
    "workload_principal",
    "execution_principal",
)
WS3_AUTHORITY_COLUMNS: dict[str, tuple[str, ...]] = {
    "tenant": ("tenant_id", "status", "created_at"),
    "membership": ("tenant_id", "principal_id", "principal_kind", "user_ref", "roles", "active", "created_at"),
    "workload_principal": (
        "tenant_id",
        "principal_id",
        "principal_kind",
        "workload_ref",
        "scopes",
        "active",
        "created_at",
    ),
    "execution_principal": ("tenant_id", "principal_id", "principal_kind", "active"),
    "execution_spec": ("tenant_id", "skill_spec_hash", "spec_ref", "spec_payload", "created_at"),
    "command_payload": (
        "tenant_id",
        "payload_ref",
        "payload_hash",
        "command_schema_version",
        "sensitivity",
        "retention",
        "payload",
        "created_at",
    ),
    "agent_run": (
        "run_id",
        "tenant_id",
        "submission_id",
        "submission_digest",
        "principal_id",
        "principal_kind",
        "skill_spec_hash",
        "skill_spec_ref",
        "status",
        "revision",
        "created_at",
        "updated_at",
        "runtime_build_ref",
        "runtime_build_hash",
        "execution_fence",
        "lease_owner",
        "lease_until",
        "latest_applied_command_id",
        "latest_applied_command_digest",
        "latest_applied_command_seq",
        "latest_checkpoint_id",
    ),
    "run_command": (
        "command_id",
        "tenant_id",
        "run_id",
        "principal_id",
        "principal_kind",
        "command_seq",
        "command_type",
        "command_schema_version",
        "command_digest",
        "payload_ref",
        "payload_hash",
        "status",
        "created_at",
        "available_at",
        "lease_owner",
        "lease_until",
        "execution_fence",
        "attempt_count",
        "last_error_ref",
        "consumed_worker_id",
        "consumed_execution_fence",
        "consumed_lease_until",
        "consumed_claim_provenance_hash",
        "consumed_provenance_kind",
        "superseded_by_command_id",
        "superseded_by_command_seq",
        "superseded_by_command_digest",
        "superseded_by_provenance_hash",
    ),
    "checkpoints": (
        "tenant_id",
        "thread_id",
        "checkpoint_ns",
        "checkpoint_id",
        "parent_checkpoint_id",
        "type",
        "checkpoint",
        "metadata",
        "content_hash",
        "claim_command_id",
        "claim_command_seq",
        "claim_command_digest",
        "claim_worker_id",
        "claim_execution_fence",
        "claim_lease_until",
        "claim_runtime_build_hash",
        "claim_provenance_hash",
    ),
    "checkpoint_blobs": (
        "tenant_id",
        "thread_id",
        "checkpoint_ns",
        "channel",
        "version",
        "type",
        "blob",
        "content_hash",
        "claim_command_id",
        "claim_command_seq",
        "claim_command_digest",
        "claim_worker_id",
        "claim_execution_fence",
        "claim_lease_until",
        "claim_runtime_build_hash",
        "claim_provenance_hash",
    ),
    "checkpoint_writes": (
        "tenant_id",
        "thread_id",
        "checkpoint_ns",
        "checkpoint_id",
        "task_id",
        "task_path",
        "idx",
        "channel",
        "type",
        "blob",
        "content_hash",
        "claim_command_id",
        "claim_command_seq",
        "claim_command_digest",
        "claim_worker_id",
        "claim_execution_fence",
        "claim_lease_until",
        "claim_runtime_build_hash",
        "claim_provenance_hash",
    ),
    "checkpoint_migrations": ("v",),
}
_TRIGGER_TARGET_HASHES = {
    "public.grove_reject_execution_fence_regression()": (
        "679ab85bc8cd35116bd5ae44d2c4cdb8a2c1cbcdeca9339f8d3cf66b7a993514"
    ),
    "public.grove_reject_agent_run_runtime_build_rebinding()": (
        "42b6ae62b6c631aa7579fb2894ea35d78147050afc8a8aab11d4c962c3e249af"
    ),
    # The immutable trigger intentionally has no function-level search_path
    # setting, unlike the SECURITY DEFINER execution guards below.
    "public.grove_reject_immutable_change()": ("3afda1ae53b2aed06640d8a4846252b14265dd0cb02f1e509fd43b4dd50a32e6"),
    "public.grove_checkpoint_authority_guard()": ("9dc9649a11f3d3be97bdc9e80047f63051f3b387b469a77090d09e56c01dfde3"),
    "public.grove_checkpoint_physical_guard()": ("2534bb9763e9bccfc2d66e1941fec94e48148c2b99a0923ee3e22d46bf3a1cb9"),
    "public.grove_checkpoint_tenant_guard()": ("1fb2135b5f345ede3834aa930eaf7c260530b27444cd52c2094de947fe63c8c5"),
    "public.grove_validate_execution_principal()": ("fcccb177c86edebaff2031a335d9f96e2d62f46db577cc8b2061fb292dcecbe0"),
    "public.grove_reject_identity_key_change()": ("bcd379ca41689ba468af244cb4516d0cdb16875c887def70436c500fa9b9a5fa"),
    "public.grove_sync_execution_principal()": ("696bab3f7ada6844b72a574b4f473adf510e0056bc7a03d3bb0cf340089e370e"),
}
_TRIGGER_TARGET_ACLS = {
    "public.grove_reject_agent_run_runtime_build_rebinding()": ["grove_migration=X/grove_migration"],
    "public.grove_checkpoint_physical_guard()": ["grove_migration=X/grove_migration"],
}
_WS3_ACL_EXPECTED: dict[str, bool] = {
    f"{table}.{role}.{privilege}": (role == "grove_runtime" and privilege in {"SELECT", "INSERT", "UPDATE"})
    for table in sorted(WS3_CHECKPOINT_RELATIONS)
    for role in ("grove_api", "grove_runtime", "grove_projection", "grove_governance", "public")
    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
}


def _trigger_target(
    identity: str,
    identity_arguments: str,
    *,
    security_definer: bool,
    definition_sha256: str | None = None,
    acl: list[str] | None = None,
    settings: list[str] | None = None,
) -> dict[str, Any]:
    """Build one relation-qualified trigger target contract entry."""

    qualified_name, _ = identity.split("(", 1)
    schema, name = qualified_name.rsplit(".", 1)
    return {
        "identity": identity,
        "schema": schema,
        "name": name,
        "identity_arguments": identity_arguments,
        "owner": "grove_migration",
        "security_definer": security_definer,
        "settings": list(settings) if settings is not None else ["search_path=pg_catalog, public"],
        "acl": list(
            acl or _TRIGGER_TARGET_ACLS.get(identity, ["=X/grove_migration", "grove_migration=X/grove_migration"])
        ),
        "definition_sha256": definition_sha256 or _TRIGGER_TARGET_HASHES[identity],
    }


WS3_SCHEMA_CONTRACT: dict[str, Any] = {
    "columns": {
        "agent_run.execution_fence": ["bigint", "NO", "0"],
        "agent_run.lease_owner": ["text", "YES", None],
        "agent_run.lease_until": ["timestamp with time zone", "YES", None],
        "agent_run.runtime_build_hash": ["text", "NO", None],
        "agent_run.runtime_build_ref": ["text", "NO", None],
        "agent_run.latest_applied_command_id": ["uuid", "YES", None],
        "agent_run.latest_applied_command_digest": ["text", "YES", None],
        "agent_run.latest_applied_command_seq": ["bigint", "YES", None],
        "agent_run.latest_checkpoint_id": ["text", "YES", None],
        "run_command.attempt_count": ["integer", "NO", "0"],
        "run_command.available_at": ["timestamp with time zone", "NO", "now()"],
        "run_command.execution_fence": ["bigint", "YES", None],
        "run_command.last_error_ref": ["text", "YES", None],
        "run_command.lease_owner": ["text", "YES", None],
        "run_command.lease_until": ["timestamp with time zone", "YES", None],
        "run_command.consumed_claim_provenance_hash": ["text", "YES", None],
        "run_command.consumed_provenance_kind": ["text", "YES", None],
        "run_command.consumed_execution_fence": ["bigint", "YES", None],
        "run_command.consumed_lease_until": ["timestamp with time zone", "YES", None],
        "run_command.consumed_worker_id": ["text", "YES", None],
        "run_command.superseded_by_command_id": ["uuid", "YES", None],
        "run_command.superseded_by_command_seq": ["bigint", "YES", None],
        "run_command.superseded_by_command_digest": ["text", "YES", None],
        "run_command.superseded_by_provenance_hash": ["text", "YES", None],
        "checkpoint_blobs.blob": ["bytea", "YES", None],
        "checkpoint_blobs.channel": ["text", "NO", None],
        "checkpoint_blobs.checkpoint_ns": ["text", "NO", "''::text"],
        "checkpoint_blobs.thread_id": ["text", "NO", None],
        "checkpoint_blobs.tenant_id": [
            "text",
            "NO",
            "NULLIF(current_setting('grove.tenant_id'::text, true), ''::text)",
        ],
        "checkpoint_blobs.type": ["text", "NO", None],
        "checkpoint_blobs.version": ["text", "NO", None],
        "checkpoint_blobs.content_hash": ["text", "NO", None],
        "checkpoint_blobs.claim_command_id": ["uuid", "NO", None],
        "checkpoint_blobs.claim_command_seq": ["bigint", "NO", None],
        "checkpoint_blobs.claim_command_digest": ["text", "NO", None],
        "checkpoint_blobs.claim_worker_id": ["text", "NO", None],
        "checkpoint_blobs.claim_execution_fence": ["bigint", "NO", None],
        "checkpoint_blobs.claim_lease_until": ["timestamp with time zone", "NO", None],
        "checkpoint_blobs.claim_runtime_build_hash": ["text", "NO", None],
        "checkpoint_blobs.claim_provenance_hash": ["text", "NO", None],
        "checkpoint_migrations.v": ["integer", "NO", None],
        "checkpoint_writes.blob": ["bytea", "NO", None],
        "checkpoint_writes.channel": ["text", "NO", None],
        "checkpoint_writes.checkpoint_id": ["text", "NO", None],
        "checkpoint_writes.checkpoint_ns": ["text", "NO", "''::text"],
        "checkpoint_writes.idx": ["integer", "NO", None],
        "checkpoint_writes.task_id": ["text", "NO", None],
        "checkpoint_writes.task_path": ["text", "NO", "''::text"],
        "checkpoint_writes.tenant_id": [
            "text",
            "NO",
            "NULLIF(current_setting('grove.tenant_id'::text, true), ''::text)",
        ],
        "checkpoint_writes.thread_id": ["text", "NO", None],
        "checkpoint_writes.type": ["text", "YES", None],
        "checkpoint_writes.content_hash": ["text", "NO", None],
        "checkpoint_writes.claim_command_id": ["uuid", "NO", None],
        "checkpoint_writes.claim_command_seq": ["bigint", "NO", None],
        "checkpoint_writes.claim_command_digest": ["text", "NO", None],
        "checkpoint_writes.claim_worker_id": ["text", "NO", None],
        "checkpoint_writes.claim_execution_fence": ["bigint", "NO", None],
        "checkpoint_writes.claim_lease_until": ["timestamp with time zone", "NO", None],
        "checkpoint_writes.claim_runtime_build_hash": ["text", "NO", None],
        "checkpoint_writes.claim_provenance_hash": ["text", "NO", None],
        "checkpoints.checkpoint": ["jsonb", "NO", None],
        "checkpoints.checkpoint_id": ["text", "NO", None],
        "checkpoints.checkpoint_ns": ["text", "NO", "''::text"],
        "checkpoints.metadata": ["jsonb", "NO", "'{}'::jsonb"],
        "checkpoints.parent_checkpoint_id": ["text", "YES", None],
        "checkpoints.tenant_id": [
            "text",
            "NO",
            "NULLIF(current_setting('grove.tenant_id'::text, true), ''::text)",
        ],
        "checkpoints.thread_id": ["text", "NO", None],
        "checkpoints.type": ["text", "YES", None],
        "checkpoints.content_hash": ["text", "NO", None],
        "checkpoints.claim_command_id": ["uuid", "NO", None],
        "checkpoints.claim_command_seq": ["bigint", "NO", None],
        "checkpoints.claim_command_digest": ["text", "NO", None],
        "checkpoints.claim_worker_id": ["text", "NO", None],
        "checkpoints.claim_execution_fence": ["bigint", "NO", None],
        "checkpoints.claim_lease_until": ["timestamp with time zone", "NO", None],
        "checkpoints.claim_runtime_build_hash": ["text", "NO", None],
        "checkpoints.claim_provenance_hash": ["text", "NO", None],
    },
    "constraints": {
        "agent_run_execution_fence_ck": "CHECK (execution_fence >= 0)",
        "agent_run_runtime_build_hash_ck": "CHECK (runtime_build_hash ~ '^[0-9a-f]{64}$'::text)",
        "run_command_type_ck": (
            "CHECK (command_type = ANY (ARRAY['start'::text, 'resume'::text, 'cancel'::text, "
            "'continue'::text, 'signal'::text]))"
        ),
        "run_command_schema_version_ck": (
            "CHECK (command_type = 'start'::text AND command_schema_version = 'start.v1'::text OR "
            "command_type = 'resume'::text AND command_schema_version = 'resume.v1'::text OR "
            "command_type = 'cancel'::text AND command_schema_version = 'cancel.v1'::text OR "
            "command_type = 'continue'::text AND command_schema_version = 'continue.v1'::text OR "
            "command_type = 'signal'::text AND command_schema_version = 'signal.v1'::text)"
        ),
        "run_command_status_ck": (
            "CHECK (status = ANY (ARRAY['pending'::text, 'leased'::text, 'consumed'::text, 'dead_letter'::text]))"
        ),
        "run_command_seq_ck": "CHECK (command_seq >= 0)",
        "run_command_digest_ck": "CHECK (length(command_digest) = 64)",
        "run_command_payload_hash_ck": "CHECK (length(payload_hash) = 64)",
        "run_command_payload_fk": (
            "FOREIGN KEY (tenant_id, payload_ref, payload_hash, command_schema_version) "
            "REFERENCES command_payload(tenant_id, payload_ref, payload_hash, command_schema_version)"
        ),
        "run_command_attempt_count_ck": "CHECK (attempt_count >= 0)",
        "run_command_lease_shape_ck": (
            "CHECK (status = 'leased'::text AND lease_owner IS NOT NULL AND lease_until IS NOT NULL "
            "AND execution_fence IS NOT NULL OR status <> 'leased'::text AND lease_owner IS NULL "
            "AND lease_until IS NULL AND execution_fence IS NULL)"
        ),
        "agent_run_latest_applied_seq_ck": (
            "CHECK (latest_applied_command_seq IS NULL OR latest_applied_command_seq >= 0)"
        ),
        "run_command_consumed_provenance_ck": (
            "CHECK (status = 'consumed'::text AND consumed_provenance_kind IS NOT NULL "
            "AND consumed_provenance_kind = 'claim.v1'::text AND consumed_worker_id IS NOT NULL "
            "AND consumed_execution_fence IS NOT NULL AND consumed_lease_until IS NOT NULL "
            "AND consumed_claim_provenance_hash ~ '^[0-9a-f]{64}$'::text "
            "OR status = 'consumed'::text AND consumed_provenance_kind IS NOT NULL "
            "AND consumed_provenance_kind = 'legacy_unverified'::text AND consumed_worker_id IS NULL "
            "AND consumed_execution_fence IS NULL AND consumed_lease_until IS NULL "
            "AND consumed_claim_provenance_hash IS NULL "
            "OR status <> 'consumed'::text AND consumed_provenance_kind IS NULL "
            "AND consumed_worker_id IS NULL AND consumed_execution_fence IS NULL "
            "AND consumed_lease_until IS NULL AND consumed_claim_provenance_hash IS NULL)"
        ),
        "run_command_superseded_provenance_ck": (
            "CHECK (superseded_by_command_id IS NOT NULL AND superseded_by_command_seq IS NOT NULL "
            "AND superseded_by_command_digest IS NOT NULL "
            "AND superseded_by_command_digest ~ '^[0-9a-f]{64}$'::text "
            "AND (superseded_by_provenance_hash IS NULL OR "
            "superseded_by_provenance_hash ~ '^[0-9a-f]{64}$'::text) OR "
            "superseded_by_command_id IS NULL AND superseded_by_command_seq IS NULL "
            "AND superseded_by_command_digest IS NULL AND superseded_by_provenance_hash IS NULL)"
        ),
        "run_command_superseded_target_fk": (
            "FOREIGN KEY (tenant_id, superseded_by_command_id) REFERENCES run_command(tenant_id, command_id)"
        ),
        "command_payload_hash_ck": "CHECK (length(payload_hash) = 64)",
        "command_payload_schema_version_ck": (
            "CHECK (command_schema_version = ANY (ARRAY['start.v1'::text, 'resume.v1'::text, "
            "'cancel.v1'::text, 'continue.v1'::text, 'signal.v1'::text]))"
        ),
        "command_payload_sensitivity_ck": "CHECK (sensitivity = 'sensitive'::text)",
        "command_payload_retention_ck": "CHECK (retention = 'run_completion'::text)",
        "checkpoints_content_hash_ck": "CHECK (content_hash ~ '^[0-9a-f]{64}$'::text)",
        "checkpoints_claim_provenance_ck": "CHECK (claim_provenance_hash ~ '^[0-9a-f]{64}$'::text)",
        "checkpoint_blobs_content_hash_ck": "CHECK (content_hash ~ '^[0-9a-f]{64}$'::text)",
        "checkpoint_blobs_claim_provenance_ck": "CHECK (claim_provenance_hash ~ '^[0-9a-f]{64}$'::text)",
        "checkpoint_writes_content_hash_ck": "CHECK (content_hash ~ '^[0-9a-f]{64}$'::text)",
        "checkpoint_writes_claim_provenance_ck": "CHECK (claim_provenance_hash ~ '^[0-9a-f]{64}$'::text)",
    },
    "functions": {
        "public.grove_execution_claim_lifecycle_valid(p_run_status text, p_command_type text)": {
            "identity_arguments": "p_run_status text, p_command_type text",
            "owner": "grove_migration",
            "security_definer": False,
            "settings": ["search_path=pg_catalog, public"],
            "definition_sha256": "2c889d72c538eb508c3f9c8761c43dbc4193a9781b937903c2bbed4f973a7077",
        },
        "public.grove_checkpoint_authority_guard()": {
            "identity_arguments": "",
            "owner": "grove_migration",
            "security_definer": True,
            "settings": ["search_path=pg_catalog, public"],
            "definition_sha256": "9dc9649a11f3d3be97bdc9e80047f63051f3b387b469a77090d09e56c01dfde3",
        },
        (
            "public.grove_checkpoint_claim_provenance("
            "p_tenant_id text, p_run_id uuid, p_command_id uuid, p_command_seq bigint, "
            "p_command_digest text, p_runtime_build_hash text, p_worker_id text, "
            "p_execution_fence bigint, p_lease_until timestamp with time zone)"
        ): {
            "identity_arguments": (
                "p_tenant_id text, p_run_id uuid, p_command_id uuid, p_command_seq bigint, "
                "p_command_digest text, p_runtime_build_hash text, p_worker_id text, "
                "p_execution_fence bigint, p_lease_until timestamp with time zone"
            ),
            "owner": "grove_migration",
            "security_definer": False,
            "settings": ["search_path=pg_catalog, public"],
            "definition_sha256": "d5229607c6a2e357d7c8219e50bc3a1b07bd56c0a4a59d52375fdf2a5b2e023b",
        },
        "public.grove_checkpoint_tenant_guard()": {
            "identity_arguments": "",
            "owner": "grove_migration",
            "security_definer": True,
            "settings": ["search_path=pg_catalog, public"],
            "definition_sha256": "1fb2135b5f345ede3834aa930eaf7c260530b27444cd52c2094de947fe63c8c5",
        },
        "public.grove_checkpoint_physical_guard()": {
            "identity_arguments": "",
            "owner": "grove_migration",
            "security_definer": True,
            "settings": ["search_path=pg_catalog, public"],
            "definition_sha256": "2534bb9763e9bccfc2d66e1941fec94e48148c2b99a0923ee3e22d46bf3a1cb9",
        },
        "public.grove_reject_agent_run_runtime_build_rebinding()": {
            "identity_arguments": "",
            "owner": "grove_migration",
            "security_definer": False,
            "settings": ["search_path=pg_catalog, public"],
            "definition_sha256": "42b6ae62b6c631aa7579fb2894ea35d78147050afc8a8aab11d4c962c3e249af",
        },
        (
            "public.grove_claim_run_command("
            "p_tenant_id text, p_worker_id text, p_runtime_build_hash text, "
            "p_lease_seconds double precision)"
        ): {
            "identity_arguments": (
                "p_tenant_id text, p_worker_id text, p_runtime_build_hash text, p_lease_seconds double precision"
            ),
            "owner": "grove_migration",
            "security_definer": True,
            "settings": ["search_path=pg_catalog, public"],
            "definition_sha256": "bfb082256ba3424c200a5d4b0adc95ea79634c164cb7879aba22a140f1bbfff4",
        },
        (
            "public.grove_accept_cancel_run("
            "p_tenant_id text, p_run_id uuid, p_command_id uuid, p_expected_revision bigint, "
            "p_command_digest text, p_runtime_build_hash text, p_payload_ref text, "
            "p_payload_hash text, p_payload jsonb)"
        ): {
            "identity_arguments": (
                "p_tenant_id text, p_run_id uuid, p_command_id uuid, p_expected_revision bigint, "
                "p_command_digest text, p_runtime_build_hash text, p_payload_ref text, p_payload_hash text, "
                "p_payload jsonb"
            ),
            "owner": "grove_migration",
            "security_definer": True,
            "settings": ["search_path=pg_catalog, public"],
            "definition_sha256": "75760c64029a52f5268530ffb979da1caaf683c269f8d9159c2dd5fe8766770f",
        },
        (
            "public.grove_heartbeat_run_command("
            "p_tenant_id text, p_run_id uuid, p_command_id uuid, p_command_seq bigint, "
            "p_command_digest text, p_runtime_build_hash text, p_worker_id text, "
            "p_execution_fence bigint, p_expected_lease_until timestamp with time zone, "
            "p_lease_seconds double precision)"
        ): {
            "identity_arguments": (
                "p_tenant_id text, p_run_id uuid, p_command_id uuid, p_command_seq bigint, "
                "p_command_digest text, p_runtime_build_hash text, p_worker_id text, "
                "p_execution_fence bigint, p_expected_lease_until timestamp with time zone, "
                "p_lease_seconds double precision"
            ),
            "owner": "grove_migration",
            "security_definer": True,
            "settings": ["search_path=pg_catalog, public"],
            "definition_sha256": "7020e7ed070e3401cb29fe0417eb8c398f28fede4a1454de2bf53965ea47d911",
        },
        (
            "public.grove_heartbeat_run_command_internal("
            "p_tenant_id text, p_run_id uuid, p_command_id uuid, p_command_seq bigint, "
            "p_command_digest text, p_runtime_build_hash text, p_worker_id text, "
            "p_execution_fence bigint, p_expected_lease_until timestamp with time zone, "
            "p_lease_seconds double precision)"
        ): {
            "identity_arguments": (
                "p_tenant_id text, p_run_id uuid, p_command_id uuid, p_command_seq bigint, "
                "p_command_digest text, p_runtime_build_hash text, p_worker_id text, "
                "p_execution_fence bigint, p_expected_lease_until timestamp with time zone, "
                "p_lease_seconds double precision"
            ),
            "owner": "grove_migration",
            "security_definer": True,
            "settings": ["search_path=pg_catalog, public"],
            "definition_sha256": "6ad7c5a9681557b772749acaa781e16bfa1133e3e9592c865b4ff80847542c2a",
        },
        (
            "public.grove_consume_run_command("
            "p_tenant_id text, p_run_id uuid, p_command_id uuid, p_command_seq bigint, "
            "p_command_digest text, p_runtime_build_hash text, p_worker_id text, "
            "p_execution_fence bigint, p_expected_lease_until timestamp with time zone)"
        ): {
            "identity_arguments": (
                "p_tenant_id text, p_run_id uuid, p_command_id uuid, p_command_seq bigint, "
                "p_command_digest text, p_runtime_build_hash text, p_worker_id text, "
                "p_execution_fence bigint, p_expected_lease_until timestamp with time zone"
            ),
            "owner": "grove_migration",
            "security_definer": True,
            "settings": ["search_path=pg_catalog, public"],
            "definition_sha256": "2dd440b55ae19bef2876c148a7309bb3f0e867e96a2765450a5505e1656662d7",
        },
        (
            "public.grove_consume_run_command_internal("
            "p_tenant_id text, p_run_id uuid, p_command_id uuid, p_command_seq bigint, "
            "p_command_digest text, p_runtime_build_hash text, p_worker_id text, "
            "p_execution_fence bigint, p_expected_lease_until timestamp with time zone)"
        ): {
            "identity_arguments": (
                "p_tenant_id text, p_run_id uuid, p_command_id uuid, p_command_seq bigint, "
                "p_command_digest text, p_runtime_build_hash text, p_worker_id text, "
                "p_execution_fence bigint, p_expected_lease_until timestamp with time zone"
            ),
            "owner": "grove_migration",
            "security_definer": True,
            "settings": ["search_path=pg_catalog, public"],
            "definition_sha256": "afe7cb6fe0903ff597dffdd0da02e677e9e84d47cb4e5c1bc02fdef544e3db13",
        },
        (
            "public.grove_dead_letter_run_command("
            "p_tenant_id text, p_run_id uuid, p_command_id uuid, p_command_seq bigint, "
            "p_command_digest text, p_runtime_build_hash text, p_worker_id text, "
            "p_execution_fence bigint, p_expected_lease_until timestamp with time zone, "
            "p_reason_ref text)"
        ): {
            "identity_arguments": (
                "p_tenant_id text, p_run_id uuid, p_command_id uuid, p_command_seq bigint, "
                "p_command_digest text, p_runtime_build_hash text, p_worker_id text, "
                "p_execution_fence bigint, p_expected_lease_until timestamp with time zone, "
                "p_reason_ref text"
            ),
            "owner": "grove_migration",
            "security_definer": True,
            "settings": ["search_path=pg_catalog, public"],
            "definition_sha256": "22e4372f8dd46665348be2661b78c5a4f22b66f213e628c3a4b2840e442a6e5f",
        },
        (
            "public.grove_dead_letter_run_command_internal("
            "p_tenant_id text, p_run_id uuid, p_command_id uuid, p_command_seq bigint, "
            "p_command_digest text, p_runtime_build_hash text, p_worker_id text, "
            "p_execution_fence bigint, p_expected_lease_until timestamp with time zone, "
            "p_reason_ref text)"
        ): {
            "identity_arguments": (
                "p_tenant_id text, p_run_id uuid, p_command_id uuid, p_command_seq bigint, "
                "p_command_digest text, p_runtime_build_hash text, p_worker_id text, "
                "p_execution_fence bigint, p_expected_lease_until timestamp with time zone, p_reason_ref text"
            ),
            "owner": "grove_migration",
            "security_definer": True,
            "settings": ["search_path=pg_catalog, public"],
            "definition_sha256": "0a4f57c32412d276cc8e6a36ac7f906c66d0abacab2bbba8baf91f9ef06eb9cb",
        },
        "public.grove_reconcile_expired_run_command(p_tenant_id text, p_run_id uuid)": {
            "identity_arguments": "p_tenant_id text, p_run_id uuid",
            "owner": "grove_migration",
            "security_definer": True,
            "settings": ["search_path=pg_catalog, public"],
            "definition_sha256": "2933e118b2de91d8903a4ac9dba903cf7861bf414fd4aac62aa5ada927df55b5",
        },
        "public.grove_reconcile_expired_run_command_internal(p_tenant_id text, p_run_id uuid)": {
            "identity_arguments": "p_tenant_id text, p_run_id uuid",
            "owner": "grove_migration",
            "security_definer": True,
            "settings": ["search_path=pg_catalog, public"],
            "definition_sha256": "62c7d6ef60331441a024e2a082a437bf44d01757b5ad4d450badc5071d069525",
        },
    },
    "function_acl": {
        "public.grove_execution_claim_lifecycle_valid(p_run_status text, p_command_type text)": (
            "{grove_migration=X/grove_migration}"
        ),
        "public.grove_checkpoint_physical_guard()": "{grove_migration=X/grove_migration}",
        (
            "public.grove_heartbeat_run_command_internal("
            "p_tenant_id text, p_run_id uuid, p_command_id uuid, p_command_seq bigint, "
            "p_command_digest text, p_runtime_build_hash text, p_worker_id text, "
            "p_execution_fence bigint, p_expected_lease_until timestamp with time zone, "
            "p_lease_seconds double precision)"
        ): ("{grove_migration=X/grove_migration}"),
        (
            "public.grove_consume_run_command_internal("
            "p_tenant_id text, p_run_id uuid, p_command_id uuid, p_command_seq bigint, "
            "p_command_digest text, p_runtime_build_hash text, p_worker_id text, "
            "p_execution_fence bigint, p_expected_lease_until timestamp with time zone)"
        ): ("{grove_migration=X/grove_migration}"),
        (
            "public.grove_dead_letter_run_command_internal("
            "p_tenant_id text, p_run_id uuid, p_command_id uuid, p_command_seq bigint, "
            "p_command_digest text, p_runtime_build_hash text, p_worker_id text, "
            "p_execution_fence bigint, p_expected_lease_until timestamp with time zone, "
            "p_reason_ref text)"
        ): ("{grove_migration=X/grove_migration}"),
        "public.grove_reconcile_expired_run_command_internal(p_tenant_id text, p_run_id uuid)": (
            "{grove_migration=X/grove_migration}"
        ),
        "public.grove_reject_agent_run_runtime_build_rebinding()": "{grove_migration=X/grove_migration}",
        (
            "public.grove_dead_letter_run_command("
            "p_tenant_id text, p_run_id uuid, p_command_id uuid, p_command_seq bigint, "
            "p_command_digest text, p_runtime_build_hash text, p_worker_id text, "
            "p_execution_fence bigint, p_expected_lease_until timestamp with time zone, "
            "p_reason_ref text)"
        ): "{grove_migration=X/grove_migration,grove_runtime=X/grove_migration}",
        "public.grove_reconcile_expired_run_command(p_tenant_id text, p_run_id uuid)": (
            "{grove_migration=X/grove_migration,grove_projection=X/grove_migration}"
        ),
    },
    "trigger": {
        "schema": "public",
        "table": "agent_run",
        "name": "agent_run_execution_fence_guard",
        "enabled": "O",
        "definition": (
            "CREATE TRIGGER agent_run_execution_fence_guard BEFORE UPDATE OF execution_fence ON agent_run "
            "FOR EACH ROW EXECUTE FUNCTION grove_reject_execution_fence_regression()"
        ),
        "target_function": _trigger_target(
            "public.grove_reject_execution_fence_regression()", "", security_definer=False
        ),
    },
    "agent_run_triggers": {
        "public.agent_run.agent_run_execution_fence_guard": {
            "schema": "public",
            "table": "agent_run",
            "name": "agent_run_execution_fence_guard",
            "enabled": "O",
            "definition": (
                "CREATE TRIGGER agent_run_execution_fence_guard BEFORE UPDATE OF execution_fence ON agent_run "
                "FOR EACH ROW EXECUTE FUNCTION grove_reject_execution_fence_regression()"
            ),
            "target_function": _trigger_target(
                "public.grove_reject_execution_fence_regression()", "", security_definer=False
            ),
        },
        "public.agent_run.agent_run_runtime_build_guard": {
            "schema": "public",
            "table": "agent_run",
            "name": "agent_run_runtime_build_guard",
            "enabled": "O",
            "definition": (
                "CREATE TRIGGER agent_run_runtime_build_guard BEFORE UPDATE OF runtime_build_ref, runtime_build_hash "
                "ON agent_run FOR EACH ROW EXECUTE FUNCTION grove_reject_agent_run_runtime_build_rebinding()"
            ),
            "target_function": _trigger_target(
                "public.grove_reject_agent_run_runtime_build_rebinding()",
                "",
                security_definer=False,
                acl=["grove_migration=X/grove_migration"],
            ),
        },
    },
    "checkpoint_triggers": {
        "public.checkpoints.checkpoints_authority_guard": {
            "schema": "public",
            "table": "checkpoints",
            "name": "checkpoints_authority_guard",
            "enabled": "O",
            "definition": (
                "CREATE TRIGGER checkpoints_authority_guard BEFORE INSERT OR UPDATE ON checkpoints "
                "FOR EACH ROW EXECUTE FUNCTION grove_checkpoint_authority_guard()"
            ),
            "target_function": _trigger_target("public.grove_checkpoint_authority_guard()", "", security_definer=True),
        },
        "public.checkpoints.checkpoints_physical_guard": {
            "schema": "public",
            "table": "checkpoints",
            "name": "checkpoints_physical_guard",
            "enabled": "O",
            "definition": (
                "CREATE TRIGGER checkpoints_physical_guard BEFORE INSERT OR UPDATE ON checkpoints "
                "FOR EACH ROW EXECUTE FUNCTION grove_checkpoint_physical_guard()"
            ),
            "target_function": _trigger_target(
                "public.grove_checkpoint_physical_guard()",
                "",
                security_definer=True,
                acl=["grove_migration=X/grove_migration"],
            ),
        },
        "public.checkpoints.checkpoints_tenant_guard": {
            "schema": "public",
            "table": "checkpoints",
            "name": "checkpoints_tenant_guard",
            "enabled": "O",
            "definition": (
                "CREATE TRIGGER checkpoints_tenant_guard BEFORE INSERT OR UPDATE ON checkpoints "
                "FOR EACH ROW EXECUTE FUNCTION grove_checkpoint_tenant_guard()"
            ),
            "target_function": _trigger_target("public.grove_checkpoint_tenant_guard()", "", security_definer=True),
        },
        "public.checkpoint_blobs.checkpoint_blobs_authority_guard": {
            "schema": "public",
            "table": "checkpoint_blobs",
            "name": "checkpoint_blobs_authority_guard",
            "enabled": "O",
            "definition": (
                "CREATE TRIGGER checkpoint_blobs_authority_guard BEFORE INSERT OR UPDATE ON checkpoint_blobs "
                "FOR EACH ROW EXECUTE FUNCTION grove_checkpoint_authority_guard()"
            ),
            "target_function": _trigger_target("public.grove_checkpoint_authority_guard()", "", security_definer=True),
        },
        "public.checkpoint_blobs.checkpoint_blobs_physical_guard": {
            "schema": "public",
            "table": "checkpoint_blobs",
            "name": "checkpoint_blobs_physical_guard",
            "enabled": "O",
            "definition": (
                "CREATE TRIGGER checkpoint_blobs_physical_guard BEFORE INSERT OR UPDATE ON checkpoint_blobs "
                "FOR EACH ROW EXECUTE FUNCTION grove_checkpoint_physical_guard()"
            ),
            "target_function": _trigger_target(
                "public.grove_checkpoint_physical_guard()",
                "",
                security_definer=True,
                acl=["grove_migration=X/grove_migration"],
            ),
        },
        "public.checkpoint_writes.checkpoint_writes_authority_guard": {
            "schema": "public",
            "table": "checkpoint_writes",
            "name": "checkpoint_writes_authority_guard",
            "enabled": "O",
            "definition": (
                "CREATE TRIGGER checkpoint_writes_authority_guard BEFORE INSERT OR UPDATE ON checkpoint_writes "
                "FOR EACH ROW EXECUTE FUNCTION grove_checkpoint_authority_guard()"
            ),
            "target_function": _trigger_target("public.grove_checkpoint_authority_guard()", "", security_definer=True),
        },
        "public.checkpoint_writes.checkpoint_writes_physical_guard": {
            "schema": "public",
            "table": "checkpoint_writes",
            "name": "checkpoint_writes_physical_guard",
            "enabled": "O",
            "definition": (
                "CREATE TRIGGER checkpoint_writes_physical_guard BEFORE INSERT OR UPDATE ON checkpoint_writes "
                "FOR EACH ROW EXECUTE FUNCTION grove_checkpoint_physical_guard()"
            ),
            "target_function": _trigger_target(
                "public.grove_checkpoint_physical_guard()",
                "",
                security_definer=True,
                acl=["grove_migration=X/grove_migration"],
            ),
        },
        "public.checkpoint_blobs.checkpoint_blobs_tenant_guard": {
            "schema": "public",
            "table": "checkpoint_blobs",
            "name": "checkpoint_blobs_tenant_guard",
            "enabled": "O",
            "definition": (
                "CREATE TRIGGER checkpoint_blobs_tenant_guard BEFORE INSERT OR UPDATE ON checkpoint_blobs "
                "FOR EACH ROW EXECUTE FUNCTION grove_checkpoint_tenant_guard()"
            ),
            "target_function": _trigger_target("public.grove_checkpoint_tenant_guard()", "", security_definer=True),
        },
        "public.checkpoint_writes.checkpoint_writes_tenant_guard": {
            "schema": "public",
            "table": "checkpoint_writes",
            "name": "checkpoint_writes_tenant_guard",
            "enabled": "O",
            "definition": (
                "CREATE TRIGGER checkpoint_writes_tenant_guard BEFORE INSERT OR UPDATE ON checkpoint_writes "
                "FOR EACH ROW EXECUTE FUNCTION grove_checkpoint_tenant_guard()"
            ),
            "target_function": _trigger_target("public.grove_checkpoint_tenant_guard()", "", security_definer=True),
        },
    },
    "rls": {
        "agent_run": [True, True],
        "run_command": [True, True],
        "checkpoint_blobs": [True, True],
        "checkpoint_writes": [True, True],
        "checkpoints": [True, True],
    },
    "privileges": {
        "api_cancel_execute": True,
        "runtime_cancel_execute": False,
        "governance_cancel_execute": False,
        "projection_cancel_execute": False,
        "public_cancel_execute": False,
        "api_claim_execute": False,
        "api_fence_insert": False,
        "api_fence_update": False,
        "api_runtime_build_hash_insert": True,
        "api_runtime_build_ref_insert": True,
        "governance_claim_execute": False,
        "projection_claim_execute": False,
        "public_claim_execute": False,
        "public_heartbeat_execute": False,
        "runtime_claim_execute": True,
        "runtime_fence_update": False,
        "runtime_heartbeat_execute": True,
        "runtime_tenant_update": False,
        "runtime_consume_execute": True,
        "runtime_dead_letter_execute": True,
        "projection_reconcile_expired_execute": True,
        "public_dead_letter_execute": False,
        "public_reconcile_expired_execute": False,
        "runtime_checkpoint_select": True,
        "runtime_checkpoint_insert": True,
        "runtime_checkpoint_update": True,
        "api_checkpoint_select": False,
        "governance_checkpoint_select": False,
        "projection_checkpoint_select": False,
        "api_payload_body_select": False,
        "runtime_payload_body_select": False,
        "projection_payload_body_select": False,
        "governance_payload_body_select": False,
        "public_payload_body_select": False,
    },
    "database_temp_privileges": {
        "grove_api": False,
        "grove_runtime": False,
        "grove_projection": False,
        "grove_governance": False,
    },
    "policies": {
        f"{table}_tenant_policy": {
            "command": "*",
            "permissive": True,
            "using": "(tenant_id = NULLIF(current_setting('grove.tenant_id'::text, true), ''::text))",
            "with_check": "(tenant_id = NULLIF(current_setting('grove.tenant_id'::text, true), ''::text))",
            "roles": "{0}",
        }
        for table in sorted(WS3_CHECKPOINT_RELATIONS)
    },
    "migration_rows": list(range(10)),
    "table_acl": _WS3_ACL_EXPECTED,
}

# v7 fixes constraints as a per-relation complete map.  The old flat
# ``constraints`` section remains a compatibility projection; this map is the
# authoritative external expected side and includes primary/unique/foreign
# keys as well as checks.
_WS3_AUTHORITY_CONSTRAINT_DEFINITIONS: dict[str, tuple[str, str]] = {
    "public.alembic_version.alembic_version_pkc": ("p", "PRIMARY KEY (version_num)"),
    "public.agent_run.agent_run_execution_fence_ck": ("c", "CHECK (execution_fence >= 0)"),
    "public.agent_run.agent_run_latest_applied_seq_ck": (
        "c",
        "CHECK (latest_applied_command_seq IS NULL OR latest_applied_command_seq >= 0)",
    ),
    "public.agent_run.agent_run_pkey": ("p", "PRIMARY KEY (run_id)"),
    "public.agent_run.agent_run_principal_fk": (
        "f",
        (
            "FOREIGN KEY (tenant_id, principal_id, principal_kind) REFERENCES "
            "execution_principal(tenant_id, principal_id, principal_kind)"
        ),
    ),
    "public.agent_run.agent_run_principal_kind_ck": (
        "c",
        "CHECK (principal_kind = ANY (ARRAY['human'::text, 'workload'::text]))",
    ),
    "public.agent_run.agent_run_revision_ck": ("c", "CHECK (revision >= 0)"),
    "public.agent_run.agent_run_run_principal_uq": (
        "u",
        "UNIQUE (tenant_id, run_id, principal_id, principal_kind)",
    ),
    "public.agent_run.agent_run_runtime_build_hash_ck": (
        "c",
        "CHECK (runtime_build_hash ~ '^[0-9a-f]{64}$'::text)",
    ),
    "public.agent_run.agent_run_skill_spec_hash_ck": ("c", "CHECK (length(skill_spec_hash) = 64)"),
    "public.agent_run.agent_run_spec_fk": (
        "f",
        (
            "FOREIGN KEY (tenant_id, skill_spec_hash, skill_spec_ref) REFERENCES "
            "execution_spec(tenant_id, skill_spec_hash, spec_ref)"
        ),
    ),
    "public.agent_run.agent_run_status_ck": (
        "c",
        (
            "CHECK (status = ANY (ARRAY['accepted'::text, 'running'::text, "
            "'waiting_user_input'::text, 'waiting_action_result'::text, "
            "'waiting_child_result'::text, 'cancel_requested'::text, "
            "'succeeded'::text, 'failed'::text, 'cancelled'::text]))"
        ),
    ),
    "public.agent_run.agent_run_submission_digest_ck": ("c", "CHECK (length(submission_digest) = 64)"),
    "public.agent_run.agent_run_tenant_fk": ("f", "FOREIGN KEY (tenant_id) REFERENCES tenant(tenant_id)"),
    "public.agent_run.agent_run_tenant_run_uq": ("u", "UNIQUE (tenant_id, run_id)"),
    "public.agent_run.agent_run_tenant_submission_uq": ("u", "UNIQUE (tenant_id, submission_id)"),
    "public.checkpoint_blobs.checkpoint_blobs_claim_provenance_ck": (
        "c",
        "CHECK (claim_provenance_hash ~ '^[0-9a-f]{64}$'::text)",
    ),
    "public.checkpoint_blobs.checkpoint_blobs_content_hash_ck": (
        "c",
        "CHECK (content_hash ~ '^[0-9a-f]{64}$'::text)",
    ),
    "public.checkpoint_blobs.checkpoint_blobs_pkey": (
        "p",
        "PRIMARY KEY (thread_id, checkpoint_ns, channel, version)",
    ),
    "public.checkpoint_migrations.checkpoint_migrations_pkey": ("p", "PRIMARY KEY (v)"),
    "public.checkpoint_writes.checkpoint_writes_claim_provenance_ck": (
        "c",
        "CHECK (claim_provenance_hash ~ '^[0-9a-f]{64}$'::text)",
    ),
    "public.checkpoint_writes.checkpoint_writes_content_hash_ck": (
        "c",
        "CHECK (content_hash ~ '^[0-9a-f]{64}$'::text)",
    ),
    "public.checkpoint_writes.checkpoint_writes_pkey": (
        "p",
        "PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)",
    ),
    "public.checkpoints.checkpoints_claim_provenance_ck": (
        "c",
        "CHECK (claim_provenance_hash ~ '^[0-9a-f]{64}$'::text)",
    ),
    "public.checkpoints.checkpoints_content_hash_ck": ("c", "CHECK (content_hash ~ '^[0-9a-f]{64}$'::text)"),
    "public.checkpoints.checkpoints_pkey": ("p", "PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)"),
    "public.command_payload.command_payload_hash_ck": ("c", "CHECK (length(payload_hash) = 64)"),
    "public.command_payload.command_payload_hash_uq": ("u", "UNIQUE (tenant_id, payload_hash)"),
    "public.command_payload.command_payload_pkey": ("p", "PRIMARY KEY (tenant_id, payload_ref)"),
    "public.command_payload.command_payload_ref_hash_schema_uq": (
        "u",
        "UNIQUE (tenant_id, payload_ref, payload_hash, command_schema_version)",
    ),
    "public.command_payload.command_payload_retention_ck": (
        "c",
        "CHECK (retention = 'run_completion'::text)",
    ),
    "public.command_payload.command_payload_schema_version_ck": (
        "c",
        (
            "CHECK (command_schema_version = ANY (ARRAY['start.v1'::text, "
            "'resume.v1'::text, 'cancel.v1'::text, 'continue.v1'::text, "
            "'signal.v1'::text]))"
        ),
    ),
    "public.command_payload.command_payload_sensitivity_ck": (
        "c",
        "CHECK (sensitivity = 'sensitive'::text)",
    ),
    "public.command_payload.command_payload_tenant_fk": (
        "f",
        "FOREIGN KEY (tenant_id) REFERENCES tenant(tenant_id)",
    ),
    "public.execution_principal.execution_principal_kind_ck": (
        "c",
        "CHECK (principal_kind = ANY (ARRAY['human'::text, 'workload'::text]))",
    ),
    "public.execution_principal.execution_principal_pkey": (
        "p",
        "PRIMARY KEY (tenant_id, principal_id, principal_kind)",
    ),
    "public.execution_principal.execution_principal_tenant_fk": (
        "f",
        "FOREIGN KEY (tenant_id) REFERENCES tenant(tenant_id)",
    ),
    "public.execution_spec.execution_spec_hash_ck": ("c", "CHECK (length(skill_spec_hash) = 64)"),
    "public.execution_spec.execution_spec_pkey": ("p", "PRIMARY KEY (tenant_id, skill_spec_hash)"),
    "public.execution_spec.execution_spec_tenant_fk": (
        "f",
        "FOREIGN KEY (tenant_id) REFERENCES tenant(tenant_id)",
    ),
    "public.execution_spec.execution_spec_tenant_hash_ref_uq": (
        "u",
        "UNIQUE (tenant_id, skill_spec_hash, spec_ref)",
    ),
    "public.membership.membership_pkey": ("p", "PRIMARY KEY (tenant_id, principal_id)"),
    "public.membership.membership_principal_kind_ck": ("c", "CHECK (principal_kind = 'human'::text)"),
    "public.membership.membership_tenant_fk": (
        "f",
        "FOREIGN KEY (tenant_id) REFERENCES tenant(tenant_id)",
    ),
    "public.run_command.run_command_attempt_count_ck": ("c", "CHECK (attempt_count >= 0)"),
    "public.run_command.run_command_consumed_provenance_ck": (
        "c",
        (
            "CHECK (status = 'consumed'::text AND consumed_provenance_kind IS NOT NULL AND "
            "consumed_provenance_kind = 'claim.v1'::text AND consumed_worker_id IS NOT NULL AND "
            "consumed_execution_fence IS NOT NULL AND consumed_lease_until IS NOT NULL AND "
            "consumed_claim_provenance_hash ~ '^[0-9a-f]{64}$'::text OR "
            "status = 'consumed'::text AND consumed_provenance_kind IS NOT NULL AND "
            "consumed_provenance_kind = 'legacy_unverified'::text AND consumed_worker_id IS NULL AND "
            "consumed_execution_fence IS NULL AND consumed_lease_until IS NULL AND "
            "consumed_claim_provenance_hash IS NULL OR "
            "status <> 'consumed'::text AND consumed_provenance_kind IS NULL AND "
            "consumed_worker_id IS NULL AND consumed_execution_fence IS NULL AND "
            "consumed_lease_until IS NULL AND consumed_claim_provenance_hash IS NULL)"
        ),
    ),
    "public.run_command.run_command_digest_ck": ("c", "CHECK (length(command_digest) = 64)"),
    "public.run_command.run_command_identity_fk": (
        "f",
        (
            "FOREIGN KEY (tenant_id, principal_id, principal_kind) REFERENCES "
            "execution_principal(tenant_id, principal_id, principal_kind)"
        ),
    ),
    "public.run_command.run_command_lease_shape_ck": (
        "c",
        (
            "CHECK (status = 'leased'::text AND lease_owner IS NOT NULL AND lease_until IS NOT NULL AND "
            "execution_fence IS NOT NULL OR status <> 'leased'::text AND lease_owner IS NULL AND "
            "lease_until IS NULL AND execution_fence IS NULL)"
        ),
    ),
    "public.run_command.run_command_payload_fk": (
        "f",
        (
            "FOREIGN KEY (tenant_id, payload_ref, payload_hash, command_schema_version) REFERENCES "
            "command_payload(tenant_id, payload_ref, payload_hash, command_schema_version)"
        ),
    ),
    "public.run_command.run_command_payload_hash_ck": ("c", "CHECK (length(payload_hash) = 64)"),
    "public.run_command.run_command_pkey": ("p", "PRIMARY KEY (command_id)"),
    "public.run_command.run_command_principal_fk": (
        "f",
        (
            "FOREIGN KEY (tenant_id, run_id, principal_id, principal_kind) REFERENCES "
            "agent_run(tenant_id, run_id, principal_id, principal_kind)"
        ),
    ),
    "public.run_command.run_command_principal_kind_ck": (
        "c",
        "CHECK (principal_kind = ANY (ARRAY['human'::text, 'workload'::text]))",
    ),
    "public.run_command.run_command_run_fk": (
        "f",
        "FOREIGN KEY (tenant_id, run_id) REFERENCES agent_run(tenant_id, run_id)",
    ),
    "public.run_command.run_command_run_seq_uq": ("u", "UNIQUE (tenant_id, run_id, command_seq)"),
    "public.run_command.run_command_schema_version_ck": (
        "c",
        (
            "CHECK (command_type = 'start'::text AND command_schema_version = 'start.v1'::text OR "
            "command_type = 'resume'::text AND command_schema_version = 'resume.v1'::text OR "
            "command_type = 'cancel'::text AND command_schema_version = 'cancel.v1'::text OR "
            "command_type = 'continue'::text AND command_schema_version = 'continue.v1'::text OR "
            "command_type = 'signal'::text AND command_schema_version = 'signal.v1'::text)"
        ),
    ),
    "public.run_command.run_command_seq_ck": ("c", "CHECK (command_seq >= 0)"),
    "public.run_command.run_command_status_ck": (
        "c",
        "CHECK (status = ANY (ARRAY['pending'::text, 'leased'::text, 'consumed'::text, 'dead_letter'::text]))",
    ),
    "public.run_command.run_command_superseded_provenance_ck": (
        "c",
        (
            "CHECK (superseded_by_command_id IS NOT NULL AND superseded_by_command_seq IS NOT NULL AND "
            "superseded_by_command_digest IS NOT NULL AND superseded_by_command_digest ~ '^[0-9a-f]{64}$'::text "
            "AND (superseded_by_provenance_hash IS NULL OR superseded_by_provenance_hash ~ '^[0-9a-f]{64}$'::text) "
            "OR superseded_by_command_id IS NULL AND superseded_by_command_seq IS NULL AND "
            "superseded_by_command_digest IS NULL AND superseded_by_provenance_hash IS NULL)"
        ),
    ),
    "public.run_command.run_command_superseded_target_fk": (
        "f",
        "FOREIGN KEY (tenant_id, superseded_by_command_id) REFERENCES run_command(tenant_id, command_id)",
    ),
    "public.run_command.run_command_tenant_command_uq": ("u", "UNIQUE (tenant_id, command_id)"),
    "public.run_command.run_command_type_ck": (
        "c",
        (
            "CHECK (command_type = ANY (ARRAY['start'::text, 'resume'::text, "
            "'cancel'::text, 'continue'::text, 'signal'::text]))"
        ),
    ),
    "public.tenant.tenant_pkey": ("p", "PRIMARY KEY (tenant_id)"),
    "public.tenant.tenant_status_ck": (
        "c",
        "CHECK (status = ANY (ARRAY['active'::text, 'suspended'::text]))",
    ),
    "public.workload_principal.workload_principal_kind_ck": (
        "c",
        "CHECK (principal_kind = 'workload'::text)",
    ),
    "public.workload_principal.workload_principal_pkey": ("p", "PRIMARY KEY (tenant_id, principal_id)"),
    "public.workload_principal.workload_tenant_fk": (
        "f",
        "FOREIGN KEY (tenant_id) REFERENCES tenant(tenant_id)",
    ),
}
WS3_AUTHORITY_CONSTRAINTS = {
    identity: {
        "schema": identity.split(".", 1)[0],
        "relation": identity.split(".", 2)[1],
        "name": identity.rsplit(".", 1)[1],
        "type": contype,
        "deferrable": False,
        "deferred": False,
        "validated": True,
        "definition": definition,
    }
    for identity, (contype, definition) in _WS3_AUTHORITY_CONSTRAINT_DEFINITIONS.items()
}
# Keep the legacy flat projection complete as well; the relation-qualified
# ``authority_constraints`` map remains the authoritative v7 contract.
WS3_SCHEMA_CONTRACT["constraints"] = {
    facts["name"]: facts["definition"] for facts in WS3_AUTHORITY_CONSTRAINTS.values()
}

# Every protected trigger contract includes the complete pg_proc signature
# family for the target schema+proname.  Current migration targets are
# singleton families, but the family is explicit so a same-named overload
# cannot carry an unpinned executable body through preflight.
for _trigger_entry in (
    WS3_SCHEMA_CONTRACT["trigger"],
    *WS3_SCHEMA_CONTRACT["agent_run_triggers"].values(),
    *WS3_SCHEMA_CONTRACT["checkpoint_triggers"].values(),
):
    _target = _trigger_entry["target_function"]
    _trigger_entry["target_function_family"] = {str(_target["identity"]): dict(_target)}


def _immutable_trigger(relation: str) -> dict[str, Any]:
    """Build the canonical immutable artifact trigger contract entry."""

    target = _trigger_target(
        "public.grove_reject_immutable_change()",
        "",
        security_definer=False,
        settings=[],
    )
    return {
        "schema": "public",
        "table": relation,
        "name": f"{relation}_immutable_guard",
        "enabled": "O",
        "definition": (
            f"CREATE TRIGGER {relation}_immutable_guard BEFORE DELETE OR UPDATE ON {relation} "
            "FOR EACH ROW EXECUTE FUNCTION grove_reject_immutable_change()"
        ),
        "target_function": target,
        "target_function_family": {str(target["identity"]): dict(target)},
    }


def _empty_mutation_grants() -> dict[str, dict[str, Any]]:
    """Return a complete role/privilege matrix with no mutation privileges."""

    return {
        role: {
            "table": {privilege: False for privilege in WS3_AUTHORITY_MUTATION_PRIVILEGES},
            "columns": {privilege: [] for privilege in WS3_AUTHORITY_COLUMN_MUTATION_PRIVILEGES},
        }
        for role in WS3_AUTHORITY_ONLINE_ROLES
    }


def _mutation_grants(
    *,
    table: dict[str, dict[str, bool]] | None = None,
    columns: dict[str, dict[str, list[str] | str]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build a complete mutation matrix from the small allowed-grant seam."""

    matrix = _empty_mutation_grants()
    if table:
        for role_name, role_privileges in table.items():
            for privilege, enabled in role_privileges.items():
                matrix[role_name]["table"][privilege] = bool(enabled)
    if columns:
        for role_name, role_columns in columns.items():
            for privilege, allowed_columns in role_columns.items():
                matrix[role_name]["columns"][privilege] = (
                    "*" if allowed_columns == "*" else sorted(str(column) for column in allowed_columns)
                )
    return matrix


_AUTHORITY_TRIGGER_MAPS: dict[str, dict[str, dict[str, Any]]] = {
    "public.agent_run": deepcopy(WS3_SCHEMA_CONTRACT["agent_run_triggers"]),
    "public.run_command": {},
    "public.checkpoints": {
        key: value
        for key, value in deepcopy(WS3_SCHEMA_CONTRACT["checkpoint_triggers"]).items()
        if value["table"] == "checkpoints"
    },
    "public.checkpoint_blobs": {
        key: value
        for key, value in deepcopy(WS3_SCHEMA_CONTRACT["checkpoint_triggers"]).items()
        if value["table"] == "checkpoint_blobs"
    },
    "public.checkpoint_writes": {
        key: value
        for key, value in deepcopy(WS3_SCHEMA_CONTRACT["checkpoint_triggers"]).items()
        if value["table"] == "checkpoint_writes"
    },
    "public.execution_spec": {
        "public.execution_spec.execution_spec_immutable_guard": _immutable_trigger("execution_spec")
    },
    "public.command_payload": {
        "public.command_payload.command_payload_immutable_guard": _immutable_trigger("command_payload")
    },
}

WS3_AUTHORITY_FUNCTION_TARGETS: dict[str, tuple[str, ...]] = {
    "grove_accept_cancel_run": ("public.agent_run", "public.command_payload", "public.run_command"),
    "grove_claim_run_command": ("public.agent_run", "public.run_command"),
    "grove_claim_run_command_internal": ("public.agent_run", "public.run_command"),
    "grove_heartbeat_run_command": ("public.agent_run", "public.run_command"),
    "grove_heartbeat_run_command_internal": ("public.agent_run", "public.run_command"),
    "grove_consume_run_command": ("public.agent_run", "public.run_command"),
    "grove_consume_run_command_internal": ("public.agent_run", "public.run_command"),
    "grove_dead_letter_run_command": ("public.agent_run", "public.run_command"),
    "grove_dead_letter_run_command_internal": ("public.agent_run", "public.run_command"),
    "grove_reconcile_expired_run_command": ("public.agent_run", "public.run_command"),
    "grove_reconcile_expired_run_command_internal": ("public.agent_run", "public.run_command"),
    "grove_checkpoint_authority_guard": ("public.agent_run", "public.run_command"),
    "grove_checkpoint_physical_guard": ("public.agent_run", "public.run_command"),
    "grove_finish_delivery": ("public.agent_run", "public.command_payload", "public.run_command"),
}

WS3_AUTHORITY_RELATION_REGISTRY: dict[str, dict[str, Any]] = {
    "public.execution_spec": {
        "schema": "public",
        "name": "execution_spec",
        "state_owner": "api",
        "write_seams": ["app.repositories.execution.insert_spec_if_absent"],
        "rls": [True, True],
        "policy": {
            "name": "execution_spec_tenant_isolation",
            "permissive": True,
            "using": "(tenant_id = grove_active_tenant())",
            "with_check": "(tenant_id = grove_active_tenant())",
            "roles": "{0}",
        },
        "direct_mutation_grants": _mutation_grants(
            columns={"grove_api": {"INSERT": ["tenant_id", "skill_spec_hash", "spec_ref", "spec_payload"]}}
        ),
        "triggers": _AUTHORITY_TRIGGER_MAPS["public.execution_spec"],
        "target_family_closure": True,
    },
    "public.command_payload": {
        "schema": "public",
        "name": "command_payload",
        "state_owner": "api",
        "write_seams": [
            "app.repositories.execution.insert_payload_if_absent",
            "app.execution.postgres.cancel_run",
        ],
        "rls": [True, True],
        "policy": {
            "name": "command_payload_tenant_isolation",
            "permissive": True,
            "using": "(tenant_id = grove_active_tenant())",
            "with_check": "(tenant_id = grove_active_tenant())",
            "roles": "{0}",
        },
        "direct_mutation_grants": _mutation_grants(
            columns={
                "grove_api": {
                    "INSERT": [
                        "tenant_id",
                        "payload_ref",
                        "payload_hash",
                        "command_schema_version",
                        "sensitivity",
                        "retention",
                        "payload",
                    ]
                }
            }
        ),
        "triggers": _AUTHORITY_TRIGGER_MAPS["public.command_payload"],
        "target_family_closure": True,
    },
    "public.agent_run": {
        "schema": "public",
        "name": "agent_run",
        "state_owner": "authority_functions",
        "write_seams": ["app.repositories.execution.insert_run_if_absent", "public.grove_*_run_command"],
        "rls": [True, True],
        "policy": {
            "name": "agent_run_tenant_isolation",
            "permissive": True,
            "using": "(tenant_id = grove_active_tenant())",
            "with_check": "(tenant_id = grove_active_tenant())",
            "roles": "{0}",
        },
        "direct_mutation_grants": _mutation_grants(
            columns={
                "grove_api": {
                    "INSERT": [
                        "run_id",
                        "tenant_id",
                        "submission_id",
                        "submission_digest",
                        "principal_id",
                        "principal_kind",
                        "skill_spec_hash",
                        "skill_spec_ref",
                        "status",
                        "revision",
                        "runtime_build_ref",
                        "runtime_build_hash",
                    ]
                }
            }
        ),
        "triggers": _AUTHORITY_TRIGGER_MAPS["public.agent_run"],
        "target_family_closure": True,
    },
    "public.run_command": {
        "schema": "public",
        "name": "run_command",
        "state_owner": "authority_functions",
        "write_seams": ["app.repositories.execution.insert_command", "public.grove_*_run_command"],
        "rls": [True, True],
        "policy": {
            "name": "run_command_tenant_isolation",
            "permissive": True,
            "using": "(tenant_id = grove_active_tenant())",
            "with_check": "(tenant_id = grove_active_tenant())",
            "roles": "{0}",
        },
        "direct_mutation_grants": _mutation_grants(
            columns={
                "grove_api": {
                    "INSERT": [
                        "command_id",
                        "tenant_id",
                        "run_id",
                        "principal_id",
                        "principal_kind",
                        "command_seq",
                        "command_type",
                        "command_schema_version",
                        "command_digest",
                        "payload_ref",
                        "payload_hash",
                        "status",
                    ]
                }
            }
        ),
        "triggers": _AUTHORITY_TRIGGER_MAPS["public.run_command"],
        "target_family_closure": True,
    },
    **{
        relation: {
            "schema": "public",
            "name": relation.rsplit(".", 1)[1],
            "state_owner": "runtime",
            "write_seams": [f"app.execution.checkpoint.{relation.rsplit('.', 1)[1]}"],
            "rls": [True, True],
            "policy": {
                "name": f"{relation.rsplit('.', 1)[1]}_tenant_policy",
                "permissive": True,
                "using": "(tenant_id = NULLIF(current_setting('grove.tenant_id'::text, true), ''::text))",
                "with_check": "(tenant_id = NULLIF(current_setting('grove.tenant_id'::text, true), ''::text))",
                "roles": "{0}",
            },
            "direct_mutation_grants": _mutation_grants(
                table={"grove_runtime": {"INSERT": True, "UPDATE": True}},
                columns={"grove_runtime": {"INSERT": "*", "UPDATE": "*"}},
            ),
            "triggers": _AUTHORITY_TRIGGER_MAPS[relation],
            "target_family_closure": True,
        }
        for relation in (
            "public.checkpoints",
            "public.checkpoint_blobs",
            "public.checkpoint_writes",
        )
    },
}

# WS-2 identity relations are read-only to online roles, but their trigger,
# RLS, policy, ownership and grant facts are still part of the executable
# authority surface.  Keep them in the same external registry as the seven
# mutation relations so a reader cannot silently skip a "read-only" table.
_IDENTITY_IMMUTABLE_TARGET = _trigger_target(
    "public.grove_reject_identity_key_change()", "", security_definer=False, settings=[]
)
_IDENTITY_VALIDATION_TARGET = _trigger_target(
    "public.grove_validate_execution_principal()", "", security_definer=False, settings=[]
)
_IDENTITY_SYNC_TARGET = _trigger_target(
    "public.grove_sync_execution_principal()",
    "",
    security_definer=True,
    settings=["search_path=public"],
)


def _identity_trigger(
    relation: str,
    name: str,
    definition: str,
    target: dict[str, Any],
) -> dict[str, Any]:
    """Build one complete WS-2 identity trigger contract entry."""

    return {
        "schema": "public",
        "table": relation,
        "name": name,
        "enabled": "O",
        "definition": definition,
        "target_function": deepcopy(target),
        "target_function_family": {str(target["identity"]): deepcopy(target)},
    }


_AUTHORITY_TRIGGER_MAPS.update(
    {
        "public.membership": {
            "public.membership.membership_identity_key_guard": _identity_trigger(
                "membership",
                "membership_identity_key_guard",
                "CREATE TRIGGER membership_identity_key_guard BEFORE UPDATE ON membership "
                "FOR EACH ROW EXECUTE FUNCTION grove_reject_identity_key_change()",
                _IDENTITY_IMMUTABLE_TARGET,
            ),
            "public.membership.membership_execution_principal_sync": _identity_trigger(
                "membership",
                "membership_execution_principal_sync",
                "CREATE TRIGGER membership_execution_principal_sync AFTER INSERT OR DELETE OR UPDATE ON membership "
                "FOR EACH ROW EXECUTE FUNCTION grove_sync_execution_principal()",
                _IDENTITY_SYNC_TARGET,
            ),
        },
        "public.workload_principal": {
            "public.workload_principal.workload_identity_key_guard": _identity_trigger(
                "workload_principal",
                "workload_identity_key_guard",
                "CREATE TRIGGER workload_identity_key_guard BEFORE UPDATE ON workload_principal "
                "FOR EACH ROW EXECUTE FUNCTION grove_reject_identity_key_change()",
                _IDENTITY_IMMUTABLE_TARGET,
            ),
            "public.workload_principal.workload_execution_principal_sync": _identity_trigger(
                "workload_principal",
                "workload_execution_principal_sync",
                "CREATE TRIGGER workload_execution_principal_sync AFTER INSERT OR DELETE OR UPDATE ON "
                "workload_principal "
                "FOR EACH ROW EXECUTE FUNCTION grove_sync_execution_principal()",
                _IDENTITY_SYNC_TARGET,
            ),
        },
        "public.execution_principal": {
            "public.execution_principal.execution_principal_identity_guard": _identity_trigger(
                "execution_principal",
                "execution_principal_identity_guard",
                "CREATE TRIGGER execution_principal_identity_guard BEFORE INSERT OR UPDATE ON execution_principal "
                "FOR EACH ROW EXECUTE FUNCTION grove_validate_execution_principal()",
                _IDENTITY_VALIDATION_TARGET,
            ),
            "public.execution_principal.execution_principal_identity_key_guard": _identity_trigger(
                "execution_principal",
                "execution_principal_identity_key_guard",
                "CREATE TRIGGER execution_principal_identity_key_guard BEFORE UPDATE ON execution_principal "
                "FOR EACH ROW EXECUTE FUNCTION grove_reject_identity_key_change()",
                _IDENTITY_IMMUTABLE_TARGET,
            ),
        },
    }
)


def _identity_relation_entry(relation: str) -> dict[str, Any]:
    policy_name = f"{relation}_tenant_isolation"
    policy = {
        "name": policy_name,
        "permissive": True,
        "using": "(tenant_id = grove_active_tenant())",
        "with_check": "(tenant_id = grove_active_tenant())",
        "roles": "{0}",
    }
    return {
        "schema": "public",
        "name": relation,
        "state_owner": "identity_source",
        "write_seams": [],
        "owner": "grove_migration",
        "relkind": "r",
        "is_partition": False,
        "parent": None,
        "partition_bound": None,
        "rls": [True, True],
        "policies": {f"public.{relation}.{policy_name}": policy},
        "rules": {},
        "direct_mutation_grants": _empty_mutation_grants(),
        "triggers": deepcopy(_AUTHORITY_TRIGGER_MAPS.get(f"public.{relation}", {})),
        "target_family_closure": True,
    }


for _identity_relation in ("tenant", "membership", "workload_principal", "execution_principal"):
    WS3_AUTHORITY_RELATION_REGISTRY[f"public.{_identity_relation}"] = _identity_relation_entry(_identity_relation)

WS3_AUTHORITY_RELATION_REGISTRY["public.checkpoint_migrations"] = {
    "schema": "public",
    "name": "checkpoint_migrations",
    "state_owner": "checkpoint_adapter",
    "write_seams": [],
    "owner": "grove_migration",
    "relkind": "r",
    "is_partition": False,
    "parent": None,
    "partition_bound": None,
    "rls": [False, False],
    "policies": {},
    "rules": {},
    "direct_mutation_grants": _empty_mutation_grants(),
    "triggers": {},
    "target_family_closure": True,
}

# Actual DML closure is a handwritten expected map.  Wrapper functions that
# merely delegate to an internal authority seam are intentionally absent; the
# trigger helper and identity synchronizer are included because their real
# definitions contain static writes.
WS3_AUTHORITY_DML_TARGETS = {
    "public.grove_accept_cancel_run(p_tenant_id text, p_run_id uuid, p_command_id uuid, p_expected_revision bigint, p_command_digest text, p_runtime_build_hash text, p_payload_ref text, p_payload_hash text, p_payload jsonb)": (  # noqa: E501
        "public.agent_run",
        "public.run_command",
    ),
    "public.grove_checkpoint_physical_guard()": ("public.agent_run", "public.run_command"),
    "public.grove_claim_run_command(p_tenant_id text, p_worker_id text, p_runtime_build_hash text, p_lease_seconds double precision)": (  # noqa: E501
        "public.agent_run",
        "public.run_command",
    ),
    "public.grove_consume_run_command_internal(p_tenant_id text, p_run_id uuid, p_command_id uuid, p_command_seq bigint, p_command_digest text, p_runtime_build_hash text, p_worker_id text, p_execution_fence bigint, p_expected_lease_until timestamp with time zone)": (  # noqa: E501
        "public.agent_run",
        "public.run_command",
    ),
    "public.grove_dead_letter_run_command_internal(p_tenant_id text, p_run_id uuid, p_command_id uuid, p_command_seq bigint, p_command_digest text, p_runtime_build_hash text, p_worker_id text, p_execution_fence bigint, p_expected_lease_until timestamp with time zone, p_reason_ref text)": (  # noqa: E501
        "public.agent_run",
        "public.run_command",
    ),
    "public.grove_heartbeat_run_command_internal(p_tenant_id text, p_run_id uuid, p_command_id uuid, p_command_seq bigint, p_command_digest text, p_runtime_build_hash text, p_worker_id text, p_execution_fence bigint, p_expected_lease_until timestamp with time zone, p_lease_seconds double precision)": (  # noqa: E501
        "public.agent_run",
        "public.run_command",
    ),
    "public.grove_reconcile_expired_run_command_internal(p_tenant_id text, p_run_id uuid)": (  # noqa: E501
        "public.agent_run",
        "public.run_command",
    ),
    "public.grove_sync_execution_principal()": ("public.execution_principal",),
}

# Normalize the original seven mutation entries into the registry catalog shape.
WS3_AUTHORITY_DML_TARGETS[
    "public.grove_finish_delivery(p_tenant_id text, p_run_id uuid, p_command_id uuid, p_command_seq bigint, p_command_digest text, p_runtime_build_hash text, p_worker_id text, p_execution_fence bigint, p_expected_lease_until timestamp with time zone, p_outcome_kind text, p_continue_payload_ref text, p_continue_payload_hash text, p_continue_payload jsonb)"  # noqa: E501
] = ("public.agent_run", "public.command_payload", "public.run_command")
# This conversion is local to the expected registry and does not consume any
# live catalog value, keeping the expected side independent from the reader.
for _relation_key, _relation_entry in WS3_AUTHORITY_RELATION_REGISTRY.items():
    _relation_entry.setdefault("owner", "grove_migration")
    _relation_entry.setdefault("relkind", "r")
    _relation_entry.setdefault("relpersistence", "p")
    _relation_entry.setdefault("replica_identity", "d")
    _relation_entry.setdefault("reloptions", [])
    _relation_entry.setdefault("is_partition", False)
    _relation_entry.setdefault("parent", None)
    _relation_entry.setdefault("children", [])
    _relation_entry.setdefault("partition_bound", None)
    _relation_entry.setdefault("rules", {})
    _legacy_policy = _relation_entry.pop("policy", None)
    if _legacy_policy is not None:
        _relation_name = str(_relation_entry["name"])
        _policy_identity = f"public.{_relation_name}.{_legacy_policy['name']}"
        _relation_entry["policies"] = {_policy_identity: _legacy_policy}
    else:
        _relation_entry.setdefault("policies", {})
    for _policy in _relation_entry["policies"].values():
        _policy.setdefault("command", "*")

# The executable catalog reader enumerates every non-extension public function,
# including the WS-2 identity/tenant helpers that older v5 evidence omitted.
WS3_SCHEMA_CONTRACT["functions"].update(
    {
        "public.grove_active_tenant()": {
            "identity_arguments": "",
            "owner": "grove_migration",
            "security_definer": False,
            "settings": [],
            "definition_sha256": "68cd279af445472c13034fb0a4bcdbf77d37c4dba552ef176c3659bc122fee87",
        },
        "public.grove_reject_execution_fence_regression()": {
            "identity_arguments": "",
            "owner": "grove_migration",
            "security_definer": False,
            "settings": ["search_path=pg_catalog, public"],
            "definition_sha256": "679ab85bc8cd35116bd5ae44d2c4cdb8a2c1cbcdeca9339f8d3cf66b7a993514",
        },
        "public.grove_reject_immutable_change()": {
            "identity_arguments": "",
            "owner": "grove_migration",
            "security_definer": False,
            "settings": [],
            "definition_sha256": "3afda1ae53b2aed06640d8a4846252b14265dd0cb02f1e509fd43b4dd50a32e6",
        },
        "public.grove_reject_identity_key_change()": {
            "identity_arguments": "",
            "owner": "grove_migration",
            "security_definer": False,
            "settings": [],
            "definition_sha256": "bcd379ca41689ba468af244cb4516d0cdb16875c887def70436c500fa9b9a5fa",
        },
        "public.grove_sync_execution_principal()": {
            "identity_arguments": "",
            "owner": "grove_migration",
            "security_definer": True,
            "settings": ["search_path=public"],
            "definition_sha256": "696bab3f7ada6844b72a574b4f473adf510e0056bc7a03d3bb0cf340089e370e",
        },
        "public.grove_validate_execution_principal()": {
            "identity_arguments": "",
            "owner": "grove_migration",
            "security_definer": False,
            "settings": [],
            "definition_sha256": "fcccb177c86edebaff2031a335d9f96e2d62f46db577cc8b2061fb292dcecbe0",
        },
        "public.grove_finish_delivery(p_tenant_id text, p_run_id uuid, p_command_id uuid, p_command_seq bigint, p_command_digest text, p_runtime_build_hash text, p_worker_id text, p_execution_fence bigint, p_expected_lease_until timestamp with time zone, p_outcome_kind text, p_continue_payload_ref text, p_continue_payload_hash text, p_continue_payload jsonb)": {  # noqa: E501
            "identity_arguments": "p_tenant_id text, p_run_id uuid, p_command_id uuid, p_command_seq bigint, p_command_digest text, p_runtime_build_hash text, p_worker_id text, p_execution_fence bigint, p_expected_lease_until timestamp with time zone, p_outcome_kind text, p_continue_payload_ref text, p_continue_payload_hash text, p_continue_payload jsonb",  # noqa: E501
            "owner": "grove_migration",
            "security_definer": True,
            "settings": ["search_path=pg_catalog, public"],
            "definition_sha256": "3f5c15a9a70dbce72c076d3250919cdfed8a81cc97a55834aedd15e4d387f7c7",
        },
    }
)
WS3_SCHEMA_CONTRACT["function_acl"].update(
    {
        "public.grove_active_tenant()": "{=X/grove_migration,grove_migration=X/grove_migration}",
        "public.grove_reject_execution_fence_regression()": "{=X/grove_migration,grove_migration=X/grove_migration}",
        "public.grove_reject_immutable_change()": "{=X/grove_migration,grove_migration=X/grove_migration}",
        "public.grove_reject_identity_key_change()": "{=X/grove_migration,grove_migration=X/grove_migration}",
        "public.grove_sync_execution_principal()": "{=X/grove_migration,grove_migration=X/grove_migration}",
        "public.grove_validate_execution_principal()": "{=X/grove_migration,grove_migration=X/grove_migration}",
    }
)
WS3_SCHEMA_CONTRACT["function_acl"].update(
    {
        identity: acl
        for identity, acl in {
            "public.grove_accept_cancel_run(p_tenant_id text, p_run_id uuid, p_command_id uuid, p_expected_revision bigint, p_command_digest text, p_runtime_build_hash text, p_payload_ref text, p_payload_hash text, p_payload jsonb)": "{grove_api=X/grove_migration,grove_migration=X/grove_migration}",  # noqa: E501
            "public.grove_claim_run_command(p_tenant_id text, p_worker_id text, p_runtime_build_hash text, p_lease_seconds double precision)": "{grove_migration=X/grove_migration,grove_runtime=X/grove_migration}",  # noqa: E501
            "public.grove_consume_run_command(p_tenant_id text, p_run_id uuid, p_command_id uuid, p_command_seq bigint, p_command_digest text, p_runtime_build_hash text, p_worker_id text, p_execution_fence bigint, p_expected_lease_until timestamp with time zone)": "{grove_migration=X/grove_migration,grove_runtime=X/grove_migration}",  # noqa: E501
            "public.grove_dead_letter_run_command(p_tenant_id text, p_run_id uuid, p_command_id uuid, p_command_seq bigint, p_command_digest text, p_runtime_build_hash text, p_worker_id text, p_execution_fence bigint, p_expected_lease_until timestamp with time zone, p_reason_ref text)": "{grove_migration=X/grove_migration,grove_runtime=X/grove_migration}",  # noqa: E501
            "public.grove_heartbeat_run_command(p_tenant_id text, p_run_id uuid, p_command_id uuid, p_command_seq bigint, p_command_digest text, p_runtime_build_hash text, p_worker_id text, p_execution_fence bigint, p_expected_lease_until timestamp with time zone, p_lease_seconds double precision)": "{grove_migration=X/grove_migration,grove_runtime=X/grove_migration}",  # noqa: E501
            "public.grove_reconcile_expired_run_command(p_tenant_id text, p_run_id uuid)": "{grove_migration=X/grove_migration,grove_projection=X/grove_migration}",  # noqa: E501
            "public.grove_finish_delivery(p_tenant_id text, p_run_id uuid, p_command_id uuid, p_command_seq bigint, p_command_digest text, p_runtime_build_hash text, p_worker_id text, p_execution_fence bigint, p_expected_lease_until timestamp with time zone, p_outcome_kind text, p_continue_payload_ref text, p_continue_payload_hash text, p_continue_payload jsonb)": "{grove_migration=X/grove_migration,grove_runtime=X/grove_migration}",  # noqa: E501
            "public.grove_checkpoint_authority_guard()": "{=X/grove_migration,grove_migration=X/grove_migration}",
            "public.grove_checkpoint_claim_provenance(p_tenant_id text, p_run_id uuid, p_command_id uuid, p_command_seq bigint, p_command_digest text, p_runtime_build_hash text, p_worker_id text, p_execution_fence bigint, p_lease_until timestamp with time zone)": "{=X/grove_migration,grove_migration=X/grove_migration}",  # noqa: E501
            "public.grove_checkpoint_tenant_guard()": "{=X/grove_migration,grove_migration=X/grove_migration}",
        }.items()
    }
)

WS3_AUTHORITY_RELATION_NAMES = tuple(sorted(key.rsplit(".", 1)[1] for key in WS3_AUTHORITY_RELATION_REGISTRY))

WS3_AUTHORITY_RELATION_EXCLUSIONS: dict[str, dict[str, Any]] = {
    f"public.{relation}": {
        "schema": "public",
        "name": relation,
        "reason": "identity source/derived identity; outside execution authority write surface",
        "online_mutation_grants": False,
        "authority_dml_targets": False,
        "authority_dml_target_identities": [],
    }
    for relation in WS3_AUTHORITY_EXCLUDED_RELATION_NAMES
}
# The identity synchronizer is the only current authority function that writes
# an excluded identity relation; this is an explicit expected fact.
WS3_AUTHORITY_RELATION_EXCLUSIONS["public.execution_principal"]["authority_dml_targets"] = True
WS3_AUTHORITY_RELATION_EXCLUSIONS["public.execution_principal"]["authority_dml_target_identities"] = [
    "public.grove_sync_execution_principal()"
]


def _full_relation_grants(relation: str) -> dict[str, dict[str, Any]]:
    """Return external expected table/column grants for one catalog relation."""

    columns = list(WS3_AUTHORITY_COLUMNS[relation])
    online = {
        role: {privilege: False for privilege in WS3_AUTHORITY_GRANT_PRIVILEGES} for role in WS3_AUTHORITY_ONLINE_ROLES
    }
    column_grants: dict[str, dict[str, list[str]]] = {
        role: {privilege: [] for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES")}
        for role in WS3_AUTHORITY_GRANT_ROLES
    }
    migration = {privilege: True for privilege in WS3_AUTHORITY_GRANT_PRIVILEGES}
    table_grants: dict[str, dict[str, bool]] = {role: dict(values) for role, values in online.items()}
    table_grants["grove_migration"] = migration
    if relation in WS3_AUTHORITY_EXCLUDED_RELATION_NAMES:
        for role in WS3_AUTHORITY_ONLINE_ROLES[:-1]:
            table_grants[role]["SELECT"] = True
            column_grants[role]["SELECT"] = list(columns)
    elif relation in WS3_CHECKPOINT_RELATIONS:
        table_grants["grove_runtime"].update({"SELECT": True, "INSERT": True, "UPDATE": True})
        for privilege in ("SELECT", "INSERT", "UPDATE"):
            column_grants["grove_runtime"][privilege] = list(columns)
    elif relation == "agent_run":
        for role in WS3_AUTHORITY_ONLINE_ROLES[:-1]:
            table_grants[role]["SELECT"] = True
            column_grants[role]["SELECT"] = list(columns)
    elif relation == "run_command":
        for role in WS3_AUTHORITY_ONLINE_ROLES[:-1]:
            table_grants[role]["SELECT"] = True
            column_grants[role]["SELECT"] = list(columns)
    elif relation in {"execution_spec", "command_payload"}:
        readable = [column for column in columns if column not in {"spec_payload", "payload"}]
        for role in WS3_AUTHORITY_ONLINE_ROLES[:-1]:
            column_grants[role]["SELECT"] = list(readable)
        if relation == "execution_spec":
            column_grants["grove_api"]["INSERT"] = list(columns[:-1])
        else:
            column_grants["grove_api"]["INSERT"] = list(columns[:-1])
    elif relation == "checkpoint_migrations":
        pass
    direct_mutation = WS3_AUTHORITY_RELATION_REGISTRY[f"public.{relation}"]["direct_mutation_grants"]
    for role in WS3_AUTHORITY_ONLINE_ROLES:
        for privilege in WS3_AUTHORITY_COLUMN_MUTATION_PRIVILEGES:
            configured = direct_mutation[role]["columns"][privilege]
            column_grants[role][privilege] = list(columns) if configured == "*" else list(configured)
    for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
        column_grants["grove_migration"][privilege] = list(columns)
    return {
        "table": table_grants,
        "columns": {
            role: {privilege: sorted(columns_for_privilege) for privilege, columns_for_privilege in privileges.items()}
            for role, privileges in column_grants.items()
        },
    }


WS3_AUTHORITY_RELATION_GRANTS = {
    relation_key: _full_relation_grants(relation_key.rsplit(".", 1)[1])
    for relation_key in WS3_AUTHORITY_RELATION_REGISTRY
}

_ROLE_BASE_ATTRIBUTES = {
    "rolsuper": False,
    "rolinherit": True,
    "rolcreaterole": False,
    "rolcreatedb": False,
    "rolcanlogin": True,
    "rolreplication": False,
    "rolconnlimit": -1,
    "rolbypassrls": False,
}
WS3_AUTHORITY_ROLE_REGISTRY: dict[str, dict[str, Any]] = {
    role: {
        "attributes": dict(_ROLE_BASE_ATTRIBUTES),
        "memberships": {"direct": [], "transitive": []},
        "schema_privileges": {"public": {"USAGE": True, "CREATE": False}},
        "database_privileges": {"CONNECT": True, "CREATE": False, "TEMP": False},
        "owned_authority_objects": [],
    }
    for role in ("grove_api", "grove_runtime", "grove_projection", "grove_governance")
}
WS3_AUTHORITY_ROLE_REGISTRY["grove_migration"] = {
    "attributes": {**_ROLE_BASE_ATTRIBUTES, "rolsuper": True, "rolbypassrls": True},
    "memberships": {"direct": [], "transitive": []},
    "schema_privileges": {"public": {"USAGE": True, "CREATE": True}},
    "database_privileges": {"CONNECT": True, "CREATE": True, "TEMP": True},
    "owned_authority_objects": sorted(list(WS3_AUTHORITY_RELATION_REGISTRY) + list(WS3_SCHEMA_CONTRACT["functions"])),
}

# Canonical v7 evidence is registry-shaped.  The old maps remain as derived
# compatibility aliases for readers that have not migrated yet.
WS3_SCHEMA_CONTRACT["authority_roles"] = deepcopy(WS3_AUTHORITY_ROLE_REGISTRY)
WS3_SCHEMA_CONTRACT["authority_public_functions"] = {
    identity: deepcopy(facts) for identity, facts in WS3_SCHEMA_CONTRACT["functions"].items()
}
WS3_SCHEMA_CONTRACT["authority_relations"] = deepcopy(WS3_AUTHORITY_RELATION_REGISTRY)
WS3_SCHEMA_CONTRACT["authority_relation_exclusions"] = deepcopy(WS3_AUTHORITY_RELATION_EXCLUSIONS)
WS3_SCHEMA_CONTRACT["authority_relation_grants"] = deepcopy(WS3_AUTHORITY_RELATION_GRANTS)
WS3_SCHEMA_CONTRACT["authority_relation_policies"] = {
    relation: deepcopy(entry["policies"]) for relation, entry in WS3_AUTHORITY_RELATION_REGISTRY.items()
}
WS3_SCHEMA_CONTRACT["authority_relation_rules"] = {
    relation: deepcopy(entry["rules"]) for relation, entry in WS3_AUTHORITY_RELATION_REGISTRY.items()
}
WS3_SCHEMA_CONTRACT["authority_object_inventory"] = deepcopy(WS3_AUTHORITY_OBJECT_INVENTORY)
WS3_SCHEMA_CONTRACT["authority_constraints"] = deepcopy(WS3_AUTHORITY_CONSTRAINTS)

_WS3_SQL_FUNCTIONS = {
    identity.split(".", 1)[1].split("(", 1)[0]: (
        "sql",
        "i" if "checkpoint_claim" in identity or "lifecycle" in identity else "s",
    )
    for identity in WS3_SCHEMA_CONTRACT["functions"]
    if (
        "grove_active_tenant" in identity
        or "grove_checkpoint_claim_provenance" in identity
        or "grove_execution_claim_lifecycle_valid" in identity
    )
}
for _function_identity, _function_facts_expected in WS3_SCHEMA_CONTRACT["functions"].items():
    _function_name = _function_identity.rsplit(".", 1)[1].split("(", 1)[0]
    _language, _volatility = _WS3_SQL_FUNCTIONS.get(_function_name, ("plpgsql", "v"))
    _function_facts_expected.update(
        {
            "prokind": "f",
            "prolang": _language,
            "provolatile": _volatility,
            "proparallel": "s" if _function_name == "grove_active_tenant" else "u",
            "proisstrict": _function_name == "grove_checkpoint_claim_provenance",
            "proleakproof": False,
            # pg_get_functiondef is deterministic and includes the body.  The
            # separate key makes body drift explicit without trusting a
            # projection that omits executable text.
            "body_sha256": _function_facts_expected["definition_sha256"],
        }
    )


def _function_acl_entry(grantee: str) -> dict[str, Any]:
    return {"grantor": "grove_migration", "grantee": grantee, "privilege": "EXECUTE", "grantable": False}


WS3_AUTHORITY_FUNCTION_ACL = {}
_function_grantees: tuple[str, ...]
for _function_identity in WS3_SCHEMA_CONTRACT["functions"]:
    _function_name = _function_identity.rsplit(".", 1)[1].split("(", 1)[0]
    if _function_name == "grove_accept_cancel_run":
        _function_grantees = ("grove_api", "grove_migration")
    elif _function_name in {
        "grove_claim_run_command",
        "grove_heartbeat_run_command",
        "grove_consume_run_command",
        "grove_dead_letter_run_command",
    }:
        _function_grantees = ("grove_migration", "grove_runtime")
    elif _function_name == "grove_reconcile_expired_run_command":
        _function_grantees = ("grove_migration", "grove_projection")
    elif _function_name in {
        "grove_reject_agent_run_runtime_build_rebinding",
        "grove_execution_claim_lifecycle_valid",
        "grove_checkpoint_physical_guard",
    } or _function_name.endswith("_internal"):
        _function_grantees = ("grove_migration",)
    elif _function_name == "grove_finish_delivery":
        _function_grantees = ("grove_migration", "grove_runtime")
    else:
        _function_grantees = ("PUBLIC", "grove_migration")
    WS3_AUTHORITY_FUNCTION_ACL[_function_identity] = sorted(
        (_function_acl_entry(_grantee) for _grantee in _function_grantees),
        key=lambda entry: (entry["grantor"], entry["grantee"], entry["privilege"], entry["grantable"]),
    )
WS3_SCHEMA_CONTRACT["authority_function_acl"] = deepcopy(WS3_AUTHORITY_FUNCTION_ACL)
for _function_identity, _function_acl_entries in WS3_AUTHORITY_FUNCTION_ACL.items():
    WS3_SCHEMA_CONTRACT["functions"][_function_identity]["acl_entries"] = deepcopy(_function_acl_entries)
for _trigger_contract in (
    WS3_SCHEMA_CONTRACT["trigger"],
    *WS3_SCHEMA_CONTRACT["agent_run_triggers"].values(),
    *WS3_SCHEMA_CONTRACT["checkpoint_triggers"].values(),
):
    _trigger_target_contract = _trigger_contract["target_function"]
    _target_identity = str(_trigger_target_contract["identity"])
    if _target_identity in WS3_AUTHORITY_FUNCTION_ACL:
        _trigger_target_contract["acl_entries"] = deepcopy(WS3_AUTHORITY_FUNCTION_ACL[_target_identity])
        _trigger_contract["target_function_family"][_target_identity]["acl_entries"] = deepcopy(
            WS3_AUTHORITY_FUNCTION_ACL[_target_identity]
        )
        _function_semantics = WS3_SCHEMA_CONTRACT["functions"].get(_target_identity, {})
        for _key in (
            "prokind",
            "prolang",
            "provolatile",
            "proparallel",
            "proisstrict",
            "proleakproof",
            "body_sha256",
        ):
            if _key in _function_semantics:
                _trigger_target_contract[_key] = deepcopy(_function_semantics[_key])
                _trigger_contract["target_function_family"][_target_identity][_key] = deepcopy(
                    _function_semantics[_key]
                )

_WS3_RELATION_ACL_PRIVILEGES = ("DELETE", "INSERT", "REFERENCES", "SELECT", "TRIGGER", "TRUNCATE", "UPDATE")


def _relation_acl_entry(grantee: str, privilege: str, grantor: str = "grove_migration") -> dict[str, Any]:
    return {"grantor": grantor, "grantee": grantee, "privilege": privilege, "grantable": False}


def _authority_acl_expected() -> dict[str, Any]:
    table_acl: dict[str, list[dict[str, Any]]] = {}
    for object_identity, object_facts in WS3_AUTHORITY_OBJECT_INVENTORY.items():
        owner = str(object_facts["owner"])
        entries = [_relation_acl_entry(owner, privilege, owner) for privilege in _WS3_RELATION_ACL_PRIVILEGES]
        table_acl[object_identity] = entries

    for relation_identity, relation_facts in WS3_AUTHORITY_RELATION_GRANTS.items():
        entries = table_acl[relation_identity]
        for role, role_facts in relation_facts["table"].items():
            entries.extend(_relation_acl_entry(role, privilege) for privilege, enabled in role_facts.items() if enabled)
        table_acl[relation_identity] = sorted(
            {
                (entry["grantor"], entry["grantee"], entry["privilege"], entry["grantable"]): entry for entry in entries
            }.values(),
            key=lambda entry: (entry["grantor"], entry["grantee"], entry["privilege"], entry["grantable"]),
        )
    # Bootstrap deliberately grants the API role read access to the migration
    # version table; it is part of the public object universe even though it
    # is not an authority relation.
    table_acl["public.alembic_version"].append(_relation_acl_entry("grove_api", "SELECT"))
    table_acl["public.alembic_version"] = sorted(
        {
            (entry["grantor"], entry["grantee"], entry["privilege"], entry["grantable"]): entry
            for entry in table_acl["public.alembic_version"]
        }.values(),
        key=lambda entry: (entry["grantor"], entry["grantee"], entry["privilege"], entry["grantable"]),
    )

    column_acl: dict[str, list[dict[str, Any]]] = {}
    for relation in ("execution_spec", "command_payload", "agent_run", "run_command"):
        relation_identity = f"public.{relation}"
        grants = WS3_AUTHORITY_RELATION_GRANTS[relation_identity]["columns"]
        for role, role_grants in grants.items():
            # PostgreSQL stores only explicit column ACLs in pg_attribute.attacl;
            # owner/default table privileges are represented by relacl and must
            # not be projected onto every column.
            for privilege, columns in role_grants.items():
                if relation in {"execution_spec", "command_payload"}:
                    if privilege == "SELECT" and role not in WS3_AUTHORITY_ONLINE_ROLES:
                        continue
                    if privilege == "INSERT" and role != "grove_api":
                        continue
                    if privilege not in {"SELECT", "INSERT"}:
                        continue
                elif role != "grove_api" or privilege != "INSERT":
                    continue
                if privilege not in {"SELECT", "INSERT", "UPDATE", "REFERENCES"}:
                    continue
                if columns == "*":
                    columns = WS3_AUTHORITY_COLUMNS[relation]
                for column in columns:
                    column_identity = f"{relation_identity}.{column}"
                    column_acl.setdefault(column_identity, []).append(_relation_acl_entry(role, privilege))
    for column_identity in column_acl:
        column_acl[column_identity] = sorted(
            {
                (entry["grantor"], entry["grantee"], entry["privilege"], entry["grantable"]): entry
                for entry in column_acl[column_identity]
            }.values(),
            key=lambda entry: (entry["grantor"], entry["grantee"], entry["privilege"], entry["grantable"]),
        )

    schema_acl = {
        "public": [
            {"grantor": "pg_database_owner", "grantee": "PUBLIC", "privilege": "USAGE", "grantable": False},
            {"grantor": "pg_database_owner", "grantee": "grove_api", "privilege": "USAGE", "grantable": False},
            {
                "grantor": "pg_database_owner",
                "grantee": "grove_governance",
                "privilege": "USAGE",
                "grantable": False,
            },
            {
                "grantor": "pg_database_owner",
                "grantee": "grove_migration",
                "privilege": "CREATE",
                "grantable": False,
            },
            {
                "grantor": "pg_database_owner",
                "grantee": "grove_migration",
                "privilege": "USAGE",
                "grantable": False,
            },
            {
                "grantor": "pg_database_owner",
                "grantee": "grove_projection",
                "privilege": "USAGE",
                "grantable": False,
            },
            {
                "grantor": "pg_database_owner",
                "grantee": "grove_runtime",
                "privilege": "USAGE",
                "grantable": False,
            },
            {
                "grantor": "pg_database_owner",
                "grantee": "pg_database_owner",
                "privilege": "CREATE",
                "grantable": False,
            },
            {
                "grantor": "pg_database_owner",
                "grantee": "pg_database_owner",
                "privilege": "USAGE",
                "grantable": False,
            },
        ]
    }
    database_acl = {
        "grove": sorted(
            [
                {"grantor": "grove", "grantee": "PUBLIC", "privilege": "CONNECT", "grantable": False},
                {"grantor": "grove", "grantee": "grove", "privilege": "CONNECT", "grantable": False},
                {"grantor": "grove", "grantee": "grove", "privilege": "CREATE", "grantable": False},
                {"grantor": "grove", "grantee": "grove", "privilege": "TEMPORARY", "grantable": False},
                *[
                    {"grantor": "grove", "grantee": role, "privilege": "CONNECT", "grantable": False}
                    for role in WS3_AUTHORITY_ROLES
                    if role != "grove_migration"
                ],
                {"grantor": "grove", "grantee": "grove_migration", "privilege": "CONNECT", "grantable": False},
            ],
            key=lambda entry: (entry["grantor"], entry["grantee"], entry["privilege"], entry["grantable"]),
        )
    }
    return {"table": table_acl, "column": column_acl, "schema": schema_acl, "database": database_acl}


WS3_AUTHORITY_ACL_EXPECTED = _authority_acl_expected()
WS3_SCHEMA_CONTRACT["authority_acl"] = deepcopy(WS3_AUTHORITY_ACL_EXPECTED)
WS3_SCHEMA_CONTRACT["authority_public_functions"] = {
    identity: {key: value for key, value in facts.items() if key not in {"schema", "name", "acl"}}
    for identity, facts in WS3_SCHEMA_CONTRACT["functions"].items()
}

WS3_SCHEMA_CONTRACT["authority_mutation_grants"] = {
    f"public.{relation}": deepcopy(WS3_AUTHORITY_RELATION_REGISTRY[f"public.{relation}"]["direct_mutation_grants"])
    for relation in WS3_AUTHORITY_MUTATION_RELATION_NAMES
}
WS3_SCHEMA_CONTRACT["table_acl"] = {
    f"{relation.rsplit('.', 1)[1]}.{role}.{privilege}": bool(role_facts["table"][privilege])
    for relation, relation_facts in WS3_AUTHORITY_RELATION_REGISTRY.items()
    for role, role_facts in relation_facts["direct_mutation_grants"].items()
    for privilege in WS3_AUTHORITY_MUTATION_PRIVILEGES
}
WS3_SCHEMA_CONTRACT["authority_dml_targets"] = {
    identity: list(targets) for identity, targets in WS3_AUTHORITY_DML_TARGETS.items()
}
WS3_SCHEMA_CONTRACT["rls"] = {entry["name"]: list(entry["rls"]) for entry in WS3_AUTHORITY_RELATION_REGISTRY.values()}
# Compatibility aliases are projections of the registry, never independent
# expected facts.
WS3_SCHEMA_CONTRACT["agent_run_triggers"] = deepcopy(WS3_AUTHORITY_RELATION_REGISTRY["public.agent_run"]["triggers"])
WS3_SCHEMA_CONTRACT["checkpoint_triggers"] = {
    key: value
    for relation in ("public.checkpoints", "public.checkpoint_blobs", "public.checkpoint_writes")
    for key, value in WS3_AUTHORITY_RELATION_REGISTRY[relation]["triggers"].items()
}
WS3_SCHEMA_CONTRACT["trigger"] = deepcopy(
    WS3_AUTHORITY_RELATION_REGISTRY["public.agent_run"]["triggers"]["public.agent_run.agent_run_execution_fence_guard"]
)

# The compatibility aliases above are rebuilt from the registry so they can
# never become an independent expected source.  Re-apply the same complete
# target-function semantics to those projections and to the registry-shaped
# relation map after rebuilding them.
_all_trigger_contracts = [
    WS3_SCHEMA_CONTRACT["trigger"],
    *WS3_SCHEMA_CONTRACT["agent_run_triggers"].values(),
    *WS3_SCHEMA_CONTRACT["checkpoint_triggers"].values(),
    *[
        trigger
        for relation_entry in WS3_SCHEMA_CONTRACT["authority_relations"].values()
        for trigger in relation_entry["triggers"].values()
    ],
]
for _trigger_contract in _all_trigger_contracts:
    _target = _trigger_contract["target_function"]
    _target_identity = str(_target["identity"])
    _function_semantics = WS3_SCHEMA_CONTRACT["functions"].get(_target_identity)
    if _function_semantics is None:
        continue
    _target["acl_entries"] = deepcopy(WS3_AUTHORITY_FUNCTION_ACL[_target_identity])
    _trigger_contract["target_function_family"][_target_identity]["acl_entries"] = deepcopy(
        WS3_AUTHORITY_FUNCTION_ACL[_target_identity]
    )
    for _key in (
        "prokind",
        "prolang",
        "provolatile",
        "proparallel",
        "proisstrict",
        "proleakproof",
        "body_sha256",
    ):
        _target[_key] = deepcopy(_function_semantics[_key])
        _trigger_contract["target_function_family"][_target_identity][_key] = deepcopy(_function_semantics[_key])


DEPENDENCY_NAMES = (
    "fastapi",
    "pydantic",
    "pydantic-ai-slim",
    "sqlalchemy",
    "structlog",
    "psycopg",
    "alembic",
    "langgraph",
    "langgraph-checkpoint-postgres",
)


class ManifestError(ValueError):
    """Raised when a manifest cannot be verified."""


class SourceInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    commit: str = Field(min_length=1, max_length=128)
    dirty: bool


class MigrationInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    head: str = Field(min_length=1, max_length=128)
    hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ImageInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    application: str = Field(min_length=1, max_length=128)
    postgres: str = Field(min_length=1, max_length=128)

    @field_validator("application", "postgres")
    @classmethod
    def validate_image_reference(cls, value: str) -> str:
        if value in {"not_built", "not_resolved"}:
            return value
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
            raise ValueError("image reference must be a sha256 image ID or an explicit draft placeholder")
        return value


class AdapterCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool
    status: Literal["disabled", "enabled", "not_configured"]
    reason: str = Field(min_length=1, max_length=128)


class SigningInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["not_configured"]
    reference: str | None = Field(default=None, max_length=256)


def _validate_evidence_reference(
    ref: str,
    digest: str,
    label: str,
    expected_filename: str,
) -> None:
    if ref == EVIDENCE_PLACEHOLDER and digest == EVIDENCE_PLACEHOLDER:
        return
    if ref == EVIDENCE_PLACEHOLDER or digest == EVIDENCE_PLACEHOLDER:
        raise ValueError(f"{label} evidence ref and hash must be provided together")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError(f"{label} evidence hash must be a sha256 digest")
    match = EVIDENCE_REF_PATTERN.fullmatch(ref)
    if match is None or match.group(1) != digest:
        raise ValueError(f"{label} evidence ref must contain its sha256 digest")
    if Path(ref).name != expected_filename:
        raise ValueError(f"{label} evidence ref must name {expected_filename}")


def _trusted_catalog_anchor() -> dict[str, Any]:
    """Load code-fixed catalog facts instead of trusting Manifest self-report."""

    try:
        return trusted_catalog_authority_anchor()
    except CatalogAuthorityError as exc:
        raise ValueError(f"catalog authority trusted anchor is invalid: {exc}") from exc


def _validate_manifest_catalog_anchor(manifest: RuntimeBuildManifest, anchor: dict[str, Any]) -> None:
    expected_sections = anchor["sections"]
    actual_sections = {
        name: summary.model_dump(mode="json") for name, summary in manifest.catalog_authority_sections.items()
    }
    if manifest.catalog_authority_compiler_version != anchor["compiler_version"]:
        raise ValueError("catalog authority compiler version does not match the trusted source anchor")
    if manifest.catalog_authority_artifact_hash != anchor["artifact_hash"]:
        raise ValueError("catalog authority artifact hash does not match the trusted source anchor")
    if manifest.catalog_authority_expected_root != anchor["expected_root"]:
        raise ValueError("catalog authority expected root does not match the trusted source anchor")
    if actual_sections != expected_sections:
        raise ValueError("catalog authority section summaries do not match the trusted source anchor")


class CatalogAuthoritySectionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    count: int = Field(ge=0)
    root: str = Field(pattern=r"^[0-9a-f]{64}$")


class RuntimeBuildManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = Field(default=1)
    evidence_mode: ManifestMode
    source: SourceInfo
    python: str = Field(min_length=1, max_length=32)
    uv_lock_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    dependencies: dict[str, str]
    migration: MigrationInfo
    sbom_ref: str = Field(default=EVIDENCE_PLACEHOLDER, min_length=1, max_length=256)
    sbom_hash: str = Field(default=EVIDENCE_PLACEHOLDER, max_length=64)
    migration_report_ref: str = Field(default=EVIDENCE_PLACEHOLDER, min_length=1, max_length=256)
    migration_report_hash: str = Field(default=EVIDENCE_PLACEHOLDER, max_length=64)
    images: ImageInfo
    roles: tuple[Role, ...]
    adapter_capabilities: dict[str, AdapterCapability]
    application_version: str = Field(min_length=1, max_length=32)
    schema_contract_version: str = Field(min_length=1, max_length=32)
    # These are external anchors, not values derived from a live database or
    # from the artifact's self-hash.  They stay present for draft manifests so
    # an omitted/NULL anchor cannot silently downgrade release verification.
    catalog_authority_compiler_version: str = Field(min_length=1, max_length=64)
    catalog_authority_artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_authority_expected_root: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_authority_sections: dict[str, CatalogAuthoritySectionSummary]
    signing: SigningInfo
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("roles")
    @classmethod
    def validate_roles(cls, value: tuple[Role, ...]) -> tuple[Role, ...]:
        expected = tuple(Role)
        if value != expected:
            raise ValueError("manifest roles must contain the canonical four roles in order")
        return value

    @model_validator(mode="after")
    def validate_evidence(self) -> RuntimeBuildManifest:
        _validate_evidence_reference(self.sbom_ref, self.sbom_hash, "SBOM", "runtime-sbom.cdx.json")
        _validate_evidence_reference(
            self.migration_report_ref,
            self.migration_report_hash,
            "migration report",
            "migrations.json",
        )
        _validate_manifest_catalog_anchor(self, _trusted_catalog_anchor())
        return self


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_payload(manifest: RuntimeBuildManifest | dict[str, Any]) -> dict[str, Any]:
    if type(manifest) is RuntimeBuildManifest:
        data = manifest.model_dump(mode="json")
    elif type(manifest) is dict:
        data = dict(manifest)
    else:
        raise TypeError("runtime build manifest must be an exact RuntimeBuildManifest or dict")
    data.pop("manifest_hash", None)
    return data


def canonical_bytes(manifest: RuntimeBuildManifest | dict[str, Any]) -> bytes:
    """Serialize a manifest without timestamps, paths, or non-deterministic whitespace."""

    payload = _canonical_payload(manifest)
    return canonical_contract_bytes(payload, exclude_fields=("manifest_hash",))


def _manifest_with_hash(payload: dict[str, Any]) -> dict[str, Any]:
    without_hash = dict(payload)
    without_hash.pop("manifest_hash", None)
    without_hash["manifest_hash"] = _sha256(canonical_bytes(without_hash))
    # Validate generated manifests at the boundary so an invalid image or evidence
    # reference can never be emitted as apparently usable JSON.  Draft placeholders
    # remain valid here; the strict release verifier rejects them explicitly.
    RuntimeBuildManifest.model_validate(without_hash)
    return without_hash


def verify_manifest(
    manifest: RuntimeBuildManifest | dict[str, Any],
    *,
    root: Path | None = None,
    raise_on_error: bool = False,
    require_release: bool = False,
) -> bool:
    """Verify schema and content hash; optionally raise a precise error for CLI use."""

    try:
        if type(manifest) is RuntimeBuildManifest:
            parsed = manifest
        elif type(manifest) is dict:
            parsed = RuntimeBuildManifest.model_validate(manifest)
        else:
            raise ManifestError("runtime build manifest must be an exact RuntimeBuildManifest or dict")
        # Re-read the trusted source anchor at the verification boundary.  A
        # caller may have constructed a model without the normal validator or
        # may have rehashed every self-declared field; neither is an authority
        # for the catalog contract.
        _validate_manifest_catalog_anchor(parsed, _trusted_catalog_anchor())
        if require_release:
            if parsed.source.dirty:
                raise ManifestError(
                    "release verification requires source.dirty=false; draft evidence is not publishable"
                )
            if parsed.evidence_mode != "release":
                raise ManifestError("release verification requires evidence_mode=release")
            if parsed.images.application in {"not_built", "not_resolved"} or parsed.images.postgres in {
                "not_built",
                "not_resolved",
            }:
                raise ManifestError("release verification requires resolved image IDs")
            if root is None:
                raise ManifestError("release verification requires an evidence root")
        has_content_addressed_evidence = any(
            ref != EVIDENCE_PLACEHOLDER for ref in (parsed.sbom_ref, parsed.migration_report_ref)
        )
        if root is None and has_content_addressed_evidence:
            raise ManifestError("evidence verification requires an evidence root")
        if root is not None:
            _verify_evidence_files(parsed, root, require_release=require_release)
        expected = _sha256(canonical_bytes(parsed))
        if parsed.manifest_hash != expected:
            raise ManifestError("manifest hash mismatch")
    except (ManifestError, OSError, ValueError, TypeError) as exc:
        if raise_on_error:
            if isinstance(exc, ManifestError):
                raise
            raise ManifestError(str(exc)) from exc
        return False
    return True


def _git_output(root: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def migration_hash(root: Path) -> str:
    """Hash migration execution plus the source-controlled catalog authority root."""

    digest = hashlib.sha256()
    relative_files = set(MIGRATION_EXECUTION_FILES)
    versions_root = root / "alembic" / "versions"
    # Revisions are normally Python modules, but the execution closure is the
    # whole revisions directory.  Include every regular source file (including
    # nested revision assets) while excluding interpreter cache artifacts.
    relative_files.update(
        path.relative_to(root).as_posix()
        for path in versions_root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )
    for relative in sorted(relative_files):
        path = root / relative
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes() if path.is_file() else b"<missing>")
        digest.update(b"\0")
    return digest.hexdigest()


def migration_head(root: Path) -> str:
    """Return the single head from Alembic's actual revision graph."""

    config = AlembicConfig(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    heads = ScriptDirectory.from_config(config).get_heads()
    if len(heads) != 1:
        raise ManifestError(f"expected one Alembic head, found {heads!r}")
    return heads[0]


def write_content_addressed_artifact(output: Path, payload: bytes) -> str:
    """Write an artifact and its immutable sibling, returning the digest."""

    digest = _sha256(payload)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.parent.is_symlink() or not output.parent.is_dir():
        raise ManifestError("evidence output directory must be a regular directory")

    sha_root = output.parent / "sha256"
    _ensure_regular_directory(sha_root, "evidence CAS root")
    digest_directory = sha_root / digest
    _ensure_regular_directory(digest_directory, "evidence CAS digest directory")
    content_path = digest_directory / output.name
    _write_immutable_cas_file(content_path, payload)
    _atomic_replace_alias(output, payload)
    return digest


def _ensure_regular_directory(path: Path, label: str) -> None:
    if path.is_symlink():
        raise ManifestError(f"{label} must not be a symbolic link")
    try:
        path.mkdir()
    except FileExistsError:
        pass
    if path.is_symlink() or not path.is_dir():
        raise ManifestError(f"{label} must be a regular directory")


def _write_immutable_cas_file(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        raise ManifestError("evidence CAS file must not be a symbolic link")
    if path.exists():
        if not path.is_file():
            raise ManifestError("evidence CAS path must be a regular file")
        if path.read_bytes() != payload:
            raise ManifestError("existing evidence CAS content conflicts with its digest path")
        return

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o644)
    except FileExistsError:
        _write_immutable_cas_file(path, payload)
        return
    with os.fdopen(descriptor, "wb") as stream:
        os.fchmod(stream.fileno(), 0o644)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _atomic_replace_alias(output: Path, payload: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            # mkstemp intentionally starts at 0600. Evidence contains no
            # secrets and must remain readable by the host artifact uploader
            # after a non-root container atomically replaces the alias.
            os.fchmod(stream.fileno(), 0o644)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def _workspace_evidence(root: Path, filename: str) -> tuple[str, str]:
    fixed_path = root / "ci-evidence" / filename
    if not fixed_path.is_file():
        return EVIDENCE_PLACEHOLDER, EVIDENCE_PLACEHOLDER
    digest = _sha256(fixed_path.read_bytes())
    ref = f"ci-evidence/sha256/{digest}/{filename}"
    content_path = root / ref
    if not content_path.is_file():
        raise ManifestError(f"evidence CAS copy is missing for {filename}: {ref}")
    if _sha256(content_path.read_bytes()) != digest:
        raise ManifestError(f"evidence CAS copy hash mismatch for {filename}: {ref}")
    return ref, digest


def _verify_evidence_files(manifest: RuntimeBuildManifest, root: Path, *, require_release: bool) -> None:
    root = root.resolve()
    for label, ref, expected in (
        ("SBOM", manifest.sbom_ref, manifest.sbom_hash),
        ("migration report", manifest.migration_report_ref, manifest.migration_report_hash),
    ):
        if ref == EVIDENCE_PLACEHOLDER:
            if require_release:
                raise ManifestError(f"release verification requires {label} evidence")
            continue
        path = root / ref
        if not path.is_file():
            raise ManifestError(f"{label} evidence file is missing: {ref}")
        try:
            path.resolve().relative_to(root)
        except ValueError as exc:
            raise ManifestError(f"{label} evidence file escapes evidence root: {ref}") from exc
        if path.is_symlink():
            raise ManifestError(f"{label} evidence file must be a regular CAS file: {ref}")
        actual = _sha256(path.read_bytes())
        if actual != expected:
            raise ManifestError(f"{label} evidence hash mismatch")
        if label == "SBOM":
            _verify_sbom(path, manifest)
        elif label == "migration report":
            _verify_migration_report(path, manifest)


def _load_evidence_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"{label} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ManifestError(f"{label} must be a JSON object")
    return payload


def _normalize_package_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _verify_sbom(path: Path, manifest: RuntimeBuildManifest) -> None:
    """Verify the minimum CycloneDX semantics bound by a release manifest."""

    payload = _load_evidence_object(path, "SBOM")
    if payload.get("bomFormat") != "CycloneDX" or payload.get("specVersion") != "1.5":
        raise ManifestError("SBOM must be CycloneDX 1.5")
    metadata = payload.get("metadata")
    component = metadata.get("component") if isinstance(metadata, dict) else None
    if not isinstance(component, dict):
        raise ManifestError("SBOM metadata.component must be an object")
    if component.get("name") != "grove" or component.get("version") != manifest.application_version:
        raise ManifestError("SBOM root component does not match the manifest application")

    components = payload.get("components")
    if not isinstance(components, list):
        raise ManifestError("SBOM components must be a list")
    available: dict[str, set[str]] = {}
    for item in components:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or not isinstance(item.get("version"), str)
        ):
            raise ManifestError("SBOM components must contain string name and version fields")
        available.setdefault(_normalize_package_name(item["name"]), set()).add(item["version"])
    for name, version in manifest.dependencies.items():
        if version not in available.get(_normalize_package_name(name), set()):
            raise ManifestError(f"SBOM dependency does not match manifest: {name}")


def _verify_migration_report(path: Path, manifest: RuntimeBuildManifest) -> None:
    """Verify migration evidence semantics in addition to its CAS bytes."""

    payload = _load_evidence_object(path, "migration report")
    if payload.get("status") != "completed":
        raise ManifestError("migration report status must be completed")
    report_head = payload.get("head")
    if not isinstance(report_head, str) or report_head != manifest.migration.head:
        raise ManifestError("migration report head does not match manifest migration head")
    report_hash = payload.get("migration_hash")
    if not isinstance(report_hash, str) or report_hash != manifest.migration.hash:
        raise ManifestError("migration report hash does not match manifest migration hash")
    if payload.get("round_trip") != ["upgrade head", "downgrade base", "upgrade head"]:
        raise ManifestError("migration report round trip is incomplete")
    relations = payload.get("business_tables")
    if not isinstance(relations, list) or not all(isinstance(item, str) for item in relations):
        raise ManifestError("migration report business_tables must be a list of relation names")
    expected_relations = (
        WS2_BUSINESS_RELATIONS
        if report_head in {"ws2_tenant_commands", "ws3_execution_driver"}
        else WS3_BUSINESS_RELATIONS
        if report_head
        in {
            "ws3_checkpoint_fenced",
            "ws3_cancel_acceptance",
            "ws3_dead_letter_reconciliation",
            "ws3_execution_authority_closure",
            "ws3_runtime_worker_delivery",
        }
        else WS4_BUSINESS_RELATIONS
        if report_head in WS4_MIGRATION_HEADS
        else frozenset()
    )
    if set(relations) != expected_relations:
        raise ManifestError("migration report relation set does not match migration head")
    infrastructure_relations = payload.get("infrastructure_tables", [])
    if (
        report_head
        in {
            "ws3_checkpoint_fenced",
            "ws3_cancel_acceptance",
            "ws3_dead_letter_reconciliation",
            "ws3_execution_authority_closure",
            "ws3_runtime_worker_delivery",
        }
        or report_head in WS4_MIGRATION_HEADS
    ) and set(infrastructure_relations) != WS3_INFRASTRUCTURE_RELATIONS:
        raise ManifestError("migration report infrastructure relation set does not match migration head")
    report_contract_version = payload.get("schema_contract_version")
    if report_contract_version is not None and report_contract_version != manifest.schema_contract_version:
        raise ManifestError("migration report schema contract version does not match manifest")
    if (
        report_head
        in {
            "ws3_execution_driver",
            "ws3_checkpoint_fenced",
            "ws3_cancel_acceptance",
            "ws3_dead_letter_reconciliation",
            "ws3_execution_authority_closure",
            "ws3_runtime_worker_delivery",
        }
        or report_head in WS4_MIGRATION_HEADS
    ):
        if report_contract_version != WS3_SCHEMA_CONTRACT_VERSION or payload.get("ws3_schema") != WS3_SCHEMA_CONTRACT:
            raise ManifestError("migration report WS-3 schema evidence does not match the fixed contract")
    if report_head in {"ws3_execution_authority_closure", "ws3_runtime_worker_delivery"}:
        catalog_report = payload.get("catalog_authority")
        if not isinstance(catalog_report, dict):
            raise ManifestError("migration report catalog authority evidence is missing")
        trusted = _trusted_catalog_anchor()
        if catalog_report.get("compiler_version") != trusted["compiler_version"]:
            raise ManifestError("migration report catalog authority compiler does not match trusted anchor")
        if catalog_report.get("expected_artifact_hash") != trusted["artifact_hash"]:
            raise ManifestError("migration report catalog authority artifact does not match trusted anchor")
        if catalog_report.get("expected_root") != trusted["expected_root"]:
            raise ManifestError("migration report catalog authority expected root does not match trusted anchor")
        actual_root = catalog_report.get("actual_root")
        if actual_root != trusted["expected_root"]:
            raise ManifestError("migration report catalog authority actual root does not match trusted anchor")
        report_sections = catalog_report.get("sections")
        expected_sections = trusted["sections"]
        if report_sections != expected_sections:
            raise ManifestError("migration report catalog authority sections do not match trusted anchor")
        report_counts = catalog_report.get("section_counts")
        expected_counts = {name: section["count"] for name, section in expected_sections.items()}
        if report_counts != expected_counts:
            raise ManifestError("migration report catalog authority section counts do not match trusted anchor")


def _dependency_versions() -> dict[str, str]:
    values: dict[str, str] = {}
    for name in DEPENDENCY_NAMES:
        try:
            values[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            values[name] = "not_installed"
    return values


def build_manifest(
    *,
    root: Path,
    source_commit: str,
    dirty: bool,
    python_version: str,
    uv_lock_hash: str,
    migration_head: str,
    migration_hash: str,
    app_image_id: str,
    postgres_image_id: str,
    sbom_ref: str = EVIDENCE_PLACEHOLDER,
    sbom_hash: str = EVIDENCE_PLACEHOLDER,
    migration_report_ref: str = EVIDENCE_PLACEHOLDER,
    migration_report_hash: str = EVIDENCE_PLACEHOLDER,
    evidence_mode: ManifestMode = "draft",
) -> dict[str, Any]:
    """Build the stable baseline manifest from explicit, already-resolved inputs."""

    payload: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "evidence_mode": evidence_mode,
        "source": {"commit": source_commit, "dirty": dirty},
        "python": python_version,
        "uv_lock_hash": uv_lock_hash,
        "dependencies": _dependency_versions(),
        "migration": {"head": migration_head, "hash": migration_hash},
        "sbom_ref": sbom_ref,
        "sbom_hash": sbom_hash,
        "migration_report_ref": migration_report_ref,
        "migration_report_hash": migration_report_hash,
        "images": {"application": app_image_id, "postgres": postgres_image_id},
        "roles": tuple(role.value for role in Role),
        "adapter_capabilities": {
            "dbos": {"enabled": False, "status": "disabled", "reason": "not_installed"},
        },
        "application_version": __version__,
        "schema_contract_version": (
            WS3_SCHEMA_CONTRACT_VERSION
            if migration_head
            in {
                "ws3_execution_driver",
                "ws3_checkpoint_fenced",
                "ws3_cancel_acceptance",
                "ws3_dead_letter_reconciliation",
                "ws3_execution_authority_closure",
                "ws3_runtime_worker_delivery",
            }
            or migration_head in WS4_MIGRATION_HEADS
            else "ws2-tenant-commands"
        ),
        "signing": {"status": "not_configured", "reference": None},
        "catalog_authority_compiler_version": CATALOG_AUTHORITY_COMPILER_VERSION,
        "catalog_authority_artifact_hash": expected_catalog_artifact_hash(),
        "catalog_authority_expected_root": expected_catalog_authority_root(),
        "catalog_authority_sections": expected_catalog_authority_sections(),
    }
    # The root is intentionally used only for local hashing; it never enters the manifest.
    if not root:
        raise ManifestError("manifest root is required")
    return _manifest_with_hash(payload)


def build_manifest_from_workspace(root: Path) -> dict[str, Any]:
    lock_path = root / "uv.lock"
    if not lock_path.is_file():
        raise ManifestError("uv.lock is required")
    source_commit = _git_output(root, "rev-parse", "HEAD")
    dirty = bool(_git_output(root, "status", "--porcelain=v1"))
    uv_hash = _sha256(lock_path.read_bytes())
    app_image_id = _image_id_or_default("GROVE_APP_IMAGE_ID", "not_built")
    postgres_image_id = _image_id_or_default("GROVE_POSTGRES_IMAGE_ID", "not_resolved")
    sbom_ref, sbom_digest = _workspace_evidence(root, "runtime-sbom.cdx.json")
    migration_report_ref, migration_report_digest = _workspace_evidence(root, "migrations.json")
    # A fixed-name evidence alias may be left over from an older migration
    # graph. Do not emit it as if it described the current workspace; draft
    # manifests fall back to explicit placeholders until a fresh report is
    # produced. Explicit manifests still fail closed in ``verify_manifest``.
    current_migration_head = migration_head(root)
    current_migration_hash = migration_hash(root)
    if migration_report_ref != EVIDENCE_PLACEHOLDER:
        try:
            report = json.loads((root / migration_report_ref).read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            report = None
        if (
            not isinstance(report, dict)
            or report.get("head") != current_migration_head
            or report.get("migration_hash") != current_migration_hash
        ):
            migration_report_ref, migration_report_digest = EVIDENCE_PLACEHOLDER, EVIDENCE_PLACEHOLDER
    resolved_images = re.fullmatch(r"sha256:[0-9a-f]{64}", app_image_id) and re.fullmatch(
        r"sha256:[0-9a-f]{64}", postgres_image_id
    )
    evidence_mode: ManifestMode = (
        "release"
        if not dirty
        and resolved_images
        and sbom_ref != EVIDENCE_PLACEHOLDER
        and migration_report_ref != EVIDENCE_PLACEHOLDER
        else "draft"
    )
    return build_manifest(
        root=root,
        source_commit=source_commit,
        dirty=dirty,
        python_version="3.12.12",
        uv_lock_hash=uv_hash,
        migration_head=current_migration_head,
        migration_hash=current_migration_hash,
        app_image_id=app_image_id,
        postgres_image_id=postgres_image_id,
        sbom_ref=sbom_ref,
        sbom_hash=sbom_digest,
        migration_report_ref=migration_report_ref,
        migration_report_hash=migration_report_digest,
        evidence_mode=evidence_mode,
    )


def _image_id_or_default(name: str, default: str) -> str:
    import os

    value = os.environ.get(name, "").strip()
    if re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        return value
    # Local tags are intentionally not evidence.  A tag can point at an image
    # built from a different source/lock state; only an explicit immutable ID
    # may be paired with a manifest, and dirty source remains draft regardless.
    return default
