from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import psycopg
import pytest
from app.build.manifest import WS3_SCHEMA_CONTRACT
from scripts import migration_report


def test_ws3_trigger_catalog_is_relation_qualified_and_not_name_whitelisted() -> None:
    query = migration_report._WS3_TRIGGER_CATALOG_SQL
    assert "JOIN pg_class AS c ON c.oid = t.tgrelid" in query
    assert "JOIN pg_namespace AS n ON n.oid = c.relnamespace" in query
    assert "JOIN pg_proc AS target_function ON target_function.oid = t.tgfoid" in query
    assert "pg_get_function_identity_arguments(target_function.oid)" in query
    assert "pg_get_functiondef(target_function.oid)" in query
    assert "c.relname = ANY(%s)" not in query
    assert "NOT EXISTS" in query
    assert "t.tgname IN (" not in query
    assert "NOT t.tgisinternal" in query
    family_query = migration_report._WS3_TRIGGER_TARGET_FAMILY_SQL
    assert "JOIN pg_proc AS family_function" in family_query
    assert "family_function.proname = target_function.proname" in family_query
    assert "pg_get_function_identity_arguments(family_function.oid)" in family_query
    assert "pg_get_functiondef(family_function.oid)" in family_query


def test_migration_subprocess_has_a_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[object, str, bool]] = []

    def fake_run(*_args: object, **kwargs: Any) -> None:
        calls.append(
            (
                kwargs["timeout"],
                kwargs["env"]["GROVE_DATABASE_URL"],
                "GROVE_API_BASE_URL" in kwargs["env"],
            )
        )

    monkeypatch.setenv("GROVE_API_BASE_URL", "http://integration-only")
    monkeypatch.setattr(subprocess, "run", fake_run)
    migration_report.run_migration(tmp_path, "upgrade head", "postgresql://migration@db/temp")
    assert calls == [(migration_report.MIGRATION_TIMEOUT_SECONDS, "postgresql://migration@db/temp", False)]
    assert os.environ.get("GROVE_DATABASE_URL") != "postgresql://migration@db/temp"


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("failed"), subprocess.TimeoutExpired("alembic", 30), KeyboardInterrupt()],
)
def test_temporary_migration_database_is_dropped_for_all_exit_paths(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        migration_report,
        "_create_temporary_database",
        lambda _url: ("grove_migration_report_" + "a" * 32, "postgresql://migration@db/temp"),
    )
    monkeypatch.setattr(
        migration_report,
        "_drop_temporary_database",
        lambda url, name: calls.append((url, name)),
    )
    monkeypatch.setattr(migration_report, "_initialize_temporary_database", lambda _url: None)

    with pytest.raises(type(failure)):
        with migration_report.temporary_migration_database("postgresql://migration@db/grove"):
            raise failure

    assert calls == [("postgresql://migration@db/grove", "grove_migration_report_" + "a" * 32)]


def test_temporary_migration_database_is_dropped_after_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        migration_report,
        "_create_temporary_database",
        lambda _url: ("grove_migration_report_" + "b" * 32, "postgresql://migration@db/temp"),
    )
    monkeypatch.setattr(
        migration_report,
        "_drop_temporary_database",
        lambda url, name: calls.append((url, name)),
    )
    monkeypatch.setattr(migration_report, "_initialize_temporary_database", lambda _url: None)

    with migration_report.temporary_migration_database("postgresql://migration@db/grove") as database_url:
        assert database_url == "postgresql://migration@db/temp"

    assert calls == [("postgresql://migration@db/grove", "grove_migration_report_" + "b" * 32)]


def test_temporary_migration_database_is_dropped_when_capability_initialization_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        migration_report,
        "_create_temporary_database",
        lambda _url: ("grove_migration_report_" + "c" * 32, "postgresql://migration@db/temp"),
    )
    monkeypatch.setattr(
        migration_report,
        "_drop_temporary_database",
        lambda url, name: calls.append((url, name)),
    )
    monkeypatch.setattr(
        migration_report,
        "_initialize_temporary_database",
        lambda _url: (_ for _ in ()).throw(RuntimeError("extension init failed")),
    )

    with pytest.raises(RuntimeError, match="extension init failed"):
        with migration_report.temporary_migration_database("postgresql://migration@db/grove"):
            raise AssertionError("unreachable")

    assert calls == [("postgresql://migration@db/grove", "grove_migration_report_" + "c" * 32)]


def test_temporary_capabilities_use_the_same_owner_as_integration(monkeypatch: pytest.MonkeyPatch) -> None:
    statements: list[str] = []

    class Cursor:
        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, statement: object) -> None:
            statements.append(str(statement))

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def cursor(self) -> Cursor:
            return Cursor()

    monkeypatch.setattr(psycopg, "connect", lambda *_args, **_kwargs: Connection())

    migration_report._initialize_temporary_database("postgresql://migration@db/grove_migration_report_" + "d" * 32)

    assert statements[0] == "SET ROLE grove"
    assert statements[1] == "REVOKE CREATE ON SCHEMA public FROM PUBLIC"
    assert "GRANT CONNECT ON DATABASE" in statements[2]
    assert statements[3:7] == [
        "GRANT USAGE ON SCHEMA public TO grove_api, grove_runtime, grove_projection, grove_governance",
        "GRANT USAGE, CREATE ON SCHEMA public TO grove_migration",
        "CREATE EXTENSION postgis",
        "CREATE EXTENSION vector",
    ]
    assert statements[7] == "RESET ROLE"


def test_authority_dml_targets_are_actual_and_do_not_preseed_expected() -> None:
    functions: dict[str, dict[str, object]] = {
        "public.test_fn()": {},
    }
    definitions = {
        "public.test_fn()": """
            WITH current_run AS (SELECT * FROM public.agent_run)
            UPDATE current_run SET status = 'accepted';
            SELECT * FROM public.run_command FOR UPDATE SKIP LOCKED;
        """,
    }
    assert migration_report._authority_dml_targets(functions, definitions) == {"public.test_fn()": ["public.agent_run"]}


def test_authority_dml_targets_reject_dynamic_and_unknown_sql() -> None:
    functions: dict[str, dict[str, object]] = {"public.test_fn()": {}}
    with pytest.raises(migration_report.MigrationReportError, match="dynamic SQL"):
        migration_report._authority_dml_targets(
            functions, {"public.test_fn()": "EXECUTE format('UPDATE %s', 'agent_run')"}
        )
    with pytest.raises(migration_report.MigrationReportError, match="unknown unqualified"):
        migration_report._authority_dml_targets(functions, {"public.test_fn()": "UPDATE hidden_table SET x = 1"})
    with pytest.raises(migration_report.MigrationReportError, match="quoted SQL"):
        migration_report._authority_dml_targets(
            functions, {"public.test_fn()": "UPDATE \"agent_run\" SET status = 'running'"}
        )


def test_authority_dml_lexer_rejection_is_compatibility_diagnostic_only() -> None:
    functions: dict[str, dict[str, object]] = {"public.test_fn()": {}}
    result = migration_report._authority_dml_targets_diagnostic(
        functions,
        {"public.test_fn()": "EXECUTE format('UPDATE %s', 'agent_run')"},
    )
    assert result == WS3_SCHEMA_CONTRACT["authority_dml_targets"]


def test_authority_dml_targets_accept_qualified_alias_and_for_update_without_false_positive() -> None:
    functions: dict[str, dict[str, object]] = {"public.test_fn()": {}}
    definitions = {
        "public.test_fn()": """
            UPDATE public.agent_run AS r SET status = 'running';
            DELETE FROM public.run_command AS c WHERE c.status = 'queued';
            SELECT * FROM public.run_command FOR NO KEY UPDATE SKIP LOCKED;
        """,
    }
    assert migration_report._authority_dml_targets(functions, definitions) == {
        "public.test_fn()": ["public.agent_run", "public.run_command"]
    }


def test_migration_report_writes_content_addressed_copy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    class TemporaryDatabase:
        def __enter__(self) -> str:
            return "postgresql://migration@db/grove_migration_report_private"

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(migration_report, "temporary_migration_database", lambda _url: TemporaryDatabase())
    monkeypatch.setattr(
        migration_report,
        "run_migration",
        lambda _root, command, database_url: calls.append((command, database_url)),
    )
    monkeypatch.setattr(
        migration_report,
        "database_state",
        lambda database_url: (
            ("baseline", [])
            if database_url == "postgresql://migration@db/grove_migration_report_private"
            else (_ for _ in ()).throw(AssertionError("migration report inspected the integration database"))
        ),
    )
    monkeypatch.setattr(
        migration_report,
        "load_settings",
        lambda: type(
            "Settings",
            (),
            {"database_url_value": lambda self: "postgresql://migration@db/grove"},
        )(),
    )
    monkeypatch.setattr(migration_report, "migration_head", lambda _root: "baseline")
    output = tmp_path / "ci-evidence" / "migrations.json"

    migration_report.write_report(tmp_path, output)

    payload = output.read_bytes()
    report = json.loads(payload)
    assert report["status"] == "completed"
    assert "grove_migration_report_private" not in payload.decode()
    assert calls == [
        (command, "postgresql://migration@db/grove_migration_report_private") for command in migration_report.ROUND_TRIP
    ]
    digest = hashlib.sha256(payload).hexdigest()
    assert (output.parent / "sha256" / digest / output.name).read_bytes() == payload


def test_migration_report_rejects_database_head_not_in_graph(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class TemporaryDatabase:
        def __enter__(self) -> str:
            return "postgresql://migration@db/temp"

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(migration_report, "temporary_migration_database", lambda _url: TemporaryDatabase())
    monkeypatch.setattr(migration_report, "run_migration", lambda _root, _command, _database_url: None)
    monkeypatch.setattr(migration_report, "database_state", lambda _url: ("stale", []))
    monkeypatch.setattr(
        migration_report,
        "load_settings",
        lambda: type("Settings", (), {"database_url_value": lambda self: "postgresql://migration@db/grove"})(),
    )
    monkeypatch.setattr(migration_report, "migration_head", lambda _root: "baseline")
    with pytest.raises(migration_report.MigrationReportError, match="does not match Alembic graph"):
        migration_report.write_report(tmp_path, tmp_path / "migrations.json")


def test_reconcile_contract_closes_lifecycle_and_prior_command_owner() -> None:
    sql = Path("alembic/versions/0006_ws3_dead_letter_reconciliation.py").read_text()
    assert "(run_row.status = 'running' AND command_row.command_type = 'start')" in sql
    assert "(run_row.status = 'cancel_requested' AND command_row.command_type = 'cancel')" in sql
    assert "LEFT JOIN public.run_command AS prior_command" in sql
    assert "prior_command.status = 'consumed'" in sql
    for field in (
        "consumed_worker_id",
        "consumed_execution_fence",
        "consumed_lease_until",
        "consumed_claim_provenance_hash",
    ):
        assert f"prior_command.{field}" in sql


def test_execution_authority_closure_contract_is_a_new_migration_layer() -> None:
    sql = Path("alembic/versions/0007_ws3_execution_authority_closure.py").read_text()
    assert 'revision: str = "ws3_execution_authority_closure"' in sql
    assert 'down_revision: str | None = "ws3_dead_letter_reconciliation"' in sql
    assert "grove_execution_claim_lifecycle_valid" in sql
    assert "p_run_status = 'running' AND p_command_type = 'start'" in sql
    assert "p_run_status = 'cancel_requested' AND p_command_type = 'cancel'" in sql
    predicate_body = sql.split("CREATE OR REPLACE FUNCTION grove_execution_claim_lifecycle_valid", 1)[1].split("$$", 2)[
        1
    ]
    assert "accepted" not in predicate_body
    assert "authority_now := clock_timestamp()" in sql
    assert "CREATE OR REPLACE FUNCTION grove_checkpoint_authority_guard" in sql
    assert "RENAME TO grove_claim_run_command_internal" in sql
    assert "RENAME TO grove_heartbeat_run_command_internal" in sql
    assert "RENAME TO grove_consume_run_command_internal" in sql
    assert "RENAME TO grove_dead_letter_run_command_internal" in sql
    assert "RENAME TO grove_reconcile_expired_run_command_internal" in sql
    assert "public.grove_heartbeat_run_command_internal" in sql
    assert "public.grove_consume_run_command_internal" in sql
    assert "public.grove_dead_letter_run_command_internal" in sql
    assert "public.grove_reconcile_expired_run_command_internal" in sql
    assert "FOR UPDATE OF r SKIP LOCKED" in sql
    # Claim discovery snapshots every identity/eligibility field and the
    # post-lock CAS binds the same candidate on both durable rows.
    for field in (
        "candidate_run_tenant_id",
        "candidate_run_id",
        "candidate_run_status",
        "candidate_run_runtime_build_hash",
        "candidate_run_lease_owner",
        "candidate_run_lease_until",
        "candidate_run_execution_fence",
        "candidate_command_tenant_id",
        "candidate_command_id",
        "candidate_command_run_id",
        "candidate_command_seq",
        "candidate_command_digest",
        "candidate_command_type",
        "candidate_command_schema_version",
        "candidate_command_status",
        "candidate_command_available_at",
        "candidate_command_lease_owner",
        "candidate_command_lease_until",
        "candidate_command_execution_fence",
        "candidate_command_superseded_by_command_id",
    ):
        assert field in sql
    assert "GET DIAGNOSTICS run_update_count = ROW_COUNT" in sql
    assert "GET DIAGNOSTICS command_update_count = ROW_COUNT" in sql
    # GV001 is a Grove-private five-character code used only to roll back a
    # command CAS miss inside the local PL/pgSQL subtransaction.  PostgreSQL's
    # real serialization/deadlock/trigger errors must never be caught here.
    assert "USING ERRCODE = 'GV001'" in sql
    assert "WHEN SQLSTATE 'GV001' THEN" in sql
    assert "WHEN SQLSTATE '40001' THEN" not in sql
    assert "claimed_run.tenant_id = candidate.candidate_run_tenant_id" in sql
    assert "claimed_command.tenant_id = candidate.candidate_command_tenant_id" in sql
    assert "claimed_command.command_schema_version = candidate.candidate_command_schema_version" in sql
    assert "claimed_command.superseded_by_command_id IS NULL" in sql
    assert "DROP FUNCTION grove_claim_run_command_internal" in sql
    assert "_legacy" not in sql
