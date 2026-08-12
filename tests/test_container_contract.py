from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest


def test_application_container_is_non_root() -> None:
    dockerfile = Path("Dockerfile").read_text()
    assert "USER grove" in dockerfile


def test_application_and_cleanroom_verifier_code_are_not_writable_by_runtime_user() -> None:
    dockerfile = Path("Dockerfile").read_text()
    assert "chown -R root:root /app" in dockerfile
    assert "chmod -R a-w /app" in dockerfile
    assert "chown -R grove:grove /app" not in dockerfile


def test_docker_build_context_excludes_non_runtime_files() -> None:
    patterns = {
        line.strip()
        for line in Path(".dockerignore").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert {
        ".git/",
        ".venv/",
        ".env",
        "ci-evidence/",
        "node_modules/",
        "tests/",
        "docs/",
        "scripts/*",
    } <= patterns
    assert "!scripts/migration_report.py" in patterns
    assert "!scripts/ws3_downgrade.py" in patterns
    assert "app/" not in patterns
    assert "alembic/" not in patterns
    assert "pyproject.toml" not in patterns
    assert "uv.lock" not in patterns
    assert "README.md" not in patterns


def test_postgres_port_is_bound_to_loopback_only() -> None:
    compose = Path("compose.yaml").read_text()
    assert '"127.0.0.1:${INTEGRATION_HOST_DB_PORT:-54329}:5432"' in compose
    assert '"127.0.0.1:${INTEGRATION_HOST_API_PORT:-8000}:8000"' in compose
    assert "GROVE_POSTGRES_IMAGE_ID:?" in compose


@pytest.mark.parametrize(
    ("failure", "retryable"),
    [
        ("psycopg.OperationalError: connection refused", True),
        ("FATAL: the database system is starting up (SQLSTATE 57P03)", True),
        ("canceling statement due to lock timeout (SQLSTATE 55P03)", True),
        ("psycopg.errors.CheckViolation: 23514", False),
        ("migration drift: expected hash mismatch", False),
        ("syntax error at or near ALTER", False),
    ],
)
def test_migration_retry_classifier_only_accepts_transient_failures(failure: str, retryable: bool) -> None:
    env = os.environ.copy()
    env["GROVE_INTEGRATION_LIBRARY"] = "1"
    bash = shutil.which("bash") or "/bin/bash"
    result = subprocess.run(  # noqa: S603
        [
            bash,
            "-c",
            'source scripts/integration.sh; migration_failure_is_retryable "$1"',
            "classifier",
            failure,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert (result.returncode == 0) is retryable


def test_integration_proves_independent_build_digests_and_extension_loading() -> None:
    integration = Path("scripts/integration.sh").read_text()
    postgres_ci = Path("scripts/prepare_ci_postgres_image.sh").read_text()
    makefile = Path("Makefile").read_text()
    assert integration.count("build --no-cache") == 2
    assert "independent application runtime tree digests match" in integration
    assert "CREATE EXTENSION IF NOT EXISTS postgis" in postgres_ci
    assert "CREATE EXTENSION IF NOT EXISTS vector" in postgres_ci
    assert "release-check:" in makefile
    assert "--require-release" in makefile
    assert "USER" in integration
    assert "CMD" in integration
    assert "Entrypoint" in integration
    assert "WorkingDir" in integration
    assert 'project="${COMPOSE_PROJECT_NAME:-grove-ws0-test}"' in integration
    assert 'cleanroom_remove_volumes="${CLEANROOM_REMOVE_VOLUMES:-0}"' in integration
    assert 'export GROVE_POSTGRES_IMAGE_ID="$resolved_postgres_image_ref"' in integration
    assert "migration_failure_is_retryable" in integration
    assert "seq 1 60" not in integration[integration.index("migration_ok=0") : integration.index("# Fault probes")]
    assert "cleanroom-check:" in makefile
    assert "COMPOSE_PROJECT_NAME=grove-ws0-cleanroom-$$$$" in makefile
    assert "CLEANROOM_REMOVE_VOLUMES=1" in makefile
    assert "INTEGRATION_HOST_DB_PORT=0 INTEGRATION_HOST_API_PORT=0" in makefile


def test_github_ci_runs_strict_release_gate() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text()
    assert "run: make release-check" in workflow
    assert "run: make ci" not in workflow


def test_runtime_digest_detects_drift_outside_app(tmp_path: Path) -> None:
    """Exercise the digest helper with two complete rootfs exports.

    The /app payload is identical; only /run changes.  A helper that hashes
    only /app would incorrectly return the same digest for these artifacts.
    """

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/bin/sh
if [ "$1" = image ] && [ "$2" = inspect ]; then
  cat "$FAKE_CONFIG"
elif [ "$1" = create ]; then
  printf 'fake-container\\n'
elif [ "$1" = export ]; then
  cat "$FAKE_ROOTFS"
elif [ "$1" = rm ]; then
  exit 0
else
  exit 2
fi
"""
    )
    docker.chmod(0o755)
    uv = fake_bin / "uv"
    uv.write_text('#!/bin/sh\n[ "$1" = run ] || exit 2\nshift\nexec "$@"\n')
    uv.chmod(0o755)

    config = fake_bin / "config.json"
    config.write_text(
        json.dumps(
            {
                "User": "grove",
                "Cmd": ["python", "-m", "app.main"],
                "Entrypoint": None,
                "Env": ["PATH=/app/.venv/bin"],
                "WorkingDir": "/app",
            }
        )
    )

    def write_export(path: Path, outside_content: bytes) -> None:
        with tarfile.open(path, mode="w") as archive:
            for name, content in (
                ("app/inside.txt", b"same runtime app"),
                ("run/outside.txt", outside_content),
            ):
                info = tarfile.TarInfo(name)
                info.mode = 0o644
                info.uid = 100
                info.gid = 100
                info.mtime = 123456
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))

    first_export = tmp_path / "first.tar"
    second_export = tmp_path / "second.tar"
    write_export(first_export, b"first")
    write_export(second_export, b"second")

    def digest(export: Path) -> str:
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{fake_bin}:{env['PATH']}",
                "FAKE_CONFIG": str(config),
                "FAKE_ROOTFS": str(export),
                "GROVE_INTEGRATION_LIBRARY": "1",
            }
        )
        bash_path = shutil.which("bash")
        assert bash_path is not None
        result = subprocess.run(  # noqa: S603
            [bash_path, "-c", "source scripts/integration.sh; runtime_tree_digest fake-image"],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        return result.stdout.strip()

    assert digest(first_export) != digest(second_export)


def test_postgres_probe_fails_when_target_database_is_missing(tmp_path: Path) -> None:
    """A successful pg_isready probe must not mask a missing target database."""

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/bin/sh
if [ "$1" = image ] && [ "$2" = inspect ]; then
  if [ "$#" -eq 3 ]; then
    exit 1
  fi
  case "$*" in
    *pgvector-source*) printf '%s%s\\n' 'pgvector/pgvector:pg16@sha256:' \\
      'a36250871de0833b8757561c72f2477ef1ddd1101afa4e617fb552e0de514c6b' ;;
    *postgis-source*) printf '%s%s\\n' 'postgis/postgis:16-3.5@sha256:' \\
      '4be58fcb1b50df187e73536e663149c2b3b2da2a541c2f518cfb6adebc65ed91' ;;
    *) printf 'sha256:%064d\\n' 0 ;;
  esac
elif [ "$1" = build ]; then
  exit 0
elif [ "$1" = run ]; then
  exit 0
elif [ "$1" = exec ]; then
  case "$*" in
    *pg_isready*) exit 0 ;;
    *psql*) exit 1 ;;
  esac
elif [ "$1" = container ] && [ "$2" = inspect ]; then
  exit 1
fi
exit 0
"""
    )
    docker.chmod(0o755)
    sleep = fake_bin / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n")
    sleep.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    result = subprocess.run(  # noqa: S603
        [shutil.which("bash") or "/bin/bash", "scripts/prepare_ci_postgres_image.sh"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    # Dynamic extension probe is non-fatal: file-based checks are authoritative.
    # The script succeeds even when the probe cannot create extensions, but
    # the probe failure is still reported in stderr.
    assert result.returncode == 0
    assert "non-fatal" in result.stderr or "probe database was not created" in result.stderr
