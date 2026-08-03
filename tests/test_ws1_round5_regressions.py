"""Round-five regressions for external Manifest binding and reflection closure."""

from __future__ import annotations

import ast
import hashlib
import inspect
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from app.contracts import (
    ContractMeta,
    FinalAnswer,
    FinalAnswerPayload,
    KnowledgeProposalPayload,
    TypedSchemaRegistry,
    VersionedRef,
    enrich_decision,
    enrich_knowledge_decision,
    parse_canonical_decision,
    parse_inference_decision,
    parse_knowledge_decision,
    parse_knowledge_proposal,
    read_contract,
)
from app.contracts.canonical import ContractReaderRegistry, invoke_runtime_manifest_verifier
from app.skill_abi import ArtifactHashMismatchError, SkillRuntimeManifest
from app.skill_abi.runtime import _is_exact_generic_origin
from pydantic import BaseModel, TypeAdapter
from scripts.check_contract_dependencies import find_violations


class StrictOutput(BaseModel):
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


def _payload() -> FinalAnswerPayload[StrictOutput]:
    return FinalAnswerPayload[StrictOutput](
        kind="final_answer",
        output=StrictOutput(answer="ok"),
        rationale_summary="r",
        confidence=0.5,
    )


def _registry(manifest: SkillRuntimeManifest) -> TypedSchemaRegistry:
    registry = TypedSchemaRegistry()
    registry.register(manifest.output_schema_ref, StrictOutput, role="output")
    return registry


def _knowledge_payload() -> dict[str, object]:
    return {
        "kind": "knowledge_proposal",
        "query": "q",
        "knowledge_refs": (),
        "filter": {},
        "rationale_summary": "r",
        "confidence": 0.5,
    }


def test_schema_bearing_public_signatures_require_external_hash() -> None:
    for function in (parse_inference_decision, parse_canonical_decision, enrich_decision, read_contract):
        parameter = inspect.signature(function).parameters["expected_manifest_hash"]
        assert parameter.default is inspect.Parameter.empty


def test_knowledge_entrypoints_are_schema_free_and_closed() -> None:
    payload = parse_knowledge_proposal(_knowledge_payload())
    assert isinstance(payload, KnowledgeProposalPayload)
    decision = enrich_knowledge_decision(payload, meta=_meta(), run_id=uuid4(), decision_id=uuid4())
    assert parse_knowledge_decision(decision) == decision
    for executable in (
        _payload().model_dump(mode="python"),
        {"kind": "tool_proposal"},
        {"kind": "action_proposal"},
        {"kind": "delegate_proposal"},
    ):
        with pytest.raises((TypeError, ValueError)):
            parse_knowledge_proposal(executable)
        with pytest.raises((TypeError, ValueError)):
            enrich_knowledge_decision(executable, meta=_meta(), run_id=uuid4(), decision_id=uuid4())


def test_knowledge_entrypoints_revalidate_exact_models_and_reject_bad_inputs() -> None:
    payload_model = KnowledgeProposalPayload.model_validate(_knowledge_payload())
    assert parse_knowledge_proposal(payload_model) == payload_model
    malformed_payload = payload_model.model_construct(kind="tool_proposal")
    with pytest.raises(ValueError):
        parse_knowledge_proposal(malformed_payload)
    with pytest.raises(TypeError):
        parse_knowledge_proposal(42)

    decision = enrich_knowledge_decision(payload_model, meta=_meta(), run_id=uuid4(), decision_id=uuid4())
    assert parse_knowledge_decision(decision.model_dump(mode="python")) == decision
    malformed_decision = decision.model_construct(kind="tool_proposal")
    with pytest.raises(ValueError):
        parse_knowledge_decision(malformed_decision)
    with pytest.raises(TypeError):
        parse_knowledge_decision(42)


def test_schema_seams_reject_missing_external_manifest_hash() -> None:
    manifest = _manifest()
    registry = _registry(manifest)
    payload = _payload()
    raw = payload.model_dump(mode="python")
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
        cast(Any, parse_inference_decision)(raw, manifest=manifest, schema_registry=registry)
    with pytest.raises((TypeError, ValueError)):
        cast(Any, parse_canonical_decision)(decision, manifest=manifest, schema_registry=registry)
    with pytest.raises((TypeError, ValueError)):
        cast(Any, enrich_decision)(
            payload,
            meta=_meta(),
            run_id=uuid4(),
            decision_id=uuid4(),
            manifest=manifest,
            schema_registry=registry,
        )
    with pytest.raises((TypeError, ValueError)):
        cast(Any, read_contract)("inference.payload", raw, version="v1", manifest=manifest, schema_registry=registry)
    with pytest.raises((TypeError, ValueError)):
        cast(Any, invoke_runtime_manifest_verifier)(manifest)


def test_rehashed_manifest_without_external_expected_hash_is_not_trusted() -> None:
    original = _manifest()
    registry = _registry(original)
    rehashed = original.model_copy(update={"required_capabilities": ("graph",)}).with_hash()
    raw = _payload().model_dump(mode="python")
    assert rehashed.manifest_hash != original.manifest_hash

    with pytest.raises((TypeError, ValueError, ArtifactHashMismatchError)):
        cast(Any, parse_inference_decision)(raw, manifest=rehashed, schema_registry=registry)
    with pytest.raises(ArtifactHashMismatchError):
        invoke_runtime_manifest_verifier(rehashed, expected_manifest_hash=original.manifest_hash)


@pytest.mark.parametrize("expected", [object(), "bad", "0" * 64])
def test_external_manifest_hash_binding_rejects_invalid_values(expected: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        invoke_runtime_manifest_verifier(_manifest(), expected_manifest_hash=cast(Any, expected))


@pytest.mark.parametrize(
    "source",
    [
        "import builtins as b\nvalue = b.__getattribute__('name')\n",
        "from builtins import eval as load\nvalue = load('1')\n",
        "import importlib as loader\nvalue = loader.import_module('x')\n",
        "import operator\nvalue = operator.getitem({'x': 1}, 'x')\n",
        "from operator import attrgetter as pick\nvalue = pick('x')\n",
        "value = vars(__builtins__).get('eval')\n",
        "value = __builtins__.__getattribute__('eval')\n",
        "value = {'__import__': 1}[name]\n",
        "value = {'__im' + 'port__': 1}[name]\n",
    ],
)
def test_dependency_checker_rejects_closed_reflection_variants(tmp_path: Path, source: str) -> None:
    spine = tmp_path / "app" / "contracts"
    spine.mkdir(parents=True)
    (spine / "probe.py").write_text(source, encoding="utf-8")
    assert find_violations(tmp_path)


def test_dependency_checker_recurses_into_nested_ast_nodes(tmp_path: Path) -> None:
    spine = tmp_path / "app" / "skill_abi"
    spine.mkdir(parents=True)
    tree = ast.unparse(ast.parse("def f(value, name):\n    return (lambda fn: fn(value))(getattr(value, name))\n"))
    (spine / "nested.py").write_text(tree, encoding="utf-8")
    assert find_violations(tmp_path)


def test_closed_reader_and_generic_origin_do_not_use_reflection_fallbacks() -> None:
    reader = ContractReaderRegistry()
    reference = _ref("probe@1", b"probe")
    reader.register("probe", "v1", TypeAdapter(VersionedRef))
    assert reader.read("probe", "v1", reference.model_dump(mode="python")) == reference

    class CustomReader:
        def validate_python(self, value: object) -> VersionedRef:
            return VersionedRef.model_validate(value)

    reader.register("probe", "v2", CustomReader())
    assert reader.read("probe", "v2", reference.model_dump(mode="python")) == reference
    assert _is_exact_generic_origin(object(), VersionedRef) is False
