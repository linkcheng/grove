"""Add the WS-3 PostgreSQL execution lease and durable fence slice."""

from collections.abc import Sequence

from alembic import op

revision: str = "ws3_execution_driver"
down_revision: str | None = "ws2_tenant_commands"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE agent_run ADD COLUMN runtime_build_ref TEXT")
    op.execute("ALTER TABLE agent_run ADD COLUMN runtime_build_hash TEXT")
    op.execute(
        """
        UPDATE agent_run AS r
           SET runtime_build_ref = s.spec_payload #>> '{runtime_build,ref}',
               runtime_build_hash = s.spec_payload #>> '{runtime_build,content_hash}'
          FROM execution_spec AS s
         WHERE s.tenant_id = r.tenant_id
           AND s.skill_spec_hash = r.skill_spec_hash
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM agent_run
                 WHERE runtime_build_ref IS NULL
                    OR runtime_build_hash !~ '^[0-9a-f]{64}$'
            ) THEN
                RAISE EXCEPTION 'existing run lacks a verified runtime build binding';
            END IF;
        END $$
        """
    )
    op.execute("ALTER TABLE agent_run ALTER COLUMN runtime_build_ref SET NOT NULL")
    op.execute("ALTER TABLE agent_run ALTER COLUMN runtime_build_hash SET NOT NULL")
    op.execute("ALTER TABLE agent_run ADD COLUMN execution_fence BIGINT NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE agent_run ADD COLUMN lease_owner TEXT")
    op.execute("ALTER TABLE agent_run ADD COLUMN lease_until TIMESTAMPTZ")

    op.execute("ALTER TABLE run_command ADD COLUMN available_at TIMESTAMPTZ NOT NULL DEFAULT now()")
    op.execute("ALTER TABLE run_command ADD COLUMN lease_owner TEXT")
    op.execute("ALTER TABLE run_command ADD COLUMN lease_until TIMESTAMPTZ")
    op.execute("ALTER TABLE run_command ADD COLUMN execution_fence BIGINT")
    op.execute("ALTER TABLE run_command ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE run_command ADD COLUMN last_error_ref TEXT")

    op.execute("ALTER TABLE agent_run DROP CONSTRAINT agent_run_status_ck")
    op.execute("ALTER TABLE agent_run DROP CONSTRAINT agent_run_revision_ck")
    op.execute(
        """
        ALTER TABLE agent_run ADD CONSTRAINT agent_run_status_ck CHECK (status IN (
            'accepted', 'running', 'waiting_user_input', 'waiting_action_result',
            'waiting_child_result', 'cancel_requested', 'succeeded', 'failed', 'cancelled'
        ))
        """
    )
    op.execute("ALTER TABLE agent_run ADD CONSTRAINT agent_run_revision_ck CHECK (revision >= 0)")
    op.execute("ALTER TABLE agent_run ADD CONSTRAINT agent_run_execution_fence_ck CHECK (execution_fence >= 0)")
    op.execute(
        "ALTER TABLE agent_run ADD CONSTRAINT agent_run_runtime_build_hash_ck "
        "CHECK (runtime_build_hash ~ '^[0-9a-f]{64}$')"
    )

    for constraint in (
        "run_command_type_ck",
        "run_command_schema_version_ck",
        "run_command_status_ck",
        "run_command_seq_ck",
    ):
        op.execute(f"ALTER TABLE run_command DROP CONSTRAINT {constraint}")
    op.execute(
        "ALTER TABLE run_command ADD CONSTRAINT run_command_type_ck "
        "CHECK (command_type IN ('start', 'resume', 'cancel', 'continue', 'signal'))"
    )
    op.execute(
        "ALTER TABLE run_command ADD CONSTRAINT run_command_schema_version_ck CHECK ("
        "(command_type = 'start' AND command_schema_version = 'start.v1') OR "
        "(command_type = 'resume' AND command_schema_version = 'resume.v1') OR "
        "(command_type = 'cancel' AND command_schema_version = 'cancel.v1') OR "
        "(command_type = 'continue' AND command_schema_version = 'continue.v1') OR "
        "(command_type = 'signal' AND command_schema_version = 'signal.v1'))"
    )
    op.execute(
        "ALTER TABLE run_command ADD CONSTRAINT run_command_status_ck "
        "CHECK (status IN ('pending', 'leased', 'consumed', 'dead_letter'))"
    )
    op.execute("ALTER TABLE run_command ADD CONSTRAINT run_command_seq_ck CHECK (command_seq >= 0)")
    op.execute("ALTER TABLE run_command ADD CONSTRAINT run_command_attempt_count_ck CHECK (attempt_count >= 0)")
    op.execute(
        """
        ALTER TABLE run_command ADD CONSTRAINT run_command_lease_shape_ck CHECK (
            (status = 'leased' AND lease_owner IS NOT NULL AND lease_until IS NOT NULL
             AND execution_fence IS NOT NULL)
            OR
            (status <> 'leased' AND lease_owner IS NULL AND lease_until IS NULL
             AND execution_fence IS NULL)
        )
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION grove_reject_execution_fence_regression() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            IF NEW.execution_fence < OLD.execution_fence THEN
                RAISE EXCEPTION 'execution fence cannot decrease';
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER agent_run_execution_fence_guard
        BEFORE UPDATE OF execution_fence ON agent_run
        FOR EACH ROW EXECUTE FUNCTION grove_reject_execution_fence_regression()
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION grove_claim_run_command(
            p_tenant_id TEXT,
            p_worker_id TEXT,
            p_runtime_build_hash TEXT,
            p_lease_seconds DOUBLE PRECISION
        ) RETURNS TABLE (
            result_code TEXT,
            command_id UUID,
            run_id UUID,
            command_seq BIGINT,
            command_digest TEXT,
            runtime_build_hash TEXT,
            execution_fence BIGINT,
            lease_until TIMESTAMPTZ
        )
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        DECLARE
            claimed_command_id UUID;
            claimed_run_id UUID;
            claimed_command_seq BIGINT;
            claimed_command_digest TEXT;
            claimed_runtime_build_hash TEXT;
            locked_execution_fence BIGINT;
            next_fence BIGINT;
            next_lease_until TIMESTAMPTZ;
            database_now TIMESTAMPTZ;
        BEGIN
            IF session_user <> 'grove_runtime' THEN
                RAISE EXCEPTION 'runtime claim role required' USING ERRCODE = '42501';
            END IF;
            IF NULLIF(current_setting('grove.tenant_id', true), '') IS DISTINCT FROM p_tenant_id THEN
                RAISE EXCEPTION 'runtime tenant scope mismatch' USING ERRCODE = '42501';
            END IF;
            IF p_worker_id IS NULL OR length(p_worker_id) NOT BETWEEN 1 AND 256
               OR p_runtime_build_hash !~ '^[0-9a-f]{64}$'
               OR p_lease_seconds IS NULL OR p_lease_seconds <= 0 OR p_lease_seconds > 90
               OR p_lease_seconds = 'Infinity'::float8 OR p_lease_seconds = '-Infinity'::float8
               OR p_lease_seconds = 'NaN'::float8 THEN
                RAISE EXCEPTION 'invalid runtime claim arguments' USING ERRCODE = '22023';
            END IF;

            database_now := clock_timestamp();
            SELECT c.command_id, c.run_id, c.command_seq, c.command_digest,
                   r.runtime_build_hash, r.execution_fence
              INTO claimed_command_id, claimed_run_id, claimed_command_seq,
                   claimed_command_digest, claimed_runtime_build_hash, locked_execution_fence
              FROM public.run_command AS c
              JOIN public.agent_run AS r
                ON r.tenant_id = c.tenant_id AND r.run_id = c.run_id
             WHERE r.tenant_id = p_tenant_id
               AND r.runtime_build_hash = p_runtime_build_hash
               AND r.status NOT IN ('succeeded', 'failed', 'cancelled')
               AND c.available_at <= database_now
               AND (c.status = 'pending' OR (c.status = 'leased' AND c.lease_until <= database_now))
               AND (r.lease_until IS NULL OR r.lease_until <= database_now)
             ORDER BY c.available_at, c.created_at, c.command_id
             FOR UPDATE OF c, r SKIP LOCKED
             LIMIT 1;

            IF NOT FOUND THEN
                IF EXISTS (
                    SELECT 1
                      FROM public.run_command AS c
                      JOIN public.agent_run AS r
                        ON r.tenant_id = c.tenant_id AND r.run_id = c.run_id
                     WHERE c.tenant_id = p_tenant_id
                       AND r.runtime_build_hash = p_runtime_build_hash
                       AND r.status NOT IN ('succeeded', 'failed', 'cancelled')
                       AND c.status IN ('pending', 'leased')
                ) THEN
                    RETURN;
                END IF;
                IF EXISTS (
                    SELECT 1
                      FROM public.run_command AS c
                      JOIN public.agent_run AS r
                        ON r.tenant_id = c.tenant_id AND r.run_id = c.run_id
                     WHERE c.tenant_id = p_tenant_id
                       AND r.runtime_build_hash <> p_runtime_build_hash
                       AND r.status NOT IN ('succeeded', 'failed', 'cancelled')
                       AND c.status IN ('pending', 'leased')
                ) THEN
                    RETURN QUERY SELECT 'version_unavailable'::TEXT, NULL::UUID, NULL::UUID,
                        NULL::BIGINT, NULL::TEXT, NULL::TEXT, NULL::BIGINT, NULL::TIMESTAMPTZ;
                END IF;
                RETURN;
            END IF;

            next_fence := locked_execution_fence + 1;
            next_lease_until := database_now + make_interval(secs => p_lease_seconds);
            UPDATE public.agent_run
               SET execution_fence = next_fence,
                   lease_owner = p_worker_id,
                   lease_until = next_lease_until,
                   status = CASE WHEN status = 'accepted' THEN 'running' ELSE status END,
                   updated_at = clock_timestamp()
             WHERE tenant_id = p_tenant_id AND agent_run.run_id = claimed_run_id;
            UPDATE public.run_command
               SET status = 'leased', lease_owner = p_worker_id,
                   lease_until = next_lease_until, execution_fence = next_fence,
                   attempt_count = attempt_count + 1
             WHERE tenant_id = p_tenant_id AND run_command.command_id = claimed_command_id;

            RETURN QUERY SELECT 'claimed'::TEXT, claimed_command_id, claimed_run_id,
                claimed_command_seq, claimed_command_digest, claimed_runtime_build_hash,
                next_fence, next_lease_until;
        END $$
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION grove_heartbeat_run_command(
            p_tenant_id TEXT,
            p_run_id UUID,
            p_command_id UUID,
            p_command_seq BIGINT,
            p_command_digest TEXT,
            p_runtime_build_hash TEXT,
            p_worker_id TEXT,
            p_execution_fence BIGINT,
            p_expected_lease_until TIMESTAMPTZ,
            p_lease_seconds DOUBLE PRECISION
        ) RETURNS TIMESTAMPTZ
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        DECLARE
            command_row public.run_command%ROWTYPE;
            run_row public.agent_run%ROWTYPE;
            next_lease_until TIMESTAMPTZ;
            database_now TIMESTAMPTZ;
        BEGIN
            IF session_user <> 'grove_runtime' THEN
                RAISE EXCEPTION 'runtime heartbeat role required' USING ERRCODE = '42501';
            END IF;
            IF NULLIF(current_setting('grove.tenant_id', true), '') IS DISTINCT FROM p_tenant_id THEN
                RAISE EXCEPTION 'runtime tenant scope mismatch' USING ERRCODE = '42501';
            END IF;
            IF p_worker_id IS NULL OR length(p_worker_id) NOT BETWEEN 1 AND 256
               OR p_command_seq < 0 OR p_command_digest !~ '^[0-9a-f]{64}$'
               OR p_runtime_build_hash !~ '^[0-9a-f]{64}$'
               OR p_execution_fence < 1 OR p_expected_lease_until IS NULL OR p_lease_seconds IS NULL
               OR p_lease_seconds <= 0 OR p_lease_seconds > 90
               OR p_lease_seconds = 'Infinity'::float8 OR p_lease_seconds = '-Infinity'::float8
               OR p_lease_seconds = 'NaN'::float8 THEN
                RAISE EXCEPTION 'invalid runtime heartbeat arguments' USING ERRCODE = '22023';
            END IF;

            database_now := clock_timestamp();
            SELECT * INTO run_row FROM public.agent_run
             WHERE tenant_id = p_tenant_id AND agent_run.run_id = p_run_id
             FOR UPDATE;
            SELECT * INTO command_row FROM public.run_command
             WHERE tenant_id = p_tenant_id AND run_command.command_id = p_command_id
             FOR UPDATE;
            IF run_row.run_id IS NULL OR command_row.command_id IS NULL
               OR command_row.run_id <> p_run_id
               OR command_row.command_seq <> p_command_seq
               OR command_row.command_digest <> p_command_digest
               OR command_row.status <> 'leased'
               OR command_row.lease_owner <> p_worker_id
               OR command_row.execution_fence <> p_execution_fence
               OR command_row.lease_until IS DISTINCT FROM p_expected_lease_until
               OR command_row.lease_until <= database_now
               OR run_row.lease_owner <> p_worker_id
               OR run_row.runtime_build_hash <> p_runtime_build_hash
               OR run_row.execution_fence <> p_execution_fence
               OR run_row.lease_until IS DISTINCT FROM p_expected_lease_until
               OR run_row.lease_until <= database_now THEN
                RETURN NULL;
            END IF;

            next_lease_until := database_now + make_interval(secs => p_lease_seconds);
            IF next_lease_until <= run_row.lease_until OR next_lease_until <= command_row.lease_until THEN
                RETURN NULL;
            END IF;
            UPDATE public.agent_run SET lease_until = next_lease_until, updated_at = clock_timestamp()
             WHERE tenant_id = p_tenant_id AND agent_run.run_id = p_run_id;
            UPDATE public.run_command SET lease_until = next_lease_until
             WHERE tenant_id = p_tenant_id AND run_command.command_id = p_command_id;
            RETURN next_lease_until;
        END $$
        """
    )

    op.execute("REVOKE ALL ON FUNCTION grove_claim_run_command(TEXT, TEXT, TEXT, DOUBLE PRECISION) FROM PUBLIC")
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "grove_heartbeat_run_command("
        "TEXT, UUID, UUID, BIGINT, TEXT, TEXT, TEXT, BIGINT, TIMESTAMPTZ, DOUBLE PRECISION) FROM PUBLIC"
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grove_api') THEN
                GRANT INSERT (runtime_build_ref, runtime_build_hash) ON agent_run TO grove_api;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grove_runtime') THEN
                GRANT EXECUTE ON FUNCTION grove_claim_run_command(TEXT, TEXT, TEXT, DOUBLE PRECISION)
                    TO grove_runtime;
                GRANT EXECUTE ON FUNCTION grove_heartbeat_run_command(
                    TEXT, UUID, UUID, BIGINT, TEXT, TEXT, TEXT, BIGINT, TIMESTAMPTZ, DOUBLE PRECISION)
                    TO grove_runtime;
            END IF;
        END $$
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS grove_heartbeat_run_command("
        "TEXT, UUID, UUID, BIGINT, TEXT, TEXT, TEXT, BIGINT, TIMESTAMPTZ, DOUBLE PRECISION)"
    )
    op.execute("DROP FUNCTION IF EXISTS grove_claim_run_command(TEXT, TEXT, TEXT, DOUBLE PRECISION)")
    op.execute("DROP TRIGGER IF EXISTS agent_run_execution_fence_guard ON agent_run")
    op.execute("DROP FUNCTION IF EXISTS grove_reject_execution_fence_regression()")
    op.execute("ALTER TABLE run_command DROP CONSTRAINT run_command_lease_shape_ck")
    op.execute("ALTER TABLE run_command DROP CONSTRAINT run_command_attempt_count_ck")
    for constraint in (
        "run_command_type_ck",
        "run_command_schema_version_ck",
        "run_command_status_ck",
        "run_command_seq_ck",
    ):
        op.execute(f"ALTER TABLE run_command DROP CONSTRAINT {constraint}")
    op.execute("ALTER TABLE run_command ADD CONSTRAINT run_command_type_ck CHECK (command_type = 'start')")
    op.execute(
        "ALTER TABLE run_command ADD CONSTRAINT run_command_schema_version_ck "
        "CHECK (command_schema_version = 'start.v1')"
    )
    op.execute("ALTER TABLE run_command ADD CONSTRAINT run_command_status_ck CHECK (status = 'pending')")
    op.execute("ALTER TABLE run_command ADD CONSTRAINT run_command_seq_ck CHECK (command_seq = 0)")
    for column in (
        "last_error_ref",
        "attempt_count",
        "execution_fence",
        "lease_until",
        "lease_owner",
        "available_at",
    ):
        op.execute(f"ALTER TABLE run_command DROP COLUMN {column}")

    op.execute("ALTER TABLE agent_run DROP CONSTRAINT agent_run_runtime_build_hash_ck")
    op.execute("ALTER TABLE agent_run DROP CONSTRAINT agent_run_execution_fence_ck")
    op.execute("ALTER TABLE agent_run DROP CONSTRAINT agent_run_status_ck")
    op.execute("ALTER TABLE agent_run DROP CONSTRAINT agent_run_revision_ck")
    op.execute("ALTER TABLE agent_run ADD CONSTRAINT agent_run_status_ck CHECK (status = 'accepted')")
    op.execute("ALTER TABLE agent_run ADD CONSTRAINT agent_run_revision_ck CHECK (revision = 0)")
    for column in ("lease_until", "lease_owner", "execution_fence", "runtime_build_hash", "runtime_build_ref"):
        op.execute(f"ALTER TABLE agent_run DROP COLUMN {column}")
