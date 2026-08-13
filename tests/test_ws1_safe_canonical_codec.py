"""WS-1 safe canonical codec contract tests.

These tests exercise the public raw-data seam.  In particular, invalid raw
values must be rejected before the schema registry or a Pydantic validator is
allowed to observe them.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, time
from typing import Annotated, Any, ClassVar, cast
from uuid import UUID

import pytest
from app.contracts.canonical import (
    CanonicalReadLimits,
    SafeCanonicalCodec,
    TypedSchemaRegistry,
    VersionedRef,
    read_canonical_inference_request,
    validate_canonical_inference_request,
)
from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag, field_validator
from pydantic._internal._model_construction import ModelMetaclass


def _ref(name: str = "schema.input@1") -> VersionedRef:
    return VersionedRef(ref=name, version="v1", content_hash="a" * 64)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    value: str
    nested: list[dict[str, int]] = []

    calls: ClassVar[int] = 0

    @field_validator("value")
    @classmethod
    def count_validation(cls, value: str) -> str:
        cls.calls += 1
        return value


class NestedInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    value: str

    calls: ClassVar[int] = 0

    @field_validator("value")
    @classmethod
    def count_validation(cls, value: str) -> str:
        cls.calls += 1
        return value


class InputWithNested(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    value: NestedInput


class DatetimeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    at: datetime

    calls: ClassVar[int] = 0

    @field_validator("at")
    @classmethod
    def count_validation(cls, value: datetime) -> datetime:
        cls.calls += 1
        return value


class UUIDOrStringInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    value: UUID | str


class DatetimeOrStringInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    value: datetime | str


class ConstrainedInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: Annotated[str, Field(min_length=2, max_length=4, pattern=r"^[a-z]+$")]
    count: Annotated[int, Field(ge=1, le=3)]
    items: Annotated[list[int], Field(min_length=1, max_length=2)]

    calls: ClassVar[int] = 0

    @field_validator("name", "count", "items", mode="before")
    @classmethod
    def count_validation(cls, value: Any) -> Any:
        cls.calls += 1
        return value


def _codec(model: type[BaseModel] = Input, reference: VersionedRef | None = None) -> SafeCanonicalCodec:
    registry = TypedSchemaRegistry()
    registry.register(reference or _ref(), model, role="input")
    return SafeCanonicalCodec(registry)


def test_read_dict_validates_raw_json_types_before_schema() -> None:
    Input.calls = 0
    codec = _codec()

    parsed = codec.read_dict({"value": "ok", "nested": [{"n": 1}]}, schema_ref=_ref(), role="input")

    assert isinstance(parsed, Input)
    assert parsed.value == "ok"
    assert Input.calls == 1

    for bad in (
        {"value": b"bytes"},
        {"value": datetime(2026, 1, 1)},
        {"value": date(2026, 1, 1)},
        {"value": time(1, 2)},
        {"value": ("tuple",)},
        {"value": object()},
    ):
        with pytest.raises(ValueError):
            codec.read_dict(cast(Any, bad), schema_ref=_ref(), role="input")
    assert Input.calls == 1


def test_read_dict_rejects_model_copy_extra_and_container_subclasses_before_schema() -> None:
    Input.calls = 0
    codec = _codec()

    forged = Input.model_construct(value="forged").model_copy(update={"extra": "injected"})
    with pytest.raises(TypeError):
        codec.read_dict(cast(Any, forged), schema_ref=_ref(), role="input")

    class DictSubclass(dict[str, Any]):
        pass

    class ListSubclass(list[Any]):
        pass

    with pytest.raises(TypeError):
        codec.read_dict(cast(Any, DictSubclass(value="x")), schema_ref=_ref(), role="input")
    with pytest.raises(ValueError):
        codec.read_dict({"value": "x", "nested": ListSubclass([{"n": 1}])}, schema_ref=_ref(), role="input")
    assert Input.calls == 0


def test_read_dict_rejects_top_level_and_nested_extra_before_any_schema_validator() -> None:
    NestedInput.calls = 0
    reference = _ref("schema.nested@1")
    codec = _codec(InputWithNested, reference)

    with pytest.raises(ValueError, match="extra"):
        codec.read_dict(
            {"value": "x", "unexpected": True},
            schema_ref=reference,
            role="input",
        )
    with pytest.raises(ValueError, match="extra"):
        codec.read_dict(
            {"value": {"value": "x", "unexpected": True}},
            schema_ref=reference,
            role="input",
        )
    assert NestedInput.calls == 0


def test_read_dict_rejects_missing_required_field_before_any_schema_validator() -> None:
    Input.calls = 0
    codec = _codec()

    with pytest.raises(ValueError, match="missing"):
        codec.read_dict({"nested": []}, schema_ref=_ref(), role="input")
    assert Input.calls == 0


def test_forged_limits_are_revalidated_without_executing_comparison_code() -> None:
    class Evil:
        calls = 0

        def __lt__(self, other: object) -> bool:
            type(self).calls += 1
            return False

    limits = CanonicalReadLimits()
    object.__setattr__(limits, "max_nodes", Evil())

    with pytest.raises(TypeError, match="max_nodes"):
        _codec().read_dict({"value": "x", "nested": []}, schema_ref=_ref(), role="input", limits=limits)
    assert Evil.calls == 0


def test_invalid_unicode_and_naive_datetime_fail_before_schema_validators() -> None:
    Input.calls = 0
    with pytest.raises(ValueError, match="UTF-8"):
        _codec().read_dict({"value": "\ud800", "nested": []}, schema_ref=_ref(), role="input")
    assert Input.calls == 0

    DatetimeInput.calls = 0
    reference = _ref("schema.datetime@1")
    codec = _codec(DatetimeInput, reference)
    with pytest.raises(ValueError, match="timezone-aware"):
        codec.read_dict({"at": "2026-01-01T00:00:00"}, schema_ref=reference, role="input")
    assert DatetimeInput.calls == 0

    parsed = codec.read_dict({"at": "2026-01-01T08:00:00+08:00"}, schema_ref=reference, role="input")
    assert parsed.at == datetime(2026, 1, 1, tzinfo=UTC)


def test_union_prefers_exact_raw_string_branch_independent_of_declaration_order() -> None:
    uuid_reference = _ref("schema.uuid-or-string@1")
    uuid_codec = _codec(UUIDOrStringInput, uuid_reference)
    uuid_text = "00000000-0000-0000-0000-000000000001"
    assert uuid_codec.read_dict({"value": uuid_text}, schema_ref=uuid_reference, role="input").value == uuid_text
    assert uuid_codec.read_dict({"value": "not-a-uuid"}, schema_ref=uuid_reference, role="input").value == "not-a-uuid"

    datetime_reference = _ref("schema.datetime-or-string@1")
    datetime_codec = _codec(DatetimeOrStringInput, datetime_reference)
    timestamp = "2026-01-01T00:00:00Z"
    parsed_datetime = datetime_codec.read_dict({"value": timestamp}, schema_ref=datetime_reference, role="input")
    assert parsed_datetime.value == timestamp


def test_registry_rejects_duck_reference_before_accessing_properties() -> None:
    class DuckReference:
        calls = 0

        @property
        def ref(self) -> str:
            type(self).calls += 1
            return "schema.input@1"

        @property
        def version(self) -> str:
            type(self).calls += 1
            return "v1"

        @property
        def content_hash(self) -> str:
            type(self).calls += 1
            return "a" * 64

    registry = TypedSchemaRegistry()
    duck = cast(Any, DuckReference())
    with pytest.raises(TypeError):
        registry.register(duck, Input, role="input")
    with pytest.raises(TypeError):
        registry.resolve_model(duck, role="input")
    assert DuckReference.calls == 0


def test_registry_rejects_custom_model_metaclass_before_attribute_access() -> None:
    class CountingModelMetaclass(ModelMetaclass):
        calls = 0

        def __getattribute__(cls, name: str) -> object:
            CountingModelMetaclass.calls += 1
            return super().__getattribute__(name)

    class EvilSchema(BaseModel, metaclass=CountingModelMetaclass):
        model_config = ConfigDict(extra="forbid", frozen=True)
        value: str

    CountingModelMetaclass.calls = 0
    registry = TypedSchemaRegistry()
    with pytest.raises(TypeError, match="concrete strict BaseModel"):
        registry.register(_ref("schema.evil-meta@v1"), EvilSchema, role="input")
    assert CountingModelMetaclass.calls == 0


@pytest.mark.parametrize(
    "raw",
    (
        {"name": "x", "count": 1, "items": [1]},
        {"name": "ABCDE", "count": 1, "items": [1]},
        {"name": "A1", "count": 1, "items": [1]},
        {"name": "ok", "count": 0, "items": [1]},
        {"name": "ok", "count": 4, "items": [1]},
        {"name": "ok", "count": 1, "items": []},
        {"name": "ok", "count": 1, "items": [1, 2, 3]},
    ),
)
def test_native_schema_constraints_fail_before_pydantic_callbacks(raw: dict[str, Any]) -> None:
    ConstrainedInput.calls = 0
    reference = _ref("schema.constrained@1")
    codec = _codec(ConstrainedInput, reference)

    with pytest.raises(ValueError):
        codec.read_dict(raw, schema_ref=reference, role="input")
    assert ConstrainedInput.calls == 0


def test_registry_rejects_callable_discriminator_without_executing_it() -> None:
    calls = 0

    def choose(value: Any) -> str:
        nonlocal calls
        calls += 1
        return "int" if type(value) is int else "str"

    choice = Annotated[
        Annotated[int, Tag("int")] | Annotated[str, Tag("str")],
        Discriminator(choose),
    ]

    class CallableDiscriminatorInput(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)
        value: choice

    registry = TypedSchemaRegistry()
    with pytest.raises(ValueError, match="executable or unsupported"):
        registry.register(_ref("schema.callable-discriminator@1"), CallableDiscriminatorInput, role="input")
    assert calls == 0


def test_read_bytes_order_hash_duplicate_utf8_depth_and_nodes() -> None:
    Input.calls = 0
    codec = _codec()
    raw = b'{"value":"ok","nested":[]}'
    expected = hashlib.sha256(raw).hexdigest()
    assert codec.read_bytes(raw, expected_hash=expected, schema_ref=_ref(), role="input").value == "ok"
    assert Input.calls == 1

    with pytest.raises(ValueError, match="hash"):
        codec.read_bytes(raw, expected_hash="b" * 64, schema_ref=_ref(), role="input")
    assert Input.calls == 1

    for bad in (
        b'{"value":"x","value":"y","nested":[]}',
        b"\xff",
        b"[1]",
    ):
        with pytest.raises(ValueError):
            codec.read_bytes(bad, expected_hash=hashlib.sha256(bad).hexdigest(), schema_ref=_ref(), role="input")
    assert Input.calls == 1

    with pytest.raises(ValueError):
        codec.read_bytes(
            b'{"value":"x","nested":[]}',
            expected_hash=hashlib.sha256(b'{"value":"x","nested":[]}').hexdigest(),
            schema_ref=_ref(),
            role="input",
            limits=CanonicalReadLimits(max_bytes=4, max_depth=8, max_nodes=100),
        )
    deep = json.dumps({"value": "x", "nested": []}).encode()
    with pytest.raises(ValueError):
        codec.read_bytes(
            deep,
            expected_hash=hashlib.sha256(deep).hexdigest(),
            schema_ref=_ref(),
            role="input",
            limits=CanonicalReadLimits(max_bytes=1024, max_depth=1, max_nodes=100),
        )
    assert Input.calls == 1


@pytest.mark.parametrize(
    "moving",
    (
        "main",
        "MAIN",
        "master",
        "Master",
        "@main",
        "provider@MAIN",
        "provider:master",
        "refs:heads:main",
        "refs/heads/master",
        "refs/heads/main_",
        "refs/heads/main.",
        "refs/heads/main+",
        "refs/heads/main-",
        "provider:stable",
        "provider/current+",
        "provider:HEAD_",
        "provider/release.",
        "provider/dev-",
        "provider/develop+",
        "provider/trunk_",
    ),
)
def test_versioned_refs_reject_the_complete_moving_alias_family(moving: str) -> None:
    with pytest.raises(ValueError):
        VersionedRef(ref=moving, version="v1", content_hash="a" * 64)


@pytest.mark.parametrize("precise", ("release.fixture@1", "core.release@2026.08.12", "main.profile@v1"))
def test_versioned_refs_allow_nonterminal_namespace_words_when_terminal_is_exact(precise: str) -> None:
    assert VersionedRef(ref=precise, version="v1", content_hash="a" * 64).ref == precise


def _request_raw(*, context: object = ..., tenant: str = "tenant-a") -> dict[str, Any]:
    raw: dict[str, Any] = {
        "meta": {
            "contract_name": "canonical.inference.request",
            "contract_version": "v1",
            "message_id": "00000000-0000-0000-0000-000000000001",
            "tenant_id": tenant,
            "correlation_id": "corr-1",
        },
        "inference_request_id": "00000000-0000-0000-0000-000000000002",
        "run_id": "00000000-0000-0000-0000-000000000003",
        "node_id": "answer",
        "node_attempt": 0,
        "input": {"value": "x", "nested": []},
        "context_refs": [],
        "instructions": [{"role": "user", "content": "answer"}],
        "model_policy": {"model_ref": "model@v1", "temperature": 0.1, "max_output_tokens": 10},
        "result_schema_ref": "schema.output@v1",
        "prompt_policy_ref": "prompt@v1",
        "model_policy_ref": "policy.model@v1",
        "retry_policy": {"max_schema_retries": 0, "max_provider_retries": 0},
        "inference_retry_policy_ref": "policy.retry@v1",
        "budget": {"max_tokens": 10, "max_cost_micros": 100, "deadline_ms": 1000},
        "budget_policy_ref": "policy.budget@v1",
    }
    if context is not ...:
        raw["context"] = context
    return raw


def test_inference_request_reader_preserves_context_three_state_and_tenant() -> None:
    reference = _ref()
    registry = TypedSchemaRegistry()
    registry.register(reference, Input, role="input")

    omitted = read_canonical_inference_request(_request_raw(), input_schema_ref=reference, registry=registry)
    explicit_null = read_canonical_inference_request(
        _request_raw(context=None), input_schema_ref=reference, registry=registry
    )
    present = read_canonical_inference_request(
        _request_raw(context={"context_ref": "context@v1", "summary": "summary"}),
        input_schema_ref=reference,
        registry=registry,
    )
    assert omitted.context_state == "omitted"
    assert explicit_null.context_state == "null"
    assert present.context_state == "present"
    assert omitted.request.context is None
    assert explicit_null.request.context is None
    assert present.request.context is not None

    artifact = {
        "artifact_id": "00000000-0000-0000-0000-000000000004",
        "tenant_id": "tenant-b",
        "version": "v1",
        "content_hash": "a" * 64,
        "media_type": "application/json",
        "sensitivity": "internal",
        "retention_policy_ref": "retention@v1",
    }
    forged = _request_raw()
    forged["context_refs"] = [artifact]
    with pytest.raises(ValueError, match="tenant"):
        read_canonical_inference_request(forged, input_schema_ref=reference, registry=registry)


def test_inference_request_raw_failures_do_not_invoke_input_schema_validator() -> None:
    Input.calls = 0
    reference = _ref()
    registry = TypedSchemaRegistry()
    registry.register(reference, Input, role="input")

    forged = _request_raw()
    forged["input"] = {"value": object(), "nested": []}
    with pytest.raises(ValueError):
        read_canonical_inference_request(forged, input_schema_ref=reference, registry=registry)
    assert Input.calls == 0


def test_typed_inference_request_revalidation_rejects_model_copy_extra_without_callback() -> None:
    Input.calls = 0
    reference = _ref()
    registry = TypedSchemaRegistry()
    registry.register(reference, Input, role="input")
    decoded = read_canonical_inference_request(_request_raw(), input_schema_ref=reference, registry=registry)
    calls_after_valid_construction = Input.calls
    forged = decoded.request.model_copy(update={"attacker_extra": "unvalidated"})

    with pytest.raises(ValueError, match="unvalidated fields"):
        validate_canonical_inference_request(
            forged,
            input_schema_ref=reference,
            registry=registry,
        )

    assert Input.calls == calls_after_valid_construction
