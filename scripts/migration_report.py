#!/usr/bin/env python3
"""Run the real migration round trip and write deterministic evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import FrameType
from typing import Any, cast

import psycopg
from app.build.catalog_authority import (
    CatalogAuthorityError,
    compare_expected_catalog_root,
    discover_catalog_authority,
    expected_catalog_artifact_hash,
    expected_catalog_authority_root,
)
from app.build.manifest import (
    WS2_BUSINESS_RELATIONS,
    WS3_AUTHORITY_ACL_EXPECTED,
    WS3_AUTHORITY_COLUMN_MUTATION_PRIVILEGES,
    WS3_AUTHORITY_CONSTRAINTS,
    WS3_AUTHORITY_FUNCTION_TARGETS,
    WS3_AUTHORITY_GRANT_PRIVILEGES,
    WS3_AUTHORITY_GRANT_ROLES,
    WS3_AUTHORITY_MUTATION_PRIVILEGES,
    WS3_AUTHORITY_MUTATION_RELATION_NAMES,
    WS3_AUTHORITY_OBJECT_INVENTORY,
    WS3_AUTHORITY_OBJECT_RELKINDS,
    WS3_AUTHORITY_ONLINE_ROLES,
    WS3_AUTHORITY_RELATION_EXCLUSIONS,
    WS3_AUTHORITY_RELATION_NAMES,
    WS3_AUTHORITY_RELATION_REGISTRY,
    WS3_AUTHORITY_ROLE_REGISTRY,
    WS3_AUTHORITY_ROLES,
    WS3_BUSINESS_RELATIONS,
    WS3_INFRASTRUCTURE_RELATIONS,
    WS3_SCHEMA_CONTRACT,
    WS3_SCHEMA_CONTRACT_VERSION,
    WS4_BUSINESS_RELATIONS,
    WS4_MIGRATION_HEADS,
    migration_hash,
    migration_head,
    write_content_addressed_artifact,
)
from app.core.config import load_settings
from psycopg import sql
from sqlalchemy.engine import make_url

ROUND_TRIP = ("upgrade head", "downgrade base", "upgrade head")
MIGRATION_TIMEOUT_SECONDS = 30
TEMPORARY_DATABASE_PREFIX = "grove_migration_report_"
EXPECTED_BUSINESS_RELATIONS = WS2_BUSINESS_RELATIONS
_WS3_TRIGGER_CATALOG_SQL = """
    SELECT n.nspname, c.relname, t.tgname, t.tgenabled, pg_get_triggerdef(t.oid, true),
           target_namespace.nspname, target_function.proname,
           pg_get_function_identity_arguments(target_function.oid),
           target_owner.rolname, target_function.prosecdef, target_function.proconfig,
           target_function.prokind, target_language.lanname, target_function.provolatile,
           target_function.proparallel, target_function.proisstrict, target_function.proleakproof,
           COALESCE((
               SELECT jsonb_agg(jsonb_build_object(
                   'grantor', grantor.rolname,
                   'grantee', COALESCE(grantee.rolname, 'PUBLIC'),
                   'privilege', acl_item.privilege_type,
                   'grantable', acl_item.is_grantable
               ) ORDER BY grantor.rolname, COALESCE(grantee.rolname, 'PUBLIC'), acl_item.privilege_type)
                 FROM aclexplode(
                     COALESCE(target_function.proacl, acldefault('f', target_function.proowner))
                 ) AS acl_item
                 JOIN pg_roles AS grantor ON grantor.oid = acl_item.grantor
                 LEFT JOIN pg_roles AS grantee ON grantee.oid = acl_item.grantee
           ), '[]'::jsonb),
           pg_get_functiondef(target_function.oid)
      FROM pg_trigger AS t
      JOIN pg_class AS c ON c.oid = t.tgrelid
      JOIN pg_namespace AS n ON n.oid = c.relnamespace
      JOIN pg_proc AS target_function ON target_function.oid = t.tgfoid
      JOIN pg_language AS target_language ON target_language.oid = target_function.prolang
      JOIN pg_namespace AS target_namespace ON target_namespace.oid = target_function.pronamespace
      JOIN pg_roles AS target_owner ON target_owner.oid = target_function.proowner
     WHERE NOT t.tgisinternal
       AND n.nspname = 'public'
       AND NOT EXISTS (
           SELECT 1
             FROM pg_depend AS d
             JOIN pg_extension AS e ON e.oid = d.refobjid
            WHERE d.classid = 'pg_class'::regclass
              AND d.objid = c.oid
              AND d.deptype = 'e'
       )
     ORDER BY n.nspname, c.relname, t.tgname
"""
_WS3_TRIGGER_TARGET_FAMILY_SQL = """
    SELECT DISTINCT target_namespace.nspname, target_function.proname,
           family_namespace.nspname, family_function.proname,
           pg_get_function_identity_arguments(family_function.oid),
           family_owner.rolname, family_function.prosecdef, family_function.proconfig,
           family_function.prokind, family_language.lanname, family_function.provolatile,
           family_function.proparallel, family_function.proisstrict, family_function.proleakproof,
           COALESCE((
               SELECT jsonb_agg(jsonb_build_object(
                   'grantor', grantor.rolname,
                   'grantee', COALESCE(grantee.rolname, 'PUBLIC'),
                   'privilege', acl_item.privilege_type,
                   'grantable', acl_item.is_grantable
               ) ORDER BY grantor.rolname, COALESCE(grantee.rolname, 'PUBLIC'), acl_item.privilege_type)
                 FROM aclexplode(
                     COALESCE(family_function.proacl, acldefault('f', family_function.proowner))
                 ) AS acl_item
                 JOIN pg_roles AS grantor ON grantor.oid = acl_item.grantor
                 LEFT JOIN pg_roles AS grantee ON grantee.oid = acl_item.grantee
           ), '[]'::jsonb),
           pg_get_functiondef(family_function.oid)
      FROM pg_trigger AS t
      JOIN pg_class AS c ON c.oid = t.tgrelid
      JOIN pg_namespace AS n ON n.oid = c.relnamespace
      JOIN pg_proc AS target_function ON target_function.oid = t.tgfoid
      JOIN pg_namespace AS target_namespace ON target_namespace.oid = target_function.pronamespace
      JOIN pg_proc AS family_function
        ON family_function.pronamespace = target_function.pronamespace
       AND family_function.proname = target_function.proname
      JOIN pg_namespace AS family_namespace ON family_namespace.oid = family_function.pronamespace
      JOIN pg_roles AS family_owner ON family_owner.oid = family_function.proowner
      JOIN pg_language AS family_language ON family_language.oid = family_function.prolang
     WHERE NOT t.tgisinternal
       AND n.nspname = 'public'
       AND NOT EXISTS (
           SELECT 1
             FROM pg_depend AS d
             JOIN pg_extension AS e ON e.oid = d.refobjid
            WHERE d.classid = 'pg_class'::regclass
              AND d.objid = c.oid
              AND d.deptype = 'e'
       )
     ORDER BY 1, 2, 3, 4, 5
"""


class MigrationReportError(RuntimeError):
    """Raised when live database evidence does not match the baseline contract."""


class MigrationCancelled(MigrationReportError):
    """Raised on SIGINT/SIGTERM so temporary database cleanup still runs."""


def _legacy_acl_items(value: object) -> list[str]:
    """Tokenize a PostgreSQL ACL literal without splitting quoted role names."""

    raw = str(value or "")
    if raw.startswith("{") and raw.endswith("}"):
        raw = raw[1:-1]
    if not raw:
        return []
    items: list[str] = []
    token: list[str] = []
    quoted = False
    index = 0
    while index < len(raw):
        character = raw[index]
        if character == '"':
            if quoted and index + 1 < len(raw) and raw[index + 1] == '"':
                token.append('"')
                index += 2
                continue
            quoted = not quoted
            index += 1
            continue
        if character == "," and not quoted:
            if token:
                items.append("".join(token))
                token = []
            index += 1
            continue
        token.append(character)
        index += 1
    if token:
        items.append("".join(token))
    return sorted(items)


def _canonical_acl_entries(value: object) -> list[dict[str, object]]:
    """Canonicalize aclexplode JSON rows while preserving arbitrary role names."""

    if value is None:
        return []
    raw: object = value
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MigrationReportError("catalog ACL evidence is not structured JSON") from exc
    if not isinstance(raw, list):
        raise MigrationReportError("catalog ACL evidence must be a JSON array")
    entries: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise MigrationReportError("catalog ACL evidence contains a non-object entry")
        required = {"grantor", "grantee", "privilege", "grantable"}
        if set(item) != required:
            raise MigrationReportError("catalog ACL evidence has an unexpected entry shape")
        if not isinstance(item["grantor"], str) or not isinstance(item["grantee"], str):
            raise MigrationReportError("catalog ACL evidence has an invalid role name")
        if not isinstance(item["privilege"], str) or not isinstance(item["grantable"], bool):
            raise MigrationReportError("catalog ACL evidence has an invalid privilege")
        entries.append(dict(item))
    return sorted(
        entries,
        key=lambda entry: (
            str(entry["grantor"]),
            str(entry["grantee"]),
            str(entry["privilege"]),
            bool(entry["grantable"]),
        ),
    )


def _normalized_acl(value: object) -> list[str]:
    """Retain the legacy text projection for compatibility-only evidence."""

    if isinstance(value, list) or (isinstance(value, str) and value.lstrip().startswith("[")):
        entries = _canonical_acl_entries(value)
        return sorted(
            f"{'' if entry['grantee'] == 'PUBLIC' else entry['grantee']}=X/{entry['grantor']}" for entry in entries
        )
    return _legacy_acl_items(value)


def _normalized_acl_text(value: object) -> str:
    """Serialize normalized ACL entries without catalog-order variance."""

    return "{" + ",".join(_normalized_acl(value)) + "}"


def _function_facts(
    schema: object,
    name: object,
    identity_arguments: object,
    owner: object,
    security_definer: object,
    settings: object,
    acl: object,
    definition: object,
    *,
    prokind: object = "f",
    prolang: object = "plpgsql",
    volatility: object = "v",
    parallel: object = "u",
    strict: object = False,
    leakproof: object = False,
) -> dict[str, object]:
    """Build one canonical target-function fact map from pg_proc columns."""

    schema_text = str(schema)
    name_text = str(name)
    arguments_text = str(identity_arguments)
    normalized_settings = settings if isinstance(settings, (list, tuple)) else ()
    acl_entries = _canonical_acl_entries(acl)
    return {
        "identity": f"{schema_text}.{name_text}({arguments_text})",
        "schema": schema_text,
        "name": name_text,
        "identity_arguments": arguments_text,
        "owner": str(owner),
        "security_definer": bool(security_definer),
        "settings": sorted(str(item) for item in normalized_settings),
        "acl": _normalized_acl(acl),
        "acl_entries": acl_entries,
        "prokind": str(prokind),
        "prolang": str(prolang),
        "provolatile": str(volatility),
        "proparallel": str(parallel),
        "proisstrict": bool(strict),
        "proleakproof": bool(leakproof),
        "definition_sha256": hashlib.sha256(str(definition).encode()).hexdigest(),
        "body_sha256": hashlib.sha256(str(definition).encode()).hexdigest(),
    }


def run_migration(root: Path, command: str, database_url: str) -> None:
    """Run one Alembic command against the isolated migration database."""

    revision, target = command.split(" ", maxsplit=1)
    child_env = {name: value for name, value in os.environ.items() if not name.startswith("GROVE_")}
    child_env["GROVE_DATABASE_URL"] = database_url
    child_env["GROVE_ROLE"] = "api"
    subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", revision, target],
        cwd=root,
        env=child_env,
        check=True,
        timeout=MIGRATION_TIMEOUT_SECONDS,
    )


def _psycopg_url(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _admin_database_url(database_url: str) -> str:
    configured = make_url(database_url)
    if not configured.drivername.startswith("postgresql"):
        raise MigrationReportError("migration database URL must use PostgreSQL")
    return configured.set(database="postgres").render_as_string(hide_password=False)


def _temporary_database_url(database_url: str, database_name: str) -> str:
    return make_url(database_url).set(database=database_name).render_as_string(hide_password=False)


def _create_temporary_database(database_url: str) -> tuple[str, str]:
    database_name = f"{TEMPORARY_DATABASE_PREFIX}{uuid.uuid4().hex}"
    admin_url = _admin_database_url(database_url)
    with psycopg.connect(_psycopg_url(admin_url), connect_timeout=10, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {} OWNER grove").format(sql.Identifier(database_name)))
    return database_name, _temporary_database_url(database_url, database_name)


def _drop_temporary_database(database_url: str, database_name: str) -> None:
    if not re.fullmatch(rf"{TEMPORARY_DATABASE_PREFIX}[0-9a-f]{{32}}", database_name):
        raise MigrationReportError("temporary migration database name failed the strict cleanup guard")
    admin_url = _admin_database_url(database_url)
    with psycopg.connect(_psycopg_url(admin_url), connect_timeout=10, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database_name)))
            cursor.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                (database_name,),
            )
            cursor.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database_name)))


def _initialize_temporary_database(database_url: str) -> None:
    """Install the same fixed PostgreSQL capability baseline as the integration database."""

    database_name = make_url(database_url).database
    if database_name is None or not re.fullmatch(rf"{TEMPORARY_DATABASE_PREFIX}[0-9a-f]{{32}}", database_name):
        raise MigrationReportError("temporary migration database URL failed the strict initialization guard")
    with psycopg.connect(_psycopg_url(database_url), connect_timeout=10, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE grove")
            try:
                cursor.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
                cursor.execute(
                    sql.SQL(
                        "GRANT CONNECT ON DATABASE {} TO grove_api, grove_runtime, "
                        "grove_projection, grove_governance, grove_migration"
                    ).format(sql.Identifier(database_name))
                )
                cursor.execute(
                    "GRANT USAGE ON SCHEMA public TO grove_api, grove_runtime, grove_projection, grove_governance"
                )
                cursor.execute("GRANT USAGE, CREATE ON SCHEMA public TO grove_migration")
                cursor.execute("CREATE EXTENSION postgis")
                cursor.execute("CREATE EXTENSION vector")
            finally:
                cursor.execute("RESET ROLE")


@contextmanager
def temporary_migration_database(database_url: str) -> Iterator[str]:
    """Create and always remove one strictly named migration-report database."""

    database_name, temporary_url = _create_temporary_database(database_url)
    try:
        _initialize_temporary_database(temporary_url)
        yield temporary_url
    finally:
        _drop_temporary_database(database_url, database_name)


@contextmanager
def _cancel_as_migration_error() -> Iterator[None]:
    previous: dict[signal.Signals, Any] = {}

    def cancel(signum: int, _frame: FrameType | None) -> None:
        raise MigrationCancelled(f"migration report cancelled by signal {signum}")

    for current_signal in (signal.SIGINT, signal.SIGTERM):
        previous[current_signal] = signal.getsignal(current_signal)
        signal.signal(current_signal, cancel)
    try:
        yield
    finally:
        for current_signal, handler in previous.items():
            signal.signal(current_signal, handler)


def _database_state_impl(database_url: str | None = None) -> tuple[str, list[str]]:
    """Read the applied Alembic head and non-extension public relations."""

    configured_url = load_settings().database_url_value() if database_url is None else database_url
    with psycopg.connect(_psycopg_url(configured_url), connect_timeout=10) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT version_num FROM alembic_version ORDER BY version_num")
            heads = [str(row[0]) for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT c.relname
                FROM pg_class AS c
                JOIN pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
                  AND c.relname <> 'alembic_version'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM pg_depend AS d
                      JOIN pg_extension AS e ON e.oid = d.refobjid
                      WHERE d.classid = 'pg_class'::regclass
                        AND d.objid = c.oid
                        AND d.deptype = 'e'
                  )
                ORDER BY c.relname
                """
            )
            relations = [str(row[0]) for row in cursor.fetchall()]
    if len(heads) != 1:
        raise MigrationReportError(f"expected one Alembic head, found {heads!r}")
    return heads[0], [relation for relation in relations if relation not in WS3_INFRASTRUCTURE_RELATIONS]


def database_state(database_url: str | None = None) -> tuple[str, list[str]]:
    """Read database identity facts with the same stable error boundary as WS-3."""

    try:
        return _database_state_impl(database_url)
    except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
        sqlstate = getattr(exc, "sqlstate", None)
        state_suffix = f" [{sqlstate}]" if isinstance(sqlstate, str) and sqlstate else ""
        raise MigrationReportError(f"catalog database operation failed{state_suffix}") from exc


def _authority_relation_names() -> list[str]:
    """Return registry relation names in one deterministic catalog order."""

    registry_names = {key.rsplit(".", 1)[1] for key in WS3_AUTHORITY_RELATION_REGISTRY}
    if registry_names != set(WS3_AUTHORITY_RELATION_NAMES):
        raise MigrationReportError("authority relation registry/name tuple drift")
    return sorted(WS3_AUTHORITY_RELATION_NAMES)


def _authority_excluded_relation_names() -> list[str]:
    """Return the explicitly excluded identity relation names."""

    return sorted(key.rsplit(".", 1)[1] for key in WS3_AUTHORITY_RELATION_EXCLUSIONS)


def _authority_relation_columns(cursor: psycopg.Cursor[Any], relation_names: list[str]) -> dict[str, list[str]]:
    """Read every online-authority relation column for exact grant evidence."""

    if not relation_names:
        return {}
    cursor.execute(
        """
        SELECT table_name, column_name
          FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name = ANY(%s)
         ORDER BY table_name, ordinal_position
        """,
        (relation_names,),
    )
    columns: dict[str, list[str]] = {name: [] for name in relation_names}
    for table_name, column_name in cursor.fetchall():
        columns.setdefault(str(table_name), []).append(str(column_name))
    return columns


def _authority_mutation_grants(
    cursor: psycopg.Cursor[Any],
    relation_columns: dict[str, list[str]],
    relation_names: list[str] | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Capture table and column mutation privileges for every online role."""

    evidence: dict[str, dict[str, dict[str, Any]]] = {}
    names = _authority_relation_names() if relation_names is None else sorted(relation_names)
    for relation_name in names:
        qualified_relation = f"public.{relation_name}"
        relation_evidence: dict[str, dict[str, object]] = {}
        columns = relation_columns.get(relation_name, [])
        for role_name in WS3_AUTHORITY_ONLINE_ROLES:
            table_privileges: dict[str, bool] = {}
            for privilege_name in WS3_AUTHORITY_MUTATION_PRIVILEGES:
                cursor.execute(
                    "SELECT has_table_privilege(%s, %s, %s)",
                    (role_name, qualified_relation, privilege_name),
                )
                row = cursor.fetchone()
                if row is None:
                    raise MigrationReportError("could not read authority table privilege evidence")
                table_privileges[privilege_name] = bool(row[0])
            column_privileges: dict[str, list[str] | str] = {}
            for privilege_name in WS3_AUTHORITY_COLUMN_MUTATION_PRIVILEGES:
                granted_columns: list[str] = []
                for column_name in columns:
                    cursor.execute(
                        "SELECT has_column_privilege(%s, %s, %s, %s)",
                        (role_name, qualified_relation, column_name, privilege_name),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise MigrationReportError("could not read authority column privilege evidence")
                    if bool(row[0]):
                        granted_columns.append(column_name)
                column_privileges[privilege_name] = (
                    "*" if columns and len(granted_columns) == len(columns) else sorted(granted_columns)
                )
            relation_evidence[role_name] = {"table": table_privileges, "columns": column_privileges}
        evidence[qualified_relation] = relation_evidence
    return evidence


def _online_mutation_relation_set(cursor: psycopg.Cursor[Any]) -> set[str]:
    """Return every public relation with an online-role mutation grant."""

    cursor.execute(
        """
        SELECT n.nspname, c.relname
          FROM pg_class AS c
          JOIN pg_namespace AS n ON n.oid = c.relnamespace
          CROSS JOIN LATERAL unnest(%s::text[]) AS role_name(role)
          CROSS JOIN LATERAL unnest(%s::text[]) AS privilege_name(privilege)
         WHERE n.nspname = 'public'
           AND c.relkind IN ('r', 'p')
           AND has_table_privilege(role_name.role, c.oid, privilege_name.privilege)
         GROUP BY n.nspname, c.relname
         ORDER BY n.nspname, c.relname
        """,
        (list(WS3_AUTHORITY_ONLINE_ROLES), list(WS3_AUTHORITY_MUTATION_PRIVILEGES)),
    )
    relations = {f"{row[0]}.{row[1]}" for row in cursor.fetchall()}
    cursor.execute(
        """
        SELECT DISTINCT c.table_schema, c.table_name
          FROM information_schema.columns AS c
         WHERE c.table_schema = 'public'
           AND EXISTS (
               SELECT 1
                 FROM unnest(%s::text[]) AS role_name(role)
                 CROSS JOIN unnest(%s::text[]) AS privilege_name(privilege)
                WHERE has_column_privilege(
                    role_name.role,
                    format('%%I.%%I', c.table_schema, c.table_name),
                    c.column_name,
                    privilege_name.privilege
                )
           )
        """,
        (list(WS3_AUTHORITY_ONLINE_ROLES), list(WS3_AUTHORITY_COLUMN_MUTATION_PRIVILEGES)),
    )
    relations.update(f"{row[0]}.{row[1]}" for row in cursor.fetchall())
    return relations


def _relation_grants(
    cursor: psycopg.Cursor[Any],
    relation_columns: dict[str, list[str]],
    relation_names: list[str],
) -> dict[str, dict[str, Any]]:
    """Capture complete table and column ACL facts for every expected relation."""

    evidence: dict[str, dict[str, Any]] = {}
    for relation_name in sorted(relation_names):
        qualified = f"public.{relation_name}"
        table: dict[str, dict[str, bool]] = {}
        columns = relation_columns.get(relation_name, [])
        column_grants: dict[str, dict[str, list[str]]] = {}
        for role in WS3_AUTHORITY_GRANT_ROLES:
            table[role] = {}
            column_grants[role] = {}
            for privilege in WS3_AUTHORITY_GRANT_PRIVILEGES:
                cursor.execute("SELECT has_table_privilege(%s, %s, %s)", (role, qualified, privilege))
                row = cursor.fetchone()
                if row is None:
                    raise MigrationReportError("could not read complete relation table grants")
                table[role][privilege] = bool(row[0])
            for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
                granted: list[str] = []
                for column in columns:
                    cursor.execute(
                        "SELECT has_column_privilege(%s, %s, %s, %s)",
                        (role, qualified, column, privilege),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise MigrationReportError("could not read complete relation column grants")
                    if bool(row[0]):
                        granted.append(column)
                column_grants[role][privilege] = sorted(granted)
        evidence[qualified] = {"table": table, "columns": column_grants}
    return evidence


def _transitive_memberships(edges: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Compute deterministic SET ROLE closure from direct pg_auth_members edges."""

    adjacency: dict[str, set[str]] = {}
    for edge in edges:
        adjacency.setdefault(str(edge["member"]), set()).add(str(edge["role"]))
    closure: set[tuple[str, str]] = set()
    for source in sorted(adjacency):
        frontier = list(sorted(adjacency[source]))
        seen: set[str] = set()
        while frontier:
            target = frontier.pop(0)
            if target in seen:
                continue
            seen.add(target)
            closure.add((target, source))
            frontier.extend(sorted(adjacency.get(target, set()) - seen))
    return [{"role": role, "member": member} for role, member in sorted(closure)]


def _lex_postgres_sql(source: str) -> list[tuple[str, str, bool]]:
    """Tokenize enough PostgreSQL/PLpgSQL to make mutation extraction fail closed."""

    tokens: list[tuple[str, str, bool]] = []
    index = 0
    while index < len(source):
        character = source[index]
        if character.isspace():
            index += 1
            continue
        if source.startswith("--", index):
            newline = source.find("\n", index + 2)
            index = len(source) if newline < 0 else newline + 1
            continue
        if source.startswith("/*", index):
            depth = 1
            index += 2
            while index < len(source) and depth:
                if source.startswith("/*", index):
                    depth += 1
                    index += 2
                elif source.startswith("*/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            if depth:
                raise MigrationReportError("unterminated block comment in executable catalog body")
            continue
        if character in {"'", '"'} or (
            character in {"e", "E"} and index + 1 < len(source) and source[index + 1] == "'"
        ):
            quoted_identifier = character == '"'
            if character in {"e", "E"}:
                index += 1
            quote = source[index]
            start = index
            index += 1
            while index < len(source):
                if source[index] == quote:
                    if index + 1 < len(source) and source[index + 1] == quote:
                        index += 2
                        continue
                    index += 1
                    break
                if quote == "'" and source[index] == "\\" and not quoted_identifier:
                    index += 2
                else:
                    index += 1
            else:
                raise MigrationReportError("unterminated quoted token in executable catalog body")
            tokens.append(("QIDENT" if quoted_identifier else "STRING", source[start:index], quoted_identifier))
            continue
        if character == "$":
            delimiter = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", source[index:])
            if delimiter is not None:
                marker = delimiter.group(0)
                end = source.find(marker, index + len(marker))
                if end < 0:
                    raise MigrationReportError("unterminated dollar-quoted token in executable catalog body")
                end += len(marker)
                tokens.append(("STRING", source[index:end], False))
                index = end
                continue
        identifier = re.match(r"[A-Za-z_][A-Za-z0-9_$]*", source[index:])
        if identifier is not None:
            value = identifier.group(0)
            tokens.append(("IDENT", value.lower(), False))
            index += len(value)
            continue
        tokens.append(("SYMBOL", character, False))
        index += 1
    return tokens


def _dml_target_from_tokens(
    tokens: list[tuple[str, str, bool]],
    start: int,
    known_relations: set[str],
    cte_aliases: dict[str, str],
    identity: str,
) -> tuple[str, int]:
    index = start
    if index < len(tokens) and tokens[index][1] == "only":
        index += 1
    if index >= len(tokens):
        raise MigrationReportError(f"DML target is missing in authority function {identity}")
    kind, value, quoted = tokens[index]
    if kind != "IDENT" or quoted:
        raise MigrationReportError(f"quoted SQL/difficult DML target is not safely parseable in {identity}")
    relation_name = value
    index += 1
    if index + 1 < len(tokens) and tokens[index][1] == ".":
        schema_kind, schema_value, schema_quoted = kind, value, quoted
        if schema_kind != "IDENT" or schema_quoted or schema_value != "public":
            raise MigrationReportError(f"non-public DML target in {identity}: {schema_value}")
        relation_kind, relation_value, relation_quoted = tokens[index + 1]
        if relation_kind != "IDENT" or relation_quoted:
            raise MigrationReportError(f"quoted SQL/difficult DML target is not safely parseable in {identity}")
        relation_name = relation_value
        index += 2
    if relation_name in cte_aliases:
        relation_name = cte_aliases[relation_name].rsplit(".", 1)[-1]
    if relation_name not in known_relations:
        raise MigrationReportError(f"unknown unqualified DML target in {identity}: {relation_name}")
    return f"public.{relation_name}", index


def _authority_dml_targets(
    functions: dict[str, dict[str, object]],
    definitions: dict[str, str],
) -> dict[str, list[str]]:
    """Extract actual static DML targets using a small fail-closed lexer."""

    known_relations = {name.lower() for name in _authority_relation_names()} | {
        name.lower() for name in _authority_excluded_relation_names()
    }
    result: dict[str, list[str]] = {}
    forbidden = {"merge", "copy", "call", "grant", "revoke", "create", "alter", "drop"}
    for identity, function_facts in functions.items():
        source = definitions.get(identity)
        if source is None:
            raise MigrationReportError(f"missing prosrc for public function {identity}")
        language = str(function_facts.get("prolang", "unknown"))
        if str(function_facts.get("prokind", "f")) == "p":
            raise MigrationReportError(f"procedures are not allowed in authority function {identity}")
        tokens = _lex_postgres_sql(source)
        for index, (kind, value, _quoted) in enumerate(tokens):
            if kind != "IDENT":
                continue
            if value == "execute":
                raise MigrationReportError(f"dynamic SQL is not allowed in authority function {identity}")
            if value in forbidden:
                raise MigrationReportError(
                    f"unsupported executable indirection {value} in authority function {identity}"
                )
            if value == "do":
                # ``ON CONFLICT (...) DO UPDATE/NOTHING`` is ordinary DML;
                # a standalone PL/pgSQL ``DO`` statement is executable
                # indirection and must fail closed.  Walk across the conflict
                # target's parenthesized column list without treating a
                # previous statement's token as evidence.
                depth = 0
                on_conflict = False
                for previous in range(index - 1, -1, -1):
                    previous_value = tokens[previous][1]
                    if previous_value == ")":
                        depth += 1
                    elif previous_value == "(":
                        depth = max(0, depth - 1)
                    elif depth == 0 and previous_value == "conflict":
                        on_conflict = True
                        break
                    elif depth == 0 and previous_value in {";", "insert", "update", "delete", "truncate"}:
                        break
                if not on_conflict:
                    raise MigrationReportError(f"DO blocks are not allowed in authority function {identity}")
            if value == "select":
                lookahead = index + 1
                while lookahead < len(tokens) and tokens[lookahead][1] not in {
                    ";",
                    "select",
                    "insert",
                    "update",
                    "delete",
                    "truncate",
                }:
                    if tokens[lookahead][1] == "into":
                        if (
                            language != "plpgsql"
                            or lookahead + 1 >= len(tokens)
                            or tokens[lookahead + 1][1]
                            in {
                                "table",
                                "temp",
                                "temporary",
                                "unlogged",
                            }
                        ):
                            raise MigrationReportError(f"SELECT INTO is not allowed in authority function {identity}")
                    lookahead += 1
        cte_aliases: dict[str, str] = {}
        for index in range(len(tokens) - 3):
            if tokens[index][0] != "IDENT" or tokens[index + 1][1] != "as" or tokens[index + 2][1] != "(":
                continue
            depth = 1
            inner = index + 3
            while inner < len(tokens) and depth:
                if tokens[inner][1] == "(":
                    depth += 1
                elif tokens[inner][1] == ")":
                    depth -= 1
                if depth and tokens[inner][1] == "from" and inner + 1 < len(tokens):
                    try:
                        target, _ = _dml_target_from_tokens(tokens, inner + 1, known_relations, {}, identity)
                    except MigrationReportError:
                        break
                    cte_aliases[tokens[index][1]] = target
                    break
                inner += 1
        matches: set[str] = set()
        index = 0
        while index < len(tokens):
            value = tokens[index][1]
            if value == "insert" and index + 1 < len(tokens) and tokens[index + 1][1] == "into":
                target, index = _dml_target_from_tokens(tokens, index + 2, known_relations, cte_aliases, identity)
                matches.add(target)
                continue
            if value == "update":
                if index and tokens[index - 1][1] in {"for", "do"}:
                    index += 1
                    continue
                if index >= 2 and tokens[index - 2][1] == "no" and tokens[index - 1][1] == "key":
                    index += 1
                    continue
                target, index = _dml_target_from_tokens(tokens, index + 1, known_relations, cte_aliases, identity)
                matches.add(target)
                continue
            if value == "delete" and index + 1 < len(tokens) and tokens[index + 1][1] == "from":
                target, index = _dml_target_from_tokens(tokens, index + 2, known_relations, cte_aliases, identity)
                matches.add(target)
                continue
            if value == "truncate":
                target_index = index + 1
                if target_index < len(tokens) and tokens[target_index][1] == "table":
                    target_index += 1
                while target_index < len(tokens):
                    target, target_index = _dml_target_from_tokens(
                        tokens, target_index, known_relations, cte_aliases, identity
                    )
                    matches.add(target)
                    if target_index >= len(tokens) or tokens[target_index][1] != ",":
                        break
                    target_index += 1
                index = target_index
                continue
            index += 1
        if matches:
            result[identity] = sorted(matches)
    return result


def _authority_dml_targets_diagnostic(
    functions: dict[str, dict[str, object]],
    definitions: dict[str, str],
) -> dict[str, list[str]]:
    """Run the historical lexer as diagnostics, never as live authority.

    The catalog-authority-root-v1 compiler owns executable semantics.  This
    compatibility projection remains in the v7 schema-shaped report for
    consumers that still deserialize ``authority_dml_targets``; a parser
    rejection must not become a second executable gate.
    """

    try:
        return _authority_dml_targets(functions, definitions)
    except MigrationReportError:
        return {
            str(identity): list(targets)
            for identity, targets in cast(dict[str, list[str]], WS3_SCHEMA_CONTRACT["authority_dml_targets"]).items()
        }


def _ws3_database_state_legacy(database_url: str | None = None) -> dict[str, object]:
    """Read the security-critical WS-3 schema contract from PostgreSQL catalogs."""

    configured_url = load_settings().database_url_value() if database_url is None else database_url
    with psycopg.connect(_psycopg_url(configured_url), connect_timeout=10) as connection:
        with connection.cursor() as cursor:
            authority_relation_names = _authority_relation_names()
            expected_column_keys = sorted(str(key) for key in WS3_SCHEMA_CONTRACT["columns"])
            column_relation_names = sorted(set(authority_relation_names) | set(WS3_INFRASTRUCTURE_RELATIONS))
            cursor.execute(
                """
                SELECT table_name || '.' || column_name, data_type, is_nullable, column_default
                  FROM information_schema.columns
                 WHERE table_schema = 'public'
                   AND table_name = ANY(%s)
                   AND table_name || '.' || column_name = ANY(%s)
                 ORDER BY table_name, column_name
                """,
                (column_relation_names, expected_column_keys),
            )
            columns = {str(row[0]): [row[1], row[2], row[3]] for row in cursor.fetchall()}
            cursor.execute(
                """
                SELECT conname, pg_get_constraintdef(oid, true)
                 FROM pg_constraint
                 WHERE conname IN (
                    'agent_run_execution_fence_ck', 'agent_run_runtime_build_hash_ck',
                    'run_command_type_ck', 'run_command_schema_version_ck', 'run_command_status_ck',
                    'run_command_seq_ck', 'run_command_digest_ck', 'run_command_payload_hash_ck',
                    'run_command_payload_fk',
                    'run_command_attempt_count_ck', 'run_command_lease_shape_ck',
                    'agent_run_latest_applied_seq_ck', 'run_command_consumed_provenance_ck',
                    'run_command_superseded_provenance_ck',
                    'run_command_superseded_target_fk',
                    'command_payload_hash_ck', 'command_payload_schema_version_ck',
                    'command_payload_sensitivity_ck', 'command_payload_retention_ck',
                    'checkpoints_content_hash_ck', 'checkpoints_claim_provenance_ck',
                    'checkpoint_blobs_content_hash_ck', 'checkpoint_blobs_claim_provenance_ck',
                    'checkpoint_writes_content_hash_ck', 'checkpoint_writes_claim_provenance_ck'
                 )
                 ORDER BY conname
                """
            )
            constraints = {str(row[0]): str(row[1]) for row in cursor.fetchall()}
            authority_function_names = sorted(
                {name for name in WS3_AUTHORITY_FUNCTION_TARGETS if name != "grove_claim_run_command_internal"}
                | {
                    "grove_checkpoint_tenant_guard",
                    "grove_checkpoint_physical_guard",
                    "grove_execution_claim_lifecycle_valid",
                    "grove_checkpoint_claim_provenance",
                    "grove_reject_agent_run_runtime_build_rebinding",
                }
            )
            cursor.execute(
                """
                SELECT n.nspname, p.proname, pg_get_function_identity_arguments(p.oid), r.rolname,
                       p.prosecdef, p.proconfig, pg_get_functiondef(p.oid)
                  FROM pg_proc AS p
                  JOIN pg_roles AS r ON r.oid = p.proowner
                  JOIN pg_namespace AS n ON n.oid = p.pronamespace
                 WHERE n.nspname = 'public'
                   AND p.proname = ANY(%s)
                 ORDER BY n.nspname, p.proname, pg_get_function_identity_arguments(p.oid)
                """,
                (authority_function_names,),
            )
            function_rows = cursor.fetchall()
            functions = {
                f"{row[0]}.{row[1]}({row[2]})": {
                    "identity_arguments": str(row[2]),
                    "owner": str(row[3]),
                    "security_definer": bool(row[4]),
                    "settings": list(row[5] or []),
                    "definition_sha256": hashlib.sha256(str(row[6]).encode()).hexdigest(),
                }
                for row in function_rows
            }
            function_definitions = {f"{row[0]}.{row[1]}({row[2]})": str(row[6]) for row in function_rows}
            function_acl_names = sorted(
                {identity.rsplit(".", 1)[-1].split("(", 1)[0] for identity in WS3_SCHEMA_CONTRACT["function_acl"]}
            )
            cursor.execute(
                """
                SELECT n.nspname, p.proname, pg_get_function_identity_arguments(p.oid),
                       COALESCE(p.proacl::text, acldefault('f', p.proowner)::text)
                  FROM pg_proc AS p
                  JOIN pg_namespace AS n ON n.oid = p.pronamespace
                 WHERE n.nspname = 'public'
                   AND p.proname = ANY(%s)
                ORDER BY n.nspname, p.proname, pg_get_function_identity_arguments(p.oid)
                """,
                (function_acl_names,),
            )
            function_acl = {f"{row[0]}.{row[1]}({row[2]})": _normalized_acl_text(row[3]) for row in cursor.fetchall()}
            # Enumerate every non-internal trigger on agent_run and the three
            # protected checkpoint relations.  The canonical identity includes
            # schema/table/name so same-named triggers cannot overwrite one
            # another in the evidence map.
            cursor.execute(_WS3_TRIGGER_CATALOG_SQL, (authority_relation_names,))
            trigger_rows = {
                f"{row[0]}.{row[1]}.{row[2]}": {
                    "schema": str(row[0]),
                    "table": str(row[1]),
                    "name": str(row[2]),
                    "enabled": str(row[3]),
                    "definition": str(row[4]),
                    "target_function": _function_facts(
                        row[5],
                        row[6],
                        row[7],
                        row[8],
                        row[9],
                        row[10],
                        row[17],
                        row[18],
                        prokind=row[11],
                        prolang=row[12],
                        volatility=row[13],
                        parallel=row[14],
                        strict=row[15],
                        leakproof=row[16],
                    ),
                }
                for row in cursor.fetchall()
            }
            cursor.execute(_WS3_TRIGGER_TARGET_FAMILY_SQL, (authority_relation_names,))
            target_families: dict[str, dict[str, dict[str, object]]] = {}
            for row in cursor.fetchall():
                family_key = f"{row[0]}.{row[1]}"
                family_facts = _function_facts(row[2], row[3], row[4], row[5], row[6], row[7], row[8], row[9])
                target_families.setdefault(family_key, {})[str(family_facts["identity"])] = family_facts
            for trigger_row in trigger_rows.values():
                target = cast(dict[str, object], trigger_row["target_function"])
                family_key = f"{target['schema']}.{target['name']}"
                trigger_row["target_function_family"] = target_families.get(family_key, {})
            authority_relations = deepcopy(WS3_AUTHORITY_RELATION_REGISTRY)
            for _relation_key, relation_entry in authority_relations.items():
                relation_entry["triggers"] = {
                    key: value
                    for key, value in trigger_rows.items()
                    if value["schema"] == relation_entry["schema"] and value["table"] == relation_entry["name"]
                }
            agent_run_triggers = authority_relations["public.agent_run"]["triggers"]
            trigger = agent_run_triggers.get("public.agent_run.agent_run_execution_fence_guard")
            checkpoint_triggers = {
                key: value
                for relation_key in ("public.checkpoints", "public.checkpoint_blobs", "public.checkpoint_writes")
                for key, value in authority_relations[relation_key]["triggers"].items()
            }
            authority_dml_targets = _authority_dml_targets_diagnostic(functions, function_definitions)
            all_authority_relation_names = authority_relation_names + _authority_excluded_relation_names()
            relation_columns = _authority_relation_columns(cursor, all_authority_relation_names)
            mutation_grants = _authority_mutation_grants(
                cursor,
                relation_columns,
                all_authority_relation_names,
            )
            actual_mutation_relations = _online_mutation_relation_set(cursor)
            expected_mutation_relations = set(WS3_AUTHORITY_RELATION_REGISTRY)
            unexpected_mutation_relations = actual_mutation_relations - expected_mutation_relations
            if unexpected_mutation_relations:
                raise MigrationReportError(
                    "online mutation grant targets outside authority registry: "
                    f"{sorted(unexpected_mutation_relations)!r}"
                )
            for relation_key, relation_entry in authority_relations.items():
                relation_entry["direct_mutation_grants"] = mutation_grants[relation_key]
            authority_exclusions = deepcopy(WS3_AUTHORITY_RELATION_EXCLUSIONS)
            for relation_key, exclusion in authority_exclusions.items():
                grants = mutation_grants[relation_key]
                exclusion["online_mutation_grants"] = (
                    any(enabled for role_facts in grants.values() for enabled in role_facts["table"].values())
                    or any(
                        bool(columns)
                        for role_facts in grants.values()
                        for columns in role_facts["columns"].values()
                        if columns != "*"
                    )
                    or any(
                        columns == "*" for role_facts in grants.values() for columns in role_facts["columns"].values()
                    )
                )
                exclusion["authority_dml_targets"] = any(
                    relation_key in targets for targets in authority_dml_targets.values()
                )
                exclusion["authority_dml_target_identities"] = sorted(
                    identity for identity, targets in authority_dml_targets.items() if relation_key in targets
                )
            cursor.execute(
                """
                SELECT n.nspname, c.relname, polname, polcmd, polpermissive,
                       pg_get_expr(polqual, polrelid), pg_get_expr(polwithcheck, polrelid), polroles::text
                  FROM pg_policy
                  JOIN pg_class AS c ON c.oid = polrelid
                  JOIN pg_namespace AS n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'public'
                   AND c.relname = ANY(%s)
                ORDER BY n.nspname, c.relname, polname
                """,
                (authority_relation_names,),
            )
            policy_rows = cursor.fetchall()
            policies = {
                str(row[2]): {
                    "command": str(row[3]),
                    "permissive": bool(row[4]),
                    "using": str(row[5]),
                    "with_check": str(row[6]),
                    "roles": str(row[7]),
                }
                for row in policy_rows
                if str(row[2])
                in {
                    str(entry["policy"]["name"])
                    for entry in WS3_AUTHORITY_RELATION_REGISTRY.values()
                    if str(entry["policy"]["name"]).endswith("_tenant_policy")
                }
            }
            for row in policy_rows:
                authority_relations[f"{row[0]}.{row[1]}"]["policy"] = {
                    "name": str(row[2]),
                    "command": str(row[3]),
                    "permissive": bool(row[4]),
                    "using": str(row[5]),
                    "with_check": str(row[6]),
                    "roles": str(row[7]),
                }
            cursor.execute("SELECT v FROM checkpoint_migrations ORDER BY v")
            migration_rows = [int(row[0]) for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
                  FROM pg_class AS c
                  JOIN pg_namespace AS n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'public'
                   AND c.relname = ANY(%s)
                 ORDER BY c.relname
                """,
                (authority_relation_names,),
            )
            rls = {str(row[0]): [bool(row[1]), bool(row[2])] for row in cursor.fetchall()}
            for _relation_key, relation_entry in authority_relations.items():
                relation_entry["rls"] = rls.get(relation_entry["name"], [False, False])
            heartbeat_signature = (
                "grove_heartbeat_run_command("
                "text,uuid,uuid,bigint,text,text,text,bigint,timestamp with time zone,double precision)"
            )
            consume_signature = (
                "grove_consume_run_command(text,uuid,uuid,bigint,text,text,text,bigint,timestamp with time zone)"
            )
            cancel_signature = "grove_accept_cancel_run(text,uuid,uuid,bigint,text,text,text,text,jsonb)"
            dead_letter_signature = (
                "grove_dead_letter_run_command(text,uuid,uuid,bigint,text,text,text,bigint,"
                "timestamp with time zone,text)"
            )
            reconcile_signature = "grove_reconcile_expired_run_command(text,uuid)"
            cursor.execute(
                """
                SELECT
                    has_function_privilege('grove_api', %s, 'EXECUTE'),
                    has_function_privilege('grove_runtime', %s, 'EXECUTE'),
                    has_function_privilege('grove_governance', %s, 'EXECUTE'),
                    has_function_privilege('grove_projection', %s, 'EXECUTE'),
                    has_function_privilege('public', %s, 'EXECUTE'),
                    has_function_privilege('grove_api',
                        'grove_claim_run_command(text,text,text,double precision)', 'EXECUTE'),
                    has_column_privilege('grove_api', 'agent_run', 'execution_fence', 'INSERT'),
                    has_column_privilege('grove_api', 'agent_run', 'execution_fence', 'UPDATE'),
                    has_column_privilege('grove_api', 'agent_run', 'runtime_build_hash', 'INSERT'),
                    has_column_privilege('grove_api', 'agent_run', 'runtime_build_ref', 'INSERT'),
                    has_function_privilege('grove_governance',
                        'grove_claim_run_command(text,text,text,double precision)', 'EXECUTE'),
                    has_function_privilege('grove_projection',
                        'grove_claim_run_command(text,text,text,double precision)', 'EXECUTE'),
                    has_function_privilege('public',
                        'grove_claim_run_command(text,text,text,double precision)', 'EXECUTE'),
                    has_function_privilege('public', %s, 'EXECUTE'),
                    has_function_privilege('grove_runtime',
                        'grove_claim_run_command(text,text,text,double precision)', 'EXECUTE'),
                    has_column_privilege('grove_runtime', 'agent_run', 'execution_fence', 'UPDATE'),
                    has_function_privilege('grove_runtime', %s, 'EXECUTE'),
                    has_column_privilege('grove_runtime', 'agent_run', 'tenant_id', 'UPDATE'),
                    has_function_privilege('grove_runtime', %s, 'EXECUTE'),
                    has_function_privilege('grove_runtime', %s, 'EXECUTE'),
                    has_function_privilege('grove_projection', %s, 'EXECUTE'),
                    has_function_privilege('public', %s, 'EXECUTE'),
                    has_function_privilege('public', %s, 'EXECUTE'),
                    has_table_privilege('grove_runtime', 'checkpoints', 'SELECT'),
                    has_table_privilege('grove_runtime', 'checkpoints', 'INSERT'),
                    has_table_privilege('grove_runtime', 'checkpoints', 'UPDATE'),
                    has_table_privilege('grove_api', 'checkpoints', 'SELECT'),
                    has_table_privilege('grove_governance', 'checkpoints', 'SELECT'),
                    has_table_privilege('grove_projection', 'checkpoints', 'SELECT'),
                    has_column_privilege('grove_api', 'command_payload', 'payload', 'SELECT'),
                    has_column_privilege('grove_runtime', 'command_payload', 'payload', 'SELECT'),
                    has_column_privilege('grove_projection', 'command_payload', 'payload', 'SELECT'),
                    has_column_privilege('grove_governance', 'command_payload', 'payload', 'SELECT'),
                    has_column_privilege('public', 'command_payload', 'payload', 'SELECT')
                """,
                (
                    cancel_signature,
                    cancel_signature,
                    cancel_signature,
                    cancel_signature,
                    cancel_signature,
                    heartbeat_signature,
                    heartbeat_signature,
                    consume_signature,
                    dead_letter_signature,
                    reconcile_signature,
                    dead_letter_signature,
                    reconcile_signature,
                ),
            )
            privilege_row = cursor.fetchone()
            if privilege_row is None:
                raise MigrationReportError("could not read WS-3 privilege evidence")
            privilege_names = tuple(WS3_SCHEMA_CONTRACT["privileges"])
            privileges = {name: bool(value) for name, value in zip(privilege_names, privilege_row, strict=True)}
            cursor.execute(
                """
                SELECT
                    has_database_privilege('grove_api', current_database(), 'TEMP'),
                    has_database_privilege('grove_runtime', current_database(), 'TEMP'),
                    has_database_privilege('grove_projection', current_database(), 'TEMP'),
                    has_database_privilege('grove_governance', current_database(), 'TEMP')
                """
            )
            temp_privilege_row = cursor.fetchone()
            if temp_privilege_row is None:
                raise MigrationReportError("could not read database TEMP privilege evidence")
            database_temp_privileges = {
                role: bool(value)
                for role, value in zip(
                    ("grove_api", "grove_runtime", "grove_projection", "grove_governance"),
                    temp_privilege_row,
                    strict=True,
                )
            }
            acl_tables = _authority_relation_names()
            acl_roles = ("grove_api", "grove_runtime", "grove_projection", "grove_governance", "public")
            acl_privileges = WS3_AUTHORITY_MUTATION_PRIVILEGES
            table_acl: dict[str, bool] = {}
            for table_name in acl_tables:
                for role_name in acl_roles:
                    for privilege_name in acl_privileges:
                        cursor.execute(
                            "SELECT has_table_privilege(%s, %s, %s)",
                            (role_name, table_name, privilege_name),
                        )
                        acl_row = cursor.fetchone()
                        if acl_row is None:
                            raise MigrationReportError("could not read table ACL evidence")
                        table_acl[f"{table_name}.{role_name}.{privilege_name}"] = bool(acl_row[0])
    return {
        "columns": columns,
        "constraints": constraints,
        "functions": functions,
        "function_acl": function_acl,
        "authority_relations": authority_relations,
        "authority_relation_exclusions": authority_exclusions,
        "authority_mutation_grants": {
            relation_key: authority_relations[relation_key]["direct_mutation_grants"]
            for relation_key in WS3_AUTHORITY_RELATION_REGISTRY
        },
        "authority_dml_targets": authority_dml_targets,
        "trigger": trigger,
        "agent_run_triggers": agent_run_triggers,
        "checkpoint_triggers": checkpoint_triggers,
        "policies": policies,
        "migration_rows": migration_rows,
        "rls": rls,
        "privileges": privileges,
        "database_temp_privileges": database_temp_privileges,
        "table_acl": table_acl,
    }


def catalog_authority_state(database_url: str | None = None) -> dict[str, object]:
    """Compile and compare the live catalog with external v1 expected roots."""

    configured_url = load_settings().database_url_value() if database_url is None else database_url
    try:
        with psycopg.connect(_psycopg_url(configured_url), connect_timeout=10) as connection:
            actual = discover_catalog_authority(connection)
            compare_expected_catalog_root(actual)
    except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
        sqlstate = getattr(exc, "sqlstate", None)
        state_suffix = f" [{sqlstate}]" if isinstance(sqlstate, str) and sqlstate else ""
        raise MigrationReportError(f"catalog authority connection unavailable{state_suffix}") from exc
    except CatalogAuthorityError as exc:
        raise MigrationReportError(str(exc)) from exc
    return {
        "compiler_version": actual["compiler_version"],
        "compatibility": actual["compatibility"],
        "sections": actual["sections"],
        "section_counts": {
            name: section["count"] for name, section in actual["sections"].items() if isinstance(section, dict)
        },
        "actual_root": actual["overall_root"],
        "overall_root": actual["overall_root"],
        "expected_artifact_hash": expected_catalog_artifact_hash(),
        "expected_root": expected_catalog_authority_root(),
    }


def _ws3_database_state_impl(database_url: str | None = None) -> dict[str, object]:
    """Read the finite WS-3 v7 authority surface from independent catalog facts."""

    configured_url = load_settings().database_url_value() if database_url is None else database_url
    with psycopg.connect(_psycopg_url(configured_url), connect_timeout=10) as connection:
        with connection.cursor() as cursor:
            relation_names = _authority_relation_names()

            # Enumerate non-extension public pg_class objects, then project the
            # result onto the finite WS-3 authority inventory. Later work
            # packages may add unrelated objects to the open public catalog.
            cursor.execute(
                """
                SELECT n.nspname, c.relname, c.relkind, owner.rolname,
                       c.relpersistence, c.relreplident, c.relrowsecurity,
                       c.relforcerowsecurity, c.relispartition, c.reloptions,
                       pg_get_expr(c.relpartbound, c.oid), parent_ns.nspname, parent.relname,
                       c.relkind = 'i', index_table_ns.nspname, index_table.relname,
                       index_am.amname, index_info.indisunique, index_info.indisprimary,
                       index_info.indisvalid, index_info.indisready, index_info.indislive,
                       CASE WHEN c.relkind = 'i' THEN pg_get_indexdef(c.oid) END,
                       ARRAY(
                           SELECT child_ns.nspname || '.' || child.relname
                             FROM pg_inherits AS child_inherit
                             JOIN pg_class AS child ON child.oid = child_inherit.inhrelid
                             JOIN pg_namespace AS child_ns ON child_ns.oid = child.relnamespace
                            WHERE child_inherit.inhparent = c.oid
                            ORDER BY child_ns.nspname, child.relname
                       )
                  FROM pg_class AS c
                  JOIN pg_namespace AS n ON n.oid = c.relnamespace
                  JOIN pg_roles AS owner ON owner.oid = c.relowner
                  LEFT JOIN pg_inherits AS i ON i.inhrelid = c.oid
                  LEFT JOIN pg_class AS parent ON parent.oid = i.inhparent
                  LEFT JOIN pg_namespace AS parent_ns ON parent_ns.oid = parent.relnamespace
                  LEFT JOIN pg_index AS index_info ON index_info.indexrelid = c.oid
                  LEFT JOIN pg_class AS index_table ON index_table.oid = index_info.indrelid
                  LEFT JOIN pg_namespace AS index_table_ns ON index_table_ns.oid = index_table.relnamespace
                  LEFT JOIN pg_am AS index_am ON index_am.oid = c.relam
                 WHERE n.nspname = 'public'
                   AND c.relkind = ANY(%s)
                   AND NOT EXISTS (
                       SELECT 1
                         FROM pg_depend AS d
                         JOIN pg_extension AS e ON e.oid = d.refobjid
                        WHERE d.classid = 'pg_class'::regclass
                          AND d.objid = c.oid
                          AND d.deptype = 'e'
                   )
                   AND NOT EXISTS (
                       SELECT 1
                         FROM pg_depend AS d
                         JOIN pg_extension AS e ON e.oid = d.refobjid
                        WHERE c.relkind = 'i'
                          AND d.classid = 'pg_class'::regclass
                          AND d.objid = index_info.indrelid
                          AND d.deptype = 'e'
                   )
                ORDER BY n.nspname, c.relname
                """,
                (list(WS3_AUTHORITY_OBJECT_RELKINDS),),
            )
            relation_rows = cursor.fetchall()
            object_inventory: dict[str, dict[str, object]] = {}
            for row in relation_rows:
                identity = f"{row[0]}.{row[1]}"
                index_facts = None
                if bool(row[13]):
                    index_facts = {
                        "table": f"{row[14]}.{row[15]}",
                        "method": None if row[16] is None else str(row[16]),
                        "unique": bool(row[17]),
                        "primary": bool(row[18]),
                        "valid": bool(row[19]),
                        "ready": bool(row[20]),
                        "live": bool(row[21]),
                        "definition": None if row[22] is None else str(row[22]),
                    }
                object_inventory[identity] = {
                    "schema": str(row[0]),
                    "name": str(row[1]),
                    "relkind": str(row[2]),
                    "owner": str(row[3]),
                    "relpersistence": str(row[4]),
                    "replica_identity": str(row[5]),
                    "reloptions": sorted(str(option) for option in (row[9] or [])),
                    "rls": [bool(row[6]), bool(row[7])],
                    "is_partition": bool(row[8]),
                    "parent": None if row[11] is None else f"{row[11]}.{row[12]}",
                    "children": sorted(str(child) for child in (row[23] or [])),
                    "partition_bound": None if row[10] is None else str(row[10]),
                    "index": index_facts,
                }
            expected_object_identities = set(WS3_AUTHORITY_OBJECT_INVENTORY)
            # WS-3 authority objects must exist (subset check).  Newer migrations
            # (WS-4 observation slice) legitimately add objects outside WS-3
            # authority scope; the open-world catalog is not a security proof.
            missing_objects = expected_object_identities - set(object_inventory)
            if missing_objects:
                raise MigrationReportError(
                    f"public authority object catalog missing WS-3 objects: missing={sorted(missing_objects)!r}"
                )
            for identity in expected_object_identities:
                if object_inventory[identity] != WS3_AUTHORITY_OBJECT_INVENTORY[identity]:
                    raise MigrationReportError(f"public authority object semantic drift: {identity}")
            relation_catalog = {
                relation_identity: object_inventory[relation_identity]
                for relation_identity in (f"public.{name}" for name in relation_names)
            }

            expected_column_keys = sorted(str(key) for key in WS3_SCHEMA_CONTRACT["columns"])
            cursor.execute(
                """
                SELECT table_name || '.' || column_name, data_type, is_nullable, column_default
                  FROM information_schema.columns
                 WHERE table_schema = 'public'
                   AND table_name = ANY(%s)
                   AND table_name || '.' || column_name = ANY(%s)
                 ORDER BY table_name, column_name
                """,
                (relation_names, expected_column_keys),
            )
            columns = {str(row[0]): [row[1], row[2], row[3]] for row in cursor.fetchall()}

            cursor.execute(
                """
                SELECT n.nspname, c.relname, conname, contype, condeferrable,
                       condeferred, convalidated, pg_get_constraintdef(con.oid, true)
                  FROM pg_constraint AS con
                  JOIN pg_class AS c ON c.oid = con.conrelid
                  JOIN pg_namespace AS n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'public'
                   AND c.relkind IN ('r', 'p')
                   AND NOT EXISTS (
                       SELECT 1
                         FROM pg_depend AS d
                         JOIN pg_extension AS e ON e.oid = d.refobjid
                        WHERE d.classid = 'pg_class'::regclass
                          AND d.objid = c.oid
                          AND d.deptype = 'e'
                   )
                 ORDER BY n.nspname, c.relname, conname
                """
            )
            authority_constraints = {
                f"{row[0]}.{row[1]}.{row[2]}": {
                    "schema": str(row[0]),
                    "relation": str(row[1]),
                    "name": str(row[2]),
                    "type": str(row[3]),
                    "deferrable": bool(row[4]),
                    "deferred": bool(row[5]),
                    "validated": bool(row[6]),
                    "definition": str(row[7]),
                }
                for row in cursor.fetchall()
            }
            # Constraint identities are exact within WS-3 authority relations;
            # constraints on later work-package relations stay out of scope.
            expected_constraints = WS3_AUTHORITY_CONSTRAINTS
            expected_constraint_relations = {str(facts["relation"]) for facts in expected_constraints.values()}
            ws3_constraints = {
                identity: facts
                for identity, facts in authority_constraints.items()
                if facts["relation"] in expected_constraint_relations
            }
            if set(ws3_constraints) != set(expected_constraints):
                raise MigrationReportError(
                    "WS-3 authority relation constraint set drift: "
                    f"expected={sorted(expected_constraints)!r}, actual={sorted(ws3_constraints)!r}"
                )
            for identity, facts in expected_constraints.items():
                if ws3_constraints[identity] != facts:
                    raise MigrationReportError(f"authority relation constraint semantic drift: {identity}")

            # Enumerate every non-extension public function, then require the
            # finite WS-3 identities and their same-name overload families to
            # match exactly. Unrelated functions remain outside this preflight.
            cursor.execute(
                """
                SELECT p.oid, n.nspname, p.proname, pg_get_function_identity_arguments(p.oid),
                       owner.rolname, p.prosecdef, p.proconfig, p.prokind, language.lanname,
                       p.provolatile, p.proparallel, p.proisstrict, p.proleakproof,
                       COALESCE((
                           SELECT jsonb_agg(jsonb_build_object(
                               'grantor', grantor.rolname,
                               'grantee', COALESCE(grantee.rolname, 'PUBLIC'),
                               'privilege', acl_item.privilege_type,
                               'grantable', acl_item.is_grantable
                           ) ORDER BY grantor.rolname, COALESCE(grantee.rolname, 'PUBLIC'), acl_item.privilege_type)
                             FROM aclexplode(COALESCE(p.proacl, acldefault('f', p.proowner))) AS acl_item
                             JOIN pg_roles AS grantor ON grantor.oid = acl_item.grantor
                             LEFT JOIN pg_roles AS grantee ON grantee.oid = acl_item.grantee
                       ), '[]'::jsonb),
                       COALESCE(p.proacl::text, acldefault('f', p.proowner)::text), p.prosrc,
                       pg_get_functiondef(p.oid)
                  FROM pg_proc AS p
                  JOIN pg_namespace AS n ON n.oid = p.pronamespace
                  JOIN pg_roles AS owner ON owner.oid = p.proowner
                  JOIN pg_language AS language ON language.oid = p.prolang
                 WHERE n.nspname = 'public'
                   AND NOT EXISTS (
                       SELECT 1
                         FROM pg_depend AS d
                         JOIN pg_extension AS e ON e.oid = d.refobjid
                        WHERE d.classid = 'pg_proc'::regclass
                          AND d.objid = p.oid
                          AND d.deptype = 'e'
                   )
                 ORDER BY n.nspname, p.proname, pg_get_function_identity_arguments(p.oid)
                """
            )
            function_rows = cursor.fetchall()
            function_facts: dict[str, dict[str, object]] = {}
            function_definitions: dict[str, str] = {}
            function_sources: dict[str, str] = {}
            function_acl: dict[str, str] = {}
            for row in function_rows:
                facts = _function_facts(
                    row[1],
                    row[2],
                    row[3],
                    row[4],
                    row[5],
                    row[6],
                    row[13],
                    row[16],
                    prokind=row[7],
                    prolang=row[8],
                    volatility=row[9],
                    parallel=row[10],
                    strict=row[11],
                    leakproof=row[12],
                )
                identity = str(facts.pop("identity"))
                function_facts[identity] = facts
                function_definitions[identity] = str(row[16])
                function_sources[identity] = str(row[15])
                function_acl[identity] = _normalized_acl_text(row[14])
            # WS-3 authority functions must exist (subset check).  Newer
            # migrations add functions outside WS-3 authority scope.
            expected_functions = set(WS3_SCHEMA_CONTRACT["functions"])
            missing_functions = expected_functions - set(function_facts)
            if missing_functions:
                raise MigrationReportError(
                    f"public function identity set missing WS-3 functions: missing={sorted(missing_functions)!r}"
                )
            protected_function_names = {
                (str(facts["schema"]), str(facts["name"]))
                for identity, facts in function_facts.items()
                if identity in expected_functions
            }
            unexpected_protected_overloads = {
                identity
                for identity, facts in function_facts.items()
                if (str(facts["schema"]), str(facts["name"])) in protected_function_names
                and identity not in expected_functions
            }
            if unexpected_protected_overloads:
                raise MigrationReportError(
                    f"protected WS-3 function overload set drift: unexpected={sorted(unexpected_protected_overloads)!r}"
                )
            authority_function_facts = {identity: function_facts[identity] for identity in expected_functions}
            authority_function_sources = {identity: function_sources[identity] for identity in expected_functions}
            public_function_facts = {
                identity: {key: value for key, value in facts.items() if key not in {"schema", "name", "acl"}}
                for identity, facts in authority_function_facts.items()
            }

            # Trigger and target-family maps are complete over the same public
            # relation/function catalog, with no trigger-name whitelist.
            cursor.execute(_WS3_TRIGGER_CATALOG_SQL)
            trigger_rows = {
                f"{row[0]}.{row[1]}.{row[2]}": {
                    "schema": str(row[0]),
                    "table": str(row[1]),
                    "name": str(row[2]),
                    "enabled": str(row[3]),
                    "definition": str(row[4]),
                    "target_function": _function_facts(
                        row[5],
                        row[6],
                        row[7],
                        row[8],
                        row[9],
                        row[10],
                        row[17],
                        row[18],
                        prokind=row[11],
                        prolang=row[12],
                        volatility=row[13],
                        parallel=row[14],
                        strict=row[15],
                        leakproof=row[16],
                    ),
                }
                for row in cursor.fetchall()
            }
            cursor.execute(_WS3_TRIGGER_TARGET_FAMILY_SQL)
            target_families: dict[str, dict[str, dict[str, object]]] = {}
            for row in cursor.fetchall():
                family_key = f"{row[0]}.{row[1]}"
                facts = _function_facts(
                    row[2],
                    row[3],
                    row[4],
                    row[5],
                    row[6],
                    row[7],
                    row[14],
                    row[15],
                    prokind=row[8],
                    prolang=row[9],
                    volatility=row[10],
                    parallel=row[11],
                    strict=row[12],
                    leakproof=row[13],
                )
                target_families.setdefault(family_key, {})[str(facts["identity"])] = facts
            for trigger_row in trigger_rows.values():
                target = cast(dict[str, object], trigger_row["target_function"])
                trigger_row["target_function_family"] = target_families.get(f"{target['schema']}.{target['name']}", {})

            # Policies are keyed by stable schema/relation/policy identity;
            # multiple policies on one relation therefore cannot overwrite one
            # another.  Rules are read independently even though the baseline
            # expects the complete map to be empty.
            cursor.execute(
                """
                SELECT n.nspname, c.relname, polname, polcmd, polpermissive,
                       pg_get_expr(polqual, polrelid), pg_get_expr(polwithcheck, polrelid), polroles::text
                  FROM pg_policy
                  JOIN pg_class AS c ON c.oid = polrelid
                  JOIN pg_namespace AS n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'public'
                   AND NOT EXISTS (
                       SELECT 1
                         FROM pg_depend AS d
                         JOIN pg_extension AS e ON e.oid = d.refobjid
                        WHERE d.classid = 'pg_class'::regclass
                          AND d.objid = c.oid
                          AND d.deptype = 'e'
                   )
                 ORDER BY n.nspname, c.relname, polname
                """
            )
            relation_policies: dict[str, dict[str, dict[str, object]]] = {key: {} for key in relation_catalog}
            for row in cursor.fetchall():
                relation_key = f"{row[0]}.{row[1]}"
                policy_identity = f"{relation_key}.{row[2]}"
                relation_policies.setdefault(relation_key, {})[policy_identity] = {
                    "name": str(row[2]),
                    "command": str(row[3]),
                    "permissive": bool(row[4]),
                    "using": str(row[5]),
                    "with_check": str(row[6]),
                    "roles": str(row[7]),
                }
            cursor.execute(
                """
                SELECT n.nspname, c.relname, r.rulename, r.ev_enabled, pg_get_ruledef(r.oid, true)
                  FROM pg_rewrite AS r
                  JOIN pg_class AS c ON c.oid = r.ev_class
                  JOIN pg_namespace AS n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'public'
                   AND c.relname <> 'alembic_version'
                   AND NOT EXISTS (
                       SELECT 1
                         FROM pg_depend AS d
                         JOIN pg_extension AS e ON e.oid = d.refobjid
                        WHERE d.classid = 'pg_class'::regclass
                          AND d.objid = c.oid
                          AND d.deptype = 'e'
                   )
                 ORDER BY n.nspname, c.relname, r.rulename
                """
            )
            relation_rules: dict[str, dict[str, dict[str, object]]] = {key: {} for key in relation_catalog}
            for row in cursor.fetchall():
                relation_key = f"{row[0]}.{row[1]}"
                relation_rules.setdefault(relation_key, {})[f"{relation_key}.{row[2]}"] = {
                    "name": str(row[2]),
                    "enabled": str(row[3]),
                    "definition": str(row[4]),
                }

            relation_columns = _authority_relation_columns(cursor, relation_names)
            relation_grants = _relation_grants(cursor, relation_columns, relation_names)
            mutation_grants_all = _authority_mutation_grants(cursor, relation_columns, relation_names)

            # ACL evidence is read with PostgreSQL's aclexplode rather than
            # text parsing.  Every grantee (including PUBLIC and roles not in
            # our fixed role set) is preserved as a value, so commas/quotes in
            # role names cannot change canonical boundaries.
            cursor.execute(
                """
                SELECT n.nspname, c.relname,
                       COALESCE(jsonb_agg(jsonb_build_object(
                           'grantor', grantor.rolname,
                           'grantee', COALESCE(grantee.rolname, 'PUBLIC'),
                           'privilege', acl_item.privilege_type,
                           'grantable', acl_item.is_grantable
                       ) ORDER BY grantor.rolname, COALESCE(grantee.rolname, 'PUBLIC'), acl_item.privilege_type)
                       FILTER (WHERE acl_item.grantor IS NOT NULL), '[]'::jsonb)
                  FROM pg_class AS c
                  JOIN pg_namespace AS n ON n.oid = c.relnamespace
                  LEFT JOIN LATERAL aclexplode(COALESCE(c.relacl, acldefault('r', c.relowner))) AS acl_item ON TRUE
                  LEFT JOIN pg_roles AS grantor ON grantor.oid = acl_item.grantor
                  LEFT JOIN pg_roles AS grantee ON grantee.oid = acl_item.grantee
                 WHERE n.nspname = 'public'
                   AND c.relkind = ANY(%s)
                   AND NOT EXISTS (
                       SELECT 1
                         FROM pg_depend AS d
                         JOIN pg_extension AS e ON e.oid = d.refobjid
                        WHERE d.classid = 'pg_class'::regclass
                          AND d.objid = c.oid
                          AND d.deptype = 'e'
                   )
                   AND NOT (
                       c.relkind = 'i'
                       AND EXISTS (
                           SELECT 1
                             FROM pg_index AS ix
                             JOIN pg_depend AS base_dep ON base_dep.classid = 'pg_class'::regclass
                                                        AND base_dep.objid = ix.indrelid
                                                        AND base_dep.deptype = 'e'
                             JOIN pg_extension AS base_ext ON base_ext.oid = base_dep.refobjid
                            WHERE ix.indexrelid = c.oid
                       )
                   )
                 GROUP BY n.nspname, c.relname
                 ORDER BY n.nspname, c.relname
                """,
                (list(WS3_AUTHORITY_OBJECT_RELKINDS),),
            )
            expected_table_acl_keys = set(WS3_AUTHORITY_ACL_EXPECTED["table"])
            table_acl_entries = {
                identity: _canonical_acl_entries(row[2])
                for row in cursor.fetchall()
                if (identity := f"{row[0]}.{row[1]}") in expected_table_acl_keys
            }
            cursor.execute(
                """
                SELECT n.nspname, c.relname, a.attname,
                       COALESCE(jsonb_agg(jsonb_build_object(
                           'grantor', grantor.rolname,
                           'grantee', COALESCE(grantee.rolname, 'PUBLIC'),
                           'privilege', acl_item.privilege_type,
                           'grantable', acl_item.is_grantable
                       ) ORDER BY grantor.rolname, COALESCE(grantee.rolname, 'PUBLIC'), acl_item.privilege_type)
                       FILTER (WHERE acl_item.grantor IS NOT NULL), '[]'::jsonb)
                  FROM pg_attribute AS a
                  JOIN pg_class AS c ON c.oid = a.attrelid
                  JOIN pg_namespace AS n ON n.oid = c.relnamespace
                  LEFT JOIN LATERAL aclexplode(a.attacl) AS acl_item ON TRUE
                  LEFT JOIN pg_roles AS grantor ON grantor.oid = acl_item.grantor
                  LEFT JOIN pg_roles AS grantee ON grantee.oid = acl_item.grantee
                 WHERE n.nspname = 'public'
                   AND a.attnum > 0
                   AND NOT a.attisdropped
                   AND c.relkind = ANY(%s)
                   AND NOT EXISTS (
                       SELECT 1
                         FROM pg_depend AS d
                         JOIN pg_extension AS e ON e.oid = d.refobjid
                        WHERE d.classid = 'pg_class'::regclass
                          AND d.objid = c.oid
                          AND d.deptype = 'e'
                   )
                 GROUP BY n.nspname, c.relname, a.attname, a.attnum
                 ORDER BY n.nspname, c.relname, a.attnum
                """,
                (list(WS3_AUTHORITY_OBJECT_RELKINDS),),
            )
            expected_column_acl_keys = set(WS3_AUTHORITY_ACL_EXPECTED["column"])
            column_acl_entries = {
                identity: entries
                for row in cursor.fetchall()
                if (identity := f"{row[0]}.{row[1]}.{row[2]}") in expected_column_acl_keys
                if (entries := _canonical_acl_entries(row[3]))
            }
            cursor.execute(
                """
                SELECT n.nspname,
                       COALESCE(jsonb_agg(jsonb_build_object(
                           'grantor', grantor.rolname,
                           'grantee', COALESCE(grantee.rolname, 'PUBLIC'),
                           'privilege', acl_item.privilege_type,
                           'grantable', acl_item.is_grantable
                       ) ORDER BY grantor.rolname, COALESCE(grantee.rolname, 'PUBLIC'), acl_item.privilege_type)
                       FILTER (WHERE acl_item.grantor IS NOT NULL), '[]'::jsonb)
                  FROM pg_namespace AS n
                  LEFT JOIN LATERAL aclexplode(COALESCE(n.nspacl, acldefault('n', n.nspowner))) AS acl_item ON TRUE
                  LEFT JOIN pg_roles AS grantor ON grantor.oid = acl_item.grantor
                  LEFT JOIN pg_roles AS grantee ON grantee.oid = acl_item.grantee
                 WHERE n.nspname = 'public'
                 GROUP BY n.nspname
                """
            )
            schema_acl_entries = {str(row[0]): _canonical_acl_entries(row[1]) for row in cursor.fetchall()}
            cursor.execute(
                """
                SELECT d.datname,
                       COALESCE(jsonb_agg(jsonb_build_object(
                           'grantor', grantor.rolname,
                           'grantee', COALESCE(grantee.rolname, 'PUBLIC'),
                           'privilege', acl_item.privilege_type,
                           'grantable', acl_item.is_grantable
                       ) ORDER BY grantor.rolname, COALESCE(grantee.rolname, 'PUBLIC'), acl_item.privilege_type)
                       FILTER (WHERE acl_item.grantor IS NOT NULL), '[]'::jsonb)
                  FROM pg_database AS d
                  LEFT JOIN LATERAL aclexplode(COALESCE(d.datacl, acldefault('d', d.datdba))) AS acl_item ON TRUE
                  LEFT JOIN pg_roles AS grantor ON grantor.oid = acl_item.grantor
                  LEFT JOIN pg_roles AS grantee ON grantee.oid = acl_item.grantee
                 WHERE d.datname = current_database()
                 GROUP BY d.datname
                """
            )
            database_acl_rows = cursor.fetchall()
            if len(database_acl_rows) != 1:
                raise MigrationReportError("authority database ACL identity is ambiguous")
            # migration-report databases use a random physical name for
            # concurrency and cleanup safety.  The canonical evidence keeps
            # the fixed logical database identity and never leaks that name.
            database_acl_entries = {"grove": _canonical_acl_entries(database_acl_rows[0][1])}
            authority_acl = {
                "table": table_acl_entries,
                "column": column_acl_entries,
                "schema": schema_acl_entries,
                "database": database_acl_entries,
            }
            if authority_acl != WS3_AUTHORITY_ACL_EXPECTED:
                raise MigrationReportError("authority ACL closure drift")
            actual_mutation_relations = _online_mutation_relation_set(cursor)
            expected_mutation_relations = {f"public.{name}" for name in WS3_AUTHORITY_MUTATION_RELATION_NAMES}
            ws3_relation_scope = set(WS3_AUTHORITY_RELATION_REGISTRY) | set(WS3_AUTHORITY_RELATION_EXCLUSIONS)
            actual_ws3_mutation_relations = actual_mutation_relations & ws3_relation_scope
            if actual_ws3_mutation_relations != expected_mutation_relations:
                raise MigrationReportError(
                    "online mutation grant closure drift: "
                    f"expected={sorted(expected_mutation_relations)!r}, "
                    f"actual={sorted(actual_ws3_mutation_relations)!r}"
                )

            authority_relations = deepcopy(WS3_AUTHORITY_RELATION_REGISTRY)
            for relation_key, entry in authority_relations.items():
                live = relation_catalog[relation_key]
                entry.update(
                    {
                        "owner": live["owner"],
                        "relkind": live["relkind"],
                        "relpersistence": live["relpersistence"],
                        "replica_identity": live["replica_identity"],
                        "reloptions": live["reloptions"],
                        "is_partition": live["is_partition"],
                        "parent": live["parent"],
                        "children": live["children"],
                        "partition_bound": live["partition_bound"],
                        "rls": live["rls"],
                        "triggers": {
                            key: value
                            for key, value in trigger_rows.items()
                            if value["schema"] == entry["schema"] and value["table"] == entry["name"]
                        },
                        "policies": relation_policies[relation_key],
                        "rules": relation_rules[relation_key],
                        "direct_mutation_grants": mutation_grants_all[relation_key],
                    }
                )
            authority_dml_targets = _authority_dml_targets_diagnostic(
                authority_function_facts,
                authority_function_sources,
            )
            authority_exclusions = deepcopy(WS3_AUTHORITY_RELATION_EXCLUSIONS)
            for relation_key, exclusion in authority_exclusions.items():
                grants = mutation_grants_all[relation_key]
                exclusion["online_mutation_grants"] = any(
                    bool(value) for role_facts in grants.values() for value in role_facts["table"].values()
                ) or any(bool(value) for role_facts in grants.values() for value in role_facts["columns"].values())
                exclusion["authority_dml_targets"] = any(
                    relation_key in targets for targets in authority_dml_targets.values()
                )
                exclusion["authority_dml_target_identities"] = sorted(
                    identity for identity, targets in authority_dml_targets.items() if relation_key in targets
                )

            cursor.execute("SELECT v FROM checkpoint_migrations ORDER BY v")
            migration_rows = [int(row[0]) for row in cursor.fetchall()]

            # Authority role facts include direct and transitive SET ROLE edges,
            # schema/database privileges, and ownership of every expected public
            # relation/function object.  No password or secret catalog fields are
            # read.
            cursor.execute(
                """
                SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb,
                       rolcanlogin, rolreplication, rolconnlimit, rolbypassrls
                  FROM pg_roles
                 WHERE rolname = ANY(%s)
                 ORDER BY rolname
                """,
                (list(WS3_AUTHORITY_ROLES),),
            )
            role_rows = cursor.fetchall()
            if {str(row[0]) for row in role_rows} != set(WS3_AUTHORITY_ROLES):
                raise MigrationReportError("authority role catalog is incomplete")
            cursor.execute(
                """
                SELECT granted.rolname, member.rolname, am.admin_option, am.inherit_option, am.set_option
                  FROM pg_auth_members AS am
                  JOIN pg_roles AS granted ON granted.oid = am.roleid
                  JOIN pg_roles AS member ON member.oid = am.member
                 WHERE granted.rolname = ANY(%s) OR member.rolname = ANY(%s)
                 ORDER BY granted.rolname, member.rolname
                """,
                (list(WS3_AUTHORITY_ROLES), list(WS3_AUTHORITY_ROLES)),
            )
            direct_edges = [
                {
                    "role": str(row[0]),
                    "member": str(row[1]),
                    "admin_option": bool(row[2]),
                    "inherit_option": bool(row[3]),
                    "set_option": bool(row[4]),
                }
                for row in cursor.fetchall()
            ]
            transitive_edges = _transitive_memberships(direct_edges)
            authority_roles = deepcopy(WS3_AUTHORITY_ROLE_REGISTRY)
            for row in role_rows:
                role_name = str(row[0])
                authority_roles[role_name]["attributes"] = {
                    "rolsuper": bool(row[1]),
                    "rolinherit": bool(row[2]),
                    "rolcreaterole": bool(row[3]),
                    "rolcreatedb": bool(row[4]),
                    "rolcanlogin": bool(row[5]),
                    "rolreplication": bool(row[6]),
                    "rolconnlimit": int(row[7]),
                    "rolbypassrls": bool(row[8]),
                }
                authority_roles[role_name]["memberships"] = {
                    "direct": [
                        edge for edge in direct_edges if edge["role"] == role_name or edge["member"] == role_name
                    ],
                    "transitive": [
                        edge for edge in transitive_edges if edge["role"] == role_name or edge["member"] == role_name
                    ],
                }
                schema_privileges = {}
                for privilege in ("USAGE", "CREATE"):
                    cursor.execute("SELECT has_schema_privilege(%s, 'public', %s)", (role_name, privilege))
                    privilege_row = cursor.fetchone()
                    if privilege_row is None:
                        raise MigrationReportError(f"missing schema privilege result for {role_name}/{privilege}")
                    schema_privileges[privilege] = bool(privilege_row[0])
                authority_roles[role_name]["schema_privileges"] = {"public": schema_privileges}
                database_privileges = {}
                for privilege in ("CONNECT", "CREATE", "TEMP"):
                    cursor.execute(
                        "SELECT has_database_privilege(%s, current_database(), %s)",
                        (role_name, privilege),
                    )
                    privilege_row = cursor.fetchone()
                    if privilege_row is None:
                        raise MigrationReportError(f"missing database privilege result for {role_name}/{privilege}")
                    database_privileges[privilege] = bool(privilege_row[0])
                authority_roles[role_name]["database_privileges"] = database_privileges
            owned_objects: dict[str, list[str]] = {role: [] for role in WS3_AUTHORITY_ROLES}
            for relation_key, live in relation_catalog.items():
                owner = str(live["owner"])
                if owner not in owned_objects:
                    raise MigrationReportError(f"authority relation {relation_key} has unexpected owner {owner}")
                owned_objects[owner].append(relation_key)
            for identity, facts in authority_function_facts.items():
                owner = str(facts["owner"])
                if owner not in owned_objects:
                    raise MigrationReportError(f"authority function {identity} has unexpected owner {owner}")
                owned_objects[owner].append(identity)
            for role in authority_roles:
                authority_roles[role]["owned_authority_objects"] = sorted(owned_objects[role])

            # Stable privilege closure retained for the public report API.
            def _has_function(role: str, signature: str) -> bool:
                cursor.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')", (role, signature))
                row = cursor.fetchone()
                if row is None:
                    raise MigrationReportError(f"missing function privilege result for {role}/{signature}")
                return bool(row[0])

            def _has_table(role: str, relation: str, privilege: str) -> bool:
                cursor.execute("SELECT has_table_privilege(%s, %s, %s)", (role, relation, privilege))
                row = cursor.fetchone()
                if row is None:
                    raise MigrationReportError(f"missing table privilege result for {role}/{relation}/{privilege}")
                return bool(row[0])

            def _has_column(role: str, relation: str, column: str, privilege: str) -> bool:
                cursor.execute("SELECT has_column_privilege(%s, %s, %s, %s)", (role, relation, column, privilege))
                row = cursor.fetchone()
                if row is None:
                    raise MigrationReportError(
                        f"missing column privilege result for {role}/{relation}/{column}/{privilege}"
                    )
                return bool(row[0])

            heartbeat_signature = (
                "grove_heartbeat_run_command(text,uuid,uuid,bigint,text,text,text,bigint,"
                "timestamp with time zone,double precision)"
            )
            consume_signature = (
                "grove_consume_run_command(text,uuid,uuid,bigint,text,text,text,bigint,timestamp with time zone)"
            )
            cancel_signature = "grove_accept_cancel_run(text,uuid,uuid,bigint,text,text,text,text,jsonb)"
            dead_letter_signature = (
                "grove_dead_letter_run_command(text,uuid,uuid,bigint,text,text,text,bigint,"
                "timestamp with time zone,text)"
            )
            reconcile_signature = "grove_reconcile_expired_run_command(text,uuid)"
            claim_signature = "grove_claim_run_command(text,text,text,double precision)"
            privileges = {
                "api_cancel_execute": _has_function("grove_api", cancel_signature),
                "runtime_cancel_execute": _has_function("grove_runtime", cancel_signature),
                "governance_cancel_execute": _has_function("grove_governance", cancel_signature),
                "projection_cancel_execute": _has_function("grove_projection", cancel_signature),
                "public_cancel_execute": _has_function("public", cancel_signature),
                "api_claim_execute": _has_function("grove_api", claim_signature),
                "api_fence_insert": _has_column("grove_api", "agent_run", "execution_fence", "INSERT"),
                "api_fence_update": _has_column("grove_api", "agent_run", "execution_fence", "UPDATE"),
                "api_runtime_build_hash_insert": _has_column("grove_api", "agent_run", "runtime_build_hash", "INSERT"),
                "api_runtime_build_ref_insert": _has_column("grove_api", "agent_run", "runtime_build_ref", "INSERT"),
                "governance_claim_execute": _has_function("grove_governance", claim_signature),
                "projection_claim_execute": _has_function("grove_projection", claim_signature),
                "public_claim_execute": _has_function("public", claim_signature),
                "public_heartbeat_execute": _has_function("public", heartbeat_signature),
                "runtime_claim_execute": _has_function("grove_runtime", claim_signature),
                "runtime_fence_update": _has_column("grove_runtime", "agent_run", "execution_fence", "UPDATE"),
                "runtime_heartbeat_execute": _has_function("grove_runtime", heartbeat_signature),
                "runtime_tenant_update": _has_column("grove_runtime", "agent_run", "tenant_id", "UPDATE"),
                "runtime_consume_execute": _has_function("grove_runtime", consume_signature),
                "runtime_dead_letter_execute": _has_function("grove_runtime", dead_letter_signature),
                "projection_reconcile_expired_execute": _has_function("grove_projection", reconcile_signature),
                "public_dead_letter_execute": _has_function("public", dead_letter_signature),
                "public_reconcile_expired_execute": _has_function("public", reconcile_signature),
                "runtime_checkpoint_select": _has_table("grove_runtime", "checkpoints", "SELECT"),
                "runtime_checkpoint_insert": _has_table("grove_runtime", "checkpoints", "INSERT"),
                "runtime_checkpoint_update": _has_table("grove_runtime", "checkpoints", "UPDATE"),
                "api_checkpoint_select": _has_table("grove_api", "checkpoints", "SELECT"),
                "governance_checkpoint_select": _has_table("grove_governance", "checkpoints", "SELECT"),
                "projection_checkpoint_select": _has_table("grove_projection", "checkpoints", "SELECT"),
                "api_payload_body_select": _has_column("grove_api", "command_payload", "payload", "SELECT"),
                "runtime_payload_body_select": _has_column("grove_runtime", "command_payload", "payload", "SELECT"),
                "projection_payload_body_select": _has_column(
                    "grove_projection", "command_payload", "payload", "SELECT"
                ),
                "governance_payload_body_select": _has_column(
                    "grove_governance", "command_payload", "payload", "SELECT"
                ),
                "public_payload_body_select": _has_column("public", "command_payload", "payload", "SELECT"),
            }
            database_temp_privileges = {
                role: authority_roles[role]["database_privileges"]["TEMP"]
                for role in ("grove_api", "grove_runtime", "grove_projection", "grove_governance")
            }
            table_acl: dict[str, bool] = {}
            for relation_name in relation_names:
                for role_name in WS3_AUTHORITY_ONLINE_ROLES:
                    for privilege_name in WS3_AUTHORITY_MUTATION_PRIVILEGES:
                        table_acl[f"{relation_name}.{role_name}.{privilege_name}"] = _has_table(
                            role_name, relation_name, privilege_name
                        )

            agent_run_triggers = authority_relations["public.agent_run"]["triggers"]
            trigger = agent_run_triggers.get("public.agent_run.agent_run_execution_fence_guard")
            checkpoint_triggers = {
                key: value
                for relation_key in ("public.checkpoints", "public.checkpoint_blobs", "public.checkpoint_writes")
                for key, value in authority_relations[relation_key]["triggers"].items()
            }
            policies = {
                str(policy["name"]): {key: value for key, value in policy.items() if key != "name"}
                for relation_key in ("public.checkpoints", "public.checkpoint_blobs", "public.checkpoint_writes")
                for policy in authority_relations[relation_key]["policies"].values()
            }
    return {
        "columns": columns,
        "constraints": {
            str(facts["name"]): facts["definition"]
            for identity, facts in ws3_constraints.items()
            if identity in WS3_AUTHORITY_CONSTRAINTS
        },
        "functions": public_function_facts,
        "authority_public_functions": public_function_facts,
        "authority_function_acl": {
            identity: list(cast(list[dict[str, object]], facts.get("acl_entries", [])))
            for identity, facts in function_facts.items()
            if identity in expected_functions
        },
        "authority_object_inventory": {identity: object_inventory[identity] for identity in expected_object_identities},
        "authority_constraints": {
            identity: ws3_constraints[identity] for identity in WS3_AUTHORITY_CONSTRAINTS if identity in ws3_constraints
        },
        "authority_acl": authority_acl,
        "function_acl": {identity: function_acl[identity] for identity in expected_functions},
        "authority_roles": authority_roles,
        "authority_relations": authority_relations,
        "authority_relation_exclusions": authority_exclusions,
        "authority_relation_grants": relation_grants,
        "authority_relation_policies": {
            f"public.{relation_name}": authority_relations[f"public.{relation_name}"]["policies"]
            for relation_name in relation_names
        },
        "authority_relation_rules": {
            f"public.{relation_name}": authority_relations[f"public.{relation_name}"]["rules"]
            for relation_name in relation_names
        },
        "authority_mutation_grants": {
            f"public.{relation_name}": authority_relations[f"public.{relation_name}"]["direct_mutation_grants"]
            for relation_name in WS3_AUTHORITY_MUTATION_RELATION_NAMES
        },
        "authority_dml_targets": authority_dml_targets,
        "trigger": trigger,
        "agent_run_triggers": agent_run_triggers,
        "checkpoint_triggers": checkpoint_triggers,
        "policies": policies,
        "migration_rows": migration_rows,
        "rls": {name: relation_catalog[f"public.{name}"]["rls"] for name in relation_names},
        "privileges": privileges,
        "database_temp_privileges": database_temp_privileges,
        "table_acl": table_acl,
    }


def ws3_database_state(database_url: str | None = None) -> dict[str, object]:
    """Read the WS-3 catalog contract with a stable database-error boundary."""

    try:
        return _ws3_database_state_impl(database_url)
    except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
        sqlstate = getattr(exc, "sqlstate", None)
        state_suffix = f" [{sqlstate}]" if isinstance(sqlstate, str) and sqlstate else ""
        raise MigrationReportError(f"catalog database operation failed{state_suffix}") from exc


def write_report(root: Path, output: Path, *, database_url: str | None = None) -> None:
    configured_url = load_settings().database_url_value() if database_url is None else database_url
    expected_head = migration_head(root)
    with temporary_migration_database(configured_url) as temporary_url:
        for command in ROUND_TRIP:
            run_migration(root, command, temporary_url)
        head, business_tables = database_state(temporary_url)
        if head != expected_head:
            raise MigrationReportError(f"database head {head!r} does not match Alembic graph head {expected_head!r}")
        # Keep the historical baseline unit fixture usable while enforcing an
        # exact relation set for the WS-2 graph used by real evidence.
        if expected_head == "ws2_tenant_commands" and set(business_tables) != EXPECTED_BUSINESS_RELATIONS:
            raise MigrationReportError(
                "non-infrastructure relation set does not match WS-2 contract: "
                f"expected={sorted(EXPECTED_BUSINESS_RELATIONS)!r}, actual={business_tables!r}"
            )
        if (
            expected_head
            in {"ws3_cancel_acceptance", "ws3_dead_letter_reconciliation", "ws3_execution_authority_closure"}
            and set(business_tables) != WS3_BUSINESS_RELATIONS
        ):
            raise MigrationReportError(
                "checkpoint relation set does not match WS-3 contract: "
                f"expected={sorted(WS3_BUSINESS_RELATIONS)!r}, actual={business_tables!r}"
            )
        if expected_head in WS4_MIGRATION_HEADS and set(business_tables) != WS4_BUSINESS_RELATIONS:
            raise MigrationReportError(
                "observation relation set does not match WS-4 contract: "
                f"expected={sorted(WS4_BUSINESS_RELATIONS)!r}, actual={business_tables!r}"
            )
        ws3_schema: dict[str, object] | None = None
        schema_contract_version = "ws2-tenant-commands"
        if (
            expected_head
            in {
                "ws3_execution_driver",
                "ws3_checkpoint_fenced",
                "ws3_cancel_acceptance",
                "ws3_dead_letter_reconciliation",
                "ws3_execution_authority_closure",
                "ws3_runtime_worker_delivery",
            }
            or expected_head in WS4_MIGRATION_HEADS
        ):
            schema_contract_version = WS3_SCHEMA_CONTRACT_VERSION
            ws3_schema = ws3_database_state(temporary_url)
            if ws3_schema != WS3_SCHEMA_CONTRACT:
                raise MigrationReportError("live WS-3 schema does not match the fixed schema contract")
        catalog_authority: dict[str, object] | None = None
        if expected_head == "ws3_execution_authority_closure":
            catalog_authority = catalog_authority_state(temporary_url)
        report: dict[str, object] = {
            "head": head,
            "migration_hash": migration_hash(root),
            "business_tables": business_tables,
            "infrastructure_tables": sorted(WS3_INFRASTRUCTURE_RELATIONS)
            if expected_head
            in {
                "ws3_checkpoint_fenced",
                "ws3_cancel_acceptance",
                "ws3_dead_letter_reconciliation",
                "ws3_execution_authority_closure",
                "ws3_runtime_worker_delivery",
            }
            or expected_head in WS4_MIGRATION_HEADS
            else [],
            "round_trip": list(ROUND_TRIP),
            "schema_contract_version": schema_contract_version,
            "status": "completed",
        }
        if ws3_schema is not None:
            report["ws3_schema"] = ws3_schema
        if catalog_authority is not None:
            report["catalog_authority"] = catalog_authority
        payload = (json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode()
        write_content_addressed_artifact(output, payload)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, default=Path("ci-evidence/migrations.json"))
    args = parser.parse_args()
    try:
        with _cancel_as_migration_error():
            write_report(args.root.resolve(), args.output)
    except (
        MigrationReportError,
        OSError,
        psycopg.Error,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        ValueError,
    ) as exc:
        print(f"migration error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
