from __future__ import annotations

import pytest
from app.schemas.response import ApiResponse
from pydantic import ValidationError


def test_response_message_and_trace_id_have_bounded_lengths() -> None:
    with pytest.raises(ValidationError):
        ApiResponse(code=0, message="", data=None)
    with pytest.raises(ValidationError):
        ApiResponse(code=0, message="x" * 257, data=None)
    with pytest.raises(ValidationError):
        ApiResponse(code=0, message="ok", data=None, trace_id="t" * 129)
