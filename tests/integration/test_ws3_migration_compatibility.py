from __future__ import annotations

import os
import re
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
from scripts import migration_report
from sqlalchemy.engine import URL, make_url

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LEGACY_COMMAND_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
_PENDING_COMMAND_ID = uuid.UUID("10000000-0000-0000-0000-000000000002")
_RUN_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
_SUBMISSION_ID = uuid.UUID("30000000-0000-0000-0000-000000000001")
_CLAIM_PROVENANCE_ASSIGNMENT = "consumed_provenance_kind = 'claim.v1',"
_PROVENANCE_WRITER_SIGNATURES = (
    (
        "public.grove_consume_run_command_internal("
        "text,uuid,uuid,bigint,text,text,text,bigint,timestamp with time zone)",
        2,
    ),
    ("public.grove_reconcile_expired_run_command_internal(text,uuid)", 1),
    (
        "public.grove_finish_delivery("
        "text,uuid,uuid,bigint,text,text,text,bigint,timestamp with time zone,text,text,text,jsonb)",
        1,
    ),
)


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


def _migration_report_database_names(database_url: URL) -> list[str]:
    admin_url = database_url.set(database="postgres")
    with psycopg.connect(_raw_url(admin_url), autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT datname FROM pg_database WHERE datname LIKE %s ORDER BY datname",
                (migration_report.TEMPORARY_DATABASE_PREFIX + "%",),
            )
            return [str(row[0]) for row in cursor.fetchall()]


def _invoke_alembic(database_url: URL, command: str, target: str) -> subprocess.CompletedProcess[str]:
    child_env = {name: value for name, value in os.environ.items() if not name.startswith("GROVE_")}
    child_env.update(
        {
            "GROVE_DATABASE_URL": database_url.render_as_string(hide_password=False),
            "GROVE_ROLE": "api",
        }
    )
    return subprocess.run(  # noqa: S603 - fixed interpreter/module/command allowlist
        [sys.executable, "-m", "alembic", command, target],
        cwd=_PROJECT_ROOT,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _run_alembic(database_url: URL, command: str, target: str) -> None:
    result = _invoke_alembic(database_url, command, target)
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


def _simulate_published_head_without_provenance_discriminator(database_url: URL) -> None:
    with psycopg.connect(_raw_url(database_url), autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE run_command SET consumed_provenance_kind = 'claim.v1', "
                "consumed_worker_id = 'published-worker', consumed_execution_fence = 7, "
                "consumed_lease_until = %s, consumed_claim_provenance_hash = %s "
                "WHERE command_id = %s",
                (datetime(2030, 1, 1, tzinfo=UTC), "9" * 64, _LEGACY_COMMAND_ID),
            )
            for signature, expected_count in _PROVENANCE_WRITER_SIGNATURES:
                cursor.execute("SELECT pg_get_functiondef(to_regprocedure(%s))", (signature,))
                row = cursor.fetchone()
                assert row is not None
                old_definition, replacement_count = re.subn(
                    r"(?m)^[ \t]*consumed_provenance_kind = 'claim\.v1',\n",
                    "",
                    row[0],
                )
                assert replacement_count == expected_count
                cursor.execute(old_definition)
            cursor.execute("ALTER TABLE run_command DROP CONSTRAINT run_command_consumed_provenance_ck")
            cursor.execute("ALTER TABLE run_command DROP COLUMN consumed_provenance_kind")
            cursor.execute(
                "ALTER TABLE run_command ADD CONSTRAINT run_command_consumed_provenance_ck CHECK ("
                "(status = 'consumed' AND consumed_worker_id IS NOT NULL "
                "AND consumed_execution_fence IS NOT NULL AND consumed_lease_until IS NOT NULL "
                "AND consumed_claim_provenance_hash ~ '^[0-9a-f]{64}$') OR "
                "(status <> 'consumed' AND consumed_worker_id IS NULL "
                "AND consumed_execution_fence IS NULL AND consumed_lease_until IS NULL "
                "AND consumed_claim_provenance_hash IS NULL))"
            )


def _assert_published_head_forward_migration(database_url: URL) -> None:
    with psycopg.connect(_raw_url(database_url), autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT version_num FROM alembic_version")
            assert cursor.fetchone() == ("ws6_domain_view_runtime_event",)
            cursor.execute(
                "SELECT status, consumed_provenance_kind, consumed_worker_id, "
                "consumed_execution_fence, consumed_lease_until, consumed_claim_provenance_hash "
                "FROM run_command WHERE command_id = %s",
                (_LEGACY_COMMAND_ID,),
            )
            assert cursor.fetchone() == (
                "consumed",
                "claim.v1",
                "published-worker",
                7,
                datetime(2030, 1, 1, tzinfo=UTC),
                "9" * 64,
            )
            cursor.execute(
                "SELECT consumed_provenance_kind FROM run_command WHERE command_id = %s",
                (_PENDING_COMMAND_ID,),
            )
            assert cursor.fetchone() == (None,)
            for signature, expected_count in _PROVENANCE_WRITER_SIGNATURES:
                cursor.execute("SELECT pg_get_functiondef(to_regprocedure(%s))", (signature,))
                row = cursor.fetchone()
                assert row is not None
                assert row[0].count(_CLAIM_PROVENANCE_ASSIGNMENT) == expected_count


def _tamper_consume_writer(database_url: URL) -> str:
    signature = _PROVENANCE_WRITER_SIGNATURES[0][0]
    with psycopg.connect(_raw_url(database_url), autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_get_functiondef(to_regprocedure(%s))", (signature,))
            row = cursor.fetchone()
            assert row is not None
            tampered_definition = row[0].replace(
                "AS $function$\n",
                "AS $function$\n        -- compatibility hash drift\n",
                1,
            )
            assert tampered_definition != row[0]
            cursor.execute(tampered_definition)
            cursor.execute("SELECT pg_get_functiondef(to_regprocedure(%s))", (signature,))
            persisted = cursor.fetchone()
            assert persisted is not None
            return str(persisted[0])


def _assert_failed_upgrade_rolled_back(
    database_url: URL,
    tampered_definition: str,
    *,
    expected_column: bool,
) -> None:
    signature = _PROVENANCE_WRITER_SIGNATURES[0][0]
    with psycopg.connect(_raw_url(database_url), autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT version_num FROM alembic_version")
            assert cursor.fetchone() == ("ws4_authority_audit_emitters",)
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'run_command' "
                "AND column_name = 'consumed_provenance_kind')"
            )
            assert cursor.fetchone() == (expected_column,)
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_constraint "
                "WHERE conrelid = 'public.run_command'::regclass "
                "AND conname = 'run_command_consumed_provenance_ck')"
            )
            assert cursor.fetchone() == (True,)
            cursor.execute("SELECT pg_get_functiondef(to_regprocedure(%s))", (signature,))
            row = cursor.fetchone()
            assert row == (tampered_definition,)


def _downgrade_snapshot(database_url: URL) -> tuple[object, ...]:
    with psycopg.connect(_raw_url(database_url), autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT version_num FROM alembic_version")
            head = cursor.fetchone()
            cursor.execute(
                "SELECT status, command_seq, consumed_provenance_kind, consumed_worker_id, "
                "consumed_execution_fence, consumed_lease_until, consumed_claim_provenance_hash "
                "FROM run_command WHERE command_id = %s",
                (_LEGACY_COMMAND_ID,),
            )
            command = cursor.fetchone()
            cursor.execute(
                "SELECT pg_get_functiondef(to_regprocedure(%s))",
                (_PROVENANCE_WRITER_SIGNATURES[0][0],),
            )
            function_definition = cursor.fetchone()
            cursor.execute(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'run_command'::regclass "
                "AND conname = 'run_command_consumed_provenance_ck'"
            )
            constraint_definition = cursor.fetchone()
    return head, command, function_definition, constraint_definition


@pytest.mark.integration
def test_migration_report_round_trip_isolated_and_temporary_database_removed(tmp_path: Path) -> None:
    database_url = _migration_url()
    before = _migration_report_database_names(database_url)
    output = tmp_path / "migrations.json"

    migration_report.write_report(
        _PROJECT_ROOT,
        output,
        database_url=database_url.render_as_string(hide_password=False),
    )

    assert _migration_report_database_names(database_url) == before
    assert migration_report.TEMPORARY_DATABASE_PREFIX not in output.read_text()


@pytest.mark.integration
def test_live_downgrade_rejects_consumed_command_before_any_ddl() -> None:
    with _temporary_database() as database_url:
        _run_alembic(database_url, "upgrade", "ws3_execution_driver")
        _seed_0003_consumed_command(database_url)
        _run_alembic(database_url, "upgrade", "head")
        _assert_head_legacy_semantics(database_url)
        before = _downgrade_snapshot(database_url)

        result = _invoke_alembic(database_url, "downgrade", "base")

        assert result.returncode != 0
        assert "WS3_DOWNGRADE_INCOMPATIBLE_LIVE_DATA" in result.stderr
        assert _downgrade_snapshot(database_url) == before


@pytest.mark.integration
def test_published_head_upgrades_forward_without_replaying_rewritten_revisions() -> None:
    with _temporary_database() as database_url:
        _run_alembic(database_url, "upgrade", "ws3_execution_driver")
        _seed_0003_consumed_command(database_url)
        _run_alembic(database_url, "upgrade", "ws4_authority_audit_emitters")
        _simulate_published_head_without_provenance_discriminator(database_url)

        _run_alembic(database_url, "upgrade", "head")

        _assert_published_head_forward_migration(database_url)


@pytest.mark.integration
def test_published_head_hash_drift_rejects_upgrade_and_rolls_back_all_ddl() -> None:
    with _temporary_database() as database_url:
        _run_alembic(database_url, "upgrade", "ws3_execution_driver")
        _seed_0003_consumed_command(database_url)
        _run_alembic(database_url, "upgrade", "ws4_authority_audit_emitters")
        _simulate_published_head_without_provenance_discriminator(database_url)
        tampered_definition = _tamper_consume_writer(database_url)

        result = _invoke_alembic(database_url, "upgrade", "head")

        assert result.returncode != 0
        assert "published provenance writer definition drift" in result.stderr
        _assert_failed_upgrade_rolled_back(database_url, tampered_definition, expected_column=False)


@pytest.mark.integration
def test_current_writer_hash_drift_rejects_upgrade_and_rolls_back_all_ddl() -> None:
    with _temporary_database() as database_url:
        _run_alembic(database_url, "upgrade", "ws3_execution_driver")
        _seed_0003_consumed_command(database_url)
        _run_alembic(database_url, "upgrade", "ws4_authority_audit_emitters")
        tampered_definition = _tamper_consume_writer(database_url)

        result = _invoke_alembic(database_url, "upgrade", "head")

        assert result.returncode != 0
        assert "current provenance writer definition drift" in result.stderr
        _assert_failed_upgrade_rolled_back(database_url, tampered_definition, expected_column=True)
