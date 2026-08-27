"""Unit gate for the POC-M capacity probe's closing-ceiling math."""

from __future__ import annotations

from typing import Any

import pytest
from scripts.ws6_capacity_probe import CapacityProbeError, closing_ceiling


def test_ceiling_is_min_candidate_times_safety_factor_floored() -> None:
    assert closing_ceiling({"rows": 1024, "bytes": 300, "context": 90, "deadline": 512}) == 72
    assert closing_ceiling({"rows": 10, "bytes": 10, "context": 10, "deadline": 10}) == 8


def test_ceiling_fails_closed_on_incomplete_candidates() -> None:
    with pytest.raises(CapacityProbeError, match="incomplete"):
        closing_ceiling({"rows": 10, "bytes": 10, "context": 10})


def test_ceiling_fails_closed_on_nonpositive_or_non_int_candidates() -> None:
    with pytest.raises(CapacityProbeError, match="positive int"):
        closing_ceiling({"rows": 1024, "bytes": 0, "context": 90, "deadline": 512})
    float_candidate: dict[str, Any] = {"rows": 1024, "bytes": 10.5, "context": 90, "deadline": 512}
    with pytest.raises(CapacityProbeError, match="positive int"):
        closing_ceiling(float_candidate)


def test_ceiling_never_collapses_below_one() -> None:
    with pytest.raises(CapacityProbeError, match="collapsed"):
        closing_ceiling({"rows": 1, "bytes": 1, "context": 1, "deadline": 1})
