from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import URL, make_url

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LEGACY_COMMAND_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
_PENDING_COMMAND_ID = uuid.UUID("10000000-0000-0000-0000-000000000002")
_RUN_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
_SUBMISSION_ID = uuid.UUID("30000000-0000-0000-0000-000000000001")


def _raw_url(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _migration_url() -> URL:
    configured = os.environ.get("GROVE_MIGRATION_DATABASE_URL", os.environ["GROVE_DATABASE_URL"])
    return make_url(configured)


@contextmanager
def _temporary_database() -> Iterator[URL]:
    configured = _migration_url()
    database_name = f"grove_ws3_migration_{uuid.uuid4().hex}"
    admin_url = configured.set(database="postgres")
    with psycopg.connect(_raw_url(admin_url), autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    database_url = configured.set(database=database_name)
    try:
        yield database_url
    finally:
        with psycopg.connect(_raw_url(admin_url), autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (database_name,),
                )
                cursor.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database_name)))


def _run_alembic(database_url: URL, command: str, target: str) -> None:
    child_env = {name: value for name, value in os.environ.items() if not name.startswith("GROVE_")}
    child_env.update(
        {
            "GROVE_DATABASE_URL": database_url.render_as_string(hide_password=False),
            "GROVE_ROLE": "api",
        }
    )
    result = subprocess.run(  # noqa: S603 - fixed interpreter/module/command allowlist
        [sys.executable, "-m", "alembic", command, target],
        cwd=_PROJECT_ROOT,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _seed_0003_consumed_command(database_url: URL) -> None:
    with psycopg.connect(_raw_url(database_url), autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO tenant (tenant_id) VALUES ('migration-legacy')")
            cursor.execute(
                "INSERT INTO membership (tenant_id, principal_id, user_ref) "
                "VALUES ('migration-legacy', 'legacy-human', 'user://legacy-human')"
            )
            cursor.execute(
                "INSERT INTO execution_spec "
                "(tenant_id, skill_spec_hash, spec_ref, spec_payload) VALUES (%s, %s, %s, %s::jsonb)",
                (
                    "migration-legacy",
                    "a" * 64,
                    "spec://legacy",
                    '{"runtime_build":{"ref":"runtime://legacy","content_hash":"' + "b" * 64 + '"}}',
                ),
            )
            cursor.execute(
                "INSERT INTO command_payload "
                "(tenant_id, payload_ref, payload_hash, command_schema_version, payload) "
                "VALUES ('migration-legacy', 'payload://legacy-start', %s, 'start.v1', '{}'::jsonb), "
                "('migration-legacy', 'payload://pending-start', %s, 'start.v1', '{}'::jsonb)",
                ("c" * 64, "d" * 64),
            )
            cursor.execute(
                "INSERT INTO agent_run "
                "(run_id, tenant_id, submission_id, submission_digest, principal_id, principal_kind, "
                "skill_spec_hash, skill_spec_ref, status, runtime_build_ref, runtime_build_hash, execution_fence) "
                "VALUES (%s, 'migration-legacy', %s, %s, 'legacy-human', 'human', %s, "
                "'spec://legacy', 'running', 'runtime://legacy', %s, 1)",
                (_RUN_ID, _SUBMISSION_ID, "e" * 64, "a" * 64, "b" * 64),
            )
            cursor.execute(
                "INSERT INTO run_command "
                "(command_id, tenant_id, run_id, principal_id, principal_kind, command_seq, command_type, "
                "command_schema_version, command_digest, payload_ref, payload_hash, status) VALUES "
                "(%s, 'migration-legacy', %s, 'legacy-human', 'human', 0, 'start', 'start.v1', %s, "
                "'payload://legacy-start', %s, 'consumed'), "
                "(%s, 'migration-legacy', %s, 'legacy-human', 'human', 1, 'start', 'start.v1', %s, "
                "'payload://pending-start', %s, 'pending')",
                (
                    _LEGACY_COMMAND_ID,
                    _RUN_ID,
                    "f" * 64,
                    "c" * 64,
                    _PENDING_COMMAND_ID,
                    _RUN_ID,
                    "1" * 64,
                    "d" * 64,
                ),
            )


def _assert_head_legacy_semantics(database_url: URL) -> None:
    with psycopg.connect(_raw_url(database_url), autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT status, consumed_provenance_kind, consumed_worker_id, "
                "consumed_execution_fence, consumed_lease_until, "
                "consumed_claim_provenance_hash FROM run_command WHERE command_id = %s",
                (_LEGACY_COMMAND_ID,),
            )
            assert cursor.fetchone() == ("consumed", "legacy_unverified", None, None, None, None)
            cursor.execute(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'run_command'::regclass AND conname = 'run_command_consumed_provenance_ck'"
            )
            definition = cursor.fetchone()
            assert definition is not None
            assert "legacy_unverified" in definition[0]
            cursor.execute(
                "SELECT has_column_privilege('grove_api', 'run_command', 'consumed_provenance_kind', 'INSERT'), "
                "has_column_privilege('grove_runtime', 'run_command', 'consumed_provenance_kind', 'UPDATE')"
            )
            assert cursor.fetchone() == (False, False)
            with pytest.raises(psycopg.errors.CheckViolation, match="run_command_consumed_provenance_ck"):
                cursor.execute(
                    "UPDATE run_command SET status = 'consumed' WHERE command_id = %s",
                    (_PENDING_COMMAND_ID,),
                )
            cursor.execute("SELECT status FROM run_command WHERE command_id = %s", (_PENDING_COMMAND_ID,))
            assert cursor.fetchone() == ("pending",)

    runtime_url = database_url.set(
        username="grove_runtime",
        password="grove_runtime_ws0",  # noqa: S106 - test-only bootstrap credential
    )
    with psycopg.connect(_raw_url(runtime_url), autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('grove.tenant_id', 'migration-legacy', false)")
            cursor.execute(
                "SELECT result_code FROM grove_consume_run_command(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    "migration-legacy",
                    _RUN_ID,
                    _LEGACY_COMMAND_ID,
                    0,
                    "f" * 64,
                    "b" * 64,
                    "guessed-legacy-worker",
                    1,
                    datetime(2030, 1, 1, tzinfo=UTC),
                ),
            )
            assert cursor.fetchone() == ("stale",)


@pytest.mark.integration
def test_0003_consumed_rows_survive_upgrade_downgrade_upgrade_without_forged_provenance() -> None:
    with _temporary_database() as database_url:
        _run_alembic(database_url, "upgrade", "ws3_execution_driver")
        _seed_0003_consumed_command(database_url)

        _run_alembic(database_url, "upgrade", "head")
        _assert_head_legacy_semantics(database_url)

        _run_alembic(database_url, "downgrade", "ws3_execution_driver")
        with psycopg.connect(_raw_url(database_url)) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT status FROM run_command WHERE command_id = %s", (_LEGACY_COMMAND_ID,))
                assert cursor.fetchone() == ("consumed",)
                cursor.execute(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'run_command' "
                    "AND column_name LIKE 'consumed_%'"
                )
                assert cursor.fetchone() == (0,)

        _run_alembic(database_url, "upgrade", "head")
        _assert_head_legacy_semantics(database_url)
