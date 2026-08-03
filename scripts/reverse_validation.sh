#!/usr/bin/env bash
set -euo pipefail

manifest="ci-evidence/runtime-build-manifest.json"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

role_output="$tmp_dir/role.txt"
if GROVE_ROLE=invalid_role uv run python -m app.main --self-check >"$role_output" 2>&1; then
  printf 'illegal role unexpectedly succeeded\n' >&2
  exit 1
else
  role_status=$?
fi
if [[ "$role_status" == "0" ]]; then
  printf 'illegal role returned zero\n' >&2
  exit 1
fi
printf 'illegal role rejected with exit=%s\n' "$role_status"

tampered="$tmp_dir/tampered.json"
sed 's/"schema_version":1/"schema_version":999/' "$manifest" >"$tampered"
if uv run python scripts/build_manifest.py --verify "$tampered"; then
  printf 'tampered manifest unexpectedly succeeded\n' >&2
  exit 1
else
  tampered_status=$?
fi
if [[ "$tampered_status" == "0" ]]; then
  printf 'tampered manifest returned zero\n' >&2
  exit 1
fi
printf 'tampered manifest rejected with exit=%s\n' "$tampered_status"

sbom_path="$(uv run python -c 'import json, sys; print(json.load(open(sys.argv[1]))["sbom_ref"])' "$manifest")"
migration_path="$(uv run python -c 'import json, sys; print(json.load(open(sys.argv[1]))["migration_report_ref"])' "$manifest")"
active_sbom_hash="$(uv run python -c 'import hashlib, sys; print(hashlib.file_digest(open(sys.argv[1], "rb"), "sha256").hexdigest())' "$sbom_path")"
evidence_root="$tmp_dir/evidence-root"
temporary_manifest="$tmp_dir/runtime-build-manifest.json"
cp "$manifest" "$temporary_manifest"
for evidence_path in "$sbom_path" "$migration_path"; do
  mkdir -p "$evidence_root/$(dirname "$evidence_path")"
  cp "$evidence_path" "$evidence_root/$evidence_path"
done
printf '{"tampered":true}\n' >"$evidence_root/$sbom_path"
if uv run python scripts/build_manifest.py --root "$evidence_root" --verify "$temporary_manifest"; then
  printf 'tampered SBOM unexpectedly succeeded\n' >&2
  exit 1
else
  sbom_status=$?
fi
if [[ "$sbom_status" == "0" ]]; then
  printf 'tampered SBOM returned zero\n' >&2
  exit 1
fi
printf 'tampered SBOM rejected with exit=%s\n' "$sbom_status"
current_sbom_hash="$(uv run python -c 'import hashlib, sys; print(hashlib.file_digest(open(sys.argv[1], "rb"), "sha256").hexdigest())' "$sbom_path")"
if [[ "$current_sbom_hash" != "$active_sbom_hash" ]]; then
  printf 'active SBOM changed during reverse validation\n' >&2
  exit 1
fi
printf 'active SBOM remained unchanged\n'

uv run python scripts/build_manifest.py --verify "$manifest"
