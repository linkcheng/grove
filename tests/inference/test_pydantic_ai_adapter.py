from __future__ import annotations

import asyncio
import json
from typing import ClassVar, Literal

import httpx
import pytest
from app.contracts.canonical import (
    CanonicalInferenceRequest,
    ContractMeta,
    InferenceBudget,
    InferenceContext,
    ResolvedInferenceRetryPolicy,
    ResolvedModelPolicy,
    StructuredInferenceInput,
    StructuredInferenceOutput,
    VersionedRef,
    canonical_hash,
)
from app.inference.ai_config import AIGatewayConfig
from app.inference.contracts import PricingPolicy, ProviderBindingManifest, ProviderProfilePolicy
from app.inference.errors import InferenceErrorCode
from app.inference.ledger import current_invocation_budget
from app.inference.pydantic_ai_adapter import PydanticAIInferencePort
from app.inference.schema_catalog import (
    STRUCTURED_INPUT_REF,
    STRUCTURED_OUTPUT_REF,
)
from pydantic import BaseModel, ConfigDict, SecretStr

Input = StructuredInferenceInput
Output = StructuredInferenceOutput


def _ref(ref: str, digest: str) -> VersionedRef:
    return VersionedRef(ref=ref, version="v1", content_hash=digest)


def _manifest(
    *,
    schema_retries: int = 0,
    provider_retries: int = 0,
    output_mode: Literal["native", "prompted"] = "native",
) -> ProviderBindingManifest:
    input_ref = STRUCTURED_INPUT_REF
    output_ref = STRUCTURED_OUTPUT_REF
    profile_payload = {
        "supports_tools": False,
        "supports_json_schema_output": output_mode == "native",
        "supports_json_object_output": True,
        "default_structured_output_mode": output_mode,
        "openai_chat_supports_max_completion_tokens": False,
    }
    model_policy = ResolvedModelPolicy(model_ref="model@2026", temperature=0.0, max_output_tokens=32)
    retry_policy = ResolvedInferenceRetryPolicy(
        max_schema_retries=schema_retries,
        max_provider_retries=provider_retries,
    )
    budget_policy = InferenceBudget(max_tokens=100, max_cost_micros=1000, deadline_ms=5000)
    pricing_policy = PricingPolicy(
        currency="CNY",
        input_micros_per_million=100,
        output_micros_per_million=200,
        base_cost_micros=10,
    )
    return ProviderBindingManifest(
        schema_version="provider-binding-manifest.v1",
        provider_type="openai-compatible",
        endpoint_url="http://127.0.0.1/v1",
        provider_profile=ProviderProfilePolicy(
            profile_ref=_ref("provider-profile/gateway", canonical_hash(profile_payload)),
            supports_tools=False,
            supports_json_schema_output=output_mode == "native",
            supports_json_object_output=True,
            default_structured_output_mode=output_mode,
            openai_chat_supports_max_completion_tokens=False,
        ),
        model_identifier="model@2026",
        model_hash="f" * 64,
        endpoint_config_fingerprint="c" * 64,
        sdk_version="3.0.0",
        sdk_hash="1" * 64,
        pydantic_ai_version="2.22.0",
        pydantic_ai_hash="2" * 64,
        adapter_version="grove.inference.v2",
        adapter_hash="3" * 64,
        runtime_build_version="build@2026",
        runtime_build_hash="d" * 64,
        model_policy=model_policy,
        retry_policy=retry_policy,
        budget_policy=budget_policy,
        pricing_policy=pricing_policy,
        input_schema_ref=input_ref,
        output_schema_ref=output_ref,
        prompt_policy_ref=_ref("prompt@v1", "4" * 64),
        model_policy_ref=_ref("model-policy@v1", canonical_hash(model_policy)),
        retry_policy_ref=_ref("retry@v1", canonical_hash(retry_policy)),
        budget_policy_ref=_ref("budget@v1", canonical_hash(budget_policy)),
        pricing_policy_ref=_ref("pricing@v1", canonical_hash(pricing_policy)),
        sdk_max_retries=0,
        credential_slot_id="gateway-primary",
    )


def _port(
    handler: httpx.AsyncBaseTransport,
    *,
    schema_retries: int = 0,
    provider_retries: int = 0,
    output_mode: Literal["native", "prompted"] = "native",
) -> PydanticAIInferencePort:
    manifest = _manifest(
        schema_retries=schema_retries,
        provider_retries=provider_retries,
        output_mode=output_mode,
    )
    config = AIGatewayConfig(
        app_env="test",
        url="http://127.0.0.1/v1",
        api_key=SecretStr("secret"),
        model="model@2026",
        credential_slot_id="gateway-primary",
    )
    return PydanticAIInferencePort._compose(
        manifest=manifest,
        gateway_config=config,
        transport=handler,
    )


def request(*, schema_retries: int = 0, provider_retries: int = 0) -> CanonicalInferenceRequest[Input]:
    from uuid import uuid4

    return CanonicalInferenceRequest[Input](
        meta=ContractMeta(
            contract_name="canonical.inference.request",
            contract_version="v1",
            message_id=uuid4(),
            tenant_id="tenant-a",
            correlation_id="corr-a",
        ),
        inference_request_id=uuid4(),
        run_id=uuid4(),
        node_id="node-a",
        node_attempt=0,
        input=Input(question="hello"),
        model_policy=ResolvedModelPolicy(model_ref="model@2026", temperature=0.0, max_output_tokens=32),
        result_schema_ref=STRUCTURED_OUTPUT_REF.ref,
        prompt_policy_ref="prompt@v1",
        model_policy_ref="model-policy@v1",
        retry_policy=ResolvedInferenceRetryPolicy(
            max_schema_retries=schema_retries,
            max_provider_retries=provider_retries,
        ),
        inference_retry_policy_ref="retry@v1",
        budget=InferenceBudget(max_tokens=100, max_cost_micros=1000, deadline_ms=5000),
        budget_policy_ref="budget@v1",
    )


@pytest.mark.asyncio
async def test_invalid_request_fails_before_any_transport_send() -> None:
    sends = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal sends
        sends += 1
        return httpx.Response(500)

    port = _port(httpx.MockTransport(handler))
    bad = request().model_copy(
        update={
            "model_policy": ResolvedModelPolicy(
                model_ref="other@1",
                temperature=0,
                max_output_tokens=32,
            )
        }
    )
    with pytest.raises(Exception) as exc_info:
        await port.infer(bad, result_type=Output)
    assert getattr(exc_info.value, "code", None) is InferenceErrorCode.POLICY_REJECTED
    assert port.physical_sends == 0
    assert sends == 0
    await port.aclose()


@pytest.mark.asyncio
async def test_model_copy_extra_in_request_is_rejected_before_transport() -> None:
    sends = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal sends
        sends += 1
        return httpx.Response(500)

    port = _port(httpx.MockTransport(handler))
    injected = request().model_copy(update={"attacker_extra": "not validated"})
    with pytest.raises(Exception) as exc_info:
        await port.infer(injected, result_type=Output)
    assert getattr(exc_info.value, "code", None) is InferenceErrorCode.POLICY_REJECTED
    assert sends == 0
    assert port.physical_sends == 0
    await port.aclose()


@pytest.mark.asyncio
async def test_oversized_typed_request_is_rejected_before_transport() -> None:
    sends = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal sends
        sends += 1
        return httpx.Response(500)

    port = _port(httpx.MockTransport(handler))
    oversized = request().model_copy(update={"input": Input(question="x" * 1_048_576)})
    with pytest.raises(Exception) as exc_info:
        await port.infer(oversized, result_type=Output)
    assert getattr(exc_info.value, "code", None) is InferenceErrorCode.POLICY_REJECTED
    assert sends == 0
    assert port.physical_sends == 0
    await port.aclose()


@pytest.mark.asyncio
async def test_present_context_survives_typed_request_revalidation() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "response-context",
                "object": "chat.completion",
                "created": 1,
                "model": "model@2026",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": '{"answer":"done"}'},
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    port = _port(httpx.MockTransport(handler))
    with_context = request().model_copy(update={"context": InferenceContext(summary="safe context")})
    assert (await port.infer(with_context, result_type=Output)).result.answer == "done"
    await port.aclose()


@pytest.mark.asyncio
async def test_forged_pydantic_extras_fail_without_equality_user_code() -> None:
    class Evil:
        calls: ClassVar[int] = 0

        def __eq__(self, _: object) -> bool:
            Evil.calls += 1
            return False

    forged = request()
    object.__setattr__(forged, "__pydantic_extra__", Evil())
    port = _port(httpx.MockTransport(lambda _: httpx.Response(500)))
    with pytest.raises(Exception) as exc_info:
        await port.infer(forged, result_type=Output)
    assert getattr(exc_info.value, "code", None) is InferenceErrorCode.POLICY_REJECTED
    assert Evil.calls == 0
    assert port.physical_sends == 0
    await port.aclose()


@pytest.mark.asyncio
async def test_duck_request_is_rejected_before_class_property() -> None:
    class Evil:
        calls = 0

        def __getattribute__(self, name: str) -> object:
            if name == "__class__":
                Evil.calls += 1
                return CanonicalInferenceRequest
            return super().__getattribute__(name)

    port = _port(httpx.MockTransport(lambda _: httpx.Response(500)))
    with pytest.raises(Exception) as exc_info:
        await port.infer(Evil(), result_type=Output)  # type: ignore[arg-type]
    assert getattr(exc_info.value, "code", None) is InferenceErrorCode.POLICY_REJECTED
    assert Evil.calls == 0
    assert port.physical_sends == 0
    await port.aclose()


@pytest.mark.asyncio
async def test_unbound_result_type_is_rejected_without_overridden_schema_hook() -> None:
    class EvilOutput(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)
        calls: ClassVar[int] = 0
        value: str

        @classmethod
        def model_json_schema(cls, *args: object, **kwargs: object) -> dict[str, object]:
            EvilOutput.calls += 1
            return Output.model_json_schema()

    port = _port(httpx.MockTransport(lambda _: httpx.Response(500)))
    with pytest.raises(Exception) as exc_info:
        await port.infer(request(), result_type=EvilOutput)
    assert getattr(exc_info.value, "code", None) is InferenceErrorCode.POLICY_REJECTED
    assert EvilOutput.calls == 0
    assert port.physical_sends == 0
    await port.aclose()


@pytest.mark.asyncio
async def test_unbound_result_type_is_rejected_without_pydantic_json_schema_hook() -> None:
    class EvilOutput(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)
        calls: ClassVar[int] = 0
        answer: str

        @classmethod
        def __get_pydantic_json_schema__(cls, core_schema: object, handler: object) -> dict[str, object]:
            EvilOutput.calls += 1
            return Output.model_json_schema()

    port = _port(httpx.MockTransport(lambda _: httpx.Response(500)))
    with pytest.raises(Exception) as exc_info:
        await port.infer(request(), result_type=EvilOutput)
    assert getattr(exc_info.value, "code", None) is InferenceErrorCode.POLICY_REJECTED
    assert EvilOutput.calls == 0
    assert port.physical_sends == 0
    await port.aclose()


@pytest.mark.asyncio
async def test_openai_ambient_routing_environment_cannot_change_final_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    async def handler(incoming: httpx.Request) -> httpx.Response:
        requests.append(incoming)
        return httpx.Response(
            200,
            json={
                "id": "response-ambient",
                "object": "chat.completion",
                "created": 1,
                "model": "model@2026",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": '{"answer":"done"}'},
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    monkeypatch.setenv("OPENAI_ORG_ID", "ambient-org")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "ambient-project")
    monkeypatch.setenv("OPENAI_WEBHOOK_SECRET", "ambient-webhook")
    port = _port(httpx.MockTransport(handler))
    await port.infer(request(), result_type=Output)
    assert len(requests) == 1
    assert "OpenAI-Organization" not in requests[0].headers
    assert "OpenAI-Project" not in requests[0].headers
    await port.aclose()


@pytest.mark.asyncio
async def test_policy_ref_drift_fails_before_transport() -> None:
    port = _port(httpx.MockTransport(lambda _: httpx.Response(500)))
    drifted = request().model_copy(update={"prompt_policy_ref": "other-prompt@v1"})
    with pytest.raises(Exception) as exc_info:
        await port.infer(drifted, result_type=Output)
    assert getattr(exc_info.value, "code", None) is InferenceErrorCode.POLICY_REJECTED
    assert port.physical_sends == 0
    await port.aclose()


def test_adapter_does_not_allow_model_client_in_public_constructor() -> None:
    assert "model_client" not in PydanticAIInferencePort.__init__.__annotations__


@pytest.mark.asyncio
async def test_structured_output_and_usage_follow_physical_transport() -> None:
    requests: list[httpx.Request] = []

    async def handler(incoming: httpx.Request) -> httpx.Response:
        requests.append(incoming)
        return httpx.Response(
            200,
            json={
                "id": "response-1",
                "object": "chat.completion",
                "created": 1,
                "model": "model@2026",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": '{"answer":"done"}'},
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
            },
        )

    port = _port(httpx.MockTransport(handler))
    result = await port.infer(request(), result_type=Output)
    assert result.result == Output(answer="done")
    assert result.provider_attempts == len(requests) == 1
    assert result.usage.input_tokens == 3
    assert result.usage.output_tokens == 4
    assert result.usage.cost_micros == 11
    await port.aclose()


@pytest.mark.asyncio
async def test_signed_prompted_profile_controls_final_http_request() -> None:
    payloads: list[dict[str, object]] = []

    async def handler(incoming: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(incoming.content))
        return httpx.Response(
            200,
            json={
                "id": "response-prompted",
                "object": "chat.completion",
                "created": 1,
                "model": "model@2026",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": '{"answer":"done"}'},
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
            },
        )

    port = _port(httpx.MockTransport(handler), output_mode="prompted")
    result = await port.infer(request(), result_type=Output)
    assert result.result.answer == "done"
    assert len(payloads) == 1
    assert payloads[0]["response_format"] == {"type": "json_object"}
    assert "tools" not in payloads[0]
    await port.aclose()


@pytest.mark.asyncio
async def test_content_filter_is_permanent() -> None:
    requests: list[httpx.Request] = []

    async def handler(incoming: httpx.Request) -> httpx.Response:
        requests.append(incoming)
        return httpx.Response(
            200,
            json={
                "id": "filtered-1",
                "object": "chat.completion",
                "created": 1,
                "model": "model@2026",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "content_filter",
                        "message": {"role": "assistant", "content": None},
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1},
            },
        )

    port = _port(httpx.MockTransport(handler))
    with pytest.raises(Exception) as exc_info:
        await port.infer(request(), result_type=Output)
    assert getattr(exc_info.value, "code", None) is InferenceErrorCode.CONTENT_FILTERED
    assert len(requests) == 1
    await port.aclose()


@pytest.mark.asyncio
async def test_provider_transient_retry_shares_physical_attempt_ledger() -> None:
    requests: list[httpx.Request] = []

    async def handler(incoming: httpx.Request) -> httpx.Response:
        requests.append(incoming)
        if len(requests) == 1:
            return httpx.Response(
                503,
                json={
                    "error": {"message": "temporarily unavailable"},
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "response-2",
                "object": "chat.completion",
                "created": 1,
                "model": "model@2026",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": '{"answer":"done"}'},
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
            },
        )

    port = _port(httpx.MockTransport(handler), provider_retries=1)
    result = await port.infer(request(provider_retries=1), result_type=Output)
    assert result.result.answer == "done"
    assert result.provider_attempts == len(requests) == 2
    assert result.usage.input_tokens == 5
    assert result.usage.output_tokens == 5
    assert result.usage.cost_micros == 21
    await port.aclose()


@pytest.mark.asyncio
async def test_transport_token_budget_error_keeps_stable_error_code() -> None:
    requests: list[httpx.Request] = []

    async def handler(incoming: httpx.Request) -> httpx.Response:
        requests.append(incoming)
        return httpx.Response(
            200,
            json={
                "id": "response-over-budget",
                "object": "chat.completion",
                "created": 1,
                "model": "model@2026",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": '{"answer":"done"}'},
                    }
                ],
                "usage": {"prompt_tokens": 101, "completion_tokens": 0, "total_tokens": 101},
            },
        )

    port = _port(httpx.MockTransport(handler), provider_retries=1)
    with pytest.raises(Exception) as exc_info:
        await port.infer(request(provider_retries=1), result_type=Output)
    assert getattr(exc_info.value, "code", None) is InferenceErrorCode.BUDGET_EXHAUSTED
    assert len(requests) == 1
    await port.aclose()


@pytest.mark.asyncio
async def test_connection_error_uses_adapter_owned_provider_retry() -> None:
    requests: list[httpx.Request] = []

    async def handler(incoming: httpx.Request) -> httpx.Response:
        requests.append(incoming)
        raise httpx.ConnectError("connection reset", request=incoming)

    port = _port(httpx.MockTransport(handler), provider_retries=1)
    with pytest.raises(Exception) as exc_info:
        await port.infer(request(provider_retries=1), result_type=Output)
    assert getattr(exc_info.value, "code", None) is InferenceErrorCode.PROVIDER_TRANSIENT
    assert port.physical_sends == len(requests) == 2
    await port.aclose()


@pytest.mark.asyncio
async def test_schema_repair_uses_same_physical_attempt_ledger() -> None:
    requests: list[httpx.Request] = []

    async def handler(incoming: httpx.Request) -> httpx.Response:
        requests.append(incoming)
        content = '{"wrong":"shape"}' if len(requests) == 1 else '{"answer":"repaired"}'
        return httpx.Response(
            200,
            json={
                "id": f"response-{len(requests)}",
                "object": "chat.completion",
                "created": 1,
                "model": "model@2026",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": content},
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    port = _port(httpx.MockTransport(handler), schema_retries=1)
    result = await port.infer(request(schema_retries=1), result_type=Output)
    assert result.result.answer == "repaired"
    assert result.provider_attempts == len(requests) == 2
    assert result.schema_retries == 1
    assert result.usage.cost_micros == 21
    await port.aclose()


@pytest.mark.asyncio
async def test_refusal_is_permanent_and_never_enters_schema_retry() -> None:
    requests: list[httpx.Request] = []

    async def handler(incoming: httpx.Request) -> httpx.Response:
        requests.append(incoming)
        return httpx.Response(
            200,
            json={
                "id": "refusal-1",
                "object": "chat.completion",
                "created": 1,
                "model": "model@2026",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": None, "refusal": "cannot comply"},
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1},
            },
        )

    port = _port(httpx.MockTransport(handler), schema_retries=1)
    with pytest.raises(Exception) as exc_info:
        await port.infer(request(schema_retries=1), result_type=Output)
    assert getattr(exc_info.value, "code", None) is InferenceErrorCode.REFUSED
    assert len(requests) == 1
    await port.aclose()


@pytest.mark.asyncio
async def test_content_filter_does_not_classify_unrelated_refusal_substring() -> None:
    requests: list[httpx.Request] = []

    async def handler(incoming: httpx.Request) -> httpx.Response:
        requests.append(incoming)
        return httpx.Response(
            200,
            json={
                "id": "filtered-substring",
                "object": "chat.completion",
                "created": 1,
                "model": "contains-refusal-token",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "content_filter",
                        "message": {"role": "assistant", "content": None},
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1},
            },
        )

    port = _port(httpx.MockTransport(handler), schema_retries=1)
    with pytest.raises(Exception) as exc_info:
        await port.infer(request(schema_retries=1), result_type=Output)
    assert getattr(exc_info.value, "code", None) is InferenceErrorCode.CONTENT_FILTERED
    assert len(requests) == 1
    await port.aclose()


@pytest.mark.asyncio
async def test_adapter_cancellation_propagates_and_cleans_context() -> None:
    started = asyncio.Event()

    async def handler(_: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError

    port = _port(httpx.MockTransport(handler))
    task = asyncio.create_task(port.infer(request(), result_type=Output))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert current_invocation_budget.get() is None
    await port.aclose()
