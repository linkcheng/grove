"""Private PydanticAI implementation owned by the runtime-worker root."""

from __future__ import annotations

import asyncio
import json
from typing import Any, TypeVar, cast

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel, TypeAdapter
from pydantic_ai import Agent, NativeOutput, PromptedOutput, UsageLimits
from pydantic_ai.exceptions import (
    ContentFilterError,
    ModelAPIError,
    ModelHTTPError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider

from app.contracts.canonical import (
    CanonicalInferenceRequest,
    CanonicalInferenceResult,
    ModelUsage,
    TypedSchemaRegistry,
    derive_contract_meta,
    validate_canonical_inference_request,
)
from app.inference.ai_config import AIGatewayConfig
from app.inference.contracts import ProviderBindingManifest
from app.inference.errors import InferenceError, InferenceErrorCode
from app.inference.ledger import InvocationBudget, current_invocation_budget
from app.inference.schema_catalog import resolve_manifest_schema_binding
from app.inference.transport import LedgerTransport

InputT = TypeVar("InputT", bound=BaseModel)
ResultT = TypeVar("ResultT", bound=BaseModel)


class PydanticAIInferencePort:
    """Sealed implementation; callers cannot inject a Model or SDK client."""

    def __init__(
        self,
        *,
        manifest: ProviderBindingManifest,
        gateway_config: AIGatewayConfig,
        http_client: httpx.AsyncClient,
    ) -> None:
        if type(manifest) is not ProviderBindingManifest:
            raise TypeError("manifest must be an exact ProviderBindingManifest")
        if type(gateway_config) is not AIGatewayConfig:
            raise TypeError("gateway_config must be an exact AIGatewayConfig")
        if gateway_config.model != manifest.model_identifier:
            raise InferenceError(InferenceErrorCode.INVALID_BINDING)
        if gateway_config.credential_slot_id != manifest.credential_slot_id:
            raise InferenceError(InferenceErrorCode.INVALID_BINDING)
        self._manifest = manifest
        self._http_client = http_client
        self._input_model, self._output_model = resolve_manifest_schema_binding(manifest)
        request_type: Any = CanonicalInferenceRequest.__class_getitem__(self._input_model)
        self._request_type = request_type
        registry = TypedSchemaRegistry()
        registry.register(manifest.input_schema_ref, self._input_model, role="input")
        registry.register(manifest.output_schema_ref, self._output_model, role="output")
        self._schema_registry = registry
        sdk_client = AsyncOpenAI(
            api_key=gateway_config.api_key.get_secret_value(),
            admin_api_key="",
            organization="",
            project="",
            webhook_secret="",
            base_url=gateway_config.url,
            max_retries=manifest.sdk_max_retries,
            http_client=cast(Any, http_client),
        )
        sdk_client.admin_api_key = None
        sdk_client.organization = None
        sdk_client.project = None
        sdk_client.webhook_secret = None
        provider = OpenAIProvider(openai_client=sdk_client)
        profile = manifest.provider_profile
        self._model = OpenAIChatModel(
            manifest.model_identifier,
            provider=provider,
            profile=lambda _: OpenAIModelProfile(
                supports_tools=profile.supports_tools,
                supports_json_schema_output=profile.supports_json_schema_output,
                supports_json_object_output=profile.supports_json_object_output,
                default_structured_output_mode=profile.default_structured_output_mode,
                openai_chat_supports_max_completion_tokens=profile.openai_chat_supports_max_completion_tokens,
            ),
        )
        self._last_budget: InvocationBudget | None = None

    @classmethod
    def _compose(
        cls,
        *,
        manifest: ProviderBindingManifest,
        gateway_config: AIGatewayConfig,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> PydanticAIInferencePort:
        delegate = transport if transport is not None else httpx.AsyncHTTPTransport(retries=0, trust_env=False)
        http_client = httpx.AsyncClient(transport=LedgerTransport(delegate), timeout=30.0)
        return cls(
            manifest=manifest,
            gateway_config=gateway_config,
            http_client=http_client,
        )

    @property
    def physical_sends(self) -> int:
        return 0 if self._last_budget is None else self._last_budget.physical_sends

    async def aclose(self) -> None:
        await self._http_client.aclose()

    async def infer(
        self,
        request: CanonicalInferenceRequest[InputT],
        *,
        result_type: type[ResultT],
    ) -> CanonicalInferenceResult[ResultT]:
        request = self._validate_invocation(request, result_type)
        policy = self._manifest.pricing_policy
        max_attempts = 1 + request.retry_policy.max_provider_retries + request.retry_policy.max_schema_retries
        budget = InvocationBudget(
            max_attempts=max_attempts,
            max_tokens=request.budget.max_tokens,
            deadline_ms=request.budget.deadline_ms,
            max_cost_micros=request.budget.max_cost_micros,
            base_cost_micros=policy.base_cost_micros,
            input_micros_per_million=policy.input_micros_per_million,
            output_micros_per_million=policy.output_micros_per_million,
        )
        self._last_budget = budget
        token = current_invocation_budget.set(budget)
        try:
            run_result, run_calls = await self._run_with_provider_retries(request, result_type, budget)
            result = run_result.output
            if type(result) is not result_type:
                raise InferenceError(InferenceErrorCode.INVALID_RESULT)
            envelope_type: Any = CanonicalInferenceResult.__class_getitem__(result_type)
            return cast(
                CanonicalInferenceResult[ResultT],
                envelope_type(
                    meta=derive_contract_meta(
                        request.meta,
                        contract_name="canonical.inference.result",
                        causation_id=request.meta.message_id,
                    ),
                    inference_request_id=request.inference_request_id,
                    result=result,
                    model_ref=self._manifest.model_identifier,
                    usage=ModelUsage(
                        input_tokens=budget.input_tokens,
                        output_tokens=budget.output_tokens,
                        cost_micros=budget.total_cost_micros,
                    ),
                    provider_attempts=budget.physical_sends,
                    schema_retries=max(0, budget.physical_sends - run_calls),
                ),
            )
        except asyncio.CancelledError:
            raise
        finally:
            current_invocation_budget.reset(token)

    def _validate_invocation(
        self,
        request: CanonicalInferenceRequest[InputT],
        result_type: type[ResultT],
    ) -> CanonicalInferenceRequest[InputT]:
        try:
            if type(request) is not self._request_type or result_type is not self._output_model:
                raise TypeError("untrusted inference types")
            decoded = validate_canonical_inference_request(
                request,
                input_schema_ref=self._manifest.input_schema_ref,
                registry=self._schema_registry,
            )
        except Exception:
            raise InferenceError(InferenceErrorCode.POLICY_REJECTED) from None
        if (
            type(decoded.request.input) is not self._input_model
            or decoded.request.model_policy != self._manifest.model_policy
            or decoded.request.retry_policy != self._manifest.retry_policy
            or decoded.request.budget.max_tokens > self._manifest.budget_policy.max_tokens
            or decoded.request.budget.max_cost_micros > self._manifest.budget_policy.max_cost_micros
            or decoded.request.budget.deadline_ms > self._manifest.budget_policy.deadline_ms
            or decoded.request.result_schema_ref != self._manifest.output_schema_ref.ref
            or decoded.request.prompt_policy_ref != self._manifest.prompt_policy_ref.ref
            or decoded.request.model_policy_ref != self._manifest.model_policy_ref.ref
            or decoded.request.inference_retry_policy_ref != self._manifest.retry_policy_ref.ref
            or decoded.request.budget_policy_ref != self._manifest.budget_policy_ref.ref
        ):
            raise InferenceError(InferenceErrorCode.POLICY_REJECTED)
        return cast(CanonicalInferenceRequest[InputT], decoded.request)

    async def _run_with_provider_retries(
        self,
        request: CanonicalInferenceRequest[InputT],
        result_type: type[ResultT],
        budget: InvocationBudget,
    ) -> tuple[Any, int]:
        history, prompt = self._provider_messages(request)
        profile = self._manifest.provider_profile
        output_type = (
            NativeOutput(result_type, strict=True)
            if profile.default_structured_output_mode == "native"
            else PromptedOutput(result_type)
        )
        agent: Agent[None, ResultT] = Agent(
            self._model,
            output_type=output_type,
            retries=request.retry_policy.max_schema_retries,
            model_settings=OpenAIChatModelSettings(
                temperature=request.model_policy.temperature,
                max_tokens=request.model_policy.max_output_tokens,
                timeout=budget.remaining_seconds,
            ),
        )
        limits = UsageLimits(
            request_limit=1 + request.retry_policy.max_schema_retries,
            total_tokens_limit=request.budget.max_tokens,
        )
        run_calls = 0
        for provider_attempt in range(request.retry_policy.max_provider_retries + 1):
            run_calls += 1
            try:
                # Deadline authority is the ledger: reserve_send rejects expired
                # invocations before every physical send, and model_settings bounds
                # each send with the remaining httpx timeout. A wall-clock window
                # around agent.run would bill local validation/schema work to the
                # provider deadline.
                return await agent.run(prompt, message_history=history, usage_limits=limits), run_calls
            except asyncio.CancelledError:
                raise
            except ContentFilterError as exc:
                code = (
                    InferenceErrorCode.REFUSED
                    if _is_provider_refusal(exc.body)
                    else InferenceErrorCode.CONTENT_FILTERED
                )
                raise InferenceError(code) from None
            except UsageLimitExceeded:
                raise InferenceError(InferenceErrorCode.BUDGET_EXHAUSTED) from None
            except TimeoutError:
                raise InferenceError(InferenceErrorCode.DEADLINE_EXCEEDED) from None
            except ModelHTTPError as exc:
                transient = exc.status_code in {408, 409, 429} or 500 <= exc.status_code <= 599
                if not transient:
                    raise InferenceError(InferenceErrorCode.PROVIDER_PERMANENT) from None
                if provider_attempt >= request.retry_policy.max_provider_retries:
                    raise InferenceError(InferenceErrorCode.PROVIDER_TRANSIENT) from None
            except ModelAPIError as exc:
                stable_error = _caused_inference_error(exc)
                if stable_error is not None:
                    raise stable_error from None
                if provider_attempt >= request.retry_policy.max_provider_retries:
                    raise InferenceError(InferenceErrorCode.PROVIDER_TRANSIENT) from None
            except UnexpectedModelBehavior:
                raise InferenceError(InferenceErrorCode.INVALID_RESULT) from None
        raise InferenceError(InferenceErrorCode.PROVIDER_TRANSIENT)

    def _provider_messages(
        self,
        request: CanonicalInferenceRequest[InputT],
    ) -> tuple[list[ModelMessage], str]:
        """Map canonical instructions/context to provider messages without flattening roles."""

        history: list[ModelMessage] = []
        for item in request.instructions:
            content = item.content
            if item.content_schema_ref is not None:
                content = f"{content}\ngrove.content_schema_ref={item.content_schema_ref}"
            if item.role == "system":
                history.append(ModelRequest(parts=[SystemPromptPart(content=content)]))
            elif item.role == "assistant":
                history.append(ModelResponse(parts=[TextPart(content=content)]))
            else:
                history.append(ModelRequest(parts=[UserPromptPart(content=content)]))
        parts = [TypeAdapter(type(request.input)).dump_json(request.input).decode("utf-8")]
        parts.append(f"grove.input_schema_ref={self._manifest.input_schema_ref.ref}")
        if request.context is not None:
            parts.append(f"grove.context={request.context.model_dump_json(exclude_none=True)}")
        for artifact in request.context_refs:
            parts.append(f"grove.context_ref={artifact.model_dump_json()}")
        return history, "\n".join(parts)


def _caused_inference_error(error: BaseException) -> InferenceError | None:
    """Recover a stable ledger error wrapped by the provider SDK."""

    current: BaseException | None = error
    for _ in range(8):
        if current is None:
            return None
        if type(current) is InferenceError:
            return current
        cause = BaseException.__getattribute__(current, "__cause__")
        current = cause if isinstance(cause, BaseException) else None
    return None


def _is_provider_refusal(body: object) -> bool:
    """Recognize only PydanticAI's bounded structured refusal detail."""

    if type(body) is not str or len(body) > 1_000_000:
        return False
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return False
    if type(payload) is not list or len(payload) != 1 or type(payload[0]) is not dict:
        return False
    details = payload[0].get("provider_details")
    if type(details) is not dict:
        return False
    refusal = details.get("refusal")
    return type(refusal) is str and 0 < len(refusal) <= 100_000
