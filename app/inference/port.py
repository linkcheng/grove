"""The only inference capability visible to Graph and Node adapters."""

from __future__ import annotations

from typing import Protocol, TypeVar

from pydantic import BaseModel

from app.contracts.canonical import CanonicalInferenceRequest, CanonicalInferenceResult

InputT = TypeVar("InputT", bound=BaseModel)
ResultT = TypeVar("ResultT", bound=BaseModel)


class TypedInferencePort(Protocol):
    async def infer(
        self,
        request: CanonicalInferenceRequest[InputT],
        *,
        result_type: type[ResultT],
    ) -> CanonicalInferenceResult[ResultT]: ...
