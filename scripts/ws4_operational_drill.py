#!/usr/bin/env python3
"""Fail-closed static drill for WS-4 Collector/dashboard/alert/runbook assets."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

_FORBIDDEN = frozenset(
    {"tenant_id", "principal_id", "run_id", "command_id", "execution_fence", "authorization", "payload"}
)


class OperationalDrillError(RuntimeError):
    """Raised when the WS-4 operational closure is incomplete or unsafe."""


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise OperationalDrillError(f"{path} must contain an object")
    return value


def validate_assets(root: Path) -> dict[str, Any]:
    collector = _load(root / "ops/otel-collector.yaml")
    processors = collector.get("processors", {})
    for required in ("memory_limiter", "attributes/redact", "batch"):
        if required not in processors:
            raise OperationalDrillError(f"collector missing processor: {required}")
    redacted = {
        action.get("key")
        for action in processors["attributes/redact"].get("actions", [])
        if action.get("action") == "delete"
    }
    if not _FORBIDDEN.issubset(redacted):
        raise OperationalDrillError("collector redaction policy is incomplete")
    exporter = collector.get("exporters", {}).get("otlphttp/backend", {})
    if exporter.get("sending_queue", {}).get("enabled") is not True:
        raise OperationalDrillError("collector queue must be enabled")
    if exporter.get("retry_on_failure", {}).get("max_elapsed_time") != "30s":
        raise OperationalDrillError("collector retry must have a 30s total bound")

    dashboards = [_load(path) for path in sorted((root / "ops/dashboards").glob("ws4-*.json"))]
    if len(dashboards) != 4 or len({item.get("id") for item in dashboards}) != 4:
        raise OperationalDrillError("exactly four unique WS-4 dashboards are required")
    if any(not item.get("owner") or not item.get("slo") or not item.get("runbook") for item in dashboards):
        raise OperationalDrillError("each dashboard requires owner, SLO and runbook")
    if any(len(item.get("panels", [])) < 3 for item in dashboards):
        raise OperationalDrillError("each dashboard requires at least three actionable panels")

    alerts = _load(root / "ops/alerts/ws4-observation.yaml")
    rules = alerts.get("groups", [{}])[0].get("rules", [])
    if len(rules) < 4:
        raise OperationalDrillError("WS-4 alerts are incomplete")
    if any(not rule.get("labels", {}).get("owner") or not rule.get("annotations", {}).get("runbook") for rule in rules):
        raise OperationalDrillError("each alert requires owner and runbook")

    runbook = (root / "docs/runbooks/ws4-observation.md").read_text(encoding="utf-8")
    if "禁止直接查询生产数据库" not in runbook or "/inspect" not in runbook:
        raise OperationalDrillError("runbook must use safe Run Inspect without production SQL")
    target = _load(root / "ops/ws4-reference-target.json")
    capacity_probe = (root / "scripts/ws4_capacity_probe.py").read_text(encoding="utf-8")
    if "WS4_TARGET_CAPACITY_CHECK" not in capacity_probe or "reference_target_v1" not in capacity_probe:
        raise OperationalDrillError("reference-target capacity probe is missing or not opt-in")
    return {
        "status": "PASS",
        "dashboards": len(dashboards),
        "alerts": len(rules),
        "collector_queue_size": exporter["sending_queue"]["queue_size"],
        "reference_target": target,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    try:
        result = validate_assets(args.root.resolve())
    except (OSError, ValueError, OperationalDrillError) as exc:
        print(json.dumps({"status": "FAIL", "reason": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
