#!/usr/bin/env bash
set -euo pipefail

target="pgvector-postgis:pg16"
pgvector_source="pgvector/pgvector:pg16@sha256:a36250871de0833b8757561c72f2477ef1ddd1101afa4e617fb552e0de514c6b"
postgis_source="postgis/postgis:16-3.5@sha256:4be58fcb1b50df187e73536e663149c2b3b2da2a541c2f518cfb6adebc65ed91"
probe_container="grove-ws0-pg-capability-probe-$$"

cleanup_probe() {
  if docker container inspect "$probe_container" >/dev/null 2>&1; then
    docker rm -f "$probe_container" >/dev/null
  fi
}

trap cleanup_probe EXIT

probe_extensions() {
  local ready=0
  local database_ready=0
  local extension_rows
  if ! docker run --rm --name "$probe_container" \
    -e POSTGRES_DB=probe \
    -e POSTGRES_USER=postgres \
    -e POSTGRES_PASSWORD=probe \
    -d "$target" >/dev/null; then
    printf 'failed to start PostgreSQL capability probe container\n' >&2
    return 1
  fi
  for _attempt in $(seq 1 60); do
    if docker exec "$probe_container" pg_isready -U postgres -d template1 >/dev/null 2>&1; then
      ready=1
      if docker exec "$probe_container" psql -v ON_ERROR_STOP=1 -U postgres -d template1 -Atqc \
        "SELECT 1 FROM pg_database WHERE datname = 'probe'" | grep -Fxq 1; then
        database_ready=1
        break
      fi
    fi
    sleep 1
  done
  if [[ "$ready" != "1" ]]; then
    printf 'PostgreSQL capability probe server did not become ready\n' >&2
    return 1
  fi
  if [[ "$database_ready" != "1" ]]; then
    printf 'PostgreSQL capability probe database was not created\n' >&2
    return 1
  fi

  if ! docker exec "$probe_container" psql -v ON_ERROR_STOP=1 -U postgres -d probe -c \
    "CREATE EXTENSION postgis"; then
    printf 'CREATE EXTENSION postgis failed\n' >&2
    return 1
  fi
  if ! docker exec "$probe_container" psql -v ON_ERROR_STOP=1 -U postgres -d probe -c \
    "CREATE EXTENSION vector"; then
    printf 'CREATE EXTENSION vector failed\n' >&2
    return 1
  fi
  if ! extension_rows="$(docker exec "$probe_container" psql -v ON_ERROR_STOP=1 -U postgres -d probe -Atqc \
    "SELECT extname FROM pg_extension WHERE extname IN ('postgis', 'vector') ORDER BY extname")"; then
    printf 'extension capability query failed\n' >&2
    return 1
  fi
  if [[ "$extension_rows" != $'postgis\nvector' ]]; then
    printf 'extension capability query returned unexpected rows: %s\n' "$extension_rows" >&2
    return 1
  fi
  if ! docker stop "$probe_container" >/dev/null; then
    printf 'failed to stop PostgreSQL capability probe container\n' >&2
    return 1
  fi
  printf 'PostGIS/pgvector dynamic extension probe passed\n'
}

verify_target() {
  local vector_label postgis_label
  if ! vector_label="$(docker image inspect "$target" --format '{{index .Config.Labels "org.grove.pgvector-source"}}')"; then
    printf 'could not inspect pgvector source label\n' >&2
    return 1
  fi
  if ! postgis_label="$(docker image inspect "$target" --format '{{index .Config.Labels "org.grove.postgis-source"}}')"; then
    printf 'could not inspect PostGIS source label\n' >&2
    return 1
  fi
  if [[ "$vector_label" != "$pgvector_source" || "$postgis_label" != "$postgis_source" ]]; then
    printf 'PostgreSQL image source labels do not match pinned inputs\n' >&2
    return 1
  fi
  if ! docker run --rm "$target" sh -c \
    'test -f /usr/share/postgresql/16/extension/postgis.control && \
     test -f /usr/share/postgresql/16/extension/vector.control && \
     test -x /usr/lib/postgresql/16/lib/vector.so'; then
    printf 'PostGIS/pgvector files are missing from PostgreSQL image\n' >&2
    return 1
  fi
  if ! probe_extensions; then
    printf 'PostGIS/pgvector dynamic extension probe failed\n' >&2
    return 1
  fi
}

if docker image inspect "$target" >/dev/null 2>&1; then
  if verify_target; then
    docker image inspect "$target" --format 'using verified PostgreSQL test image {{.Id}}'
    exit 0
  fi
  printf 'existing PostgreSQL image failed source/capability verification; rebuilding\n' >&2
fi

# GitHub runners do not have the private/local composite image. Build the same
# PostGIS + pgvector capability set from public layers under the exact compose
# name instead of silently substituting a database with a different extension set.
docker build --pull \
  --file scripts/ci-postgres.Dockerfile \
  --tag "$target" \
  .
verify_target
docker image inspect "$target" --format 'prepared PostgreSQL test image {{.Id}}'
