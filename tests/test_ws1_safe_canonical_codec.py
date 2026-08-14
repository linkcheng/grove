"""WS-1 safe canonical codec contract tests.

These tests exercise the public raw-data seam.  In particular, invalid raw
values must be rejected before the schema registry or a Pydantic validator is
allowed to observe them.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from traceback import format_exception
from typing import Annotated, Any, ClassVar, cast
from uuid import UUID

import pytest
from app.contracts.canonical import (
    CanonicalCodecError,
    CanonicalReadLimits,
    SafeCanonicalCodec,
    TypedSchemaRegistry,
    VersionedRef,
    canonical_bytes,
    read_canonical_inference_request,
    validate_canonical_inference_request,
)
from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag, field_serializer, field_validator
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


class NumericInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    value: float


class CallbackInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    value: str

    validator_calls: ClassVar[int] = 0
    serializer_calls: ClassVar[int] = 0

    @field_validator("value")
    @classmethod
    def count_validation(cls, value: str) -> str:
        cls.validator_calls += 1
        return value

    @field_serializer("value")
    def count_serialization(self, value: str) -> str:
        type(self).serializer_calls += 1
        return value


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


def test_read_bytes_accepts_only_the_unique_canonical_representation() -> None:
    Input.calls = 0
    codec = _codec()
    raw = canonical_bytes({"value": "ok", "nested": []})
    expected = hashlib.sha256(raw).hexdigest()
    assert codec.read_bytes(raw, expected_hash=expected, schema_ref=_ref(), role="input").value == "ok"
    assert Input.calls == 1

    with pytest.raises(ValueError, match="hash"):
        codec.read_bytes(raw, expected_hash="b" * 64, schema_ref=_ref(), role="input")
    assert Input.calls == 1


@pytest.mark.parametrize(
    "raw",
    (
        b'{"nested":[],"value":"ok"}',
        b'{"nested":[],"value":"ok"}\n\n',
        b' {"nested":[],"value":"ok"}\n',
        b'{"nested":[],"value":"ok"} \n',
        b'{"nested": [],"value":"ok"}\n',
        b'{"value":"ok","nested":[]}\n',
        b'{"nested": [], "value": "ok"}\n',
        b'{"nested":[],"value":"\\u00e9"}\n',
    ),
)
def test_noncanonical_equivalent_bytes_are_rejected_with_their_own_hash_before_schema(raw: bytes) -> None:
    Input.calls = 0
    codec = _codec()

    with pytest.raises(CanonicalCodecError, match="bytes mismatch"):
        codec.read_bytes(raw, expected_hash=hashlib.sha256(raw).hexdigest(), schema_ref=_ref(), role="input")
    assert Input.calls == 0


def test_noncanonical_number_spelling_is_rejected_before_schema() -> None:
    raw = b'{"value":1e0}\n'
    reference = _ref("schema.numeric@1")
    codec = _codec(NumericInput, reference)

    with pytest.raises(CanonicalCodecError, match="bytes mismatch"):
        codec.read_bytes(raw, expected_hash=hashlib.sha256(raw).hexdigest(), schema_ref=reference, role="input")


def test_noncanonical_bytes_with_canonical_hash_fail_at_hash_before_schema() -> None:
    Input.calls = 0
    codec = _codec()
    canonical = canonical_bytes({"value": "ok", "nested": []})
    noncanonical = canonical.removesuffix(b"\n")

    with pytest.raises(CanonicalCodecError, match="hash"):
        codec.read_bytes(
            noncanonical,
            expected_hash=hashlib.sha256(canonical).hexdigest(),
            schema_ref=_ref(),
            role="input",
        )
    assert Input.calls == 0


@pytest.mark.parametrize(
    "bad",
    (
        b'{"nested":[],"value":"x","value":"y"}\n',
        b'{"nested":[],"value":NaN}\n',
        b'{"nested":[],"value":Infinity}\n',
        b"\xff",
    ),
)
def test_invalid_json_spellings_remain_rejected_before_schema(bad: bytes) -> None:
    Input.calls = 0
    codec = _codec()

    with pytest.raises(ValueError):
        codec.read_bytes(bad, expected_hash=hashlib.sha256(bad).hexdigest(), schema_ref=_ref(), role="input")
    assert Input.calls == 0


def test_read_bytes_keeps_root_depth_node_and_raw_size_guards() -> None:
    Input.calls = 0
    codec = _codec()
    root_array = b"[1]\n"
    with pytest.raises(ValueError, match="root"):
        codec.read_bytes(
            root_array,
            expected_hash=hashlib.sha256(root_array).hexdigest(),
            schema_ref=_ref(),
            role="input",
        )

    raw = canonical_bytes({"value": "x", "nested": []})
    with pytest.raises(ValueError):
        codec.read_bytes(
            raw,
            expected_hash=hashlib.sha256(raw).hexdigest(),
            schema_ref=_ref(),
            role="input",
            limits=CanonicalReadLimits(max_bytes=4, max_depth=8, max_nodes=100),
        )
    deep = canonical_bytes({"value": "x", "nested": []})
    with pytest.raises(ValueError):
        codec.read_bytes(
            deep,
            expected_hash=hashlib.sha256(deep).hexdigest(),
            schema_ref=_ref(),
            role="input",
            limits=CanonicalReadLimits(max_bytes=1024, max_depth=1, max_nodes=100),
        )
    assert Input.calls == 0


def test_read_dict_rejects_canonical_size_over_limit_before_schema() -> None:
    Input.calls = 0
    raw = {"value": "x", "nested": []}
    max_bytes = len(canonical_bytes(raw)) - 1

    with pytest.raises(CanonicalCodecError, match="size limit"):
        _codec().read_dict(
            raw,
            schema_ref=_ref(),
            role="input",
            limits=CanonicalReadLimits(max_bytes=max_bytes, max_depth=8, max_nodes=100),
        )
    assert Input.calls == 0


def test_noncanonical_bytes_never_invoke_pydantic_validator_or_serializer() -> None:
    CallbackInput.validator_calls = 0
    CallbackInput.serializer_calls = 0
    reference = _ref("schema.callback@1")
    codec = _codec(CallbackInput, reference)
    raw = b'{"value": "secret-marker"}\n'

    with pytest.raises(CanonicalCodecError, match="bytes mismatch"):
        codec.read_bytes(raw, expected_hash=hashlib.sha256(raw).hexdigest(), schema_ref=reference, role="input")
    assert CallbackInput.validator_calls == 0
    assert CallbackInput.serializer_calls == 0


def test_canonical_encoding_failure_has_no_input_or_underlying_exception_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codec = _codec()
    input_marker = "input-" + "secret-marker"
    underlying_marker = "underlying-" + "secret-marker"

    def fail_encoding(_: object, *, exclude_fields: tuple[str, ...] = ()) -> bytes:
        del exclude_fields
        raise ValueError(underlying_marker)

    monkeypatch.setattr("app.contracts.canonical.canonical_bytes", fail_encoding)
    with pytest.raises(CanonicalCodecError, match="encoding failed") as exc_info:
        codec.read_dict({"value": input_marker, "nested": []}, schema_ref=_ref(), role="input")

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    rendered = "".join(format_exception(exc_info.value))
    assert input_marker not in rendered
    assert underlying_marker not in rendered


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
    state_bytes = {
        canonical_bytes(_request_raw()),
        canonical_bytes(_request_raw(context=None)),
        canonical_bytes(_request_raw(context={"context_ref": "context@v1", "summary": "summary"})),
    }
    assert len(state_bytes) == 3

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


def test_typed_inference_request_size_fails_before_schema_resolution() -> None:
    reference = _ref()
    registry = TypedSchemaRegistry()
    registry.register(reference, Input, role="input")
    decoded = read_canonical_inference_request(_request_raw(), input_schema_ref=reference, registry=registry)
    oversized = decoded.request.model_copy(
        update={"input": Input(value="x" * 256, nested=[])},
    )

    class ResolveTrap(dict[tuple[str, str, str, str], type[BaseModel]]):
        calls = 0

        def __getitem__(self, key: tuple[str, str, str, str]) -> type[BaseModel]:
            type(self).calls += 1
            return super().__getitem__(key)

    object.__setattr__(registry, "_bindings", ResolveTrap(registry._bindings))
    with pytest.raises(CanonicalCodecError, match="size limit"):
        validate_canonical_inference_request(
            oversized,
            input_schema_ref=reference,
            registry=registry,
            limits=CanonicalReadLimits(max_bytes=128, max_depth=32, max_nodes=10_000),
        )
    assert ResolveTrap.calls == 0


def test_typed_inference_request_storage_projection_does_not_execute_subclass_property() -> None:
    reference = _ref()
    registry = TypedSchemaRegistry()
    registry.register(reference, Input, role="input")
    decoded = read_canonical_inference_request(_request_raw(), input_schema_ref=reference, registry=registry)
    request_model = type(decoded.request)
    calls = 0

    class EvilRequest(request_model):  # type: ignore[misc, valid-type]
        @property
        def __pydantic_extra__(self) -> dict[str, Any] | None:
            nonlocal calls
            calls += 1
            return None

    forged = object.__new__(EvilRequest)
    with pytest.raises(TypeError, match="invalid model storage"):
        validate_canonical_inference_request(
            forged,
            input_schema_ref=reference,
            registry=registry,
        )
    assert calls == 0


def test_typed_inference_request_preserves_context_presence_states() -> None:
    reference = _ref()
    registry = TypedSchemaRegistry()
    registry.register(reference, Input, role="input")
    states = []
    for raw in (
        _request_raw(),
        _request_raw(context=None),
        _request_raw(context={"context_ref": "context@v1", "summary": "summary"}),
    ):
        decoded = read_canonical_inference_request(raw, input_schema_ref=reference, registry=registry)
        revalidated = validate_canonical_inference_request(
            decoded.request,
            input_schema_ref=reference,
            registry=registry,
        )
        states.append(revalidated.context_state)

    assert states == ["omitted", "null", "present"]


def test_typed_preflight_does_not_execute_field_catalog_descriptor() -> None:
    reference = _ref()
    registry = TypedSchemaRegistry()
    registry.register(reference, Input, role="input")
    decoded = read_canonical_inference_request(_request_raw(), input_schema_ref=reference, registry=registry)
    calls = 0

    class EvilDescriptor:
        def __get__(self, instance: object, owner: object) -> dict[str, Any]:
            del instance, owner
            nonlocal calls
            calls += 1
            return {}

    namespace = type.__getattribute__(Input, "__dict__")
    original_fields = namespace["__pydantic_fields__"]
    type.__setattr__(Input, "__pydantic_fields__", EvilDescriptor())
    try:
        with pytest.raises(TypeError, match="field catalog"):
            validate_canonical_inference_request(
                decoded.request,
                input_schema_ref=reference,
                registry=registry,
            )
    finally:
        type.__setattr__(Input, "__pydantic_fields__", original_fields)
    assert calls == 0


def test_typed_preflight_does_not_execute_replaced_base_model_descriptor() -> None:
    reference = _ref()
    registry = TypedSchemaRegistry()
    registry.register(reference, Input, role="input")
    decoded = read_canonical_inference_request(_request_raw(), input_schema_ref=reference, registry=registry)
    calls = 0

    class EvilDescriptor:
        def __get__(self, instance: object, owner: object) -> None:
            del instance, owner
            nonlocal calls
            calls += 1

    base_namespace = type.__getattribute__(BaseModel, "__dict__")
    original_extra_descriptor = base_namespace["__pydantic_extra__"]
    type.__setattr__(BaseModel, "__pydantic_extra__", EvilDescriptor())
    try:
        with pytest.raises(TypeError, match="invalid model storage"):
            validate_canonical_inference_request(
                decoded.request,
                input_schema_ref=reference,
                registry=registry,
            )
    finally:
        type.__setattr__(BaseModel, "__pydantic_extra__", original_extra_descriptor)
    assert calls == 0


def test_typed_revalidation_does_not_execute_model_fields_descriptor() -> None:
    reference = _ref()
    registry = TypedSchemaRegistry()
    registry.register(reference, Input, role="input")
    decoded = read_canonical_inference_request(_request_raw(), input_schema_ref=reference, registry=registry)
    calls = 0

    class EvilDescriptor:
        def __get__(self, instance: object, owner: object) -> dict[str, Any]:
            del instance, owner
            nonlocal calls
            calls += 1
            raise RuntimeError("model-fields-marker")

    type.__setattr__(Input, "model_fields", EvilDescriptor())
    try:
        with pytest.raises(TypeError, match="field catalog"):
            validate_canonical_inference_request(
                decoded.request,
                input_schema_ref=reference,
                registry=registry,
            )
    finally:
        type.__delattr__(Input, "model_fields")
    assert calls == 0


def test_typed_preflight_bounds_cycles_before_schema_resolution() -> None:
    reference = _ref()
    registry = TypedSchemaRegistry()
    registry.register(reference, Input, role="input")
    decoded = read_canonical_inference_request(_request_raw(), input_schema_ref=reference, registry=registry)
    cycle: list[Any] = []
    cycle.append(cycle)
    forged_input = decoded.request.input.model_copy(update={"nested": cycle})
    forged_request = decoded.request.model_copy(update={"input": forged_input})

    class ResolveTrap(dict[tuple[str, str, str, str], type[BaseModel]]):
        calls = 0

        def __getitem__(self, key: tuple[str, str, str, str]) -> type[BaseModel]:
            type(self).calls += 1
            return super().__getitem__(key)

    object.__setattr__(registry, "_bindings", ResolveTrap(registry._bindings))
    with pytest.raises(CanonicalCodecError, match="depth limit"):
        validate_canonical_inference_request(
            forged_request,
            input_schema_ref=reference,
            registry=registry,
            limits=CanonicalReadLimits(max_bytes=1024, max_depth=4, max_nodes=100),
        )
    assert ResolveTrap.calls == 0


def test_typed_preflight_rejects_custom_datetime_timezone_without_calling_it() -> None:
    class EvilTimezone(tzinfo):
        calls = 0

        def utcoffset(self, value: datetime | None) -> timedelta:
            del value
            type(self).calls += 1
            return timedelta(0)

        def dst(self, value: datetime | None) -> timedelta:
            del value
            return timedelta(0)

        def tzname(self, value: datetime | None) -> str:
            del value
            return "evil"

    reference = _ref("schema.datetime@1")
    registry = TypedSchemaRegistry()
    registry.register(reference, DatetimeInput, role="input")
    raw = _request_raw()
    raw["input"] = {"at": "2026-08-14T00:00:00Z"}
    decoded = read_canonical_inference_request(raw, input_schema_ref=reference, registry=registry)
    validated = validate_canonical_inference_request(
        decoded.request,
        input_schema_ref=reference,
        registry=registry,
    )
    assert cast(DatetimeInput, validated.request.input).at == datetime(2026, 8, 14, tzinfo=UTC)

    forged_input = decoded.request.input.model_copy(
        update={"at": datetime(2026, 8, 14, tzinfo=EvilTimezone())},
    )
    forged_request = decoded.request.model_copy(update={"input": forged_input})
    with pytest.raises(TypeError, match="timezone"):
        validate_canonical_inference_request(
            forged_request,
            input_schema_ref=reference,
            registry=registry,
        )
    assert EvilTimezone.calls == 0


def test_typed_revalidation_rejects_wrong_nested_model_before_validator() -> None:
    class OtherNested(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)
        value: str

    NestedInput.calls = 0
    reference = _ref("schema.nested-model@1")
    registry = TypedSchemaRegistry()
    registry.register(reference, InputWithNested, role="input")
    raw = _request_raw()
    raw["input"] = {"value": {"value": "ok"}}
    decoded = read_canonical_inference_request(raw, input_schema_ref=reference, registry=registry)
    calls_after_construction = NestedInput.calls
    forged_input = decoded.request.input.model_copy(update={"value": OtherNested(value="ok")})
    forged_request = decoded.request.model_copy(update={"input": forged_input})

    with pytest.raises(TypeError, match="unexpected model type"):
        validate_canonical_inference_request(
            forged_request,
            input_schema_ref=reference,
            registry=registry,
        )
    assert NestedInput.calls == calls_after_construction
