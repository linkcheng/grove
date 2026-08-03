#!/usr/bin/env python3
"""Write a deterministic CycloneDX SBOM for the runtime dependency graph."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from app.build.manifest import write_content_addressed_artifact


def _canonicalize(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove uv's generated identity fields while preserving CycloneDX structure."""

    payload.pop("serialNumber", None)
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        metadata.pop("timestamp", None)
    return payload


def generate_runtime_sbom(root: Path, output: Path) -> None:
    """Export only locked runtime dependencies as canonical CycloneDX JSON."""

    uv_path = shutil.which("uv")
    if uv_path is None:
        raise ValueError("uv executable is required for runtime SBOM generation")
    with tempfile.TemporaryDirectory(prefix="grove-sbom-") as temporary_directory:
        raw_output = Path(temporary_directory) / "runtime-sbom.json"
        subprocess.run(  # noqa: S603
            [
                uv_path,
                "export",
                "--preview-features",
                "sbom-export",
                "--frozen",
                "--no-dev",
                "--format",
                "cyclonedx1.5",
                "--no-annotate",
                "--output-file",
                str(raw_output),
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(raw_output.read_text())
    if not isinstance(payload, dict) or payload.get("bomFormat") != "CycloneDX" or payload.get("specVersion") != "1.5":
        raise ValueError("uv did not produce a CycloneDX 1.5 SBOM")
    canonical = json.dumps(_canonicalize(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    write_content_addressed_artifact(output, canonical.encode())


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    try:
        generate_runtime_sbom(root, root / "ci-evidence" / "runtime-sbom.cdx.json")
    except (OSError, ValueError, subprocess.CalledProcessError):
        print("runtime SBOM generation failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
