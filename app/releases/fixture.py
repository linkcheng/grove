"""One typed, content-addressed non-production WS-2 fixture release.

The fixture is deliberately published as a small immutable artifact graph.  A
release document only names exact bytes; every resolver input is loaded and
verified through the existing WS-1/runtime-build validators before a caller
can construct a ``SkillExecutionSpec``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.build.catalog_authority import (
    CATALOG_AUTHORITY_COMPILER_VERSION,
    expected_catalog_artifact_hash,
    expected_catalog_authority_root,
    expected_catalog_authority_sections,
)
from app.build.manifest import RuntimeBuildManifest, verify_manifest
from app.contracts.canonical import ArtifactRef, EvaluationEvidenceRef, VersionedRef, canonical_hash
from app.core.config import Role
from app.skill_abi.models import ContractBinding, GraphBinding, SkillRuntimeManifest
from app.skill_abi.runtime import validate_artifact, verify_runtime_manifest


class FixtureReleaseError(ValueError):
    """The trusted fixture release or one of its bytes is unavailable/invalid."""


class FixtureInput(BaseModel):
    """The single input contract published by the fixture release."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=16_384)

    @model_validator(mode="before")
    @classmethod
    def reject_unknown_input_fields(cls, value: object) -> object:
        if isinstance(value, dict) and set(value) - {"question"}:
            raise ValueError("input fields are not accepted")
        return value


class FixtureOutputSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=32)
    status: Literal["accepted"]


class FixtureSkillArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["skill"]
    ref: str = Field(min_length=1, max_length=256)
    release_ref: str = Field(min_length=1, max_length=256)


class FixtureAgentArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["agent"]
    ref: str = Field(min_length=1, max_length=256)
    skill_ref: str = Field(min_length=1, max_length=256)
    release_ref: str = Field(min_length=1, max_length=256)


class FixtureGraphArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: str = Field(min_length=1, max_length=256)
    version: str = Field(min_length=1, max_length=32)
    state_schema_version: str = Field(min_length=1, max_length=128)
    nodes: tuple[str, ...] = Field(min_length=1, max_length=32)


class FixtureContractArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: str = Field(min_length=1, max_length=256)
    version: str = Field(min_length=1, max_length=32)
    command_schema_version: Literal["start.v1"]


class FixtureAuthorizationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: str = Field(min_length=1, max_length=256)
    revision: str = Field(min_length=1, max_length=32)
    ceiling_scopes: tuple[str, ...] = Field(min_length=1, max_length=32)
    effect: Literal["accepted.start"]


class FixturePermissionPreset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: str = Field(min_length=1, max_length=256)
    preset: Literal["interactive", "read_only", "workspace_edit", "unattended"]
    evaluation_effect: Literal["accepted.start"]


class FixtureEvidenceAttestation(BaseModel):
    """Immutable, pre-published conformance attestation bytes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["evaluation_attestation"]
    preset_ref: str = Field(min_length=1, max_length=256)
    suite_ref: str = Field(min_length=1, max_length=256)
    evaluation_run_id: UUID
    evaluation_subject_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["passed"]
    issuer: str = Field(min_length=1, max_length=256)


class FixtureEvidenceBundle(BaseModel):
    """The exact suite/run bundle referenced by ``EvaluationEvidenceRef``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["evaluation_bundle"]
    preset_ref: str = Field(min_length=1, max_length=256)
    suite_ref: str = Field(min_length=1, max_length=256)
    evaluation_run_id: UUID
    evaluation_subject_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["passed"]
    issuer: str = Field(min_length=1, max_length=256)
    attestation_ref: ArtifactRef
    attestation_artifact_ref: VersionedRef


class FixtureEvidenceIndex(BaseModel):
    """A pre-published suite/run index bound to one exact subject."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    preset_ref: str = Field(min_length=1, max_length=256)
    suite_ref: str = Field(min_length=1, max_length=256)
    evaluation_run_id: UUID
    evaluation_subject_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["passed"]
    issuer: str = Field(min_length=1, max_length=256)
    bundle_ref: VersionedRef
    attestation_artifact_ref: VersionedRef
    evaluation: EvaluationEvidenceRef


class FixtureRelease(BaseModel):
    """Release metadata plus exact references to its typed artifact bytes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    release_ref: str = Field(min_length=1, max_length=256)
    skill_ref: str = Field(min_length=1, max_length=256)
    agent_ref: str = Field(min_length=1, max_length=256)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    budget: dict[str, Any]
    runtime_manifest: dict[str, Any]
    graph: dict[str, Any]
    asset_risk_graph: dict[str, Any]
    asset_risk_evaluation_subject_hashes: dict[str, str]
    contracts: dict[str, Any]
    runtime_build: dict[str, Any]
    authorization_policy: dict[str, Any]
    evaluation_subject_hashes: dict[str, str]
    artifact_refs: dict[str, VersionedRef]
    resolver_version: str = Field(min_length=1, max_length=128)


@dataclass(frozen=True, slots=True)
class FixtureReleaseBundle:
    release: FixtureRelease
    input_schema: dict[str, Any]
    output_schema: FixtureOutputSchema
    budget: dict[str, Any]
    budget_ref: VersionedRef
    skill: FixtureSkillArtifact
    agent: FixtureAgentArtifact
    runtime_manifest: SkillRuntimeManifest
    runtime_manifest_ref: VersionedRef
    graph: FixtureGraphArtifact
    graph_ref: VersionedRef
    graph_binding: GraphBinding
    asset_risk_graph: FixtureGraphArtifact
    asset_risk_graph_ref: VersionedRef
    asset_risk_graph_binding: GraphBinding
    contracts: FixtureContractArtifact
    contracts_ref: VersionedRef
    contracts_binding: ContractBinding
    runtime_build: RuntimeBuildManifest
    runtime_build_ref: VersionedRef
    authorization_policy: FixtureAuthorizationPolicy
    authorization_policy_ref: VersionedRef
    permission_presets: Mapping[str, FixturePermissionPreset]
    evaluation_subject_hashes: Mapping[str, str]
    evidence_indexes: Mapping[str, FixtureEvidenceIndex]
    asset_risk_evaluation_subject_hashes: Mapping[str, str]
    asset_risk_evidence_indexes: Mapping[str, FixtureEvidenceIndex]
    evaluation_evidence: Mapping[str, EvaluationEvidenceRef]
    artifact_bytes: Mapping[str, bytes]


FIXTURE_RELEASE_REF = "release.fixture@1"
FIXTURE_EVALUATION_ISSUER = "trusted.conformance.publisher@1"
FIXTURE_EVALUATION_ISSUER_ALLOWLIST = frozenset({FIXTURE_EVALUATION_ISSUER})
FIXTURE_EVALUATION_SUITE_REF = "suite.fixture.conformance@1"
FIXTURE_EVIDENCE_TENANT = "fixture"
FIXTURE_CONSTRAINTS_PAYLOAD: dict[str, Any] = {
    "deadline_ms": None,
    "max_cost": None,
    "data_residency": None,
}


def _canonical_bytes(document: Mapping[str, Any]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _artifact_ref(ref: str, payload: bytes) -> VersionedRef:
    return VersionedRef(ref=ref, version="1", content_hash=_sha256(payload))


def _model_bytes(model: BaseModel) -> bytes:
    return _canonical_bytes(model.model_dump(mode="json"))


def _fixture_subject_hash(
    *,
    skill_ref: VersionedRef,
    graph_binding: GraphBinding,
    contracts_binding: ContractBinding,
    runtime_manifest_ref: VersionedRef,
    runtime_build_ref: VersionedRef,
    authorization_policy_ref: VersionedRef,
    permission_preset_ref: VersionedRef,
    permission_envelope_hash: str,
    budget_ref: VersionedRef,
) -> str:
    """Compute the exact behavior subject used by the WS-1 runtime verifier."""

    contracts = contracts_binding.model_dump(mode="python", exclude_unset=True)
    contracts.pop("converter_bundle", None)
    return canonical_hash(
        {
            "skill": skill_ref.model_dump(mode="python", exclude_unset=True),
            "run_mode": "live",
            "graph": graph_binding.model_dump(mode="python", exclude_unset=True),
            "contracts": contracts,
            "runtime_manifest": runtime_manifest_ref.model_dump(mode="python", exclude_unset=True),
            "runtime_build": runtime_build_ref.model_dump(mode="python", exclude_unset=True),
            "permission_envelope": permission_envelope_hash,
            "authorization_policy": authorization_policy_ref.model_dump(mode="python", exclude_unset=True),
            "permission_preset": permission_preset_ref.model_dump(mode="python", exclude_unset=True),
            "behavior_policy_refs": (),
            "evaluation_budget": budget_ref.model_dump(mode="python", exclude_unset=True),
        }
    )


def _draft_runtime_build() -> RuntimeBuildManifest:
    document: dict[str, Any] = {
        "schema_version": 1,
        "evidence_mode": "draft",
        "source": {"commit": "fixture-release", "dirty": True},
        "python": "3.12.12",
        "uv_lock_hash": "a" * 64,
        "dependencies": {},
        "migration": {"head": "ws2_tenant_commands", "hash": "b" * 64},
        "sbom_ref": "not_generated",
        "sbom_hash": "not_generated",
        "migration_report_ref": "not_generated",
        "migration_report_hash": "not_generated",
        "images": {"application": "not_built", "postgres": "not_resolved"},
        "roles": [role.value for role in Role],
        "adapter_capabilities": {},
        "application_version": "0.1.0",
        "schema_contract_version": "ws2.fixture@1",
        "catalog_authority_compiler_version": CATALOG_AUTHORITY_COMPILER_VERSION,
        "catalog_authority_artifact_hash": expected_catalog_artifact_hash(),
        "catalog_authority_expected_root": expected_catalog_authority_root(),
        "catalog_authority_sections": expected_catalog_authority_sections(),
        "signing": {"status": "not_configured", "reference": None},
    }
    without_hash = _canonical_bytes(document)
    document["manifest_hash"] = _sha256(without_hash)
    manifest = RuntimeBuildManifest.model_validate(document)
    verify_manifest(manifest, raise_on_error=True)
    return manifest


def _publish_variant_evidence(
    *,
    variant: str,
    preset: str,
    preset_ref: VersionedRef,
    subject_hash: str,
    publish: Any,
    index_map: dict[str, VersionedRef],
    bundle_map: dict[str, VersionedRef],
    attestation_map: dict[str, VersionedRef],
) -> None:
    run_id = uuid5(NAMESPACE_URL, f"grove:fixture:evaluation-run:{variant}:{preset_ref.ref}:{subject_hash}")
    attestation_model = FixtureEvidenceAttestation(
        kind="evaluation_attestation",
        preset_ref=preset_ref.ref,
        suite_ref=FIXTURE_EVALUATION_SUITE_REF,
        evaluation_run_id=run_id,
        evaluation_subject_hash=subject_hash,
        decision="passed",
        issuer=FIXTURE_EVALUATION_ISSUER,
    )
    attestation_payload = _model_bytes(attestation_model)
    attestation_artifact_ref = publish(f"evidence.attestation.{variant}.{preset}@1", attestation_payload)
    attestation_ref = ArtifactRef(
        artifact_id=uuid5(NAMESPACE_URL, f"grove:fixture:attestation:{variant}:{preset}"),
        tenant_id=FIXTURE_EVIDENCE_TENANT,
        version="1",
        content_hash=attestation_artifact_ref.content_hash,
        media_type="application/vnd.grove.evaluation-attestation+json",
        schema_ref="evidence.attestation@1",
        sensitivity="internal",
        retention_policy_ref="retention.fixture@1",
    )
    bundle_model = FixtureEvidenceBundle(
        kind="evaluation_bundle",
        preset_ref=preset_ref.ref,
        suite_ref=FIXTURE_EVALUATION_SUITE_REF,
        evaluation_run_id=run_id,
        evaluation_subject_hash=subject_hash,
        decision="passed",
        issuer=FIXTURE_EVALUATION_ISSUER,
        attestation_ref=attestation_ref,
        attestation_artifact_ref=attestation_artifact_ref,
    )
    bundle_payload = _model_bytes(bundle_model)
    bundle_ref = publish(f"evidence.bundle.{variant}.{preset}@1", bundle_payload)
    evaluation = EvaluationEvidenceRef(
        evaluation_run_id=run_id,
        tenant_id=FIXTURE_EVIDENCE_TENANT,
        evaluation_subject_hash=subject_hash,
        suite_ref=FIXTURE_EVALUATION_SUITE_REF,
        decision="passed",
        evidence_bundle_hash=bundle_ref.content_hash,
        issuer=FIXTURE_EVALUATION_ISSUER,
        attestation_ref=attestation_ref,
    )
    index_model = FixtureEvidenceIndex(
        preset_ref=preset_ref.ref,
        suite_ref=FIXTURE_EVALUATION_SUITE_REF,
        evaluation_run_id=run_id,
        evaluation_subject_hash=subject_hash,
        decision="passed",
        issuer=FIXTURE_EVALUATION_ISSUER,
        bundle_ref=bundle_ref,
        attestation_artifact_ref=attestation_artifact_ref,
        evaluation=evaluation,
    )
    index_payload = _model_bytes(index_model)
    index_ref = publish(f"evidence.index.{variant}.{preset}@1", index_payload)
    index_map[preset] = index_ref
    bundle_map[preset] = bundle_ref
    attestation_map[preset] = attestation_artifact_ref


def _build_artifacts() -> tuple[dict[str, bytes], dict[str, VersionedRef], dict[str, Any]]:
    artifacts: dict[str, bytes] = {}
    refs: dict[str, VersionedRef] = {}

    def publish(name: str, payload: bytes) -> VersionedRef:
        ref = _artifact_ref(name, payload)
        artifacts[name] = payload
        refs[name] = ref
        return ref

    input_schema = FixtureInput.model_json_schema()
    input_ref = publish("schema.fixture.input@1", _canonical_bytes(input_schema))
    output_model = FixtureOutputSchema(name="fixture.output", version="1", status="accepted")
    output_ref = publish("schema.fixture.output@1", _model_bytes(output_model))
    skill_model = FixtureSkillArtifact(kind="skill", ref="fixture.skill@1", release_ref=FIXTURE_RELEASE_REF)
    skill_ref = publish(skill_model.ref, _model_bytes(skill_model))
    agent_model = FixtureAgentArtifact(
        kind="agent", ref="fixture.agent@1", skill_ref=skill_model.ref, release_ref=FIXTURE_RELEASE_REF
    )
    agent_ref = publish(agent_model.ref, _model_bytes(agent_model))
    graph_model = FixtureGraphArtifact(
        ref="graph.fixture@1", version="1", state_schema_version="state.fixture@1", nodes=("start",)
    )
    graph_ref = publish(graph_model.ref, _model_bytes(graph_model))
    graph_binding = GraphBinding(graph=graph_ref, graph_state_schema_version=graph_model.state_schema_version)
    graph_binding_ref = publish("graph-binding.fixture@1", _model_bytes(graph_binding))
    asset_risk_graph_model = FixtureGraphArtifact(
        ref="graph.asset-risk@1",
        version="1",
        state_schema_version="state.asset-risk@1",
        nodes=(
            "validate_input",
            "retrieve_policy_knowledge",
            "read_asset_state",
            "inference",
            "typed_report",
        ),
    )
    asset_risk_graph_ref = publish(asset_risk_graph_model.ref, _model_bytes(asset_risk_graph_model))
    asset_risk_graph_binding = GraphBinding(
        graph=asset_risk_graph_ref,
        graph_state_schema_version=asset_risk_graph_model.state_schema_version,
    )
    asset_risk_graph_binding_ref = publish("graph-binding.asset-risk@1", _model_bytes(asset_risk_graph_binding))
    contracts_model = FixtureContractArtifact(ref="contracts.fixture@1", version="1", command_schema_version="start.v1")
    contracts_ref = publish(contracts_model.ref, _model_bytes(contracts_model))
    contracts_binding = ContractBinding(contracts=contracts_ref, converter_bundle=None)
    contracts_binding_ref = publish("contracts-binding.fixture@1", _model_bytes(contracts_binding))
    budget_ref = publish("budget.fixture@1", _canonical_bytes(FIXTURE_CONSTRAINTS_PAYLOAD))
    runtime_manifest = SkillRuntimeManifest(
        manifest_version="v1",
        skill_ref=skill_ref,
        input_schema_ref=input_ref,
        output_schema_ref=output_ref,
        dependencies=(),
        tool_bindings=(),
        required_capabilities=(),
    ).with_hash()
    runtime_manifest_ref = publish("manifest.fixture@1", _model_bytes(runtime_manifest))
    runtime_build = _draft_runtime_build()
    runtime_build_ref = publish("build.fixture@1", _model_bytes(runtime_build))
    policy_model = FixtureAuthorizationPolicy(
        ref="authorization.fixture@1",
        revision="1",
        ceiling_scopes=("execution.query", "execution.submit"),
        effect="accepted.start",
    )
    policy_ref = publish(policy_model.ref, _model_bytes(policy_model))
    preset_refs: dict[str, VersionedRef] = {}
    for preset in ("interactive", "read_only", "workspace_edit", "unattended"):
        preset_model = FixturePermissionPreset(
            ref=f"permission.{preset}@1", preset=preset, evaluation_effect="accepted.start"
        )
        preset_refs[preset] = publish(preset_model.ref, _model_bytes(preset_model))

    permission_envelope_hash = canonical_hash(
        {
            "ceiling": tuple(sorted(policy_model.ceiling_scopes)),
            "effect": policy_model.effect,
            "policy_revision": policy_model.revision,
            "authorization_policy": policy_ref.model_dump(mode="json"),
        }
    )
    evaluation_subject_hashes = {
        preset: _fixture_subject_hash(
            skill_ref=skill_ref,
            graph_binding=graph_binding,
            contracts_binding=contracts_binding,
            runtime_manifest_ref=runtime_manifest_ref,
            runtime_build_ref=runtime_build_ref,
            authorization_policy_ref=policy_ref,
            permission_preset_ref=preset_refs[preset],
            permission_envelope_hash=permission_envelope_hash,
            budget_ref=budget_ref,
        )
        for preset in preset_refs
    }

    asset_risk_evaluation_subject_hashes = {
        preset: _fixture_subject_hash(
            skill_ref=skill_ref,
            graph_binding=asset_risk_graph_binding,
            contracts_binding=contracts_binding,
            runtime_manifest_ref=runtime_manifest_ref,
            runtime_build_ref=runtime_build_ref,
            authorization_policy_ref=policy_ref,
            permission_preset_ref=preset_refs[preset],
            permission_envelope_hash=permission_envelope_hash,
            budget_ref=budget_ref,
        )
        for preset in preset_refs
    }
    evidence_indexes: dict[str, VersionedRef] = {}
    evidence_bundles: dict[str, VersionedRef] = {}
    evidence_attestations: dict[str, VersionedRef] = {}
    asset_risk_evidence_indexes: dict[str, VersionedRef] = {}
    asset_risk_evidence_bundles: dict[str, VersionedRef] = {}
    asset_risk_evidence_attestations: dict[str, VersionedRef] = {}
    for variant, variant_subject_hashes, variant_index_map, variant_bundle_map, variant_attestation_map in (
        (
            "fixture",
            evaluation_subject_hashes,
            evidence_indexes,
            evidence_bundles,
            evidence_attestations,
        ),
        (
            "asset_risk",
            asset_risk_evaluation_subject_hashes,
            asset_risk_evidence_indexes,
            asset_risk_evidence_bundles,
            asset_risk_evidence_attestations,
        ),
    ):
        for preset, preset_ref in preset_refs.items():
            _publish_variant_evidence(
                variant=variant,
                preset=preset,
                preset_ref=preset_ref,
                subject_hash=variant_subject_hashes[preset],
                publish=publish,
                index_map=variant_index_map,
                bundle_map=variant_bundle_map,
                attestation_map=variant_attestation_map,
            )
    metadata = {
        "input_schema": input_schema,
        "output_schema": output_model.model_dump(mode="json"),
        "budget": FIXTURE_CONSTRAINTS_PAYLOAD,
        "runtime_manifest": runtime_manifest.model_dump(mode="json"),
        "graph": graph_model.model_dump(mode="json"),
        "asset_risk_graph": asset_risk_graph_model.model_dump(mode="json"),
        "contracts": contracts_model.model_dump(mode="json"),
        "runtime_build": runtime_build.model_dump(mode="json"),
        "authorization_policy": policy_model.model_dump(mode="json"),
        "evaluation_subject_hashes": evaluation_subject_hashes,
        "artifact_refs": {
            "input_schema": input_ref.model_dump(mode="json"),
            "output_schema": output_ref.model_dump(mode="json"),
            "skill": skill_ref.model_dump(mode="json"),
            "agent": agent_ref.model_dump(mode="json"),
            "runtime_manifest": runtime_manifest_ref.model_dump(mode="json"),
            "graph": graph_ref.model_dump(mode="json"),
            "graph_binding": graph_binding_ref.model_dump(mode="json"),
            "contracts": contracts_ref.model_dump(mode="json"),
            "contracts_binding": contracts_binding_ref.model_dump(mode="json"),
            "budget": budget_ref.model_dump(mode="json"),
            "runtime_build": runtime_build_ref.model_dump(mode="json"),
            "authorization_policy": policy_ref.model_dump(mode="json"),
            **{f"permission_preset.{preset}": ref.model_dump(mode="json") for preset, ref in preset_refs.items()},
            **{f"evidence_index.{preset}": ref.model_dump(mode="json") for preset, ref in evidence_indexes.items()},
            **{f"evidence_bundle.{preset}": ref.model_dump(mode="json") for preset, ref in evidence_bundles.items()},
            **{
                f"evidence_attestation.{preset}": ref.model_dump(mode="json")
                for preset, ref in evidence_attestations.items()
            },
            "asset_risk_graph": asset_risk_graph_ref.model_dump(mode="json"),
            "asset_risk_graph_binding": asset_risk_graph_binding_ref.model_dump(mode="json"),
            **{
                f"evidence_index.asset_risk.{preset}": ref.model_dump(mode="json")
                for preset, ref in asset_risk_evidence_indexes.items()
            },
            **{
                f"evidence_bundle.asset_risk.{preset}": ref.model_dump(mode="json")
                for preset, ref in asset_risk_evidence_bundles.items()
            },
            **{
                f"evidence_attestation.asset_risk.{preset}": ref.model_dump(mode="json")
                for preset, ref in asset_risk_evidence_attestations.items()
            },
        },
    }
    metadata["asset_risk_evaluation_subject_hashes"] = dict(asset_risk_evaluation_subject_hashes)
    return artifacts, refs, metadata


FIXTURE_ARTIFACT_REGISTRY, _FIXTURE_REFS, _FIXTURE_METADATA = _build_artifacts()
FIXTURE_RELEASE_DOCUMENT = {
    "release_ref": FIXTURE_RELEASE_REF,
    "skill_ref": "fixture.skill@1",
    "agent_ref": "fixture.agent@1",
    **_FIXTURE_METADATA,
    "resolver_version": "resolver.ws2.fixture@1",
}
FIXTURE_RELEASE_ARTIFACT = _canonical_bytes(FIXTURE_RELEASE_DOCUMENT)
FIXTURE_RELEASE_HASH = _sha256(FIXTURE_RELEASE_ARTIFACT)
FIXTURE_RELEASE_REGISTRY: dict[str, bytes] = {FIXTURE_RELEASE_REF: FIXTURE_RELEASE_ARTIFACT}


def _load_artifact(ref: VersionedRef, registry: Mapping[str, bytes]) -> bytes:
    payload = registry.get(ref.ref)
    if payload is None:
        raise FixtureReleaseError(f"fixture artifact is unavailable: {ref.ref}")
    try:
        validate_artifact(payload, ref.content_hash)
    except (TypeError, ValueError) as exc:
        raise FixtureReleaseError(f"fixture artifact hash mismatch: {ref.ref}") from exc
    return payload


def _load_bundle(
    ref: str,
    *,
    registry: Mapping[str, bytes],
    artifact_registry: Mapping[str, bytes],
) -> FixtureReleaseBundle:
    payload = registry.get(ref)
    if payload is None:
        raise FixtureReleaseError(f"published release is unavailable: {ref}")
    if ref != FIXTURE_RELEASE_REF:
        raise FixtureReleaseError(f"published release is not allowed: {ref}")
    if _sha256(payload) != FIXTURE_RELEASE_HASH:
        raise FixtureReleaseError(f"published release hash mismatch: {ref}")
    try:
        release = FixtureRelease.model_validate(json.loads(payload))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise FixtureReleaseError(f"published release is invalid: {ref}") from exc
    if release.release_ref != ref or release.input_schema != FixtureInput.model_json_schema():
        raise FixtureReleaseError(f"published release contract mismatch: {ref}")

    refs = release.artifact_refs
    required = {
        "input_schema",
        "output_schema",
        "budget",
        "skill",
        "agent",
        "runtime_manifest",
        "graph",
        "graph_binding",
        "contracts",
        "contracts_binding",
        "runtime_build",
        "authorization_policy",
        "permission_preset.interactive",
        "permission_preset.read_only",
        "permission_preset.workspace_edit",
        "permission_preset.unattended",
        *(f"evidence_index.{preset}" for preset in ("interactive", "read_only", "workspace_edit", "unattended")),
        *(f"evidence_bundle.{preset}" for preset in ("interactive", "read_only", "workspace_edit", "unattended")),
        *(f"evidence_attestation.{preset}" for preset in ("interactive", "read_only", "workspace_edit", "unattended")),
        "asset_risk_graph",
        "asset_risk_graph_binding",
        *(
            f"evidence_index.asset_risk.{preset}"
            for preset in ("interactive", "read_only", "workspace_edit", "unattended")
        ),
        *(
            f"evidence_bundle.asset_risk.{preset}"
            for preset in ("interactive", "read_only", "workspace_edit", "unattended")
        ),
        *(
            f"evidence_attestation.asset_risk.{preset}"
            for preset in ("interactive", "read_only", "workspace_edit", "unattended")
        ),
    }
    if set(refs) != required:
        raise FixtureReleaseError("published release artifact closure is incomplete")
    typed_refs = {name: VersionedRef.model_validate(value) for name, value in refs.items()}
    loaded = {name: _load_artifact(reference, artifact_registry) for name, reference in typed_refs.items()}
    try:
        input_schema = json.loads(loaded["input_schema"])
        if input_schema != FixtureInput.model_json_schema():
            raise FixtureReleaseError("fixture input schema artifact mismatch")
        output_schema = FixtureOutputSchema.model_validate(json.loads(loaded["output_schema"]))
        budget = json.loads(loaded["budget"])
        skill = FixtureSkillArtifact.model_validate(json.loads(loaded["skill"]))
        agent = FixtureAgentArtifact.model_validate(json.loads(loaded["agent"]))
        graph = FixtureGraphArtifact.model_validate(json.loads(loaded["graph"]))
        graph_binding = GraphBinding.model_validate(json.loads(loaded["graph_binding"]))
        asset_risk_graph = FixtureGraphArtifact.model_validate(json.loads(loaded["asset_risk_graph"]))
        asset_risk_graph_binding = GraphBinding.model_validate(json.loads(loaded["asset_risk_graph_binding"]))
        contracts = FixtureContractArtifact.model_validate(json.loads(loaded["contracts"]))
        contracts_binding = ContractBinding.model_validate(json.loads(loaded["contracts_binding"]))
        runtime_manifest = SkillRuntimeManifest.model_validate(json.loads(loaded["runtime_manifest"]))
        runtime_build = RuntimeBuildManifest.model_validate(json.loads(loaded["runtime_build"]))
        authorization_policy = FixtureAuthorizationPolicy.model_validate(json.loads(loaded["authorization_policy"]))
        permission_presets = {
            preset: FixturePermissionPreset.model_validate(json.loads(loaded[f"permission_preset.{preset}"]))
            for preset in ("interactive", "read_only", "workspace_edit", "unattended")
        }
        evidence_bundles = {
            preset: FixtureEvidenceBundle.model_validate(json.loads(loaded[f"evidence_bundle.{preset}"]))
            for preset in ("interactive", "read_only", "workspace_edit", "unattended")
        }
        evidence_indexes = {
            preset: FixtureEvidenceIndex.model_validate(json.loads(loaded[f"evidence_index.{preset}"]))
            for preset in ("interactive", "read_only", "workspace_edit", "unattended")
        }
        evaluation_evidence = {preset: index.evaluation for preset, index in evidence_indexes.items()}
        asset_risk_evidence_indexes = {
            preset: FixtureEvidenceIndex.model_validate(json.loads(loaded[f"evidence_index.asset_risk.{preset}"]))
            for preset in ("interactive", "read_only", "workspace_edit", "unattended")
        }
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise FixtureReleaseError("typed fixture artifact validation failed") from exc
    if skill.ref != release.skill_ref or agent.ref != release.agent_ref or agent.skill_ref != skill.ref:
        raise FixtureReleaseError("fixture skill/agent binding mismatch")
    if budget != FIXTURE_CONSTRAINTS_PAYLOAD or release.budget != FIXTURE_CONSTRAINTS_PAYLOAD:
        raise FixtureReleaseError("fixture budget envelope is not the published fixed envelope")
    if graph_binding.graph != typed_refs["graph"] or contracts_binding.contracts != typed_refs["contracts"]:
        raise FixtureReleaseError("fixture graph/contract binding mismatch")
    if runtime_manifest.skill_ref != typed_refs["skill"]:
        raise FixtureReleaseError("fixture runtime manifest skill mismatch")
    if runtime_manifest.input_schema_ref != typed_refs["input_schema"]:
        raise FixtureReleaseError("fixture runtime manifest input mismatch")
    if runtime_manifest.output_schema_ref != typed_refs["output_schema"]:
        raise FixtureReleaseError("fixture runtime manifest output mismatch")
    payload_by_ref = {typed_refs[name].ref: payload for name, payload in loaded.items()}
    verify_runtime_manifest(
        runtime_manifest,
        expected_manifest_hash=runtime_manifest.manifest_hash,
        artifact_payloads=payload_by_ref,
    )
    if not verify_manifest(runtime_build):
        raise FixtureReleaseError("fixture runtime build manifest verification failed")
    if authorization_policy.ref != typed_refs["authorization_policy"].ref:
        raise FixtureReleaseError("fixture authorization policy reference mismatch")
    for preset, model in permission_presets.items():
        if model.ref != typed_refs[f"permission_preset.{preset}"].ref or model.preset != preset:
            raise FixtureReleaseError("fixture permission preset reference mismatch")
    expected_subject_hashes = {
        preset: _fixture_subject_hash(
            skill_ref=typed_refs["skill"],
            graph_binding=graph_binding,
            contracts_binding=contracts_binding,
            runtime_manifest_ref=typed_refs["runtime_manifest"],
            runtime_build_ref=typed_refs["runtime_build"],
            authorization_policy_ref=typed_refs["authorization_policy"],
            permission_preset_ref=typed_refs[f"permission_preset.{preset}"],
            permission_envelope_hash=canonical_hash(
                {
                    "ceiling": tuple(sorted(authorization_policy.ceiling_scopes)),
                    "effect": authorization_policy.effect,
                    "policy_revision": authorization_policy.revision,
                    "authorization_policy": typed_refs["authorization_policy"].model_dump(mode="json"),
                }
            ),
            budget_ref=typed_refs["budget"],
        )
        for preset in permission_presets
    }
    if release.evaluation_subject_hashes != expected_subject_hashes:
        raise FixtureReleaseError("fixture evaluation subject registry mismatch")
    for preset, index in evidence_indexes.items():
        bundle = evidence_bundles[preset]
        evidence = index.evaluation
        attestation_payload = loaded[f"evidence_attestation.{preset}"]
        try:
            attestation = FixtureEvidenceAttestation.model_validate(json.loads(attestation_payload))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise FixtureReleaseError("fixture evidence attestation is invalid") from exc
        expected_bundle_ref = typed_refs[f"evidence_bundle.{preset}"]
        expected_attestation_ref = typed_refs[f"evidence_attestation.{preset}"]
        if (
            index.preset_ref != typed_refs[f"permission_preset.{preset}"].ref
            or index.suite_ref != FIXTURE_EVALUATION_SUITE_REF
            or index.evaluation_run_id != evidence.evaluation_run_id
            or index.evaluation_subject_hash != expected_subject_hashes[preset]
            or index.decision != "passed"
            or index.issuer not in FIXTURE_EVALUATION_ISSUER_ALLOWLIST
            or index.bundle_ref != expected_bundle_ref
            or index.attestation_artifact_ref != expected_attestation_ref
            or evidence.tenant_id != FIXTURE_EVIDENCE_TENANT
            or evidence.evaluation_subject_hash != expected_subject_hashes[preset]
            or evidence.suite_ref != FIXTURE_EVALUATION_SUITE_REF
            or evidence.decision != "passed"
            or evidence.issuer not in FIXTURE_EVALUATION_ISSUER_ALLOWLIST
            or evidence.evidence_bundle_hash != expected_bundle_ref.content_hash
            or evidence.attestation_ref.content_hash != expected_attestation_ref.content_hash
            or evidence.attestation_ref.tenant_id != FIXTURE_EVIDENCE_TENANT
            or bundle.preset_ref != index.preset_ref
            or bundle.suite_ref != index.suite_ref
            or bundle.evaluation_run_id != index.evaluation_run_id
            or bundle.evaluation_subject_hash != index.evaluation_subject_hash
            or bundle.decision != "passed"
            or bundle.issuer not in FIXTURE_EVALUATION_ISSUER_ALLOWLIST
            or bundle.attestation_ref != evidence.attestation_ref
            or bundle.attestation_artifact_ref != expected_attestation_ref
            or attestation.preset_ref != index.preset_ref
            or attestation.suite_ref != index.suite_ref
            or attestation.evaluation_run_id != index.evaluation_run_id
            or attestation.evaluation_subject_hash != index.evaluation_subject_hash
            or attestation.decision != "passed"
            or attestation.issuer not in FIXTURE_EVALUATION_ISSUER_ALLOWLIST
        ):
            raise FixtureReleaseError(f"fixture evidence closure mismatch: {preset}")
    artifact_bytes = {
        **loaded,
        **{typed_refs[name].ref: payload for name, payload in loaded.items()},
    }
    return FixtureReleaseBundle(
        release=release,
        input_schema=input_schema,
        output_schema=output_schema,
        budget=budget,
        budget_ref=typed_refs["budget"],
        skill=skill,
        agent=agent,
        runtime_manifest=runtime_manifest,
        runtime_manifest_ref=typed_refs["runtime_manifest"],
        graph=graph,
        graph_ref=typed_refs["graph"],
        graph_binding=graph_binding,
        asset_risk_graph=asset_risk_graph,
        asset_risk_graph_ref=typed_refs["asset_risk_graph"],
        asset_risk_graph_binding=asset_risk_graph_binding,
        contracts=contracts,
        contracts_ref=typed_refs["contracts"],
        contracts_binding=contracts_binding,
        runtime_build=runtime_build,
        runtime_build_ref=typed_refs["runtime_build"],
        authorization_policy=authorization_policy,
        authorization_policy_ref=typed_refs["authorization_policy"],
        permission_presets=permission_presets,
        evaluation_subject_hashes=expected_subject_hashes,
        evidence_indexes=evidence_indexes,
        asset_risk_evaluation_subject_hashes=release.asset_risk_evaluation_subject_hashes,
        asset_risk_evidence_indexes=asset_risk_evidence_indexes,
        evaluation_evidence=evaluation_evidence,
        artifact_bytes=artifact_bytes,
    )


def load_fixture_release_bundle(
    ref: str = FIXTURE_RELEASE_REF,
    *,
    registry: Mapping[str, bytes] | None = None,
    artifact_registry: Mapping[str, bytes] | None = None,
) -> FixtureReleaseBundle:
    return _load_bundle(
        ref,
        registry=FIXTURE_RELEASE_REGISTRY if registry is None else registry,
        artifact_registry=FIXTURE_ARTIFACT_REGISTRY if artifact_registry is None else artifact_registry,
    )


def load_fixture_release(
    ref: str = FIXTURE_RELEASE_REF,
    *,
    registry: Mapping[str, bytes] | None = None,
    artifact_registry: Mapping[str, bytes] | None = None,
) -> FixtureRelease:
    """Load one exact release and its entire typed artifact closure."""

    return load_fixture_release_bundle(
        ref,
        registry=registry,
        artifact_registry=artifact_registry,
    ).release


def build_fixture_evidence(
    *,
    release_ref: str,
    preset_ref: str,
    evaluation_subject_hash: str,
) -> tuple[VersionedRef, bytes]:
    """Load one exact pre-published evidence index; never issue online evidence."""

    bundle = load_fixture_release_bundle(release_ref)
    preset_name = preset_ref.removeprefix("permission.").removesuffix("@1")
    expected_subject = bundle.evaluation_subject_hashes.get(preset_name)
    if expected_subject != evaluation_subject_hash:
        raise FixtureReleaseError("evaluation subject has no pre-published passed evidence")
    evidence_ref = bundle.release.artifact_refs.get(f"evidence_index.{preset_name}")
    if evidence_ref is None:
        raise FixtureReleaseError("published evidence index is unavailable")
    return evidence_ref, bundle.artifact_bytes[evidence_ref.ref]


def load_fixture_evidence(
    payload: bytes,
    *,
    expected_ref: VersionedRef,
    expected_subject_hash: str,
    expected_preset_ref: str,
    release_ref: str,
    artifact_registry: Mapping[str, bytes] | None = None,
) -> FixtureEvidenceIndex:
    """Verify exact registry bytes and every immutable evidence binding."""

    artifact_registry = FIXTURE_ARTIFACT_REGISTRY if artifact_registry is None else artifact_registry
    bundle = load_fixture_release_bundle(release_ref, artifact_registry=artifact_registry)
    preset_name = expected_preset_ref.removeprefix("permission.").removesuffix("@1")
    if expected_ref.ref.startswith("evidence.index.asset_risk."):
        expected_subject = bundle.asset_risk_evaluation_subject_hashes.get(preset_name)
        evidence = bundle.asset_risk_evidence_indexes.get(preset_name)
    else:
        expected_subject = bundle.evaluation_subject_hashes.get(preset_name)
        evidence = bundle.evidence_indexes.get(preset_name)
    registry_payload = artifact_registry.get(expected_ref.ref)
    if evidence is None or expected_subject != expected_subject_hash or registry_payload is None:
        raise FixtureReleaseError("fixture evidence subject is not pre-published")
    index_key = (
        f"evidence_index.asset_risk.{preset_name}"
        if expected_ref.ref.startswith("evidence.index.asset_risk.")
        else f"evidence_index.{preset_name}"
    )
    if expected_ref != bundle.release.artifact_refs.get(index_key):
        raise FixtureReleaseError("fixture evidence reference is not the published index")
    if payload != registry_payload:
        raise FixtureReleaseError("fixture evidence registry bytes mismatch")
    try:
        validate_artifact(payload, expected_ref.content_hash)
        parsed = FixtureEvidenceIndex.model_validate(json.loads(payload))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise FixtureReleaseError("fixture evidence index is invalid") from exc
    if parsed != evidence:
        raise FixtureReleaseError("fixture evidence index differs from published closure")
    return parsed


__all__ = [
    "FIXTURE_ARTIFACT_REGISTRY",
    "FIXTURE_CONSTRAINTS_PAYLOAD",
    "FIXTURE_EVALUATION_ISSUER",
    "FIXTURE_EVALUATION_ISSUER_ALLOWLIST",
    "FIXTURE_EVALUATION_SUITE_REF",
    "FIXTURE_RELEASE_ARTIFACT",
    "FIXTURE_RELEASE_HASH",
    "FIXTURE_RELEASE_REF",
    "FIXTURE_RELEASE_REGISTRY",
    "FixtureAgentArtifact",
    "FixtureAuthorizationPolicy",
    "FixtureContractArtifact",
    "FixtureEvidenceAttestation",
    "FixtureEvidenceBundle",
    "FixtureEvidenceIndex",
    "FixtureGraphArtifact",
    "FixtureInput",
    "FixtureOutputSchema",
    "FixturePermissionPreset",
    "FixtureRelease",
    "FixtureReleaseBundle",
    "FixtureReleaseError",
    "FixtureSkillArtifact",
    "build_fixture_evidence",
    "load_fixture_evidence",
    "load_fixture_release",
    "load_fixture_release_bundle",
]
