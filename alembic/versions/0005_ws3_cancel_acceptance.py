"""Add atomic production CancelRun acceptance and cancel-aware claiming.

Cancellation is a database-owned state transition.  The function locks the
run before its command rows, verifies the exact typed payload body, revokes the
run fence and lease, closes older pending/leased delivery rows with an explicit
superseding cancel identity, and inserts the cancel command before the
transaction can commit.  The claim function is redefined in the same revision
so superseded rows are never eligible work and a pending cancel wins over any
older delivery row.  A data-bearing downgrade fails closed because the
pre-cancel lease/status facts are not durably reconstructible in this schema.
"""

# The SQL strings below are migration-owned constants; they contain no user
# values.  The rule is disabled for this file because Alembic receives raw SQL.
# ruff: noqa: S608

from collections.abc import Sequence

from alembic import op

revision: str = "ws3_cancel_acceptance"
down_revision: str | None = "ws3_checkpoint_fenced"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _claim_function_sql() -> str:
    return """
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
            candidate_command_id UUID;
            candidate_run_id UUID;
            candidate_command_seq BIGINT;
            candidate_command_digest TEXT;
            candidate_runtime_build_hash TEXT;
            locked_execution_fence BIGINT;
            next_fence BIGINT;
            next_lease_until TIMESTAMPTZ;
            database_now TIMESTAMPTZ;
            command_row public.run_command%ROWTYPE;
        BEGIN
            IF session_user <> 'grove_runtime' THEN
                RAISE EXCEPTION 'runtime claim role required' USING ERRCODE = '42501';
            END IF;
            IF NULLIF(current_setting('grove.tenant_id', true), '') IS DISTINCT FROM p_tenant_id THEN
                RAISE EXCEPTION 'runtime tenant scope mismatch' USING ERRCODE = '42501';
            END IF;
            IF p_worker_id IS NULL OR length(p_worker_id) NOT BETWEEN 1 AND 256
               OR p_runtime_build_hash IS NULL OR p_runtime_build_hash !~ '^[0-9a-f]{64}$'
               OR p_lease_seconds IS NULL OR p_lease_seconds <= 0 OR p_lease_seconds > 90
               OR p_lease_seconds = 'Infinity'::float8 OR p_lease_seconds = '-Infinity'::float8
               OR p_lease_seconds = 'NaN'::float8 THEN
                RAISE EXCEPTION 'invalid runtime claim arguments' USING ERRCODE = '22023';
            END IF;

            database_now := clock_timestamp();
            -- Lock the run first.  Cancellation uses the same order, so a
            -- claim and cancel can never hold opposite row locks for a run.
            SELECT c.command_id, c.run_id, c.command_seq, c.command_digest,
                   r.runtime_build_hash, r.execution_fence
              INTO candidate_command_id, candidate_run_id, candidate_command_seq,
                   candidate_command_digest, candidate_runtime_build_hash, locked_execution_fence
              FROM public.agent_run AS r
              JOIN LATERAL (
                    SELECT c.command_id, c.run_id, c.command_seq, c.command_digest
                      FROM public.run_command AS c
                     WHERE c.tenant_id = p_tenant_id
                       AND c.run_id = r.run_id
                       AND c.superseded_by_command_id IS NULL
                       AND c.available_at <= database_now
                       AND (c.status = 'pending' OR (c.status = 'leased' AND c.lease_until <= database_now))
                     ORDER BY (c.command_type = 'cancel') DESC, c.available_at, c.created_at, c.command_id
                     LIMIT 1
              ) AS c ON TRUE
             WHERE r.tenant_id = p_tenant_id
               AND r.runtime_build_hash = p_runtime_build_hash
               AND r.status NOT IN ('succeeded', 'failed', 'cancelled')
               AND (r.lease_until IS NULL OR r.lease_until <= database_now)
             ORDER BY (c.command_id IS NOT NULL), c.command_id
             FOR UPDATE OF r, c SKIP LOCKED
             LIMIT 1;

            IF FOUND THEN
                SELECT * INTO command_row
                  FROM public.run_command AS c
                 WHERE c.tenant_id = p_tenant_id AND c.command_id = candidate_command_id
                 FOR UPDATE;
                IF FOUND AND command_row.superseded_by_command_id IS NULL
                   AND command_row.available_at <= database_now
                   AND (command_row.status = 'pending'
                        OR (command_row.status = 'leased' AND command_row.lease_until <= database_now)) THEN
                    IF locked_execution_fence >= 9223372036854775807 THEN
                        RETURN QUERY SELECT 'fence_exhausted'::TEXT, candidate_command_id, candidate_run_id,
                            candidate_command_seq, candidate_command_digest, candidate_runtime_build_hash,
                            locked_execution_fence, NULL::TIMESTAMPTZ;
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
                     WHERE tenant_id = p_tenant_id AND agent_run.run_id = candidate_run_id;
                    UPDATE public.run_command
                       SET status = 'leased', lease_owner = p_worker_id,
                           lease_until = next_lease_until, execution_fence = next_fence,
                           attempt_count = attempt_count + 1
                     WHERE tenant_id = p_tenant_id AND run_command.command_id = candidate_command_id;

                    RETURN QUERY SELECT 'claimed'::TEXT, candidate_command_id, candidate_run_id,
                        candidate_command_seq, candidate_command_digest, candidate_runtime_build_hash,
                        next_fence, next_lease_until;
                    RETURN;
                END IF;
            END IF;

            -- A matching build that is locked, future-dated, leased, or has a
            -- different active run lease is not ready and must not become
            -- VersionUnavailable.  Superseded rows are closed, not work.
            IF EXISTS (
                SELECT 1
                  FROM public.run_command AS c
                  JOIN public.agent_run AS r
                    ON r.tenant_id = c.tenant_id AND r.run_id = c.run_id
                 WHERE c.tenant_id = p_tenant_id
                   AND r.runtime_build_hash = p_runtime_build_hash
                   AND r.status NOT IN ('succeeded', 'failed', 'cancelled')
                   AND c.superseded_by_command_id IS NULL
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
                   AND c.superseded_by_command_id IS NULL
                   AND c.status IN ('pending', 'leased')
            ) THEN
                RETURN QUERY SELECT 'version_unavailable'::TEXT, NULL::UUID, NULL::UUID,
                    NULL::BIGINT, NULL::TEXT, NULL::TEXT, NULL::BIGINT, NULL::TIMESTAMPTZ;
            END IF;
            RETURN;
        END $$
    """


def upgrade() -> None:
    # The runtime build is part of the immutable run identity.  It is copied
    # from the content-addressed execution spec during 0003 and must never be
    # rebound in place: cancel/claim decisions otherwise become dependent on a
    # mutable row value rather than the durable build selected at submission.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION grove_reject_agent_run_runtime_build_rebinding() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            IF NEW.runtime_build_ref IS DISTINCT FROM OLD.runtime_build_ref
               OR NEW.runtime_build_hash IS DISTINCT FROM OLD.runtime_build_hash THEN
                RAISE EXCEPTION 'agent_run runtime build binding is immutable' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER agent_run_runtime_build_guard
        BEFORE UPDATE OF runtime_build_ref, runtime_build_hash ON agent_run
        FOR EACH ROW EXECUTE FUNCTION grove_reject_agent_run_runtime_build_rebinding()
        """
    )
    op.execute(
        """
        REVOKE ALL ON FUNCTION grove_reject_agent_run_runtime_build_rebinding() FROM PUBLIC
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grove_migration') THEN
                GRANT EXECUTE ON FUNCTION grove_reject_agent_run_runtime_build_rebinding()
                    TO grove_migration;
            END IF;
        END $$
        """
    )
    cancel_sql = """
        CREATE OR REPLACE FUNCTION grove_accept_cancel_run(
            p_tenant_id TEXT,
            p_run_id UUID,
            p_command_id UUID,
            p_expected_revision BIGINT,
            p_command_digest TEXT,
            p_runtime_build_hash TEXT,
            p_payload_ref TEXT,
            p_payload_hash TEXT,
            p_payload JSONB
        ) RETURNS TABLE (
            result_code TEXT,
            command_id UUID,
            tenant_id TEXT,
            run_id UUID,
            command_seq BIGINT,
            command_type TEXT,
            command_schema_version TEXT,
            command_digest TEXT,
            runtime_build_hash TEXT,
            status TEXT
        )
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        DECLARE
            run_row public.agent_run%ROWTYPE;
            existing_command public.run_command%ROWTYPE;
            payload_exists BOOLEAN;
            next_command_seq BIGINT;
        BEGIN
            IF session_user <> 'grove_api' THEN
                RAISE EXCEPTION 'cancel acceptance role required' USING ERRCODE = '42501';
            END IF;
            IF NULLIF(current_setting('grove.tenant_id', true), '') IS DISTINCT FROM p_tenant_id THEN
                RAISE EXCEPTION 'cancel tenant scope mismatch' USING ERRCODE = '42501';
            END IF;
            IF p_tenant_id IS NULL OR length(p_tenant_id) NOT BETWEEN 1 AND 128
               OR p_run_id IS NULL OR p_command_id IS NULL
               OR p_expected_revision IS NULL OR p_expected_revision < 0
               OR p_command_digest IS NULL OR p_command_digest !~ '^[0-9a-f]{64}$'
               OR p_runtime_build_hash IS NULL OR p_runtime_build_hash !~ '^[0-9a-f]{64}$'
               OR p_payload_ref IS NULL OR length(p_payload_ref) NOT BETWEEN 1 AND 256
               OR p_payload_hash IS NULL OR p_payload_hash !~ '^[0-9a-f]{64}$'
               OR p_payload IS NULL OR jsonb_typeof(p_payload) <> 'object' THEN
                RAISE EXCEPTION 'invalid cancel acceptance arguments' USING ERRCODE = '22023';
            END IF;
            IF p_expected_revision >= 9223372036854775807 THEN
                RETURN QUERY SELECT 'revision_overflow'::TEXT, p_command_id, p_tenant_id, p_run_id,
                    NULL::BIGINT, 'cancel'::TEXT, 'cancel.v1'::TEXT, p_command_digest,
                    p_runtime_build_hash, 'unknown'::TEXT;
                RETURN;
            END IF;
            -- The run is the lifecycle/fence owner.  Every cancel and claim
            -- takes this lock before touching command rows.
            SELECT * INTO run_row
              FROM public.agent_run AS r
             WHERE r.tenant_id = p_tenant_id AND r.run_id = p_run_id
             FOR UPDATE;
            IF NOT FOUND THEN
                RETURN QUERY SELECT 'run_not_found'::TEXT, p_command_id, p_tenant_id, p_run_id,
                    NULL::BIGINT, 'cancel'::TEXT, 'cancel.v1'::TEXT, p_command_digest,
                    p_runtime_build_hash, 'unknown'::TEXT;
                RETURN;
            END IF;

            -- The API transaction inserts the typed wrapper before entering
            -- this function.  The SECURITY DEFINER seam only verifies the
            -- immutable reference/hash/schema/body binding; it never
            -- manufactures a payload or reads an external reason artifact.
            -- This check precedes the durable-idempotency lookup so a poisoned
            -- body cannot make an existing command appear idempotent.
            SELECT EXISTS (
                SELECT 1
                  FROM public.command_payload AS p
                 WHERE p.tenant_id = p_tenant_id
                   AND p.payload_ref = p_payload_ref
                   AND p.payload_hash = p_payload_hash
                   AND p.command_schema_version = 'cancel.v1'
                   AND p.payload = p_payload
            ) INTO payload_exists;
            IF NOT payload_exists THEN
                RETURN QUERY SELECT 'payload_conflict'::TEXT, p_command_id, p_tenant_id, p_run_id,
                    NULL::BIGINT, 'cancel'::TEXT, 'cancel.v1'::TEXT, p_command_digest,
                    p_runtime_build_hash, 'unknown'::TEXT;
                RETURN;
            END IF;

            -- An idempotent retry is checked against the durable command and
            -- payload binding before revision/state/fence CAS.  This keeps a
            -- retry stable even after later lifecycle progress reaches a
            -- BIGINT high-water mark.  Expected revision is the revision
            -- immediately before this command's sequence.
            SELECT * INTO existing_command
              FROM public.run_command AS c
             WHERE c.tenant_id = p_tenant_id AND c.command_id = p_command_id
             FOR UPDATE;
            IF FOUND THEN
                IF existing_command.run_id <> p_run_id
                   OR existing_command.command_type <> 'cancel'
                   OR existing_command.command_schema_version <> 'cancel.v1'
                   OR existing_command.command_digest <> p_command_digest
                   OR existing_command.payload_ref <> p_payload_ref
                   OR existing_command.payload_hash <> p_payload_hash
                   OR existing_command.command_seq <> p_expected_revision + 1
                   OR run_row.runtime_build_hash <> p_runtime_build_hash THEN
                    RETURN QUERY SELECT 'command_conflict'::TEXT, p_command_id, p_tenant_id, p_run_id,
                        existing_command.command_seq, existing_command.command_type,
                        existing_command.command_schema_version, existing_command.command_digest,
                        run_row.runtime_build_hash, existing_command.status;
                    RETURN;
                END IF;
                RETURN QUERY SELECT 'idempotent'::TEXT, existing_command.command_id,
                    existing_command.tenant_id, existing_command.run_id, existing_command.command_seq,
                    existing_command.command_type, existing_command.command_schema_version,
                    existing_command.command_digest, p_runtime_build_hash, existing_command.status;
                RETURN;
            END IF;

            IF run_row.revision >= 9223372036854775807 THEN
                RETURN QUERY SELECT 'revision_overflow'::TEXT, p_command_id, p_tenant_id, p_run_id,
                    NULL::BIGINT, 'cancel'::TEXT, 'cancel.v1'::TEXT, p_command_digest,
                    p_runtime_build_hash, 'unknown'::TEXT;
                RETURN;
            END IF;
            IF run_row.execution_fence >= 9223372036854775807 THEN
                RETURN QUERY SELECT 'fence_exhausted'::TEXT, p_command_id, p_tenant_id, p_run_id,
                    NULL::BIGINT, 'cancel'::TEXT, 'cancel.v1'::TEXT, p_command_digest,
                    p_runtime_build_hash, 'unknown'::TEXT;
                RETURN;
            END IF;

            IF run_row.runtime_build_hash <> p_runtime_build_hash THEN
                RETURN QUERY SELECT 'build_conflict'::TEXT, p_command_id, p_tenant_id, p_run_id,
                    NULL::BIGINT, 'cancel'::TEXT, 'cancel.v1'::TEXT, p_command_digest,
                    run_row.runtime_build_hash, 'unknown'::TEXT;
                RETURN;
            END IF;
            IF run_row.revision <> p_expected_revision THEN
                RETURN QUERY SELECT 'revision_conflict'::TEXT, p_command_id, p_tenant_id, p_run_id,
                    NULL::BIGINT, 'cancel'::TEXT, 'cancel.v1'::TEXT, p_command_digest,
                    p_runtime_build_hash, 'unknown'::TEXT;
                RETURN;
            END IF;
            IF run_row.status NOT IN (
                'accepted', 'running', 'waiting_user_input', 'waiting_action_result', 'waiting_child_result'
            ) THEN
                RETURN QUERY SELECT 'invalid_state'::TEXT, p_command_id, p_tenant_id, p_run_id,
                    NULL::BIGINT, 'cancel'::TEXT, 'cancel.v1'::TEXT, p_command_digest,
                    p_runtime_build_hash, 'unknown'::TEXT;
                RETURN;
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM public.run_command AS c
                 WHERE c.tenant_id = p_tenant_id AND c.run_id = p_run_id
                   AND c.command_seq < run_row.revision + 1
                   AND c.status IN ('pending', 'leased')
                   AND c.superseded_by_command_id IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'run contains an already superseded outstanding command' USING ERRCODE = '40001';
            END IF;

            next_command_seq := run_row.revision + 1;
            -- Create the superseding identity before attaching the foreign-key
            -- closure to older rows.  The enclosing transaction still makes
            -- the insert and every closure update one atomic acceptance.
            BEGIN
                INSERT INTO public.run_command (
                    command_id, tenant_id, run_id, principal_id, principal_kind, command_seq,
                    command_type, command_schema_version, command_digest, payload_ref, payload_hash, status
                ) VALUES (
                    p_command_id, p_tenant_id, p_run_id, run_row.principal_id, run_row.principal_kind,
                    next_command_seq, 'cancel', 'cancel.v1', p_command_digest, p_payload_ref, p_payload_hash, 'pending'
                );
            EXCEPTION WHEN unique_violation THEN
                RETURN QUERY SELECT 'command_conflict'::TEXT, p_command_id, p_tenant_id, p_run_id,
                    NULL::BIGINT, 'cancel'::TEXT, 'cancel.v1'::TEXT, p_command_digest,
                    p_runtime_build_hash, 'unknown'::TEXT;
                RETURN;
            END;
            -- The rows are already protected by the run lock.  Pending rows
            -- remain pending so the existing public status union is unchanged;
            -- the explicit superseding identity is the durable closure proof.
            UPDATE public.run_command AS older
               SET status = 'pending',
                   lease_owner = NULL,
                   lease_until = NULL,
                   execution_fence = NULL,
                   superseded_by_command_id = p_command_id,
                   superseded_by_command_seq = next_command_seq,
                   superseded_by_command_digest = p_command_digest,
                   superseded_by_provenance_hash = NULL
             WHERE older.tenant_id = p_tenant_id
               AND older.run_id = p_run_id
               AND older.command_seq < next_command_seq
               AND older.status IN ('pending', 'leased')
               AND older.superseded_by_command_id IS NULL;

            UPDATE public.agent_run AS r
               SET revision = next_command_seq,
                   execution_fence = r.execution_fence + 1,
                   lease_owner = NULL,
                   lease_until = NULL,
                   status = 'cancel_requested',
                   updated_at = clock_timestamp()
             WHERE r.tenant_id = p_tenant_id AND r.run_id = p_run_id;

            RETURN QUERY SELECT 'accepted'::TEXT, p_command_id, p_tenant_id, p_run_id,
                next_command_seq, 'cancel'::TEXT, 'cancel.v1'::TEXT, p_command_digest,
                p_runtime_build_hash, 'pending'::TEXT;
        END $$
        """
    op.execute(cancel_sql)
    op.execute(_claim_function_sql())
    op.execute(
        """
        REVOKE ALL ON FUNCTION grove_accept_cancel_run(
            TEXT, UUID, UUID, BIGINT, TEXT, TEXT, TEXT, TEXT, JSONB
        ) FROM PUBLIC, grove_api, grove_runtime, grove_governance, grove_projection
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grove_api') THEN
                GRANT EXECUTE ON FUNCTION grove_accept_cancel_run(
                    TEXT, UUID, UUID, BIGINT, TEXT, TEXT, TEXT, TEXT, JSONB
                ) TO grove_api;
            END IF;
        END $$
        """
    )


def downgrade() -> None:
    # 0005 adds irreversible cancel closure facts.  The pre-0005 schema has
    # no durable record from which to reconstruct older lease/status state, so
    # data-bearing downgrade must fail closed before any DDL is attempted.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM public.run_command
                 WHERE command_type = 'cancel' OR command_schema_version = 'cancel.v1'
            ) OR EXISTS (
                SELECT 1 FROM public.command_payload
                 WHERE command_schema_version = 'cancel.v1'
            ) THEN
                RAISE EXCEPTION
                    'ws3_cancel_acceptance downgrade blocked: cancel artifacts require operator data migration'
                    USING ERRCODE = '55000';
            END IF;
        END $$
        """
    )
    op.execute(
        """
        REVOKE ALL ON FUNCTION grove_accept_cancel_run(
            TEXT, UUID, UUID, BIGINT, TEXT, TEXT, TEXT, TEXT, JSONB
        ) FROM PUBLIC, grove_api, grove_runtime, grove_governance, grove_projection
        """
    )
    op.execute(
        """
        REVOKE ALL ON FUNCTION grove_reject_agent_run_runtime_build_rebinding() FROM PUBLIC, grove_migration
        """
    )
    op.execute("DROP TRIGGER IF EXISTS agent_run_runtime_build_guard ON agent_run")
    op.execute("DROP FUNCTION IF EXISTS grove_reject_agent_run_runtime_build_rebinding()")
    op.execute(
        """
        DROP FUNCTION IF EXISTS grove_accept_cancel_run(
            TEXT, UUID, UUID, BIGINT, TEXT, TEXT, TEXT, TEXT, JSONB
        )
        """
    )
    # Restore the exact WS-3 checkpoint-fenced claim function.  Keeping the
    # downgrade text self-contained makes upgrade→downgrade→upgrade evidence
    # compare the 0004 catalog rather than leaving a hidden 0005 definition.
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
             WHERE c.tenant_id = p_tenant_id
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
