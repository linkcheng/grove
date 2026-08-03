from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.dependency_report import generate_runtime_sbom


def test_runtime_sbom_is_cyclonedx_runtime_only_and_deterministic(tmp_path: Path) -> None:
    output = tmp_path / "ci-evidence" / "runtime-sbom.cdx.json"
    generate_runtime_sbom(Path.cwd(), output)
    second_output = tmp_path / "runtime-sbom.second.cdx.json"
    generate_runtime_sbom(Path.cwd(), second_output)

    payload = json.loads(output.read_text())
    assert payload["bomFormat"] == "CycloneDX"
    assert payload["specVersion"] == "1.5"
    assert "serialNumber" not in payload
    assert "timestamp" not in payload["metadata"]
    names = {component["name"] for component in payload["components"]}
    assert {"langgraph", "pydantic-ai-slim", "fastapi"} <= names
    assert "pytest" not in names
    expected = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    assert output.read_bytes() == expected.encode()
    assert output.read_bytes() == second_output.read_bytes()
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    assert (output.parent / "sha256" / digest / output.name).read_bytes() == output.read_bytes()
