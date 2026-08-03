"""Round-four regressions for WS-1 trust-boundary hardening."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import Enum, StrEnum
from pathlib import Path
from typing import Annotated, Any, cast
from uuid import UUID, uuid4

import pytest
from app.build.manifest import ImageInfo, _validate_evidence_reference
from app.build.manifest import canonical_bytes as build_canonical_bytes
from app.contracts import (
    ContractMeta,
    FinalAnswer,
    FinalAnswerPayload,
    TypedSchemaRegistry,
    VersionedRef,
    enrich_decision,
    parse_canonical_decision,
    parse_inference_decision,
)
from app.contracts.canonical import (
    _assert_safe_annotation,
    _assert_strict_model,
    _reject_untrusted_fields,
    _schema_has_mapping_field,
    canonical_bytes,
)
from app.skill_abi import (
    ABIConversionError,
    ArtifactHashMismatchError,
    DependencyNode,
    MissingArtifactError,
    SkillExecutionSpec,
    SkillRuntimeManifest,
    build_skill_execution_spec,
    compute_evaluation_subject_hash,
    compute_manifest_hash,
    compute_skill_spec_hash,
    issue_dependency_closure_proof,
    read_skill_execution_spec,
    resolve_dependency_closure,
    verify_runtime_manifest,
    verify_skill_execution_spec,
)
from pydantic import BaseModel
from scripts.check_contract_dependencies import find_violations


class StrictOutput(BaseModel):
    model_config = {"extra": "forbid", "frozen": True}
    answer: str


class OtherOutput(BaseModel):
    model_config = {"extra": "forbid", "frozen": True}
    answer: str


def _ref(name: str, payload: bytes) -> VersionedRef:
    return VersionedRef(ref=name, version="1", content_hash=hashlib.sha256(payload).hexdigest())


def _manifest() -> SkillRuntimeManifest:
    return SkillRuntimeManifest(
        manifest_version="v1",
        skill_ref=_ref("skill.root@1", b"root"),
        input_schema_ref=_ref("schema.input@1", b"input"),
        output_schema_ref=_ref("schema.output@1", b"output"),
        dependencies=(),
        tool_bindings=(),
        required_capabilities=(),
    ).with_hash()


def _meta() -> ContractMeta:
    return ContractMeta(
        contract_name="canonical.inference",
        contract_version="v1",
        message_id=uuid4(),
        tenant_id="tenant-a",
        correlation_id="corr-a",
    )


def _spec() -> SkillExecutionSpec:
    return build_skill_execution_spec(
        abi_version="v1",
        spec_id=UUID("00000000-0000-0000-0000-000000000010"),
        issuer="resolver@1",
        tenant_id="tenant-a",
        run_mode="live",
        skill=VersionedRef(ref="skill.root@1", version="1", content_hash="1" * 64),
        graph={
            "graph": VersionedRef(ref="graph.root@1", version="1", content_hash="4" * 64),
            "graph_state_schema_version": "state@1",
        },
        contracts={
            "contracts": VersionedRef(ref="contracts.core@1", version="1", content_hash="5" * 64),
            "converter_bundle": None,
        },
        runtime_manifest=VersionedRef(ref="manifest.root@1", version="1", content_hash="6" * 64),
        runtime_build=VersionedRef(ref="build.root@1", version="1", content_hash="7" * 64),
        permission={
            "run_authority_ref": "authority@1",
            "run_authority_hash": "8" * 64,
            "authorization_policy": VersionedRef(ref="auth.policy@1", version="1", content_hash="9" * 64),
            "permission_preset": VersionedRef(ref="permission.interactive@1", version="1", content_hash="a" * 64),
            "permission_envelope_hash": "b" * 64,
            "effective_scopes": ("domain:read",),
        },
        required_capabilities=("graph", "knowledge"),
        budget={
            "evaluation_envelope": VersionedRef(ref="budget.eval@1", version="1", content_hash="c" * 64),
            "effective_budget": VersionedRef(ref="budget.eval@1", version="1", content_hash="c" * 64),
        },
        policy_refs=(
            {"kind": "model", "policy": VersionedRef(ref="model.policy@1", version="1", content_hash="d" * 64)},
            {"kind": "prompt", "policy": VersionedRef(ref="prompt.policy@1", version="1", content_hash="e" * 64)},
        ),
        evaluation_evidence_set=VersionedRef(ref="evidence.set@1", version="1", content_hash="f" * 64),
        resolver_version="resolver@1",
        resolved_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_schema_bearing_payload_and_decision_require_manifest_registry() -> None:
    raw = {
        "kind": "final_answer",
        "output": {"answer": "ok"},
        "rationale_summary": "r",
        "confidence": 0.5,
    }
    with pytest.raises((TypeError, ValueError)):
        parse_inference_decision(raw, output_type=StrictOutput, expected_manifest_hash="a" * 64)
    payload = FinalAnswerPayload[StrictOutput](
        kind="final_answer", output=StrictOutput(answer="ok"), rationale_summary="r", confidence=0.5
    )
    with pytest.raises((TypeError, ValueError)):
        enrich_decision(
            payload,
            meta=_meta(),
            run_id=uuid4(),
            decision_id=uuid4(),
            expected_manifest_hash="a" * 64,
        )
    decision = FinalAnswer[StrictOutput](
        kind="final_answer",
        meta=ContractMeta(
            contract_name="canonical.decision",
            contract_version="v1",
            message_id=uuid4(),
            tenant_id="tenant-a",
            correlation_id="corr-a",
        ),
        run_id=uuid4(),
        decision_id=uuid4(),
        output=StrictOutput(answer="ok"),
        artifact_refs=(),
        rationale_summary="r",
        confidence=0.5,
    )
    with pytest.raises((TypeError, ValueError)):
        parse_canonical_decision(decision, expected_manifest_hash="a" * 64)


def test_manifest_subclass_cannot_reach_authoritative_verifier() -> None:
    manifest = _manifest()

    class ForgedManifest(SkillRuntimeManifest):
        def with_hash(self) -> SkillRuntimeManifest:
            return self.model_copy(update={"manifest_hash": self.manifest_hash})

        def find_tool_binding(self, reference: str | VersionedRef) -> Any:
            raise AssertionError("overridden binding lookup must not run")

    forged = ForgedManifest.model_validate(manifest.model_dump(mode="python", exclude_unset=False))
    with pytest.raises((TypeError, ValueError)):
        verify_runtime_manifest(forged, expected_manifest_hash=manifest.manifest_hash)
    with pytest.raises(TypeError):
        forged.verify(expected_manifest_hash=manifest.manifest_hash)
    with pytest.raises(TypeError):
        compute_manifest_hash(forged)


def test_schema_seam_rejects_registry_subclasses_and_wrong_expected_hash() -> None:
    manifest = _manifest()
    registry = TypedSchemaRegistry()
    registry.register(manifest.output_schema_ref, StrictOutput, role="output")
    raw = {
        "kind": "final_answer",
        "output": {"answer": "ok"},
        "rationale_summary": "r",
        "confidence": 0.5,
    }

    class ForgedRegistry(TypedSchemaRegistry):
        pass

    with pytest.raises(TypeError):
        parse_inference_decision(
            raw,
            manifest=manifest,
            schema_registry=ForgedRegistry(),
            expected_manifest_hash=manifest.manifest_hash,
        )
    with pytest.raises(ArtifactHashMismatchError):
        parse_inference_decision(
            raw,
            manifest=manifest,
            schema_registry=registry,
            expected_manifest_hash="a" * 64,
        )

    class ForgedPayload(FinalAnswerPayload[StrictOutput]):
        pass

    forged = ForgedPayload(kind="final_answer", output=StrictOutput(answer="ok"), rationale_summary="r", confidence=0.5)
    with pytest.raises(TypeError):
        parse_inference_decision(
            forged,
            manifest=manifest,
            schema_registry=registry,
            expected_manifest_hash=manifest.manifest_hash,
        )
    with pytest.raises(TypeError):
        ForgedRegistry().register(manifest.output_schema_ref, StrictOutput, role="output")

    decision = FinalAnswer[StrictOutput].model_construct(
        kind="final_answer",
        meta=ContractMeta(
            contract_name="canonical.decision",
            contract_version="v1",
            message_id=uuid4(),
            tenant_id="tenant-a",
            correlation_id="corr-a",
        ),
        run_id=uuid4(),
        decision_id=uuid4(),
        output=StrictOutput(answer="ok"),
        artifact_refs=(),
        rationale_summary="r",
        confidence=0.5,
    )
    wrong_registry = TypedSchemaRegistry()
    wrong_registry.register(manifest.output_schema_ref, OtherOutput, role="output")
    with pytest.raises(ValueError):
        parse_canonical_decision(
            decision,
            manifest=manifest,
            schema_registry=wrong_registry,
            expected_manifest_hash=manifest.manifest_hash,
        )


def test_spec_verifier_and_hashers_reject_subclasses() -> None:
    spec = _spec()

    class ForgedSpec(SkillExecutionSpec):
        pass

    forged = ForgedSpec.model_validate(spec.model_dump(mode="python", exclude_unset=False))
    with pytest.raises(TypeError):
        verify_skill_execution_spec(forged)
    with pytest.raises(TypeError):
        compute_skill_spec_hash(forged)
    with pytest.raises(TypeError):
        compute_evaluation_subject_hash(forged)


def test_dependency_issuer_rejects_unreachable_node() -> None:
    root = DependencyNode(_ref("skill.root@1", b"root"), artifact_payload=b"root")
    unreachable = DependencyNode(_ref("skill.evil@1", b"evil"), artifact_payload=b"evil")
    with pytest.raises(ValueError):
        issue_dependency_closure_proof(root, (unreachable,), resolver_version=_ref("resolver@1", b"resolver"))
    with pytest.raises(MissingArtifactError):
        issue_dependency_closure_proof(DependencyNode(root.skill), (), resolver_version=_ref("resolver@1", b"resolver"))
    with pytest.raises(ArtifactHashMismatchError):
        issue_dependency_closure_proof(
            DependencyNode(root.skill, artifact_payload=b"tampered"),
            (),
            resolver_version=_ref("resolver@1", b"resolver"),
        )
    with pytest.raises(MissingArtifactError):
        resolve_dependency_closure(
            root,
            (),
            content_loader=lambda _: (_ for _ in ()).throw(KeyError("missing")),
        )


def test_v2_reader_rechecks_both_content_hashes() -> None:
    # Reuse the complete v1 fixture only to avoid constructing a partial ABI
    # record.  Changing the explicit ABI version requires a new spec hash.
    source = _spec()
    raw = source.model_dump(mode="python", exclude_unset=True)
    raw["abi_version"] = "v2"
    candidate = SkillExecutionSpec.model_validate(raw)
    raw["skill_spec_hash"] = compute_skill_spec_hash(candidate)
    assert read_skill_execution_spec(raw).abi_version == "v2"
    with pytest.raises(ABIConversionError):
        read_skill_execution_spec({**raw, "skill_spec_hash": "a" * 64})
    with pytest.raises(ABIConversionError):
        read_skill_execution_spec({**raw, "evaluation_subject_hash": "b" * 64})


def test_canonical_serializer_and_annotation_guards_cover_nested_rejections() -> None:
    class Ordinary(Enum):
        VALUE = "value"

    class WireValue(StrEnum):
        VALUE = "value"

    with pytest.raises(TypeError):
        canonical_bytes({1: "not a string key"})
    with pytest.raises(TypeError):
        canonical_bytes(Ordinary.VALUE)
    assert canonical_bytes(WireValue.VALUE)
    with pytest.raises(ValueError):
        _assert_safe_annotation(tuple, set())
    with pytest.raises(ValueError):
        _assert_safe_annotation(tuple[()], set())
    _assert_safe_annotation(tuple[int, ...], set())
    _assert_safe_annotation(Annotated[list[str], "metadata"], set())
    with pytest.raises(ValueError):
        _assert_strict_model(BaseModel)
    with pytest.raises(ValueError):
        _reject_untrusted_fields([{"tenant_id": "forged"}])
    _reject_untrusted_fields([{"safe": "value"}])

    class MappingField(BaseModel):
        model_config = {"extra": "forbid", "frozen": True}
        values: Annotated[dict[str, str], "metadata"]

    assert _schema_has_mapping_field(MappingField)


def test_runtime_build_image_reference_is_fail_closed() -> None:
    assert ImageInfo(application="not_built", postgres="not_resolved")
    with pytest.raises(ValueError):
        ImageInfo(application="local:tag", postgres="not_resolved")
    with pytest.raises(ValueError):
        _validate_evidence_reference("not_generated", "a" * 64, "SBOM", "runtime-sbom.cdx.json")
    with pytest.raises(ValueError):
        _validate_evidence_reference("ci-evidence/sha256/not-a-digest/file", "bad", "SBOM", "runtime-sbom.cdx.json")
    digest = "a" * 64
    with pytest.raises(ValueError):
        _validate_evidence_reference(f"ci-evidence/sha256/{digest}/wrong.json", digest, "SBOM", "runtime-sbom.cdx.json")
    with pytest.raises(TypeError):
        build_canonical_bytes(cast(Any, object()))


@pytest.mark.parametrize(
    "source",
    [
        "import builtins\n",
        "from builtins import __import__ as load\n",
        "def f(b, name):\n    return getattr(b, name)\n",
        "def f(b, name):\n    return b.__dict__[name]\n",
        "def f(b, name):\n    return b[name]\n",
        "def f(name):\n    return {'__import__': load}[name]\n",
    ],
)
def test_dependency_checker_rejects_builtin_dynamic_entrypoints(tmp_path: Path, source: str) -> None:
    spine = tmp_path / "app" / "contracts"
    spine.mkdir(parents=True)
    (spine / "probe.py").write_text(source)
    assert find_violations(tmp_path)
