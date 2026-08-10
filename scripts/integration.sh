#!/usr/bin/env bash
set -euo pipefail

project="${COMPOSE_PROJECT_NAME:-grove-ws0-test}"
cleanroom_remove_volumes="${CLEANROOM_REMOVE_VOLUMES:-0}"
compose=(docker compose -p "$project" -f compose.yaml)
api_response=""
lock_holder_pid=""

if [[ "$cleanroom_remove_volumes" != "0" && "$cleanroom_remove_volumes" != "1" ]]; then
  printf 'CLEANROOM_REMOVE_VOLUMES must be 0 or 1\n' >&2
  exit 1
fi

cleanup() {
  if [[ -n "$lock_holder_pid" ]]; then
    kill "$lock_holder_pid" >/dev/null 2>&1 || true
    wait "$lock_holder_pid" >/dev/null 2>&1 || true
    lock_holder_pid=""
  fi
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

# The official image can report a healthy probe while its entrypoint is still
# handing off from temporary initialization.  Wait for PID 1 to be the real
# PostgreSQL server before accepting SQL stability as readiness evidence.
db_pid1_comm=""
for _attempt in $(seq 1 60); do
  db_pid1_comm="$("${compose[@]}" exec -T db sh -c 'cat /proc/1/comm' 2>/dev/null | tr -d '\r' || true)"
  if [[ "$db_pid1_comm" == "postgres" ]]; then
    break
  fi
  sleep 1
done
if [[ "$db_pid1_comm" != "postgres" ]]; then
  printf 'PostgreSQL PID 1 did not become postgres within the bounded retry window: %s\n' "$db_pid1_comm" >&2
  exit 1
fi

# Healthcheck/pg_isready can succeed while an entrypoint restart still has the
# database in a transient state. Require bounded SQL success for three
# consecutive probes and reset the counter after any failure.
stable_sql_probes=0
for _attempt in $(seq 1 60); do
  sql_probe="$("${compose[@]}" exec -T db env PGCONNECT_TIMEOUT=2 psql -U grove -d grove -Atqc 'SELECT 1' 2>/dev/null || true)"
  if [[ "$sql_probe" == "1" ]]; then
    stable_sql_probes=$((stable_sql_probes + 1))
    if [[ "$stable_sql_probes" -ge 3 ]]; then
      break
    fi
  else
    stable_sql_probes=0
  fi
  sleep 1
done
if [[ "$stable_sql_probes" -lt 3 ]]; then
  printf 'PostgreSQL SQL probe did not remain stable within the bounded retry window\n' >&2
  exit 1
fi

# The named volume may predate the init hook, so apply the idempotent role
# bootstrap explicitly as well. The application roles never use grove. A
# transient database shutdown resets the bounded retry, rather than turning a
# readiness race into a false success.
bootstrap_timeout_seconds=10
bootstrap_pgoptions='-c statement_timeout=5000 -c lock_timeout=1000'
run_bootstrap() {
  "${compose[@]}" exec -T db timeout "${bootstrap_timeout_seconds}s" env \
    PGCONNECT_TIMEOUT=5 PGOPTIONS="$bootstrap_pgoptions" \
    psql -X -v ON_ERROR_STOP=1 -U grove -d grove
}

bootstrap_ok=0
for _attempt in $(seq 1 60); do
  if run_bootstrap < scripts/postgres-init.sql; then
    bootstrap_ok=1
    break
  fi
  sleep 1
done
if [[ "$bootstrap_ok" != "1" ]]; then
  printf 'PostgreSQL role bootstrap did not complete within the bounded retry window\n' >&2
  exit 1
fi
# Exercise the same bootstrap path twice on the live volume; role grants and
# ownership changes must remain idempotent across repeated invocations. Retry
# the second real command independently as well.
bootstrap_ok=0
for _attempt in $(seq 1 60); do
  if run_bootstrap < scripts/postgres-init.sql; then
    bootstrap_ok=1
    break
  fi
  sleep 1
done
if [[ "$bootstrap_ok" != "1" ]]; then
  printf 'PostgreSQL repeated role bootstrap did not complete within the bounded retry window\n' >&2
  exit 1
fi

mkdir -p ci-evidence
# The migration_report runs inside the container as a non-root grove user.
# Host-generated ci-evidence files are owned by the host user; make the
# entire evidence tree world-writable so the container can write CAS
# artifacts, temp files, and subdirectories.
chmod -R a+rwX ci-evidence
migration_ok=0
for _attempt in $(seq 1 60); do
  if "${compose[@]}" run --rm \
    -e GROVE_DATABASE_URL=postgresql+psycopg://grove_migration:grove_migration_ws0@db:5432/grove \
    -e PGCONNECT_TIMEOUT=10 \
    -e PGOPTIONS='-c statement_timeout=60000 -c lock_timeout=30000' \
    api sh -c 'umask 0000 && python scripts/migration_report.py --output /app/ci-evidence/migrations.json'; then
    migration_ok=1
    break
  fi
  sleep 1
done
if [[ "$migration_ok" != "1" ]]; then
  printf 'migration report did not complete within the bounded retry window\n' >&2
  exit 1
fi

# Fault probes prove that the bootstrap command itself fails closed.  First an
# injected SQL error must stop the script with ON_ERROR_STOP; then an
# ACCESS-EXCLUSIVE lock on the object touched by the bootstrap must hit
# lock_timeout (and the outer coreutils timeout is a second fixed upper bound).
if printf '%s\n' 'SELECT 1;' 'SELECT grove_bootstrap_injected_error;' | run_bootstrap; then
  printf 'bootstrap error probe unexpectedly succeeded\n' >&2
  exit 1
fi

lock_holder_status=0
"${compose[@]}" exec -T db timeout 15s env PGCONNECT_TIMEOUT=5 \
  psql -X -v ON_ERROR_STOP=1 -U grove -d grove \
  -c 'BEGIN; LOCK TABLE public.alembic_version IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep(5); COMMIT;' &
lock_holder_pid=$!
lock_acquired=0
for _attempt in $(seq 1 20); do
  lock_state="$("${compose[@]}" exec -T db env PGCONNECT_TIMEOUT=5 psql -X -U grove -d grove -Atqc \
    "SELECT 1 FROM pg_locks l JOIN pg_class c ON c.oid = l.relation WHERE c.relname = 'alembic_version' AND l.mode = 'AccessExclusiveLock' AND l.granted" \
    2>/dev/null || true)"
  if [[ "$lock_state" == "1" ]]; then
    lock_acquired=1
    break
  fi
  sleep 0.1
done
if [[ "$lock_acquired" != "1" ]]; then
  printf 'bootstrap lock holder did not acquire the required lock\n' >&2
  kill "$lock_holder_pid" >/dev/null 2>&1 || true
  wait "$lock_holder_pid" >/dev/null 2>&1 || true
  lock_holder_pid=""
  exit 1
fi
if run_bootstrap < scripts/postgres-init.sql; then
  printf 'bootstrap lock probe unexpectedly succeeded\n' >&2
  kill "$lock_holder_pid" >/dev/null 2>&1 || true
  wait "$lock_holder_pid" >/dev/null 2>&1 || true
  lock_holder_pid=""
  exit 1
fi
wait "$lock_holder_pid" || lock_holder_status=$?
lock_holder_pid=""
if [[ "$lock_holder_status" != "0" ]]; then
  printf 'bootstrap lock holder failed with exit=%s\n' "$lock_holder_status" >&2
  exit 1
fi

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

expected_head="$(uv run alembic heads 2>/dev/null | tail -1 | awk '{print $1}')"
if [[ -z "$expected_head" ]]; then
  printf 'could not resolve alembic head for migration verification\n' >&2
  exit 1
fi
"${compose[@]}" exec -T db psql -U grove_migration -d grove -Atc "SELECT version_num FROM alembic_version" | grep -Fx "$expected_head"
GROVE_DATABASE_URL=postgresql+psycopg://grove_api:grove_api_ws0@localhost:54329/grove \
GROVE_MIGRATION_DATABASE_URL=postgresql+psycopg://grove_migration:grove_migration_ws0@localhost:54329/grove \
GROVE_API_BASE_URL=http://127.0.0.1:8000 \
uv run pytest tests/integration -m integration -ra

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
