"""Stable production-inference failure contract."""

from __future__ import annotations

from enum import StrEnum


class InferenceErrorCode(StrEnum):
    INVALID_BINDING = "invalid_binding"
    POLICY_REJECTED = "policy_rejected"
    BUDGET_EXHAUSTED = "budget_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    PROVIDER_TRANSIENT = "provider_transient"
    PROVIDER_PERMANENT = "provider_permanent"
    CONTENT_FILTERED = "content_filtered"
    REFUSED = "refused"
    INVALID_RESULT = "invalid_result"


class InferenceError(RuntimeError):
    """Input-independent error raised by the production inference boundary."""

    def __init__(self, code: InferenceErrorCode) -> None:
        if type(code) is not InferenceErrorCode:
            raise TypeError("code must be an exact InferenceErrorCode")
        self.code = code
        super().__init__(code.value)
