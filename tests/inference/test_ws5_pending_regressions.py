from __future__ import annotations

import json

import httpx
import pytest
from app.contracts.canonical import ArtifactRef, CanonicalMessage, InferenceContext
from app.inference.errors import InferenceErrorCode
from app.inference.schema_catalog import STRUCTURED_INPUT_REF

from .test_pydantic_ai_adapter import Output, _port, request


@pytest.mark.asyncio
async def test_provider_request_preserves_canonical_roles_context_and_refs() -> None:
    from uuid import uuid4

    payloads: list[dict[str, object]] = []

    async def handler(incoming: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(incoming.content))
        return httpx.Response(
            200,
            json={
                "id": "response-context-envelope",
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

    artifact = ArtifactRef(
        artifact_id=uuid4(),
        tenant_id="tenant-a",
        version="v1",
        content_hash="a" * 64,
        media_type="application/json",
        schema_ref="context.schema@v1",
        sensitivity="internal",
        retention_policy_ref="retention@v1",
    )
    rich_request = request().model_copy(
        update={
            "instructions": (
                CanonicalMessage(role="system", content="system-content", content_schema_ref="system@v1"),
                CanonicalMessage(role="user", content="user-content", content_schema_ref="user@v1"),
                CanonicalMessage(role="assistant", content="assistant-content"),
                CanonicalMessage(role="tool", content="tool-content", content_schema_ref="tool@v1"),
            ),
            "context": InferenceContext(context_ref="context@v1", summary="context-summary"),
            "context_refs": (artifact,),
        }
    )
    port = _port(httpx.MockTransport(handler))
    result = await port.infer(rich_request, result_type=Output)
    assert result.result.answer == "done"
    assert len(payloads) == 1
    messages = payloads[0]["messages"]
    assert type(messages) is list
    assert [message["role"] for message in messages] == ["system", "user", "assistant", "user", "user"]
    encoded = json.dumps(payloads[0], sort_keys=True, separators=(",", ":"))
    for expected in (
        "system@v1",
        "user@v1",
        "tool@v1",
        "context@v1",
        "context-summary",
        str(artifact.artifact_id),
        artifact.content_hash,
        "context.schema@v1",
        STRUCTURED_INPUT_REF.ref,
    ):
        assert expected in encoded
    await port.aclose()


@pytest.mark.asyncio
async def test_oversized_canonical_request_fails_before_provider() -> None:
    sends = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal sends
        sends += 1
        return httpx.Response(500)

    oversized = request().model_copy(
        update={
            "instructions": tuple(
                CanonicalMessage(role="user", content=f"{index:03d}:" + "x" * 16_000) for index in range(70)
            )
        }
    )
    port = _port(httpx.MockTransport(handler))
    with pytest.raises(Exception) as exc_info:
        await port.infer(oversized, result_type=Output)
    assert getattr(exc_info.value, "code", None) is InferenceErrorCode.POLICY_REJECTED
    assert sends == port.physical_sends == 0
    await port.aclose()


@pytest.mark.asyncio
async def test_schema_and_provider_retries_share_one_failure_ledger() -> None:
    requests: list[httpx.Request] = []

    async def handler(incoming: httpx.Request) -> httpx.Response:
        requests.append(incoming)
        if len(requests) == 2:
            return httpx.Response(
                503,
                json={
                    "error": {"message": "temporarily unavailable"},
                    "usage": {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1},
                },
            )
        content = '{"wrong":"shape"}' if len(requests) == 1 else '{"answer":"repaired"}'
        return httpx.Response(
            200,
            json={
                "id": f"response-interleaved-{len(requests)}",
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

    port = _port(httpx.MockTransport(handler), schema_retries=1, provider_retries=1)
    result = await port.infer(request(schema_retries=1, provider_retries=1), result_type=Output)
    assert result.result.answer == "repaired"
    assert result.provider_attempts == len(requests) == 3
    assert result.schema_retries == 1
    assert result.usage.input_tokens == 3
    assert result.usage.output_tokens == 2
    await port.aclose()
