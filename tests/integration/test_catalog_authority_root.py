from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest
from app.build.catalog_authority import compare_expected_catalog_root, discover_catalog_authority
from psycopg import sql
from scripts import ws3_preflight


def _migration_url() -> str:
    api_url = os.environ["GROVE_DATABASE_URL"]
    return os.environ.get(
        "GROVE_MIGRATION_DATABASE_URL",
        api_url.replace("grove_api:grove_api_ws0", "grove_migration:grove_migration_ws0", 1),
    ).replace("postgresql+psycopg://", "postgresql://", 1)


def _expect_red_green(connection: psycopg.Connection[object], statement: str, cleanup: str, url: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(statement)
    with pytest.raises(ws3_preflight.WS3PreflightError):
        ws3_preflight.check(Path.cwd(), url)
    with connection.cursor() as cursor:
        cursor.execute(cleanup)
    ws3_preflight.check(Path.cwd(), url)


def _expect_semantic_delta(
    connection: psycopg.Connection[object],
    setup: str,
    mutate: str,
    cleanup: str,
    url: str,
) -> None:
    """Prove a field mutation changes the live root, then restore the baseline."""

    with connection.cursor() as cursor:
        cursor.execute(setup)
    try:
        with connection.cursor() as cursor:
            before = discover_catalog_authority(connection)["overall_root"]
            cursor.execute(mutate)
            after = discover_catalog_authority(connection)["overall_root"]
        assert before != after
    finally:
        with connection.cursor() as cursor:
            cursor.execute(cleanup)
        ws3_preflight.check(Path.cwd(), url)


def _expect_extension_section_delta(
    connection: psycopg.Connection[object],
    setup: str,
    mutate: str,
    cleanup: str,
    url: str,
) -> None:
    """Prove recursive extension member semantics observe a nested mutation."""

    with connection.cursor() as cursor:
        cursor.execute(setup)
    try:
        before = discover_catalog_authority(connection)
        with connection.cursor() as cursor:
            cursor.execute(mutate)
        after = discover_catalog_authority(connection)
        assert before["sections"]["extensions"]["root"] != after["sections"]["extensions"]["root"]
        assert (
            before["sections"]["extension_dependencies"]["root"] != after["sections"]["extension_dependencies"]["root"]
        )
    finally:
        with connection.cursor() as cursor:
            cursor.execute(cleanup)
        ws3_preflight.check(Path.cwd(), url)


@pytest.mark.integration
def test_catalog_authority_root_is_repeatable_and_matches_external_artifact() -> None:
    url = _migration_url()
    with psycopg.connect(url) as connection:
        first = discover_catalog_authority(connection)
        second = discover_catalog_authority(connection)
    assert first["overall_root"] == second["overall_root"]
    assert first["sections"] == second["sections"]
    compare_expected_catalog_root(first)


@pytest.mark.integration
def test_catalog_authority_v1_tamper_matrix_is_red_then_green() -> None:
    url = _migration_url()
    with psycopg.connect(url, autocommit=True) as connection:
        _expect_red_green(
            connection,
            """
            CREATE OR REPLACE FUNCTION public.catalog_v1_extension_probe()
            RETURNS integer LANGUAGE sql SECURITY DEFINER AS $$ SELECT 1 $$;
            ALTER FUNCTION public.catalog_v1_extension_probe() OWNER TO grove_runtime;
            ALTER EXTENSION postgis ADD FUNCTION public.catalog_v1_extension_probe();
            """,
            """
            ALTER EXTENSION postgis DROP FUNCTION public.catalog_v1_extension_probe();
            DROP FUNCTION public.catalog_v1_extension_probe();
            """,
            url,
        )
        _expect_red_green(
            connection,
            """
            CREATE SEQUENCE public.catalog_v1_online_sequence;
            ALTER SEQUENCE public.catalog_v1_online_sequence OWNER TO grove_runtime;
            """,
            "DROP SEQUENCE public.catalog_v1_online_sequence;",
            url,
        )
        _expect_red_green(
            connection,
            """
            CREATE TYPE public.catalog_v1_composite AS (marker text);
            ALTER TYPE public.catalog_v1_composite ADD ATTRIBUTE changed text;
            """,
            "DROP TYPE public.catalog_v1_composite;",
            url,
        )
        _expect_red_green(
            connection,
            """
            CREATE TABLE public.catalog_v1_attr_probe (
                id bigint GENERATED ALWAYS AS IDENTITY,
                body text COLLATE \"C\",
                generated text GENERATED ALWAYS AS (body) STORED
            );
            CREATE INDEX catalog_v1_clustered_idx ON public.tenant (tenant_id);
            ALTER TABLE public.tenant CLUSTER ON catalog_v1_clustered_idx;
            """,
            """
            ALTER TABLE public.tenant SET WITHOUT CLUSTER;
            DROP INDEX public.catalog_v1_clustered_idx;
            DROP TABLE public.catalog_v1_attr_probe;
            """,
            url,
        )
        _expect_red_green(
            connection,
            """
            CREATE OR REPLACE FUNCTION public.catalog_v1_ddl_probe()
            RETURNS integer LANGUAGE plpgsql AS $$
            BEGIN
                -- COMMENT ON, REINDEX, REFRESH MATERIALIZED VIEW and SECURITY LABEL
                RETURN 1;
            END $$;
            """,
            "DROP FUNCTION public.catalog_v1_ddl_probe();",
            url,
        )
        _expect_red_green(
            connection,
            """
            CREATE ROLE \"catalog,v1_unknown\" NOLOGIN;
            GRANT SELECT ON public.tenant TO \"catalog,v1_unknown\";
            CREATE ROLE catalog_v1_group NOLOGIN;
            GRANT catalog_v1_group TO grove_runtime;
            """,
            """
            REVOKE SELECT ON public.tenant FROM \"catalog,v1_unknown\";
            REVOKE catalog_v1_group FROM grove_runtime;
            DROP ROLE \"catalog,v1_unknown\";
            DROP ROLE catalog_v1_group;
            """,
            url,
        )


@pytest.mark.integration
def test_catalog_authority_v1_semantic_binding_matrix_is_red_then_green() -> None:
    url = _migration_url()
    with psycopg.connect(url, autocommit=True) as connection:
        _expect_semantic_delta(
            connection,
            "CREATE TYPE public.catalog_v1_owner_type AS ENUM ('one', 'two');",
            "ALTER TYPE public.catalog_v1_owner_type OWNER TO grove_runtime;",
            "DROP TYPE public.catalog_v1_owner_type;",
            url,
        )


@pytest.mark.integration
def test_catalog_authority_v2_extension_recursive_and_capability_matrix_is_red_then_green() -> None:
    url = _migration_url()
    with psycopg.connect(url, autocommit=True) as connection:
        _expect_extension_section_delta(
            connection,
            "SELECT 1;",
            "ALTER TABLE topology.topology ADD COLUMN catalog_v2_probe text;",
            "ALTER TABLE topology.topology DROP COLUMN catalog_v2_probe;",
            url,
        )
        _expect_extension_section_delta(
            connection,
            "SELECT 1;",
            "REVOKE EXECUTE ON FUNCTION topology.addedge(character varying, public.geometry) FROM PUBLIC;",
            "GRANT EXECUTE ON FUNCTION topology.addedge(character varying, public.geometry) TO PUBLIC;",
            url,
        )
        _expect_semantic_delta(
            connection,
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO grove_runtime;",
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM grove_runtime;",
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM grove_runtime;",
            url,
        )
        _expect_semantic_delta(
            connection,
            "ALTER ROLE grove_runtime IN DATABASE grove SET search_path TO public, pg_catalog;",
            "ALTER ROLE grove_runtime IN DATABASE grove SET search_path TO topology, public, pg_catalog;",
            "ALTER ROLE grove_runtime IN DATABASE grove RESET search_path;",
            url,
        )


@pytest.mark.integration
def test_catalog_authority_v2_internal_constraint_trigger_state_changes_root() -> None:
    url = _migration_url()
    with psycopg.connect(url, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT tgname
                  FROM pg_trigger
                 WHERE tgrelid = 'public.run_command'::regclass
                   AND tgisinternal
                 ORDER BY tgname
                 LIMIT 1
                """
            )
            trigger_row = cursor.fetchone()
            assert trigger_row is not None
            trigger_name = trigger_row[0]
        before = discover_catalog_authority(connection)
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("ALTER TABLE public.run_command DISABLE TRIGGER {}").format(
                        sql.Identifier(str(trigger_name))
                    )
                )
            after = discover_catalog_authority(connection)
            assert before["sections"]["triggers"]["root"] != after["sections"]["triggers"]["root"]
        finally:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("ALTER TABLE public.run_command ENABLE TRIGGER {}").format(
                        sql.Identifier(str(trigger_name))
                    )
                )
            ws3_preflight.check(Path.cwd(), url)


@pytest.mark.integration
def test_catalog_authority_v2_foreign_data_options_are_complete_and_redacted() -> None:
    url = _migration_url()
    with psycopg.connect(url, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("CREATE FOREIGN DATA WRAPPER catalog_v2_fdw")
            cursor.execute(
                "CREATE SERVER catalog_v2_server FOREIGN DATA WRAPPER catalog_v2_fdw "
                "OPTIONS (host 'catalog-secret-host', dbname 'catalog-secret-db')"
            )
            cursor.execute(
                "CREATE USER MAPPING FOR grove_runtime SERVER catalog_v2_server "
                "OPTIONS (user 'catalog-secret-user', password 'catalog-secret-password')"
            )
        try:
            before = discover_catalog_authority(connection)
            with connection.cursor() as cursor:
                cursor.execute("ALTER SERVER catalog_v2_server OPTIONS (SET host 'catalog-secret-host-updated')")
            after_server = discover_catalog_authority(connection)
            assert before["sections"]["foreign_data"]["root"] != after_server["sections"]["foreign_data"]["root"]
            assert "catalog-secret-host" not in str(after_server["section_facts"]["foreign_data"])
            with connection.cursor() as cursor:
                cursor.execute(
                    "ALTER USER MAPPING FOR grove_runtime SERVER catalog_v2_server "
                    "OPTIONS (SET password 'catalog-secret-password-updated')"
                )
            after_mapping = discover_catalog_authority(connection)
            assert after_server["sections"]["foreign_data"]["root"] != after_mapping["sections"]["foreign_data"]["root"]
            assert "catalog-secret-password" not in str(after_mapping["section_facts"]["foreign_data"])
        finally:
            with connection.cursor() as cursor:
                cursor.execute("DROP USER MAPPING FOR grove_runtime SERVER catalog_v2_server")
                cursor.execute("DROP SERVER catalog_v2_server")
                cursor.execute("DROP FOREIGN DATA WRAPPER catalog_v2_fdw")
            ws3_preflight.check(Path.cwd(), url)


@pytest.mark.integration
def test_catalog_authority_v2_publication_schema_and_table_membership_changes_root() -> None:
    url = _migration_url()
    with psycopg.connect(url, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("CREATE SCHEMA catalog_v2_pub_schema")
            cursor.execute("CREATE TABLE public.catalog_v2_pub_table (id integer)")
            cursor.execute("CREATE PUBLICATION catalog_v2_pub FOR TABLE public.tenant")
        try:
            before = discover_catalog_authority(connection)
            with connection.cursor() as cursor:
                cursor.execute("ALTER PUBLICATION catalog_v2_pub ADD TABLE public.catalog_v2_pub_table")
            after_table = discover_catalog_authority(connection)
            assert before["sections"]["publications"]["root"] != after_table["sections"]["publications"]["root"]
            with connection.cursor() as cursor:
                cursor.execute("ALTER PUBLICATION catalog_v2_pub ADD TABLES IN SCHEMA catalog_v2_pub_schema")
            after_schema = discover_catalog_authority(connection)
            assert after_table["sections"]["publications"]["root"] != after_schema["sections"]["publications"]["root"]
        finally:
            with connection.cursor() as cursor:
                cursor.execute("DROP PUBLICATION catalog_v2_pub")
                cursor.execute("DROP TABLE public.catalog_v2_pub_table")
                cursor.execute("DROP SCHEMA catalog_v2_pub_schema")
            ws3_preflight.check(Path.cwd(), url)


@pytest.mark.integration
def test_catalog_authority_v2_subscription_connection_hash_is_complete_and_redacted() -> None:
    url = _migration_url()
    with psycopg.connect(url, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("CREATE PUBLICATION catalog_v2_sub_pub FOR TABLE public.tenant")
            cursor.execute(
                "CREATE SUBSCRIPTION catalog_v2_sub CONNECTION "
                "'host=catalog-secret-host dbname=catalog-secret-db user=catalog-secret-user "
                "password=catalog-secret-password' PUBLICATION catalog_v2_sub_pub WITH (connect=false)"
            )
        try:
            before = discover_catalog_authority(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    "ALTER SUBSCRIPTION catalog_v2_sub CONNECTION "
                    "'host=catalog-secret-host-updated dbname=catalog-secret-db user=catalog-secret-user "
                    "password=catalog-secret-password-updated'"
                )
            after = discover_catalog_authority(connection)
            assert before["sections"]["subscriptions"]["root"] != after["sections"]["subscriptions"]["root"]
            assert "catalog-secret-password" not in str(after["section_facts"]["subscriptions"])
            assert "catalog-secret-host" not in str(after["section_facts"]["subscriptions"])
        finally:
            with connection.cursor() as cursor:
                cursor.execute("ALTER SUBSCRIPTION catalog_v2_sub DISABLE")
                cursor.execute("ALTER SUBSCRIPTION catalog_v2_sub SET (slot_name = NONE)")
                cursor.execute("DROP SUBSCRIPTION catalog_v2_sub")
                cursor.execute("DROP PUBLICATION catalog_v2_sub_pub")
            ws3_preflight.check(Path.cwd(), url)


@pytest.mark.integration
def test_catalog_authority_v2_domain_default_is_oid_and_time_independent() -> None:
    url = _migration_url()
    with psycopg.connect(url, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE FUNCTION public.catalog_v2_default_value()
                RETURNS text LANGUAGE sql IMMUTABLE AS $$ SELECT 'stable' $$;
                CREATE DOMAIN public.catalog_v2_default_domain AS text
                    DEFAULT public.catalog_v2_default_value();
                """
            )
        try:
            first = discover_catalog_authority(connection)["overall_root"]
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    DROP DOMAIN public.catalog_v2_default_domain;
                    DROP FUNCTION public.catalog_v2_default_value();
                    CREATE FUNCTION public.catalog_v2_default_value()
                    RETURNS text LANGUAGE sql IMMUTABLE AS $$ SELECT 'stable' $$;
                    CREATE DOMAIN public.catalog_v2_default_domain AS text
                        DEFAULT public.catalog_v2_default_value();
                    """
                )
            second = discover_catalog_authority(connection)["overall_root"]
            assert first == second
        finally:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    DROP DOMAIN IF EXISTS public.catalog_v2_default_domain;
                    DROP FUNCTION IF EXISTS public.catalog_v2_default_value();
                    """
                )
            ws3_preflight.check(Path.cwd(), url)
        _expect_semantic_delta(
            connection,
            """
            CREATE TABLE public.catalog_v1_index_probe (value text COLLATE \"C\");
            CREATE INDEX catalog_v1_opclass_idx
                ON public.catalog_v1_index_probe USING btree (value text_ops);
            """,
            """
            DROP INDEX public.catalog_v1_opclass_idx;
            CREATE INDEX catalog_v1_opclass_idx
                ON public.catalog_v1_index_probe USING btree (value text_pattern_ops);
            """,
            """
            DROP TABLE public.catalog_v1_index_probe;
            """,
            url,
        )
        _expect_semantic_delta(
            connection,
            "CREATE TYPE public.catalog_v1_acl_type AS ENUM ('one', 'two');",
            "GRANT USAGE ON TYPE public.catalog_v1_acl_type TO grove_runtime;",
            "DROP TYPE public.catalog_v1_acl_type;",
            url,
        )
        _expect_semantic_delta(
            connection,
            """
            CREATE MATERIALIZED VIEW public.catalog_v1_matview AS SELECT 1 AS marker;
            """,
            "REFRESH MATERIALIZED VIEW public.catalog_v1_matview WITH NO DATA;",
            "DROP MATERIALIZED VIEW public.catalog_v1_matview;",
            url,
        )
        _expect_semantic_delta(
            connection,
            "CREATE DOMAIN public.catalog_v1_domain AS text;",
            "ALTER DOMAIN public.catalog_v1_domain ADD CONSTRAINT marker_ck CHECK (VALUE <> '');",
            "DROP DOMAIN public.catalog_v1_domain;",
            url,
        )
        _expect_semantic_delta(
            connection,
            """
            CREATE FUNCTION public.catalog_v1_agg_sfunc_one(state integer, value integer)
            RETURNS integer LANGUAGE sql IMMUTABLE AS $$ SELECT state + value $$;
            CREATE FUNCTION public.catalog_v1_agg_sfunc_two(state integer, value integer)
            RETURNS integer LANGUAGE sql IMMUTABLE AS $$ SELECT state - value $$;
            CREATE AGGREGATE public.catalog_v1_aggregate(integer)
            (SFUNC = public.catalog_v1_agg_sfunc_one, STYPE = integer, INITCOND = '0');
            """,
            """
            DROP AGGREGATE public.catalog_v1_aggregate(integer);
            CREATE AGGREGATE public.catalog_v1_aggregate(integer)
            (SFUNC = public.catalog_v1_agg_sfunc_two, STYPE = integer, INITCOND = '0');
            """,
            """
            DROP AGGREGATE public.catalog_v1_aggregate(integer);
            DROP FUNCTION public.catalog_v1_agg_sfunc_one(integer, integer);
            DROP FUNCTION public.catalog_v1_agg_sfunc_two(integer, integer);
            """,
            url,
        )
        _expect_semantic_delta(
            connection,
            """
            CREATE FUNCTION public.catalog_v1_operator_left(left_value text, right_value text)
            RETURNS boolean LANGUAGE sql IMMUTABLE AS $$ SELECT left_value = 'left' $$;
            CREATE FUNCTION public.catalog_v1_operator_right(left_value text, right_value text)
            RETURNS boolean LANGUAGE sql IMMUTABLE AS $$ SELECT right_value = 'right' $$;
            CREATE OPERATOR public.## (LEFTARG = text, RIGHTARG = text,
                PROCEDURE = public.catalog_v1_operator_left);
            """,
            """
            DROP OPERATOR public.## (text, text);
            CREATE OPERATOR public.## (LEFTARG = text, RIGHTARG = text,
                PROCEDURE = public.catalog_v1_operator_right);
            """,
            """
            DROP OPERATOR public.## (text, text);
            DROP FUNCTION public.catalog_v1_operator_left(text, text);
            DROP FUNCTION public.catalog_v1_operator_right(text, text);
            """,
            url,
        )
