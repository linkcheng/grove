"""WS-4 operational asset closure tests."""

from pathlib import Path

import pytest
from scripts.ws4_capacity_probe import CapacityProbeError, _load_profile
from scripts.ws4_operational_drill import OperationalDrillError, validate_assets


def test_operational_assets_form_actionable_closed_set() -> None:
    result = validate_assets(Path.cwd())
    assert result["status"] == "PASS"
    assert result["dashboards"] == 4
    assert result["alerts"] >= 4
    assert result["collector_queue_size"] == 2048


def test_operational_drill_rejects_missing_assets(tmp_path: Path) -> None:
    with pytest.raises((OSError, OperationalDrillError)):
        validate_assets(tmp_path)


def test_capacity_target_is_exact_and_requires_explicit_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WS4_TARGET_CAPACITY_CHECK", raising=False)
    with pytest.raises(CapacityProbeError, match="requires"):
        _load_profile(True)

    monkeypatch.setenv("WS4_TARGET_CAPACITY_CHECK", "1")
    profile = _load_profile(True)
    assert profile.mode == "reference_target_v1"
    assert profile.event_rate == 200
    assert profile.sse_connections == 500
    assert profile.telemetry_outage_seconds == 900
    assert profile.max_event_bytes == 65_536
