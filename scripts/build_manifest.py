#!/usr/bin/env python3
"""Generate or verify the canonical workspace RuntimeBuildManifest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.build.manifest import ManifestError, build_manifest_from_workspace, verify_manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify", type=Path)
    parser.add_argument("--require-release", action="store_true")
    args = parser.parse_args()
    try:
        if args.verify:
            manifest = json.loads(args.verify.read_text())
            verify_manifest(
                manifest,
                root=args.root.resolve(),
                raise_on_error=True,
                require_release=args.require_release,
            )
            print("manifest verification: valid")
            return 0
        manifest = build_manifest_from_workspace(args.root.resolve())
        payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload)
        else:
            sys.stdout.write(payload)
        return 0
    except (ManifestError, OSError, ValueError) as exc:
        print(f"manifest error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
