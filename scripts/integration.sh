#!/usr/bin/env bash
set -euo pipefail

project="${COMPOSE_PROJECT_NAME:-grove-ws0-test}"
cleanroom_remove_volumes="${CLEANROOM_REMOVE_VOLUMES:-0}"
compose=(docker compose -p "$project" -f compose.yaml)
api_response=""

if [[ "$cleanroom_remove_volumes" != "0" && "$cleanroom_remove_volumes" != "1" ]]; then
  printf 'CLEANROOM_REMOVE_VOLUMES must be 0 or 1\n' >&2
  exit 1
fi

cleanup() {
  if [[ -n "$api_response" ]]; then
    rm -f "$api_response"
  fi
  local down_args=(down --remove-orphans)
  if [[ "$cleanroom_remove_volumes" == "1" ]]; then
    down_args+=(--volumes)
  fi
  if ! "${compose[@]}" "${down_args[@]}"; then
    printf 'integration cleanup could not remove compose resources\n' >&2
  fi
}
if [[ "${GROVE_INTEGRATION_LIBRARY:-0}" != "1" ]]; then
  trap cleanup EXIT
fi

runtime_tree_digest() (
  # Docker daemon image IDs include build metadata such as CreatedAt. Compare
  # a canonical runtime filesystem *and* the image's runtime configuration so
  # two builds cannot pass while differing in USER/CMD/ENTRYPOINT/ENV/WORKDIR.
  local image="$1"
  local scratch container_id
  scratch="$(mktemp -d)"
  container_id=""
  cleanup_digest() {
    if [[ -n "$container_id" ]] && docker container inspect "$container_id" >/dev/null 2>&1; then
      docker rm -f "$container_id" >/dev/null
    fi
    rm -rf "$scratch"
  }
  trap cleanup_digest EXIT
  docker image inspect "$image" --format '{{json .Config}}' >"$scratch/config.json"
  container_id="$(docker create "$image")"
  docker export "$container_id" >"$scratch/runtime.tar"
  docker rm "$container_id" >/dev/null
  container_id=""
  local digest
  if ! digest="$(uv run python - "$scratch/runtime.tar" "$scratch/config.json" <<'PY'
import hashlib
import json
import pathlib
import tarfile
import sys

runtime_tar = pathlib.Path(sys.argv[1])
config = json.loads(pathlib.Path(sys.argv[2]).read_text())
required = ("User", "Cmd", "Entrypoint", "Env", "WorkingDir")
runtime_fields = {name: config.get(name) for name in required}

# docker export excludes the container bind-mounted pseudo filesystems.  Do
# not discard /run: image layers may contain static runtime files there.
dynamic_mounts = ("dev", "proc", "sys")
dynamic_files = {"etc/hostname", "etc/hosts", "etc/resolv.conf"}
filesystem = []
with tarfile.open(runtime_tar, mode="r:") as archive:
    for member in archive:
        name = member.name.lstrip("/")
        if name.startswith("./"):
            name = name[2:]
        if name in dynamic_files or name.split("/", 1)[0] in dynamic_mounts:
            continue
        entry = {
            "gid": member.gid,
            "linkname": member.linkname,
            "mode": member.mode,
            "name": name,
            "size": member.size,
            "type": member.type.decode("ascii", errors="replace"),
            "uid": member.uid,
        }
        if member.isreg():
            content = archive.extractfile(member)
            if content is None:
                raise SystemExit(f"cannot read runtime file: {name}")
            entry["sha256"] = hashlib.file_digest(content, "sha256").hexdigest()
        filesystem.append(entry)
filesystem.sort(key=lambda item: item["name"])

digest = hashlib.sha256()
digest.update(
    json.dumps(
        {"config": config, "filesystem": filesystem, "runtime_fields": runtime_fields},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
)
print(digest.hexdigest())
PY
  )"; then
    return 1
  fi
  printf '%s\n' "$digest"
)

if [[ "${GROVE_INTEGRATION_LIBRARY:-0}" != "1" ]]; then
"${compose[@]}" build --no-cache
first_build_image_id="$(docker image inspect grove-ws0:local --format '{{.Id}}')"
first_runtime_tree_digest="$(runtime_tree_digest grove-ws0:local)"
"${compose[@]}" build --no-cache
second_build_image_id="$(docker image inspect grove-ws0:local --format '{{.Id}}')"
second_runtime_tree_digest="$(runtime_tree_digest grove-ws0:local)"
if [[ "$first_runtime_tree_digest" != "$second_runtime_tree_digest" ]]; then
  printf 'independent application runtime trees produced different digests: %s != %s\n' \
    "$first_runtime_tree_digest" "$second_runtime_tree_digest" >&2
  exit 1
fi
printf 'independent application image IDs: %s -> %s\n' "$first_build_image_id" "$second_build_image_id"
printf 'independent application runtime tree digests match: %s\n' "$second_runtime_tree_digest"
"${compose[@]}" up -d db
db_container="$("${compose[@]}" ps -q db)"
for _attempt in $(seq 1 60); do
  health="$(docker inspect --format '{{.State.Health.Status}}' "$db_container")"
  if [[ "$health" == "healthy" ]]; then
    break
  fi
  sleep 1
done
health="$(docker inspect --format '{{.State.Health.Status}}' "$db_container")"
if [[ "$health" != "healthy" ]]; then
  printf 'PostgreSQL healthcheck did not become healthy: %s\n' "$health" >&2
  exit 1
fi
"${compose[@]}" exec -T db pg_isready -U grove -d grove

# The named volume may predate the init hook, so apply the idempotent role
# bootstrap explicitly as well. The application roles never use grove.
"${compose[@]}" exec -T db psql -U grove -d grove < scripts/postgres-init.sql

mkdir -p ci-evidence
"${compose[@]}" run --rm \
  -e GROVE_DATABASE_URL=postgresql+psycopg://grove_migration:grove_migration_ws0@db:5432/grove \
  api python scripts/migration_report.py --output /app/ci-evidence/migrations.json

for role_service in runtime-worker projection-reconciliation offline-governance; do
  role_output="$("${compose[@]}" run --rm "$role_service")"
  printf '%s\n' "$role_output"
  grep -F '"database_status": "connected"' <<<"$role_output"
done

"${compose[@]}" up -d api
api_response="$(mktemp)"
api_ready=0
for _attempt in $(seq 1 60); do
  if curl --fail --silent -H 'X-Request-ID: integration_trace' http://127.0.0.1:8000/api/v1/health/live -o "$api_response"; then
    api_ready=1
    break
  fi
  sleep 1
done
if [[ "$api_ready" != "1" ]]; then
  printf 'API did not become ready in time\n' >&2
  exit 1
fi
curl --fail --silent --show-error -H 'X-Request-ID: integration_trace' http://127.0.0.1:8000/api/v1/health/live
printf '\n'
curl --fail --silent --show-error -H 'X-Request-ID: integration_ready' http://127.0.0.1:8000/api/v1/health/ready
printf '\n'

"${compose[@]}" exec -T db psql -U grove_migration -d grove -Atc "SELECT version_num FROM alembic_version" | grep -Fx baseline
GROVE_DATABASE_URL=postgresql+psycopg://grove_migration:grove_migration_ws0@localhost:54329/grove uv run pytest tests/integration -m integration -ra

app_image_id="$(docker image inspect grove-ws0:local --format '{{.Id}}')"
postgres_image_id="$(docker image inspect pgvector-postgis:pg16 --format '{{.Id}}')"
GROVE_APP_IMAGE_ID="$app_image_id" GROVE_POSTGRES_IMAGE_ID="$postgres_image_id" make manifest-check
manifest_dirty="$(uv run python -c 'import json, sys; print(str(json.load(open(sys.argv[1]))["source"]["dirty"]).lower())' ci-evidence/runtime-build-manifest.json)"
case "$manifest_dirty" in
  false)
    GROVE_APP_IMAGE_ID="$app_image_id" GROVE_POSTGRES_IMAGE_ID="$postgres_image_id" \
      uv run python scripts/build_manifest.py --verify ci-evidence/runtime-build-manifest.json --require-release
    ;;
  true)
    printf 'release verification deferred: source.dirty=true\n'
    ;;
  *)
    printf 'manifest source.dirty has invalid value: %s\n' "$manifest_dirty" >&2
    exit 1
    ;;
esac
rm -f "$api_response"
api_response=""
fi
