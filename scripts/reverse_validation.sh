#!/usr/bin/env bash
set -euo pipefail

manifest="${GROVE_REVERSE_MANIFEST:-ci-evidence/runtime-build-manifest.json}"
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
generated_evidence=()
if [[ "$sbom_path" != "not_generated" ]]; then
  generated_evidence+=("SBOM:$sbom_path")
fi
if [[ "$migration_path" != "not_generated" ]]; then
  generated_evidence+=("migration report:$migration_path")
fi
if (( ${#generated_evidence[@]} == 0 )); then
  printf 'evidence reverse validation skipped: draft manifest has no generated evidence\n'
else
  evidence_root="$tmp_dir/evidence-root"
  temporary_manifest="$tmp_dir/runtime-build-manifest.json"
  cp "$manifest" "$temporary_manifest"
  for current in "${generated_evidence[@]}"; do
    # Every iteration starts with a clean copy of all generated evidence so a
    # prior tamper cannot mask the next artifact's rejection.
    rm -rf "$evidence_root"
    mkdir -p "$evidence_root"
    for evidence in "${generated_evidence[@]}"; do
      evidence_path="${evidence#*:}"
      mkdir -p "$evidence_root/$(dirname "$evidence_path")"
      cp "$evidence_path" "$evidence_root/$evidence_path"
    done
    label="${current%%:*}"
    tampered="${current#*:}"
    active_hash="$(uv run python -c 'import hashlib, sys; print(hashlib.file_digest(open(sys.argv[1], "rb"), "sha256").hexdigest())' "$tampered")"
    printf '{"tampered":true}\n' >"$evidence_root/$tampered"
    if uv run python scripts/build_manifest.py --root "$evidence_root" --verify "$temporary_manifest"; then
      printf 'tampered %s unexpectedly succeeded\n' "$label" >&2
      exit 1
    else
      tampered_status=$?
    fi
    if [[ "$tampered_status" == "0" ]]; then
      printf 'tampered %s returned zero\n' "$label" >&2
      exit 1
    fi
    printf 'tampered %s rejected with exit=%s\n' "$label" "$tampered_status"
    current_hash="$(uv run python -c 'import hashlib, sys; print(hashlib.file_digest(open(sys.argv[1], "rb"), "sha256").hexdigest())' "$tampered")"
    if [[ "$current_hash" != "$active_hash" ]]; then
      printf 'active %s changed during reverse validation\n' "$label" >&2
      exit 1
    fi
    printf 'active %s remained unchanged\n' "$label"
  done
fi

uv run python scripts/build_manifest.py --verify "$manifest"
