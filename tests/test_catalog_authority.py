from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import psycopg
import pytest
from app.build import catalog_authority as authority
from app.build.catalog_authority import (
    _SHARED_SECURITY_LABELS_SQL,
    _SUPPORTED_EXTENSION_MEMBER_CLASSES,
    CATALOG_AUTHORITY_COMPILER_VERSION,
    CATALOG_AUTHORITY_EXCLUDED_FIELDS,
    CATALOG_AUTHORITY_EXPECTED_ARTIFACT,
    CATALOG_AUTHORITY_EXPECTED_ROOT,
    CatalogAuthorityError,
    canonical_section_root,
    compare_expected_catalog_root,
    compile_catalog_authority,
    discover_catalog_authority,
)
from app.contracts.canonical import canonical_bytes
from scripts import migration_report


def test_section_root_is_deterministic_and_uses_one_canonical_byte_boundary() -> None:
    left = {"rows": [{"name": "b", "value": None}, {"name": "a", "value": 1}]}
    right = {"rows": [{"name": "a", "value": 1}, {"name": "b", "value": None}]}
    assert canonical_section_root(left) != canonical_section_root(right)
    assert canonical_section_root(left) == canonical_section_root(json.loads(json.dumps(left)))


def test_canonical_value_and_section_reject_duplicate_facts() -> None:
    assert authority._canonical_value((b"ab", {"nested": (1, 2)})) == [
        "6162",
        {"nested": [1, 2]},
    ]
    with pytest.raises(CatalogAuthorityError, match="duplicate catalog fact"):
        authority._section([{"identity": "public.same"}, {"identity": "public.same"}])


def test_pure_compiler_accepts_small_section_facts_and_is_repeatable() -> None:
    kwargs: dict[str, list[dict[str, object]]] = {
        name: []
        for name in (
            "extensions",
            "namespaces",
            "database",
            "roles",
            "memberships",
            "objects",
            "attributes",
            "constraints",
            "indexes",
            "triggers",
            "rewrites",
            "policies",
            "functions",
            "types",
            "comments",
            "security_labels",
            "columns",
            "casts",
            "operators",
            "opclasses",
            "opfamilies",
            "collations",
            "conversions",
            "transforms",
            "text_search",
        )
    }
    kwargs["extensions"] = [{"extname": "fake", "members": []}]
    kwargs["functions"] = [{"identity": "public.fake()"}]
    first = compile_catalog_authority([{"database_name": "fake"}], **kwargs)
    second = compile_catalog_authority([{"database_name": "fake"}], **kwargs)
    assert first["overall_root"] == second["overall_root"]
    assert len(first["sections"]) == 37


def test_extension_member_dispatch_is_fail_closed_for_unknown_catalog_class() -> None:
    class Cursor:
        def execute(self, _statement: str, _params: object = None) -> None:  # noqa: ANN001
            return None

    with pytest.raises(CatalogAuthorityError, match="unsupported extension member catalog class"):
        authority._extension_member_semantics(
            cast(Any, Cursor()),
            [{"member_class": "pg_unknown", "member_oid": 1}],
        )


def test_discovery_collection_and_extractors_are_unit_coverable_without_postgres() -> None:
    class Cursor:
        def __init__(self) -> None:
            self.description: list[SimpleNamespace] | None = None
            self.rows: list[tuple[object, ...]] = []

        def execute(self, statement: str, params: tuple[object, ...] | None = None) -> None:
            if statement == "SET search_path TO pg_catalog":
                self.description = None
                self.rows = []
                return
            if statement in {
                authority._EXTENSION_PG_PROC_FACTS_SQL,
                authority._EXTENSION_PG_CLASS_FACTS_SQL,
                authority._EXTENSION_PG_TYPE_FACTS_SQL,
                authority._EXTENSION_PG_CAST_FACTS_SQL,
                authority._EXTENSION_PG_OPERATOR_FACTS_SQL,
                authority._EXTENSION_PG_OPFAMILY_FACTS_SQL,
                authority._EXTENSION_PG_OPCLASS_FACTS_SQL,
                authority._EXTENSION_PG_LANGUAGE_FACTS_SQL,
            }:
                oid = int(cast(list[int], params[0])[0]) if params else 1
                facts: dict[str, object] = {"member_oid": oid}
                if statement == authority._EXTENSION_PG_PROC_FACTS_SQL:
                    facts["definition"] = "SELECT 1"
                self._set_rows(facts)
                return
            rows_by_statement: dict[str, list[dict[str, object]]] = {
                authority._COMPATIBILITY_SQL: [{"database_name": "fake"}],
                authority._EXTENSIONS_SQL: [{"extname": "fake"}],
                authority._EXTENSION_MEMBERS_SQL: [
                    {
                        "extname": "fake",
                        "member_class": class_name,
                        "member_oid": index + 1,
                        "object_type": "object",
                        "object_schema": "public",
                        "object_name": f"member_{index}",
                        "object_identity": f"public.member_{index}",
                        "semantic_definition": "identity",
                    }
                    for index, class_name in enumerate(sorted(authority._SUPPORTED_EXTENSION_MEMBER_CLASSES))
                ],
                authority._ATTRIBUTES_SQL: [
                    {
                        "atthasmissing": False,
                        "default_expression": None,
                        "missing_value_raw": None,
                        "data_type": "text",
                    },
                    {
                        "atthasmissing": True,
                        "default_expression": "now()",
                        "missing_value_raw": "literal",
                        "data_type": "timestamp with time zone",
                    },
                    {
                        "atthasmissing": True,
                        "default_expression": "'x'",
                        "missing_value_raw": "x",
                        "data_type": "text",
                    },
                ],
                authority._FUNCTIONS_SQL: [{"definition": "SELECT 1"}],
            }
            rows = rows_by_statement.get(statement, [])
            self._set_rows(rows[0] if rows and isinstance(rows[0], dict) else None, rows)

        def _set_rows(
            self,
            row: dict[str, object] | None,
            rows: list[dict[str, object]] | None = None,
        ) -> None:
            values = rows if rows is not None else ([row] if row is not None else [])
            names = list(values[0]) if values else ["unused"]
            self.description = [SimpleNamespace(name=name) for name in names]
            self.rows = [tuple(item.get(name) for name in names) for item in values]

        def fetchall(self) -> list[tuple[object, ...]]:
            return self.rows

        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class Connection:
        def __init__(self) -> None:
            self.cursor_value = Cursor()

        def cursor(self) -> Cursor:
            return self.cursor_value

    state = discover_catalog_authority(cast(Any, Connection()))
    assert len(state["sections"]) == 37
    assert all("member_oid" not in member for member in state["section_facts"]["extensions"][0]["members"])
    assert state["section_facts"]["attributes"][1]["missing_value"]["mode"] == "dynamic_default_expression"
    assert state["section_facts"]["attributes"][2]["missing_value"]["mode"] == "literal"


def test_rows_requires_catalog_column_description_and_preserves_nulls() -> None:
    class Cursor:
        description: list[SimpleNamespace] | None = [SimpleNamespace(name="value")]

        def execute(self, _statement: str, _params: object = None) -> None:  # noqa: ANN001
            return None

        def fetchall(self) -> list[tuple[object, ...]]:
            return [(None,)]

    assert authority._rows(cast(Any, Cursor()), "SELECT value") == [{"value": None}]

    class NoDescription(Cursor):
        description = None

    with pytest.raises(CatalogAuthorityError, match="no column description"):
        authority._rows(cast(Any, NoDescription()), "SELECT value")


def test_expected_artifact_rejects_physical_oid_or_filenode_fields(tmp_path: Path) -> None:
    expected_path = tmp_path / "expected.json"
    artifact = json.loads(CATALOG_AUTHORITY_EXPECTED_ARTIFACT.read_bytes())
    artifact["sections"]["objects"]["oid"] = 42
    artifact["overall_root"] = hashlib.sha256(
        canonical_bytes(
            {
                "compiler_version": artifact["compiler_version"],
                "compatibility": artifact["compatibility"],
                "sections": artifact["sections"],
            }
        )
    ).hexdigest()
    expected_path.write_text(json.dumps(artifact), encoding="utf-8")
    with pytest.raises(CatalogAuthorityError, match="physical catalog key"):
        compare_expected_catalog_root(json.loads(CATALOG_AUTHORITY_EXPECTED_ARTIFACT.read_bytes()), expected_path)


def test_expected_artifact_rejects_oid_ast_values_even_without_physical_field_names(tmp_path: Path) -> None:
    expected_path = tmp_path / "expected.json"
    artifact = json.loads(CATALOG_AUTHORITY_EXPECTED_ARTIFACT.read_bytes())
    artifact["sections"]["objects"]["semantic"] = {"Node": {"varno": 42, "varattno": 1}}
    artifact["overall_root"] = hashlib.sha256(
        canonical_bytes(
            {
                "compiler_version": artifact["compiler_version"],
                "compatibility": artifact["compatibility"],
                "sections": artifact["sections"],
            }
        )
    ).hexdigest()
    expected_path.write_text(json.dumps(artifact), encoding="utf-8")
    with pytest.raises(CatalogAuthorityError, match="physical catalog AST"):
        compare_expected_catalog_root(json.loads(CATALOG_AUTHORITY_EXPECTED_ARTIFACT.read_bytes()), expected_path)


def test_catalog_sql_uses_rendered_semantics_instead_of_node_text() -> None:
    assert "typdefaultbin::text" not in authority._TYPES_SQL
    assert "typdefaultbin::text" not in authority._EXTENSION_PG_TYPE_FACTS_SQL
    assert "pg_get_expr(type_row.typdefaultbin" in authority._TYPES_SQL
    assert "adbin::text" not in authority._ATTRIBUTES_SQL
    assert "conbin" not in authority._CONSTRAINTS_SQL


def test_fast_default_classification_is_conservative_and_does_not_use_function_names() -> None:
    literal = {
        "atthasmissing": True,
        "missing_value_raw": "42",
        "data_type": "integer",
        "default_expression": "42",
    }
    dynamic = {
        "atthasmissing": True,
        "missing_value_raw": "2026-08-08 12:00:00+08",
        "data_type": "timestamp with time zone",
        "default_expression": "CURRENT_TIMESTAMP",
    }
    assert authority._canonical_missing_value(literal) == {
        "mode": "literal",
        "type": "integer",
        "value": "42",
    }
    assert authority._canonical_missing_value(dynamic) == {
        "mode": "dynamic_default_expression",
        "type": "timestamp with time zone",
        "expression": "CURRENT_TIMESTAMP",
    }


def test_expected_artifact_is_source_controlled_and_has_external_identity() -> None:
    artifact = json.loads(CATALOG_AUTHORITY_EXPECTED_ARTIFACT.read_bytes())
    assert artifact["compiler_version"] == CATALOG_AUTHORITY_COMPILER_VERSION
    assert artifact["issuer"] == "source-controlled-grove-catalog-authority"
    assert set(artifact) == {"compiler_version", "compatibility", "sections", "overall_root", "issuer"}
    assert (
        artifact["overall_root"]
        == hashlib.sha256(
            canonical_bytes(
                {
                    "compiler_version": artifact["compiler_version"],
                    "compatibility": artifact["compatibility"],
                    "sections": artifact["sections"],
                }
            )
        ).hexdigest()
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (lambda artifact: artifact.clear(), "invalid shape"),
        (lambda artifact: artifact.__setitem__("compiler_version", "other"), "compiler version"),
        (lambda artifact: artifact.__setitem__("issuer", "other"), "issuer"),
        (lambda artifact: artifact.__setitem__("sections", []), "roots are malformed"),
        (lambda artifact: artifact["sections"].pop("acl"), "sections are incomplete"),
        (lambda artifact: artifact["sections"]["acl"].__setitem__("extra", 1), "section summary"),
        (lambda artifact: artifact["sections"]["acl"].__setitem__("count", -1), "section summary"),
        (lambda artifact: artifact.__setitem__("overall_root", "0" * 64), "self-hash"),
    ),
)
def test_artifact_validator_rejects_malformed_external_facts(mutation: object, message: str) -> None:
    artifact = json.loads(CATALOG_AUTHORITY_EXPECTED_ARTIFACT.read_bytes())
    cast(Any, mutation)(artifact)
    with pytest.raises(CatalogAuthorityError, match=message):
        authority._validate_artifact(artifact)


def test_artifact_anchor_and_live_root_guards_are_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    custom = tmp_path / "expected.json"
    custom.write_bytes(CATALOG_AUTHORITY_EXPECTED_ARTIFACT.read_bytes())
    assert authority.expected_catalog_artifact_hash(custom) == hashlib.sha256(custom.read_bytes()).hexdigest()
    assert authority.expected_catalog_authority_root(custom) == CATALOG_AUTHORITY_EXPECTED_ROOT

    monkeypatch.setattr(authority, "CATALOG_AUTHORITY_EXPECTED_ARTIFACT_SHA256", "0" * 64)
    with pytest.raises(CatalogAuthorityError, match="external hash anchor"):
        authority.expected_catalog_artifact_hash()

    monkeypatch.setattr(
        authority,
        "CATALOG_AUTHORITY_EXPECTED_ARTIFACT_SHA256",
        hashlib.sha256(CATALOG_AUTHORITY_EXPECTED_ARTIFACT.read_bytes()).hexdigest(),
    )
    monkeypatch.setattr(authority, "CATALOG_AUTHORITY_EXPECTED_ROOT", "0" * 64)
    with pytest.raises(CatalogAuthorityError, match="root external anchor"):
        authority.expected_catalog_authority_root()

    monkeypatch.setattr(authority, "CATALOG_AUTHORITY_EXPECTED_ROOT", CATALOG_AUTHORITY_EXPECTED_ROOT)
    actual = json.loads(CATALOG_AUTHORITY_EXPECTED_ARTIFACT.read_bytes())
    actual["overall_root"] = "0" * 64
    with pytest.raises(CatalogAuthorityError, match="live catalog authority root self-hash"):
        compare_expected_catalog_root(actual)


def test_expected_catalog_comparison_never_self_heals(tmp_path: Path) -> None:
    expected_path = tmp_path / "expected.json"
    expected_path.write_bytes(CATALOG_AUTHORITY_EXPECTED_ARTIFACT.read_bytes())
    actual = json.loads(expected_path.read_bytes())
    changed = json.loads(expected_path.read_bytes())
    changed["sections"]["objects"]["count"] = 1
    changed["overall_root"] = hashlib.sha256(
        canonical_bytes(
            {
                "compiler_version": changed["compiler_version"],
                "compatibility": changed["compatibility"],
                "sections": changed["sections"],
            }
        )
    ).hexdigest()
    expected_path.write_text(json.dumps(changed, sort_keys=True), encoding="utf-8")
    with pytest.raises(CatalogAuthorityError, match="expected catalog authority root mismatch"):
        compare_expected_catalog_root(actual, expected_path)
    assert json.loads(expected_path.read_bytes())["sections"]["objects"]["count"] == 1


def test_discovery_sql_is_not_prefiltered_by_expected_names() -> None:
    doc = discover_catalog_authority.__doc__
    assert doc is not None
    assert "pg_class" in doc
    assert "relname = ANY" not in doc


def test_every_excluded_catalog_field_has_a_structural_reason() -> None:
    assert CATALOG_AUTHORITY_EXCLUDED_FIELDS
    assert all(isinstance(field, str) and reason for field, reason in CATALOG_AUTHORITY_EXCLUDED_FIELDS.items())


def test_extension_member_class_support_is_explicit_and_covers_fresh_baseline() -> None:
    assert _SUPPORTED_EXTENSION_MEMBER_CLASSES == {
        "pg_cast",
        "pg_class",
        "pg_language",
        "pg_opclass",
        "pg_operator",
        "pg_opfamily",
        "pg_proc",
        "pg_type",
    }


def test_capability_catalog_queries_and_full_cast_universe_are_explicit() -> None:
    assert "FROM pg_parameter_acl" in authority._PARAMETER_ACLS_SQL
    assert "FROM pg_default_acl" in authority._DEFAULT_ACLS_SQL
    assert "FROM pg_db_role_setting" in authority._DB_ROLE_SETTINGS_SQL
    assert "FROM pg_event_trigger" in authority._EVENT_TRIGGERS_SQL
    assert "FROM pg_foreign_data_wrapper" in authority._FOREIGN_DATA_SQL
    assert "FROM pg_largeobject_metadata" in authority._LARGE_OBJECTS_SQL
    assert "FROM pg_publication" in authority._PUBLICATIONS_SQL
    assert "FROM pg_subscription" in authority._SUBSCRIPTIONS_SQL
    assert "option_values" in authority._FOREIGN_DATA_SQL
    assert "pg_publication_namespace" in authority._PUBLICATIONS_SQL
    assert "connection_info" in authority._SUBSCRIPTIONS_SQL
    assert "source_namespace.nspname" not in authority._CASTS_SQL
    assert "target_namespace.nspname" not in authority._CASTS_SQL


def test_capability_secret_normalizers_hash_values_without_emitting_them() -> None:
    foreign_data = authority._redacted_option_facts(
        [{"kind": "server", "option_values": ["host=secret-host", "password=secret-value"]}]
    )
    subscription = authority._redacted_subscription_facts(
        [{"subname": "sub", "connection_info": "host=secret-host password=secret-value"}]
    )
    serialized = json.dumps([foreign_data, subscription], sort_keys=True)
    assert "secret-host" not in serialized
    assert "secret-value" not in serialized
    assert foreign_data[0]["options"][1]["value_sha256"] == hashlib.sha256(b"secret-value").hexdigest()
    assert (
        subscription[0]["connection_info_sha256"]
        == hashlib.sha256(b"host=secret-host password=secret-value").hexdigest()
    )


def test_trigger_query_closes_internal_constraint_trigger_state_without_physical_names() -> None:
    assert "trigger_row.tgisinternal" in authority._TRIGGERS_SQL
    assert "constraint_identity" in authority._TRIGGERS_SQL
    assert "NOT trigger_row.tgisinternal" not in authority._TRIGGERS_SQL
    assert "pg_get_triggerdef" in authority._TRIGGERS_SQL


def test_extension_class_extractors_have_recursive_semantic_query_families() -> None:
    assert authority._EXTENSION_CLASS_RECURSIVE_SQL["pg_class"]
    assert authority._EXTENSION_CLASS_RECURSIVE_SQL["pg_proc"]
    assert authority._EXTENSION_CLASS_RECURSIVE_SQL["pg_type"]
    assert authority._EXTENSION_CLASS_RECURSIVE_SQL["pg_opfamily"]
    assert any("pg_attribute" in statement for statement in authority._EXTENSION_CLASS_RECURSIVE_SQL["pg_class"])
    assert any("pg_sequence" in statement for statement in authority._EXTENSION_CLASS_RECURSIVE_SQL["pg_class"])


def test_shared_security_label_query_is_present_even_when_baseline_is_empty() -> None:
    assert "FROM pg_shseclabel" in _SHARED_SECURITY_LABELS_SQL
    assert "pg_identify_object" in _SHARED_SECURITY_LABELS_SQL


def test_catalog_query_programming_errors_are_not_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    error = psycopg.errors.UndefinedColumn("catalog query defect")
    monkeypatch.setattr(migration_report, "_ws3_database_state_impl", lambda _url: (_ for _ in ()).throw(error))
    with pytest.raises(psycopg.errors.UndefinedColumn):
        migration_report.ws3_database_state("postgresql://unused")
