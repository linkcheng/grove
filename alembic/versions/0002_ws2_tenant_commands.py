"""WS-2 tenant identity, immutable execution specs and start commands."""

from collections.abc import Sequence

from alembic import op

revision: str = "ws2_tenant_commands"
down_revision: str | None = "baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLES = (
    "tenant",
    "membership",
    "workload_principal",
    "execution_principal",
    "execution_spec",
    "command_payload",
    "agent_run",
    "run_command",
)


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE tenant (
            tenant_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT tenant_status_ck CHECK (status IN ('active', 'suspended'))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE membership (
            tenant_id TEXT NOT NULL,
            principal_id TEXT NOT NULL,
            principal_kind TEXT NOT NULL DEFAULT 'human',
            user_ref TEXT NOT NULL,
            roles JSONB NOT NULL DEFAULT '[]'::jsonb,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, principal_id),
            CONSTRAINT membership_tenant_fk FOREIGN KEY (tenant_id) REFERENCES tenant (tenant_id),
            CONSTRAINT membership_principal_kind_ck CHECK (principal_kind = 'human')
        )
        """
    )
    op.execute(
        """
        CREATE TABLE workload_principal (
            tenant_id TEXT NOT NULL,
            principal_id TEXT NOT NULL,
            principal_kind TEXT NOT NULL DEFAULT 'workload',
            workload_ref TEXT NOT NULL,
            scopes JSONB NOT NULL DEFAULT '[]'::jsonb,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, principal_id),
            CONSTRAINT workload_tenant_fk FOREIGN KEY (tenant_id) REFERENCES tenant (tenant_id),
            CONSTRAINT workload_principal_kind_ck CHECK (principal_kind = 'workload')
        )
        """
    )
    op.execute(
        """
        CREATE TABLE execution_principal (
            tenant_id TEXT NOT NULL,
            principal_id TEXT NOT NULL,
            principal_kind TEXT NOT NULL,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            PRIMARY KEY (tenant_id, principal_id, principal_kind),
            CONSTRAINT execution_principal_tenant_fk FOREIGN KEY (tenant_id)
                REFERENCES tenant (tenant_id),
            CONSTRAINT execution_principal_kind_ck CHECK (principal_kind IN ('human', 'workload'))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE execution_spec (
            tenant_id TEXT NOT NULL,
            skill_spec_hash TEXT NOT NULL,
            spec_ref TEXT NOT NULL,
            spec_payload JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, skill_spec_hash),
            CONSTRAINT execution_spec_tenant_hash_ref_uq UNIQUE (tenant_id, skill_spec_hash, spec_ref),
            CONSTRAINT execution_spec_tenant_fk FOREIGN KEY (tenant_id) REFERENCES tenant (tenant_id),
            CONSTRAINT execution_spec_hash_ck CHECK (length(skill_spec_hash) = 64)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE command_payload (
            tenant_id TEXT NOT NULL,
            payload_ref TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            command_schema_version TEXT NOT NULL,
            sensitivity TEXT NOT NULL DEFAULT 'sensitive',
            retention TEXT NOT NULL DEFAULT 'run_completion',
            payload JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, payload_ref),
            CONSTRAINT command_payload_tenant_fk FOREIGN KEY (tenant_id)
                REFERENCES tenant (tenant_id),
            CONSTRAINT command_payload_hash_uq UNIQUE (tenant_id, payload_hash),
            CONSTRAINT command_payload_ref_hash_schema_uq UNIQUE
                (tenant_id, payload_ref, payload_hash, command_schema_version),
            CONSTRAINT command_payload_hash_ck CHECK (length(payload_hash) = 64),
            CONSTRAINT command_payload_schema_version_ck CHECK (command_schema_version = 'start.v1'),
            CONSTRAINT command_payload_sensitivity_ck CHECK (sensitivity = 'sensitive'),
            CONSTRAINT command_payload_retention_ck CHECK (retention = 'run_completion')
        )
        """
    )
    op.execute(
        """
        CREATE TABLE agent_run (
            run_id UUID PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            submission_id UUID NOT NULL,
            submission_digest TEXT NOT NULL,
            principal_id TEXT NOT NULL,
            principal_kind TEXT NOT NULL,
            skill_spec_hash TEXT NOT NULL,
            skill_spec_ref TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'accepted',
            revision BIGINT NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT agent_run_tenant_fk FOREIGN KEY (tenant_id) REFERENCES tenant (tenant_id),
            CONSTRAINT agent_run_principal_fk FOREIGN KEY (tenant_id, principal_id, principal_kind)
                REFERENCES execution_principal (tenant_id, principal_id, principal_kind),
            CONSTRAINT agent_run_spec_fk FOREIGN KEY (tenant_id, skill_spec_hash, skill_spec_ref)
                REFERENCES execution_spec (tenant_id, skill_spec_hash, spec_ref),
            CONSTRAINT agent_run_tenant_submission_uq UNIQUE (tenant_id, submission_id),
            CONSTRAINT agent_run_tenant_run_uq UNIQUE (tenant_id, run_id),
            CONSTRAINT agent_run_run_principal_uq UNIQUE (tenant_id, run_id, principal_id, principal_kind),
            CONSTRAINT agent_run_principal_kind_ck CHECK (principal_kind IN ('human', 'workload')),
            CONSTRAINT agent_run_status_ck CHECK (status = 'accepted'),
            CONSTRAINT agent_run_revision_ck CHECK (revision = 0),
            CONSTRAINT agent_run_submission_digest_ck CHECK (length(submission_digest) = 64),
            CONSTRAINT agent_run_skill_spec_hash_ck CHECK (length(skill_spec_hash) = 64)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE run_command (
            command_id UUID PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            run_id UUID NOT NULL,
            principal_id TEXT NOT NULL,
            principal_kind TEXT NOT NULL,
            command_seq BIGINT NOT NULL,
            command_type TEXT NOT NULL,
            command_schema_version TEXT NOT NULL,
            command_digest TEXT NOT NULL,
            payload_ref TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT run_command_run_fk FOREIGN KEY (tenant_id, run_id)
                REFERENCES agent_run (tenant_id, run_id),
            CONSTRAINT run_command_principal_fk FOREIGN KEY (tenant_id, run_id, principal_id, principal_kind)
                REFERENCES agent_run (tenant_id, run_id, principal_id, principal_kind),
            CONSTRAINT run_command_identity_fk FOREIGN KEY (tenant_id, principal_id, principal_kind)
                REFERENCES execution_principal (tenant_id, principal_id, principal_kind),
            CONSTRAINT run_command_payload_fk FOREIGN KEY
                (tenant_id, payload_ref, payload_hash, command_schema_version)
                REFERENCES command_payload (tenant_id, payload_ref, payload_hash, command_schema_version),
            CONSTRAINT run_command_tenant_command_uq UNIQUE (tenant_id, command_id),
            CONSTRAINT run_command_run_seq_uq UNIQUE (tenant_id, run_id, command_seq),
            CONSTRAINT run_command_principal_kind_ck CHECK (principal_kind IN ('human', 'workload')),
            CONSTRAINT run_command_type_ck CHECK (command_type = 'start'),
            CONSTRAINT run_command_schema_version_ck CHECK (command_schema_version = 'start.v1'),
            CONSTRAINT run_command_status_ck CHECK (status = 'pending'),
            CONSTRAINT run_command_seq_ck CHECK (command_seq = 0),
            CONSTRAINT run_command_digest_ck CHECK (length(command_digest) = 64),
            CONSTRAINT run_command_payload_hash_ck CHECK (length(payload_hash) = 64)
        )
        """
    )
    op.execute("CREATE INDEX membership_tenant_principal_idx ON membership (tenant_id, principal_id)")
    op.execute("CREATE INDEX workload_tenant_principal_idx ON workload_principal (tenant_id, principal_id)")
    op.execute("CREATE INDEX execution_spec_tenant_hash_idx ON execution_spec (tenant_id, skill_spec_hash)")
    op.execute("CREATE INDEX agent_run_tenant_idx ON agent_run (tenant_id, run_id)")
    op.execute("CREATE INDEX run_command_tenant_run_idx ON run_command (tenant_id, run_id, command_seq)")

    # This trigger makes the polymorphic FK authoritative: an API connection
    # cannot manufacture an execution principal without a matching identity.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION grove_validate_execution_principal() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.active AND NEW.principal_kind = 'human' AND NOT EXISTS (
                SELECT 1 FROM membership
                WHERE tenant_id = NEW.tenant_id AND principal_id = NEW.principal_id
                  AND principal_kind = 'human'
            ) THEN
                RAISE EXCEPTION 'execution principal is not backed by membership';
            END IF;
            IF NEW.active AND NEW.principal_kind = 'workload' AND NOT EXISTS (
                SELECT 1 FROM workload_principal
                WHERE tenant_id = NEW.tenant_id AND principal_id = NEW.principal_id
                  AND principal_kind = 'workload'
            ) THEN
                RAISE EXCEPTION 'execution principal is not backed by workload principal';
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER execution_principal_identity_guard
        BEFORE INSERT OR UPDATE ON execution_principal
        FOR EACH ROW EXECUTE FUNCTION grove_validate_execution_principal()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION grove_reject_identity_key_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.tenant_id <> OLD.tenant_id
               OR NEW.principal_id <> OLD.principal_id
               OR NEW.principal_kind <> OLD.principal_kind THEN
                RAISE EXCEPTION 'identity key is immutable; deactivate the existing identity instead';
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER membership_identity_key_guard
        BEFORE UPDATE ON membership
        FOR EACH ROW EXECUTE FUNCTION grove_reject_identity_key_change()
        """
    )
    op.execute(
        """
        CREATE TRIGGER workload_identity_key_guard
        BEFORE UPDATE ON workload_principal
        FOR EACH ROW EXECUTE FUNCTION grove_reject_identity_key_change()
        """
    )
    op.execute(
        """
        CREATE TRIGGER execution_principal_identity_key_guard
        BEFORE UPDATE ON execution_principal
        FOR EACH ROW EXECUTE FUNCTION grove_reject_identity_key_change()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION grove_sync_execution_principal() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                UPDATE execution_principal
                   SET active = FALSE
                 WHERE tenant_id = OLD.tenant_id
                   AND principal_id = OLD.principal_id
                   AND principal_kind = OLD.principal_kind;
                RETURN OLD;
            END IF;
            INSERT INTO execution_principal (tenant_id, principal_id, principal_kind, active)
            VALUES (NEW.tenant_id, NEW.principal_id, NEW.principal_kind, NEW.active)
            ON CONFLICT (tenant_id, principal_id, principal_kind)
            DO UPDATE SET active = EXCLUDED.active;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER membership_execution_principal_sync
        AFTER INSERT OR UPDATE OR DELETE ON membership
        FOR EACH ROW EXECUTE FUNCTION grove_sync_execution_principal()
        """
    )
    op.execute(
        """
        CREATE TRIGGER workload_execution_principal_sync
        AFTER INSERT OR UPDATE OR DELETE ON workload_principal
        FOR EACH ROW EXECUTE FUNCTION grove_sync_execution_principal()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION grove_reject_immutable_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'immutable WS-2 artifact cannot be changed';
        END $$
        """
    )
    for table in ("execution_spec", "command_payload"):
        op.execute(
            f"CREATE TRIGGER {table}_immutable_guard BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION grove_reject_immutable_change()"
        )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION grove_active_tenant() RETURNS TEXT
        LANGUAGE sql STABLE PARALLEL SAFE
        AS $$ SELECT NULLIF(current_setting('grove.tenant_id', true), '') $$
        """
    )
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant_isolation ON {table} "
            "USING (tenant_id = grove_active_tenant()) "
            "WITH CHECK (tenant_id = grove_active_tenant())"
        )

    # Role grants are deliberately explicit. The API can create accepted
    # rows/artifacts but cannot update or delete immutable/run state.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grove_api') THEN
                GRANT SELECT ON tenant, membership, workload_principal, execution_principal TO grove_api;
                GRANT SELECT (tenant_id, skill_spec_hash, spec_ref, created_at)
                    ON execution_spec TO grove_api;
                GRANT SELECT ON agent_run, run_command TO grove_api;
                GRANT SELECT (tenant_id, payload_ref, payload_hash, command_schema_version,
                    sensitivity, retention, created_at) ON command_payload TO grove_api;
                GRANT INSERT (tenant_id, skill_spec_hash, spec_ref, spec_payload)
                    ON execution_spec TO grove_api;
                GRANT INSERT (tenant_id, payload_ref, payload_hash, command_schema_version,
                    sensitivity, retention, payload)
                    ON command_payload TO grove_api;
                GRANT INSERT (run_id, tenant_id, submission_id, submission_digest, principal_id,
                    principal_kind, skill_spec_hash, skill_spec_ref, status, revision)
                    ON agent_run TO grove_api;
                GRANT INSERT (command_id, tenant_id, run_id, principal_id, principal_kind,
                    command_seq, command_type, command_schema_version, command_digest,
                    payload_ref, payload_hash, status)
                    ON run_command TO grove_api;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grove_runtime') THEN
                GRANT SELECT ON tenant, membership, workload_principal, execution_principal,
                    agent_run, run_command TO grove_runtime;
                GRANT SELECT (tenant_id, skill_spec_hash, spec_ref, created_at)
                    ON execution_spec TO grove_runtime;
                GRANT SELECT (tenant_id, payload_ref, payload_hash, command_schema_version,
                    sensitivity, retention, created_at) ON command_payload TO grove_runtime;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grove_projection') THEN
                GRANT SELECT ON tenant, membership, workload_principal, execution_principal,
                    agent_run, run_command TO grove_projection;
                GRANT SELECT (tenant_id, skill_spec_hash, spec_ref, created_at)
                    ON execution_spec TO grove_projection;
                GRANT SELECT (tenant_id, payload_ref, payload_hash, command_schema_version,
                    sensitivity, retention, created_at) ON command_payload TO grove_projection;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grove_governance') THEN
                GRANT SELECT ON tenant, membership, workload_principal, execution_principal,
                    agent_run, run_command TO grove_governance;
                GRANT SELECT (tenant_id, skill_spec_hash, spec_ref, created_at)
                    ON execution_spec TO grove_governance;
                GRANT SELECT (tenant_id, payload_ref, payload_hash, command_schema_version,
                    sensitivity, retention, created_at) ON command_payload TO grove_governance;
            END IF;
        END $$
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grove_api') THEN
                GRANT SELECT ON alembic_version TO grove_api;
            END IF;
        END $$
        """
    )


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute("DROP FUNCTION IF EXISTS grove_active_tenant()")
    op.execute("DROP FUNCTION IF EXISTS grove_validate_execution_principal()")
    op.execute("DROP FUNCTION IF EXISTS grove_sync_execution_principal()")
    op.execute("DROP FUNCTION IF EXISTS grove_reject_identity_key_change()")
    op.execute("DROP FUNCTION IF EXISTS grove_reject_immutable_change()")
