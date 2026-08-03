-- WS-0 test-only role bootstrap. The application processes never use the
-- grove superuser; each role receives its own login and connection pool.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grove_api') THEN
        CREATE ROLE grove_api LOGIN PASSWORD 'grove_api_ws0';
    ELSE
        ALTER ROLE grove_api LOGIN PASSWORD 'grove_api_ws0';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grove_runtime') THEN
        CREATE ROLE grove_runtime LOGIN PASSWORD 'grove_runtime_ws0';
    ELSE
        ALTER ROLE grove_runtime LOGIN PASSWORD 'grove_runtime_ws0';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grove_projection') THEN
        CREATE ROLE grove_projection LOGIN PASSWORD 'grove_projection_ws0';
    ELSE
        ALTER ROLE grove_projection LOGIN PASSWORD 'grove_projection_ws0';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grove_governance') THEN
        CREATE ROLE grove_governance LOGIN PASSWORD 'grove_governance_ws0';
    ELSE
        ALTER ROLE grove_governance LOGIN PASSWORD 'grove_governance_ws0';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grove_migration') THEN
        CREATE ROLE grove_migration LOGIN PASSWORD 'grove_migration_ws0';
    ELSE
        ALTER ROLE grove_migration LOGIN PASSWORD 'grove_migration_ws0';
    END IF;
    IF to_regclass('public.alembic_version') IS NOT NULL THEN
        ALTER TABLE public.alembic_version OWNER TO grove_migration;
    END IF;
END
$$;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT CONNECT ON DATABASE grove TO grove_api, grove_runtime, grove_projection, grove_governance, grove_migration;
GRANT USAGE ON SCHEMA public TO grove_api, grove_runtime, grove_projection, grove_governance;
GRANT USAGE, CREATE ON SCHEMA public TO grove_migration;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO grove_migration;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO grove_migration;
